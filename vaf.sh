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
# Backend health probe. The main API port serves plain HTTP normally, but HTTPS
# when local_network_tls_enabled is set, so backend_is_up() tries both schemes.
# Override the port via VAF_BACKEND_PORT if it was customised in config.json.
BACKEND_PORT="${VAF_BACKEND_PORT:-8001}"
BACKEND_HEALTH_PATH="/api/auth/needs-setup"

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

# Is the VAF backend answering?  Probes the live backend (the same health
# endpoint cmd_start waits on) rather than the .vaf.pid file, which is only
# written by 'vaf.sh start' — NOT by the tray / native-wrapper launch path.
# This makes status + the start guard path-agnostic.
backend_is_up() {
    if command -v curl >/dev/null 2>&1; then
        # Normal mode serves plain HTTP; TLS mode (local_network_tls_enabled)
        # serves HTTPS with a self-signed local cert (-k). A 2xx from the
        # unauthenticated needs-setup endpoint on either scheme = backend alive.
        curl -sf  --max-time 2 "http://127.0.0.1:${BACKEND_PORT}${BACKEND_HEALTH_PATH}"  >/dev/null 2>&1 && return 0
        curl -skf --max-time 2 "https://127.0.0.1:${BACKEND_PORT}${BACKEND_HEALTH_PATH}" >/dev/null 2>&1 && return 0
        return 1
    fi
    # Fallback when curl is missing: is anything listening on the API port?
    # Scheme-agnostic (so it also covers TLS mode), but only proves a listener
    # exists, not that the backend is actually healthy.
    ss -tlnp 2>/dev/null | grep -q ":${BACKEND_PORT} "
}

# ============================================================
# STATUS
# ============================================================
cmd_status() {
    echo ""
    echo "=== VAF Status ==="
    echo ""

    # VAF backend — probe the live backend on :8001 so we report correctly no
    # matter how VAF was started.  The .vaf.pid file is only written by
    # 'vaf.sh start', so it is treated as secondary info, not the source of truth.
    if backend_is_up; then
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            ok "VAF is running (PID $(cat "$PID_FILE"))"
        else
            [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
            ok "VAF is running (backend on :${BACKEND_PORT} — started outside vaf.sh, no PID file)"
        fi
    else
        # Backend not answering — clean up any stale PID file and report down.
        if [ -f "$PID_FILE" ]; then
            warn "VAF is not running (stale PID file removed)"
            rm -f "$PID_FILE"
        else
            warn "VAF is not running"
        fi
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
    CONTAINER_LIST=$("$DOCKER" ps --format "{{.Names}}\t{{.Status}}" 2>/dev/null | grep "vaf-" || true)
    if [ -n "$CONTAINER_LIST" ]; then
        while IFS= read -r line; do
            NAME=$(echo "$line" | cut -f1)
            STATUS=$(echo "$line" | cut -f2-)
            if echo "$STATUS" | grep -qi "unhealthy"; then
                warn "$NAME  →  $STATUS"
            elif echo "$STATUS" | grep -qi "health: starting"; then
                info "$NAME  →  $STATUS"
            else
                echo -e "  ${GREEN}[OK]${NC}  $NAME  →  $STATUS"
            fi
        done <<< "$CONTAINER_LIST"
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

    # Check if already running — probe the live backend first so we also catch
    # an instance started via the tray / native wrapper (which writes no
    # .vaf.pid).  Order: health (:8001) → PID file → only then spawn.  This
    # prevents a second instance fighting over port 8001/3000.
    if backend_is_up; then
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            warn "VAF is already running (PID $(cat "$PID_FILE"))"
        else
            warn "VAF is already running (backend on :${BACKEND_PORT} — started outside vaf.sh)"
        fi
        exit 0
    fi
    # Backend not answering — drop any stale PID file before starting.
    [ -f "$PID_FILE" ] && rm -f "$PID_FILE"

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
        # Warn about unhealthy containers
        UNHEALTHY=$("$DOCKER" ps --format "{{.Names}}\t{{.Status}}" 2>/dev/null | grep "vaf-" | grep -i "unhealthy" || true)
        if [ -n "$UNHEALTHY" ]; then
            while IFS= read -r line; do
                NAME=$(echo "$line" | cut -f1)
                STATUS=$(echo "$line" | cut -f2-)
                warn "Container unhealthy: $NAME ($STATUS)"
            done <<< "$UNHEALTHY"
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

    # Wait up to 15 seconds for backend to come up on the API port
    info "Waiting for backend to start..."
    for i in $(seq 1 15); do
        sleep 1
        if ! kill -0 "$PID" 2>/dev/null; then
            err "VAF process exited unexpectedly"
            err "Check log: $LOG_DIR/vaf_run.log"
            rm -f "$PID_FILE"
            exit 1
        fi
        if backend_is_up; then
            ok "VAF started (PID $PID) — backend on :${BACKEND_PORT}"
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

    # 7. Stop Docker containers
    if "$DOCKER" info &>/dev/null; then
        if "$DOCKER" ps --format "{{.Names}}" 2>/dev/null | grep -q "vaf-"; then
            info "Stopping Docker containers..."
            "$DOCKER" compose -f "$COMPOSE_FILE" down 2>/dev/null && ok "Docker containers stopped" || warn "Could not stop Docker containers"
        fi
    fi

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
    echo ""
    for i in 5 4 3 2 1; do
        printf "  ${CYAN}[..]${NC}  Starting in %s...\r" "$i"
        sleep 1
    done
    echo ""
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
