# VAF Network

The `vaf.network` package answers three questions for everything that serves VAF to other
devices or fetches something on its behalf: who is this client, may it connect, and where
may VAF itself connect to. Each question has one answer in one function; the web server,
the firewall, the CLI and the settings ask that function instead of deciding for
themselves.

## Who the client is

`binding.connection_client_ip(conn)` takes a Starlette `Request` or `WebSocket` and returns
the real client. The integrated HTTPS proxy relays every device to the backend over
loopback, so the socket peer is `127.0.0.1` for all of them; the function honours the
proxy's `X-Forwarded-For` only when the peer is loopback, so a client can make itself
look more remote, never more local. It is the only reader of the socket peer in `vaf/`
(`tests/test_client_address_has_one_reader.py`). A pure ASGI middleware wraps its scope:
`connection_client_ip(HTTPConnection(scope))`.

## Who may connect

`binding.inbound_policy(cfg=None)` returns an `InboundPolicy`: this machine always, then
the local networks (RFC 1918) or, with `local_network_vpn_only`, the networks of the
detected VPN interfaces, plus the private networks an admin listed in
`local_network_allowed_networks`. Readers:

| Function | Used by |
|---|---|
| `is_allowed_ip(ip)` | the IP check middleware and the WebSocket handshake, on every request |
| `firewall_sources(narrow_lan=False)` | every OS firewall backend in `firewall.py` |
| `access_addresses()` | the access URLs in the settings, `vaf top` and `vaf server status` |
| `normalize_allowed_networks(values)` | the settings, the CLI and `vaf doctor`: what an entry means, or why it is refused |

`local_interfaces()` lists this machine's LAN and VPN addresses (psutil; container bridges,
loopback and public addresses left out) as `LocalInterface(name, ip, network, kind)`.
`get_local_network_ip()` is the LAN address only, for what is about the local network (the
DHCP warning, room invitations); `get_all_local_ips()` is every LAN and VPN address, which
the certificate carries.

Public networks are never admitted: that is the internet mode, which needs a real
certificate and a door that turns scanners away, not a list entry. The design and the named
boundaries are in [NETWORK_FEATURES.md](../../docs/setup/NETWORK_FEATURES.md#remote-access-over-a-vpn).

## Where VAF may connect

The outbound side: `binding.classify_address(ip)` sorts a destination into public, private,
loopback or forbidden, and `egress.py` builds the session every fetch whose URL someone else
chose goes through (`vaf.egress_session()`, see [EMBEDDING.md](../../docs/EMBEDDING.md)).
It never dials loopback, where a tokenless request is the owner.

## Modules

- **binding.py**: interfaces, the inbound policy, the real client, the foreign-origin
  decision, port resolution, the outbound address classifier.
- **firewall.py**: the OS firewall for the admitted networks (firewalld, ufw, iptables,
  netsh, pf); `apply_lan_firewall()` is what the start and an admission change run;
  `elevation_argv()` is the one privilege lane of the framework.
- **https_proxy.py**: the integrated TLS entry point (`/api`, `/ws` to the backend,
  everything else to the frontend); it writes `X-Forwarded-For` from its own peer.
- **ssl_utils.py**: the local CA and the server certificate, re-issued when an address is
  missing from it.
- **egress.py**: outbound fetches that never reach this machine.
- **oauth_redirect.py**, **runtime_status.py**, **connection_tracker.py**: OAuth callback
  addresses, the port the proxy really bound, the live connection map.
