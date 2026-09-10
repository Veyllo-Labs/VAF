# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The person's marks per chat and the one grouped overview (vaf/core/channel_message_store.py).

`chat_marks` holds when the person last opened a chat, marked it done, or was asked about it
by the agent; `chat_overview` is the single statement every conversation list is built from,
and every writer announces `inbox_changed` so an open window refetches instead of polling.

MUTATION: drop the seeding in init_store and the upgrade test goes red; count outbound rows as
unread and the marker test goes red; remove the trailing timer and the throttle test goes red.
"""
import sqlite3
import time

import pytest

from vaf.core import channel_message_store as store
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: default))
    monkeypatch.setattr(store, "_local_admin", lambda: "admin")
    monkeypatch.setattr(store, "_local_admin_scope_id", lambda: "admin-scope")
    frames = []
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: frames.append(scope))
    store._reset_announce_state()   # cancels the timers an earlier test left behind; the dicts stay the module's own
    return frames


def _row(chat, body, direction="in", ts=0.0, sender=None, channel="whatsapp", user="alice", scope=SCOPE, **kw):
    store.append_message(user, chat, body, direction=direction, ts=ts, sender_jid=sender,
                         channel=channel, user_scope_id=scope, **kw)


def _one(rows, chat_id, channel="whatsapp"):
    return next(r for r in rows if r["chat_id"] == chat_id and r["channel"] == channel)


def test_marks_key_on_the_channel_so_two_channels_sharing_a_chat_id_stay_apart(scratch):
    # Distinct message ids: the messages' primary key predates the channel column, which is
    # exactly why the marks carry the channel themselves.
    _row("+491700000042", "hi", ts=100.0, message_id="w1")
    _row("+491700000042", "hi", ts=100.0, channel="telegram", message_id="t1")
    store.mark_seen("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE, ts=200.0)
    assert set(store.chat_marks("alice", user_scope_id=SCOPE)) == {("whatsapp", "+491700000042")}
    rows = store.chat_overview("alice", user_scope_id=SCOPE)
    assert _one(rows, "+491700000042")["unread"] == 0
    assert _one(rows, "+491700000042", "telegram")["unread"] == 1


def test_unread_counts_only_inbound_rows_after_the_seen_marker(scratch):
    _row("+491700000042", "first", ts=100.0)
    _row("+491700000042", "agent reply", direction="out", ts=200.0)
    _row("+491700000042", "second", ts=300.0)
    _row("+491700000042", "owner reply", direction="out", ts=400.0, sender=store.OWNER_SENDER)
    assert _one(store.chat_overview("alice", user_scope_id=SCOPE), "+491700000042")["unread"] == 2
    store.mark_seen("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE, ts=150.0)
    row = _one(store.chat_overview("alice", user_scope_id=SCOPE), "+491700000042")
    assert row["unread"] == 1, "the outbound rows are never unread; only the inbound after the marker"
    assert row["last_in_ts"] == 300.0 and row["last_agent_ts"] == 200.0 and row["last_owner_ts"] == 400.0
    assert row["last_direction"] == "out" and row["last_sender"] == store.OWNER_SENDER
    # The marker never moves backwards.
    assert store.mark_seen("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE, ts=120.0) == 150.0


def test_upgrading_a_store_with_history_starts_with_nothing_unread(scratch, tmp_path):
    path = store._db_path("alice", SCOPE)
    conn = sqlite3.connect(str(path))
    conn.execute("""CREATE TABLE channel_messages (username TEXT NOT NULL DEFAULT '', chat_id TEXT NOT NULL,
        chat_name TEXT, sender_jid TEXT, body TEXT NOT NULL DEFAULT '', direction TEXT NOT NULL DEFAULT 'in',
        ts REAL NOT NULL, message_id TEXT, content_type TEXT DEFAULT 'text', channel TEXT NOT NULL DEFAULT 'whatsapp',
        PRIMARY KEY (username, chat_id, message_id, direction))""")
    for ts in (100.0, 200.0, 300.0):
        conn.execute("INSERT INTO channel_messages (username, chat_id, body, direction, ts, message_id) VALUES (?,?,?,?,?,?)",
                     ("alice", "+491700000042", f"old {ts}", "in", ts, f"m{ts}"))
    conn.commit()
    conn.close()
    store.init_store("alice", SCOPE)
    assert store.chat_marks("alice", user_scope_id=SCOPE)[("whatsapp", "+491700000042")]["seen_ts"] == 300.0
    assert _one(store.chat_overview("alice", user_scope_id=SCOPE), "+491700000042")["unread"] == 0
    _row("+491700000042", "new", ts=400.0)
    assert _one(store.chat_overview("alice", user_scope_id=SCOPE), "+491700000042")["unread"] == 1
    store.init_store("alice", SCOPE)   # a second init never re-seeds
    assert _one(store.chat_overview("alice", user_scope_id=SCOPE), "+491700000042")["unread"] == 1


def test_the_overview_is_the_projection_the_old_listing_reads_and_skips_tombstones(scratch):
    _row("+491700000042", "hello", ts=100.0, chat_name="Dana", message_id="a")
    _row("+491700000042", "gone", ts=200.0, message_id="b")
    _row("+491700000042", "agent", direction="out", ts=300.0, message_id="c")
    _row("+491700000050", "other", ts=50.0, channel="telegram")
    store.mark_deleted("alice", "+491700000042", "b", direction="in", user_scope_id=SCOPE)
    rows = store.chat_overview("alice", user_scope_id=SCOPE)
    assert [(r["channel"], r["chat_id"]) for r in rows] == [("whatsapp", "+491700000042"), ("telegram", "+491700000050")]
    wa = rows[0]
    assert wa["message_count"] == 2 and wa["last_body"] == "agent" and wa["chat_name"] == "Dana"
    listed = store.list_chats_from_store("alice", user_scope_id=SCOPE)
    assert listed == [{k: wa[k] for k in ("chat_id", "last_ts", "message_count", "chat_name", "last_body", "last_direction")}]
    assert store.list_chats_from_store("alice", user_scope_id=SCOPE, channel=None)[1]["chat_id"] == "+491700000050"
    assert store.chat_overview("alice", user_scope_id=SCOPE, channel="telegram")[0]["chat_id"] == "+491700000050"


def test_the_overview_carries_the_reply_window_inputs(scratch):
    _row("+491700000042", "agent wrote first", direction="out", ts=1000.0)
    _row("+491700000042", "answer inside the window", ts=1500.0)
    _row("+491700000042", "much later", ts=9000.0)
    row = _one(store.chat_overview("alice", user_scope_id=SCOPE, reply_window_seconds=3600.0), "+491700000042")
    assert row["last_agent_ts"] == 1000.0 and row["last_in_within_ts"] == 1500.0 and row["last_in_ts"] == 9000.0
    assert _one(store.chat_overview("alice", user_scope_id=SCOPE), "+491700000042")["last_in_within_ts"] is None
    _row("+491700000042", "owner wrote", direction="out", ts=9500.0, sender=store.OWNER_SENDER)
    row = _one(store.chat_overview("alice", user_scope_id=SCOPE, reply_window_seconds=3600.0), "+491700000042")
    assert row["last_agent_ts"] == 1000.0, "the person's own send opens no window"


def test_mark_channel_seen_reads_the_whole_channel_in_one_statement(scratch):
    """MUTATION: drop the `seen_ts < ?` clause and an older bulk mark moves markers back;
    drop the EXISTS clause and a read chat is counted again; route the ids through the
    overview and the 600th chat stays unread."""
    for i in range(600):
        _row(f"+4917{i:08d}", "hallo", ts=100.0 + i)
    _row("1@g.us", "wer kommt?", ts=50.0)
    _row("+491799999999", "erledigt", ts=10.0, direction="out")   # only our own word: nothing to read, not counted
    frames = scratch
    store._reset_announce_state()   # the seed's own announce and its trailing timer are not under test
    frames.clear()
    assert store.mark_channel_seen("alice", "whatsapp", user_scope_id=SCOPE, ts=1000.0, exclude_like="%@g.us") == 600
    assert frames == [SCOPE], "one announce for the whole channel"
    marks = store.chat_marks("alice", SCOPE, channel="whatsapp")
    assert marks[("whatsapp", "+491700000000")]["seen_ts"] == 1000.0 and marks[("whatsapp", "+491700000599")]["seen_ts"] == 1000.0, "no cap"
    assert (marks.get(("whatsapp", "1@g.us")) or {}).get("seen_ts") is None, "the group pattern left alone"
    assert (marks.get(("whatsapp", "+491799999999")) or {}).get("seen_ts") is None, "nothing to read, nothing marked"
    unread = {r["chat_id"]: r["unread"] for r in store.chat_overview("alice", user_scope_id=SCOPE, channel="whatsapp", limit=500)}
    assert all(v == 0 for k, v in unread.items() if k != "1@g.us"), "every listed chat reads as read"
    assert store.mark_channel_seen("alice", "whatsapp", user_scope_id=SCOPE, ts=1000.0) == 1, "now the group, nothing else twice"
    store._reset_announce_state(); frames.clear()
    assert store.mark_channel_seen("alice", "whatsapp", user_scope_id=SCOPE, ts=900.0) == 0, "never backwards"
    assert frames == [], "nothing moved, nothing announced"
    assert store.chat_marks("alice", SCOPE, channel="whatsapp")[("whatsapp", "+4917" + "0" * 8)]["seen_ts"] == 1000.0
    # The agent's unanswered question counts as something to read; one the agent or the
    # person answered since does not.
    store.mark_owner_asked("alice", "whatsapp", "+491700000001", user_scope_id=SCOPE, ts=1100.0)
    store.mark_owner_asked("alice", "whatsapp", "+491700000002", user_scope_id=SCOPE, ts=1100.0)
    _row("+491700000002", "ich frage nach", ts=1200.0, direction="out")
    # A done chat the agent asked about is not waiting either: closed by the mark, or by the
    # person's own newest reply.
    store.mark_owner_asked("alice", "whatsapp", "+491700000004", user_scope_id=SCOPE, ts=1100.0)
    store.mark_done("alice", "whatsapp", "+491700000004", user_scope_id=SCOPE, ts=1150.0)
    store.mark_owner_asked("alice", "whatsapp", "+491700000005", user_scope_id=SCOPE, ts=1100.0)
    _row("+491700000005", "ja, gleich", ts=1050.0, direction="out", sender=store.OWNER_SENDER)
    assert store.mark_channel_seen("alice", "whatsapp", user_scope_id=SCOPE, ts=1300.0) == 1
    marks = store.chat_marks("alice", SCOPE, channel="whatsapp")
    assert marks[("whatsapp", "+491700000001")]["seen_ts"] == 1300.0 and marks[("whatsapp", "+491700000002")]["seen_ts"] == 1000.0
    assert marks[("whatsapp", "+491700000004")]["seen_ts"] == 1000.0 and marks[("whatsapp", "+491700000005")]["seen_ts"] == 1000.0
    assert store.mark_channel_seen("alice", "telegram", user_scope_id=SCOPE) == 0, "another channel: nothing there"


def test_done_and_owner_asked_marks_round_trip(scratch):
    _row("+491700000042", "hi", ts=100.0)
    store.mark_done("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE, ts=150.0)
    store.mark_owner_asked("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE, ts=160.0)
    mark = store.chat_marks("alice", user_scope_id=SCOPE)[("whatsapp", "+491700000042")]
    assert mark == {"seen_ts": None, "done_ts": 150.0, "owner_asked_ts": 160.0}
    store.mark_done("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE, done=False)
    assert store.chat_marks("alice", user_scope_id=SCOPE, channel="whatsapp")[("whatsapp", "+491700000042")]["done_ts"] is None
    row = _one(store.chat_overview("alice", user_scope_id=SCOPE), "+491700000042")
    assert row["done_ts"] is None and row["owner_asked_ts"] == 160.0


def test_another_scope_and_a_missing_store_see_nothing(scratch, tmp_path):
    _row("+491700000042", "hi", ts=100.0)
    assert store.chat_overview("bob", user_scope_id=OTHER) == []
    assert store.chat_marks("bob", user_scope_id=OTHER) == {}
    assert not store._db_path("bob", OTHER).exists(), "a glance never creates a database"


def test_writers_announce_once_per_scope_and_once_more_when_the_burst_ends(scratch, monkeypatch):
    """Deterministic: the throttle reads a clock and arms a timer, and both are handed to the
    test, so ten appends may take as long as a slow runner needs. The wall-clock version of
    this test (a 0.4 s interval and real sleeps) went red on the Windows leg when the burst
    outlived the interval and the trailing announcement fired before the first assertion."""
    import threading
    from types import SimpleNamespace

    clock = [1000.0]
    monkeypatch.setattr(store, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    armed = []

    class FakeTimer:
        def __init__(self, delay, fn, args=()):
            self.delay, self.fn, self.args, self.daemon = delay, fn, args, False

        def start(self):
            armed.append(self)

        def cancel(self):
            pass

    monkeypatch.setattr(store, "threading", SimpleNamespace(Timer=FakeTimer, Lock=threading.Lock))
    interval = store._ANNOUNCE_MIN_INTERVAL_S

    for i in range(10):
        _row("+491700000042", f"m{i}", ts=100.0 + i)
    assert scratch == [SCOPE], "the first write announces at once, the burst collapses"
    assert len(armed) == 1 and abs(armed[0].delay - interval) < 1e-6, "one trailing timer, for the rest of the interval"
    clock[0] += interval
    armed[0].fn(*armed[0].args)          # the interval ends: the trailing announcement fires
    assert scratch == [SCOPE, SCOPE], "the trailing announcement carries what the burst appended"
    scratch.clear()
    armed.clear()
    clock[0] += interval
    store.mark_seen("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE)
    assert scratch == [SCOPE] and armed == [], "a write after the interval announces at once, nothing pending"
    # A Discord row lives in the admin's file with no scope: it announces to the admin's
    # scope, whose own throttle has not fired yet.
    scratch.clear()
    store.append_message("admin", "1234", "dm", channel="discord", ts=500.0)
    assert scratch == ["admin-scope"]
