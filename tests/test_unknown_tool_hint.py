# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""An unknown tool name is answered with a correction, not only refused.

Live incident: a model called ``mark_task_done`` as a tool. It is a parameter of
``update_working_memory``, and the tool result it had just read said "call mark_task_done
on it", so the guess was ours before it was the model's. The dispatch pipeline answered
``Error: Unknown tool 'mark_task_done'`` and nothing else, twice, and the model then took
the other way out that result had offered: it confirmed a full wipe of its task list
instead of marking two finished steps done.

The prefix ``Error: Unknown tool`` is contract (the event ``ok`` flag and
tests/test_dispatch_event_baseline.py pin it); what these tests pin is the correction that
follows it, shape by shape, and that the hint can never take the refusal down with it.
"""
from vaf.core.tool_dispatch import ToolCaller, unknown_tool_hint
from vaf.tools.base import BaseTool

SCOPE = "deadbeef-0000-0000-0000-000000000000"   # synthetic; never a real scope UUID


def _tool(name, props, required=()):
    cls = type(f"_T_{name}", (BaseTool,), {
        "name": name,
        "description": name,
        "permission_level": "read",
        "parameters": {"type": "object", "properties": props, "required": list(required)},
        "run": lambda self, **kw: "OK",
    })
    return cls()


def _registry(*tools):
    return {t.name: t for t in tools}


def _wm():
    return _tool("update_working_memory", {
        "add_task": {"type": "string"},
        "mark_task_done": {"type": "integer"},
        "tasks": {"type": "array"},
    })


def _mail():
    return _tool("find_mail", {"query": {"type": "string"}, "limit": {"type": "integer"}},
                 required=("query",))


def _inbox():
    return _tool("inbox", {"query": {"type": "string"}, "channel": {"type": "string"}})


def _search():
    return _tool("search_tools", {"query": {"type": "string"}}, required=("query",))


def _caller(registry, events=None):
    return ToolCaller(registry, user_scope_id=SCOPE, user_role="user", username="tenant",
                      on_event=(events.append if events is not None else None))


# ── shape 1: a parameter called as a tool ────────────────────────────────────

def test_a_parameter_called_as_a_tool_is_redirected_to_the_exact_call():
    events = []
    result = _caller(_registry(_wm()), events).execute("mark_task_done", {"index": 0})
    assert result.startswith("Error: Unknown tool 'mark_task_done'"), "the prefix is contract"
    assert "'mark_task_done' is a parameter of update_working_memory, not a tool" in result
    assert "call update_working_memory(mark_task_done=0) instead" in result, \
        "the value the model already passed is carried into the corrected call"
    assert events[-1]["type"] == "tool_end" and events[-1]["ok"] is False


def test_a_call_with_several_arguments_gets_a_placeholder_not_a_guess():
    result = _caller(_registry(_wm())).execute("mark_task_done", {"index": 0, "note": "x"})
    assert "update_working_memory(mark_task_done=...)" in result


def test_a_string_value_is_quoted_in_the_corrected_call():
    result = _caller(_registry(_wm())).execute("add_task", {"text": "verify the entry"})
    assert 'update_working_memory(add_task="verify the entry")' in result


def test_a_parameter_shared_by_several_tools_names_them_all():
    result = _caller(_registry(_mail(), _inbox())).execute("query", {"query": "x"})
    assert "'query' is a parameter of find_mail, inbox, not a tool" in result
    assert "call one of those with query=..." in result


def test_many_owners_are_capped_with_a_count():
    tools = [_tool(f"tool_{i}", {"query": {"type": "string"}}) for i in range(5)]
    hint = unknown_tool_hint("query", _registry(*tools), {}, limit=3)
    assert "tool_0, tool_1, tool_2 and 2 more" in hint


# ── shape 2: a near miss of a tool name ──────────────────────────────────────

def test_a_near_miss_gets_the_closest_names_with_their_signatures():
    result = _caller(_registry(_mail(), _inbox())).execute("find_mails", {})
    assert result.startswith("Error: Unknown tool 'find_mails'")
    assert "Did you mean find_mail(query: string, [limit: integer])" in result, \
        "the same signature rendering search_tools gives a discovered tool"


def test_the_near_miss_is_case_insensitive():
    result = _caller(_registry(_mail())).execute("Find_Mail", {})
    assert "find_mail(query: string" in result


def test_a_parameter_match_outranks_a_near_miss():
    # "tasks" is a parameter of update_working_memory AND close to a tool named "task".
    registry = _registry(_wm(), _tool("task", {"id": {"type": "string"}}))
    result = _caller(registry).execute("tasks", {"tasks": []})
    assert "is a parameter of update_working_memory" in result
    assert "Did you mean" not in result


# ── shape 3: nothing near ────────────────────────────────────────────────────

def test_nothing_near_points_at_discovery_when_the_registry_has_it():
    result = _caller(_registry(_mail(), _search())).execute("zzz_totally_else", {})
    assert 'call search_tools(query="...")' in result


def test_without_any_hint_the_refusal_is_byte_identical_to_before():
    result = _caller(_registry(_mail())).execute("zzz_totally_else", {})
    assert result == "Error: Unknown tool 'zzz_totally_else'"


# ── the hint can never cost the refusal ──────────────────────────────────────

def test_a_malformed_registry_still_gets_the_refusal():
    broken = _tool("broken", {})
    broken.parameters = "not a schema"
    nothing = _tool("nothing", {})
    nothing.parameters = None
    result = _caller(_registry(broken, nothing)).execute("mark_task_done", {"index": 0})
    assert result.startswith("Error: Unknown tool 'mark_task_done'")


def test_a_value_that_is_not_json_falls_back_to_a_placeholder():
    result = _caller(_registry(_wm())).execute("mark_task_done", {"index": object()})
    assert "update_working_memory(mark_task_done=...)" in result


def test_an_empty_registry_and_an_empty_name_answer_nothing():
    assert unknown_tool_hint("", _registry(_wm()), {}) == ""
    assert unknown_tool_hint("mark_task_done", {}, {}) == ""
    assert unknown_tool_hint(None, None, None) == ""
