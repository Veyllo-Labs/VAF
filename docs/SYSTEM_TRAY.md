# System Tray & Desktop Window

VAF includes a persistent background service managed by a system tray application and a native desktop window. This allows for instant agent availability, dynamic resource management, and a native desktop experience — without requiring the user to open a browser manually.

## Features

- **Native Desktop Window**: VAF opens in its own dedicated window (no browser tab needed), powered by the system's native WebView engine.
    - Windows: Edge/WebView2 (Chromium-based)
    - macOS: WKWebView (Safari engine)
    - Linux: WebKitGTK
- **Persistent Tray Icon**: A system tray icon on all platforms indicates the server state.
    - 🟢 **Green / Active**: Server is running, model is loaded into RAM.
    - 🟡 **Yellow / Idle**: Server is standing by, model is unloaded (saves RAM).
    - 🔵 **Blue / Persistent**: Model is pinned in RAM (Persistent Mode).
- **Window Minimize to Tray**: Closing the window hides it — the app stays running in the system tray. Click "Open VAF" to bring the window back.
- **Dynamic Resource Management**: Automatically unloads the LLM from RAM after 15 seconds (default) of inactivity to free up system resources.
- **WebUI-Aware Idle**: The local model unloads after 15 seconds with no active WebUI WebSocket connections (unless persistence is enabled).
- **Instant Wake-on-Demand**: The server wakes up instantly when you run a CLI command (`vaf run`) or open the Web UI.
- **Graceful Shutdown**: Checks for active CLI sessions before quitting to prevent data loss.
- **Single HTTP Backend**: The tray manages a single `llama-server` on `127.0.0.1:8080`. Other components reuse it instead of spawning duplicates.
- **Hot-Reload Settings**: Changing `n_ctx` (context size) or `gpu_layers` in the Web UI settings automatically restarts the `llama-server` with the new values — no full app restart required.
- **Provider Switch**: Switching provider triggers backend config reload (`RELOAD_CONFIG`) and updates local/API execution mode; local server lifecycle then follows normal tray/runtime management.

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

- **Open VAF**: Shows the VAF desktop window (brings it to the front if already open).
- **Persistent Server**: Toggle this to keep the model loaded in RAM even when idle (useful for frequent interaction).
- **Quit**: Stops the server, closes the window, and exits the application.

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
| `provider` | Marks config refresh (`requires_refresh`) and triggers backend `RELOAD_CONFIG` path. |

You can also toggle local network hosting and SSL from the CLI (no UI needed):

| Command | Effect |
| :--- | :--- |
| `vaf server on` | Enable local network + TLS/HTTPS entrypoint. Tray restarts backend and frontend automatically. |
| `vaf server off` | Disable local network (back to 127.0.0.1) and SSL. Tray restarts with localhost-only binding. |
| `vaf server status` | Show whether hosting is enabled, SSL state, backend port, and the network URLs (e.g. https://192.168.x.x:3000) for other devices. |

This is implemented via the Config observer pattern: `Config.save()` detects changes to critical keys and notifies `on_config_changed()` in `tray.py`, which performs the restart in a background thread.

## Architecture

The system uses a shared `TrayContext` to manage state between the Uvicorn web server and the UI loop.

- **All platforms**: Uses `pystray` for the system tray icon (runs in a background thread via `icon.run_detached()`).
- **Desktop window**: `pywebview` (`vaf/core/desktop_window.py`) creates a native WebView window that owns the main thread (`webview.start()` blocks). Closing the window hides it; Quit destroys it and exits. Login sessions and localStorage are **persisted** across restarts (`private_mode=False`, storage in `.vaf_webview/`).
- **Thread model**:
  - Main thread → `webview.start()` (pywebview GUI loop)
  - Background threads → pystray tray icon, uvicorn backend, Next.js frontend, agent loop
- **HTTP Backend**: The tray starts and unloads the local backend; health checks reuse an existing backend to avoid multiple processes.

### Linux system dependencies

The desktop window on Linux uses **Qt WebEngine (Chromium-based)** via `PyQt6-WebEngine` (installed automatically via `requirements.txt`). This gives smooth, Chrome-like rendering with full GPU acceleration.

System packages required:

```bash
# OpenSUSE
sudo zypper install typelib-1_0-AyatanaAppIndicator3-0_1 typelib-1_0-WebKit2-4_1 libwebkit2gtk-4_1-0

# Debian / Ubuntu
sudo apt install python3-gi gir1.2-webkit2-4.0 gir1.2-ayatanaappindicator3-0.1

# Fedora
sudo dnf install python3-gobject webkit2gtk4.0

# Arch
sudo pacman -S python-gobject webkit2gtk
```

VAF sets the following environment variables automatically on startup:
- `GDK_BACKEND=x11` — forces GTK (pystray) to use X11/XWayland
- `QT_QPA_PLATFORM=xcb` — forces Qt WebEngine to use X11/XWayland (avoids EGL/GLX conflict)
- `WEBKIT_DISABLE_DMABUF_RENDERER=1` — avoids GBM buffer errors under XWayland

**GPU acceleration** is enabled automatically via Chromium flags:
```
--disable-frame-rate-limit --disable-gpu-vsync --enable-gpu-rasterization
--enable-accelerated-2d-canvas --num-raster-threads=4
```

If PyQt6-WebEngine is not installed, VAF falls back to browser-only mode (tray icon still works).

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
| **Tray library** | `pystray` (same as Windows/Linux). `rumps` was removed — it conflicts with pywebview for main-thread ownership. |
| **Initialization** | pystray runs detached; pywebview owns the main thread. No delayed-init workaround needed. |
| **Signals** | Handle `SIGTERM`, `SIGINT`, `SIGHUP` for clean shutdown (Dock Quit, Cmd+Q). |
| **Icon** | 44×44 recommended for Retina; macOS downscales as needed. |

### Cross-platform

- Prefer `vaf.core.platform.Platform` helpers for OS checks and paths (instead of direct `platform.system()` checks in new code).
- Tray callbacks accept `(icon, item)` (pystray convention) on all platforms.
- Desktop window API: `vaf.core.desktop_window` — `init()`, `start()`, `show()`, `hide()`, `navigate()`, `destroy()`.
