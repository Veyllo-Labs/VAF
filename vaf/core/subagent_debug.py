from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Dict

from vaf.core.platform import Platform


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _preview(text: str, max_chars: int = 500) -> str:
    t = str(text or "")
    if len(t) <= max_chars:
        return t
    return t[:max_chars] + f"... [truncated {len(t)} chars]"


def _sha256_text(text: str) -> str:
    h = hashlib.sha256()
    h.update((text or "").encode("utf-8", errors="replace"))
    return h.hexdigest()


def _repo_root_from_file() -> Optional[Path]:
    """
    Best-effort: resolve repo root from installed package location.
    In this repo layout: <root>/vaf/core/subagent_debug.py -> parents[2] == <root>
    """
    try:
        p = Path(__file__).resolve()
        # .../<root>/vaf/core/subagent_debug.py
        root = p.parents[2]
        # Sanity: ensure it looks like the repo
        if (root / "vaf").is_dir() and (root / "logs").exists():
            return root
        if (root / "vaf").is_dir() and (root / "README.md").exists():
            return root
    except Exception:
        pass
    return None


_RETENTION_DAYS = 14
_retention_done = False


def _sweep_old_run_dirs(root: Path) -> None:
    """Best-effort retention: drop run dirs older than _RETENTION_DAYS.

    Every run persists telemetry now (not only IPC-spawned ones), so the debug
    tree would grow without bound otherwise. Runs at most once per process.
    """
    global _retention_done
    if _retention_done:
        return
    _retention_done = True
    cutoff = time.time() - _RETENTION_DAYS * 86400
    try:
        import shutil
        for agent_dir in root.iterdir():
            if not agent_dir.is_dir():
                continue
            for run_dir in agent_dir.iterdir():
                try:
                    if run_dir.is_dir() and run_dir.stat().st_mtime < cutoff:
                        shutil.rmtree(run_dir, ignore_errors=True)
                except Exception:
                    continue
    except Exception:
        pass


def get_debug_root_dir() -> Path:
    """
    Prefer repo-local logs/debug (as requested). Fallback to ~/.vaf/logs/debug if not writable.
    """
    repo = _repo_root_from_file()
    if repo:
        candidate = repo / "logs" / "debug"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            _sweep_old_run_dirs(candidate)
            return candidate
        except Exception:
            pass

    fallback = Platform.vaf_dir() / "logs" / "debug"
    fallback.mkdir(parents=True, exist_ok=True)
    _sweep_old_run_dirs(fallback)
    return fallback


def summarize_result(result: Any) -> Dict[str, Any]:
    s = "" if result is None else str(result)
    return {
        "result_len": len(s),
        "result_sha256": _sha256_text(s),
        "result_preview": _preview(s, 400),
    }


def sanitize_args(tool: str, args: Any) -> Any:
    """
    IMPORTANT: Never dump large or sensitive bodies verbatim.
    We log "what happened" while avoiding full content.
    """
    if not isinstance(args, dict):
        return args

    tool = str(tool or "")
    out = dict(args)

    def _sanitize_field(field: str, max_preview: int = 200) -> None:
        if field in out:
            raw = str(out.get(field) or "")
            out[field + "_len"] = len(raw)
            out[field + "_sha256"] = _sha256_text(raw)
            out[field + "_preview"] = _preview(raw, max_preview)
            out.pop(field, None)

    # Common heavy fields
    if tool == "write_file":
        _sanitize_field("content", 200)
    elif tool in ("python_sandbox", "python_exec"):
        _sanitize_field("code", 250)
    elif tool == "bash":
        _sanitize_field("command", 300)
    else:
        # Generic fallback: if any known big fields exist, sanitize them
        for k in ("content", "code"):
            if k in out and isinstance(out.get(k), (str, bytes)):
                _sanitize_field(k, 200)

    return out


@dataclass
class SubAgentDebugLogger:
    agent_type: str
    task_id: str
    session_id: str = ""

    def __post_init__(self) -> None:
        # Sanitize agent_type for filesystem (Windows doesn't allow : in paths)
        safe_agent_type = self.agent_type.replace(":", "_")
        self._base_dir = get_debug_root_dir() / safe_agent_type / self.task_id
        self._base_dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def events_file(self) -> Path:
        return self._base_dir / "events.jsonl"

    def event(self, kind: str, payload: Optional[Dict[str, Any]] = None, **fields: Any) -> None:
        rec: Dict[str, Any] = {
            "ts": _utc_iso(),
            "kind": kind,
            "agent_type": self.agent_type,
            "task_id": self.task_id,
        }
        if self.session_id:
            rec["session_id"] = self.session_id
        if payload is not None:
            rec["payload"] = payload
        if fields:
            rec.update(fields)

        try:
            with self.events_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            # Never crash due to debug logging
            pass


def get_subagent_logger_from_env(
    create_fallback: bool = False,
    agent_type: str = "",
) -> Optional[SubAgentDebugLogger]:
    """
    Default: enabled only inside sub-agent terminals; requires VAF_AGENT_TYPE and
    VAF_TASK_ID to be set (done by subagent runner).

    With create_fallback=True the logger is always created so observability does
    not depend on the IPC spawn path: a missing VAF_TASK_ID is replaced by a
    generated run id ("local-<timestamp>-<pid>") and agent_type may be passed
    directly. The generated id lives only in the returned logger object — the
    process environment is never mutated (concurrent workers share os.environ).
    """
    env_task_id = (os.environ.get("VAF_TASK_ID") or "").strip()
    env_agent_type = (os.environ.get("VAF_AGENT_TYPE") or "").strip()
    session_id = (os.environ.get("VAF_SESSION_ID") or "").strip()

    if not create_fallback:
        in_subagent = os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip().lower() in ("1", "true", "yes")
        in_workflow_term = os.environ.get("VAF_IN_WORKFLOW_TERMINAL", "").strip().lower() in ("1", "true", "yes")
        in_workflow = os.environ.get("VAF_IN_WORKFLOW", "").strip().lower() in ("1", "true", "yes")
        if not (in_subagent or in_workflow_term or in_workflow):
            return None
        if not env_task_id or not env_agent_type:
            return None
        return SubAgentDebugLogger(agent_type=env_agent_type, task_id=env_task_id, session_id=session_id)

    effective_agent_type = env_agent_type or agent_type
    if not effective_agent_type:
        return None
    effective_task_id = env_task_id or f"local-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    try:
        return SubAgentDebugLogger(
            agent_type=effective_agent_type,
            task_id=effective_task_id,
            session_id=session_id,
        )
    except Exception:
        # Observability must never break the actual run
        return None

