#!/bin/sh
# VAF Browser Container Entrypoint
#
# Chromium runs HEADED under a virtual X display (Xvfb) instead of --headless=new:
# real headed Chrome leaks far fewer automation tells, so it is the stronger
# anti-bot baseline.
#
# Chrome 112+ binds the remote-debugging port to 127.0.0.1 only (security), so
# Chromium listens on 127.0.0.1:9223 and socat exposes 0.0.0.0:9222 -> 9223
# (Docker maps host:9222 -> container:9222).

set -e

CHROMIUM=/usr/lib/chromium/chromium

# ── Virtual display (headed mode) ───────────────────────────────────────────
export DISPLAY=:99
# Remove a stale lock/socket left by a previous (crashed) run — otherwise Xvfb aborts with
# "Server is already active for display 99", the leftover socket makes the readiness check below
# pass anyway, and Chromium then launches against a dead display ("Missing X server"). This bit us
# on container restarts where /tmp survived.
rm -f /tmp/.X99-lock 2>/dev/null || true
rm -f /tmp/.X11-unix/X99 2>/dev/null || true

Xvfb :99 -screen 0 1920x1080x24 -ac -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
# Wait until the X server is genuinely up: the socket must exist AND the Xvfb process must still be
# alive (a stale socket alone is not enough — see above). Bail out loudly if Xvfb dies.
i=0
while [ $i -lt 100 ]; do
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        echo "Xvfb failed to start:"; cat /tmp/xvfb.log 2>/dev/null; exit 1
    fi
    [ -e /tmp/.X11-unix/X99 ] && break
    i=$((i + 1)); sleep 0.1
done
echo "Xvfb ready on :99"

# ── Version-matched User-Agent ──────────────────────────────────────────────
# A UA whose Chrome version differs from the actual binary is itself a fingerprint
# tell, so derive it from the installed Chromium at runtime (headed UA has no
# "HeadlessChrome" marker). The JS supplement reads navigator.userAgent to keep
# navigator.userAgentData consistent with this string.
CHROME_VER="$("$CHROMIUM" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"
[ -z "$CHROME_VER" ] && CHROME_VER="124.0.0.0"
USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${CHROME_VER} Safari/537.36"

# ── Optional proxy (VAF_BROWSER_PROXY=http://user:pass@host:port | socks5://…) ─
# Proxy is a launch-time setting, so it must be applied here (we connect via CDP
# afterwards and cannot change it). When proxied, also stop WebRTC from leaking
# the real local IP around the proxy.
PROXY_ARGS=""
if [ -n "$VAF_BROWSER_PROXY" ]; then
    PROXY_ARGS="--proxy-server=$VAF_BROWSER_PROXY --force-webrtc-ip-handling-policy=disable_non_proxied_udp"
    echo "Browser proxy: enabled"
fi

echo "Chromium $CHROME_VER (headed under Xvfb)"
echo "UA: $USER_AGENT"

# ── Launch headed Chromium on internal port 9223 ────────────────────────────
"$CHROMIUM" \
    --no-sandbox \
    --disable-dev-shm-usage \
    --remote-debugging-port=9223 \
    --disable-blink-features=AutomationControlled \
    --user-agent="$USER_AGENT" \
    --lang=en-US \
    --accept-lang=en-US,en \
    --window-position=0,0 \
    --window-size=1920,1080 \
    --disable-extensions \
    --disable-background-networking \
    --disable-default-apps \
    --disable-sync \
    --disable-translate \
    --metrics-recording-only \
    --mute-audio \
    --no-first-run \
    --no-default-browser-check \
    --safebrowsing-disable-auto-update \
    --disable-quic \
    --use-gl=angle \
    --use-angle=swiftshader \
    --enable-unsafe-swiftshader \
    $PROXY_ARGS \
    about:blank &

# Wait until Chromium CDP is ready
echo "Waiting for Chromium CDP on 127.0.0.1:9223..."
i=0
while [ $i -lt 60 ]; do
    if curl -sf http://127.0.0.1:9223/json/version > /dev/null 2>&1; then
        echo "Chromium ready."
        break
    fi
    i=$((i + 1)); sleep 0.5
done

# Proxy 0.0.0.0:9222 → 127.0.0.1:9223 so CDP is reachable via Docker port mapping.
echo "Starting socat proxy 0.0.0.0:9222 -> 127.0.0.1:9223"
exec socat TCP-LISTEN:9222,fork,reuseaddr TCP:127.0.0.1:9223
