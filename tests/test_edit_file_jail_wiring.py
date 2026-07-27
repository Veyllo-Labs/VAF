# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Runtime proof that the dispatcher hands edit_file the caller's identity.

tests/test_edit_file_jail.py pins what the TOOL does once it has a scope. This file pins
the other half - that execute_tool actually delivers one - by running the real dispatcher
against a spy and reading what arrived. A source grep cannot tell an injection that fires
from one that sits in a branch nothing reaches.

The second assertion is the load-bearing one: ``tool_args`` starts life as the arguments
the MODEL produced, so the injection has to OVERWRITE. A model that emits
``user_role: "admin"`` (directly, or because a prompt talked it into doing so) must not be
able to hand itself an unjailed edit.

Attempted live instead, and worth recording so nobody repeats it: driving this through a
real chat turn proves nothing, because the local model never produced valid edit_file
arguments - twenty-one dispatches in a row ended in "invalid arguments", so the jail was
never reached and the untouched target file said nothing about the fix.
"""
from types import SimpleNamespace
from unittest.mock import patch

from conftest import bind_chat_stages
from vaf.core.agent import Agent
from vaf.tools.filesystem import EditFileTool

TENANT = "deadbeef-0000-0000-0000-000000000000"


class SpyEditFileTool(EditFileTool):
    """Same name, schema and contract as the real tool - only the body is replaced, so the
    dispatcher treats it exactly like edit_file and we see the kwargs it was handed."""

    def __init__(self):
        super().__init__()
        self.seen = None

    def run(self, **kwargs):
        self.seen = dict(kwargs)
        return "spy ok"


def _dispatch(model_args: dict, *, scope=TENANT, role="user"):
    spy = SpyEditFileTool()
    fake_agent = bind_chat_stages(SimpleNamespace(
        tools={"edit_file": spy},
        _event_sink=None,
        _allow_once_tools={"edit_file"},
        _noninteractive=True,
        _current_turn_thinking_mode=False,
        _current_chat_source="web",
        current_session_id=None,
        _current_user_scope_id=scope,
        _current_user_role=role,
        _current_username="tenant",
        _record_tool_used=lambda name: None,
        _plan_gate_decision=lambda name, tool, tool_args=None: None,
        _proactive_reply_gate_decision=lambda name, tool, args: None,
        _ask_first_gate_decision=lambda name, tool: None,
    ))
    with patch("vaf.core.trust.get_tool_policy", return_value="always"), patch(
        "vaf.core.trust.is_trusted_dir", return_value=True
    ):
        result = Agent.execute_tool(fake_agent, "edit_file", model_args)
    return spy, result


def test_the_dispatcher_hands_edit_file_the_callers_scope_and_role():
    spy, result = _dispatch({"path": "/tmp/x.txt", "edits": [{"search": "a", "replace": "b"}]})
    assert spy.seen is not None, f"edit_file never ran: {result}"
    assert spy.seen.get("user_scope_id") == TENANT
    assert spy.seen.get("user_role") == "user"


def test_a_model_supplied_role_is_overwritten_not_honored():
    """The escalation this closes: without an ASSIGN, a model could name its own role."""
    spy, result = _dispatch({
        "path": "/tmp/x.txt",
        "edits": [{"search": "a", "replace": "b"}],
        "user_role": "admin",
        "user_scope_id": "00000000-0000-0000-0000-000000000001",
    })
    assert spy.seen is not None, f"edit_file never ran: {result}"
    assert spy.seen.get("user_role") == "user", "the model's role claim survived"
    assert spy.seen.get("user_scope_id") == TENANT, "the model's scope claim survived"


def test_an_admin_session_is_passed_through_as_admin():
    spy, _ = _dispatch(
        {"path": "/tmp/x.txt", "edits": [{"search": "a", "replace": "b"}]},
        scope="abcdef12-0000-0000-0000-000000000000", role="admin",
    )
    assert spy.seen.get("user_role") == "admin"
