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


# ── generator teaches deterministic delivery (live incident f9efc6d6) ────────

def test_workflow_generator_teaches_verbatim_send_rule():
    # A generated automation once sent the user the RAW web_search dump plus a
    # dangling "summarize" instruction: send steps deliver their args verbatim,
    # no LLM sits between template resolution and delivery. The generator
    # prompt must teach the summarize-before-send pattern and file attachment.
    from pathlib import Path
    import vaf.tools.automation as auto_mod
    src = Path(auto_mod.__file__).read_text(encoding="utf-8")
    assert "send_to_user(message, file_path)" in src, "generator must know the delivery tool"
    assert "DETERMINISTIC" in src and "VERBATIM" in src, "verbatim rule missing"
    assert '"file_path": "{output_path}/weather_berlin_{{date}}.html"' in src, (
        "delivery example must attach the produced file"
    )
    assert '"output": "message_text"' in src, (
        "delivery example must summarize BEFORE sending"
    )


def test_workflow_generator_is_platform_agnostic():
    # The delivery step must never hardwire a platform tool: the platform is
    # the user's configuration (main_messenger), resolved at run time by
    # send_to_user. A hardwired send_telegram froze the platform into every
    # generated automation JSON (live incident f9efc6d6).
    from pathlib import Path
    import vaf.tools.automation as auto_mod
    src = Path(auto_mod.__file__).read_text(encoding="utf-8")
    start = src.index("Available Tools:")
    end = src.index("Return ONLY valid JSON array", start)
    generator_prompt = src[start:end]
    for platform_tool in ("send_telegram", "send_whatsapp", "send_discord", "send_slack"):
        assert f'"{platform_tool}"' not in generator_prompt, (
            f"generator example/steps must not hardwire {platform_tool}"
        )
