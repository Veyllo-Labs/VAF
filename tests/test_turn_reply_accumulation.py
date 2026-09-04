# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A validated answer must survive the rounds that come after it.

A turn is a loop. The pending-task auto-continue re-enters that loop AFTER an
answer has passed the emptiness probe and the meta-response validation and been
committed to history, and each re-entry resets the response buffers. So the
continuation's closing remark ("already done") became the whole reply, and the
web bubble lost the answer too, because the post-tool buffer clear wiped it off
the screen. Measured live: a 2259-character deliverable validated at step one,
two auto-continue rounds later a 227-character confirmation was all that
remained. Rule 4 invariant 2 (never erase a streamed reply), broken by a line
that looked like initialisation.

The fix is threshold-free because the engine already knows which rounds were
answers: exactly those that reached the final history append. `kept_turn_answers`
collects them, `_join_turn_answers` builds the reply from them, and
`_restream_kept_answers` puts them back into the bubble after every buffer clear.
"""
import re
from pathlib import Path

from vaf.core.agent import _join_turn_answers, _restream_kept_answers

ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────────────
# The join
# ─────────────────────────────────────────────────────────────────────────────

def test_a_single_answer_turn_returns_the_tail_byte_identical():
    """The no-change guarantee: unless displacement happened, the reply is
    exactly what it always was, whitespace included."""
    tail = "  The build is green.\n"
    assert _join_turn_answers(["The build is green."], tail) is tail
    assert _join_turn_answers([], tail) is tail


def test_a_displaced_answer_is_restored_ahead_of_the_closing_remark():
    """THE reported failure, in miniature: the deliverable came first, the
    auto-continue confirmations came later, only the last one survived."""
    deliverable = "1. Alpha\n2. Beta\n3. Gamma"
    joined = _join_turn_answers([deliverable, "Step two is done."],
                                "All steps are complete.")
    assert joined.startswith(deliverable)
    assert joined.endswith("All steps are complete.")
    assert "Step two is done." in joined


def test_an_answer_already_inside_the_tail_is_not_repeated():
    """A model that restates its answer in the closing round must not produce
    the answer twice."""
    joined = _join_turn_answers(["The list: A, B, C."],
                                "The list: A, B, C. Anything else?")
    assert joined == "The list: A, B, C. Anything else?"


def test_duplicate_kept_entries_collapse():
    joined = _join_turn_answers(["Same text.", "Same text."], "Done.")
    assert joined.count("Same text.") == 1


def test_empty_and_none_entries_are_ignored():
    assert _join_turn_answers(["", None, "  "], "Done.") == "Done."


# ─────────────────────────────────────────────────────────────────────────────
# The restream
# ─────────────────────────────────────────────────────────────────────────────

def test_kept_answers_go_back_into_a_cleared_buffer():
    got = []
    _restream_kept_answers(got.append, ["The list.", "Step two done."])
    assert got == ["The list.\n\nStep two done.\n\n"]


def test_nothing_kept_means_nothing_emitted():
    """The buffer clear after an ordinary tool round stays exactly as it was:
    announcements do not come back."""
    got = []
    _restream_kept_answers(got.append, [])
    assert got == []


def test_a_raising_callback_cannot_break_the_turn():
    def boom(_):
        raise RuntimeError("sink gone")
    _restream_kept_answers(boom, ["kept"])   # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# The wiring (static, in the house style: chat_step is not executable in a unit
# test, so the guards pin the source)
# ─────────────────────────────────────────────────────────────────────────────

def _agent_source() -> str:
    return (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")


def test_the_final_append_feeds_the_accumulator():
    """MUTATION: drop the kept_turn_answers.append line and this goes red, and
    the displaced-answer defect is back."""
    src = _agent_source()
    i = src.index('self.history.append({"role": "assistant", "content": history_content})')
    following = src[i:i + 400]
    assert "kept_turn_answers.append(self._clean_reasoning(full_response))" in following, (
        "the validated final answer is no longer captured at the moment it is "
        "committed to history; the next auto-continue round will displace it")


def test_the_return_builds_the_reply_from_the_accumulator():
    src = _agent_source()
    assert "return _join_turn_answers(kept_turn_answers, self._clean_reasoning(full_response))" in src, (
        "chat_step returns the last round alone again")


def test_every_buffer_clear_restores_the_kept_answers():
    """A new clear site without the restream is the same defect in a new place:
    the bubble loses what the user was already shown. Adjacency is checked per
    occurrence, so one covered site cannot vouch for another."""
    src = _agent_source()
    clears = [m.end() for m in re.finditer(r"stream_callback\.clear\(\)", src)]
    assert clears, "no clear sites found; the pattern moved"
    for pos in clears:
        window = src[pos:pos + 120]
        assert "_restream_kept_answers(stream_callback, kept_turn_answers)" in window, (
            "a stream_callback.clear() is not followed by the restream; the web "
            f"bubble loses validated answers there. Context: {src[pos-200:pos+120]!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# The post-tool buffer clear (static, same house style)
# ─────────────────────────────────────────────────────────────────────────────
#
# The web stream buffer is what the bubble shows AND what the session stores as
# the turn's answer. It is cleared before the round that follows a tool round,
# so the rounds before it live only in the tool-call messages they belong to.
# That clear used to be keyed on the history tail being a tool result, and the
# tail is not that whenever a deferred error nudge, an anti-spin or no-progress
# note, a limit reminder or a compaction trim lands after the results - every
# such round kept the buffer growing. Measured live: one stored answer carrying
# seven rounds' think blocks, rendered as plain text after a reload because only
# a LEADING think block is a thinking panel.

def test_the_buffer_clear_is_keyed_on_the_completed_round_not_the_history_tail():
    """MUTATION: restore the `history[-1].get("role") == "tool"` probe and this
    goes red, and the concatenated-rounds defect is back."""
    src = _agent_source()
    assert 'history[-1].get("role") == "tool"' not in src, (
        "the post-tool buffer clear probes the history tail again; a system "
        "note or a compaction after the tool results disables the clear")
    i = src.index("if _tool_round_completed:")
    window = src[i:i + 400]
    assert "_tool_round_completed = False" in window, "the flag is not consumed"
    assert "stream_callback.clear()" in window, "the flag no longer clears the buffer"


def test_the_flag_is_raised_where_the_tool_round_ends():
    """The one `continue` that re-enters the loop after tool execution must
    raise the flag right before it; a tool round that does not raise it is a
    round whose text stacks into the answer."""
    src = _agent_source()
    i = src.index("Summarizing intel (turn {tool_turn_count}")
    following = src[i:i + 300]
    assert "_tool_round_completed = True" in following, (
        "the post-tool continue no longer announces the finished round")
    assert following.index("_tool_round_completed = True") < following.index("continue"), (
        "the flag must be raised BEFORE the continue, or the next round streams "
        "into the old buffer")
