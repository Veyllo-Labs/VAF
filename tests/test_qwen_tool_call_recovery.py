# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""_parse_qwen_tool_calls recovers Qwen/Hermes-style text tool calls that a
reasoning model sometimes emits instead of a native call.

Live incident: a local model wrote a tool call as
text but garbled its own closing tags -
`<tool_call><function=update_working_memory><parameter=plan>[...]</parameter></tasks></working_memory>`
(hallucinated `</tasks></working_memory>` instead of `</parameter></function>
</tool_call>`). The properly-closed `<parameter=plan>...</parameter>` inside
was perfectly recoverable, but the strict parser required the OUTER
`</function></tool_call>` close too and returned nothing - the turn ended
with tool-call-shaped text visible in history and no tool ever actually
invoked.

THREE adversarially-reviewed prior attempts at this fix leaked real
false-positive execution risk (see the CRITICAL/HIGH-labeled tests below):
a version that fell back to end-of-text, a version that accepted a
closing-tag-SHAPED sequence anywhere later in the text, and a version that
accepted a SINGLE wrong-named closing tag immediately after the last
parameter - which an example wrapped in inline markup
(`...</parameter></code>`) satisfies by construction. The current design
parses `<parameter>` tags one at a time (each bounded only by its OWN
`</parameter>`, however tag-like its value looks); a strict
`</function></tool_call>` close is accepted anywhere, while LENIENT
recovery additionally requires the `<tool_call>` open to sit at a LINE
START (how models genuinely emit calls; inline mentions in prose/markup do
not) and either TWO+ consecutive wrong-named closing tags or the next
`<tool_call>` beginning immediately (back-to-back calls with the first
close forgotten). Prose between the last parameter and the close attempt
rejects the call outright.
"""
from vaf.core.agent import _parse_qwen_tool_calls

TOOLS = {"update_working_memory", "web_search", "write_file", "delete_file"}


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
    text = '<tool_call><function=totally_fake_tool><parameter=x>1</parameter></weird></close>'
    assert _parse_qwen_tool_calls(text, TOOLS) == []


def test_call_with_no_closing_shape_at_all_is_not_recovered():
    """No <parameter> tags and NO closing-tag-shaped sequence anywhere: this
    is indistinguishable from a model just trailing off into prose, so it
    must NOT be treated as an attempted call."""
    text = "<tool_call><function=update_working_memory>I did the research and wrote the plan."
    assert _parse_qwen_tool_calls(text, TOOLS) == []


def test_zero_parameters_with_prose_before_a_wrong_close_is_rejected():
    """Tightened boundary (this is what closes the false-positive class
    below): prose BETWEEN the function open and a closing-tag attempt, even
    a wrong-named one, is indistinguishable from an explanation trailing
    into an unrelated close - rejected, unlike a clean wrong-close."""
    text = "<tool_call><function=update_working_memory>I did the research.</done></finished>"
    assert _parse_qwen_tool_calls(text, TOOLS) == []


def test_zero_parameters_with_only_whitespace_before_a_wrong_close_recovers():
    """Same shape, but ONLY whitespace between the open and the (wrong-named)
    close - the structural signal of a genuine, botched close attempt."""
    text = "<tool_call><function=update_working_memory>\n</done>\n</finished>"
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


def test_explanatory_mention_followed_by_real_html_is_not_executed():
    """CRITICAL finding from a second adversarial review: a version that
    accepted ANY closing-tag-shaped sequence anywhere later in the text (not
    anchored to immediately follow the last parameter) let ordinary HTML
    markup elsewhere in an explanatory sentence stand in for a "closing
    attempt" - dispatching delete_file with a real path purely because the
    model, discussing the format, happened to mention nesting <div><span>
    tags a few words later."""
    text = (
        "For example, the format looks like this: "
        "<tool_call><function=delete_file><parameter=path>/tmp/user_data.txt</parameter> "
        "and if you wanted to nest some HTML you might write <div><span>hello</span></div> "
        "in your reply."
    )
    assert _parse_qwen_tool_calls(text, TOOLS) == []


def test_single_closing_tag_after_parameter_is_not_a_close_attempt():
    """CRITICAL finding from a third adversarial review: a version that
    accepted ONE wrong-named closing tag immediately after the last parameter
    let an example written inside a code block dispatch for real - the
    block's own closing tag lands exactly there by construction. A genuine
    botched close (the live incident) trails MULTIPLE hallucinated tags."""
    text = (
        "Example of the format:\n"
        "<tool_call><function=delete_file><parameter=path>/tmp/user_data.txt</parameter></code>\n"
        "That is how you would write it."
    )
    assert _parse_qwen_tool_calls(text, TOOLS) == []


def test_inline_markup_mention_is_rejected_even_with_two_closing_tags():
    """Same review, belt-and-braces rail: nested inline markup can produce
    TWO adjacent closing tags (`</code></li>`), but an inline mention never
    puts `<tool_call>` at the start of a line - lenient recovery requires
    that, so this stays prose."""
    text = (
        "For example you might write <code><tool_call><function=delete_file>"
        "<parameter=path>/tmp/user_data.txt</parameter></code></li> in a list."
    )
    assert _parse_qwen_tool_calls(text, TOOLS) == []


def test_back_to_back_calls_with_the_first_close_forgotten_recover_both():
    """The original incident shape in its two-call variant: the model opens
    the next <tool_call> without ever closing the first. The first call's
    parameters are complete and the immediately-following <tool_call> is the
    structural close signal - dropping the first call here silently loses a
    tool call again (the exact failure this recovery lane exists for)."""
    text = (
        '<tool_call><function=update_working_memory><parameter=plan>["x"]</parameter>\n'
        '<tool_call><function=web_search><parameter=query>berlin</parameter></function></tool_call>'
    )
    assert _parse_qwen_tool_calls(text, TOOLS) == [
        ("update_working_memory", {"plan": ["x"]}),
        ("web_search", {"query": "berlin"}),
    ]


def test_parameter_value_containing_closing_tag_shaped_text_is_not_truncated():
    """HIGH finding from the same review: the previous outer-boundary regex
    stopped scanning at the FIRST closing-tag-shaped substring it saw,
    including one embedded inside a legitimate, still-open parameter VALUE -
    silently dropping the parameter from an otherwise perfectly well-formed,
    correctly-closed call. Parsing parameters one at a time by their OWN
    </parameter> fixes this structurally."""
    text = (
        '<tool_call><function=update_working_memory><parameter=plan>'
        '["Remove the stray trailing tags </div></span> left over from the old template, then re-render"]'
        '</parameter></function></tool_call>'
    )
    result = _parse_qwen_tool_calls(text, TOOLS)
    assert result == [
        (
            "update_working_memory",
            {"plan": ["Remove the stray trailing tags </div></span> left over from the old template, then re-render"]},
        )
    ]


def test_no_tool_call_markers_returns_empty():
    assert _parse_qwen_tool_calls("just a normal reply, no tool calls here", TOOLS) == []


def test_empty_and_none_text_do_not_raise():
    assert _parse_qwen_tool_calls("", TOOLS) == []
    assert _parse_qwen_tool_calls(None, TOOLS) == []


def test_large_adversarial_input_completes_quickly_no_backtracking_blowup():
    """Regex catastrophic-backtracking guard: the sequential parameter loop
    must stay linear-ish on pathological near-miss input."""
    import time

    adversarial = "<tool_call><function=web_search>" + ("<parameter=x>val</paramx> " * 5000) + "end"
    t0 = time.monotonic()
    result = _parse_qwen_tool_calls(adversarial, TOOLS)
    assert time.monotonic() - t0 < 2.0
    assert result == []
