# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""C9/T11: the remote-image proxy must close the DNS-rebinding TOCTOU. The host
is resolved ONCE and the socket pinned to that validated IP (assert_ip_safe /
resolve_pinned_target), so a rebind to a private address between check and
connect is impossible; only the standard web ports (80/443) are reachable."""
import asyncio
import socket

import pytest
from fastapi import HTTPException

import vaf.network.binding as binding
from vaf.api import mail_routes
from vaf.network.binding import assert_ip_safe, resolve_pinned_target


def _addrinfo(ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)) for ip in ips]


def test_assert_ip_safe_allows_public_blocks_internal():
    assert_ip_safe("1.2.3.4") is None                      # public: no raise
    for bad in ("192.168.1.5", "10.0.0.1", "172.16.0.1",   # RFC-1918 private
                "127.0.0.1", "::1",                          # loopback
                "169.254.169.254"):                          # link-local cloud metadata
        with pytest.raises(ValueError):
            assert_ip_safe(bad)


def test_assert_ip_safe_private_opt_in():
    # allow_private lets a genuine LAN target through, but never metadata/link-local.
    assert_ip_safe("192.168.1.5", allow_private=True) is None
    with pytest.raises(ValueError):
        assert_ip_safe("169.254.169.254", allow_private=True)


def test_resolve_pinned_target_returns_public_ip(monkeypatch):
    monkeypatch.setattr(binding.socket, "getaddrinfo", lambda *a, **k: _addrinfo(["93.184.216.34"]))
    assert resolve_pinned_target("example.com", 443) == "93.184.216.34"


def test_resolve_pinned_target_rebinding_blocked(monkeypatch):
    # DNS rebinding: the answer at connect time is a private address. Because we
    # resolve+validate+pin in ONE step, that address is validated (and rejected)
    # instead of silently connected to.
    monkeypatch.setattr(binding.socket, "getaddrinfo", lambda *a, **k: _addrinfo(["10.0.0.7"]))
    with pytest.raises(ValueError):
        resolve_pinned_target("evil.example", 443)


def test_resolve_pinned_target_validates_every_address(monkeypatch):
    # A multi-record answer where ONE address is internal must be rejected whole.
    monkeypatch.setattr(binding.socket, "getaddrinfo",
                        lambda *a, **k: _addrinfo(["93.184.216.34", "192.168.0.9"]))
    with pytest.raises(ValueError):
        resolve_pinned_target("mixed.example", 443)


def test_resolve_pinned_target_unresolvable_propagates(monkeypatch):
    def _boom(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(binding.socket, "getaddrinfo", _boom)
    with pytest.raises(OSError):   # gaierror is an OSError subclass: caller maps to 502
        resolve_pinned_target("nx.example", 443)


def test_image_proxy_rejects_nonstandard_port(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mail_routes.image_proxy(
            url="http://example.com:8080/x.png", _user={"username": "u"}))
    assert ei.value.status_code == 400


def test_image_proxy_rejects_non_http_scheme(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mail_routes.image_proxy(
            url="file:///etc/passwd", _user={"username": "u"}))
    assert ei.value.status_code == 400


def test_image_proxy_blocked_ip_logs_event_and_403(monkeypatch):

    def _blocked(*a, **k):
        raise ValueError("private")
    monkeypatch.setattr(binding, "resolve_pinned_target", _blocked)

    events = []
    import vaf.core.security_events as sec
    monkeypatch.setattr(sec, "log_security_event",
                        lambda kind, **kw: events.append((kind, kw)))

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mail_routes.image_proxy(
            url="https://rebind.example/tracker.png", _user={"username": "u"}))
    assert ei.value.status_code == 403
    assert events and events[0][0] == "mail_image_proxy_blocked"
