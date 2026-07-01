#!/bin/bash
# Wrapper to run VAF in the virtual environment without manual activation

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$SCRIPT_DIR"

if [ -f "$PROJECT_ROOT/venv/bin/activate" ]; then
    source "$PROJECT_ROOT/venv/bin/activate"
    export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"

    # Start Docker services (postgres, redis, browser, etc.)
    if command -v docker &>/dev/null && [ -f "$PROJECT_ROOT/docker-compose.memory.yml" ]; then
        echo "Starting Docker services..."
        docker compose -f "$PROJECT_ROOT/docker-compose.memory.yml" up -d --quiet-pull 2>/dev/null \
            && echo "   Docker services running" \
            || echo "   Docker not available - services skipped"
    fi

    # Run VAF with the venv's own Python. The venv is built from a framework
    # Python (Homebrew's python@X.Y ships a Python.framework), so venv/bin/python
    # is GUI-capable (menu-bar tray via pystray + pyobjc) AND sees the venv's
    # installed packages.
    #
    # A previous version exec'd the RAW framework binary
    # (.../Python.app/Contents/MacOS/Python), which bypassed the venv entirely:
    # none of the installed deps (typer, fastapi, torch, ...) were importable, so
    # VAF failed to start. It also read the version from `python3` AFTER
    # activating the venv, so on a Homebrew Python 3.14 machine it hunted for the
    # 3.14 framework binary. (rumps was removed; the tray is pystray on every
    # platform, so no special framework binary is needed.)
    VENV_PYTHON="$PROJECT_ROOT/venv/bin/python"

    if [ ! -x "$VENV_PYTHON" ]; then
        echo "venv Python not found at $VENV_PYTHON"
        echo "Please run: ./scripts/setup_mac.sh"
        exit 1
    fi

    echo "Starting VAF using venv Python: $VENV_PYTHON ($("$VENV_PYTHON" --version 2>&1))"
    # exec to keep the PID (matters for the app bundle / signal handling)
    if [ -z "$1" ]; then
        exec "$VENV_PYTHON" -m vaf.main tray
    else
        exec "$VENV_PYTHON" -m vaf.main "$@"
    fi
else
    echo "Virtual environment not found."
    echo "Please run: ./scripts/setup_mac.sh"
fi
