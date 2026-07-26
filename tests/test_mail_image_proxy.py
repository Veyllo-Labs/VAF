# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""The remote-image proxy: SSRF hardening on the direct path, and correct
behaviour behind a site egress proxy.

Direct path: the host is resolved ONCE and the socket pinned to that validated IP
(assert_ip_safe / resolve_pinned_target), so a rebind to a private address between
check and connect is impossible; only ports 80/443 are reachable.

Proxied path (managed networks): the pin is impossible through CONNECT, so the
guarantees shift - the site proxy performs egress control and DNS, VAF still
refuses a host that resolves locally to an internal address, and a name only the
proxy can resolve is passed through rather than treated as blocked."""
import asyncio
import os
import socket

import pytest
from fastapi import HTTPException

import vaf.network.binding as binding
from vaf.api import mail_routes
from vaf.network.binding import assert_ip_safe, resolve_pinned_target


_PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")


@pytest.fixture(autouse=True)
def _no_inherited_proxy(monkeypatch):
    """Start every test from "direct connect". Without this, a developer or CI
    machine that happens to export HTTPS_PROXY would silently push the direct-path
    tests down the proxy branch - they would still pass, while testing something
    else entirely."""
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)


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


def test_system_proxy_for_env_matrix(monkeypatch):
    """A managed network publishes its egress proxy through these variables.
    Getting the matrix wrong either leaks around the proxy or breaks image loading
    for everyone behind one."""
    assert binding.system_proxy_for("https", "t.example") is None

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    assert binding.system_proxy_for("https", "t.example") == "http://proxy.corp:3128"

    # Uppercase HTTP_PROXY is attacker-influenced in CGI deployments, so it is not
    # honoured for http targets - on POSIX. Windows environment variables are
    # case-insensitive and Python mirrors that, so there the two names ARE one
    # variable and the distinction cannot exist. Asserting the POSIX behaviour
    # everywhere is what turned this green locally and red on the Windows runner.
    monkeypatch.setenv("HTTP_PROXY", "http://evil:8080")
    if os.name == "nt":
        assert binding.system_proxy_for("http", "t.example") == "http://evil:8080"
    else:
        assert binding.system_proxy_for("http", "t.example") is None

    monkeypatch.setenv("http_proxy", "http://proxy.corp:3128")
    assert binding.system_proxy_for("http", "t.example") == "http://proxy.corp:3128"

    monkeypatch.setenv("NO_PROXY", ".example,corp.internal")
    assert binding.system_proxy_for("https", "a.example") is None       # suffix
    assert binding.system_proxy_for("https", "corp.internal") is None   # exact
    assert binding.system_proxy_for("https", "other.net") == "http://proxy.corp:3128"
    monkeypatch.setenv("NO_PROXY", "*")
    assert binding.system_proxy_for("https", "anything") is None

    monkeypatch.delenv("NO_PROXY")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://box:1080")
    assert binding.system_proxy_for("https", "t.example") is None       # unsupported


def _proxy_env(monkeypatch, url="http://proxy.corp:3128"):
    monkeypatch.setenv("HTTPS_PROXY", url)   # the autouse fixture already cleared


class _Resp:
    status = 200
    headers = {"Content-Type": "image/png"}

    def read(self, _n):
        return b"\x89PNG"


def test_image_proxy_uses_the_site_proxy_with_an_absolute_target(monkeypatch):
    """Behind a proxy the request must be absolute-form and carry the tracking
    query untouched; a path-only request would be meaningless to the proxy."""
    import urllib3
    _proxy_env(monkeypatch)
    seen = {}

    class _PM:
        def __init__(self, url, **kw):
            seen["proxy"] = url

        def request(self, method, target, **kw):
            seen["target"] = target
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(urllib3, "ProxyManager", _PM)
    monkeypatch.setattr(binding.socket, "getaddrinfo", lambda *a, **k: _addrinfo(["93.184.216.34"]))

    r = asyncio.run(mail_routes.image_proxy(
        url="https://track.example/p.gif?u=abc", _user={"username": "u"}))
    assert r.status_code == 200
    assert seen["proxy"] == "http://proxy.corp:3128"
    assert seen["target"] == "https://track.example:443/p.gif?u=abc"


def test_image_proxy_behind_proxy_still_refuses_a_locally_private_host(monkeypatch):
    """The IP pin is impossible through CONNECT, but the SSRF check that still
    works must stay: a name that resolves HERE to an internal address is refused
    before it is handed to the proxy."""
    import urllib3
    _proxy_env(monkeypatch)
    monkeypatch.setattr(binding.socket, "getaddrinfo", lambda *a, **k: _addrinfo(["10.0.0.7"]))
    monkeypatch.setattr(urllib3, "ProxyManager",
                        lambda *a, **k: pytest.fail("must not reach the proxy"))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(mail_routes.image_proxy(
            url="https://internal.example/x.png", _user={"username": "u"}))
    assert ei.value.status_code == 403


def test_image_proxy_behind_proxy_allows_a_split_horizon_name(monkeypatch):
    """Only the proxy can resolve some names in a managed network. An unresolvable
    host must be passed to it, not treated as blocked."""
    import urllib3
    _proxy_env(monkeypatch)

    def _boom(*a, **k):
        raise socket.gaierror("proxy-only DNS")

    monkeypatch.setattr(binding.socket, "getaddrinfo", _boom)
    reached = {}

    class _PM:
        def __init__(self, *a, **k):
            reached["yes"] = True

        def request(self, *a, **k):
            return _Resp()

        def close(self):
            pass

    monkeypatch.setattr(urllib3, "ProxyManager", _PM)
    r = asyncio.run(mail_routes.image_proxy(
        url="https://only-via-proxy.corp/x.png", _user={"username": "u"}))
    assert r.status_code == 200 and reached.get("yes")


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
