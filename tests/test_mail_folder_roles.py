# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A folder is found by the part it plays, and a folder that is not there says so.

Live incident. The person sent a drafted mail from the chat, and the agent tried to confirm it
in the mailbox: `find_mail(folder="[Gmail]/Sent Mail")`. The account is a German Gmail, whose
sent mail sits in "[Google Mail]/Gesendet" (328 messages synced, the mail among them). The
store compared folder NAMES only, so the search matched no folder and the tool answered "No
emails matching ... in [Gmail]/Sent Mail". The agent read that as the mailbox's word about the
mail and told the person that a mail sent the evening before had not gone out.

Two fixes, both measured here on a real store. The store resolves what a caller CALLS a folder:
a real name first, then a role word ("sent") or any provider's well-known name for a special
folder, then a name in another case (`MailStore.folder_filter`, one place for the search, the
thread list and the message list). And a folder that means nothing here is answered as that,
naming the folders the mailbox has (`tool_bridge.unknown_folder`), never as "no match".

MUTATION: return None from folder_filter's role branch and the alias tests go red; drop the
`unknown_folder` call in find_mail and the tool test answers "No emails matching" again.
"""
import os

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.mail.parser import ParsedMessage
from vaf.mail.store import MailStore

SCOPE = "12345678-1234-1234-1234-123456789abc"
SENT = "[Google Mail]/Gesendet"


@pytest.fixture(autouse=True)
def _pinned_crypto_key():
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


@pytest.fixture()
def store(tmp_path):
    s = MailStore(SCOPE, base_dir=tmp_path)
    apk = s.upsert_account("alice@example.com", "imap", "alice@example.com")
    inbox = s.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    sent = s.upsert_folder(apk, SENT, special_use="\\Sent", sync_tier="headers")
    s.upsert_folder(apk, "Rechnungen")
    s.ingest_message(apk, inbox, 1, ParsedMessage(
        message_id="<in@example.com>", subject="Newsletter", from_addr="news@example.com",
        to_addrs="alice@example.com", date_ts=1_790_000_000, refs=[], body_text="news"))
    s.ingest_message(apk, sent, 1, ParsedMessage(
        message_id="<out@example.com>", subject="Testmail", from_addr="alice@example.com",
        to_addrs="bob@example.com", date_ts=1_790_000_100, refs=[], body_text="nur ein Test"))
    yield s
    s.close()


@pytest.mark.parametrize("asked", [SENT, "sent", "Sent", "[Gmail]/Sent Mail", "Gesendet"])
def test_the_sent_folder_is_found_whatever_the_caller_calls_it(store, asked):
    hits = store.search("Testmail", folder=asked)
    assert [h["subject"] for h in hits] == ["Testmail"], asked


def test_a_real_name_means_exactly_itself(store):
    assert store.folder_filter(SENT) == ("name", SENT)
    assert store.folder_filter("INBOX") == ("name", "INBOX")
    assert store.folder_filter("rechnungen") == ("name", "Rechnungen")
    assert store.search("Testmail", folder="INBOX") == []


def test_a_folder_that_is_not_here_matches_nothing(store):
    assert store.folder_filter("[Gmail]/Nope") is None
    assert store.folder_filter("archive") is None     # a role no folder here plays
    assert store.search("Testmail", folder="[Gmail]/Nope") == []


def test_the_thread_list_and_the_message_list_read_the_same_answer(store):
    assert [t["subject"] for t in store.list_threads(folder="sent")] == ["Testmail"]
    assert [m["subject"] for m in store.list_messages(folder="[Gmail]/Sent Mail")] == ["Testmail"]


def test_a_missing_folder_is_answered_with_the_folders_there_are(store, monkeypatch):
    from vaf.core.platform import Platform
    from vaf.mail.tool_bridge import unknown_folder
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: store.db_path.parents[2]))
    note = unknown_folder("[Gmail]/Nope", SCOPE)
    assert note.startswith("Tool Error: this mailbox has no folder '[Gmail]/Nope'")
    assert f"{SENT} (sent)" in note and "INBOX (inbox)" in note and "Rechnungen" in note
    assert note.index(SENT) < note.index("Rechnungen"), "the special folders come first"
    assert unknown_folder("sent", SCOPE) == ""
    assert unknown_folder("", SCOPE) == ""


def test_find_mail_says_the_folder_is_missing_instead_of_no_match(store, monkeypatch):
    from vaf.core.context import tool_result_is_error
    from vaf.core.platform import Platform
    from vaf.tools.find_mail import FindMailTool
    import vaf.mail.tool_bridge as bridge
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: store.db_path.parents[2]))
    monkeypatch.setattr(bridge, "search_messages_merged", lambda *a, **k: [])
    out = FindMailTool().run(query="Testmail", folder="[Gmail]/Nope",
                             user_scope_id=SCOPE, username="alice")
    assert out.startswith("Tool Error: this mailbox has no folder"), out
    assert tool_result_is_error(out)
    # Not the "do not retry" shape: the agent is meant to ask again with a real folder.
    assert not out.lower().startswith("error")
