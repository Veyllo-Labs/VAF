# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Tests for set_todos item normalization in the coder.

Regression cover for the `KeyError: slice(None, 50, None)` crash: DeepSeek sent
set_todos items as dicts ({"text": "...", "status": "..."}) instead of strings,
and `item[:50]` in the display loop hit a dict — which on Python 3.12+ (where
slice objects became hashable) is a failing key lookup that raises KeyError.
"""
import pytest

from vaf.tools.coder import _todo_item_text


def test_dict_with_text_key_extracts_text():
    item = {"text": "Modify getValidMoves() in index.html", "status": "pending"}
    assert _todo_item_text(item) == "Modify getValidMoves() in index.html"


@pytest.mark.parametrize("key", ["task", "text", "title", "description", "name", "content"])
def test_all_supported_description_keys(key):
    assert _todo_item_text({key: "do the thing", "status": "pending"}) == "do the thing"


def test_plain_string_passthrough():
    assert _todo_item_text("just a string task") == "just a string task"


def test_dict_without_known_key_falls_back_to_json_string():
    out = _todo_item_text({"foo": "bar", "n": 1})
    assert isinstance(out, str)
    assert "foo" in out and "bar" in out  # JSON-ish, not a crash


def test_non_string_scalar_coerced():
    assert _todo_item_text(123) == "123"


def test_result_is_always_sliceable():
    # The whole point: the returned value must support [:50] without raising.
    for item in [
        {"text": "x" * 200, "status": "pending"},
        {"unknown": "y"},
        "z" * 200,
        42,
        None,
    ]:
        assert _todo_item_text(item)[:50] == _todo_item_text(item)[:50]


def test_documents_the_python_312_slice_hashable_trap():
    # Why the crash surfaced "out of nowhere": on Python 3.12+ slices are hashable,
    # so indexing a dict with a slice is a (failing) key lookup -> KeyError, not the
    # old TypeError. Our normalization removes the dict before any such slice.
    import sys
    if sys.version_info >= (3, 12):
        with pytest.raises(KeyError):
            {"text": "a"}[:50]  # type: ignore[misc]
    # normalized value never hits that path:
    assert _todo_item_text({"text": "a"})[:50] == "a"
