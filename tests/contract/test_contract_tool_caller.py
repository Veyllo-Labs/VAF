# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `vaf.ToolCaller` / `vaf.ToolRequest` (docs/EMBEDDING.md, "Running a
tool yourself" and "Deciding about a tool call").

Everything here goes through the REAL dispatch pipeline with plain BaseTool
subclasses - the same object the agent's own dispatch uses is the promise, so a
stub would test nothing. Return strings are pinned by PREFIX or short substring
only (prose may be reworded); event shapes by key presence and types.
"""
import itertools

import pytest

import vaf

# A synthetic non-owner scope: role "user" plus this scope guarantees the caller
# never matches the machine's admin identity, so policy answers deterministically.
SCOPE = "deadbeef-0000-0000-0000-000000000000"


class _Recording(vaf.BaseTool):
    """Shared base for the probes: records every run() kwargs dict."""
    def __init__(self):
        self.calls = []

    def run(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        return "echo:" + str(kwargs.get("payload", ""))


class EchoTool(_Recording):
    name = "contract_echo"
    description = "Echoes its payload back."
    parameters = {"type": "object",
                  "properties": {"payload": {"type": "string"}}, "required": []}


class ScopeRoleTool(EchoTool):
    name = "contract_scope_role"
    identity_kwargs = ("user_scope_id", "user_role")


class UsernameTool(EchoTool):
    name = "contract_username"
    identity_kwargs = ("username",)


class AdminOnlyTool(EchoTool):
    name = "contract_admin_only"
    admin_only = True


class DangerousTool(EchoTool):
    name = "contract_dangerous"
    permission_level = "dangerous"


class RaisingTool(EchoTool):
    name = "contract_raising"

    def run(self, **kwargs) -> str:
        raise RuntimeError("probe exploded on purpose")


class NeedsArgTool(EchoTool):
    name = "contract_needs_arg"
    parameters = {"type": "object",
                  "properties": {"payload": {"type": "string"}},
                  "required": ["payload"]}


class LongTool(EchoTool):
    name = "contract_long"

    def run(self, **kwargs) -> str:
        return "x" * 5000


def test_a_registry_only_caller_runs_the_tool_and_returns_its_string():
    """The minimal documented use: a {name: instance} dict and execute()."""
    tool = EchoTool()
    assert vaf.ToolCaller({"t": tool}).execute("t", {"payload": "hi"}) == "echo:hi"
    assert tool.calls == [{"payload": "hi"}]


def test_an_explicit_username_reaches_a_tool_that_declares_it():
    """'Pass username if you serve more than one tenant' - a given name wins."""
    tool = UsernameTool()
    vaf.ToolCaller({"t": tool}, username="alice").execute("t", {})
    assert tool.calls[-1]["username"] == "alice"


def test_declared_identity_is_assigned_from_the_caller_never_from_the_model():
    """Model-spoofed identity args are overwritten with the caller's real values;
    that overwrite is the documented defense against prompt injection."""
    tool = ScopeRoleTool()
    caller = vaf.ToolCaller({"t": tool}, user_scope_id=SCOPE, user_role="user")
    caller.execute("t", {"payload": "p", "user_role": "admin",
                         "user_scope_id": "ab12cd34-0000-0000-0000-000000000000"})
    assert tool.calls[-1]["user_role"] == "user"
    assert tool.calls[-1]["user_scope_id"] == SCOPE


def test_a_tool_declaring_no_identity_receives_none_of_the_identity_keys():
    """Declaring nothing gets nothing - the safe direction, documented."""
    tool = EchoTool()
    vaf.ToolCaller({"t": tool}, user_scope_id=SCOPE, username="alice",
                   user_role="user").execute("t", {"payload": "hi"})
    assert tool.calls[-1] == {"payload": "hi"}


def test_a_nameless_foreign_scope_caller_gets_the_stable_synthetic_name():
    """No username + a non-owner scope resolves to 'scope_' + first 8 hex, so a
    tenant never lands on the owner's name-keyed data (documented in the
    identity row of the ToolCaller argument table)."""
    tool = UsernameTool()
    vaf.ToolCaller({"t": tool}, user_scope_id=SCOPE, user_role="user").execute("t", {})
    assert tool.calls[-1]["username"] == "scope_deadbeef"


def test_refusals_come_back_as_strings_with_the_documented_prefixes():
    """execute() never raises for tool failures; the prefixes are the contract,
    the prose after them is not."""
    caller = vaf.ToolCaller(
        {"admin": AdminOnlyTool(), "boom": RaisingTool(), "needs": NeedsArgTool()},
        user_scope_id=SCOPE, user_role="user",
    )
    assert caller.execute("admin", {}).startswith("Security Error:")
    assert caller.execute("boom", {}).startswith("Tool Error:")
    schema_refusal = caller.execute("needs", {})
    assert schema_refusal.startswith("Tool Error:")
    assert "invalid arguments" in schema_refusal
    assert caller.execute("no_such_tool", {}).startswith("Error: Unknown tool")


def test_a_gated_tool_without_a_human_is_refused_with_the_marker(tmp_path):
    """'Gated tools never hang or raise': headless, the refusal string CONTAINS
    vaf.markers.TOOL_CONFIRMATION_REQUIRED - substring semantics, documented."""
    tool = DangerousTool()
    result = vaf.ToolCaller({"t": tool}, interactive=False,
                            trust_dir=tmp_path).execute("t", {})
    assert vaf.markers.TOOL_CONFIRMATION_REQUIRED in result
    assert tool.calls == []


def test_the_gate_honours_the_decide_callback(tmp_path):
    """decide(tool_name, reason) -> 'cancel' refuses with the pinned prefix;
    'allow_once' lets exactly this tool run (in memory only - 'allow_always' is
    the one persistent write and is deliberately not exercised here)."""
    tool = DangerousTool()
    cancelled = vaf.ToolCaller(
        {"t": tool}, interactive=True, trust_dir=tmp_path,
        decide=lambda name, reason: "cancel",
    ).execute("t", {})
    assert cancelled.startswith("[CANCELLED]")
    assert tool.calls == []

    allowed = vaf.ToolCaller(
        {"t": tool}, interactive=True, trust_dir=tmp_path,
        decide=lambda name, reason: "allow_once",
    ).execute("t", {"payload": "go"})
    assert allowed == "echo:go"
    assert tool.calls == [{"payload": "go"}]


def test_a_successful_call_emits_exactly_a_tool_start_tool_end_pair():
    events = []
    vaf.ToolCaller({"t": EchoTool()}, on_event=events.append).execute(
        "t", {"payload": "hi"})
    assert [e["type"] for e in events] == ["tool_start", "tool_end"]
    start, end = events
    assert {"type", "tool", "args"} <= set(start) and start["tool"] == "t"
    assert {"type", "tool", "duration_ms", "ok", "result"} <= set(end)
    assert isinstance(end["duration_ms"], int)
    assert end["ok"] is True and isinstance(end["result"], str)


def test_failures_are_reported_on_tool_end_with_ok_false():
    events = []
    caller = vaf.ToolCaller({"boom": RaisingTool()}, on_event=events.append)
    assert caller.execute("boom", {}).startswith("Tool Error:")
    assert events[-1]["type"] == "tool_end" and events[-1]["ok"] is False

    events.clear()
    caller.execute("no_such_tool", {})
    assert [e["type"] for e in events] == ["tool_start", "tool_end"]
    assert events[-1]["ok"] is False


def test_a_hard_policy_block_emits_no_events_at_all():
    """Documented: an observer never sees a blocked tool reported as run."""
    events = []
    result = vaf.ToolCaller({"admin": AdminOnlyTool()}, user_scope_id=SCOPE,
                            user_role="user", on_event=events.append).execute("admin", {})
    assert result.startswith("Security Error:")
    assert events == []


def test_a_refused_headless_gate_emits_only_gate_required(tmp_path):
    """The published asymmetry: gate_required fires, gate_decision does not."""
    events = []
    vaf.ToolCaller({"t": DangerousTool()}, interactive=False, trust_dir=tmp_path,
                   on_event=events.append).execute("t", {"payload": "p"})
    assert [e["type"] for e in events] == ["gate_required"]
    assert {"tool", "cwd", "reason", "args_preview"} <= set(events[0])


def test_a_raising_event_sink_never_breaks_the_call():
    """Observation is fail-open by contract: a broken observer must not fail a
    run it only watches."""
    def sink(evt):
        raise RuntimeError("observer down")
    assert vaf.ToolCaller({"t": EchoTool()}, on_event=sink).execute(
        "t", {"payload": "hi"}) == "echo:hi"


def test_the_return_cut_and_the_event_cut_are_independent():
    """max_result_chars governs the caller's copy (None = off); the tool_end
    copy stays capped near 800 chars either way. The cap suffix is asserted
    tolerantly (suffix + length bound), not as an exact literal."""
    for chars, check in ((2000, lambda r: "Total length: 5000" in r),
                         (None, lambda r: r == "x" * 5000)):
        events = []
        result = vaf.ToolCaller({"t": LongTool()}, max_result_chars=chars,
                                on_event=events.append).execute("t", {})
        assert check(result)
        capped = events[-1]["result"]
        assert len(capped) < 900 and capped.endswith("chars]")


def _request(**overrides):
    kw = dict(tool_name="t", tool=None, args={"payload": "p"}, user_scope_id=SCOPE,
              username="alice", user_role="user", source="", session_id=None)
    kw.update(overrides)
    return vaf.ToolRequest(**kw)


def test_tool_request_precedence_is_deny_over_ask_over_allow_in_any_order():
    """Documented: 'the order you call them in cannot change the outcome'.
    A fresh request has no opinion, and allow() takes NO reason argument."""
    assert _request().decision is None
    for order in itertools.permutations(("deny", "ask", "allow")):
        req = _request()
        for kind in order:
            getattr(req, kind)()
        assert req.decision == "deny", f"order {order} did not settle on deny"
    for order in (("ask", "allow"), ("allow", "ask")):
        req = _request()
        for kind in order:
            getattr(req, kind)()
        assert req.decision == "ask"
    req = _request()
    req.deny("tenant quota exceeded")
    assert req.decision == "deny" and req.reason == "tenant quota exceeded"
    with pytest.raises(TypeError):
        _request().allow("not a thing")


def test_an_authorizer_deny_refuses_silently_and_a_raise_fails_closed():
    """deny() -> 'Security Error:' with zero events; a raising authorizer is a
    refusal too (a broken guard must not quietly become no guard)."""
    for authorize in (lambda req: req.deny("no shell on this plan"),
                      lambda req: (_ for _ in ()).throw(RuntimeError("auth db down"))):
        events, tool = [], EchoTool()
        result = vaf.ToolCaller({"t": tool}, authorize=authorize,
                                on_event=events.append).execute("t", {})
        assert result.startswith("Security Error:")
        assert events == [] and tool.calls == []


def test_authorizer_allow_skips_the_gate_and_ask_forces_it(tmp_path):
    """allow() un-gates a dangerous tool for THIS call only; ask() on a plain
    read tool forces the gate, which headless means a marker refusal."""
    tool = DangerousTool()
    result = vaf.ToolCaller({"t": tool}, interactive=False, trust_dir=tmp_path,
                            authorize=lambda r: r.allow()).execute("t", {"payload": "go"})
    assert result == "echo:go" and tool.calls == [{"payload": "go"}]

    plain = EchoTool()
    asked = vaf.ToolCaller({"t": plain}, interactive=False, trust_dir=tmp_path,
                           authorize=lambda r: r.ask("prove it")).execute("t", {})
    assert vaf.markers.TOOL_CONFIRMATION_REQUIRED in asked
    assert plain.calls == []


def test_request_args_are_a_snapshot_the_authorizer_cannot_edit():
    """Deciding is not editing: mutating req.args changes nothing downstream."""
    tool = EchoTool()

    def tamper(req):
        req.args["payload"] = "tampered"

    result = vaf.ToolCaller({"t": tool}, authorize=tamper).execute(
        "t", {"payload": "genuine"})
    assert result == "echo:genuine"
    assert tool.calls[-1]["payload"] == "genuine"


def test_every_execute_leaves_an_audit_line_including_refused_calls(contract_log_dir):
    """Documented: one line per call into tool_use_<date>.log BEFORE anything
    else happens, so a policy-refused call is recorded too; VAF_LOG_DIR (set by
    the autouse fixture) is the documented redirect."""
    caller = vaf.ToolCaller({"echo": EchoTool(), "admin": AdminOnlyTool()},
                            user_scope_id=SCOPE, user_role="user")
    caller.execute("echo", {"payload": "hi"})
    assert caller.execute("admin", {}).startswith("Security Error:")
    logs = list(contract_log_dir.glob("tool_use_*.log"))
    assert len(logs) == 1, f"expected one audit log, found {logs}"
    content = logs[0].read_text(encoding="utf-8")
    assert "tool=echo" in content
    assert "tool=admin" in content
