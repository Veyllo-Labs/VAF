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


def get_debug_root_dir() -> Path:
    """
    Prefer repo-local logs/debug (as requested). Fallback to ~/.vaf/logs/debug if not writable.
    """
    repo = _repo_root_from_file()
    if repo:
        candidate = repo / "logs" / "debug"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass

    fallback = Platform.vaf_dir() / "logs" / "debug"
    fallback.mkdir(parents=True, exist_ok=True)
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


def get_subagent_logger_from_env() -> Optional[SubAgentDebugLogger]:
    """
    Enabled only inside sub-agent terminals.
    Requires VAF_AGENT_TYPE and VAF_TASK_ID to be set (done by subagent runner).
    """
    in_subagent = os.environ.get("VAF_IN_SUBAGENT_TERMINAL", "").strip().lower() in ("1", "true", "yes")
    in_workflow_term = os.environ.get("VAF_IN_WORKFLOW_TERMINAL", "").strip().lower() in ("1", "true", "yes")
    in_workflow = os.environ.get("VAF_IN_WORKFLOW", "").strip().lower() in ("1", "true", "yes")
    if not (in_subagent or in_workflow_term or in_workflow):
        return None

    task_id = (os.environ.get("VAF_TASK_ID") or "").strip()
    agent_type = (os.environ.get("VAF_AGENT_TYPE") or "").strip()
    if not task_id or not agent_type:
        return None

    session_id = (os.environ.get("VAF_SESSION_ID") or "").strip()
    return SubAgentDebugLogger(agent_type=agent_type, task_id=task_id, session_id=session_id)

