#!/bin/bash
# Wrapper to start VAF Tray App in background detached from terminal

# Get directory of this script
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# Check if log dir exists
mkdir -p logs

echo "Starting VAF Tray App in background..."
echo "Logs will be written to ./logs/tray_debug.log"

# Start Docker services (postgres, redis, browser, etc.)
if command -v docker &>/dev/null && [ -f "$DIR/docker-compose.memory.yml" ]; then
    echo "Starting Docker services..."
    docker compose -f "$DIR/docker-compose.memory.yml" up -d --quiet-pull 2>/dev/null \
        && echo "   ✓ Docker services running" \
        || echo "   ⚠ Docker not available — services skipped"
fi

# Ensure PATH includes common locations for Node/npm (Homebrew, etc.)
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.nvm/versions/node/$(ls $HOME/.nvm/versions/node/ | head -n 1)/bin:$PATH"

# Check for virtual environment
PYTHON_CMD="python3"
if [ -f "$DIR/venv/bin/python3" ]; then
    PYTHON_CMD="$DIR/venv/bin/python3"
    echo "Using venv: $PYTHON_CMD"
fi

# Run with nohup to detach from terminal session
# use python3 -m vaf.main tray
nohup "$PYTHON_CMD" -m vaf.main tray > logs/tray_debug.log 2>&1 &

PID=$!
echo "VAF started with PID $PID"
echo "You can close this terminal now."
