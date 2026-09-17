# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The owner's Front Office profile: how their agent behaves towards the people it
answers on their behalf.

One JSON file per user next to the user identity (`~/.vaf/users/<username>/front_office.json`),
the way `soul.md` and `user_identity.json` are per user: a Front Office answers for ONE
owner, and two accounts on a LAN install brief their agents differently.

- `briefing`: the owner's own instructions for those turns, appended to the Front Office
  block of the system prompt (who the agent speaks for, tone, what it may offer and
  promise, what it must never say, when to fetch the owner). Free text, capped.
- `use_general_memory`: whether a Front Office turn may also search the owner's general
  memory. Off by default: a stranger is driving that turn, and what the owner told their
  own agent is not theirs to read. What the Front Office knows instead is the knowledge
  lane (`vaf.memory.lanes.FRONT_OFFICE_SOURCE`), the documents learned in Settings.

The reader tolerates a missing or broken file (the defaults apply) and the writer only
ever writes the keys it knows, so a file from a later version keeps its extra keys.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from vaf.core.config import Config, get_local_admin_username

PROFILE_FILE = "front_office.json"
BRIEFING_MAX_CHARS = 8000

DEFAULT_PROFILE: Dict[str, Any] = {
    "briefing": "",
    "use_general_memory": False,
}


def _safe_username(username: Optional[str]) -> str:
    u = (username or "").strip() or get_local_admin_username()
    safe = "".join(c for c in u if c.isalnum() or c in "_-")
    return safe or get_local_admin_username()


def profile_path(username: Optional[str]) -> Path:
    return Config.APP_DIR / "users" / _safe_username(username) / PROFILE_FILE


def _coerce(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULT_PROFILE)
    briefing = raw.get("briefing")
    if isinstance(briefing, str):
        out["briefing"] = briefing[:BRIEFING_MAX_CHARS]
    out["use_general_memory"] = bool(raw.get("use_general_memory", False))
    return out


def load_front_office_profile(username: Optional[str]) -> Dict[str, Any]:
    """The profile with every key present; the defaults when there is no file yet."""
    path = profile_path(username)
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_PROFILE)
    return _coerce(raw if isinstance(raw, dict) else {})


def save_front_office_profile(username: Optional[str], **changes: Any) -> Dict[str, Any]:
    """Merge the given keys into the profile and write it. Unknown keys are refused with a
    ValueError, so a typo never lands silently; a briefing longer than the cap is cut."""
    unknown = sorted(set(changes) - set(DEFAULT_PROFILE))
    if unknown:
        raise ValueError(f"unknown Front Office profile keys: {unknown}")
    path = profile_path(username)
    try:
        stored = json.loads(path.read_bytes().decode("utf-8"))
        if not isinstance(stored, dict):
            stored = {}
    except (OSError, ValueError):
        stored = {}
    merged = dict(stored)
    merged.update(_coerce({**_coerce(stored), **changes}))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes((json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    tmp.replace(path)
    return _coerce(merged)


def front_office_briefing(username: Optional[str]) -> str:
    """The briefing text, stripped; "" when the owner wrote none."""
    return str(load_front_office_profile(username).get("briefing") or "").strip()
