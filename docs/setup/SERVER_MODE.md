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

Server mode is selected during installation, either interactively or with a flag:

```bash
chmod +x install.sh && ./install.sh    # interactive prompt
./install.sh --server                  # non-interactive server install
```

The hosted one-liner accepts the same flag (`bash -s --` forwards it):

```bash
curl -fsSL https://raw.githubusercontent.com/Veyllo-Labs/VAF/main/packaging/install/bootstrap.sh | bash -s -- --server
```

When prompted interactively:

```
[1] Desktop  - personal use, local only, system tray (default)
[2] Server   - always-on service, LAN accessible via HTTPS, starts at boot

Choose [1/2, default 1]: 2
```

The installer then:

1. Optionally asks for a master passphrase (see Credential Encryption below); if given, it is stored owner-only in `~/.vaf/service.env` for the service. Unattended installs can pre-set `VAF_MASTER_PASSPHRASE` in the environment instead.
2. Runs `vaf server provision`: writes `server_mode: true`, `local_network_enabled: true` and `local_network_tls_enabled: true` through the config layer, generates the TLS certificates, opens the OS firewall for the LAN access port (subnet-scoped), and warns when the LAN IP looks DHCP-assigned.
3. Enables the Docker daemon at boot (`systemctl enable docker`) so the memory stack comes back after a reboot.
4. Masks the systemd sleep/suspend targets so a repurposed desktop or laptop stays reachable. Revert with `sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target`.
5. Warns when the system clock is not NTP-synced (TLS validation and OAuth sign-ins depend on correct time).
6. Installs a systemd user service at `~/.config/systemd/user/vaf.service`, enables it, enables linger so it starts at boot without an active login session (`loginctl enable-linger`), and starts it immediately.

`vaf server provision` is idempotent and can be re-run at any time (for example after moving the machine to another subnet), and re-running `./install.sh --server` upgrades an existing server install in place. Immutable/transactional distributions (openSUSE MicroOS, Leap Micro) are detected early and refused with a clear message. This is a current limitation of the alpha installer, not a design decision: support via `transactional-update` is planned, and exactly these systems are attractive server targets. Until then, run VAF on a standard distribution or inside a container/VM on the immutable host.

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

## Live Dashboard (`vaf top`)

For a running overview in the terminal (over SSH included), `vaf top` renders a
self-refreshing dashboard headed by the Veyllo mark and the hostname: VAF
version, mode, the active provider with its actual model (API providers show
their API model, not the local one), the LAN access URLs (hostname first - the
certificate carries it as a DNS SAN - then the IPs), host OS and uptimes (host
and service process tree with PID/RSS/CPU), live CPU/RAM/disk/GPU utilization,
a Network section with the total up/down rates and the connected clients
(inbound connections grouped per remote IP, with the ports they use), and the
health of every Docker service. Per-IP byte rates are deliberately absent: they
would need packet capture (root); connection counts are the honest per-IP
signal available to an unprivileged process.

```bash
vaf top              # live view, refresh every 2s (Ctrl+C to exit)
vaf top -i 5         # slower refresh
vaf top --once       # print one snapshot and exit (for scripts and checks)
vaf top --no-logs    # dashboard only, without the log pane
vaf start            # in a terminal: starts the service, then opens this dashboard
vaf tray             # in a terminal: tray in the background, dashboard in front
```

In an interactive terminal the dashboard is the default face of both start
commands: `vaf start` opens it after starting (suppress with `--no-watch`;
scripts and pipes never get it), and `vaf tray` runs the real tray as a
background child writing the service log while the terminal shows the
dashboard with that log live underneath (`--no-top` restores the raw
foreground run). Leaving the `vaf tray` dashboard with Ctrl+C stops the tray
it started - the same contract as the old foreground tray; attaching to an
already running VAF only detaches on Ctrl+C and stops nothing. Closing the
terminal window outright leaves VAF running; `vaf stop` ends it.

Below the dashboard, `vaf top` follows the service's live output in a log pane
that fills the remaining terminal height and resizes with the window. The
source is picked automatically: the systemd journal in server mode, otherwise
the most recently written of the known log files (`~/.vaf/logs/vaf_run.log`
from `vaf start` and from a terminal- or desktop-started tray, which tees its
own output there; `logs/vaf_run.log` from vaf.sh; `logs/tray_debug.log` from
start_vaf.sh). A file that does not belong to the current run - older than the
running service, or untouched for 10 minutes with no service running - is not
shown at all; only a source that goes quiet after `vaf top` attached is marked
stale. The pane is skipped entirely when the terminal is too small, and the
pane appears on its own as soon as a log source shows up.

The Docker probe runs on its own slow cadence in the background, so a stopped
Docker daemon (its status check can take up to 10 seconds) never freezes the
view. GPU utilization is live on NVIDIA (`nvidia-smi`); other vendors show the
detected card with utilization as n/a. VAF-internal queue and session metrics
are not shown: they exist only inside the service process, and exposing them
here requires the planned admin API endpoint on the same collector.

## LAN Access

VAF listens on `https://<LAN-IP>:8443` in the usual case: the configured HTTPS port is 443, and an unprivileged service cannot bind it, so the proxy falls back to 8443. When 443 is bindable (root, `CAP_NET_BIND_SERVICE`), the address is plain `https://<LAN-IP>`. To find your LAN IP:

```bash
ip route get 1.1.1.1 | grep -oP 'src \K\S+'
```

**TLS certificate:** VAF auto-generates a self-signed certificate on first start (`~/.vaf/ssl/`). Browsers will show a certificate warning on the first visit - this is expected for local networks. Accept the exception once; the certificate is then trusted for that browser.

**Authentication:** All access (local and LAN) requires login. Credentials are set during the initial setup wizard at `https://<LAN-IP>:8443`.

**Ports:**

| Port | Purpose |
|------|---------|
| 8443 | HTTPS proxy (LAN access, TLS) - effective port; 8443 only when 443 falls back |
| 3000 | Next.js frontend (internal, localhost only) |
| 8001 | FastAPI backend (internal, localhost only) |
| 8005 | Internal plain-HTTP backend channel (internal, localhost only) |
| 8080 | llama-server LLM backend (internal, localhost only) |

Only the HTTPS proxy access port (8443 after a 443 fallback) is exposed on the network interface. All other ports are bound to `127.0.0.1`.

## Locked Settings

In server mode, the following config keys are locked and cannot be changed via the Settings UI or the API:

- `local_network_enabled` - always `true`
- `local_network_tls_enabled` - always `true`
- `server_mode` - always `true`

Attempts to write these keys via `PATCH /api/config` are silently ignored.

To change them you must edit `~/.vaf/config.json` directly and restart the service.

## Credential Encryption (headless)

Headless servers often have no OS keyring (no Secret Service running), so VAF falls back to an AES-256-GCM encrypted file under the data directory for OAuth tokens and IMAP/SMTP passwords. By default the encryption key is wrapped by a random key stored in its own owner-only (`0600`) file, `secure_store.kek` in `~/.vaf`; no key material is written to `config.json`.

For stronger protection, set a master passphrase so the encryption key is derived from it (scrypt) and never written to disk. The server installer asks for it during installation (pressing Enter skips it); when given, it is written owner-only to `~/.vaf/service.env`, and the systemd unit loads that file via `EnvironmentFile=-%h/.vaf/service.env`. The passphrase therefore reaches service starts only - an interactive `vaf` shell does not read the file. To set or change it later:

```bash
( umask 077; printf 'VAF_MASTER_PASSPHRASE=%s\n' '<a long, unique passphrase>' > ~/.vaf/service.env )
systemctl --user restart vaf
```

With the passphrase set, the encrypted fallback cannot be opened without it - even by someone who can read the files. Keep it out of `config.json` and shell history. If the passphrase is lost, the stored credentials cannot be recovered and the affected accounts must be re-linked.

## Memory isolation (Row-Level Security)

The memory database (`vaf_memory`) enforces PostgreSQL Row-Level Security on the `memories` table, so one user cannot read or write another user's memories at the database layer - independent of the application-level scope filter. The application's data connection (`memory_db_url`) uses a non-superuser role (`vaf_app`, `NOSUPERUSER`/`NOBYPASSRLS`); a separate owner connection (`memory_db_owner_url`, role `vaf`) handles DDL, migrations and global stats. The policy is fail-closed: a row is visible/writable only when its `user_scope_id` equals the per-transaction `app.current_user_scope_id` GUC.

- **Enable / cut over an existing install:** apply `scripts/rls_app_role.sql` (creates the `vaf_app` role + grants), then `scripts/rls_enforce.sql` (fail-closed policy + `ENABLE`/`FORCE`); set `memory_db_url` to the `vaf_app` DSN and `memory_db_owner_url` to the `vaf` DSN, then restart. Fresh installs get the role and policy from `init_db` automatically - only the DSN switch is needed to enforce.
- **Roll back:** set `memory_db_url` back to the owner (`vaf`) DSN and restart - the superuser bypasses RLS, so all rows are visible again. Optionally run `scripts/rls_disable.sql`. No data is mutated.

See [USER_ISOLATION.md](../security/USER_ISOLATION.md) for the full model.

## Concurrency and throughput

VAF serves many users from one process, in two layers that behave differently:

- **Transport is async and never blocks.** All WebSocket connections share one event loop. A chat message
  is enqueued onto a shared `TaskQueue` and the connection returns immediately, so other users' I/O and
  token streaming keep flowing while a model call runs.
- **Execution runs in a worker pool.** Worker threads pull turns from the queue and run them. The default is
  **one** worker (`parallel_main_workers: 1`), so turns execute strictly one at a time - a long reasoning
  turn by one user blocks the others until it finishes.

To run users concurrently, raise the pool (admin-only keys; restart to apply):

```json
"parallel_main_workers": 5,
"queue_policy": "weighted_fair"
```

The requested count is **clamped per provider**, so it is safe to set high:

| Provider | Effective workers | Cap key |
|----------|-------------------|---------|
| API (veyllo/openai/anthropic/deepseek/google/openrouter) | `min(requested, max_parallel_api_workers)` - default 5 | `max_parallel_api_workers` |
| `local` (one shared llama-server) | `min(requested, max_parallel_local_workers, n_parallel slots)` - default 2 | `max_parallel_local_workers` |

`local` is a single llama-server process, so its concurrency is bounded by its `--parallel` decode slots and
VRAM; the cap prevents oversubscribing the GPU. API providers have no local GPU limit - there the cap exists
to stay within provider rate limits.

**Isolation under concurrency.** Each worker builds its own `Agent`, and the queue never hands the same
session to two workers at once. The guarantee is therefore **per session, not per user**: the in-flight set
is keyed on the session id alone (`vaf/core/task_queue.py`), so one person's web session and their
`telegram_...` session can occupy two workers simultaneously, while turns *within* one session always run in
order. `weighted_fair` additionally schedules the interactive / automation / background task classes fairly.
The per-session guarantee holds under the `legacy` policy too - `weighted_fair` only adds lane fairness.

A session leaves the in-flight set when the worker calls `task_done()`, and also when that same worker asks
for its next task: one worker holds one task, so the request itself says the previous one is finished. That
second release is what keeps a consumer which never calls `task_done()` from parking its session forever -
the stale-reservation sweep cannot cover it, because it only reclaims threads that have actually died.

**Rate limits.** With several workers hitting one provider, transient `429`s are expected. VAF retries them
for every provider, honoring a `Retry-After` header capped by `api_retry_after_max` - see
[API_INTEGRATION.md](../llm/API_INTEGRATION.md).

**Known limitations with more than one worker.** Editing a custom tool in Settings hot-reloads worker #1
only (the others pick it up on the next restart); the "session active" hint shown on reconnect reflects
worker #1's last task (cosmetic - live status and Stop are per-session).

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

# Disable linger (optional - only if you don't want any user services at boot)
sudo loginctl disable-linger $USER

# Re-enable sleep/suspend (the server install masked these)
sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

## Troubleshooting

**Service fails to start:**
```bash
journalctl --user -u vaf -n 50
```
Common causes: Python venv path changed after a `git pull` to a different directory, or Docker containers not running (memory system unavailable).

**Port 8443 not reachable from other devices:**
- The server installer opens the firewall during installation. The first fix is to re-run that step - it is idempotent and prints what it did:
  ```bash
  cd <VAF-checkout> && sudo -v && venv/bin/python -m vaf.main server provision
  ```
  (`sudo -v` refreshes the sudo timestamp the headless elevation relies on; in a desktop session a native password dialog appears instead.)
- Check firewall state: `sudo firewall-cmd --list-rich-rules` (firewalld) or `sudo ufw status`
- VAF also configures the OS firewall at runtime via `vaf/network/firewall.py`. On Linux it prefers firewalld when it is running and opens **only** the access port for the LAN subnet (a scoped rich rule), not a blanket world-open. iptables/ufw are used as a fallback when firewalld is not running.
- Elevation differs by environment:
  - **Desktop session:** when hosting is enabled VAF prompts automatically through a native polkit/pkexec password dialog and adds the rule for you.
  - **Headless/server:** the running service cannot elevate at all (`NoNewPrivileges`, non-interactive `sudo -n`) - that is exactly why the installer and `vaf server provision` own this step.
- Manual fallback, firewalld (preferred subnet-scoped rich rule form; replace `<LAN-subnet>` with your network, e.g. `192.168.2.0`):
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
The access URL, the certificate's IP SANs and the subnet-scoped firewall rule are all bound to the address. Give the server a static LAN IP, or reserve its address in the router's DHCP settings; `vaf server provision` warns during installation when the address looks DHCP-assigned. After an IP change: regenerate the certificate (`rm -rf ~/.vaf/ssl/` + restart, see above) and re-run `vaf server provision` so the firewall rule matches the new subnet. Alternatively use the hostname instead of the IP: the auto-generated certificate carries the machine's hostname and FQDN as DNS SANs, so a name that resolves on your network keeps working across IP changes (note the certificate is NOT re-issued when the hostname itself changes - only IP changes trigger regeneration).
