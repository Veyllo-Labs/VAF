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
from pathlib import Path

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
    store._reset_announce_state()   # cancels the timers an earlier test left behind; the dicts stay the module's own
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


def _mail_thread(unread=True, sent_reply=False, *, message_id="<q@example.com>", subject="Vertrag Q4",
                 from_addr="Lena <lena@example.com>", category=None, junk=False, uid=1):
    s = MailStore(SCOPE)
    apk = s.upsert_account("alice@example.com", "imap", "alice@example.com")
    inbox_pk = s.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    folder_pk = s.upsert_folder(apk, "Junk", special_use="\\Junk", sync_tier="eager") if junk else inbox_pk
    root = s.ingest_message(apk, folder_pk, uid, ParsedMessage(
        message_id=message_id, subject=subject, from_addr=from_addr,
        to_addrs="alice@example.com", date_ts=int(NOW) - 600, refs=[], body_text="zwei Punkte offen"))
    if category is not None:
        s.set_category(root, category)
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
    counts = all_rows["counts"]
    pinned = ("all", "waits", "unread", "agent", "per_channel", "waits_per_channel", "stored_per_channel")
    assert {k: counts[k] for k in pinned} == {"all": 3, "waits": 2, "unread": 2, "agent": 1,
                                             "per_channel": {"whatsapp": 2, "telegram": 1, "discord": 0, "mail": 0, "room": 0},
                                             "waits_per_channel": {"whatsapp": 1, "telegram": 1, "discord": 0, "mail": 0, "room": 0},
                                             "stored_per_channel": {"whatsapp": 3, "telegram": 1, "discord": 0, "mail": 0, "room": 0}}
    assert sum(counts["unread_per_channel"].values()) == counts["unread"] and counts["invitations"] == 0
    assert set(counts) == set(pinned) | {"unread_per_channel", "invitations", "bulk_hidden"}, "a new count is a doc and a test, not a surprise"
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
    with pytest.raises(ValueError, match="unknown room"):
        # a row the fixture invented has no room behind it
        inbox.mark_conversation("alice", SCOPE, "room", "r1", seen=True)
    with pytest.raises(ValueError, match="unknown room"):
        inbox.mark_conversation("alice", SCOPE, "room", "nobody", seen=True)


def test_mark_conversation_routes_seen_and_done_per_lane(world):
    _msg("+491700000042", "hallo", ts=NOW - 900)
    assert _row("whatsapp:+491700000042")["unread"] == 1
    out = inbox.mark_conversation("alice", SCOPE, "whatsapp", "+491700000042", seen=True)
    assert out["seen_ts"] >= NOW - 900 and _row("whatsapp:+491700000042")["unread"] == 0
    assert not _row("whatsapp:+491700000042")["waits"], "reading takes a chat off waits: the reader decides"
    inbox.mark_conversation("alice", SCOPE, "whatsapp", "+491700000042", done=True)
    assert _row("whatsapp:+491700000042")["done"]
    thread = _mail_thread()
    inbox.mark_conversation("alice", SCOPE, "mail", thread, seen=True)
    assert _row(f"mail:{thread}")["unread"] == 0
    with pytest.raises(ValueError):
        inbox.mark_conversation("alice", SCOPE, "fax", "1", seen=True)
    with pytest.raises(ValueError, match="local admin"):
        inbox.mark_conversation("bob", "0000000000000000000000000000000b", "discord", "7", seen=True)


def test_can_compose_follows_the_whatsapp_windows_rule(world, monkeypatch):
    """MUTATION: drop `whatsapp_off` from _messenger_rows and the owner's chat cannot be
    drafted from the inbox while the WhatsApp window offers the compose box."""
    _msg("+491700000009", "hi", ts=NOW - 900)
    _msg("+491700000042", "hallo", ts=NOW - 800)
    rows = {r["key"]: r for r in _rows()["rows"]}
    assert rows["whatsapp:+491700000009"]["mode"] == "owner" and rows["whatsapp:+491700000009"]["can_compose"] is False
    assert rows["whatsapp:+491700000042"]["mode"] == "readonly" and rows["whatsapp:+491700000042"]["can_compose"] is True
    import vaf.core.config as cfg_mod
    off = dict(CONFIG, whatsapp_config=dict(CONFIG["whatsapp_config"], inbound_to_agent=False))
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: off.get(key, default)))
    rows = {r["key"]: r for r in _rows()["rows"]}
    assert rows["whatsapp:+491700000009"]["can_compose"] is True, "the switch off: every chat is the person's to write in"


def test_reading_takes_a_conversation_off_waits_for_you(world):
    """MUTATION: drop `unread > 0` from chat_state (or the seen floor from the owner-asked
    rule, or `unread > 0` from mail_thread_state) and a read conversation keeps waiting."""
    _msg("+491700000042", "wann passt es dir?", ts=NOW - 900)
    assert _row("whatsapp:+491700000042")["waits"] and _rows()["counts"]["waits"] == 1
    seen = inbox.mark_conversation("alice", SCOPE, "whatsapp", "+491700000042", seen=True)["seen_ts"]
    row = _row("whatsapp:+491700000042")
    assert not row["waits"] and row["unread"] == 0 and not row["done"], "read, not done: the person decides"
    assert _rows()["counts"]["waits"] == 0 and _rows(view="waits")["rows"] == []
    _msg("+491700000042", "und morgen?", ts=seen + 1)
    assert _row("whatsapp:+491700000042")["waits"], "a newer question waits again"
    seen = inbox.mark_conversation("alice", SCOPE, "whatsapp", "+491700000042", seen=True)["seen_ts"]
    store.mark_owner_asked("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE, ts=seen + 1)
    assert _row("whatsapp:+491700000042")["waits_reason"] == inbox.WAITS_OWNER_ASKED
    store.mark_seen("alice", "whatsapp", "+491700000042", user_scope_id=SCOPE, ts=seen + 2)
    assert not _row("whatsapp:+491700000042")["waits"], "opening the chat after the agent's question reads that too"
    thread = _mail_thread()
    assert _row(f"mail:{thread}")["waits"]
    inbox.mark_conversation("alice", SCOPE, "mail", thread, seen=True)
    assert not _row(f"mail:{thread}")["waits"], "a read mail thread is the person's to answer or not"
    unread = {"newest_special_use": "\\Inbox", "newest_answered_at": None, "last_date_ts": 100.0, "unread_count": 1, "snippet": "Können wir telefonieren?"}
    assert inbox.mail_thread_state(unread, None, waits_threshold_value=0.6)["waits"] is True
    assert inbox.mail_thread_state(dict(unread, unread_count=0), None, waits_threshold_value=0.6)["waits"] is False


def test_mark_all_seen_reads_every_lane_at_once_and_respects_the_selection(world):
    """MUTATION: drop the group filter and the group chat is read with the rest; drop the
    channel filter and Telegram is read on a WhatsApp-only call; drop the mail lane and the
    thread stays unread."""
    _msg("+491700000042", "wann passt es dir?", ts=NOW - 900)
    _msg("1@g.us", "wer kommt morgen?", ts=NOW - 850)
    _msg("7", "kannst du mich anrufen?", ts=NOW - 800, channel="telegram")
    thread = _mail_thread()
    before = _rows()["counts"]
    assert before["unread"] == 4 and before["waits"] == 4
    moved = inbox.mark_all_seen("alice", SCOPE, channels=["whatsapp"], include_groups=False)
    assert moved == {"whatsapp": 1}
    rows = {r["key"]: r for r in _rows()["rows"]}
    assert rows["whatsapp:+491700000042"]["unread"] == 0 and not rows["whatsapp:+491700000042"]["waits"]
    assert rows["whatsapp:1@g.us"]["unread"] == 1 and rows["whatsapp:1@g.us"]["waits"], "groups left alone"
    assert rows["telegram:7"]["unread"] == 1 and rows[f"mail:{thread}"]["unread"] == 1, "other channels left alone"
    moved = inbox.mark_all_seen("alice", SCOPE)
    assert moved["whatsapp"] == 1 and moved["telegram"] == 1 and moved["mail"] == 1 and moved["room"] == 0
    after = _rows()["counts"]
    assert after["unread"] == 0 and after["waits"] == 0
    assert all(not r["done"] for r in _rows()["rows"]), "read, not done"
    assert inbox.mark_all_seen("alice", SCOPE) == {"whatsapp": 0, "telegram": 0, "mail": 0, "room": 0}, "a marker never moves backwards, nothing to move twice"
    assert inbox.mark_all_seen("alice", SCOPE, channels=["fax"]) == {}
    # Rooms follow the group toggle, and an invitation is a decision, not a message.
    world.append({"room_id": "r-inv", "name": "Door", "unread": 0, "members": 2, "message_count": 0, "last_ts": 0.0, "last": None, "invited": True})
    world.append({"room_id": "r-new", "name": "Phoenix", "unread": 2, "members": 3, "message_count": 5, "last_ts": NOW - 50,
                  "last": {"sender": "atlas", "text": "Entwurf", "mine": False}})
    assert "room" not in inbox.mark_all_seen("alice", SCOPE, include_groups=False), "rooms are hidden with the groups"
    read = []
    import vaf.core.inbox as inbox_mod
    monkeypatch_read = lambda scope, room_id: read.append(room_id) or True
    orig = inbox_mod._read_room
    inbox_mod._read_room = monkeypatch_read
    try:
        assert inbox.mark_all_seen("alice", SCOPE, channels=["room"]) == {"room": 1}
    finally:
        inbox_mod._read_room = orig
    assert read == ["r-new"], "the unread room only, never the invitation"
    rows = {r["key"]: r for r in _rows()["rows"]}
    assert rows["room:r-inv"]["waits_reason"] == inbox.WAITS_INVITATION
    assert _rows()["counts"]["invitations"] == 1 and _rows()["counts"]["unread_per_channel"]["room"] == 2
    assert inbox.mark_conversation("alice", SCOPE, "room", "r-inv", seen=True) == {"channel": "room", "id": "r-inv", "seen": False}, \
        "an invitation is read by answering it: a seen moves nothing"
    rows = {r["key"]: r for r in _rows()["rows"]}
    assert rows["room:r-inv"]["waits"], "and it still waits"


def test_bulk_mail_is_hidden_unless_asked_for(world):
    """MUTATION: drop the `if not include_bulk` filter in list_conversations and the newsletter
    is listed; drop the gate in _mail_rows and "mark all" reads the bulk mail; drop the primary
    exception and a no-reply the person filed under primary vanishes."""
    person = _mail_thread()
    promo = _mail_thread(message_id="<p@example.com>", subject="Sale", from_addr="Shop <shop@example.com>", category="promotions", uid=2)
    social = _mail_thread(message_id="<s@example.com>", subject="Pins", from_addr="Pinterest <recommendations@pinterest.example>", category="social", uid=3)
    junk = _mail_thread(message_id="<j@example.com>", subject="Win", from_addr="Lotto <lotto@example.com>", junk=True, uid=4)
    letter = _mail_thread(message_id="<n@example.com>", subject="News", from_addr="Galaxus <news@newsletter.galaxus.example>", uid=5)
    bank = _mail_thread(message_id="<b@example.com>", subject="Kontoauszug", from_addr="Bank <no-reply@bank.example>", category="primary", uid=6)
    keys = {r["key"] for r in _rows()["rows"]}
    assert keys == {f"mail:{person}", f"mail:{bank}"}, "primary mail only: the provider's tab, the person's label, the junk folder and the sender heuristic hide the rest"
    counts = _rows()["counts"]
    assert counts["per_channel"]["mail"] == 2 and counts["stored_per_channel"]["mail"] == 6, "the lane still holds them"
    assert counts["bulk_hidden"] == 4 and _rows(include_bulk=True)["counts"]["bulk_hidden"] == 0
    shown = {r["key"]: r["bulk"] for r in _rows(include_bulk=True)["rows"]}
    assert set(shown) == {f"mail:{t}" for t in (person, promo, social, junk, letter, bank)}
    assert shown[f"mail:{person}"] is False and shown[f"mail:{bank}"] is False and all(shown[f"mail:{t}"] for t in (promo, social, junk, letter)), "the row says which it is"
    assert not inbox.is_bulk_mail({"category": "primary", "from_addr": "no-reply@x"}) and inbox.is_bulk_mail({"category": "updates"})
    assert inbox.is_bulk_mail({"category": "", "newest_special_use": "\\Junk", "from_addr": "a@b"})
    assert inbox.is_bulk_mail({"category": "primary", "newest_special_use": "\\Junk", "from_addr": "a@b"}), "Gmail stamps Junk primary; the folder wins"
    assert not inbox.is_bulk_mail({"category": "work", "from_addr": "no-reply@x"}), "a label of the person's own is not bulk"
    # "Mark all as read" follows the same toggle: hidden bulk mail keeps its unread flag.
    assert inbox.mark_all_seen("alice", SCOPE, channels=["mail"]) == {"mail": 2}
    assert inbox.mark_all_seen("alice", SCOPE, channels=["mail"], include_bulk=True) == {"mail": 4}


def test_a_primary_thread_behind_a_wall_of_bulk_mail_still_reaches_the_inbox(world):
    """MUTATION: fetch one page of 200 threads and the person's mail behind 250 newsletters
    is invisible to the inbox, its counts and the bulk read."""
    person = _mail_thread(message_id="<p@example.com>", subject="Vertrag", uid=1)
    s = MailStore(SCOPE)
    apk = s.upsert_account("alice@example.com", "imap", "alice@example.com")
    fpk = s.upsert_folder(apk, "INBOX", special_use="\\Inbox", sync_tier="eager")
    for i in range(250):
        pk = s.ingest_message(apk, fpk, 100 + i, ParsedMessage(
            message_id=f"<n{i}@example.com>", subject=f"Deal {i}", from_addr="Shop <news@shop.example>",
            to_addrs="alice@example.com", date_ts=int(NOW) - 500 + i, refs=[], body_text="sale"))
        s.set_category(pk, "promotions")
    s.close()
    keys = [r["key"] for r in _rows()["rows"]]
    assert keys == [f"mail:{person}"], "the one primary thread, 250 newsletters newer than it notwithstanding"
    assert _rows()["counts"]["bulk_hidden"] == 250 and _rows()["counts"]["stored_per_channel"]["mail"] == 251
    assert len(_rows(include_bulk=True)["rows"]) == 200, "with bulk shown the lane is the newest 200"


def test_the_mail_lane_lists_a_thread_once_when_a_sync_shifts_the_pages(world):
    """MUTATION: drop the `seen` set in _mail_rows and the thread that closed one page and
    opened the next is listed twice."""
    class Shifting:
        def __init__(self):
            self.calls = 0
        def list_threads(self, account_id=None, folder=None, limit=200, offset=0):
            self.calls += 1
            start = offset - (1 if offset else 0)   # a sync landed: everything moved down by one
            return [{"thread_id": i, "last_date_ts": 10_000 - i, "subject": f"t{i}", "from_addr": "Shop <news@shop.example>",
                     "snippet": "sale", "message_count": 1, "unread_count": 1, "category": "promotions", "acct": "a@x",
                     "newest_folder": "INBOX", "newest_special_use": "\\Inbox", "newest_answered_at": None,
                     "newest_message_id": f"<{i}@x>", "newest_gm_msgid": "", "newest_pk": i}
                    for i in range(start, min(start + limit, 260))]
    _mail_thread()   # the lane exists (the store is on disk and v2 is on); the fake service replaces its listing
    svc = Shifting()
    rows = inbox._mail_rows("alice", SCOPE, limit=200, svc=svc, include_bulk=False)
    keys = [r["key"] for r in rows]
    assert len(keys) == len(set(keys)) == 260 and svc.calls == 2, "two pages, the overlapping thread once"


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


def test_reply_expectation_reads_the_text_without_a_model():
    """MUTATION: drop the closer list and "danke" waits; drop the question-mark bonus and
    "ok?" does not; let a closer outweigh the mark and "Danke, und wann?" does not."""
    waits = lambda s: inbox.reply_expectation(s) >= inbox.WAITS_THRESHOLD_DEFAULT
    for closer in ("danke", "Vielen Dank!", "bis später", "ok", "OK 👍", "\U0001F44D", "Alles klar, bis dann", "Super, danke dir!",
                   "thanks", "see you", "Ja, gerne!", "Vielen Dank für die schnelle Antwort, das hilft mir sehr weiter.",
                   "Danke, morgen dann", "Alles klar, wie besprochen", "Nein danke", "Hallo Max, danke dir", "Hi Bob, thanks!",
                   "Ich sag dir morgen Bescheid", "Kann ich dir morgen sagen", "10 Uhr passt", "Termin bestätigt",
                   "Gut, dann sehen wir uns morgen", "FYI: the meeting moved to 3pm", "Nur zur Info, ich bin morgen im Homeoffice",
                   "Dear DeepSeek API user, DeepSeek plans to officially release the V4.1 Flash model around September 10.",
                   "Liebe Kundin, lieber Kunde, ab Oktober gelten neue Preise. Alle Rechte vorbehalten.",
                   "Ich geb dir Bescheid", "Sag ich dir heute Abend", "Kann ich dir erst nächste Woche sagen", "Ich melde mich, sobald ich mehr weiß",
                   "Dienstag um 9 passt mir", "Freitag geht bei mir", "3pm works", "Aber gerne", "Danke, aber nein", "Hallo Anna! Ja, ich komme",
                   "Ist notiert 👍", "Das muss bis Montag fertig sein, dann sehen wir uns", "Ich brauche nichts, danke",
                   # the thanks and goodbyes a German chat borrows
                   "merci", "grazie", "gracias", "teşekkürler", "ありがとう", "谢谢", "görüşürüz"):
        assert not waits(closer), closer
    assert inbox.reply_expectation("Schau mal https://example.com/?q=1") == inbox.reply_expectation("Schau mal"), "a link's own ? asks nothing"
    for asks in ("hallo", "Guten Morgen", "Wann kommst du", "ok?", "Danke, und wann?", "Passt Donnerstag 10 Uhr für die Übergabe?",
                 "Hallo, wegen der Übergabe der Wohnung: ich könnte Donnerstag.", "Kannst du mir bitte den Vertrag schicken",
                 "Thanks! One more thing: can you send the invoice?", "Bin da.", "\u3053\u3093\u306b\u3061\u306f\uff1f",
                 "Vielen Dank für die schnelle Antwort, das hilft mir sehr weiter, was ist mit morgen",
                 "Ok, schick mir bitte die Adresse", "Ja und du", "Ja oder nein", "Hallo Max wann kommst du",
                 "Schaffst du das bis Freitag", "Passt dir Donnerstag 15 Uhr", "Alles gut bei dir",
                 "Super, danke! Eine Sache noch: der Link funktioniert nicht", "Donnerstag wäre mir lieber",
                 "Gut, und dir", "Ich bin morgen nicht im Büro, können wir telefonieren", "Hallo Max, hat der Kunde schon bezahlt",
                 "Kannst du mir den Newsletter-Text bis Freitag schicken", "Wir bräuchten bis Freitag eine Antwort",
                 "Ich geb dir Bescheid, aber schick mir bitte vorher die Adresse", "Dear Customer Service, my order has not arrived, can you check",
                 "Kannst du mein Passwort zurücksetzen?", "Bitte melde mich für den Kurs an", "Donnerstag passt mir leider nicht"):
        assert waits(asks), asks
    quoted = 'Er fragte "kommst du?" und ich sagte nein'
    assert inbox.reply_expectation(quoted) == inbox.reply_expectation("Er fragte und ich sagte nein") < 1.0, "a quoted question mark asks nothing"
    assert inbox.reply_expectation("") == 0.0
    assert 0.0 <= inbox.reply_expectation("?" * 50) <= 1.0


def test_the_threshold_comes_from_the_config_and_reaches_every_lane(world, monkeypatch):
    _msg("+491700000042", "danke", ts=NOW - 10)
    _msg("+491700000050", "wann passt es dir?", ts=NOW - 5)
    keys = lambda **kw: [r["key"] for r in _rows(view="waits", **kw)["rows"]]
    assert keys() == ["whatsapp:+491700000050"], "a thank-you waits for nobody, a question does"
    import vaf.core.config as cfg_mod
    strict = dict(CONFIG, inbox_waits_threshold=1.0)
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: strict.get(key, default)))
    assert keys() == ["whatsapp:+491700000050"], "at 1.0 only a question (capped at 1.0) still waits"
    loose = dict(CONFIG, inbox_waits_threshold=0.0)
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: loose.get(key, default)))
    assert keys() == ["whatsapp:+491700000050", "whatsapp:+491700000042"], "at 0 every inbound waits, as before the rule"
    from vaf.core.config import Config
    assert Config.DEFAULTS["inbox_waits_threshold"] == 0.6
    doc = (Path(__file__).resolve().parent.parent / "docs" / "setup" / "CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    assert "`inbox_waits_threshold`" in doc
    # The mail rule, at the default threshold (the config above still says 0.0).
    thread = {"newest_special_use": "\\Inbox", "newest_answered_at": None, "last_date_ts": 100.0, "unread_count": 1, "snippet": "Danke, hat geklappt!"}
    assert inbox.mail_thread_state(thread, None, waits_threshold_value=0.6)["waits"] is False, "mail runs the newest snippet through the same rule"
    assert inbox.mail_thread_state(dict(thread, snippet="Könnten Sie mir das Angebot schicken?"), None, waits_threshold_value=0.6)["waits"] is True
    assert inbox.mail_thread_state(thread, None)["waits"] is True, "and the configured threshold (0.0 here) reaches the mail lane too"


def test_an_automated_sender_never_waits():
    """MUTATION: drop is_automated_sender from mail_thread_state and a status page waits."""
    automated = ("OpenAI (via incident.io) <no-reply@status.incident.io>", "noreply@github.com", "notifications@slack.com",
                 "Newsletter <news@shop.example>", "MAILER-DAEMON@mail.example", "Alerts <alerts@monitor.example>",
                 "do_not_reply@bank.example", "GitHub <noreply@github.com>", '"Do Not Reply" <x@bank.example>', "No Reply <x@bank.example>")
    for sender in automated:
        assert inbox.is_automated_sender(sender), sender
    for person in ("Lena <lena@example.com>", "bob.mueller@firma.example", "Alice Reply <alice@example.com>", "info@firma.example",
                   "Max Info <max@example.com>", "support@shop.example", "alice.news@example.com", "Status Meier <s.meier@example.com>"):
        assert not inbox.is_automated_sender(person), person
    assert inbox.is_automated_sender("news@shop.example") and inbox.is_automated_sender("alerts@monitor.example")
    assert inbox.is_automated_sender("lena@example.com", "promotions") and not inbox.is_automated_sender("lena@example.com", "primary")
    thread = {"newest_special_use": "\\Inbox", "newest_answered_at": None, "last_date_ts": 100.0, "unread_count": 1,
              "snippet": "Incident resolved. Can you confirm?", "from_addr": "OpenAI (via incident.io) <no-reply@status.incident.io>"}
    assert inbox.mail_thread_state(thread, None, waits_threshold_value=0.6)["waits"] is False, "a status page reads no answer"
    assert inbox.mail_thread_state(dict(thread, from_addr="Lena <lena@example.com>"), None, waits_threshold_value=0.6)["waits"] is True


def test_the_counts_say_what_each_lane_holds_before_any_filter(world):
    _msg("+491700000042", "hi", ts=NOW - 10)
    _msg("+491700000042", "bye", direction="out", ts=NOW - 5, sender=store.OWNER_SENDER)   # done by the person
    out = _rows(view="waits")
    assert out["rows"] == [] and out["counts"]["per_channel"]["whatsapp"] == 0, "the done toggle hid it from the counts"
    assert out["counts"]["stored_per_channel"]["whatsapp"] == 1, "but the lane holds a chat"
    assert out["counts"]["stored_per_channel"]["telegram"] == 0


def test_owner_endpoints_file_a_formatted_whitelist_number_under_the_store_key(monkeypatch):
    from vaf.core import messaging_connections as mc
    import vaf.core.config as cfg_mod
    cfg = {"whatsapp_config": {"whitelist": [
        {"phone_number": "+49 170 000 0009", "vaf_username": "alice", "user_scope_id": SCOPE},
        {"phone_number": "0049 170 0000010", "vaf_username": "alice", "user_scope_id": SCOPE},
        {"phone_number": "0170 0000011", "vaf_username": "alice", "user_scope_id": SCOPE},
    ]}}
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    assert mc.owner_endpoints("whatsapp", "alice", SCOPE) == {"+491700000009", "+491700000010", "+491700000011"}


def test_the_group_pattern_agrees_with_the_group_rule():
    """MUTATION: change either side and the two disagree on a sample."""
    import re
    for channel, like in inbox._GROUP_LIKE.items():
        pattern = re.compile("^" + re.escape(like).replace("%", ".*") + "$")
        for sample in ("1@g.us", "+491700000042", "-500", "7", "555@lid", "-1001234567890"):
            assert bool(pattern.match(sample)) == inbox.is_group(channel, sample), (channel, sample)
    assert "discord" not in inbox._GROUP_LIKE and not inbox.is_group("discord", "-5")


def test_the_pure_rules_stand_alone():
    assert inbox.reply_window_until(None, None, 3600) is None
    assert inbox.reply_window_until(100.0, None, 3600) == 3700.0
    assert inbox.reply_window_until(100.0, 500.0, 3600) == 4100.0
    assert inbox.reply_window_until(100.0, 500.0, 0) is None
    assert inbox.is_group("whatsapp", "1@g.us") and inbox.is_group("telegram", "-5") and inbox.is_group("room", "x")
    assert not inbox.is_group("discord", "-5") and not inbox.is_group("whatsapp", "+49")
    assert inbox.channel_label("whatsapp") == "WhatsApp" and inbox.channel_label("room") == "Room"
    # A thread whose older message was answered still waits when the newest one was not.
    older_answered = {"newest_special_use": "\\Inbox", "newest_answered_at": None, "answered": 1, "last_date_ts": 100.0,
                      "unread_count": 1, "snippet": "Wann können wir telefonieren?"}
    assert inbox.mail_thread_state(older_answered, None)["waits"] is True
    assert inbox.mail_thread_state(dict(older_answered, newest_answered_at="2026-09-09 10:00:00"), None)["waits"] is False
