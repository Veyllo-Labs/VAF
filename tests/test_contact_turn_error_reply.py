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
