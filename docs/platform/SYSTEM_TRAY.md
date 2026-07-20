# System Tray & Desktop Window

VAF includes a persistent background service managed by a system tray application and a native desktop window. This allows for instant agent availability, dynamic resource management, and a native desktop experience — without requiring the user to open a browser manually.

## Features

- **Native Desktop Window**: VAF opens in its own dedicated window (no browser tab needed), powered by a `pywebview` WebView.
    - Windows: Edge/WebView2 (Chromium-based)
    - macOS: WKWebView (Safari engine)
    - Linux: **PySide6 / Qt WebEngine (Chromium)** — VAF forces `QT_QPA_PLATFORM=xcb` and the Qt backend (not WebKitGTK) and tunes Chromium via `QTWEBENGINE_CHROMIUM_FLAGS` in `vaf/core/desktop_window.py`. See the rendering/memory note below.
- **Persistent Tray Icon**: A system tray icon on all platforms indicates the server state.
    - **Green / Active**: Server is running, model is loaded into RAM.
    - **Yellow / Idle**: Server is standing by, model is unloaded (saves RAM).
    - **Blue / Persistent**: Model is pinned in RAM (Persistent Mode).
- **Window Minimize to Tray**: Closing the window hides it — the app stays running in the system tray. Click "Open VAF" to bring the window back.
- **Dynamic Resource Management**: Automatically unloads the LLM from RAM after 15 seconds (default) of inactivity to free up system resources. **Never while work is in flight**: the idle check also asks whether the machine is currently busy on the user's behalf (a chat turn running or queued, a sub-agent working, an open voice call), and that term is independent of user activity. Its other inputs describe the USER, not the machine - "is a browser attached" and "has the user typed lately" - and on 2026-07-20 a long tool call was unloaded out from under itself because the user had simply been quiet for a while. The probe fails towards KEEPING the model: a needlessly warm model costs VRAM until the next check, a wrong unload destroys work in progress.
- **WebUI-Aware Idle**: The local model unloads after 15 seconds with no active WebUI WebSocket connections (unless persistence is enabled).
- **Instant Wake-on-Demand**: The server wakes up instantly when you run a CLI command (`vaf run`) or open the Web UI.
- **Graceful Shutdown**: Checks for active CLI sessions before quitting to prevent data loss.
- **Single HTTP Backend**: The tray manages a single `llama-server` on `127.0.0.1:8080`. Other components reuse it instead of spawning duplicates.
- **Hot-Reload Settings**: Changing `n_ctx` (context size) or `gpu_layers` in the Web UI settings automatically restarts the `llama-server` with the new values — no full app restart required.
- **Provider Switch**: Switching provider triggers backend config reload (`RELOAD_CONFIG`) and updates local/API execution mode. The tray's activity loop also reads the provider live and manages the local model: switching to a cloud/API provider **unloads** the local model immediately (frees RAM/VRAM), switching back to local **(re)loads** it — no waiting for the idle window.

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
| `provider` | Marks config refresh (`requires_refresh`) and triggers backend `RELOAD_CONFIG`. The activity loop (not `on_config_changed`) then unloads the local model on a cloud/API provider and (re)loads it on switch back to local. |

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
- **Native download/print dialogs** (Qt backend): `_install_download_print_handlers()` wires the embedded QtWebEngine view so WebUI actions get native dialogs — `downloadRequested` opens a save dialog (file downloads and blob exports like Save-as-PDF), `printRequested`/`printRequestedByFrame` open a save-as-PDF dialog and render via `printToPdf()`. The frame variant matters: `window.print()` inside an iframe (Document Editor, research report print) prints the frame's content, not the app shell. pywebview's own download handler is not used — it calls the Qt5-only `download.setPath()`, which does not exist on PySide6/Qt6.
- **Thread model**:
  - Main thread → `webview.start()` (pywebview GUI loop)
  - Background threads → pystray tray icon, uvicorn backend, Next.js frontend, agent loop
- **HTTP Backend**: The tray starts and unloads the local backend; health checks reuse an existing backend to avoid multiple processes.

### Linux system dependencies

The desktop window on Linux uses **Qt WebEngine (Chromium-based)** via **PySide6** (LGPLv3, installed automatically via `requirements.txt` on Linux; PySide6 bundles Qt WebEngine). `vaf/core/desktop_window.py` sets `QT_API=pyside6`. This gives smooth, Chrome-like rendering with GPU acceleration.

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

The first two are set together by `force_x11()` in `vaf/core/display_platform.py` and
**override** a Wayland session's own `QT_QPA_PLATFORM` (KDE/GNOME export it, so the earlier
`setdefault` never applied). They are skipped when XWayland is missing (no `DISPLAY`) or
when `VAF_ALLOW_WAYLAND=1` is set; the choice is logged as `display platform: ...`.
See [LINUX.md](LINUX.md).

**GPU acceleration** is on (Chromium-based Qt WebEngine). The exact
`QTWEBENGINE_CHROMIUM_FLAGS` live in `vaf/core/desktop_window.py` and are tuned to
avoid the in-process-GPU memory leak: vsync deliberately stays **on**
(`--disable-frame-rate-limit` / `--disable-gpu-vsync` are **not** set) and
`--enable-accelerated-2d-canvas` is avoided. See the Anti-Leak Notes below for the
rationale and the full flag list.

If PySide6 is not installed, VAF falls back to browser-only mode (tray icon still works).

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
| **Initialization** | pystray runs detached; pywebview owns the main thread. This is exactly why the menu-bar icon fails when VAF is launched as an `.app` bundle — see *Menu-bar tray & bundle launch* below. |
| **Signals** | Handle `SIGTERM`, `SIGINT`, `SIGHUP` for clean shutdown (Dock Quit, Cmd+Q). |
| **Icon** | 44×44 recommended for Retina; macOS downscales as needed. |

#### Menu-bar tray & bundle launch (Spotlight / Dock)

**`pystray`'s menu-bar icon only appears reliably when VAF is launched from a
terminal — NOT when launched as an `.app` bundle** (Spotlight / Dock / `open`).
Symptom: bundle-launched VAF has a window but no tray icon, so there is no way to
quit it from the UI.

Root cause: on macOS the status item (`NSStatusItem`) must be created on the
**main thread**. Since commit `d4e8dbd` (which added the native desktop window)
pywebview owns the main thread, so pystray runs **detached** (background thread).
A detached status item still registers when the process is a plain terminal
child, but a **bundle**-launched process binds to the `.app`'s NSApplication
(pywebview's) and the detached icon never shows. Before `d4e8dbd`, VAF was
browser-only and pystray ran on the main thread via `icon.run()`, so the bundled
app worked — this is a regression introduced by the desktop window.

The fundamental constraint: **one Python process cannot give the macOS main
thread to BOTH the native window (pywebview) AND the menu-bar tray (pystray).**

**Fix in use — Terminal hand-off:** `VAF.app` does not run VAF directly. Its
launcher (generated by `scripts/create_app_shortcut.py`) hands the run off to
**Terminal** via `osascript` (`do script "cd <vaf> && ./run_vaf.sh tray"`),
minimises the Terminal window, and the `.app` exits. VAF then runs in the
working terminal context → menu-bar tray **+ native window + splash + working
Quit**, all via Spotlight. Notes:
- First launch shows a one-time macOS prompt: *"VAF" wants to control "Terminal"*
  (Automation) — the user must approve it once.
- The minimised Terminal window keeps VAF alive; closing it hard-kills VAF. The
  clean way to quit is the tray **Quit**.

**Alternative (built, not wired into the installer):** the native Swift menu-bar
app `scripts/macos/VAFTray` (+ `scripts/macos/build_app.sh`) owns the main thread
for a real `NSStatusBar` tray and runs VAF headless (`VAF_NATIVE_WRAPPER=1`).
Trade-off: it opens the Web UI in the **browser** — no pywebview window / splash.
`setup_mac.sh` currently calls `create_app_shortcut.py` (the Terminal hand-off),
not `build_app.sh`.

### Cross-platform

- Prefer `vaf.core.platform.Platform` helpers for OS checks and paths (instead of direct `platform.system()` checks in new code).
- Tray callbacks accept `(icon, item)` (pystray convention) on all platforms.
- Desktop window API: `vaf.core.desktop_window` — `init()`, `start()`, `show()`, `hide()`, `navigate()`, `destroy()`.

## Rendering & Memory (Linux / Qt WebEngine) — Anti-Leak Notes

On Linux the desktop window is **Qt WebEngine (Chromium)** running with the **GPU in-process**.
In that configuration a continuously *repainting* UI animation leaks GPU tile/texture memory
into the **renderer** process — the JS heap stays tiny (~10 MB) while the OS RSS of
`QtWebEngineProcess` climbs unbounded. A May-2026 investigation traced RSS from ~0.9 GB to
**7 GB** to exactly these causes; the rules below keep it bounded (~1.5 GB, self-reclaiming):

- **Chromium flags** (`QTWEBENGINE_CHROMIUM_FLAGS` in `desktop_window.py`):
  - Do **not** add `--disable-frame-rate-limit` or `--disable-gpu-vsync`. Uncapping the
    framerate (especially on a large high-Hz display, e.g. 5120×1440 @ 240 Hz) makes the
    in-process GPU pile up tiles at ~40 MB/s. Keep vsync on + the default ~60 fps cap.
  - Do **not** add `--enable-accelerated-2d-canvas` (GPU-backs `<canvas>` buffers).
  - Keep `--aggressive-cache-discard`, `--renderer-process-limit=1`, `--disk-cache-size`,
    `--js-flags=--max-old-space-size=1024` are kept for RAM containment.
- **Frontend animation rule:** continuously-running (infinite) animations may animate
  **only `transform` / `opacity`** (compositor-only). Never continuously animate
  `border-radius`, `filter`/`blur`, `box-shadow`, `width`/`height`, or paint with a
  full-screen `<canvas>`. See `web/components/CustomCursor.tsx`, `web/app/globals.css`
  (avatar keyframes), and `docs/web-ui/AgentAvatar.md`.
- **Trade-off:** true high-refresh (120/240 Hz) is only reachable via the leaking
  framerate flag on this engine, so the app intentionally runs vsync-capped.
- **Re-diagnosing:** a disabled, opt-in memory logger lives in `desktop_window.py`.
  Uncomment the `_start_mem_logger()` call, restart, and read `logs/leak_diag_<date>.log`
  (columns: total / gpu / renderer / jsHeap / domNodes RSS over time).
