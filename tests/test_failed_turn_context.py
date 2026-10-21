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
builds a model's context from a stored chat reads it through `session.model_text`, which
leaves it out (and takes off a failure that followed an answer)."""
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
    the key, red for the two sentinel cases. MUTATION: drop the reset of `_turn_failure` at
    chat_step entry - the good second turn still carries the first turn's failure (a host
    would mark its stored answer), red."""
    from vaf.core.completion import is_error_reply
    streamed, returned = engine.turn("Hallo", failing)
    assert trace in streamed                       # the person still sees what happened
    assert is_error_reply(returned) and engine._turn_failed is True
    assert trace in engine._turn_failure           # it was streamed
    assert not any(trace in str(m.get("content")) for m in engine.history)

    streamed, _ = engine.turn("Was war los?", _answers)
    assert engine._turn_failed is False            # both reset for every turn
    assert engine._turn_failure == ""
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


@pytest.mark.parametrize("second, trace", [
    (_fails_with_the_sentinel, "sk-proj-****abcd"),
    (_raises, _PATH_ERROR),
], ids=["sentinel", "raises"])
def test_a_turn_that_answered_before_a_round_failed_keeps_its_answer(engine, monkeypatch,
                                                                    second, trace):
    """The auto-continue re-enters a turn after an answer was validated, committed and
    shown. When that later round fails, the turn HAS answered: it returns the answer and is
    not failed, so a contact gets the answer. The failure stays in the stream (nothing
    streamed is erased) and in `_turn_failure`, never in the history, and a stored copy
    reads back as the answer alone. MUTATION: drop the `if kept_turn_answers` branch of
    `_end_turn_without_answer` - red."""
    from vaf.core.session import Message, failed_turn_meta, model_text
    calls = {"n": 0}

    def answers_then_fails():
        calls["n"] += 1
        if calls["n"] == 1:
            yield "Ihr Termin ist am Montag um 10 Uhr."
            return
        yield from second()

    # One open step in the working memory makes the turn continue after its answer.
    monkeypatch.setattr(engine.main_persistence, "_current_step",
                        lambda tasks: (0, "Termin bestätigen", 0, 2))
    monkeypatch.setattr(engine, "_reply_needs_user", lambda text: False)
    streamed, returned = engine.turn("Wann ist mein Termin?", answers_then_fails)
    assert calls["n"] == 2, "the turn did not continue after its answer"
    assert returned.strip() == "Ihr Termin ist am Montag um 10 Uhr."
    assert engine._turn_failed is False and trace in engine._turn_failure
    assert "Ihr Termin" in streamed and trace in streamed
    assert any("Ihr Termin" in str(m.get("content")) for m in engine.history)
    assert not any(trace in str(m.get("content")) for m in engine.history)

    stored = Message(role="assistant", content=streamed,
                     metadata=failed_turn_meta(engine._turn_failed, engine._turn_failure))
    assert model_text(stored).startswith("Ihr Termin ist am Montag um 10 Uhr.")
    assert trace not in model_text(stored)


@pytest.mark.parametrize("host", ["_chat_turn_copy", "_drain_turn_copy"],
                         ids=["chat-lane", "sub-agent-drain"])
@pytest.mark.parametrize("case", ["answered-then-raised", "failed-after-text"])
def test_the_runners_copies_never_hand_a_failure_to_the_next_turn(engine, monkeypatch,
                                                                  host, case):
    """Both runner hosts store the turn's stream through their own cleanup: the chat lane
    and the sub-agent result drain, whose summary turn is a turn like any other. A drain
    turn that answered, auto-continued and then raised carries a path in its tail; one that
    failed after streaming some text carries the masked key, and its stored text does not
    begin with a marker. The REAL engine streams, the host's REAL copy function builds the
    copy and its metadata, and the replay feeds the next turn; the runner calls these at its
    two save sites (`test_the_hosts_that_store_the_stream_mark_a_failed_turn`). MUTATION:
    return the drain's copy without its metadata - the failed case hands the key to the next
    turn, red. MUTATION: record the raw failure next to a cleaned copy - the chat lane's
    collapse breaks it on the triple newline, red."""
    from vaf.core import headless_runner as hr
    from vaf.core.session import SessionManager
    calls = {"n": 0}
    triple = "database is locked:\n\n\n" + _PATH_ERROR

    def answers_then_raises():
        calls["n"] += 1
        if calls["n"] == 1:
            yield "Der Sub-Agent hat drei Dateien gefunden."
            return
        raise RuntimeError(triple)
        yield

    if case == "answered-then-raised":
        monkeypatch.setattr(engine.main_persistence, "_current_step",
                            lambda tasks: (0, "Ergebnis melden", 0, 2))
        monkeypatch.setattr(engine, "_reply_needs_user", lambda text: False)
        streamed, _ = engine.turn("Fasse das Ergebnis zusammen.", answers_then_raises)
        assert engine._turn_failed is False and _PATH_ERROR in engine._turn_failure
        trace = _PATH_ERROR
    else:
        streamed, _ = engine.turn("Fasse das Ergebnis zusammen.", _fails_after_text)
        assert engine._turn_failed is True
        trace = "sk-proj-****abcd"
    if host == "_chat_turn_copy":
        copy, meta = hr._chat_turn_copy([streamed], "egal", ends_at_draft=False,
                                        failed=engine._turn_failed,
                                        turn_failure=engine._turn_failure)
    else:
        copy, meta = hr._drain_turn_copy([streamed], failed=engine._turn_failed,
                                         turn_failure=engine._turn_failure)
    sm = SessionManager()
    s = sm.new(name="chat")
    s.add_message("user", "Fasse das Ergebnis zusammen.")
    s.add_message("assistant", copy, metadata=meta)
    sm.save(s)

    engine.reload(s.id)
    engine.turn("Und jetzt?", _answers)
    context = _context_of(engine.seen[2])
    assert trace not in context
    if case == "answered-then-raised":
        assert "Der Sub-Agent hat drei Dateien gefunden." in context   # the answer stays


def test_the_chat_lane_records_a_failure_only_when_its_copy_is_the_stream():
    """The chat lane stores the stream, or the turn's return value when nothing streamed or
    the turn ended at a draft. The return value never holds a failure that followed an
    answer, so recording one there would make every reader drop the answer the person saw.
    MUTATION: record the failure for a return-value copy - red. MUTATION: record the raw
    failure next to the cleaned stream - its blank-line run is collapsed in the copy, so a
    reader drops the answer, red."""
    from vaf.core import headless_runner as hr
    from vaf.core.session import Message, model_text
    failure = "[Error] API backend failure: locked\n\n\n\n" + _PATH_ERROR
    copy, meta = hr._chat_turn_copy(["Der Termin ist Montag.", "\n", failure], "egal",
                                    ends_at_draft=False, failed=False, turn_failure=failure)
    assert meta and copy.endswith(meta["turn_failure"])
    assert model_text(Message(role="assistant", content=copy, metadata=meta)) == \
        "Der Termin ist Montag."
    for parts, ends_at_draft in (([], False), (["Vorher gestreamt."], True)):
        copy, meta = hr._chat_turn_copy(parts, "Der Termin ist Montag.", ends_at_draft=ends_at_draft,
                                        failed=False, turn_failure=failure)
        assert (copy, meta) == ("Der Termin ist Montag.", None)
    copy, meta = hr._chat_turn_copy([], _KEY_ERROR, ends_at_draft=False, failed=True,
                                    turn_failure="")
    assert meta == {"turn_failed": True}


def test_the_drain_marks_its_copy():
    """The sub-agent drain's summary turn is stored through `_drain_turn_copy`: its cleanup
    removes the reasoning and leaked tool-call JSON, an answered-then-failed turn records its
    failure as the copy's tail, and a failed turn is marked. MUTATION: return the drain's
    copy without its metadata - red."""
    from vaf.core import headless_runner as hr
    from vaf.core.session import Message, model_text
    failure = "[Error] API backend failure: " + _PATH_ERROR
    copy, meta = hr._drain_turn_copy(["<think>Ich fasse zusammen.</think>Drei Dateien gefunden.",
                                      "\n", failure], failed=False, turn_failure=failure)
    assert "<think>" not in copy and copy.endswith(meta["turn_failure"])
    assert model_text(Message(role="assistant", content=copy, metadata=meta)) == \
        "Drei Dateien gefunden."
    copy, meta = hr._drain_turn_copy(["Einen Moment. ", _KEY_ERROR], failed=True,
                                     turn_failure=_KEY_ERROR)
    assert meta == {"turn_failed": True}
    assert model_text(Message(role="assistant", content=copy, metadata=meta)) is None


def test_stored_turn_copy_keeps_the_failure_verbatim_whatever_the_cleanup_does():
    """The failure is cleaned apart from the answer and stays the copy's tail, so a reader
    finds and takes it off exactly. A failure the cleanup erases is not recorded. A stream
    that does not end with its failure was changed after the engine's last chunk; whether
    the cleanup kept failure text across the boundary cannot be told, so the copy is marked
    failed, by construction. MUTATION: clean the whole stream and record the failure
    cleaned alone - the collapse case no longer finds it, red. MUTATION: record the cleaned
    failure for a stream that does not end with it - a cleanup that keeps failure text in
    front of the recorded tail hands it to a reader, red."""
    from vaf.core import headless_runner as hr
    from vaf.core.session import Message, model_text, stored_turn_copy
    answer = "Der Termin ist Montag."
    for clean in (hr._clean_for_session, hr._clean_drain_text):
        for failure in ("[Error] API backend failure: a\n\n\n\nb " + _PATH_ERROR + "\n",
                        _KEY_ERROR, '[Error] bad request, "name": "send_mail"'):
            copy, meta = stored_turn_copy(answer + "\n" + failure, False, failure, clean)
            assert meta and copy.endswith(meta["turn_failure"])
            readable = model_text(Message(role="assistant", content=copy, metadata=meta))
            assert readable == answer, (clean.__name__, failure)
    assert stored_turn_copy(answer, False, "", hr._clean_for_session) == (answer, None)
    copy, meta = stored_turn_copy(answer + " [x]", False, "[x]", lambda t: t.replace("[x]", ""))
    assert meta is None and "[x]" not in copy
    # Text after the failure, and a cleanup that eats across the boundary: the cleaned failure
    # " Ja" is still the copy's tail, while failure text stands in front of it.
    eats = lambda t: t.replace("[DIM", "").replace("x dim] ", "")  # noqa: E731
    stream = "Antwort [DIM" + "[Error] QQSECRET42 [x dim] Ja" + "\n"
    copy, meta = stored_turn_copy(stream, False, "[Error] QQSECRET42 [x dim] Ja", eats)
    assert meta == {"turn_failed": True}
    assert model_text(Message(role="assistant", content=copy, metadata=meta)) is None


def test_a_stop_that_never_streamed_leaves_the_answer_readable(engine, monkeypatch):
    """The spend stop ends a turn that had answered (auto-continue, then a tool round). The
    note goes to an event, never to the stream, so the stored copy cannot hold it: nothing is
    recorded for a host to take off, and the stored answer stays what every reader reads.
    MUTATION: record the text whether or not it streamed - the copy gets a failure it does
    not hold, red."""
    import vaf.core.cost as cost
    from vaf.core.session import Message, failed_turn_meta, model_text
    over = {"v": False}
    monkeypatch.setattr(cost, "budget_exceeded", lambda scope: (over["v"], float(_SPEND), 5.0))
    calls = {"n": 0}

    def answers_then_calls_a_tool():
        calls["n"] += 1
        if calls["n"] == 1:
            yield "Ihr Termin ist am Montag um 10 Uhr."
            return
        over["v"] = True
        yield json.dumps({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                          "function": {"name": "list_tools", "arguments": "{}"}}]})

    monkeypatch.setattr(engine.main_persistence, "_current_step",
                        lambda tasks: (0, "Termin bestätigen", 0, 2))
    monkeypatch.setattr(engine, "_reply_needs_user", lambda text: False)
    streamed, returned = engine.turn("Wann ist mein Termin?", answers_then_calls_a_tool)
    # Two rounds at least: the tool-only round may be asked again without streaming.
    assert calls["n"] >= 2 and over["v"] is True and _SPEND not in streamed
    assert returned.strip() == "Ihr Termin ist am Montag um 10 Uhr."
    assert engine._turn_failed is False and engine._turn_failure == ""
    stored = Message(role="assistant", content=streamed,
                     metadata=failed_turn_meta(engine._turn_failed, engine._turn_failure))
    assert (model_text(stored) or "").startswith("Ihr Termin ist am Montag um 10 Uhr.")


@pytest.mark.parametrize("answered_first", [False, True], ids=["flagged", "answer-then-flagged"])
def test_a_reply_under_correction_does_not_count_as_the_answer(engine, monkeypatch,
                                                              answered_first):
    """The result-grounding guard keeps a flagged reply on screen and asks for a correction.
    When the correction round fails, that reply is an unchecked claim: the turn has failed,
    and a contact gets the fixed sentence, never the claim, also when a validated answer came
    before it. MUTATION: count any kept reply as the answer (drop `and _last_kept_is_final`)
    - the claim goes to the contact, red."""
    from types import SimpleNamespace

    import vaf.core.telegram_reply as tr
    from vaf.core import headless_runner as hr
    claim = "I have forwarded your request to Alice, she will call you back today."
    judged = {"n": 0}

    def judge(text, results):
        judged["n"] += 1
        return (judged["n"] == (2 if answered_first else 1), "forwarded your request to Alice")

    monkeypatch.setattr(engine, "_detect_ungrounded_result_claim", judge)
    monkeypatch.setattr(engine, "_guard_keeps_answer", lambda text: True)
    if answered_first:
        monkeypatch.setattr(engine.main_persistence, "_current_step",
                            lambda tasks: (0, "Alice informieren", 0, 2))
        monkeypatch.setattr(engine, "_reply_needs_user", lambda text: False)
    calls = {"n": 0}
    script = (["Ihr Termin ist am Montag.", claim] if answered_first else [claim])

    def replies_then_fails():
        calls["n"] += 1
        if calls["n"] <= len(script):
            yield script[calls["n"] - 1]
            return
        yield _KEY_ERROR

    streamed, returned = engine.turn("Bitte sagen Sie Alice Bescheid.", replies_then_fails)
    assert calls["n"] == len(script) + 1, "the correction round never ran"
    assert claim in streamed                       # it stays on the owner's screen
    assert engine._turn_failed is True and "sk-proj" in returned

    sent = []
    monkeypatch.setattr(tr, "send_telegram_reply", lambda chat_id, text, *a, **k: sent.append(text))
    task = SimpleNamespace(source="telegram", session_id="s1",
                           metadata={"telegram_chat_id": "1001", "from_contact": True})
    hr._deliver_channel_reply(task, streamed, turn_failed=engine._turn_failed,
                              turn_failure=engine._turn_failure, contact=None)
    assert sent == ["Sorry, I can't reply right now. Please try again a little later."]


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
    MUTATION: drop the `model_text` skip in `load_session_context` - red."""
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
    s.add_message(role="assistant", content="Ein [LOOP_PROTECTION]-Stopp heißt: die Runde war zu lang.")
    s.add_message(role="assistant", content="[API Error from x: y]",
                  tool_calls=[{"id": "call_9", "type": "function",
                               "function": {"name": "list_tools", "arguments": "{}"}}])
    s.add_message(role="tool", content="ok", tool_call_id="call_9", name="list_tools")
    s.add_message(role="assistant", content="Fertig.", metadata={"turn_failed": True})
    sm.save(s)

    engine.reload(s.id)
    assistants = [str(m.get("content")) for m in engine.history if m.get("role") == "assistant"]
    assert assistants == ["Das Log zeigt: [Error] disk full. Ich räume auf.",
                          "Ein [LOOP_PROTECTION]-Stopp heißt: die Runde war zu lang.",
                          "[API Error from x: y]"]
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


def test_a_stop_note_is_one_that_begins_with_the_marker():
    """A real answer that explains the marker is an answer. MUTATION: test for the marker
    anywhere in the text - red."""
    from vaf.core.completion import is_stop_note
    assert is_stop_note("\u26a0\ufe0f [LOOP_PROTECTION] Hard stop enforced after 75 tool turns.")
    assert is_stop_note("  [LOOP_PROTECTION] Wall-clock stop")
    assert not is_stop_note("Ein [LOOP_PROTECTION]-Stopp heißt: die Runde war zu lang.")
    assert not is_stop_note(None)


def test_model_text():
    """An answer that a later failure followed reads back without it. A recorded failure the
    stored text does not hold verbatim drops the message, the safe side: an altered failure
    need carry no signature at all (see the CLI test below), so clean-looking content proves
    nothing. A failure the rest holds once more drops the message too: the rest cannot be
    told from failure text (the failure stored twice, or a cleaned failure as short as a
    `<think>` the answer holds too). MUTATION: return the content when the failure is not
    found - red. MUTATION: return "" for a message that was the failure alone - red.
    MUTATION: drop the held-once-more check - the second copy of the key, and the answer's
    own `<think>` block passed off as an answer, red."""
    from vaf.core.session import Message, failed_turn_meta, model_text
    assert failed_turn_meta(False, "x") == {"turn_failure": "x"}
    assert failed_turn_meta(True, "x") == {"turn_failed": True}
    answered = Message(role="assistant", content="Erledigt.\n\n" + _KEY_ERROR,
                       metadata=failed_turn_meta(False, _KEY_ERROR))
    assert model_text(answered) == "Erledigt."
    altered = Message(role="assistant", content="Erledigt. [API Error from openai: gekürzt]",
                      metadata=failed_turn_meta(False, _KEY_ERROR))
    assert model_text(altered) is None
    altered_stop = Message(role="assistant", content="Erledigt. [LOOP_PROTECTION] gekürzt",
                           metadata=failed_turn_meta(False, "\u26a0\ufe0f [LOOP_PROTECTION] x"))
    assert model_text(altered_stop) is None
    looks_clean = Message(role="assistant", content="Der Termin ist Montag.",
                          metadata=failed_turn_meta(False, _KEY_ERROR))
    assert model_text(looks_clean) is None
    # A message that was the recorded failure alone. A failure that begins with a marker is
    # caught before (`is_failed_turn`); one without a marker must not read as an empty answer,
    # which `get_history` would replay as an empty assistant entry some providers reject.
    only = Message(role="assistant", content="  timeout after 30s\n",
                   metadata=failed_turn_meta(False, "timeout after 30s"))
    assert model_text(only) is None
    twice = Message(role="assistant", content="A " + _KEY_ERROR + "\n\n" + _KEY_ERROR,
                    metadata=failed_turn_meta(False, _KEY_ERROR))
    assert model_text(twice) is None
    think = Message(role="assistant", content="A <think>b</think>\n\n<think>",
                    metadata=failed_turn_meta(False, "<think>"))
    assert model_text(think) is None
    assert model_text(Message(role="assistant", content=_KEY_ERROR,
                              metadata=failed_turn_meta(True))) is None
    assert model_text(Message(role="user", content="Hallo")) == "Hallo"


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
    """Every host that stores a turn marks its copy from the engine's flags, read before
    anything else runs a chat_step on that agent, and records a failure that followed an
    answer the way the copy holds it (`stored_turn_copy` with the host's own cleanup), or
    none when the copy is the turn's return value:
    - the runner's chat lane (both save branches; the stream, or the return value when
      nothing streamed or the turn ended at a draft), which hands both flags to delivery;
    - the runner's sub-agent drain;
    - the interactive CLI and the TUI (`_process_agent_message`), and `vaf run -p`, whose
      copy is the return value.
    MUTATION: drop the metadata from one save - red."""
    from vaf.cli.cmd import run
    from vaf.core import headless_runner as hr
    loop = " ".join(inspect.getsource(hr.run_headless_agent).split())
    assert loop.count("metadata=_turn_meta)") == 2
    assert loop.count('add_message(role="assistant", content=_clean_response') == 2
    assert ("_clean_response, _turn_meta = _chat_turn_copy( response_parts, response_text, "
            "ends_at_draft=_turn_ends_at_draft, failed=_turn_failed, "
            "turn_failure=_turn_failure)") in loop
    for flag in ('_turn_failed = bool(getattr(agent, "_turn_failed", False))',
                 '_turn_failure = str(getattr(agent, "_turn_failure", "") or "")'):
        assert loop.index(flag) < loop.index("_hallucination_detected = ("), flag
    assert "turn_failed=_turn_failed, turn_failure=_turn_failure)" in loop

    drain_call = loop.index("drain_summary_text = agent.chat_step(")
    for flag in ('_drain_failed = bool(getattr(agent, "_turn_failed", False))',
                 '_drain_failure = str(getattr(agent, "_turn_failure", "") or "")'):
        assert drain_call < loop.index(flag) < loop.index("_clean_result, _drain_meta"), flag
    assert ("_clean_result, _drain_meta = _drain_turn_copy( response_parts, "
            "failed=_drain_failed, turn_failure=_drain_failure)") in loop
    assert 'add_message( role="assistant", content=_clean_result, metadata=_drain_meta)' in loop

    once = " ".join(inspect.getsource(run.prompt_once).split())
    assert once.count('metadata=failed_turn_meta(getattr(agent, "_turn_failed", False)))') == 2
    assert once.count('add_message("assistant"') == 2 and "_turn_failure" not in once
    classic = " ".join(inspect.getsource(run._process_agent_message).split())
    assert ("full_response_clean, _turn_meta = stored_turn_copy( full_response_raw, "
            'getattr(agent, "_turn_failed", False), getattr(agent, "_turn_failure", "") or "", '
            "convert_rich_to_xml)") in classic
    assert 'add_message("assistant", full_response_clean, metadata=_turn_meta)' in classic
    assert classic.count('add_message("assistant"') == 1


def test_every_engine_failure_exit_goes_through_the_one_closure():
    """Every exit that ends a turn on the engine's text returns through
    `_end_turn_without_answer`, which alone sets `_turn_failed`, and none of them appends
    that text to the history. MUTATION: return one exit's text directly - red."""
    src = (ROOT / "vaf" / "core" / "agent.py").read_text(encoding="utf-8")
    for exit_text in ("_end_turn_without_answer(_round_failure, streamed=bool(stream_callback))",
                      "_end_turn_without_answer(err_msg, streamed=bool(stream_callback))",
                      '_end_turn_without_answer("[Error] Server rejected the request',
                      '_end_turn_without_answer("[Error] Server unavailable after retries.',
                      "_end_turn_without_answer(_sp_msg, streamed=False)",
                      "_end_turn_without_answer(_wc_msg, streamed=False)",
                      "_end_turn_without_answer(_kill_msg, streamed=False)"):
        assert f"return {exit_text}" in src, exit_text
    assert src.count("return _end_turn_without_answer(err_msg, streamed=bool(stream_callback))") == 2
    # The local server's statuses are returned, never streamed.
    assert src.count('", streamed=False)') == 2
    assert src.count("self._turn_failed = True") == 1
    for bare in ("return err_msg", "return _sp_msg", "return _wc_msg", "return _kill_msg",
                 'return "[Error] Server'):
        assert bare not in src, bare
    for stop in ("_sp_msg", "_wc_msg", "_kill_msg"):
        assert f'"content": {stop}}}' not in src, stop


def test_a_failure_chunk_is_a_chunk_that_begins_with_the_sentinel():
    from vaf.core.completion import is_failure_chunk
    assert is_failure_chunk(_KEY_ERROR)
    assert not is_failure_chunk("Die Meldung war " + _KEY_ERROR)
    assert not is_failure_chunk(None) and not is_failure_chunk({"content": _KEY_ERROR})


# ── the Telegram and Discord message index, which contact_history reads ──────────────

_SCOPE = "11111111-2222-3333-4444-555555555555"
_TG = "7000000101"


@pytest.fixture
def index_world(monkeypatch, tmp_path):
    """A Telegram contact chat on disk with a failed turn and a turn that answered before a
    failure, the contact pinned the way the runner pins it. Isolated: tmp data dir and a
    config of its own."""
    import vaf.core.config as cfg_mod
    from types import SimpleNamespace

    from vaf.core import channel_message_store as cms
    from vaf.core import contacts_store
    from vaf.core.platform import Platform
    from vaf.core.session import SessionManager, failed_turn_meta
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    state = {"local_admin_scope_id": _SCOPE, "local_admin_username": "alice"}
    monkeypatch.setattr(cfg_mod.Config, "get",
                        classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr(cfg_mod.Config, "load", classmethod(lambda cls: dict(state)))
    cms._reset_announce_state()
    bob = contacts_store.create_contact("Bob", "alice", user_scope_id=_SCOPE, telegram_user_id=_TG,
                                        allow_as_assistant_user=True)
    sm = SessionManager()
    s = sm.new(model="m", project_path=str(tmp_path))
    s.id = f"telegram_{_TG}"
    s.metadata = dict(s.metadata or {}, username="alice", user_scope_id=_SCOPE)
    s.add_message("user", "Können Sie die Bestellung prüfen?", metadata={"channel_message_id": "42"})
    s.add_message("assistant", _KEY_ERROR, metadata=failed_turn_meta(True))
    s.add_message("user", "Und mein Termin?")
    s.add_message("assistant", "Ihr Termin ist am Montag.\n\n" + _PATH_ERROR,
                  metadata=failed_turn_meta(False, _PATH_ERROR))
    sm.save(s)
    return SimpleNamespace(_front_office_contact=bob, _front_office_mode=True)


def _contact_history(agent) -> str:
    from vaf.tools.contact_history import ContactHistoryTool
    return ContactHistoryTool().run(_agent=agent, username="alice", user_scope_id=_SCOPE)


def test_the_owners_inbox_refresh_does_not_hand_a_contact_the_failure(index_world):
    """The owner's `inbox` and `find_telegram_messages` rebuild the index from the stored
    chats, and `contact_history` on the contact's next turn reads it. MUTATION: project
    every stored assistant message in `_session_rows` (no `model_text`) - red."""
    from vaf.core.telegram_history import sync_telegram_history
    assert sync_telegram_history() > 0
    out = _contact_history(index_world)
    assert "Ihr Termin ist am Montag." in out and "Können Sie die Bestellung" in out
    assert "sk-proj-****abcd" not in out and _PATH_ERROR not in out


def test_a_contacts_own_edit_does_not_hand_them_the_failure(index_world):
    """No owner action needed: the contact edits an earlier Telegram message, the bridge
    patches the stored chat and re-syncs that chat's index (`edit_channel_message`).
    MUTATION: as above - red."""
    from vaf.core.channel_history import edit_channel_message
    assert edit_channel_message("telegram", _TG, "Können Sie meine Bestellung prüfen?",
                                message_id="42")
    out = _contact_history(index_world)
    assert "meine Bestellung" in out and "Ihr Termin ist am Montag." in out
    assert "sk-proj-****abcd" not in out and _PATH_ERROR not in out


def test_the_read_tools_see_what_a_model_may_read(index_world):
    """`read_telegram_chat` and `read_discord_chat` read the stored chat through the same
    projection."""
    from vaf.core.channel_history import read_channel_session
    bodies = [r["body"] for r in read_channel_session("telegram", _TG)]
    assert "Ihr Termin ist am Montag." in bodies
    assert not any("sk-proj" in b or _PATH_ERROR in b for b in bodies)


# ── the interactive CLI and the TUI store their copy through a markup cleanup ─────────

_TOOL_BRACKET_FAILURE = ("[API Error from openai: Error code: 400 - {'error': {'message': \"Invalid "
                         "'messages[2].content': Incorrect API key provided: sk-proj-****abcd\"}}]")


class _AnsweredThenFailed:
    """What chat_step leaves for a host after an answer and a failing later round. `before`
    is text the failing round streamed ahead of its failure, on the same line."""

    def __init__(self, failure, before=""):
        self._turn_failed = False
        self._turn_failure = failure
        self._before = before

    def chat_step(self, user_input, stream_callback=None, **kwargs):
        for piece in ("Ihr Termin ist am Montag um 10 Uhr.", "\n", self._before,
                      self._turn_failure):
            if piece:
                stream_callback(piece)
        return "Ihr Termin ist am Montag um 10 Uhr."

    def get_token_usage(self):
        return 10, 100


@pytest.mark.parametrize("failure, leftover, before, shown", [
    ("[Error] API backend failure: " + _PATH_ERROR, _PATH_ERROR, "", True),
    (_TOOL_BRACKET_FAILURE, "sk-proj-****abcd", "", True),
    # The cleanup's [...dim...] pattern matches lazily across brackets: inside the whole
    # stream it starts in the text before the failure and ends inside it, which the failure
    # converted on its own never does. Converted apart, the failure becomes a bare <think>.
    ("[Error] API backend failure: QQSECRET42DIM : x[2][2]", "QQSECRET42", "x[dim ", False),
], ids=["engine-error", "sentinel-with-inner-bracket", "dim-across-the-boundary"])
def test_the_cli_copy_of_an_answered_turn_reads_back_without_the_failure(
        engine, monkeypatch, tmp_path, failure, leftover, before, shown):
    """`_process_agent_message` (the interactive CLI and the TUI) stores the stream through its
    markup cleanup, which deletes every [...] group: "[Error]" and the head of the sentinel
    vanish, the tail stays, with no signature left. The REAL function stores the copy, and the
    recorded failure is the cleaned one, so a reader finds it and takes it off. A failure it
    cannot find drops the message (`test_model_text` pins that safe side).
    MUTATION: record the raw failure - not found, so the message is dropped and the answer
    lost, red. MUTATION: convert the whole stream and record the failure converted alone -
    the dim case leaves the secret in what a model reads, red."""
    from contextlib import nullcontext
    from types import SimpleNamespace

    import vaf.cli.cmd.run as run_mod
    import vaf.core.speech as speech_mod
    from vaf.core.session import SessionManager, model_text
    web = SimpleNamespace(update_status=lambda *a, **k: None, log=lambda *a, **k: None,
                          emit_agent_message=lambda *a, **k: None,
                          emit_message_complete=lambda *a, **k: None,
                          emit_stats=lambda *a, **k: None,
                          push_update_to_user=lambda *a, **k: None)
    monkeypatch.setattr(run_mod, "get_web_interface", lambda: web)
    monkeypatch.setattr(run_mod, "get_dated_log_path", lambda name, ext: tmp_path / f"{name}.{ext}")
    monkeypatch.setattr(speech_mod, "get_speech_manager", lambda: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(run_mod, "_check_subagent_results", lambda tui, agent: [])
    tui = SimpleNamespace(console=SimpleNamespace(print=lambda *a, **k: None),
                          spinner=lambda *a, **k: nullcontext(), newline=lambda: None,
                          error=lambda *a, **k: None, primary="cyan")
    sm = SessionManager()
    session = sm.new(name="cli chat")
    session.add_message("user", "Wann ist mein Termin?")

    run_mod._process_agent_message(_AnsweredThenFailed(failure, before),
                                   "Wann ist mein Termin?", tui, session)
    stored = session.messages[-1]
    assert stored.role == "assistant"
    assert (leftover in stored.content) is shown    # the transcript shows what the cleanup kept
    readable = model_text(stored) or ""
    assert readable.startswith("Ihr Termin ist am Montag um 10 Uhr.") and leftover not in readable

    sm.save(session)
    engine.reload(session.id)
    replayed = [str(m.get("content")) for m in engine.history if m.get("role") == "assistant"]
    assert len(replayed) == 1 and replayed[0].startswith("Ihr Termin ist am Montag um 10 Uhr.")
    assert leftover not in replayed[0]
