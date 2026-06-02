"""Roundtrip + OAuth refresh-token-rotation tests for the credential stores.

All tests force the encrypted-file fallback path (keyring unavailable) so the
SecureBlobStore integration and the refresh-rotation fix are exercised directly.
"""

import time

import pytest

import vaf.core.secure_store as ss
import vaf.core.credential_store as cs
import vaf.cloud.credential_cloud as cc
import vaf.core.oauth_pkce as op
import vaf.cloud.oauth_cloud as oc
from vaf.core.config import Config


class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self.text = ""
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def fallback_env(tmp_path, monkeypatch):
    """Point both stores at tmp paths and force the encrypted-file fallback."""
    monkeypatch.setattr(Config, "APP_DIR", tmp_path, raising=False)
    monkeypatch.setattr(Config, "CONFIG_FILE", tmp_path / "config.json", raising=False)
    ss.set_passphrase(None)
    # Force fallback (no keyring) in both modules; they bound the name at import.
    monkeypatch.setattr(cs, "keyring_available", lambda: False)
    monkeypatch.setattr(cc, "keyring_available", lambda: False)
    # Fresh per-test store instances under tmp.
    monkeypatch.setattr(
        cs, "_store_singleton",
        ss.SecureBlobStore("email", tmp_path / "email_credentials.enc", "email_credentials_key"),
    )
    monkeypatch.setattr(
        cc, "_store_singleton",
        ss.SecureBlobStore("cloud", tmp_path / "cloud_credentials.enc", "cloud_credentials_key"),
    )
    yield tmp_path
    ss.set_passphrase(None)


# ── roundtrips through the encrypted fallback ─────────────────────────────────

def test_email_imap_roundtrip(fallback_env):
    cs.set_email_imap_password("user@example.com", "hunter2")
    creds = cs.get_email_credentials("user@example.com", "imap")
    assert creds == {"password": "hunter2", "type": "imap"}


def test_cloud_oauth_roundtrip(fallback_env):
    cc.set_cloud_oauth_tokens("user@gmail.com", "google_drive", "acc", "ref", time.time() + 3600)
    creds = cc.get_cloud_credentials("user@gmail.com", "google_drive")
    assert creds["access_token"] == "acc"
    assert creds["refresh_token"] == "ref"


# ── refresh-token rotation (the bug fix) ───────────────────────────────────────

def test_email_refresh_persists_new_refresh_token(fallback_env, monkeypatch):
    acct = "user@gmail.com"
    cs.set_email_oauth_tokens(acct, "gmail", "old-access", "old-refresh", expires_at=time.time() - 100)
    monkeypatch.setattr(
        op.requests, "post",
        lambda *a, **k: _FakeResp({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}),
    )

    token = op.get_valid_access_token(acct, "gmail")
    assert token == "new-access"
    stored = cs.get_email_credentials(acct, "gmail")
    assert stored["refresh_token"] == "new-refresh"  # rotated token persisted
    assert stored["access_token"] == "new-access"


def test_email_refresh_keeps_old_token_when_absent(fallback_env, monkeypatch):
    acct = "user@gmail.com"
    cs.set_email_oauth_tokens(acct, "gmail", "old-access", "keep-me", expires_at=time.time() - 100)
    monkeypatch.setattr(
        op.requests, "post",
        lambda *a, **k: _FakeResp({"access_token": "new-access", "expires_in": 3600}),  # no refresh_token
    )

    assert op.get_valid_access_token(acct, "gmail") == "new-access"
    assert cs.get_email_credentials(acct, "gmail")["refresh_token"] == "keep-me"


def test_cloud_refresh_persists_new_refresh_token(fallback_env, monkeypatch):
    acct = "user@gmail.com"
    cc.set_cloud_oauth_tokens(acct, "google_drive", "old-access", "old-refresh", expires_at=time.time() - 100)
    monkeypatch.setattr(
        oc.requests, "post",
        lambda *a, **k: _FakeResp({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}),
    )

    token = oc.get_valid_access_token(acct, "google_drive")
    assert token == "new-access"
    assert cc.get_cloud_credentials(acct, "google_drive")["refresh_token"] == "new-refresh"
