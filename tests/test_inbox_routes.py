# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The inbox routes (vaf/api/inbox_routes.py): the Posteingang reads the rows vaf/core/inbox.py
builds, the footer badge reads the counts, a mark round-trips, one history shape serves every
lane, and a GET never asks a bridge.

MUTATION: let the list route call get_whatsapp_chats and the first test goes red; drop the
scope from the identity and the other-scope test goes red; accept a seen for a room that is
not the person's and the refusal test goes red; drop the mail-seen signal and its test goes red.
"""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import vaf.api.inbox_routes as ir
from vaf.core import channel_message_store as store
from vaf.core.platform import Platform

SCOPE = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"
NOW = time.time() - 3600.0   # an hour ago: the marks stamp real time and must land after the seed

CONFIG = {
    "whatsapp_config": {"whitelist": [], "reply_window_hours": 72},
    "telegram_config": {"bot_token": "t", "verified": True, "whitelist": [], "relay_whitelist": []},
    "discord_config": {"verified": True, "admin_user_id": "42"},
}


def _request(scope=SCOPE, username="alice"):
    return SimpleNamespace(state=SimpleNamespace(user={"username": username, "user_scope_id": scope}))


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    import vaf.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod.Config, "get", classmethod(lambda cls, key, default=None: CONFIG.get(key, default)))
    import vaf.core.contacts_store as contacts
    monkeypatch.setattr(contacts, "front_office_endpoints",
                        lambda username=None, user_scope_id=None, channel="whatsapp": set())
    monkeypatch.setattr(contacts, "is_local_admin_caller", lambda username, user_scope_id: False)
    monkeypatch.setattr(contacts, "get_contact_name_by_phone", lambda phone, username=None, user_scope_id=None: None)
    signals = []
    import vaf.core.web_interface as wi
    monkeypatch.setattr(wi, "notify_inbox_changed", lambda scope: signals.append(scope))
    store._reset_announce_state()   # cancels the timers an earlier test left behind; the dicts stay the module's own
    import vaf.core.session as session_mod
    monkeypatch.setattr(session_mod, "_room_rows", lambda scope: [])
    import vaf.core.whatsapp_auth as wa_auth
    monkeypatch.setattr(wa_auth, "whatsapp_auth_exists", lambda username: username == "alice")
    import vaf.api.whatsapp_bridge as wa
    monkeypatch.setattr(wa, "is_bridge_running", lambda: True)
    monkeypatch.setattr(wa, "get_whatsapp_chats", lambda *a, **k: pytest.fail("a GET asked the bridge"))
    import vaf.api.telegram_bridge as tg
    monkeypatch.setattr(tg, "is_bridge_running", lambda: False)
    return signals


def _msg(chat, body, direction="in", ts=NOW, sender=None, channel="whatsapp", message_id=None):
    store.append_message("alice", chat, body, direction=direction, ts=ts, sender_jid=sender, channel=channel,
                         user_scope_id=SCOPE, message_id=message_id)


def _list(request=None, **kw):
    return asyncio.run(ir.list_inbox(request or _request(), **kw))


def test_the_list_carries_rows_counts_and_status_without_asking_a_bridge(world):
    _msg("+491700000042", "hallo", ts=NOW - 10, message_id="a1")
    _msg("7", "hi", ts=NOW - 5, channel="telegram", message_id="a2")
    out = _list()
    assert [r["key"] for r in out["rows"]] == ["telegram:7", "whatsapp:+491700000042"]
    assert out["counts"]["waits"] == 2 and out["counts"]["unread"] == 2
    assert out["status"]["whatsapp"] == {"linked": True, "running": True}
    assert out["status"]["telegram"] == {"configured": True, "running": False}
    assert out["status"]["discord"] == {"configured": False, "running": False}, "not the local admin"
    assert out["status"]["mail"] == {"accounts": 0, "last_sync_at": None}
    only = _list(channel="whatsapp", view="waits")
    assert [r["key"] for r in only["rows"]] == ["whatsapp:+491700000042"] and only["channels"] == ["whatsapp"]
    assert [r["key"] for r in _list(channel="telegram,whatsapp", limit=1)["rows"]] == ["telegram:7"]
    with pytest.raises(HTTPException) as e:
        _list(channel="fax")
    assert e.value.status_code == 400


def test_the_summary_counts_waits_and_unread_for_the_badge(world):
    _msg("+491700000042", "hallo", ts=NOW - 10, message_id="b1")
    _msg("+491700000042", "answer", direction="out", ts=NOW - 5, sender="agent", message_id="b2")
    _msg("+491700000050", "moin", ts=NOW - 3, message_id="b3")
    out = asyncio.run(ir.inbox_summary(_request()))
    assert (out["waits"], out["unread"], out["all"], out["agent"]) == (1, 2, 2, 1)
    assert out["waits_per_channel"] == {"whatsapp": 1, "telegram": 0, "discord": 0, "mail": 0, "room": 0}
    assert out["per_channel"]["whatsapp"] == 2 and out["stored_per_channel"]["whatsapp"] == 2
    # The rail's toggles reach the summary: with done rows shown, the person's own reply counts too.
    _msg("+491700000060", "bye", direction="out", ts=NOW - 2, sender=store.OWNER_SENDER, message_id="b4")
    assert asyncio.run(ir.inbox_summary(_request()))["all"] == 2
    assert asyncio.run(ir.inbox_summary(_request(), done=True))["all"] == 3


def test_marks_all_reads_the_selection_and_refuses_an_unknown_channel(world):
    _msg("+491700000042", "wann?", ts=NOW - 20, message_id="a1")
    _msg("7", "und du?", ts=NOW - 10, message_id="a2", channel="telegram")
    out = asyncio.run(ir.inbox_marks_all(_request(), {"channels": ["whatsapp"]}))
    assert out["ok"] is True and out["moved"]["whatsapp"] == 1 and "telegram" not in out["moved"]
    rows = {r["key"]: r for r in _list()["rows"]}
    assert rows["whatsapp:+491700000042"]["unread"] == 0 and rows["telegram:7"]["unread"] == 1
    out = asyncio.run(ir.inbox_marks_all(_request(), {}))
    assert out["moved"]["telegram"] == 1 and _list()["counts"]["unread"] == 0 and _list()["counts"]["waits"] == 0
    with pytest.raises(HTTPException) as e:
        asyncio.run(ir.inbox_marks_all(_request(), {"channels": ["fax"]}))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException):
        asyncio.run(ir.inbox_marks_all(_request(), {"channels": 7}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(ir.inbox_marks_all(_request(), {"channels": []}))
    assert e.value.detail == "no channel"
    assert asyncio.run(ir.inbox_marks_all(_request(), {"channels": "telegram", "groups": "false"}))["moved"] == {"telegram": 0}, "the string false reads as false"
    assert set(asyncio.run(ir.inbox_marks_all(_request(), {"channels": ["all"]}))["moved"]) >= {"whatsapp", "telegram"}, "all inside a list is the string form"


def test_marks_round_trip_seen_lifts_unread_done_hides_and_an_unknown_rooms_seen_is_refused(world):
    _msg("+491700000042", "hallo", ts=NOW - 10, message_id="c1")
    asyncio.run(ir.inbox_marks(_request(), {"channel": "whatsapp", "id": "+491700000042", "seen": True}))
    row = _list()["rows"][0]
    assert row["unread"] == 0 and row["waits"] is False, "opening a chat reads it, and a read chat no longer waits for you"
    out = asyncio.run(ir.inbox_marks(_request(), {"channel": "whatsapp", "id": "+491700000042", "done": True}))
    assert out["ok"] is True and out["done"] is True
    assert _list()["rows"] == []
    assert _list(done=True)["rows"][0]["done"] is True
    asyncio.run(ir.inbox_marks(_request(), {"channel": "whatsapp", "id": "+491700000042", "done": False}))
    assert _list()["rows"][0]["done"] is False and _list()["rows"][0]["waits"] is False, "reopened, and still read"
    with pytest.raises(HTTPException) as e:
        asyncio.run(ir.inbox_marks(_request(), {"channel": "room", "id": "r1", "seen": True}))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(ir.inbox_marks(_request(), {"channel": "whatsapp", "id": "+491700000042"}))
    assert e.value.status_code == 400


def test_the_mail_seen_mark_announces_the_change_itself(world, monkeypatch):
    import vaf.core.inbox as core_inbox
    calls = []
    monkeypatch.setattr(core_inbox, "mark_conversation",
                        lambda *a, **k: calls.append((a, k)) or {"channel": "mail", "id": "3", "seen": True})
    asyncio.run(ir.inbox_marks(_request(), {"channel": "mail", "id": "3", "seen": True}))
    assert world == [SCOPE] and calls[0][0] == ("alice", SCOPE, "mail", "3") and calls[0][1] == {"seen": True, "done": None}


def test_history_is_the_pane_shape_and_another_scope_sees_nothing(world):
    _msg("+491700000042", "hallo", ts=NOW - 10, message_id="d1")
    _msg("+491700000042", "hi back", direction="out", ts=NOW - 5, sender="agent", message_id="d2")
    out = asyncio.run(ir.inbox_history(_request(), channel="whatsapp", id="+491700000042"))
    assert [(m["role"], m["sender"], m["content"]) for m in out["messages"]] == \
        [("user", "them", "hallo"), ("assistant", "agent", "hi back")]
    assert set(out["messages"][0]) == {"role", "content", "timestamp", "content_type", "sender"}
    other = _request(scope=OTHER, username="bob")
    assert asyncio.run(ir.inbox_history(other, channel="whatsapp", id="+491700000042"))["messages"] == []
    assert _list(other)["rows"] == [] and _list(other)["status"]["whatsapp"]["linked"] is False
    with pytest.raises(HTTPException) as e:
        asyncio.run(ir.inbox_history(_request(), channel="mail", id="nope"))
    assert e.value.status_code == 400


def test_no_bridge_round_trip_and_the_server_wires_the_routes_and_the_signal():
    src = Path(ir.__file__).read_text(encoding="utf-8")
    assert "get_whatsapp_chats" not in src and "get_connection_status" not in src and "wait_timeout" not in src
    assert src.count("asyncio.to_thread(") >= 5, "every store read leaves the event loop"
    server = (Path(__file__).resolve().parent.parent / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    assert "from vaf.api.inbox_routes import router as inbox_router" in server
    assert "_mail_sup.on_change(lambda scope, _account_id, _stats: notify_inbox_changed(scope))" in server
