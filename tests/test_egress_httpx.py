# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""egress_httpx_client: the destination guard for httpx, which the MCP SDK speaks.

The same rules as the requests session (tests/test_egress_session.py, whose resolver and
local server this file reuses): loopback and forbidden refused before any connection, every
redirect judged, the connection pinned to the checked address with the certificate verified
against the name. Each test names the mutation it catches."""
import asyncio
import http.server
import ssl
import threading

import httpx
import pytest

from tests.test_egress_session import _Server, _world, server  # noqa: F401 - fixtures
from vaf.network.egress import EgressPolicy, EgressRefused, egress_httpx_client


def _get(url, policy=None, **kw):
    async def go():
        async with egress_httpx_client(policy=policy or EgressPolicy(), **kw) as client:
            return await client.get(url)
    return asyncio.run(go())


def test_a_public_host_is_reached_under_its_name(server):
    """MUTATION: rewrite the Host header to the address, and the server sees the address."""
    r = _get(f"http://public.test:{server.port}/ok")
    assert r.status_code == 200 and r.text == f"host=public.test:{server.port}"


def test_this_machine_is_refused_before_any_connection(server):
    """MUTATION: skip the judgement in handle_async_request."""
    with pytest.raises(httpx.ConnectError) as refused:
        _get(f"http://loop.test:{server.port}/api/users", EgressPolicy(allow_private=True))
    assert isinstance(refused.value.__cause__, EgressRefused)
    assert server.hits == []


def test_a_refusal_is_the_transport_failure_httpx_callers_handle(server):
    """The MCP SDK handles an httpx.TransportError; a bare EgressRefused escaped its
    handling. The reason stays the message. MUTATION: let EgressRefused leave the transport."""
    with pytest.raises(httpx.TransportError) as refused:
        _get(f"http://loop.test:{server.port}/x")
    assert "Only internet addresses are fetched" in str(refused.value)
    assert refused.value.request is not None


def test_a_redirect_is_judged_like_the_first_request(server):
    """httpx follows redirects (the MCP SDK's default) and calls the transport per hop.
    MUTATION: judge only the first request."""
    with pytest.raises(httpx.ConnectError) as refused:
        _get(f"http://public.test:{server.port}/redir/http://loop.test:{server.port}/x")
    assert isinstance(refused.value.__cause__, EgressRefused)
    assert len(server.hits) == 1


def test_a_relative_redirect_stays_on_the_name(server):
    """httpx resolves a relative Location against the request it sent. MUTATION: rewrite the
    caller's request to the pinned address, and the second hop goes to the address - the
    server then sees the address as its Host."""
    r = _get(f"http://public.test:{server.port}/redir//ok")
    assert r.text == f"host=public.test:{server.port}"
    assert str(r.request.url).startswith("http://public.test:")


def test_a_trusted_host_is_admitted(server):
    """A registered MCP server on this machine: past the judgement (the connection then fails
    only because nothing listens at the fake address). A refusal raises the same error type,
    so the cause tells them apart. MUTATION: ignore trusted_host."""
    with pytest.raises(httpx.ConnectError) as failed:
        _get(f"http://loop.test:{server.port}/x", EgressPolicy(trusted_host="loop.test"))
    assert not isinstance(failed.value.__cause__, EgressRefused)


def test_the_certificate_is_checked_against_the_name(tmp_path, monkeypatch):
    """MUTATION: drop sni_hostname, and the certificate is checked against the address."""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from vaf.network import ssl_utils
    monkeypatch.setattr(ssl_utils, "_collect_sans", lambda: [x509.DNSName("public.test")])
    ca_key, ca_cert = ssl_utils._generate_ca(tmp_path)
    cert_path, key_path = ssl_utils._generate_server_cert(tmp_path, ca_key, ca_cert)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.handle_error = lambda *a: None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    verify = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    import vaf.network.egress as egress
    real = egress.httpx.AsyncHTTPTransport
    monkeypatch.setattr(egress.httpx, "AsyncHTTPTransport", lambda **kw: real(verify=verify, **kw))
    try:
        assert _get(f"https://public.test:{port}/").text == "ok"
        with pytest.raises(httpx.ConnectError):
            _get(f"https://other.test:{port}/")
    finally:
        httpd.shutdown()
        httpd.server_close()
