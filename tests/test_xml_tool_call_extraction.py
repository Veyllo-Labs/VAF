# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Recovering tool calls a model emits as XML/text instead of structured tool_calls.

DeepSeek (v4) intermittently emits a real tool call as assistant CONTENT wrapped in its
special tokens (``<｜｜DSML｜｜invoke name="read_file">…``) instead of the structured
tool_calls field. _extract_xml_tool_call recovers such a tool call (and the plain
Claude-style XML shape) so the coder's existing fallback chain can dispatch it instead of
surfacing the raw markup. These tests pin that recovery.
"""
import json

from vaf.core.tool_call_recovery import extract_xml_tool_call
from vaf.tools.coder import _extract_xml_tool_call  # thin wrapper, must delegate to the shared parser


def test_deepseek_dsml_leak_is_recovered():
    # A representative DSML-wrapped tool call as emitted by DeepSeek v4.
    content = (
        "<think>Now let me find the App component.</think>\n\n"
        "<｜｜DSML｜｜tool_calls>\n"
        '<｜｜DSML｜｜invoke name="read_file">\n'
        '<｜｜DSML｜｜parameter name="end_line" string="false">740</｜｜DSML｜｜parameter>\n'
        '<｜｜DSML｜｜parameter name="path" string="true">/home/user/Documents/VAF_Projects/x/finance-dashboard.html</｜｜DSML｜｜parameter>\n'
        '<｜｜DSML｜｜parameter name="start_line" string="false">660</｜｜DSML｜｜parameter>\n'
        "</｜｜DSML｜｜invoke>\n"
        "</｜｜DSML｜｜tool_calls>"
    )
    tc = _extract_xml_tool_call(content)
    assert tc is not None
    assert tc["function"]["name"] == "read_file"
    args = json.loads(tc["function"]["arguments"])
    assert args["path"] == "/home/user/Documents/VAF_Projects/x/finance-dashboard.html"
    assert args["start_line"] == 660 and args["end_line"] == 740   # coerced from text to int
    assert isinstance(args["start_line"], int)


def test_plain_claude_style_xml_is_parsed():
    content = (
        '<invoke name="write_file">\n'
        '<parameter name="path">app.py</parameter>\n'
        '<parameter name="content">print(1)</parameter>\n'
        "</invoke>"
    )
    tc = _extract_xml_tool_call(content)
    assert tc["function"]["name"] == "write_file"
    args = json.loads(tc["function"]["arguments"])
    assert args["path"] == "app.py" and args["content"] == "print(1)"


def test_parameter_typing():
    content = (
        '<invoke name="t">'
        '<parameter name="s" string="true">42</parameter>'
        '<parameter name="n" string="false">42</parameter>'
        '<parameter name="b">true</parameter>'
        "</invoke>"
    )
    args = json.loads(_extract_xml_tool_call(content)["function"]["arguments"])
    assert args["s"] == "42" and isinstance(args["s"], str)   # forced string
    assert args["n"] == 42                                    # coerced int
    assert args["b"] is True                                  # json bool


def test_no_invoke_markup_returns_none():
    assert _extract_xml_tool_call("just prose, no tool call here") is None
    assert _extract_xml_tool_call("") is None
    assert _extract_xml_tool_call(None) is None
    # substring 'invoke name=' present but no real tag -> still None
    assert _extract_xml_tool_call("the phrase invoke name= appears but no tags") is None


def test_valid_names_filter_for_main_agent():
    # The main agent passes self.tools as valid_names so only known tools are recovered.
    content = '<invoke name="read_file"><parameter name="path" string="true">x.py</parameter></invoke>'
    assert extract_xml_tool_call(content)["function"]["name"] == "read_file"          # no filter
    assert extract_xml_tool_call(content, {"read_file": 1})["function"]["name"] == "read_file"
    assert extract_xml_tool_call(content, {"write_file": 1}) is None                   # unknown -> dropped


def test_coder_wrapper_delegates_to_shared_parser():
    content = '<｜｜DSML｜｜invoke name="list_files"><｜｜DSML｜｜parameter name="path" string="true">.</｜｜DSML｜｜parameter></｜｜DSML｜｜invoke>'
    assert _extract_xml_tool_call(content) == extract_xml_tool_call(content)
    assert _extract_xml_tool_call(content)["function"]["name"] == "list_files"


def test_morph_tool_use_format():
    # Morph's <tool_use name="X" id="..."> with tag-named parameters.
    content = (
        '<tool_use name="write_to_file" id="toolu_12345">\n'
        "  <filepath>src/main.js</filepath>\n"
        "  <content>\n    function hi() { console.log(1); }\n  </content>\n"
        "</tool_use>"
    )
    tc = extract_xml_tool_call(content)
    assert tc["function"]["name"] == "write_to_file"
    args = json.loads(tc["function"]["arguments"])
    assert args["filepath"] == "src/main.js"
    assert "function hi()" in args["content"]


def test_morph_tool_as_tag_needs_valid_names():
    # Morph docs format: the tool NAME is the tag; only recoverable with a known-tools allowlist.
    content = (
        "<edit_file>\n"
        "  <path>src/components/Button.tsx</path>\n"
        "  <instruction>Add a loading state</instruction>\n"
        "  <code>// ...</code>\n"
        "</edit_file>"
    )
    assert extract_xml_tool_call(content) is None                      # no allowlist -> can't disambiguate
    tc = extract_xml_tool_call(content, {"edit_file": 1})
    assert tc["function"]["name"] == "edit_file"
    args = json.loads(tc["function"]["arguments"])
    assert args["path"] == "src/components/Button.tsx"
    assert args["instruction"] == "Add a loading state"


def test_strip_tool_call_markup_removes_dsml():
    from vaf.core.tool_call_recovery import strip_tool_call_markup
    content = (
        "<think>Let me read the file.</think>\n"
        '<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name="read_file">\n'
        '<｜｜DSML｜｜parameter name="path" string="true">/x/y.html</｜｜DSML｜｜parameter>\n'
        "</｜｜DSML｜｜invoke>\n</｜｜DSML｜｜tool_calls>"
    )
    out = strip_tool_call_markup(content)
    assert "DSML" not in out and "invoke name=" not in out and "｜" not in out
    assert "<think>Let me read the file.</think>" in out


def test_strip_tool_call_markup_is_noop_without_markup():
    from vaf.core.tool_call_recovery import strip_tool_call_markup
    # Plain text (incl. a legit fullwidth pipe) must be returned byte-for-byte.
    plain = "Here is a table cell ｜ and some prose. No tool calls."
    assert strip_tool_call_markup(plain) == plain
    assert strip_tool_call_markup("") == ""


def test_strip_tool_call_markup_removes_tool_use():
    from vaf.core.tool_call_recovery import strip_tool_call_markup
    content = 'Working on it. <tool_use name="write_to_file"><path>a.js</path></tool_use>'
    out = strip_tool_call_markup(content)
    assert out == "Working on it."
