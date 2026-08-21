# VAF Browser Container
# ======================
# Runs Chromium with Chrome DevTools Protocol (CDP) on port 9222.
# VAF connects to it via ws://localhost:9222 - no Chromium on the host needed.
#
# Stealth: Chromium runs HEADED under a virtual X display instead of
# --headless=new. Real headed Chrome leaks far fewer automation tells (the new
# headless mode is still detectable), so this is the stronger anti-bot baseline.
# The display server is KasmVNC's Xkasmvnc (an X server like Xvfb, plus a
# built-in WebSocket stream of the display on port 6901), so the same Chromium
# can also be watched and driven interactively from the VAF web UI.
#
# Build (automatic via docker compose):
#   docker compose -f docker-compose.memory.yml build vaf-browser
#
# Standalone test:
#   docker run --rm -p 9222:9222 vaf-browser
#   curl http://localhost:9222/json/version

FROM debian:bookworm-slim

# Install Chromium + Xvfb (virtual display for headed mode) + a fuller font set.
# A realistic font list matters for fingerprinting, so we ship more than the bare
# minimum (liberation/dejavu/noto) without pulling the whole desktop.
# Acquire::Check-Date=false: tolerate a skewed build-host clock (common in VMs/WSL2/CI),
# otherwise apt rejects freshly-signed repo metadata as "not valid yet". GPG sig still verified.
RUN apt-get -o Acquire::Check-Date=false update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-sandbox \
    matchbox-window-manager \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-core \
    fonts-noto-color-emoji \
    libatk-bridge2.0-0 \
    libnss3 \
    libxss1 \
    libgbm1 \
    libasound2 \
    ca-certificates \
    tzdata \
    curl \
    socat \
    procps \
    && rm -rf /var/lib/apt/lists/*

# KasmVNC: the X server for the headed Chromium AND the 60fps WebSocket stream
# of that display. Version-pinned vendor .deb with a verified checksum; apt
# resolves its dependency list.
#
# LICENCE, AND THE ONE LINE THAT MUST NOT BE CROSSED. KasmVNC is GPL-2.0. As it
# stands VAF distributes NOTHING of it: this Dockerfile downloads an unmodified,
# version-pinned vendor .deb and the image is built ON THE USER'S OWN MACHINE
# (docker compose build), where it runs as a separate process alongside the other
# Debian packages - aggregation, with no copyleft reach into VAF's own code.
# Publishing a PREBUILT vaf-browser image changes exactly that: Veyllo would then
# be distributing a GPL-2.0 binary and would owe, for this pinned release, the
# licence text and the complete corresponding source (or a written offer for it).
# So it stays a local build until someone deliberately takes that on.
#
# GPL-2.0, running as a separate process in this
# container (an aggregate alongside the other Debian packages, no code linkage).
ARG TARGETARCH=amd64
ARG KASMVNC_VERSION=1.5.0
ARG KASMVNC_SHA256_AMD64=770fd3df51510beecc89666879d82faf411276e68c6e11df612f736b891b5f71
ARG KASMVNC_SHA256_ARM64=aa83a1a6c9069d1a02239988b07a3a2a082a433042b4d4ee2b9e9f6b2df9643c
RUN set -e; \
    case "$TARGETARCH" in \
      arm64) KASM_SHA="$KASMVNC_SHA256_ARM64" ;; \
      *)     KASM_SHA="$KASMVNC_SHA256_AMD64"; TARGETARCH=amd64 ;; \
    esac; \
    curl -fsSL -o /tmp/kasmvncserver.deb \
      "https://github.com/kasmtech/KasmVNC/releases/download/v${KASMVNC_VERSION}/kasmvncserver_bookworm_${KASMVNC_VERSION}_${TARGETARCH}.deb"; \
    echo "$KASM_SHA  /tmp/kasmvncserver.deb" | sha256sum -c -; \
    apt-get -o Acquire::Check-Date=false update; \
    apt-get install -y --no-install-recommends /tmp/kasmvncserver.deb; \
    rm -f /tmp/kasmvncserver.deb; rm -rf /var/lib/apt/lists/*
COPY kasmvnc.yaml /etc/kasmvnc/kasmvnc.yaml

# Non-root user for security
RUN groupadd -r browser && useradd -r -g browser -m browser

# Chromium needs a writable home for profile/tmp
RUN mkdir -p /home/browser/.config/chromium /home/browser/.vnc && \
    chown -R browser:browser /home/browser

# No bookmarks bar. Debian seeds every fresh profile from initial_bookmarks.html
# (Debian.org / Latest News / Help), which is furniture from another product sitting
# in a browser VAF presents as its own; emptying the SOURCE is what keeps it out of
# the profile, and the preference keeps the empty bar itself from taking a row.
# The preference is EDITED IN PLACE rather than prepended: the file already carries a
# bookmark_bar block further down, and with duplicate JSON keys the last one wins - a
# copy at the top parsed fine and changed nothing (measured: still true). The grep is
# the guard, so a renamed key fails the build instead of silently doing nothing.
RUN printf '<!DOCTYPE NETSCAPE-Bookmark-file-1>\n<DL><p>\n</DL><p>\n' \
      > /usr/share/chromium/initial_bookmarks.html && \
    sed -i 's/"show_on_all_tabs": true/"show_on_all_tabs": false/' \
      /etc/chromium/master_preferences && \
    grep -q '"show_on_all_tabs": false' /etc/chromium/master_preferences

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# X11 socket dir must exist and be world-writable so Xvfb (running as the non-root
# 'browser' user) can create its display socket. Without it Xvfb warns
# "_XSERVTransmkdir: euid != 0, directory /tmp/.X11-unix will not be created".
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

USER browser
WORKDIR /home/browser

# Health-check: socat proxy must be up and forwarding to Chromium
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -sf http://localhost:9222/json/version || exit 1

EXPOSE 9222 6901

CMD ["/entrypoint.sh"]
