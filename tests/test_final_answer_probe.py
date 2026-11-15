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

The other way round, a reply that is only a code block IS an answer. The probe deleted every
fenced block whole, so "only the code, please" counted as no answer: the empty retry took the
bubble the user was reading off the screen and asked again, and every guard that asks the
probe whether it may retract lost that protection for code. The probe now drops the fence
markers and counts the code; a fence with nothing in it stays nothing.

Inside a closed fence nothing is read as a tag either: the leftover-tag strip ran before the
fence step and took a code-only HTML reply (`<input type="text">`) for markup, so it was
empty again. Reasoning is stripped before, inside fences too, and an opener that is never
closed protects nothing.

MUTATIONS for that part, each turns the named test red:
- strip tags with `_drop_angle_tags` again, fences or not
  -> test_a_code_only_reply_counts[html, xml, jsx], test_a_code_only_reply_is_an_answer[html],
     the fence fuzz (oracle d);
- let an unclosed opener protect to the end of the text
  -> test_a_fence_protects_no_reasoning_and_no_stray_markup[unclosed-fence];
- restore `re.sub(r'```[\\s\\S]*?```', '', clean)` -> test_a_code_only_reply_is_an_answer and
  the fence fuzz (oracle b);
- drop the fence-line step and keep only `replace("```", "")`
  -> test_an_empty_fence_is_still_empty[python] and [indented-crlf], the fence fuzz;
- drop `\\r` from the fence line's trailing class
  -> test_an_empty_fence_is_still_empty[indented-crlf];
- change any other step of the probe (e.g. drop "hier" from the filler list)
  -> test_the_probe_is_unchanged_outside_fences.
"""
import os
import random
import re
import time
from pathlib import Path

import pytest

from vaf.core.agent import _final_answer_probe

ROOT = Path(__file__).resolve().parents[1]


def test_a_real_answer_survives_the_probe():
    assert _final_answer_probe("<think>weighing it</think>The build is green") != ""
    assert _final_answer_probe("Hi") != ""


def test_closed_thinking_alone_is_empty():
    assert _final_answer_probe("<think>only reasoning, no reply</think>") == ""


def test_an_unclosed_think_block_is_thinking_too():
    """MUTATION: keep the text after an unclosed opener in `strip_think_blocks`, the rule
    `_final_answer_probe` shares with the reply cleaner.

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


# ── a reply that is only code ────────────────────────────────────────────────

CODE_ONLY = "```python\nprint('hallo welt')\n```"
HTML_ONLY = '```html\n<input type="text">\n```'
CODE_ONLY_REPLIES = [
    pytest.param(CODE_ONLY, id="python"),
    pytest.param(HTML_ONLY, id="html"),
    pytest.param('```xml\n<config><item name="a"/></config>\n```', id="xml"),
    pytest.param("```jsx\n<Button onClick={save} />\n```", id="jsx"),
]


@pytest.mark.parametrize("reply", CODE_ONLY_REPLIES)
def test_a_code_only_reply_counts(reply):
    """Inside a closed fence nothing is read as a tag: markup there is the code asked for."""
    assert _final_answer_probe(reply) != ""


@pytest.mark.parametrize("reply", [pytest.param(CODE_ONLY, id="python"),
                                   pytest.param(HTML_ONLY, id="html")])
def test_a_code_only_reply_is_an_answer(monkeypatch, tmp_path, reply):
    """On the real `chat_step` a code-only reply is one request, never retracted, and the
    returned answer."""
    from test_empty_retry_history import make_agent

    agent = make_agent(monkeypatch, tmp_path)
    retracted = []
    monkeypatch.setattr(agent, "_retract_streamed_reply",
                        lambda *a, **k: retracted.append(a))
    streamed, returned = agent.run([reply])
    assert len(agent.requests) == 1, "the code reply was asked again"
    assert retracted == []
    assert returned == reply
    assert streamed.startswith(reply)


@pytest.mark.parametrize("reply", [
    pytest.param("```html\n<think>x</think>\n```", id="reasoning-in-a-fence"),
    pytest.param("```\n<tool_call><arg/></tool_call>", id="unclosed-fence"),
    pytest.param("<tool_call>\n```\n```", id="markup-outside"),
])
def test_a_fence_protects_no_reasoning_and_no_stray_markup(reply):
    """Reasoning is stripped before the fences are looked at, so a fence never turns it into
    an answer; an opener that is never closed protects nothing."""
    assert _final_answer_probe(reply) == ""


@pytest.mark.parametrize("reply", [
    pytest.param("```\n```", id="bare"),
    pytest.param("```python\n```", id="python"),
    pytest.param("<think>x</think>```\n```", id="after-thinking"),
    pytest.param("  ```json  \r\n```\r\n", id="indented-crlf"),
])
def test_an_empty_fence_is_still_empty(reply):
    assert _final_answer_probe(reply) == ""


def _probe_before_the_fence_change(full_content):
    """The probe as it was before fences were unwrapped, verbatim but for its fence line: on
    text without a backtick the two must agree."""
    from vaf.core.agent import _drop_angle_tags, _reply_is_reasoning_echo
    from vaf.core.completion import strip_think_blocks
    if _reply_is_reasoning_echo(full_content):
        return ""
    readings = []
    for greedy in (False, True):
        clean = strip_think_blocks(full_content or "", greedy=greedy)
        clean = _drop_angle_tags(clean)
        clean = re.sub(r'```[\s\S]*?```', '', clean)
        clean = clean.replace(".", "").replace("\n", "").replace(":", "").strip()
        probe = clean.lower()
        for pattern in ("answer", "antwort", "response", "here", "hier", "ok", "okay"):
            probe = probe.replace(pattern, "")
        probe = probe.strip()
        if len(probe) >= 2:
            return probe
        readings.append(probe)
    return max(readings, key=len)


_PIECES = ["<think>", "</think>", "<THINKING>", "</thinking>", "<redacted_reasoning>",
           "</redacted_reasoning>", "<", ">", "<tool_call>", "answer", "Antwort", "here", "Hier",
           "ok", "Okay", "response", ".", ":", "\n", " ", "!", "?", "Hi", "Ja", "4", "x",
           "Das Ergebnis ist 42", "print(1)", "a"]
_TAGS = ["", "python", "c++", "json", "objective-c"]


# Pieces without a lone angle bracket (every tag in them is whole), and pieces without any.
_WHOLE_TAG_PIECES = [p for p in _PIECES if p not in ("<", ">")]
_ANGLE_FREE_PIECES = [p for p in _PIECES if "<" not in p and ">" not in p]
_MARKUP = ['<input type="text">', "<div>", "</div>", "<a/>", "<Button onClick={go} />",
           '<?xml version="1.0"?>', "<tool_call>", "<", ">"]


def _text(rng, most=8, pieces=_PIECES):
    return "".join(rng.choice(pieces) for _ in range(rng.randint(0, most)))


def _fence_stripped(text):
    from vaf.core.agent import _FENCE_LINE_RE
    return _FENCE_LINE_RE.sub("", text).replace("```", "")


def _only_fence_markers_lost(text):
    """True when, at the stage where the probe meets the fences (after its tag steps, in both
    readings), dropping the fence lines and markers leaves nothing the old probe counts. A
    tag the probe drops can put a fence on a line of its own ("</think>```HiJa" is the opener
    of an empty block with the info string HiJa), so the raw text is not that stage."""
    from vaf.core.agent import _drop_angle_tags
    from vaf.core.completion import strip_think_blocks
    return all(
        not _probe_before_the_fence_change(
            _fence_stripped(_drop_angle_tags(strip_think_blocks(text, greedy=greedy))))
        for greedy in (False, True))


def probe_fence_mismatches(seed, draws):
    """Oracles: (a) without a backtick the probe is the one from before; (b) a fence on its
    own lines around X counts as X alone does, for an X without angle brackets (inside a
    fence a tag is code and counts, outside it does not, which is the change); (c) where the
    old probe found an answer and the new one finds none, there was nothing but fence markers
    to find (a lone opener); (d) a code-only reply that holds markup counts."""
    rng = random.Random(seed)
    bad = []
    for _ in range(draws):
        plain = _text(rng, 12)
        if _final_answer_probe(plain) != _probe_before_the_fence_change(plain):
            bad.append(("a", plain))
        a = _text(rng, pieces=_WHOLE_TAG_PIECES)
        x = _text(rng, pieces=_ANGLE_FREE_PIECES)
        b = _text(rng, pieces=_WHOLE_TAG_PIECES)
        a = a + "\n" if a and rng.random() < 0.8 else ""
        b = "\n" + b if b and rng.random() < 0.8 else ""
        fenced = f"{a}```{rng.choice(_TAGS)}\n{x}\n```{b}"
        if bool(_final_answer_probe(fenced)) != bool(_final_answer_probe(a + x + b)):
            bad.append(("b", fenced))
        mixed = "".join(rng.choice(_PIECES + ["```", "```python\n", "\n```\n", "```json"])
                        for _ in range(rng.randint(0, 10)))
        if (_probe_before_the_fence_change(mixed) and not _final_answer_probe(mixed)
                and not _only_fence_markers_lost(mixed)):
            bad.append(("c", mixed))
        code = _text(rng, 4, _ANGLE_FREE_PIECES).replace("```", "") + rng.choice(_MARKUP[:-2])
        code += _text(rng, 4, _MARKUP + _ANGLE_FREE_PIECES)
        markup_only = f"```{rng.choice(_TAGS)}\n{code}\n```"
        if not _final_answer_probe(markup_only):
            bad.append(("d", markup_only))
    return bad


def test_the_probe_is_unchanged_outside_fences():
    """3 seeds x 500 draws here; `VAF_EMPTY_RETRY_FUZZ_DRAWS` runs more outside the suite."""
    draws = int(os.environ.get("VAF_EMPTY_RETRY_FUZZ_DRAWS", "500"))
    for seed in (5, 11, 2026):
        bad = probe_fence_mismatches(seed, draws)
        assert not bad, f"seed {seed}: {len(bad)} mismatches, first: {bad[:3]}"


def test_the_fence_steps_are_linear():
    """128 KB of fence markers, on one line and one per line, through the probe."""
    for text in ("```" * 43_000, "```\n" * 32_000, "`" * 130_000 + "x", " \t" * 64_000 + "y"):
        started = time.monotonic()
        _final_answer_probe(text)
        assert time.monotonic() - started < 2.0, repr(text[:8])
