"""Unit tests for the pure Gemma-4 tool-call parser (vaf.core.agent._parse_gemma4_tool_calls).

The parser must be delimiter-aware: commas and braces INSIDE a quoted <|"|>...<|"|> value must be
preserved. A naive comma-split would corrupt a common case like a weather query "Berlin, Germany".
"""
from vaf.core.agent import _parse_gemma4_tool_calls as parse


def test_comma_inside_quoted_value_is_preserved():
    txt = '<|tool_call>call:web_search{query:<|"|>Berlin, Germany weather<|"|>}<tool_call|>'
    assert parse(txt, None) == [("web_search", {"query": "Berlin, Germany weather"})]


def test_multiple_args_string_and_bare():
    txt = '<|tool_call>call:set_timer{label:<|"|>tea<|"|>,seconds:300}<tool_call|>'
    assert parse(txt, None) == [("set_timer", {"label": "tea", "seconds": "300"})]


def test_multiple_calls_in_one_response():
    txt = ('<|tool_call>call:web_search{query:<|"|>a<|"|>}<tool_call|>'
           ' then '
           '<|tool_call>call:memory_search{query:<|"|>b<|"|>}<tool_call|>')
    assert parse(txt, None) == [
        ("web_search", {"query": "a"}),
        ("memory_search", {"query": "b"}),
    ]


def test_empty_args():
    assert parse('<|tool_call>call:list_tools{}<tool_call|>', None) == [("list_tools", {})]


def test_brace_inside_quoted_value_does_not_end_the_call():
    txt = '<|tool_call>call:run_code{code:<|"|>x = {1: 2}<|"|>}<tool_call|>'
    assert parse(txt, None) == [("run_code", {"code": "x = {1: 2}"})]


def test_name_filtering_against_valid_names():
    txt = '<|tool_call>call:evil{query:<|"|>x<|"|>}<tool_call|>'
    assert parse(txt, {"web_search"}) == []                      # not loaded -> dropped
    assert parse(txt, {"evil"}) == [("evil", {"query": "x"})]    # loaded -> kept


def test_no_tool_calls_returns_empty():
    assert parse("just a normal answer, no tools here", None) == []
    assert parse("", None) == []
