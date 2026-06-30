# VAF Browser Container
# ======================
# Runs Chromium with Chrome DevTools Protocol (CDP) on port 9222.
# VAF connects to it via ws://localhost:9222 — no Chromium on the host needed.
#
# Stealth: Chromium runs HEADED under a virtual X display (Xvfb) instead of
# --headless=new. Real headed Chrome leaks far fewer automation tells (the new
# headless mode is still detectable), so this is the stronger anti-bot baseline.
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
    xvfb \
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
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN groupadd -r browser && useradd -r -g browser -m browser

# Chromium needs a writable home for profile/tmp
RUN mkdir -p /home/browser/.config/chromium && \
    chown -R browser:browser /home/browser

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER browser
WORKDIR /home/browser

# Health-check: socat proxy must be up and forwarding to Chromium
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -sf http://localhost:9222/json/version || exit 1

EXPOSE 9222

CMD ["/entrypoint.sh"]
