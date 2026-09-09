# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A reply the person sends from the agent number's own phone is the person's, not the
agent's (vaf/api/whatsapp_bridge.py): the live `owner_sent` event stores their row under
OWNER_SENDER, and the history import labels an outbound row the bridge did not send the same
way (the bridge's own ids carry Baileys' 3EB0 shape). The inbox then reads the chat as
answered by the person ("done"), never as "agent answered".

MUTATION: drop the sender label from the history import and the person's reply reads as the
agent's; drop the body from the owner_sent handler and the reply is not stored at all.
"""
import time

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.core import channel_message_store as store
from vaf.core import inbox
from vaf.core.config import Config
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    cfg = {"whatsapp_config": {}}
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: cfg.get(key, default)))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"whatsapp_config": dict(cfg["whatsapp_config"])}))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, c: cfg.__setitem__("whatsapp_config", c.get("whatsapp_config") or {})))
    monkeypatch.setattr(wa, "_append_chat_activity", lambda *a, **k: None)
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    store._reset_announce_state()
    return cfg


def _state(chat_id):
    row = next(r for r in store.chat_overview("alice", user_scope_id=SCOPE, channel="whatsapp") if r["chat_id"] == chat_id)
    return inbox.chat_state(row, waits_threshold_value=0.6)


def test_the_history_import_tells_the_persons_phone_from_the_bridges_own_sends(isolated):
    wa._dispatch_bridge_event("alice", SCOPE, "history_messages", {"messages": [
        {"chat_id": "+491700000042", "body": "kommst du morgen?", "direction": "in", "ts": 100, "message_id": "A1"},
        {"chat_id": "+491700000042", "body": "ja, gleich", "direction": "out", "ts": 200, "message_id": "ABCDEF0123456789ABCDEF0123456789"},
        {"chat_id": "+491700000043", "body": "hallo?", "direction": "in", "ts": 100, "message_id": "A2"},
        {"chat_id": "+491700000043", "body": "hier der Agent", "direction": "out", "ts": 300, "message_id": "3EB0ABCDEF123456"},
    ]})
    person = _state("+491700000042")
    assert person["done"] and not person["answered_by_agent"] and person["preview_from"] == "you", "the phone's reply is the person's"
    agent = _state("+491700000043")
    assert agent["answered_by_agent"] and not agent["done"] and agent["preview_from"] == "agent", "the bridge's own send stays the agent's"
    assert wa._sent_by_this_bridge("3eb0ff") and not wa._sent_by_this_bridge("BAE5ABC") and not wa._sent_by_this_bridge(None)


def test_a_live_reply_from_the_phone_is_stored_as_the_persons_row(isolated):
    now = int(time.time())
    wa._dispatch_bridge_event("alice", SCOPE, "message", {"from": "491700000042@s.whatsapp.net", "body": "wann?", "ts": now - 60,
                                                          "message_id": "IN1", "content_type": "text"})
    # The live inbound is stamped when it lands; the phone's reply carries its own, later time.
    wa._dispatch_bridge_event("alice", SCOPE, "owner_sent", {"from": "491700000042@s.whatsapp.net", "ts": now + 5, "msg_ts": now + 5,
                                                             "body": "mach ich", "message_id": "PH1", "content_type": "text"})
    rows = store.get_chat_messages("alice", "+491700000042", user_scope_id=SCOPE)
    mine = [r for r in rows if r["direction"] == "out"]
    assert len(mine) == 1 and mine[0]["body"] == "mach ich" and mine[0]["sender_jid"] == store.OWNER_SENDER
    assert isolated["whatsapp_config"].get("owner_control", {}).get("491700000042@s.whatsapp.net"), "the takeover mark is written as before"
    state = _state("+491700000042")
    assert state["done"] and not state["answered_by_agent"] and not state["waits"]
    # Without a body (an older bridge) nothing is stored, and nothing breaks.
    wa._dispatch_bridge_event("alice", SCOPE, "owner_sent", {"from": "491700000042@s.whatsapp.net", "ts": now + 6})
    assert len([r for r in store.get_chat_messages("alice", "+491700000042", user_scope_id=SCOPE) if r["direction"] == "out"]) == 1


def test_a_phone_reply_in_a_lid_chat_lands_in_the_contacts_chat_and_no_status_post_is_stored(isolated):
    now = int(time.time())
    wa._dispatch_bridge_event("alice", SCOPE, "message", {"from": "123456789012345@lid", "fromE164": "+491700000099", "body": "hallo?",
                                                          "ts": now - 60, "message_id": "IN2", "content_type": "text"})
    wa._dispatch_bridge_event("alice", SCOPE, "owner_sent", {"from": "123456789012345@lid", "fromE164": "+491700000099", "ts": now, "msg_ts": now,
                                                             "body": "hi", "message_id": "PH2", "content_type": "text"})
    chats = {r["chat_id"] for r in store.chat_overview("alice", user_scope_id=SCOPE, channel="whatsapp")}
    assert chats == {"+491700000099"}, "one chat, under the resolved number, no phantom @lid row"
    assert _state("+491700000099")["done"]
    for jid in ("status@broadcast", "1234@newsletter", "9@broadcast", "120363@g.us"):
        wa._dispatch_bridge_event("alice", SCOPE, "owner_sent", {"from": jid, "ts": now + 1, "msg_ts": now + 1, "body": "on holiday", "message_id": "S1"})
    assert {r["chat_id"] for r in store.chat_overview("alice", user_scope_id=SCOPE, channel="whatsapp")} == {"+491700000099"}
    # A captionless picture from the phone is stored as its placeholder, as the history does.
    wa._dispatch_bridge_event("alice", SCOPE, "owner_sent", {"from": "491700000099@s.whatsapp.net", "ts": now + 2, "msg_ts": now + 2,
                                                             "body": "<media:image>", "message_id": "PH3", "content_type": "image"})
    rows = sorted(store.get_chat_messages("alice", "+491700000099", user_scope_id=SCOPE), key=lambda r: float(r["ts"]))
    assert [r["body"] for r in rows if r["direction"] == "out"] == ["hi", "<media:image>"]


def test_a_backlog_keeps_its_order_against_the_phones_reply(isolated):
    """The inbound payload carries the message's own time; without it the row is stamped
    on arrival and a backlog delivered after an outage would land after the reply."""
    now = int(time.time())
    wa._dispatch_bridge_event("alice", SCOPE, "owner_sent", {"from": "491700000042@s.whatsapp.net", "ts": now - 3600, "msg_ts": now - 3600,
                                                             "body": "ja klar", "message_id": "PH4", "content_type": "text"})
    wa._dispatch_bridge_event("alice", SCOPE, "message", {"from": "491700000042@s.whatsapp.net", "body": "kannst du morgen?", "ts": now - 7200,
                                                          "message_id": "IN4", "content_type": "text"})
    rows = sorted(store.get_chat_messages("alice", "+491700000042", user_scope_id=SCOPE), key=lambda r: float(r["ts"]))
    assert [(r["direction"], r["body"]) for r in rows] == [("in", "kannst du morgen?"), ("out", "ja klar")]
    assert _state("+491700000042")["done"] and not _state("+491700000042")["waits"]


def test_the_history_import_keeps_what_the_send_path_stored(isolated):
    """The compose box stores the person's send the moment it leaves, under the person's
    label and without WhatsApp's id; the history batch that carries the same message under
    its id (Baileys' shape) adds no second row and relabels nothing."""
    now = float(int(time.time()))
    store.append_message("alice", "+491700000042", "bis morgen dann", direction="out", sender_jid=store.OWNER_SENDER,
                         user_scope_id=SCOPE, ts=now - 100)
    wa._dispatch_bridge_event("alice", SCOPE, "history_messages", {"messages": [
        {"chat_id": "+491700000042", "body": "bis morgen dann", "direction": "out", "ts": now - 100, "message_id": "3EB0DEADBEEF0001"},
        {"chat_id": "+491700000042", "body": "ok", "direction": "in", "ts": now - 50, "message_id": "IN5"},
    ]})
    rows = store.get_chat_messages("alice", "+491700000042", user_scope_id=SCOPE)
    mine = [r for r in rows if r["direction"] == "out"]
    assert len(mine) == 1 and mine[0]["sender_jid"] == store.OWNER_SENDER, "one row, still the person's"
    # A phone reply that repeats the agent's words is not the agent's row: it is kept.
    store.append_message("alice", "+491700000044", "ok", direction="out", sender_jid=None, user_scope_id=SCOPE, ts=now - 400)
    wa._dispatch_bridge_event("alice", SCOPE, "history_messages", {"messages": [
        {"chat_id": "+491700000044", "body": "ok", "direction": "out", "ts": now - 100, "message_id": "PHONEOK1"}]})
    assert sorted((r["body"], r["sender_jid"]) for r in store.get_chat_messages("alice", "+491700000044", user_scope_id=SCOPE)
                  if r["direction"] == "out") == [("ok", ""), ("ok", store.OWNER_SENDER)]
    # The bridge's own voice note and document, stored with the send path's labels, are found
    # again under the history's placeholders and not added twice.
    store.append_message("alice", "+491700000045", "[Voice message]", direction="out", content_type="voice", user_scope_id=SCOPE, ts=now - 90)
    store.append_message("alice", "+491700000045", "[Document] Rechnung", direction="out", content_type="document", user_scope_id=SCOPE, ts=now - 80)
    wa._dispatch_bridge_event("alice", SCOPE, "history_messages", {"messages": [
        {"chat_id": "+491700000045", "body": "<media:audio>", "direction": "out", "ts": now - 90, "message_id": "3EB0V1", "content_type": "audio"},
        {"chat_id": "+491700000045", "body": "Rechnung", "direction": "out", "ts": now - 80, "message_id": "3EB0D1", "content_type": "document"}]})
    assert len([r for r in store.get_chat_messages("alice", "+491700000045", user_scope_id=SCOPE) if r["direction"] == "out"]) == 2
    # A reaction from the phone is not a reply.
    wa._dispatch_bridge_event("alice", SCOPE, "owner_sent", {"from": "491700000045@s.whatsapp.net", "ts": now, "msg_ts": now,
                                                             "body": "<media:reaction>", "message_id": "R1", "content_type": "reaction"})
    assert len([r for r in store.get_chat_messages("alice", "+491700000045", user_scope_id=SCOPE) if r["direction"] == "out"]) == 2
    # A row the store already holds under its id keeps its label on a second import too.
    wa._dispatch_bridge_event("alice", SCOPE, "history_messages", {"messages": [
        {"chat_id": "+491700000043", "body": "vom Telefon", "direction": "out", "ts": now - 30, "message_id": "PHONE1"}]})
    store.append_message("alice", "+491700000043", "vom Telefon", direction="out", sender_jid=None, message_id="PHONE1",
                         user_scope_id=SCOPE, ts=now - 30, keep_existing=True)
    assert [r["sender_jid"] for r in store.get_chat_messages("alice", "+491700000043", user_scope_id=SCOPE)] == [store.OWNER_SENDER]
