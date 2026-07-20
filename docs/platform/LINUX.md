# VAF on Linux

This document explains how VAF **runs** on Linux — the runtime stack and the
platform-specific behavior and fixes that are unique to it. For step-by-step
installation, see [LINUX_SETUP.md](../setup/LINUX_SETUP.md); this page does not
duplicate the install steps.

## Runtime stack on Linux

- **Python.** VAF runs from a project-local virtualenv at `venv/`, launched as
  `venv/bin/python -m vaf.main`. The installer requires Python 3.10-3.13 (the
  CI-tested range; newer interpreters are rejected because brand-new releases
  lack prebuilt wheels for key packages) and provisions one via
  [uv](https://docs.astral.sh/uv/) when the system Python is missing or outside
  that range (no system Python needed). `PyGObject` is pinned to `<3.52` in the venv because 3.52+
  needs `girepository-2.0`, which older distros (e.g. Ubuntu 24.04) do not ship.
- **Desktop window / web-view backend.** The native window is **pywebview on the
  Qt / QtWebEngine (Chromium) backend** — it is a Qt window, **not** GTK.
  `requirements.txt` pins `PySide6>=6.7.0` and `qtpy>=2.0.0` for
  `sys_platform == "linux"` (see also the `desktop` extra in
  [setup.py](../../setup.py)). PySide6 (LGPLv3) is chosen over PyQt6 (GPLv3) so
  the Qt binding stays dynamically linked / user-replaceable;
  `vaf/core/desktop_window.py` sets `QT_API=pyside6` so both VAF and pywebview's
  Qt backend select PySide6. PySide6 bundles Qt WebEngine, so no separate
  Chromium is required.
- **System tray.** The tray icon uses `pystray` (same library as Windows/macOS),
  which draws through GTK/AppIndicator on Linux. `pystray` runs detached in a
  background thread while pywebview owns the main thread. The tray needs the
  AppIndicator + WebKitGTK typelibs (installed by the installer); if they are
  missing the tray icon may not appear, but the Qt app window is unaffected. See
  [SYSTEM_TRAY.md](./SYSTEM_TRAY.md).
- **Docker services.** The database (PostgreSQL/pgvector), cache, sandbox, STT,
  document engine, TTS and browser containers run via
  `docker compose -f docker-compose.memory.yml up -d`. `run_vaf.sh` starts them
  automatically when `docker` is on PATH. See
  [DOCKER_SERVICES.md](../setup/DOCKER_SERVICES.md).
- **Launcher / entry point.** [run_vaf.sh](../../run_vaf.sh) activates `venv/`,
  starts the Docker stack, and `exec`s `venv/bin/python -m vaf.main tray` (or
  passes through any `vaf` subcommand). The installer also creates a freedesktop
  launcher at `~/.local/share/applications/vaf.desktop` (Exec = `run_vaf.sh`,
  `Terminal=false`) and a `vaf` shell alias.

## Installing & launching

Install via [install.sh](../../install.sh) (details in
[LINUX_SETUP.md](../setup/LINUX_SETUP.md) and the general
[INSTALLATION_GUIDE.md](../setup/INSTALLATION_GUIDE.md)). Once installed you can
launch VAF three ways: type `vaf` in a terminal (the alias added to your shell
rc), run `./run_vaf.sh` directly, or start it from your application menu / desktop
via the generated **VAF** launcher. Any of these starts the tray plus the native
Qt desktop window; a bare `vaf` / `run_vaf.sh` with no arguments defaults to the
`tray` subcommand. Individual CLI subcommands work too, e.g. `vaf run` or
`vaf server on`.

## Platform-specific behavior & fixes

- **Qt window forced onto X11/XWayland.** Both `vaf/tray.py` and
  `vaf/core/desktop_window.py` call `vaf/core/display_platform.py` `force_x11()`,
  which sets `GDK_BACKEND=x11` (GTK/pystray) and `QT_QPA_PLATFORM=xcb` (Qt
  WebEngine) — always as a PAIR, since a mismatch is itself a GLX/EGL conflict.
  Native Wayland causes GTK protocol errors and an EGL/GLX conflict that makes
  `QWebEngineProfile` `qFatal()` on startup; with the GPU in-process it can also
  deadlock Chromium's compositor against the Qt scene graph (SIGABRT, incident
  2026-07-20).
  - It **overrides** a value the session exported. This is deliberate: KDE and
    GNOME Wayland sessions export `QT_QPA_PLATFORM=wayland`, and the earlier
    `os.environ.setdefault()` therefore did nothing on exactly the systems the
    guard protects.
  - It stands down when **XWayland is not actually reachable** — no `DISPLAY`, or a
    `DISPLAY` whose local X socket (`/tmp/.X11-unix/X<n>`) does not exist (a stale
    value, a partially imported service environment). Qt's xcb plugin `qFatal()`s on
    a failed X connection, so forcing it there would turn a session that at least
    started into one with no window at all.
  - An **explicit non-Wayland** `QT_QPA_PLATFORM`/`GDK_BACKEND` (`offscreen`,
    `minimal`, `eglfs`, `vnc`, `broadway`) is always honored: a headless or CI run
    opted out deliberately, and a session-type hint must not beat an explicit request.
  - Wayland is detected from `QT_QPA_PLATFORM`/`GDK_BACKEND`, `XDG_SESSION_TYPE` or
    `WAYLAND_DISPLAY` (the last one covers compositors started by hand from a TTY).
  - Opt out with **`VAF_ALLOW_WAYLAND=1`** to keep the session's display server
    (native Wayland is fine on some GPUs). The decision is written to the tray
    startup log as `display platform: ...`.
- **`WEBKIT_DISABLE_DMABUF_RENDERER=1`.** Set on Linux to avoid GBM buffer errors
  under XWayland with many GPU drivers, while keeping compositing on.
- **`GI_TYPELIB_PATH` injected.** The system typelib path
  (`/usr/lib64/girepository-1.0:/usr/lib/girepository-1.0`) is prepended so the
  venv's PyGObject can find the AppIndicator/WebKitGTK typelibs used by the tray.
- **In-process-GPU anti-leak rules.** QtWebEngine runs the GPU in-process, so a
  continuously *repainting* UI animation leaks tile/texture RAM into the renderer
  process (RSS once climbed to ~7 GB). `desktop_window.py` therefore keeps vsync
  on (no `--disable-frame-rate-limit` / `--disable-gpu-vsync`), avoids
  `--enable-accelerated-2d-canvas`, and sets `--aggressive-cache-discard`,
  `--renderer-process-limit=1`, `--disk-cache-size` and a V8 heap cap; the
  frontend animates only `transform`/`opacity`. Full detail in the Anti-Leak
  Notes of [SYSTEM_TRAY.md](./SYSTEM_TRAY.md).
- **Renderer crash auto-recovery.** If the QtWebEngine render process dies,
  `renderProcessTerminated` fires and the window reloads to respawn the renderer
  (with a crash-loop guard), instead of leaving a blank window.
- **Native Save/Print dialogs parented to the window.** On X11 a parentless
  `QFileDialog` can open *behind* the webview (looks like "nothing happens"), so
  VAF wires Qt6-correct `downloadRequested` / print handlers parented to the main
  window (`desktop_window.py`).
- **Startup splash.** The window opens on a self-contained splash screen and only
  switches to the Web UI once the Next.js frontend is serving. See
  [STARTUP_SPLASH.md](./STARTUP_SPLASH.md).
- **Autostart is freedesktop-native.** Tray login-autostart writes
  `~/.config/autostart/vaf-tray.desktop` (`Platform.set_tray_autostart`). Server
  mode instead installs a systemd **user** service (`systemctl --user enable vaf`)
  with `loginctl enable-linger` so it starts at boot without a login session.

### Sub-agent terminal windows

In CLI mode a sub-agent or workflow gets its own terminal window. The emulator is probed in
order (`gnome-terminal`, `xterm`, `konsole`, `x-terminal-emulator`) and the command runs
under `bash -c` with **no shell tail after it**.

The child, not the shell, decides what happens to the window (`vaf/cli/autoclose.py`):
a successful run shows a short countdown and closes it, a failed run and `--no-auto-close`
hold it open until you press Enter, and a piped WebUI child exits immediately because there
is no window to hold. The child is Python, so all three platforms behave the same. This
replaced an appended `; exec bash`, which re-execed an interactive shell over the finished
child so the window could never close: the countdown printed "Terminal closing." and the
window stayed on a prompt (live incident 2026-07-20). Emulator hold flags were not used
instead, because support varies between them and versions, and `x-terminal-emulator` is a
Debian alternatives symlink to an unknown emulator.

Per-spawn values (`VAF_SESSION_ID`, `VAF_TASK_ID`, `VAF_AGENT_TYPE`) are embedded into the
command as an `env K=V ...` prefix rather than passed via `Popen(env=...)`, because
`gnome-terminal` is a thin D-Bus client of a long-lived `gnome-terminal-server` and whether
the client's environment reaches the new shell is version dependent.

Whether a spawn opens a window at all is decided per spawn: an explicit `VAF_SPAWN_MODE` in
the spawn's own environment wins, otherwise `VAF_WEBUI_ACTIVE`. That flag means "this process
serves a web UI" and is set once when the server starts. It used to be set on WebSocket
connect and cleared when the last browser disconnected, so a transient socket drop silently
changed spawn behaviour process-wide and a host terminal window appeared for a sub-agent that
had been launched from the browser.

## Known limitations / gotchas

- **Wayland is not used directly.** VAF runs the Qt window and tray through
  XWayland (`QT_QPA_PLATFORM=xcb`) for stability, overriding a Wayland session's own
  setting. Two exceptions: with `VAF_ALLOW_WAYLAND=1` the session's choice is kept,
  and on a Wayland session **without XWayland** (no `DISPLAY`) VAF leaves the
  display server alone rather than forcing a platform with no X server behind it.
- **Tray icon depends on typelibs.** Without the AppIndicator + WebKitGTK
  GObject-introspection typelibs (or if `PyGObject` failed to build), the tray
  icon may be missing. The app window and Web UI still work; open the UI from the
  menu launcher or `http://localhost:3000`.
- **High-refresh displays are capped.** True 120/240 Hz rendering is only
  reachable via the framerate flag that leaks on this engine, so the window
  intentionally runs vsync-capped (~60 fps).
- **Wayland-native screen capture / some GPU drivers.** Because rendering goes via
  XWayland, behavior can vary by GPU driver; the DMA-buf renderer is disabled to
  work around the most common buffer errors.
- **Optional system tools.** `poppler-utils` (PDF→image) and `tesseract-ocr` are
  needed for scanned-PDF OCR; without them those paths degrade. `ffmpeg` and
  `portaudio` support the speech features.

## See also

- [LINUX_SETUP.md](../setup/LINUX_SETUP.md) — Linux install & usage guide
- [INSTALLATION_GUIDE.md](../setup/INSTALLATION_GUIDE.md) — cross-platform install overview
- [DOCKER_SERVICES.md](../setup/DOCKER_SERVICES.md) — the container stack VAF depends on
- [SYSTEM_TRAY.md](./SYSTEM_TRAY.md) — tray + desktop window + Qt anti-leak notes
- [STARTUP_SPLASH.md](./STARTUP_SPLASH.md) — the desktop startup splash
- [run_vaf.sh](../../run_vaf.sh) — the venv launcher
- [install.sh](../../install.sh) — the Linux/macOS installer
