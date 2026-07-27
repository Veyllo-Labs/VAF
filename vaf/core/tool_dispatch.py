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

import json
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


def emit_event(sink, evt: dict) -> None:
    """Hand one event to a caller's sink, and never let the sink break the dispatch.

    Observation is fail-OPEN on purpose, and that is the opposite of how a gate behaves: a
    broken observer must not take a tool call down with it, while a broken guard must not
    degrade to "allowed". The sink's return value is ignored - this is a notification, not a
    veto. A caller wanting a say gets it before dispatch, not here.
    """
    if callable(sink):
        try:
            sink(evt)
        except Exception:
            pass


def with_subagent_debug_mirror(sink):
    """Wrap a sink so its events are ALSO written to the sub-agent debug log.

    Deliberately separate from ``emit_event`` rather than folded into it. The mirror is a
    property of the chat lane, not of dispatching: it writes to ``events.jsonl`` whenever the
    process is running as a sub-agent terminal. Folding it into the shared path would hand
    every future caller - the workflow engine first - a debug artifact it does not produce
    today, which is a behaviour change smuggled in through a refactor.

    Note the wrapper mirrors even when there is NO sink, which is what the chat lane does
    today: in the web app ``_event_sink`` is often None while the debug log is still wanted.
    """
    def _mirrored(evt: dict) -> None:
        emit_event(sink, evt)
        try:
            from vaf.core.subagent_debug import get_subagent_logger_from_env
            lg = get_subagent_logger_from_env()
            if lg:
                lg.event("agent_event", payload=evt)
        except Exception:
            pass
    return _mirrored


def resolve_confirmation_gate(tool_name: str, *, reason: str, args: dict | None,
                              trust_dir, allow_once: set, interactive: bool,
                              decide=None, emit=None, on_gate_required=None) -> str | None:
    """Decide whether a confirmation-gated tool may run.

    Returns ``None`` when it may proceed, or the string to hand back to the model. Never
    raises and never returns a partial state: the caller either dispatches or returns this.

    Standing grants are checked first and silently - a tool whose policy is "allow", or any
    tool under a trusted directory, or one already allowed once this turn, produces no event
    at all. Only an actual gate is worth telling anyone about.

    The one thing callers genuinely differ on is HOW a decision is obtained, so that is a
    callback rather than a branch here. Keeping it out means this module does not depend on
    the web server or the CLI interface, which is what lets a non-chat caller use it.

    ``interactive=False`` returns the refusal string and emits NO ``gate_decision``. That
    asymmetry is published (docs/OBSERVABILITY.md) and load-bearing for embedders, whose
    documented guarantee is that a gated tool returns a string rather than blocking on a human
    (docs/EMBEDDING.md, "Gated tools never hang or raise"). The prefix is matched by callers
    via ``vaf.markers.TOOL_CONFIRMATION_REQUIRED``, so its wording is contract, not prose.

    ``trust_dir`` is passed in rather than read here because it is the HOST PROCESS's working
    directory at call time - "always" trusts that directory and its whole subtree, so which
    directory it was has to be the caller's answer, and the same value must be used for the
    check, the event and the grant.
    """
    from vaf.core.trust import get_tool_policy, is_trusted_dir, mark_trusted_dir, set_tool_policy

    if get_tool_policy(tool_name) == "allow" or is_trusted_dir(trust_dir) or tool_name in allow_once:
        return None

    try:
        preview = json.dumps(make_json_serializable(args or {}), ensure_ascii=False)[:300]
    except Exception:
        preview = ""
    event = {"type": "gate_required", "tool": tool_name, "cwd": str(trust_dir),
             "reason": reason, "args_preview": preview}
    emit_event(emit, event)
    if callable(on_gate_required):
        try:
            on_gate_required(event)
        except Exception:
            pass

    if not interactive:
        return (f"[ERROR] Tool '{tool_name}' requires confirmation ({reason}). "
                f"Re-run interactively or mark folder trusted.")

    choice = decide() if callable(decide) else "cancel"
    if choice == "allow_once":
        # In memory, for this agent only. Persisting a single approval would silently widen
        # it into a standing one.
        allow_once.add(tool_name)
        emit_event(emit, {"type": "gate_decision", "tool": tool_name, "decision": "allow_once"})
        return None
    if choice == "allow_always":
        # Both at once, as documented: the directory subtree AND the tool. Outlives the
        # process and is machine-global - the only persistent write on any dispatch path.
        mark_trusted_dir(trust_dir)
        set_tool_policy(tool_name, "allow")
        emit_event(emit, {"type": "gate_decision", "tool": tool_name, "decision": "allow_always"})
        return None
    emit_event(emit, {"type": "gate_decision", "tool": tool_name, "decision": "cancel"})
    return f"[CANCELLED] Tool '{tool_name}' cancelled by user."


def session_stop_check(session_id: str | None):
    """A stop predicate for one session, or a never-stop one when there is no session.

    The Stop button is a per-session flag in the task queue; polling it DURING a call is what
    makes the button work at all, since a tool that has already started would otherwise run
    to completion. Fully defensive: if the queue cannot be reached the answer is "do not
    stop", because a false stop kills work the user did not cancel.
    """
    def _check() -> bool:
        try:
            if not session_id:
                return False
            from vaf.core.task_queue import TaskQueue
            return bool(TaskQueue().should_stop(session_id))
        except Exception:
            return False
    return _check


def run_tool_bounded(tool: Any, args: dict, *, tool_name: str,
                     timeout_for=None, self_supervised=None, stop_check=None,
                     poll: float | None = None):
    """Run one tool call, bounded in wall-clock time unless the tool supervises itself.

    Three things differ per caller, and each is an argument rather than a second copy of this
    function:

    - ``timeout_for(name) -> seconds``. Defaults to the per-agent budget. The workflow engine
      passes its own, which raises a floor for heavy sub-agent steps: the generic cap once
      killed a healthy coder mid-loop at minute five.
    - ``self_supervised``: the names that must NOT be wrapped, because a hard timeout would
      abandon them mid-work while they are still making progress. The engine deliberately
      excludes ``browser_agent`` from its own set - a workflow must not stall forever on one
      browsing step, even though a standalone call may.
    - ``stop_check``: how this caller learns the user pressed Stop. The chat lane polls the
      task queue by session; the workflow engine is handed a callback from outside.

    Returns whatever the tool returns, or one of the abort sentinels from
    ``vaf/core/bounded_run.py`` on timeout or stop.
    """
    from vaf.core.bounded_run import (
        SELF_SUPERVISED_TOOLS,
        agent_timeout_seconds,
        run_bounded,
    )
    supervised = SELF_SUPERVISED_TOOLS if self_supervised is None else self_supervised
    if tool_name in supervised:
        return tool.run(**args)

    if poll is None:
        from vaf.core.config import Config
        poll = float(Config.get("tool_stop_poll_seconds", 0.5))
    resolve_timeout = timeout_for or agent_timeout_seconds
    return run_bounded(
        lambda: tool.run(**args),
        timeout=resolve_timeout(tool_name),
        stop_check=stop_check,
        poll=poll,
        label=tool_name,
    )


class ToolCallHooks:
    """Where a caller's own machinery interleaves with the shared pipeline.

    Four points, and each is a MEASURED position rather than an extension point invented on
    spec. They exist because the chat lane genuinely has stages in the middle of the pipeline,
    and moving any of them to the edge changes an event order, a precedence or a side effect
    that something already depends on. A caller with no such stages passes ``ToolCallHooks()``
    and gets the bare pipeline.

    - ``after_policy(name, tool, args) -> str | None`` - the chat gates (plan, note firewall,
      proactive reply, ask-first). Returning a string ends the call with it. Runs AFTER the
      hard policy block, never before: a blocked tool must not reach a soft gate.
    - ``before_dispatch(name, tool_args) -> str | None`` - may mutate ``tool_args`` (the chat
      lane's session plumbing) and may refuse (its duplicate sub-agent guard). Runs after
      identity assignment, so what it adds is not validated against the tool's schema.
    - ``after_dispatch(name, tool_args, result) -> str`` - may replace the result. The chat
      lane discovers tools from a ``search_tools`` answer here and runs its python_exec
      fallback, which emits its own event pair.
    - ``after_emit(name) -> None`` - only on paths that actually dispatched. The chat lane
      records router recency here, and deliberately NOT for a blocked or refused call.
    """

    __slots__ = ("after_policy", "before_dispatch", "after_dispatch", "after_emit")

    def __init__(self, after_policy=None, before_dispatch=None, after_dispatch=None,
                 after_emit=None):
        self.after_policy = after_policy
        self.before_dispatch = before_dispatch
        self.after_dispatch = after_dispatch
        self.after_emit = after_emit


class ToolCaller:
    """Run a tool through the full pipeline, configured for one caller.

    VAF had five places that ran a tool and only one of them evaluated policy, honoured the
    confirmation gate or read a tool's identity declaration. This is the one path they share,
    so that "which door did the caller come through" stops being a security answer.

    What differs per caller are ARGUMENTS, not forks - that distinction is the whole design.
    Whether a human can answer a gate, which timeout budget applies, which tools supervise
    themselves, how Stop arrives, whose identity this is: all parameters. If a second caller
    ever needs a fork instead, the parameter list is wrong.

    A caller with no agent, no web server and no terminal gets the same pipeline as the
    product's chat loop. That is the point, and it is checked: this module imports neither.
    """

    def __init__(
        self,
        tools,
        *,
        # WHO is calling
        user_scope_id: str | None = None,
        username: str | None = None,
        user_role: str | None = None,
        source: str = "",
        session_id: str | None = None,
        # UNDER WHICH RULES
        interactive: bool = False,
        trust_dir=None,
        allow_once: set | None = None,
        decide=None,
        on_gate_required=None,
        # RUNTIME CONTROL
        timeout_for=None,
        self_supervised=None,
        stop_check=None,
        poll: float | None = None,
        max_result_chars: int | None = 2000,
        # PLUMBING
        on_event=None,
        hooks: "ToolCallHooks | None" = None,
        model_name: str | None = None,
    ):
        self.tools = tools
        self.user_scope_id = user_scope_id
        self.username = username
        self.user_role = user_role
        self.source = source
        self.session_id = session_id
        self.interactive = interactive
        self.trust_dir = trust_dir
        self.allow_once = allow_once if allow_once is not None else set()
        self.decide = decide
        self.on_gate_required = on_gate_required
        self.timeout_for = timeout_for
        self.self_supervised = self_supervised
        self.stop_check = stop_check
        self.poll = poll
        self.max_result_chars = max_result_chars
        self.on_event = on_event
        self.hooks = hooks or ToolCallHooks()
        self.model_name = model_name

    # ── the pipeline ─────────────────────────────────────────────────────────

    def execute(self, name: str, args: dict | None = None) -> str:
        """Dispatch one tool call. Always returns a string; never raises for tool failures.

        The ORDER below is contract, not convenience, and three parts of it were only
        discovered by measuring (tests/test_dispatch_event_baseline.py):

        - a hard policy block and a refused gate emit NOTHING and dispatch nothing, so a
          consumer never sees a blocked tool reported as run;
        - a schema error is about THIS call while the duplicate-guard message is about
          another one already running, so the schema error wins;
        - the result is truncated LAST, after any hook has had its say, because
          ``search_tools`` caps itself just under this limit.
        """
        import time

        tool = self.tools.get(name)

        decision = self._policy(name, tool)
        if decision.blocked:
            return f"Security Error: {decision.reason}"

        if callable(self.hooks.after_policy):
            gate_msg = self.hooks.after_policy(name, tool, args)
            if gate_msg is not None:
                return gate_msg

        if decision.requires_confirmation:
            refusal = resolve_confirmation_gate(
                name, reason=decision.reason, args=args,
                trust_dir=self.trust_dir if self.trust_dir is not None else Path.cwd(),
                allow_once=self.allow_once, interactive=self.interactive,
                decide=self.decide, emit=self.on_event,
                on_gate_required=self.on_gate_required,
            )
            if refusal is not None:
                return refusal

        emit_event(self.on_event, {"type": "tool_start", "tool": name,
                                   "args": self._preview(name, args)})
        started = time.monotonic()
        try:
            result = self._dispatch(name, tool, args)
        except Exception as exc:                                  # noqa: BLE001
            result = f"Tool Error: {exc}"

        if callable(self.hooks.after_dispatch):
            result = self.hooks.after_dispatch(name, args, result)

        emit_event(self.on_event, {
            "type": "tool_end", "tool": name,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "ok": not (isinstance(result, str) and (
                result.startswith("Tool Error:") or result.startswith("Error: Unknown tool"))),
        })
        if callable(self.hooks.after_emit):
            self.hooks.after_emit(name)
        return self._truncate(result)

    # ── stages ───────────────────────────────────────────────────────────────

    def _policy(self, name, tool):
        from vaf.core.tool_contract import evaluate_tool_policy
        return evaluate_tool_policy(
            tool_name=name, tool=tool, current_source=self.source,
            is_channel_session=is_channel_session(self.source, self.session_id),
            is_admin=policy_admin_flag(self.user_role, self.user_scope_id),
        )

    def _preview(self, name, args):
        """Argument preview for the event stream - heavy fields stripped, Paths stringified."""
        serializable = make_json_serializable(args) if args else {}
        try:
            from vaf.core.subagent_debug import sanitize_args
            return sanitize_args(name, serializable)
        except Exception:
            return serializable

    def _dispatch(self, name, tool, args):
        if tool is None:
            return f"Error: Unknown tool '{name}'"
        tool_args = dict(args) if args else {}
        tool_args, errors = repair_arguments(tool, tool_args, tool_name=name,
                                             model_name=self.model_name)
        assign_declared_identity(
            tool, tool_args, user_scope_id=self.user_scope_id,
            username=self.username, user_role=self.user_role,
        )
        if callable(self.hooks.before_dispatch):
            refusal = self.hooks.before_dispatch(name, tool_args)
            if refusal is not None and errors:
                # A schema error is about THIS call; the hook's refusal is about another one
                # already in flight. The call that cannot even be formed loses first.
                return "Tool Error: invalid arguments for '%s': %s" % (name, "; ".join(errors))
            if refusal is not None:
                return refusal
        if errors:
            return "Tool Error: invalid arguments for '%s': %s" % (name, "; ".join(errors))
        return run_tool_bounded(
            tool, tool_args, tool_name=name, timeout_for=self.timeout_for,
            self_supervised=self.self_supervised, stop_check=self.stop_check, poll=self.poll,
        )

    def _truncate(self, result):
        limit = self.max_result_chars
        if limit is None:
            return result
        text = str(result)
        if len(text) <= limit:
            return result
        return (f"{text[:limit]}\n... [Output Truncated. Total length: {len(text)} chars. "
                f"Use specific filters or read sub-parts.]")


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
