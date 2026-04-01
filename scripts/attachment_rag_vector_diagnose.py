from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4

import psutil

from vaf.core.config import Config
from vaf.core.log_helper import get_dated_log_path
from vaf.memory.attachment_rag import (
    clear_session_attachments_sync,
    index_session_attachments_sync,
    search_session_attachments_sync,
)


def _rss_mb() -> float:
    return psutil.Process().memory_info().rss / 1024 / 1024


def _stage_metrics(name: str, samples: List[float], failures: int = 0) -> Dict[str, Any]:
    start = samples[0] if samples else 0.0
    end = samples[-1] if samples else 0.0
    peak = max(samples) if samples else 0.0
    return {
        "stage": name,
        "start_rss_mb": round(start, 2),
        "end_rss_mb": round(end, 2),
        "peak_rss_mb": round(peak, 2),
        "delta_rss_mb": round(end - start, 2),
        "peak_delta_rss_mb": round(peak - start, 2),
        "failures": int(failures),
    }


def _guarded_loop_state(max_total_delta_mb: float, max_step_delta_mb: float) -> Dict[str, Any]:
    return {
        "max_total_delta_mb": float(max_total_delta_mb),
        "max_step_delta_mb": float(max_step_delta_mb),
        "guard_triggered": False,
        "guard_reason": "",
    }


def _check_guard(samples: List[float], guard: Dict[str, Any]) -> bool:
    if len(samples) < 2:
        return False
    total_delta = samples[-1] - samples[0]
    step_delta = samples[-1] - samples[-2]
    if total_delta > guard["max_total_delta_mb"]:
        guard["guard_triggered"] = True
        guard["guard_reason"] = f"total_delta_mb={total_delta:.2f}>{guard['max_total_delta_mb']:.2f}"
        return True
    if step_delta > guard["max_step_delta_mb"]:
        guard["guard_triggered"] = True
        guard["guard_reason"] = f"step_delta_mb={step_delta:.2f}>{guard['max_step_delta_mb']:.2f}"
        return True
    return False


def run_index_only(
    iterations: int = 40,
    sleep_s: float = 0.2,
    max_total_delta_mb: float = 768.0,
    max_step_delta_mb: float = 256.0,
) -> Dict[str, Any]:
    samples: List[float] = [_rss_mb()]
    failures = 0
    guard = _guarded_loop_state(max_total_delta_mb=max_total_delta_mb, max_step_delta_mb=max_step_delta_mb)
    scope = uuid4()
    doc = {"name": "vector-doc.txt", "content": "policy text " * 350}
    for i in range(iterations):
        sid = f"diag-index-{i}-{uuid4().hex[:8]}"
        res = index_session_attachments_sync(sid, scope, [doc])
        if res.get("error"):
            failures += 1
        samples.append(_rss_mb())
        if _check_guard(samples, guard):
            break
        time.sleep(sleep_s)
    out = _stage_metrics("index_only", samples, failures=failures)
    out.update(guard)
    out["iterations_executed"] = len(samples) - 1
    return out


def run_search_only(
    iterations: int = 80,
    sleep_s: float = 0.2,
    max_total_delta_mb: float = 768.0,
    max_step_delta_mb: float = 256.0,
) -> Dict[str, Any]:
    samples: List[float] = [_rss_mb()]
    failures = 0
    guard = _guarded_loop_state(max_total_delta_mb=max_total_delta_mb, max_step_delta_mb=max_step_delta_mb)
    scope = uuid4()
    sid = f"diag-search-{uuid4().hex[:8]}"
    doc = {"name": "vector-doc.txt", "content": "policy text " * 350}
    index_res = index_session_attachments_sync(sid, scope, [doc])
    if index_res.get("error"):
        failures += 1
    hits_last = 0
    for _ in range(iterations):
        res = search_session_attachments_sync("policy text", sid, scope)
        if not isinstance(res, list):
            failures += 1
            hits_last = 0
        else:
            hits_last = len(res)
        samples.append(_rss_mb())
        if _check_guard(samples, guard):
            break
        time.sleep(sleep_s)
    clear_session_attachments_sync(sid, scope)
    out = _stage_metrics("search_only", samples, failures=failures)
    out.update(guard)
    out["iterations_executed"] = len(samples) - 1
    out["search_hits_last"] = hits_last
    return out


def run_clear_only(
    iterations: int = 50,
    sleep_s: float = 0.2,
    max_total_delta_mb: float = 768.0,
    max_step_delta_mb: float = 256.0,
) -> Dict[str, Any]:
    samples: List[float] = [_rss_mb()]
    failures = 0
    guard = _guarded_loop_state(max_total_delta_mb=max_total_delta_mb, max_step_delta_mb=max_step_delta_mb)
    scope = uuid4()
    seeded_ids: List[str] = []
    # Prepare rows to clear repeatedly with exact IDs.
    for i in range(iterations):
        sid = f"diag-clear-seed-{i}-{uuid4().hex[:8]}"
        seeded_ids.append(sid)
        index_session_attachments_sync(sid, scope, [{"name": "seed.txt", "content": "seed text " * 50}])
    for sid in seeded_ids:
        cleared = clear_session_attachments_sync(sid, scope)
        if cleared < 0:
            failures += 1
        samples.append(_rss_mb())
        if _check_guard(samples, guard):
            break
        time.sleep(sleep_s)
    out = _stage_metrics("clear_only", samples, failures=failures)
    out.update(guard)
    out["iterations_executed"] = len(samples) - 1
    return out


def run_combined(
    iterations: int = 40,
    sleep_s: float = 0.2,
    max_total_delta_mb: float = 768.0,
    max_step_delta_mb: float = 256.0,
) -> Dict[str, Any]:
    samples: List[float] = [_rss_mb()]
    failures = 0
    guard = _guarded_loop_state(max_total_delta_mb=max_total_delta_mb, max_step_delta_mb=max_step_delta_mb)
    scope = uuid4()
    doc = {"name": "combined.txt", "content": "policy text " * 350}
    for i in range(iterations):
        sid = f"diag-combined-{i}-{uuid4().hex[:8]}"
        idx = index_session_attachments_sync(sid, scope, [doc])
        _ = search_session_attachments_sync("policy text", sid, scope)
        _ = clear_session_attachments_sync(sid, scope)
        if idx.get("error"):
            failures += 1
        samples.append(_rss_mb())
        if _check_guard(samples, guard):
            break
        time.sleep(sleep_s)
    out = _stage_metrics("combined_cycle", samples, failures=failures)
    out.update(guard)
    out["iterations_executed"] = len(samples) - 1
    return out


def _tail_attachment_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    picked = [ln for ln in lines if "ATTACH_" in ln or "attachment" in ln.lower()]
    return picked[-25:]


def main() -> int:
    prev_enabled = Config.get("attachment_rag_enabled", False)
    prev_safe = Config.get("attachment_rag_safe_mode", True)
    prev_debug = Config.get("debug_logs_enabled", False)

    Config.set("attachment_rag_enabled", True)
    Config.set("attachment_rag_safe_mode", False)
    Config.set("debug_logs_enabled", True)

    rag_log_path = get_dated_log_path("rag", "log")
    summary: Dict[str, Any] = {
        "config": {
            "attachment_rag_enabled": True,
            "attachment_rag_safe_mode": False,
            "debug_logs_enabled": True,
            "rag_log_path": str(rag_log_path),
        },
        "baseline_rss_mb": round(_rss_mb(), 2),
    }

    try:
        stage = (sys.argv[1].strip().lower() if len(sys.argv) > 1 else "all")
        runners = {
            "index": run_index_only,
            "search": run_search_only,
            "clear": run_clear_only,
            "combined": run_combined,
        }
        if stage == "all":
            summary["stages"] = [runners["index"](), runners["search"](), runners["clear"](), runners["combined"]()]
        else:
            fn = runners.get(stage)
            if fn is None:
                raise ValueError("Unknown stage. Use one of: all, index, search, clear, combined")
            summary["stages"] = [fn()]
        summary["final_rss_mb"] = round(_rss_mb(), 2)
        summary["rag_log_tail"] = _tail_attachment_lines(rag_log_path)
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        Config.set("attachment_rag_enabled", prev_enabled)
        Config.set("attachment_rag_safe_mode", prev_safe)
        Config.set("debug_logs_enabled", prev_debug)


if __name__ == "__main__":
    raise SystemExit(main())
