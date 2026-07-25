# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Sync-engine tests against an in-memory fake IMAP client (duck-typed subset
of the IMAPClient API). Covers the RFC 4549 state machine: initial + idempotent
resync, incremental new mail, flag writeback detection, expunge detection,
UIDVALIDITY reset, per-message error boundary, Gmail X-GM extension capture,
and the fail-safe on a failed flag window."""
import os
from datetime import datetime, timezone

import pytest

import vaf.mail.crypto as mail_crypto
from vaf.mail.store import MailStore
from vaf.mail.sync import ImapSyncEngine

_SCOPE = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture(autouse=True)
def _pinned_crypto_key():
    old = mail_crypto._cached_key
    mail_crypto._cached_key = os.urandom(32)
    yield
    mail_crypto._cached_key = old


def _raw(mid: str, subject: str, refs: str = "") -> bytes:
    r = f"Message-ID: <{mid}>\r\nSubject: {subject}\r\nFrom: a@example.com\r\n"
    if refs:
        r += f"References: {refs}\r\n"
    r += "Date: Mon, 01 Jul 2026 10:00:00 +0000\r\n\r\nbody of " + subject
    return r.encode()


class FakeImap:
    def __init__(self, caps=("IMAP4REV1",)):
        self._caps = [c.encode() for c in caps]
        self.folders = {"INBOX": {"uidvalidity": 100, "messages": {}}}
        self.fail_flag_windows = False

    def add(self, folder, uid, raw, flags=("\\Seen",), gm=None, category=None):
        self.folders.setdefault(folder, {"uidvalidity": 100, "messages": {}})
        self.folders[folder]["messages"][uid] = {"raw": raw, "flags": list(flags),
                                                 "gm": gm or {}, "category": category}

    def capabilities(self):
        return self._caps

    def list_folders(self):
        return [((b"\\HasNoChildren",), b"/", name) for name in self.folders]

    def select_folder(self, name, readonly=True):
        assert readonly is True, "phase 1 must select readonly"
        f = self.folders[name]
        uids = f["messages"].keys()
        self._selected = name
        return {b"UIDVALIDITY": f["uidvalidity"], b"UIDNEXT": (max(uids) + 1) if uids else 1}

    def search(self, criteria):
        uids = sorted(self.folders[self._selected]["messages"].keys())
        msgs = self.folders[self._selected]["messages"]
        crit = list(criteria) if isinstance(criteria, (list, tuple)) else [criteria]
        # Gmail tab lookup: SEARCH UID lo:hi X-GM-RAW "category:<tab>". Gmail
        # answers from its own search index - the tab is NOT in X-GM-LABELS - so
        # the fake models it with a per-message 'category' attribute.
        if "X-GM-RAW" in crit:
            raw = str(crit[crit.index("X-GM-RAW") + 1])
            want = raw.split("category:")[-1].strip() if "category:" in raw else None
            hits = [u for u in uids if want and msgs[u].get("category") == want]
            if "UID" in crit:
                lo, _, hi = str(crit[crit.index("UID") + 1]).partition(":")
                hi_i = max(uids) if hi == "*" else int(hi)
                hits = [u for u in hits if int(lo) <= u <= hi_i]
            return hits
        if crit and crit[0] == "UID":
            start = int(str(crit[1]).split(":")[0])
            hits = [u for u in uids if u >= start]
            return hits or (uids[-1:] if uids else [])  # IMAP m:* quirk
        return uids

    def fetch(self, uids, items, modifiers=None):
        if self.fail_flag_windows and items == ["FLAGS"]:
            raise OSError("connection dropped")
        msgs = self.folders[self._selected]["messages"]
        if isinstance(uids, str):
            start, _, end = uids.partition(":")
            wanted = [u for u in msgs if int(start) <= u <= int(end)]
        else:
            wanted = [u for u in uids if u in msgs]
        out = {}
        for u in wanted:
            m = msgs[u]
            d = {b"FLAGS": [f.encode() for f in m["flags"]],
                 b"INTERNALDATE": datetime(2026, 7, 1, tzinfo=timezone.utc),
                 b"RFC822.SIZE": len(m["raw"])}
            if "BODY.PEEK[]" in items:
                d[b"BODY[]"] = m["raw"]
            if "BODY.PEEK[HEADER]" in items:
                d[b"BODY[HEADER]"] = m["raw"].split(b"\r\n\r\n")[0] + b"\r\n\r\n"
            for k, v in m["gm"].items():
                d[k.encode()] = v
            out[u] = d
        return out

    def logout(self):
        pass


@pytest.fixture()
def store(tmp_path):
    s = MailStore(_SCOPE, base_dir=tmp_path)
    yield s
    s.close()


def _engine(store, fake):
    return ImapSyncEngine(store, "bob@example.com", "imap", "bob@example.com", fake)


def test_initial_sync_and_idempotent_resync(store):
    fake = FakeImap()
    for uid in (1, 2, 3):
        fake.add("INBOX", uid, _raw(f"m{uid}@x", f"Mail {uid}"))
    eng = _engine(store, fake)
    stats = eng.sync_folder("INBOX")
    assert stats["new"] == 3 and stats["errors"] == 0
    # resync: nothing new, nothing duplicated
    stats2 = eng.sync_folder("INBOX")
    assert stats2["new"] == 0 and stats2["vanished"] == 0
    assert len(store.list_messages()) == 3
    assert store.get_folder(eng.account_pk, "INBOX")["last_seen_uid"] == 3


def test_incremental_new_mail(store):
    fake = FakeImap()
    fake.add("INBOX", 1, _raw("m1@x", "First"))
    eng = _engine(store, fake)
    eng.sync_folder("INBOX")
    fake.add("INBOX", 2, _raw("m2@x", "Second"))
    stats = eng.sync_folder("INBOX")
    assert stats["new"] == 1
    subjects = {m["subject"] for m in store.list_messages()}
    assert subjects == {"First", "Second"}


def test_flag_resync_and_expunge(store):
    fake = FakeImap()
    fake.add("INBOX", 1, _raw("m1@x", "One"), flags=())
    fake.add("INBOX", 2, _raw("m2@x", "Two"), flags=())
    eng = _engine(store, fake)
    eng.sync_folder("INBOX")
    fake.folders["INBOX"]["messages"][1]["flags"] = ["\\Seen", "\\Flagged"]
    del fake.folders["INBOX"]["messages"][2]
    stats = eng.sync_folder("INBOX")
    assert stats["flag_updates"] == 1 and stats["vanished"] == 1
    msgs = store.list_messages()
    assert len(msgs) == 1 and set(msgs[0]["flags"]) == {"\\Seen", "\\Flagged"}


def test_uidvalidity_reset_refetches(store):
    fake = FakeImap()
    fake.add("INBOX", 1, _raw("m1@x", "Old world"))
    eng = _engine(store, fake)
    eng.sync_folder("INBOX")
    # server rebuilds the mailbox: new UIDVALIDITY, same message under new uid
    fake.folders["INBOX"] = {"uidvalidity": 999, "messages": {}}
    fake.add("INBOX", 7, _raw("m1@x", "Old world"))
    stats = eng.sync_folder("INBOX")
    assert stats["reset"] == 1 and stats["new"] == 1
    msgs = store.list_messages()
    assert len(msgs) == 1 and msgs[0]["uid"] == 7


def test_broken_message_does_not_abort_sync(store, monkeypatch):
    fake = FakeImap()
    fake.add("INBOX", 1, _raw("ok1@x", "Fine"))
    fake.add("INBOX", 2, _raw("ok2@x", "Also fine"))
    eng = _engine(store, fake)
    real_ingest = store.ingest_message

    def _boom(account_pk, folder_pk, uid, *a, **k):
        if uid == 1:
            raise RuntimeError("synthetic ingest failure")
        return real_ingest(account_pk, folder_pk, uid, *a, **k)

    monkeypatch.setattr(store, "ingest_message", _boom)
    stats = eng.sync_folder("INBOX")
    assert stats["errors"] == 1 and stats["new"] == 1
    assert store.get_folder(eng.account_pk, "INBOX")["last_seen_uid"] == 2


def test_failed_flag_window_never_expunges(store):
    fake = FakeImap()
    fake.add("INBOX", 1, _raw("m1@x", "Keep me"))
    eng = _engine(store, fake)
    eng.sync_folder("INBOX")
    fake.fail_flag_windows = True
    stats = eng.sync_folder("INBOX")
    assert stats["vanished"] == 0
    assert len(store.list_messages()) == 1


def test_gmail_extensions_thread_and_category(store):
    """Tabs come from X-GM-RAW, not X-GM-LABELS: Gmail's categories are saved
    searches, so FETCH X-GM-LABELS carries no tab at all (the earlier label
    mapping stamped every message 'primary' against a real mailbox)."""
    fake = FakeImap(caps=("IMAP4REV1", "X-GM-EXT-1"))
    gm1 = {"X-GM-MSGID": 111, "X-GM-THRID": 900, "X-GM-LABELS": [b"\\\\Inbox"]}
    gm2 = {"X-GM-MSGID": 112, "X-GM-THRID": 900, "X-GM-LABELS": [b"\\\\Inbox"]}
    fake.add("INBOX", 1, _raw("g1@x", "Deal"), gm=gm1)
    fake.add("INBOX", 2, _raw("g2@x", "Unrelated subject"), gm=gm2, category="promotions")
    eng = _engine(store, fake)
    assert eng.is_gmail is True
    eng.sync_folder("INBOX")
    msgs = {m["uid"]: m for m in store.list_messages()}
    assert msgs[1]["thread_id"] == msgs[2]["thread_id"]  # X-GM-THRID join
    assert msgs[2]["category"] == "promotions"
    assert msgs[1]["category"] == "primary"


def test_gmail_category_search_failure_does_not_break_the_sync(store, monkeypatch):
    """A category is cosmetic; a lost message is not. A failing X-GM-RAW search
    must degrade to 'primary', never abort the folder."""
    fake = FakeImap(caps=("IMAP4REV1", "X-GM-EXT-1"))
    fake.add("INBOX", 1, _raw("g1@x", "Deal"), category="promotions")
    eng = _engine(store, fake)

    def _boom(criteria):
        if isinstance(criteria, (list, tuple)) and "X-GM-RAW" in criteria:
            raise OSError("SEARCH not supported here")
        return sorted(fake.folders["INBOX"]["messages"])

    monkeypatch.setattr(fake, "search", _boom)
    eng.sync_folder("INBOX")
    rows = store.list_messages()
    assert len(rows) == 1 and rows[0]["category"] == "primary"


def test_sender_rule_wins_over_the_gmail_tab_at_ingest(store, monkeypatch):
    """label_mail promises that future mail from a sender gets the same label.
    That only holds if the rule is applied at INGEST - the v2 engine did not do
    it, so a learned rule silently missed every new arrival."""
    import vaf.core.email_accounts as ea
    monkeypatch.setattr(ea, "get_sender_rules",
                        lambda u=None, user_scope_id=None: [
                            {"pattern": "a@example.com", "category": "social"}])
    fake = FakeImap(caps=("IMAP4REV1", "X-GM-EXT-1"))
    fake.add("INBOX", 1, _raw("g1@x", "Deal"), category="promotions")  # From: a@example.com
    eng = _engine(store, fake)
    eng.sync_folder("INBOX")
    assert store.list_messages()[0]["category"] == "social"  # rule beats the tab


def test_folder_discovery_special_use_and_localized_fallback(store):
    fake = FakeImap()
    fake.folders["Gesendete Objekte"] = {"uidvalidity": 5, "messages": {}}
    fake.folders["Papierkorb"] = {"uidvalidity": 6, "messages": {}}
    eng = _engine(store, fake)
    folders = {f["name"]: f for f in eng.discover_folders()}
    assert folders["INBOX"]["special_use"] == "\\Inbox"
    assert folders["Gesendete Objekte"]["special_use"] == "\\Sent"
    assert folders["Papierkorb"]["special_use"] == "\\Trash"
    assert folders["INBOX"]["tier"] == "eager"
    assert folders["Gesendete Objekte"]["tier"] == "headers"


def test_failed_fetch_batch_does_not_advance_watermark(store, monkeypatch):
    """Review finding (critical): a failed fetch batch must stop the watermark
    so its UIDs are retried next sync - a later successful batch must not
    advance last_seen past the gap (that silently lost up to 100 mails)."""
    import vaf.mail.sync as sync_mod
    monkeypatch.setattr(sync_mod, "NEW_FETCH_BATCH", 1)
    fake = FakeImap()
    for uid in (1, 2, 3):
        fake.add("INBOX", uid, _raw(f"w{uid}@x", f"Mail {uid}"))
    eng = _engine(store, fake)

    real_fetch = fake.fetch
    def _flaky(uids, items, modifiers=None):
        wanted = uids if isinstance(uids, list) else []
        if 2 in wanted and any("BODY" in i for i in items):
            raise OSError("transient network error")
        return real_fetch(uids, items, modifiers)
    fake.fetch = _flaky

    stats = eng.sync_folder("INBOX")
    assert stats["errors"] == 1 and stats["new"] == 1
    assert store.get_folder(eng.account_pk, "INBOX")["last_seen_uid"] == 1
    fake.fetch = real_fetch
    stats2 = eng.sync_folder("INBOX")
    assert stats2["new"] == 2  # uids 2 and 3 recovered
    assert store.get_folder(eng.account_pk, "INBOX")["last_seen_uid"] == 3
    assert {m["uid"] for m in store.list_messages()} == {1, 2, 3}
