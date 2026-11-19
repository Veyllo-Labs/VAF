# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""A notification for an account that arrives without a name stays that account's.

THE DEFECT, measured with a fake Node process and synthetic scopes: `send_to_main_messenger`
read every lane as `username or "admin"`. A non-admin's nameless notification (the room
report of a session that stores no name, an old reminder, the thinking nudge) therefore:
- read the main-messenger preference of the account named "admin";
- resolved the WhatsApp endpoint through the whitelist by scope OR by the name "admin", so
  the admin's registered number won when its entry came first, and that number was then
  SAVED as the non-admin's own endpoint;
- went into the queue as ("admin", jid, text) without its scope, and the process named
  "admin" took it as its own.
The admin's process sent the non-admin's reminder to the admin's own number.

THE RULE, three halves, each pinned here: the delivery resolves the name from the scope
(`resolve_caller_username`); a scoped endpoint lookup matches the scope only; and a queued
request carries its scope, which `_process_for` holds every process to, its own-name match
included. Plus the routes: a signed-in request without a scope or a name is refused
(`user_routes.request_account`) instead of being read as the local admin.
"""
import asyncio
import io
import json
import queue
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from vaf.api import whatsapp_bridge as wa
from vaf.core import messaging_connections as mc
from vaf.core.config import Config
from vaf.core.platform import Platform

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
ALICE_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
ADMIN_PHONE = "+4917000000001"
ADMIN_JID = "4917000000001@s.whatsapp.net"
NAMES = {ALICE_SCOPE: "alice"}


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Only the admin's Node process runs; the admin's number is registered and its endpoint
    is stored under the admin's name. Returns what the admin's process was asked."""
    state = {
        "local_admin_scope_id": ADMIN_SCOPE, "local_admin_username": "admin",
        "channel_ingress_policy": None,
        "whatsapp_config": {"enabled": True, "inbound_to_agent": True, "whitelist": [
            {"phone_number": ADMIN_PHONE, "user_scope_id": ADMIN_SCOPE, "vaf_username": "admin"}]},
    }
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr(mc, "_ENDPOINTS_FILE", tmp_path / "data" / "messaging_endpoints.json")
    monkeypatch.setattr("vaf.core.config.scope_id_for_username",
                        lambda name: {"admin": ADMIN_SCOPE, "alice": ALICE_SCOPE}.get(str(name or "")))
    # The account directory: the lookup `resolve_caller_username` makes for a nameless scope.
    monkeypatch.setattr("vaf.core.thinking_mode._resolve_username_for_scope", lambda scope: NAMES.get(scope))
    written = io.StringIO()
    monkeypatch.setattr(wa, "_processes", {"admin": SimpleNamespace(stdin=written, poll=lambda: None)})
    monkeypatch.setattr(wa, "_scope_by_user", {"admin": ADMIN_SCOPE})
    monkeypatch.setattr(wa, "_append_chat_activity", lambda *a, **k: None)
    monkeypatch.setattr(wa, "_outgoing_queue", queue.Queue())
    mc.save_whatsapp_chat_jid(ADMIN_SCOPE, "admin", ADMIN_JID)
    return written


def _drain():
    wa._outgoing_queue.put(None)
    wa._sender_loop()


def _asked(written):
    return [json.loads(line) for line in written.getvalue().splitlines()]


# -- (b) the endpoint ------------------------------------------------------------------------

def test_a_scoped_lookup_never_resolves_or_saves_the_admins_number(world):
    """MUTATION: the whitelist matches the name next to the scope again, or the endpoint
    lookup reads `whatsapp_by_username["admin"]` for a nameless scoped caller."""
    assert mc.get_whatsapp_chat_jid_from_whitelist(ALICE_SCOPE, None) is None
    assert mc.get_whatsapp_chat_jid_from_whitelist(ALICE_SCOPE, "admin") is None
    assert mc.get_whatsapp_chat_jid(ALICE_SCOPE, None) is None
    assert mc.get_whatsapp_chat_jid(ALICE_SCOPE, "alice") is None, "a wrong endpoint was saved for her"
    # The legacy lanes keep working: the admin by scope, and a caller with no scope at all.
    assert mc.get_whatsapp_chat_jid(ADMIN_SCOPE, None) == ADMIN_JID
    assert mc.get_whatsapp_chat_jid_from_whitelist(None, "admin") == ADMIN_JID


# -- (c) the process -------------------------------------------------------------------------

def test_a_queued_reply_for_one_scope_never_rides_another_scopes_process(world):
    """The reply lane under the default name. MUTATION: `_process_for` takes the own-name
    process without comparing its scope."""
    assert wa._enqueue_reply("admin", ADMIN_JID, "alice's private reminder", user_scope_id=ALICE_SCOPE) is True
    assert wa._enqueue_reply("admin", ADMIN_JID, "the admin's own reminder", user_scope_id=ADMIN_SCOPE) is True
    _drain()
    assert [c["text"] for c in _asked(world)] == ["the admin's own reminder"]


def test_a_confirmed_send_carries_its_scope_on_both_paths(world, monkeypatch):
    """In process: the queue item names its scope. Over the file IPC (where a request for
    another scope's process ends up): the request names it, and the sender loop refuses it.
    MUTATION: drop `user_scope_id` from the queue item, the request file or its dequeue."""
    results = {}
    monkeypatch.setattr(wa, "_complete_send_request", lambda req_id, ok, err=None, **k: results.__setitem__(req_id, (ok, err)))
    monkeypatch.setattr(wa, "_pending_sends", {})
    out = wa.send_whatsapp_with_confirmation("admin", ADMIN_JID, "hi", timeout=0.05, allow_contact_send=True,
                                             user_scope_id=ADMIN_SCOPE)
    assert "No delivery confirmation" in out
    assert wa._outgoing_queue.get_nowait()[8] == ADMIN_SCOPE

    monkeypatch.setattr(wa, "_outgoing_queue", None)
    monkeypatch.setattr(wa, "_read_bridge_state", lambda: {"running": True, "usernames": ["admin"]})
    monkeypatch.setattr(wa, "_wait_for_external_send_result", lambda *a, **k: "Message sent via WhatsApp.")
    wa.send_whatsapp_with_confirmation("admin", ADMIN_JID, "hi", allow_contact_send=True, user_scope_id=ALICE_SCOPE)
    item = wa._dequeue_external_send_request()
    assert item[8] == ALICE_SCOPE
    monkeypatch.setattr(wa, "_outgoing_queue", queue.Queue())
    wa._outgoing_queue.put(item)
    _drain()
    assert _asked(world) == [] and list(results.values()) == [(False, wa.NO_PROCESS_FOR_ACCOUNT)]


# -- (a) the delivery ------------------------------------------------------------------------

def test_a_nameless_notification_is_delivered_as_its_own_account(world, monkeypatch):
    """The measured delivery end to end. MUTATION: `uname = username or "admin"` in
    `send_to_main_messenger`."""
    from vaf.core import whatsapp_reply
    monkeypatch.setattr(whatsapp_reply, "_send_callback", wa._enqueue_reply)
    asked = []
    monkeypatch.setattr(mc, "get_messaging_connections",
                        lambda username=None, user_scope_id=None: asked.append(username) or {"main_messenger": "whatsapp"})
    sent, channel = mc.send_to_main_messenger(ALICE_SCOPE, None, "alice's private reminder")
    _drain()
    assert asked == ["alice"], "the preference of another account was read"
    assert (sent, channel) == (False, None), "Alice has no number of her own, so nothing goes out"
    assert _asked(world) == []
    assert mc.get_whatsapp_chat_jid(ALICE_SCOPE, "alice") is None
    # Her own number registered: it goes to her number, through nobody else's process.
    world_cfg = Config.get("whatsapp_config")
    world_cfg["whitelist"].append({"phone_number": "+4917000000002", "user_scope_id": ALICE_SCOPE, "vaf_username": "alice"})
    mc.send_to_main_messenger(ALICE_SCOPE, None, "alice's private reminder")
    _drain()
    assert _asked(world) == []


# -- the callers that named nobody -----------------------------------------------------------

def test_an_old_reminder_without_a_name_is_delivered_as_its_scope(world, monkeypatch):
    from vaf.core import reminders
    seen = []
    monkeypatch.setattr(mc, "send_to_main_messenger", lambda scope, username, text, **k: seen.append(username) or (True, "whatsapp"))
    reminders._deliver({"user_scope_id": ALICE_SCOPE, "message": "dentist"})
    assert seen == ["alice"]


def test_a_speaker_question_without_a_name_is_delivered_as_its_scope(world, monkeypatch):
    from vaf.core import speaker_confirm as sc
    seen = []
    monkeypatch.setattr(mc, "send_to_main_messenger",
                        lambda scope, username, text, file_path=None, record=True: seen.append(username) or (True, "whatsapp"))
    monkeypatch.setattr(sc, "_emit_web_card", lambda rec, q: None)
    monkeypatch.setattr(sc, "is_enabled", lambda: True)
    monkeypatch.setattr(sc, "_store_dir", lambda: Platform.data_dir() / "pending")
    (Platform.data_dir() / "pending").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("vaf.core.session.get_user_projects_root", lambda scope: Platform.data_dir() / "p" / scope)
    rec = sc.maybe_request_confirmation(ALICE_SCOPE, None, b"\x00" * 400, {"label": "unsure", "score": 0.57},
                                        session_id="s1")
    assert rec is not None and rec["username"] == "alice" and seen == ["alice"]


# -- the routes ------------------------------------------------------------------------------

def _req(user):
    return SimpleNamespace(state=SimpleNamespace(user=user))


@pytest.mark.parametrize("user", [
    {"role": "user", "user_scope_id": "", "username": "dave"},
    {"role": "user", "user_scope_id": ALICE_SCOPE, "username": ""},
], ids=["no-scope", "no-name"])
def test_a_sign_in_without_an_account_is_refused_not_read_as_the_admin(world, monkeypatch, user):
    """`/send` would go out through the admin's process, `/dashboard`, `/chat-messages` and
    `/composer` would read the admin's chats. MUTATION: `whatsapp_routes.get_current_vaf_user` lenient
    again, or Telegram's pairing read through its lenient reader."""
    from vaf.api import telegram_routes, whatsapp_routes as routes
    from vaf.api.user_routes import UNLINKED_SIGN_IN
    monkeypatch.setattr(wa, "is_bridge_running", lambda: True)
    monkeypatch.setattr("vaf.core.composer_lane.settings", lambda: {"enabled": True})
    calls = [
        lambda r: routes.whatsapp_composer(r, routes.ComposerRequest(chat_id=ADMIN_PHONE)),
        lambda r: routes.send_whatsapp_message(r, routes.SendRequest(chat_id=ADMIN_PHONE, text="hi")),
        lambda r: routes.get_whatsapp_dashboard(r),
        lambda r: routes.get_whatsapp_chat_messages(r, chat_id=ADMIN_PHONE),
        lambda r: telegram_routes.start_pairing(r),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as err:
            asyncio.run(call(_req(user)))
        assert (err.value.status_code, err.value.detail) == (400, UNLINKED_SIGN_IN)
    assert _asked(world) == []


def test_the_desktop_without_a_sign_in_is_the_local_admin(world):
    from vaf.api.user_routes import request_account
    assert request_account(SimpleNamespace(state=SimpleNamespace())) == {"user_scope_id": ADMIN_SCOPE,
                                                                        "username": "admin"}
    assert request_account(_req({"user_scope_id": ALICE_SCOPE, "username": "alice", "role": "user"})) == {
        "user_scope_id": ALICE_SCOPE, "username": "alice"}
