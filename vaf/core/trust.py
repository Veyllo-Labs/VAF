# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""
Trust & Capability Gating

Minimal "trusted folders" + user decisions for risky actions:
- once    this one call, nothing is remembered
- chat    this tool, for the rest of ONE chat of ONE user, in memory only
- always  this tool and the current directory subtree, persisted per user
- cancel

PER USER. The store used to be one machine-global file, so a single "always"
armed that tool for every tenant of a LAN instance - and unobservably, because
a standing grant short-circuits the gate before any event is emitted. Every
function therefore takes a ``user_scope_id``; the file lives under a
scope-keyed name, with the local admin collapsing to "default" the way
thinking_workspace and reminders already do.

Design goals:
- OS-independent (Platform.config_dir)
- No hardcoded paths
- Safe defaults (ask)
- One tenant's decision never speaks for another
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from vaf.core.platform import Platform

Decision = Literal["allow_once", "allow_chat", "allow_always", "cancel"]


RISKY_TOOLS = {
    # Filesystem moves (write_file is deliberately NOT here: main-agent writes are
    # workspace-anchored + per-user jailed and gate via the plan gate instead,
    # consistent with document_writer which writes the same workspace unprompted)
    "move_file",
    # Shell execution tools (if present)
    "bash",
    "run_command",
    # Host Python execution (outside sandbox)
    "python_exec",
}


@dataclass
class TrustState:
    trusted_dirs: set[str]
    tool_policies: dict[str, str]  # tool_name -> "allow" | "ask"


# Format tag for the per-scope files (see docs/security/USER_ISOLATION.md).
TRUST_FORMAT = "trust-2-b17c4e"


def _scope_key(user_scope_id: Optional[str]) -> str:
    """Canonical per-user key. Mirrors thinking_workspace._scope_key."""
    if user_scope_id is None or not str(user_scope_id).strip():
        return "default"
    try:
        from vaf.core.config import get_local_admin_scope_id
        if str(user_scope_id).strip() == str(get_local_admin_scope_id()).strip():
            return "default"
    except Exception:
        pass
    return str(user_scope_id).strip()


def _trust_file(user_scope_id: Optional[str] = None) -> Path:
    return Platform.config_dir() / "trust" / f"{_scope_key(user_scope_id)}.json"


def _retire_legacy_store() -> None:
    """Move the old machine-global trust.json aside, exactly once.

    Deliberately NOT migrated into the admin's scope: the entries were granted
    under a store that could not tell tenants apart, so inheriting them would
    carry that ambiguity forward. Everyone confirms once more instead.
    """
    legacy = Platform.config_dir() / "trust.json"
    try:
        if legacy.exists():
            legacy.rename(legacy.with_suffix(".json.pre-scope"))
    except Exception:
        pass


def load_trust_state(user_scope_id: Optional[str] = None) -> TrustState:
    _retire_legacy_store()
    path = _trust_file(user_scope_id)
    if not path.exists():
        return TrustState(trusted_dirs=set(), tool_policies={})
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        trusted_dirs = set(data.get("trusted_dirs", []))
        tool_policies = dict(data.get("tool_policies", {}))
        return TrustState(trusted_dirs=trusted_dirs, tool_policies=tool_policies)
    except Exception:
        return TrustState(trusted_dirs=set(), tool_policies={})


def save_trust_state(state: TrustState, user_scope_id: Optional[str] = None) -> None:
    path = _trust_file(user_scope_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "format": TRUST_FORMAT,
        # str() defensively: trusted_dirs must be JSON-serializable strings. A Path here
        # (e.g. from a helper that returns Path) would make json.dumps raise and silently
        # break "allow always" for every dangerous tool.
        "trusted_dirs": sorted(str(d) for d in state.trusted_dirs),
        "tool_policies": state.tool_policies,
    }
    # tmp+rename: two lanes dispatch tools concurrently in one process, and a
    # half-written store reads as "nothing trusted" - fail-safe, but it would
    # silently drop a grant the user just gave.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _norm_dir(p: Path) -> str:
    # Normalize via Platform helper if available. Platform.normalize_path returns a Path,
    # so str() the result - trusted_dirs must hold strings (see save_trust_state).
    try:
        return str(Platform.normalize_path(str(p.resolve())))
    except Exception:
        return str(p.resolve())


def is_trusted_dir(cwd: Path, user_scope_id: Optional[str] = None) -> bool:
    state = load_trust_state(user_scope_id)
    cur = cwd.resolve()
    while True:
        if _norm_dir(cur) in state.trusted_dirs:
            return True
        if cur.parent == cur:
            return False
        cur = cur.parent


def mark_trusted_dir(cwd: Path, user_scope_id: Optional[str] = None) -> None:
    state = load_trust_state(user_scope_id)
    state.trusted_dirs.add(_norm_dir(cwd))
    save_trust_state(state, user_scope_id)


def set_tool_policy(tool_name: str, policy: Literal["allow", "deny", "ask"],
                    user_scope_id: Optional[str] = None) -> None:
    state = load_trust_state(user_scope_id)
    # We intentionally do NOT persist "deny" (use cancel instead)
    if policy == "deny":
        policy = "ask"
    state.tool_policies[tool_name] = policy
    save_trust_state(state, user_scope_id)


def get_tool_policy(tool_name: str, user_scope_id: Optional[str] = None) -> str:
    state = load_trust_state(user_scope_id)
    return state.tool_policies.get(tool_name, "ask")


# ── Chat grants ("allow for this chat") ──────────────────────────────────────
#
# The middle answer between one call and a persistent "always". It replaced an
# in-memory "allow once" set that lived on the one Agent object a web or tray
# process serves every session and every account from, and was never cleared:
# one click armed the tool for the rest of the process, for every chat and
# every tenant, and nothing announced it. A grant is therefore KEYED on the
# person and the chat, and a call with no chat can hold none.
#
# In memory only, deliberately: it answers "may this tool keep running in the
# conversation I am looking at", and a restart ends that conversation's grants
# the same way closing a terminal ends a shell.
#
# A spawned sub-agent is another process, so it inherits the grants of the ONE
# chat it serves as data: tool names in CHAT_GRANTS_ENV, bound to the identity
# the spawn already hands over (VAF_USER_SCOPE_ID, VAF_SESSION_ID). They count
# only for that same person and chat.

CHAT_GRANTS_ENV = "VAF_CHAT_TOOL_GRANTS"

_chat_grants: dict[tuple[str, str], set[str]] = {}
_chat_grants_lock = threading.Lock()


def _chat_key(user_scope_id: Optional[str], session_id: Optional[str]) -> Optional[tuple[str, str]]:
    session = str(session_id or "").strip()
    if not session:
        return None
    return (_scope_key(user_scope_id), session)


def grant_tool_for_chat(tool_name: str, user_scope_id: Optional[str],
                        session_id: Optional[str]) -> bool:
    """Allow ``tool_name`` for the rest of this chat. False when there is no chat to key on."""
    key = _chat_key(user_scope_id, session_id)
    if key is None:
        return False
    with _chat_grants_lock:
        _chat_grants.setdefault(key, set()).add(tool_name)
    return True


def _inherited_chat_grants(key: tuple[str, str]) -> frozenset[str]:
    raw = os.environ.get(CHAT_GRANTS_ENV, "")
    if not raw:
        return frozenset()
    own = _chat_key(os.environ.get("VAF_USER_SCOPE_ID"), os.environ.get("VAF_SESSION_ID"))
    if own != key:
        return frozenset()
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def chat_grants(user_scope_id: Optional[str], session_id: Optional[str]) -> frozenset[str]:
    """The tools this person allowed for this chat, including the ones a child inherited."""
    key = _chat_key(user_scope_id, session_id)
    if key is None:
        return frozenset()
    with _chat_grants_lock:
        own = frozenset(_chat_grants.get(key, ()))
    return own | _inherited_chat_grants(key)


def has_chat_grant(tool_name: str, user_scope_id: Optional[str],
                   session_id: Optional[str]) -> bool:
    return tool_name in chat_grants(user_scope_id, session_id)


def should_gate_tool(tool_name: str) -> bool:
    return tool_name in RISKY_TOOLS


def explain_gate(tool_name: str) -> str:
    if tool_name in {"move_file"}:
        return "This action modifies files on disk."
    if tool_name in {"bash", "run_command"}:
        return "This action runs shell commands on your machine."
    return "This action is considered risky."


