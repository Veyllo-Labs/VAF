# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Signing in to a remote MCP server, one account at a time, against a real one: an MCP server
built with the official SDK that is its own OAuth authorization server (metadata discovery,
dynamic client registration, PKCE, refresh, revocation), served on a free local port.

Measured before the change: a server that asks for a sign-in answered every request with 401 and
VAF had nothing to offer but one fixed token for the whole installation, so a service that holds
each person's own data (a workspace, a mailbox) could not be used at all, and one that could
would have run every account's calls as the admin.

The "browser" in these tests is an HTTP client that opens the authorization address and reads
where the service sends it back, which is what a person's browser does after "Allow".
"""
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

REDIRECT = "http://localhost:8001/api/mcp/oauth/callback"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Service:
    """An authorization server that approves every request at once, in memory."""

    def __init__(self):
        self.clients, self.codes, self.access, self.refresh = {}, {}, {}, {}
        self.revoked = []

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, info):
        self.clients[info.client_id] = info

    async def authorize(self, client, params):
        from mcp.server.auth.provider import AuthorizationCode, construct_redirect_uri
        code = secrets.token_urlsafe(24)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300, client_id=client.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, code):
        return self.codes.get(code)

    def _issue(self, client_id, scopes):
        from mcp.server.auth.provider import AccessToken, RefreshToken
        from mcp.shared.auth import OAuthToken
        access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes,
                                          expires_at=int(time.time() + 3600))
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes)
        return OAuthToken(access_token=access, expires_in=3600, refresh_token=refresh)

    async def exchange_authorization_code(self, client, code):
        self.codes.pop(code.code, None)
        return self._issue(client.client_id, code.scopes)

    async def load_refresh_token(self, client, token):
        return self.refresh.get(token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.refresh.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token):
        return self.access.get(token)

    async def revoke_token(self, token):
        self.revoked.append(token.token)
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)


class _Recorder:
    """Remembers every request's path and body."""

    def __init__(self, app):
        self.app = app
        self.requests = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        record = {"path": scope.get("path"), "body": b""}
        self.requests.append(record)

        async def _receive():
            message = await receive()
            if message.get("type") == "http.request":
                record["body"] += message.get("body", b"")
            return message
        await self.app(scope, _receive, send)


def _secured(port, kind):
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.fastmcp import FastMCP
    service = _Service()
    base = f"http://127.0.0.1:{port}"
    server = FastMCP("secured", auth_server_provider=service, host="127.0.0.1", port=port, auth=AuthSettings(
        issuer_url=base, resource_server_url=base + ("/mcp" if kind == "http" else "/sse"),
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True)))

    @server.tool()
    def whoami() -> str:
        """Which client the call came from."""
        from mcp.server.auth.middleware.auth_context import get_access_token
        token = get_access_token()
        return f"client:{token.client_id if token else None}"

    @server.tool()
    def echo(text: str) -> str:
        """Echo the text."""
        return f"echo:{text}"

    app = server.streamable_http_app() if kind == "http" else server.sse_app()
    return _Recorder(app), service


def _serve(app, port):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server


@pytest.fixture(scope="module")
def remote():
    out = {}
    servers = []
    for kind, path in (("http", "/mcp"), ("sse", "/sse")):
        port = _free_port()
        app, service = _secured(port, kind)
        servers.append(_serve(app, port))
        out[kind] = type("S", (), {"url": f"http://127.0.0.1:{port}{path}", "app": app, "service": service})
    yield out
    from vaf.core.mcp_remote import get_remote_pool
    get_remote_pool().close()
    for srv in servers:
        srv.should_exit = True


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    import json

    import vaf.core.mcp_registry as reg
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(reg, "get_mcp_manifest_path", lambda: path)

    def write(servers):
        path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
    return type("M", (), {"write": staticmethod(write), "path": path})


def _browser(authorization_url):
    """Open the address like a browser that pressed "Allow": the service answers with a
    redirect to VAF's callback carrying `code` and `state`."""
    response = httpx.get(authorization_url, follow_redirects=False, timeout=10)
    assert response.status_code in (302, 303, 307), response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query["code"][0], query["state"][0]


def _sign_in(server, scope, username="alice"):
    from vaf.core import mcp_oauth
    started = mcp_oauth.start_sign_in(server, user_scope_id=scope, username=username, redirect_uri=REDIRECT)
    code, state = _browser(started["authorization_url"])
    return mcp_oauth.finish_sign_in(state, code)


def _tools(timeout=15):
    import vaf.core.mcp_registry as reg
    return reg.discover_mcp_tools(timeout_seconds=timeout)


# -- signing in ------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["http", "sse"])
def test_each_account_signs_in_and_its_calls_run_as_itself(remote, manifest, kind):
    from vaf.core import mcp_oauth
    srv = remote[kind]
    manifest.write({"svc": {"transport": kind, "url": srv.url, "auth": "oauth"}})
    tools, status = _tools()
    assert status["svc"]["connected"] is False and status["svc"].get("sign_in_required") is True, status

    result = _sign_in("svc", "scope-alice")
    assert result == {"ok": True, "server": "svc", "error": None}
    assert "access_token" not in manifest.path.read_text(encoding="utf-8")
    tools, status = _tools()
    assert status["svc"]["connected"] is True, status
    whoami = tools["mcp_svc_whoami"]
    assert whoami.identity_kwargs == ("user_scope_id",), "the dispatcher has to say who calls"

    alice = whoami.run(user_scope_id="scope-alice")
    assert alice.startswith("client:") and alice != "client:None"
    before = len(srv.app.requests)
    refused = whoami.run(user_scope_id="scope-bob")
    assert "not signed in" in refused and "Connections" in refused
    assert len(srv.app.requests) == before, "an account without a sign-in never reaches the server"

    assert _sign_in("svc", "scope-bob", "bob")["ok"] is True
    bob = whoami.run(user_scope_id="scope-bob")
    assert bob.startswith("client:") and bob != alice, "each account runs in its own session"
    assert whoami.run(user_scope_id="scope-alice") == alice
    assert mcp_oauth.signed_in("svc", "scope-bob") and not mcp_oauth.signed_in("svc", "scope-carol")

    # Through the dispatcher: the caller's scope is ASSIGNED, a model-supplied one never counts.
    from vaf.core.tool_dispatch import assign_declared_identity
    args = assign_declared_identity(whoami, {"user_scope_id": "scope-alice"}, user_scope_id="scope-bob",
                                    username="bob", user_role="user")
    assert whoami.run(**args) == bob


def test_the_identity_key_is_never_sent_to_the_server(remote, manifest):
    srv = remote["http"]
    manifest.write({"svc": {"transport": "http", "url": srv.url, "auth": "oauth"}})
    _sign_in("svc", "scope-alice")
    tools, _ = _tools()
    before = len(srv.app.requests)
    assert tools["mcp_svc_echo"].run(text="hi", user_scope_id="scope-alice") == "echo:hi"
    bodies = b"".join(r["body"] for r in srv.app.requests[before:])
    assert b'"text"' in bodies and b"user_scope_id" not in bodies and b"scope-alice" not in bodies


def test_a_token_that_ran_out_while_vaf_was_down_is_refreshed(remote, manifest):
    """A restart: the session is gone, the stored access token has run out. The provider must
    refresh it, not treat it as valid, meet a 401 and ask a person to sign in again."""
    from vaf.core import mcp_oauth, mcp_secrets
    from vaf.core.mcp_remote import get_remote_pool
    srv = remote["http"]
    manifest.write({"svc": {"transport": "http", "url": srv.url, "auth": "oauth"}})
    _sign_in("svc", "scope-alice")
    account = mcp_oauth.account_key("scope-alice")
    record = mcp_secrets.oauth_record("svc", account)
    old_access = record["tokens"]["access_token"]
    srv.service.access.pop(old_access)                # the service no longer takes it
    record["expires_at"] = time.time() - 60           # and VAF knows it ran out
    mcp_secrets.set_oauth_record("svc", account, record)
    get_remote_pool().close()                         # the restart

    tools, _ = _tools()
    assert tools["mcp_svc_echo"].run(text="back", user_scope_id="scope-alice") == "echo:back"
    fresh = mcp_secrets.oauth_record("svc", account)["tokens"]["access_token"]
    assert fresh != old_access and fresh in srv.service.access


@pytest.mark.parametrize("session", ["reopened", "open"])
def test_a_sign_in_the_service_revoked_asks_to_sign_in_again_at_once(remote, manifest, session):
    """"open": the account's session is live when its tokens stop working. The SDK's flow then
    fails inside the request and ends the transport, and the waiting call has to learn that at
    once rather than wait out the call timeout for an answer nobody will send."""
    from vaf.core.mcp_remote import get_remote_pool
    srv = remote["http"]
    manifest.write({"svc": {"transport": "http", "url": srv.url, "auth": "oauth"}})
    _sign_in("svc", "scope-alice")
    tools, _ = _tools()
    assert tools["mcp_svc_echo"].run(text="a", user_scope_id="scope-alice") == "echo:a"
    srv.service.access.clear()
    srv.service.refresh.clear()
    if session == "reopened":
        get_remote_pool().close()
    started = time.monotonic()
    out = tools["mcp_svc_echo"].run(text="x", user_scope_id="scope-alice")
    assert "sign in" in out, out
    assert time.monotonic() - started < 15, "it must not wait for a browser nobody opened"


def test_a_started_sign_in_hands_back_the_same_address_while_it_waits(remote, manifest):
    from vaf.core import mcp_oauth
    manifest.write({"svc": {"transport": "http", "url": remote["http"].url, "auth": "oauth"}})
    first = mcp_oauth.start_sign_in("svc", user_scope_id="scope-alice", redirect_uri=REDIRECT)
    again = mcp_oauth.start_sign_in("svc", user_scope_id="scope-alice", redirect_uri=REDIRECT)
    assert first == again
    status = {s["name"]: s for s in mcp_oauth.sign_in_status("scope-alice")}
    assert status["svc"]["pending"] is True and status["svc"]["signed_in"] is False
    code, state = _browser(first["authorization_url"])
    assert mcp_oauth.finish_sign_in(state, code)["ok"] is True
    assert mcp_oauth.finish_sign_in(state, code)["ok"] is False, "a state is used once"


def test_a_sign_in_started_and_abandoned_changes_nothing(remote, manifest):
    """Whoever sends the start (the person, or a page that posts it in their name), the working
    sign-in stays until a new one has tokens, and the account's calls do not wait behind it."""
    from vaf.core import mcp_oauth, mcp_secrets
    srv = remote["http"]
    manifest.write({"svc": {"transport": "http", "url": srv.url, "auth": "oauth"}})
    _sign_in("svc", "scope-alice")
    tools, _ = _tools()
    before = mcp_secrets.oauth_record("svc", "scope-alice")
    started = mcp_oauth.start_sign_in("svc", user_scope_id="scope-alice", redirect_uri=REDIRECT)
    assert started["authorization_url"]
    assert mcp_secrets.oauth_record("svc", "scope-alice") == before
    t0 = time.monotonic()
    assert tools["mcp_svc_echo"].run(text="still", user_scope_id="scope-alice") == "echo:still"
    assert time.monotonic() - t0 < 10


def test_a_new_sign_in_replaces_the_old_one_everywhere(remote, manifest):
    from vaf.core import mcp_secrets
    srv = remote["http"]
    manifest.write({"svc": {"transport": "http", "url": srv.url, "auth": "oauth"}})
    _sign_in("svc", "scope-alice")
    tools, _ = _tools()
    assert tools["mcp_svc_echo"].run(text="a", user_scope_id="scope-alice") == "echo:a"   # a session is open
    old_access = mcp_secrets.oauth_record("svc", "scope-alice")["tokens"]["access_token"]
    assert _sign_in("svc", "scope-alice")["ok"] is True
    new_access = mcp_secrets.oauth_record("svc", "scope-alice")["tokens"]["access_token"]
    assert new_access != old_access
    srv.service.access.pop(old_access)            # the old token stops working
    assert tools["mcp_svc_echo"].run(text="b", user_scope_id="scope-alice") == "echo:b", \
        "the session holding the old token was not replaced"


def test_a_refused_or_unknown_callback_signs_nobody_in(remote, manifest):
    from vaf.core import mcp_oauth
    manifest.write({"svc": {"transport": "http", "url": remote["http"].url, "auth": "oauth"}})
    assert mcp_oauth.finish_sign_in("made-up", "code")["ok"] is False
    started = mcp_oauth.start_sign_in("svc", user_scope_id="scope-alice", redirect_uri=REDIRECT)
    _code, state = _browser(started["authorization_url"])
    assert mcp_oauth.pending_owner(state)["user_scope_id"] == "scope-alice"
    result = mcp_oauth.finish_sign_in(state, error="access_denied<script>")
    assert result["ok"] is False and "access_denied" in result["error"] and "<" not in result["error"]
    status = {s["name"]: s for s in mcp_oauth.sign_in_status("scope-alice")}
    assert status["svc"]["signed_in"] is False and "access_denied" in (status["svc"]["error"] or "")


def test_signing_out_forgets_the_tokens_and_the_service_invalidates_them(remote, manifest):
    from vaf.core import mcp_oauth, mcp_secrets
    srv = remote["http"]
    manifest.write({"svc": {"transport": "http", "url": srv.url, "auth": "oauth"}})
    _sign_in("svc", "scope-alice")
    tools, _ = _tools()
    refresh = mcp_secrets.oauth_record("svc", mcp_oauth.account_key("scope-alice"))["tokens"]["refresh_token"]
    assert mcp_oauth.sign_out("svc", user_scope_id="scope-alice") == {"revoked": True}
    assert not mcp_oauth.signed_in("svc", "scope-alice")
    assert refresh in srv.service.revoked
    assert "not signed in" in tools["mcp_svc_echo"].run(text="x", user_scope_id="scope-alice"), \
        "the open session went with the sign-in"


@pytest.mark.parametrize("change", ["other_host", "auth_off", "other_client"])
def test_a_changed_server_drops_every_sign_in(remote, manifest, change):
    import vaf.core.mcp_registry as reg
    from vaf.core import mcp_oauth, mcp_secrets
    srv = remote["http"]
    reg.upsert_server("svc", transport="http", url=srv.url, auth="oauth")
    _sign_in("svc", "scope-alice")
    _sign_in("svc", "scope-bob", "bob")
    same = dict(transport="http", url=srv.url, auth="oauth")
    if change == "other_host":
        reg.upsert_server("svc", **{**same, "url": "https://elsewhere.example.net/mcp"})
    elif change == "auth_off":
        reg.upsert_server("svc", **{**same, "auth": ""})
    else:
        reg.upsert_server("svc", **same, oauth_client_id="hand-registered")
    assert mcp_secrets.oauth_accounts("svc") == []
    assert not mcp_oauth.signed_in("svc", "scope-alice")


def test_an_unchanged_save_keeps_the_sign_ins(remote, manifest):
    import vaf.core.mcp_registry as reg
    from vaf.core import mcp_oauth
    srv = remote["http"]
    reg.upsert_server("svc", transport="http", url=srv.url, auth="oauth")
    _sign_in("svc", "scope-alice")
    reg.upsert_server("svc", transport="http", url=srv.url, permission_level="read")   # auth left as it is
    assert mcp_oauth.signed_in("svc", "scope-alice")


def test_removing_the_server_forgets_every_sign_in(remote, manifest):
    import vaf.core.mcp_registry as reg
    from vaf.core import mcp_secrets
    srv = remote["http"]
    reg.upsert_server("svc", transport="http", url=srv.url, auth="oauth")
    _sign_in("svc", "scope-alice")
    refresh = mcp_secrets.oauth_record("svc", "scope-alice")["tokens"]["refresh_token"]
    assert reg.remove_server("svc") is True
    assert mcp_secrets.oauth_accounts("svc") == []
    for _ in range(100):                      # the service is asked in the background
        if refresh in srv.service.revoked:
            break
        time.sleep(0.05)
    assert refresh in srv.service.revoked


def test_a_sign_in_server_takes_no_fixed_token(manifest):
    import vaf.core.mcp_registry as reg
    from vaf.core import mcp_secrets
    reg.upsert_server("svc", transport="http", url="https://mcp.example.com/mcp", token="tok_1")
    reg.upsert_server("svc", transport="http", url="https://mcp.example.com/mcp", auth="oauth", token="tok_2")
    assert mcp_secrets.server_token("svc") == ""
    mcp_secrets.set_server_token("svc", "tok_by_hand")      # e.g. a hand edit the load moved
    cfg = {"transport": "http", "auth": "oauth", "url": "https://mcp.example.com/mcp"}
    assert "Authorization" not in reg.server_headers("svc", cfg), "each session carries its account's own"
    assert reg.server_headers("svc", {**cfg, "auth": ""}) == {"Authorization": "Bearer tok_by_hand"}
    with pytest.raises(ValueError):
        reg.upsert_server("local", command="x", auth="oauth")
    with pytest.raises(ValueError):
        reg.upsert_server("svc", transport="http", url="https://mcp.example.com/mcp", auth="oauth",
                          oauth_client_secret="s3cret")


def test_a_hand_registered_client_secret_lives_in_the_ring(manifest):
    import json

    import vaf.core.mcp_registry as reg
    from vaf.core import mcp_secrets
    reg.upsert_server("svc", transport="http", url="https://mcp.example.com/mcp", auth="oauth",
                      oauth_client_id="client-1", oauth_client_secret="s3cret")
    assert "s3cret" not in manifest.path.read_text(encoding="utf-8")
    assert mcp_secrets.oauth_client_secret("svc") == "s3cret"
    shown = {s["name"]: s for s in reg.servers_for_display({})}["svc"]
    assert shown["auth"] == "oauth" and shown["oauth_client_id"] == "client-1"
    assert shown["oauth_client_secret_set"] is True and "s3cret" not in json.dumps(shown)
    reg.upsert_server("svc", transport="http", url="https://mcp.example.com/mcp", auth="oauth")   # form echo
    assert mcp_secrets.oauth_client_secret("svc") == "s3cret", "an empty field keeps it"
    manifest.write({"hand": {"transport": "http", "url": "https://x.example/mcp", "auth": "oauth",
                             "oauth_client_id": "c", "oauth_client_secret": "written-by-hand"}})
    reg.load_mcp_manifest()
    assert "written-by-hand" not in manifest.path.read_text(encoding="utf-8")
    assert mcp_secrets.oauth_client_secret("hand") == "written-by-hand"


def test_the_editor_test_reads_a_sign_in_server_as_waiting_for_a_sign_in(remote, manifest):
    import vaf.core.mcp_registry as reg
    srv = remote["http"]
    cfg = {"transport": "http", "url": srv.url, "auth": "oauth"}
    out = reg.probe_mcp_server(cfg, 15)
    assert out["connected"] is False and out.get("sign_in_required") is True
    reg.upsert_server("svc", **cfg)
    _sign_in("svc", "scope-alice")
    assert reg.probe_mcp_server(cfg, 15, name="svc", user_scope_id="scope-alice")["connected"] is True
    other = reg.probe_mcp_server(cfg, 15, name="svc", user_scope_id="scope-bob")
    assert other["connected"] is False, "the test never runs as somebody else's account"


# -- the web routes --------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from vaf.api.mcp_routes import router
    app = FastAPI()
    who = {"user": {"username": "alice", "user_scope_id": "scope-alice", "role": "user"}}

    @app.middleware("http")
    async def _identity(request, call_next):
        request.state.user = who["user"]
        return await call_next(request)
    app.include_router(router)
    return type("C", (), {"http": TestClient(app), "who": who})


def test_the_routes_sign_the_caller_in_and_out(remote, manifest, client):
    from vaf.core import mcp_oauth
    manifest.write({"svc": {"transport": "http", "url": remote["http"].url, "auth": "oauth"},
                    "plain": {"transport": "http", "url": "https://x.example/mcp"}})
    listed = client.http.get("/api/mcp/sign-in").json()
    assert listed["redirect_uri"].endswith("/api/mcp/oauth/callback")
    assert [s["name"] for s in listed["servers"]] == ["svc"]
    started = client.http.post("/api/mcp/sign-in/svc").json()
    code, state = _browser(started["authorization_url"])
    back = client.http.get(f"/api/mcp/oauth/callback?code={code}&state={state}", follow_redirects=False)
    assert back.status_code == 302 and "mcp_sign_in=success" in back.headers["location"]
    assert mcp_oauth.signed_in("svc", "scope-alice")
    assert client.http.get("/api/mcp/sign-in").json()["servers"][0]["signed_in"] is True
    assert client.http.delete("/api/mcp/sign-in/svc").json()["revoked"] is True
    assert not mcp_oauth.signed_in("svc", "scope-alice")
    assert client.http.post("/api/mcp/sign-in/plain").status_code == 400


def test_the_callback_belongs_to_the_person_who_started_it(remote, manifest, client, monkeypatch):
    """On the network, the browser that comes back must be the one that started the sign-in."""
    from vaf.core import mcp_oauth
    from vaf.core.config import Config
    real_get = Config.get
    monkeypatch.setattr(Config, "get", classmethod(
        lambda cls, key, default=None: True if key == "local_network_enabled" else real_get(key, default)))
    monkeypatch.setattr("vaf.network.binding.effective_client_ip", lambda peer, fwd: "192.168.1.20")
    manifest.write({"svc": {"transport": "http", "url": remote["http"].url, "auth": "oauth"}})
    started = client.http.post("/api/mcp/sign-in/svc").json()
    code, state = _browser(started["authorization_url"])
    client.who["user"] = {"username": "mallory", "user_scope_id": "scope-mallory", "role": "user"}
    refused = client.http.get(f"/api/mcp/oauth/callback?code={code}&state={state}", follow_redirects=False)
    assert refused.status_code == 403
    assert not mcp_oauth.signed_in("svc", "scope-mallory") and not mcp_oauth.signed_in("svc", "scope-alice")
    unknown = client.http.get("/api/mcp/oauth/callback?code=x&state=nope", follow_redirects=False)
    assert "mcp_sign_in=expired" in unknown.headers["location"]
