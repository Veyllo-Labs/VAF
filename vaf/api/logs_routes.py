# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Admin-only API for reading debug log files from the VAF log directory.

GET /api/logs                → list available log files (admin only)
GET /api/logs/{fname}        → tail a specific log file  (admin only)
GET /api/logs/timeline/dates → list available timeline dates (admin only)
GET /api/logs/timeline/events→ return merged timeline events for a date (admin only)
"""
import hashlib
import json
import re
from datetime import datetime as _dt, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from vaf.api.user_routes import require_admin
from vaf.core.log_helper import get_app_log_dir

router = APIRouter(prefix="/api/logs", tags=["logs"])

_DATE_RE = re.compile(r"^(.+?)_(\d{4}-\d{2}-\d{2})$")


def _describe_files() -> List[Dict[str, Any]]:
    log_dir = get_app_log_dir()
    files: List[Dict[str, Any]] = []
    try:
        for p in sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True):
            stat = p.stat()
            m = _DATE_RE.match(p.stem)
            domain = m.group(1) if m else p.stem
            date = m.group(2) if m else ""
            files.append({
                "filename": p.name,
                "domain": domain,
                "date": date,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            })
    except Exception:
        pass
    return files


@router.get("")
async def list_logs(_: Dict[str, Any] = Depends(require_admin)):
    """Return metadata for all log files in the log directory."""
    return {"files": _describe_files()}


@router.get("/{filename}")
async def read_log(
    filename: str,
    tail: int = Query(default=500, ge=1, le=10000),
    _: Dict[str, Any] = Depends(require_admin),
):
    """Return last `tail` lines of a specific log file."""
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not filename.endswith(".log"):
        raise HTTPException(status_code=400, detail="Only .log files are accessible")

    log_dir = get_app_log_dir().resolve()
    path = (log_dir / filename).resolve()
    # Ensure the resolved path stays inside the log directory (guards symlinks)
    if path.parent != log_dir:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Log file not found")

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail_lines = [ln.rstrip("\n") for ln in all_lines[-tail:]]
        return {"filename": filename, "total_lines": len(all_lines), "lines": tail_lines}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read log: {exc}")


# ── Timeline endpoints ────────────────────────────────────────────────────────

_JSONL_DATE_RE = re.compile(r"^timeline_(\d{4}-\d{2}-\d{2})\.jsonl$")


def _verify_chain(events: List[Dict[str, Any]]) -> bool:
    """Return True if the hash chain across all events is intact."""
    for i, ev in enumerate(events):
        expected_prev = "GENESIS" if i == 0 else events[i - 1].get("hash", "")
        if ev.get("prev_hash") != expected_prev:
            return False
        payload = {k: v for k, v in ev.items() if k != "hash"}
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        computed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if ev.get("hash") != computed:
            return False
    return True


def _merge_timeline(raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Merge paired events:
    - tool_start + tool_end by call_id
    - subagent_start + subagent_end by task_id
    Unmatched starts are kept with status='running'; orphan ends are discarded.
    thinking_run and other events are passed through as-is.
    """
    tool_starts: Dict[str, Dict[str, Any]] = {}
    subagent_starts: Dict[str, Dict[str, Any]] = {}
    ww_starts: Dict[str, Dict[str, Any]] = {}
    merged: List[Dict[str, Any]] = []
    for ev in raw_events:
        etype = ev.get("type")
        if etype == "tool_start":
            tool_starts[ev.get("call_id", "")] = ev
        elif etype == "tool_end":
            start = tool_starts.pop(ev.get("call_id", ""), None)
            if start:
                merged.append({**start, "status": ev.get("status"), "duration_s": ev.get("duration_s"), "result": ev.get("result")})
            # orphan end → discard
        elif etype == "subagent_start":
            subagent_starts[ev.get("task_id", "")] = ev
        elif etype == "subagent_end":
            start = subagent_starts.pop(ev.get("task_id", ""), None)
            if start:
                merged.append({**start, "status": ev.get("status"), "duration_s": ev.get("duration_s"), "ended_at": ev.get("ts")})
            # orphan end → discard
        elif etype == "ww_train_start":
            ww_starts[ev.get("run_id", "")] = ev
        elif etype == "ww_train_end":
            start = ww_starts.pop(ev.get("run_id", ""), None)
            if start:
                merged.append({**start, "status": ev.get("status"), "duration_s": ev.get("duration_s"),
                               "result": ev.get("result"), "confirmed": ev.get("confirmed"),
                               "challenge_passed": ev.get("challenge_passed"), "confidence": ev.get("confidence"),
                               "mode": ev.get("mode"), "ended_at": ev.get("ts")})
            # orphan end → discard
        else:
            # thinking_run, unknown — pass through
            merged.append(ev)
    # Remaining starts without a matching end (still running or lost)
    for ev in tool_starts.values():
        merged.append({**ev, "status": "running"})
    for ev in subagent_starts.values():
        merged.append({**ev, "status": "running"})
    for ev in ww_starts.values():
        merged.append({**ev, "status": "running"})
    # Re-sort by timestamp
    merged.sort(key=lambda e: e.get("ts", ""))
    return merged


@router.get("/timeline/dates")
async def list_timeline_dates(_: Dict[str, Any] = Depends(require_admin)):
    """Return list of dates for which timeline JSONL files exist."""
    log_dir = get_app_log_dir()
    dates: List[str] = []
    try:
        for p in sorted(log_dir.glob("timeline_*.jsonl"), key=lambda f: f.name, reverse=True):
            m = _JSONL_DATE_RE.match(p.name)
            if m:
                dates.append(m.group(1))
    except Exception:
        pass
    return {"dates": dates}


@router.get("/timeline/events")
async def get_timeline_events(
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD; omit for today"),
    merge: bool = Query(default=True, description="Merge tool_start+tool_end pairs"),
    _: Dict[str, Any] = Depends(require_admin),
):
    """Return timeline events for a given date with hash-chain verification."""
    from datetime import datetime as _dt
    if date is None:
        date = _dt.now().strftime("%Y-%m-%d")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=400, detail="Invalid date format")

    log_dir = get_app_log_dir().resolve()
    path = (log_dir / f"timeline_{date}.jsonl").resolve()
    if path.parent != log_dir:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists():
        return {"date": date, "events": [], "chain_ok": True, "total_raw": 0}

    try:
        raw_events: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        raw_events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        chain_ok = _verify_chain(raw_events)
        events = _merge_timeline(raw_events) if merge else raw_events
        return {"date": date, "events": events, "chain_ok": chain_ok, "total_raw": len(raw_events)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read timeline: {exc}")


# ── Log context endpoint ──────────────────────────────────────────────────────

# Timestamp anywhere in line (not anchored to start — some logs have prefix before ts)
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T[\d:.]+)")

# Skip files larger than this to keep response fast
_MAX_LOG_BYTES = 2 * 1024 * 1024  # 2 MB


def _read_think_block(log_dir, date_str: str, run_id: str) -> List[Dict[str, Any]]:
    """Extract the full multi-line THINKING RUN block that contains run_id."""
    path = log_dir / f"vaf_think_{date_str}.log"
    if not path.exists():
        return []
    results = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        i = 0
        while i < len(lines):
            # Find block start
            if lines[i].startswith("=") and i + 1 < len(lines):
                block_start = i
                block_lines = []
                i += 1
                # Collect until next separator or EOF
                while i < len(lines) and not lines[i].startswith("="):
                    block_lines.append(lines[i].rstrip("\n"))
                    i += 1
                # Check if this block contains our run_id
                block_text = "\n".join(block_lines)
                if run_id in block_text:
                    # Extract timestamp from [THINKING RUN] line
                    ts_found = ""
                    for bl in block_lines[:3]:
                        m = _TS_RE.search(bl)
                        if m:
                            ts_found = m.group(1)
                            break
                    results.append({
                        "file": f"vaf_think_{date_str}.log",
                        "ts": ts_found,
                        "line": block_text,
                        "block": True,
                    })
            else:
                i += 1
    except Exception:
        pass
    return results


def _read_tool_use_lines(log_dir, date_str: str, tool: str, session: str,
                         t_start, t_end) -> List[Dict[str, Any]]:
    """Return tool_use log lines for a specific tool+session within a time window."""
    path = log_dir / f"tool_use_{date_str}.log"
    if not path.exists():
        return []
    results = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if not line:
                    continue
                # Quick filter before parsing
                if tool and tool not in line:
                    continue
                m = _TS_RE.search(line)
                if not m:
                    continue
                try:
                    lt = _dt.fromisoformat(m.group(1))
                    if t_start <= lt <= t_end:
                        results.append({"file": f"tool_use_{date_str}.log", "ts": m.group(1), "line": line})
                except Exception:
                    pass
    except Exception:
        pass
    return results


@router.get("/timeline/context")
async def get_log_context(
    ts: str = Query(..., description="ISO timestamp of the event"),
    ev_type: str = Query(default="", alias="type", description="Event type: thinking_run, tool_end, ..."),
    run_id: Optional[str] = Query(default=None),
    call_id: Optional[str] = Query(default=None),
    tool: Optional[str] = Query(default=None),
    session: Optional[str] = Query(default=None),
    window_s: int = Query(default=30, ge=5, le=120),
    _: Dict[str, Any] = Depends(require_admin),
):
    """
    Return event-specific log entries:
    - thinking_run → full block from vaf_think log matching run_id
    - tool_* → lines from tool_use log matching tool+session, plus JSONL entry
    Always includes the matching JSONL timeline entry for full context.
    """
    try:
        event_time = _dt.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid timestamp")

    t_start  = event_time - timedelta(seconds=window_s)
    t_end    = event_time + timedelta(seconds=window_s)
    date_str = event_time.strftime("%Y-%m-%d")

    log_dir = get_app_log_dir().resolve()
    results: List[Dict[str, Any]] = []

    # ── 1. JSONL timeline entry — always include (gives call_id, session, scope, duration…)
    jsonl_path = (log_dir / f"timeline_{date_str}.jsonl").resolve()
    if jsonl_path.parent == log_dir and jsonl_path.exists():
        try:
            with open(jsonl_path, "r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                        match = (
                            (call_id and ev.get("call_id") == call_id) or
                            (run_id  and ev.get("run_id")  == run_id)  or
                            (not call_id and not run_id and
                             abs((_dt.fromisoformat(ev.get("ts","").replace("Z","")).replace(tzinfo=None) - event_time).total_seconds()) < 1)
                        )
                        if match:
                            results.append({
                                "file": f"timeline.jsonl",
                                "ts": ev.get("ts",""),
                                "line": json.dumps(ev, ensure_ascii=False, indent=2),
                                "block": True,
                            })
                    except Exception:
                        pass
        except Exception:
            pass

    # ── 2. Type-specific log source ───────────────────────────────────────────
    if ev_type == "thinking_run" and run_id:
        results += _read_think_block(log_dir, date_str, run_id)
    elif ev_type in ("tool_end", "tool_start") and tool:
        results += _read_tool_use_lines(log_dir, date_str, tool, session or "", t_start, t_end)

    results.sort(key=lambda r: r.get("ts", ""))
    return {"ts": ts, "type": ev_type, "total": len(results), "lines": results}
