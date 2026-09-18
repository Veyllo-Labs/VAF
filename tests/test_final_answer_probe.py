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


# ── the reply that is only the reasoning again ────────────────────────────────

INCIDENT_ECHO_TAIL = (
    "The correction flags again. In my last reply I said the draft exists, and the tool "
    "result does say so. Maybe the flagged claim is the mailbox sentence instead. I will "
    "re-run list_files so the claim is produced by a tool in this turn, then answer. "
) * 3


def test_a_long_reply_that_only_repeats_the_reasoning_is_not_an_answer():
    """MUTATION: drop the `_reply_is_reasoning_echo` call in `_final_answer_probe`.

    Shape of the live incident, and not a model habit but a gateway one: the harness wraps
    `reasoning_content` in <think> tags and appends the content field after it, so a
    provider that returns the reasoning in BOTH fields produces a reply whose answer is the
    thinking. The probe must call that no answer, which is what routes the turn into the
    empty-response retry instead of shipping deliberation to the user.
    """
    echo = f"<think>{INCIDENT_ECHO_TAIL}</think>\n\n{INCIDENT_ECHO_TAIL}"
    assert len(INCIDENT_ECHO_TAIL.strip()) > 400
    assert _final_answer_probe(echo) == ""


def test_a_short_reply_may_repeat_its_own_one_line_thought():
    """The counter-case that decides the length floor, measured on stored sessions: 21 of 23
    duplicate-shaped finals were short status lines like this one, and killing those would
    break every background confirmation."""
    assert _final_answer_probe("<think>Download #5/10 erledigt.</think>\n\nDownload #5/10 erledigt.") != ""


def test_a_real_answer_after_long_thinking_survives():
    """The guard must key on the duplicate, never on the length of the thinking: an answer
    that differs from the reasoning is an answer however long the reasoning was."""
    long_think = "Let me weigh the options for the contract numbers. " * 40
    assert _final_answer_probe(f"<think>{long_think}</think>\n\nHier ist der Entwurf fuer Uwe.") != ""


def test_the_echo_predicate_needs_both_halves():
    from vaf.core.agent import _reply_is_reasoning_echo

    assert _reply_is_reasoning_echo(f"<think>{INCIDENT_ECHO_TAIL}</think>{INCIDENT_ECHO_TAIL}") is True
    # Whitespace differences are not a different answer.
    assert _reply_is_reasoning_echo(
        f"<think>{INCIDENT_ECHO_TAIL}</think>\n\n  {' '.join(INCIDENT_ECHO_TAIL.split())}  "
    ) is True
    # No think block, nothing to echo; and a plain answer of the same length is untouched.
    assert _reply_is_reasoning_echo(INCIDENT_ECHO_TAIL) is False
    assert _reply_is_reasoning_echo(f"<think>{INCIDENT_ECHO_TAIL}</think>{INCIDENT_ECHO_TAIL[:-80]}x") is False


def test_an_answer_before_the_duplicated_thinking_is_still_an_answer():
    """MUTATION: compare only the text after the LAST `</think>`.

    A model that answers first and then duplicates its thinking has still answered, and the
    api_backend stream opens and closes a think block per interleave, so content can precede
    one. Reading only the tail declared such a generation "no answer", which retracts it - the
    very breach this round exists to close.
    """
    from vaf.core.agent import _reply_is_reasoning_echo

    answer = "Hier ist der Entwurf fuer Uwe, die Vertragsnummern stehen drin."
    assert _reply_is_reasoning_echo(f"{answer}<think>{INCIDENT_ECHO_TAIL}</think>{INCIDENT_ECHO_TAIL}") is False
    assert _final_answer_probe(f"{answer}<think>{INCIDENT_ECHO_TAIL}</think>{INCIDENT_ECHO_TAIL}") != ""
    # A block in the middle with the echo around it is still only thinking.
    assert _reply_is_reasoning_echo(
        f"<think>{INCIDENT_ECHO_TAIL}</think>{INCIDENT_ECHO_TAIL}<think>{INCIDENT_ECHO_TAIL}</think>"
    ) is True


def test_a_generation_whose_every_call_was_blocked_is_regenerated():
    """MUTATION: delete the `continue` after the windowed filter in chat_step.

    Loop protection can refuse every call of one generation. Without the re-generation the
    turn falls through to the final-answer path with whatever that generation streamed, and
    the refusal it was just told about in a system message is never read. Pinned as source
    wiring, like the probe test above: the flag must be computed from the pre-filter batch
    and the retry must be bounded.
    """
    from vaf.core.agent import MAX_BLOCKED_BATCH_ROUNDS

    source = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    # One decision for all three sites that can refuse a call: a flag set at each of them,
    # read once after the filter. Two of the three used to bypass the check entirely, and a
    # generation refused there skipped every post-stream guard, because a non-empty
    # `streaming_tools` gates all of them.
    assert source.count("_blocked_every_call = True") == 3, (
        "each loop-protection refusal must set the shared flag")
    region = source.split("if (_blocked_every_call and not tool_calls_detected", 1)[1][:1200]
    assert "not self._guard_keeps_answer(full_content)" in region, (
        "a generation that answered must keep the old fall-through")
    assert "_blocked_batch_rounds < MAX_BLOCKED_BATCH_ROUNDS" in region
    assert "continue" in region
    # The bound is a real bound: zero would disable the whole fix while the wiring still reads
    # correctly, which is how a source check lies.
    assert MAX_BLOCKED_BATCH_ROUNDS >= 1
