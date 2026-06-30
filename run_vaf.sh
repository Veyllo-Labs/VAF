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
            && echo "   ✓ Docker services running" \
            || echo "   ⚠ Docker not available — services skipped"
    fi

    # Try to find a Framework Python for macOS GUI apps (Rumps tray icon)
    # This is critical when running from an App Bundle
    FRAMEWORK_PYTHON=""
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # Check standard Homebrew location for Python 3.11/3.10/etc
        # Get version from python3
        PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        
        CANDIDATES=(
            "/opt/homebrew/opt/python@$PY_VER/Frameworks/Python.framework/Versions/$PY_VER/Resources/Python.app/Contents/MacOS/Python"
            "/usr/local/opt/python@$PY_VER/Frameworks/Python.framework/Versions/$PY_VER/Resources/Python.app/Contents/MacOS/Python"
            "/Library/Frameworks/Python.framework/Versions/$PY_VER/Resources/Python.app/Contents/MacOS/Python"
        )
        
        for cand in "${CANDIDATES[@]}"; do
            if [ -f "$cand" ] && [ -x "$cand" ]; then
                FRAMEWORK_PYTHON="$cand"
                break
            fi
        done
    fi

    if [ -n "$FRAMEWORK_PYTHON" ]; then
        echo "🚀 Starting VAF using Framework Python: $FRAMEWORK_PYTHON"
        # We must use exec to keep PID
        exec "$FRAMEWORK_PYTHON" -m vaf.main "$@"
    else
        echo "🚀 Starting VAF using standard python3..."
        if [ -z "$1" ]; then
            exec python3 -m vaf.main tray
        else
            exec python3 -m vaf.main "$@"
        fi
    fi
else
    echo "❌ Virtual environment not found."
    echo "Please run: ./scripts/setup_mac.sh"
fi
