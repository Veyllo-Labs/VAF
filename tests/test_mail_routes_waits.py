# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The mail window and the inbox agree about who waits (vaf/api/mail_routes.py, vaf/mail/supervisor.py):
thread rows carry waits / done / answered_by_agent from vaf.core.inbox.mail_thread_state and the
same done marks; a read-flag change and a sync that changed something announce inbox_changed.

MUTATION: drop _with_inbox_state and the first test goes red; drop the flags emit and the third
goes red; let sync_one return without notify_change and the last goes red.
"""
import asyncio
import time

import pytest

import vaf.api.mail_routes as mr
from vaf.core import channel_message_store as store
from vaf.core.platform import Platform
from vaf.mail.parser import ParsedMessage
from vaf.mail.store import MailStore

SCOPE = "11111111-2222-3333-4444-555555555555"
NOW = time.time() - 3600.0
USER = {"username": "alice", "user_scope_id": SCOPE, "role": "user"}


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: default))
    signals = []
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: signals.append(scope))
    store._reset_announce_state()   # cancels the timers an earlier test left behind; the dicts stay the module's own
    return signals


def _thread(sent_reply=False):
    s = MailStore(SCOPE)
    apk = s.upsert_account("alice@example.com", "imap", "alice@example.com")
    inbox_pk = s.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    root = s.ingest_message(apk, inbox_pk, 1, ParsedMessage(
        message_id="<q@example.com>", subject="Vertrag Q4", from_addr="Lena <lena@example.com>",
        to_addrs="alice@example.com", date_ts=int(NOW) - 600, refs=[], body_text="zwei Punkte offen"))
    if sent_reply:
        sent_pk = s.upsert_folder(apk, "Sent", special_use="\\Sent", sync_tier="eager")
        s.ingest_message(apk, sent_pk, 2, ParsedMessage(
            message_id="<a@example.com>", subject="Re: Vertrag Q4", from_addr="alice@example.com",
            to_addrs="lena@example.com", date_ts=int(NOW) - 300, refs=["<q@example.com>"], body_text="erledigt"))
    s.close()
    return root


def _threads():
    return asyncio.run(mr.list_threads(_user=USER))["threads"]


def test_thread_rows_carry_the_inbox_state_and_the_done_mark(world):
    _thread()
    t = _threads()[0]
    assert t["waits"] is True and t["waits_reason"] == "unanswered"
    assert t["done"] is False and t["answered_by_agent"] is False
    store.mark_done("alice", "mail", str(t["thread_id"]), user_scope_id=SCOPE, done=True)
    t2 = _threads()[0]
    assert t2["done"] is True and t2["waits"] is False


def test_a_sent_reply_closes_the_thread(world):
    _thread(sent_reply=True)
    t = _threads()[0]
    assert t["done"] is True and t["waits"] is False


def test_a_read_flag_change_announces_the_inbox_and_a_star_does_not(world):
    pk = _thread()
    out = asyncio.run(mr.patch_flags(pk, {"read": True}, _user=USER))
    assert out["ok"] is True and world == [SCOPE]
    asyncio.run(mr.patch_flags(pk, {"starred": True}, _user=USER))
    assert world == [SCOPE], "a star changes no conversation list"


def test_the_supervisor_tells_its_observers_when_a_sync_changed_something(monkeypatch):
    from vaf.mail import supervisor as sup
    seen = []
    s = sup.MailSyncSupervisor()
    s.on_change(lambda scope, aid, stats: seen.append((scope, aid)))
    results = iter([
        {"ok": True, "account": "a@x", "stats": {"INBOX": {"new": 0, "flag_updates": 0, "vanished": 0}}},
        {"ok": True, "account": "a@x", "stats": {"INBOX": {"new": 0, "flag_updates": 2, "vanished": 0}}},
        {"ok": False, "account": "a@x", "error": "auth"},
        {"ok": True, "account": "a@x", "stats": {"INBOX": {"new": 1, "flag_updates": 0, "vanished": 0},
                                                  "Sent": {"new": 0, "flag_updates": 0, "vanished": 0}}},
        {"ok": True, "account": "a@x", "stats": {"INBOX": {"new": 0, "flag_updates": 0, "vanished": 1}}},
    ])
    monkeypatch.setattr(sup, "_sync_one", lambda scope, cred, acc: next(results))
    for _ in range(5):
        s.sync_one(SCOPE, "alice", {"account_id": "a@x"})
    assert seen == [(SCOPE, "a@x")] * 3
