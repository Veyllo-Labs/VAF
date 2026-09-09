# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The one list of conversations (vaf/core/inbox.py): five sources, one row shape, one set of
rules for unread, waits-for-you, answered-by-agent and done, pinned without a bridge, a
Postgres or a Telegram token.

MUTATION: let the agent's reply close a row and the waits test goes red; drop the owner-asked
floor and the back-channel test goes red; read Discord rows for every caller and the local-admin
test goes red.
"""
import time

import pytest

from vaf.core import channel_message_store as store
from vaf.core import inbox
from vaf.core.platform import Platform
from vaf.mail.parser import ParsedMessage
from vaf.mail.store import MailStore

SCOPE = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"
NOW = time.time() - 3600.0   # an hour ago: the marks stamp real time and must land after the seed

CONFIG = {
    "whatsapp_config": {"whitelist": [{"phone_number": "+491700000009", "vaf_username": "alice", "user_scope_id": SCOPE}],
                        "reply_window_hours": 72, "lid_to_e164": {"555@lid": "+491700000077"}},
    "telegram_config": {"whitelist": [{"telegram_user_id": "7", "vaf_username": "alice", "user_scope_id": SCOPE}],
                        "relay_whitelist": [{"telegram_user_id": "9", "vaf_username": "alice", "user_scope_id": SCOPE}]},
    "discord_config": {"admin_user_id": "42"},
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: CONFIG.get(key, default)))
    import vaf.core.contacts_store as contacts
    monkeypatch.setattr(contacts, "front_office_endpoints",
                        lambda username=None, user_scope_id=None, channel="whatsapp": {"+491700000005"} if channel == "whatsapp" else set())
    monkeypatch.setattr(contacts, "is_local_admin_caller", lambda username, user_scope_id: (username or "") == "admin" and not user_scope_id)
    monkeypatch.setattr(contacts, "get_contact_name_by_phone", lambda phone, username=None, user_scope_id=None: "Bob" if phone == "+491700000005" else None)
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    monkeypatch.setattr(store, "_announce_last", {})
    monkeypatch.setattr(store, "_announce_timers", {})
    rooms = []
    import vaf.core.session as session_mod
    monkeypatch.setattr(session_mod, "_room_rows", lambda scope: list(rooms) if scope == SCOPE else [])
    return rooms


def _msg(chat, body, direction="in", ts=NOW, sender=None, channel="whatsapp", user="alice", scope=SCOPE, **kw):
    store.append_message(user, chat, body, direction=direction, ts=ts, sender_jid=sender, channel=channel,
                         user_scope_id=scope, **kw)


def _rows(**kw):
    kw.setdefault("now", NOW + 1)
    return inbox.list_conversations("alice", SCOPE, **kw)


def _row(key, **kw):
    kw.setdefault("include_done", True)
    return next(r for r in _rows(**kw)["rows"] if r["key"] == key)


def _mail_thread(unread=True, sent_reply=False):
    s = MailStore(SCOPE)
    apk = s.upsert_account("alice@example.com", "imap", "alice@example.com")
    inbox_pk = s.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    root = s.ingest_message(apk, inbox_pk, 1, ParsedMessage(
        message_id="<q@example.com>", subject="Vertrag Q4", from_addr="Lena <lena@example.com>",
        to_addrs="alice@example.com", date_ts=int(NOW) - 600, refs=[], body_text="zwei Punkte offen"))
    if not unread:
        s.set_local_flags(root, add=["\\Seen"], remove=())
    if sent_reply:
        sent_pk = s.upsert_folder(apk, "Sent", special_use="\\Sent", sync_tier="eager")
        s.ingest_message(apk, sent_pk, 2, ParsedMessage(
            message_id="<a@example.com>", subject="Re: Vertrag Q4", from_addr="alice@example.com",
            to_addrs="lena@example.com", date_ts=int(NOW) - 300, refs=["<q@example.com>"], body_text="erledigt"))
    thread_id = s.get_message(root)["thread_id"]
    s.close()
    return str(thread_id)


def test_rows_come_from_every_lane_newest_first(world):
    _msg("+491700000042", "hallo", ts=NOW - 900)
    _msg("7", "hi there", ts=NOW - 800, channel="telegram")
    thread = _mail_thread()
    world.append({"room_id": "r1", "name": "Projekt Phoenix", "unread": 2, "members": 3, "message_count": 5,
                  "last_ts": NOW - 100, "last": {"sender": "atlas", "text": "Entwurf 3", "mine": False}})
    keys = [r["key"] for r in _rows()["rows"]]
    assert keys == ["room:r1", f"mail:{thread}", "telegram:7", "whatsapp:+491700000042"]
    assert _rows()["counts"]["per_channel"] == {"whatsapp": 1, "telegram": 1, "discord": 0, "mail": 1, "room": 1}


def test_waits_is_the_unanswered_inbound_or_the_agents_question_and_the_agents_reply_lifts_it(world):
    _msg("+491700000042", "passt Donnerstag?", ts=NOW - 900)
    row = _row("whatsapp:+491700000042")
    assert row["waits"] and row["waits_reason"] == inbox.WAITS_UNANSWERED and row["preview_from"] == "them"
    _msg("+491700000042", "Donnerstag geht", direction="out", ts=NOW - 800)
    row = _row("whatsapp:+491700000042")
    assert not row["waits"] and row["answered_by_agent"] and not row["done"], "the agent's reply lifts waits, not done"
    assert row["preview_from"] == "agent"
    store.mark_owner_asked("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE, ts=NOW - 700)
    row = _row("whatsapp:+491700000042")
    assert row["waits"] and row["waits_reason"] == inbox.WAITS_OWNER_ASKED
    _msg("+491700000042", "ich frage nach", direction="out", ts=NOW - 600)
    assert not _row("whatsapp:+491700000042")["waits"], "the agent writing to the contact again answers its own question"


def test_done_by_mark_or_by_the_persons_own_reply_and_a_newer_message_reopens_it(world):
    _msg("+491700000042", "hallo", ts=NOW - 900)
    assert [r["key"] for r in _rows()["rows"]] == ["whatsapp:+491700000042"]
    store.mark_done("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE, ts=NOW - 850)
    assert _rows()["rows"] == [] and _row("whatsapp:+491700000042")["done"]
    _msg("+491700000042", "noch da?", ts=NOW - 800)
    row = _row("whatsapp:+491700000042")
    assert not row["done"] and row["waits"]
    _msg("+491700000042", "ja, gleich", direction="out", ts=NOW - 700, sender=store.OWNER_SENDER)
    row = _row("whatsapp:+491700000042")
    assert row["done"] and not row["waits"] and row["preview_from"] == "you"


def test_the_five_mode_chips(world):
    _msg("+491700000009", "owner", ts=NOW - 900)
    _msg("+491700000005", "contact", ts=NOW - 800)
    _msg("+491700000042", "agent wrote", direction="out", ts=NOW - 700)
    _msg("+491700000042", "reply", ts=NOW - 650)
    _msg("+491700000099", "stranger", ts=NOW - 600)
    _msg("777@lid", "who is this", ts=NOW - 500)
    _msg("555@lid", "resolved", ts=NOW - 400)
    _msg("7", "owner tg", ts=NOW - 300, channel="telegram")
    _msg("9", "relay tg", ts=NOW - 200, channel="telegram")
    modes = {r["key"]: r["mode"] for r in _rows()["rows"]}
    assert modes["whatsapp:+491700000009"] == "owner"
    assert modes["whatsapp:+491700000005"] == "contact"
    assert modes["whatsapp:+491700000042"] == "conversation"
    assert modes["whatsapp:+491700000099"] == "readonly"
    assert modes["whatsapp:777@lid"] == "needs_assign" and modes["whatsapp:555@lid"] == "readonly"
    assert modes["telegram:7"] == "owner" and modes["telegram:9"] == "relay"
    can = {r["key"]: r["can_compose"] for r in _rows()["rows"]}
    assert can["whatsapp:+491700000099"] is True and can["whatsapp:777@lid"] is False and can["whatsapp:+491700000005"] is False
    assert _row("whatsapp:+491700000005")["name"] == "Bob", "the contact book names a number the store did not"
    assert _row("whatsapp:+491700000042")["session_id"] == "whatsapp_alice_491700000042"


def test_the_reply_window_agrees_with_the_bridge_on_the_same_seed(world):
    from vaf.api import whatsapp_bridge
    _msg("+491700000042", "agent first", direction="out", ts=NOW - 5000)
    _msg("+491700000042", "answer", ts=NOW - 4000)
    _msg("+491700000042", "later, rejected", ts=NOW + 400_000)
    mine = _row("whatsapp:+491700000042")["reply_window_until"]
    assert mine == whatsapp_bridge.conversation_open_until("alice", "+491700000042", SCOPE)
    assert mine == (NOW - 4000) + 72 * 3600


def test_views_groups_and_the_done_toggle(world):
    _msg("+491700000042", "waiting", ts=NOW - 900)
    _msg("+491700000043", "seen and answered", ts=NOW - 850)
    _msg("+491700000043", "sure", direction="out", ts=NOW - 840)
    _msg("123@g.us", "group chatter", ts=NOW - 800)
    _msg("-500", "tg group", ts=NOW - 700, channel="telegram")
    store.mark_seen("alice", "whatsapp", "+491700000043", user_scope_id=SCOPE, ts=NOW - 830)
    store.mark_done("alice", "whatsapp", "123@g.us", user_scope_id=SCOPE, ts=NOW - 790)
    all_rows = _rows()
    assert [r["key"] for r in all_rows["rows"]] == ["telegram:-500", "whatsapp:+491700000043", "whatsapp:+491700000042"]
    assert all_rows["counts"] == {"all": 3, "waits": 2, "unread": 2, "agent": 1,
                                  "per_channel": {"whatsapp": 2, "telegram": 1, "discord": 0, "mail": 0, "room": 0},
                                  "waits_per_channel": {"whatsapp": 1, "telegram": 1, "discord": 0, "mail": 0, "room": 0}}
    assert [r["key"] for r in _rows(include_groups=False)["rows"]] == ["whatsapp:+491700000043", "whatsapp:+491700000042"]
    assert [r["key"] for r in _rows(include_done=True)["rows"]][:1] == ["telegram:-500"]
    assert "whatsapp:123@g.us" in [r["key"] for r in _rows(include_done=True)["rows"]]
    assert [r["key"] for r in _rows(view="waits")["rows"]] == ["telegram:-500", "whatsapp:+491700000042"]
    assert [r["key"] for r in _rows(view="unread")["rows"]] == ["telegram:-500", "whatsapp:+491700000042"]
    assert [r["key"] for r in _rows(view="agent")["rows"]] == ["whatsapp:+491700000043"]
    assert _rows(view="agent")["counts"]["all"] == 3, "counts describe the toggles, not the view"
    assert [r["key"] for r in _rows(channels=["telegram"])["rows"]] == ["telegram:-500"]


def test_a_query_matches_the_name_the_preview_or_a_stored_message(world):
    _msg("+491700000042", "hallo", ts=NOW - 900, chat_name="Alice Müller")
    _msg("+491700000043", "the invoice is attached", ts=NOW - 850)
    _msg("+491700000043", "thanks", ts=NOW - 840)
    _msg("+491700000044", "unrelated", ts=NOW - 830)
    assert [r["key"] for r in _rows(query="müller")["rows"]] == ["whatsapp:+491700000042"]
    assert [r["key"] for r in _rows(query="invoice")["rows"]] == ["whatsapp:+491700000043"], "an older stored message counts"
    assert _rows(query="nothing here")["rows"] == []


def test_discord_rows_belong_to_the_local_admin_only(world):
    store.append_message("admin", "4242", "dm", channel="discord", ts=NOW - 100)
    assert [r["key"] for r in _rows()["rows"]] == []
    admin_rows = inbox.list_conversations("admin", None, now=NOW + 1)["rows"]
    assert [r["key"] for r in admin_rows] == ["discord:4242"] and admin_rows[0]["mode"] == "admin"
    assert admin_rows[0]["is_group"] is False


def test_the_mail_lane_reads_v2_threads_and_stays_silent_for_a_legacy_user(world):
    thread = _mail_thread()
    row = _row(f"mail:{thread}")
    assert row["unread"] == 1 and row["waits"] and row["mode"] == "mail" and row["jump"]["thread_id"] == thread
    assert row["jump"]["folder"] == "INBOX" and row["name"].startswith("Lena")
    assert inbox.list_conversations("alice", None, now=NOW + 1)["rows"] == [], "no scope, no mail store of its own"


def test_a_sent_reply_closes_a_mail_thread_and_a_done_mark_hides_it(world):
    answered = _mail_thread(unread=False, sent_reply=True)
    row = _row(f"mail:{answered}")
    assert row["done"] and not row["waits"] and row["preview_from"] == "you" and row["jump"]["folder"] == "Sent"
    assert _rows()["rows"] == []
    inbox.mark_conversation("alice", SCOPE, "mail", answered, done=False)
    assert _row(f"mail:{answered}")["done"], "the Sent folder decides even without a mark"


def test_room_rows_wait_when_unread_or_invited_and_take_the_done_mark(world):
    world.append({"room_id": "r1", "name": "Phoenix", "unread": 2, "members": 3, "message_count": 5,
                  "last_ts": NOW - 100, "last": {"sender": "atlas", "text": "Entwurf 3", "mine": False}})
    world.append({"room_id": "r2", "name": "Door", "unread": 0, "members": 2, "message_count": 0,
                  "last_ts": 0.0, "last": None, "invited": True})
    rows = {r["key"]: r for r in _rows()["rows"]}
    assert rows["room:r1"]["waits"] and rows["room:r1"]["waits_reason"] == inbox.WAITS_UNANSWERED
    assert rows["room:r1"]["preview"] == "Entwurf 3" and rows["room:r1"]["preview_from"] == "atlas"
    assert rows["room:r2"]["waits_reason"] == inbox.WAITS_INVITATION and rows["room:r2"]["is_group"]
    inbox.mark_conversation("alice", SCOPE, "room", "r1", done=True)
    assert "room:r1" not in {r["key"] for r in _rows()["rows"]}
    with pytest.raises(ValueError):
        inbox.mark_conversation("alice", SCOPE, "room", "r1", seen=True)


def test_mark_conversation_routes_seen_and_done_per_lane(world):
    _msg("+491700000042", "hallo", ts=NOW - 900)
    assert _row("whatsapp:+491700000042")["unread"] == 1
    out = inbox.mark_conversation("alice", SCOPE, "whatsapp", "+491700000042", seen=True)
    assert out["seen_ts"] >= NOW - 900 and _row("whatsapp:+491700000042")["unread"] == 0
    assert _row("whatsapp:+491700000042")["waits"], "reading is not answering"
    inbox.mark_conversation("alice", SCOPE, "whatsapp", "+491700000042", done=True)
    assert _row("whatsapp:+491700000042")["done"]
    thread = _mail_thread()
    inbox.mark_conversation("alice", SCOPE, "mail", thread, seen=True)
    assert _row(f"mail:{thread}")["unread"] == 0
    with pytest.raises(ValueError):
        inbox.mark_conversation("alice", SCOPE, "fax", "1", seen=True)


def test_conversation_history_is_one_shape_for_the_lanes(world):
    _msg("+491700000042", "hallo", ts=NOW - 900)
    _msg("+491700000042", "hi", direction="out", ts=NOW - 800)
    _msg("+491700000042", "from me", direction="out", ts=NOW - 700, sender=store.OWNER_SENDER)
    hist = inbox.conversation_history("alice", SCOPE, "whatsapp", "+491700000042")
    assert [(h["role"], h["sender"], h["content"]) for h in hist] == [
        ("user", "them", "hallo"), ("assistant", "agent", "hi"), ("assistant", "you", "from me")]
    assert hist[0]["timestamp"] and hist[0]["content_type"] == "text"
    thread = _mail_thread(sent_reply=True)
    mail = inbox.conversation_history("alice", SCOPE, "mail", thread)
    assert [(m["role"], m["sender"]) for m in mail] == [("user", "Lena <lena@example.com>"), ("assistant", "you")]
    assert inbox.conversation_history("alice", SCOPE, "discord", "4242") == []


def test_another_scope_sees_nothing(world):
    _msg("+491700000042", "hallo", ts=NOW - 900)
    _mail_thread()
    assert inbox.list_conversations("bob", OTHER, now=NOW + 1)["rows"] == []
    assert inbox.conversation_history("bob", OTHER, "whatsapp", "+491700000042") == []


def test_the_pure_rules_stand_alone():
    assert inbox.reply_window_until(None, None, 3600) is None
    assert inbox.reply_window_until(100.0, None, 3600) == 3700.0
    assert inbox.reply_window_until(100.0, 500.0, 3600) == 4100.0
    assert inbox.reply_window_until(100.0, 500.0, 0) is None
    assert inbox.is_group("whatsapp", "1@g.us") and inbox.is_group("telegram", "-5") and inbox.is_group("room", "x")
    assert not inbox.is_group("discord", "-5") and not inbox.is_group("whatsapp", "+49")
    assert inbox.channel_label("whatsapp") == "WhatsApp" and inbox.channel_label("room") == "Room"
