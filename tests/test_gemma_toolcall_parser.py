# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
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


def test_incident_calendar_leak_is_parsed():
    """Live incident 2026-07-21 (session purple636998): the local Qwen 3.5 4B emitted a
    create_calendar_event call in exactly this pipe-delimited format. It leaked into the chat
    as text and nothing ran, because lane 4 was gated to Gemma only. The parser itself handles
    it; the gate is what failed."""
    txt = ('<|tool_call>call:create_calendar_event{'
           'end_time:<|"|>2026-07-28T11:00:00<|"|>,'
           'start_time:<|"|>2026-07-28T10:00:00<|"|>,'
           'title:<|"|>VAF Reddit Post Draft für r/opensource verfassen<|"|>}<tool_call|>')
    assert parse(txt, {"create_calendar_event"}) == [
        ("create_calendar_event", {
            "end_time": "2026-07-28T11:00:00",
            "start_time": "2026-07-28T10:00:00",
            "title": "VAF Reddit Post Draft für r/opensource verfassen",
        })
    ]


def test_the_recovery_lane_is_not_model_gated():
    """The parser worked all along; the bug was that its call site (lane 4 in Agent chat_step)
    only ran for model_mode == 'gemma4', so the identical Qwen output was never recovered. This
    pins that the pipe-delimited recovery net runs for ANY local model, like lanes 5 and 6."""
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "vaf/core/agent.py").read_bytes().decode(
        "utf-8").replace("\r\n", "\n")

    # Find the `if <cond>:` that guards the call to _parse_gemma4_tool_calls and check the
    # condition does not require a specific model_mode.
    tree = ast.parse(src)
    guards = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if "_parse_gemma4_tool_calls" in ast.dump(node.test) + "".join(
                ast.dump(n) for n in node.body[:2]):
            guards.append(ast.get_source_segment(src, node.test))
    assert guards, "could not locate the pipe-delimited recovery lane"
    for cond in guards:
        assert "model_mode" not in cond and "gemma4" not in cond, (
            f"the recovery lane is still model-gated ({cond!r}); Qwen's leaked calls will "
            "vanish again"
        )


# ── _parse_paren_tool_calls (fallback 3: "name(...)" written as text) ──

from vaf.core.agent import _parse_paren_tool_calls as parse_paren  # noqa: E402

TOOLS = {"find_mail": object(), "mail_inbox": object(),
         "web_search": {"parameters": {"properties": {"query": {}}}}}


def test_paren_leaked_plan_bullets_with_json_args():
    """Live incident 2026-07-14: deepseek-v4 wrote its next calls as markdown
    bullets; unrecovered they became the FINAL ANSWER and were read aloud on a
    voice call. Exact shape from the incident chat."""
    txt = ("Keine Treffer. Lass mich die E-Mails von heute direkt durchgehen.\n"
           '- find_mail({"query": "alice1985", "limit": 20})\n'
           '- find_mail({"query": "after", "limit": 10})\n'
           '- mail_inbox({"account": "user@example.com", "limit": 10, "query": "afterparty"})')
    assert parse_paren(txt, TOOLS) == [
        ("find_mail", {"query": "alice1985", "limit": 20}),
        ("find_mail", {"query": "after", "limit": 10}),
        ("mail_inbox", {"account": "user@example.com", "limit": 10, "query": "afterparty"}),
    ]


def test_paren_classic_quoted_single_arg_maps_to_first_param():
    txt = 'Answer: web_search("wetter berlin")'
    assert parse_paren(txt, TOOLS) == [("web_search", {"query": "wetter berlin"})]


def test_paren_unknown_tools_and_prose_are_ignored():
    txt = ("Ich habe alles(!) geprueft.\n"
           '- delete_everything({"path": "/"})\n'
           "Das Ergebnis (siehe oben) steht fest.")
    assert parse_paren(txt, TOOLS) == []
