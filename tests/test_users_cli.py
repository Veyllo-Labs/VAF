# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""`vaf users reset-password`: the way back in when an admin's password is gone.

It needs a person at the terminal and a proof (the admin password or the recovery key from
VAF-BackThisUp.md), never the group door, which opens for a caller without a terminal and
asks for exactly the password that is gone. The account layer is faked; what is pinned is
who gets through and what the reset sets.
"""
import contextlib
import uuid
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import vaf.cli.cmd.users as users_cmd


@pytest.fixture
def world(monkeypatch):
    """A fake account store, a recovery key, prompts answered from a list."""
    import vaf.auth.database as database
    import vaf.auth.user_admin as user_admin
    import vaf.cli.gate as gate
    import vaf.core.recovery_kit as recovery_kit
    import vaf.core.security_events as security_events

    state = {"user": SimpleNamespace(username="alice", role="admin", seal="x",
                                     id="11111111-0000-4000-8000-0000000000a1"),
             "resets": [], "events": [], "answers": [], "sealed": True, "closed": [],
             "close_answer": None}

    @contextlib.asynccontextmanager
    async def _db():
        yield object()

    async def _find(_db, name):
        return state["user"] if name.lower() == "alice" else None

    async def _reset(_db, user, password):
        state["resets"].append((user.username, password))

    monkeypatch.setattr(database, "get_auth_db", _db)
    monkeypatch.setattr(user_admin, "find_user", _find)
    monkeypatch.setattr(user_admin, "reset_password_from_terminal", _reset)
    from vaf.auth import account_seal
    monkeypatch.setattr(account_seal, "is_sealed", lambda u: state["sealed"])
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: ("", False))
    wrap = SimpleNamespace(exists=lambda: True)
    monkeypatch.setattr(recovery_kit, "recovery_wrap_path", lambda: wrap)
    monkeypatch.setattr(recovery_kit, "unwrap_with_secret",
                        lambda s: b"dek" if s == "the-real-key" else None)
    monkeypatch.setattr(security_events, "log_security_event",
                        lambda kind, **kw: state["events"].append((kind, kw.get("detail", ""))))
    monkeypatch.setattr(users_cmd.getpass, "getpass", lambda _p="": state["answers"].pop(0))
    # Never the running VAF on this machine: the call that closes open connections is recorded.
    monkeypatch.setattr(users_cmd, "_close_open_connections",
                        lambda user_id: state["closed"].append(user_id) or state["close_answer"])
    return state


def _run(args, input_text=""):
    return CliRunner().invoke(users_cmd.app, args, input=input_text)


def test_a_reset_needs_a_person_at_the_terminal(world, monkeypatch):
    """The agent's shell and scripts have no terminal; the group door would have let them in.
    MUTATION: drop the is_interactive check - red."""
    import vaf.cli.gate as gate
    monkeypatch.setattr(gate, "is_interactive", lambda: False)
    world["answers"] = ["the-real-key", "new-password-123", "new-password-123"]
    r = _run(["reset-password", "alice", "--recovery-key"])
    assert r.exit_code == 1 and world["resets"] == []


def test_the_agents_own_command_cannot_reset_even_with_a_terminal(world, monkeypatch):
    """The agent can give its command a terminal of its own (`script`), and could read a
    recovery note left on the machine. MUTATION: drop the refuse_agent_command check - red."""
    from vaf.core.processes import AGENT_COMMAND_ENV
    monkeypatch.setenv(AGENT_COMMAND_ENV, "1")
    world["answers"] = ["the-real-key", "new-password-123", "new-password-123"]
    r = _run(["reset-password", "alice", "--recovery-key"])
    assert r.exit_code == 1 and world["resets"] == []


def test_a_wrong_recovery_key_resets_nothing_and_is_recorded(world):
    world["answers"] = ["guessed"]
    r = _run(["reset-password", "alice", "--recovery-key"])
    assert r.exit_code == 1 and world["resets"] == []
    assert world["events"] and world["events"][0][0] == "cli_password_gate_failed"


def test_the_recovery_key_lets_the_owner_who_forgot_the_password_back_in(world):
    """MUTATION: accept the recovery key without unwrapping it - the wrong-key test goes red;
    drop the reset - this one goes red."""
    world["answers"] = ["the-real-key", "new-password-123", "new-password-123"]
    r = _run(["reset-password", "alice", "--recovery-key"])
    assert r.exit_code == 0, r.output
    assert world["resets"] == [("alice", "new-password-123")]
    assert ("password_changed", "reset in a terminal on this machine (recovery key)") in world["events"]


def test_without_a_recorded_password_the_admin_password_route_points_to_the_key(world):
    r = _run(["reset-password", "alice"])
    assert r.exit_code == 1 and "--recovery-key" in r.output and world["resets"] == []


def test_a_record_changed_outside_vaf_is_shown_and_asked_about(world):
    """Resetting accepts the record as it stands (its role included): the person sees that
    first. MUTATION: skip the question - red."""
    world["sealed"] = False
    world["answers"] = ["the-real-key"]
    r = _run(["reset-password", "alice", "--recovery-key"], input_text="n\n")
    assert r.exit_code == 1 and "changed outside VAF" in r.output and world["resets"] == []


def test_the_terminal_reset_sets_a_fresh_second_factor_and_reseals(monkeypatch):
    """On a real row (SQLite): a new hash, the second factor set up again, and a row changed
    outside VAF re-sealed under the trusted rewrite."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import Session

    from vaf.auth import account_seal
    from vaf.auth.crypto import verify_password
    from vaf.auth.models import LocalUser, UserSession
    from vaf.auth.user_admin import apply_terminal_reset

    @compiles(JSONB, "sqlite")
    def _jsonb(_t, _c, **_kw):
        return "JSON"

    monkeypatch.setattr(account_seal, "_key_cache", b"k" * 32)
    monkeypatch.setattr(account_seal, "_enforced_cache", True)
    eng = create_engine("sqlite://")
    LocalUser.__table__.create(eng)
    UserSession.__table__.create(eng)
    with Session(eng) as s:
        s.add(LocalUser(username="alice", password_hash="old", role="admin",
                        totp_secret=b"s", totp_nonce=b"n", requires_2fa_setup=False))
        s.commit()
    with eng.begin() as conn:
        conn.execute(text("UPDATE local_users SET role = 'user'"))   # an outside change
    with Session(eng) as s:
        user = s.query(LocalUser).one()
        assert not account_seal.is_sealed(user)
        apply_terminal_reset(user, "new-password-123")
        with account_seal.trusted_rewrite():
            s.commit()
        assert verify_password(user.password_hash, "new-password-123")
        assert user.totp_secret is None and user.requires_2fa_setup is True
        assert account_seal.is_sealed(user)
    account_seal.reset_caches()
    _ = uuid


# -- an account changes its own password (Settings, Account) -----------------------

def _own_change(monkeypatch, user, caller_user_id=None):
    from fastapi import FastAPI, Request
    from starlette.testclient import TestClient

    import vaf.api.auth_routes as auth_routes
    import vaf.auth.rate_limit as rate_limit
    events = []

    class _Db:
        async def execute(self, *_a, **_k):
            # rowcount: the change also ends the account's other sessions (an UPDATE).
            return SimpleNamespace(scalar_one_or_none=lambda: user, rowcount=0,
                                   scalars=lambda: SimpleNamespace(all=lambda: []))

        async def commit(self):
            user.committed = True

    @contextlib.asynccontextmanager
    async def _get_db(*_a, **_k):
        yield _Db()

    monkeypatch.setattr(auth_routes, "get_auth_db", _get_db)
    # The real limiter, middleware and route both, counting what stays in its window (the
    # middleware counts a request before it runs and takes it back unless it failed).
    tracker = rate_limit._AttemptTracker()

    class _Counted:
        def __len__(self):
            return sum(len(v) for v in tracker._attempts.values())

    failures = _Counted()
    monkeypatch.setattr(rate_limit, "_tracker", tracker)
    import vaf.core.security_events as security_events
    monkeypatch.setattr(security_events, "log_security_event",
                        lambda kind, **kw: events.append((kind, kw.get("detail", ""))))
    app = FastAPI()

    @app.middleware("http")
    async def _as_caller(request: Request, call_next):
        if caller_user_id:
            request.state.user = {"user_id": caller_user_id}
        return await call_next(request)

    app.include_router(auth_routes.router)
    app.add_middleware(rate_limit.RateLimitMiddleware)
    return TestClient(app, raise_server_exceptions=False), events, failures


def _account(password="old-password-123", totp=False):
    from vaf.auth.crypto import encrypt_totp_secret, hash_password
    secret = nonce = None
    if totp:
        secret, nonce = encrypt_totp_secret("JBSWY3DPEHPK3PXP")
    return SimpleNamespace(id=uuid.uuid4(), username="bob", password_hash=hash_password(password),
                           role="user", totp_secret=secret, totp_nonce=nonce,
                           requires_2fa_setup=not totp, is_active=True, user_scope_id=uuid.uuid4(),
                           permissions={}, seal=None, committed=False)


def test_a_wrong_current_password_changes_nothing_and_counts_as_a_failed_login(monkeypatch):
    """Counted ONCE: the 401 is the rate limiter's to count (review finding: the route counted it
    a second time). MUTATION: drop the current-password check, or count it in the route - red."""
    import vaf.auth.crypto as crypto
    monkeypatch.setattr(crypto, "_get_jwt_secret", lambda: "x" * 48)
    user = _account()
    c, events, failures = _own_change(monkeypatch, user, caller_user_id=str(user.id))
    r = c.post("/api/auth/change-password", json={"current_password": "nope",
                                                  "new_password": "brand-new-password"})
    assert r.status_code == 401 and not user.committed
    assert len(failures) == 1 and events[0][0] == "login_failed"


def test_with_2fa_on_the_code_is_needed_too(monkeypatch):
    """A session left open on another screen must not be enough. MUTATION: skip the code - red."""
    import pyotp

    import vaf.auth.crypto as crypto
    monkeypatch.setattr(crypto, "_get_jwt_secret", lambda: "x" * 48)
    user = _account(totp=True)
    c, events, failures = _own_change(monkeypatch, user, caller_user_id=str(user.id))
    body = {"current_password": "old-password-123", "new_password": "brand-new-password"}
    assert c.post("/api/auth/change-password", json=dict(body, code="000000")).status_code == 400
    # A 400 the limiter does not see on its own: the route counts it. MUTATION: drop that - red.
    assert not user.committed and len(failures) == 1
    ok = c.post("/api/auth/change-password",
                json=dict(body, code=pyotp.TOTP("JBSWY3DPEHPK3PXP").now()))
    assert ok.status_code == 200 and user.committed
    from vaf.auth.crypto import verify_password
    assert verify_password(user.password_hash, "brand-new-password")
    assert ("password_changed", "changed by the account itself in Settings") in events


def test_every_refusal_of_the_own_change_is_said_in_the_readers_language():
    """The page maps each refusal code to a translated sentence and shows the server's English
    one for any code it does not know. Review finding: `twofa_unreadable` was missing. Every
    code the change can raise must be in the map, and every key it uses in the catalogue.
    MUTATION: drop a code from the map, or raise a new one in user_admin - red."""
    import inspect
    import json
    import re
    from pathlib import Path

    from vaf.auth import user_admin
    raised = set()
    for fn in (user_admin.change_own_password, user_admin.validate_password):
        raised |= set(re.findall(r'UserAdminError\(\s*"(\w+)"', inspect.getsource(fn)))
    assert {"wrong_password", "wrong_code", "password_too_short"} <= raised   # the scan works

    root = Path(__file__).resolve().parents[1]
    page = (root / "web/components/settings/AccountSection.tsx").read_text(encoding="utf-8")
    known = page.split("const known", 1)[1].split("};", 1)[0]
    mapped = dict(re.findall(r"(\w+): t\('(\w+)'", known))
    assert raised <= set(mapped), sorted(raised - set(mapped))
    catalogue = json.loads((root / "web/messages/en.json").read_text(encoding="utf-8"))["account"]
    assert set(mapped.values()) <= set(catalogue), sorted(set(mapped.values()) - set(catalogue))



def test_the_reset_asks_the_running_service_to_close_open_connections(world):
    """The command ended the sessions in the database, but a socket is checked only at its
    handshake and only the service holds it: an intruder's open chat stayed until a restart
    while the command said every device was signed out. MUTATION: skip the call - red."""
    world["answers"] = ["the-real-key", "a-new-password-1", "a-new-password-1"]
    r = _run(["reset-password", "alice", "--recovery-key"])
    assert r.exit_code == 0, r.output
    assert world["closed"] == ["11111111-0000-4000-8000-0000000000a1"]
    assert "Every device signed in to it is signed out" in " ".join(r.output.split())


def test_an_unconfirmed_close_is_said_plainly(world):
    """When the service does not confirm, the command does not claim it. MUTATION: print the
    success line whatever the answer - red."""
    world["answers"] = ["the-real-key", "a-new-password-1", "a-new-password-1"]
    world["close_answer"] = "it answered 500"
    r = _run(["reset-password", "alice", "--recovery-key"])
    assert r.exit_code == 0, r.output
    said = " ".join(r.output.split())
    assert "restart VAF" in said and "Every device signed in to it is signed out" not in said


def _loopback_server():
    """A stand-in for the running VAF on a free loopback port: records each request."""
    import http.server
    import threading

    seen = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_the_close_goes_through_the_loopback_lane_and_no_proxy(monkeypatch):
    """The same lane as the other commands (internal_api_base), never through a proxy from the
    environment: with HTTP_PROXY set and no loopback exception, the call went to the proxy,
    failed like a VAF that is not running, and the command said every device was signed out.
    MUTATION: honour the environment again - red."""
    import os

    import vaf.core.web_interface as web_interface
    from vaf.cli.cmd import users as cmd
    server, seen = _loopback_server()
    try:
        monkeypatch.setattr(web_interface, "internal_api_base",
                            lambda: f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        if os.name != "nt":
            # Linux and macOS read both spellings as two variables; on Windows the
            # environment is case-insensitive and the upper-case lines above are both.
            monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
            monkeypatch.delenv("no_proxy", raising=False)
        assert cmd._close_open_connections("u1") is None
        assert seen == ["/api/users/u1/sign-out"]
    finally:
        server.shutdown()


def test_only_a_refused_connection_means_no_vaf_runs(monkeypatch):
    """A refused connection: nothing listens, nothing is open. A timeout, a TLS or a proxy
    failure: a VAF may run and hold the sockets, so the restart warning. MUTATION: treat
    every ConnectionError as not running - red."""
    import socket

    import requests

    import vaf.core.web_interface as web_interface
    from vaf.cli.cmd import users as cmd
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(web_interface, "internal_api_base", lambda: f"http://127.0.0.1:{free_port}")
    assert cmd._close_open_connections("u1") is None             # refused: really not running
    worded = requests.exceptions.ConnectionError("reset")
    worded.reason = "a reason that is only text"
    for error in (requests.exceptions.ProxyError("proxy"), requests.exceptions.SSLError("tls"),
                  requests.exceptions.ConnectTimeout("slow"), requests.exceptions.ReadTimeout("hung"),
                  worded):
        def _fail(self, *_a, _error=error, **_k):
            raise _error
        monkeypatch.setattr(requests.Session, "post", _fail)
        why = cmd._close_open_connections("u1")
        assert why and why.startswith("no answer:"), (error, why)


def test_the_close_reads_the_answer(monkeypatch):
    import requests

    from vaf.cli.cmd import users as cmd

    class _Answer:
        def __init__(self, code):
            self.status_code = code

    answers = [_Answer(200), _Answer(500)]
    monkeypatch.setattr(requests.Session, "post", lambda self, *a, **k: answers.pop(0))
    assert cmd._close_open_connections("u1") is None
    assert cmd._close_open_connections("u1") == "it answered 500"


# ── sessions, signing out and tokens for programs, against the real session store ───

def test_sessions_sign_out_and_token(monkeypatch):
    """`vaf users sessions` lists the live sessions, `sign-out` ends them and asks the running
    VAF to close the open chats, and `token` needs a person at the terminal and the proof
    before it mints anything. All output is ASCII. MUTATION: mint before the proof; print
    nothing for the token - red."""
    import re

    from auth_harness import Harness

    import vaf.cli.gate as gate
    from vaf.auth.crypto import hash_password
    h = Harness(monkeypatch)
    user_id, _ = h.add_user("alice", "alice-password-1", no_second_factor=True)
    a = h.sign_in(h.client("192.168.81.1"), "alice", "alice-password-1", None)
    b = h.sign_in(h.client("192.168.81.2"), "alice", "alice-password-1", None)
    closed = []
    monkeypatch.setattr(users_cmd, "_close_open_connections", lambda uid: closed.append(uid) or None)
    monkeypatch.setattr(gate, "require_admin_password", lambda **_k: True)

    r = _run(["sessions", "alice"])
    assert r.exit_code == 0, r.output
    assert "192.168.81.1" in r.output and "192.168.81.2" in r.output and "browser" in r.output
    assert r.output.isascii()
    # An 80-column terminal reads every line whole: the facts go on indented lines, the id last.
    lines = r.output.splitlines()
    assert all(len(line) <= 80 for line in lines), max(lines, key=len)
    heads = [i for i, line in enumerate(lines) if line.startswith("browser: ")]
    assert len(heads) == 2
    for i in heads:
        assert lines[i + 1].startswith("    192.168.81.") and lines[i + 3].startswith("    id ")
    assert _run(["sessions", "nobody"]).exit_code == 1

    def _probe(token):
        return h.client("192.168.81.9").get("/api/probe",
                                            headers={"Authorization": f"Bearer {token}"}).status_code

    r = _run(["sign-out", "alice"])
    assert r.exit_code == 0 and "2 session(s) of alice ended" in r.output, r.output
    assert closed == [user_id] and _probe(a) == 401 and _probe(b) == 401

    # The token: no terminal, then a wrong proof, then the right one.
    rows_before = len(h.sessions())
    monkeypatch.setattr(gate, "is_interactive", lambda: False)
    r = _run(["token", "alice", "--label", "backup"])
    assert r.exit_code == 1 and len(h.sessions()) == rows_before
    monkeypatch.setattr(gate, "is_interactive", lambda: True)
    monkeypatch.setattr(gate, "_stored_hash", lambda: (hash_password("the-admin-password"), False))
    answers = ["guess-1", "guess-2", "guess-3"]
    monkeypatch.setattr(users_cmd.getpass, "getpass", lambda _p="": answers.pop(0))
    r = _run(["token", "alice", "--label", "backup"])
    assert r.exit_code == 1 and len(h.sessions()) == rows_before
    assert not h.kinds("program_token_created")
    answers[:] = ["the-admin-password"]
    r = _run(["token", "alice", "--label", "backup", "--days", "90"])
    assert r.exit_code == 0, r.output
    assert r.output.isascii()
    tokens = re.findall(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", r.output, re.M)
    assert len(tokens) == 1
    me = h.client("192.168.81.3").get("/api/auth/me", headers={"Authorization": f"Bearer {tokens[0]}"})
    assert me.status_code == 200 and me.json()["username"] == "alice"
    created = h.kinds("program_token_created")
    assert len(created) == 1 and "admin password" in created[0]["detail"]
    assert "90 days" in created[0]["detail"] and tokens[0] not in str(created)
    assert _run(["token", "alice", "--label", "x", "--days", "14"]).exit_code == 1


def test_the_token_hint_gives_curl_the_authority(monkeypatch):
    """Under VAF's own certificate the example call names the authority (`--cacert`) and never
    `-k`, which would send the token to whoever answers; with TLS off it is plain HTTP on the
    plain port. MUTATION: the old one-line hint (https, no authority) - red."""
    import vaf.network.binding as binding
    import vaf.network.ssl_utils as ssl_utils

    monkeypatch.setattr(binding, "access_addresses", lambda: [SimpleNamespace(ip="192.168.81.5")])
    served = {"source": "automatic"}
    monkeypatch.setattr(ssl_utils, "served_certificate",
                        lambda cfg=None: SimpleNamespace(source=served["source"]))
    monkeypatch.setattr(ssl_utils, "ca_certificate_path", lambda: None)

    monkeypatch.setattr(binding, "resolve_lan_access_ports", lambda wait_for_proxy=False: (8443, 8001))
    url, source = users_cmd._access_hint()
    assert (url, source) == ("https://192.168.81.5:8443", "automatic")
    hint = "\n".join(users_cmd._token_use_lines(url, source))
    assert "curl --cacert vaf-authority.pem -H" in hint and "vaf server ca" in hint
    assert " -k " not in hint.replace("Not -k instead", "")

    served["source"] = "own"
    assert "--cacert" not in "\n".join(users_cmd._token_use_lines(*users_cmd._access_hint()))

    served["source"] = "none"
    monkeypatch.setattr(binding, "resolve_lan_access_ports", lambda wait_for_proxy=False: (8001, 3000))
    url, source = users_cmd._access_hint()
    assert url == "http://192.168.81.5:8001"
    assert users_cmd._token_use_lines(url, source) == [
        'Use it as: curl -H "Authorization: Bearer <token>" http://192.168.81.5:8001/api/auth/me']
