# VAF on Linux: Setup & Usage Guide

Supported distributions: **OpenSUSE**, **Fedora**, **Ubuntu/Debian**, **Arch Linux**

---

## Installation

### 1. System Packages

**OpenSUSE (zypper):**
```bash
sudo zypper install portaudio-devel alsa-devel python3-devel gcc git nodejs-default npm-default docker docker-compose
```

**Fedora (dnf):**
```bash
sudo dnf install portaudio-devel alsa-devel python3-devel gcc git nodejs npm docker docker-compose
```

**Ubuntu / Debian (apt):**
```bash
sudo apt-get install portaudio19-dev python3-dev python3-venv build-essential git nodejs npm ffmpeg
```

**Arch:**
```bash
sudo pacman -S portaudio python git nodejs npm docker docker-compose base-devel
```

### 2. Enable Docker

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
- Starts VAF in the background (PID stored in `.vaf.pid`)
- Log: `logs/vaf_run.log`

**stop:**
- Sends SIGTERM to VAF process, waits up to 10 seconds
- Stops llama-server cleanly
- Releases ports 8001 and 8080
- Fallback: SIGKILL if process does not respond

**status:**
- Shows whether VAF and llama-server are running
- Shows status of all Docker containers

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

## GPU Acceleration (optional)

VAF detects on first start whether an NVIDIA GPU is available. If CUDA is not installed, it will ask whether to set it up automatically.

To install manually afterwards:
```bash
source venv/bin/activate
python -m vaf.main install-gpu
```

---

## Known Issues

### llama-server crashes on startup (ABRT / Signal 6)

**Cause:** The `--jinja` flag (or previously `--reasoning-format deepseek`) causes `common_chat_templates_support_enable_thinking` to throw an exception during server init when the loaded model's Jinja chat template is incompatible.

**Status:** Fixed in `vaf/core/backend.py` — both flags have been removed.

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
