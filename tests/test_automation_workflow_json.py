# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Robust workflow-JSON extraction for create_automation.

The model often wraps the workflow array in prose/markdown or emits trailing text or a second array. The
old greedy ``re.search(r'\\[.*\\]')`` grabbed from the first ``[`` to the LAST ``]`` and json.loads then
failed with 'Extra data ...', routing the run into the (previously unbounded) prompt-based fallback — the
runaway entry point. `_extract_first_json_array` returns the first bracket-balanced array that parses to a
list, tolerating all of the above.
"""
from vaf.tools.automation import _extract_first_json_array as f


def test_clean_array():
    assert f('[{"tool": "web_search"}]') == [{"tool": "web_search"}]


def test_array_wrapped_in_prose():
    assert f('Here is the workflow:\n[{"tool": "a"}]\nDone.') == [{"tool": "a"}]


def test_extra_data_returns_first_valid_array():
    # The exact failure mode that crashed the greedy regex ("Extra data: line N").
    assert f('[{"tool": "a"}]\n\nSome explanation.\n\n[{"tool": "b"}]') == [{"tool": "a"}]


def test_nested_arrays_preserved():
    assert f('[{"args": {"x": [1, 2, 3]}}]') == [{"args": {"x": [1, 2, 3]}}]


def test_markdown_code_block():
    assert f('```json\n[{"tool": "a"}]\n```') == [{"tool": "a"}]


def test_bracket_inside_string_not_treated_as_close():
    # A ']' inside a JSON string must not prematurely end the array.
    assert f('[{"q": "weather ] today"}]') == [{"q": "weather ] today"}]


def test_no_array():
    assert f("no json array here at all") is None


def test_empty_and_none():
    assert f("") is None
    assert f(None) is None


def test_first_array_invalid_falls_through_to_next():
    # A malformed first '[...]' is skipped; the next valid array wins.
    assert f('[not, valid, json]\n[{"tool": "ok"}]') == [{"tool": "ok"}]
