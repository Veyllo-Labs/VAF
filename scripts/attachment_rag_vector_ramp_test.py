from __future__ import annotations

import time
from uuid import uuid4

import psutil

from vaf.core.config import Config
from vaf.memory.attachment_rag import (
    clear_session_attachments_sync,
    index_session_attachments_sync,
    search_session_attachments_sync,
)


def rss_mb() -> float:
    return psutil.Process().memory_info().rss / 1024 / 1024


def run_stage(duration_s: int, limit_mb: float) -> dict:
    scope = uuid4()
    start = rss_mb()
    peak = start
    deadline = time.time() + duration_s
    i = 0
    stopped_by_guard = False

    while time.time() < deadline:
        i += 1
        sid = f"ramp-{duration_s}s-{uuid4().hex[:8]}"
        doc = {"name": "ramp.txt", "content": "policy text " * 350}

        _ = index_session_attachments_sync(sid, scope, [doc])
        _ = search_session_attachments_sync("policy text", sid, scope)
        _ = clear_session_attachments_sync(sid, scope)

        current = rss_mb()
        if current > peak:
            peak = current
        if current >= limit_mb:
            stopped_by_guard = True
            break

        time.sleep(0.2)

    end = rss_mb()
    return {
        "duration_s": duration_s,
        "iterations": i,
        "start_rss_mb": round(start, 2),
        "peak_rss_mb": round(peak, 2),
        "end_rss_mb": round(end, 2),
        "delta_rss_mb": round(end - start, 2),
        "guard_limit_mb": limit_mb,
        "stopped_by_guard": stopped_by_guard,
    }


def main() -> int:
    limit_mb = 2048.0
    stages = [5, 10, 20, 30]
    prev_enabled = Config.get("attachment_rag_enabled", False)
    prev_safe_mode = Config.get("attachment_rag_safe_mode", True)

    Config.set("attachment_rag_enabled", True)
    Config.set("attachment_rag_safe_mode", False)

    try:
        print(f"[ramp] guard_limit_mb={limit_mb}")
        for seconds in stages:
            result = run_stage(seconds, limit_mb=limit_mb)
            print(f"[stage {seconds}s] {result}")
            if result["stopped_by_guard"]:
                print("[ramp] STOP: guard limit reached.")
                return 2
        print("[ramp] PASS: all stages completed under guard limit.")
        return 0
    finally:
        Config.set("attachment_rag_enabled", prev_enabled)
        Config.set("attachment_rag_safe_mode", prev_safe_mode)


if __name__ == "__main__":
    raise SystemExit(main())
