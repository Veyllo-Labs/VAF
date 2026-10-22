# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""Pages relayed by the integrated HTTPS proxy carry anti-framing and transport headers.

Measured before: `/login` through the proxy carried none of X-Frame-Options, a CSP
frame-ancestors, HSTS, nosniff or Referrer-Policy, because the backend's header table covers
API responses only and the proxy copied the frontend's headers as they were. So the login
and the app could be framed by any site.

The set at the proxy is deliberately not the backend's: no Permissions-Policy (it would
switch off the UI's own voice input), and frame-ancestors on pages only (the interactive
browser's viewer is a BACKEND response framed by the UI under SAMEORIGIN).
"""
import httpx
import pytest
from starlette.testclient import TestClient

import vaf.network.https_proxy as proxy

# Every page here is relayed to a device on the LAN, which the door serves in network mode only.
pytestmark = pytest.mark.usefixtures("local_network_on")

@pytest.fixture(autouse=True)
def _machine_named_workstation(monkeypatch):
    """A fixed host name and FQDN, so the real machine's names never decide a test (and no
    test waits on a DNS lookup); the memo of the machine's names starts empty."""
    import socket

    import vaf.network.binding as binding
    monkeypatch.setattr(socket, "gethostname", lambda: "Workstation")
    monkeypatch.setattr(socket, "getfqdn", lambda *_a: "Workstation.example.lan")
    monkeypatch.setattr(binding, "_OWN_LABELS", {})


def _upstream(headers):
    """The proxy's pooled client, a real httpx client whose upstream answers with the headers
    each test hands it. The body is a stream, as a real upstream's is: the proxy relays it
    without reading it first."""
    def _answer(_request):
        return httpx.Response(200, headers=list(headers.items()),
                              stream=httpx.ByteStream(b"<html></html>"))
    return httpx.AsyncClient(transport=httpx.MockTransport(_answer))


def _get(monkeypatch, path, upstream_headers, host="vaf.lan:8443"):
    upstream = _upstream(upstream_headers)

    async def _client(_origin):
        return upstream

    monkeypatch.setattr(proxy, "_get_client", _client)
    c = TestClient(proxy.create_proxy_app(), client=("192.168.1.20", 40000))
    return c.get(path, headers={"Host": host})


def test_pages_through_the_proxy_carry_the_headers(monkeypatch):
    """MUTATION: drop the page header step - red."""
    r = _get(monkeypatch, "/login", {"content-type": "text/html"})
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["strict-transport-security"] == "max-age=31536000"
    # The UI's own voice input runs in these pages.
    assert "permissions-policy" not in r.headers


def test_a_page_keeps_what_the_frontend_set(monkeypatch):
    """Add if absent, never over the frontend's own value. MUTATION: overwrite - red."""
    r = _get(monkeypatch, "/", {"content-type": "text/html", "X-Frame-Options": "SAMEORIGIN"})
    assert r.headers["x-frame-options"] == "SAMEORIGIN"


@pytest.mark.parametrize("path", ["/api/browser-vnc/t/abcdefghijklmnop/vnc.html", "/api/users"])
def test_backend_responses_get_hsts_only(monkeypatch, path):
    """The viewer is framed by the UI: a CSP frame-ancestors would override its SAMEORIGIN.
    MUTATION: apply the page set to backend responses - red."""
    r = _get(monkeypatch, path, {"content-type": "text/html", "X-Frame-Options": "SAMEORIGIN"})
    assert "content-security-policy" not in r.headers
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert r.headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.parametrize("host", ["192.168.1.5:8443", "[::1]:8443", "127.0.0.1:8443"])
def test_no_hsts_for_an_address(monkeypatch, host):
    """Browsers ignore HSTS for an address (RFC 6797 8.1.1), and an address is how other
    devices usually dial VAF. MUTATION: send it for every host - red."""
    r = _get(monkeypatch, "/login", {"content-type": "text/html"}, host=host)
    assert "strict-transport-security" not in r.headers
    assert r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("host", ["localhost:8443", "app.localhost:8443", "localhost.:8443"])
def test_localhost_gets_no_year_and_drops_an_old_pin(monkeypatch, host):
    """HSTS holds a host name on every port for a year: on localhost it would upgrade every
    plain-HTTP app there, the Web UI's own port included. max-age=0 drops a pin an earlier
    version left. MUTATION: send the year for every host, or with includeSubDomains - red."""
    r = _get(monkeypatch, "/login", {"content-type": "text/html"}, host=host)
    assert r.headers["strict-transport-security"] == "max-age=0"
    assert r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("host", ["workstation:8443", "workstation.fritz.box:8443",
                                  "WORKSTATION.local:8443", "workstation.tailnet.ts.net:8443",
                                  "workstation.:8443"])
def test_no_hsts_for_this_machines_own_names(monkeypatch, host):
    """The machine's host name and every name built on it (FQDN, mDNS, a router's or a VPN's)
    reach every other service on the machine too: a year of HSTS there makes each plain-HTTP
    one unreachable from that browser. Earlier versions sent that year with
    includeSubDomains; max-age=0 drops it at the next visit. MUTATION: drop the own-name
    check, or answer None there (the old pin would stay) - red."""
    r = _get(monkeypatch, "/login", {"content-type": "text/html"}, host=host)
    assert r.headers["strict-transport-security"] == "max-age=0"
    assert r.headers["x-frame-options"] == "DENY"


def test_the_backend_scopes_its_hsts_the_same_way():
    """The backend's own table (web_server._SecurityHeadersMiddleware) asks the same function.
    A trailing dot is the same name. MUTATION: keep the trailing dot - red."""
    from vaf.network.binding import hsts_for_host
    assert hsts_for_host("vaf.lan:8443") == "max-age=31536000"
    assert hsts_for_host("vaf.lan.") == "max-age=31536000"
    assert hsts_for_host("workstation-two.lan") == "max-age=31536000"   # a label, not a prefix
    for host in ("localhost", "localhost.", "localhost.:3000", "x.localhost.", "x.localhost",
                 "workstation", "workstation.example.lan."):
        assert hsts_for_host(host) == "max-age=0", host
    for host in ("192.168.1.5", "192.168.1.5.", "[::1]:8443", "", None, "."):
        assert hsts_for_host(host) is None, host
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "vaf" / "core" / "web_server.py").read_text(
        encoding="utf-8")
    assert "max-age=31536000; includeSubDomains" not in src
    assert "hsts_for_host(request.headers.get(\"x-forwarded-host\")" in src


def test_the_access_log_keeps_one_request_on_one_line(monkeypatch):
    """The path arrives percent-decoded: GET /page%0a<text> wrote a forged second line into
    https_proxy_access_<date>.log. MUTATION: write the line unsanitized - red."""
    # (the router answers 404 to a path with a newline; the access log is written before it)
    _get(monkeypatch, "/page%0a2026-10-21T00:00:00.000  HTTP  10.0.0.9  GET /forged", {})
    lines = proxy._access_log_path().read_text(encoding="utf-8").splitlines()
    assert not any(ln.startswith("2026-10-21T00:00:00.000") for ln in lines)
    assert any("GET /page 2026-10-21T00:00:00.000" in ln for ln in lines)


def test_the_certificate_carries_the_names_hsts_keeps_off(monkeypatch):
    """One list for both: the local certificate's DNS names are localhost plus
    machine_dns_names(), and HSTS is sent for none of them. MUTATION: leave the machine's
    names out of the certificate - red."""
    import socket

    from cryptography import x509

    import vaf.network.binding as binding
    from vaf.network import ssl_utils
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [])
    names = [v.value for v in ssl_utils._collect_sans() if isinstance(v, x509.DNSName)]
    assert names == ["localhost", "Workstation", "Workstation.example.lan", "workstation.local"]
    for name in names:
        assert binding.hsts_for_host(f"{name}:8443") == binding.HSTS_CLEAR, name



def test_an_fqdn_with_another_first_label_is_the_machines_own_too(monkeypatch):
    """getfqdn() can name the machine differently from gethostname() (a host alias), and the
    certificate carries both: the FQDN and the names built on its label get max-age=0 as
    well, and the year still goes to a name of its own. MUTATION: judge by gethostname()
    alone - red."""
    import socket

    import vaf.network.binding as binding
    monkeypatch.setattr(socket, "getfqdn", lambda *_a: "nas.example.lan")
    assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_CLEAR
    assert binding.hsts_for_host("nas.local") == binding.HSTS_CLEAR
    assert binding.hsts_for_host("workstation.lan") == binding.HSTS_CLEAR
    assert binding.hsts_for_host("vaf.lan") == binding.HSTS_VALUE


def test_the_machines_names_are_looked_up_once_not_on_every_answer(monkeypatch):
    """getfqdn() is a DNS lookup and HSTS is decided on every answer: the answers read a memo
    per host name, and the certificate check fills it at the start. MUTATION: drop the memo,
    or the warm-up in ensure_ssl_certificates - red."""
    import socket

    import vaf.network.binding as binding
    lookups = []
    monkeypatch.setattr(socket, "getfqdn", lambda *_a: lookups.append(1) or "nas.example.lan")
    for _ in range(50):
        binding.hsts_for_host("vaf.lan:8443")
    assert len(lookups) == 1

    # The start fills it before any answer: the certificate check with TLS on asks first.
    from vaf.core.config import Config
    from vaf.network import ssl_utils
    monkeypatch.setattr(binding, "_OWN_LABELS", {})
    monkeypatch.setattr(Config, "get", staticmethod(
        lambda key, default=None: True if key == "local_network_tls_enabled" else default))

    class _Stop(Exception):
        pass

    def _stop():
        raise _Stop()

    monkeypatch.setattr(ssl_utils, "_ssl_dir_path", _stop)     # no certificate work in a test
    with pytest.raises(_Stop):
        ssl_utils.ensure_ssl_certificates()
    assert len(lookups) == 2 and binding._OWN_LABELS


def test_a_lookup_from_before_dns_was_up_is_renewed_beside_the_answers(monkeypatch):
    """With DNS not up at the start, getfqdn() answers with the bare host name, and the FQDN
    (which a certificate from an earlier start can carry) got the year until a restart. A
    result older than OWN_NAMES_MAX_AGE_S is now looked up again in a thread of its own: the
    answer that finds it goes out at once with the old names, the answers during the lookup
    start no second one, and the answers after it use the new names. MUTATION: never renew,
    renew in front of the answer, or start a lookup per answer while one runs - red."""
    import socket
    import threading
    import time

    import vaf.network.binding as binding
    dns_up = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    lookups = []

    def _getfqdn(*_a):
        lookups.append(1)
        if not dns_up.is_set():
            return "Workstation"            # what getfqdn() answers when the lookup fails
        entered.set()
        release.wait(5)
        return "nas.example.lan"

    monkeypatch.setattr(socket, "getfqdn", _getfqdn)
    assert binding.own_name_labels() == frozenset({"workstation"})      # the start
    dns_up.set()
    for _ in range(20):
        assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_VALUE
    assert len(lookups) == 1                 # a fresh result: no lookup on an answer

    labels, looked_up_at, order = binding._OWN_LABELS["Workstation"]
    binding._OWN_LABELS["Workstation"] = (labels, looked_up_at - binding.OWN_NAMES_MAX_AGE_S,
                                          order)
    started = time.monotonic()
    assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_VALUE
    assert time.monotonic() - started < 2.5  # the answer did not wait for the lookup
    assert entered.wait(5)
    for _ in range(20):
        binding.hsts_for_host("nas.example.lan:8443")
    assert len(lookups) == 2                 # one renewal at a time

    release.set()
    binding._OWN_LABELS_REFRESHER.join(5)
    assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_CLEAR
    assert binding.hsts_for_host("vaf.lan") == binding.HSTS_VALUE
    assert len(lookups) == 2


def test_a_certificate_issued_now_and_hsts_hold_the_same_lookup(monkeypatch):
    """The start's lookup can come before DNS is up and the certificate's a moment later,
    after it: the certificate then names the FQDN while HSTS still held the bare host name.
    Every lookup of the machine's names renews the memo, so they agree. MUTATION: keep the
    memo's first entry instead of renewing it - red."""
    import socket

    from cryptography import x509

    import vaf.network.binding as binding
    from vaf.network import ssl_utils
    answer = {"fqdn": "Workstation"}
    monkeypatch.setattr(socket, "getfqdn", lambda *_a: answer["fqdn"])
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [])
    assert binding.own_name_labels() == frozenset({"workstation"})      # the start's lookup
    answer["fqdn"] = "nas.example.lan"
    names = [v.value for v in ssl_utils._collect_sans() if isinstance(v, x509.DNSName)]
    assert "nas.example.lan" in names
    assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_CLEAR


def test_a_renewal_landing_after_the_certificates_lookup_puts_no_older_names_back(monkeypatch):
    """The start's own warm-up can find the memo old and start a renewal, and the certificate's
    lookup runs a moment later. When the renewal began before the machine's FQDN changed and
    lands after the certificate's lookup, it put the old FQDN back, and the new one the
    certificate carries got the year. The memo keeps the lookup that started last, not the
    one that finished last. MUTATION: let every lookup replace the entry, or order by the
    finish - red."""
    import socket
    import threading

    from cryptography import x509

    import vaf.network.binding as binding
    from vaf.network import ssl_utils
    entered = threading.Event()
    release = threading.Event()
    answer = {"fqdn": "old.example.lan"}
    lookups = []

    def _getfqdn(*_a):
        lookups.append(threading.current_thread().name)
        if threading.current_thread().name == "vaf-own-names":
            entered.set()
            release.wait(5)
            return "old.example.lan"        # its query began before the FQDN changed
        return answer["fqdn"]

    monkeypatch.setattr(socket, "getfqdn", _getfqdn)
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [])
    assert binding.own_name_labels() == frozenset({"workstation", "old"})   # the start
    labels, looked_up_at, order = binding._OWN_LABELS["Workstation"]
    binding._OWN_LABELS["Workstation"] = (labels, looked_up_at - binding.OWN_NAMES_MAX_AGE_S,
                                          order)

    try:
        assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_VALUE  # starts it
        assert entered.wait(5)
        answer["fqdn"] = "nas.example.lan"  # the FQDN the certificate's lookup sees
        names = [v.value for v in ssl_utils._collect_sans() if isinstance(v, x509.DNSName)]
        assert "nas.example.lan" in names
        assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_CLEAR
    finally:
        release.set()                       # the older lookup lands last
        binding._OWN_LABELS_REFRESHER.join(5)
    assert not binding._OWN_LABELS_REFRESHER.is_alive()
    assert lookups.count("vaf-own-names") == 1 and len(lookups) == 3
    assert binding._OWN_LABELS["Workstation"][0] == frozenset({"workstation", "nas"})
    assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_CLEAR


def test_a_renewal_during_a_dns_outage_keeps_the_fqdn(monkeypatch):
    """While DNS is down, getfqdn() answers with the bare host name. A renewal in that moment
    replaced the names with the host name alone, and the FQDN, which the certificate carries,
    got the year until the next renewal (measured: max-age=0 before the outage, the year
    after it). A lookup that finds no FQDN keeps the names the memo holds and renews only
    their time. MUTATION: let it replace the names, or leave the entry's time old - red."""
    import socket

    from cryptography import x509

    import vaf.network.binding as binding
    from vaf.network import ssl_utils
    dns = {"up": True}
    lookups = []

    def _getfqdn(*_a):
        lookups.append(1)
        return "nas.example.lan" if dns["up"] else "Workstation"

    monkeypatch.setattr(socket, "getfqdn", _getfqdn)
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [])
    assert binding.own_name_labels() == frozenset({"workstation", "nas"})   # the start
    labels, looked_up_at, order = binding._OWN_LABELS["Workstation"]
    binding._OWN_LABELS["Workstation"] = (labels, looked_up_at - binding.OWN_NAMES_MAX_AGE_S,
                                          order)

    dns["up"] = False
    assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_CLEAR  # starts it
    binding._OWN_LABELS_REFRESHER.join(5)
    assert len(lookups) == 2
    assert binding._OWN_LABELS["Workstation"][0] == frozenset({"workstation", "nas"})
    for _ in range(20):
        assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_CLEAR
    binding._OWN_LABELS_REFRESHER.join(5)
    assert len(lookups) == 2                 # the time was renewed: no lookup per answer

    # A certificate issued during the outage names no FQDN; HSTS still keeps the FQDN off.
    names = [v.value for v in ssl_utils._collect_sans() if isinstance(v, x509.DNSName)]
    assert names == ["localhost", "Workstation", "workstation.local"]
    assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_CLEAR


def test_a_renewal_without_an_fqdn_does_not_outrank_the_certificates_lookup(monkeypatch):
    """The start ran while DNS was down, so the memo holds the host name alone. A certificate's
    lookup starts; while its query runs, the entry crosses the minute and an answer starts a
    renewal, whose query fails at once (the bare host name) and lands first. The certificate's
    query then finds the FQDN. The renewal had taken the newer place in the start order with
    no new names, so the certificate's lookup was thrown away and the FQDN it carries got the
    year. A lookup that finds no FQDN keeps the place of the names it keeps. MUTATION: give
    it its own place in the start order - red."""
    import socket
    import threading

    from cryptography import x509

    import vaf.network.binding as binding
    from vaf.network import ssl_utils
    certificate = {"issuing": False}
    lookups = []

    def _getfqdn(*_a):
        renewal = threading.current_thread().name == "vaf-own-names"
        lookups.append("renewal" if renewal else "direct")
        if renewal:
            return "Workstation"            # the renewal's query fails: the bare host name
        if certificate["issuing"]:
            # The certificate's query is in flight: the entry crosses the minute, an answer
            # starts a renewal, and that renewal lands before this query returns.
            labels, looked_up_at, order = binding._OWN_LABELS["Workstation"]
            binding._OWN_LABELS["Workstation"] = (
                labels, looked_up_at - binding.OWN_NAMES_MAX_AGE_S, order)
            assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_VALUE
            binding._OWN_LABELS_REFRESHER.join(5)
            assert not binding._OWN_LABELS_REFRESHER.is_alive()
            return "nas.example.lan"
        return "Workstation"                # the start: DNS not up yet

    monkeypatch.setattr(socket, "getfqdn", _getfqdn)
    monkeypatch.setattr(binding, "get_all_local_ips", lambda: [])
    assert binding.own_name_labels() == frozenset({"workstation"})          # the start
    certificate["issuing"] = True
    names = [v.value for v in ssl_utils._collect_sans() if isinstance(v, x509.DNSName)]
    assert names == ["localhost", "Workstation", "nas.example.lan", "workstation.local"]
    assert lookups == ["direct", "direct", "renewal"]
    assert binding._OWN_LABELS["Workstation"][0] == frozenset({"workstation", "nas"})
    assert binding.hsts_for_host("nas.example.lan:8443") == binding.HSTS_CLEAR
    assert binding.hsts_for_host("vaf.lan") == binding.HSTS_VALUE


def test_the_https_proxy_looks_the_names_up_before_it_serves(monkeypatch):
    """With TLS on and the LAN switched off the proxy still runs, and no certificate check at
    the start has looked the machine's names up: the proxy does it before its first answer.
    MUTATION: drop the lookup in run_https_proxy - red."""
    import socket

    import vaf.network.binding as binding
    from vaf.network import runtime_status
    lookups = []
    monkeypatch.setattr(socket, "getfqdn", lambda *_a: lookups.append(1) or "nas.example.lan")
    monkeypatch.setattr(binding, "pick_bindable_port", lambda _host, port, _fallback: port)
    monkeypatch.setattr(runtime_status, "set_proxy_failed", lambda *_a: None)
    seen = {}

    def _create_app():
        seen["labels"] = dict(binding._OWN_LABELS)
        raise RuntimeError("stop before serving")

    monkeypatch.setattr(proxy, "create_proxy_app", _create_app)
    proxy.run_https_proxy("127.0.0.1", 8443, "cert.pem", "key.pem")
    assert len(lookups) == 1
    assert seen["labels"]["Workstation"][0] == frozenset({"workstation", "nas"})
