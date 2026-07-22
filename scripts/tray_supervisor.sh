#!/bin/bash
# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
#
# Bounded restart supervisor for the VAF tray process (windowed mode).
#
# QtWebEngine runs its GPU service in-process: a GPU-driver abort in the HOST
# process (live incident 2026-07-22: SIGABRT raised inside the NVIDIA GL library
# while presenting a frame during a window resize) kills the whole tray process.
# The in-app crash recovery only covers the separate RENDERER child process
# (renderProcessTerminated), so a host abort was a permanent outage until now.
# This wrapper turns it into a short one.
#
# Rules:
#   - restart ONLY on abnormal exit: code 0 (normal quit) and 130/143
#     (user-initiated Ctrl+C / SIGTERM) never restart
#   - at most MAX_RESTARTS restarts within a WINDOW_S-second window, then give
#     up so a broken startup cannot crash-loop forever
#   - before each restart, wait for the tray singleton port to be free
#
# Usage: tray_supervisor.sh <python-executable> [extra args for vaf.main tray]

PY="$1"
shift

MAX_RESTARTS=3
WINDOW_S=600
SINGLETON_PORT=8002

child=""
on_signal() {
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null
        wait "$child" 2>/dev/null
    fi
    exit 143
}
trap on_signal TERM INT

restarts=0
window_start=$(date +%s)

while true; do
    "$PY" -m vaf.main tray "$@" &
    child=$!
    wait "$child"
    code=$?
    child=""

    case "$code" in
        0|130|143) exit "$code" ;;
    esac

    now=$(date +%s)
    if [ $((now - window_start)) -gt "$WINDOW_S" ]; then
        restarts=0
        window_start=$now
    fi
    restarts=$((restarts + 1))
    if [ "$restarts" -gt "$MAX_RESTARTS" ]; then
        echo "[tray-supervisor] $restarts abnormal exits within ${WINDOW_S}s (last code $code) - giving up" >&2
        exit "$code"
    fi
    echo "[tray-supervisor] tray exited abnormally (code $code) - restarting ($restarts/$MAX_RESTARTS)" >&2

    # The dead instance's singleton port (tray.py) can linger briefly; a
    # successful /dev/tcp connect means it is still occupied.
    for _ in $(seq 1 30); do
        if ! (exec 3<>"/dev/tcp/127.0.0.1/$SINGLETON_PORT") 2>/dev/null; then
            break
        fi
        sleep 1
    done
    sleep 2
done
