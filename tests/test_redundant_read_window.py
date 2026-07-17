# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Windowed redundant-read detection (_find_redundant_read_call).

The streaming loop's adjacent redundant block compares a new tool call only
against the NEWEST tool message, so a single interleaved call (a failed
workflow attempt, a plan-gate bounce) hid a verbatim re-search from it - live
incident: a weak model re-ran its two web searches word for word after a
workflow detour, four wasted calls in one turn (plus a doubled sibling within
one response, covered by the in-batch dedupe key).

Contract pinned here: a pure lookup is refused only when the identical
(name, args) call already SUCCEEDED in the current turn AND no mutating tool
succeeded since; everything else stays allowed (fail-open).
"""
import json

from vaf.core.agent import (
    _find_redundant_read_call,
    _is_window_dedup_tool,
    _normalize_tool_args,
)


def _turn(*entries):
    """Build a history list: ('tool', name, args, result) | ('assistant', ...) | ('user', text).
    Tool entries auto-wire tool_call_id and the producing assistant message."""
    history = [{"role": "user", "content": "do the thing"}]
    n = 0
    for e in entries:
        if e[0] == "tool":
            _, name, args, result = e
            n += 1
            cid = f"call_{n}"
            history.append({"role": "assistant", "tool_calls": [
                {"id": cid, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}]})
            history.append({"role": "tool", "tool_call_id": cid, "name": name,
                            "content": result})
        elif e[0] == "user":
            history.append({"role": "user", "content": e[1]})
        else:
            history.append({"role": "assistant", "content": e[1]})
    return history


Q = {"query": "wetter Berlin heute"}


def test_identical_successful_search_this_turn_is_redundant():
    h = _turn(("tool", "web_search", Q, "### Web Search Results ..."))
    assert _find_redundant_read_call(h, "web_search", json.dumps(Q)) is True


def test_interleaved_failed_calls_do_not_hide_the_duplicate():
    """The incident shape: searches, then a plan-gate bounce and a failed
    workflow attempt, then the verbatim re-search. The adjacent check was
    blind here; the windowed one must not be."""
    h = _turn(
        ("tool", "web_search", Q, "### Web Search Results ..."),
        ("tool", "write_file", {"path": "x.html"}, "[PLAN REQUIRED] set your approach first"),
        ("tool", "execute_workflow", {"workflow_id": "create_agent_workflow"},
         "❌ 'create_agent_workflow' is the name of a TOOL, not a saved workflow template"),
    )
    assert _find_redundant_read_call(h, "web_search", json.dumps(Q)) is True


def test_different_arguments_are_never_blocked():
    h = _turn(("tool", "web_search", Q, "### Web Search Results ..."))
    assert _find_redundant_read_call(
        h, "web_search", json.dumps({"query": "weather New York"})) is False


def test_key_order_and_whitespace_do_not_defeat_the_match():
    h = _turn(("tool", "web_search", {"query": "x", "limit": 5}, "### results"))
    assert _find_redundant_read_call(
        h, "web_search", '{ "limit": 5, "query": "x" }') is True


def test_successful_mutation_in_between_clears_the_block():
    """A re-read after a successful write can legitimately differ - fail open."""
    h = _turn(
        ("tool", "read_file", {"path": "notes.md"}, "old content"),
        ("tool", "write_file", {"path": "notes.md", "content": "new"},
         "File written successfully to notes.md"),
    )
    assert _find_redundant_read_call(h, "read_file", json.dumps({"path": "notes.md"})) is False


def test_failed_mutation_in_between_does_not_clear_the_block():
    h = _turn(
        ("tool", "web_search", Q, "### results"),
        ("tool", "write_file", {"path": "x.html"}, "[PLAN REQUIRED] set a plan first"),
    )
    assert _find_redundant_read_call(h, "web_search", json.dumps(Q)) is True


def test_earlier_FAILED_read_may_be_retried():
    h = _turn(("tool", "web_search", Q, "Search failed: HTTP 502"))
    assert _find_redundant_read_call(h, "web_search", json.dumps(Q)) is False


def test_previous_turn_results_are_not_in_scope():
    """The window stops at the user message: a NEW user turn may legitimately
    repeat last turn's search (freshness is the user's call)."""
    h = _turn(
        ("tool", "web_search", Q, "### results"),
        ("user", "und nochmal bitte aktuell"),
    )
    assert _find_redundant_read_call(h, "web_search", json.dumps(Q)) is False


def test_mutating_tools_are_never_windowed_blocked():
    h = _turn(("tool", "write_file", {"path": "x", "content": "c"},
               "File written successfully to x"))
    assert _find_redundant_read_call(
        h, "write_file", json.dumps({"path": "x", "content": "c"})) is False


def test_window_cap_limits_the_scan():
    entries = [("tool", "web_search", Q, "### results")]
    entries += [("tool", "list_files", {"path": f"/d{i}"}, "listing") for i in range(15)]
    h = _turn(*entries)
    # The identical search is 16 tool messages back - beyond the 12-message window.
    assert _find_redundant_read_call(h, "web_search", json.dumps(Q)) is False


def test_classification_and_normalization_helpers():
    assert _is_window_dedup_tool("web_search") is True
    assert _is_window_dedup_tool("read_file") is True       # read_ prefix
    assert _is_window_dedup_tool("list_automations") is True
    assert _is_window_dedup_tool("write_file") is False
    assert _is_window_dedup_tool("send_telegram") is False
    assert _is_window_dedup_tool("create_agent_workflow") is False
    assert _normalize_tool_args('{"b": 1, "a": 2}') == _normalize_tool_args({"a": 2, "b": 1})
    assert _normalize_tool_args("  plain  ") == "plain"


def test_never_raises_on_garbage_history():
    garbage = [None, {"role": "tool"}, {"role": "assistant", "tool_calls": [None]},
               {"role": "tool", "name": "web_search", "tool_call_id": "nope",
                "content": None}]
    assert _find_redundant_read_call(garbage, "web_search", "{bad json") is False
