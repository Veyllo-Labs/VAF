# VAF on macOS

This document explains how VAF *runs* on macOS — the runtime stack and the platform-specific behavior and fixes that are unique to the Mac. For step-by-step installation see [MACOS_SETUP.md](../setup/MACOS_SETUP.md); this doc does not repeat the install steps.

## Runtime stack on macOS

- **Python** — VAF runs from the project's own virtual environment (`venv/bin/python`), created by [setup_mac.sh](../../scripts/setup_mac.sh) from a Homebrew `python@X.Y` interpreter. Homebrew's Python ships a `Python.framework`, so `venv/bin/python` is both GUI-capable (needed for the menu-bar tray via `pystray` + `pyobjc`) *and* sees the venv's installed packages. `run_vaf.sh` explicitly launches this interpreter — see the fix note below.
- **Desktop window / web-view backend** — the native window is `pywebview` using the **WKWebView** backend (the system Safari/WebKit engine); no bundled Chromium. On the Mac there is no separate WebView package to install — `pyobjc-framework-Cocoa` (darwin-only in [requirements.txt](../../requirements.txt) / the `desktop` extra in [setup.py](../../setup.py)) is what `pystray` needs.
- **Menu-bar / system tray** — `pystray` (the same library on every OS; the old `rumps` dependency was removed because it fought pywebview for the main thread). pywebview owns the macOS main thread, so `pystray` runs detached — which has consequences for `.app`-bundle launches. See [SYSTEM_TRAY.md](SYSTEM_TRAY.md#menu-bar-tray--bundle-launch-spotlight--dock).
- **Docker services** — the memory/RAG stack (Postgres/pgvector, Redis, browser, TTS, …) runs in Docker via either **Docker Desktop** or **Colima** (a free, no-licence engine the installer can auto-install through Homebrew). `run_vaf.sh` brings the stack up with `docker compose` on launch. See [DOCKER_SERVICES.md](../setup/DOCKER_SERVICES.md).
- **Launcher / entry point** — the process starts via `run_vaf.sh` (venv launcher), which `exec`s `python -m vaf.main tray` (or any `vaf` subcommand when arguments are passed). `setup_mac.sh` also adds a `vaf` shell alias to `~/.zshrc` and generates a `VAF.app` bundle under `~/Applications` via [create_app_shortcut.py](../../scripts/create_app_shortcut.py).

## Installing & launching

Install once with [MACOS_SETUP.md](../setup/MACOS_SETUP.md) (Homebrew, `setup_mac.sh`). After that, VAF launches two ways: from a terminal, type `vaf` (the alias) or run `./run_vaf.sh` — this starts the Docker stack, the tray, the desktop window, and the splash, all in one process. From the GUI, open **VAF** via Spotlight (Cmd+Space) or the Dock; the `VAF.app` bundle hands the run off to Terminal (see below) so the menu-bar tray appears, then minimises the Terminal window. If VAF is already running, the bundle just opens the Web UI in the browser instead of starting a second instance.

## Platform-specific behavior & fixes

- **venv interpreter, not the raw framework binary** — `run_vaf.sh` runs `venv/bin/python`. An earlier version `exec`'d the raw framework binary (`.../Python.app/Contents/MacOS/Python`), which bypassed the venv entirely (none of the installed deps were importable) and mis-detected the Python version after activating the venv. Why it matters: on a modern Homebrew Python the old path failed to start VAF at all.
- **Menu-bar tray via Terminal hand-off (`.app` launch)** — on macOS the tray status item (`NSStatusItem`) must be created on the main thread, but pywebview owns it, so `pystray` runs detached. A detached icon registers fine from a terminal but *not* from an `.app` bundle (Spotlight/Dock), so a bundle-launched VAF had a window but no tray and no way to quit. Fix: `VAF.app`'s launcher uses `osascript` to run `./run_vaf.sh tray` inside Terminal and minimises that window. First launch shows a one-time *"VAF wants to control Terminal"* Automation prompt. Deep dive: [SYSTEM_TRAY.md](SYSTEM_TRAY.md#menu-bar-tray--bundle-launch-spotlight--dock).
- **Startup splash (no `:3000` flash)** — the window opens on a self-contained `vaf/media/splash.html` and only navigates to the real Web UI once the frontend is actually serving on its resolved port. Why it matters: it avoids briefly showing whatever other app happens to hold `http://127.0.0.1:3000` during boot. Deep dive: [STARTUP_SPLASH.md](STARTUP_SPLASH.md).
- **Homebrew-PATH visibility for the tray** — `vaf/tray.py` calls `_ensure_macos_brew_path()` to prepend `/opt/homebrew/bin` and `/usr/local/bin` before probing for Docker/Colima, so a Homebrew-installed engine is visible even when the process was launched with a minimal PATH (e.g. from the `.app`).
- **Onboarding animation "double-play" shim (WebKit only)** — in `web/app/login/page.tsx` the setup-wizard step animation passed an empty `onUpdate` handler to framer-motion. On WKWebView the compositor (WAAPI) path re-read a committed end value mid-transition, making the card slide up, snap back, and slide up again; the shim forces main-thread animation. It is a WebKit-only workaround to be dropped after upgrading framer-motion to 11.0.11+.

## Known limitations / gotchas

- **Microphone / voice input in the desktop window** — WKWebView default-denies `getUserMedia` and exposes no permission API at pywebview's layer, so WebUI voice input is not granted inside the native window. Workaround: allow the controlling app under **System Settings → Privacy & Security → Microphone**, or open the Web UI in a real browser at `http://localhost:3000`. A native pyobjc WKUIDelegate grant is planned.
- **The minimised Terminal window keeps VAF alive** — after an `.app` launch, closing that Terminal window hard-kills VAF. Quit cleanly via the tray **Quit** item, not by closing Terminal.
- **First `.app` launch requires the Automation prompt** — VAF cannot open its Terminal-hosted tray until you approve *"VAF wants to control Terminal"* once.
- **Docker engine must be running** — if neither Docker Desktop nor Colima is up, sign-in and the memory stack are unavailable; start it with `colima start` (or open Docker Desktop) and relaunch.

## See also

- [MACOS_SETUP.md](../setup/MACOS_SETUP.md) — macOS install steps (Homebrew, `setup_mac.sh`, Colima)
- [INSTALLATION_GUIDE.md](../setup/INSTALLATION_GUIDE.md) — cross-platform installation overview
- [DOCKER_SERVICES.md](../setup/DOCKER_SERVICES.md) — the Docker memory/RAG stack
- [SYSTEM_TRAY.md](SYSTEM_TRAY.md) — tray + desktop window architecture (incl. the macOS bundle-launch note)
- [STARTUP_SPLASH.md](STARTUP_SPLASH.md) — the startup splash / loading screen
