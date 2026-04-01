from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import psutil

from vaf.core.config import Config


def _worker_loop() -> int:
    from vaf.memory.attachment_rag import (
        clear_session_attachments_sync,
        index_session_attachments_sync,
        search_session_attachments_sync,
    )

    scope = uuid4()
    doc = {"name": "supervised.txt", "content": "policy text " * 350}
    session_id = "supervised-session"
    i = 0
    while True:
        i += 1
        # Use one stable session_id to exercise coalescing/single-flight behavior
        # under rapid attachment updates in the same chat session.
        index_session_attachments_sync(session_id, scope, [doc])
        search_session_attachments_sync("policy text", session_id, scope)
        clear_session_attachments_sync(session_id, scope)
        time.sleep(0.05)


def _total_rss_mb(proc: psutil.Process) -> float:
    total = 0
    try:
        total += proc.memory_info().rss
        for ch in proc.children(recursive=True):
            try:
                total += ch.memory_info().rss
            except Exception:
                pass
    except Exception:
        return 0.0
    return total / 1024 / 1024


def _supervise_once(max_seconds: float = 5.0, max_rss_mb: float = 2048.0) -> dict:
    script = Path(__file__).resolve()
    cmd = [sys.executable, str(script), "--worker"]
    child = subprocess.Popen(cmd)
    ps = psutil.Process(child.pid)

    start = time.time()
    peak_mb = 0.0
    kill_reason = ""
    killed = False

    while True:
        if child.poll() is not None:
            break
        elapsed = time.time() - start
        rss_mb = _total_rss_mb(ps)
        peak_mb = max(peak_mb, rss_mb)

        if rss_mb >= max_rss_mb:
            kill_reason = f"rss_limit_exceeded ({rss_mb:.2f}MB >= {max_rss_mb:.2f}MB)"
            killed = True
            break
        if elapsed >= max_seconds:
            kill_reason = f"time_limit_reached ({elapsed:.2f}s >= {max_seconds:.2f}s)"
            killed = True
            break
        time.sleep(0.1)

    if killed and child.poll() is None:
        try:
            child.kill()
        except Exception:
            pass

    exit_code = child.wait(timeout=10)
    return {
        "max_seconds": max_seconds,
        "max_rss_mb": max_rss_mb,
        "elapsed_s": round(time.time() - start, 2),
        "peak_rss_mb": round(peak_mb, 2),
        "killed": killed,
        "kill_reason": kill_reason,
        "child_exit_code": exit_code,
    }


def main() -> int:
    if "--worker" in sys.argv:
        return _worker_loop()

    max_seconds = 5.0
    max_rss_mb = 2048.0
    if len(sys.argv) >= 2:
        try:
            max_seconds = float(sys.argv[1])
        except Exception:
            pass
    if len(sys.argv) >= 3:
        try:
            max_rss_mb = float(sys.argv[2])
        except Exception:
            pass

    prev_enabled = Config.get("attachment_rag_enabled", False)
    prev_safe_mode = Config.get("attachment_rag_safe_mode", True)
    prev_debug = Config.get("debug_logs_enabled", False)

    Config.set("attachment_rag_enabled", True)
    Config.set("attachment_rag_safe_mode", False)
    Config.set("debug_logs_enabled", True)

    try:
        result = _supervise_once(max_seconds=max_seconds, max_rss_mb=max_rss_mb)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        Config.set("attachment_rag_enabled", prev_enabled)
        Config.set("attachment_rag_safe_mode", prev_safe_mode)
        Config.set("debug_logs_enabled", prev_debug)


if __name__ == "__main__":
    raise SystemExit(main())
