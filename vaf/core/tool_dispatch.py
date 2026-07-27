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

    Delegates to ``vaf.core.config.is_admin_identity``, the one definition the file jail and
    roughly thirty other gates already use, so "is this caller an admin" has a single answer
    across VAF. Fail-closed: anything unexpected resolves to False.

    It did not always. This spot compared the role EXACTLY while the shared definition strips
    and lowercases first, so for a role spelled "Admin" the file jail lifted while
    ``admin_only`` tools stayed blocked - the same person, two answers, in the one place that
    decides whether a tool may run at all. It survived the round that gave the file gates the
    shared rule because it sat inline in a 600-line method rather than behind a name.
    """
    try:
        from vaf.core.config import is_admin_identity
        return is_admin_identity(role, scope_id)
    except Exception:
        return False


IDENTITY_KEYS = ("user_scope_id", "username", "user_role")


def assign_declared_identity(tool: Any, args: dict, *, user_scope_id: str | None,
                             username: str | None, user_role: str | None) -> dict:
    """Give a tool exactly the identity keys it declares, and nothing else.

    A tool states its needs through ``BaseTool.identity_kwargs``. That declaration replaced
    roughly forty hardcoded name lists, which had two costs: they drifted apart (a tool added
    to one list and not its sibling), and a tool registered by an embedder through
    ``Agent.add_tool()`` could never receive an identity at all, because the dispatcher only
    knew VAF's own names.

    ASSIGNED, never defaulted. ``args`` starts out as whatever the MODEL produced, so a
    prompt-injected ``user_role="admin"`` is overwritten with the caller's real role rather
    than honoured. Declaring nothing gets nothing - the safe direction.

    The ``username`` fallback to "admin" is deliberate and load-bearing: the tokenless
    desktop, the CLI and automations carry no username, and the stores keyed on it treat that
    as the machine owner. Mutates and returns ``args``.
    """
    available = {
        "user_scope_id": user_scope_id,
        "username": username or "admin",
        "user_role": user_role,
    }
    for key in (getattr(tool, "identity_kwargs", ()) or ()):
        if key in available:
            args[key] = available[key]
    return args


def repair_arguments(tool: Any, args: dict, *, tool_name: str,
                     model_name: str | None = None) -> tuple[dict, list]:
    """Validate the model's arguments against the tool's schema and repair weak shapes.

    Handles the mistakes small models make with tool schemas - a bare string where an array
    belongs, a stringified array, null on an optional field, a single-key placeholder - and
    reports what could not be repaired so the caller can refuse rather than dispatch with
    invalid input.

    Runs on the RAW model arguments only, before any runtime kwarg is injected: the injected
    keys are not in the tool's declared schema, and validating them would reject every call.
    Fully defensive - any failure here is a no-op and dispatch proceeds, because a broken
    repair pass must not become a broken dispatcher.

    Returns ``(args, errors)``; a non-empty ``errors`` means the arguments still violate the
    schema.
    """
    errors: list = []
    try:
        from vaf.core.tool_input_repair import repair_tool_input
        args, applied, errors = repair_tool_input(
            getattr(tool, "parameters", None), args,
            getattr(tool, "input_aliases", None),
        )
        if applied:
            try:
                from vaf.core.log_helper import log_timeline_event
                log_timeline_event("tool_input_repaired", tool=tool_name,
                                   model=model_name, repairs=applied)
            except Exception:
                pass
    except Exception:
        errors = []
    return args, errors


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
