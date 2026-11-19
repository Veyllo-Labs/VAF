# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The mail account lists change only through the mail routes, and the per-account
connection switches only through their own save step and the pairings.

THE DEFECT, measured on both save lanes (PATCH /api/config and the real WebSocket
`save_config`):
- Data loss with nobody attacking: the Settings page sends the whole page back, and a
  non-admin's page holds a scoped view of the mail lists (`email_config` None,
  `email_config_by_user` empty, only their own `email_config_by_scope` entry). A non-admin
  changing only the language set the local admin's `email_config` to None and replaced
  every other account's entry.
- Credential theft: a body written by hand pointed another account's `imap_host` and
  `smtp_host` at a server of their choice, and the sync and the sender then logged in there
  with that account's stored password or token.
- `connection_enabled_by_scope`, the map of every account's channel switches, was merged as
  sent: a non-admin turned another account's WhatsApp lane on.

THE RULE: the mail lists are in `Config.ROUTE_OWNED_KEYS`, which both save lanes strip for
every caller (`Config.without_route_owned_keys`), `split_connection_toggles` drops
the switch map from every body, and a non-admin reads only their own switches.
Synthetic identities and a scratch config only.
"""
import copy
import inspect
import json
import threading
from types import SimpleNamespace

import pytest

from vaf.core.config import Config

TENANT_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
OTHER_SCOPE = "ab12cd34-0000-4000-8000-000000000002"
ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"

MAIL = {
    "email_config": {"accounts": [{"account_id": "owner@example.com", "email": "owner@example.com",
                                   "imap_host": "imap.example.com", "smtp_host": "smtp.example.com"}]},
    "email_config_by_scope": {
        TENANT_SCOPE: {"accounts": [{"account_id": "alice@example.com",
                                     "imap_host": "imap.example.org"}]},
        OTHER_SCOPE: {"accounts": [{"account_id": "bob@example.net",
                                    "imap_host": "imap.example.net",
                                    "smtp_host": "smtp.example.net"}]},
    },
    "email_config_by_user": {"carol": {"accounts": [{"account_id": "carol@example.com"}]}},
    "connection_enabled_by_scope": {OTHER_SCOPE: {"whatsapp": False, "telegram": False},
                                    TENANT_SCOPE: {"whatsapp": False}},
}
LISTS = ("email_config", "email_config_by_scope", "email_config_by_user",
         "connection_enabled_by_scope")

HOSTILE = {
    "email_config": {"accounts": [{"account_id": "owner@example.com",
                                   "imap_host": "mail.attacker.example",
                                   "smtp_host": "mail.attacker.example"}]},
    "email_config_by_scope": {OTHER_SCOPE: {"accounts": [{"account_id": "bob@example.net",
                                                          "imap_host": "mail.attacker.example",
                                                          "smtp_host": "mail.attacker.example"}]}},
    "email_config_by_user": {},
    "connection_enabled_by_scope": {OTHER_SCOPE: {"whatsapp": True, "telegram": True}},
    "tts_auto_speak": True,
}

NON_ADMIN = {"role": "user", "user_scope_id": TENANT_SCOPE, "username": "alice"}
ADMIN = {"role": "admin", "user_scope_id": ADMIN_SCOPE, "username": "admin"}


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """A synthetic config file Config reads and writes; returns a reader of the raw file."""
    from vaf.core import migrations

    path = tmp_path / "config.json"
    monkeypatch.setattr(Config, "CONFIG_FILE", path)
    monkeypatch.setattr(Config, "APP_DIR", tmp_path)
    monkeypatch.setattr(Config, "_filelock", None)

    def write(cfg: dict):
        path.write_text(json.dumps({"config_format_version": migrations.CONFIG_FORMAT_VERSION,
                                    **cfg}), encoding="utf-8")

    def raw() -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    write({**copy.deepcopy(MAIL), "tts_auto_speak": False, "default_language": "en"})
    return SimpleNamespace(write=write, raw=raw, path=path)


def _patch(body: dict, user: dict) -> dict:
    import asyncio

    from vaf.api import config_routes
    request = SimpleNamespace(state=SimpleNamespace(user=user))
    return asyncio.run(config_routes.patch_config(copy.deepcopy(body), request, user))


def _ws_save(monkeypatch, user: dict, body: dict) -> dict:
    """One `save_config` over a real `/ws` connection with a signed token; the answer."""
    import jwt
    from starlette.testclient import TestClient

    import vaf.auth.crypto as crypto
    from vaf.core.web_server import app

    monkeypatch.setattr(crypto, "get_jwt_secret", lambda: "s" * 32)
    token = jwt.encode({"sub": "7", "user_scope_id": user["user_scope_id"],
                        "username": user["username"], "role": user["role"]},
                       "s" * 32, algorithm="HS256")
    box = {}
    with TestClient(app, client=("127.0.0.1", 40000)).websocket_connect(f"/ws?token={token}") as ws:
        ws.send_text(json.dumps({"type": "save_config", "config": body}))

        def pump():
            try:
                for _ in range(100):
                    msg = ws.receive_json()
                    if msg.get("type") == "config_saved":
                        box["msg"] = msg
                        return
            except Exception as e:  # noqa: BLE001 - reported by the assert below
                box["error"] = e

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        reader.join(20)
    assert "msg" in box, f"no config_saved ({box.get('error')!r})"
    assert box["msg"].get("status") == "success", box["msg"]
    return box["msg"]


def _save(lane: str, monkeypatch, user: dict, body: dict) -> None:
    if lane == "patch":
        _patch(body, user)
    else:
        _ws_save(monkeypatch, user, body)


def _assert_lists_unchanged(stored, *, own_switches_saved: bool = False):
    """Every list byte for byte. With `own_switches_saved`, the saving account's own entry of
    the switch map may spell out the switches its page sent (all off, as they were): that is
    the save step recording that account's own `enabled`, the one write it is for."""
    raw = stored.raw()
    for key in LISTS:
        got, want = raw.get(key), MAIL[key]
        if key == "connection_enabled_by_scope" and own_switches_saved:
            got, want = dict(got), dict(want)
            assert not any(got.pop(TENANT_SCOPE).values()), raw[key]
            want.pop(TENANT_SCOPE)
        assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True), key


@pytest.mark.parametrize("lane", ["patch", "ws"])
@pytest.mark.parametrize("user", [NON_ADMIN, ADMIN], ids=["non_admin", "admin"])
def test_a_hand_written_body_cannot_rewrite_the_mail_lists(stored, monkeypatch, lane, user):
    """MUTATIONS: remove the strip in `patch_config` (patch cases red) or in the WebSocket
    lane (ws cases red); remove the pop in `split_connection_toggles`."""
    _save(lane, monkeypatch, user, HOSTILE)
    _assert_lists_unchanged(stored)
    assert stored.raw()["tts_auto_speak"] is True
    # What the mail sync and the sender would log in to.
    from vaf.core.sync_supervisor import collect_email_accounts
    assert "attacker" not in json.dumps([acc for _s, _u, acc in collect_email_accounts()])


@pytest.mark.parametrize("lane", ["patch", "ws"])
def test_a_non_admin_language_save_keeps_every_mail_list(stored, monkeypatch, lane):
    """The ordinary save: the page's whole view with one change. MUTATION: remove the strip
    in the lane under test."""
    view = Config.config_for_user(Config.load(), TENANT_SCOPE, "user")
    _save(lane, monkeypatch, NON_ADMIN, {**view, "default_language": "de"})
    _assert_lists_unchanged(stored, own_switches_saved=True)
    assert stored.raw()["default_language"] == "de"


@pytest.mark.parametrize("lane", ["patch", "ws"])
def test_an_admin_page_loaded_before_an_account_was_added_keeps_it(stored, monkeypatch, lane):
    """An admin's echo of a page older than a mail-route write. MUTATION: remove the strip in
    the lane under test."""
    view = Config.config_for_user(Config.load(), ADMIN_SCOPE, "admin")
    from vaf.core.email_accounts import save_email_config
    added = {"accounts": [*MAIL["email_config"]["accounts"], {"account_id": "new@example.com"}]}
    save_email_config(added, None, user_scope_id=None)
    _save(lane, monkeypatch, ADMIN, {**view, "default_language": "fr"})
    raw = stored.raw()
    assert raw["email_config"] == added
    assert raw["default_language"] == "fr"


def test_a_non_admin_cannot_turn_another_accounts_lane_on(stored):
    """MUTATION: remove the pop of `connection_enabled_by_scope` in `split_connection_toggles`."""
    from vaf.core.messaging_connections import channel_enabled_for_scope

    _patch({"connection_enabled_by_scope": {OTHER_SCOPE: {"whatsapp": True}}}, NON_ADMIN)
    assert channel_enabled_for_scope("whatsapp", OTHER_SCOPE) is False
    assert stored.raw()["connection_enabled_by_scope"] == MAIL["connection_enabled_by_scope"]


def test_the_own_switch_still_saves_and_survives_the_page_echo(stored):
    """The one way a non-admin changes their switch: the channel's `enabled` in a save. The
    page's own copy of the map rides along and used to overwrite it. MUTATION: remove the
    pop (the echoed map wins and the switch is lost)."""
    view = Config.config_for_user(Config.load(), TENANT_SCOPE, "user")
    body = {**view, "whatsapp_config": {**(view.get("whatsapp_config") or {}), "enabled": True}}
    _patch(body, NON_ADMIN)
    by_scope = stored.raw()["connection_enabled_by_scope"]
    assert by_scope[TENANT_SCOPE]["whatsapp"] is True
    assert by_scope[OTHER_SCOPE] == MAIL["connection_enabled_by_scope"][OTHER_SCOPE]


def test_a_non_admin_reads_only_their_own_switches():
    """MUTATION: delete the projection in `config_for_user`'s non-admin branch."""
    view = Config.config_for_user(copy.deepcopy(MAIL), TENANT_SCOPE, "user")
    assert view["connection_enabled_by_scope"] == {
        TENANT_SCOPE: MAIL["connection_enabled_by_scope"][TENANT_SCOPE]}
    assert Config.config_for_user(copy.deepcopy(MAIL), None, "user")[
        "connection_enabled_by_scope"] == {}
    admin = Config.config_for_user(copy.deepcopy(MAIL), ADMIN_SCOPE, "admin")
    assert admin["connection_enabled_by_scope"] == MAIL["connection_enabled_by_scope"]


def test_both_lanes_strip_the_mail_lists_before_the_shared_step():
    """By source as well, so a lane that moves the call behind the merge is caught."""
    from vaf.api import config_routes
    from vaf.core import web_server

    patch_src = inspect.getsource(config_routes.patch_config)
    assert "Config.without_route_owned_keys(" in patch_src
    assert patch_src.index("Config.without_route_owned_keys(") < patch_src.index(
        "Config.split_connection_toggles(")
    source = inspect.getsource(web_server)
    lane = source[source.index('elif type == "save_config":'):]
    lane = lane[:lane.index("Config.split_connection_toggles")]
    assert "new_config = Config.without_route_owned_keys(new_config, stored=existing)" in lane


def test_the_route_owned_strip_drops_the_mail_lists_and_leaves_other_keys_alone():
    body = {"language": "de"}
    assert Config.without_route_owned_keys(body, stored={}) is body
    mail = ("email_config", "email_config_by_scope", "email_config_by_user")
    assert Config.without_route_owned_keys({k: 1 for k in mail} | {"theme": "x"}, stored={}) == {
        "theme": "x"}
