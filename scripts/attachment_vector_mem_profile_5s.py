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


def _top_diff(before: tracemalloc.Snapshot, after: tracemalloc.Snapshot, limit: int = 14) -> List[Dict[str, Any]]:
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
        "attachment_rag_vector_max_index_ops_per_window": Config.get("attachment_rag_vector_max_index_ops_per_window", 1),
        "attachment_rag_vector_max_search_ops_per_window": Config.get("attachment_rag_vector_max_search_ops_per_window", 4),
    }
    Config.set("attachment_rag_enabled", True)
    Config.set("attachment_rag_safe_mode", False)
    Config.set("attachment_rag_vector_max_index_ops_per_window", 500)
    Config.set("attachment_rag_vector_max_search_ops_per_window", 1000)

    # Warm up model once so snapshots focus on runtime growth.
    svc = get_embedding_service()
    _ = svc.embed_sync("vector warmup " + ("policy text " * 200))
    svc.clear_cache()

    tracemalloc.start(25)
    s0 = tracemalloc.take_snapshot()
    r0 = _rss_mb()
    peak = r0
    t0 = time.time()
    i = 0
    errors: List[str] = []
    samples: List[Dict[str, Any]] = []
    scope = uuid4()

    try:
        while time.time() - t0 < 5.0:
            i += 1
            sid = f"vprof-{i}-{uuid4().hex[:8]}"
            doc = {"name": "vprof.txt", "content": "policy text " * 350}
            try:
                idx = index_session_attachments_sync(sid, scope, [doc])
                _ = search_session_attachments_sync("policy text", sid, scope)
                _ = clear_session_attachments_sync(sid, scope)
                if isinstance(idx, dict) and idx.get("error"):
                    errors.append(str(idx.get("error")))
            except Exception as e:
                errors.append(str(e))
                break

            rss = _rss_mb()
            peak = max(peak, rss)
            if i % 2 == 0:
                samples.append(
                    {
                        "iter": i,
                        "elapsed_s": round(time.time() - t0, 2),
                        "rss_mb": round(rss, 2),
                        "cache_len": len(getattr(svc, "_cache_keys", []) or []),
                        "model_loaded": bool(getattr(emb_mod, "_model", None) is not None),
                    }
                )

        s1 = tracemalloc.take_snapshot()
        r1 = _rss_mb()
        report = {
            "duration_s": round(time.time() - t0, 2),
            "iterations": i,
            "start_rss_mb": round(r0, 2),
            "peak_rss_mb": round(peak, 2),
            "end_rss_mb": round(r1, 2),
            "rss_delta_mb": round(r1 - r0, 2),
            "cache_len_final": len(getattr(svc, "_cache_keys", []) or []),
            "errors": errors[-8:],
            "python_top_allocations": _top_diff(s0, s1),
            "samples": samples[-12:],
        }
        print(json.dumps(report, indent=2))
        return 0
    finally:
        Config.set("attachment_rag_enabled", prev["attachment_rag_enabled"])
        Config.set("attachment_rag_safe_mode", prev["attachment_rag_safe_mode"])
        Config.set("attachment_rag_vector_max_index_ops_per_window", prev["attachment_rag_vector_max_index_ops_per_window"])
        Config.set("attachment_rag_vector_max_search_ops_per_window", prev["attachment_rag_vector_max_search_ops_per_window"])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        import traceback

        print(json.dumps({"fatal_error": str(e), "traceback": traceback.format_exc()}, indent=2))
        raise
