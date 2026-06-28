# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Regression tests for the shared OAuth callback base-URL helper.

The OAuth redirect_uri must point at the port the integrated HTTPS proxy ACTUALLY bound (8443 fallback
when privileged 443 is unbindable), and email + cloud must build it identically. These were silent
breakages before: email/cloud hardcoded 443 / used request.base_url, so the desktop callback was
unreachable. Network-free (Config + runtime_status are monkeypatched; no sockets)."""
import pytest

from vaf.core.config import Config
from vaf.network import runtime_status
from vaf.network.oauth_redirect import effective_https_suffix, oauth_callback_base_url


def _fake_config(monkeypatch, overrides: dict):
    _orig = Config.get
    monkeypatch.setattr(Config, "get", staticmethod(lambda k, d=None: overrides.get(k, _orig(k, d))))


def _fake_proxy(monkeypatch, effective):
    monkeypatch.setattr(runtime_status, "get_proxy_status",
                        lambda: {"effective_https_port": effective})


# --- effective_https_suffix ------------------------------------------------------

def test_suffix_443_fallback_to_8443_when_unbound(monkeypatch):
    _fake_config(monkeypatch, {"local_network_https_port": 443})
    _fake_proxy(monkeypatch, None)  # proxy not reported yet
    assert effective_https_suffix() == ":8443"


def test_suffix_uses_bound_effective_port(monkeypatch):
    _fake_config(monkeypatch, {"local_network_https_port": 443})
    _fake_proxy(monkeypatch, 8443)
    assert effective_https_suffix() == ":8443"


def test_suffix_empty_when_proxy_really_bound_443(monkeypatch):
    # Mac/Windows with admin: proxy actually bound 443 -> no port suffix.
    _fake_config(monkeypatch, {"local_network_https_port": 443})
    _fake_proxy(monkeypatch, 443)
    assert effective_https_suffix() == ""


def test_suffix_custom_bound_port(monkeypatch):
    _fake_config(monkeypatch, {"local_network_https_port": 9443})
    _fake_proxy(monkeypatch, 9443)
    assert effective_https_suffix() == ":9443"


# --- oauth_callback_base_url -----------------------------------------------------

def test_base_network_tls_uses_proxy_port(monkeypatch):
    _fake_config(monkeypatch, {
        "local_network_enabled": True, "local_network_tls_enabled": True,
        "local_network_https_port": 443, "email_oauth_callback_base_url": "",
    })
    _fake_proxy(monkeypatch, 8443)
    assert oauth_callback_base_url("email_oauth_callback_base_url") == "https://localhost:8443"


def test_base_localhost_mode(monkeypatch):
    _fake_config(monkeypatch, {
        "local_network_enabled": False, "local_network_tls_enabled": False,
        "local_network_port": 8001, "email_oauth_callback_base_url": "",
    })
    assert oauth_callback_base_url("email_oauth_callback_base_url") == "http://localhost:8001"


def test_base_override_wins(monkeypatch):
    _fake_config(monkeypatch, {
        "local_network_enabled": True, "local_network_tls_enabled": True,
        "cloud_oauth_callback_base_url": "https://mail.example.com",
    })
    _fake_proxy(monkeypatch, 8443)
    assert oauth_callback_base_url("cloud_oauth_callback_base_url") == "https://mail.example.com"


def test_email_and_cloud_bases_are_identical(monkeypatch):
    # The whole point: cloud must build the same reachable base as email (no override set).
    _fake_config(monkeypatch, {
        "local_network_enabled": True, "local_network_tls_enabled": True,
        "local_network_https_port": 443,
        "email_oauth_callback_base_url": "", "cloud_oauth_callback_base_url": "",
    })
    _fake_proxy(monkeypatch, 8443)
    email = oauth_callback_base_url("email_oauth_callback_base_url")
    cloud = oauth_callback_base_url("cloud_oauth_callback_base_url")
    assert email == cloud == "https://localhost:8443"


# --- cloud redirect_uri + email delegation consistency ---------------------------

def test_cloud_redirect_uri_uses_effective_port(monkeypatch):
    from vaf.cloud.oauth_cloud import get_cloud_callback_redirect_uri
    _fake_config(monkeypatch, {
        "local_network_enabled": True, "local_network_tls_enabled": True,
        "local_network_https_port": 443, "cloud_oauth_callback_base_url": "",
    })
    _fake_proxy(monkeypatch, 8443)
    assert get_cloud_callback_redirect_uri("http://ignored:3000") == \
        "https://localhost:8443/api/cloud/oauth/callback"


def test_email_helper_delegates_to_shared(monkeypatch):
    import vaf.api.email_routes as er
    _fake_config(monkeypatch, {
        "local_network_enabled": True, "local_network_tls_enabled": True,
        "local_network_https_port": 443, "email_oauth_callback_base_url": "",
    })
    _fake_proxy(monkeypatch, 8443)
    assert er._oauth_callback_base_url() == "https://localhost:8443"
    assert er._effective_https_suffix() == ":8443"
