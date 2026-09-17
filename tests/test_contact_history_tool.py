# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""contact_history (vaf/tools/contact_history.py, FRONT_OFFICE.md "Tool restriction"): the
one Front Office read tool, pinned by the runner to the contact being answered. It renders
that person's messages and mail across channels, both directions, hides what the phishing
filter hides and marks an unverified mail sender, filters by channel and word, never shows
the owner's notes, and refuses without a pin. Isolated: tmp data dir, pinned key.

MUTATION: drop `kinds=("message", "mail")` from the timeline call and the notes test goes
red; drop the phishing filter and the hidden-mail test goes red; drop the pin check and the
no-pin test goes red."""
import os
import time
from types import SimpleNamespace

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.core import channel_message_store as cms
from vaf.core import contacts_store
from vaf.core.platform import Platform
from vaf.mail.parser import parse_message
from vaf.mail.store import MailStore
from vaf.tools.contact_history import ContactHistoryTool

SCOPE = "11111111-2222-3333-4444-555555555555"
ACCOUNT = "alice@example.com"
NOW = time.time()


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    import vaf.core.config as cfg_mod
    state = {"local_admin_scope_id": SCOPE, "local_admin_username": "alice",
             "email_config_by_scope": {SCOPE: {"accounts": [{"account_id": ACCOUNT, "email": ACCOUNT, "provider": "imap", "enabled": True}]}}}
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr(cfg_mod.Config, "load", classmethod(lambda cls: dict(state)))
    cms._reset_announce_state()
    yield state
    mail_crypto._cached_key = old


def _raw(mid, subject, body, sender="Bob <bob@example.org>", auth="mx.google.com; dmarc=pass header.from=example.org"):
    head = f"Authentication-Results: {auth}\n" if auth else ""
    return (f"{head}From: {sender}\nTo: {ACCOUNT}\nSubject: {subject}\nDate: Mon, 20 Nov 2023 10:00:00 +0000\nMessage-ID: {mid}\n\n{body}\n").encode()


def _bob(world):
    bob = contacts_store.create_contact("Bob", "alice", user_scope_id=SCOPE, whatsapp_phone="+491700000042",
                                        email="bob@example.org", allow_as_assistant_user=True)
    contacts_store.add_contact_note(bob["id"], "Bob haggles, never give the first price", "alice", user_scope_id=SCOPE)
    cms.append_message("alice", "+491700000042", "hey, did you get my mail about the offer?", "in", chat_name="Bob",
                       ts=NOW - 60, user_scope_id=SCOPE, channel="whatsapp")
    cms.append_message("alice", "+491700000042", "let me check", "out", ts=NOW - 30, user_scope_id=SCOPE, channel="whatsapp")
    s = MailStore(SCOPE)
    apk = s.upsert_account(ACCOUNT, "imap", ACCOUNT)
    fpk = s.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    from vaf.mail.verification import auth_policy_for_account
    policy = auth_policy_for_account({"account_id": ACCOUNT, "email": ACCOUNT, "trusted_authserv_id": "mx.google.com"})
    s.ingest_message(apk, fpk, 1, parse_message(_raw("<offer@example.org>", "Angebot xyz", "Here is my offer for xyz, 4000 EUR.")),
                     raw=_raw("<offer@example.org>", "Angebot xyz", "Here is my offer for xyz, 4000 EUR."), auth_policy=policy)
    unverified = _raw("<spoof@example.org>", "Urgent: wire transfer", "Please wire 9000 EUR now, click here, unusual activity",
                      auth="mx.google.com; dmarc=fail header.from=example.org")
    s.ingest_message(apk, fpk, 2, parse_message(unverified), raw=unverified, auth_policy=policy)
    s.close()
    return bob


def _run(bob, **kw):
    agent = SimpleNamespace(_front_office_contact=bob, _front_office_mode=True)
    return ContactHistoryTool().run(_agent=agent, username="alice", user_scope_id=SCOPE, **kw)


def test_the_pinned_contacts_mail_and_chat_are_listed_newest_first_and_notes_stay_out(world):
    bob = _bob(world)
    out = _run(bob)
    assert out.startswith("Earlier correspondence with Bob")
    assert "[WhatsApp]" in out and "them: hey, did you get my mail about the offer?" in out and "us: let me check" in out
    assert '[Mail]' in out and 'subject "Angebot xyz"' in out and "4000 EUR" in out
    assert "haggles" not in out and "first price" not in out, "the owner's notes never reach a contact's turn"
    assert out.index("us: let me check") < out.index("them: hey"), "newest first"


def test_a_mail_the_phishing_filter_hides_is_left_out_and_the_channel_and_query_filters_work(world):
    bob = _bob(world)
    out = _run(bob)
    assert "wire transfer" not in out and "9000" not in out, "a failed authentication plus the risk words hides the mail"
    mail_only = _run(bob, channel="mail")
    assert "[WhatsApp]" not in mail_only and "Angebot xyz" in mail_only
    chat_only = _run(bob, channel="whatsapp")
    assert "[Mail]" not in chat_only and "hey, did you get" in chat_only
    assert "Angebot xyz" in _run(bob, query="offer") and "let me check" not in _run(bob, query="offer")
    assert _run(bob, query="zzz-nothing").startswith("No earlier messages or mails with Bob")


def test_an_unverified_but_harmless_mail_is_marked(world, monkeypatch):
    bob = _bob(world)
    harmless = _raw("<plain@example.org>", "Lunch", "Lunch on Friday?", auth="")
    s = MailStore(SCOPE)
    apk = s.account_pk(ACCOUNT)
    fpk = s.get_folder(apk, "INBOX")["id"]
    s.ingest_message(apk, fpk, 3, parse_message(harmless), raw=harmless, auth_policy={"trusted_authserv_id": "mx.google.com"})
    s.close()
    out = _run(bob, channel="mail")
    assert "(sender unknown)" in out and 'subject "Lunch"' in out


def test_a_mail_behind_a_long_chat_is_still_found(world):
    bob = _bob(world)
    for i in range(130):
        cms.append_message("alice", "+491700000042", f"ping {i}", "in", chat_name="Bob", ts=int(NOW) - 400 + i,
                           user_scope_id=SCOPE, channel="whatsapp")
    out = _run(bob, channel="mail", limit=5)
    assert 'subject "Angebot xyz"' in out, "the timeline is paged past the chat until the filter is satisfied"


def test_without_a_pin_the_tool_refuses(world):
    out = ContactHistoryTool().run(_agent=SimpleNamespace(_front_office_contact=None), username="alice", user_scope_id=SCOPE)
    assert "no contact pinned" in out
    assert "no contact pinned" in ContactHistoryTool().run(username="alice", user_scope_id=SCOPE)


def test_the_runner_pins_and_clears_the_contact_where_it_sets_the_mode():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "headless_runner.py").read_text(encoding="utf-8")
    assert src.count("agent._front_office_contact = None") == 3, "cleared in the else branch, at the pin site's default, and in finally"
    assert "agent._front_office_contact = dict(contact) if isinstance(contact, dict) else None" in src
    from vaf.core.front_office_tools import FRONT_OFFICE_ALLOWED_TOOLS
    assert "contact_history" in FRONT_OFFICE_ALLOWED_TOOLS
