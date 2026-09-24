# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Whose messenger lane is on, and how an account pairs its own Telegram.

Measured on the code before this change:
- The per-account Telegram switch was stored and shown, and the bot answered every paired
  account regardless: an account that turned Telegram off was still answered, and VAF kept
  writing to it there.
- Enforcing it by scope alone would have cut off a second admin: an admin's save writes the
  bot's global switch, never a per-account one.
- A second admin's WhatsApp switch replaced the GLOBAL `whatsapp_config.enabled`, which is
  the local admin's own number: turning theirs off turned the local admin's off, and their
  own bridge never started.
- An account that was not an admin had no way to pair its own Telegram: the only way was the
  setup wizard, which needs the bot token and is an admin's step.
"""
import asyncio
import json

import pytest

from vaf.core.config import Config

ADMIN_SCOPE = "11111111-1111-1111-1111-111111111111"
SECOND_ADMIN = "33333333-3333-3333-3333-333333333333"
ALICE = "aaaaaaaa-0000-0000-0000-000000000001"
BOB = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"local_admin_scope_id": ADMIN_SCOPE}), encoding="utf-8")
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    from vaf.core import tool_dispatch
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", lambda: [
        {"username": "carol", "user_scope_id": SECOND_ADMIN, "active": True, "role": "admin"},
        {"username": "alice", "user_scope_id": ALICE, "active": True, "role": "user"},
        {"username": "bob", "user_scope_id": BOB, "active": True, "role": "user"},
    ])

    def seed(**blocks):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(blocks)
        path.write_text(json.dumps(data), encoding="utf-8")

    def on_disk():
        return json.loads(path.read_text(encoding="utf-8"))

    return type("F", (), {"seed": staticmethod(seed), "on_disk": staticmethod(on_disk)})


# -- one lane rule ------------------------------------------------------------------------

@pytest.mark.parametrize("bot_on", [True, False])
def test_the_telegram_lane_is_the_bot_and_the_accounts_own_switch(cfg, bot_on):
    from vaf.core.messaging_connections import channel_enabled_for_scope as lane
    cfg.seed(telegram_config={"enabled": bot_on}, connection_enabled_by_scope={ALICE: {"telegram": True}})
    assert lane("telegram", ADMIN_SCOPE) is bot_on, "the local admin's lane is the bot"
    assert lane("telegram", None) is bot_on, "no scope is the local admin's lane"
    assert lane("telegram", SECOND_ADMIN) is bot_on, "a second admin rides the bot, with no switch of their own"
    assert lane("telegram", ALICE) is bot_on, "Alice switched hers on; nothing is on while the bot is off"
    assert lane("telegram", BOB) is False, "Bob never switched his on"


def test_a_whatsapp_switch_is_each_accounts_own(cfg):
    from vaf.core.messaging_connections import channel_enabled_for_scope as lane
    cfg.seed(whatsapp_config={"enabled": True}, connection_enabled_by_scope={SECOND_ADMIN: {"whatsapp": False}})
    assert lane("whatsapp", ADMIN_SCOPE) is True
    assert lane("whatsapp", SECOND_ADMIN) is False, "the global switch is the local admin's number"


def test_nobody_but_the_local_admin_has_a_discord_lane(cfg):
    from vaf.core.messaging_connections import channel_enabled_for_scope as lane
    cfg.seed(discord_config={"enabled": True}, connection_enabled_by_scope={ALICE: {"discord": True}})
    assert lane("discord", ADMIN_SCOPE) is True
    assert lane("discord", ALICE) is False and lane("discord", SECOND_ADMIN) is False


def test_the_directory_carries_the_role_and_nothing_else_makes_an_admin(cfg, monkeypatch):
    from vaf.core import tool_dispatch
    from vaf.core.config import is_admin_account
    assert is_admin_account(SECOND_ADMIN) is True and is_admin_account(ALICE) is False
    assert is_admin_account(ADMIN_SCOPE) is True, "the owner without a lookup"
    assert is_admin_account("99999999-0000-0000-0000-000000000009") is False, "unknown"
    monkeypatch.setattr(tool_dispatch, "_account_directory_resolver", None)
    assert is_admin_account(SECOND_ADMIN) is False, "no directory: the restrictive answer"


# -- the switch is enforced ----------------------------------------------------------------

def _paired(*scopes):
    return {"enabled": True, "verified": True,
            "whitelist": [{"telegram_user_id": str(i), "user_scope_id": s, "vaf_username": f"u{i}"}
                          for i, s in enumerate(scopes, start=1)]}


def test_the_bot_answers_a_paired_account_only_while_its_switch_is_on(cfg):
    from vaf.api import telegram_bridge as tb
    cfg.seed(telegram_config=_paired(ALICE, SECOND_ADMIN, ADMIN_SCOPE))
    assert tb._resolve_telegram_user("1")[0] is None, "Alice's switch is off"
    assert tb._resolve_telegram_user("2")[0]["user_scope_id"] == SECOND_ADMIN, "a second admin is answered"
    assert tb._resolve_telegram_user("3")[0]["user_scope_id"] == ADMIN_SCOPE
    cfg.seed(connection_enabled_by_scope={ALICE: {"telegram": True}})
    assert tb._resolve_telegram_user("1")[0]["user_scope_id"] == ALICE


def test_nothing_is_sent_to_a_switched_off_account_but_its_chat_can_still_be_read(cfg):
    from vaf.core import messaging_connections as mc
    cfg.seed(telegram_config=_paired(ALICE))
    assert mc.get_telegram_chat_id(ALICE, "u1") is None
    assert mc.telegram_chat_id_of(ALICE, "u1") == "1", "reading is not reaching them"
    cfg.seed(connection_enabled_by_scope={ALICE: {"telegram": True}})
    assert mc.get_telegram_chat_id(ALICE, "u1") == "1"


# -- a second admin's WhatsApp switch is theirs -------------------------------------------

def test_a_second_admins_whatsapp_switch_never_touches_the_local_admins(cfg):
    existing = {"whatsapp_config": {"enabled": True, "whitelist": []}}
    body = Config.split_connection_toggles(existing, {"whatsapp_config": {"enabled": False, "whitelist": []}},
                                           SECOND_ADMIN, is_admin=True)
    assert body["whatsapp_config"]["enabled"] is True, "the local admin's number stays on"
    assert existing["connection_enabled_by_scope"] == {SECOND_ADMIN: {"whatsapp": False}}
    shown = Config.config_for_user({**existing, **body}, SECOND_ADMIN, "admin")
    assert shown["whatsapp_config"]["enabled"] is False, "their switch shows their lane"
    assert Config.config_for_user({**existing, **body}, ADMIN_SCOPE, "admin")["whatsapp_config"]["enabled"] is True


def test_a_second_admins_telegram_switch_is_still_the_bot(cfg):
    existing = {"telegram_config": {"enabled": True}}
    body = Config.split_connection_toggles(existing, {"telegram_config": {"enabled": False}}, SECOND_ADMIN, is_admin=True)
    assert body["telegram_config"]["enabled"] is False and "connection_enabled_by_scope" not in existing


def test_both_save_paths_run_the_one_step():
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    routes = (repo / "vaf" / "api" / "config_routes.py").read_text(encoding="utf-8")
    ws = (repo / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    for src in (routes, ws):
        assert "Config.split_connection_toggles(" in src
        assert "extract_connection_toggles_for_scope" not in src, "a second hand copy of the non-admin half"


# -- pairing one's own Telegram with the running bot --------------------------------------

def test_a_code_is_spent_by_its_first_use_and_a_new_one_withdraws_the_old(monkeypatch):
    from vaf.core import channel_pairing as cp
    monkeypatch.setattr(cp, "_codes", {})
    first = cp.issue_pairing_code("telegram", ALICE, "alice")
    second = cp.issue_pairing_code("telegram", ALICE, "alice")
    assert cp.redeem_pairing_code("telegram", first) is None, "withdrawn"
    assert cp.redeem_pairing_code("whatsapp", second) is None, "another channel's code"
    assert cp.redeem_pairing_code("telegram", second) == (ALICE, "alice")
    assert cp.redeem_pairing_code("telegram", second) is None, "spent"
    third = cp.issue_pairing_code("telegram", BOB, "bob")
    monkeypatch.setattr(cp.time, "time", lambda: 10 ** 12)
    assert cp.redeem_pairing_code("telegram", third) is None, "expired"


def test_the_running_bot_pairs_the_sender_with_the_account_that_holds_the_code(cfg, monkeypatch):
    from vaf.api import telegram_bridge as tb
    from vaf.core import channel_pairing as cp
    monkeypatch.setattr(cp, "_codes", {})
    cfg.seed(telegram_config=_paired(ADMIN_SCOPE))
    assert tb._pair_with_code("4242", "alice_tg", "not-a-live-code") is None, "anything else goes on as before"

    code = cp.issue_pairing_code("telegram", ALICE, "alice")
    answer = tb._pair_with_code("4242", "alice_tg", code)
    assert "alice" in answer
    disk = cfg.on_disk()
    assert {"telegram_user_id": "4242", "telegram_username": "alice_tg", "user_scope_id": ALICE,
            "vaf_username": "alice"} in disk["telegram_config"]["whitelist"]
    assert disk["connection_enabled_by_scope"][ALICE]["telegram"] is True, "asking for a code is asking for the lane"
    assert tb._resolve_telegram_user("4242")[0]["user_scope_id"] == ALICE


def test_a_code_cannot_take_another_accounts_telegram(cfg, monkeypatch):
    from vaf.api import telegram_bridge as tb
    from vaf.core import channel_pairing as cp
    monkeypatch.setattr(cp, "_codes", {})
    cfg.seed(telegram_config=_paired(ADMIN_SCOPE))          # Telegram id "1" is the admin's
    code = cp.issue_pairing_code("telegram", BOB, "bob")
    assert "already paired with another" in tb._pair_with_code("1", None, code)
    assert [e["user_scope_id"] for e in cfg.on_disk()["telegram_config"]["whitelist"]] == [ADMIN_SCOPE]


def _client(who):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from vaf.api import telegram_routes
    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request, call_next):
        request.state.user = who
        return await call_next(request)

    app.include_router(telegram_routes.router)
    return TestClient(app)


def test_any_account_can_ask_for_its_own_code_and_sees_when_it_is_paired(cfg, monkeypatch):
    import vaf.api.telegram_bridge as tb
    import vaf.api.telegram_routes as tr
    from vaf.core import channel_pairing as cp
    from vaf.core import channel_secrets as cs
    monkeypatch.setattr(cp, "_codes", {})
    monkeypatch.setattr(tb, "is_bridge_running", lambda: True)
    monkeypatch.setattr(tr, "_get_bot_username", lambda: "vaf_bot")
    cfg.seed(telegram_config=_paired(ADMIN_SCOPE))
    cs.set_channel_secret("telegram", "bot_token", "123:" + "A" * 35)
    client = _client({"username": "alice", "role": "user", "user_scope_id": ALICE})

    out = client.post("/api/telegram/pair").json()
    assert out["link"] == f"https://t.me/vaf_bot?start={out['code']}" and out["command"] == f"/start {out['code']}"
    assert client.get("/api/telegram/pair").json() == {"paired": False, "pending": True}
    tb._pair_with_code("4242", None, out["code"])
    assert client.get("/api/telegram/pair").json() == {"paired": True, "pending": False}


def test_no_code_while_the_bot_is_not_running_or_for_an_account_without_a_scope(cfg, monkeypatch):
    import vaf.api.telegram_bridge as tb
    from vaf.core import channel_secrets as cs
    cfg.seed(telegram_config=_paired(ADMIN_SCOPE))
    cs.set_channel_secret("telegram", "bot_token", "123:" + "A" * 35)
    monkeypatch.setattr(tb, "is_bridge_running", lambda: False)
    assert _client({"username": "alice", "role": "user", "user_scope_id": ALICE}).post("/api/telegram/pair").status_code == 409
    monkeypatch.setattr(tb, "is_bridge_running", lambda: True)
    res = _client({"username": "x", "role": "user"}).post("/api/telegram/pair")
    assert res.status_code == 400, "never read as the local admin"


def test_the_card_offers_the_pairing_and_follows_it():
    """Source guard for the page half: the Telegram card of an account that is not paired
    asks for a code, shows the command and the link, and waits for the bot."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "web" / "components" / "connections"
           / "ConnectionsPanel.tsx").read_text(encoding="utf-8")
    assert "api('api/telegram/pair'), { method: 'POST'" in src
    assert "!sharedBots.telegram.paired" in src and "telegramPairing.command" in src
    assert "if (data?.paired)" in src
