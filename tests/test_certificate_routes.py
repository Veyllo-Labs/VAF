# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The web half of the certificate: admin only, checked first, and behind a second factor
for a signed-in admin.

A change of the served certificate changes what every device sees, so a stolen admin
session alone must not be enough: a signed-in admin gives the 2FA code
(`security_routes.verify_admin_totp`, the one step-up check). A tokenless request reaches
these routes only from this machine, the same trust as the terminal. The generic config
saves cannot set the two keys at all.
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import vaf.network.binding as binding
from cert_factory import isolated_home, key_pem, make_key, own_chain, pem
from vaf.api import network_routes, security_routes
from vaf.core.config import Config
from vaf.network import ssl_utils

ROOT = Path(__file__).resolve().parents[1]
PEER = ("127.0.0.1", 40000)
ADMIN = {"user_id": "ab12cd34-0000-4000-8000-000000000001", "username": "alice",
         "role": "admin", "user_scope_id": "ab12cd34-0000-4000-8000-000000000001"}
USER = {"user_id": "ab12cd34-0000-4000-8000-000000000002", "username": "bob", "role": "user",
        "user_scope_id": "ab12cd34-0000-4000-8000-000000000002"}
GOOD_CODE = "123456"


def _client(user=None):
    app = FastAPI()
    if user is not None:
        class _As(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.user = user
                return await call_next(request)
        app.add_middleware(_As)
    app.include_router(network_routes.router)
    return TestClient(app, client=PEER)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [("enp3s0", "192.168.1.20")])
    monkeypatch.setattr(binding, "local_interfaces", lambda: [])
    monkeypatch.setattr(binding, "machine_dns_names", lambda: ["vafhost"])
    ssl_dir = isolated_home(tmp_path, monkeypatch, local_network_enabled=True,
                            local_network_tls_enabled=True)
    ssl_utils.ensure_ssl_certificates()
    return ssl_dir


@pytest.fixture
def step_up(monkeypatch):
    """The step-up check, answered here: GOOD_CODE passes, anything else is 403."""
    calls = []

    async def _verify(user, code, ip=""):
        calls.append((user.get("username"), code, ip))
        if code != GOOD_CODE:
            raise HTTPException(status_code=403, detail="Invalid 2FA code")
    monkeypatch.setattr(security_routes, "verify_admin_totp", _verify)
    return calls


def _upload(code=""):
    leaf, key, inter, root = own_chain(names=("vafhost", "192.168.1.20"))
    return {"certificate": pem(leaf, inter, root).decode(), "key": key_pem(key).decode(),
            "code": code}, key


# ── who may ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, path, body", [
    ("get", "/api/network/certificate", None),
    ("post", "/api/network/certificate/check", {"certificate": "x"}),
    ("put", "/api/network/certificate", {"certificate": "x"}),
    ("delete", "/api/network/certificate", None),
    ("post", "/api/network/certificate/authority", {"confirm": True}),
    ("post", "/api/network/certificate/reload", None),
])
def test_an_ordinary_account_gets_nothing(home, method, path, body):
    client = _client(USER)
    kwargs = {"json": body} if body is not None else {}
    assert client.request(method.upper(), path, **kwargs).status_code == 403


# ── the second factor ───────────────────────────────────────────────────────

@pytest.mark.parametrize("code", ["", "000000"])
@pytest.mark.parametrize("method, path, body", [
    ("PUT", "/api/network/certificate", "upload"),
    ("DELETE", "/api/network/certificate", {}),
    ("POST", "/api/network/certificate/authority", {"confirm": True}),
])
def test_a_signed_in_admin_without_the_right_code_is_refused(home, step_up, method, path,
                                                             body, code):
    """AC 20. MUTATION: skip the step-up for signed-in accounts - red."""
    payload = _upload(code)[0] if body == "upload" else {**body, "code": code}
    ca_before = ssl_utils.ca_fingerprint()
    response = _client(ADMIN).request(method, path, json=payload)
    assert response.status_code == 403
    assert step_up == [("alice", code, "127.0.0.1")]
    assert ssl_utils.served_certificate().source == "automatic"
    assert ssl_utils.ca_fingerprint() == ca_before


def test_a_signed_in_admin_with_the_code_installs(home, step_up):
    payload, _key = _upload(GOOD_CODE)
    response = _client(ADMIN).put("/api/network/certificate", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["state"]["source"] == "own"
    assert response.json()["result"]["reload"]["ok"] is True
    assert step_up == [("alice", GOOD_CODE, "127.0.0.1")]


def test_tokenless_from_this_machine_needs_no_code(home, step_up):
    payload, _key = _upload()
    assert _client().put("/api/network/certificate", json=payload).status_code == 200
    assert _client().delete("/api/network/certificate").status_code == 200
    assert ssl_utils.served_certificate().source == "automatic"
    assert step_up == []


def test_the_state_says_when_a_code_is_asked(home):
    assert _client(ADMIN).get("/api/network/certificate").json()["step_up_required"] is True
    assert _client().get("/api/network/certificate").json()["step_up_required"] is False


# ── refused, confirmed, capped ──────────────────────────────────────────────

def test_a_refused_upload_writes_nothing_and_spends_no_code(home, step_up):
    """AC 20. The check runs before the code is asked for. MUTATION: write before the
    report check - red."""
    leaf, _key, inter, root = own_chain()
    payload = {"certificate": pem(leaf, inter, root).decode(),
               "key": key_pem(make_key()).decode(), "code": GOOD_CODE}
    response = _client(ADMIN).put("/api/network/certificate", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "refused"
    assert "key_mismatch" in [e["code"] for e in detail["report"]["errors"]]
    assert step_up == []
    assert not (home / "custom").exists()


def test_the_authority_is_renewed_only_once_confirmed(home, step_up):
    before = ssl_utils.ca_fingerprint()
    response = _client().post("/api/network/certificate/authority", json={})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "confirm"
    assert ssl_utils.ca_fingerprint() == before
    response = _client().post("/api/network/certificate/authority", json={"confirm": True})
    assert response.status_code == 200
    assert response.json()["result"]["old_authority"] == before
    assert ssl_utils.ca_fingerprint() != before


def test_a_body_over_the_cap_is_refused(home):
    payload = {"certificate": "A" * (64 * 1024 + 1), "key": "", "code": ""}
    assert _client().put("/api/network/certificate", json=payload).status_code == 422
    assert _client().post("/api/network/certificate/check", json=payload).status_code == 422
    assert not (home / "custom").exists()


def test_no_answer_carries_key_material(home):
    payload, key = _upload()
    body = key_pem(key).decode().splitlines()[1]
    client = _client()
    for response in (client.post("/api/network/certificate/check", json=payload),
                     client.put("/api/network/certificate", json=payload),
                     client.get("/api/network/certificate"),
                     client.post("/api/network/certificate/reload")):
        assert response.status_code == 200
        assert "PRIVATE KEY" not in response.text and body not in response.text


def test_the_reload_route_answers_the_terminal(home):
    answer = _client().post("/api/network/certificate/reload").json()
    assert answer["ok"] is True and answer["source"] == "automatic"


# ── one second-factor check ─────────────────────────────────────────────────

def test_one_second_factor_check():
    """AC 21: the routes call security_routes.verify_admin_totp and hold no TOTP logic of
    their own. MUTATION: a local copy of the check - red."""
    source = (ROOT / "vaf" / "api" / "network_routes.py").read_text(encoding="utf-8")
    assert "from vaf.api.security_routes import verify_admin_totp" in source
    for own in ("check_second_factor", "totp_secret", "pyotp", "def verify_admin_totp"):
        assert own not in source
    security = (ROOT / "vaf" / "api" / "security_routes.py").read_text(encoding="utf-8")
    assert "async def verify_admin_totp(" in security and "_verify_admin_totp" not in security


# ── the generic saves cannot set the keys ───────────────────────────────────

def test_generic_saves_cannot_set_the_certificate(tmp_path, monkeypatch):
    """AC 17: PATCH /api/config drops both keys; the stored values stay. MUTATION: remove the
    call from the PATCH lane - red."""
    from vaf.api import config_routes
    isolated_home(tmp_path, monkeypatch, local_network_ssl_cert="/srv/mine.pem",
                  local_network_ssl_key="/srv/mine.key")
    app = FastAPI()
    app.include_router(config_routes.router)
    client = TestClient(app, client=PEER)
    response = client.patch("/api/config", json={
        "local_network_ssl_cert": "/tmp/planted.pem", "local_network_ssl_key": "/tmp/planted.key",
        "local_network_https_port": 8443})
    assert response.status_code == 200, response.text
    stored = json.loads(Config.CONFIG_FILE.read_text(encoding="utf-8"))
    assert stored["local_network_ssl_cert"] == "/srv/mine.pem"
    assert stored["local_network_ssl_key"] == "/srv/mine.key"
    assert stored["local_network_https_port"] == 8443


def test_both_save_lanes_pass_the_filter():
    """AC 17: the WebSocket save_config and PATCH /api/config both drop the keys, next to
    the inference-profile filter."""
    web = (ROOT / "vaf" / "core" / "web_server.py").read_text(encoding="utf-8")
    lane = web[web.index('elif type == "save_config":'):]
    lane = lane[:lane.index("Config.save(merged)")]
    assert "without_certificate_keys(new_config)" in lane
    patch = (ROOT / "vaf" / "api" / "config_routes.py").read_text(encoding="utf-8")
    route = patch[patch.index("async def patch_config"):]
    route = route[:route.index("Config.save(merged)")]
    assert "without_certificate_keys(" in route
