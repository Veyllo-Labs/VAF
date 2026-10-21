# VAF Network

The `vaf.network` package answers three questions for everything that serves VAF to other
devices or fetches something on its behalf: who is this client, may it connect, and where
may VAF itself connect to. Each question has one answer in one function; the web server,
the firewall, the CLI and the settings ask that function instead of deciding for
themselves.

## Who the client is

`binding.connection_client_ip(conn)` takes a Starlette `Request` or `WebSocket` and returns
the real client. The integrated HTTPS proxy relays every device to the backend over
loopback, so the socket peer is `127.0.0.1` for all of them; the function honours
`X-Forwarded-For` only when the peer is loopback, and reads the rightmost hop that is not
loopback (a relay appends what it saw; a client can only write to the left of it), so a
client can make itself look more remote, never more local. A hop that is not an IP literal
reads as `unknown`. A raw TCP forward on this machine writes no header and its clients are
this machine (named boundary, NETWORK_FEATURES.md). It is the only reader of the socket peer in `vaf/`
(`tests/test_client_address_has_one_reader.py`). A pure ASGI middleware wraps its scope:
`connection_client_ip(HTTPConnection(scope))`.

## Who may connect

`binding.inbound_policy(cfg=None)` returns an `InboundPolicy`: this machine always, then
the local networks (RFC 1918) or, with `local_network_vpn_only`, the networks of the
detected VPN interfaces, plus the private networks an admin listed in
`local_network_allowed_networks`. Readers:

| Function | Used by |
|---|---|
| `is_allowed_ip(ip)` | `AdmissionGuard` (`admission.py`), which both doors wrap: the integrated HTTPS proxy and the backend, for every HTTP request and every WebSocket handshake; the remote-access settings' display |
| `firewall_sources(narrow_lan=False)` | every OS firewall backend in `firewall.py` |
| `access_addresses()` | the access URLs in the settings, `vaf top` and `vaf server status` |
| `normalize_allowed_networks(values)` | the settings, the CLI and `vaf doctor`: what an entry means, or why it is refused |

`local_interfaces()` lists this machine's LAN and VPN addresses (psutil; container bridges,
loopback and public addresses left out) as `LocalInterface(name, ip, network, kind)`.
`get_local_network_ip()` is the LAN address only, for what is about the local network (the
DHCP warning, room invitations); `get_all_local_ips()` is every LAN and VPN address, which
the certificate carries.

Public networks are never admitted, by any setting: VAF is served to this machine, the local
networks and VPNs only. `public_addresses()` lists this machine's own public IPv4 addresses
(any interface that is up, no names skipped; IPv6 left out, since the access port listens on
IPv4 only); `vaf doctor`, `vaf server provision` and `status`, and the remote-access settings
read it to say when the machine sits on the internet. The design and the named boundaries are
in [NETWORK_FEATURES.md](../../docs/setup/NETWORK_FEATURES.md#scope-private-networks-only).

## Where VAF may connect

The outbound side: `binding.classify_address(ip)` sorts a destination into public, private,
loopback or forbidden, and `egress.py` builds the session every fetch whose URL someone else
chose goes through (`vaf.egress_session()`, see [EMBEDDING.md](../../docs/EMBEDDING.md)).
It never dials loopback, where a tokenless request is the owner.

## Modules

- **admission.py**: `AdmissionGuard(app)`, the admission check as a pure ASGI door: it
  refuses a client `is_allowed_ip` does not admit, HTTP with 403 (a browser loading a page
  gets a short page saying what to do, everything else JSON) and a WebSocket with 4003
  before accept, and records `ip_blocked` with the path in `detail`. `ByLocalNetwork` picks
  a door per request by the Local Network setting and `this_machine_only` is the door while
  it is off; both of VAF's doors are built on them. Not imported by the package: it needs
  starlette, which a slim install lacks.
- **binding.py**: interfaces, the inbound policy, the real client, the foreign-origin
  decision, port resolution, the outbound address classifier.
- **firewall.py**: the OS firewall for the admitted networks (firewalld, ufw, iptables,
  netsh, pf); `apply_lan_firewall()` is what the start and an admission change run;
  `elevation_argv()` is the one privilege lane of the framework, and
  `elevation_unavailable()` says why that lane cannot work in this process (the
  no-new-privileges flag, or a user namespace without root, as in the server's
  systemd service), so no caller runs sudo there.
- **https_proxy.py**: the integrated TLS entry point (`/api`, `/ws` to the backend,
  everything else to the frontend), wrapped in `AdmissionGuard` so a client that is not
  admitted (with Local Network off: any client but this machine) gets no page of VAF's;
  it writes `X-Forwarded-For` from `connection_client_ip`
  (the device itself, or the client a relay on this machine named), on every HTTP request
  and every WebSocket handshake, and adds the page security headers (`PAGE_SECURITY_HEADERS`)
  to what the frontend serves, and HSTS scoped by the dialled host (`binding.hsts_for_host`:
  never a year for `localhost` or this machine's own names).
- **ssl_utils.py**: the local CA and the server certificate, re-issued when an address is
  missing from it.
- **egress.py**: outbound fetches that never reach this machine.
- **oauth_redirect.py**, **runtime_status.py**, **connection_tracker.py**: OAuth callback
  addresses, the port the proxy really bound, the live connection map.
