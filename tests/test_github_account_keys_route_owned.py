# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The GitHub account lists change only through the GitHub routes, and each account reads
only its own.

THE DEFECT, measured on both save lanes (PATCH /api/config and the real WebSocket
`save_config`): `github_config` (the local admin's list) and `github_config_by_user` (every
other account's, by username) were merged as sent. A non-admin set `allow_write` on the
admin's GitHub account, the only gate in front of the tools that create issues and change
files, and emptied another account's list; and every account read every list, so every
GitHub login on the instance. The lists are in `Config.ROUTE_OWNED_KEYS` now (stripped from
every save, for every caller), and `config_for_user` gives a non-admin only their own entry.
Synthetic identities and a scratch config only.
"""
import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from vaf.core.config import Config

TENANT_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"

GITHUB = {
    "github_config": {"accounts": [{"account_id": "1001", "login": "admin-gh",
                                    "allow_write": False, "enabled": True}]},
    "github_config_by_user": {
        "alice": {"accounts": [{"account_id": "2002", "login": "alice-gh", "allow_write": False}]},
        "bob": {"accounts": [{"account_id": "3003", "login": "bob-gh", "allow_write": True}]},
    },
}
LISTS = tuple(GITHUB)
HOSTILE = {
    "github_config": {"accounts": [{"account_id": "1001", "login": "admin-gh",
                                    "allow_write": True, "enabled": True}]},
    "github_config_by_user": {"bob": {"accounts": []}},
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

    def raw() -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    path.write_text(json.dumps({"config_format_version": migrations.CONFIG_FORMAT_VERSION,
                                "local_admin_username": "admin", "tts_auto_speak": False,
                                "default_language": "en", **copy.deepcopy(GITHUB)}),
                    encoding="utf-8")
    return SimpleNamespace(raw=raw, path=path)


def _request(user: dict):
    return SimpleNamespace(state=SimpleNamespace(user=user))


def _patch(body: dict, user: dict) -> dict:
    from vaf.api import config_routes
    return asyncio.run(config_routes.patch_config(copy.deepcopy(body), _request(user), user))


def _ws(monkeypatch, user: dict, message: dict, answer_type: str) -> dict:
    """One message over a real `/ws` connection with a signed token; the first answer of
    ``answer_type``."""
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
        ws.send_text(json.dumps(message))

        def pump():
            try:
                for _ in range(100):
                    msg = ws.receive_json()
                    if msg.get("type") == answer_type:
                        box["msg"] = msg
                        return
            except Exception as e:  # noqa: BLE001 - reported by the assert below
                box["error"] = e

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        reader.join(20)
    assert "msg" in box, f"no {answer_type} ({box.get('error')!r})"
    return box["msg"]


def _save(lane: str, monkeypatch, user: dict, body: dict) -> None:
    if lane == "patch":
        _patch(body, user)
    else:
        msg = _ws(monkeypatch, user, {"type": "save_config", "config": body}, "config_saved")
        assert msg.get("status") == "success", msg


def _assert_lists_unchanged(stored):
    raw = stored.raw()
    for key in LISTS:
        assert json.dumps(raw.get(key), sort_keys=True) == json.dumps(GITHUB[key], sort_keys=True), key


@pytest.mark.parametrize("lane", ["patch", "ws"])
@pytest.mark.parametrize("user", [NON_ADMIN, ADMIN], ids=["non_admin", "admin"])
def test_a_hand_written_body_cannot_rewrite_the_github_lists(stored, monkeypatch, lane, user):
    """MUTATION: take the two GitHub keys out of `Config.ROUTE_OWNED_KEYS`."""
    _save(lane, monkeypatch, user, HOSTILE)
    _assert_lists_unchanged(stored)
    assert stored.raw()["tts_auto_speak"] is True
    # The gate in front of `github_create_issue` and `github_update_file`.
    from vaf.tools.github_tools import _get_github_account_for_user
    assert _get_github_account_for_user(None)["allow_write"] is False


@pytest.mark.parametrize("lane", ["patch", "ws"])
def test_a_non_admin_language_save_keeps_every_github_list(stored, monkeypatch, lane):
    """The page's whole view with one change. MUTATION: take the keys out of the table."""
    view = Config.config_for_user(Config.load(), TENANT_SCOPE, "user", username="alice")
    _save(lane, monkeypatch, NON_ADMIN, {**view, "default_language": "de"})
    _assert_lists_unchanged(stored)
    assert stored.raw()["default_language"] == "de"


@pytest.mark.parametrize("lane", ["patch", "ws"])
def test_an_admin_page_loaded_before_a_connect_keeps_it(stored, monkeypatch, lane):
    """MUTATION: take the keys out of the table."""
    view = Config.config_for_user(Config.load(), ADMIN_SCOPE, "admin", username="admin")
    from vaf.api.github_routes import _save_github_config
    added = {"accounts": [*GITHUB["github_config"]["accounts"],
                          {"account_id": "4004", "login": "admin-gh-2", "allow_write": False}]}
    _save_github_config(added, None)
    _save(lane, monkeypatch, ADMIN, {**view, "default_language": "fr"})
    raw = stored.raw()
    assert raw["github_config"] == added
    assert raw["default_language"] == "fr"


def test_a_non_admin_reads_only_their_own_github_entry():
    """MUTATION: delete the GitHub projection in `config_for_user`'s non-admin branch."""
    view = Config.config_for_user(copy.deepcopy(GITHUB), TENANT_SCOPE, "user", username="alice")
    assert view["github_config"] is None
    assert view["github_config_by_user"] == {"alice": GITHUB["github_config_by_user"]["alice"]}
    assert Config.config_for_user(copy.deepcopy(GITHUB), TENANT_SCOPE, "user")[
        "github_config_by_user"] == {}
    assert Config.config_for_user(copy.deepcopy(GITHUB), TENANT_SCOPE, "user", username="carol")[
        "github_config_by_user"] == {}
    admin = Config.config_for_user(copy.deepcopy(GITHUB), ADMIN_SCOPE, "admin", username="admin")
    assert admin["github_config"] == GITHUB["github_config"]
    assert admin["github_config_by_user"] == GITHUB["github_config_by_user"]


def test_every_read_lane_names_the_caller(stored, monkeypatch):
    """GET /api/config, the PATCH answer and the WebSocket `get_config` each pass the
    username, so the projection is the caller's own. MUTATION: drop `username=` in any of
    the three callers."""
    from vaf.api import config_routes

    answers = {
        "get": asyncio.run(config_routes.get_config(_request(NON_ADMIN))),
        "patch": _patch({"tts_auto_speak": True}, NON_ADMIN),
        "ws": _ws(monkeypatch, NON_ADMIN, {"type": "get_config"}, "config_update")["config"],
    }
    for lane, cfg in answers.items():
        assert cfg["github_config"] is None, lane
        assert cfg["github_config_by_user"] == {
            "alice": GITHUB["github_config_by_user"]["alice"]}, lane
        assert "bob-gh" not in json.dumps(cfg) and "admin-gh" not in json.dumps(cfg), lane
