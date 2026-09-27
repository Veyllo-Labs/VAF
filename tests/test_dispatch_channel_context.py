# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Two dispatcher decisions that the kwargs baseline could not see, and why they need values.

``tests/test_dispatch_kwargs_baseline.py`` freezes, per tool, the SET OF KEYS the dispatcher
adds. That is the right shape for plumbing - but it leaves two holes, and both were found by
adversarially re-reading the cascade rather than by any test going red.

**Hole 1: the value, not the key.** ``host_bash`` used to receive ``_is_channel_session`` in
every context and guard on its truthiness, so the key-set baseline stayed green whatever the
value was - FAIL-OPEN. The refusal now lives in the policy (a dangerous tool with "channel" in
its restrictions is refused on a channel in the lane that would ask a person, before the admin's
lift), so what is pinned here is the OUTCOME: host_bash and python_exec never run on a channel,
with the lift ON, on either way a session becomes a channel. It is the one place where getting
the plumbing wrong is a security bug rather than a cosmetic one: measured before the policy
rule, python_exec ran from Telegram with a stored "always".

**Hole 2: the context itself.** ``with_vaf_tools=False`` is set for ``python_sandbox`` ONLY on
channel sessions. The key-set baseline measures one canonical context - a non-channel web turn
- so that line appears in no row at all. It could have been deleted, inverted or made
unconditional without a single test failing.

So this file pins VALUES across the contexts that matter. It also pins both ways a session
becomes a channel: the chat source ("telegram", matched exactly) and the session-id prefix
("telegram_..."), which is the form a resumed or drained session carries. Trusting the tool's
own guard instead of the dispatcher's is not equivalent - python_sandbox checks only the
source, so the prefix lane would be left open.
"""
import importlib
import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from conftest import bind_chat_stages
from vaf.core.agent import Agent
from vaf.tools.base import BaseTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID

# (label, chat source, session id) - the third is the resumed/drained form.
CONTEXTS = {
    "web": ("web", "s1"),
    "channel_by_source": ("telegram", "s1"),
    "channel_by_session_prefix": ("web", "telegram_42"),
}
CHANNEL_CONTEXTS = ["channel_by_source", "channel_by_session_prefix"]


def _tool_class(name):
    mod = importlib.import_module(f"vaf.tools.{name}")
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, BaseTool) and obj is not BaseTool and getattr(obj, "name", None) == name:
            return obj
    raise AssertionError(f"{name} no longer resolves to a tool class")


def _stub(orig):
    class _Stub(BaseTool):
        name = orig.name
        description = "stub"
        parameters = getattr(orig, "parameters", {"type": "object", "properties": {}})
        identity_kwargs = getattr(orig, "identity_kwargs", ())
        permission_level = getattr(orig, "permission_level", "read")
        admin_only = getattr(orig, "admin_only", False)
        channel_restrictions = getattr(orig, "channel_restrictions", ())

        def __init__(self):
            super().__init__()
            self.seen = None

        def run(self, **kwargs):
            self.seen = dict(kwargs)
            return "STUB_OK"

    return _Stub()


def _required(schema):
    schema = schema or {}
    props = schema.get("properties") or {}
    out = {}
    for field in schema.get("required") or []:
        spec = props.get(field) or {}
        t = spec.get("type")
        if isinstance(t, list):
            t = t[0]
        out[field] = (spec["enum"][0] if spec.get("enum") else
                      1 if t == "integer" else 1.0 if t == "number" else
                      False if t == "boolean" else [] if t == "array" else
                      {} if t == "object" else "probe")
    return out


def _run(tool_name, context):
    """Run one dispatch in the named context; (result, what the tool received or None)."""
    source, session_id = CONTEXTS[context]
    cls = _tool_class(tool_name)
    stub = _stub(cls)
    model_args = _required(getattr(cls, "parameters", None))
    fake = bind_chat_stages(SimpleNamespace(
        tools={tool_name: stub}, _event_sink=None,
        _noninteractive=True, _current_turn_thinking_mode=False,
        _current_chat_source=source, current_session_id=session_id,
        _current_user_scope_id=SCOPE, _current_user_role="admin",
        _current_username="tenant", _run_kind="chat", _ww_training=False,
        _active_tools=set(), _turn_ran_progress_tool=False, _session_workspace=None,
        history=[], main_persistence=None, _record_tool_used=lambda n: None,
        _plan_gate_decision=lambda n, t, tool_args=None: None,
        _working_memory_note_gate=lambda tool_args: None,
        _proactive_reply_gate_decision=lambda n, t, a: None,
        _ask_first_gate_decision=lambda n, t: None,
        _room_mode_gate_decision=lambda n, t: None,
        get_live_session_subagents=lambda: [], _extract_subagent_goal=lambda a: "",
        model_display_name="probe",
    ))
    with patch("vaf.core.trust.get_tool_policy", return_value="always"), \
         patch("vaf.core.trust.is_trusted_dir", return_value=True), \
         patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: True if k == "channel_tools_unrestricted" else d):
        result = Agent.execute_tool(fake, tool_name, dict(model_args))
    return result, stub.seen


def _dispatch(tool_name, context):
    """Run one dispatch in the named context and report what the tool received."""
    result, seen = _run(tool_name, context)
    assert seen is not None, f"{tool_name} never ran in {context}: {result[:120]!r}"
    return seen


# ── the host tools: the outcome is the whole point ───────────────────────────

@pytest.mark.parametrize("tool_name", ["host_bash", "python_exec"])
@pytest.mark.parametrize("context", CHANNEL_CONTEXTS)
def test_a_host_tool_never_runs_on_a_channel(tool_name, context):
    """THE hole. With the admin's lift ON (the shipped default) the confirmation is lifted
    too, and a channel cannot show one: the tool must be refused before it runs."""
    result, seen = _run(tool_name, context)
    assert seen is None and result.startswith("Security Error"), (
        f"{tool_name} ran in {context}: host commands from Telegram/WhatsApp/Discord unconfirmed"
    )


@pytest.mark.parametrize("tool_name", ["host_bash", "python_exec"])
def test_a_host_tool_runs_in_the_web_app(tool_name):
    """The other direction matters too: a refusal everywhere would break the local app,
    which is the only place these tools are meant to work."""
    seen = _dispatch(tool_name, "web")
    assert "_is_channel_session" not in seen, "the tool is no longer handed the flag"


# ── python_sandbox: the line the canonical context never reaches ─────────────

@pytest.mark.parametrize("context", CHANNEL_CONTEXTS)
def test_the_sandbox_tool_bridge_is_off_on_channels(context):
    """Sandbox code can call back into the host tool registry. Not from a messaging channel."""
    assert _dispatch("python_sandbox", context).get("with_vaf_tools") is False


def test_the_sandbox_tool_bridge_is_left_alone_in_the_web_app():
    """Off-by-dispatcher only on channels; elsewhere the model's own choice stands, since
    with_vaf_tools is a declared schema parameter."""
    assert "with_vaf_tools" not in _dispatch("python_sandbox", "web")


def test_a_model_cannot_ask_for_the_bridge_back_on_a_channel():
    """The line overrides a MODEL-supplied value - that is why it is an assignment and not a
    default. Without it, asking for the bridge would be enough to get it."""
    source, session_id = CONTEXTS["channel_by_source"]
    cls = _tool_class("python_sandbox")
    stub = _stub(cls)
    fake = bind_chat_stages(SimpleNamespace(
        tools={"python_sandbox": stub}, _event_sink=None,
        _noninteractive=True, _current_turn_thinking_mode=False,
        _current_chat_source=source, current_session_id=session_id,
        _current_user_scope_id=SCOPE, _current_user_role="admin",
        _current_username="tenant", _run_kind="chat", _ww_training=False,
        _active_tools=set(), _turn_ran_progress_tool=False, _session_workspace=None,
        history=[], main_persistence=None, _record_tool_used=lambda n: None,
        _plan_gate_decision=lambda n, t, tool_args=None: None,
        _working_memory_note_gate=lambda tool_args: None,
        _proactive_reply_gate_decision=lambda n, t, a: None,
        _ask_first_gate_decision=lambda n, t: None,
        _room_mode_gate_decision=lambda n, t: None,
        get_live_session_subagents=lambda: [], _extract_subagent_goal=lambda a: "",
        model_display_name="probe",
    ))
    args = dict(_required(getattr(cls, "parameters", None)))
    args["with_vaf_tools"] = True
    with patch("vaf.core.trust.get_tool_policy", return_value="always"), \
         patch("vaf.core.trust.is_trusted_dir", return_value=True), \
         patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: True if k == "channel_tools_unrestricted" else d):
        Agent.execute_tool(fake, "python_sandbox", args)
    assert stub.seen.get("with_vaf_tools") is False, "a model-supplied value survived"


# ── both ways a session becomes a channel ────────────────────────────────────

def test_the_session_prefix_alone_makes_it_a_channel():
    """Pinned separately because the dispatcher's check is strictly BROADER than the tool's
    own: python_sandbox looks at the chat source only, so relying on the tool's guard would
    leave this lane - a resumed or drained channel session - open."""
    seen = _dispatch("python_sandbox", "channel_by_session_prefix")
    assert seen.get("with_vaf_tools") is False
    result, ran = _run("host_bash", "channel_by_session_prefix")
    assert ran is None and result.startswith("Security Error")


# ── the rule itself, at the policy and with the real python_exec ─────────────

def test_the_rule_needs_both_declarations_and_the_gated_lane():
    """Dangerous AND off every channel is what "needs a person" means. A write-level tool that
    is kept off channels (browser_agent) is still lifted by the admin's switch, as before, and
    the unattended lanes (gate off) are not refused by this rule."""
    from vaf.core.tool_contract import evaluate_tool_policy

    def decide(tool_name, *, gated_lane=True):
        with patch("vaf.core.config.Config.get",
                   side_effect=lambda k, d=None: True if k == "channel_tools_unrestricted" else d):
            return evaluate_tool_policy(tool_name=tool_name, tool=_tool_class(tool_name)(),
                                        current_source="telegram", is_channel_session=True,
                                        is_admin=True, gated_lane=gated_lane)

    assert decide("host_bash").blocked and decide("python_exec").blocked
    assert not decide("browser_agent").blocked, "a convenience tool keeps the admin's lift"
    assert not decide("host_bash", gated_lane=False).blocked, "the coder may still build"


def test_python_exec_from_telegram_does_not_run_with_a_stored_always(tmp_path):
    """Measured before the rule: the lift took the confirmation away, and python_exec's own
    check let the stored "always" through, so a Telegram message ran host Python."""
    from vaf.core.tool_dispatch import ToolCaller
    marker = tmp_path / "ran.txt"
    caller = ToolCaller({"python_exec": _tool_class("python_exec")()}, source="telegram",
                        session_id="telegram_42", interactive=False, user_role="admin")
    with patch("vaf.core.trust.get_tool_policy", return_value="allow"), \
         patch("vaf.tools.python_exec.get_tool_policy", return_value="allow"):
        out = caller.execute("python_exec", {"code": f"open({str(marker)!r}, 'w').write('x')"})
    assert out.startswith("Security Error"), out[:200]
    assert not marker.exists(), "host Python ran from a messaging channel"
