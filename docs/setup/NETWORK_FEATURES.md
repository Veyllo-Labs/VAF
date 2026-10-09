# VAF Network Features & Security

VAF (Veyllo Agent Framework) includes robust networking capabilities designed to allow secure, local collaboration. This document details the architecture, security measures, and usage of these features.

**Integrated HTTPS proxy (no Nginx required):** When **Local Network** and **SSL/TLS** are enabled and certificate/key paths are set, VAF starts an integrated reverse proxy on `0.0.0.0:local_network_https_port` (default 443). On **any platform** (Linux/macOS/Windows), if 443 is privileged and cannot be bound by a non-root user, VAF automatically falls back to 8443. The effective bound port is surfaced via `/api/network/status` (`effective_https_port`), so the UI always shows the real port. The proxy is the single TLS entry point and routes requests as follows:

| Path | Target | Description |
|------|--------|-------------|
| `/ws` | `ws://127.0.0.1:8005/ws` | WebSocket relay (bidirectional) |
| `/api`, `/api/*` | `http://127.0.0.1:8005` | Backend API (all HTTP methods) |
| `/sounds/*` | `http://127.0.0.1:8005` | Notification sound files (GET/HEAD) |
| Everything else | `http://127.0.0.1:3000` | Next.js frontend |

The proxy uses **shared httpx clients with connection pooling** (max 50 connections, 20 keep-alive) for both frontend and backend targets, avoiding the overhead of opening a new TCP connection for every resource request. The desktop app window loads the frontend directly over plain HTTP at `http://127.0.0.1:3000` (it must not use the proxy URL, whose self-signed cert QtWebEngine rejects). The proxy URL (`https://<LAN-IP>:8443`, or `:443` when bindable) is for LAN/remote devices and works without an external proxy. Optional: [NGINX_REVERSE_PROXY.md](NGINX_REVERSE_PROXY.md) and `docs/setup/nginx-vaf-https.conf.example`.

## Security Model

Security is the primary design constraint for VAF's network features. The system employs a **Defense in Depth** strategy with five network layers, plus an origin guard that runs in every mode, including single-user:

### Origin Guard (every mode)

A tokenless request from this machine is the owner (the desktop window, internal IPC), and the `vaf_token` cookie is sent to every port of `localhost`, because `SameSite=Lax` keys on the site and every localhost port is one site. Neither trust can tell VAF's own Web UI from another web page open in the same browser. Only the browser's own marks can, so `ForeignOriginGuard` reads them before any identity is looked at and refuses what they name as foreign: HTTP with 403, a WebSocket handshake with close code 4003 before accept.

- **Not a web page, not judged.** A request with neither `Origin` nor `Sec-Fetch-Site` (CLI, sub-agent IPC, the tray, a script, `curl`) passes unchanged.
- **Host.** The `Host` the browser dialled must be `localhost` or an IP address. DNS rebinding needs a name the attacker controls, so a name is refused. `X-Forwarded-Host` is held to the same rule unless `X-Forwarded-Proto` is `https` (a TLS door cannot be rebound: the certificate would not match). The real `Host` is always checked, so forwarding headers a page adds itself change nothing.
- **Origin.** When present it must be the request's own origin, the origin a TLS proxy was dialled under (`https://` + `X-Forwarded-Host`: the integrated proxy, nginx), or the Web UI on this machine: plain `http` on `localhost`, `127.0.0.1` or `[::1]` at the port the frontend really runs on (`frontend_port()` in `vaf/network/binding.py`, the port file the frontend writes, else `local_network_port_frontend`). `null` is never own.
- **No Origin.** `Sec-Fetch-Site` `same-origin` or `none` (typed, bookmarked) passes, and so does a GET top-level navigation (`Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`), a link the person followed, which is how OAuth callbacks arrive. A cross-site image, script, frame or form from someone else's page is refused.
- **Every door carries the marks.** The HTTPS proxy forwards all client headers on HTTP and, on its WebSocket relay, the `Origin`, the dialled host (`X-Forwarded-Host`), `X-Forwarded-Proto: https` and `Sec-Fetch-Site`. The Next.js `/api` route forwards `origin`, `sec-fetch-site` and the browser's `host` as `x-forwarded-host` (it leaves `sec-fetch-mode` out: Node's fetch overwrites it with `cors`).
- **Frames the app sandboxes without `allow-same-origin`** (the HTML viewer) run under an opaque origin: a request they send with an `Origin` header (a fetch, a form) carries `Origin: null`, which the guard refuses; their other requests are judged by the rules above like any other page's. The guard cannot help markup that runs under the app's OWN origin; that is why no frame of the web UI gets `allow-scripts` together with `allow-same-origin` (`tests/test_web_untrusted_html_frames.py`, [WEB_UI.md](../web-ui/WEB_UI.md)).
- **Recorded** as `foreign_origin_blocked` in the security event log (`detail`: the reason `origin`, `site` or `host`, and the origin or host).

Named boundaries: a browser that sends no fetch metadata (Safari before 16.4) is judged on `Origin` and `Host` alone, so its plain cross-site GET passes. GET navigations from other pages pass, as they do for `SameSite=Lax` cookies, so a GET must not change state (an OAuth callback is protected by its `state`). The API serves no other web origin, even with a token. Another operating-system user on the same machine can reach the backend without a browser; that is the existing boundary of the tokenless localhost trust and not something an origin check can see.

Implementation: `ForeignOriginGuard` in `vaf/auth/middleware.py`, the decision in `foreign_request_reason` (`vaf/network/binding.py`), registered unconditionally in `vaf/core/web_server.py`. Pinned by `tests/test_foreign_origin_guard.py`.

### Layer 1: OS Firewall Automation

When "Local Network Hosting" is enabled, VAF automatically configures the OS firewall (Windows Firewall, macOS pf, or Linux).

On **Linux**, VAF **prefers firewalld** when it is running. It opens **only the effective proxy port** (e.g. 8443) for the **admitted networks** via rich rules (e.g. `source address="192.168.2.0/24" port="8443" protocol="tcp" accept`) - not a blanket world-open. The sources are the LAN subnets this machine sits on (never all of RFC 1918), the admitted VPN networks and the networks an admin added (see [Remote access over a VPN](#remote-access-over-a-vpn)); each gets one rule in the zone of every LAN and VPN interface and in the default zone, because a fresh WireGuard or Tailscale interface is usually in no zone and its packets are judged in the default one. The backend (8001) and frontend (3000) bind `127.0.0.1` and are deliberately **not** opened (they are unreachable from the LAN). Elevation uses **pkexec** in a desktop session (a native polkit password dialog appears when hosting is enabled) and `sudo -n` headless/server (non-interactive, fails fast, never hangs on a TTY). Firewall setup runs off the startup critical path (daemon thread) so it never blocks startup. Idempotence deliberately does NOT ask firewalld: an unprivileged `firewall-cmd --query-rich-rule` is a polkit `auth_admin` action on common distros (the firewalld `config.info` action; only the runtime `info` action is free), so a presence check would itself raise the root password dialog on every start. Even `firewall-cmd --state` is admin-gated there (measured: polkit action `FirewallD1.config`), so the "is firewalld running" probe asks `systemctl is-active` instead - the only firewall-cmd calls a normal start may make are the two free zone lookups. Instead, a local marker file (`~/.vaf/firewalld_lan.json`) remembers the set of rules this install set up: on a marker hit the start runs zero firewall-cmd config reads and can never prompt. Only a change (first run, another port, subnet or zone, an admin admitting or dropping a network) elevates - exactly once, with each check and both adds (runtime + permanent) inside the same elevated shell, together with the removal of the rules this install had set up and no longer wants. A removal asks first, so a rule that is already gone is fine; a removal that fails fails the whole elevation, the marker keeps the rule, and the next setup tries again (otherwise the permanent copy would return at the next boot while the marker had forgotten it). A marker from before the rule sets (one zone, one rule) still counts, so an update alone raises no dialog. The setup also runs **at most once per app process** (in TLS mode the same app runs two server lifespans; without that claim a cancelled dialog would chain straight into the twin's dialog). If the rule is ever removed behind VAF's back the marker goes stale and the port stays closed (the safe direction); delete the marker file or toggle Local Network to re-run the setup. `iptables`/`ufw` remain as the fallback when firewalld is not running.

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

### Layer 2: IP Validation Middleware

Every HTTP request passes through `IPValidationMiddleware` which validates the client IP against the networks `inbound_policy()` admits, at the application level: this machine, then by default the RFC 1918 ranges, or with "VPN only" the VPN networks instead, plus the private networks an admin added (which may lie outside RFC 1918, such as `100.64.0.0/10`). This acts as a second barrier if firewall rules are misconfigured or bypassed.

- Rejects any IP outside the admitted networks with HTTP 403 (recorded as `ip_blocked` with the device's address); under "VPN only" that includes the home network
- Judges the REAL client, not the socket peer: the integrated proxy relays every device over loopback, so the peer is `127.0.0.1` for all of them. The address comes from `connection_client_ip` in `vaf/network/binding.py`, which honors the proxy's `X-Forwarded-For` only when the peer is loopback. This check used to read the peer and therefore passed every relayed request, a public address on the proxy port included (measured: 200 through the proxy, 403 only on a direct connection), while the WebSocket handshake already refused it.
- Admitted are this machine, the local networks (RFC 1918) and the networks an admin added; with "VPN only" the networks of the detected VPN interfaces replace the local ones. WireGuard and OpenVPN clients (`10.x`) pass by default; mesh VPNs that hand out `100.64.0.0/10` (Tailscale, Headscale, NetBird) pass once that network is admitted. The decision is `inbound_policy()` in `vaf/network/binding.py`, read on every request, so a change applies without a restart - see [Remote access over a VPN](#remote-access-over-a-vpn).
- Active only in network mode (localhost mode skips this layer)
- Named boundary: the check sits in the backend, so it covers `/api` and `/ws`. Page loads go from the proxy straight to the Next.js frontend and do not pass it; a client outside the allowed networks still receives the login screen, but every API call that screen makes is refused. The pages carry no data, only the fact that VAF runs there.

`connection_client_ip` is the only place in `vaf/` that reads a connection's client address; the auth middleware, the rate limiter, the login routes, the WebSocket handshake, the OAuth callback and the security log entries all ask it. `tests/test_client_address_has_one_reader.py` refuses a direct read anywhere else (the proxy itself, which writes the header from the peer it sees, is the one exception).

Implementation: `vaf/auth/middleware.py` -> `IPValidationMiddleware`

### Layer 3: JWT Authentication Middleware

Network clients must authenticate via JWT tokens. The `AuthMiddleware` enforces this:

- **Token First, IP Second**: A presented access token is validated **before** any peer-IP branching. If a request carries a valid JWT (`Authorization: Bearer <token>` header or `vaf_token` cookie), the authenticated user's identity and scope are applied regardless of the source IP. This matters when a LAN user is proxied over loopback (the request arrives from `127.0.0.1` but belongs to a remote user): they get **their own** scope, not the local admin's.
- **Localhost Bypass (tokenless only)**: A **tokenless** request from `127.0.0.1` is allowed without authentication (internal IPC and single-user desktop mode). A web page that is not VAF's own never reaches this bypass: the [Origin Guard](#origin-guard-every-mode) refuses it first. This bypass applies only when no token is presented. A present-but-invalid token rejects a network client with HTTP 401, while a localhost client with an invalid token falls through to the tokenless localhost path. VAF's OWN outbound fetches are loopback clients too: the agent's web tools, remote MCP servers and WebDAV used to reach this bypass with any URL a page, a mail or the model chose (measured: the contacts, the account list and the configuration). They now go through the destination guard (`vaf/network/egress.py`, see [SANDBOXING.md](../security/SANDBOXING.md)), which never connects to this machine. NAMED BOUNDARY: the receiving side still trusts a tokenless loopback request; a shell command the agent runs with the person's grant (`curl` through `host_bash`) is not covered by the guard, and replacing the bypass with a per-start IPC token is a separate change.
- **Network Clients**: Must present a valid JWT via `Authorization: Bearer <token>` header or `vaf_token` cookie.
- **Auth-Exempt Paths**: Login, bootstrap, and static asset endpoints are accessible without a token.
- **2FA Enforcement**: If `local_network_require_2fa` is enabled, tokens from users who haven't completed 2FA setup are rejected with HTTP 403.
- **WebSocket token in the URL, redacted from logs**: a WebSocket handshake cannot carry an `Authorization` header, so the `/ws` client passes its JWT in the query string (`/ws?token=<jwt>`). uvicorn's access log would otherwise print that live token in full (terminal and `tray_debug`); a redaction filter (`RedactTokenFilter` / `redacted_uvicorn_log_config` in `vaf/core/log_helper.py`) masks it only if EVERY uvicorn the process starts passes it: the uvicorn loggers are process-wide, and a Config built with uvicorn's defaults re-runs `dictConfig` and strips the filter from all servers, depending on which starts last (the HTTPS proxy did, and the desktop's live token was measured in `tray_debug.log`). The tray's main port and internal 8005 channel, the HTTPS proxy and `run_server` all pass it, pinned by `tests/test_ws_frame_size_pin.py`. The filter masks it to `token=***` (and the same for `access_token` / `api_key` / `password`). The URL still authenticates; only the log line is masked.
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
- Checks include weak network posture flags (TLS/firewall/login/2FA), a channel whose Inbound is open to new senders, and channel-enabled-without-pairing states.
- Output is intentionally non-secret and safe to share in internal troubleshooting.

### Layer 4: Rate Limiting

The `RateLimitMiddleware` protects login endpoints against brute-force attacks:

- Tracks failed login attempts per IP address
- Blocks IPs after exceeding the threshold (default: 5 attempts)
- Sliding time window (default: 15 minutes)
- Applies to `/api/auth/login`, `/api/auth/bootstrap`, `/api/auth/verify-2fa`, and `/api/email/accounts/test` (the email credential-test endpoint shares the same per-IP limiter, so failed mailbox-login tests count toward the block)
- Returns HTTP 429 with `Retry-After` header when blocked
- Automatically clears failure count on successful login

Configuration:
| Key | Default | Description |
|-----|---------|-------------|
| `local_network_rate_limit_attempts` | `5` | Max failed attempts before blocking |
| `local_network_rate_limit_window_minutes` | `15` | Sliding window in minutes |

Implementation: `vaf/auth/rate_limit.py` -> `RateLimitMiddleware`

### Layer 5: Security Headers

All HTTP responses include security headers to protect against common web attacks:

| Header | Value | Purpose |
|--------|-------|---------|
| `X-Content-Type-Options` | `nosniff` | Prevents MIME-type sniffing |
| `X-Frame-Options` | `DENY` | Prevents clickjacking via iframes |
| `X-XSS-Protection` | `1; mode=block` | Legacy XSS filter |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Limits referrer leakage |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` | Disables browser APIs |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | HSTS (only when TLS active) |

Implementation: `_SecurityHeadersMiddleware` in `vaf/core/web_server.py`

### Middleware Execution Order

Requests pass through middleware from outermost to innermost. Starlette puts the middleware added LAST outermost, so this is the reverse of the `add_middleware` order in `vaf/core/web_server.py` (measured on `app.user_middleware`):

```
Request -> SecurityHeaders -> ForeignOriginGuard -> AuthMiddleware -> IPValidationMiddleware -> RateLimitMiddleware -> OwnOriginCORSMiddleware -> Route Handler
```

`AuthMiddleware`, `IPValidationMiddleware` and `RateLimitMiddleware` are registered only in network mode; the guard and CORS always. The guard sits outside CORS, so a preflight from a foreign page is refused before CORS could answer it.

### Security Event Log

Rejections from the layers above are recorded in an always-on security event log (independent of `debug_logs_enabled`; the writer never raises and never slows the request path):

- **Recorded kinds** (`vaf/core/security_events.py`): `ip_blocked` (Layer 2 403), `unauthenticated_blocked` and `token_rejected` (Layer 3 401s), `login_failed` and `twofa_failed` (failed login/2FA attempts), `ws_rejected` (rejected WebSocket handshakes), and `foreign_origin_blocked` (the origin guard, every mode). The messenger pairing kinds and the rest of the registry are listed in [SECURITY_DASHBOARD.md](../security/SECURITY_DASHBOARD.md); a messenger sender the agent refused to answer is channel traffic, recorded in that channel's inbound log, not here.
- **Sinks**: each event is appended to `security_events_<date>.jsonl` (structured) and mirrored human-readably to `security_<date>.log` (the "security" domain in the Logs file rail).
- **Throttle**: a per-source throttle (kind + ip + username + channel, 5s) prevents floods without letting distinct sources swallow each other's events.
- **Never logged**: passwords, 2FA codes, or tokens - only the event kind, source, and a short detail.
- **Visibility**: admin-only via `GET /api/security/events` (`vaf/api/security_routes.py`), surfaced on the Logs Overview dashboard.

### Authentication Details

- **Password Hashing**: Argon2id (time_cost=2, memory_cost=64MB)
- **JWT Tokens**: HS256, configurable expiry (default 24h), refresh tokens (7 days). A valid
  signature is not enough: the HTTP middleware and the WebSocket handshake also check that the
  token's account still exists, is active and holds the token's role (cached a few seconds,
  cleared by the admin routes), so a deactivation, a deletion or a demotion ends the token at
  once instead of at its expiry. See "Taking access away" in
  [USER_ISOLATION.md](../security/USER_ISOLATION.md).
- **2FA**: TOTP (RFC 6238), secrets encrypted at rest with AES-256-GCM
- **Session Tracking**: Token hashes (SHA-256) stored in PostgreSQL, no plaintext tokens in DB
- **Cookies**: `vaf_token` cookie with `httponly`, `samesite=lax`, and `secure` flag (when TLS active). The cookie's `max-age` is always derived from the token's own `exp` claim at the single set point (`_cookie_max_age_for` in `auth_routes.py`), so the cookie can never outlive the JWT it carries. The login form's `remember_me` flag does NOT extend the session - a longer session requires raising `local_network_jwt_expiry_hours` or wiring the existing `/api/auth/refresh` flow into the frontend. Do not reintroduce a hardcoded longer cookie lifetime: a present-but-expired cookie desyncs the server-side route gate from the bearer token and causes a login redirect loop (live incident 2026-07-22).

**2FA persistence after restart:** Your 2FA setup is stored in two places that must persist across restarts:
1. **Key store** (`<data_dir>/data_keys.enc` plus its master key, by default the `secure_store.kek` file in `~/.vaf` or `VAF_CONFIG_DIR`): the JWT secret that encrypts TOTP secrets lives in the data keyring, not in `config.json`, and both halves must be kept. If either is lost (new install, different user, a backup restored without the keyring), the server cannot decrypt existing 2FA data. See [ENCRYPTION_AT_REST.md](../security/ENCRYPTION_AT_REST.md) for what to back up.
2. **Database** (PostgreSQL, see `memory_db_url`): User accounts and 2FA state (`requires_2fa_setup`, encrypted `totp_secret`) live in the same DB as RAG memory. If the DB is recreated or the data is lost (e.g. Docker without a persistent volume), users will be asked to set up 2FA again (new QR code) after the next login.

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
access check takes a change at once; the firewall is re-applied by the running app when
`local_network_allowed_networks` or `local_network_vpn_only` change (one password
dialog on a desktop with firewalld), with no restart. Certificates carry every LAN and
VPN address (see "How Auto-SSL Works").

Named boundaries: Windows names a WireGuard adapter after its tunnel file, so it reads as
a LAN - outside "VPN only" that changes nothing, in "VPN only" its network is listed by
hand. A WireGuard interface configured with a single address (`/32`, likewise OpenVPN in point-to-point
mode) names no network, so under "VPN only" none of its peers gets in; VAF does not guess a
wider one. `vaf server vpn-only on`, `vaf server status`, `vaf doctor` and the settings say so;
list the VPN's subnet the same way. A VPN that comes up while VAF runs is admitted at
once if its network is, but enters the certificate at the next start.

## TLS/SSL Encryption

VAF supports full TLS encryption for both HTTP (HTTPS) and WebSocket (WSS) traffic within the local network. This prevents eavesdropping and man-in-the-middle attacks even on shared LANs.

### Quick Start (Automatic Certificates)

The simplest way to enable TLS:

1. Enable Local Network Hosting (`local_network_enabled=true`) in Settings or via `vaf server on`
2. Restart VAF

That's it. VAF automatically generates a local Certificate Authority (CA) and server certificate. No manual `openssl` commands needed.

> **Important:** Network mode is TLS-only. If `local_network_enabled=true`, VAF automatically enforces `local_network_tls_enabled=true` when loading/saving config.

### How Auto-SSL Works

When TLS is enabled and no valid certificate is configured, VAF's `ssl_utils` module automatically:

1. **Creates a local CA** (`~/.vaf/ssl/ca.pem` + `ca-key.pem`)
   - RSA 2048-bit key
   - Valid for 10 years
   - Used to sign server certificates
   - Only needs to be installed once in the browser/OS for trust

2. **Creates a server certificate** (`~/.vaf/ssl/server.pem` + `server-key.pem`)
   - RSA 2048-bit key, signed by the local CA
   - Valid for 1 year
   - Includes Subject Alternative Names (SANs) for:
     - `localhost` / `127.0.0.1` / `::1`
     - Every detected LAN and VPN address (e.g. `192.168.1.100`, `10.8.0.1`, `100.101.102.103`)
     - Machine hostname and FQDN

3. **Persists certificates** in `~/.vaf/ssl/`
   - Certificates are generated once and reused across restarts
   - On each startup, the server checks if the certificate has at least 30 days remaining
   - If expired or expiring soon, only the server certificate is regenerated (CA stays the same)
   - A certificate is also replaced when it cannot be VERIFIED, not only when it expires
     (see below) - and that is the one case in which the CA itself is replaced too
   - Config paths (`local_network_ssl_cert`, `local_network_ssl_key`) are updated automatically

#### Why a valid certificate can still be replaced

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

### Certificate Lifecycle

```
First Start (TLS enabled)
    |
    v
Certificates exist in ~/.vaf/ssl/?
    |                    |
    No                  Yes
    |                    |
    v                    v
Generate CA         Certificate valid, verifiable (>30 days)?
Generate Server         |              |
    |                  Yes             No
    |                   |              |
    v                   v              v
Store in              Reuse        Regenerate server cert
~/.vaf/ssl/                        (keep CA unless it is
                                    itself unverifiable)
    |
    v
Update config paths
    |
    v
Start Uvicorn with SSL
```

### Eliminating Browser Warnings

Self-signed certificates will show a browser warning. To eliminate this, install the CA certificate as a trusted root:

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

### Custom Certificates

If you prefer to use your own certificates (e.g. from a corporate CA or Let's Encrypt):

```json
{
  "local_network_tls_enabled": true,
  "local_network_ssl_cert": "/path/to/your/cert.pem",
  "local_network_ssl_key": "/path/to/your/key.pem"
}
```

When custom paths are configured and the files exist, VAF uses them directly without auto-generating anything.

### What TLS Protects

When TLS is active, the following changes take effect across the stack:

| Component | Without TLS | With TLS |
|-----------|-------------|----------|
| Backend API | `http://host:8001` | LAN: via proxy `https://<LAN-IP>:8443`; desktop: internal plain `http://127.0.0.1:8005` |
| WebSocket | `ws://host:8001/ws` | LAN: same-origin `wss://<LAN-IP>:8443/ws` (via proxy); desktop: plain `ws://127.0.0.1:8005/ws` |
| Auth Cookies | `httponly`, `samesite=lax` | `httponly`, `samesite=lax`, **`secure`** |
| CORS Origins | the Web UI on this machine only (see [CORS Configuration](#cors-configuration)) | Unchanged |
| Security Headers | Standard set | Standard set + **HSTS** (`max-age=31536000`) |
| Frontend Proxy | `http://127.0.0.1:8001` | `http://127.0.0.1:8005` (internal plain channel) |

The WebSocket transport differs by client: `/api/network/ws-config` returns `wss://` + the effective proxy port for LAN clients (identified by the `X-Forwarded-Proto: https` header the proxy stamps), and plain `ws://` + the internal `8005` channel for the desktop window. The internal `8005` channel is plain HTTP, always running while TLS is on, and exists so the Next.js proxy and the desktop reach the backend without the self-signed cert.

### TLS Configuration Reference

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `local_network_tls_enabled` | `bool` | `false` | TLS flag. Enforced to `true` whenever `local_network_enabled=true` |
| `local_network_https_port` | `int` | `443` | HTTPS proxy listen port (falls back to 8443 automatically on any platform when 443 is privileged/unbindable) |
| `local_network_ssl_cert` | `string` | `""` | Path to PEM certificate (auto-populated if empty) |
| `local_network_ssl_key` | `string` | `""` | Path to PEM private key (auto-populated if empty) |

### File Locations

```
~/.vaf/ssl/
  ca.pem            # Local CA certificate (install in browser for trust)
  ca-key.pem        # Local CA private key (chmod 600)
  server.pem        # Server certificate + CA chain
  server-key.pem    # Server private key (chmod 600)
```

Implementation: `vaf/network/ssl_utils.py`

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
| `local_network_require_2fa` | `bool` | `true` | Enforce TOTP 2FA for network users |
| `local_network_jwt_secret` | `string` | `""` | JWT signing secret. It lives in the data keyring, not here: a legacy config value is adopted byte-identically on first use and the plaintext copy is then blanked, so `""` is the normal state. Never regenerate it - the TOTP encryption key is derived from it (`vaf/auth/crypto.py`), so a new secret invalidates every stored second factor. |
| `local_network_jwt_expiry_hours` | `int` | `24` | Access token TTL in hours |
| `local_network_rate_limit_attempts` | `int` | `5` | Failed login attempts before blocking |
| `local_network_rate_limit_window_minutes` | `int` | `15` | Rate limit sliding window |
| `local_network_tls_enabled` | `bool` | `false` | Enable HTTPS/WSS encryption |
| `local_network_https_port` | `int` | `443` | HTTPS proxy listen port (falls back to 8443 automatically on any platform when 443 is privileged/unbindable) |
| `local_network_ssl_cert` | `string` | `""` | PEM certificate path (auto-populated) |
| `local_network_ssl_key` | `string` | `""` | PEM private key path (auto-populated) |
| `local_network_allowed_networks` | `list[str]` | `[]` | Private networks admitted besides the local ones (CIDR or address); public networks are refused. See [Remote access over a VPN](#remote-access-over-a-vpn) |
| `local_network_vpn_only` | `bool` | `false` | Admit the networks of the detected VPN interfaces instead of the local networks |

### Live Updates

Changes to network settings trigger an automatic, orchestrated restart of the frontend and backend services to apply new bindings (e.g., switching from `127.0.0.1` to `0.0.0.0`). Enabling or disabling Local Network flips several config keys in one save; VAF coalesces them into a single restart. Disabling Local Network actually **stops** the integrated HTTPS proxy (8443) and the internal 8005 channel, so LAN access truly closes. The permanent firewalld rule remains (harmless - nothing is listening on the port).

Who is admitted (`local_network_allowed_networks`, `local_network_vpn_only`) restarts nothing: the IP check reads it on every request, and the running app re-applies the OS firewall for the new networks (`apply_lan_firewall` in `vaf/network/firewall.py`, also what the start runs). The key lists live once in `vaf/core/config.py` (`NETWORK_RESTART_KEYS`, `NETWORK_ADMISSION_KEYS`); the tray and its file poll read them.

When TLS is enabled, firewall setup uses the effective HTTPS access port (`local_network_https_port`, or `8443` when `443` is privileged on any platform) so LAN clients can reach the proxy entry point. On Linux, firewalld is preferred: it opens only that effective proxy port for the LAN subnet via a rich rule, elevating through pkexec (desktop GUI dialog) or `sudo -n` (headless).
On Windows, creating firewall rules via `netsh advfirewall` requires elevated rights. If VAF is not started as Administrator, LAN access can fail even when hosting is enabled.
The integrated HTTPS proxy is configured for broad client compatibility (`TLS 1.2+`) so older LAN devices do not fail with empty-response errors during TLS negotiation.
Auto-generated TLS certificates carry the machine's hostname and FQDN as DNS SANs plus every LAN and VPN address, and are re-generated at start when an address is missing (a changed LAN IP, a VPN that came up), so the IP SANs stay aligned with the access addresses (a hostname change alone does not trigger re-issuance - the freshness check covers IP SANs only).

---

## Architecture Overview

### File Structure

```
vaf/
  auth/
    middleware.py        # ForeignOriginGuard, OwnOriginCORSMiddleware, AuthMiddleware, IPValidationMiddleware
    rate_limit.py        # RateLimitMiddleware (brute-force protection)
    crypto.py            # Argon2, JWT, AES-256-GCM for TOTP
    models.py            # SQLAlchemy models (LocalUser, UserSession)
    database.py          # Auth DB session (shared with memory DB)
    user_config.py       # Per-user config directories
  network/
    binding.py           # LAN/VPN interfaces, who is admitted (inbound_policy), real client, foreign-origin decision, frontend port
    firewall.py          # OS firewall automation (Windows/macOS/Linux) for the admitted networks
    https_proxy.py       # Integrated HTTPS reverse proxy (/api, /ws -> 8005; rest -> 3000)
    connection_tracker.py # Real-time connection monitoring
    ssl_utils.py         # Auto-SSL certificate generation
  api/
    auth_routes.py       # Login, 2FA, bootstrap, token refresh
    network_routes.py    # Access URL, connection list
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
Integrated HTTPS proxy (0.0.0.0:8443 or 443)  [connection-pooled httpx clients]
    |
    +-- /api, /api/*, /ws     -->  http://127.0.0.1:8005 (internal channel, same FastAPI app)
    +-- /sounds/*             -->  http://127.0.0.1:8005 (notification sounds from backend)
    +-- all other paths       -->  http://127.0.0.1:3000 (Next.js frontend)
    |
    v (for /api, /sounds, and /ws)
Uvicorn + FastAPI (127.0.0.1:8005)
    +-- SecurityHeaders, ForeignOriginGuard, AuthMiddleware, IPValidationMiddleware, RateLimitMiddleware, OwnOriginCORSMiddleware
    v
Route Handler (reads request.state.user for identity & scoping)
```

The proxy `/ws` relay connects to the backend with `max_size=None`, so it does **not** impose its own per-frame size cap on the backend leg. The effective bound is `WS_MAX_SIZE_BYTES` (200 MB, `vaf/core/log_helper.py`), which EVERY uvicorn the product starts now passes explicitly: `run_server`, the tray's own `start_uvicorn` (main port AND the internal 8005 channel this proxy relays into), and the proxy's front listener - a pin test walks all `uvicorn.Config` sites. For a long time only `run_server` set it, so the desktop/tray path and the LAN front door silently capped frames at uvicorn's 16 MB default: an upload above ~12 MB raw (base64 inflates by ~4/3) dropped the connection mid-transfer with nothing but the reconnect banner. Attachments are additionally gated at 100 MB per file on BOTH sides (client before encoding, server after decoding) with a named error instead of a dropped socket.

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

Tells the caller which WebSocket transport to use; the answer differs per client so one frontend build works on the desktop and over the LAN. TLS off -> `{ "useWss": false, "port": 8001 }`; TLS on with `X-Forwarded-Proto: https` (a LAN client behind the proxy) -> `{ "useWss": true, "port": <effective proxy port> }`; TLS on without that header (the local desktop on `http://127.0.0.1:3000`) -> `{ "useWss": false, "port": 8005 }` (the internal plain channel, since QtWebEngine rejects the proxy's self-signed cert).

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
  "your_address": "192.168.2.50"
}
```

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

### 6. Authentication Endpoints

| Method | Endpoint | Auth Required | Description |
|--------|----------|---------------|-------------|
| GET | `/api/auth/needs-setup` | No | Check if first admin must be created |
| POST | `/api/auth/bootstrap` | No | Create first admin account |
| POST | `/api/auth/login` | No | Username/password login |
| POST | `/api/auth/setup-2fa` | Bearer | Generate TOTP QR code |
| POST | `/api/auth/verify-2fa` | Temp Token | Verify TOTP code, get full token |
| POST | `/api/auth/refresh` | Refresh Token | Exchange refresh token for new access token |
| POST | `/api/auth/logout` | No | Clear auth cookie |
| GET | `/api/auth/me` | Bearer/Cookie | Get current user info; if both are sent, **Bearer is tried before** the `vaf_token` cookie so a stale cookie does not invalidate a valid header token. |
