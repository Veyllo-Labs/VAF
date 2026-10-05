# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""egress_session / check_destination: what a fetch whose URL someone else chose may reach.

Measured before the guard existed: a tokenless GET from 127.0.0.1 to VAF's backend
answered /api/contacts, /api/users and /api/config, because loopback is the owner. Every
test below names the mutation it catches.

Real sockets, real HTTP: a small server on 127.0.0.1 plays "the internet". The fake
resolver maps test names onto it, and a classifier seam calls that one address public -
everything else keeps the real classification, so the loopback refusals are tested
against the real rules."""
import http.server
import socket
import threading

import pytest
import requests

from vaf.network import binding, egress
from vaf.network.egress import EgressPolicy, EgressRefused, check_destination, egress_session

_PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY")
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CLASSIFY = binding.classify_address

# test name -> the addresses the fake resolver answers, in order of calls
NAMES = {
    "public.test": ["127.0.0.1"],
    "other.test": ["127.0.0.1"],
    "loop.test": ["127.0.0.9"],
    "lan.test": ["10.0.0.5"],
    "meta.test": ["169.254.169.254"],
    "mixed.test": ["127.0.0.1", "169.254.169.254"],
}


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    """Direct connect, the fake resolver, and 127.0.0.1 standing in for a public host."""
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    calls = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        if host in NAMES:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))
                    for ip in NAMES[host]]
        if host == "rebind.test":
            ip = "127.0.0.1" if calls.count("rebind.test") == 1 else "127.0.0.9"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]
        if host.endswith(".test"):
            raise socket.gaierror(socket.EAI_NONAME, "unknown test name")
        return _REAL_GETADDRINFO(host, port, *args, **kwargs)

    monkeypatch.setattr(binding.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(binding, "classify_address",
                        lambda ip: "public" if ip == "127.0.0.1" else _REAL_CLASSIFY(ip))
    events = []
    import vaf.core.security_events as se
    monkeypatch.setattr(se, "log_security_event", lambda kind, **kw: events.append((kind, kw)))
    lines = []
    import vaf.core.log_helper as lh
    monkeypatch.setattr(lh, "append_domain_log", lambda domain, line, *a, **k: lines.append((domain, line)))
    return {"calls": calls, "events": events, "lines": lines}


class _Server:
    """127.0.0.1 HTTP server: /ok answers the Host header, /redir/<url> redirects there."""

    def __init__(self):
        self.hits = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append((self.path, self.headers.get("Host")))
                if self.path.startswith("/redir/"):
                    self.send_response(302)
                    self.send_header("Location", self.path[len("/redir/"):])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = f"host={self.headers.get('Host')}".encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


def _public():
    return EgressPolicy(allow_private=False)


def test_a_public_host_is_fetched_and_the_host_header_carries_the_name(server):
    """MUTATION: drop the Host header line and the server sees the pinned address."""
    with egress_session(_public()) as s:
        r = s.get(f"http://public.test:{server.port}/ok", timeout=5)
    assert r.status_code == 200
    assert r.text == f"host=public.test:{server.port}"


def test_the_backend_on_loopback_is_refused_before_any_connection(server, _world):
    """The measured hole, with the REAL classification of 127.0.0.9. MUTATION: skip the
    judgement in send() and the request reaches the server."""
    with egress_session(EgressPolicy(allow_private=True)) as s, pytest.raises(EgressRefused) as e:
        s.get(f"http://loop.test:{server.port}/api/contacts", timeout=5)
    assert e.value.kind == "loopback"
    assert server.hits == []
    assert _world["events"] and _world["events"][0][0] == "egress_blocked"


@pytest.mark.parametrize("host,kind", [("meta.test", "forbidden"), ("mixed.test", "forbidden")])
def test_metadata_is_refused_even_with_the_lan_allowed(host, kind):
    """MUTATION: admit forbidden addresses under allow_private. mixed.test: ONE bad address
    among the answers is enough (MUTATION: judge only the first)."""
    with egress_session(EgressPolicy(allow_private=True)) as s, pytest.raises(EgressRefused) as e:
        s.get(f"http://{host}/", timeout=5)
    assert e.value.kind == kind


def test_the_lan_follows_the_policy_and_an_allowed_lan_fetch_is_logged(_world):
    """MUTATION: ignore allow_private, or drop the egress log line."""
    with pytest.raises(EgressRefused) as e:
        check_destination("http://lan.test/", _public())
    assert e.value.kind == "private"
    assert check_destination("http://lan.test/", EgressPolicy(allow_private=True)) == "10.0.0.5"
    assert any(d == "egress" and "lan.test" in line for d, line in _world["lines"])


def test_a_redirect_into_the_backend_is_refused(server):
    """A public page answering 302 -> loopback. MUTATION: judge only the first hop."""
    target = f"http://loop.test:{server.port}/api/users"
    with egress_session(_public()) as s, pytest.raises(EgressRefused):
        s.get(f"http://public.test:{server.port}/redir/{target}", timeout=5)
    assert [p for p, _ in server.hits] == [f"/redir/{target}"]


def test_a_redirect_to_another_public_host_works(server):
    with egress_session(_public()) as s:
        r = s.get(f"http://public.test:{server.port}/redir/http://other.test:{server.port}/ok", timeout=5)
    assert r.text == f"host=other.test:{server.port}"


def test_too_many_redirects_stop(server):
    loop = f"http://public.test:{server.port}/redir/" * 7 + "x"
    with egress_session(EgressPolicy(max_redirects=5)) as s, pytest.raises(requests.TooManyRedirects):
        s.get(loop, timeout=5)


def test_the_connection_goes_to_the_address_that_was_checked(server, _world):
    """DNS rebinding: the name answers a safe address to the check and loopback to anyone
    who asks again. MUTATION: resolve a second time to connect, and the second answer wins."""
    with egress_session(_public()) as s:
        r = s.get(f"http://rebind.test:{server.port}/ok", timeout=5)
    assert r.status_code == 200
    assert _world["calls"].count("rebind.test") == 1


def test_the_port_policy(server):
    """MUTATION: ignore EgressPolicy.ports."""
    with egress_session(EgressPolicy(ports=(80, 443))) as s, pytest.raises(EgressRefused):
        s.get(f"http://public.test:{server.port}/ok", timeout=5)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://public.test/x", "gopher://public.test/",
                                 "http:///nohost", "http://public.test:notaport/"])
def test_only_http_and_https_to_a_named_host(url):
    with pytest.raises(EgressRefused):
        check_destination(url, _public())


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://public.test/x"])
def test_the_session_answers_every_other_scheme_itself(url):
    """MUTATION: mount the adapter for http(s) only, and requests answers "no connection
    adapters" instead of the refusal a tool can pass on."""
    with egress_session(_public()) as s, pytest.raises(EgressRefused, match="only http and https"):
        s.get(url, timeout=5)


def test_a_trusted_host_may_be_local_but_never_forbidden():
    """An MCP server an administrator registered on this machine."""
    assert check_destination("http://loop.test/", EgressPolicy(trusted_host="loop.test")) == "127.0.0.9"
    with pytest.raises(EgressRefused):
        check_destination("http://meta.test/", EgressPolicy(trusted_host="meta.test"))
    with pytest.raises(EgressRefused):
        check_destination("http://loop.test/", EgressPolicy(trusted_host="other.test"))


def test_behind_a_site_proxy(monkeypatch, server):
    """The proxy resolves names, so nothing is pinned - but a name that resolves LOCALLY to
    a refused address is still refused, and the uppercase HTTP_PROXY (attacker-settable in
    CGI-style environments) is not used. MUTATION: skip the local check on the proxy branch."""
    monkeypatch.setenv("http_proxy", "http://unknown-proxy.test:3128")
    with egress_session(_public()) as s, pytest.raises(EgressRefused):
        s.get("http://loop.test/", timeout=5)
    with egress_session(_public()) as s:
        with pytest.raises(requests.exceptions.ConnectionError) as e:
            s.get("http://split-horizon.test/", timeout=5)
        assert not isinstance(e.value, EgressRefused), "an unknown name is the proxy's to judge"
    monkeypatch.delenv("http_proxy")
    monkeypatch.setenv("HTTP_PROXY", "http://unknown-proxy.test:3128")
    with egress_session(_public()) as s:
        assert s.get(f"http://public.test:{server.port}/ok", timeout=5).status_code == 200


def test_the_policy_reads_the_admin_key(monkeypatch):
    from vaf.core.config import Config
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: False if k == "egress_allow_private_hosts" else d))
    assert EgressPolicy.from_config().allow_private is False
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: d))
    assert EgressPolicy.from_config().allow_private is True, "the LAN is allowed by default"
    # Stored as text (a settings form, a hand-edited file): "false" means off. MUTATION:
    # bool(Config.get(...)), which reads every non-empty string as on.
    monkeypatch.setattr(Config, "get", classmethod(lambda cls, k, d=None: "false" if k == "egress_allow_private_hosts" else d))
    assert EgressPolicy.from_config().allow_private is False


def test_tls_is_verified_against_the_name_not_the_pinned_address(tmp_path, monkeypatch):
    """The connection goes to an address, the certificate is checked against the NAME.
    MUTATION: drop assert_hostname/server_hostname and a certificate for another name
    passes."""
    pytest.importorskip("cryptography")
    import ssl
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
    httpd.handle_error = lambda *a: None    # the refused handshake is the expected outcome
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        with egress_session(_public()) as s:
            assert s.get(f"https://public.test:{port}/", timeout=5, verify=str(tmp_path / "ca.pem")).text == "ok"
        with egress_session(_public()) as s, pytest.raises(requests.exceptions.SSLError):
            s.get(f"https://other.test:{port}/", timeout=5, verify=str(tmp_path / "ca.pem"))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_refusal_is_a_connection_error_and_a_value_error():
    """Callers that already handle "could not connect" handle a refusal; routes answer 400."""
    assert issubclass(EgressRefused, requests.exceptions.ConnectionError)
    assert issubclass(EgressRefused, ValueError)
    assert "Only internet addresses" in str(EgressRefused("h", "127.0.0.1", "loopback"))
    assert egress.EgressRefused is EgressRefused


def test_refusals_of_two_hosts_are_two_security_events(monkeypatch):
    """The security log throttles by kind, user and path: without the host as the path a
    second refused host within the window was never recorded. MUTATION: drop path=host."""
    from vaf.core import security_events
    seen = []
    monkeypatch.setattr(security_events, "log_security_event",
                        lambda kind, **kw: seen.append((kind, kw.get("path"))))
    egress._record_refusal("a.example", "127.0.0.1", "loopback", "alice")
    egress._record_refusal("b.example", "127.0.0.1", "loopback", "alice")
    assert seen == [("egress_blocked", "a.example"), ("egress_blocked", "b.example")]
