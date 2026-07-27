# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One pipeline, configured per caller - and a caller that is nobody in particular.

VAF had five places that ran a tool. Exactly one of them evaluated policy, honoured the
confirmation gate, or read a tool's identity declaration; the other four each rebuilt a part
and left the rest out. The result was that "which door did the caller come through" was a
security answer, and a tool author could not see the door.

``ToolCaller`` is the one path they share. What differs per caller are ARGUMENTS - whether a
human can answer a gate, which timeout budget applies, whose identity this is - because the
moment a difference becomes a fork, the second implementation starts drifting from the first.
That is not a guess about the future; it is what already happened here twice.

This file drives it as an EMBEDDER would: no Agent, no web server, no terminal, no session.
If any of those were required, the module would be product code wearing a library's name.
That property is asserted rather than assumed, at the bottom.

The pipeline ORDER is contract, and three parts of it were only found by measuring
(tests/test_dispatch_event_baseline.py): a hard block emits NOTHING, a schema error outranks
a refusal about a different call, and truncation happens last so a hook can still speak.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from vaf.core.tool_dispatch import ToolCaller, ToolCallHooks
from vaf.tools.base import BaseTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID


class _Probe(BaseTool):
    name = "probe"
    description = "probe"
    permission_level = "read"
    parameters = {"type": "object", "properties": {"x": {"type": "string"}}}

    def __init__(self, fn=None, **attrs):
        super().__init__()
        self._fn = fn or (lambda **kw: "OK")
        self.seen = None
        for k, v in attrs.items():
            setattr(self, k, v)

    def run(self, **kwargs):
        self.seen = dict(kwargs)
        return self._fn(**kwargs)


def _tool(name="probe", fn=None, **attrs):
    t = _Probe(fn, **attrs)
    t.name = name
    return t


def _caller(tool, events=None, **kw):
    kw.setdefault("user_scope_id", SCOPE)
    kw.setdefault("user_role", "user")
    kw.setdefault("username", "tenant")
    return ToolCaller({tool.name: tool}, on_event=(events.append if events is not None else None),
                      **kw)


# ── the happy path, as an embedder sees it ───────────────────────────────────

def test_a_caller_with_nothing_but_a_registry_can_run_a_tool():
    tool = _tool()
    assert _caller(tool).execute("probe", {"x": "1"}) == "OK"


def test_the_declared_identity_arrives():
    tool = _tool(identity_kwargs=("user_scope_id", "user_role"))
    _caller(tool).execute("probe", {})
    assert tool.seen["user_scope_id"] == SCOPE
    assert tool.seen["user_role"] == "user"


def test_only_the_declared_keys_arrive():
    tool = _tool(identity_kwargs=("user_scope_id",))
    _caller(tool).execute("probe", {})
    assert "username" not in tool.seen and "user_role" not in tool.seen


def test_a_model_supplied_identity_is_overwritten():
    """The escalation this closes: arguments start out as whatever a model produced."""
    tool = _tool(identity_kwargs=("user_scope_id", "user_role"))
    _caller(tool).execute("probe", {"user_role": "admin", "user_scope_id": "ffffffff-0000"})
    assert tool.seen["user_role"] == "user"
    assert tool.seen["user_scope_id"] == SCOPE


def test_the_documented_event_pair_is_emitted():
    events = []
    _caller(_tool(), events).execute("probe", {})
    assert [(e["type"], e.get("ok")) for e in events] == [("tool_start", None), ("tool_end", True)]
    assert "duration_ms" in events[-1]


# ── refusals: what a caller gets instead of an exception ─────────────────────

def test_an_unknown_tool_is_an_error_string_not_a_crash():
    result = _caller(_tool()).execute("nope", {})
    assert result.startswith("Error: Unknown tool")


def test_a_raising_tool_is_an_error_string():
    def _boom(**kw):
        raise ValueError("kaputt")

    result = _caller(_tool(fn=_boom)).execute("probe", {})
    assert result.startswith("Tool Error:")


def test_a_failed_dispatch_is_reported_as_not_ok():
    events = []
    _caller(_tool(), events).execute("nope", {})
    assert events[-1]["ok"] is False


def test_invalid_arguments_are_refused_rather_than_dispatched():
    strict = _tool(parameters={"type": "object", "properties": {"x": {"type": "string"}},
                               "required": ["x"]})
    result = _caller(strict).execute("probe", {})
    assert result.startswith("Tool Error: invalid arguments")
    assert strict.seen is None, "the tool ran with arguments it declared invalid"


# ── policy: the half four lanes were missing entirely ────────────────────────

def test_an_admin_only_tool_is_blocked_for_an_ordinary_caller():
    tool = _tool(admin_only=True)
    assert _caller(tool).execute("probe", {}).startswith("Security Error:")
    assert tool.seen is None


def test_an_admin_only_tool_runs_for_an_admin():
    tool = _tool(admin_only=True)
    assert _caller(tool, user_role="admin").execute("probe", {}) == "OK"


def test_channel_restrictions_apply():
    tool = _tool(channel_restrictions=("telegram",))
    with patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: False if k == "channel_tools_unrestricted" else d):
        result = _caller(tool, source="telegram").execute("probe", {})
    assert result.startswith("Security Error:")


def test_a_hard_block_says_nothing_at_all():
    """Silence is contract. A funnel that emitted tool_start before evaluating policy would
    tell every consumer that a blocked tool ran."""
    events = []
    _caller(_tool(admin_only=True), events).execute("probe", {})
    assert events == []


# ── the gate: no human, no hang ──────────────────────────────────────────────

def test_a_gated_tool_returns_a_string_when_nobody_can_answer():
    """The embedder guarantee - a gated tool never blocks on a person who is not there."""
    from vaf import markers

    tool = _tool(permission_level="dangerous")
    with patch("vaf.core.trust.get_tool_policy", return_value="ask"), \
         patch("vaf.core.trust.is_trusted_dir", return_value=False):
        result = _caller(tool, interactive=False, trust_dir=Path("/tmp/p")).execute("probe", {})
    assert markers.TOOL_CONFIRMATION_REQUIRED in result
    assert tool.seen is None


def test_a_standing_grant_lets_a_gated_tool_through_silently():
    tool = _tool(permission_level="dangerous")
    events = []
    with patch("vaf.core.trust.get_tool_policy", return_value="allow"), \
         patch("vaf.core.trust.is_trusted_dir", return_value=False):
        result = _caller(tool, events, interactive=False, trust_dir=Path("/tmp/p")).execute("probe", {})
    assert result == "OK"
    assert [e["type"] for e in events] == ["tool_start", "tool_end"]


# ── truncation ───────────────────────────────────────────────────────────────

def test_a_long_result_is_truncated_with_a_notice():
    result = _caller(_tool(fn=lambda **kw: "y" * 5000)).execute("probe", {})
    assert "[Output Truncated." in result


def test_a_caller_can_switch_truncation_off():
    """The workflow engine chains step outputs; cutting them at 2000 chars would break the
    substitution it does on them."""
    caller = ToolCaller({"probe": _tool(fn=lambda **kw: "y" * 5000)}, max_result_chars=None)
    assert len(caller.execute("probe", {})) == 5000


# ── the four hooks, at their measured positions ──────────────────────────────

def test_after_policy_can_end_the_call():
    tool = _tool()
    hooks = ToolCallHooks(after_policy=lambda n, t, a: "[PLAN REQUIRED] first make a plan")
    assert _caller(tool, hooks=hooks).execute("probe", {}).startswith("[PLAN REQUIRED]")
    assert tool.seen is None


def test_after_policy_runs_after_the_hard_block_never_before():
    """A blocked tool must not reach a soft gate - otherwise the gate's message would
    replace the security error and the caller would learn the wrong reason."""
    called = []
    hooks = ToolCallHooks(after_policy=lambda n, t, a: called.append(n) or None)
    result = _caller(_tool(admin_only=True), hooks=hooks).execute("probe", {})
    assert result.startswith("Security Error:")
    assert called == []


def test_before_dispatch_can_add_arguments():
    tool = _tool()

    def _plumbing(name, tool_args):
        tool_args["_session_id"] = "s1"
        return None

    _caller(tool, hooks=ToolCallHooks(before_dispatch=_plumbing)).execute("probe", {})
    assert tool.seen["_session_id"] == "s1"


def test_before_dispatch_can_refuse():
    tool = _tool()
    hooks = ToolCallHooks(before_dispatch=lambda n, a: "already running")
    assert _caller(tool, hooks=hooks).execute("probe", {}) == "already running"
    assert tool.seen is None


def test_a_schema_error_outranks_a_refusal_about_another_call():
    """Measured precedence: the schema error is about THIS call, the refusal is about one
    already in flight. The call that cannot even be formed loses first."""
    strict = _tool(parameters={"type": "object", "properties": {"x": {"type": "string"}},
                               "required": ["x"]})
    hooks = ToolCallHooks(before_dispatch=lambda n, a: "already running")
    assert _caller(strict, hooks=hooks).execute("probe", {}).startswith("Tool Error: invalid arguments")


def test_after_dispatch_can_replace_the_result():
    hooks = ToolCallHooks(after_dispatch=lambda n, a, r: f"{r} + extra")
    assert _caller(_tool(), hooks=hooks).execute("probe", {}) == "OK + extra"


def test_after_dispatch_runs_before_truncation():
    """search_tools caps itself just under the limit, so a hook that appends must still be
    subject to the cut - not exempt from it."""
    hooks = ToolCallHooks(after_dispatch=lambda n, a, r: "y" * 5000)
    assert "[Output Truncated." in _caller(_tool(), hooks=hooks).execute("probe", {})


def test_after_emit_fires_only_when_something_was_dispatched():
    """Router recency: a blocked call must not count as the model having used the tool."""
    seen = []
    hooks = ToolCallHooks(after_emit=lambda n, r: seen.append(n))
    _caller(_tool(), hooks=hooks).execute("probe", {})
    assert seen == ["probe"]
    seen.clear()
    _caller(_tool(admin_only=True), hooks=hooks).execute("probe", {})
    assert seen == []


def test_no_hooks_is_the_bare_pipeline():
    assert _caller(_tool()).execute("probe", {}) == "OK"


# ── the property that makes it a library and not product code ────────────────

def test_the_shared_path_does_not_reach_for_the_product():
    """A caller with no web server and no terminal must get the same pipeline. If this
    module imported either, that claim would be false the moment a gate or an event fired."""
    import inspect

    from vaf.core import tool_dispatch

    src = inspect.getsource(tool_dispatch)
    assert "web_interface" not in src
    assert "vaf.cli" not in src


def test_the_module_stays_importable_on_the_slim_base():
    """Module level must be stdlib only; everything else is imported inside the function
    that needs it, so `import vaf` stays cheap and the base extra keeps its promise."""
    import inspect
    import re

    from vaf.core import tool_dispatch

    top = [l for l in inspect.getsource(tool_dispatch).split("\n")
           if re.match(r"^(import|from) ", l)]
    assert all("vaf" not in l for l in top), top
