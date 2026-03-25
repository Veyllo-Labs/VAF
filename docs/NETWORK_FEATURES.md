# VAF Network Features & Security

VAF (Veyllo Agent Framework) includes robust networking capabilities designed to allow secure, local collaboration. This document details the architecture, security measures, and usage of these features.

**Integrated HTTPS proxy (no Nginx required):** When **Local Network** and **SSL/TLS** are enabled and certificate/key paths are set, VAF starts an integrated reverse proxy on `0.0.0.0:local_network_https_port` (default 443; on Windows often 8443 if 443 requires admin). The proxy is the single TLS entry point and routes requests as follows:

| Path | Target | Description |
|------|--------|-------------|
| `/ws` | `ws://127.0.0.1:8005/ws` | WebSocket relay (bidirectional) |
| `/api`, `/api/*` | `http://127.0.0.1:8005` | Backend API (all HTTP methods) |
| `/sounds/*` | `http://127.0.0.1:8005` | Notification sound files (GET/HEAD) |
| Everything else | `http://127.0.0.1:3000` | Next.js frontend |

The proxy uses **shared httpx clients with connection pooling** (max 50 connections, 20 keep-alive) for both frontend and backend targets, avoiding the overhead of opening a new TCP connection for every resource request. Access via `https://127.0.0.1` (or `https://127.0.0.1:8443` when using 8443) and `https://<LAN-IP>` works without an external proxy. Optional: [NGINX_REVERSE_PROXY.md](NGINX_REVERSE_PROXY.md) and `docs/nginx-vaf-https.conf.example`.

## Security Model

Security is the primary design constraint for VAF's network features. The system employs a **Defense in Depth** strategy with five layers:

### Layer 1: OS Firewall Automation

When "Local Network Hosting" is enabled, VAF automatically configures the OS firewall (Windows Firewall, macOS pf, or Linux iptables) to:
- **Allow**: Traffic from RFC 1918 Private IP ranges (`192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`).
- **Allow**: Localhost traffic (`127.0.0.0/8`, `::1`).
- **Block**: All other incoming traffic to VAF ports (default 3000/8001).

Implementation: `vaf/network/firewall.py`

### Layer 2: IP Validation Middleware

Every HTTP request passes through `IPValidationMiddleware` which validates the client IP against RFC 1918 private ranges at the application level. This acts as a second barrier if firewall rules are misconfigured or bypassed.

- Rejects any non-private IP with HTTP 403
- Uses `vaf/network/binding.py` for IP classification
- Active only in network mode (localhost mode skips this layer)

Implementation: `vaf/auth/middleware.py` -> `IPValidationMiddleware`

### Layer 3: JWT Authentication Middleware

Network clients must authenticate via JWT tokens. The `AuthMiddleware` enforces this:

- **Localhost Bypass**: Connections from `127.0.0.1` are allowed without a token (backward-compatible with single-user desktop mode).
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
- `request.state.user_scope_id` — UUID used for data isolation (see [USER_ISOLATION.md](USER_ISOLATION.md))

**Consolidated dict** (used by all API route handlers):
```python
request.state.user = {
    "user_id": "<sub>",
    "username": "<username>",
    "role": "<role>",
    "user_scope_id": "<uuid>",
}
```

All API route files (`config_routes`, `email_routes`, `cloud_routes`, `whatsapp_routes`, `telegram_routes`, `contact_routes`, `user_persona_routes`, `memory/routes`) read `request.state.user` as a dict to extract the current user's identity. When running in localhost mode (no authentication), `request.state.user` is not set and routes fall back to the local admin defaults.

Implementation: `vaf/auth/middleware.py` -> `AuthMiddleware`

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

If you see "2FA was reset (e.g. after config or restart)" when entering your code, the encryption key changed (e.g. config was reset). Use "Back to login", sign in again, and set up 2FA with the new QR code.

### Identity vs. Memory Scoping

- **Global Personality (Soul)**: The agent's identity (Name, Emoji) and behavioral rules (Soul) are defined by the **Administrator** and are global for all users. This ensures a consistent experience across the network.
- **Isolated Memory (RAG)**: While the personality is shared, the **RAG memory is strictly isolated per user**. Facts and history stored by a user are only accessible to them, preventing data leakage between connected devices.

### Connection Tracking

The system actively tracks all connections (WebSocket and HTTP) to the VAF backend.
- **Real-time Monitoring**: The "Network Topology" map in Settings visualizes all active devices.
- **Pre-Auth Tracking**: Devices are detected and displayed as "Guest" or "Unauthenticated" immediately upon connection, ensuring visibility of unauthorized access attempts.

---

## TLS/SSL Encryption

VAF supports full TLS encryption for both HTTP (HTTPS) and WebSocket (WSS) traffic within the local network. This prevents eavesdropping and man-in-the-middle attacks even on shared LANs.

### Quick Start (Automatic Certificates)

The simplest way to enable TLS:

1. Set `local_network_tls_enabled` to `true` in `~/.vaf/config.json`
2. Restart VAF

That's it. VAF automatically generates a local Certificate Authority (CA) and server certificate. No manual `openssl` commands needed.

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
| Backend API | `http://host:8001` | `https://host:8001` |
| WebSocket | `ws://host:8001/ws` | `wss://host:8001/ws` |
| Auth Cookies | `httponly`, `samesite=lax` | `httponly`, `samesite=lax`, **`secure`** |
| CORS Origins | `http://` variants only | `http://` + `https://` variants |
| Security Headers | Standard set | Standard set + **HSTS** (`max-age=31536000`) |
| Frontend Proxy | `http://127.0.0.1:8001` | `https://127.0.0.1:8001` |

### TLS Configuration Reference

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `local_network_tls_enabled` | `bool` | `false` | Master toggle for TLS |
| `local_network_https_port` | `int` | `443` | HTTPS proxy listen port (8443 on Windows if 443 needs admin) |
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
| `local_network_https_port` | `int` | `443` | HTTPS proxy listen port (e.g. 8443 on Windows if 443 needs admin) |
| `local_network_ssl_cert` | `string` | `""` | PEM certificate path (auto-populated) |
| `local_network_ssl_key` | `string` | `""` | PEM private key path (auto-populated) |

### Live Updates

Changes to network settings trigger an automatic, orchestrated restart of the frontend and backend services to apply new bindings (e.g., switching from `127.0.0.1` to `0.0.0.0`).

When TLS is enabled, firewall setup uses the effective HTTPS access port (`local_network_https_port`, or `8443` on Windows when `443` would require elevation) so LAN clients can reach the proxy entry point.
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

When TLS is enabled, the **integrated HTTPS proxy** is the single entry point. The backend serves TLS on port 8001 and an internal HTTP-only channel on port 8005; the proxy talks to the frontend (3000) and to the internal channel (8005) so TLS is terminated only at the proxy.

```
Browser (https://127.0.0.1 or https://<LAN-IP>, port 443 or 8443)
    |
    v
Integrated HTTPS proxy (0.0.0.0:443 or 8443)  [connection-pooled httpx clients]
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

---

## API Reference

### 1. Get Access URL
**GET** `/api/network/access-url`

Returns the URL other devices on the LAN should use. When TLS is enabled, the port matches the integrated HTTPS proxy (443 or 8443 on Windows). The Web UI uses this for the "For other devices on LAN" row in Network settings.

**Response (TLS on, Windows):**
```json
{
  "host": "192.168.1.50",
  "port": 8443,
  "url": "https://192.168.1.50:8443"
}
```

**Response (no LAN IP detected):** `{ "host": null, "port": 443, "url": null }`

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

### 3. Authentication Endpoints

| Method | Endpoint | Auth Required | Description |
|--------|----------|---------------|-------------|
| GET | `/api/auth/needs-setup` | No | Check if first admin must be created |
| POST | `/api/auth/bootstrap` | No | Create first admin account |
| POST | `/api/auth/login` | No | Username/password login |
| POST | `/api/auth/setup-2fa` | Bearer | Generate TOTP QR code |
| POST | `/api/auth/verify-2fa` | Temp Token | Verify TOTP code, get full token |
| POST | `/api/auth/refresh` | Refresh Token | Exchange refresh token for new access token |
| POST | `/api/auth/logout` | No | Clear auth cookie |
| GET | `/api/auth/me` | Bearer/Cookie | Get current user info |
