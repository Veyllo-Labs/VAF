# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A tool declares which parts of the caller's identity it needs; the dispatcher obeys.

`execute_tool` used to hand out `user_scope_id` / `username` / `user_role` from roughly
forty hardcoded lists of OUR tool names. That had two costs. It drifted - a tool added to
one list and forgotten in the sibling one is exactly how the write/read jail ended up
asymmetric. And a tool an embedder registers through `Agent.add_tool()` could never receive
an identity at all, because the dispatcher only recognised names it already knew;
`docs/EMBEDDING.md` could only warn about that, with no primitive to point at.

`BaseTool.identity_kwargs` is that primitive, in the shape the class already uses for
`channel_restrictions`. A tuple rather than a boolean because the real usage has five
distinct combinations: 51 tools take scope+username, 22 scope only, 15 role+scope, and one
each of username-only and all three. A boolean would have to pass all three, and tools that
never received `username` would start seeing one - `mail_utils.store_username_from_kwargs`
reads exactly that value, so it would be a silent behaviour change.

THE POINT OF THIS FILE is the frozen baseline below. It is the identity map as it was
measured from the dispatcher immediately BEFORE the migration, tool by tool. The migration
is only correct if every tool still receives precisely what it received then - not one key
fewer (a tool silently loses its scope and reads another tenant's data) and not one more
(a tool starts branching on a value it never had). That is a claim which has to be
checkable, so it is checked here rather than asserted in a commit message.
"""
import importlib
import inspect
import pkgutil
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import vaf.core.agent as agent_mod
from vaf.core.agent import Agent
from vaf.tools.base import BaseTool

# ── The frozen baseline: what each tool received from the hardcoded name lists ──
# Extracted from execute_tool before the migration. Do NOT "fix" an entry to make a test
# pass - a mismatch means the migration changed what a tool gets, which is the bug this
# file exists to catch.
IDENTITY_BASELINE = {
    "add_automation_note": ["user_scope_id"],
    "add_automation_todo": ["user_scope_id"],
    "analyze_image": ["user_scope_id"],
    "archive_mail": ["user_scope_id", "username"],
    "ask_user": ["user_scope_id", "username"],
    "browser_agent": ["user_scope_id"],
    "create_automation": ["user_role", "user_scope_id"],
    "create_calendar_event": ["user_scope_id", "username"],
    "create_contact": ["user_scope_id", "username"],
    "create_skill": ["user_scope_id", "username"],
    "delete_automation": ["user_role", "user_scope_id"],
    "delete_automation_note": ["user_scope_id"],
    "delete_automation_todo": ["user_scope_id"],
    "delete_calendar_event": ["user_scope_id", "username"],
    "delete_contact": ["user_scope_id", "username"],
    "delete_mail": ["user_scope_id", "username"],
    "delete_skill": ["user_scope_id", "username"],
    "discord_inbox": ["user_scope_id", "username"],
    "document_editor": ["user_scope_id"],
    "document_viewer": ["user_scope_id"],
    "edit_file": ["user_role", "user_scope_id"],
    "find_discord_messages": ["user_scope_id", "username"],
    "find_files": ["user_role", "user_scope_id"],
    "find_mail": ["user_scope_id", "username"],
    "find_telegram_messages": ["user_scope_id", "username"],
    "find_whatsapp_messages": ["user_scope_id", "username"],
    "folder_size": ["user_role", "user_scope_id"],
    "forward_mail": ["user_scope_id", "username"],
    "get_contact": ["user_scope_id", "username"],
    "github_create_issue": ["user_scope_id", "username"],
    "github_get_file": ["user_scope_id", "username"],
    "github_get_file_structure": ["user_scope_id", "username"],
    "github_get_tree": ["user_scope_id", "username"],
    "github_list_directory": ["user_scope_id", "username"],
    "github_list_issues": ["user_scope_id", "username"],
    "github_list_pulls": ["user_scope_id", "username"],
    "github_list_repos": ["user_scope_id", "username"],
    "github_search_files": ["user_scope_id", "username"],
    "github_update_file": ["user_scope_id", "username"],
    "label_mail": ["user_scope_id", "username"],
    "learn_attached_knowledge": ["user_scope_id"],
    "learn_document": ["user_scope_id"],
    "librarian_agent": ["user_role", "user_scope_id"],
    "list_automation_notes": ["user_scope_id"],
    "list_automation_todos": ["user_scope_id"],
    "list_automations": ["user_role", "user_scope_id"],
    "list_calendar_events": ["user_scope_id", "username"],
    "list_contacts": ["user_scope_id", "username"],
    "list_email_accounts": ["user_scope_id", "username"],
    "list_files": ["user_role", "user_scope_id"],
    "list_skills": ["user_scope_id", "username"],
    "list_trash": ["user_role", "user_scope_id"],
    "mail_inbox": ["user_scope_id", "username"],
    "mark_mail_answered": ["user_scope_id", "username"],
    "memory_save": ["user_scope_id"],
    "memory_search": ["user_scope_id"],
    "python_sandbox": ["user_scope_id"],
    "read_automation": ["user_role", "user_scope_id"],
    "read_discord_chat": ["user_scope_id", "username"],
    "read_file": ["user_role", "user_scope_id"],
    "read_mail": ["user_scope_id", "username"],
    "read_skill": ["user_scope_id", "username"],
    "read_telegram_chat": ["user_scope_id", "username"],
    "read_whatsapp_chat": ["user_scope_id", "username"],
    "replace_editor_selection": ["user_scope_id"],
    "reply_mail": ["user_scope_id", "username"],
    "restore_automation": ["user_role", "user_scope_id"],
    "schedule_reminder": ["user_scope_id", "username"],
    "send_discord": ["user_scope_id", "username"],
    "send_mail": ["user_role", "user_scope_id", "username"],
    "send_slack": ["user_scope_id", "username"],
    "send_telegram": ["user_scope_id", "username"],
    "send_to_user": ["user_scope_id", "username"],
    "send_whatsapp": ["user_scope_id", "username"],
    "telegram_inbox": ["user_scope_id", "username"],
    "thinking_workspace_handoff": ["user_scope_id"],
    "thinking_workspace_read": ["user_scope_id"],
    "thinking_workspace_write": ["user_scope_id"],
    "tree": ["user_role", "user_scope_id"],
    "update_automation": ["user_role", "user_scope_id"],
    "update_calendar_event": ["user_scope_id", "username"],
    "update_contact": ["user_scope_id", "username"],
    "update_intent": ["user_scope_id"],
    "update_skill": ["user_scope_id", "username"],
    "update_user_identity": ["username"],
    "update_working_memory": ["user_scope_id"],
    "use_skill": ["user_scope_id"],
    "whatsapp_call": ["user_scope_id", "username"],
    "whatsapp_inbox": ["user_scope_id", "username"],
    "write_file": ["user_role", "user_scope_id"],
}

# ask_user is deliberately NOT declarative and stays a hand-written branch: it injects the
# identity ONLY for background runs (_run_kind in automation/thinking). The reason is in the
# dispatcher comment - a non-admin's private question would otherwise be delivered to the
# ADMIN's messenger. A declaration injects unconditionally, which would change that.
CONDITIONAL_BY_DESIGN = {"ask_user"}

VALID_KEYS = {"user_scope_id", "username", "user_role"}


def _all_tool_classes():
    import vaf.core
    import vaf.tools
    seen = {}
    for pkg in (vaf.tools, vaf.core):
        for m in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
            try:
                mod = importlib.import_module(m.name)
            except Exception:
                continue
            for _, obj in inspect.getmembers(mod, inspect.isclass):
                if issubclass(obj, BaseTool) and obj is not BaseTool and getattr(obj, "name", None):
                    seen[obj.name] = obj
    return seen


TOOLS = _all_tool_classes()


# ── The migration must not have changed what any tool receives ───────────────

@pytest.mark.parametrize("tool_name", sorted(set(IDENTITY_BASELINE) - CONDITIONAL_BY_DESIGN))
def test_every_tool_declares_exactly_what_it_used_to_be_given(tool_name):
    cls = TOOLS.get(tool_name)
    assert cls is not None, f"{tool_name} no longer resolves to a tool class"
    declared = sorted(getattr(cls, "identity_kwargs", ()) or ())
    expected = sorted(IDENTITY_BASELINE[tool_name])
    assert declared == expected, (
        f"{tool_name} received {expected} before the migration but declares {declared}. "
        "Fewer keys = it silently loses its scope; more = it starts branching on a value "
        "it never had."
    )


def test_the_one_conditional_tool_stays_undeclared():
    """ask_user must NOT declare - its injection is conditional on the run kind."""
    for name in CONDITIONAL_BY_DESIGN:
        cls = TOOLS.get(name)
        assert cls is not None
        assert not getattr(cls, "identity_kwargs", ()), (
            f"{name} declared identity_kwargs, which injects unconditionally - but its "
            "branch injects only for background runs, on purpose."
        )


def test_no_tool_declares_a_key_the_dispatcher_cannot_supply():
    bad = {n: c.identity_kwargs for n, c in TOOLS.items()
           if set(getattr(c, "identity_kwargs", ()) or ()) - VALID_KEYS}
    assert not bad, f"unknown identity keys (silently ignored at dispatch): {bad}"


def test_the_dispatcher_no_longer_hands_out_identity_by_tool_name():
    """The deletion this change is for. A leftover name list would drift again, and would
    still leave a third-party tool with nothing.

    Scans the WHOLE of execute_tool rather than a delimited region: the assignment has since
    moved into vaf/core/tool_dispatch.py, and a guard anchored on two markers inside a method
    stops guarding the moment either marker moves. Whole-method is both simpler and stricter.
    """
    import inspect

    src = inspect.getsource(agent_mod.Agent.execute_tool)
    leftovers = re.findall(r'tool_args\["(user_scope_id|username|user_role)"\]\s*=', src)
    # ask_user's conditional branch is the single sanctioned exception (2 keys) - its
    # injection depends on the run kind, so it cannot become a declaration.
    assert len(leftovers) <= 2, (
        f"identity is assigned by tool NAME in {len(leftovers)} places: {leftovers}"
    )


# ── Runtime: the declaration is actually honoured, for anyone ────────────────

class _SpyTool(BaseTool):
    """A tool the dispatcher has never heard of - i.e. what an embedder registers."""
    name = "third_party_spy"
    description = "spy"
    permission_level = "read"
    identity_kwargs = ("user_scope_id", "user_role")
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        super().__init__()
        self.seen = None

    def run(self, **kwargs):
        self.seen = dict(kwargs)
        return "ok"


def _dispatch(tool, model_args, *, scope="deadbeef-0000-0000-0000-000000000000",
              role="user", username="tenant"):
    fake = SimpleNamespace(
        tools={tool.name: tool}, _event_sink=None, _allow_once_tools={tool.name},
        _noninteractive=True, _current_turn_thinking_mode=False, _current_chat_source="web",
        current_session_id=None, _current_user_scope_id=scope, _current_user_role=role,
        _current_username=username, _record_tool_used=lambda name: None,
        _plan_gate_decision=lambda name, tool, tool_args=None: None,
        _proactive_reply_gate_decision=lambda name, tool, args: None,
        _ask_first_gate_decision=lambda name, tool: None,
    )
    with patch("vaf.core.trust.get_tool_policy", return_value="always"), patch(
        "vaf.core.trust.is_trusted_dir", return_value=True
    ):
        return Agent.execute_tool(fake, tool.name, model_args)


def test_a_tool_the_dispatcher_never_heard_of_receives_its_identity():
    """THE framework half. Before this, identity came from a list of built-in names, so a
    tool registered via Agent.add_tool() got nothing no matter what it did."""
    spy = _SpyTool()
    result = _dispatch(spy, {})
    assert spy.seen is not None, f"tool never ran: {result}"
    assert spy.seen.get("user_scope_id") == "deadbeef-0000-0000-0000-000000000000"
    assert spy.seen.get("user_role") == "user"


def test_only_the_declared_keys_are_passed():
    """Declaring scope+role must not also deliver username - that is the whole reason the
    declaration is a tuple and not a boolean."""
    spy = _SpyTool()
    _dispatch(spy, {})
    assert "username" not in spy.seen


def test_a_model_supplied_identity_is_overwritten_not_honored():
    """The escalation this closes: arguments start out as whatever the MODEL produced."""
    spy = _SpyTool()
    _dispatch(spy, {"user_role": "admin", "user_scope_id": "00000000-0000-0000-0000-000000000001"})
    assert spy.seen.get("user_role") == "user"
    assert spy.seen.get("user_scope_id") == "deadbeef-0000-0000-0000-000000000000"


def test_a_tool_that_declares_nothing_receives_nothing():
    """The safe direction: not declaring means not getting, rather than getting by accident."""
    class _Quiet(_SpyTool):
        name = "third_party_quiet"
        identity_kwargs = ()

    quiet = _Quiet()
    _dispatch(quiet, {})
    assert not (VALID_KEYS & set(quiet.seen)), quiet.seen
