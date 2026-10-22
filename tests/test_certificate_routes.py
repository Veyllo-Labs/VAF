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
from cert_factory import fingerprint, isolated_home, key_pem, make_key, own_chain, pem
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


def test_a_part_over_the_cap_is_refused_without_echoing_it(home):
    """AC 20. The cap is the check's (too_large); the answer carries the code, not the part.
    A pydantic max_length answered 422 with the whole refused value in "input", which for an
    oversized key was the private key (measured). MUTATION: give the routes a model with
    max_length again - red."""
    _payload, key = _upload()
    oversized = key_pem(key).decode() + "A" * (64 * 1024)
    payload = {"certificate": _payload["certificate"], "key": oversized, "code": ""}
    put = _client().put("/api/network/certificate", json=payload)
    check = _client().post("/api/network/certificate/check", json=payload)
    assert put.status_code == 422 and put.json()["detail"]["code"] == "refused"
    assert "too_large" in [e["code"] for e in put.json()["detail"]["report"]["errors"]]
    assert check.status_code == 200 and not check.json()["ok"]
    body = key_pem(key).decode().splitlines()[1]
    for answer in (put, check):
        assert "PRIVATE KEY" not in answer.text and body not in answer.text
    assert not (home / "custom").exists()


@pytest.mark.parametrize("payload", [["-----BEGIN PRIVATE KEY-----"],
                                     {"certificate": "x", "key": ["-----BEGIN PRIVATE KEY-----"]}])
def test_a_body_of_the_wrong_shape_is_refused_without_echoing_it(home, payload):
    answer = _client().put("/api/network/certificate", json=payload)
    assert answer.status_code == 422 and answer.json()["detail"]["code"] == "invalid"
    assert "PRIVATE KEY" not in answer.text


def test_the_ca_download_never_reads_the_private_key(home, monkeypatch):
    """/api/a2a/ca.pem is open to every admitted device without an account: it serves the
    chain the running listener loaded and never loads a key (it read and parsed the key on
    every request, 945 ms with an RSA 4096 key, measured on the event loop). MUTATION: read
    served_certificate() (the key check) in served_chain while a listener runs - red."""
    import ssl as _ssl
    import types

    from cryptography.hazmat.primitives import serialization

    import vaf.core.web_server as web_server
    payload, _key = _upload()
    _client().put("/api/network/certificate", json=payload)
    served = ssl_utils.served_certificate()
    server = types.SimpleNamespace(started=True, should_exit=False, config=types.SimpleNamespace(
        ssl=object(), ssl_certfile=served.cert_path, ssl_keyfile=served.key_path))
    ssl_utils.register_tls_listener("proxy", server, None)

    loads = []
    real_key, real_chain = serialization.load_pem_private_key, _ssl.SSLContext.load_cert_chain
    monkeypatch.setattr(serialization, "load_pem_private_key",
                        lambda *a, **kw: loads.append("key") or real_key(*a, **kw))
    monkeypatch.setattr(_ssl.SSLContext, "load_cert_chain",
                        lambda self, *a, **kw: loads.append("pair") or real_chain(self, *a, **kw))
    monkeypatch.setattr(ssl_utils, "_PAIR_CHECKS", {})
    answer = TestClient(web_server.app, client=PEER).get("/api/a2a/ca.pem")
    assert answer.status_code == 200
    assert loads == [], "the download read a private key"
    assert ssl_utils.fingerprint_of(answer.content) == fingerprint(_root_of(served.cert_path))


def _root_of(path):
    from cryptography import x509
    blocks = ssl_utils._pem_blocks(Path(path).read_bytes())[0]
    return x509.load_pem_x509_certificate(blocks[-1])


def test_the_pair_check_is_cached_until_a_file_changes(home, monkeypatch):
    """served_certificate() is asked by the settings, the doctor and the remote access
    section; the check of an own pair reads the key, so it is answered from a cache while
    neither file changed. MUTATION: drop the cache - red."""
    payload, _key = _upload()
    _client().put("/api/network/certificate", json=payload)
    calls = []
    real = ssl_utils._check_pair
    monkeypatch.setattr(ssl_utils, "_check_pair", lambda c, k: calls.append(c) or real(c, k))
    monkeypatch.setattr(ssl_utils, "_PAIR_CHECKS", {})
    ssl_utils.served_certificate()
    ssl_utils.served_certificate()
    assert len(calls) == 1
    served = ssl_utils.served_certificate()
    from pathlib import Path as _P
    _P(served.cert_path).write_bytes(_P(served.cert_path).read_bytes() + b"\n")
    ssl_utils.served_certificate()
    assert len(calls) == 2


def test_tokenless_from_this_machine_is_not_signed_in_behind_the_real_layers(home, monkeypatch):
    """_signed_in reads request.state.user. Behind the real NetworkModeLayers (AuthMiddleware
    in network mode), a tokenless request from this machine leaves it unset, so the desktop
    is not asked for a code it has no account for."""
    from vaf.auth.middleware import NetworkModeLayers
    from vaf.core.config import Config as _Config
    real_get_bool = _Config.get_bool.__func__
    for network in (True, False):
        monkeypatch.setattr(_Config, "get_bool", classmethod(
            lambda cls, key, default=False, _n=network: _n if key == "local_network_enabled"
            else real_get_bool(cls, key, default)))
        app = FastAPI()
        app.add_middleware(NetworkModeLayers)
        app.include_router(network_routes.router)
        answer = TestClient(app, client=PEER).get("/api/network/certificate")
        assert answer.status_code == 200, answer.text
        assert answer.json()["step_up_required"] is False


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


@pytest.mark.parametrize("content_type", ["text/plain", None])
def test_a_body_that_is_not_json_is_refused(home, content_type):
    """The routes read the body themselves: only application/json, the type a cross-site
    form cannot send without a preflight. MUTATION: drop the content-type check - red."""
    import json as _json
    payload, _key = _upload()
    headers = {"Content-Type": content_type} if content_type else {}
    answer = _client().post("/api/network/certificate/check",
                            content=_json.dumps(payload).encode(), headers=headers)
    assert answer.status_code == 422 and answer.json()["detail"]["code"] == "invalid"


def test_the_upload_routes_document_their_body():
    """OpenAPI shows what the two routes read, although they parse the body themselves.
    MUTATION: drop openapi_extra from the PUT route - red."""
    app = FastAPI()
    app.include_router(network_routes.router)
    paths = app.openapi()["paths"]
    for method, path in (("put", "/api/network/certificate"),
                         ("post", "/api/network/certificate/check")):
        schema = paths[path][method]["requestBody"]["content"]["application/json"]["schema"]
        assert {"certificate", "key", "chain"} <= set(schema["properties"])
