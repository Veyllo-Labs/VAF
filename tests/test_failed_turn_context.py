# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A turn that produced no answer never reaches the model's context again.

chat_step used to commit a provider's failure, streamed as a content chunk ("[API Error from
openai: ... Incorrect API key provided: sk-proj-****abcd]"), as the assistant's answer, and
the three loop-protection stops committed their note (the budget stop names the owner's spend
and a config key). The queue runner stored every failure shape as the chat's answer and the
replay put it back after a session switch or a restart. On the next turn the model read it as
its own words; asked "what happened?", it could repeat it without the marker, which then
passed every delivery check and went to the contact.

Now no failure or stop text is committed, the turn says so in `_turn_failed`, a host that
stores the streamed text marks its copy (`session.failed_turn_meta`), and every reader that
builds a model's context from a stored chat skips it (`session.is_failed_turn`)."""
import contextvars
import inspect
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_KEY_ERROR = ("[API Error from openai: Error code: 401 - Incorrect API key provided: "
              "sk-proj-****abcd]")
_PATH_ERROR = "database is locked: /home/user/.vaf/users/alice/chats.db"
_SPEND = "4.12"


@pytest.fixture
def engine(monkeypatch, tmp_path):
    """The REAL chat_step with only the provider faked. `script` maps a turn number to a
    generator function for the main call (the one carrying tools); every main call's
    messages are recorded per turn."""
    from vaf.core.agent import Agent
    from vaf.core.platform import Platform
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    a.current_session_id = "green123456"
    a._current_chat_source = "whatsapp"
    monkeypatch.setattr(a, "_try_workflow", lambda *args, **kw: None)
    state = {"turn": 0, "script": {}, "seen": {}}

    def provider(messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        state["seen"].setdefault(state["turn"], []).append([dict(m) for m in messages])
        yield from state["script"][state["turn"]]()
    a.api_backend.chat_completion = provider

    def turn(text, main_call):
        state["turn"] += 1
        state["script"][state["turn"]] = main_call
        streamed = []
        returned = a.chat_step(user_input=text, raw_user_input=text,
                               stream_callback=streamed.append)
        return "".join(streamed), returned

    def reload(session_id):
        # load_session_context declares the session on the calling context; run in a copy,
        # that declaration does not outlive the test (the contract suite asserts a context
        # nobody told).
        contextvars.copy_context().run(a.load_session_context, session_id, force=True)

    a.turn = turn
    a.reload = reload
    a.seen = state["seen"]
    return a


def _context_of(seen_calls) -> str:
    return json.dumps(seen_calls, ensure_ascii=False, default=str)


def _fails_with_the_sentinel():
    yield _KEY_ERROR


def _fails_after_text():
    yield "Einen Moment, ich schaue nach. "
    yield _KEY_ERROR


def _raises():
    raise RuntimeError(_PATH_ERROR)
    yield  # a generator, like the real provider


def _answers():
    yield "Alles in Ordnung, bis morgen."


@pytest.mark.parametrize("failing, trace", [
    (_fails_with_the_sentinel, "sk-proj-****abcd"),
    (_fails_after_text, "sk-proj-****abcd"),
    (_raises, _PATH_ERROR),
], ids=["sentinel", "sentinel-after-text", "raises"])
def test_a_failed_turn_is_not_in_the_next_turns_context(engine, failing, trace):
    """MUTATION: commit the failed round anyway (append `{"role": "assistant", "content":
    full_content}` in the round-failure exit of chat_step) - the next turn's messages carry
    the key, red for the two sentinel cases."""
    from vaf.core.completion import is_error_reply
    streamed, returned = engine.turn("Hallo", failing)
    assert trace in streamed                       # the person still sees what happened
    assert is_error_reply(returned) and engine._turn_failed is True
    assert not any(trace in str(m.get("content")) for m in engine.history)

    streamed, _ = engine.turn("Was war los?", _answers)
    assert engine._turn_failed is False            # reset for every turn
    assert "Alles in Ordnung" in streamed
    assert trace not in _context_of(engine.seen[2])
    assert "Hallo" in _context_of(engine.seen[2])  # the request itself is still there


def test_the_backends_own_error_chunk_ends_the_round(engine):
    """A provider whose SDK is missing yields "[Error] ... SDK missing." as its first chunk.
    The round ends on it: it is shown once, not committed, and the same backend is not asked
    again without streaming (that repeated the error and committed it as the answer).
    MUTATION: drop `and not _round_failure` from the non-stream retry - red."""
    def sdk_missing():
        yield "[Error] OpenAI SDK missing."

    streamed, returned = engine.turn("Hallo", sdk_missing)
    assert streamed.count("SDK missing") == 1 and "SDK missing" in returned
    assert engine._turn_failed is True and len(engine.seen[1]) == 1
    assert not any("SDK missing" in str(m.get("content")) for m in engine.history)


def test_a_loop_protection_stop_is_not_in_the_next_turns_context(engine, monkeypatch):
    """The budget stop names the owner's spend and a config key. MUTATION: put
    `self.history.append({"role": "assistant", "content": _sp_msg})` back - red."""
    import vaf.core.cost as cost
    monkeypatch.setattr(cost, "budget_exceeded", lambda scope: (True, float(_SPEND), 5.0))

    def calls_a_tool():
        yield json.dumps({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                          "function": {"name": "list_tools", "arguments": "{}"}}]})

    _, returned = engine.turn("Wie spät ist es?", calls_a_tool)
    from vaf.markers import LOOP_PROTECTION
    assert LOOP_PROTECTION in returned and _SPEND in returned
    assert engine._turn_failed is True
    monkeypatch.setattr(cost, "budget_exceeded", lambda scope: (False, 0.0, 0.0))
    engine.turn("Was war los?", _answers)
    context = _context_of(engine.seen[2])
    assert _SPEND not in context and "spend_budget_usd_per_day" not in context


def test_a_stored_failed_turn_is_not_replayed(engine):
    """The runner stores the streamed text for the web UI, marked by the engine's own flag.
    After a session switch or a restart the replay rebuilds the history from that file.
    MUTATION: drop the `is_failed_turn` skip in `load_session_context` - red."""
    from vaf.core.session import SessionManager, failed_turn_meta
    streamed, _ = engine.turn("Hallo", _fails_after_text)
    sm = SessionManager()
    s = sm.new(name="WhatsApp contact")
    s.add_message(role="user", content="Hallo")
    s.add_message(role="assistant", content=streamed,
                  metadata=failed_turn_meta(engine._turn_failed))
    sm.save(s)

    engine.reload(s.id)
    assert not any("sk-proj" in str(m.get("content")) for m in engine.history)
    engine.turn("Was war los?", _answers)
    assert "sk-proj-****abcd" not in _context_of(engine.seen[2])
    assert "Hallo" in _context_of(engine.seen[2])


def test_the_replay_keeps_answers_and_drops_failures(engine):
    """Transcripts stored before the mark existed hold failures as plain answers; the replay
    knows them by how they begin. A real answer that quotes a status further in, and a
    tool-call message (its calls anchor the results after it), are kept."""
    from vaf.core.session import SessionManager
    sm = SessionManager()
    s = sm.new(name="old chat")
    s.add_message(role="user", content="eins")
    s.add_message(role="assistant", content="[Error] API backend failure: " + _PATH_ERROR)
    s.add_message(role="user", content="zwei")
    s.add_message(role="assistant", content="<think>hm</think>\n\n" + _KEY_ERROR)
    s.add_message(role="user", content="drei")
    s.add_message(role="assistant", content="⚠️ [LOOP_PROTECTION] Daily API budget reached "
                                            f"(~${_SPEND} of $5.00 estimated).")
    s.add_message(role="user", content="vier")
    s.add_message(role="assistant", content="Das Log zeigt: [Error] disk full. Ich räume auf.")
    s.add_message(role="assistant", content="[API Error from x: y]",
                  tool_calls=[{"id": "call_9", "type": "function",
                               "function": {"name": "list_tools", "arguments": "{}"}}])
    s.add_message(role="tool", content="ok", tool_call_id="call_9", name="list_tools")
    s.add_message(role="assistant", content="Fertig.", metadata={"turn_failed": True})
    sm.save(s)

    engine.reload(s.id)
    assistants = [str(m.get("content")) for m in engine.history if m.get("role") == "assistant"]
    assert assistants == ["Das Log zeigt: [Error] disk full. Ich räume auf.", "[API Error from x: y]"]
    assert [m.get("content") for m in engine.history if m.get("role") == "user"] == [
        "eins", "zwei", "drei", "vier"]


def test_the_other_transcript_readers_skip_a_failed_turn(tmp_path, monkeypatch):
    """`Session.get_history()` (the CLI's resume), the chat compaction's transcript and the
    Cross Chat Hint scan read stored chats into a model's context too. MUTATION: drop the
    skip in any one of the three - red."""
    monkeypatch.chdir(tmp_path)
    from vaf.core import cross_chat
    from vaf.core.session import SessionManager
    from vaf.memory.rag import session_dialogue_excerpt
    sm = SessionManager()
    s = sm.new(name="chat")
    s.add_message(role="user", content="Hallo")
    s.add_message(role="assistant", content="Einen Moment.\n" + _KEY_ERROR,
                  metadata={"turn_failed": True})
    s.add_message(role="user", content="Und?")
    s.add_message(role="assistant", content="Erledigt.")
    sm.save(s)

    history = sm.load(s.id).get_history()
    assert [m["content"] for m in history] == ["Hallo", "Und?", "Erledigt."]
    excerpt = session_dialogue_excerpt(s.id)
    assert "Erledigt." in excerpt and "sk-proj" not in excerpt
    scanned = cross_chat._scannable_messages({"messages": [m.to_dict() for m in s.messages]})
    assert [m["content"] for m in scanned] == ["Hallo", "Und?", "Erledigt."]


def test_is_failed_turn():
    from vaf.core.session import Message, failed_turn_meta, is_failed_turn
    assert failed_turn_meta(False) is None
    assert is_failed_turn(Message(role="assistant", content="Fertig.",
                                  metadata=failed_turn_meta(True)))
    assert is_failed_turn({"role": "assistant", "content": "  [Error] Server unavailable"})
    assert not is_failed_turn({"role": "user", "content": "[Error] in my log, what is it?"})
    assert not is_failed_turn({"role": "assistant", "content": "Siehe [Error] oben."})
    assert not is_failed_turn({"role": "assistant", "content": "x", "metadata": "garbled"})


def test_the_hosts_that_store_the_stream_mark_a_failed_turn():
    """The queue runner stores the streamed text (both of its save branches), the CLI and
    the TUI theirs (`_process_agent_message`, and `vaf run -p` with a saved session). Each
    marks the copy from the engine's flag, read by the runner before anything else runs a
    chat_step on that agent. MUTATION: drop the metadata from one save - red."""
    from vaf.cli.cmd import run
    from vaf.core import headless_runner as hr
    loop = inspect.getsource(hr.run_headless_agent)
    assert loop.count("metadata=failed_turn_meta(_turn_failed))") == 2
    assert loop.count('add_message(role="assistant", content=_clean_response') == 2
    flag = '_turn_failed = bool(getattr(agent, "_turn_failed", False))'
    assert loop.index(flag) < loop.index("_hallucination_detected = (")
    assert "turn_failed=_turn_failed)" in loop
    cli = inspect.getsource(run._process_agent_message) + inspect.getsource(run.prompt_once)
    assert cli.count('metadata=failed_turn_meta(getattr(agent, "_turn_failed", False))') == 3
    assert cli.count('add_message("assistant"') == 3


def test_every_engine_failure_exit_sets_the_flag():
    """Every exit that returns the engine's text instead of an answer sets `_turn_failed`;
    none of them appends that text to the history. MUTATION: drop the flag from one - red."""
    src = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    for exit_text in ('return "[Error] Server unavailable after retries.',
                      "return err_msg", "return _sp_msg", "return _wc_msg", "return _kill_msg"):
        for at in [i for i in range(len(src)) if src.startswith(exit_text, i)]:
            before = src[max(0, at - 700):at]
            assert "self._turn_failed = True" in before, exit_text
    for stop in ("_sp_msg", "_wc_msg", "_kill_msg"):
        assert f'"content": {stop}}}' not in src, stop


def test_a_failure_chunk_is_a_chunk_that_begins_with_the_sentinel():
    from vaf.core.completion import is_failure_chunk
    assert is_failure_chunk(_KEY_ERROR)
    assert not is_failure_chunk("Die Meldung war " + _KEY_ERROR)
    assert not is_failure_chunk(None) and not is_failure_chunk({"content": _KEY_ERROR})
