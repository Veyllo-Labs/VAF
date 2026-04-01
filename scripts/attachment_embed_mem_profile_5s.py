from __future__ import annotations

import json
import time
import tracemalloc
from typing import Any, Dict, List

import psutil

from vaf.memory import embeddings as emb_mod
from vaf.memory.embeddings import get_embedding_service


def _rss_mb() -> float:
    return psutil.Process().memory_info().rss / 1024 / 1024


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
    svc = get_embedding_service()
    svc.clear_cache()
    # Warm-up once so profiling captures steady-state growth (not import/model load burst).
    _ = svc.embed_sync("warmup attachment profile " + ("policy text " * 200))
    svc.clear_cache()

    tracemalloc.start(25)
    s0 = tracemalloc.take_snapshot()
    r0 = _rss_mb()
    peak = r0
    t0 = time.time()
    i = 0
    samples: List[Dict[str, Any]] = []

    while time.time() - t0 < 5.0:
        i += 1
        # Unique text to bypass cache and reveal allocation pressure.
        text = f"attachment profile sample {i} :: " + ("policy text " * 250)
        _ = svc.embed_sync(text)
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
                    "model_name": str(getattr(emb_mod, "_model_name", "") or ""),
                }
            )

    s1 = tracemalloc.take_snapshot()
    r1 = _rss_mb()
    report = {
        "warmup_done": True,
        "duration_s": round(time.time() - t0, 2),
        "iterations": i,
        "start_rss_mb": round(r0, 2),
        "peak_rss_mb": round(peak, 2),
        "end_rss_mb": round(r1, 2),
        "rss_delta_mb": round(r1 - r0, 2),
        "cache_len_final": len(getattr(svc, "_cache_keys", []) or []),
        "python_top_allocations": _top_diff(s0, s1),
        "samples": samples[-12:],
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
