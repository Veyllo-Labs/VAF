# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""What the WhatsApp window tells a caller about other accounts and the host.

THE DEFECT, measured on the code before this change: `/dashboard` sent `log_path`, the absolute
host path of the log every account's link and process writes into, to every signed-in caller,
and `/qr/log-path` sent the same path to any caller without reading an account (no caller in
`web/`, `vaf/`, `tests/` or `docs/`). `/dashboard/debug` answered `bridge_running`, whether
anybody's WhatsApp runs, to any caller. `/dashboard`'s `linked` and `configured` were true
when ANOTHER account named in a visible whitelist entry was linked.

The real router with signed-in requests, the bridge's lanes as recorders, a scratch APP_DIR.
"""
import json

import pytest

from vaf.api import whatsapp_bridge as wa
from vaf.api import whatsapp_routes as routes
from vaf.core.config import Config
from vaf.core.platform import Platform

ADMIN_SCOPE = "ab12cd34-0000-4000-8000-0000000000aa"
ALICE_SCOPE = "ab12cd34-0000-4000-8000-000000000001"
ALICE = {"username": "alice", "role": "user", "user_scope_id": ALICE_SCOPE}
ADMIN = {"username": "admin", "role": "admin", "user_scope_id": ADMIN_SCOPE}


@pytest.fixture
def world(monkeypatch, tmp_path):
    state = {"local_admin_scope_id": ADMIN_SCOPE, "local_admin_username": "admin",
             "whatsapp_config": {"enabled": True, "whitelist": []}, "channel_ingress_policy": None}
    monkeypatch.setattr(Platform, "data_dir", staticmethod(lambda: tmp_path / "data"))
    monkeypatch.setattr(Config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, key, default=None: state.get(key, default)))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: dict(state)))
    monkeypatch.setattr(Config, "save", classmethod(lambda cls, cfg: None))
    asked = []

    def lane(name, empty):
        def call(username, *a, **k):
            asked.append((name, username, k.get("user_scope_id")))
            return empty
        return call
    monkeypatch.setattr(wa, "get_whatsapp_chats", lane("chats", [{"jid": "491700000042@s.whatsapp.net"}]))
    monkeypatch.setattr(wa, "get_contact_names", lane("names", {}))
    monkeypatch.setattr(wa, "get_lid_mappings", lane("lids", []))
    monkeypatch.setattr(wa, "get_connection_status", lane("ping", False))
    monkeypatch.setattr(wa, "is_bridge_running", lambda: True)
    monkeypatch.setattr(wa, "has_process_for_user",
                        lambda username, scope=None: asked.append(("running", username, scope)) or scope == ALICE_SCOPE)
    monkeypatch.setattr(wa, "_append_chat_activity", lambda *a, **k: None)
    state["asked"] = asked
    return state


def _client(user):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request, call_next):
        request.state.user = dict(user)
        return await call_next(request)
    app.include_router(routes.router)
    return TestClient(app)


def _link(name):
    d = Config.APP_DIR / "users" / name / "whatsapp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "creds.json").write_text(json.dumps({"me": {"id": "491700000099:7@s.whatsapp.net"}}), encoding="utf-8")


def test_log_path_only_for_an_admin(world):
    """MUTATION: send `log_path` to everybody again - red."""
    mine = _client(ALICE).get("/api/whatsapp/dashboard")
    assert mine.status_code == 200, mine.text
    assert mine.json()["log_path"] is None
    assert str(Config.APP_DIR.parent) not in mine.text
    admins = _client(ADMIN).get("/api/whatsapp/dashboard").json()
    assert admins["log_path"] and "whatsapp_qr" in admins["log_path"]


def test_every_dashboard_lane_names_the_callers_scope(world):
    """A lane asked without the scope reads nothing now (`_process_for`), so a route that
    forgets it shows an empty window: pinned here."""
    _client(ALICE).get("/api/whatsapp/dashboard")
    lanes = {name for name, _, _ in world["asked"]}
    assert {"chats", "names", "lids", "running"} <= lanes
    assert all(user == "alice" and scope == ALICE_SCOPE for _, user, scope in world["asked"]), world["asked"]


def test_debug_bridge_running_only_for_an_admin(world):
    """MUTATION: answer `bridge_running` to everybody - red."""
    mine = _client(ALICE).get("/api/whatsapp/dashboard/debug").json()
    assert "bridge_running" not in mine
    assert mine == {"running": True, "raw_chats_count": 1, "username": "alice"}
    admins = _client(ADMIN).get("/api/whatsapp/dashboard/debug").json()
    assert admins["bridge_running"] is True and admins["running"] is False


def test_linked_is_the_callers_own(world):
    """Alice's visible whitelist entry names "admin" (older versions filed entries under the
    name "admin"), and the admin is linked; Alice is not. MUTATION: restore
    `any_whitelist_linked` - red."""
    world["whatsapp_config"]["whitelist"] = [
        {"phone_number": "+491700000001", "vaf_username": "admin", "user_scope_id": ALICE_SCOPE}]
    _link("admin")
    out = _client(ALICE).get("/api/whatsapp/dashboard").json()
    assert out["linked"] is False and out["configured"] is False and out["linked_phone"] is None
    _link("alice")
    out = _client(ALICE).get("/api/whatsapp/dashboard").json()
    assert out["linked"] is True and out["linked_phone"] == "+491700000099"


def test_the_qr_log_path_route_is_gone(world):
    assert _client(ALICE).get("/api/whatsapp/qr/log-path").status_code == 404
    assert _client(ADMIN).get("/api/whatsapp/qr/log-path").status_code == 404
