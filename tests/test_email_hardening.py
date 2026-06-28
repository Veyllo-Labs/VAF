# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression tests for the email-subsystem hardening pass.

Covers the security/correctness invariants that are easy to break silently:
  - non-admins must never receive secret config values (API keys, client secrets, JWT, DB URL)
  - RFC-2047 encoded headers must be decoded to readable Unicode
  - recipient strings must be parsed/validated (for cc/bcc + SMTP envelope)
  - the SSRF guard must reject non-public mail hosts (with an explicit opt-in override)
  - auto-sync must include UUID-scoped (network) users, not just legacy/per-username configs
  - the OAuth callback binding allows a genuine loopback (desktop) callback via signed state, but
    still requires a matching session for LAN callers and cannot be bypassed by a spoofed XFF

All tests are network-free and Pillow-free.
"""
import pytest

from fastapi import HTTPException

from vaf.core.config import Config
from vaf.core.email_transport import _decode_mail_header, normalize_recipients
from vaf.network.binding import assert_safe_remote_host
import vaf.api.email_routes as er
import vaf.api.oauth_session_binding as ob


# --- A.1: config secret redaction -------------------------------------------------

def test_config_for_user_redacts_secrets_for_non_admin():
    cfg = {
        "api_key_openai": "sk-SECRET",
        "email_oauth_google_client_secret": "CLIENTSECRET",
        "local_network_jwt_secret": "JWT",
        "secure_store_kek": "KEK",
        "memory_db_url": "postgresql://user:pw@host/db",
        "redis_url": "redis://:pw@host:6379/0",
        # non-secret, admin-only-to-write but safe to READ — must survive:
        "api_model_openai": "gpt-x",
        "language": "de",
    }
    out = Config.config_for_user(cfg, user_scope_id="scope-1", role="user")
    for secret in (
        "api_key_openai",
        "email_oauth_google_client_secret",
        "local_network_jwt_secret",
        "secure_store_kek",
        "memory_db_url",
        "redis_url",
    ):
        assert secret not in out, f"{secret} leaked to non-admin"
    # Non-secret keys the UI legitimately reads are preserved.
    assert out.get("api_model_openai") == "gpt-x"
    assert out.get("language") == "de"


def test_config_for_user_admin_keeps_secrets():
    cfg = {"api_key_openai": "sk-SECRET", "local_network_jwt_secret": "JWT"}
    out = Config.config_for_user(cfg, user_scope_id=None, role="admin")
    assert out.get("api_key_openai") == "sk-SECRET"
    assert out.get("local_network_jwt_secret") == "JWT"


def test_is_secret_config_key_classification():
    assert Config.is_secret_config_key("api_key_anthropic")
    assert Config.is_secret_config_key("email_oauth_google_client_secret")
    assert Config.is_secret_config_key("local_network_jwt_secret")
    assert Config.is_secret_config_key("secure_store_kek")
    assert not Config.is_secret_config_key("api_model_openai")
    assert not Config.is_secret_config_key("language")


# --- C.10: RFC-2047 header decoding ----------------------------------------------

def test_decode_mail_header_encoded_words():
    assert _decode_mail_header("=?UTF-8?B?w6TDtsO8?=") == "äöü"
    assert _decode_mail_header("=?UTF-8?Q?Caf=C3=A9?=") == "Café"


def test_decode_mail_header_passthrough():
    assert _decode_mail_header("Plain Sender") == "Plain Sender"
    assert _decode_mail_header("") == ""
    assert _decode_mail_header(None) == ""


# --- C.13: recipient parsing/validation ------------------------------------------

def test_normalize_recipients():
    assert normalize_recipients("a@x.com, b@y.com") == ["a@x.com", "b@y.com"]
    assert normalize_recipients("Max Mustermann <max@x.com>") == ["max@x.com"]
    assert normalize_recipients(["a@x.com", "a@x.com"]) == ["a@x.com"]  # dedupe, order kept
    assert normalize_recipients("not-an-email") == []
    assert normalize_recipients("") == []
    assert normalize_recipients(None) == []


# --- A.3: SSRF guard on mail host ------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.10", "10.0.0.5", "169.254.169.254"])
def test_ssrf_guard_rejects_non_public(host):
    with pytest.raises(ValueError):
        assert_safe_remote_host(host)


def test_ssrf_guard_allows_public():
    # literal public IP — no DNS lookup needed
    assert_safe_remote_host("8.8.8.8")


def test_ssrf_guard_override_allows_private_but_never_link_local():
    # opt-in allows RFC-1918 / loopback...
    assert_safe_remote_host("192.168.1.10", allow_private=True)
    assert_safe_remote_host("127.0.0.1", allow_private=True)
    # ...but the cloud-metadata / link-local range stays blocked even with the override.
    with pytest.raises(ValueError):
        assert_safe_remote_host("169.254.169.254", allow_private=True)


# --- B.9: auto-sync must include UUID-scoped network users ------------------------

def test_collect_auto_sync_includes_per_scope(monkeypatch):
    cfg = {
        "email_config": {"accounts": [{"account_id": "admin@x.com", "auto_sync_enabled": True}]},
        "email_config_by_user": {"bob": {"accounts": [{"account_id": "bob@x.com", "auto_sync_enabled": True}]}},
        "email_config_by_scope": {
            "scope-alice": {"accounts": [{"account_id": "alice@x.com", "auto_sync_enabled": True}]},
            "scope-carol": {"accounts": [{"account_id": "carol@x.com", "auto_sync_enabled": False}]},
            "local-admin-scope": {"accounts": [{"account_id": "dup@x.com", "auto_sync_enabled": True}]},
        },
    }
    monkeypatch.setattr(er.Config, "get", lambda key, default=None: cfg.get(key, default))
    monkeypatch.setattr(er, "get_local_admin_username", lambda: "admin")
    monkeypatch.setattr(er, "get_local_admin_scope_id", lambda: "local-admin-scope")

    items = er._collect_auto_sync_accounts()
    accounts = {a.get("account_id") for (_u, a, _ec, _s) in items}
    scopes = {s for (_u, _a, _ec, s) in items}

    assert "admin@x.com" in accounts   # legacy/local admin
    assert "bob@x.com" in accounts     # per-username
    assert "alice@x.com" in accounts   # per-scope (the fix)
    assert "carol@x.com" not in accounts  # auto_sync disabled
    assert "dup@x.com" not in accounts    # local-admin scope skipped (covered by legacy)
    assert "scope-alice" in scopes

    # per-scope entry carries its scope id and no username; legacy carries username and no scope
    for cfg_user, acc, _ec, scope in items:
        if acc.get("account_id") == "alice@x.com":
            assert scope == "scope-alice" and cfg_user is None
        if acc.get("account_id") == "admin@x.com":
            assert scope is None


# --- OAuth callback binding: loopback (desktop) vs LAN vs spoofing -----------------

class _FakeClient:
    def __init__(self, host): self.host = host

class _FakeHeaders:
    def __init__(self, d): self._d = {k.lower(): v for k, v in (d or {}).items()}
    def get(self, k, default=None): return self._d.get(k.lower(), default)

class _FakeState:
    def __init__(self, user): self.user = user

class _FakeRequest:
    def __init__(self, host, headers=None, user=None):
        self.client = _FakeClient(host)
        self.headers = _FakeHeaders(headers)
        self.state = _FakeState(user)


@pytest.fixture
def _network_mode_on(monkeypatch):
    _orig = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda k, d=None: True if k == "local_network_enabled" else _orig(k, d)))


def _raises(req):
    try:
        ob.enforce_callback_actor_binding(req, "alice", "scope-alice")
        return None
    except HTTPException as e:
        return e.status_code


def test_oauth_binding_loopback_desktop_allowed_without_cookie(_network_mode_on):
    # Desktop: system browser callback arrives via the local proxy (peer 127.0.0.1, XFF 127.0.0.1),
    # no session cookie. Trusted via signed state -> allowed.
    req = _FakeRequest("127.0.0.1", {"x-forwarded-for": "127.0.0.1"}, user=None)
    assert _raises(req) is None


def test_oauth_binding_lan_requires_login(_network_mode_on):
    # LAN user via the proxy (real peer 192.168.x via XFF), no cookie -> 401.
    req = _FakeRequest("127.0.0.1", {"x-forwarded-for": "192.168.1.50"}, user=None)
    assert _raises(req) == 401


def test_oauth_binding_spoofed_xff_cannot_fake_loopback(_network_mode_on):
    # LAN attacker connecting DIRECTLY (peer is a real LAN IP) forging XFF=127.0.0.1 must NOT be
    # treated as loopback (XFF is only trusted when the immediate peer is itself loopback/the proxy).
    req = _FakeRequest("192.168.1.50", {"x-forwarded-for": "127.0.0.1"}, user=None)
    assert _raises(req) == 401


def test_oauth_binding_lan_user_mismatch_rejected(_network_mode_on):
    req = _FakeRequest("127.0.0.1", {"x-forwarded-for": "192.168.1.50"},
                       user={"username": "bob", "user_scope_id": "scope-bob"})
    assert _raises(req) == 403


def test_oauth_binding_lan_user_match_allowed(_network_mode_on):
    req = _FakeRequest("127.0.0.1", {"x-forwarded-for": "192.168.1.50"},
                       user={"username": "alice", "user_scope_id": "scope-alice"})
    assert _raises(req) is None


def test_oauth_binding_noop_when_network_off(monkeypatch):
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: False if k == "local_network_enabled" else d))
    req = _FakeRequest("192.168.1.50", {"x-forwarded-for": "10.0.0.9"}, user=None)
    assert _raises(req) is None  # binding disabled entirely when not in network mode
