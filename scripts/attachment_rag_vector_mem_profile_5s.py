from __future__ import annotations

import json
import time
import tracemalloc
from typing import Any, Dict, List
from uuid import uuid4

import psutil

from vaf.core.config import Config
from vaf.memory import embeddings as emb_mod
from vaf.memory.attachment_rag import (
    clear_session_attachments_sync,
    index_session_attachments_sync,
    search_session_attachments_sync,
)
from vaf.memory.embeddings import get_embedding_service


def _rss_mb() -> float:
    return psutil.Process().memory_info().rss / 1024 / 1024


def _cache_state() -> Dict[str, Any]:
    svc = get_embedding_service()
    cache_len = len(getattr(svc, "_cache_keys", []) or [])
    return {
        "cache_len": cache_len,
        "model_loaded": bool(getattr(emb_mod, "_model", None) is not None),
        "model_name": str(getattr(emb_mod, "_model_name", "") or ""),
    }


def _top_diff(before: tracemalloc.Snapshot, after: tracemalloc.Snapshot, limit: int = 12) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for stat in after.compare_to(before, "lineno")[:limit]:
        tb = stat.traceback.format()
        out.append(
            {
                "size_kib": round(stat.size_diff / 1024, 2),
                "count_diff": int(stat.count_diff),
                "trace": tb[0] if tb else "n/a",
            }
        )
    return out


def main() -> int:
    prev = {
        "attachment_rag_enabled": Config.get("attachment_rag_enabled", False),
        "attachment_rag_safe_mode": Config.get("attachment_rag_safe_mode", True),
        "attachment_rag_vector_max_index_ops_per_window": Config.get(
            "attachment_rag_vector_max_index_ops_per_window", 1
        ),
        "attachment_rag_vector_max_search_ops_per_window": Config.get(
            "attachment_rag_vector_max_search_ops_per_window", 4
        ),
    }

    # Profile real vector behavior without safe-mode and without backpressure masking.
    Config.set("attachment_rag_enabled", True)
    Config.set("attachment_rag_safe_mode", False)
    Config.set("attachment_rag_vector_max_index_ops_per_window", 500)
    Config.set("attachment_rag_vector_max_search_ops_per_window", 1000)

    tracemalloc.start(25)
    baseline = tracemalloc.take_snapshot()
    start_rss = _rss_mb()
    peak_rss = start_rss
    scope = uuid4()
    t0 = time.time()
    i = 0
    samples: List[Dict[str, Any]] = []

    try:
        while time.time() - t0 < 5.0:
            i += 1
            sid = f"profile-{i}-{uuid4().hex[:8]}"
            doc = {"name": "profile.txt", "content": "policy text " * 350}
            index_session_attachments_sync(sid, scope, [doc])
            search_session_attachments_sync("policy text", sid, scope)
            clear_session_attachments_sync(sid, scope)

            rss = _rss_mb()
            peak_rss = max(peak_rss, rss)
            if i % 3 == 0:
                samples.append(
                    {
                        "iter": i,
                        "elapsed_s": round(time.time() - t0, 2),
                        "rss_mb": round(rss, 2),
                        **_cache_state(),
                    }
                )
        final = tracemalloc.take_snapshot()
        end_rss = _rss_mb()
        report = {
            "duration_s": round(time.time() - t0, 2),
            "iterations": i,
            "start_rss_mb": round(start_rss, 2),
            "peak_rss_mb": round(peak_rss, 2),
            "end_rss_mb": round(end_rss, 2),
            "rss_delta_mb": round(end_rss - start_rss, 2),
            "cache_state_final": _cache_state(),
            "python_top_allocations": _top_diff(baseline, final),
            "samples": samples[-12:],
        }
        print(json.dumps(report, indent=2))
        return 0
    finally:
        Config.set("attachment_rag_enabled", prev["attachment_rag_enabled"])
        Config.set("attachment_rag_safe_mode", prev["attachment_rag_safe_mode"])
        Config.set(
            "attachment_rag_vector_max_index_ops_per_window",
            prev["attachment_rag_vector_max_index_ops_per_window"],
        )
        Config.set(
            "attachment_rag_vector_max_search_ops_per_window",
            prev["attachment_rag_vector_max_search_ops_per_window"],
        )


if __name__ == "__main__":
    raise SystemExit(main())
