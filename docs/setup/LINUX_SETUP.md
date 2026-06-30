# VAF on Linux: Setup & Usage Guide

Supported distributions: **OpenSUSE**, **Fedora**, **Ubuntu/Debian**, **Arch Linux**

---

## Installation

The automated installer ([install.sh](../../install.sh)) is the recommended path. It installs the
build/audio/desktop system packages (step 1), provisions Python via [uv](https://docs.astral.sh/uv/)
(no system Python required), provisions **Node** (portable download, falling back to your package
manager / NodeSource), and **auto-installs and starts Docker** (distro package + `systemctl enable
--now docker` + adds you to the `docker` group) when it is missing — parity with the macOS and
Windows installers. The package lists below are for the manual path, or if you prefer to install
things ahead of time.

### 1. System Packages

**OpenSUSE (zypper):**
```bash
sudo zypper install portaudio-devel alsa-devel python3-devel gcc git nodejs-default npm-default docker docker-compose
# Desktop window (pywebview):
sudo zypper install typelib-1_0-WebKit2-4_1 libwebkit2gtk-4_1-0
```

**Fedora (dnf):**
```bash
sudo dnf install portaudio-devel alsa-devel python3-devel gcc git nodejs npm docker docker-compose
# Desktop window (pywebview):
sudo dnf install python3-gobject3 webkit2gtk4.1
```

**Ubuntu / Debian (apt):**
```bash
sudo apt-get install portaudio19-dev python3-dev python3-venv build-essential git nodejs npm ffmpeg
# Desktop window (pywebview):
sudo apt-get install python3-gi gir1.2-webkit2-4.1   # Ubuntu 22.04: gir1.2-webkit2-4.0
```

**Arch:**
```bash
sudo pacman -S portaudio python git nodejs npm docker docker-compose base-devel
# Desktop window (pywebview):
sudo pacman -S python-gobject webkit2gtk
```

> **Note:** The WebKitGTK packages are only needed for the native desktop window. If they are missing, VAF falls back to opening the Web UI in your default browser instead.

### 2. Container runtime *(handled automatically)*

VAF keeps users, auth, setup and memory in a PostgreSQL/pgvector container, so a runtime is required
to finish setup and sign in. **The installer now installs and starts Docker for you** (distro
package + systemd + `docker` group). The manual steps below are only needed if you'd rather set it
up yourself, or to enable an already-installed Docker:

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker   # or log out and back in
```

### 3. Automated Installation

```bash
chmod +x install.sh
./install.sh
```

The script auto-detects the distribution and installs all dependencies.

### 4. Manual Installation

```bash
# Remove old Windows venv if present
rm -rf venv

# Create new Linux venv
python3 -m venv venv
source venv/bin/activate

# Python dependencies
pip install -r requirements.txt
pip install -e .

# Web UI
cd web && npm install && cd ..
```

> **Note:** If a Windows venv exists (identified by `venv/Scripts/` instead of `venv/bin/`), it must be recreated. `install.sh` detects this automatically.

---

## Start, Stop, Restart

Use `vaf.sh` in the project root:

```bash
./vaf.sh start    # Start Docker + VAF
./vaf.sh stop     # Stop VAF cleanly
./vaf.sh restart  # Restart VAF
./vaf.sh status   # Show what is running
```

### What `vaf.sh` does

**start:**
- Checks if Docker containers are running, starts them if not
- If VAF is already running — including an instance started via the tray, which writes no `.vaf.pid` — reports it and exits without starting a second instance
- Otherwise starts VAF in the background (PID stored in `.vaf.pid`)
- Log: `logs/vaf_run.log`

**stop:**
- Sends SIGTERM to VAF process, waits up to 10 seconds
- Stops Next.js frontend and llama-server cleanly
- Releases ports 8001, 8080 and 3000
- Stops all Docker containers (`docker compose down`)
- Fallback: SIGKILL if process does not respond

**status:**
- Reports whether VAF is running by probing the backend health endpoint, so it is correct no matter how VAF was started — directly, via the tray, or via `vaf.sh start` — even when no `.vaf.pid` file exists
- Works in both plain-HTTP and TLS mode (`local_network_tls_enabled`); the `.vaf.pid` file is only used as secondary detail
- Shows whether llama-server is running
- Shows status of all Docker containers, warning with `[!!]` if any container is `unhealthy`

> If VAF was started outside `vaf.sh` (the tray writes no `.vaf.pid`), `status` still detects it via the backend and notes `started outside vaf.sh`. Override the probed port with `VAF_BACKEND_PORT` if you changed `local_network_port` in `config.json`.

### Docker containers (standalone)

```bash
# Start
/usr/bin/docker compose -f docker-compose.memory.yml up -d

# Stop (data is preserved)
/usr/bin/docker compose -f docker-compose.memory.yml down

# Status
/usr/bin/docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

> **Important:** The project contains a `docker/` subdirectory. Always use the full path `/usr/bin/docker` when running docker commands from within the project directory to avoid a shell conflict.

---

## Wayland / Headless Mode

`vaf.sh start` runs VAF in **headless mode** (`VAF_NATIVE_WRAPPER=1 vaf.main tray`). In this mode `pystray` is never imported, so no X11 display is required. This works on Wayland, pure server sessions, and any environment without a desktop.

The backend, agent loop, and frontend manager all start as daemon threads inside the same process — no tray icon is shown.

If you want the X11 tray icon (XWayland required):
```bash
export DISPLAY=:0
export XAUTHORITY=$(ls /run/user/$(id -u)/xauth_* 2>/dev/null | head -1)
python -m vaf.main tray   # must NOT set VAF_NATIVE_WRAPPER=1
```

---

## GPU Acceleration

VAF uses GPU acceleration in two places:

### 1. Local model inference (llama-server)

Uses **Vulkan** — works with NVIDIA, AMD, and Intel GPUs without the CUDA toolkit (only the GPU driver is needed).

On first start, VAF automatically downloads the `llama-b*-bin-ubuntu-vulkan-x64.tar.gz` binary if a compatible GPU is detected.

To verify GPU is active, check the server output for:
```
load_backend: loaded Vulkan backend from .../libggml-vulkan.so
```

### 2. Desktop window (Qt WebEngine / Chromium)

VAF automatically enables Chromium GPU rasterization for smooth rendering. The following flags are set at startup (Linux only):

```
--disable-frame-rate-limit   → animations run at the monitor's actual refresh rate
--disable-gpu-vsync          → avoids double-vsync latency between Qt and Chromium
--enable-gpu-rasterization   → GPU-based tile rasterization (biggest speedup)
--enable-accelerated-2d-canvas
--num-raster-threads=4
```

> **Note:** `--enable-zero-copy` is intentionally omitted — it maps GPU texture memory into the process address space, which causes the process to appear to use several GB of additional RAM in system monitors (the memory is GPU-backed and not actually paged, but tools like `top` report it as RSS). Removing it has no visible impact on rendering performance.

No manual configuration needed — these are applied automatically.

---

## Known Issues

### llama-server crashes on startup (ABRT / Signal 6)

**Cause:** llama-server build `b4320` crashed in `common_chat_templates_support_enable_thinking` when processing the model's embedded Jinja chat template with `--jinja` enabled. Jinja was enabled by default in that build even without the flag.

**Status:** Fixed — VAF now downloads `b9058+` (Vulkan binary) which handles the native template correctly. The `--jinja` flag is kept (required for tool calling); `--chat-template chatml` is NOT used so the model's native tool-call format is preserved.

### Docker permission error

```
permission denied while trying to connect to the docker API
```

**Fix:**
```bash
sudo usermod -aG docker $USER
newgrp docker
```

### venv not working after migration from Windows

The Windows venv (`venv/Scripts/`) is not usable on Linux. Recreate it:
```bash
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt && pip install -e .
```
