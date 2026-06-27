#!/usr/bin/env bash
# VAF - one-click bootstrap (Linux / macOS).
#
# Hosted entry point. Once the VAF repo is PUBLIC on GitHub, a user runs:
#
#     curl -fsSL https://raw.githubusercontent.com/Veyllo-Labs/VAF/main/packaging/install/bootstrap.sh | bash
#
# It provisions a bare machine (no sudo for the core):
#   1. ensures `uv` (provisions Python, user-scoped),
#   2. ensures `git` (clone + `vaf update` need a git checkout),
#   3. clones the repo, then hands off to install.sh (which does uv venv, portable Node,
#      Docker detection, deps, shortcut).
#
# URLs are final (owner/repo verified via `git remote`); they resolve the day the repo goes
# public - nothing to "fill in" later. During the private alpha, use a local clone + ./install.sh.
set -euo pipefail

VAF_REPO="${VAF_REPO:-Veyllo-Labs/VAF}"     # single source of truth
VAF_REF="${VAF_REF:-main}"                  # branch or release tag
INSTALL_DIR="${VAF_INSTALL_DIR:-$HOME/VAF}"

info() { printf '  \033[0;90m[i]\033[0m %s\n' "$1"; }
ok()   { printf '  \033[0;32m[OK]\033[0m %s\n' "$1"; }
warn() { printf '  \033[0;33m[!]\033[0m %s\n' "$1"; }

printf '\n== VAF bootstrap ==\n\n'

# 1. uv (provisions Python without sudo)
if ! command -v uv >/dev/null 2>&1; then
    info "Installing uv (provisions Python, no sudo)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
if command -v uv >/dev/null 2>&1; then ok "uv ready"; else warn "uv not on PATH yet (install.sh will retry)"; fi

# 2. git (needed to clone, and so `vaf update` keeps working)
if ! command -v git >/dev/null 2>&1; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
        warn "git not found - triggering Xcode Command Line Tools (includes git)..."
        xcode-select --install 2>/dev/null || true
        warn "Finish the install dialog, then re-run this command."
    else
        warn "git not found. Install it (e.g. 'sudo apt-get install git') and re-run."
    fi
    exit 1
fi
ok "git found"

# 3. clone (or update) the repo
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Updating existing checkout at $INSTALL_DIR ..."
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$VAF_REF"
    git -C "$INSTALL_DIR" checkout "$VAF_REF"
    git -C "$INSTALL_DIR" pull --ff-only || true
else
    info "Cloning https://github.com/$VAF_REPO (ref: $VAF_REF) -> $INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 --branch "$VAF_REF" "https://github.com/$VAF_REPO.git" "$INSTALL_DIR"
fi
ok "Repository ready: $INSTALL_DIR"

# 4. hand off to the full installer
info "Running install.sh ..."
cd "$INSTALL_DIR"
chmod +x install.sh 2>/dev/null || true
bash install.sh

printf '\n== Done. Launch with: %s/run_vaf.sh  (or the desktop entry) ==\n\n' "$INSTALL_DIR"
