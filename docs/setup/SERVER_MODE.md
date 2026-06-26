# VAF Server Mode

Server mode is an installation profile for running VAF as a persistent background service on a Linux machine. It is intended for home servers, NAS devices, and any headless system where VAF should be reachable from other devices on the local network.

## Differences from Desktop Mode

| | Desktop | Server |
|---|---|---|
| Starts at boot | No (manual) | Yes (systemd) |
| LAN access | Optional, user-controlled | Always on, locked |
| TLS (HTTPS) | Optional | Always on |
| Tray icon | Yes (where available) | No (headless) |
| Settings → Local Network tab | Visible | Visible (LAN toggle replaced by locked notice) |
| Settings → Start Tray on Login | Visible | Hidden |

## Installation

Server mode is selected during installation:

```bash
chmod +x install.sh && ./install.sh
```

When prompted:

```
[1] Desktop  — personal use, local only, system tray (default)
[2] Server   — always-on service, LAN accessible via HTTPS, starts at boot

Choose [1/2, default 1]: 2
```

The installer then:

1. Writes `server_mode: true`, `local_network_enabled: true`, and `local_network_tls_enabled: true` to `~/.vaf/config.json`.
2. Installs a systemd user service at `~/.config/systemd/user/vaf.service`.
3. Enables the service (`systemctl --user enable vaf`).
4. Enables linger so the service starts at boot without an active login session (`loginctl enable-linger`).
5. Starts the service immediately.

## Service Management

```bash
# Status
systemctl --user status vaf

# Start / stop / restart
systemctl --user start vaf
systemctl --user stop vaf
systemctl --user restart vaf

# Live logs
journalctl --user -u vaf -f

# Recent logs (last 100 lines)
journalctl --user -u vaf -n 100

# Disable autostart
systemctl --user disable vaf
```

## LAN Access

VAF listens on `https://<LAN-IP>:8443`. To find your LAN IP:

```bash
ip route get 1.1.1.1 | grep -oP 'src \K\S+'
```

**TLS certificate:** VAF auto-generates a self-signed certificate on first start (`~/.vaf/ssl/`). Browsers will show a certificate warning on the first visit — this is expected for local networks. Accept the exception once; the certificate is then trusted for that browser.

**Authentication:** All access (local and LAN) requires login. Credentials are set during the initial setup wizard at `https://<LAN-IP>:8443`.

**Ports:**

| Port | Purpose |
|------|---------|
| 8443 | HTTPS proxy (LAN access, TLS) — effective port; 8443 only when 443 falls back |
| 3000 | Next.js frontend (internal, localhost only) |
| 8001 | FastAPI backend (internal, localhost only) |
| 8005 | Internal plain-HTTP backend channel (internal, localhost only) |
| 8080 | llama-server LLM backend (internal, localhost only) |

Only the HTTPS proxy access port (8443 after a 443 fallback) is exposed on the network interface. All other ports are bound to `127.0.0.1`.

## Locked Settings

In server mode, the following config keys are locked and cannot be changed via the Settings UI or the API:

- `local_network_enabled` — always `true`
- `local_network_tls_enabled` — always `true`
- `server_mode` — always `true`

Attempts to write these keys via `PATCH /api/config` are silently ignored.

To change them you must edit `~/.vaf/config.json` directly and restart the service.

## Credential Encryption (headless)

Headless servers often have no OS keyring (no Secret Service running), so VAF falls back to an AES-256-GCM encrypted file under the data directory for OAuth tokens and IMAP/SMTP passwords. By default the encryption key is wrapped by a random key stored in `config.json` (owner-only, `0600`).

For stronger protection, set a master passphrase so the encryption key is derived from it (scrypt) and never written to disk:

```bash
# In the unit's environment (e.g. systemd drop-in or the service's EnvironmentFile)
VAF_MASTER_PASSPHRASE="<a long, unique passphrase>"
```

With the passphrase set, the encrypted fallback cannot be opened without it — even by someone who can read the files. Keep it out of `config.json` and shell history; supply it via the service environment. If the passphrase is lost, the stored credentials cannot be recovered and the affected accounts must be re-linked.

## Memory isolation (Row-Level Security)

The memory database (`vaf_memory`) enforces PostgreSQL Row-Level Security on the `memories` table, so one user cannot read or write another user's memories at the database layer — independent of the application-level scope filter. The application's data connection (`memory_db_url`) uses a non-superuser role (`vaf_app`, `NOSUPERUSER`/`NOBYPASSRLS`); a separate owner connection (`memory_db_owner_url`, role `vaf`) handles DDL, migrations and global stats. The policy is fail-closed: a row is visible/writable only when its `user_scope_id` equals the per-transaction `app.current_user_scope_id` GUC.

- **Enable / cut over an existing install:** apply `scripts/rls_app_role.sql` (creates the `vaf_app` role + grants), then `scripts/rls_enforce.sql` (fail-closed policy + `ENABLE`/`FORCE`); set `memory_db_url` to the `vaf_app` DSN and `memory_db_owner_url` to the `vaf` DSN, then restart. Fresh installs get the role and policy from `init_db` automatically — only the DSN switch is needed to enforce.
- **Roll back:** set `memory_db_url` back to the owner (`vaf`) DSN and restart — the superuser bypasses RLS, so all rows are visible again. Optionally run `scripts/rls_disable.sql`. No data is mutated.

See [USER_ISOLATION.md](../security/USER_ISOLATION.md) for the full model.

## Concurrency and throughput

VAF serves many users from one process, in two layers that behave differently:

- **Transport is async and never blocks.** All WebSocket connections share one event loop. A chat message
  is enqueued onto a shared `TaskQueue` and the connection returns immediately, so other users' I/O and
  token streaming keep flowing while a model call runs.
- **Execution runs in a worker pool.** Worker threads pull turns from the queue and run them. The default is
  **one** worker (`parallel_main_workers: 1`), so turns execute strictly one at a time — a long reasoning
  turn by one user blocks the others until it finishes.

To run users concurrently, raise the pool (admin-only keys; restart to apply):

```json
"parallel_main_workers": 5,
"queue_policy": "weighted_fair"
```

The requested count is **clamped per provider**, so it is safe to set high:

| Provider | Effective workers | Cap key |
|----------|-------------------|---------|
| API (veyllo/openai/anthropic/deepseek/google/openrouter) | `min(requested, max_parallel_api_workers)` — default 5 | `max_parallel_api_workers` |
| `local` (one shared llama-server) | `min(requested, max_parallel_local_workers, n_parallel slots)` — default 2 | `max_parallel_local_workers` |

`local` is a single llama-server process, so its concurrency is bounded by its `--parallel` decode slots and
VRAM; the cap prevents oversubscribing the GPU. API providers have no local GPU limit — there the cap exists
to stay within provider rate limits.

**Isolation under concurrency.** Each worker builds its own `Agent`, and the queue never hands the same
session to two workers at once. So a single user's turns always run in order while *different* users run in
parallel; `weighted_fair` additionally schedules the interactive / automation / background task classes
fairly. The per-session guarantee holds under the `legacy` policy too — `weighted_fair` only adds lane
fairness.

**Rate limits.** With several workers hitting one provider, transient `429`s are expected. VAF retries them
for every provider, honoring a `Retry-After` header capped by `api_retry_after_max` — see
[API_INTEGRATION.md](../llm/API_INTEGRATION.md).

**Known limitations with more than one worker.** Editing a custom tool in Settings hot-reloads worker #1
only (the others pick it up on the next restart); the "session active" hint shown on reconnect reflects
worker #1's last task (cosmetic — live status and Stop are per-session).

Mechanics: [TOOL_SUPERVISION.md](../agents/TOOL_SUPERVISION.md#worker-and-queue-model). Config keys:
[CONFIG_SCHEMA.md](CONFIG_SCHEMA.md).

## Reverting to Desktop Mode

To switch back to desktop mode:

```bash
# Stop and disable the service
systemctl --user stop vaf
systemctl --user disable vaf

# Edit config
nano ~/.vaf/config.json
# Set: "server_mode": false, "local_network_enabled": false

# Disable linger (optional — only if you don't want any user services at boot)
sudo loginctl disable-linger $USER
```

## Troubleshooting

**Service fails to start:**
```bash
journalctl --user -u vaf -n 50
```
Common causes: Python venv path changed after a `git pull` to a different directory, or Docker containers not running (memory system unavailable).

**Port 8443 not reachable from other devices:**
- Check firewall: `sudo firewall-cmd --list-rich-rules` (firewalld) or `sudo ufw status`
- VAF configures the OS firewall itself via `vaf/network/firewall.py`. On Linux it prefers firewalld when it is running and opens **only** port 8443 for the LAN subnet (a scoped rich rule), not a blanket world-open. iptables/ufw are used as a fallback when firewalld is not running.
- Elevation differs by environment:
  - **Desktop session:** when hosting is enabled VAF prompts automatically through a native polkit/pkexec password dialog and adds the rule for you.
  - **Headless/server:** VAF uses non-interactive `sudo -n` (it fails fast rather than hanging on a TTY), so the rule is typically not added automatically — run the manual command below.
- Manual firewalld command (preferred subnet-scoped rich rule form; replace `<LAN-subnet>` with your network, e.g. `192.168.2.0`):
  ```bash
  sudo firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="<LAN-subnet>/24" port port="8443" protocol="tcp" accept' && sudo firewall-cmd --reload
  ```
- Ubuntu (ufw fallback): `sudo ufw allow 8443/tcp`

**Certificate regeneration:**
If the TLS certificate has expired or the LAN IP changed:
```bash
rm -rf ~/.vaf/ssl/
systemctl --user restart vaf
```
VAF regenerates the certificate on the next start.

**LAN IP changed (DHCP):**
Set a static LAN IP on the server, or use the hostname instead of the IP address.
