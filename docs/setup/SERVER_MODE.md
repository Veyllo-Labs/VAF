# VAF Server Mode

Server mode is an installation profile for running VAF as a persistent background service on a Linux machine. It is intended for home servers, NAS devices, and any headless system where VAF should be reachable from other devices on the local network.

## Differences from Desktop Mode

| | Desktop | Server |
|---|---|---|
| Starts at boot | No (manual) | Yes (systemd) |
| LAN access | Optional, user-controlled | Always on, locked |
| TLS (HTTPS) | Optional | Always on |
| Tray icon | Yes (where available) | No (headless) |
| Desktop packages | Installed (tray icon, desktop window, terminal microphone) | Left out ([below](#what-a-server-install-leaves-out)) |
| Settings → Local Network tab | Visible | Visible (LAN toggle replaced by locked notice) |
| Settings → Start Tray on Login | Visible | Hidden |

Server or desktop is one choice; whether the machine runs its own models is another, the
**inference profile** ([INSTALL_PROFILES.md](INSTALL_PROFILES.md)). Either install can be
`local` (the models and the speech containers run here) or `api` (none of them; chat and
speech-to-text go to the providers set in Settings, text-to-speech to a provider or a TTS
server you run elsewhere, `chatterbox`). A rented server usually wants `api`.

## Installation

Server mode is selected during installation, either interactively or with a flag:

```bash
chmod +x install.sh && ./install.sh    # interactive prompt
./install.sh --server                  # non-interactive server install
./install.sh --server --profile api    # ... without local models
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
2. Runs `vaf server provision`: writes `server_mode: true`, `local_network_enabled: true` and `local_network_tls_enabled: true` through the config layer; on a machine with a public IPv4 address that is set up as a server for the first time it also turns "VPN only" on (on such a machine the private ranges VAF admits by default are the hosting provider's network and the container bridges, not your LAN; a re-run never changes it, and `vaf server vpn-only off` undoes it); it generates the TLS certificates, replaces the published default database passwords with random ones when the database answers (both roles, verified before they are written; `vaf/core/db_credentials.py`, the same rotation as `vaf secure rotate-db`; when it cannot reach the database it says so loudly and names that command; a run that stops part-way names the keys already replaced and the ones still on the default, and a password that was changed but did not verify is printed with the config key to save it under, since it is the only copy), opens the OS firewall for the access port to the admitted networks (subnet-scoped; on Ubuntu, where ufw is installed but switched off by default, it changes nothing and says so, since rules added to a switched-off ufw do nothing; with "VPN only" on and no VPN up nothing besides this machine is admitted, so it opens nothing and says to run it again once the VPN is up), and warns when the LAN IP looks DHCP-assigned. While no admin exists yet, it ends by saying how to create one: `vaf setup` in the terminal, or a browser with the code `vaf setup code` shows.
3. Enables the Docker daemon at boot (`systemctl enable docker`) so the memory stack comes back after a reboot.
4. Masks the systemd sleep/suspend targets so a repurposed desktop or laptop stays reachable. Revert with `sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target`.
5. Warns when the system clock is not NTP-synced (TLS validation and OAuth sign-ins depend on correct time).
6. Installs a systemd user service at `~/.config/systemd/user/vaf.service`, enables it, enables linger so it starts at boot without an active login session (`loginctl enable-linger`), and starts it immediately. When the same run added you to the `docker` group, it restarts your systemd user manager first (`user@<uid>.service`): the manager keeps the groups it started with, and the service would otherwise reach Docker only after the next reboot. When other user services are running or still starting it does not cut them off: at a terminal it asks first, otherwise VAF reaches Docker after the next reboot (or `sudo systemctl restart user@$(id -u).service`). Timers, sockets, path watchers and finished one-shot services that were active are started again after the restart. An install started from a terminal of a desktop session runs inside that same manager and would end itself, so it leaves the restart to you there; an SSH login is not affected. A server without a system Node.js gets the installer's portable one in `~/.vaf/node`; VAF finds it there itself (`vaf/core/node_runtime.py`), since the unit does not read your shell's startup file.

`vaf server provision` is idempotent and can be re-run at any time (for example after moving the machine to another subnet), and re-running `./install.sh --server` upgrades an existing server install in place. Immutable/transactional distributions (openSUSE MicroOS, Leap Micro) are detected early and refused with a clear message. This is a current limitation of the alpha installer, not a design decision: support via `transactional-update` is planned, and exactly these systems are attractive server targets. Until then, run VAF on a standard distribution or inside a container/VM on the immutable host.

### What a server install leaves out

The service runs headless (`VAF_NATIVE_WRAPPER=1`): no tray icon, no desktop window, no
terminal microphone. A server install therefore leaves out what only those use:

- **System packages:** the tray icon's GTK, AppIndicator and WebKitGTK libraries, the headers
  PyGObject is built against, and the PortAudio headers. On Ubuntu and Debian, ffmpeg is
  installed without apt's recommendations, which brought pocketsphinx's speech model, GTK 3,
  icon themes and video drivers along. ffmpeg itself stays (voice replies to messengers are
  converted to OGG/Opus), and so do git, tesseract (OCR) and the compiler with the Python
  headers (pip builds a package that has no wheel for this machine).
- **Python packages:** PyGObject, and `requirements-headless.txt` instead of
  `requirements.txt`: no PySide6, pywebview, pystray or qtpy (the window and the tray), and no
  SpeechRecognition (the terminal microphone; its wheel bundles a 38 MB pocketsphinx model).
  Speech in the web UI goes through the speech services and is unaffected.

Measured on Ubuntu 24.04: 174 fewer system packages and 419 MB less on disk against the server
cloud image (229 and 471 MB against the minimal container image), plus 730 MB of Python
packages. `vaf update` keeps the smaller set on a server: it installs the requirements file
`server_mode` selects (`vaf/core/update_check.py`, `requirements_file`).

An existing server keeps what an earlier install put there: neither pip nor the package
manager removes anything on a re-run. To free the space in VAF's own environment (nothing
outside it uses these), stop the service and run from the VAF folder:

```bash
systemctl --user stop vaf
venv/bin/python -m pip uninstall -y PySide6 PySide6_Addons PySide6_Essentials shiboken6 \
    pywebview pystray python-xlib bottle proxy_tools qtpy SpeechRecognition \
    standard-aifc standard-chunk PyGObject pycairo
systemctl --user start vaf
```

A later `vaf update` does not bring them back: on a server it installs
`requirements-headless.txt`.

The system packages are shared with the rest of the machine, so let apt decide which of them
nothing else needs. On Ubuntu, mark them as no longer wanted and let `autoremove` take only
those that nothing installed still depends on:

```bash
sudo apt-mark auto portaudio19-dev gir1.2-ayatanaappindicator3-0.1 gir1.2-webkit2-4.1 \
    libgirepository1.0-dev libcairo2-dev pocketsphinx-en-us
sudo apt-get autoremove
```

`python3-gi` is left out on purpose: Ubuntu Server's own packages depend on it, and removing
it takes the `ubuntu-server` package with it. `autoremove` also takes any other package that
nothing needs any more, not only these, so look first with `sudo apt-get -s autoremove` and
read the list apt prints before you confirm. Measured on a fresh Ubuntu 24.04 server with
these packages added: 156 packages go, and `ubuntu-server`, `python3-gi`,
`unattended-upgrades`, `openssh-server` and `ufw` stay.

## On a rented server (VPS)

A server you rent from a hosting provider differs from a home server in two ways that
decide how it is set up: it usually has no GPU and little memory, and it sits on the
internet rather than behind a router. The setup that fits both:

1. **Install without local models.** `./install.sh --server --profile api` installs the
   service and leaves out the chat model and the speech containers
   ([INSTALL_PROFILES.md](INSTALL_PROFILES.md)): about 4 GB of container memory limits
   instead of 8, and no 7.5 GB speech-to-text image on a small disk. Set the chat
   provider and, if you use speech, the speech providers under Settings after the first
   login; `vaf profile` names anything still missing.
2. **Reach it over a VPN, never over the internet.** A rented server has a public address,
   and VAF's access port must not answer on it: an agent's login page on the internet is
   attacked around the clock, and behind it are your credentials, your messages and an agent
   that can act for you. Putting it there is close to negligent. VAF refuses public addresses
   itself and starts such a server with "VPN only" on, but the first barrier is the
   provider's firewall:
   1. In the provider's firewall, open only SSH and the VPN's own port (WireGuard's UDP
      port, for one); keep the access port closed.
   2. Set up the VPN (WireGuard, Tailscale or NetBird). Under "VPN only" each is admitted
      as soon as its interface is up, a mesh VPN in `100.64.0.0/10` included; with "VPN
      only" off, a mesh VPN is admitted with `vaf server networks tailscale on`.
   3. Restart VAF (`systemctl --user restart vaf`) so the certificate names the VPN address,
      and if this machine's own firewall is on, run `sudo -v && vaf server provision` so it
      opens the port for the VPN.
   4. `vaf server status` lists the address to open in a browser on a device in the VPN.
   5. Create the first admin with `vaf setup` over SSH, or in that browser with the code
      `vaf setup code` prints.

   A server behind 1:1 NAT (several clouds) sees only a private address, so VAF cannot tell
   it is on the internet: run `vaf server vpn-only on` there yourself. `vaf doctor` warns
   while the machine has a public address, when ufw is installed but switched off, and when
   public addresses reached the port. See [Over a VPN](#over-a-vpn).
3. **Back it up off the machine.** `vaf server provision` schedules a daily encrypted
   backup with a retention (`vaf backup schedule` changes it), but a backup on the server's
   own disk is lost with the server: copy them elsewhere
   ([BACKUP_RESTORE.md](BACKUP_RESTORE.md#backups-on-a-schedule)).
4. **Watch it.** Point a monitor (Uptime Kuma, a cron job, the hosting provider's own) at
   `https://<address>:<port>/api/health` over the VPN: it needs no account, answers
   `{"status": "ok"}`, `"degraded"` or `"down"`, and HTTP 503 when down. `vaf health` on
   the server prints the checks behind the word and exits 0, 1 or 2
   ([DOCKER_SERVICES.md](DOCKER_SERVICES.md#health)).

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
signal available to an unprivileged process. Below the services, a `sandbox` row
says how many sandbox environments run and are stopped (idle while none exists).

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

VAF listens on `https://<LAN-IP>:8443` in the usual case: the configured HTTPS port is 443, and an unprivileged service cannot bind it, so the proxy falls back to 8443. When 443 is bindable (root, `CAP_NET_BIND_SERVICE`), the address is plain `https://<LAN-IP>`. `vaf server status` lists the addresses to open, one per admitted LAN and VPN interface; it never lists a public address, and warns when the machine has one.

**Private networks only.** The access port answers this machine, your local network and the VPNs you admit, and refuses every other address before any page. Never forward it on your router or publish it through a tunnel service.

**TLS certificate:** VAF issues its own certificate on first start, signed by a local certificate authority it creates in `~/.vaf/ssl/`. A device that does not trust that authority warns on the first visit. Do not click the warning away: install the authority on the device (Settings, Local Network, Certificate, "Download", or `~/.vaf/ssl/ca.pem`) after comparing its fingerprint with the one `vaf server ca` prints on the server ([NETWORK_FEATURES.md](NETWORK_FEATURES.md#eliminating-browser-warnings)). A certificate of your own (a company authority, Let's Encrypt): `vaf server cert set --cert FILE --key FILE`, checked first and served without a restart ([NETWORK_FEATURES.md](NETWORK_FEATURES.md#your-own-certificate)).

**Authentication:** All access (local and LAN) requires login. Credentials are set during the initial setup wizard at `https://<LAN-IP>:8443`, which first asks for the setup code that `vaf setup code` prints in an SSH session on the server (the browser is not on the machine itself, see [FIRST_RUN.md](FIRST_RUN.md#1-create-admin-account)); `vaf setup` creates the admin in the terminal instead. Passwords need 12 characters and may not be a common one, and every account sets up its second factor at its first sign-in; there is no switch for either. A script or a program on another machine uses a token for programs (Settings, Account, or `vaf users token NAME --label TEXT` over SSH), and Settings, Account lists every signed-in device ([NETWORK_FEATURES.md](NETWORK_FEATURES.md#authentication-details)).

**Ports:**

| Port | Purpose |
|------|---------|
| 8443 | HTTPS proxy (LAN access, TLS) - effective port; 8443 only when 443 falls back |
| 3000 | Next.js frontend (internal, localhost only) |
| 8001 | FastAPI backend (internal, localhost only) |
| 8005 | Internal plain-HTTP backend channel (internal, localhost only) |
| 8080 | llama-server LLM backend (internal, localhost only) |

Only the HTTPS proxy access port (8443 after a 443 fallback) is exposed on the network interface. All other ports are bound to `127.0.0.1`, apart from the sandbox's tool bridge, which listens on a random port for the length of a `python_sandbox` run that calls VAF's tools, guarded by a token per run (a named boundary in [NETWORK_FEATURES.md](NETWORK_FEATURES.md#scope-private-networks-only)).

## Over a VPN

A server that should be reached from outside the home or office network is reached
through a VPN, not by opening the access port to the internet. VAF runs no VPN server
itself; it recognises the VPN interface and admits its devices
([NETWORK_FEATURES.md](NETWORK_FEATURES.md#remote-access-over-a-vpn)).

- **WireGuard or OpenVPN** (addresses in `10.x`): admitted by default, nothing to set.
  `vaf server status` lists the interface with its access URL, and the certificate
  carries its address after the next start.
- **Tailscale, Headscale, NetBird** (addresses in `100.64.0.0/10`): admit them once with
  `vaf server networks tailscale on`. Under "VPN only" they are admitted as a detected VPN
  already, nothing to set.
- **Another private network** (a routed site network, a WireGuard set up with `/32`
  addresses): `vaf server networks allow 10.20.0.0/16`. Public networks, `0.0.0.0/0` and
  IPv6 are refused with the reason; `vaf server networks list` shows what is admitted
  and what is ignored, `vaf server networks remove` drops an entry.
- **Only the VPN**: `vaf server vpn-only on` locks the local network out and admits the
  networks of the detected VPN interfaces (plus your entries). With no VPN up, nobody
  but this machine gets in; the command says so, and so do `vaf server status` and
  `vaf doctor`. A server with a public address starts this way (see above).

**Opening it.** Open the web UI in a browser on a device in the VPN, at the address
`vaf server status` lists. The VAF desktop window shows the VAF on its own machine, not a
server; Chrome, Edge and Safari can open the site as an app in a window of its own
(install the page as an app, or add it to the Dock).

The access check takes a change at once, with no restart. The running service does not
change the OS firewall: it cannot gain root, not even with passwordless sudo (its systemd
hardening rules every setuid helper out, see Troubleshooting below). If this machine's own
firewall guards the access port, run `sudo -v && vaf server provision` after the change, or
open the port by hand for the networks `vaf server status` lists; `vaf server networks` and
the web settings say so after every change. On a rented server keep the access port closed
in the provider's firewall and open only the VPN's own port there; then run
`vaf server provision --no-firewall`, since the provider's firewall is the one that faces
the internet, and nothing more is needed after an admission change.

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

A server install left out the tray icon, the desktop window and the terminal microphone
([above](#what-a-server-install-leaves-out)); run `./install.sh --desktop` to install them.

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
- Check firewall state: `sudo firewall-cmd --list-rich-rules` (firewalld) or `sudo ufw status`, which must say `Status: active`; rules VAF adds to a switched-off ufw do nothing, so it adds none there and `vaf server provision` says so
- VAF also configures the OS firewall at runtime via `vaf/network/firewall.py`, but only where the running process can gain root. On Linux it prefers firewalld when it is running and opens **only** the access port for the LAN subnet (a scoped rich rule), not a blanket world-open. iptables/ufw are used as a fallback when firewalld is not running.
- Elevation differs by environment:
  - **Desktop session:** when hosting is enabled VAF prompts automatically through a native polkit/pkexec password dialog and adds the rule for you.
  - **Headless/server:** the running service cannot elevate at all, so it does not try. Each of the unit's hardening lines (`NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=full`) on its own rules out every setuid helper, sudo and pkexec alike, whatever sudoers says; `vaf.network.firewall.elevation_unavailable()` reads that from the process itself. At each start the service logs one line instead, `Firewall not changed for port 8443: this process cannot gain root (...)`, and never runs sudo. That is exactly why the installer and `vaf server provision` own this step.
- Manual fallback, firewalld (preferred subnet-scoped rich rule form; replace `<LAN-subnet>` with your network, e.g. `192.168.2.0`):
  ```bash
  sudo firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="<LAN-subnet>/24" port port="8443" protocol="tcp" accept' && sudo firewall-cmd --reload
  ```
- Ubuntu (ufw fallback), one rule per network `vaf server status` admits, never a world-open `allow 8443/tcp`: `sudo ufw allow from <network> to any port 8443 proto tcp`. Switching ufw on needs SSH and the VPN allowed first: `sudo ufw allow OpenSSH` when SSH listens on port 22 (that profile opens 22 only), else `sudo ufw allow <port>/tcp`, and the VPN's own port (WireGuard: `sudo ufw allow 51820/udp`, or the ListenPort its configuration names; another VPN: its own port); then `sudo ufw enable`. `vaf server provision` and `vaf doctor` name the SSH port the session came in on. A ufw whose unit was stopped (`systemctl disable --now ufw`) counts as off as well, since the stop flushed its rules. It needs two more steps, and the advice names them: `sudo ufw disable` before the allows, because its switch still says on and `ufw allow` fails in that state, and `sudo systemctl enable --now ufw` after `sudo ufw enable`, which never starts the unit that loads the rules at boot. With `MANAGE_BUILTINS=yes` in `/etc/default/ufw` or `/etc/ufw/ufw.conf` (Ubuntu ships `no`; ufw.conf is read last and wins), `ufw disable` and `ufw enable` also flush every other iptables rule, Docker's included, so the advice then adds `sudo systemctl restart docker`.
- Without firewalld and ufw, VAF adds iptables rules, which last until the next reboot: the service cannot gain root to add them again, so after a reboot run `sudo -v && vaf server provision`.

**Certificate regeneration:**
VAF's own certificate is issued again at a start that finds it with under 30 days left or
missing a current address. While VAF's own certificate is in use, this issues a new one at
once, without a restart:
```bash
vaf server cert reset
```
It comes from the same authority, so devices that installed it keep trusting the server.
Do not delete `~/.vaf/ssl/`: that also replaces the authority every device trusts. A
certificate of your own is never replaced, and `vaf server cert reset` would switch to VAF's
own (another authority for your devices): renew yours and install it with
`vaf server cert set`; `vaf doctor` warns 30 days before it expires.

**LAN IP changed (DHCP):**
The access URL, the certificate's IP SANs and the subnet-scoped firewall rule are all bound to the address. Give the server a static LAN IP, or reserve its address in the router's DHCP settings; `vaf server provision` warns during installation when the address looks DHCP-assigned. After an IP change: with VAF's own certificate, restart VAF or run `vaf server cert reset` so the certificate names the new address (see above); with a certificate of your own, get one that names the new address (or reach the server by a name it carries) and install it with `vaf server cert set`. Then re-run `vaf server provision` so the firewall rule matches the new subnet. Alternatively use a name instead of the IP: VAF's own certificate carries `<host>.local` (the mDNS name, where Avahi announces it) and the machine's hostname and FQDN as DNS SANs when they lie under a name its authority may vouch for (`local`, `home.arpa`, `internal`, `lan`; an authority created before that restriction carries them all), so such a name that resolves on your network keeps working across IP changes (note the certificate is NOT re-issued when the hostname itself changes - only IP changes trigger regeneration).
