#!/bin/sh
# VAF Browser Container Entrypoint
#
# Chrome 112+ ignores --remote-debugging-address=0.0.0.0 as a security measure
# and binds CDP to 127.0.0.1 only. Docker port mapping routes to the container's
# eth0 IP, not to its 127.0.0.1 — so the port would be unreachable from the host.
#
# Fix: Chromium listens on 127.0.0.1:9223 (internal), socat proxies
#      0.0.0.0:9222 → 127.0.0.1:9223, Docker maps host:9222 → container:9222.

set -e

# Start Chromium on internal port 9223
/usr/lib/chromium/chromium \
    --headless=new \
    --no-sandbox \
    --disable-dev-shm-usage \
    --remote-debugging-port=9223 \
    --window-size=1280,800 \
    --disable-extensions \
    --disable-background-networking \
    --disable-default-apps \
    --disable-sync \
    --disable-translate \
    --hide-scrollbars \
    --metrics-recording-only \
    --mute-audio \
    --no-first-run \
    --safebrowsing-disable-auto-update \
    --disable-http2 \
    --disable-quic \
    --disable-gpu \
    --disable-gpu-sandbox \
    --disable-software-rasterizer \
    about:blank &

# Wait until Chromium CDP is ready
echo "Waiting for Chromium CDP on 127.0.0.1:9223..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:9223/json/version > /dev/null 2>&1; then
        echo "Chromium ready."
        break
    fi
    sleep 0.5
done

# Proxy 0.0.0.0:9222 → 127.0.0.1:9223
# This makes the CDP reachable via Docker's port mapping from the host.
echo "Starting socat proxy 0.0.0.0:9222 -> 127.0.0.1:9223"
exec socat TCP-LISTEN:9222,fork,reuseaddr TCP:127.0.0.1:9223
