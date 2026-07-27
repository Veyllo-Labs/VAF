# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The tool execution path, on its way to being the only one.

VAF has five places that run a tool: the agent's ``execute_tool``, the workflow engine, the
coder's own loop, the librarian's own loop, and (until recently) a batch helper. Only the
first evaluates ``admin_only``, ``channel_restrictions``, ``permission_level`` or the
confirmation gate, and only the first reads a tool's ``identity_kwargs`` declaration. The
others each rebuilt part of the pipeline and left the rest out, so the same tool behaves
differently depending on which door its caller came through - and a tool author cannot see
the door.

This module is where that pipeline moves, piece by piece, so the other callers can use it
instead of reimplementing it. The parts that are genuinely per-caller (is there a human who
can answer a gate, which timeout budget applies, whose identity is this) become arguments;
the parts that are chat-turn machinery (the plan gate, the sub-agent prewrite, the
python_exec fallback, the router bookkeeping) stay in ``vaf/core/agent.py`` and do not
belong here.

The move is guarded by three measurements taken while the dispatcher was still whole:

- ``tests/test_dispatch_kwargs_baseline.py`` - what every tool receives
- ``tests/test_dispatch_event_baseline.py`` - what is emitted and returned, per outcome
- ``tests/test_dispatch_side_effect_baseline.py`` - what a dispatch writes around itself

Anything moved here must leave all three unchanged. They exist because a refactor of this
size cannot be reviewed by reading it.
"""
from __future__ import annotations

import uuid as _uuid
from pathlib import Path
from typing import Any


def make_json_serializable(obj: Any) -> Any:
    """Recursively turn Paths and UUIDs into strings so an object can be JSON-encoded.

    Used for the argument previews that go into events, the gate payload and the debug log.
    OS-independent: WindowsPath, PosixPath and PurePath all normalise the same way.
    """
    if isinstance(obj, (Path, _uuid.UUID)):
        return str(obj)
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [make_json_serializable(item) for item in obj]
    return obj


CHANNEL_SOURCES = frozenset({"telegram", "whatsapp", "discord"})
CHANNEL_SESSION_PREFIXES = ("telegram_", "whatsapp_", "discord_")


def is_channel_session(source: str | None, session_id: str | None) -> bool:
    """Is this call coming from a messaging channel rather than the web UI or the CLI?

    Two independent signals, because either can be the only one present: the chat source is
    set on a live web/bridge session, while a resumed or drained session may only carry the
    prefix in its id. Feeds ``channel_restrictions`` - note the source must match a channel
    name EXACTLY (``"telegram"``, not ``"telegram_42"``); the per-session form lives in the
    id, which is why the prefix check exists separately.
    """
    normalized = str(source or "").strip().lower()
    return normalized in CHANNEL_SOURCES or (
        isinstance(session_id, str) and session_id.startswith(CHANNEL_SESSION_PREFIXES)
    )


def policy_admin_flag(role: str | None, scope_id: str | None) -> bool:
    """Whether tool POLICY treats this identity as admin (drives ``admin_only``).

    Two halves, whichever is set: the DB role from the verified session, and the local-admin
    scope for the machine owner who has no role claim at all (tokenless desktop, CLI,
    automations). Fail-closed: anything unexpected resolves to False.

    KNOWN DRIFT, preserved deliberately rather than silently repaired. This compares the role
    EXACTLY, while ``vaf.core.config.is_admin_identity`` - the shared definition used by the
    file jail and roughly thirty other gates - strips and lowercases it first. So for a role
    spelled "Admin", the file jail lifts while ``admin_only`` tools stay blocked: the same
    person, two answers. Not reachable through the API today (``user_routes`` lowercases the
    role on create and on update), and the DB column constrains nothing, so it is a latent
    inconsistency rather than a live hole. Aligning it GRANTS access, which makes it a
    deliberate security decision and not part of a behaviour-neutral extraction - it is
    frozen here so the drift is visible instead of buried in a 600-line method.
    """
    try:
        from vaf.core.config import get_local_admin_scope_id
        return role == "admin" or (
            scope_id is not None and str(scope_id) == str(get_local_admin_scope_id())
        )
    except Exception:
        return False


def normalize_tool_name(raw_name: str | None) -> str | None:
    """Strip the ``functions.`` prefix some providers put in front of a tool name.

    Returns None for anything empty, so a caller can treat "no usable name" as one case.
    """
    if not raw_name:
        return None
    cleaned = raw_name.strip()
    if cleaned.startswith("functions."):
        cleaned = cleaned[len("functions."):]
    return cleaned or None
