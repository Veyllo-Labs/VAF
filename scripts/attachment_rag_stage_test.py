import os
import time
from uuid import uuid4

import psutil

from vaf.core.config import Config
from vaf.memory.attachment_rag import (
    clear_session_attachments_sync,
    index_session_attachments_sync,
    search_session_attachments_sync,
)


def run_stage(name: str, seconds: int, max_delta_mb: int = 256, max_step_mb: int = 128) -> bool:
    proc = psutil.Process(os.getpid())
    start = proc.memory_info().rss
    prev = start
    deadline = time.time() + seconds
    scope = uuid4()
    i = 0
    print(f"[{name}] start_rss_mb={start / 1024 / 1024:.2f}")

    while time.time() < deadline:
        i += 1
        sid = f"stage-{name}-{uuid4().hex[:8]}"
        docs = [{"name": "doc.txt", "content": "policy text " * 350}]

        index_session_attachments_sync(sid, scope, docs)
        search_session_attachments_sync("policy", sid, scope)
        clear_session_attachments_sync(sid, scope)

        cur = proc.memory_info().rss
        delta_mb = (cur - start) / 1024 / 1024
        step_mb = (cur - prev) / 1024 / 1024
        prev = cur

        if i % 5 == 0:
            print(f"[{name}] iter={i} rss_mb={cur / 1024 / 1024:.2f} delta_mb={delta_mb:.2f} step_mb={step_mb:.2f}")

        if delta_mb > max_delta_mb:
            print(f"[{name}] FAIL delta_guard exceeded: {delta_mb:.2f}MB > {max_delta_mb}MB")
            return False
        if step_mb > max_step_mb:
            print(f"[{name}] FAIL step_guard exceeded: {step_mb:.2f}MB > {max_step_mb}MB")
            return False

        time.sleep(1)

    end = proc.memory_info().rss
    print(f"[{name}] PASS end_rss_mb={end / 1024 / 1024:.2f} delta_mb={(end - start) / 1024 / 1024:.2f}")
    return True


def main() -> int:
    prev_enabled = bool(Config.get("attachment_rag_enabled", False))
    print(f"[config] previous attachment_rag_enabled={prev_enabled}")
    Config.set("attachment_rag_enabled", True)
    print("[config] attachment_rag_enabled=True for staged test")

    try:
        # Stage 1 only (2 minutes). Run longer stages only if this is stable.
        ok = run_stage("2min", 120)
        return 0 if ok else 2
    finally:
        Config.set("attachment_rag_enabled", prev_enabled)
        print(f"[config] restored attachment_rag_enabled={prev_enabled}")


if __name__ == "__main__":
    raise SystemExit(main())
