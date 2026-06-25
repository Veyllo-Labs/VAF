# VAF Network Features & Security

VAF (Veyllo Agent Framework) includes robust networking capabilities designed to allow secure, local collaboration. This document details the architecture, security measures, and usage of these features.

**Integrated HTTPS proxy (no Nginx required):** When **Local Network** and **SSL/TLS** are enabled and certificate/key paths are set, VAF starts an integrated reverse proxy on `0.0.0.0:local_network_https_port` (default 443). On **any platform** (Linux/macOS/Windows), if 443 is privileged and cannot be bound by a non-root user, VAF automatically falls back to 8443. The effective bound port is surfaced via `/api/network/status` (`effective_https_port`), so the UI always shows the real port. The proxy is the single TLS entry point and routes requests as follows:

| Path | Target | Description |
|------|--------|-------------|
| `/ws` | `ws://127.0.0.1:8005/ws` | WebSocket relay (bidirectional) |
| `/api`, `/api/*` | `http://127.0.0.1:8005` | Backend API (all HTTP methods) |
| `/sounds/*` | `http://127.0.0.1:8005` | Notification sound files (GET/HEAD) |
| Everything else | `http://127.0.0.1:3000` | Next.js frontend |

The proxy uses **shared httpx clients with connection pooling** (max 50 connections, 20 keep-alive) for both frontend and backend targets, avoiding the overhead of opening a new TCP connection for every resource request. The desktop app window loads the frontend directly over plain HTTP at `http://127.0.0.1:3000` (it must not use the proxy URL, whose self-signed cert QtWebEngine rejects). The proxy URL (`https://<LAN-IP>:8443`, or `:443` when bindable) is for LAN/remote devices and works without an external proxy. Optional: [NGINX_REVERSE_PROXY.md](NGINX_REVERSE_PROXY.md) and `docs/nginx-vaf-https.conf.example`.

## Security Model

Security is the primary design constraint for VAF's network features. The system employs a **Defense in Depth** strategy with five layers:

### Layer 1: OS Firewall Automation

When "Local Network Hosting" is enabled, VAF automatically configures the OS firewall (Windows Firewall, macOS pf, or Linux).

On **Linux**, VAF **prefers firewalld** when it is running. It opens **only the effective proxy port** (e.g. 8443) for the **LAN subnet** via a rich rule (e.g. `source address="192.168.2.0/24" port="8443" protocol="tcp" accept`) in the interface's actual zone — not a blanket world-open. The backend (8001) and frontend (3000) bind `127.0.0.1` and are deliberately **not** opened (they are unreachable from the LAN). Elevation uses **pkexec** in a desktop session (a native polkit password dialog appears when hosting is enabled) and `sudo -n` headless/server (non-interactive, fails fast, never hangs on a TTY). Firewall setup runs off the startup critical path (daemon thread) so it never blocks startup, and is idempotent (no dialog if the rule already exists). `iptables`/`ufw` remain as the fallback when firewalld is not running.

On other platforms, the firewall is configured to:
- **Allow**: Traffic from RFC 1918 Private IP ranges (`192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`).
- **Allow**: Localhost traffic (`127.0.0.0/8`, `::1`).
- **Block**: All other incoming traffic to VAF ports.

Manual firewalld command if needed:
```bash
sudo firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="<LAN-subnet>/24" port port="8443" protocol="tcp" accept' && sudo firewall-cmd --reload
```
(the subnet-scoped rich rule is preferred over a blanket `--add-port`).

Implementation: `vaf/network/firewall.py`

### Layer 2: IP Validation Middleware

Every HTTP request passes through `IPValidationMiddleware` which validates the client IP against RFC 1918 private ranges at the application level. This acts as a second barrier if firewall rules are misconfigured or bypassed.

- Rejects any non-private IP with HTTP 403
- Uses `vaf/network/binding.py` for IP classification
- Active only in network mode (localhost mode skips this layer)

Implementation: `vaf/auth/middleware.py` -> `IPValidationMiddleware`

### Layer 3: JWT Authentication Middleware

Network clients must authenticate via JWT tokens. The `AuthMiddleware` enforces this:

- **Token First, IP Second**: A presented access token is validated **before** any peer-IP branching. If a request carries a valid JWT (`Authorization: Bearer <token>` header or `vaf_token` cookie), the authenticated user's identity and scope are applied regardless of the source IP. This matters when a LAN user is proxied over loopback (the request arrives from `127.0.0.1` but belongs to a remote user): they get **their own** scope, not the local admin's.
- **Localhost Bypass (tokenless only)**: A **tokenless** request from `127.0.0.1` is allowed without authentication (internal IPC and single-user desktop mode). This bypass applies only when no token is presented. A present-but-invalid token rejects a network client with HTTP 401, while a localhost client with an invalid token falls through to the tokenless localhost path.
- **Network Clients**: Must present a valid JWT via `Authorization: Bearer <token>` header or `vaf_token` cookie.
- **Auth-Exempt Paths**: Login, bootstrap, and static asset endpoints are accessible without a token.
- **2FA Enforcement**: If `local_network_require_2fa` is enabled, tokens from users who haven't completed 2FA setup are rejected with HTTP 403.
- **User Context Propagation**: On successful authentication, the middleware populates `request.state` with both individual attributes and a consolidated `user` dict for downstream route handlers (see below).

#### `request.state` Population

After validating the JWT, `AuthMiddleware` attaches the authenticated user's identity to the request in two forms:

**Individual attributes** (legacy, used by some internal utilities):
- `request.state.user_id` — Subject claim (`sub`) from the JWT
- `request.state.username` — Authenticated username
- `request.state.role` — User role (`admin`, `user`, `guest`)
- `request.state.user_scope_id` — UUID used for data isolation (see [USER_ISOLATION.md](../security/USER_ISOLATION.md))

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
- **LAN server mode**: an unauthenticated request is **denied** for memory reads — it resolves to an empty scope and sees **no** memories (fail-closed). The local-admin floor is applied only in genuine single-user/local mode, never to an anonymous network client. The memory scope resolver (`get_current_user_scope` in `vaf/memory/routes.py`) is server-aware and chooses the fallback based on the running mode.

### OAuth Session Binding (Network Mode)

OAuth start/callback endpoints for Email, Cloud, and GitHub enforce a strict actor binding in network mode:

- OAuth start requires an authenticated user session (`request.state.user`).
- OAuth callback validates that the authenticated actor matches the identity encoded in OAuth `state` (username and/or `user_scope_id`).
- Mismatched callbacks are rejected with HTTP 403.

This avoids accidental or malicious cross-user credential binding in multi-user deployments.

Implementation: `vaf/api/oauth_session_binding.py` + OAuth routes in `vaf/api/email_routes.py`, `vaf/api/cloud_routes.py`, `vaf/api/github_routes.py`

### Operational Hardening Check

Use the built-in doctor command to detect common security misconfigurations before exposing VAF on LAN:

- `vaf doctor` (alias for `vaf security doctor`)
- Checks include weak network posture flags (TLS/firewall/login/2FA), permissive channel ingress policy, and channel-enabled-without-pairing states.
- Output is intentionally non-secret and safe to share in internal troubleshooting.

### Layer 4: Rate Limiting

The `RateLimitMiddleware` protects login endpoints against brute-force attacks:

- Tracks failed login attempts per IP address
- Blocks IPs after exceeding the threshold (default: 5 attempts)
- Sliding time window (default: 15 minutes)
- Applies to `/api/auth/login`, `/api/auth/bootstrap`, `/api/auth/verify-2fa`
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

Requests pass through middleware from outermost to innermost:

```
Request -> RateLimitMiddleware -> IPValidationMiddleware -> AuthMiddleware -> SecurityHeaders -> Route Handler
```

### Authentication Details

- **Password Hashing**: Argon2id (time_cost=2, memory_cost=64MB)
- **JWT Tokens**: HS256, configurable expiry (default 24h), refresh tokens (7 days)
- **2FA**: TOTP (RFC 6238), secrets encrypted at rest with AES-256-GCM
- **Session Tracking**: Token hashes (SHA-256) stored in PostgreSQL, no plaintext tokens in DB
- **Cookies**: `vaf_token` cookie with `httponly`, `samesite=lax`, and `secure` flag (when TLS active)

**2FA persistence after restart:** Your 2FA setup is stored in two places that must persist across restarts:
1. **Config** (`~/.vaf/config.json` or `VAF_CONFIG_DIR`): The JWT secret used to encrypt TOTP secrets must be kept. If this file is missing or the secret is lost (e.g. new install or different user), the server cannot decrypt existing 2FA data.
2. **Database** (PostgreSQL, see `memory_db_url`): User accounts and 2FA state (`requires_2fa_setup`, encrypted `totp_secret`) live in the same DB as RAG memory. If the DB is recreated or the data is lost (e.g. Docker without a persistent volume), users will be asked to set up 2FA again (new QR code) after the next login.

**Staying logged in across a DB restart:** validating `/me` (user + active session) queries PostgreSQL, but a backend/Docker restart leaves Postgres briefly unavailable (`the database system is starting up`). To avoid logging users out on that race, `/me` **retries** the DB for a few seconds and, if it is still not ready, **falls back to JWT-only auth** (the already-verified token) instead of returning 401 — so a transient DB restart does not clear your session. Transient PG states (starting up / shutting down / in recovery / too many connections) are treated as retryable. See `auth_routes.py` `_me_user_from_token`.

If you see "2FA was reset (e.g. after config or restart)" when entering your code, the encryption key changed (e.g. config was reset). Use "Back to login", sign in again, and set up 2FA with the new QR code.

### Identity vs. Memory Scoping

- **Global Personality (Soul)**: The agent's identity (Name, Emoji) and behavioral rules (Soul) are defined by the **Administrator** and are global for all users. This ensures a consistent experience across the network.
- **Isolated Memory (RAG)**: While the personality is shared, the **RAG memory is strictly isolated per user**. Facts and history stored by a user are only accessible to them, preventing data leakage between connected devices. This isolation is **fail-closed**: an unresolved or empty user scope yields **no results** rather than searching across all users, so an unauthenticated network request returns nothing instead of leaking another user's memories. Only a genuine single-user/local request floors to the local-admin scope.

### Connection Tracking

The system actively tracks all connections (WebSocket and HTTP) to the VAF backend.
- **Real-time Monitoring**: The "Network Topology" map in Settings visualizes all active devices.
- **Pre-Auth Tracking**: Devices are detected and displayed as "Guest" or "Unauthenticated" immediately upon connection, ensuring visibility of unauthorized access attempts.

---

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
     - All detected local network IPs (e.g. `192.168.1.100`)
     - Machine hostname and FQDN

3. **Persists certificates** in `~/.vaf/ssl/`
   - Certificates are generated once and reused across restarts
   - On each startup, the server checks if the certificate has at least 30 days remaining
   - If expired or expiring soon, only the server certificate is regenerated (CA stays the same)
   - Config paths (`local_network_ssl_cert`, `local_network_ssl_key`) are updated automatically

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
Generate CA         Certificate valid (>30 days)?
Generate Server         |              |
    |                  Yes             No
    |                   |              |
    v                   v              v
Store in              Reuse        Regenerate server cert
~/.vaf/ssl/                        (keep CA unchanged)
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
| CORS Origins | `http://` variants only | `http://` + `https://` variants |
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

CORS origins are dynamically built based on the current mode:

- **Localhost mode**: `http://localhost:3000-3011` and `http://127.0.0.1:3000-3011`
- **Network mode**: Adds all detected local network IPs (e.g. `http://192.168.1.100:3000`)
- **TLS mode**: Adds `https://` variants of all allowed origins

This ensures that browsers on network devices can make credentialed requests to the API without CORS errors.

Implementation: `_build_cors_origins()` in `vaf/core/web_server.py`

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
| `local_network_jwt_secret` | `string` | `""` | JWT signing secret (auto-generated if empty) |
| `local_network_jwt_expiry_hours` | `int` | `24` | Access token TTL in hours |
| `local_network_rate_limit_attempts` | `int` | `5` | Failed login attempts before blocking |
| `local_network_rate_limit_window_minutes` | `int` | `15` | Rate limit sliding window |
| `local_network_tls_enabled` | `bool` | `false` | Enable HTTPS/WSS encryption |
| `local_network_https_port` | `int` | `443` | HTTPS proxy listen port (falls back to 8443 automatically on any platform when 443 is privileged/unbindable) |
| `local_network_ssl_cert` | `string` | `""` | PEM certificate path (auto-populated) |
| `local_network_ssl_key` | `string` | `""` | PEM private key path (auto-populated) |

### Live Updates

Changes to network settings trigger an automatic, orchestrated restart of the frontend and backend services to apply new bindings (e.g., switching from `127.0.0.1` to `0.0.0.0`). Enabling or disabling Local Network flips several config keys in one save; VAF coalesces them into a single restart. Disabling Local Network actually **stops** the integrated HTTPS proxy (8443) and the internal 8005 channel, so LAN access truly closes. The permanent firewalld rule remains (harmless — nothing is listening on the port).

When TLS is enabled, firewall setup uses the effective HTTPS access port (`local_network_https_port`, or `8443` when `443` is privileged on any platform) so LAN clients can reach the proxy entry point. On Linux, firewalld is preferred: it opens only that effective proxy port for the LAN subnet via a rich rule, elevating through pkexec (desktop GUI dialog) or `sudo -n` (headless).
On Windows, creating firewall rules via `netsh advfirewall` requires elevated rights. If VAF is not started as Administrator, LAN access can fail even when hosting is enabled.
The integrated HTTPS proxy is configured for broad client compatibility (`TLS 1.2+`) so older LAN devices do not fail with empty-response errors during TLS negotiation.
Auto-generated TLS certificates are re-generated when the current LAN IP changes, so the certificate SAN list stays aligned with the active access IP.

---

## Architecture Overview

### File Structure

```
vaf/
  auth/
    middleware.py        # AuthMiddleware + IPValidationMiddleware
    rate_limit.py        # RateLimitMiddleware (brute-force protection)
    crypto.py            # Argon2, JWT, AES-256-GCM for TOTP
    models.py            # SQLAlchemy models (LocalUser, UserSession)
    database.py          # Auth DB session (shared with memory DB)
    user_config.py       # Per-user config directories
  network/
    binding.py           # IP detection, RFC 1918 validation
    firewall.py          # OS firewall automation (Windows/macOS/Linux)
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
    +-- SecurityHeadersMiddleware, RateLimitMiddleware, IPValidationMiddleware, AuthMiddleware
    v
Route Handler (reads request.state.user for identity & scoping)
```

The proxy `/ws` relay connects to the backend with `max_size=None`, so it does **not** impose its own per-frame size cap on the WebSocket it relays. This lets oversized frames pass through untouched — for example a `history_update` that embeds inline base64 images can exceed the default WebSocket frame limit. The effective upper bound is the backend's own `ws_max_size` (configured at roughly 200 MB in `vaf/core/web_server.py`), not the library's 16 MB default. Without this, the relay raised `PayloadTooBig` on the oversized frame and the LAN Web UI reconnect-flapped with a "connection lost" loop.

---

## API Reference

### 1. Get Access URL
**GET** `/api/network/access-url`

Returns the URL other devices on the LAN should use. When TLS is enabled, the port matches the **effective** integrated HTTPS proxy port (443, or 8443 after the cross-platform fallback). The Web UI uses this for the "For other devices on LAN" row in Network settings.

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

`backend_port` (and `ports.backend`) is informational — the FastAPI backend binds `127.0.0.1` and is not reachable from the LAN.

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

### 5. Authentication Endpoints

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
