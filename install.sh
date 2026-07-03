#!/bin/bash
#
# VAF - Veyllo Agentic Framework - Cross-Platform Installer
# Supports: macOS (Intel/Apple Silicon) and Linux (Debian/Ubuntu/Fedora/Arch)
#
# Usage:
#   ./install.sh                 # Full installation
#   ./install.sh --skip-docker   # Skip Docker setup
#   ./install.sh --help          # Show help
#
# Requirements:
#   - Python 3.10+
#   - Internet connection
#   - A container runtime (REQUIRED: database for users/auth/setup + memory)
#

set -e

# ============================================================================
# CONFIGURATION
# ============================================================================
MIN_PYTHON_VERSION="3.10"
MIN_NODE_VERSION="18"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Flags
SKIP_DOCKER=false
VERBOSE=false

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Functions
print_step()    { echo -e "\n${CYAN}>> $1${NC}"; }
print_success() { echo -e "  ${GREEN}[OK] $1${NC}"; }
print_warning() { echo -e "  ${YELLOW}[!] $1${NC}"; }
print_error()   { echo -e "  ${RED}[X] $1${NC}"; }
print_info()    { echo -e "  ${NC}[i] $1${NC}"; }

# ============================================================================
# ARGUMENT PARSING
# ============================================================================
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-docker)
            SKIP_DOCKER=true
            shift
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        --help|-h)
            echo "VAF Installer - Cross-Platform Setup Script"
            echo ""
            echo "Usage: ./install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --skip-docker    Skip Docker installation/setup"
            echo "  --verbose, -v    Show verbose output"
            echo "  --help, -h       Show this help message"
            echo ""
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ============================================================================
# BANNER
# ============================================================================
echo -e "${MAGENTA}"
cat << 'EOF'

   ===============================================================
      VAF - Veyllo Agentic Framework  (Cross-Platform Installer)
      Python + FastAPI + Next.js + pgvector + Local LLM
   ===============================================================

EOF
echo -e "${NC}"

# ============================================================================
# SYSTEM DETECTION
# ============================================================================
print_step "Detecting System Configuration..."

OS_TYPE=""
OS_NAME=""
PKG_MANAGER=""
INSTALL_CMD=""
ARCH=$(uname -m)

if [[ "$OSTYPE" == "darwin"* ]]; then
    OS_TYPE="macos"
    OS_NAME="macOS"
    
    # Check for Apple Silicon
    if [[ "$ARCH" == "arm64" ]]; then
        print_info "macOS (Apple Silicon - $ARCH)"
    else
        print_info "macOS (Intel - $ARCH)"
    fi
    
    # Check for Homebrew
    if command -v brew &> /dev/null; then
        PKG_MANAGER="brew"
        INSTALL_CMD="brew install"
        print_success "Homebrew detected"
    else
        print_warning "Homebrew not found"
        print_info "Install from: https://brew.sh"
        echo ""
        echo -e "  Run: ${CYAN}/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"${NC}"
        echo ""
        if [[ -t 0 ]]; then
            read -p "  Install Homebrew now? (Y/n) " response
        else
            response="Y"  # non-interactive (piped/CI): auto-install so the installer just runs through
        fi
        if [[ "$response" != "n" && "$response" != "N" ]]; then
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            # The Homebrew installer only writes a shellenv line to ~/.zprofile; it does NOT touch
            # the CURRENT process PATH. Source it now so brew/colima/docker resolve in this same run
            # (otherwise every later `brew install` here is command-not-found and silently no-ops).
            for _bp in /opt/homebrew/bin /usr/local/bin; do
                if [ -x "$_bp/brew" ]; then eval "$("$_bp/brew" shellenv)"; break; fi
            done
            if ! command -v brew &> /dev/null; then
                print_error "Homebrew installed but not on PATH - open a new terminal and re-run ./install.sh"
                exit 1
            fi
            PKG_MANAGER="brew"
            INSTALL_CMD="brew install"
        else
            print_error "Homebrew is required for macOS installation"
            exit 1
        fi
    fi

elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS_TYPE="linux"
    
    # Detect Linux distribution
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_NAME="$NAME"
        
        case "$ID" in
            ubuntu|debian|pop|linuxmint)
                PKG_MANAGER="apt"
                INSTALL_CMD="sudo apt-get install -y"
                ;;
            fedora|rhel|centos|rocky|almalinux)
                PKG_MANAGER="dnf"
                INSTALL_CMD="sudo dnf install -y"
                if ! command -v dnf &> /dev/null; then
                    PKG_MANAGER="yum"
                    INSTALL_CMD="sudo yum install -y"
                fi
                ;;
            arch|manjaro|endeavouros)
                PKG_MANAGER="pacman"
                INSTALL_CMD="sudo pacman -S --noconfirm"
                ;;
            opensuse*)
                PKG_MANAGER="zypper"
                INSTALL_CMD="sudo zypper install -y"
                ;;
            *)
                print_warning "Unknown Linux distribution: $ID"
                PKG_MANAGER="unknown"
                ;;
        esac
    else
        OS_NAME="Linux"
        print_warning "Could not detect Linux distribution"
    fi
    
    print_info "$OS_NAME ($ARCH)"
    if [[ "$PKG_MANAGER" != "unknown" ]]; then
        print_success "Package manager: $PKG_MANAGER"
    fi
else
    print_error "Unsupported operating system: $OSTYPE"
    exit 1
fi

# ============================================================================
# 1. PYTHON CHECK
# ============================================================================
print_step "Checking Python Installation..."

PYTHON_CMD=""
PYTHON_VERSION=""
USE_UV=false

# Try python3 first, then python
for cmd in python3 python; do
    if command -v $cmd &> /dev/null; then
        version=$($cmd --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        # Compare as VERSION numbers, not floats: bc/float treats "3.10" as 3.1, so "3.9 >= 3.10"
        # is wrongly true and would accept the old macOS system Python 3.9. Compare major.minor ints.
        v_maj=${version%%.*}; v_min=${version#*.}; v_min=${v_min%%.*}
        r_maj=${MIN_PYTHON_VERSION%%.*}; r_min=${MIN_PYTHON_VERSION#*.}; r_min=${r_min%%.*}
        if [ -n "$v_maj" ] && [ -n "$v_min" ] && { [ "$v_maj" -gt "$r_maj" ] || { [ "$v_maj" -eq "$r_maj" ] && [ "$v_min" -ge "$r_min" ]; }; }; then
            PYTHON_CMD=$cmd
            PYTHON_VERSION=$version
            break
        fi
    fi
done

# Prefer uv: it provisions Python without sudo, so a bare machine needs nothing
# pre-installed. Install uv when neither a suitable Python nor uv is present.
if [[ -z "$PYTHON_CMD" ]] && ! command -v uv &> /dev/null; then
    print_warning "No suitable Python found - installing uv (provisions Python, no sudo)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null || print_warning "uv install failed"
    export PATH="$HOME/.local/bin:$PATH"
fi

if command -v uv &> /dev/null; then
    USE_UV=true
    print_success "Using uv to manage Python ($(command -v uv))"
elif [[ -n "$PYTHON_CMD" ]]; then
    print_success "Python $PYTHON_VERSION found ($PYTHON_CMD)"
else
    print_error "Python $MIN_PYTHON_VERSION or higher not found and uv could not be installed!"
    echo ""
    if [[ "$OS_TYPE" == "macos" ]]; then
        echo -e "  Install with: ${CYAN}brew install python@3.12${NC}  or  ${CYAN}curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
    else
        echo -e "  Install Python via your package manager, or: ${CYAN}curl -LsSf https://astral.sh/uv/install.sh | sh${NC}"
    fi
    exit 1
fi

# ============================================================================
# 2. SYSTEM DEPENDENCIES
# ============================================================================
print_step "Installing System Dependencies..."

if [[ "$OS_TYPE" == "macos" ]]; then
    # macOS dependencies via Homebrew
    DEPS="portaudio git ffmpeg"
    print_info "Installing: $DEPS"
    brew install $DEPS 2>/dev/null || print_warning "Some packages may already be installed"
    
elif [[ "$OS_TYPE" == "linux" ]]; then
    case "$PKG_MANAGER" in
        apt)
            print_info "Updating package lists..."
            sudo apt-get update -qq || print_warning "apt update failed - continuing with cached lists"
            # Core build deps in ONE call. Keep the WebKitGTK typelib SEPARATE: Ubuntu 24.04 dropped
            # gir1.2-webkit2-4.0 (only 4.1 exists), and a single unknown package name aborts the whole
            # apt-get transaction - which would otherwise silently skip build-essential/portaudio/etc.
            DEPS="portaudio19-dev python3-dev python3-venv build-essential git ffmpeg python3-gi gir1.2-ayatanaappindicator3-0.1 libgirepository1.0-dev libcairo2-dev"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            # WebKitGTK typelib (used by the AppIndicator tray icon; the app window uses Qt, so this is
            # non-fatal): try 4.1 (Ubuntu 24.04+), fall back to 4.0 (22.04), best-effort.
            $INSTALL_CMD gir1.2-webkit2-4.1 2>/dev/null \
                || $INSTALL_CMD gir1.2-webkit2-4.0 2>/dev/null \
                || print_warning "WebKitGTK typelib unavailable (tray icon may not load; app window unaffected)"
            ;;
        dnf|yum)
            # WebKitGTK kept separate (Fedora 39+ ships webkit2gtk4.1, not 4.0) so one bad name can't
            # abort the whole transaction.
            DEPS="portaudio-devel python3-devel gcc git ffmpeg python3-gobject3 libappindicator-gtk3 gobject-introspection-devel cairo-devel"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            $INSTALL_CMD webkit2gtk4.1 2>/dev/null \
                || $INSTALL_CMD webkit2gtk4.0 2>/dev/null \
                || $INSTALL_CMD webkit2gtk3 2>/dev/null \
                || print_warning "WebKitGTK unavailable (tray icon may not load; app window unaffected)"
            ;;
        pacman)
            DEPS="portaudio python git ffmpeg base-devel python-gobject webkit2gtk libappindicator-gtk3 gobject-introspection cairo"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            ;;
        zypper)
            DEPS="portaudio-devel alsa-devel python3-devel gcc git ffmpeg nodejs-default npm-default docker-compose typelib-1_0-WebKit2-4_1 libwebkit2gtk-4_1-0 typelib-1_0-AyatanaAppIndicator3-0_1 gobject-introspection-devel cairo-devel"
            print_info "Installing: $DEPS"
            $INSTALL_CMD $DEPS 2>/dev/null || print_warning "Some packages may have failed"
            ;;
        *)
            print_warning "Please manually install: portaudio, git, ffmpeg, python dev headers"
            ;;
    esac

    # (PyGObject is installed into the venv AFTER it is created  see the venv step.
    #  It used to run here, but the venv does not exist yet on a first install, so the
    #  guard silently skipped it and the GTK desktop window never worked.)
fi

print_success "System dependencies installed"

# ============================================================================
# 3. GPU DETECTION
# ============================================================================
print_step "Detecting GPU for LLM Acceleration..."

GPU_TYPE="cpu"
GPU_NAME="None"

if [[ "$OS_TYPE" == "macos" ]]; then
    # Check for Apple Silicon (Metal)
    if [[ "$ARCH" == "arm64" ]]; then
        GPU_TYPE="metal"
        GPU_NAME="Apple Silicon (Metal)"
        print_success "Apple Silicon detected - Metal GPU acceleration available"
    else
        print_info "Intel Mac - CPU mode (Metal not available)"
    fi
else
    # Linux GPU detection
    if command -v nvidia-smi &> /dev/null; then
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
        if [[ -n "$GPU_NAME" ]]; then
            GPU_TYPE="cuda"
            print_success "NVIDIA GPU detected: $GPU_NAME"
            print_success "CUDA acceleration available"
        fi
    elif command -v rocm-smi &> /dev/null; then
        GPU_TYPE="rocm"
        GPU_NAME="AMD (ROCm)"
        print_success "AMD GPU with ROCm detected"
    elif [[ -d /sys/class/drm/card0 ]]; then
        GPU_NAME=$(cat /sys/class/drm/card0/device/uevent 2>/dev/null | grep -oP 'DRIVER=\K.*' || echo "Unknown")
        print_info "GPU detected: $GPU_NAME (may support Vulkan)"
    else
        print_info "No dedicated GPU detected - will use CPU for LLM"
    fi
fi

# ============================================================================
# 4. DOCKER DETECTION
# ============================================================================
print_step "Checking Docker Installation (for Memory System)..."

DOCKER_INSTALLED=false
DOCKER_RUNNING=false
DOCKER_COMPOSE=false
DOCKER_BIN=""
DOCKER_SUDO=""   # set to "sudo" on Linux right after auto-install, before the docker group is active in this shell

# Resolve the real docker binary via PATH. NEVER hardcode /usr/bin/docker: on macOS
# (Homebrew/Colima) docker lives in /opt/homebrew/bin or /usr/local/bin, so a hardcoded
# /usr/bin/docker check fails even when the engine is perfectly fine.
resolve_docker_bin() {
    DOCKER_BIN="$(command -v docker 2>/dev/null || true)"
}

# Poll until the daemon answers (or give up). Used after starting an engine that boots a VM.
wait_for_docker() {
    local tries=${1:-60} i=0
    resolve_docker_bin
    while [ "$i" -lt "$tries" ]; do
        if [ -n "$DOCKER_BIN" ] && $DOCKER_SUDO "$DOCKER_BIN" info &>/dev/null; then return 0; fi
        sleep 2; i=$((i+1))
        resolve_docker_bin
    done
    return 1
}

# macOS: bring a container engine up. Use whatever the user actually has - Docker Desktop
# if its app is installed; otherwise Colima (the free engine, auto-installed via Homebrew).
# Mirrors the Windows installer auto-installing Rancher: the installer must "just run through".
start_macos_engine() {
    if [ -d "/Applications/Docker.app" ] || [ -d "$HOME/Applications/Docker.app" ]; then
        print_info "Docker Desktop detected - starting it..."
        open -a Docker 2>/dev/null || true
    else
        if ! command -v colima &>/dev/null; then
            print_info "Installing Colima - free container engine, no Docker Desktop license needed..."
            brew install colima docker docker-compose 2>/dev/null \
                || brew install colima docker docker-compose \
                || print_warning "Colima install hit an issue - try: brew install colima docker docker-compose"
        fi
        # Size the VM to the machine: >=16GB RAM -> 8GB guest, else 4GB (DB + core are light).
        local mem=4
        local ram_gb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 8589934592) / 1073741824 ))
        [ "$ram_gb" -ge 16 ] && mem=8
        print_info "Starting Colima (provisions a small Linux VM; first start ~30-60s, ${mem}GB RAM)..."
        colima start --cpu 4 --memory "$mem" 2>/dev/null \
            || colima start 2>/dev/null \
            || print_warning "colima start failed - run it manually: colima start"
    fi
    resolve_docker_bin
}

# Linux: auto-install + start Docker via the distro package + systemd + the 'docker' group - parity
# with macOS (Colima) and Windows (Rancher) so the installer "just runs through". The group change
# only takes effect on next login, so DOCKER_SUDO lets the rest of THIS run still reach the daemon.
start_linux_engine() {
    if ! command -v docker &>/dev/null; then
        print_info "Installing Docker (distro package) - VAF needs a container engine..."
        case "$PKG_MANAGER" in
            apt)     $INSTALL_CMD docker.io docker-compose-v2 2>/dev/null || $INSTALL_CMD docker.io docker-compose 2>/dev/null || print_warning "Docker install via apt failed" ;;
            dnf|yum) $INSTALL_CMD moby-engine docker-compose 2>/dev/null || $INSTALL_CMD docker docker-compose 2>/dev/null || print_warning "Docker install via dnf/yum failed" ;;
            pacman)  $INSTALL_CMD docker docker-compose 2>/dev/null || print_warning "Docker install via pacman failed" ;;
            zypper)  $INSTALL_CMD docker docker-compose 2>/dev/null || print_warning "Docker install via zypper failed" ;;
            *)       print_warning "Unknown package manager - please install Docker manually." ;;
        esac
    fi
    # Enable + start the daemon (systemd; service fallback).
    sudo systemctl enable --now docker 2>/dev/null || sudo service docker start 2>/dev/null || true
    # Add the user to the 'docker' group so future sessions don't need sudo (active on next login).
    local _u="${USER:-$(id -un)}"
    if ! id -nG "$_u" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        if sudo usermod -aG docker "$_u" 2>/dev/null; then
            print_info "Added '$_u' to the 'docker' group - log out and back in once to use docker/vaf without sudo."
        fi
    fi
    resolve_docker_bin
    # The group change isn't active in this shell yet: if the daemon is up but our session can't
    # reach the socket, use sudo for the remaining docker calls in this run (the stack still comes up).
    if [ -n "$DOCKER_BIN" ] && ! "$DOCKER_BIN" info &>/dev/null && sudo "$DOCKER_BIN" info &>/dev/null; then
        DOCKER_SUDO="sudo"
    fi
}

if [[ "$SKIP_DOCKER" == "false" ]]; then
    resolve_docker_bin
    if [ -n "$DOCKER_BIN" ] && "$DOCKER_BIN" info &>/dev/null; then
        DOCKER_INSTALLED=true
        DOCKER_RUNNING=true
        DOCKER_VERSION=$("$DOCKER_BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
        print_success "Container engine running (docker ${DOCKER_VERSION:-?})"
    elif [[ "$OS_TYPE" == "macos" ]]; then
        print_warning "No running container engine - VAF requires one (users, auth, setup and memory live in a PostgreSQL/pgvector container)."
        start_macos_engine
        if wait_for_docker 60; then
            DOCKER_INSTALLED=true
            DOCKER_RUNNING=true
            print_success "Container engine is up"
        else
            # Binaries are installed even if the VM is still booting; section 9 / the tray will retry.
            [ -n "$DOCKER_BIN" ] && DOCKER_INSTALLED=true
            print_warning "Container engine not ready yet - VAF will retry on launch (or run: colima start)."
        fi
    elif [[ "$OS_TYPE" == "linux" ]]; then
        print_warning "No running container engine - VAF requires one (users, auth, setup and memory live in a PostgreSQL/pgvector container)."
        start_linux_engine
        if wait_for_docker 30; then
            DOCKER_INSTALLED=true
            DOCKER_RUNNING=true
            print_success "Container engine is up"
        else
            [ -n "$DOCKER_BIN" ] && DOCKER_INSTALLED=true
            print_warning "Container engine not ready yet - VAF will retry on launch (or: sudo systemctl enable --now docker)."
        fi
    fi

    if [[ "$DOCKER_RUNNING" == "true" ]]; then
        if $DOCKER_SUDO "$DOCKER_BIN" compose version &>/dev/null || command -v docker-compose &>/dev/null; then
            DOCKER_COMPOSE=true
            print_success "Docker Compose available"
        fi
    fi
else
    print_info "Docker check skipped (--skip-docker flag)"
fi

# ============================================================================
# 5. NODE.JS CHECK
# ============================================================================
print_step "Checking Node.js Installation (for Web UI)..."

NODE_INSTALLED=false

if command -v node &> /dev/null; then
    NODE_VERSION=$(node --version | grep -oE '[0-9]+' | head -1)
    if [[ "$NODE_VERSION" -ge "$MIN_NODE_VERSION" ]]; then
        NODE_INSTALLED=true
        print_success "Node.js v$NODE_VERSION installed"
    else
        print_warning "Node.js v$NODE_VERSION is outdated (need v$MIN_NODE_VERSION+)"
    fi
else
    print_warning "Node.js not found"
fi

if [[ "$NODE_INSTALLED" == "false" ]]; then
    print_info "Node.js not found - downloading a portable Node (user-scoped, no sudo)..."
    # Fetched from the official nodejs.org dist (NOT bundled in the repo). Node core is MIT.
    NARCH=$(uname -m)
    case "$NARCH" in x86_64|amd64) NARCH=x64;; aarch64|arm64) NARCH=arm64;; esac
    if [[ "$OS_TYPE" == "macos" ]]; then NPLAT=darwin; NEXT=tar.gz; else NPLAT=linux; NEXT=tar.xz; fi
    NODE_BASE="https://nodejs.org/dist/latest-v22.x"
    NFILE=$(curl -fsSL "$NODE_BASE/SHASUMS256.txt" 2>/dev/null | grep -oE "node-v[0-9.]+-$NPLAT-$NARCH\.$NEXT" | head -1)
    if [[ -n "$NFILE" ]] && curl -fsSL "$NODE_BASE/$NFILE" -o "/tmp/$NFILE" 2>/dev/null; then
        NODE_DIR="$HOME/.vaf/node"
        rm -rf "$NODE_DIR" && mkdir -p "$NODE_DIR"
        tar -xf "/tmp/$NFILE" -C "$NODE_DIR" --strip-components=1 2>/dev/null
        export PATH="$NODE_DIR/bin:$PATH"
        if command -v node &> /dev/null; then
            NODE_INSTALLED=true
            print_success "Portable Node.js $(node --version) installed ($NODE_DIR)"
            # Persist for future launches (run_vaf.sh starts a fresh shell).
            for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
                if [[ -f "$rc" ]] && ! grep -q '.vaf/node/bin' "$rc"; then
                    echo 'export PATH="$HOME/.vaf/node/bin:$PATH"' >> "$rc"
                fi
            done
        fi
    fi
    if [[ "$NODE_INSTALLED" == "false" ]]; then
        print_info "Portable Node unavailable - installing Node via the package manager..."
        if [[ "$OS_TYPE" == "macos" ]]; then
            brew install node 2>/dev/null || true
        else
            case "$PKG_MANAGER" in
                apt)     sudo apt-get install -y nodejs npm 2>/dev/null || true ;;
                dnf|yum) $INSTALL_CMD nodejs npm 2>/dev/null || true ;;
                pacman)  sudo pacman -S --noconfirm nodejs npm 2>/dev/null || true ;;
                zypper)  $INSTALL_CMD nodejs npm 2>/dev/null || $INSTALL_CMD nodejs-default npm-default 2>/dev/null || true ;;
            esac
            # If the distro Node is too old (e.g. Ubuntu 22.04 ships Node 12) get a current LTS from NodeSource (apt).
            _nv=$(node --version 2>/dev/null | grep -oE '[0-9]+' | head -1)
            if { [[ -z "$_nv" ]] || [[ "$_nv" -lt "$MIN_NODE_VERSION" ]]; } && [[ "$PKG_MANAGER" == "apt" ]]; then
                print_info "Distro Node too old/absent - installing current LTS via NodeSource..."
                curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - 2>/dev/null \
                    && sudo apt-get install -y nodejs 2>/dev/null || true
            fi
        fi
        if command -v node &> /dev/null; then
            NODE_VERSION=$(node --version | grep -oE '[0-9]+' | head -1)
            if [[ "$NODE_VERSION" -ge "$MIN_NODE_VERSION" ]]; then
                NODE_INSTALLED=true
                print_success "Node.js v$NODE_VERSION installed (package manager)"
            fi
        fi
    fi
    if [[ "$NODE_INSTALLED" == "false" ]]; then
        print_warning "Could not install Node $MIN_NODE_VERSION+ automatically - the Web UI needs it."
        if [[ "$OS_TYPE" == "macos" ]]; then
            echo -e "  Install with: ${CYAN}brew install node${NC}"
        else
            echo -e "  Install manually, e.g. ${CYAN}sudo apt install nodejs npm${NC}"
        fi
    fi
fi

# ============================================================================
# 6. VIRTUAL ENVIRONMENT
# ============================================================================
print_step "Setting up Python Virtual Environment..."

cd "$PROJECT_ROOT"

# Drop a Windows-style venv (Scripts/ instead of bin/) so it gets recreated for this OS.
if [[ -d "venv" && ! -f "venv/bin/activate" && -f "venv/Scripts/activate" ]]; then
    print_warning "Windows virtual environment detected  recreating for this OS..."
    rm -rf venv
fi

if [[ -d "venv/bin" ]]; then
    print_success "Virtual environment already exists"
elif [[ "$USE_UV" == "true" ]]; then
    # uv creates the venv (and downloads Python 3.12 if needed). --seed adds pip so the
    # `python3 -m pip install` steps below keep working inside a uv venv.
    uv venv venv --python 3.12 --seed
    print_success "Virtual environment created (uv, Python 3.12)"
else
    $PYTHON_CMD -m venv venv
    print_success "Virtual environment created"
fi

# Activate venv
source venv/bin/activate
print_info "Python: $(python3 --version)"

# PyGObject into the venv (Linux desktop window / pywebview GTK backend). Needs the
# gobject-introspection + cairo dev headers from the system-deps step. Done AFTER the venv
# exists  fixes the old ordering bug where it ran before venv creation and was silently skipped.
if [[ "$OS_TYPE" == "linux" ]]; then
    # PyGObject powers the AppIndicator TRAY ICON on Linux; the window itself uses Qt/QtWebEngine, so
    # this is non-fatal. Pin < 3.52 because 3.52+ needs girepository-2.0, which Ubuntu 24.04 and older
    # distros do not ship (they have libgirepository-1.0-dev). Best-effort with a fallback to latest.
    print_info "Installing PyGObject into venv (tray icon)..."
    pip install "PyGObject<3.52" 2>/dev/null \
        || pip install PyGObject 2>/dev/null \
        || print_warning "PyGObject not installed - tray icon may be missing (the app window still works via Qt)."
fi

# ============================================================================
# 7. PYTHON DEPENDENCIES
# ============================================================================
print_step "Installing Python Dependencies..."

# Set compiler flags for audio libraries
if [[ "$OS_TYPE" == "macos" ]]; then
    export LDFLAGS="-L$(brew --prefix portaudio)/lib"
    export CFLAGS="-I$(brew --prefix portaudio)/include"
fi

# Don't let `pip install -e .` re-trigger setup.py's platform post-install (setup_mac.sh),
# which would redo brew/venv/alias/.app work install.sh already did (macOS double-path).
export VAF_SKIP_POSTINSTALL=1

# Upgrade pip
print_info "Upgrading pip..."
python3 -m pip install --upgrade pip --quiet

# Install core dependencies
print_info "Installing core dependencies..."
python3 -m pip install -e . --quiet 2>/dev/null || python3 -m pip install -e .

# Install all requirements
print_info "Installing all requirements (this may take a few minutes)..."
python3 -m pip install -r requirements.txt --quiet 2>/dev/null || {
    print_warning "Some optional dependencies failed - core functionality should work"
}

print_success "Python dependencies installed"

# ============================================================================
# 8. WEB UI SETUP
# ============================================================================
if [[ "$NODE_INSTALLED" == "true" ]]; then
    print_step "Setting up Web UI (Next.js)..."
    
    if [[ -d "web" ]]; then
        cd web
        print_info "Installing/updating npm packages (Web UI dependencies from web/package.json)..."
        npm install --silent 2>/dev/null || npm install
        print_success "Web UI dependencies installed"
        cd "$PROJECT_ROOT"
    fi
fi

# ============================================================================
# 9. DOCKER SETUP (Memory System)  Smart Update
# ============================================================================
COMPOSE_FILE="docker-compose.memory.yml"
COMPOSE_CHANGED=false

# Check if docker-compose.memory.yml changed in the latest commit
if git diff --name-only HEAD~1 HEAD 2>/dev/null | grep -q "$COMPOSE_FILE"; then
    COMPOSE_CHANGED=true
    print_info "docker-compose.memory.yml changed  will update Docker stack"
elif ! $DOCKER_SUDO "${DOCKER_BIN:-docker}" ps 2>/dev/null | grep -q "vaf-memory-db"; then
    # Stack not running at all  treat as needing startup
    COMPOSE_CHANGED=true
fi

if [[ "$DOCKER_INSTALLED" == "true" ]]; then
    print_step "Setting up Memory System Docker Stack..."

    # Auto-start Docker if compose changed but daemon is not running
    if [[ "$DOCKER_RUNNING" != "true" && "$COMPOSE_CHANGED" == "true" ]]; then
        print_warning "Container engine not running - attempting to start it automatically..."
        if [[ "$OS_TYPE" == "macos" ]]; then
            start_macos_engine
        elif [[ "$OS_TYPE" == "linux" ]]; then
            start_linux_engine
        fi

        if wait_for_docker 30; then
            DOCKER_RUNNING=true
            DOCKER_COMPOSE=true
            print_success "Container engine is now running"
        else
            print_warning "Container engine did not start in time. Please start it manually."
            print_info "Then run: ${DOCKER_BIN:-docker} compose -f docker-compose.memory.yml up -d"
        fi
    fi

    if [[ "$DOCKER_RUNNING" == "true" ]]; then
        if [[ -f "$COMPOSE_FILE" ]]; then
            resolve_docker_bin
            # Two-phase like the Windows installer: bring up the core (registry images) first so a
            # slow local build of tts/vaf-browser can never block the database the app needs to boot.
            print_info "Starting core services (database, cache, sandbox, STT, document engine)..."
            # Retry the core pull/up a few times: the first pull of the registry images over a
            # flaky connection often hits a transient "TLS handshake timeout" from Docker's CDN.
            # Pulls resume from cached layers, so a retry usually completes. Never abort on this.
            core_up=false
            for _attempt in 1 2 3; do
                if $DOCKER_SUDO "$DOCKER_BIN" compose -f "$COMPOSE_FILE" up -d postgres redis sandbox stt gotenberg; then
                    core_up=true; break
                fi
                print_warning "Core image pull/start failed (attempt $_attempt/3) - often a transient registry/TLS timeout; retrying in 10s..."
                sleep 10
            done
            if [ "$core_up" != "true" ]; then
                $DOCKER_SUDO docker-compose -f "$COMPOSE_FILE" up -d postgres redis sandbox stt gotenberg \
                    || print_warning "Core stack not up yet (network/registry). VAF retries on launch; or re-run: ${DOCKER_BIN:-docker} compose -f $COMPOSE_FILE up -d"
            fi
            print_info "Starting optional services (TTS, browser) - these build locally and may take a while..."
            $DOCKER_SUDO "$DOCKER_BIN" compose -f "$COMPOSE_FILE" up -d tts vaf-browser 2>/dev/null \
                || $DOCKER_SUDO docker-compose -f "$COMPOSE_FILE" up -d tts vaf-browser 2>/dev/null || true

            sleep 2
            if $DOCKER_SUDO "$DOCKER_BIN" ps | grep -q "vaf-memory-db"; then
                print_success "Docker stack is running"
                print_info "Database: postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory"
            else
                print_warning "Containers may still be starting - check with: ${DOCKER_BIN:-docker} ps"
            fi
        fi
    elif [[ "$COMPOSE_CHANGED" == "true" && "$DOCKER_RUNNING" != "true" ]]; then
        print_warning "Docker stack has changes but Docker is offline."
        print_info "Start Docker, then run: docker compose -f docker-compose.memory.yml up -d"
    fi
elif [[ "$DOCKER_INSTALLED" != "true" ]]; then
    print_info "Container engine not available - skipping stack setup (VAF will retry on launch)"
fi

# ============================================================================
# 10. CREATE SHORTCUTS/ALIASES
# ============================================================================
print_step "Creating Shortcuts..."

# Make run script executable
chmod +x run_vaf.sh 2>/dev/null
chmod +x start_vaf.sh 2>/dev/null

# Add shell alias
if [[ "$OS_TYPE" == "macos" ]]; then
    SHELL_CONFIG="$HOME/.zshrc"
else
    SHELL_CONFIG="$HOME/.bashrc"
    [[ -f "$HOME/.zshrc" ]] && SHELL_CONFIG="$HOME/.zshrc"
fi

RUN_SCRIPT="$PROJECT_ROOT/run_vaf.sh"

if grep -q "alias vaf=" "$SHELL_CONFIG" 2>/dev/null; then
    sed -i.bak "s|alias vaf=.*|alias vaf='$RUN_SCRIPT'|" "$SHELL_CONFIG"
    print_success "Shell alias updated in $SHELL_CONFIG"
else
    echo "" >> "$SHELL_CONFIG"
    echo "# VAF - Veyllo Agentic Framework" >> "$SHELL_CONFIG"
    echo "alias vaf='$RUN_SCRIPT'" >> "$SHELL_CONFIG"
    print_success "Shell alias added to $SHELL_CONFIG"
fi

# Create application bundle (macOS)
if [[ "$OS_TYPE" == "macos" ]]; then
    print_info "Creating macOS application bundle..."
    python3 scripts/create_app_shortcut.py 2>/dev/null || print_warning "Could not create app bundle"
    # Microphone for WebUI voice input in the desktop window (WKWebView needs
    # NSMicrophoneUsageDescription in the host Python.app) - see the script for details.
    print_info "Enabling microphone for the desktop window..."
    bash scripts/macos_mic_plist.sh ./venv/bin/python || true
fi

# Create desktop entry (Linux)  works the same on Arch/Debian/Fedora (freedesktop std)
if [[ "$OS_TYPE" == "linux" ]]; then
    DESKTOP_FILE="$HOME/.local/share/applications/vaf.desktop"
    mkdir -p "$(dirname "$DESKTOP_FILE")"

    # Prefer the PNG icon (renders reliably across GNOME/KDE/XFCE); fall back to the .ico
    # if the PNG is missing (e.g. an older checkout). Many Linux DEs don't render .ico well.
    ICON_PATH="$PROJECT_ROOT/vaf/media/vaf_icon.png"
    [[ -f "$ICON_PATH" ]] || ICON_PATH="$PROJECT_ROOT/vaf/media/vaf_icon_v6.ico"

    cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=VAF
GenericName=Veyllo Agentic Framework
Comment=AI-powered local assistant
Exec=$PROJECT_ROOT/run_vaf.sh
Icon=$ICON_PATH
Terminal=false
Categories=Development;
Keywords=ai;assistant;llm;
StartupNotify=true
EOF

    chmod +x "$DESKTOP_FILE"

    # Refresh the application menu so the entry shows up immediately (no re-login).
    # update-desktop-database ships in desktop-file-utils on all of Arch/Debian/Fedora;
    # it's optional, so guard it and never fail the install if it's absent.
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$(dirname "$DESKTOP_FILE")" >/dev/null 2>&1 || true
    fi
    print_success "Linux desktop entry created (icon: $(basename "$ICON_PATH"), no terminal window)"
fi

# ============================================================================
# 11. SERVER SETUP (Linux only)
# ============================================================================

SETUP_AUTOSTART=false
SETUP_LAN=false
INSTALL_MODE="desktop"

if [[ "$OS_TYPE" == "linux" ]] && [[ -t 0 ]]; then
    echo ""
    print_step "Installation Mode..."
    echo ""
    echo -e "  ${CYAN}[1] Desktop${NC}   personal use, local only, system tray (default)"
    echo -e "  ${CYAN}[2] Server${NC}    always-on service, LAN accessible via HTTPS, starts at boot"
    echo ""
    read -p "  Choose [1/2, default 1]: " _mode_response
    if [[ "$_mode_response" == "2" ]]; then
        INSTALL_MODE="server"
        SETUP_AUTOSTART=true
        SETUP_LAN=true
        print_success "Server mode selected"
    else
        INSTALL_MODE="desktop"
        print_success "Desktop mode selected"
    fi
fi

# --- Server mode: write config ---
if [[ "$SETUP_LAN" == "true" ]]; then
    print_info "Writing server mode config..."
    mkdir -p "$HOME/.vaf"
    INSTALL_MODE_VAR="$INSTALL_MODE"
    "$PROJECT_ROOT/venv/bin/python3" - << PYEOF
import json, os
p = os.path.expanduser("~/.vaf/config.json")
try:
    cfg = json.loads(open(p).read()) if os.path.exists(p) else {}
except Exception:
    cfg = {}
cfg["server_mode"] = True
cfg["local_network_enabled"] = True
cfg["local_network_tls_enabled"] = True
open(p, "w").write(json.dumps(cfg, indent=2))
PYEOF
    print_success "Server mode enabled in config"
    print_success "LAN access enabled (HTTPS, port 8443)"
    print_info "A self-signed TLS certificate is auto-generated on first start."
    print_warning "Browsers will show a certificate warning  expected for local networks."
fi

# --- Autostart: install systemd user service ---
if [[ "$SETUP_AUTOSTART" == "true" ]]; then
    if command -v systemctl &>/dev/null && systemctl --user daemon-reload &>/dev/null 2>&1; then
        print_info "Installing systemd user service..."

        SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
        mkdir -p "$SYSTEMD_USER_DIR"

        # Write the unit file with current user's paths baked in
        cat > "$SYSTEMD_USER_DIR/vaf.service" << EOF
[Unit]
Description=VAF - Veyllo Agentic Framework
Documentation=https://github.com/Veyllo-Labs/VAF
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
Environment=PYTHONPATH=$PROJECT_ROOT
Environment=VAF_NATIVE_WRAPPER=1
ExecStart=$PROJECT_ROOT/venv/bin/python3 -m vaf.main tray
ExecStop=/bin/kill -s TERM \$MAINPID
Restart=on-failure
RestartSec=10s
TimeoutStopSec=30

# Security hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

StandardOutput=journal
StandardError=journal
SyslogIdentifier=vaf

[Install]
WantedBy=default.target
EOF

        systemctl --user daemon-reload
        systemctl --user enable vaf

        # Enable linger so the service starts at boot even with no active login session
        if sudo loginctl enable-linger "$USER" 2>/dev/null; then
            print_success "Boot autostart enabled (loginctl linger)"
        else
            print_warning "Could not enable linger (sudo required)"
            print_info "To enable boot start: sudo loginctl enable-linger $USER"
        fi

        # Start the service immediately
        if systemctl --user start vaf 2>/dev/null; then
            print_success "VAF service started"
        else
            print_warning "Service will start on next boot/login"
        fi

        print_success "Service installed: $SYSTEMD_USER_DIR/vaf.service"
        print_info "Manage: systemctl --user {start|stop|restart|status} vaf"
        print_info "Logs:   journalctl --user -u vaf -f"

    else
        print_warning "systemd user session not available  skipping autostart"
        print_info "Manual start: ./vaf.sh start"
    fi
fi

# ============================================================================
# 12. VERIFICATION
# ============================================================================
print_step "Verifying Installation..."

# Activate venv for verification
source "$PROJECT_ROOT/venv/bin/activate"

verify_module() {
    if python3 -c "import $1" 2>/dev/null; then
        print_success "$2"
        return 0
    else
        print_warning "$2 - not available"
        return 1
    fi
}

verify_module "vaf" "VAF Module" || true
verify_module "fastapi" "FastAPI" || true
# pyttsx3 removed  caused 1-4GB RAM explosion on Windows via SAPI/comtypes.
# TTS is now handled by Docker (Piper). See docs/web-ui/SPEECH_FEATURES.md.
# verify_module "pyttsx3" "TTS Engine"
verify_module "speech_recognition" "Speech Recognition" || true

# ============================================================================
# SUMMARY
# ============================================================================
echo ""
echo -e "${GREEN}=====================================================================${NC}"
echo -e "${GREEN}                 [OK] INSTALLATION COMPLETE!${NC}"
echo -e "${GREEN}=====================================================================${NC}"
echo ""

echo -e "  ${CYAN}Quick Start:${NC}"
echo -e "    - Restart your terminal (or run: source $SHELL_CONFIG)"
echo -e "    - Then just type: ${CYAN}vaf${NC}"
echo -e "    - Or run: ${CYAN}./run_vaf.sh${NC}"
echo ""

if [[ "$SETUP_AUTOSTART" == "true" ]]; then
    echo -e "  ${CYAN}Service (autostart enabled):${NC}"
    echo -e "    - Status:  systemctl --user status vaf"
    echo -e "    - Logs:    journalctl --user -u vaf -f"
    echo -e "    - Stop:    systemctl --user stop vaf"
    echo ""
fi

if [[ "$SETUP_LAN" == "true" ]]; then
    _LAN_IP=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || hostname -I 2>/dev/null | awk '{print $1}')
    echo -e "  ${CYAN}LAN Access (HTTPS):${NC}"
    echo -e "    - https://${_LAN_IP:-<your-ip>}:8443"
    echo -e "    - localhost: https://127.0.0.1:8443"
    echo -e "    - Accept the self-signed certificate warning on first visit."
    echo ""
fi

if [[ "$DOCKER_RUNNING" == "true" ]]; then
    echo -e "  ${CYAN}Memory System:${NC}"
    echo -e "    - Database: postgresql://localhost:5432/vaf_memory"
    echo -e "    - Stop: docker compose -f docker-compose.memory.yml down"
    echo ""
fi

echo -e "  ${CYAN}GPU Acceleration:${NC} $GPU_TYPE ($GPU_NAME)"
echo ""
echo -e "  ${NC}Documentation: https://github.com/Veyllo-Labs/VAF${NC}"
echo ""
