# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What `chat_step` returns when the user presses Stop.

MEASURED BEFORE THE FIX: a stop in the middle of a stream returned `full_content.strip()`,
the raw text: an unclosed `<think>` block came back as the answer, and so did a closed one in
front of the partial reply. `vaf.Agent.run()` passes that through, although the embedding
contract says its value is the cleaned final answer. And none of the four stop exits returned
the answers the turn had already given (the pending-task auto-continue): the failure exit
did, the stop did not.

The marker rule that results: the return carries `[Generation stopped by user]` exactly when
the stopped round wrote no answer text, after the turn's earlier final answers. What the
stream showed and what the history commits are unchanged.

MUTATIONS, each turns the named test red:
- return `full_content.strip() or "[Generation stopped by user]"` at the mid-stream stop again
  -> test_a_midstream_stop_returns_the_cleaned_text[open-think], [redacted], [closed-think],
     and test_a_stop_after_an_answer_returns_the_answer[midstream-*];
- drop the `_join_turn_answers` branch of `_end_turn_on_stop`
  -> test_a_stop_after_an_answer_returns_the_answer (all three) and the two tool-exit tests;
- return the bare marker at the exit after a tool round
  -> test_a_stop_during_a_tool_returns_the_answer_first;
- return the bare marker at the exit between tools
  -> test_a_stop_between_two_tools_returns_the_answer_first;
- drop the `_last_kept_is_final` condition there
  -> test_a_stop_after_a_bounced_reply_returns_no_kept_text.
"""
import pytest

from test_empty_retry_history import STOP, TASK, TOOL, TOOL2, make_agent

MARK = "[Generation stopped by user]"
FIRST = "Antwort eins steht."


@pytest.fixture
def agent(monkeypatch, tmp_path):
    return make_agent(monkeypatch, tmp_path)


def _last_assistant(history):
    return next(m.get("content") for m in reversed(history) if m.get("role") == "assistant")


@pytest.mark.parametrize("first_chunk, returned", [
    pytest.param("<think>open", MARK, id="open-think"),
    pytest.param("<redacted_reasoning>r</redacted_reasoning>", MARK, id="redacted"),
    pytest.param("<think>a</think>Teilantwort", "Teilantwort", id="closed-think"),
    pytest.param("Teilantwort ohne Marken", "Teilantwort ohne Marken", id="plain"),
])
def test_a_midstream_stop_returns_the_cleaned_text(agent, first_chunk, returned):
    """The stop arrives before the second chunk. The return is the cleaned text, or the
    marker when the round wrote no answer text; the stream and the committed message are
    what they were: the round's raw text, then the stop notice."""
    streamed, out = agent.run([[first_chunk, STOP, " und mehr"]])
    assert out == returned
    assert len(agent.requests) == 1
    assert streamed == f"{first_chunk}\n\n{MARK}\n\n", streamed
    assert _last_assistant(agent.history) == first_chunk


def _auto_continue_once(agent, monkeypatch):
    steps = iter([(0, "Pakete installieren", 0, 1)])
    monkeypatch.setattr(agent.main_persistence, "_current_step", lambda tasks: next(steps, None))
    monkeypatch.setattr(agent, "_reply_needs_user", lambda text: False)


@pytest.mark.parametrize("script, returned", [
    pytest.param([FIRST, ["<think>Teil", STOP, " zwei"]], f"{FIRST}\n\n{MARK}",
                 id="midstream-reasoning"),
    pytest.param([FIRST, ["Teil zwei", STOP, " und drei"]], f"{FIRST}\n\nTeil zwei",
                 id="midstream-text"),
    # The stop lands after the answer's last chunk: the round ends, the auto-continue adds its
    # note, and the next loop pass meets the stop at its top.
    pytest.param([[FIRST, STOP]], f"{FIRST}\n\n{MARK}", id="looptop"),
])
def test_a_stop_after_an_answer_returns_the_answer(agent, monkeypatch, script, returned):
    _auto_continue_once(agent, monkeypatch)
    _, out = agent.run(script)
    assert out == returned
    assert agent.last_turn.failed is False


def test_a_stop_during_a_tool_returns_the_answer_first(agent, monkeypatch):
    """The exit after a tool round: an answer, the auto-continue, a round with one tool call
    whose run presses Stop. The tool finishes, the stop is seen after it, and the return is
    the earlier answer with the marker."""
    _auto_continue_once(agent, monkeypatch)
    agent.tools[TASK].on_run = agent.press_stop
    _, out = agent.run([FIRST, TOOL])
    assert len(agent.tools[TASK].calls) == 1
    assert len(agent.requests) == 2, "the turn went on after the stop"
    assert out == f"{FIRST}\n\n{MARK}"


def test_a_stop_between_two_tools_returns_the_answer_first(agent, monkeypatch):
    """The exit between tools: an answer, the auto-continue, a round with two tool calls, the
    first of which presses Stop. The second call never runs, and the return is the earlier
    answer with the marker."""
    _auto_continue_once(agent, monkeypatch)
    agent.tools[TASK].on_run = agent.press_stop
    _, out = agent.run([FIRST, TOOL2])
    assert len(agent.tools[TASK].calls) == 1, "the second call ran after the stop"
    assert len(agent.requests) == 2, "the turn went on after the stop"
    assert out == f"{FIRST}\n\n{MARK}"


def test_a_stop_after_a_bounced_reply_returns_no_kept_text(agent, monkeypatch):
    """A reply the result-grounding guard kept on screen and sent back for correction is an
    unchecked claim: a stop before the correction returns the marker alone, as the failure
    exit returns no such reply."""
    verdicts = iter([(True, "installed")])
    monkeypatch.setattr(agent, "_detect_ungrounded_result_claim",
                        lambda text, res: next(verdicts, (False, None)))
    _, out = agent.run(["Alle zehn Pakete sind installiert.", ["<think>Teil", STOP, " zwei"]])
    assert len(agent.requests) == 2, "the correction round never ran"
    assert out == MARK
