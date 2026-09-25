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

Two fixes, both measured here on a real store. The store resolves what a caller CALLS a folder,
per account: the account's own folder of that name first, then a role word ("sent") or any
provider's well-known name for a special folder, then a name in another case
(`MailStore.folder_matches`, one place for the search, the thread list and the message list).
And a folder that means nothing here is answered as that, naming the folders the mailbox has
(`tool_bridge.unknown_folder`), never as "no match" - counting the folders of an account only
the legacy lane holds, which the search read too.

MUTATION: drop folder_matches' role branch and the alias tests go red; resolve across all
accounts at once again and the two-account test goes red; drop the `unknown_folder` call in
find_mail and the tool test answers "No emails matching" again; ignore the legacy folders and
the legacy test calls a real folder missing.
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


def _meaning(store, folder, account_id=None):
    return [(column, value) for _apk, column, value in store.folder_matches(folder, account_id)]


def test_a_real_name_means_exactly_itself(store):
    assert _meaning(store, SENT) == [("name", SENT)]
    assert _meaning(store, "INBOX") == [("name", "INBOX")]
    assert _meaning(store, "rechnungen") == [("name", "Rechnungen")]
    assert store.search("Testmail", folder="INBOX") == []


def test_a_folder_that_is_not_here_matches_nothing(store):
    assert store.folder_matches("[Gmail]/Nope") == []
    assert store.folder_matches("archive") == []     # a role no folder here plays
    assert store.search("Testmail", folder="[Gmail]/Nope") == []


def test_gmail_all_mail_is_all_mail_not_an_archive(store):
    apk = store.upsert_account("alice@example.com", "imap", "alice@example.com")
    store.upsert_folder(apk, "[Google Mail]/Alle Nachrichten", special_use="\\All")
    assert _meaning(store, "[Gmail]/All Mail") == [("special_use", "\\All")]


def test_each_account_means_its_own_folder(store):
    """A second account with a folder literally named "Sent": the word still means the first
    account's "[Google Mail]/Gesendet", and a search narrowed to that account finds it."""
    bpk = store.upsert_account("work@example.org", "imap", "work@example.org")
    bsent = store.upsert_folder(bpk, "Sent", special_use="\\Sent")
    store.ingest_message(bpk, bsent, 1, ParsedMessage(
        message_id="<work@example.org>", subject="Testmail Arbeit", from_addr="work@example.org",
        to_addrs="bob@example.com", date_ts=1_790_000_200, refs=[], body_text="Arbeit"))
    assert sorted(h["subject"] for h in store.search("Testmail", folder="Sent")) == \
        ["Testmail", "Testmail Arbeit"]
    assert [h["subject"] for h in store.search(
        "Testmail", folder="Sent", account_id="alice@example.com")] == ["Testmail"]
    assert [t["subject"] for t in store.list_threads(
        account_id="alice@example.com", folder="Sent")] == ["Testmail"]


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


def test_a_folder_only_the_legacy_lane_holds_is_not_missing(store, monkeypatch):
    """An account the engine does not sync is searched in the legacy store, so its folders
    are the mailbox's too."""
    from vaf.core.email_sync_store import upsert_messages
    from vaf.core.platform import Platform
    from vaf.mail.tool_bridge import unknown_folder
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: store.db_path.parents[2]))
    upsert_messages("old@example.net", "Projekt", [{
        "subject": "Alt", "from": "x@example.net", "date": "Mon, 1 Sep 2026 10:00:00 +0000",
        "message_id": "<old@example.net>", "body_snippet": "alt"}],
        username="alice", user_scope_id=SCOPE)
    assert unknown_folder("Projekt", SCOPE, legacy_username="alice") == ""
    missing = unknown_folder("Nirgends", SCOPE, legacy_username="alice")
    assert "Projekt" in missing, "the listing names the legacy folder too"


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
