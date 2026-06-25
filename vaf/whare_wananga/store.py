# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Whare Wananga -- tool-knowledge store (persistence + schema only).

Whare Wananga (Maori: "house of learning") is VAF's tool self-learning subsystem.
This module is ONLY the storage layer and schema for the artefact it produces:
per-tool **tool_knowledge** -- how to correctly operate a single tool. ("Matauranga"
is the poetic name for this knowledge; the code term is tool_knowledge.)

The artefact is structured by the three baskets of knowledge (Nga Kete o te Wananga):
  - aronui : what the tool returns / when to use it (observation)
  - tuatea : the dangers (pitfalls, side-effects, error behaviour)
  - tuarua : the correct ritual (procedure + verification)

Plus predict-then-verify records and lifecycle metadata. Stored GLOBALLY (tool
mechanics are objective) under ~/.vaf/whare_wananga/<tool>.json.

This module does NOT learn or inject anything -- it is the foundation the learning
loop writes to and the Action-Tag delivery reads from. See docs/agents/ACTION_TAG.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from vaf.core.platform import Platform

SCHEMA_VERSION = 1

VALID_STATUS = ("draft", "confirmed", "stale")
VALID_SOURCE = ("whare_wananga", "teacher", "runtime")

# A distilled "pitfall" is VACUOUS only when the model APOLOGISED about the training process (no
# probes, cannot quote the error, could not determine it) instead of giving a real warning. The
# markers are deliberately NARROW: real error quotes ("[ERROR] No summary provided") and informative
# negative facts ("no required arguments", "limit is optional", "requires an admin session", "no
# errors observed for ...") carry value and must be KEPT.
_VACUOUS_PITFALL_MARKERS = (
    "no probe attempt", "no probes were", "without any probe",          # "No probe attempts were provided"
    "cannot quote", "could not quote", "unable to quote", "cannot quote exact",
    "could not determine the error", "cannot determine the error", "unable to determine the error",
    "insufficient information", "not enough information", "no information available",
    "no pitfalls", "cannot provide a pitfall", "no specific pitfall",
)


def is_vacuous_pitfall(pitfall: Any) -> bool:
    """True if a pitfall is a non-pitfall (empty, or a meta-apology about training) and should be
    dropped before storing/delivering. Fail-safe: on error, keep the pitfall (return False)."""
    try:
        t = (pitfall.get("text") if isinstance(pitfall, dict) else pitfall) or ""
        t = str(t).strip().lower()
        if not t:
            return True
        return any(m in t for m in _VACUOUS_PITFALL_MARKERS)
    except Exception:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_dir() -> Path:
    d = Platform.vaf_dir() / "whare_wananga"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(tool: str) -> str:
    """Filesystem-safe tool name for the json filename."""
    name = (tool or "").strip()
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return name or "_unnamed"


def _path(tool: str) -> Path:
    return _store_dir() / f"{_safe_name(tool)}.json"


def new_record(
    tool: str,
    side_effect_class: str = "none",
    tool_schema_hash: str = "",
    source: str = "whare_wananga",
) -> Dict[str, Any]:
    """Return an empty tool_knowledge skeleton for `tool` (not yet persisted)."""
    ts = _now_iso()
    return {
        "tool": tool,
        "schema_version": SCHEMA_VERSION,
        "tool_schema_hash": tool_schema_hash,
        "side_effect_class": side_effect_class,
        # --- Nga Kete facets ---
        "aronui": {"when_to_use": "", "output_shape": "", "notes": []},
        "tuatea": {"pitfalls": []},   # each: {"text": str, "source": str, "seen": int}
        "tuarua": {"procedure": [], "verification": []},
        # --- predict-then-verify catalogue (measures "learned") ---
        "predict_records": [],        # each: {"intent","predicted","actual","match","ts"}
        # --- lifecycle ---
        "status": "draft",            # draft | confirmed | stale
        "confidence": 0.0,            # 0..1
        "uses": 0,
        "success": 0,
        "fail": 0,
        "source": source,             # whare_wananga | teacher | runtime
        "created_at": ts,
        "updated_at": ts,
    }


def compute_tool_hash(tool_definition: Any) -> str:
    """Stable short hash of a tool's identity (name + description + parameter schema).

    Used to invalidate stored know-how when the tool definition changes. Accepts a
    dict, a string, or any object exposing .name / .description / .parameters.
    """
    if isinstance(tool_definition, str):
        payload = tool_definition
    elif isinstance(tool_definition, dict):
        payload = json.dumps(tool_definition, sort_keys=True, ensure_ascii=False, default=str)
    else:
        parts = {
            "name": getattr(tool_definition, "name", ""),
            "description": getattr(tool_definition, "description", ""),
            "parameters": getattr(tool_definition, "parameters", None)
            or getattr(tool_definition, "input_schema", None),
        }
        payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load(tool: str) -> Optional[Dict[str, Any]]:
    """Load the tool_knowledge record for `tool`, or None if absent/unreadable."""
    p = _path(tool)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save(record: Dict[str, Any]) -> None:
    """Persist a tool_knowledge record atomically. `record['tool']` is required."""
    tool = (record or {}).get("tool")
    if not tool:
        raise ValueError("tool_knowledge record must have a non-empty 'tool' field")
    record["updated_at"] = _now_iso()
    record.setdefault("schema_version", SCHEMA_VERSION)
    target = _path(tool)
    # Atomic write: temp file in the same dir, then replace.
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{_safe_name(tool)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def list_tools() -> List[str]:
    """Return the tool names that have a stored tool_knowledge record."""
    d = _store_dir()
    return sorted(p.stem for p in d.glob("*.json"))


def delete(tool: str) -> bool:
    """Delete the stored record for `tool`. Returns True if a file was removed."""
    try:
        _path(tool).unlink()
        return True
    except FileNotFoundError:
        return False


# ── Learned-state ────────────────────────────────────────────────────────────
# The learned/not-learned state of a tool is DERIVED from the store; there is no
# separate flag on the tool object (that would be per-process and lost on restart).
# The record's `status` field is the persistent, global flag.

STATE_UNLEARNED = "unlearned"   # no record yet (new tool, never trained)
STATE_LEARNING = "learning"     # record exists but not confirmed (status=draft)
STATE_LEARNED = "learned"       # status=confirmed
STATE_STALE = "stale"           # status=stale (e.g. the tool definition changed)


def learned_state(tool: str) -> str:
    """Return the learning state of `tool`, derived from its stored record."""
    rec = load(tool)
    if not rec:
        return STATE_UNLEARNED
    status = rec.get("status") or "draft"
    if status == "confirmed":
        return STATE_LEARNED
    if status == "stale":
        return STATE_STALE
    return STATE_LEARNING


def is_learned(tool: str) -> bool:
    """True only when the tool's know-how is confirmed."""
    return learned_state(tool) == STATE_LEARNED


def learned_states(tools) -> Dict[str, str]:
    """Map each tool name to its learned_state (for the UI / trigger decisions)."""
    return {t: learned_state(t) for t in tools}


def invalidate_stale(tools) -> List[str]:
    """Mark stored records whose tool definition CHANGED as ``stale`` (so the know-how is no longer
    delivered or counted as learned until the tool is re-trained).

    ``tools`` maps tool name -> tool object (the agent's live registry). For each tool that has a
    stored record with a non-empty ``tool_schema_hash``, the current hash is recomputed and compared;
    a mismatch flips the record's ``status`` to ``stale``. A removed tool (no longer in ``tools``) or
    a record without a stored hash is left untouched -- this detects schema *changes*, not absence.
    Returns the names newly marked stale. Fail-safe (never raises)."""
    changed: List[str] = []
    try:
        for name in list_tools():
            try:
                rec = load(name)
                if not rec:
                    continue
                stored = rec.get("tool_schema_hash")
                if not stored or rec.get("status") == "stale":
                    continue
                tool = tools.get(name) if hasattr(tools, "get") else None
                if tool is None:
                    continue
                if compute_tool_hash(tool) != stored:
                    rec["status"] = "stale"
                    save(rec)
                    changed.append(name)
            except Exception:
                continue
    except Exception:
        pass
    return changed
