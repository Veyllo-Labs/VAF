# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""_parse_qwen_tool_calls recovers Qwen/Hermes-style text tool calls that a
reasoning model sometimes emits instead of a native call.

Live incident (session yellow276227): a local model wrote a tool call as
text but garbled its own closing tags -
`<tool_call><function=update_working_memory><parameter=plan>[...]</parameter></tasks></working_memory>`
(hallucinated `</tasks></working_memory>` instead of `</parameter></function>
</tool_call>`). The properly-closed `<parameter=plan>...</parameter>` inside
was perfectly recoverable, but the strict parser required the OUTER
`</function></tool_call>` close too and returned nothing - the turn ended
with tool-call-shaped text visible in history and no tool ever actually
invoked. The parser now bounds a call's body by whichever comes first: a
real close, the next `<tool_call>`, or ANY two closing tags in a row
(right SHAPE, wrong names) - i.e. the model attempted to close something.

A first version of this fix fell back all the way to end-of-text when no
closing shape was found at all. Adversarial review found that let an
incidental, never-closed EXPLANATION of the tool-call format (a model
musing "tool calls look like <tool_call><function=web_search>...") turn
into a genuinely dispatched call. The end-of-text fallback was removed
for exactly that reason - see the two negative tests below.
"""
from vaf.core.agent import _parse_qwen_tool_calls

TOOLS = {"update_working_memory", "web_search", "write_file"}


def test_well_formed_single_call_unchanged():
    text = '<tool_call><function=web_search><parameter=query>berlin weather</parameter></function></tool_call>'
    assert _parse_qwen_tool_calls(text, TOOLS) == [("web_search", {"query": "berlin weather"})]


def test_well_formed_multiple_calls_stay_delimited():
    text = (
        '<tool_call><function=update_working_memory><parameter=plan>["a", "b"]</parameter></function></tool_call>'
        " some prose in between "
        '<tool_call><function=web_search><parameter=query>berlin</parameter></function></tool_call>'
    )
    assert _parse_qwen_tool_calls(text, TOOLS) == [
        ("update_working_memory", {"plan": ["a", "b"]}),
        ("web_search", {"query": "berlin"}),
    ]


def test_trailing_prose_after_a_well_formed_call_is_not_swallowed():
    text = (
        '<tool_call><function=web_search><parameter=query>berlin</parameter></function></tool_call>'
        " I will now check the weather for you and let you know shortly."
    )
    result = _parse_qwen_tool_calls(text, TOOLS)
    assert result == [("web_search", {"query": "berlin"})]


def test_recovers_the_real_incident_malformed_closing_tags():
    text = (
        "<tool_call>\n"
        "<function=update_working_memory>\n"
        '<parameter=plan>["Wetterdaten fuer Berlin und New York recherchieren und als HTML-Report'
        ' im Projektordner speichern"]\n'
        "</parameter>\n"
        "</tasks>\n"
        "</working_memory>"
    )
    result = _parse_qwen_tool_calls(text, TOOLS)
    assert len(result) == 1
    name, args = result[0]
    assert name == "update_working_memory"
    assert args["plan"] == [
        "Wetterdaten fuer Berlin und New York recherchieren und als HTML-Report im Projektordner speichern"
    ]


def test_malformed_call_followed_by_a_real_second_call_does_not_merge():
    malformed = (
        "<tool_call><function=update_working_memory><parameter=plan>[\"x\"]</parameter>"
        "</tasks></working_memory>"
    )
    text = malformed + "\n\n" + (
        '<tool_call><function=web_search><parameter=query>berlin</parameter></function></tool_call>'
    )
    result = _parse_qwen_tool_calls(text, TOOLS)
    assert result == [
        ("update_working_memory", {"plan": ["x"]}),
        ("web_search", {"query": "berlin"}),
    ]


def test_unknown_tool_name_is_still_rejected():
    """The lenient closing must not widen WHICH names get accepted - only
    how a call is allowed to end."""
    text = '<tool_call><function=totally_fake_tool><parameter=x>1</parameter>'
    assert _parse_qwen_tool_calls(text, TOOLS) == []


def test_call_with_no_closing_shape_at_all_is_not_recovered():
    """No <parameter> tags and NO closing-tag-shaped sequence anywhere: this
    is indistinguishable from a model just trailing off into prose, so it
    must NOT be treated as an attempted call (this is the safety boundary
    the false-positive review is about - see the two tests below)."""
    text = "<tool_call><function=update_working_memory>I did the research and wrote the plan."
    assert _parse_qwen_tool_calls(text, TOOLS) == []


def test_call_with_wrong_two_tag_close_and_zero_parameters_still_recovers():
    """Same as above, but the model DID attempt some (wrong-named) close -
    the structural signal that distinguishes 'attempted and failed' from
    'never attempted'. Recognized with empty args; the tool's own
    validation reports the real problem instead of the attempt vanishing
    silently."""
    text = "<tool_call><function=update_working_memory>I did the research.</done></finished>"
    assert _parse_qwen_tool_calls(text, TOOLS) == [("update_working_memory", {})]


def test_incidental_explanatory_mention_is_not_executed_as_a_call():
    """The false-positive class an earlier (end-of-text-fallback) version of
    this fix introduced, per adversarial review: a model musing about the
    tool-call SYNTAX in prose, with a real tool name and a well-formed
    single <parameter> tag, but no closing-tag shape anywhere before trailing
    off into ordinary explanatory text. Must not become a dispatched call."""
    text = (
        "Note: tool calls look like "
        "<tool_call><function=web_search><parameter=query>example</parameter> "
        "when using this format.\n\n" + ("Some unrelated filler text. " * 200)
    )
    assert _parse_qwen_tool_calls(text, TOOLS) == []


def test_incidental_explanatory_mention_write_file_variant_is_not_executed():
    text = (
        "I emit something like <tool_call><function=write_file><parameter=path>notes.txt</parameter> "
        "and then a content parameter" + (" filler" * 300)
    )
    assert _parse_qwen_tool_calls(text, TOOLS) == []


def test_no_tool_call_markers_returns_empty():
    assert _parse_qwen_tool_calls("just a normal reply, no tool calls here", TOOLS) == []


def test_empty_and_none_text_do_not_raise():
    assert _parse_qwen_tool_calls("", TOOLS) == []
    assert _parse_qwen_tool_calls(None, TOOLS) == []
