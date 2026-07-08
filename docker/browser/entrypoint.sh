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
#
# Chromium is SUPERVISED: it is relaunched if it ever exits, and socat forwards
# only while its CDP endpoint is actually live. A single transient Chromium crash
# therefore self-heals in seconds instead of permanently bricking the browser
# service (socat forwarding forever to a dead port).

set -e

CHROMIUM=/usr/lib/chromium/chromium

# ── Virtual display (headed mode) ───────────────────────────────────────────
export DISPLAY=:99

start_xvfb() {
    # Remove a stale lock/socket left by a previous (crashed) Xvfb, otherwise Xvfb aborts with
    # "Server is already active for display 99", the leftover socket makes the readiness check below
    # pass anyway, and Chromium then launches against a dead display ("Missing X server"). This bit us
    # on container restarts where /tmp survived.
    rm -f /tmp/.X99-lock 2>/dev/null || true
    rm -f /tmp/.X11-unix/X99 2>/dev/null || true

    Xvfb :99 -screen 0 1920x1080x24 -ac -nolisten tcp >/tmp/xvfb.log 2>&1 &
    XVFB_PID=$!
    # Wait until the X server is genuinely up: the socket must exist AND the Xvfb process must still be
    # alive (a stale socket alone is not enough; see above). Bail out loudly if Xvfb dies.
    i=0
    while [ $i -lt 100 ]; do
        if ! kill -0 "$XVFB_PID" 2>/dev/null; then
            echo "Xvfb failed to start:"; cat /tmp/xvfb.log 2>/dev/null; exit 1
        fi
        [ -e /tmp/.X11-unix/X99 ] && break
        i=$((i + 1)); sleep 0.1
    done
    echo "Xvfb ready on :99"
}

start_xvfb

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
# Chromium is deliberately launched WITHOUT --no-first-run. On the Debian bookworm
# chromium 150.0.7871.46 package, --no-first-run makes a fresh profile in an EEA
# region (our TZ=Europe/Berlin) SIGTRAP ~1s into startup, on the search-engine
# choice / RegionalCapabilities / default-search path, so the browser dies before
# its CDP port ever opens (Debian #1141618; chromium 149 is fine, 150 regressed).
# We verified empirically that dropping --no-first-run removes the crash. The
# first-run experience it would otherwise suppress is harmless for CDP automation
# (browser_agent drives its own tabs), and the two flags below keep it quiet:
# --disable-search-engine-choice-screen suppresses the choice modal, and
# --search-engine-choice-country=US pins a non-EEA country so that whole subsystem
# is never entered. See Debian #1141618 / crbug.com/357068286.
start_chromium() {
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
        --no-default-browser-check \
        --disable-search-engine-choice-screen \
        --search-engine-choice-country=US \
        --safebrowsing-disable-auto-update \
        --disable-quic \
        --use-gl=angle \
        --use-angle=swiftshader \
        --enable-unsafe-swiftshader \
        $PROXY_ARGS \
        about:blank &
    CHROMIUM_PID=$!
}

# Wait until this Chromium's CDP endpoint answers (0), or it dies / times out (1).
wait_for_cdp() {
    i=0
    while [ $i -lt 60 ]; do
        kill -0 "$CHROMIUM_PID" 2>/dev/null || return 1
        if curl -sf http://127.0.0.1:9223/json/version >/dev/null 2>&1; then
            return 0
        fi
        i=$((i + 1)); sleep 0.5
    done
    return 1
}

# Clean shutdown on `docker stop`: kill children instead of the 10s SIGTERM->SIGKILL
# wait. This entrypoint is PID 1 now (socat is a child, not exec'd), so without a
# trap SIGTERM would be ignored and every stop would hang for the full grace period.
cleanup() {
    kill "$SOCAT_PID" "$CHROMIUM_PID" "$XVFB_PID" 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

# ── Supervise: (re)launch Chromium, forward via socat only while CDP is live ──
# `wait` returns Chromium's (possibly non-zero) crash status, which must NOT abort
# the supervisor, so errexit is off from here on.
set +e
SOCAT_PID=""
while :; do
    pkill -9 chromium 2>/dev/null                   # reap any orphaned child processes from a prior crash
    kill -0 "$XVFB_PID" 2>/dev/null || start_xvfb   # revive the display if it ever died

    start_chromium
    echo "Chromium started (pid $CHROMIUM_PID); waiting for CDP on 127.0.0.1:9223..."
    if wait_for_cdp; then
        echo "Chromium ready. Starting socat proxy 0.0.0.0:9222 -> 127.0.0.1:9223"
        socat TCP-LISTEN:9222,fork,reuseaddr TCP:127.0.0.1:9223 &
        SOCAT_PID=$!
        wait "$CHROMIUM_PID"          # block until Chromium exits (crash, OOM, docker stop)
        echo "Chromium exited (status $?); stopping socat and relaunching"
        kill "$SOCAT_PID" 2>/dev/null
        wait "$SOCAT_PID" 2>/dev/null
        SOCAT_PID=""
    else
        echo "Chromium did not become ready in time; killing and relaunching"
        kill "$CHROMIUM_PID" 2>/dev/null
        wait "$CHROMIUM_PID" 2>/dev/null
    fi
    sleep 1
done
