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
| 8443 | HTTPS proxy (LAN access, TLS) |
| 3000 | Next.js frontend (internal, localhost only) |
| 8001 | FastAPI backend (internal, localhost only) |
| 8080 | llama-server LLM backend (internal, localhost only) |

Only port 8443 is exposed on the network interface. All other ports are bound to `127.0.0.1`.

## Locked Settings

In server mode, the following config keys are locked and cannot be changed via the Settings UI or the API:

- `local_network_enabled` — always `true`
- `local_network_tls_enabled` — always `true`
- `server_mode` — always `true`

Attempts to write these keys via `PATCH /api/config` are silently ignored.

To change them you must edit `~/.vaf/config.json` directly and restart the service.

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
- Check firewall: `sudo firewall-cmd --list-ports` (firewalld) or `sudo ufw status`
- VAF uses its own firewall rules via `vaf/network/firewall.py` but these require the OS firewall to allow the port through.
- OpenSUSE/Fedora: `sudo firewall-cmd --add-port=8443/tcp --permanent && sudo firewall-cmd --reload`
- Ubuntu: `sudo ufw allow 8443/tcp`

**Certificate regeneration:**
If the TLS certificate has expired or the LAN IP changed:
```bash
rm -rf ~/.vaf/ssl/
systemctl --user restart vaf
```
VAF regenerates the certificate on the next start.

**LAN IP changed (DHCP):**
Set a static LAN IP on the server, or use the hostname instead of the IP address.
