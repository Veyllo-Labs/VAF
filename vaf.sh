#!/bin/bash
# VAF - Veyllo Agentic Framework
# Start / Stop / Restart script for Linux (OpenSUSE, Fedora, Ubuntu, ...)
#
# Usage:
#   ./vaf.sh start     - Start VAF
#   ./vaf.sh stop      - Stop VAF cleanly
#   ./vaf.sh restart   - Restart VAF
#   ./vaf.sh status    - Show status

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/.vaf.pid"
PID_FILE_WEB="$DIR/.vaf-web.pid"
LOG_DIR="$DIR/logs"
PYTHON="$DIR/venv/bin/python3"
DOCKER="/usr/bin/docker"
COMPOSE_FILE="$DIR/docker-compose.memory.yml"
NODE_DIR="$DIR/web"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${NC}  $1"; }
warn() { echo -e "  ${YELLOW}[!!]${NC}  $1"; }
err()  { echo -e "  ${RED}[ERR]${NC} $1"; }
info() { echo -e "  ${CYAN}[..]${NC}  $1"; }

# ============================================================
# STATUS
# ============================================================
cmd_status() {
    echo ""
    echo "=== VAF Status ==="
    echo ""

    # VAF Python process
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            ok "VAF is running (PID $PID)"
        else
            warn "PID file found but process is gone (PID $PID) — cleaning up..."
            rm -f "$PID_FILE"
        fi
    else
        warn "VAF is not running"
    fi

    # llama-server
    LLAMA_PID=$(pgrep -f "llama-server" 2>/dev/null | head -1)
    if [ -n "$LLAMA_PID" ]; then
        ok "llama-server is running (PID $LLAMA_PID)"
    else
        warn "llama-server is not running"
    fi

    # Docker containers
    echo ""
    info "Docker containers:"
    if "$DOCKER" ps --format "  {{.Names}}\t{{.Status}}" 2>/dev/null | grep "vaf-"; then
        :
    else
        warn "No VAF Docker containers active"
    fi
    echo ""
}

# ============================================================
# START
# ============================================================
cmd_start() {
    echo ""
    echo "=== Starting VAF ==="
    echo ""

    # Check if already running
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            warn "VAF is already running (PID $PID)"
            exit 0
        else
            rm -f "$PID_FILE"
        fi
    fi

    # Check venv
    if [ ! -f "$PYTHON" ]; then
        err "Virtual environment not found: $PYTHON"
        err "Please run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt && pip install -e ."
        exit 1
    fi

    # Start Docker containers if not running
    if "$DOCKER" info &>/dev/null; then
        if ! "$DOCKER" ps --format "{{.Names}}" 2>/dev/null | grep -q "vaf-memory-db"; then
            info "Starting Docker containers..."
            "$DOCKER" compose -f "$COMPOSE_FILE" up -d 2>/dev/null && ok "Docker containers started" || warn "Could not start Docker containers"
        else
            ok "Docker containers already running"
        fi
    else
        warn "Docker not reachable — Memory System unavailable"
    fi

    # Log directory
    mkdir -p "$LOG_DIR"

    # Start VAF in headless background mode
    # VAF_NATIVE_WRAPPER=1 triggers run_headless() in tray.py:
    #   - Starts FastAPI/Uvicorn backend on :8001
    #   - Starts Next.js frontend via FrontendManager
    #   - Blocks on signal.pause() (SIGTERM-safe, no terminal needed)
    info "Starting VAF (headless mode)..."
    export PYTHONPATH="$DIR:$PYTHONPATH"
    export VAF_NATIVE_WRAPPER=1
    nohup "$PYTHON" -m vaf.main tray > "$LOG_DIR/vaf_run.log" 2>&1 &
    PID=$!
    echo "$PID" > "$PID_FILE"

    # Wait up to 15 seconds for backend to come up on :8001
    info "Waiting for backend to start..."
    for i in $(seq 1 15); do
        sleep 1
        if ! kill -0 "$PID" 2>/dev/null; then
            err "VAF process exited unexpectedly"
            err "Check log: $LOG_DIR/vaf_run.log"
            rm -f "$PID_FILE"
            exit 1
        fi
        if curl -sf http://127.0.0.1:8001/api/auth/needs-setup >/dev/null 2>&1; then
            ok "VAF started (PID $PID) — backend on :8001"
            break
        fi
        if [ "$i" -eq 15 ]; then
            ok "VAF started (PID $PID) — backend still initializing, check logs"
        fi
    done

    ok "Log: $LOG_DIR/vaf_run.log"
    ok "Open in browser: http://localhost:3000"
    echo ""
}

# ============================================================
# STOP
# ============================================================
cmd_stop() {
    echo ""
    echo "=== Stopping VAF ==="
    echo ""

    # 1. Stop main VAF process gracefully (SIGTERM)
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            info "Stopping VAF process (PID $PID)..."
            kill -TERM "$PID" 2>/dev/null
            # Wait up to 10 seconds
            for i in $(seq 1 10); do
                sleep 1
                if ! kill -0 "$PID" 2>/dev/null; then
                    ok "VAF stopped cleanly"
                    break
                fi
                if [ "$i" -eq 10 ]; then
                    warn "Process not responding — forcing shutdown..."
                    kill -KILL "$PID" 2>/dev/null || true
                    ok "VAF stopped (forced)"
                fi
            done
        else
            warn "PID $PID no longer exists"
        fi
        rm -f "$PID_FILE"
    else
        warn "No PID file found — searching for running VAF processes..."
    fi

    # 2. Stop Next.js frontend
    if [ -f "$PID_FILE_WEB" ]; then
        WEB_PID=$(cat "$PID_FILE_WEB")
        if kill -0 "$WEB_PID" 2>/dev/null; then
            kill -TERM "$WEB_PID" 2>/dev/null || true
            sleep 2
            kill -0 "$WEB_PID" 2>/dev/null && kill -KILL "$WEB_PID" 2>/dev/null || true
            ok "Web UI stopped"
        fi
        rm -f "$PID_FILE_WEB"
    fi
    NEXT_PIDS=$(pgrep -f "next dev" 2>/dev/null || true)
    if [ -n "$NEXT_PIDS" ]; then
        echo "$NEXT_PIDS" | xargs kill -TERM 2>/dev/null || true
    fi

    # 4. Stop any remaining VAF Python processes
    PIDS=$(pgrep -f "python.*vaf.main" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "$PIDS" | xargs kill -TERM 2>/dev/null || true
        sleep 2
        REMAINING=$(pgrep -f "python.*vaf.main" 2>/dev/null || true)
        if [ -n "$REMAINING" ]; then
            echo "$REMAINING" | xargs kill -KILL 2>/dev/null || true
        fi
        ok "VAF Python processes stopped"
    fi

    # 5. Stop llama-server gracefully
    LLAMA_PIDS=$(pgrep -f "llama-server" 2>/dev/null || true)
    if [ -n "$LLAMA_PIDS" ]; then
        info "Stopping llama-server..."
        echo "$LLAMA_PIDS" | xargs kill -TERM 2>/dev/null || true
        sleep 3
        REMAINING=$(pgrep -f "llama-server" 2>/dev/null || true)
        if [ -n "$REMAINING" ]; then
            echo "$REMAINING" | xargs kill -KILL 2>/dev/null || true
        fi
        ok "llama-server stopped"
    fi

    # 6. Release VAF ports (8001 = web server, 8080 = llama, 3000 = Next.js)
    for PORT in 8001 8080 3000; do
        PID_ON_PORT=$(ss -tlnp 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' | head -1 || true)
        if [ -n "$PID_ON_PORT" ]; then
            kill -TERM "$PID_ON_PORT" 2>/dev/null || true
            ok "Port $PORT released"
        fi
    done

    ok "VAF fully stopped"
    echo ""
}

# ============================================================
# RESTART
# ============================================================
cmd_restart() {
    echo ""
    echo "=== Restarting VAF ==="
    cmd_stop
    sleep 1
    cmd_start
}

# ============================================================
# MAIN
# ============================================================
case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    *)
        echo ""
        echo "Usage: $0 {start|stop|restart|status}"
        echo ""
        echo "  start    - Start VAF + Docker"
        echo "  stop     - Stop VAF cleanly"
        echo "  restart  - Restart VAF"
        echo "  status   - Show current status"
        echo ""
        exit 1
        ;;
esac
