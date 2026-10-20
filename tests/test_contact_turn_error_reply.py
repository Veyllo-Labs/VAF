# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A failed turn answers on the messenger it came from, and a contact never reads the error.

The queue runner's outer handler used to send "Sorry, something went wrong: <str(e)>" back on
Telegram, Discord and WhatsApp for every task, a Front Office contact's included. The
exception text is internal (file paths, store and session ids, a provider's raw answer) and a
contact is a third party. A contact's turn now gets a fixed sentence from the vocabulary book,
in the language of their record, while the owner's own chat keeps the error itself."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaf.core import headless_runner as hr

ROOT = Path(__file__).resolve().parents[1]

# What a real failure carries: a path, a store id, a provider answer. None of it may reach a contact.
_SECRET_BITS = ("/home/user/.vaf/users/alice/chats.db", "ab12cd34", "upstream 500")
_ERROR = RuntimeError(
    "database is locked: /home/user/.vaf/users/alice/chats.db (scope ab12cd34)\nupstream 500")

_CHANNEL_META = {
    "telegram": {"telegram_chat_id": "1001"},
    "discord": {"discord_channel_id": "2002"},
    "whatsapp": {"whatsapp_chat_jid": "4917000000000@s.whatsapp.net", "username": "alice",
                 "user_scope_id": "ab12cd34-0000-0000-0000-000000000000"},
}


@pytest.fixture
def sent(monkeypatch):
    """Every messenger send, stubbed: (channel, text)."""
    out = []
    import vaf.core.discord_reply as dr
    import vaf.core.telegram_reply as tr
    import vaf.core.whatsapp_reply as wr
    monkeypatch.setattr(tr, "send_telegram_reply", lambda chat_id, text, *a, **k: out.append(("telegram", text)))
    monkeypatch.setattr(dr, "send_discord_reply", lambda channel_id, text: out.append(("discord", text)))
    monkeypatch.setattr(wr, "send_whatsapp_reply",
                        lambda username, jid, text, *a, **k: out.append(("whatsapp", text)))
    return out


def _task(channel, *, from_contact):
    meta = dict(_CHANNEL_META[channel])
    if from_contact:
        meta["from_contact"] = True
    return SimpleNamespace(source=channel, metadata=meta, session_id="s1")


@pytest.mark.parametrize("channel", sorted(_CHANNEL_META))
def test_a_contact_gets_the_fixed_sentence_and_no_error_text(sent, channel):
    """MUTATION: drop the `from_contact` branch in `_turn_error_text` (every task gets the
    detailed error) and each of the three channels sends the path and the id - red."""
    hr._send_turn_error_reply(_task(channel, from_contact=True), _ERROR, contact=None)
    assert len(sent) == 1 and sent[0][0] == channel
    text = sent[0][1]
    assert text == "Sorry, I can't reply right now. Please try again a little later."
    for bit in _SECRET_BITS + ("something went wrong",):
        assert bit not in text


@pytest.mark.parametrize("channel", sorted(_CHANNEL_META))
def test_the_owner_still_reads_the_error(sent, channel):
    hr._send_turn_error_reply(_task(channel, from_contact=False), _ERROR, contact=None)
    assert sent == [(channel, "Sorry, something went wrong: " + str(_ERROR).replace("\n", " "))]


def test_the_sentence_follows_the_contacts_language():
    """The record's language decides; without one the sentence is English, not the owner's
    language. MUTATION: pass no language to `vocab.pick` and the German contact reads English."""
    meta = {"from_contact": True}
    de = hr._turn_error_text(meta, _ERROR, {"preferred_language": "de"})
    # The polite form: the reader may be a stranger. MUTATION: the du/infinitive wording - red.
    assert de == "Entschuldigung, ich kann gerade nicht antworten. Bitte versuchen Sie es später noch einmal."
    assert hr._turn_error_text(meta, _ERROR, {"preferred_language": "en-GB"}).startswith("Sorry, I can't")
    assert hr._turn_error_text(meta, _ERROR, {}).startswith("Sorry, I can't")
    assert hr._turn_error_text(meta, _ERROR, None).startswith("Sorry, I can't")


def test_every_ui_language_has_the_sentence():
    """A contact whose record names a UI language gets that language, never the English fallback."""
    import re

    from vaf.core import vocab
    codes = re.findall(r"code: '([a-z]+)'", (ROOT / "web" / "lib" / "languages.ts").read_text(encoding="utf-8"))
    assert codes, "web/lib/languages.ts changed shape; re-point this guard"
    have = set(vocab.available_languages("contact_turn_failed"))
    assert set(codes) <= have, sorted(set(codes) - have)


def test_the_runner_answers_a_failed_turn_through_the_one_helper():
    """The outer handler calls the helper with the contact pinned for THIS turn, and the
    detailed sentence exists once, inside the helper. MUTATION: put the old inline
    `err_text = f"Sorry, something went wrong: {err_msg}"` block back in the handler - red."""
    src = (ROOT / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    assert src.count("Sorry, something went wrong") == 1
    handler = src.split("except _StopGenerationRequested:", 1)[1].split("finally:", 1)[0]
    assert ('_send_turn_error_reply(\n                        task, e, '
            'contact=getattr(agent, "_front_office_contact", None))') in handler
    for send in ("send_telegram_reply(", "send_discord_reply(", "send_whatsapp_reply("):
        assert send not in handler, send


# ── a turn that hands its failure back as text ───────────────────────────────
#
# chat_step does not raise for a failing provider: it returns (and streams) "[Error] API
# backend failure: <e>", and the backend streams a provider's HTTP error as a content chunk,
# "[API Error from <provider>: <raw answer>]". The runner took either for an answer and sent
# it to the contact (measured with the contact simulator on WhatsApp and Telegram).

_PATH_ERROR = "database is locked: /home/user/.vaf/users/alice/chats.db"
_KEY_ERROR = ("[API Error from openai: Error code: 401 - Incorrect API key provided: "
              "sk-proj-****abcd]")


def _engine(monkeypatch, tmp_path, main_call):
    """The REAL chat_step, the provider faked: the router and side calls get a plain answer,
    the main call (the one carrying tools) runs `main_call`."""
    from vaf.core.agent import Agent
    from vaf.core.platform import Platform
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    a = Agent(register_signals=False, run_kind="chat",
              config_overrides={"provider": "deepseek", "api_key_deepseek": "sk-test"})
    a.current_session_id = "green123456"
    a._current_chat_source = "whatsapp"
    monkeypatch.setattr(a, "_try_workflow", lambda *args, **kw: None)

    def provider(messages=None, **kw):
        if not kw.get("tools"):
            yield "ok"
            return
        yield from main_call()
    a.api_backend.chat_completion = provider
    streamed = []
    returned = a.chat_step(user_input="Hallo", raw_user_input="Hallo", stream_callback=streamed.append)
    return "".join(streamed), returned


def test_the_engines_failure_texts_are_recognised(monkeypatch, tmp_path):
    """The predicate is pinned to what chat_step really produces, not to strings written here.
    MUTATION: drop `_ENGINE_ERROR in text` from `is_error_reply` - the raising provider is
    taken for an answer, red; drop `_ERROR_SENTINEL in text` - the streamed 401, red."""
    from vaf.core.completion import is_error_reply

    def raises():
        raise RuntimeError(_PATH_ERROR)
        yield  # a generator, like the real provider

    streamed, returned = _engine(monkeypatch, tmp_path, raises)
    assert _PATH_ERROR in streamed
    assert is_error_reply(streamed) and is_error_reply(returned)

    def sentinel():
        yield _KEY_ERROR

    streamed, _ = _engine(monkeypatch, tmp_path, sentinel)
    assert "sk-proj-****abcd" in streamed and is_error_reply(streamed)

    def answers():
        yield "Gern geschehen, bis morgen."

    streamed, _ = _engine(monkeypatch, tmp_path, answers)
    assert "Gern geschehen" in streamed and not is_error_reply(streamed)


@pytest.fixture
def delivered(monkeypatch, sent):
    """What the runner's delivery sends, the mail lane included: (channel, text)."""
    monkeypatch.setattr(hr, "_deliver_email_reply",
                        lambda task, meta, final_text: sent.append(("email", final_text)))
    return sent


_MAIL_META = {"email_account_id": "acc1", "email_message_pk": 7, "email_reply_mode": "send",
              "user_scope_id": "ab12cd34-0000-0000-0000-000000000000", "username": "alice"}

_FAILED_TURNS = (
    "[Error] API backend failure: " + _PATH_ERROR,
    # An earlier round streamed text, then the provider failed: the error follows it.
    "Einen Moment, ich schaue nach.\n" + _KEY_ERROR,
    # The runner's own text for a turn that produced nothing.
    "[Error] No response was produced. The server may have rejected the request (e.g. context "
    "too large). Try closing the Document Editor or starting a new chat.",
)


def _delivery_task(channel, *, from_contact):
    meta = dict(_MAIL_META) if channel == "email" else dict(_CHANNEL_META[channel])
    if from_contact:
        meta["from_contact"] = True
    return SimpleNamespace(source=channel, metadata=meta, session_id="s1")


@pytest.mark.parametrize("final_text", _FAILED_TURNS)
@pytest.mark.parametrize("channel", sorted(_CHANNEL_META) + ["email"])
def test_a_contact_is_never_sent_a_failed_turns_text(delivered, channel, final_text):
    """MUTATION: drop the `is_error_reply` branch in `_deliver_channel_reply` - every case
    sends the raw text (the mail lane queues it in send mode), red."""
    hr._deliver_channel_reply(_delivery_task(channel, from_contact=True), final_text,
                              contact={"preferred_language": "de"})
    if channel == "email":
        assert delivered == []           # nothing queued: the mail waits for the owner
        return
    assert delivered == [(channel, "Entschuldigung, ich kann gerade nicht antworten. "
                                   "Bitte versuchen Sie es später noch einmal.")]


def test_a_failed_turn_after_a_voice_note_is_not_read_out(delivered):
    """The bridge reads a reply aloud while a voice note is pending for that chat; the fixed
    sentence goes out as text. MUTATION: drop the pop in `_send_turn_error_reply` - red."""
    from vaf.api import whatsapp_bridge as wb
    key = "alice|" + _CHANNEL_META["whatsapp"]["whatsapp_chat_jid"]
    with wb._voice_reply_lock:
        wb._voice_reply_pending[key] = "de"
    try:
        hr._deliver_channel_reply(_delivery_task("whatsapp", from_contact=True), _FAILED_TURNS[1])
        assert key not in wb._voice_reply_pending
    finally:
        with wb._voice_reply_lock:
            wb._voice_reply_pending.pop(key, None)


@pytest.mark.parametrize("channel", sorted(_CHANNEL_META) + ["email"])
def test_the_owner_and_a_real_answer_go_out_as_they_are(delivered, channel):
    """The owner's own chat reads the failure in full; a contact's real answer is sent."""
    hr._deliver_channel_reply(_delivery_task(channel, from_contact=False), _FAILED_TURNS[0])
    hr._deliver_channel_reply(_delivery_task(channel, from_contact=True), "Bis morgen, Bob.")
    assert delivered == [(channel, _FAILED_TURNS[0]), (channel, "Bis morgen, Bob.")]


def test_the_runner_delivers_through_the_one_function():
    """The loop hands the turn to `_deliver_channel_reply` with the contact pinned for THIS
    turn, and carries no second copy of the delivery. MUTATION: put the old inline block
    back in the loop - red."""
    import inspect
    loop = inspect.getsource(hr.run_headless_agent)
    assert ('_deliver_channel_reply(\n                            task, final_text, '
            'session_mgr=session_mgr,\n                            '
            'contact=getattr(agent, "_front_office_contact", None),\n                            '
            'turn_failed=_turn_failed, turn_failure=_turn_failure)') in loop
    assert "_deliver_email_reply(" not in loop


# ── a stopped turn, the engine's own mark, the reasoning, the voice note ──────────────────

_FIXED_DE = "Entschuldigung, ich kann gerade nicht antworten. Bitte versuchen Sie es später noch einmal."
_BUDGET_STOP = ("⚠️ [LOOP_PROTECTION] Daily API budget reached (~$4.12 of $5.00 estimated). "
                "Raise spend_budget_usd_per_day or wait for the next day.")


@pytest.mark.parametrize("channel", sorted(_CHANNEL_META) + ["email"])
def test_a_contact_is_never_sent_a_loop_protection_stop(delivered, channel):
    """The budget stop names the owner's spend and a config key. MUTATION: drop
    `LOOP_PROTECTION in visible` from `_deliver_channel_reply` - red (no flag passed here)."""
    hr._deliver_channel_reply(_delivery_task(channel, from_contact=True), _BUDGET_STOP,
                              contact={"preferred_language": "de"})
    assert delivered == ([] if channel == "email" else [(channel, _FIXED_DE)])


@pytest.mark.parametrize("channel", sorted(_CHANNEL_META) + ["email"])
def test_a_turn_the_engine_marked_failed_is_not_sent_to_a_contact(delivered, channel):
    """A stop after a tool round leaves the round's own words as the turn's text ("One
    moment, I'll check."), with no marker in it; the engine's flag says the turn failed.
    MUTATION: drop `turn_failed or` - red. The owner's chat still gets the text."""
    hr._deliver_channel_reply(_delivery_task(channel, from_contact=True),
                              "Einen Moment, ich schaue nach.", turn_failed=True,
                              contact={"preferred_language": "de"})
    hr._deliver_channel_reply(_delivery_task(channel, from_contact=False),
                              "Einen Moment, ich schaue nach.", turn_failed=True)
    expected = [] if channel == "email" else [(channel, _FIXED_DE)]
    assert delivered == expected + [(channel, "Einen Moment, ich schaue nach.")]


def test_reasoning_that_quotes_an_error_does_not_swallow_the_answer(delivered):
    """The reasoning streams with the turn (the backend wraps it in <think>) and never
    leaves; a model that thinks about an "[Error] ..." tool result and then answers is
    answered. MUTATION: judge `final_text` instead of `visible` - red."""
    text = ("<think>contact_history gave [Error] API backend failure: x, so I answer from "
            "the record.</think>\n\nIhr Termin ist am Montag um 10 Uhr.")
    hr._deliver_channel_reply(_delivery_task("whatsapp", from_contact=True), text)
    assert delivered == [("whatsapp", "Ihr Termin ist am Montag um 10 Uhr.")]


def test_a_failure_inside_unclosed_reasoning_is_still_caught(delivered):
    """The provider failed mid-reasoning: the "</think>" never came, and the sentinel
    follows the open block. The outbound text keeps it, so the contact gets the sentence."""
    hr._deliver_channel_reply(_delivery_task("telegram", from_contact=True),
                              "<think>Ich prüfe den Kalender " + _KEY_ERROR,
                              contact={"preferred_language": "de"})
    assert delivered == [("telegram", _FIXED_DE)]


def test_a_failed_turn_after_a_telegram_voice_note_is_not_read_out(delivered):
    """Telegram's sender reads a reply aloud while a voice note is pending for that chat,
    like WhatsApp's. MUTATION: drop the telegram branch of `_drop_pending_voice_reply` - red."""
    from vaf.api import telegram_bridge as tb
    key = _CHANNEL_META["telegram"]["telegram_chat_id"]
    with tb._voice_reply_lock:
        tb._voice_reply_pending[key] = "de"
    try:
        hr._deliver_channel_reply(_delivery_task("telegram", from_contact=True), _FAILED_TURNS[1])
        assert key not in tb._voice_reply_pending
    finally:
        with tb._voice_reply_lock:
            tb._voice_reply_pending.pop(key, None)


def test_a_contact_gets_the_answer_a_later_failure_followed(delivered):
    """The turn answered, then a later round failed (chat_step's `_turn_failure`, the turn
    not failed): the contact gets the answer and never the failure. MUTATION: do not take
    `turn_failure` off the text - the sentinel is in it, so the contact gets the fixed
    sentence instead of the answer, red."""
    hr._deliver_channel_reply(_delivery_task("whatsapp", from_contact=True),
                              "Ihr Termin ist am Montag.\n\n" + _KEY_ERROR, turn_failure=_KEY_ERROR)
    assert delivered == [("whatsapp", "Ihr Termin ist am Montag.")]


def test_an_answer_that_explains_the_stop_marker_is_sent(delivered):
    """Only a text that IS a stop note is held back; an answer that mentions the marker
    further in is an answer. MUTATION: hold back any text containing the marker - red."""
    text = "Ein [LOOP_PROTECTION]-Hinweis heißt: die Runde wurde nach zu vielen Schritten beendet."
    hr._deliver_channel_reply(_delivery_task("telegram", from_contact=True), text)
    assert delivered == [("telegram", text)]
