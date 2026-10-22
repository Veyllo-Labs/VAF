# VAF Network Features & Security

VAF (Veyllo Agent Framework) includes robust networking capabilities designed to allow secure, local collaboration. This document details the architecture, security measures, and usage of these features.

**Integrated HTTPS proxy (no Nginx required):** When **Local Network** and **SSL/TLS** are enabled and a certificate is available (your own, or VAF's own, which it issues itself), VAF starts an integrated reverse proxy on `0.0.0.0:local_network_https_port` (default 443). On **any platform** (Linux/macOS/Windows), if 443 is privileged and cannot be bound by a non-root user, VAF automatically falls back to 8443. The effective bound port is surfaced via `/api/network/status` (`effective_https_port`), so the UI always shows the real port. The proxy is the single TLS entry point and routes requests as follows:

| Path | Target | Description |
|------|--------|-------------|
| `/ws` | `ws://127.0.0.1:8005/ws` | WebSocket relay (bidirectional) |
| `/api`, `/api/*` | `http://127.0.0.1:8005` | Backend API (all HTTP methods) |
| `/sounds/*` | `http://127.0.0.1:8005` | Notification sound files (GET/HEAD) |
| Everything else | `http://127.0.0.1:3000` | Next.js frontend |

The proxy relays bodies **as they arrive, in both directions** (a live stream reaches the device as it is written), through **shared httpx clients with connection pooling** (as many connections as the door serves, 20 keep-alive) for both frontend and backend targets, avoiding the overhead of opening a new TCP connection for every resource request. Every connection to it is held to the limits in [The HTTPS door holds](#the-https-door-holds). The desktop app window loads the frontend directly over plain HTTP at `http://127.0.0.1:3000` (it must not use the proxy URL, whose certificate comes from an authority QtWebEngine does not trust). The proxy URL (`https://<LAN-IP>:8443`, or `:443` when bindable) is for devices on the LAN or a VPN, never the internet (see [Scope](#scope-private-networks-only)), and works without an external proxy. Optional: [NGINX_REVERSE_PROXY.md](NGINX_REVERSE_PROXY.md) and `docs/setup/nginx-vaf-https.conf.example`.

## Security Model

Security is the primary design constraint for VAF's network features. The system employs a **Defense in Depth** strategy with five network layers, plus an origin guard that runs in every mode, including single-user, and the limits every connection to the HTTPS port is held to:

### Scope: private networks only

VAF is served to this machine, the local network and VPNs, never to the public internet. That is a design decision, not a default: an agent's login page guards credentials, messages, files and an agent that acts, and on the internet it would be attacked without pause. Putting an agent's login page on the internet is close to negligent. Every layer below assumes it:

- The admission check refuses every address that is not admitted at the HTTPS door, before any page, and again in the backend, for HTTP and WebSocket alike ([Layer 2](#layer-2-admission-check)). There is no setting that admits a public network: an admitted network can only be private (`normalize_allowed_networks`), and "VPN only" off still refuses every public address. With Local Network off the HTTPS door and the backend serve this machine only. A person refused in a browser sees a short page that says which networks VAF answers and what to do (connect through the VPN, or run `vaf server status` on the machine VAF runs on), with no address or internal name on it.
- The firewall rules open the access port only to the admitted networks, and a switched-off ufw is said, not counted ([Layer 1](#layer-1-os-firewall-automation)).
- `vaf server provision` starts a machine with a public address in "VPN only" when it first sets it up as a server, and `vaf doctor`, `vaf server status` and the settings warn while the machine has a public address; the doctor also reports public addresses that reached the port.

A port forward on the router, a tunnel that publishes the access port (Cloudflare Tunnel, Tailscale Funnel, ngrok) or a reverse proxy that faces the internet is outside what VAF supports; remote access is a VPN ([Remote access over a VPN](#remote-access-over-a-vpn)).

Named boundaries:
- **The TLS handshake.** It happens before any HTTP check, so a client that reaches the access port still receives the certificate, whose names list the LAN and VPN addresses and the host name. Only a firewall, or a bind to the admitted interfaces only, closes that; a per-interface bind is not built, because a VPN that comes up later would need a rebind.
- **The frontend port behind a relay.** A tunnel or relay on this machine pointed at the Next.js frontend on `127.0.0.1:3000` shows its visitors the pages. Every API call those pages make is refused by the backend, which hears the visitor through the `X-Forwarded-For` the frontend's `/api` door forwards, in every mode. A check in `web/proxy.ts` would be a second copy of `inbound_policy()`; no supported path reaches port 3000 from another machine.
- **The provider's firewall.** VAF cannot see a hosting provider's firewall from inside. `network_public_refusals` in `vaf doctor` reports when the port was reached from a public address anyway.
- **1:1 NAT.** A server that sees only a private address (several clouds map a public one onto it) is not detected as public; there, run `vaf server vpn-only on` yourself.
- **The sandbox's tool bridge.** While a `python_sandbox` run calls VAF's tools (`with_vaf_tools=True`), `ToolBridgeServer` (`vaf/core/tool_bridge.py`) listens on `0.0.0.0` at a random port for the length of that run, guarded by a 128-bit token per run, not by the admission check. It is not bound to the container bridge's gateway: the environment reaches it as `host.docker.internal`, which is a different address per platform and setup (Docker's `host-gateway` address on Linux, Docker Desktop's forwarding on Windows and macOS), so a bind to one address would cut the bridge on the others. A host firewall with a default deny (firewalld, an enabled ufw) keeps it off the network; on a server, so does the provider's firewall that keeps the access port closed ([SANDBOXING.md](../security/SANDBOXING.md)).

### Origin Guard (every mode)

A tokenless request from this machine is the owner (the desktop window, internal IPC), and the `vaf_token` cookie is sent to every port of `localhost`, because `SameSite=Lax` keys on the site and every localhost port is one site. Neither trust can tell VAF's own Web UI from another web page open in the same browser. Only the browser's own marks can, so `ForeignOriginGuard` reads them before any identity is looked at and refuses what they name as foreign: HTTP with 403, a WebSocket handshake with close code 4003 before accept.

- **Not a web page, not judged.** A request with neither `Origin` nor `Sec-Fetch-Site` (CLI, sub-agent IPC, the tray, a script, `curl`) passes unchanged.
- **Host.** The `Host` the browser dialled must be `localhost` or an IP address. DNS rebinding needs a name the attacker controls, so a name is refused. `X-Forwarded-Host` is held to the same rule unless `X-Forwarded-Proto` is `https` (a TLS door cannot be rebound: the certificate would not match). The real `Host` is always checked, so forwarding headers a page adds itself change nothing.
- **Origin.** When present it must be the request's own origin, the origin a TLS proxy was dialled under (`https://` + `X-Forwarded-Host`: the integrated proxy, nginx), or the Web UI on this machine: plain `http` on `localhost`, `127.0.0.1` or `[::1]` at the port the frontend really runs on (`frontend_port()` in `vaf/network/binding.py`, the port file the frontend writes, else `local_network_port_frontend`). `null` is never own.
- **No Origin.** `Sec-Fetch-Site` `same-origin` or `none` (typed, bookmarked) passes, and so does a GET top-level navigation (`Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`), a link the person followed, which is how OAuth callbacks arrive. A cross-site image, script, frame or form from someone else's page is refused.
- **Every door carries the marks.** The HTTPS proxy forwards all client headers on HTTP and, on its WebSocket relay, the `Origin`, the dialled host (`X-Forwarded-Host`), `X-Forwarded-Proto: https` and `Sec-Fetch-Site`. The Next.js `/api` route forwards `origin`, `sec-fetch-site` and the browser's `host` as `x-forwarded-host` (it leaves `sec-fetch-mode` out: Node's fetch overwrites it with `cors`), and `x-forwarded-for`, which Next.js fills with the socket address when it is absent: the desktop window forwards its own `127.0.0.1`, a relay in front of the frontend port the client it named.
- **Frames the app sandboxes without `allow-same-origin`** (the HTML viewer) run under an opaque origin: a request they send with an `Origin` header (a fetch, a form) carries `Origin: null`, which the guard refuses; their other requests are judged by the rules above like any other page's. The guard cannot help markup that runs under the app's OWN origin; that is why no frame of the web UI gets `allow-scripts` together with `allow-same-origin` (`tests/test_web_untrusted_html_frames.py`, [WEB_UI.md](../web-ui/WEB_UI.md)).
- **Recorded** as `foreign_origin_blocked` in the security event log (`detail`: the reason `origin`, `site` or `host`, and the origin or host).

Named boundaries: a browser that sends no fetch metadata (Safari before 16.4) is judged on `Origin` and `Host` alone, so its plain cross-site GET passes. GET navigations from other pages pass, as they do for `SameSite=Lax` cookies, so a GET must not change state (an OAuth callback is protected by its `state`). The API serves no other web origin, even with a token. Another operating-system user on the same machine can reach the backend without a browser; that is the existing boundary of the tokenless localhost trust and not something an origin check can see.

Implementation: `ForeignOriginGuard` in `vaf/auth/middleware.py`, the decision in `foreign_request_reason` (`vaf/network/binding.py`), registered unconditionally in `vaf/core/web_server.py`. Pinned by `tests/test_foreign_origin_guard.py`.

### The HTTPS door holds

The access port is the one listener other devices reach, and it is hardened as if an attacker sat on the same network. Every connection is held to the limits below in the protocol and the event loop (`vaf/network/door.py`), admitted or not, before the admission check reads a single header. `vaf run --web`, which serves the backend itself on `0.0.0.0:8001` in network mode with no proxy in front, gets the same TLS policy and the same head, handshake and connection limits, for every client: the owner's own web UI reaches that backend through Next.js and counts toward the same cap.

| Limit | Value | Before (measured on the proxy with the settings it ran with) |
|---|---|---|
| TLS handshake | 15 s (`TLS_HANDSHAKE_TIMEOUT_S`) | asyncio's 60 s: a TCP connection that never started TLS was still open after 25 s |
| Request head, time | 15 s from the ready connection, or from the first byte that arrives while no request is being answered (the next request on a kept-alive connection, a stray empty line); then the connection is dropped without an answer (`HEAD_TIMEOUT_S`). The rest of a body after its request's answer is lingered for instead (below) | none: an idle connection after TLS and a head sent a byte at a time were still open after 25 s, and one empty line after an answer held a kept-alive connection for good (open after 45 s: uvicorn cancels its keep-alive timer on any byte, and the parser skips an empty line without starting a request) |
| Request head, size | 16 KiB for the head and the trailer section after a chunked body together, above it 431 (`HEAD_MAX_BYTES`, Node's own default, which every page behind the proxy already had) | none: one header of 1 MB and 2000 headers were answered 200 and relayed, and a trailer line that never ended took the process from 50 MB to about 1 GB in 48 s, admitted or not |
| Connections at once | 512, above it 503, for a WebSocket upgrade as for a request (`MAX_CONNECTIONS`; a quarter of the open-file limit when that is lower) | none: 520 idle connections, and a request still answered |
| Requests sent ahead (pipelined) | 16, above it the connection is dropped without an answer over the one still due (`PIPELINE_MAX`; browsers do not pipeline) | none: one send of 256 KiB of minimal requests whose answers were never read queued 9,362 of them (the process from 45 to 79 MB) and relayed them upstream one by one for minutes |
| Close | A close while the answer is still being sent waits as long as the answer moves: the connection is dropped only when its unsent bytes have not shrunk for 60 s (`SEND_TIMEOUT_S`, modelled on nginx's `send_timeout`: timed between two writes, never for the whole answer). Once all is out, 10 s for the client to end it (its TLS close_notify), then the connection is dropped (`CLOSE_TIMEOUT_S`). Both on every Python version, whoever closed, the client included: its own end (a FIN or a TLS close_notify) goes through the same close, and so does the close asyncio's TLS layer makes by itself on the connection under it (without them, a TLS client that ended its side and read nothing more was held on Python 3.11+, still held after 130 s, and a TLS WebSocket whose client stopped reading and sent close_notify was held past 150 s on Python 3.13 and 3.10). The head timer and the pipeline limit drop their connection at once | asyncio's 30 s for a TLS close on Python 3.11 and later (none on 3.10, measured on 3.10.15: still open after 40 s), counted over the whole close, and none for a plain close the client never reads, while the closed connection kept its descriptor and its place under the cap: four idle connections at a cap of 4 turned new requests away until 45 s, three refused clients that ignored the close held their places 30 s. A close limit counted from the close cut answers still being read: 65 to 89 % of a 4 MB one-piece answer read at 200 KB/s arrived, now all of it, plain and TLS, kept alive or with `Connection: close`, on Python 3.13 and 3.10.15 |
| Lingering | An answer given while its request's body still arrives (a 403 from the admission check, a 413 or 408 from the relay, an app that answers before it reads) reads and discards the rest, at most 30 s in all and 5 s between two reads, then closes the connection (`LINGER_TIME_S`, `LINGER_TIMEOUT_S`, modelled on nginx's `lingering_close` and its defaults). The answer stays readable, and the connection takes no further request. An upload still going after 30 s, or one that pauses for 5 s and then sends again, meets a closed connection: it may see a reset instead of the sentence | closed at once, a connection whose body still arrived was reset and its answer lost (a refused 8 MB POST got a reset, a 413 under a 40 MiB upload a reset on the next send); kept alive, a refused client that sent a request with a small body every 4 s held one connection for good (5 answers, still open after 40 s) |
| Keep-alive | 5 s (`KEEP_ALIVE_S`) | the same, uvicorn's default, now written down |
| Request body (proxy) | 128 MiB, above it 413; a declared Content-Length above it is refused before a byte is read (`BODY_MAX_BYTES`, more than twice the largest body VAF takes, a 40 MiB document as base64) | none: a 150 MB body was read whole into memory, then relayed |
| Body stall (proxy) | 60 s without a byte, then 408 and the upstream request ends (`BODY_STALL_S`) | none |

- **Open files.** A systemd service starts with a soft limit of 1024 open files (the hard limit is 524288), and about 800 slow connections then took every descriptor of the process: the proxy, the backend and its database pool. VAF raises its own soft limit when the door starts, to the hard limit but at most 65536 (10240 on macOS when the hard limit is unlimited), never lower than it was (`raise_open_file_limit`). Child processes inherit it. A connection cap below 512 is logged once with the limit that set it.
- **TLS.** One policy for the HTTPS port, the desktop's TLS port and `vaf run --web` (`server_tls_options` in `vaf/network/ssl_utils.py`): over TLS 1.2 only the six forward-secret AEAD suites of Mozilla's intermediate profile (ECDHE with AES-GCM or ChaCha20-Poly1305), TLS 1.3 as OpenSSL offers it, key exchange groups at OpenSSL's default (the post-quantum hybrid `X25519MLKEM768` first on OpenSSL 3.5), TLS 1.2 as the floor. The proxy asked for OpenSSL's `DEFAULT` and the two others for nothing, and all three accepted static RSA (no forward secrecy: a key stolen later decrypts recorded traffic) and CBC with SHA-1 (measured: `AES128-SHA`, `AES128-GCM-SHA256`, `AES256-SHA256`, `ECDHE-RSA-AES128-SHA`). A device from before about 2014 that knows only those suites cannot connect any more (the oldest the profile keeps: Android 4.4.2, Firefox 27, Chrome 31, IE 11 on Windows 7, Safari 9).
- **Forwarding headers.** uvicorn's own forwarded-header handling is off on the HTTPS port and on `vaf run --web`. It trusted the addresses in the environment variable `FORWARDED_ALLOW_IPS`, and with `*` a device on the network that wrote `X-Forwarded-For: 127.0.0.1` reached the backend as `127.0.0.1`, the tokenless owner (measured). The resolver ([Who the client is](#who-the-client-is)) is the only rule there. The backend's loopback listeners (8001, 8005) keep uvicorn's default; only this machine reaches them, and the resolver reads the header again behind it.
- **Streaming.** The proxy relays each body as it arrives, both ways. Live streams (the mail and WhatsApp drafts, the memory answer) reach the device as they are written (measured: the first event of a stream through the proxy after 3.0 s before, at once now). A compressed answer passes through compressed with the upstream's own Content-Length; a client that asked for no encoding gets none. Each response carries one Content-Length, one Date and no `Server` or `X-Powered-By` header, and repeated headers stay repeated (two `Set-Cookie` are two). Before, the proxy decoded and re-counted every body: each response carried two Content-Length, two Date and two Server headers, HEAD answered with Content-Length 17 and 0, and a compressed answer with its own Content-Length went out with two different values and no body.
- **Refused clients keep no connection.** The admission check's 403, the page and the JSON answer alike, carries `Connection: close`. Kept alive, one empty line after it held the connection and its slot of the cap for as long as the client liked (measured: three such connections at a cap of 4 turned every other device away with 503), and a request with a body every 4 s held it for good. A body still arriving is lingered for, so the 403 stays readable (measured: refused POSTs of 2, 8 and 64 MB each got the 403, and the repeat client got one answer and then the end of its connection).
- **What a refusal says.** Each answer of the door itself carries one short plain-text sentence: 431 "Request header too large", 413 "Request body too large", 408 "Request body timed out", and at the cap a 503 "Service Unavailable" (uvicorn's wording; the door writes the same answer for a WebSocket upgrade at the cap). They are English only: they are answered before any session or language exists. A 413 or 408 given while the device is still sending stays readable, because the door lingers for the rest of the body before it closes.
- **WebSocket, backend first.** The relay dials the backend before it accepts the device, and accepts only once the backend accepted the relayed handshake. A handshake the backend refuses fails at the device's handshake (HTTP 403), as it does on the desktop's 8005, so the web UI checks its session and goes to the sign-in page; it used to open on the device, close with 1000 and reconnect again and again, and a device without a session could send frames into the door until the refusal came.

Named boundaries:
- **The TLS handshake** comes before every limit but its own 15 s, and before the admission check ([Scope](#scope-private-networks-only)). A connection in its handshake counts toward no cap (measured: 60 connections that never started TLS, 60 more descriptors, released at 15 s), and the cap turns requests away, not connections: a connection it turns away holds a descriptor for at most about 70 s, 15 s for its handshake, 15 s for its head, 30 s of lingering when its body still arrives after the 503 (the 503 is uvicorn's own answer and lingers like any other; telling it apart would repeat uvicorn's cap check) and 10 s for the close (the 503 is small, so it is out at once). The spare descriptors divided by 70 s is the rate of new connections that takes them all. With the raised limit of 65536: 65536 less 211 at rest (measured) and three for each of the cap's 512 connections leaves 63,789, so about 900 a second, sustained. Where the limit stays at 1024 (cap 256): 1024 less 211 at rest and 256 to 768 for the cap's connections (one descriptor each when idle, three when relayed) leaves 45 to 557, so between about 0.6 and 8 a second. Only a firewall rate limit in front of the port closes that.
- **A device that stops taking an answer** while its connection closes is dropped after 60 s without progress, and what is still unsent is lost. Who sees a close while an answer is still being sent: a device that asked for `Connection: close` (HTTP/1.0 tools, scripts, `curl` with that header), and a kept-alive one still reading a large one-piece answer when the 5 s keep-alive ends (the backend's own one-piece answers under `vaf run --web`: room files, mail attachments, voice audio, large JSON). Progress counts when the kernel takes more of the answer, and Linux takes more only once a large share of its send buffer has drained, so such a device must take about that share within each 60 s. Measured on loopback, where that buffer grows to megabytes: a 4 MB answer read at 200 KB/s arrived whole; readers at 5 and 10 KB/s were dropped after 60 s, having read 0.3 and 0.6 MB, and in all 65 % of the answer reached the 5 KB/s reader from the kernel's buffers. On a real link the send buffer, and with it the floor, is smaller. The operator sees nothing of this in the logs; the device sees an answer that ends early, or a reset.
- **A client that ends its own side early** (a half-close) gets what is already written when its end arrives, as long as it keeps reading. Nothing written after that reaches it, because asyncio ends a connection at the client's end of file and its TLS transport cannot stay half open. Measured, plain and TLS, with or without the door's close handling: none of a 1 MB answer written 0.5 s later reached such a client, and of a streamed answer only the part written before its end did, the first 64 KiB piece of 1 MB on a door listener and nothing through the proxy. Browsers do not half-close.
- **On Windows**, asyncio's Proactor transport counts a whole pending send until it completes, so the close watch sees no progress inside one large send: a one-piece answer its device needs longer than 60 s to take is dropped there. Read in asyncio's code (`proactor_events`), not measured on Windows.
- **One address can fill the connection cap.** What the cap changes is the blast radius: a flood gets 503 at the door while the backend keeps working, and with the tray, so does the desktop window, which bypasses the proxy. Under `vaf run --web` the backend is the door itself, so the owner's own web UI, which reaches it through Next.js, is turned away with the flood. A count per address would need a second reader of the socket peer and bookkeeping across the WebSocket upgrade; no incident asks for it.
- **Slow but steady.** A device that reads an answer slowly, or never, holds one door connection and one upstream connection for as long as the answer goes on and it keeps the connection open: uvicorn has no write timeout (measured: five devices that never read a streamed answer still held both at 70 s). Once the door closes the connection, the close watch ends it. A body sent in parts, each within 60 s, holds its connection while it lasts. The cap bounds how many.
- **`vaf run --web`** has no proxy, so a body there is held only by the route that reads it.
- **The backend's loopback listeners** (the tray's 8001, with TLS when TLS is on, and 8005) keep uvicorn's defaults: none of the limits above, the close watch and lingering included. They bind `127.0.0.1`, so only this machine reaches them.
- **Refusals at this level** (431, a closed head, a dropped pipeline, 503) appear in neither the access log nor the security log, because a flood must not become a write flood; uvicorn logs one line of its own for each 503 it answers, and the door logs the same line for a WebSocket upgrade it turns away.
- **An upstream that fails after its answer started** ends the stream there: the device sees a cut-off answer, not a 502.
- **uvicorn's internals.** The bounded protocol subclasses uvicorn's httptools protocol and overrides only asyncio's protocol methods, httptools' parser callbacks, `handle_websocket_upgrade` and `on_response_complete`, and reads the state uvicorn keeps per connection (`cycle`, `pipeline`, `connections`, `tasks`, `limit_concurrency`, `flow`) and per answer (`response_started`, `response_complete`, `disconnected`, `message_event`), all unchanged from uvicorn 0.27 to 0.54; httptools 0.8 and 0.9 report a chunk's start and end, which the trailer count reads. `tests/test_network_door.py` pins them and runs on CI against the newest uvicorn `requirements.txt` resolves. The door reaches the TCP transport under a TLS one through asyncio's private `_ssl_protocol._transport`: it reads that transport's progress (Python 3.11+, where the TLS transport counts only its encryption backlog) and hooks its close (every version); the pin tests (`test_a_tls_transport_shows_the_tcp_transport_under_it`, `test_the_tcp_transport_under_tls_gets_the_close_hook`) and the TLS WebSocket test fail if that moves. A uvicorn upgraded by hand after a release is not covered.

Implementation: `vaf/network/door.py` (`door_options`, `door_event_loop`, `BoundedHttpToolsProtocol`, `raise_open_file_limit`), `server_tls_options` in `vaf/network/ssl_utils.py`, the relay in `vaf/network/https_proxy.py`. Pinned by `tests/test_network_door.py` and `tests/test_proxy_streaming.py`.

### Layer 1: OS Firewall Automation

When "Local Network Hosting" is enabled, VAF automatically configures the OS firewall (Windows Firewall, macOS pf, or Linux).

On **Linux**, VAF **prefers firewalld** when it is running. It opens **only the effective proxy port** (e.g. 8443) for the **admitted networks** via rich rules (e.g. `source address="192.168.2.0/24" port="8443" protocol="tcp" accept`) - not a blanket world-open. The sources are the LAN subnets this machine sits on (never all of RFC 1918), the admitted VPN networks and the networks an admin added (see [Remote access over a VPN](#remote-access-over-a-vpn)); each gets one rule in the zone of every LAN and VPN interface and in the default zone, because a fresh WireGuard or Tailscale interface is usually in no zone and its packets are judged in the default one. The backend (8001) and frontend (3000) bind `127.0.0.1` and are deliberately **not** opened (they are unreachable from the LAN). Elevation uses **pkexec** in a desktop session (a native polkit password dialog appears when hosting is enabled) and `sudo -n` headless (non-interactive, fails fast, never hangs on a TTY). A process that cannot gain root at all does not try: `elevation_unavailable()` in `vaf/network/firewall.py` reads the no-new-privileges flag and a user namespace that does not map root from the process itself, and for a process that is not root already either one (the server's systemd user service has both) rules out every setuid helper, sudo and pkexec alike, whatever sudoers says; a process that runs as root needs no helper and is never refused. There the setup runs no sudo and logs once per access port why the firewall was not changed; the installer and `vaf server provision` own the step from a login shell ([SERVER_MODE.md](SERVER_MODE.md#troubleshooting)). Firewall setup runs off the startup critical path (daemon thread) so it never blocks startup. Idempotence deliberately does NOT ask firewalld: an unprivileged `firewall-cmd --query-rich-rule` is a polkit `auth_admin` action on common distros (the firewalld `config.info` action; only the runtime `info` action is free), so a presence check would itself raise the root password dialog on every start. Even `firewall-cmd --state` is admin-gated there (measured: polkit action `FirewallD1.config`), so the "is firewalld running" probe asks `systemctl is-active` instead - the only firewall-cmd calls a normal start may make are the two free zone lookups. Instead, a local marker file (`~/.vaf/firewalld_lan.json`) remembers the set of rules this install set up: on a marker hit the start runs zero firewall-cmd config reads and can never prompt. Only a change (first run, another port, subnet or zone, an admin admitting or dropping a network) elevates - exactly once, with each check and both adds (runtime + permanent) inside the same elevated shell, together with the removal of the rules this install had set up and no longer wants. A removal asks first, so a rule that is already gone is fine; a removal that fails fails the whole elevation, the marker keeps the rule, and the next setup tries again (otherwise the permanent copy would return at the next boot while the marker had forgotten it). A marker from before the rule sets (one zone, one rule) still counts, so an update alone raises no dialog. The setup also runs **at most once per app process** (in TLS mode the same app runs two server lifespans; without that claim a cancelled dialog would chain straight into the twin's dialog). If the rule is ever removed behind VAF's back the marker goes stale and the port stays closed (the safe direction); delete the marker file or toggle Local Network to re-run the setup. `iptables`/`ufw` remain as the fallback when firewalld is not running.

ufw counts only when it is switched on. `host_firewall()` in `vaf/network/firewall.py` is the one answer to "which firewall guards this machine" (`firewalld`, `ufw`, `ufw_off`, `iptables`) for the setup and `vaf doctor`, and it reads ufw's own switch, `ENABLED=yes`, from `/etc/default/ufw` and then `/etc/ufw/ufw.conf` (both world-readable, no root, no command output parsed), by the shell's rules, since ufw-init sources both files in that order and that start is what loads the rules at boot. ufw-init runs under `set -e`, so a line the shell fails on in either file (`IPV6 = yes`, a CRLF copy of the file: exit 127, measured in dash) aborts the start and leaves ufw off however ufw.conf reads, and so does an empty file; an `ENABLED=yes` in `/etc/default/ufw` alone switches ufw on. The switch is on for `yes` or `YES`, names case-sensitive (`enabled=yes` is another variable), `#` a comment only at the start of a word (`ENABLED=yes#note` is off), and a line outside plain assignments makes the answer unknown rather than on (measured against dash). The files decide because systemd's `active` cannot tell: on Ubuntu 24.04 `systemctl is-active ufw` says `active` while ufw is off, which is Ubuntu's default (measured on a test server: `ENABLED=no`, and VAF had recorded allows for the access port that did nothing). Its `inactive` is trusted, though: `systemctl disable --now ufw` stops the unit, whose stop flushes ufw's rules, while `ufw.conf` keeps `ENABLED=yes`, so a ufw whose unit is `inactive` counts as off too. The setup used to take ufw whenever the binary existed and report the port as opened. A switched-off ufw now gets no rule at all, the setup answers `inactive`, the start logs once per access port that this machine's firewall does not guard it, and `vaf server provision` says it with what to do (`ufw_off_advice()`): allow SSH first, by `sudo ufw allow OpenSSH` only for port 22 (that profile opens 22 alone) and otherwise by the port number, the one the SSH session came in on when VAF can see it; allow the VPN's own port (WireGuard: `51820/udp` unless its configuration names another, any other VPN its own); then `sudo ufw enable`. The advice follows the state ufw is in (measured in ufw 0.36's own code, Ubuntu 24.04). `ufw enable` writes the switch and loads the rules itself, but never starts or enables the systemd unit, and only the unit loads them at boot: a stopped unit also gets `sudo systemctl enable --now ufw`: until then systemd keeps saying `inactive`, and a disabled unit does not load the rules at the next boot. With the switch still on while the unit is stopped, which is what `systemctl disable --now ufw` leaves, `ufw allow` writes the IPv4 rule and then fails reloading the chains the stop deleted, before it writes the IPv6 one, so the advice starts with `sudo ufw disable`, also when the switch cannot be read with certainty in either file. With `MANAGE_BUILTINS=no` in `/etc/default/ufw`, Ubuntu's default, that step only flushes ufw's own chains and sets ACCEPT policies until `ufw enable` follows. With `MANAGE_BUILTINS=yes` ufw's stop and its start both flush every other iptables rule as well, Docker's included, and nothing in ufw restores them; every ufw start does it then, so there is no gentler sequence, and the advice says so and adds `sudo systemctl restart docker` after `sudo ufw enable` (VAF's containers come back by their `unless-stopped` restart policy). When the two files give no certain value (one unreadable, or a line outside plain assignments), the advice says the same as a condition and names both, `/etc/ufw/ufw.conf` being read last and winning. With "VPN only" on and no VPN up, nothing besides this machine is admitted, so the setup opens nothing and provision says "Firewall not opened" instead of "opened". NAMED BOUNDARY: a switch that cannot be read with certainty still counts as ufw, so the setup adds its allow rule (ufw applies it at once to its own chains when its own command reads the switch as on and only records it otherwise), while counting it as off would leave the port closed to VPN clients on a ufw that is on. The setup runs no `ufw reload`: on an enabled ufw a reload is a stop and a start, and with `MANAGE_BUILTINS=yes` both flush every other iptables rule, Docker's included. What is not known is not reported as guarded, and neither is a ufw whose systemd unit is in any state other than `active` (`failed`, what an aborted start leaves, or `activating` or `unknown`, so which of its rules are loaded is not known; a host without systemd is not counted, or every such host would be), nor a switch line ufw's own command reads differently from the shell: the command reads the files line by line with its own pattern (`_get_defaults` in ufw 0.36's backend.py), so `ENABLED=yes # note`, `ENABLED='yes'`, a blank before the name or a later `enabled=no` are off for it while ufw-init loads the rules at boot, and `ufw allow` then only records the rule until ufw next starts (measured with those lines of ufw's code): provision says the rule was added but VAF could not confirm ufw is switched on, and why, `vaf doctor` reports `network_ufw_unknown`, and both name `sudo ufw status` as the check (`ufw_switch_unconfirmed()`). NAMED BOUNDARY: iptables rules (a distribution with neither firewalld nor ufw) last until the next reboot; the service cannot gain root to add them again, so a reboot needs `sudo -v && vaf server provision`.

On other platforms, the firewall is configured to:
- **Allow**: Traffic from the admitted networks: by default the RFC 1918 ranges (`192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`), with "VPN only" the VPN networks instead, plus the networks an admin added. The list comes from `firewall_sources()` in `vaf/network/binding.py`, the same decision the IP check makes; no backend keeps a list of its own. netsh, iptables and pf rules are rewritten whole on every setup; ufw keeps every allow it is given, so VAF records the allows it made in `~/.vaf/ufw_sources.json` and deletes those for networks that are no longer admitted, by the same rule spec (no ufw output is parsed; without the record, the RFC 1918 allows of earlier versions are assumed).
- **Allow**: Localhost traffic (`127.0.0.0/8`, `::1`).
- **Block**: All other incoming traffic to VAF ports.

Manual firewalld command if needed:
```bash
sudo firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="<LAN-subnet>/24" port port="8443" protocol="tcp" accept' && sudo firewall-cmd --reload
```
(the subnet-scoped rich rule is preferred over a blanket `--add-port`).

Implementation: `vaf/network/firewall.py`

### Layer 2: Admission Check

Every HTTP request and every WebSocket handshake passes through `AdmissionGuard` (`vaf/network/admission.py`) twice over: at the integrated HTTPS proxy, the access port on the network interface, before anything is relayed, and again in the backend. It validates the client address against the networks `inbound_policy()` admits, at the application level: this machine, then by default the RFC 1918 ranges, or with "VPN only" the VPN networks instead, plus the private networks an admin added (which may lie outside RFC 1918, such as `100.64.0.0/10`). This acts as a second barrier if firewall rules are misconfigured or bypassed.

- Rejects any address outside the admitted networks with HTTP 403, a WebSocket with close 4003 before accept, before any route or socket handler runs (recorded as `ip_blocked` with the device's address and the path in `detail`, so a scanner walking many paths is one line per address per 5 seconds); under "VPN only" that includes the home network
- Says what to do. A browser that loads a page (a GET or HEAD outside `/api` whose `Accept` names `text/html`) gets a short HTML page: VAF answers only this machine and the networks it admits, its local network or a VPN; connect through the VPN, or run `vaf server status` on the machine VAF runs on. It names no address, runs no script and is never cached (`refusal_page` in `vaf/network/admission.py`; English only, since no session or language exists yet, and in the system's color scheme, since reading VAF's theme from the browser's storage would take a script). An API call keeps the JSON answer, `{"detail": ...}` with the same first sentence, and a socket a short close reason. The refusal used to be one JSON sentence, "only local network clients are allowed", which is false under "VPN only" and gave a person who opened the login page from a network not admitted yet (a mesh VPN before `vaf server networks tailscale on`, the home network under "VPN only") no way forward
- Judges WebSockets too. The check used to be a `BaseHTTPMiddleware`, and Starlette passes every scope that is not HTTP through such a middleware unjudged (measured: HTTP 403 while a WebSocket route behind the same check opened and answered). Only `/ws` checked the address itself; the room socket (`/ws/a2a/{room_id}`) and the interactive browser's stream checked none, and `vaf run --web` serves the backend on `0.0.0.0:8001` in network mode, so there they faced the network directly
- Judges the REAL client, not the socket peer: the integrated proxy relays every device over loopback, so the peer is `127.0.0.1` for all of them. The address comes from `connection_client_ip` in `vaf/network/binding.py`, which honors `X-Forwarded-For` only when the peer is loopback (see [Who the client is](#who-the-client-is)). This check used to read the peer and therefore passed every relayed request, a public address on the proxy port included (measured: 200 through the proxy, 403 only on a direct connection), while the WebSocket handshake already refused it.
- Admitted are this machine, the local networks (RFC 1918) and the networks an admin added; with "VPN only" the networks of the detected VPN interfaces replace the local ones. WireGuard and OpenVPN clients (`10.x`) pass by default; mesh VPNs that hand out `100.64.0.0/10` (Tailscale, Headscale, NetBird) pass once that network is admitted. The decision is `inbound_policy()` in `vaf/network/binding.py`, read on every request, so a change applies without a restart - see [Remote access over a VPN](#remote-access-over-a-vpn).
- Active in network mode. With Local Network off the HTTPS door and the backend serve this machine only: every other client is refused with 403 (the page says Local Network is off), a WebSocket with 4003 before accept (see [Middleware Execution Order](#middleware-execution-order)). The door follows the setting per request like the backend (`ByLocalNetwork` in `vaf/network/admission.py`, both doors built on it), because the proxy outlives a switch to off until the restart stops it (it starts only with Local Network and TLS on); it used to relay pages to LAN devices then while the backend refused their API calls
- The door refuses before any page. The check used to sit only in the backend: page loads went from the proxy straight to the Next.js frontend, so a client outside the admitted networks received the login page and only the API calls behind it were refused (measured: `GET /login` 200 for `203.0.113.9`, its socket accepted by the proxy). The proxy now wraps its whole app in the guard, outermost, so a refused request reaches neither the frontend nor the backend nor the proxy's access log (which keeps one line per request and has no throttle); the security log holds it.
- Named boundary, the TLS handshake: it happens before any HTTP check, so a client that reaches the access port still receives the certificate, whose names list the LAN and VPN addresses and the host name, and learns that a TLS server runs there. Only a firewall, or a bind to the admitted interfaces only, closes that. A per-interface bind is not built: a VPN that comes up later would need a rebind.

`connection_client_ip` is the only place in `vaf/` that reads a connection's client address; the auth middleware, the rate limiter, the login routes, the WebSocket handshake, the OAuth callback and the security log entries all ask it. `tests/test_client_address_has_one_reader.py` refuses a direct read anywhere else, the integrated proxy included: it writes `X-Forwarded-For` from the same resolver (a device that connects directly as itself, a relay on this machine as the client that relay named), after dropping every copy the client sent, and on its WebSocket relay it sends the header on every handshake (`unknown` when there is no client, which fails closed).

#### Who the client is

`effective_client_ip(peer, x_forwarded_for)` in `vaf/network/binding.py` decides it, for every reader above:

- A peer that is not loopback is the client; any header it sends is ignored.
- A loopback peer is a relay on this machine (the integrated proxy, the Next.js `/api` door, or a reverse proxy in front of VAF on this machine, should there ever be one). Its `X-Forwarded-For` is walked from the RIGHT: each hop is normalized (spaces and brackets dropped, an IPv4-mapped `::ffff:a.b.c.d` read as `a.b.c.d`), a loopback hop is skipped because it names this machine, and the first hop that is not loopback is the client. A hop that is not an IP literal (a name such as `localhost`, an address with a port) ends the walk as `unknown`, which is neither local nor admitted.
- No header, or only loopback hops: the peer, so internal IPC and the desktop stay local.
- Every `X-Forwarded-For` line counts, joined in order into one list (RFC 9110 5.3). A relay that adds a line of its own (HAProxy's `option forwardfor`) puts the client it saw in the last line; the resolver read the first line alone, so a client's own line (`127.0.0.1`, or a LAN address) was the answer wherever uvicorn's proxy-header handling is off (measured with the guard: lines `127.0.0.1` then `9.9.9.9` resolved to this machine).

Why the rightmost: a relay appends the address it saw, and a client can only write to the LEFT of that. The first hop, which VAF's resolver used to read, is whatever the client wrote. In the running server that was hidden on the HTTPS port: uvicorn's default `proxy_headers`, which every uvicorn VAF started kept then, trusts `127.0.0.1` and replaces a loopback peer with the rightmost `X-Forwarded-For` entry that is not `127.0.0.1` before VAF sees the request (measured on a test server: a relay to the HTTPS port naming `203.0.113.9` was refused). The resolver now applies the same rule itself, so the answer does not hang on a server default (an embedder's server, `proxy_headers` off), and a hop that is not an address (`localhost`) is never read as this machine. The doors other devices reach (the HTTPS port, `vaf run --web`) now switch uvicorn's handling off, so there the resolver is the only rule; the backend's loopback listeners keep uvicorn's default. What was open in the running server is the Next.js `/api` door: it passed on no `X-Forwarded-For`, so a relay on this machine pointed at the frontend port (a reverse proxy, or Tailscale Serve inside a tailnet, in front of `http://localhost:3000`) reached the backend as `127.0.0.1` with no hop, and every visitor was the machine owner (measured on a test server: `X-Forwarded-For: 203.0.113.9` to port 3000 answered the account list, 200). The door forwards the header now (Origin Guard, "Every door carries the marks"). The polarity stays fail-safe: a hop can only make a client look more remote.

VAF is for the LAN and the VPN only: the admission check refuses public addresses, and putting VAF on the public internet is not supported ([Scope](#scope-private-networks-only)). Such a relay serves devices on your own networks; one that publishes VAF to the internet is outside the supported scope. Should a relay of your own on this machine ever sit in front of VAF, it must write `X-Forwarded-For` with the client it saw (nginx: `$remote_addr`, or `$proxy_add_x_forwarded_for`, which the rightmost rule makes safe as well; Tailscale Serve does it on its own). uvicorn's `FORWARDED_ALLOW_IPS` environment variable no longer decides anything on the doors other devices reach: with `*` set, a device on the network that wrote `X-Forwarded-For: 127.0.0.1` counted as this machine on the HTTPS port (measured), and the HTTPS port and `vaf run --web` now switch uvicorn's own proxy-header handling off ([The HTTPS door holds](#the-https-door-holds)). On the backend's loopback listeners, where it still applies, only this machine connects. NAMED BOUNDARY: a raw TCP forward on this machine (`ssh -R`, socat, a port forward that is not an HTTP proxy) writes no header at all, so every client it carries is this machine to every check. Never put one in front of VAF; replacing the tokenless local trust with a per-start IPC token is the separate change Layer 3 names.

Implementation: `vaf/network/admission.py` -> `AdmissionGuard`, run by `NetworkModeLayers` in `vaf/auth/middleware.py`

### Layer 3: JWT Authentication Middleware

Network clients must authenticate via JWT tokens. The `AuthMiddleware` enforces this:

- **Token First, IP Second**: A presented access token is validated **before** any peer-IP branching. If a request carries a valid JWT (`Authorization: Bearer <token>` header or `vaf_token` cookie), the authenticated user's identity and scope are applied regardless of the source IP. This matters when a LAN user is proxied over loopback (the request arrives from `127.0.0.1` but belongs to a remote user): they get **their own** scope, not the local admin's.
- **Localhost Bypass (tokenless only)**: A **tokenless** request from `127.0.0.1` is allowed without authentication (internal IPC and single-user desktop mode). A web page that is not VAF's own never reaches this bypass: the [Origin Guard](#origin-guard-every-mode) refuses it first. This bypass applies only when no token is presented. A present-but-invalid token rejects a network client with HTTP 401, while a localhost client with an invalid token falls through to the tokenless localhost path. VAF's OWN outbound fetches are loopback clients too: the agent's web tools, remote MCP servers and WebDAV used to reach this bypass with any URL a page, a mail or the model chose (measured: the contacts, the account list and the configuration). They now go through the destination guard (`vaf/network/egress.py`, see [SANDBOXING.md](../security/SANDBOXING.md)), which never connects to this machine. NAMED BOUNDARY: the receiving side still trusts a tokenless loopback request; a shell command the agent runs with the person's grant (`curl` through `host_bash`) is not covered by the guard, and replacing the bypass with a per-start IPC token is a separate change.
- **Network Clients**: Must present a valid JWT via `Authorization: Bearer <token>` header or `vaf_token` cookie.
- **Auth-Exempt Paths**: Login, bootstrap, the setup-code check and static asset endpoints are accessible without a token, and so is `GET /api/health`, which answers a monitor one word (`ok`, `degraded`, `down`) and HTTP 503 when down; the checks behind it are admin-only (`/api/system/health`). The network admission below still applies to all of them. The API documentation (`/docs`, `/openapi.json`) is not exempt: it was, and `vaf run --web` serves the backend itself on `0.0.0.0:8001` in network mode, so every device on the network read the whole route table without an account (measured: 200 for a tokenless LAN client). The machine itself still opens it on the tokenless path, and any signed-in account with its token (named boundary: it holds no data, and the source of the same routes is public).
- **Sessions**: a token whose session ended (logout, password change, reset, deactivation) is refused like an invalid one: a network client gets 401, a local client falls through to the tokenless path. When the refused token is the browser's `vaf_token` cookie, the 401 deletes the cookie: the page gate (`web/proxy.ts`) judges the cookie by its expiry alone, so an ended but unexpired cookie sent the browser from `/login` back to `/` and the page sent it back to `/login`. A Bearer token of a program's own leaves the cookie alone. The web UI then lands on `/login?reason=ended`, which says the session ended. The second-factor rule below is asked first, so a token from before the second factor, which has no session yet, keeps its 403. The two 2FA steps (`/api/auth/setup-2fa`, `/api/auth/verify-2fa`) are exempt from the middleware and make the same check themselves: a token minted after the second factor must still stand, and a token from before it carries a digest of the password it was minted with (`pwv`) and is refused once the password changed. They used to check only the signature, so a token whose session an admin's 2FA reset or a terminal reset had ended still enrolled a new second factor and came back as a live session (measured).
- **2FA Enforcement**: every network sign-in needs the second factor, and there is no switch. A token minted before the second factor was given (the `temp_token` `/login` returns for a correct password, `is_2fa_verified: false`) opens only `/api/auth/setup-2fa` and `/api/auth/verify-2fa`; every other route answers 403 "2FA verification required" to a network client. Every account is created needing a second factor and `/login` asks for it by the account's own state, so an account without one is led through the QR step at its next sign-in. The `local_network_require_2fa` key that used to sit here switched nothing any more (a token from before the second factor has no session, so the session check refused it either way); it is gone, a value left in an older `config.json` is ignored, and so is the `vaf doctor` warning that read it. The rule is one function, `vaf.auth.crypto.second_factor_done`, asked by the middleware, the WebSocket's network lane (which still admits a localhost admin) and the A2A room door. The check used to read a `requires_2fa_setup` claim no token carries, so a password alone opened the API. The WebSocket opens only on an ACCESS token (a refresh token carries no scope and opened an unscoped socket), and "localhost" there means loopback, `binding.is_localhost`, as on HTTP: it used to include the whole private /12 block Docker's bridges use, an RFC1918 range LANs use too, which reached the localhost-admin allowances.
- **The browser keeps no readable token**: the web UI's only credential is the HttpOnly `vaf_token` cookie, which no script in the page can read. It used to copy the token into the page's local storage and into the WebSocket address, where any script running in the page could read it and use it from anywhere for its whole life. No sign-in answer carries the session's token in JSON any more (bootstrap, `/login`, `/verify-2fa`); the step token between the password and the code stays in the page's memory for that step only and opens nothing but the two 2FA routes. The web UI dials `/ws` with no token in the address: the handshake sends the cookie, which reaches the backend's port on this machine as well (cookies are kept per host, not per port). One call in a module every page loads (`web/components/HostnameNormalizer.tsx`) removes the copy an older VAF left in local storage. A program uses a token of its own as `Authorization: Bearer <token>`.
- **WebSocket token in the URL, redacted from logs**: a WebSocket handshake cannot carry an `Authorization` header, so a program passes its JWT in the query string (`/ws?token=<jwt>`); the web UI sends the cookie instead. uvicorn's access log would otherwise print that live token in full (terminal and `tray_debug`); a redaction filter (`RedactTokenFilter` / `redacted_uvicorn_log_config` in `vaf/core/log_helper.py`) masks it only if EVERY uvicorn the process starts passes it: the uvicorn loggers are process-wide, and a Config built with uvicorn's defaults re-runs `dictConfig` and strips the filter from all servers, depending on which starts last (the HTTPS proxy did, and the desktop's live token was measured in `tray_debug.log`). The tray's main port and internal 8005 channel, the HTTPS proxy and `run_server` all pass it, pinned by `tests/test_ws_frame_size_pin.py`. The filter masks it to `token=***` (and the same for `access_token` / `api_key` / `password`). The URL still authenticates; only the log line is masked.
- **User Context Propagation**: On successful authentication, the middleware populates `request.state` with both individual attributes and a consolidated `user` dict for downstream route handlers (see below).

#### `request.state` Population

After validating the JWT, `AuthMiddleware` attaches the authenticated user's identity to the request in two forms:

**Individual attributes** (legacy, used by some internal utilities):
- `request.state.user_id` - Subject claim (`sub`) from the JWT
- `request.state.username` - Authenticated username
- `request.state.role` - User role (`admin`, `user`, `guest`)
- `request.state.user_scope_id` - UUID used for data isolation (see [USER_ISOLATION.md](../security/USER_ISOLATION.md))

**Consolidated dict** (used by all API route handlers):
```python
request.state.user = {
    "user_id": "<sub>",
    "username": "<username>",
    "role": "<role>",
    "user_scope_id": "<uuid>",
}
```

All API route files (`config_routes`, `email_routes`, `cloud_routes`, `whatsapp_routes`, `telegram_routes`, `contact_routes`, `user_persona_routes`, `memory/routes`) read `request.state.user` as a dict to extract the current user's identity. When `request.state.user` is not set, the fallback is **mode-dependent**:

- **Single-user / local mode**: routes fall back to the local-admin defaults (no network exposure, so the local user owns everything).
- **LAN server mode**: an unauthenticated request is **denied** for memory reads - it resolves to an empty scope and sees **no** memories (fail-closed). The local-admin floor is applied only in genuine single-user/local mode, never to an anonymous network client. The memory scope resolver (`get_current_user_scope` in `vaf/memory/routes.py`) is server-aware and chooses the fallback based on the running mode.

### OAuth Session Binding (Network Mode)

OAuth start/callback endpoints for Email, Cloud, and GitHub enforce a strict actor binding in network mode:

- OAuth start requires an authenticated user session (`request.state.user`).
- OAuth callback validates that the authenticated actor matches the identity encoded in OAuth `state` (username and/or `user_scope_id`).
- Mismatched callbacks are rejected with HTTP 403.
- The page a finished GitHub or cloud sign-in returns to (`redirect_base`, the Web UI's own `window.location.origin`) is kept only when it is an origin of VAF's own for the request that started the sign-in (`own_redirect_base` in `vaf/network/oauth_redirect.py`, the origin guard's `is_own_origin`); anything else falls back to the Web UI's address. It used to be any string: an open redirect, and on the callback's error page an unescaped `href`, so one account could hand another a callback link carrying its own markup. The error page escapes the link as well.

This avoids accidental or malicious cross-user credential binding in multi-user deployments.

Implementation: `vaf/api/oauth_session_binding.py` + OAuth routes in `vaf/api/email_routes.py`, `vaf/api/cloud_routes.py`, `vaf/api/github_routes.py`

### Operational Hardening Check

Use the built-in doctor command to detect common security misconfigurations before exposing VAF on LAN:

- `vaf doctor` (alias for `vaf security doctor`)
- Checks include weak network posture flags (TLS off, the firewall step off; there is no switch for the sign-in or the second factor, both are always required), a channel whose Inbound is open to new senders, and channel-enabled-without-pairing states.
- The certificate, in network mode: `network_certificate_expiring` (medium: the served certificate has under 30 days left), `network_certificate_expired` (high: it is served expired, so every device warns), `network_certificate_unusable` (high: the configured own certificate does not load, VAF serves its own instead) and `network_ca_unrestricted` (low: the local CA may vouch for any name; `vaf server ca renew`), all read from `ssl_utils.certificate_state`, which creates and writes nothing ([Your Own Certificate](#your-own-certificate), [Restricting the Authority](#restricting-the-authority)).
- Whether the access port faces the internet, in network mode: `network_public_address` (this machine has a public IPv4 address, `binding.public_addresses()`; medium, low once "VPN only" is on, since then a provider's own network is no longer admitted), `network_ufw_disabled` (ufw is installed but switched off while the firewall step is on, `firewall.host_firewall()`; with what to do, SSH allowed first), `network_ufw_unknown` (low: ufw's switch could not be read with certainty, ufw's own command reads it as off, or its systemd unit is not `active`, so the allow rule is added but nothing confirms ufw guards the port; check with `sudo ufw status`, `firewall.ufw_switch_unconfirmed()`) and `network_public_refusals` (high: today's security log holds `ip_blocked` refusals of public addresses, so the port was reached from the internet: an open hosting firewall, a forwarded router port or a tunnel). VAF cannot see a hosting provider's firewall from inside; the refusals are the evidence that it is open.
- Output is intentionally non-secret and safe to share in internal troubleshooting.

### Layer 4: Rate Limiting

Failed sign-ins are held in windows that only empty by time. Every window answers a held request with HTTP 429, the same body and a `Retry-After` header, and its first refusal is the security event `login_throttled` (`detail`: which window).

- **Address window** (`RateLimitMiddleware`): failed attempts per real client address (default 5 per 15 minutes) on `/api/auth/login`, `/api/auth/bootstrap`, `/api/auth/verify-2fa`, `/api/auth/change-password`, `/api/auth/test-veyllo-key` and `/api/email/accounts/test` (the credential tests record their own failures, since they answer 200). A failure is a 401: a wrong password, an unknown name, and a wrong 2FA code, which `/verify-2fa` answers with 401 (it was a 400, counted nowhere, measured: 30 wrong codes and no 429). A successful login clears nothing: `/login` answers 200 for a correct password before the second factor, so the clear let any account holder put a login of their own between guesses at other accounts (measured: 20 guesses, no 429).
- **Account window** (asked by `/login` before it checks the password, so a held account costs no hashing): failed logins per account name from any number of addresses (`local_network_rate_limit_account_attempts`, default 10 per window), so a guesser with many addresses is held too (measured before: 24 guesses at one account from 6 addresses, no 429). Unknown names are held the same way. Not held, so a stranger who knows a name cannot lock its owner out: the machine itself, and an address that completed a sign-in for that account within the last 30 days (a session row with that address); both stay under the address window.
- **Second-factor window** (`vaf/auth/second_factor.py`, one check for every door that asks for a code: `/verify-2fa`, the password change in Settings and an admin's step-up for lifting a quarantine, which before had three accountings, one by hand and two none): wrong codes per account and current password. Five hold the account's code for the window (429 at all three doors, the right code included), and each further closure within a day lasts twice as long as the one before, at most a day. A completed sign-in clears the count; a new password opens a fresh window. The machine itself is not exempt: the code cannot be reached without the password.
- **A code works once.** A code is accepted for the step before, the current and the next one (clock drift), and only when its step is later than the last one accepted for the account, so a code read over a shoulder or relayed by a phishing page cannot be used again (measured before: the same code accepted twice). A code used once already is refused as spent (`code_spent`) and not counted in either window: it is a right code, typically the one just used to sign in typed again for a password change or an admin's confirmation within the same 30 seconds, and the page says to wait for the next one. NAMED BOUNDARY: the last step lives in the backend process; after a restart a code seen before it can be used once more within its 90 seconds.
- **A burst is counted before it is checked.** The address and account windows count an attempt before it runs (check and count are one step) and take it back once it did not fail, so attempts in flight count toward the threshold and at most that many reach the password. Counted only after the awaited password work, a burst of concurrent logins all passed the check before the first failure was recorded (measured: 60 of 60 reached the password check in process; through the real HTTPS proxy, 45 per window).
- **The temp token** `/login` answers a correct password with before the second factor lives 15 minutes (it lived the full token lifetime, 24 hours). A step that ran out, and one whose token was minted before a password change, answer 401 with `"code": "sign_in_expired"`, and the page says to sign in again.

Configuration:
| Key | Default | Description |
|-----|---------|-------------|
| `local_network_rate_limit_attempts` | `5` | Failed attempts per address before blocking |
| `local_network_rate_limit_account_attempts` | `10` | Failed logins per account name, any address |
| `local_network_rate_limit_window_minutes` | `15` | Window length in minutes |

NAMED BOUNDARY: the windows live in the backend process; a restart empties them.

Implementation: `vaf/auth/rate_limit.py` (`RateLimitMiddleware`, the account window), the account window's exemptions in `vaf/api/auth_routes.py`

### Layer 5: Security Headers

All HTTP responses include security headers to protect against common web attacks:

| Header | Value | Purpose |
|--------|-------|---------|
| `X-Content-Type-Options` | `nosniff` | Prevents MIME-type sniffing |
| `X-Frame-Options` | `DENY`; `SAMEORIGIN` under `/api/browser-vnc/` | Prevents clickjacking via iframes. The interactive browser's viewer is the one page meant to be framed, and only by the web UI on the same origin |
| `X-XSS-Protection` | `1; mode=block` | Legacy XSS filter |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Limits referrer leakage |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` | Disables browser APIs |
| `Strict-Transport-Security` | `max-age=31536000` | HSTS: only when TLS is active, and the year only for a dialled DNS name that is neither `localhost` nor this machine's own (see below) |

Implementation: `_SecurityHeadersMiddleware` in `vaf/core/web_server.py`

That table covers the backend's API responses. The PAGES the integrated HTTPS proxy relays from the frontend carried none of it (measured: `/login` through the proxy had no frame protection and no HSTS), so the proxy adds its own set to every frontend response, each header only when the frontend did not set it (`PAGE_SECURITY_HEADERS` in `vaf/network/https_proxy.py`):

| Header | Value |
|--------|-------|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Content-Security-Policy` | `frame-ancestors 'none'` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Strict-Transport-Security` | `max-age=31536000`, for a dialled DNS name that is neither `localhost` nor this machine's own (see below) |

It differs from the backend's table on purpose: no `Permissions-Policy`, because the backend's `microphone=()` would switch off the web UI's own voice input, which runs in these pages; and `frame-ancestors` only on pages, because the interactive browser's viewer (`/api/browser-vnc/`, a backend response under `SAMEORIGIN`) is framed by the UI and a CSP `frame-ancestors` would override that. No page of the UI is framed by the UI (every frame is `srcDoc`, blank or that viewer). Backend responses relayed by the proxy get only HSTS when they lack it.

HSTS is scoped by the host the browser dialled (`hsts_for_host` in `vaf/network/binding.py`, asked by the proxy and by the backend's table): HSTS holds a host name on every port for a year, and the browser then refuses plain HTTP to any port of that name. The local certificate names `localhost`, the machine's host name and its FQDN besides its addresses (`machine_dns_names`, one list for both), and those names are also how every other service on the machine is reached: the web UI on port 3000, a NAS page, a printer, an app on another port. So the year is never sent for `localhost`, `*.localhost` or one of the machine's own names (any name whose first label is the first label of the host name or of the FQDN, which can differ: the host name, its FQDN, `<name>.local`, a router's or a VPN's name for it, a trailing dot ignored); those get `max-age=0`, which makes a browser drop a pin that versions before this one left there (they sent a year with `includeSubDomains` for every name). Nothing is sent for an IP address (browsers ignore it there, RFC 6797 8.1.1, and other devices usually dial VAF by address), and never `includeSubDomains`. The year goes only to a name set up for VAF on purpose with a certificate of its own (`local_network_ssl_cert`) whose first label is not one of the machine's: `vaf.example.lan` on a host called `vaf` counts as the machine's own and gets `max-age=0`. NAMED BOUNDARY: on a name that gets the year, every other plain-HTTP port is upgraded too, for a year. The FQDN is a DNS lookup (`getfqdn`), so an answer reads the machine's names from a memo (`own_name_labels` in `vaf/network/binding.py`): the certificate check and the HTTPS proxy fill it at the start, a certificate issued then renews it with the names it carries, and an answer that finds it older than a minute starts one lookup in a thread of its own and goes out with the names it has, as does every answer until that lookup lands. The memo keeps the lookup that started last, not the one that finished last, so a renewal that was already running when a certificate's lookup began cannot put older names back after it. A lookup that never returns (a resolver that hangs) leaves the names as they were, because no second renewal starts while it runs. A renewal that finds no FQDN (`getfqdn` failed or answered the bare host name, as it does while DNS is down) brings no names: it keeps the names the memo holds and their place in that start order, and renews only their time. So a short DNS outage cannot hand the FQDN the certificate carries a year of HSTS, and neither can such a renewal that lands while a certificate's lookup is still running: the certificate's names replace the older ones when its lookup lands. NAMED BOUNDARY: an FQDN that did not resolve at the start (DNS not up yet) or that changes while VAF runs counts from the first renewal after it, and the answers before that use the previous names; an FQDN removed while VAF runs keeps `max-age=0` until a restart; and the one answer that waits on DNS is the first for a host name with no lookup yet (a host renamed while VAF runs, or an answer ahead of the start's lookup).

### Middleware Execution Order

Requests pass through middleware from outermost to innermost. Starlette puts the middleware added LAST outermost, so this is the reverse of the `add_middleware` order in `vaf/core/web_server.py` (measured on `app.user_middleware`):

```
Request -> SecurityHeaders -> ForeignOriginGuard -> NetworkModeLayers -> OwnOriginCORSMiddleware -> Route Handler
                                                      |
                     network mode:  AdmissionGuard -> AuthMiddleware -> RateLimitMiddleware
                     Local Network off:  AdmissionGuard for this machine only (403, WebSocket 4003)
```

All four are registered in every mode. `NetworkModeLayers` (`vaf/auth/middleware.py`, built on `ByLocalNetwork` like the HTTPS door) reads `local_network_enabled` on every request and picks the chain: in network mode the admission check, the sign-in check and the rate limit, in that order; with Local Network off, an admission check that admits this machine only (`this_machine_only`), so a request from this machine goes straight on and any other is refused. The guard sits outside CORS, so a preflight from a foreign page is refused before CORS could answer it.

Admission comes before the sign-in check, so a client outside the admitted networks is refused as `ip_blocked` (403) before any token it sends is decoded or looked up in the account and session stores, and before the rate limiter counts it. With the sign-in check outermost, a tokenless public client got 401 and was recorded as `unauthenticated_blocked`, which `network_public_refusals` in `vaf doctor` does not count, so a backend reached directly (`vaf run --web` on `0.0.0.0:8001`) stayed invisible there.

Why per request: the three network layers used to be added when the backend module was imported, and only when Local Network was on at that moment. The tray serves a settings toggle by starting a new uvicorn on the SAME app, so a VAF started with Local Network off and switched on later ran the HTTPS proxy in front of an app without them (measured: a client the proxy relayed from `203.0.113.9` got `GET /api/config` with every key, the admin-only `/api/network/remote-access` and the account list, 200, as the local admin) until a full restart. And "off" means this machine only, not "no checks": the proxy outlives the switch by the frontend's restart (the tray restarts the frontend first and stops the proxy after it), `vaf run --web` keeps its `0.0.0.0` bind for its whole run, and a `config.json` that does not parse reads as the defaults; in each case a device that needed a sign-in a moment before would otherwise reach the local admin's route floors. The cost is one config read per request, about 0.15 ms.

### Security Event Log

Rejections from the layers above are recorded in an always-on security event log (independent of `debug_logs_enabled`; the writer never raises and never slows the request path):

- **Recorded kinds** (`vaf/core/security_events.py`): `ip_blocked` (Layer 2: 403, WebSocket 4003), `unauthenticated_blocked` and `token_rejected` (Layer 3 401s), `login_failed` and `twofa_failed` (failed login/2FA attempts), `login_throttled` (a window of Layer 4 closed, once per closure), `ws_rejected` (rejected WebSocket handshakes), and `foreign_origin_blocked` (the origin guard, every mode). The messenger pairing kinds and the rest of the registry are listed in [SECURITY_DASHBOARD.md](../security/SECURITY_DASHBOARD.md); a messenger sender the agent refused to answer is channel traffic, recorded in that channel's inbound log, not here.
- **Sinks**: each event is appended to `security_events_<date>.jsonl` (structured) and mirrored human-readably to `security_<date>.log` (the "security" domain in the Logs file rail). One event is one line there: line breaks and other control characters in any value (a username typed at the login, a path, an address) become spaces, because a login name with a newline wrote a forged second line into that file (measured), and a field value that could pose as more fields (a space, `=`, a bracket or a quote in it, such as a username `x ip=127.0.0.1 [password_changed]`) is quoted; the JSONL keeps the value as it was. The integrated proxy's access log (`https_proxy_access_<date>.log`) keeps one request on one line the same way: it writes the percent-decoded path, and `GET /page%0a...` wrote a forged line.
- **Throttle**: a per-source throttle (kind + ip + username + channel, 5s) prevents floods without letting distinct sources swallow each other's events.
- **Never logged**: passwords, 2FA codes, or tokens - only the event kind, source, and a short detail.
- **Visibility**: admin-only via `GET /api/security/events` (`vaf/api/security_routes.py`), surfaced on the Logs Overview dashboard.

### Authentication Details

- **Passwords**: every new password needs at least 12 characters and may not be one of the 10,000 most common passwords of that length (`vaf/auth/data/common_passwords.txt`, from SecLists under MIT, compared lowercased; `scripts/build_common_passwords.py` rebuilds it from the pinned source), one rule (`validate_password` in `vaf/auth/user_admin.py`) for the first admin from the browser and from `vaf setup`, an admin's new account, the account's own change in Settings and `vaf users reset-password`. A refusal carries `"code": "password_too_short"` or `"password_common"`. Passwords set before the rule keep working: VAF stores only an Argon2 hash and cannot see a stored password's length, so change a shorter one in Settings, Account. Not built: a check against the account name, a breached-password service (an outbound call), a forced change.
- **Password Hashing**: Argon2id (time_cost=2, memory_cost=64MB), run on a thread pool of its own (`vaf-argon2`, two workers, so at most 128 MB however many logins arrive) instead of the event loop that carries every chat and socket (measured before: 40 concurrent wrong logins delayed an unrelated request by about a second; after: under 0.1 s). Every login that passes the windows does exactly one verification, a name without an account against a dummy hash, so known and unknown names take the same time (measured before: 3.5 ms against 28.6 ms). No database session is open while a login waits for the pool: the lookup, the check and the writes are three steps (the writes check that the account and its password did not change meanwhile). A login held its session, a database connection on the service's engines, for the whole wait, so a burst of logins from many addresses for many names (which no window limits) held one connection each (measured: 40 at once for 40 logins), and could take the connections every other request needs. `verify_password_async` / `hash_password_async` in `vaf/auth/crypto.py`; the blocking functions stay for the terminal.
- **JWT Tokens**: HS256, each access token with a unique `jti`. There is no refresh token:
  `/api/auth/refresh` minted a 7-day credential that was stored nowhere, survived a logout and
  a password change, and no client used; it was removed, and a token it minted earlier has no
  session and is refused. A valid signature is not enough: every token reader (the HTTP
  middleware, both WebSocket lanes, the A2A room door) also checks that the token's account
  still exists, is active and holds the token's role, and that the token's session has not
  ended (`token_account_stands`; both cached a few seconds and cleared by whatever ends them),
  so a deactivation, a deletion or a demotion ends the token at once instead of at its expiry.
- **Session lifetimes**: every sign-in after the second factor ends in one place,
  `_complete_sign_in` in `vaf/api/auth_routes.py` (`/verify-2fa` and `/login` for an account
  without one), which mints the token with the lifetime of its door, opens the session, ends
  the session of the cookie the same browser carried before (every sign-in used to leave it
  live for its whole life) and sets the cookie. The first-run wizard's own session lives one
  hour: it is past the second factor before one exists, and the wizard's 2FA step replaces it.
  This departs from NIST 800-63B-4 at AAL2 (24 hours overall and one hour of inactivity, both
  SHOULD), and the reasons are written down as ASVS 7.1.1 asks: VAF answers only the LAN and
  VPNs, the second factor is always required, the token sits in an HttpOnly cookie no page
  script can read, every session is listed and can be ended, and a sign-in from a new address
  is announced. A week-long inactivity limit only means something when a session can outlive
  a day, which is why the default went from 24 hours to 30 days; a stored `24` is rewritten
  once (config format 3). An admin who wants a daily sign-in sets the key to `24`, and the
  inactivity limit then never applies. How long a session lives depends on where it is
  (`vaf/auth/sessions.py`, one rule `session_row_live` for the standing check, the session
  list and `/api/auth/me`):
  - a sign-in from another device: `local_network_jwt_expiry_hours` (720, 30 days) at most,
    and it ends after `local_network_session_idle_days` (7) without use. Use is any request
    that checks the token (its last use is written at most once an hour); a socket that only
    stays open does not count, its reconnect does.
  - the window on this machine: 30 days, no inactivity limit.
  - a token for programs: the 7, 30, 90 or 365 days it was made with, no inactivity limit.
- **Sessions end** (`vaf/auth/sessions.py`, the one owner of `user_sessions`): a logout ends
  the presented token's session, the account's own password change every other one (the
  device that made it stays signed in), an admin's password or 2FA reset, a deactivation and
  `vaf users reset-password` every session of the account. After a password change or an
  admin reset the account's open sockets close. Before, a logout only deleted the cookie and
  nothing ended a token (measured: the old token answered 200 after a logout and after a
  password change). See "Taking access away" in
  [USER_ISOLATION.md](../security/USER_ISOLATION.md).
- **2FA**: TOTP (RFC 6238), secrets encrypted at rest with AES-256-GCM; a code works once and wrong codes are held per account (Layer 4)
- **Session Tracking**: one row per completed sign-in, keyed by the token's hash (SHA-256), no plaintext tokens in DB; a token with no live row is refused. A store that cannot be asked keeps the token (the desktop default: failing closed would sign every device out on each database restart, and the refused cookie is deleted, so the sign-out would stick), except a token whose session this process ended: those hashes are remembered and refused without asking the store, so a token signed out here cannot ride on a store that went away afterwards. The sign-out route records the endings the terminal's reset wrote in its own process.
- **Cookies**: `vaf_token` cookie with `httponly`, `samesite=lax`, and `secure` flag (when TLS active). The cookie's `max-age` is always derived from the token's own `exp` claim at the single set point (`_cookie_max_age_for` in `auth_routes.py`), so the cookie can never outlive the JWT it carries. The login form's `remember_me` flag changes nothing: the lifetime is the token's (above), and a session ends earlier when it is signed out or idle. Do not reintroduce a hardcoded longer cookie lifetime: a present-but-expired cookie desyncs the server-side route gate from the bearer token and causes a login redirect loop (live incident 2026-07-22).

**2FA persistence after restart:** Your 2FA setup is stored in two places that must persist across restarts:
1. **Key store** (`<data_dir>/data_keys.enc` plus its master key, by default the `secure_store.kek` file in `~/.vaf` or `VAF_CONFIG_DIR`): the JWT secret that encrypts TOTP secrets lives in the data keyring, not in `config.json`, and both halves must be kept. If either is lost (new install, different user, a backup restored without the keyring), the server cannot decrypt existing 2FA data. See [ENCRYPTION_AT_REST.md](../security/ENCRYPTION_AT_REST.md) for what to back up.
2. **Database** (PostgreSQL, see `memory_db_url`): User accounts and 2FA state (`requires_2fa_setup`, encrypted `totp_secret`) live in the same DB as RAG memory. Each account row carries a seal, an HMAC under `account_seal_key` from the key store, over the columns that grant a login or authority (password hash, role, 2FA fields, active flag, scope, tool rights); a row changed in the database alone is refused for a login, a token and its rights, and recorded as `account_seal_broken` (see [USER_ISOLATION.md](../security/USER_ISOLATION.md#the-account-rows-are-sealed)). If the DB is recreated or the data is lost (e.g. Docker without a persistent volume) while the key store remains, the web setup stays closed, because the key store remembers that this installation had an admin: set the admin up again in a terminal on the machine with `vaf setup`.

**The first admin, from another device:** while no admin exists, `/api/auth/bootstrap` creates one for whoever asks, and every address the IP check admits may ask (on a VPS that includes the provider's private network and the Docker bridges; measured before: from `10.1.2.3`, `needs_setup: true`, then a bootstrap 200 with role admin). So a client that is not on this machine (`is_localhost` of the resolved client, in every mode) must send the first-run setup code, which only a terminal on the machine can show: `vaf setup code` mints it once into the data keyring (16 characters of Crockford's base32, 80 bits, shown as `XXXX-XXXX-XXXX-XXXX`) and prints it again until the admin exists; creating the admin deletes it. The wizard asks for it first (`setup_code_required` in `/needs-setup`) and checks it at `/api/auth/setup-code`; the wizard's Veyllo key test (`/api/auth/test-veyllo-key`) needs it from such a client too, since it tests any key against the provider for whoever asks while no admin exists. Both routes are in the address window, so a wrong code costs like a wrong password. The desktop window and `vaf setup` never need it. Implementation: `first_run_setup_code` / `setup_code_matches` in `vaf/auth/user_admin.py`.

**A forgotten admin password:** an admin resets the other accounts in Settings, but nobody resets the admin from a web page. In a terminal on the machine, `vaf users reset-password NAME` sets a new password and makes the second factor be set up again at the next login. It needs a person at the terminal (stdin a tty; it never runs from a script or the agent's shell) and a proof: the admin password, or, for the password that is gone, the recovery key from `VAF-BackThisUp.md` (`--recovery-key`, checked against the recovery file). `vaf users list` shows the accounts, their role, their 2FA state and whether their record still verifies. Each reset, and each reset an admin does in Settings, is the security event `password_changed`.

**Staying logged in across a DB restart:** validating `/me` (user + active session) queries PostgreSQL, but a backend/Docker restart leaves Postgres briefly unavailable (`the database system is starting up`). To avoid logging users out on that race, `/me` **retries** the DB for a few seconds and, if it is still not ready, **falls back to JWT-only auth** (the already-verified token) instead of returning 401, so a transient DB restart does not clear your session. Transient PG states (starting up / shutting down / in recovery / too many connections) are treated as retryable. See `auth_routes.py` `_me_user_from_token`.

If you see "2FA was reset (e.g. after config or restart)" when entering your code, the encryption key changed (the key store was lost or replaced, so a different JWT secret is in use). Use "Back to login", sign in again, and set up 2FA with the new QR code.

### Identity vs. Memory Scoping

- **Global Personality (Soul)**: The agent's identity (Name, Emoji) and behavioral rules (Soul) are defined by the **Administrator** and are global for all users. This ensures a consistent experience across the network.
- **Isolated Memory (RAG)**: While the personality is shared, the **RAG memory is strictly isolated per user**. Facts and history stored by a user are only accessible to them, preventing data leakage between connected devices. This isolation is **fail-closed**: an unresolved or empty user scope yields **no results** rather than searching across all users, so an unauthenticated network request returns nothing instead of leaking another user's memories. Only a genuine single-user/local request floors to the local-admin scope.

### Connection Tracking

The system actively tracks all connections (WebSocket and HTTP) to the VAF backend.
- **Real-time Monitoring**: The "Network Topology" map in Settings visualizes all active devices.
- **Pre-Auth Tracking**: Devices are detected and displayed as "Guest" or "Unauthenticated" immediately upon connection, ensuring visibility of unauthorized access attempts.

---

## Remote access over a VPN

VAF does not run a VPN server: that needs root, and the router (a Fritzbox has WireGuard
built in), Tailscale or the hosting's provisioning already do it well. What VAF does is
recognise the VPN, admit its devices and name its address.

**Detection.** `local_interfaces()` in `vaf/network/binding.py` lists this machine's
addresses through psutil: interfaces that are up, IPv4, inside a private range or the
shared address space `100.64.0.0/10`. A VPN interface is told apart by name - `wg*`, `wt*`
(NetBird), `tun*`/`tap*`, `utun*` (every VPN on macOS), `zt*` (ZeroTier), and the
Windows names "Tailscale", "WireGuard", "OpenVPN", "Wintun" - or by an address in
`100.64.0.0/10`. Bridges of containers and virtual machines (`docker*`, `br-*`, `veth*`,
`virbr*`, the WSL switch) are left out: nobody connects from them. A mesh VPN gives each
device one address and routes the rest of `100.64.0.0/10` to it, so its network is that
whole block; elsewhere the interface's mask is the network.

**A server with a public address.** `vaf server provision` turns "VPN only" on when it first sets up a machine that has a public IPv4 address (`binding.public_addresses()`): on a rented server the RFC 1918 ranges admitted by default are the hosting provider's network and the container bridges, not anybody's LAN. A re-run never changes it (it would lock a running server's users out); it warns and names `vaf server vpn-only on` instead, and `vaf server vpn-only off` names the provider's network when it admits the local networks again.

**Admission.** `inbound_policy()` is the one answer to "who may connect":

| Setting | Admitted |
|---|---|
| default | this machine, the local networks (RFC 1918) |
| `local_network_allowed_networks` | in addition, each private network an admin listed, e.g. `100.64.0.0/10` for Tailscale/NetBird |
| `local_network_vpn_only` | this machine and the networks of the detected VPN interfaces, plus the list above; the local networks are out, and with no VPN up nobody else gets in |

An entry is an address (taken as `/32`) or a network, and is taken only when it lies
inside RFC 1918 or `100.64.0.0/10` (`normalize_allowed_networks`). A public network,
`0.0.0.0/0`, loopback, IPv6 (the access port listens on IPv4 only) and anything
unreadable is refused with a reason, never widened into; `vaf doctor` lists refused
entries, and a "VPN only" with no VPN up and nothing listed.

**Who reads it.** The IP check (`is_allowed_ip`, every request and every WebSocket
handshake), the OS firewall (`firewall_sources()`, see Layer 1), and the shown access
addresses (`access_addresses()`: the settings, `vaf top`, `vaf server status`). The
access check takes a change at once; the running app tries to re-apply the firewall when
`local_network_allowed_networks` or `local_network_vpn_only` change (one password
dialog on a desktop with firewalld; ufw and iptables need passwordless sudo), with no
restart, where that app can gain root. The service the server installer sets up cannot
(Layer 1), so there the change reaches the OS firewall only through `sudo -v && vaf server provision`. After every
change `vaf server networks` and the web settings say which of these holds: the settings
read `firewall_after_save` from `GET /api/network/remote-access` (`automatic`, `provision`
or `manual`), the CLI reads `server_mode` and `local_network_firewall_enabled`. The
provision command is named on a server only, because it also switches server mode on and
replaces the database passwords; with the firewall step switched off, or a process that
cannot gain root on another install, the note says to bring the firewall's rules in line by
hand (open new ones, close removed ones). The CLI cannot measure the service's privileges
from the operator's shell, so on a server it says the service *usually* cannot change the
firewall. Certificates carry every LAN and
VPN address (see "How Auto-SSL Works").

Named boundaries: Windows names a WireGuard adapter after its tunnel file, so it reads as
a LAN - outside "VPN only" that changes nothing, in "VPN only" its network is listed by
hand. A WireGuard interface configured with a single address (`/32`, likewise OpenVPN in point-to-point
mode) names no network, so under "VPN only" none of its peers gets in; VAF does not guess a
wider one. `vaf server vpn-only on`, `vaf server status`, `vaf doctor` and the settings say so;
list the VPN's subnet the same way. A VPN that comes up while VAF runs is admitted at
once if its network is, but enters the certificate at the next start.

## TLS/SSL Encryption

VAF supports full TLS encryption for both HTTP (HTTPS) and WebSocket (WSS) traffic within the local network. It prevents eavesdropping, and it prevents a man in the middle once a device trusts the authority behind the certificate (it installed it, or its fingerprint was compared out of band; see [Eliminating Browser Warnings](#eliminating-browser-warnings)). A device that clicks a certificate warning away is encrypted to whoever answered.

### Quick Start (Automatic Certificates)

The simplest way to enable TLS:

1. Enable Local Network Hosting (`local_network_enabled=true`) in Settings or via `vaf server on`
2. Restart VAF

That's it. VAF automatically generates a local Certificate Authority (CA) and server certificate. No manual `openssl` commands needed. To serve a certificate of your own instead, see [Your Own Certificate](#your-own-certificate).

> **Important:** Network mode is TLS-only. If `local_network_enabled=true`, VAF automatically enforces `local_network_tls_enabled=true` when loading/saving config.

### How Auto-SSL Works

With TLS on and no own certificate configured, VAF's `ssl_utils` module:

1. **Creates a local CA** (`~/.vaf/ssl/ca.pem` + `ca-key.pem`)
   - RSA 2048-bit key, valid for 10 years
   - Restricted to private addresses and names when it is created ([Restricting the Authority](#restricting-the-authority))
   - Signs the server certificate, and only needs to be installed once per device

2. **Creates a server certificate** (`~/.vaf/ssl/server.pem` + `server-key.pem`)
   - RSA 2048-bit key, signed by the local CA, which `server.pem` carries after it
   - Valid for 1 year
   - Includes Subject Alternative Names (SANs) for:
     - `localhost` / `127.0.0.1` / `::1`
     - Every detected LAN and VPN address (e.g. `192.168.1.100`, `10.8.0.1`, `100.101.102.103`)
     - Machine hostname and FQDN; under a restricted CA only when they lie under `local`, `home.arpa`, `internal` or `lan` (see [Restricting the Authority](#restricting-the-authority))
     - `<host>.local`, the name mDNS gives the machine (both kinds of CA vouch for it)

3. **Keeps both in `~/.vaf/ssl/`**
   - On each start the server certificate is checked: under 30 days left, unusable to strict verifiers (see below) or missing a current address means a new one, from the same CA
   - `vaf server cert reset` issues a new one at once, also from the same CA, and the running VAF serves it without a restart
   - The CA is kept across these re-issues, so every device that installed it keeps trusting the machine; it is replaced only when it cannot be verified (see below), when its key does not belong to it, or when only one of its two files is there, each time at WARNING level
   - Both private keys are owner-only (0600) from their first byte: they are written to a file created owner-only and then renamed into place
   - `local_network_ssl_cert` / `local_network_ssl_key` are never written; they name your own certificate or nothing. A configuration from an older version that holds VAF's own paths there is read as VAF's own

#### Why a valid certificate can still be replaced

This applies to VAF's own files only; [your own certificate](#your-own-certificate) is never replaced.

Both certificates carry the key identifiers RFC 5280 requires: the CA names its own key,
and the server certificate names both itself and the key that signed it. Without them a
verifier cannot build the chain under strict checking, which Python turns ON by default
from 3.13 (`ssl.create_default_context()` sets `VERIFY_X509_STRICT`).

The practical effect of a certificate generated before this was fixed: `curl` and browsers
still connect, and every correctly written Python client fails with
`certificate verify failed: Missing Authority Key Identifier`. Both extensions are needed,
and neither alone helps - measured against `openssl verify -x509_strict`:

| CA has subject key id | Server has authority key id | Result |
|---|---|---|
| no | no | error 85, missing authority key identifier |
| yes | no | error 85 |
| no | yes | error 86, missing subject key identifier |
| yes | yes | OK |

So such a certificate is treated as invalid rather than as merely old, and both files are
reissued on the next start. **The new CA has a different fingerprint**, so any device that
installed the old `~/.vaf/ssl/ca.pem` in its trust store has to be given the new one. The
replacement is logged at warning level and says exactly that.

### Restricting the Authority

A device that installs VAF's CA trusts everything that CA signs. A CA without limits would
let a copy of its key sign a certificate for any name, a bank's included, and that device
would accept it. So a CA VAF creates carries critical name constraints: it may vouch for
the addresses the admission check serves (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
`100.64.0.0/10`, `127.0.0.0/8`, `::1`; the constants in `vaf/network/binding.py`) and for
the DNS name `localhost` and everything under `local`, `home.arpa`, `internal` and `lan`.
Both name types are listed, because RFC 5280 leaves a type without permitted names
unconstrained. This machine's own host name and FQDN are deliberately not among them: a name
constraint permits every name below it, and a host called `studio` (a public top-level
domain) or a server named after its domain (`example.com`) let the CA vouch for
`www.google.studio` or `www.google.example.com` (measured, OpenSSL and Python strict).

Measured under strict verification (`openssl verify -x509_strict` and a Python handshake with
`VERIFY_X509_STRICT`), with certificates signed by such a CA:

| Certificate for | Result |
|---|---|
| VAF's own names (`localhost`, `vafhost.lan`, 127.0.0.1, ::1, a LAN and a VPN address) | OK |
| `www.examplebank.com` | refused, "permitted subtree violation" |
| `www.google.studio` on a machine named `studio` | refused, "permitted subtree violation" |
| `203.0.113.9`, `2001:db8::1` | refused, "permitted subtree violation" |
| private names plus one public address | refused as a whole |

Because one name outside the constraints refuses the whole certificate, VAF's own server
certificate carries only the names inside them: a host name or FQDN outside (`mypc`,
`mypc.fritz.box`) is left out with a warning, and a device that opens VAF by it gets a
certificate warning. Open VAF by its address, as the access URL does. VAF's own certificate
also carries `<host>.local` (`mypc.local`), the name mDNS gives the machine, so a device that
resolves mDNS names can open VAF by that name. That needs both ends: the VAF machine
announces its name (Avahi on Linux: measured on a Linux desktop with Avahi and nss-mdns,
where `mypc.local` resolved; built into macOS; a Linux server without Avahi does not), and the
device resolves `.local` names (macOS, iOS and Linux desktops with nss-mdns do; current
Windows resolves them by its documentation, not measured here). The settings and
`vaf server cert show` list the names the certificate leaves out, and before a renewal
`vaf server ca renew`, the settings' "Restrict" dialog and `vaf doctor` list the names a new
authority would leave out. An authority created before VAF restricted them keeps carrying
every name, so nothing changes for devices until it is renewed.

**An existing CA is kept as it is**, so devices that installed it keep working.
`vaf server ca` and the settings show whether it is restricted, and `vaf doctor` reports an
unrestricted one (`network_ca_unrestricted`, low). `vaf server ca renew`, or "Restrict to
private addresses" in Settings, Local Network, Certificate, replaces it with a restricted
one: every device then installs the new authority, other machines that joined a room on
this VAF trust its new fingerprint (`vaf a2a trust ... --ca-fp <new>`), and the old one can be
removed from the devices. An own certificate stays served through a renewal.

Named boundaries:
- **Clients that enforce the constraints.** OpenSSL and Python (measured), Chrome on Linux
  from 112, Firefox, and Chrome on macOS (by their documentation). Not measured on Windows
  and Safari. A client that ignores them is no worse off than with an unrestricted CA.
- **The CA key is an unencrypted owner-only file.** The constraints limit what a copy of it
  can sign; they do not make it secret again.
- **`lan` is not a reserved name.** `local`, `home.arpa` and `internal` are set aside for
  private use; `lan`, which routers hand out (OpenWrt's default), is only a name no public
  registry has delegated. It is kept for the networks that use it.

### Certificate Lifecycle

```
Start (TLS enabled)
    |
    v
local_network_ssl_cert / _key name your own pair?
    |                         |
   Yes                        No (empty, or VAF's own paths)
    |                         |
    v                         v
Does it load (files there,  VAF's own certificate in ~/.vaf/ssl/
key without passphrase,     valid (>30 days), verifiable, names
key belongs to it)?         every current address?
    |            |              |                |
   Yes           No            Yes               No
    |            |              |                |
    v            v              v                v
Serve it,     ERROR line,     Serve it        Issue a new one from the
expired or    serve VAF's                     kept CA (a new restricted
not (warns    own for this                    CA only when there is none
from 30 days  run, config                     usable)
left)         untouched
    |
    v
A change later (vaf server cert set / reset, vaf server ca renew, Settings):
every running TLS listener loads the new pair, no restart
```

### Eliminating Browser Warnings

Install the authority behind the certificate on each device once: with VAF's own
certificate that is the local CA. Get it from Settings, Local Network, Certificate
("Download", `vaf-authority.pem`; the file `/api/a2a/ca.pem` serves) or as the file
`~/.vaf/ssl/ca.pem`.

**Compare the fingerprint before you trust it.** A file fetched over the connection you are
about to trust proves nothing on its own. For VAF's own certificate, compare the SHA-256
fingerprint the device shows for the authority with the one `vaf server ca` prints on this
machine, or the one the settings show on this machine. Never compare it with a page loaded
over the connection you are checking.

**Your own certificate** comes from another authority, so `vaf server ca` names the wrong one
for it. A certificate from a public authority (Let's Encrypt) needs nothing installed: devices
trust that authority already. One from an authority of your company or your own: install that
authority, and compare its fingerprint with what its operator gives you by a channel of its
own (`vaf server cert show` prints the authority of the served chain on this machine, which
tells you what VAF serves, not whether that authority is the right one).

**Reached by name, install first.** A browser that once received VAF's HSTS header for a
host name (a year for a DNS name set up for VAF) does not let a certificate warning for that
name be clicked past, so install the authority before you open VAF by name.

**Windows:**
```
1. Open ~/.vaf/ssl/ca.pem
2. Double-click -> "Install Certificate"
3. Store Location: "Local Machine"
4. Place in: "Trusted Root Certification Authorities"
5. Restart browser
```

**macOS:**
```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain ~/.vaf/ssl/ca.pem
```

**Linux (Debian/Ubuntu):**
```bash
sudo cp ~/.vaf/ssl/ca.pem /usr/local/share/ca-certificates/vaf-local-ca.crt
sudo update-ca-certificates
```

**Firefox** (all platforms):
Firefox uses its own certificate store. Go to `Settings -> Privacy & Security -> Certificates -> View Certificates -> Authorities -> Import` and select `~/.vaf/ssl/ca.pem`.

### Your Own Certificate

A certificate from a company authority, from Let's Encrypt or from an authority of your own:

```bash
vaf server cert set --cert fullchain.pem --key privkey.pem [--chain chain.pem] [--check]
```

or Settings, Local Network, Certificate, "Use your own certificate". The certificate file may
hold the chain and the key as well (one PEM bundle). It is checked first; `--check` stops
there and changes nothing.

| The check | Result |
|---|---|
| a part over 64 KiB or more than 8 certificates; no PEM certificate; no key | refused |
| a key with a passphrase (and PKCS#12, which is not read) | refused, with the conversion: `openssl pkey -in key.pem -out plain-key.pem`, or `openssl pkcs12 -in cert.p12 -noenc -out bundle.pem` |
| the key does not belong to the certificate (loaded into a throwaway server context, the check uvicorn itself makes) | refused |
| expired, or not valid yet | refused |
| an authority certificate, or an extended key usage without server authentication | refused |
| RSA under 2048 bits, a curve other than P-256 or P-384, Ed25519, Ed448 or DSA (browsers do not take them) | refused |
| a chain out of order (each certificate must be issued by the one after it) | refused |
| no subject alternative names (browsers ignore the common name) | refused |
| under 30 days left | warning |
| an admitted address or the host name it does not name | warning: devices that reach VAF there get a certificate warning |
| missing key identifiers | warning: strict clients, VAF's room peers among them, refuse it |
| the server certificate alone, not self-signed | warning: devices without its issuer fail, and a room peer would pin the certificate itself, so every renewal breaks its pin |
| the authority changes | warning: devices must trust the new one, and machines in rooms here run `vaf a2a trust ... --ca-fp <new>` (the CLI prints the line) |

Then it is served **at once, without a restart**: every running TLS listener (the HTTPS
port and the desktop's TLS port) loads the new pair; connections already open keep theirs.
The pair is loaded into a throwaway context first, so a pair that does not load never
reaches a live listener; a listener whose load fails keeps its previous pair. `vaf run
--web` loads a change made in its own Settings at once, and one made in a terminal at its
next start: it has no internal channel the terminal could ask. The exit code of
`vaf server cert set` and `reset`: 0 served now (or at the next start, when VAF is not
running), 1 refused and nothing changed, 3 changed but the running VAF still serves the
previous one (restart it).

- **Where it goes.** `~/.vaf/ssl/custom/<first 16 hex digits of its fingerprint>.pem` and
  `.key` (the key owner-only from creation, the folder 0700), and both config keys switch in
  one save. The files of the certificate it replaced are removed once nothing serves them.
- **Never replaced.** VAF serves it expired or not, and warns from 30 days before the end
  (the log, `vaf doctor`'s `network_certificate_expiring` and `network_certificate_expired`,
  `vaf server cert show`, `vaf server status`, the settings).
- **When it cannot load** (the file is gone, the key has a passphrase, the key does not
  belong to it), VAF serves its own certificate for that run and logs why at ERROR, without
  touching the configuration: fixing the file at that path is enough. `vaf doctor` reports
  `network_certificate_unusable`.
- **Back to VAF's own:** `vaf server cert reset` (or "Back to VAF's own certificate" in the
  settings) issues a fresh server certificate from the kept CA. Do not delete `~/.vaf/ssl/`
  for that: it replaces the CA every device trusts.
- **Only through the check.** The generic settings save (`PATCH /api/config`, the WebSocket
  `save_config`) cannot set the two keys. A signed-in admin gives the 2FA code for a change
  in the settings; a request without a token comes from this machine only and needs none,
  the same trust as the terminal and the configuration file.
- **The private key travels with an upload.** Upload it in the settings only from this
  machine or over a connection you already verified; otherwise use `vaf server cert set` on
  the machine.
- **Rooms on other machines** pin the authority of the served chain (its last certificate)
  and check that the certificate names the host the invitation carries
  ([A2A_PROTOCOL.md](../agents/A2A_PROTOCOL.md#trust-between-machines)): the pin alone would
  accept any certificate a public authority issued, for anybody. What still passes is any
  certificate from the pinned authority that names the host dialled: with a public authority
  that means control of the name (public authorities issue no private addresses); with a
  company authority, whoever it issues that private address to, and private addresses repeat
  across sites and VPNs, so give such a certificate a DNS name and reach VAF by it. The
  invitation names an address your certificate covers; when it covers none, a DNS name of it
  that is this machine (its host name, FQDN or `<host>.local` where the certificate covers
  it, a wildcard included, else one that resolves here to its LAN address or another admitted
  address, looked up for 2 s at most and remembered for a minute; never the apex beside a
  wildcard, which is usually the public website), which the other machine's network must
  resolve to this one. A resolver failure is held for 5 s, doubling while it repeats up to a
  minute, so after an outage the lane can return up to a minute after the resolver answers
  again; and an invitation made while another one's slow lookup still runs waits for it only
  0.1 s and gets no lane. In both cases the invitation says the names gave no answer: try
  again in about a minute, and if the lane is still missing then, check that one of the names
  resolves to this machine. The only name of a certificate is checked like any other: a
  certificate made for a website would send a peer, and its ticket, to that website. Where
  this machine uses another DNS than your devices, add the name to its hosts file; the
  devices must still resolve it to this machine. A certificate that names neither gets no
  lane for other machines, and the invitation says so and why. Switching to an own
  certificate, back, or renewing the CA changes the authority; those machines run
  `vaf a2a trust` again with the new fingerprint.

**Renewing automatically.** VAF runs no ACME client. A DNS-01 client (certbot with a DNS
plugin, acme.sh, lego) can renew a certificate for a name inside your network and run
`vaf server cert set --cert <fullchain> --key <privkey>` as its deploy hook; the exit code
says whether the running VAF serves it. The costs: the DNS provider's API credentials live
on the server, the name appears in the public certificate transparency logs, and the
certificates are short-lived, so the hook must run unattended.

### What TLS Protects

When TLS is active, the following changes take effect across the stack:

| Component | Without TLS | With TLS |
|-----------|-------------|----------|
| Backend API | `http://host:8001` | LAN: via proxy `https://<LAN-IP>:8443`; desktop: internal plain `http://127.0.0.1:8005` |
| WebSocket | `ws://host:8001/ws` | LAN: same-origin `wss://<LAN-IP>:8443/ws` (via proxy); desktop: plain `ws://127.0.0.1:8005/ws` |
| Auth Cookies | `httponly`, `samesite=lax` | `httponly`, `samesite=lax`, **`secure`** |
| CORS Origins | the Web UI on this machine only (see [CORS Configuration](#cors-configuration)) | Unchanged |
| Security Headers | Standard set | Standard set + **HSTS** (`max-age=31536000` for a name set up for VAF, `max-age=0` for `localhost` and the machine's own names) |
| Frontend Proxy | `http://127.0.0.1:8001` | `http://127.0.0.1:8005` (internal plain channel) |

The WebSocket transport differs by client. Any HTTPS page, and an HTTP page on port 80 or with no explicit port in its URL, dials its own origin, because the proxy that served it relays `/ws`; that is the only answer that survives a port forward or a NAT between the browser and the proxy, where the browser dials a different port than the one the proxy bound. Other pages ask `/api/network/ws-config`, which returns plain `ws://` + the internal `8005` channel for the desktop window, and `wss://` + the effective proxy port for a caller that came through the proxy (identified by the `X-Forwarded-Proto: https` header the proxy stamps). The internal `8005` channel is plain HTTP, always running while TLS is on, and exists so the Next.js proxy and the desktop reach the backend without trusting the certificate's authority.

### TLS Configuration Reference

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `local_network_tls_enabled` | `bool` | `false` | TLS flag. Enforced to `true` whenever `local_network_enabled=true` |
| `local_network_https_port` | `int` | `443` | HTTPS proxy listen port (falls back to 8443 automatically on any platform when 443 is privileged/unbindable) |
| `local_network_ssl_cert` | `string` | `""` | Your own certificate (PEM, may carry the chain); empty means VAF's own. Set by `vaf server cert set` or the settings; not writable through the generic settings save |
| `local_network_ssl_key` | `string` | `""` | Its private key (PEM, without a passphrase); empty means VAF's own |

Every listener VAF serves TLS on (the HTTPS port, the desktop's TLS port, `vaf run --web`) takes its TLS options from one place, `server_tls_options` in `vaf/network/ssl_utils.py`: TLS 1.2 with forward-secret AEAD suites only, and TLS 1.3 ([The HTTPS door holds](#the-https-door-holds)). Each registers there for the certificate reload (`register_tls_listener`), and takes the pair `ensure_ssl_certificates()` decides; no other module reads the two keys.

### File Locations

```
~/.vaf/ssl/
  ca.pem            # Local CA certificate (install on your devices for trust)
  ca-key.pem        # Local CA private key (0600 from creation)
  server.pem        # Server certificate + the CA after it
  server-key.pem    # Server private key (0600 from creation)
  custom/           # Your own certificate, when you set one (0700)
    <fp>.pem        #   the certificate and its chain
    <fp>.key        #   its private key (0600 from creation)
```

Implementation: `vaf/network/ssl_utils.py` (`served_certificate`, `ensure_ssl_certificates`, `inspect_certificate`, `install_certificate`, `reset_certificate`, `renew_authority`, `reload_certificate`, `served_authority`, `certificate_state`)

---

## CORS Configuration

CORS admits exactly one origin: the Web UI on this machine, `http://` on `localhost`, `127.0.0.1` or `[::1]` at the port the frontend really runs on. It is the only page that calls the backend cross-origin (its WebSocket, and `/api/version` while the Next.js server rebuilds after an update). Every other browser path is same-origin and needs no CORS: the desktop and a local browser go through the Next.js `/api` route, LAN browsers through the integrated HTTPS proxy.

`OwnOriginCORSMiddleware` (a `CORSMiddleware` subclass in `vaf/auth/middleware.py`) answers `is_allowed_origin` with `is_own_frontend_origin` from `vaf/network/binding.py`, the same function the [Origin Guard](#origin-guard-every-mode) uses. It keeps `allow_credentials=True`, `allow_methods=["*"]` and `allow_headers=["*"]` for that one origin.

This replaced one static regex that admitted every `localhost` and RFC 1918 origin with credentials, in every mode. A page from any of those addresses, open in a browser on the VAF machine, could read the owner's answers. LAN browsers never needed it: they reach the API same-origin through the proxy.

Implementation: `OwnOriginCORSMiddleware` in `vaf/auth/middleware.py`, registered in `vaf/core/web_server.py`

---

## Configuration

Network settings are managed via the Web UI (Settings -> Local Network).

For dedicated server/appliance deployments, you can hard-lock hosting mode in `~/.vaf/config.json`:

```json
{
  "local_network_force_enabled": true
}
```

With this lock enabled, attempts to disable hosting in the UI/API are ignored and `local_network_enabled` remains `true`.

### All Network Configuration Keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `local_network_enabled` | `bool` | `false` | Master toggle for LAN access |
| `local_network_force_enabled` | `bool` | `false` | Hard lock for server appliances. When `true`, hosting is always enforced (`local_network_enabled` is forced to `true` on load/save, even if UI/API tries to disable it). |
| `local_network_port` | `int` | `8001` | Backend API port |
| `local_network_port_frontend` | `int` | `3000` | Frontend port |
| `local_network_firewall_enabled` | `bool` | `true` | Auto-configure OS firewall rules |
| `local_network_jwt_secret` | `string` | `""` | JWT signing secret. It lives in the data keyring, not here: a legacy config value is adopted byte-identically on first use and the plaintext copy is then blanked, so `""` is the normal state. Never regenerate it - the TOTP encryption key is derived from it (`vaf/auth/crypto.py`), so a new secret invalidates every stored second factor. |
| `local_network_jwt_expiry_hours` | `int` | `720` | The longest a sign-in from another device lives, in hours (30 days). The window on this machine gets 30 days, a token for programs its own 7 to 365 days. A stored `24` (the old default) is rewritten to `720` once |
| `local_network_session_idle_days` | `int` | `7` | A sign-in from another device ends after this many days without use (1 to 365); not the window on this machine, not tokens for programs |
| `local_network_rate_limit_attempts` | `int` | `5` | Failed login attempts per address before blocking |
| `local_network_rate_limit_account_attempts` | `int` | `10` | Failed logins per account name from any address before new addresses are held (see Layer 4) |
| `local_network_rate_limit_window_minutes` | `int` | `15` | Rate limit sliding window |
| `local_network_tls_enabled` | `bool` | `false` | Enable HTTPS/WSS encryption |
| `local_network_https_port` | `int` | `443` | HTTPS proxy listen port (falls back to 8443 automatically on any platform when 443 is privileged/unbindable) |
| `local_network_ssl_cert` | `string` | `""` | Your own certificate (PEM); empty means VAF's own. Set by `vaf server cert set` or the settings ([Your Own Certificate](#your-own-certificate)) |
| `local_network_ssl_key` | `string` | `""` | Its private key (PEM); empty means VAF's own |
| `local_network_allowed_networks` | `list[str]` | `[]` | Private networks admitted besides the local ones (CIDR or address); public networks are refused. See [Remote access over a VPN](#remote-access-over-a-vpn) |
| `local_network_vpn_only` | `bool` | `false` | Admit the networks of the detected VPN interfaces instead of the local networks |

### Live Updates

Changes to network settings trigger an automatic, orchestrated restart of the frontend and backend services to apply new bindings (e.g., switching from `127.0.0.1` to `0.0.0.0`). Enabling or disabling Local Network flips several config keys in one save; VAF coalesces them into a single restart. Disabling Local Network actually **stops** the integrated HTTPS proxy (8443) and the internal 8005 channel, so LAN access truly closes. The permanent firewalld rule remains (harmless - nothing is listening on the port).

The backend's own checks need no restart at all: the address check, the sign-in check and the rate limit follow `local_network_enabled` on every request ([Middleware Execution Order](#middleware-execution-order)), and so does the HTTPS door's address check, so they are in place from the first request after the switch, before the restart has finished.

Who is admitted (`local_network_allowed_networks`, `local_network_vpn_only`) restarts nothing: the IP check reads it on every request, and the running app tries to re-apply the OS firewall for the new networks (`apply_lan_firewall` in `vaf/network/firewall.py`, also what the start runs) where it can gain root; the server's service cannot and logs once why instead. The key lists live once in `vaf/core/config.py` (`NETWORK_RESTART_KEYS`, `NETWORK_ADMISSION_KEYS`); the tray and its file poll read them.

When TLS is enabled, firewall setup uses the effective HTTPS access port (`local_network_https_port`, or `8443` when `443` is privileged on any platform) so LAN clients can reach the proxy entry point. On Linux, firewalld is preferred: it opens only that effective proxy port for the LAN subnet via a rich rule, elevating through pkexec (desktop GUI dialog) or `sudo -n` (headless); a process that cannot gain root (`elevation_unavailable()`: the no-new-privileges flag, or a user namespace without root, as in the server's systemd service) does not try and logs once why.
On Windows, creating firewall rules via `netsh advfirewall` requires elevated rights. If VAF is not started as Administrator, LAN access can fail even when hosting is enabled.
The integrated HTTPS proxy, like every TLS listener of VAF, accepts TLS 1.2 with forward-secret AEAD suites only, and TLS 1.3; a device from before about 2014 that knows only CBC or static-RSA suites cannot connect ([The HTTPS door holds](#the-https-door-holds)).
VAF's own TLS certificate carries every LAN and VPN address, plus the machine's hostname and FQDN as DNS SANs where its authority may vouch for them ([Restricting the Authority](#restricting-the-authority)), and is re-issued at start when an address is missing (a changed LAN IP, a VPN that came up), so the IP SANs stay aligned with the access addresses (a hostname change alone does not trigger re-issuance - the freshness check covers IP SANs only). `vaf server cert reset` re-issues it at once, without a restart. A certificate of your own is never re-issued or replaced ([Your Own Certificate](#your-own-certificate)).

---

## Architecture Overview

### File Structure

```
vaf/
  auth/
    middleware.py        # ForeignOriginGuard, OwnOriginCORSMiddleware, NetworkModeLayers, AuthMiddleware
    rate_limit.py        # RateLimitMiddleware (brute-force protection)
    crypto.py            # Argon2, JWT, AES-256-GCM for TOTP
    models.py            # SQLAlchemy models (LocalUser, UserSession)
    database.py          # Auth DB session (shared with memory DB)
    user_config.py       # Per-user config directories
  network/
    admission.py         # AdmissionGuard: refuses a client that is not admitted, HTTP and WebSocket alike
    binding.py           # LAN/VPN interfaces, who is admitted (inbound_policy), real client, foreign-origin decision, frontend port
    door.py              # The door's limits: head time and size, connections, TLS handshake, open files
    firewall.py          # OS firewall automation (Windows/macOS/Linux) for the admitted networks
    https_proxy.py       # Integrated HTTPS reverse proxy (/api, /ws -> 8005; rest -> 3000), streaming
    connection_tracker.py # Real-time connection monitoring
    ssl_utils.py         # The served certificate (yours or VAF's), the restricted local CA, the check, the live reload, the TLS policy of every listener
  api/
    auth_routes.py       # Login, 2FA, bootstrap and setup code, logout, own password change
    network_routes.py    # Access URL, connection list, remote access, certificate
  core/
    web_server.py        # FastAPI app, middleware stack, CORS, TLS server
    frontend_manager.py  # Next.js process management with TLS env vars
```

### Request Flow (Network Mode with TLS)

When TLS is enabled, the **integrated HTTPS proxy** is the single entry point **for LAN/remote clients**. The backend serves TLS on port 8001 and an internal HTTP-only channel on port 8005; the proxy talks to the frontend (3000) and to the internal channel (8005) so TLS is terminated only at the proxy. The local desktop window bypasses the proxy entirely: it loads `http://127.0.0.1:3000` directly, routes `/api` through the Next.js proxy to the plain `8005` channel, and connects its WebSocket to `ws://127.0.0.1:8005/ws`.

```
LAN/remote browser (https://<LAN-IP>, port 8443 or 443)
    |
    v
Integrated HTTPS proxy (0.0.0.0:8443 or 443)  [door limits; streaming relay over pooled httpx clients]
    |
    +-- AdmissionGuard: a client outside the admitted networks (with Local Network off: any
    |   client but this machine) gets 403, a browser the refusal page (WebSocket 4003) here
    |
    +-- /api, /api/*, /ws     -->  http://127.0.0.1:8005 (internal channel, same FastAPI app)
    +-- /sounds/*             -->  http://127.0.0.1:8005 (notification sounds from backend)
    +-- all other paths       -->  http://127.0.0.1:3000 (Next.js frontend)
    |
    v (for /api, /sounds, and /ws)
Uvicorn + FastAPI (127.0.0.1:8005)
    +-- SecurityHeaders, ForeignOriginGuard, NetworkModeLayers (AdmissionGuard, AuthMiddleware, RateLimitMiddleware), OwnOriginCORSMiddleware
    v
Route Handler (reads request.state.user for identity & scoping)
```

The proxy `/ws` relay dials the backend before it accepts the device, so a handshake the backend refuses fails at the device's handshake too ([The HTTPS door holds](#the-https-door-holds)). It connects to the backend with `max_size=None`, so it does **not** impose its own per-frame size cap on the backend leg. The effective bound is `WS_MAX_SIZE_BYTES` (200 MB, `vaf/core/log_helper.py`), which EVERY uvicorn the product starts now passes explicitly: `run_server`, the tray's own `start_uvicorn` (main port AND the internal 8005 channel this proxy relays into), and the proxy's front listener - a pin test walks all `uvicorn.Config` sites. For a long time only `run_server` set it, so the desktop/tray path and the LAN front door silently capped frames at uvicorn's 16 MB default: an upload above ~12 MB raw (base64 inflates by ~4/3) dropped the connection mid-transfer with nothing but the reconnect banner. Attachments are additionally gated at 100 MB per file on BOTH sides (client before encoding, server after decoding) with a named error instead of a dropped socket.

---

## API Reference

Every `/api/network/*` route except `/ws-config` answers an admin only (`require_admin`; the
tokenless local desktop is the local admin). The connection map lists every connected
device's address and user name, and the rest describes the machine's network; the settings
tab that reads them was admin-only while the routes were not.

### 1. Get Access URL
**GET** `/api/network/access-url`

Returns the URL other devices should use: the first admitted LAN address, else the first admitted VPN address (a server reached only over a VPN has no LAN). When TLS is enabled, the port matches the **effective** integrated HTTPS proxy port (443, or 8443 after the cross-platform fallback). The Web UI uses this for the "For other devices on LAN" row in Network settings.

**Response (TLS on, 443 unbindable → 8443 fallback):**
```json
{
  "host": "192.168.1.50",
  "port": 8443,
  "backend_port": 8001,
  "ports": { "access": 8443, "backend": 8001 },
  "url": "https://192.168.1.50:8443"
}
```

`backend_port` (and `ports.backend`) is informational - the FastAPI backend binds `127.0.0.1` and is not reachable from the LAN.

**Response (no LAN IP detected):** `{ "host": null, "port": 443, "backend_port": 8001, "ports": { "access": 443, "backend": 8001 }, "url": null }`

### 2. Get Active Connections
**GET** `/api/network/connections`

Returns a list of currently connected devices for the Network Topology map.

**Response:**
```json
[
  {
    "id": "ws_123456",
    "type": "websocket",
    "ip": "192.168.1.102",
    "device_type": "mobile",
    "username": "Guest (Connecting...)",
    "connected_at": 1700000000.0
  }
]
```

### 3. Get Network Status
**GET** `/api/network/status`

Real runtime state of LAN hosting: whether the integrated HTTPS proxy actually bound and on which port (after any 443->8443 fallback), the resulting LAN URL, and the last bind error if it failed. The Local Network status dot in the Web UI reads this.

**Response:**
```json
{
  "enabled": true,
  "tls": true,
  "host": "192.168.1.50",
  "configured_https_port": 443,
  "effective_https_port": 8443,
  "proxy_bound": true,
  "error": null,
  "url": "https://192.168.1.50:8443"
}
```

`effective_https_port` is the port the proxy actually bound; `proxy_bound`/`error` report whether binding succeeded.

### 4. Get WebSocket Config
**GET** `/api/network/ws-config`

Tells the caller which WebSocket transport to use; the answer differs per client so one frontend build works on the desktop and over the LAN. TLS off -> `{ "useWss": false, "port": 8001 }`; TLS on with `X-Forwarded-Proto: https` (a LAN client behind the proxy) -> `{ "useWss": true, "port": <effective proxy port> }`; TLS on without that header (the local desktop on `http://127.0.0.1:3000`) -> `{ "useWss": false, "port": 8005 }` (the internal plain channel, since QtWebEngine does not trust the authority of the proxy's certificate).

### 5. Remote access (VPN)
**GET** `/api/network/remote-access`

The detected LAN and VPN interfaces and who is admitted - what the "Remote access (VPN)"
section of the settings shows:

```json
{
  "enabled": true,
  "access_port": 8443,
  "interfaces": [
    {"name": "enp3s0", "ip": "192.168.2.10", "network": "192.168.2.0/24", "kind": "lan",
     "admitted": true, "url": "https://192.168.2.10:8443", "in_certificate": true, "single_address": false},
    {"name": "tailscale0", "ip": "100.101.102.103", "network": "100.64.0.0/10", "kind": "vpn",
     "admitted": false, "url": null, "in_certificate": true, "single_address": false}
  ],
  "allowed": [],
  "refused": [],
  "vpn_only": false,
  "vpn_networks": ["100.64.0.0/10"],
  "mesh_vpn_network": "100.64.0.0/10",
  "mesh_vpn_admitted": false,
  "your_address": "192.168.2.50",
  "firewall_after_save": "automatic",
  "public_addresses": []
}
```

`public_addresses` lists this machine's public IPv4 addresses as `{"name", "ip"}` (`binding.public_addresses()`, for example `[{"name": "eth0", "ip": "9.9.9.9"}]` on a rented server); the settings show a warning while it is not empty. The PUT answers the same state.

**PUT** `/api/network/remote-access` with `{"allowed": [...], "vpn_only": bool, "confirm": bool,
"base_allowed": [...], "base_vpn_only": bool}` replaces both settings, checked and written under the
config lock against the stored file, so no other setting is touched. `422` with
`{"code": "refused", "refused": [{"value", "reason"}]}` when an entry is not a private network
(reason codes of `REFUSAL_REASONS` in `vaf/network/binding.py`). `409` with
`{"code": "stale", "state"}` when `base_allowed`/`base_vpn_only` (the state the page loaded) no
longer match the stored settings: the body replaces the whole list, so a page opened before another
admin or the CLI removed a network would otherwise bring it back; the answer carries the current
state to redo the change on. `409` with `{"code": "lockout", "address"}` when the change would shut
out the address the request comes from; sent again with `"confirm": true` it is saved. The answer
is the new state. The CLI (`vaf server networks`) reads and writes the list under the same lock.

### 6. Certificate

The served certificate and VAF's authority, what Settings, Local Network, Certificate shows;
the terminal half is `vaf server cert` / `vaf server ca` ([Your Own Certificate](#your-own-certificate)).
Every route is admin-only; no answer carries key material.

| Method | Endpoint | Second factor | Description |
|--------|----------|---------------|-------------|
| GET | `/api/network/certificate` | - | `certificate_state()`: `tls`, `source` (`own`, `automatic`, `none`), `own_path`, `problem` (`{code, text}` when the configured own certificate does not load), `certificate` (subject, issuer, names, dates, `days_left`, key, `sha256`, the authority's subject and `sha256`, chain length), `authority`, `local_ca` (`path`, `fingerprint`, `not_after`, `restricted`, and for an unrestricted one `left_out_on_renewal`: this machine's names a new authority would leave out), `uncovered` (admitted addresses and the host name the certificate does not name), `mdns_name` (`<host>.local`), `step_up_required` |
| POST | `/api/network/certificate/check` | - | `{certificate, key?, chain?}` (PEM, each at most 64 KiB): the report (`ok`, `errors` and `warnings` as `{code, text, params}`, `facts`); nothing is written |
| PUT | `/api/network/certificate` | signed-in admin | `{certificate, key?, chain?, code?}`: install and reload. `422` `{code: "refused", report}` before the code is asked for, nothing written |
| DELETE | `/api/network/certificate` | signed-in admin | `{code?}`: back to VAF's own, freshly issued from the kept CA |
| POST | `/api/network/certificate/authority` | signed-in admin | `{confirm, code?}`: a new restricted CA; `409` `{code: "confirm"}` until `confirm` is set |
| POST | `/api/network/certificate/reload` | - | load the configured pair into every running TLS listener (what `vaf server cert set` asks after a change in the terminal): `{ok, listeners, refused, source}` |

A change answers `{result: {report, reload, old_authority, new_authority}, state}`. The second
factor is `security_routes.verify_admin_totp`, the one admin step-up check (403 wrong or
missing code, counted like every step-up); a request without a token comes from this machine
only and needs none. A change is the security event `tls_certificate_changed`.

### 7. Authentication Endpoints

| Method | Endpoint | Auth Required | Description |
|--------|----------|---------------|-------------|
| GET | `/api/auth/needs-setup` | No | Check if first admin must be created; `setup_code_required` says whether this browser needs the setup code |
| POST | `/api/auth/setup-code` | No | First run only: check the setup code `vaf setup code` shows (401 wrong, rate-limited; 403 once an admin exists) |
| POST | `/api/auth/bootstrap` | No (a browser not on this machine sends `setup_code`) | Create first admin account. Answers `{temp_token, user}`: the step token for the second factor, and the wizard's own one-hour session as the `vaf_token` cookie. A refusal is `{detail, code}` (`setup_code`, `password_too_short`, `password_common`, ...) |
| POST | `/api/auth/login` | No | Username/password login. With a second factor (every account VAF creates): `{requires_2fa, needs_2fa_setup, temp_token}`; without one: `{user}` and the cookie |
| POST | `/api/auth/setup-2fa` | Bearer (the step token) | Generate TOTP QR code |
| POST | `/api/auth/verify-2fa` | Temp Token | Verify the code; answers `{ok: true}` and sets the session's `vaf_token` cookie. No answer carries the session's token in JSON |
| POST | `/api/auth/logout` | Bearer/Cookie (tokenless from this machine) | End the session of each presented token, clear the auth cookie |
| GET | `/api/auth/me` | Bearer/Cookie | Get current user info; if both are sent, **Bearer is tried before** the `vaf_token` cookie so a stale cookie does not invalidate a valid header token. |
