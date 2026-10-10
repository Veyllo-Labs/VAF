# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
#
# The image behind VAF's sandbox environments (vaf/core/environment_image.py builds
# it). One image for every environment kind and for the registries proxy: Python, Node, git, a compiler for packages without a
# wheel, a headless browser for screenshots of what the environment serves, and
# tinyproxy, which the proxy container runs from this same image.
#
# BUILT LOCALLY ONLY, never published as a prebuilt image: tinyproxy is GPL-2.0
# and chromium-headless-shell carries the Chromium licences. Built here, each
# runs as a separate program next to the other Debian packages (an aggregate, no
# code linkage); distributing the image would mean shipping the licence texts and
# the corresponding source with it. docs/legal/THIRD_PARTY.md lists them.
#
# The build needs no context: VAF pipes this file to `docker build -` on stdin,
# because a wheel install has no docker/ directory. Nothing here may COPY.
FROM python:3.12-slim-bookworm

# chromium-headless-shell and not the full browser: the full Chromium's headless
# mode crashes in a container without a crash database (measured with Chromium
# 151: "chrome_crashpad_handler: --database is required", and with crashpad
# switched off an "FD ownership violation"). The headless shell is the separate
# binary Debian builds for exactly this use; measured: a 1280x800 screenshot of a
# page the environment serves in 215 ms, as a non-root user with every capability
# dropped and no network beyond the environment's own.
RUN set -e; \
    apt-get -o Acquire::Check-Date=false update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils git procps less nano ripgrep jq \
      make gcc g++ libc6-dev \
      tinyproxy \
      chromium-headless-shell fonts-dejavu-core; \
    rm -rf /var/lib/apt/lists/*

# Node.js LTS from the official release, version-pinned with a verified checksum
# per architecture. The architecture comes from dpkg, not from TARGETARCH: this
# file is built without BuildKit's platform arguments in every builder VAF may meet.
ARG NODE_VERSION=24.21.0
ARG NODE_SHA256_X64=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6
ARG NODE_SHA256_ARM64=6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2
RUN set -e; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) node_arch=x64;   node_sha="$NODE_SHA256_X64" ;; \
      arm64) node_arch=arm64; node_sha="$NODE_SHA256_ARM64" ;; \
      *) echo "FATAL: no Node.js build for architecture '$arch'" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/node.tar.xz \
      "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    echo "$node_sha  /tmp/node.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
      --exclude='*/CHANGELOG.md' --exclude='*/README.md' --exclude='*/include'; \
    rm -f /tmp/node.tar.xz; \
    node --version; npm --version

# pytest for run_tests, so a test run never installs it first.
RUN pip install --no-cache-dir --disable-pip-version-check pytest==9.1.1

# The image's own user, for hosts that do not map the caller's uid (Windows and
# macOS through Docker Desktop). On Linux VAF runs the environment as the caller's
# uid:gid so files in a mounted project stay theirs; that uid has no passwd entry,
# hence a HOME every uid can write. git refuses a repository owned by another uid
# ("dubious ownership") - in a container that only ever holds the caller's own
# project, that check protects nothing.
RUN set -e; \
    groupadd -g 10001 sandbox; \
    useradd -u 10001 -g sandbox -m -d /home/sandbox -s /bin/bash sandbox; \
    chmod 1777 /home/sandbox; \
    mkdir -p /workspace; chmod 1777 /workspace; \
    git config --system safe.directory '*'

ENV HOME=/home/sandbox \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false
USER sandbox
WORKDIR /workspace
LABEL org.veyllo.vaf.image="sandbox-env"
CMD ["sleep", "infinity"]
