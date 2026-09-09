# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The three channel dashboards read the rows the inbox reads (vaf/api/whatsapp_routes.py,
telegram_routes.py, discord_routes.py): the count is the store's, the newest message and the
person's own state (unread, waits, done) come from `chat_overview` + `chat_state`, and the
WhatsApp reply window is computed from the same rows, so nothing asks a bridge or a session file.

MUTATION: bring back the session-size overwrite and the WhatsApp count test goes red; ask the
bridge's conversation_open_until and the no-bridge guard goes red; drop the Discord sessions
and the last test goes red.
"""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import vaf.api.discord_routes as dr
import vaf.api.telegram_routes as tr
import vaf.api.whatsapp_routes as routes
from vaf.api import whatsapp_bridge as wa
from vaf.core import channel_message_store as store
from vaf.core.config import Config
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"
NOW = time.time() - 3600.0
WINDOW_H = 72


def _request(scope=SCOPE, username="alice", role="user"):
    return SimpleNamespace(state=SimpleNamespace(user={"username": username, "user_scope_id": scope, "role": role}))


def _msg(chat, body, direction="in", ts=NOW, sender=None, channel="whatsapp", user="alice", scope=SCOPE, message_id=None):
    store.append_message(user, chat, body, direction=direction, ts=ts, sender_jid=sender, channel=channel,
                         user_scope_id=scope, message_id=message_id)


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    config = {
        "whatsapp_config": {"enabled": True, "inbound_to_agent": True, "reply_window_hours": WINDOW_H, "whitelist": []},
        "telegram_config": {"whitelist": [{"telegram_user_id": "7", "vaf_username": "alice", "user_scope_id": SCOPE}],
                            "relay_whitelist": [{"telegram_user_id": "11", "vaf_username": "alice", "user_scope_id": SCOPE}],
                            "chat_activity": [{"chat_id": "11", "ts": int(NOW) - 900}]},
        "discord_config": {"verified": True, "admin_user_id": "42", "admin_username": "adm", "enabled": True},
        "channel_ingress_policy": None,
    }
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: config.get(key, default)))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: {"whatsapp_config": dict(config["whatsapp_config"])}))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: None))
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: None)
    monkeypatch.setattr(store, "_announce_last", {})
    monkeypatch.setattr(store, "_announce_timers", {})
    # WhatsApp: the bridge answers nothing; the rows must come from the store alone.
    monkeypatch.setattr(routes, "_is_whatsapp_admin", lambda req: False)
    monkeypatch.setattr(wa, "get_whatsapp_chats", lambda *a, **k: [])
    monkeypatch.setattr(wa, "is_bridge_running", lambda: False)
    monkeypatch.setattr(wa, "get_connection_status", lambda *a, **k: False)
    monkeypatch.setattr(wa, "get_lid_mappings", lambda *a, **k: [])
    monkeypatch.setattr(wa, "get_contact_names", lambda *a, **k: {})
    monkeypatch.setattr(wa, "_append_chat_activity", lambda *a, **k: None)
    monkeypatch.setattr(wa, "conversation_open_until",
                        lambda *a, **k: pytest.fail("the dashboard asked the bridge for the reply window"))
    # Telegram: no getMe round trip, the caller is not the admin.
    monkeypatch.setattr(tr, "_get_bot_username", lambda: None)
    monkeypatch.setattr(tr, "_is_telegram_admin", lambda req: False)
    import vaf.api.discord_bridge as dc
    monkeypatch.setattr(dc, "is_bridge_running", lambda: False)
    return config


def test_whatsapp_rows_carry_the_stores_count_the_state_and_the_reply_window(world):
    _msg("+491700000042", "hallo", ts=NOW - 100, message_id="w1")
    _msg("+491700000042", "hi", direction="out", ts=NOW - 50, sender="agent", message_id="w2")
    _msg("+491700000042", "danke", ts=NOW - 10, message_id="w3")
    _msg("+491700000050", "moin", ts=NOW - 5, message_id="w4")
    _msg("+491700000060", "done?", ts=NOW - 30, message_id="w5")
    _msg("+491700000060", "yes", direction="out", ts=NOW - 20, sender=store.OWNER_SENDER, message_id="w6")
    out = asyncio.run(routes.get_whatsapp_dashboard(_request()))
    by_id = {s["chat_id"]: s for s in out["sessions"]}
    a, b, c = by_id["+491700000042"], by_id["+491700000050"], by_id["+491700000060"]
    assert a["message_count"] == 3 and a["unread"] == 2 and a["waits"] is True and a["waits_reason"] == "unanswered"
    assert a["type"] == "conversation" and a["reply_window_until"] == pytest.approx(NOW - 10 + WINDOW_H * 3600)
    assert a["last_preview"] == "danke" and a["preview_from"] == "them" and a["answered_by_agent"] is False
    assert b["type"] == "unknown" and b["reply_window_until"] is None and b["unread"] == 1 and b["message_count"] == 1
    assert c["done"] is True and c["waits"] is False and c["preview_from"] == "you", "the person's own reply closes it"
    assert [s["chat_id"] for s in out["sessions"]] == ["+491700000050", "+491700000042", "+491700000060"]


def test_the_whatsapp_dashboard_reads_no_session_file_and_no_bridge_rule():
    src = (Path(routes.__file__)).read_text(encoding="utf-8")
    body = src.split("async def get_whatsapp_dashboard(", 1)[1].split("\n@router", 1)[0]
    assert "SessionManager" not in body and "list_chats_from_store" not in body
    assert "conversation_open_until" not in body and "conversation_open(" not in body
    assert "chat_overview(" in body and "chat_state(" in body and "reply_window_until(" in body


def test_telegram_sessions_come_from_the_store_and_the_activity_log_only_seeds(world):
    _msg("7", "hi", ts=NOW - 10, channel="telegram", message_id="t1")
    _msg("9", "yo", ts=NOW - 5, channel="telegram", message_id="t2")
    out = asyncio.run(tr.get_telegram_dashboard(_request()))
    by_id = {s["chat_id"]: s for s in out["sessions"]}
    seven, nine, eleven = by_id["7"], by_id["9"], by_id["11"]
    assert seven["type"] == "admin" and seven["message_count"] == 1 and seven["unread"] == 1 and seven["waits"] is True
    assert nine["type"] == "unknown" and nine["last_preview"] == "yo" and nine["preview_from"] == "them"
    assert eleven["type"] == "relay" and eleven["last_ts"] == int(NOW) - 900
    assert eleven["message_count"] == 0 and eleven["unread"] == 0, "a chat only the activity log knows has no stored count"
    assert [s["chat_id"] for s in out["sessions"]] == ["9", "7", "11"]


def test_discord_sessions_are_the_admin_stores_rows_and_nobody_elses(world):
    _msg("42", "ping", ts=NOW - 5, channel="discord", user="admin", scope=None, message_id="d1")
    out = asyncio.run(dr.get_discord_dashboard(_request(role="admin")))
    assert [(s["chat_id"], s["type"], s["unread"], s["waits"], s["message_count"]) for s in out["sessions"]] == \
        [("42", "admin", 1, True, 1)]
    assert asyncio.run(dr.get_discord_dashboard(_request(scope=OTHER, username="bob")))["sessions"] == []
