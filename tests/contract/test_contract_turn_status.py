# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Contract: `Agent.last_turn` / `CoreAgent.last_turn`, `vaf.TurnStatus`,
`vaf.stored_turn_copy` and `vaf.model_text` (docs/EMBEDDING.md, "A complete example, with
error handling" and "What is and isn't stable").

`run()` returns a failed turn's text instead of raising, and a turn that answered before a
later round failed returns its answer while `on_token` also received the failure. These
are how an application that keeps its own transcript of the stream knows which of the two
happened, stores what the person saw, and keeps the failure out of what a model reads.
"""
import pytest

import vaf

_KEY_ERROR = "[API Error from openai: Error code: 401 - Incorrect API key provided: sk-****abcd]"
_ANSWER = "Your appointment is on Monday at 10."


# Companion tier: this stub reaches past the public surface - it duck-types the engine
# that vaf.framework builds, which is a harness technique for testing the facade offline,
# not part of the embedding contract itself. Its chat_step streams what the real engine
# streams for each outcome and leaves the reading the real engine leaves.
class _ScriptedCore:
    script = None

    def __init__(self, verbose=False, register_signals=True, config_overrides=None,
                 system_prompt=None):
        self.api_backend = object()
        self.llm = None
        self.use_server = False
        self.tools = {}
        self.config = dict(config_overrides or {})
        self.history = []
        self.last_turn = vaf.TurnStatus()

    def init_chat(self):
        pass

    def chat_step(self, prompt, stream_callback=None):
        chunks, returned, self.last_turn = type(self).script
        for chunk in chunks:
            stream_callback(chunk)
        return returned

    def _clean_reasoning(self, text):
        return text


def _run(monkeypatch, chunks, returned, status):
    import vaf.framework as fw  # harness seam: CoreAgent is patched at its import site

    _ScriptedCore.script = (chunks, returned, status)
    monkeypatch.setattr(fw, "CoreAgent", _ScriptedCore)
    agent = vaf.Agent(config={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    streamed = []
    answer = agent.run("When is my appointment?", on_token=streamed.append)
    return agent, answer, "".join(streamed)


def _readable(copy, metadata):
    return vaf.model_text({"role": "assistant", "content": copy, "metadata": metadata})


def test_the_status_is_a_frozen_value_and_coerces_what_it_is_given():
    status = vaf.TurnStatus()
    assert (status.failed, status.failure) == (False, "")
    assert vaf.TurnStatus(failed=1, failure=None) == vaf.TurnStatus(failed=True)
    with pytest.raises(AttributeError):
        status.failed = True  # noqa: B018


def test_last_turn_has_the_same_name_on_the_facade_and_the_engine():
    assert isinstance(vaf.Agent.last_turn, property)
    assert isinstance(vaf.CoreAgent.last_turn, property)


def test_before_the_first_run_nothing_failed_and_reading_it_builds_no_engine(monkeypatch):
    import vaf.framework as fw  # harness seam: CoreAgent is patched at its import site

    class _Bomb:
        def __init__(self, *a, **k):
            raise AssertionError("reading last_turn built the engine")

    monkeypatch.setattr(fw, "CoreAgent", _Bomb)
    assert vaf.Agent().last_turn == vaf.TurnStatus()


def test_a_clean_turn_is_stored_and_read_as_it_streamed(monkeypatch):
    agent, answer, streamed = _run(monkeypatch, [_ANSWER], _ANSWER, vaf.TurnStatus())
    assert agent.last_turn == vaf.TurnStatus()
    copy, metadata = vaf.stored_turn_copy(streamed, agent.last_turn)
    assert (copy, metadata) == (_ANSWER, None)
    assert _readable(copy, metadata) == _ANSWER == answer


def test_a_failed_turn_is_stored_as_seen_and_a_model_reads_none_of_it(monkeypatch):
    """MUTATION: have the facade's last_turn report nothing failed - red."""
    agent, answer, streamed = _run(monkeypatch, ["One moment. ", _KEY_ERROR],
                                   "One moment. " + _KEY_ERROR,
                                   vaf.TurnStatus(failed=True, failure=_KEY_ERROR))
    assert agent.last_turn.failed is True and vaf.is_error_reply(answer)
    copy, metadata = vaf.stored_turn_copy(streamed, agent.last_turn)
    assert "sk-****abcd" in copy                     # the transcript shows what was seen
    assert _readable(copy, metadata) is None


def test_a_stop_that_never_streamed_still_keeps_the_turn_out(monkeypatch):
    """A loop-protection stop is not streamed: `failure` stays empty, `failed` is set."""
    agent, _, streamed = _run(monkeypatch, ["Let me check."],
                              "[LOOP_PROTECTION] Hard stop.",
                              vaf.TurnStatus(failed=True))
    assert agent.last_turn == vaf.TurnStatus(failed=True)
    copy, metadata = vaf.stored_turn_copy(streamed, agent.last_turn)
    assert copy == "Let me check." and _readable(copy, metadata) is None


def test_a_turn_that_answered_before_a_round_failed_keeps_its_answer(monkeypatch):
    """`run()` returns the answer, the stream also carried the failure: the copy holds both,
    the way the person saw them, and a model reads the answer alone. MUTATION: drop the
    turn's failure from the stored copy's metadata - the key reaches the model, red."""
    agent, answer, streamed = _run(monkeypatch, [_ANSWER, "\n", _KEY_ERROR], _ANSWER,
                                   vaf.TurnStatus(failure=_KEY_ERROR))
    assert answer == _ANSWER
    assert agent.last_turn.failed is False and agent.last_turn.failure == _KEY_ERROR
    copy, metadata = vaf.stored_turn_copy(streamed, agent.last_turn)
    assert copy.startswith(_ANSWER) and copy.endswith(_KEY_ERROR)
    assert _readable(copy, metadata) == _ANSWER


def test_runs_return_value_never_holds_a_failure_that_followed_the_answer(monkeypatch):
    """When the engine returns a placeholder, `run()` falls back to the stream; a failure a
    later round streamed after the answer never comes back that way, so storing the return
    value needs only `last_turn.failed`. A stream that does not end with its failure is not
    used at all. MUTATION: drop the take-off in `Agent.run` - the key is in the return
    value, red. MUTATION: fall back to the raw stream when the take-off fails - red."""
    _, answer, streamed = _run(monkeypatch, [_ANSWER, "\n", _KEY_ERROR], "...",
                               vaf.TurnStatus(failure=_KEY_ERROR))
    assert answer == _ANSWER and _KEY_ERROR in streamed
    _, answer, _ = _run(monkeypatch, [_ANSWER, "\n", _KEY_ERROR, " (2)"], "...",
                        vaf.TurnStatus(failure=_KEY_ERROR))
    assert "sk-****abcd" not in answer


def test_your_own_cleanup_runs_on_the_answer_and_the_failure_apart(monkeypatch):
    """A cleanup that would join or cut across the boundary (here: collapse every run of
    blank lines) still leaves the failure as the copy's exact tail, so it comes off."""
    import re

    def collapse(text):
        return re.sub(r"\n{2,}", "\n", text).strip()

    agent, _, streamed = _run(monkeypatch, ["<think>x</think>" + _ANSWER, "\n\n\n", _KEY_ERROR],
                              _ANSWER, vaf.TurnStatus(failure=_KEY_ERROR))
    copy, metadata = vaf.stored_turn_copy(streamed, agent.last_turn, collapse)
    assert _readable(copy, metadata) == "<think>x</think>" + _ANSWER


def test_they_are_the_engine_objects_themselves():
    import vaf.core.session as session  # re-export source named in EMBEDDING.md

    assert vaf.TurnStatus is session.TurnStatus
    assert vaf.stored_turn_copy is session.stored_turn_copy
    assert vaf.model_text is session.model_text
