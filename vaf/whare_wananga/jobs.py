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
_MAX_EVENTS = 40


def get_status(tool: str) -> Optional[Dict[str, Any]]:
    with _lock:
        s = _jobs.get(tool)
        return dict(s) if s else None


def is_running(tool: str) -> bool:
    with _lock:
        s = _jobs.get(tool)
        return bool(s and s.get("state") == "running")


def start_training(agent, tool: str) -> Dict[str, Any]:
    """Start a background training pass. Returns the initial status (or already-running)."""
    with _lock:
        cur = _jobs.get(tool)
        if cur and cur.get("state") == "running":
            return {"state": "running", "tool": tool, "already": True,
                    "attempt": cur.get("attempt", 0), "phase": cur.get("phase")}
        _jobs[tool] = {
            "tool": tool, "state": "running", "attempt": 0, "hits": 0, "fails": 0,
            "phase": "learn", "round": 0, "max_rounds": 0,
            "validate": None, "started_at": time.time(), "events": [],
        }

    def _progress(ev: dict) -> None:
        with _lock:
            s = _jobs.get(tool)
            if not s:
                return
            etype = ev.get("event")
            if etype == "start":
                s["max_rounds"] = ev.get("max_rounds", s.get("max_rounds", 0))
                s["validate_n"] = ev.get("validate_n")
                s["refine_n"] = ev.get("refine_n")
            elif etype == "attempt":
                s["attempt"] = ev.get("i", s.get("attempt", 0))
                s["hits"] = ev.get("hits", s.get("hits", 0))
                s["phase"] = ev.get("phase", s.get("phase"))
                if ev.get("actual_outcome") == "error":
                    s["fails"] = (s.get("fails", 0) or 0) + 1
                s.setdefault("events", []).append({
                    "i": ev.get("i"),
                    "match": ev.get("match"),
                    "phase": ev.get("phase"),
                    "predicted_outcome": ev.get("predicted_outcome"),
                    "actual_outcome": ev.get("actual_outcome"),
                    "verdict": ev.get("verdict"),
                    "reason": ev.get("reason"),
                    "intent": ev.get("intent"),
                    "actual": ev.get("actual"),
                })
                if len(s["events"]) > _MAX_EVENTS:
                    s["events"] = s["events"][-_MAX_EVENTS:]
            elif etype == "validate_start":
                s["phase"] = "validate"
                s["round"] = ev.get("round", s.get("round", 0))
            elif etype == "validate_result":
                s["validate"] = {"round": ev.get("round"), "hits": ev.get("hits"), "n": ev.get("n")}
            elif etype == "challenge_start":
                s["phase"] = "challenge"
                s["challenge"] = {"need": ev.get("need"), "max_fails": ev.get("max_fails"),
                                  "round_pass": 0, "round_fail": 0, "total_fails": 0, "passed": False}
            elif etype == "challenge_round":
                s["challenge"] = {**(s.get("challenge") or {}),
                                  "round_pass": ev.get("round_pass"), "round_fail": ev.get("round_fail"),
                                  "total_fails": ev.get("total_fails"), "passed": ev.get("passed")}
            elif etype == "challenge_result":
                s["challenge"] = {**(s.get("challenge") or {}),
                                  "passed": ev.get("passed"), "total_fails": ev.get("total_fails")}
            elif etype == "halt":
                s["halt_reason"] = ev.get("reason")

    def _run() -> None:
        from vaf.whare_wananga import runner
        try:
            summary = runner.train_tool(agent, tool, progress=_progress)
            with _lock:
                s = _jobs.get(tool) or {}
                if summary.get("skipped"):
                    s.update({"state": "skipped", "reason": summary.get("reason", "")})
                elif summary.get("ok"):
                    s.update({"state": "done", "summary": summary,
                              "status": summary.get("status"), "confidence": summary.get("confidence"),
                              "confirmed": summary.get("confirmed"), "rounds": summary.get("rounds"),
                              "challenge_passed": summary.get("challenge_passed"),
                              "challenge_fails": summary.get("challenge_fails"),
                              "halted": summary.get("halted"),
                              "hits": summary.get("hits", s.get("hits", 0))})
                else:
                    s.update({"state": "error", "error": summary.get("error", "failed")})
                s["ended_at"] = time.time()
        except Exception as e:
            with _lock:
                s = _jobs.get(tool) or {}
                s.update({"state": "error", "error": str(e), "ended_at": time.time()})

    threading.Thread(target=_run, name=f"ww-train-{tool}", daemon=True).start()
    return {"state": "running", "tool": tool, "attempt": 0, "phase": "learn"}
