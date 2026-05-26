# VAF Browser Container
# ======================
# Runs headless Chromium with Chrome DevTools Protocol (CDP) on port 9222.
# VAF connects to it via ws://localhost:9222 — no Chromium on the host needed.
#
# Build (automatic via docker compose):
#   docker compose -f docker-compose.memory.yml build vaf-browser
#
# Standalone test:
#   docker run --rm -p 9222:9222 vaf-browser
#   curl http://localhost:9222/json/version

FROM debian:bookworm-slim

# Install Chromium + minimal system fonts (needed for JS-heavy pages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-sandbox \
    fonts-liberation \
    fonts-dejavu-core \
    libatk-bridge2.0-0 \
    libnss3 \
    libxss1 \
    ca-certificates \
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
