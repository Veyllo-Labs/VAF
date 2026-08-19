# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Thinking is not an answer - including thinking the model never closed.

The empty-response retry is the lane that replaces a dead generation with a fresh
attempt, and it fires on what `_final_answer_probe` leaves over. The probe already
stripped CLOSED <think> blocks; a live incident showed the gap: a model opened a
second think block, drifted from it straight into leaked tool-call markup, and never
wrote </think>. The block's prose survived the strip, counted as the answer, and the
retry that would have saved the turn never fired - the user saw raw markup and a
three-character reply, frozen where an answer should have been.
"""
import re
from pathlib import Path

from vaf.core.agent import _final_answer_probe

ROOT = Path(__file__).resolve().parents[1]


def test_a_real_answer_survives_the_probe():
    assert _final_answer_probe("<think>weighing it</think>The build is green") != ""
    assert _final_answer_probe("Hi") != ""


def test_closed_thinking_alone_is_empty():
    assert _final_answer_probe("<think>only reasoning, no reply</think>") == ""


def test_an_unclosed_think_block_is_thinking_too():
    """MUTATION: drop the `<think>.*$` strip in `_final_answer_probe`.

    The incident shape, verbatim in structure: one closed block, one the model
    never returned from, then leaked closing tags of a tool-call markup dialect.
    Nothing here is an answer, so the probe must leave nothing.
    """
    leaked = (
        "<think>The ticket is not in the log. Let me check the timeline.</think>\n\n"
        "<think>The ticket is not in the log. Let me check the timeline. "
        "It matched the earlier search. Let me look there."
    )
    assert _final_answer_probe(leaked) == ""
    assert _final_answer_probe(leaked + " </tool_markup_tag> </another_tag>") == ""


def test_chat_step_asks_the_probe_and_keeps_no_private_copy():
    """MUTATION: paste the strip chain back inline instead of calling the probe.

    A private copy in the loop drifts the first time one of the two learns a new
    pattern - which is exactly how the unclosed-think gap would come back. The
    pin is the filler-word list: agent.py strips think blocks in several lanes
    for DISPLAY, but only the emptiness check filters filler words, so that list
    existing once IS the check existing once.
    """
    source = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    assert "temp_final = _final_answer_probe(full_content)" in source
    fillers = re.findall(r'"answer", "antwort", "response", "here", "hier"', source)
    assert len(fillers) == 1, (
        "the emptiness filler list may exist once, inside _final_answer_probe - "
        f"found {len(fillers)} copies")
