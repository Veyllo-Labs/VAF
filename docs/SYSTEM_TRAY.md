# System Tray & Persistent Server

VAF includes a persistent background service managed by a system tray application. This allows for instant agent availability, dynamic resource management, and a native desktop experience.

## Features

- **Persistent Icon**: A system tray (Windows/Linux) or menu bar (macOS) icon indicates the server state.
    - 🟢 **Green / Active**: Server is running, model is loaded into RAM.
    - 🟡 **Yellow / Idle**: Server is standing by, model is unloaded (saves RAM).
    - 🔵 **Blue / Persistent**: Model is pinned in RAM (Persistent Mode).
- **macOS Dock Integration**: On macOS, clicking the Dock icon (when VAF is already running) focuses the app and opens/re-activates the Web UI.
- **Smart Tab Reuse**: If a VAF tab is already open in Safari or Google Chrome, clicking the Dock icon will re-focus that existing tab instead of opening a new one.
- **Dynamic Resource Management**: Automatically unloads the LLM from RAM after 15 seconds (default) of inactivity to free up system resources.
- **WebUI-Aware Idle**: The local model unloads after 15 seconds with no active WebUI WebSocket connections (unless persistence is enabled).
- **Instant Wake-on-Demand**: The server wakes up instantly when you run a CLI command (`vaf run`) or open the Web UI.
- **Graceful Shutdown**: Checks for active CLI sessions before quitting to prevent data loss.
- **Single HTTP Backend**: The tray manages a single `llama-server` on `127.0.0.1:8080`. Other components reuse it instead of spawning duplicates.
- **Hot-Reload Settings**: Changing `n_ctx` (context size) or `gpu_layers` in the Web UI settings automatically restarts the `llama-server` with the new values — no full app restart required.
- **Provider Switch**: Switching the provider in settings (Local ↔ API) immediately unloads the local model from VRAM (Local → API) or loads it on demand (API → Local).

## Usage

### Starting the Tray App

**macOS (Recommended):**
1. Open **Spotlight** (Cmd + Space).
2. Type `VAF` and press Enter.
3. VAF will appear in your **Dock** and **Menu Bar**.

**CLI (All Platforms):**
```bash
vaf tray
```

### Menu Options

- **Open WebUI**: Opens the VAF dashboard in your default browser.
- **Persistent Server**: Toggle this to keep the model loaded in RAM even when idle (useful for frequent interaction).
- **Quit**: Stops the server and exits the application.

### CLI Integration

When the tray app is running, `vaf run` automatically detects it and uses the shared server instance instead of spinning up a separate one. This drastically reduces startup time for new sessions.

## Configuration

Settings are managed in `~/.vaf/config.json`:

| Setting | Default | Description |
| :--- | :--- | :--- |
| `server_idle_timeout` | `15` | Seconds to wait before unloading model |
| `persist_server` | `false` | If true, model stays loaded (same as checkbox) |
| `tray_autostart` | `false` | Auto-start tray app on OS login |
| `n_ctx` | `8192` | Context window size (tokens). **Hot-reloaded**: changing this in Settings auto-restarts the `llama-server`. |
| `gpu_layers` | `-1` | Number of layers offloaded to GPU (`-1` = all). **Hot-reloaded**: changing this in Settings auto-restarts the `llama-server`. |
| `llama_cache_ram` | `4096` | Prompt cache size in MB for the local llama-server. Set to `0` to disable caching; set to `-1` to use 40% of free system RAM (capped at 8192 MB). Takes effect after the next server start. |

### Hot-Reload Behavior

Certain settings trigger an automatic server restart when changed in the Web UI:

| Setting | Effect |
| :--- | :--- |
| `n_ctx`, `gpu_layers` | Stops and restarts `llama-server` with new values (local provider only). |
| `local_network_enabled`, `local_network_port`, `local_network_port_frontend` | Restarts both uvicorn backend and Next.js frontend with new network binding. |
| `provider` | Switches between local model and API backend; unloads/loads VRAM as needed. |

You can also toggle local network hosting and SSL from the CLI (no UI needed):

| Command | Effect |
| :--- | :--- |
| `vaf server on` | Enable local network (bind to 0.0.0.0) and turn on SSL/TLS (HTTPS). Tray restarts backend and frontend automatically. |
| `vaf server off` | Disable local network (back to 127.0.0.1) and SSL. Tray restarts with localhost-only binding. |
| `vaf server status` | Show whether hosting is enabled, SSL state, backend port, and the network URLs (e.g. https://192.168.x.x:3000) for other devices. |

This is implemented via the Config observer pattern: `Config.save()` detects changes to critical keys and notifies `on_config_changed()` in `tray.py`, which performs the restart in a background thread.

## Architecture

The system uses a shared `TrayContext` to manage state between the Uvicorn web server and the UI loop.
- **macOS**: Uses `rumps` for a native Cocoa menu bar experience.
- **Windows/Linux**: Uses `pystray` for cross-platform system tray support.
- **HTTP Backend**: The tray starts and unloads the local backend; health checks reuse an existing backend to avoid multiple processes.

## Platform Implementation Notes

When modifying the tray or adding platform-specific logic, observe these differences:

### Windows

| Aspect | Requirement |
|--------|-------------|
| **Icon display** | Do not set `icon.visible = True` before `icon.run()`. Use `icon.run(setup=lambda i: setattr(i, "visible", True))` so the icon is shown only after the event loop is ready. |
| **Icon size** | Taskbar expects 16×16 or 32×32; larger icons may render poorly or not appear. |
| **Subprocesses** | Use `getattr(subprocess, "CREATE_NO_WINDOW", 0)` for background processes (pythonw has no console). |
| **Open URL** | Prefer `os.startfile(url)`; fall back to `cmd /c start`. |
| **Singleton** | Do not use `SO_REUSEADDR` for the singleton socket; bind must fail if another instance runs. |

### macOS

| Aspect | Requirement |
|--------|-------------|
| **Tray library** | `rumps` (not pystray). Requires Cocoa RunLoop on main thread. |
| **Initialization** | Start backend/frontend threads only after rumps RunLoop is active (e.g. via `threading.Timer` delayed init). |
| **Signals** | Handle `SIGTERM`, `SIGINT`, `SIGHUP` for clean shutdown (Dock Quit, Cmd+Q). |
| **Icon** | 44×44 recommended for Retina; macOS downscales as needed. |

### Cross-platform

- Use `platform.system() == "Darwin"` for macOS, `== "Windows"` for Windows.
- Callbacks (e.g. pystray `checked`): accept `(icon, item)`; Rumps passes `(sender)`—handle both.
