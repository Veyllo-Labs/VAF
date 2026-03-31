# VAF Docker Services

VAF uses **one** Docker Compose file for auxiliary services: **`docker-compose.memory.yml`** (in the project root).

## Service Overview

| Service | Container | Port(s) | Description |
|---------|-----------|---------|-------------|
| PostgreSQL | `vaf-memory-db` | 5432 | Database (pgvector) for Memory/RAG and Auth/User DB |
| Redis | `vaf-redis` | 6379 | Cache (embeddings, sessions) |
| Sandbox | `vaf-sandbox` | — | Python sandbox for safe code execution |
| Gotenberg | `vaf-gotenberg` | 5005 | LibreOffice-based Office→PDF (DOCX, XLSX, PPTX, ODT, ODS, ODP) |
| TTS Multi-Lang | `vaf-tts` | 5002 | Piper TTS (single container, multi-language, on-demand model install) |
| STT | `vaf-stt` | 5003 | Whisper ASR for speech-to-text |

All services start by default when you run `docker compose up -d`.

---

## Quick Start

### Start All Services

```bash
docker compose -f docker-compose.memory.yml up -d
```

**Windows (PowerShell):**
```powershell
docker compose -f docker-compose.memory.yml up -d
```

### Verify Running Containers

```bash
docker ps --filter "name=vaf-"
```

Expected output:
```
CONTAINER ID   IMAGE                      PORTS                    NAMES
...            vaf-tts-multilang:latest   0.0.0.0:5002->5000/tcp   vaf-tts
...            whisper-asr-webservice     0.0.0.0:5003->9000/tcp   vaf-stt
...            pgvector/pgvector:pg16     0.0.0.0:5432->5432/tcp   vaf-memory-db
...            redis:7-alpine             0.0.0.0:6379->6379/tcp   vaf-redis
...            vaf-sandbox                                         vaf-sandbox
```

### Stop Services

```bash
docker compose -f docker-compose.memory.yml down
```

---

## Speech Services (TTS & STT)

### Text-to-Speech (TTS)

VAF provides multi-language TTS via Docker containers using Piper neural voices.

#### Multi-Language Container (`vaf-tts`)

The primary TTS container supports multiple languages with automatic voice selection:

| Language | Voice Model | Quality |
|----------|-------------|---------|
| German (de) | `de_DE-thorsten-high` | High |
| English (en) | `en_US-kusal-medium` | Medium |
| French (fr) | `fr_FR-siwis-medium` | Medium |

**API Endpoint:** `POST http://localhost:5002/synthesize`

```bash
# Test German TTS
curl -X POST http://localhost:5002/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Hallo, das ist ein Test.", "language": "de"}' \
  --output test_de.wav

# Test English TTS
curl -X POST http://localhost:5002/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello, this is a test.", "language": "en"}' \
  --output test_en.wav
```

**Parameters:**
- `text` (required): Text to synthesize
- `language` (optional): `de`, `en`, or `fr` (default: `de`)
- `format` (optional): `wav` or `ogg` (default: `wav`)

#### OGG/Opus Output for Telegram

The TTS container supports OGG/Opus output for Telegram voice messages:

```bash
curl -X POST http://localhost:5002/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello!", "language": "en", "format": "ogg"}' \
  --output test.ogg
```

The conversion uses ffmpeg built into the container - no local installation required.

#### Language Handling

The current compose stack uses one TTS container (`tts`) and installs language voices on demand.

### Speech-to-Text (STT)

VAF uses the `onerahmet/openai-whisper-asr-webservice` container for Whisper-based transcription.

**API Endpoint:** `POST http://localhost:5003/asr`

```bash
curl -X POST "http://localhost:5003/asr?encode=true&output=json" \
  -F "audio_file=@recording.wav"
```

**Response:**
```json
{
  "text": "This is the transcribed text.",
  "language": "en"
}
```

**Supported Input Formats:** WAV, MP3, OGG, WebM, OGA

**Key Feature:** Automatic language detection via `language` field in response.

---

## Database & Cache Services

### PostgreSQL (`vaf-memory-db`)

- **Port:** 5432
- **Purpose:** Memory/RAG storage and Auth/User database
- **Extension:** pgvector for vector similarity search
- **Volume:** `vaf_memory_pgdata`

**Connection String:**
```
postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory
```

> **Warning:** Do not delete `vaf_memory_pgdata` volume - it contains user accounts and memories.

### Redis (`vaf-redis`)

- **Port:** 6379
- **Purpose:** Cache for embeddings and sessions
- **Volume:** `vaf_redis_data`

**Connection URL:**
```
redis://localhost:6379/0
```

---

## Document Conversion (Gotenberg)

The `vaf-gotenberg` container converts Office documents (DOCX, XLSX, PPTX, ODT, ODS, ODP) to PDF using LibreOffice. This enables the Document Viewer to display the original layout with full design fidelity (fonts, colors, images).

**API Endpoint:** `POST http://localhost:5005/forms/libreoffice/convert`  
**Form field:** `files` (multipart file upload)

```bash
# Test DOCX → PDF
curl -X POST http://localhost:5005/forms/libreoffice/convert \
  -F "files=@document.docx" \
  -o result.pdf
```

**Configuration:** `document_conversion_docker_url` in `~/.vaf/config.json` (default: `http://localhost:5005`)

When Gotenberg is running, uploaded Office documents in the Document Viewer are converted to PDF and displayed in their original design. Without Gotenberg, VAF falls back to HTML rendering (python-docx, openpyxl, python-pptx).

**License:** Gotenberg is MIT; LibreOffice is MPL 2.0 – both compatible with MIT+Conclus projects.

---

## Sandbox Service

The `vaf-sandbox` container provides a secure Python environment for code execution.

- **Volume:** `vaf_sandbox_workspace`
- **Purpose:** Safe execution of generated Python code

---

## Volume Management

All data is preserved across container restarts:

| Volume | Purpose | Can Delete? |
|--------|---------|-------------|
| `vaf_memory_pgdata` | PostgreSQL data (users, memories) | **NO** |
| `vaf_redis_data` | Redis cache | Yes |
| `vaf_sandbox_workspace` | Sandbox working directory | Yes |
| `vaf_tts_models` | TTS model cache (all languages) | Yes |
| `vaf_tts_config` | TTS runtime/config cache | Yes |
| `vaf_stt_models` | STT model cache | Yes |

**Stop containers without removing data:**
```bash
docker compose -f docker-compose.memory.yml down
```

**Remove containers AND volumes (data loss):**
```bash
docker compose -f docker-compose.memory.yml down -v
```

---

## Selective Service Management

### Stop Only Speech Services

```bash
docker compose -f docker-compose.memory.yml stop tts stt
```

### Start Only Database and Redis

```bash
docker compose -f docker-compose.memory.yml up -d db redis
```

### Restart TTS After Configuration Change

```bash
docker compose -f docker-compose.memory.yml restart tts
```

---

## Auto-Start & Smart Update

### During Installation (`install.sh` / `install.ps1`)

The installer automatically manages the Docker stack:

1. **Change Detection**: After a `git pull`, the installer checks whether `docker-compose.memory.yml` has changed (via `git diff HEAD~1 HEAD`).
2. **Auto-Start Docker Daemon**: If Docker is installed but not running, the installer attempts to start it automatically:
   - **macOS**: `open -a Docker` (launches Docker Desktop)
   - **Linux**: `sudo systemctl start docker` (or `sudo service docker start`)
   - **Windows**: Launches `Docker Desktop.exe` from Program Files
3. **Wait for Readiness**: The installer waits up to 60 seconds for the Docker daemon to become responsive.
4. **Apply Changes**: Runs `docker compose -f docker-compose.memory.yml up -d`, which:
   - Starts new services (e.g., Gotenberg after an update that adds it)
   - Recreates services whose configuration changed
   - Leaves unchanged, running services untouched

> **Note:** Data in named volumes (e.g., `vaf_memory_pgdata`) is never lost during `up -d`. Only container images and configuration are updated.

### When VAF Starts (`vaf tray`)

When you start VAF (Desktop shortcut or `vaf tray`), the tray will also bring up the Docker stack if Docker is available.

### If Docker Wasn't Running During Install

Start Docker, then apply the latest stack manually:

```bash
docker compose -f docker-compose.memory.yml up -d
```



## Configuration

VAF configuration for Docker services (`~/.vaf/config.json`):

```json
{
  "speech_tts_enabled": true,
  "speech_tts_engine": "docker",
  "speech_tts_docker_url": "http://localhost:5002",
  "speech_tts_docker_url_de": "http://localhost:5002",
  "speech_tts_docker_url_en": "http://localhost:5002",
  "speech_tts_docker_url_fr": "http://localhost:5002",

  "speech_stt_enabled": true,
  "speech_stt_engine": "docker",
  "speech_stt_docker_url": "http://localhost:5003",

  "document_conversion_docker_url": "http://localhost:5005",

  "memory_db_url": "postgresql://vaf:vaf_dev_secret@localhost:5432/vaf_memory",
  "redis_url": "redis://localhost:6379/0",
  "redis_enabled": true
}
```

---

## Building Custom Containers

### Rebuild TTS Multi-Language Container

```bash
cd docker/tts-multilang
docker build -t vaf-tts-multilang:latest .
```

The container includes:
- Piper TTS with ONNX runtime
- Pre-downloaded voice models (DE, EN, FR)
- ffmpeg for OGG/Opus conversion
- Flask API server

---

## Troubleshooting

### Containers Not Starting

```bash
# Check Docker status
docker info

# View container logs
docker logs vaf-tts
docker logs vaf-stt
docker logs vaf-memory-db
```

### TTS Not Responding

```bash
# Test TTS health
curl -X POST http://localhost:5002/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "Test"}' -o /dev/null -w "%{http_code}"
# Should return: 200
```

### STT Returns 422 Error

- Ensure audio file is valid (WAV, MP3, OGG)
- Check field name is `audio_file` (not `file`)
- View STT logs: `docker logs vaf-stt`

### Database Connection Issues

```bash
# Test PostgreSQL
docker exec -it vaf-memory-db psql -U vaf -d vaf_memory -c "SELECT 1;"

# Test Redis
docker exec -it vaf-redis redis-cli ping
# Should return: PONG
```

---

## Related Documentation

- [SPEECH_FEATURES.md](./SPEECH_FEATURES.md) - Detailed speech integration documentation
- [MEMORY_SYSTEM.md](./MEMORY_SYSTEM.md) - Memory and RAG documentation
- [SANDBOXING.md](./SANDBOXING.md) - Sandbox security documentation
