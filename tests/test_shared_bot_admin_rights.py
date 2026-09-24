# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Telegram and Discord each run ONE bot for the whole installation; what another account
may do on it, measured on the code before this change:

- The setup routes answered any signed-in account. `POST /api/telegram/whitelist-add` took
  the Telegram id from the body and replaced an entry with that id whoever it belonged to,
  so an account that sent the admin's id became the owner of the admin's Telegram chat:
  their agent answered it, their window read it. The verification routes ran a bot on any
  token, and their state is one per process.
- `relay-whitelist-add` dropped an existing entry with the same id whoever owned it, so one
  account could take another's relay contact.
- `get_discord_user_id` returned the admin's Discord id for ANY identity, so another
  account's `send_discord` and `send_to_user` DMed the admin, and `read_discord_chat` read
  the admin's Discord conversation.
- Three admin checks disagreed about a second admin account (see
  test_policy_admin_matches_the_shared_rule.py): Telegram and WhatsApp knew only the local
  admin's scope, so a second admin's Telegram status read a switch nobody could set.
"""
import json

import pytest

from vaf.core.config import Config

ADMIN_SCOPE = "11111111-1111-1111-1111-111111111111"
SECOND_ADMIN = {"username": "carol", "role": "admin", "user_scope_id": "33333333-3333-3333-3333-333333333333"}
ALICE = {"username": "alice", "role": "user", "user_scope_id": "aaaaaaaa-0000-0000-0000-000000000001"}
BOB = {"username": "bob", "role": "user", "user_scope_id": "bbbbbbbb-0000-0000-0000-000000000002"}
ADMIN = {"username": "admin", "role": "admin", "user_scope_id": ADMIN_SCOPE}


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"local_admin_scope_id": ADMIN_SCOPE}), encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)

    def seed(**blocks):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(blocks)
        path.write_text(json.dumps(data), encoding="utf-8")

    def on_disk():
        return json.loads(path.read_text(encoding="utf-8"))

    return type("F", (), {"seed": staticmethod(seed), "on_disk": staticmethod(on_disk)})


def _client(channel, who):
    """The channel's router behind a signed-in `who` (a mutable one-key dict)."""
    import importlib

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request, call_next):
        request.state.user = who["user"]
        return await call_next(request)

    app.include_router(importlib.import_module(f"vaf.api.{channel}_routes").router)
    return TestClient(app)


# -- setting the bot up is an admin's ------------------------------------------------------

@pytest.mark.parametrize("channel,method,path,body", [
    ("telegram", "post", "start-verification", {"bot_token": "1:x", "verification_code": "123456"}),
    ("telegram", "get", "verification-status", None),
    ("telegram", "post", "whitelist-add", {"telegram_user_id": "4242"}),
    ("discord", "post", "start-verification", {"bot_token": "x", "verification_code": "123456"}),
    ("discord", "get", "verification-status", None),
])
def test_another_account_cannot_set_up_the_bot(config_file, channel, method, path, body):
    client = _client(channel, {"user": ALICE})
    call = getattr(client, method)
    res = call(f"/api/{channel}/{path}", json=body) if body is not None else call(f"/api/{channel}/{path}")
    assert res.status_code == 403


def test_another_account_cannot_become_the_owner_of_the_admins_telegram_chat(config_file):
    """THE takeover, measured before the fix: the same request replaced the admin's entry."""
    owner = [{"telegram_user_id": "4242", "user_scope_id": ADMIN_SCOPE, "vaf_username": "admin"}]
    config_file.seed(telegram_config={"verified": True, "whitelist": owner})

    res = _client("telegram", {"user": BOB}).post("/api/telegram/whitelist-add", json={"telegram_user_id": "4242"})

    assert res.status_code == 403
    assert config_file.on_disk()["telegram_config"]["whitelist"] == owner


def test_the_verification_result_is_not_readable_by_another_account(config_file, monkeypatch):
    import vaf.api.telegram_routes as tr
    monkeypatch.setattr(tr, "_verification_state", {"verified": True, "telegram_user_id": "4242",
                                                    "telegram_username": "owner", "error": None})
    who = {"user": ALICE}
    client = _client("telegram", who)
    assert client.get("/api/telegram/verification-status").status_code == 403
    who["user"] = ADMIN
    assert client.get("/api/telegram/verification-status").json()["telegram_user_id"] == "4242"


# -- a relay contact belongs to the account that added it ----------------------------------

def test_another_account_cannot_take_a_relay_contact_over(config_file):
    alices = {"telegram_user_id": "777", "telegram_username": None,
              "user_scope_id": ALICE["user_scope_id"], "vaf_username": "alice"}
    config_file.seed(telegram_config={"relay_whitelist": [alices]})
    who = {"user": BOB}
    client = _client("telegram", who)

    assert client.post("/api/telegram/relay-whitelist-add", json={"telegram_user_id": "777"}).status_code == 409
    assert config_file.on_disk()["telegram_config"]["relay_whitelist"] == [alices]

    assert client.post("/api/telegram/relay-whitelist-add", json={"telegram_user_id": "888"}).status_code == 200
    who["user"] = ALICE
    assert client.post("/api/telegram/relay-whitelist-add", json={"telegram_user_id": "777"}).status_code == 200
    owners = {e["telegram_user_id"]: e["vaf_username"] for e in config_file.on_disk()["telegram_config"]["relay_whitelist"]}
    assert owners == {"777": "alice", "888": "bob"}


# -- one answer to "is this an admin" ------------------------------------------------------

def test_a_second_admin_reads_the_bot_as_an_admin(config_file):
    """Telegram's own check knew only the local admin's scope, so a second admin's status
    read a per-account switch that an admin's save never writes: always off."""
    config_file.seed(telegram_config={"enabled": True, "verified": True, "whitelist": []})
    who = {"user": SECOND_ADMIN}
    status = _client("telegram", who).get("/api/telegram/status").json()
    assert status["enabled"] is True
    who["user"] = ALICE
    assert _client("telegram", who).get("/api/telegram/status").json()["enabled"] is False


def test_an_account_without_a_scope_is_not_made_an_admin(config_file):
    """The route-level resolver fills a missing scope with the local admin's; the admin check
    must read the request as it was authenticated instead."""
    from vaf.api.user_routes import caller_is_admin

    class _Req:
        state = type("S", (), {"user": {"username": "x", "role": "user"}})()

    assert caller_is_admin(_Req()) is False


# -- the Discord lane is the local admin's -------------------------------------------------

@pytest.fixture
def discord_on(config_file):
    config_file.seed(discord_config={"enabled": True, "verified": True, "admin_user_id": "9001"})
    from vaf.core import channel_secrets as cs
    cs.set_channel_secret("discord", "bot_token", "tok")
    return config_file


def test_only_the_local_admin_is_reached_on_discord(discord_on):
    from vaf.core.messaging_connections import get_discord_user_id, get_messaging_connections
    assert get_discord_user_id(ADMIN_SCOPE, "admin") == "9001"
    assert get_discord_user_id(None, "admin") == "9001", "the Discord bridge's own turns"
    for who in (ALICE, SECOND_ADMIN):
        assert get_discord_user_id(who["user_scope_id"], who["username"]) is None
        assert "discord" not in get_messaging_connections(username=who["username"],
                                                          user_scope_id=who["user_scope_id"])["available"]
    assert "discord" in get_messaging_connections(username="admin", user_scope_id=ADMIN_SCOPE)["available"]


def test_another_accounts_agent_neither_messages_nor_reads_the_admins_discord(discord_on, monkeypatch):
    import vaf.core.discord_history as dh
    import vaf.core.discord_send as ds
    from vaf.tools.read_discord_chat import ReadDiscordChatTool
    from vaf.tools.send_discord import SendDiscordTool

    sent, read = [], []
    monkeypatch.setattr(ds, "send_discord_dm", lambda *a, **k: sent.append(a) or True)
    monkeypatch.setattr(dh, "read_discord_session", lambda chat_id, limit=50: read.append(chat_id) or [])

    out = SendDiscordTool().run(message="hi", user_scope_id=ALICE["user_scope_id"], username="alice")
    assert "No Discord contact" in out and sent == []
    out = ReadDiscordChatTool().run(chat_id="9001", user_scope_id=ALICE["user_scope_id"], username="alice")
    assert "not connected for this account" in out and read == []


# -- the card's badge ----------------------------------------------------------------------

def test_the_card_reads_connected_only_with_the_switch_on_as_the_page_shows_it():
    """The status routes answer from the SAVED switch; a user's switch turned off and not
    saved yet read "Connected" until the next save. The badge is the shared bot's state and
    the switch as shown, on every path that sets it."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "web" / "components" / "connections"
           / "ConnectionsPanel.tsx").read_text(encoding="utf-8")
    helper = src[src.index("function sharedBotBadge"):]
    helper = helper[:helper.index("\n}\n")]
    assert "&& enabled ?" in helper, "the switch is part of the answer"
    check = src[src.index("const checkConnectionStatus"):src.index("const fetchFrontOffice")]
    for channel in ("discord", "telegram"):
        assert f"sharedBotBadge(bot, config.{channel}_config?.enabled === true)" in check
    toggle = src[src.index("const handleToggleConnection"):src.index("const handleDisconnect")]
    for channel in ("discord", "telegram"):
        assert f"sharedBotBadge(sharedBots.{channel}, enabled)" in toggle
