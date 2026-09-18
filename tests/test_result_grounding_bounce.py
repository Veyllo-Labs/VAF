# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A guard may append to what the user has read. It may not take it away.

Rule 4 invariant 2 of this repo says a guardrail must never erase or regenerate a streamed
reply. Three of the four post-stream guards honoured it: the empty-response retry only clears
when nothing answer-shaped exists, the false-promise retry spares a substantial answer, and
the team-await hold carries an explicit non-destructive rationale. Result grounding, the one
that is ON by default, cleared the bubble and the stream buffer unconditionally.

Live incident: a correct German email draft was erased twice by a misjudgement, and what the
user was left reading was the model's own English deliberation about the correction. The
answer existed in no message of the stored session afterwards - only as a quotation inside the
reasoning of the later attempts.

So result grounding is now purely additive: a generation without an answer is not judged at
all (the empty-response lane owns it), and a generation with one keeps it and gets the
correction appended below. Pinned here: that rule, the one shared retraction sequence, and
the retraction's actual behaviour on a stream buffer.
"""
from pathlib import Path

from vaf.core.agent import Agent

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")


def _region(start: str, end: str) -> str:
    assert start in SOURCE and end in SOURCE, (start, end)
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


class _Buffer:
    """The stream callback shape the web lane passes in: callable, with a clear()."""

    def __init__(self):
        self.chunks: list = []
        self.clears = 0

    def __call__(self, text):
        self.chunks.append(text)

    def clear(self):
        self.clears += 1
        self.chunks = []


def test_the_grounding_region_can_no_longer_retract_anything():
    """MUTATION: put a retraction back into the result-grounding bounce.

    The decision is structural rather than conditional now: the guard is gated on
    `_guard_keeps_answer`, so by the time it can fire there is always an answer on the screen,
    and the region contains no retraction at all. A source check is the right shape for "this
    call does not appear here"; the behaviour it protects is pinned by the two tests below.
    """
    region = _region("# 0b. RESULT GROUNDING", "# Team-await gate:")
    assert "self._guard_keeps_answer(full_content)" in region
    assert "_retract_streamed_reply" not in region
    assert "_clear_last_assistant_ui" not in region
    assert "stream_callback.clear()" not in region
    assert "kept_turn_answers.append(self._clean_reasoning(full_content))" in region
    assert "_grounding_correction(_claim)" in region


def test_a_generation_without_an_answer_is_not_judged_at_all():
    """MUTATION: drop the `_guard_keeps_answer` gate in front of the judge.

    Judging a generation that has no answer spends a validation call and then sends back a
    correction telling the model its reply is still on the user's screen - after that reply was
    erased. The empty-response lane owns that case, and it replaces the generation instead of
    correcting it.
    """
    agent = Agent.__new__(Agent)
    agent.use_server = False
    agent.api_backend = object()
    agent.llm = None
    agent.tools = {}
    asked = []
    agent._run_validation_llm = lambda messages, **kw: (asked.append(1), "</ungrounded>\nCLAIM: x y z")[1]

    echo = "Let me re-run list_files so the claim is produced by a tool in this turn. " * 8
    assert agent._guard_keeps_answer(f"<think>{echo}</think>\n\n{echo}") is False
    assert agent._guard_keeps_answer("<think>weighing it up</think>") is False
    assert agent._guard_keeps_answer("Der Entwurf liegt im Projektordner, die Adresse fehlt.") is True
    # The gate is what keeps the judge out of the no-answer case; the detector itself agrees.
    agent._detect_ungrounded_result_claim("<think>Die Datei wurde gespeichert.</think>", [("write_file", "ok")])
    assert asked == []


def test_retracting_clears_the_bubble_and_puts_the_validated_answers_back():
    """MUTATION: drop the restream, or the clear, from `_retract_streamed_reply`.

    Behavioural, because the sequence is what the other two guards rely on: the buffer is what
    the web bubble shows and what the turn stores, so a clear without the restream loses an
    answer the user was already shown, and a restream without the clear shows it twice.
    """
    agent = Agent.__new__(Agent)
    buf = _Buffer()
    buf("a half-written reply that the guard is about to drop")
    agent._retract_streamed_reply(buf, ["validated answer A"])
    assert buf.clears == 1
    assert buf.chunks == ["validated answer A\n\n"]

    # Nothing validated yet: the buffer is cleared and stays empty.
    buf2 = _Buffer()
    buf2("only thinking")
    agent._retract_streamed_reply(buf2, [])
    assert buf2.clears == 1 and buf2.chunks == []

    # A callback without clear() (the CLI lane) must not raise.
    agent._retract_streamed_reply(lambda _t: None, ["x"])


def test_a_kept_answer_is_not_re_streamed_twice():
    """MUTATION: drop the containment filter from `_restream_kept_answers`.

    After a kept bounce the answer is in `kept_turn_answers`; if the model restates it in the
    corrected round, the buffer would hold it twice while the joined reply holds it once. The
    two consumers of that list must agree on what counts as already there.
    """
    from vaf.core.agent import _restream_kept_answers

    buf = _Buffer()
    _restream_kept_answers(buf, ["Der Entwurf liegt im Ordner.", "Der Entwurf liegt im Ordner."])
    assert buf.chunks == ["Der Entwurf liegt im Ordner.\n\n"]


def test_no_guard_clears_the_stream_buffer_by_hand():
    """MUTATION: paste a `stream_callback.clear()` back into any guard.

    The sequence (drop the bubble, clear the buffer, re-stream the validated answers) exists
    once, in `_retract_streamed_reply`. It used to be three hand copies with three policies,
    and they had drifted: one site cleared the bubble only for a short reply but the buffer in
    every case. The only other legitimate clear is the per-round one that removes pre-tool
    announcements, which is not a retraction.
    """
    clears = SOURCE.count("stream_callback.clear()")
    assert clears == 2, (
        "expected exactly two: the per-round buffer clear and the one inside "
        f"_retract_streamed_reply - found {clears}"
    )
    assert SOURCE.count("self._clear_last_assistant_ui(") == 1, (
        "the bubble retraction has exactly one caller, _retract_streamed_reply"
    )


def test_the_false_promise_keep_survives_the_next_round():
    """MUTATION: remove the `kept_turn_answers.append` from the false-promise keep branch.

    Its comment promises a substantial reply "stays where the user is reading it", but that
    guard's correction orders a tool call, and the next round's buffer clear re-streams only
    `kept_turn_answers`. Without the append the kept text is gone from the screen and from the
    stored reply one round later. Two sites needed the same half; only one had it.
    """
    region = _region("# 0. FALSE PROMISE DETECTION", "# Reset retry counter")
    assert "kept_turn_answers.append(self._clean_reasoning(full_content))" in region
    assert "self._retract_streamed_reply(stream_callback, kept_turn_answers)" in region


def test_the_empty_response_retry_still_retracts_unconditionally():
    """The one branch where clearing is always right: it only runs when nothing answer-shaped
    was produced, so there is nothing to lose."""
    region = _region("Empty response detected. Applying snapshot and retry...", "# First empty only")
    assert "self._retract_streamed_reply(stream_callback, kept_turn_answers)" in region
    assert "if " not in region.split("self._retract_streamed_reply", 1)[1]
