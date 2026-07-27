# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Two dispatcher decisions that the kwargs baseline could not see, and why they need values.

``tests/test_dispatch_kwargs_baseline.py`` freezes, per tool, the SET OF KEYS the dispatcher
adds. That is the right shape for plumbing - but it leaves two holes, and both were found by
adversarially re-reading the cascade rather than by any test going red.

**Hole 1: the value, not the key.** ``host_bash`` receives ``_is_channel_session`` in every
context, so the key-set baseline pins it and stays green whatever the value is. The receiving
guard is ``if kwargs.get("_is_channel_session")`` - plain truthiness, therefore FAIL-OPEN. A
missing value, a None, a refactor that hands over the key without the answer: all of them are
green in the key-set baseline and all of them let host_bash run over Telegram, WhatsApp and
Discord. That guard is explicitly non-liftable - it exists because there is no way to show a
confirmation on a channel, so a message could otherwise run host commands unconfirmed. It is
the one place where getting the plumbing wrong is a security bug rather than a cosmetic one.

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


def _dispatch(tool_name, context):
    """Run one dispatch in the named context and report what the tool received."""
    source, session_id = CONTEXTS[context]
    cls = _tool_class(tool_name)
    stub = _stub(cls)
    model_args = _required(getattr(cls, "parameters", None))
    fake = bind_chat_stages(SimpleNamespace(
        tools={tool_name: stub}, _event_sink=None, _allow_once_tools={tool_name},
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
        get_live_session_subagents=lambda: [], _extract_subagent_goal=lambda a: "",
        model_display_name="probe",
    ))
    with patch("vaf.core.trust.get_tool_policy", return_value="always"), \
         patch("vaf.core.trust.is_trusted_dir", return_value=True), \
         patch("vaf.core.config.Config.get",
               side_effect=lambda k, d=None: True if k == "channel_tools_unrestricted" else d):
        result = Agent.execute_tool(fake, tool_name, dict(model_args))
    assert stub.seen is not None, f"{tool_name} never ran in {context}: {result[:120]!r}"
    return stub.seen


# ── host_bash: the value is the whole point ──────────────────────────────────

@pytest.mark.parametrize("context", CHANNEL_CONTEXTS)
def test_host_bash_is_told_it_is_on_a_channel(context):
    """THE hole. The key-set baseline pins that the key arrives; only the VALUE closes the
    guard, and the guard reads plain truthiness, so a None reopens it silently."""
    assert _dispatch("host_bash", context).get("_is_channel_session") is True, (
        "host_bash was not told it is on a channel - its non-liftable guard reads truthiness, "
        "so it would run host commands from Telegram/WhatsApp/Discord unconfirmed"
    )


def test_host_bash_is_told_it_is_not_on_a_channel_in_the_web_app():
    """The other direction matters too: a permanently-true value would break the local app,
    which is the only place host_bash is meant to work."""
    assert _dispatch("host_bash", "web").get("_is_channel_session") is False


def test_the_channel_answer_is_never_merely_present():
    """Guards against the shape this file exists to catch: a key handed over without an
    answer. None is falsy, so it reads as 'not a channel'."""
    for context in CHANNEL_CONTEXTS + ["web"]:
        value = _dispatch("host_bash", context).get("_is_channel_session")
        assert isinstance(value, bool), f"{context}: got {value!r}, not a boolean"


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
        tools={"python_sandbox": stub}, _event_sink=None, _allow_once_tools={"python_sandbox"},
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
    assert _dispatch("host_bash", "channel_by_session_prefix").get("_is_channel_session") is True
