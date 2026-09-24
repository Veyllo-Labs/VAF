# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A held mail draft takes new words before it is sent, and a chat can see what became of it.

The card in the conversation lets the person click into the draft and change it. For a mail
that is not a new message: the draft is already built (RFC822 bytes, Message-ID, ledger row),
and what the person approves has to stay byte for byte what leaves. So the text and the
subject are edited INSIDE the stored bytes, and everything else - every recipient, the Bcc,
the Message-ID, every attachment - is carried over untouched.

MUTATION: rebuild the message in `MailService.revise_draft` with `compose.build_message`
(new Message-ID, attachments lost) and the byte test goes red; update only the payload fields
and not `raw_b64` and the "what leaves" assertion goes red; let a released op be revised and
the state test goes red.
"""
import base64
import os
from email import policy
from email.parser import BytesParser

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.mail import case_token
from vaf.mail.service import MailService
from vaf.mail.store import MailStore

_SCOPE = "12345678-1234-1234-1234-123456789abc"
_PDF = b"%PDF-1.4 unit test body\x00\xff" * 40


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    monkeypatch.setattr(case_token, "_root_secret", lambda: "unit-test-root-secret-of-thirty-two-bytes")
    yield
    mail_crypto._cached_key = old


@pytest.fixture()
def svc(tmp_path):
    store = MailStore(_SCOPE, base_dir=tmp_path)
    s = MailService.__new__(MailService)
    s.user_scope_id = _SCOPE
    s.store = store
    store.upsert_account("bob@example.com", "imap", "bob@example.com")
    yield s
    store.close()


def _hold(svc, body="Hallo Anna,\nerste Fassung.", session="chat-a", to="Anna Berg <anna@example.com>"):
    return svc.queue_send("bob@example.com", to, "Angebot", body, cc="cc@example.com",
                          bcc="bcc@example.com", undo_seconds=0, sent_by="agent", hold=True,
                          attachments=[{"filename": "offer.pdf", "content_type": "application/pdf",
                                        "payload": _PDF}],
                          attachment_meta=[{"filename": "offer.pdf"}], chat_session_id=session)


def _parsed(svc, op_id):
    raw = base64.b64decode(svc.store.get_op(op_id)["payload"]["raw_b64"])
    return BytesParser(policy=policy.default).parsebytes(raw)


def test_a_held_mail_is_edited_inside_its_own_bytes(svc):
    queued = _hold(svc)
    op_id = queued["op_id"]
    before = _parsed(svc, op_id)

    assert svc.revise_draft(op_id, subject="Neues Angebot", body="Hallo Anna,\nzweite Fassung mit ä ö ü.")

    after = _parsed(svc, op_id)
    # What leaves is the new text and subject ...
    assert after["Subject"] == "Neues Angebot"
    assert after.get_body(preferencelist=("plain",)).get_content().startswith("Hallo Anna,\nzweite Fassung mit ä ö ü.")
    # ... and everything the person approved besides the words is exactly what it was.
    for header in ("From", "To", "Cc", "Bcc", "Message-ID", "Date"):
        assert after[header] == before[header], header
    assert after["Message-ID"] == queued["message_id"]
    atts = list(after.iter_attachments())
    assert len(atts) == 1 and atts[0].get_filename() == "offer.pdf" and atts[0].get_content() == _PDF
    # The payload fields an API sender builds from say the same, and the draft is marked.
    payload = svc.store.get_op(op_id)["payload"]
    assert (payload["subject"], payload["edited"]) == ("Neues Angebot", True)
    assert payload["body"].startswith("Hallo Anna,\nzweite")
    assert svc.store.get_op(op_id)["state"] == "held", "an edit sends nothing"

    # Only the text: the subject stays.
    assert svc.revise_draft(op_id, body="Dritte Fassung")
    assert _parsed(svc, op_id)["Subject"] == "Neues Angebot"


def test_only_a_waiting_draft_takes_new_words(svc):
    released = _hold(svc)["op_id"]
    assert svc.approve_draft(released)
    assert svc.revise_draft(released, body="zu spät") is False, "a released mail is on its way"
    dropped = _hold(svc)["op_id"]
    assert svc.discard_draft(dropped)
    assert svc.revise_draft(dropped, body="zu spät") is False
    # One whose last attempt may already have arrived can only be dropped.
    ambiguous = _hold(svc)["op_id"]
    op = svc.store.get_op(ambiguous)
    svc.store.mark_sent_delivery(int(op["account_id"]), op["payload"]["message_id"], "ambiguous")
    assert svc.revise_draft(ambiguous, body="nein") is False


def test_a_chat_sees_every_mail_it_asked_for_in_the_cards_words(svc):
    waiting = _hold(svc)["op_id"]
    sent = _hold(svc)["op_id"]
    svc.approve_draft(sent)
    dropped = _hold(svc)["op_id"]
    svc.discard_draft(dropped)
    replaced = _hold(svc)["op_id"]
    assert svc.discard_draft(replaced, replaced_by="mail:99")
    _hold(svc, session="chat-b")
    _hold(svc, session="")   # a Front Office answer belongs to no chat

    rows = {r["op_id"]: r for r in svc.list_chat_drafts("chat-a")}
    assert set(rows) == {waiting, sent, dropped, replaced}, "only this chat's, in every state"
    assert rows[waiting]["state"] == "held"
    assert rows[sent]["state"] == "sent"
    assert rows[dropped]["state"] == "discarded"
    assert (rows[replaced]["state"], rows[replaced]["replaced_by"]) == ("replaced", "mail:99")
    assert rows[waiting]["bcc"] == "bcc@example.com" and rows[waiting]["attachments"] == ["offer.pdf"]
    # A replaced draft is still a discard for everybody who reads the op state.
    assert svc.store.get_op(replaced)["state"] == "discarded"
    assert svc.list_chat_drafts("") == []
    assert svc.get_chat_draft(sent)["state"] == "sent"
    assert svc.get_chat_draft(123456) is None
