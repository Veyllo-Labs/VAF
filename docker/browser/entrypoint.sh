#!/bin/sh
# VAF Browser Container Entrypoint
#
# Chromium runs HEADED under a virtual X display instead of --headless=new:
# real headed Chrome leaks far fewer automation tells, so it is the stronger
# anti-bot baseline. The display server is KasmVNC's Xkasmvnc: an X server like
# Xvfb, plus a built-in WebSocket stream of the display (port 6901) so the same
# Chromium is watchable and drivable from the VAF web UI. Chromium and its CDP
# lane do not know the difference.
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
    # Remove a stale lock/socket left by a previous (crashed) X server, otherwise it aborts with
    # "Server is already active for display 99", the leftover socket makes the readiness check below
    # pass anyway, and Chromium then launches against a dead display ("Missing X server"). This bit us
    # on container restarts where /tmp survived.
    rm -f /tmp/.X99-lock 2>/dev/null || true
    rm -f /tmp/.X11-unix/X99 2>/dev/null || true

    # The three encoder settings are about ARTEFACTS, and each answers a different
    # cause of them:
    #   -CompareFB 1   compare the framebuffer pixel by pixel instead of trusting the
    #                  damage the application reports (default: auto). Chromium's
    #                  damage does not cover everything it actually repaints, and the
    #                  parts it forgets stay on screen as stale rectangles.
    #   -VideoTime/-VideoArea  push the automatic "video mode" out of reach. It would
    #                  otherwise trigger on any scroll (>45% of the screen changing for
    #                  5s), downscale the whole display and encode it lossily - which
    #                  looks exactly like smeared text.
    #   -DynamicQuality  raise the floor of dynamic JPEG scaling (default 7-8), so a
    #                  busy moment cannot drop the page into blocky quality.
    #
    # Xkasmvnc IS the X server (Xvnc lineage), launched directly rather than via the
    # kasmvncserver perl wrapper, which insists on interactive user setup. The flags
    # mirror /etc/kasmvnc/kasmvnc.yaml on purpose: whichever of the two this build
    # reads first, the answer is the same. Plain WS + no basic auth is the same
    # threat model as the unauthenticated CDP port: both leave this container only
    # through loopback-published ports, and user-facing auth lives in the VAF web
    # server that proxies the stream.
    Xkasmvnc :99 -geometry 1920x1080 -depth 24 \
        -websocketPort 6901 -interface 0.0.0.0 \
        -httpd /usr/share/kasmvnc/www \
        -disableBasicAuth -SecurityTypes None \
        -AlwaysShared -FrameRate 60 \
        -CompareFB 1 \
        -VideoTime 600 -VideoArea 100 \
        -DynamicQualityMin 8 -DynamicQualityMax 9 \
        -ac >/tmp/kasmvnc.log 2>&1 &
    XVFB_PID=$!
    # Wait until the X server is genuinely up: the socket must exist AND the process must still be
    # alive (a stale socket alone is not enough; see above). Bail out loudly if it dies.
    i=0
    while [ $i -lt 100 ]; do
        if ! kill -0 "$XVFB_PID" 2>/dev/null; then
            echo "Xkasmvnc failed to start:"; cat /tmp/kasmvnc.log 2>/dev/null; exit 1
        fi
        [ -e /tmp/.X11-unix/X99 ] && break
        i=$((i + 1)); sleep 0.1
    done
    echo "Xkasmvnc ready on :99 (VNC web stream on :6901)"

    # A window manager, for exactly one job: keep Chromium's window the size of the
    # display. Without one, nothing manages the window - it keeps the geometry it was
    # created with, and when the viewer resizes the display to match the panel it is
    # shown in, the difference stays behind as a black band with no browser in it.
    # matchbox is ~300 KB, fullscreens what it manages, and draws no decorations.
    pkill -f matchbox-window-manager 2>/dev/null || true   # -f: the name is >15 chars, -x never matches it
    matchbox-window-manager -use_titlebar no -use_cursor no >/tmp/wm.log 2>&1 &
    WM_PID=$!
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

# ── Chromium sandbox probe ──────────────────────────────────────────────────
# Chromium's own sandbox needs unprivileged user namespaces. Docker's DEFAULT
# seccomp profile denies them (clone(CLONE_NEWUSER) fails with EPERM), which
# was the only reason this browser ever ran --no-sandbox. VAF starts this
# container with docker/browser/chromium-seccomp.json (Docker's default plus
# clone/clone3/unshare/setns), so the probe succeeds and the sandbox is ON,
# with no added capability. The probe stays because the IMAGE can be run by
# runtimes that do not apply that profile (a plain docker run, an embedder's
# own compose): without user namespaces Chromium cannot start sandboxed at
# all, and a crash-looping browser helps nobody - so the fallback keeps the
# browser alive and says loudly what it gave up. The if-form matters: a bare
# probe line would abort the script under set -e.
SANDBOX_ARGS=""
if unshare -U true 2>/dev/null; then
    echo "User namespaces available: Chromium sandbox ENABLED"
else
    SANDBOX_ARGS="--no-sandbox"
    echo "WARNING: user namespaces unavailable; running WITHOUT Chromium's sandbox"
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
#
# The browser keeps its OWN window: tab strip, toolbar, bookmarks, downloads. An
# earlier version hid all of it (app mode) so the web UI could draw that chrome
# itself, and it cost more than it bought - a middle-click into a new tab opened a
# second, ordinary window inside the streamed one, and every browser feature that
# lives in the toolbar was simply gone. --force-dark-mode dresses that UI to match
# the app instead of replacing it; it themes the BROWSER, not page content, so sites
# still render the way their authors meant.
#
# $SANDBOX_ARGS is empty in the normal deployment: the sandbox probe above
# found user namespaces (granted by VAF's seccomp profile), so Chromium runs
# WITH its own sandbox; only a failed probe adds --no-sandbox.
#
# --test-type stays a STANDING flag, sandbox or not: it suppresses the yellow
# "unsupported command-line flag ... Stability and security will suffer"
# infobar (56px of display, measured), which fires for ANY non-standard flag,
# not only --no-sandbox - dropping it alongside --no-sandbox brought the bar
# straight back naming --disable-blink-features=AutomationControlled (live,
# first open after the sandbox round). That anti-bot flag is load-bearing and
# stays, so the suppressor stays with it. Not visible to pages:
# navigator.webdriver stays false, verified in this container.
#
# NOT --kiosk. It hides the browser UI and ALSO disables the right-click context
# menu - and that menu is a feature here, not decoration: it is where "save as",
# "print" and VAF's own "send this to your agent" live.
#
# --disable-gpu-compositing + --disable-partial-raster are for the VNC stream, not
# for rendering: with partial swaps the compositor repaints only what it believes
# changed, the X damage it reports does not cover the rest, and the stream keeps
# showing stale rectangles of an older frame. WebGL keeps working through
# SwiftShader, so the fingerprint surface is unchanged.
#
# uBlock Origin Lite arrives WITHOUT a flag: it is installed by Chromium's
# external-extensions provider from /usr/share/chromium/extensions (the CRX and
# its descriptor are baked in by the Dockerfile, which also explains why).
# Two flags decide whether that works, and both were measured in this container:
#   - --load-extension is silently IGNORED by Chromium 150 - it must not come
#     back, a dead flag that looks load-bearing is worse than none;
#   - --disable-default-apps KILLS the external-extensions provider on Linux
#     (it is the same install lane) - it must stay out. Nothing else was using
#     it: Debian ships no default apps, so the only thing it ever disabled here
#     is the blocker.
# The former --disable-extensions stays out for the same reason. Fingerprint-
# wise the blocker puts this browser in the largest of all cohorts (adblock
# users); no navigator surface changes.
#
# --mute-audio stays OUT: the container has no audio device, so there is no
# output either way, and a browser that claims to play audio like any desktop
# does is the more ordinary-looking one. Removing the mute changes no
# fingerprint surface (AudioContext rendering is computational and was never
# muted), it only stops declaring "this browser wants no sound".
start_chromium() {
    # Profile wipe: when VAF dropped the marker (every cross-user handover,
    # verified by VAF polling for this marker's consumption; also a same-scope
    # clean start under VAF_BROWSER_SCRUB=full), the whole profile is wiped
    # BETWEEN Chromium launches - the only moment the files are not being
    # rewritten. This is
    # what a container restart cannot do (the container filesystem survives
    # restarts), and it removes what the CDP scrub cannot reach: history,
    # passwords saved in Chromium's own password manager, autofill, bookmarks
    # and downloaded files. THREE trees, not one: the profile under .config,
    # the HTTP disk cache under .cache (measured: 2.8 MB of cached response
    # bodies with the previous holder's hostnames survived a handover while
    # the docs claimed the cache was wiped), and the NSS database under
    # .local/share/pki, which is where a client certificate AND ITS PRIVATE
    # KEY live - the one credential a banking or enterprise-SSO login leaves
    # behind that is worse than a cookie. The content blocker comes back by itself: the
    # external-extensions provider reinstalls it into the fresh profile.
    if [ -f /home/browser/.scrub-profile ]; then
        rm -rf /home/browser/.config/chromium /home/browser/.cache/chromium \
               /home/browser/.local/share/pki \
               /home/browser/Downloads /home/browser/Workspace
        rm -f /home/browser/.scrub-profile
        echo "Profile scrubbed for user handover"
    fi

    # Stale profile-singleton artifacts. Chromium's SingletonLock records
    # "hostname-pid"; after a hard stop (docker rm -f, a host reboot) the
    # lock survives in the profile, and a pooled instance's profile VOLUME
    # then meets a NEW container hostname - Chromium reads that as "in use
    # on another computer" and refuses to start, forever, one relaunch per
    # supervisor round (live incident: an unhealthy pool instance taxed
    # every browser open with its full health deadline). Deleting them here
    # is safe by construction: the supervisor's loop-top pkill -9 guarantees
    # no second Chromium runs in this container, which is the only thing the
    # lock could ever be protecting.
    rm -f /home/browser/.config/chromium/Singleton* 2>/dev/null || true

    # The two transfer folders, and the file picker anchored to them. VAF
    # mirrors the holder's files into Workspace (uploads) and drains Downloads
    # the other way; the XDG dirs plus the GTK bookmark put both one click away
    # in Chromium's file dialog, and the seeded last_directory makes the picker
    # OPEN in the workspace - the only folder in this container that holds
    # anything of the person's.
    mkdir -p /home/browser/Workspace /home/browser/Downloads /home/browser/.config/gtk-3.0
    printf 'XDG_DOCUMENTS_DIR="/home/browser/Workspace"\nXDG_DOWNLOAD_DIR="/home/browser/Downloads"\n' \
        > /home/browser/.config/user-dirs.dirs
    printf 'file:///home/browser/Workspace Workspace\nfile:///home/browser/Downloads Downloads\n' \
        > /home/browser/.config/gtk-3.0/bookmarks
    PREFS=/home/browser/.config/chromium/Default/Preferences
    if [ -f "$PREFS" ] && ! grep -q '"selectfile"' "$PREFS"; then
        sed -i 's/^{/{"selectfile":{"last_directory":"\/home\/browser\/Workspace"},/' "$PREFS" 2>/dev/null || true
    fi

    # Never restore the previous session. The supervisor below kills Chromium with
    # SIGKILL, which marks the profile as crashed, and a crashed profile REOPENS the
    # windows it had - including any ordinary window that ever appeared, which then
    # shows a full browser UI inside the streamed window and never goes away again
    # (measured after a container restart: two pages, the wrong one in front). Both
    # halves are needed: the flag suppresses the restore bubble, the edit clears the
    # crash mark that drives the restore itself.
    PREFS=/home/browser/.config/chromium/Default/Preferences
    [ -f "$PREFS" ] && sed -i 's/"exit_type":"[^"]*"/"exit_type":"Normal"/' "$PREFS" 2>/dev/null || true

    "$CHROMIUM" \
        --disable-session-crashed-bubble \
        --hide-crash-restore-bubble \
        $SANDBOX_ARGS \
        --test-type \
        --disable-dev-shm-usage \
        --remote-debugging-port=9223 \
        --disable-blink-features=AutomationControlled \
        --user-agent="$USER_AGENT" \
        --lang=en-US \
        --accept-lang=en-US,en \
        --window-position=0,0 \
        --window-size=1920,1080 \
        --force-dark-mode \
        --disable-background-networking \
        --disable-sync \
        --disable-translate \
        --metrics-recording-only \
        --no-default-browser-check \
        --disable-search-engine-choice-screen \
        --search-engine-choice-country=US \
        --safebrowsing-disable-auto-update \
        --disable-quic \
        --use-gl=angle \
        --use-angle=swiftshader \
        --enable-unsafe-swiftshader \
        --disable-gpu-compositing \
        --disable-partial-raster \
        $PROXY_ARGS &
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
    kill "$SOCAT_PID" "$CHROMIUM_PID" "$WM_PID" "$XVFB_PID" 2>/dev/null || true
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
