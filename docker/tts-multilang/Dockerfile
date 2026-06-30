# VAF TTS All-in-One Multi-Language Container
# =============================================
# Single container supporting multiple languages via Piper TTS
# Languages are downloaded on-demand based on user configuration
#
# Build: docker build -t vaf-tts-multilang .
# Run: docker run -p 5002:5000 -v vaf_tts_models:/app/models vaf-tts-multilang

FROM python:3.11-slim

# Install system dependencies.
# Acquire::Check-Date=false: tolerate a skewed build-host clock (common in VMs/WSL2/CI),
# otherwise apt rejects freshly-signed repo metadata as "not valid yet". GPG sig still verified.
RUN apt-get -o Acquire::Check-Date=false update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir \
    flask \
    flask-cors \
    piper-tts \
    requests \
    langdetect

WORKDIR /app

# Create directories
RUN mkdir -p /app/models /app/config

# Copy application files
COPY app.py /app/
COPY models.json /app/

# Environment variables
ENV TTS_PORT=5000
ENV TTS_MODELS_DIR=/app/models
ENV TTS_DEFAULT_LANG=de

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -sf http://localhost:5000/health || exit 1

# Run the application
CMD ["python", "app.py"]
