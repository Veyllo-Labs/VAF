"""
Whare Wananga -- in-memory training job manager.

Runs the predict-then-verify runner in a background thread and exposes live status for the
UI to poll. Status is process-local (not persisted); the durable result is the
tool_knowledge record the runner writes via the store.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}  # tool -> status dict
_MAX_EVENTS = 30


def get_status(tool: str) -> Optional[Dict[str, Any]]:
    with _lock:
        s = _jobs.get(tool)
        return dict(s) if s else None


def is_running(tool: str) -> bool:
    with _lock:
        s = _jobs.get(tool)
        return bool(s and s.get("state") == "running")


def start_training(agent, tool: str, max_attempts: int = 21) -> Dict[str, Any]:
    """Start a background training pass. Returns the initial status (or already-running)."""
    with _lock:
        cur = _jobs.get(tool)
        if cur and cur.get("state") == "running":
            return {"state": "running", "tool": tool, "already": True,
                    "attempt": cur.get("attempt", 0), "max_attempts": cur.get("max_attempts", max_attempts)}
        _jobs[tool] = {
            "tool": tool, "state": "running", "attempt": 0, "max_attempts": max_attempts,
            "hits": 0, "started_at": time.time(), "events": [],
        }

    def _progress(ev: dict) -> None:
        with _lock:
            s = _jobs.get(tool)
            if not s:
                return
            if ev.get("event") == "attempt":
                s["attempt"] = ev.get("i", s.get("attempt", 0))
                s["hits"] = ev.get("hits", s.get("hits", 0))
                s.setdefault("events", []).append({
                    "i": ev.get("i"),
                    "match": ev.get("match"),
                    "predicted_outcome": ev.get("predicted_outcome"),
                    "actual_outcome": ev.get("actual_outcome"),
                })
                if len(s["events"]) > _MAX_EVENTS:
                    s["events"] = s["events"][-_MAX_EVENTS:]

    def _run() -> None:
        from vaf.whare_wananga import runner
        try:
            summary = runner.train_tool(agent, tool, max_attempts=max_attempts, progress=_progress)
            with _lock:
                s = _jobs.get(tool) or {}
                if summary.get("skipped"):
                    s.update({"state": "skipped", "reason": summary.get("reason", "")})
                elif summary.get("ok"):
                    s.update({"state": "done", "summary": summary,
                              "status": summary.get("status"), "confidence": summary.get("confidence"),
                              "hits": summary.get("hits", s.get("hits", 0))})
                else:
                    s.update({"state": "error", "error": summary.get("error", "failed")})
                s["ended_at"] = time.time()
        except Exception as e:
            with _lock:
                s = _jobs.get(tool) or {}
                s.update({"state": "error", "error": str(e), "ended_at": time.time()})

    threading.Thread(target=_run, name=f"ww-train-{tool}", daemon=True).start()
    return {"state": "running", "tool": tool, "attempt": 0, "max_attempts": max_attempts}
