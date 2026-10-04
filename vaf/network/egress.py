# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""One destination guard for every outbound fetch whose URL VAF does not choose itself.

WHY THIS EXISTS. The agent's web tools fetched any URL a model, a web page, a search
result or a mail handed them, from the VAF process itself. On the machine VAF runs on, a
request from 127.0.0.1 without a token is the owner (vaf/auth/middleware.py, the
"Localhost Bypass" in docs/setup/NETWORK_FEATURES.md), so a page that said "fetch
http://127.0.0.1:8005/api/contacts" got the owner's contacts back - measured before this
module existed, together with the account list and the configuration. The same request
reaches the cloud metadata service at 169.254.169.254 and every device on the LAN.

WHAT IT DOES. `egress_session()` is a requests.Session whose adapter judges every
connection before it is made, including every redirect hop:

- only http and https, optionally only some ports;
- the host is resolved ONCE and every address is classified
  (vaf.network.binding.classify_address); the connection goes to that checked address,
  while TLS still verifies the certificate against the hostname, so a name that resolves
  differently a moment later (DNS rebinding) cannot redirect it;
- loopback and forbidden addresses (link-local and the metadata service, multicast,
  reserved, unspecified) are refused, always. Private (LAN) addresses pass when the policy
  allows them, which `EgressPolicy.from_config()` reads from the admin-only key
  `egress_allow_private_hosts` (on by default: a home network is the normal place a
  personal agent works). A pass to a private address is written to the `egress` log, a
  refusal is a security event (`egress_blocked`);
- the site egress proxy comes from vaf.network.binding.system_proxy_for, never from the
  session's environment merge. Behind a proxy the address cannot be pinned (the proxy
  resolves the name); a name that resolves locally to a refused address is still refused,
  and a name the local resolver does not know is the proxy's to judge. NAMED BOUNDARY.

`EgressPolicy(trusted_host=...)` lets ONE host an administrator registered (an MCP
server on this machine) be loopback or private; a redirect to any other host is judged
by the normal rules.

THE MAIL IMAGE PROXY keeps its own pinned fetch (vaf/api/mail_routes.py image_proxy): it
already resolves once, pins, never follows a redirect and never reaches the LAN, and it
shares this module's classifier. Folding it in would only log every refused image twice
(egress_blocked and its own mail_image_proxy_blocked). NAMED BOUNDARY.

NOT IN SCOPE, NAMED BOUNDARIES: fixed internal loopback calls (sub-agent and workflow IPC
to VAF's own backend) are VAF choosing its own destination, not a fetch; A2A rooms dial
over their own wss client with a pinned CA (vaf/core/a2a/client.py); a shell command the
agent runs with host_bash (curl) is the person's grant and is not an HTTP client this
module can wrap.
"""
from __future__ import annotations

import asyncio
import socket
import threading
from dataclasses import dataclass, replace
from typing import Iterable, Optional, Tuple
from urllib.parse import urlsplit

import httpx
import requests
from requests.adapters import HTTPAdapter

from vaf.network import binding

_SCHEMES = ("http", "https")


class EgressRefused(requests.exceptions.ConnectionError, ValueError):
    """A destination the policy does not allow. A ConnectionError, so callers that already
    handle "could not connect" handle this too; a ValueError, so a route can answer 400."""

    def __init__(self, host: str, address: str = "", kind: str = "", reason: str = ""):
        self.host = host
        self.address = address
        self.kind = kind
        if reason:
            message = f"Refused to fetch {host}: {reason}."
        elif kind == "private":
            message = (f"Refused to connect to {host}: it resolves to a private network "
                       f"address ({address}), and fetching from the local network is "
                       "switched off (egress_allow_private_hosts).")
        else:
            message = (f"Refused to connect to {host}: it resolves to a {kind or 'non-public'} "
                       f"address ({address}). Only internet addresses are fetched.")
        super().__init__(message)


@dataclass(frozen=True)
class EgressPolicy:
    """What a fetch may reach. Immutable; derive variants with `replace`."""

    allow_private: bool = False
    trusted_host: Optional[str] = None
    ports: Optional[Tuple[int, ...]] = None
    max_redirects: int = 5

    @classmethod
    def from_config(cls, **overrides) -> "EgressPolicy":
        """The instance policy: LAN access from `egress_allow_private_hosts` (default on)."""
        allow_private = True
        try:
            from vaf.core.config import Config
            allow_private = bool(Config.get("egress_allow_private_hosts", True))
        except Exception:
            pass
        return replace(cls(allow_private=allow_private), **overrides)


def _host_label(host: str, port: int, scheme: str) -> str:
    default = 443 if scheme == "https" else 80
    shown = f"[{host}]" if ":" in host else host
    return shown if port == default else f"{shown}:{port}"


def _split(url: str, policy: EgressPolicy) -> Tuple[str, str, int]:
    parts = urlsplit(str(url or ""))
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").strip().rstrip(".")
    if scheme not in _SCHEMES:
        raise EgressRefused(host or str(url)[:60], reason=f"only http and https are fetched, not {scheme or 'a bare path'!r}")
    if not host:
        raise EgressRefused(str(url)[:60], reason="the URL names no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        raise EgressRefused(host, reason="the port is not a number") from None
    if policy.ports is not None and port not in policy.ports:
        raise EgressRefused(host, reason=f"port {port} is not one of {sorted(policy.ports)}")
    return scheme, host, port


def _admitted(kind: str, host: str, policy: EgressPolicy) -> bool:
    if kind == "public":
        return True
    if kind == "forbidden":
        return False
    if policy.trusted_host and host.lower() == policy.trusted_host.strip().lower():
        return True                 # an administrator registered exactly this host
    return kind == "private" and policy.allow_private


def _judge(host: str, addresses: Iterable[str], policy: EgressPolicy, username: str) -> str:
    """Raise for the first refused address; return the worst admitted kind."""
    worst = "public"
    for ip in dict.fromkeys(addresses):
        kind = binding.classify_address(ip)
        if not _admitted(kind, host, policy):
            _record_refusal(host, ip, kind, username)
            raise EgressRefused(host, ip, kind)
        if kind != "public":
            worst = kind
    return worst


def _resolve(host: str, port: int):
    return [info[4][0] for info in binding.socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


def check_destination(url: str, policy: Optional[EgressPolicy] = None, *,
                      username: str = "") -> str:
    """Judge `url` now and return the address a connection would be pinned to.

    For a value that is STORED and fetched later (a WebDAV URL, an MCP server): refusing
    at save time tells the person at once instead of failing at the first sync. The fetch
    itself is judged again, so a name that changed in between is still caught."""
    policy = policy or EgressPolicy.from_config()
    scheme, host, port = _split(url, policy)
    try:
        addresses = _resolve(host, port)
    except OSError as exc:
        raise EgressRefused(host, reason="the host name does not resolve") from exc
    if not addresses:
        raise EgressRefused(host, reason="the host name does not resolve")
    kind = _judge(host, addresses, policy, username)
    _record_private(host, addresses[0], kind, username)
    return addresses[0]


def _record_refusal(host: str, ip: str, kind: str, username: str) -> None:
    try:
        from vaf.core.security_events import log_security_event
        log_security_event("egress_blocked", username=username or "",
                           detail=f"{host} -> {ip} ({kind})")
    except Exception:
        pass


def _record_private(host: str, ip: str, kind: str, username: str) -> None:
    if kind == "public":
        return
    try:
        from vaf.core.log_helper import append_domain_log
        append_domain_log("egress", f"[egress] {username or '-'} fetched {host} -> {ip} ({kind})")
    except Exception:
        pass


class _EgressAdapter(HTTPAdapter):
    """Judges each connection before it is opened. requests calls send() once per hop, so
    a redirect is judged like the first request."""

    def __init__(self, policy: EgressPolicy, username: str = ""):
        self._policy = policy
        self._username = username
        self._local = threading.local()
        self._pinned_pools: dict = {}
        self._pools_lock = threading.Lock()
        super().__init__(max_retries=0)

    # -- one hop ------------------------------------------------------------------
    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        scheme, host, port = _split(request.url, self._policy)
        proxy = binding.system_proxy_for(scheme, host)
        self._local.pin = None
        if proxy:
            # The proxy resolves the name, so nothing here can be pinned. A name that
            # resolves LOCALLY to a refused address is still refused; one the local
            # resolver does not know (split horizon) is the proxy's to judge.
            try:
                addresses = _resolve(host, port)
            except OSError:
                addresses = []
            if addresses:
                kind = _judge(host, addresses, self._policy, self._username)
                _record_private(host, addresses[0], kind, self._username)
            proxies = {scheme: proxy}
        else:
            try:
                addresses = _resolve(host, port)
            except OSError as exc:
                raise requests.exceptions.ConnectionError(f"Cannot resolve host: {host}") from exc
            if not addresses:
                raise requests.exceptions.ConnectionError(f"Cannot resolve host: {host}")
            kind = _judge(host, addresses, self._policy, self._username)
            _record_private(host, addresses[0], kind, self._username)
            self._local.pin = (scheme, host, port, addresses[0])
            proxies = {}
        # The Host header carries the NAME; the connection goes to the checked address.
        request.headers["Host"] = _host_label(host, port, scheme)
        return super().send(request, stream=stream, timeout=timeout, verify=verify,
                            cert=cert, proxies=proxies)

    # -- the connection: requests >= 2.32 and the 2.31 floor use different hooks -----
    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        pinned = self._pinned_pool()
        if pinned is not None:
            return pinned
        return super().get_connection_with_tls_context(request, verify, proxies=proxies, cert=cert)

    def get_connection(self, url, proxies=None):          # requests < 2.32
        pinned = self._pinned_pool()
        if pinned is not None:
            return pinned
        return super().get_connection(url, proxies)

    def _pinned_pool(self):
        pin = getattr(self._local, "pin", None)
        if pin is None:
            return None
        scheme, host, port, ip = pin
        key = (scheme, host.lower(), port, ip)
        with self._pools_lock:
            pool = self._pinned_pools.get(key)
            if pool is None:
                import urllib3
                if scheme == "https":
                    # Connect to the checked address, verify the certificate against the
                    # NAME (SNI and hostname check). cert_verify() sets cert_reqs/ca_certs
                    # on the pool afterwards, as for any requests pool.
                    pool = urllib3.HTTPSConnectionPool(
                        ip, port=port, maxsize=4, block=False,
                        server_hostname=host, assert_hostname=host)
                else:
                    pool = urllib3.HTTPConnectionPool(ip, port=port, maxsize=4, block=False)
                self._pinned_pools[key] = pool
            return pool

    def close(self):
        with self._pools_lock:
            pools, self._pinned_pools = list(self._pinned_pools.values()), {}
        for pool in pools:
            try:
                pool.close()
            except Exception:
                pass
        super().close()


class _EgressAsyncTransport(httpx.AsyncBaseTransport):
    """The same judgement for httpx, which the MCP SDK speaks. httpx calls this once per hop,
    redirects included. The request goes to the checked address with `sni_hostname` set, so
    httpcore verifies the certificate against the NAME; one inner transport per hostname, so
    a connection opened for one name is never reused for another that shares its address."""

    def __init__(self, policy: EgressPolicy, username: str = ""):
        self._policy = policy
        self._username = username
        self._inner: dict = {}

    def _transport(self, key: str, **kwargs) -> httpx.AsyncHTTPTransport:
        transport = self._inner.get(key)
        if transport is None:
            transport = self._inner[key] = httpx.AsyncHTTPTransport(**kwargs)
        return transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        scheme, host, port = _split(str(request.url), self._policy)
        proxy = binding.system_proxy_for(scheme, host)
        try:
            addresses = await asyncio.get_running_loop().run_in_executor(None, _resolve, host, port)
        except OSError:
            addresses = []
        if proxy:
            # Same rule as the requests adapter: a local answer is still judged, an unknown
            # name is the proxy's.
            if addresses:
                kind = _judge(host, addresses, self._policy, self._username)
                _record_private(host, addresses[0], kind, self._username)
            return await self._transport("proxy " + proxy, proxy=proxy).handle_async_request(request)
        if not addresses:
            raise httpx.ConnectError(f"Cannot resolve host: {host}", request=request)
        kind = _judge(host, addresses, self._policy, self._username)
        _record_private(host, addresses[0], kind, self._username)
        # The Host header was built from the URL already; only the connection target moves.
        request.url = request.url.copy_with(host=addresses[0])
        request.extensions = {**request.extensions, "sni_hostname": host}
        return await self._transport(host.lower()).handle_async_request(request)

    async def aclose(self) -> None:
        transports, self._inner = list(self._inner.values()), {}
        for transport in transports:
            try:
                await transport.aclose()
            except Exception:
                pass


def egress_httpx_client(headers=None, timeout=None, auth=None, *,
                        policy: Optional[EgressPolicy] = None,
                        username: str = "") -> httpx.AsyncClient:
    """An httpx.AsyncClient behind the destination guard, with the MCP SDK's client factory
    signature (headers, timeout, auth) and its defaults: redirects followed (each one judged),
    30 s with a 300 s read timeout for event streams. A transport of its own means httpx reads
    no proxy from the environment; the site proxy comes from system_proxy_for."""
    policy = policy or EgressPolicy.from_config()
    kwargs = {
        "follow_redirects": True,
        "max_redirects": policy.max_redirects,
        "timeout": timeout if timeout is not None else httpx.Timeout(30.0, read=300.0),
        "transport": _EgressAsyncTransport(policy, username),
    }
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


def egress_session(policy: Optional[EgressPolicy] = None, *, username: str = "") -> requests.Session:
    """A requests.Session that only reaches what `policy` allows (default: the instance
    policy). Use it like any session; a refused destination raises EgressRefused, which is
    a requests ConnectionError. `username` labels the log lines and security events."""
    policy = policy or EgressPolicy.from_config()
    session = requests.Session()
    adapter = _EgressAdapter(policy, username=username)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    # Every other scheme too, so file://, ftp:// and the like get the guard's answer
    # rather than requests' "no connection adapters".
    session.mount("", adapter)
    session.max_redirects = policy.max_redirects
    return session
