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
    unzip \
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
# TARGETARCH carries NO default on purpose. BuildKit fills the predefined
# platform ARGs only when they declare no value of their own; a default wins
# over the injected value and makes "the builder did not set it" indistinguish-
# able from "the target really is amd64". Measured on Apple Silicon with
# BuildKit active: TARGETPLATFORM=linux/arm64 and dpkg=arm64 arrived correctly,
# while TARGETARCH stayed amd64 and pulled the amd64 .deb, which then failed
# against the arm64 base with "Depends: libxext6:amd64 but it is not
# installable" (exit 100). An empty value now stops the build LOUDLY instead of
# guessing amd64 and producing an image that cannot run on this host.
ARG TARGETARCH
ARG KASMVNC_VERSION=1.5.0
ARG KASMVNC_SHA256_AMD64=770fd3df51510beecc89666879d82faf411276e68c6e11df612f736b891b5f71
ARG KASMVNC_SHA256_ARM64=aa83a1a6c9069d1a02239988b07a3a2a082a433042b4d4ee2b9e9f6b2df9643c
RUN set -e; \
    case "$TARGETARCH" in \
      amd64) KASM_SHA="$KASMVNC_SHA256_AMD64" ;; \
      arm64) KASM_SHA="$KASMVNC_SHA256_ARM64" ;; \
      "")    echo "FATAL: TARGETARCH is empty, so the target architecture is unknown." >&2; \
             echo "       BuildKit is the only builder that fills it. Build through compose:" >&2; \
             echo "         docker compose -f docker-compose.memory.yml build vaf-browser" >&2; \
             echo "       With the legacy builder (DOCKER_BUILDKIT=0), pass it explicitly:" >&2; \
             echo "         docker build --build-arg TARGETARCH=arm64 docker/browser" >&2; \
             exit 1 ;; \
      *)     echo "FATAL: TARGETARCH='$TARGETARCH' is not supported." >&2; \
             echo "       KasmVNC ${KASMVNC_VERSION} ships bookworm packages for amd64 and arm64 only." >&2; \
             exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/kasmvncserver.deb \
      "https://github.com/kasmtech/KasmVNC/releases/download/v${KASMVNC_VERSION}/kasmvncserver_bookworm_${KASMVNC_VERSION}_${TARGETARCH}.deb"; \
    echo "$KASM_SHA  /tmp/kasmvncserver.deb" | sha256sum -c -; \
    apt-get -o Acquire::Check-Date=false update; \
    apt-get install -y --no-install-recommends /tmp/kasmvncserver.deb; \
    rm -f /tmp/kasmvncserver.deb; rm -rf /var/lib/apt/lists/*
COPY kasmvnc.yaml /etc/kasmvnc/kasmvnc.yaml

# uBlock Origin Lite: content/ad blocking for everything this browser loads,
# person-driven and agent-driven alike - fewer trackers, fewer malicious ad
# payloads, less bandwidth. The MV3 declarativeNetRequest build, so blocking is
# rule-based inside the browser with no extension process observing page
# content. Fetched as the official, unmodified release zip from the project's
# GitHub releases (NOT scraped out of the Chrome Web Store, whose terms do not
# cover automated downloads), version-pinned with a verified checksum.
#
# HOW IT IS INSTALLED, because the obvious route is dead: Chromium 150 IGNORES
# --load-extension without a word (measured in this container: flag on the
# command line, files readable, no log line, no extension). The supported lane
# is the one Linux distros use for their packaged extensions: a CRX plus a
# descriptor in /usr/share/chromium/extensions/<id>.json, which the external-
# extensions provider installs and enables on profile startup. Chromium itself
# packs the CRX here at build time with a throwaway key; the extension id is
# derived from that key (sha256 of the DER public key, first 16 bytes, hex
# mapped to a-p), so it is computed rather than hardcoded, and the key is
# deleted afterwards - it signs nothing of lasting meaning. The provider is
# alive only because the entrypoint does NOT pass --disable-default-apps:
# that flag kills exactly this install lane (bisected in this container).
#
# Licence: GPL-3.0. Same boundary as KasmVNC above: the image is built on the
# user's own machine from the unmodified vendor artifact, an aggregate with no
# code linkage into VAF. Publishing a prebuilt image would make Veyllo the
# distributor of this GPL binary too, with the source obligations that carries.
ARG UBOL_VERSION=2026.820.1159
ARG UBOL_SHA256=45642289ed7c2c46dc8a3a20e801e452a1d180a9718be9a1074021d3d5b94205
RUN set -e; \
    curl -fsSL -o /tmp/ubol.zip \
      "https://github.com/uBlockOrigin/uBOL-home/releases/download/${UBOL_VERSION}/uBOLite_${UBOL_VERSION}.chromium.zip"; \
    echo "$UBOL_SHA256  /tmp/ubol.zip" | sha256sum -c -; \
    mkdir -p /tmp/ubol-src; \
    unzip -q /tmp/ubol.zip -d /tmp/ubol-src; \
    grep -q "\"version\": \"${UBOL_VERSION}\"" /tmp/ubol-src/manifest.json; \
    openssl genrsa -out /tmp/ubol-key.pem 2048; \
    HOME=/tmp /usr/lib/chromium/chromium --no-sandbox \
      --pack-extension=/tmp/ubol-src --pack-extension-key=/tmp/ubol-key.pem; \
    test -s /tmp/ubol-src.crx; \
    mv /tmp/ubol-src.crx /opt/ublock-origin-lite.crx; \
    chmod 644 /opt/ublock-origin-lite.crx; \
    EXT_ID=$(openssl rsa -in /tmp/ubol-key.pem -pubout -outform DER \
      | sha256sum | cut -c1-32 | tr '0123456789abcdef' 'abcdefghijklmnop'); \
    mkdir -p /usr/share/chromium/extensions; \
    printf '{\n  "external_crx": "/opt/ublock-origin-lite.crx",\n  "external_version": "%s"\n}\n' \
      "$UBOL_VERSION" > "/usr/share/chromium/extensions/${EXT_ID}.json"; \
    rm -rf /tmp/ubol.zip /tmp/ubol-src /tmp/ubol-key.pem

# DNS over HTTPS to Cloudflare's SECURITY resolver (malware/phishing blocking).
# Deliberately the 1.1.1.2 security variant and NOT the 1.1.1.3 family variant:
# malware filtering is wanted, content censorship is not - this browser is for
# adults. Mode "automatic" with an explicit template: encrypted Cloudflare DNS
# whenever it is reachable, fallback to the container's resolver otherwise
# (which docker-compose points at the same 1.1.1.2, so both lanes end at the
# filtering resolver and a network that blocks DoH degrades instead of dying).
# A managed policy rather than a launch flag: DoH has no stable command-line
# switch, and /etc/chromium/policies/managed is Chromium's documented policy
# path on Linux.
RUN mkdir -p /etc/chromium/policies/managed && \
    printf '{\n  "DnsOverHttpsMode": "automatic",\n  "DnsOverHttpsTemplates": "https://security.cloudflare-dns.com/dns-query"\n}\n' \
      > /etc/chromium/policies/managed/vaf-dns.json

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
