# VAF Docker Services

VAF uses **one** Docker Compose file for auxiliary services: **`docker-compose.memory.yml`** (in the project root).

## Service Overview

| Service | Container | Port(s) | Description |
|---------|-----------|---------|-------------|
| PostgreSQL | `vaf-memory-db` | 5432 | Database (pgvector) for Memory/RAG and Auth/User DB |
| Redis | `vaf-redis` | 6379 | Cache (embeddings, sessions) |
| Sandbox | `vaf-sandbox` | - | Python sandbox for safe code execution |
| Gotenberg | `vaf-gotenberg` | 5005 | LibreOffice-based Office→PDF (DOCX, XLSX, PPTX, ODT, ODS, ODP) |
| TTS Multi-Lang | `vaf-tts` | 5002 | Piper TTS (single container, multi-language, on-demand model install; local speech lane only) |
| STT | `vaf-stt` | 5003 | Whisper ASR for speech-to-text (local speech lane only) |
| **Browser** | `vaf-browser` | 9222, 6901 | Headed Chromium under KasmVNC's X server (CDP on 9222, anti-bot hardened) for the `browser_agent` tool, plus the KasmVNC WebSocket stream of the display on 6901 for the web UI's interactive browser - see [BROWSER_AGENT.md](../agents/BROWSER_AGENT.md) |

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

> **Redis needs `REDIS_PASSWORD`.** The compose file starts Redis with
> `--requirepass` only when that variable is set, while VAF's client always sends the
> password it keeps in the data keyring. VAF writes the value into an owner-only,
> gitignored `.env` beside the compose file (`vaf/core/service_stack.py`) before every
> `compose up` it drives, and `docker compose` reads that file automatically. Starting
> the stack by hand on a machine that has never run VAF therefore brings Redis up with
> no password, and the cache calls then fail to authenticate. Let VAF start the stack
> once, or export `REDIS_PASSWORD` yourself before running compose.

### Verify Running Containers

```bash
docker ps --filter "name=vaf-"
```

Expected output:
```
CONTAINER ID   IMAGE                      PORTS                          NAMES
...            vaf-tts-multilang:latest   127.0.0.1:5002->5000/tcp       vaf-tts
...            whisper-asr-webservice     127.0.0.1:5003->9000/tcp       vaf-stt
...            pgvector/pgvector:pg16     127.0.0.1:5432->5432/tcp       vaf-memory-db
...            redis:7-alpine             127.0.0.1:6379->6379/tcp       vaf-redis
...            vaf-sandbox                                                vaf-sandbox
```

> **Security:** All ports are bound to `127.0.0.1` - reachable only locally, not on the LAN or the internet. If `0.0.0.0` is shown, the ports are open on all network interfaces; in that case check `docker-compose.memory.yml` and make sure every port mapping uses the `127.0.0.1:PORT:PORT` format.
>
> **Network isolation:** Each container joins only the Docker network it needs:
> - `vaf-network`: postgres, redis, tts, stt, gotenberg
> - `vaf-sandbox-network`: sandbox (no access to postgres/redis)
> - `vaf-browser-network`: vaf-browser (no access to postgres/redis - SSRF protection)

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

The password is **not** part of `redis_url`: it lives in the data keyring and is spliced
into the URL at connect time (`vaf/memory/cache.py`), and handed to the container through
`REDIS_PASSWORD` in the compose `.env`. A password written into `redis_url` by hand wins
and is left alone.

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

Important: Gotenberg is a **preview/rendering** service, not the mutable editing engine for the native DOCX editor. The native DOCX editor uses VAF's own DOCX-first document model and dedicated load/save endpoints; Gotenberg remains responsible for high-fidelity Office-to-PDF rendering.

**License:** Gotenberg is MIT; LibreOffice is MPL 2.0 – both are permissive/weak-copyleft and compatible with an AGPL-3.0 project like VAF.

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
docker compose -f docker-compose.memory.yml up -d postgres redis
```

### Restart TTS After Configuration Change

```bash
docker compose -f docker-compose.memory.yml restart tts
```

---

## Auto-Start & Smart Update

### During Installation (`install.sh` / `install.ps1`)

A runtime is **required**: VAF keeps users, auth, setup and memory in a PostgreSQL/pgvector container, so it
is needed to finish setup and sign in. The installer uses whatever engine you already have and otherwise
sets up a free one for you - no Docker Desktop licence needed:

- **Windows**: auto-installs **Rancher Desktop** (engine `moby`).
- **macOS**: uses **Docker Desktop** if installed, otherwise **auto-installs and starts Colima** via Homebrew.
- **Linux**: auto-installs the distro Docker package, enables it via systemd, and adds you to the `docker` group (uses an existing Docker if already present).

When a runtime is present (or has just been set up), the installer manages the stack:

1. **Change Detection**: After a `git pull`, the installer checks whether `docker-compose.memory.yml` has changed (via `git diff HEAD~1 HEAD`).
2. **Auto-Start the Engine**: If the engine is installed but not running, the installer starts it automatically:
   - **macOS**: starts Docker Desktop if present, otherwise `colima start`
   - **Linux**: `sudo systemctl start docker` (or `sudo service docker start`)
   - **Windows**: starts Rancher Desktop (and does **not** restart it if it is already running)
3. **Wait for Readiness**: The installer polls until the daemon is responsive (up to ~60–120s on a first Colima boot).
4. **Apply Changes - two-phase**: it brings up the core registry-image services first
   (`postgres redis sandbox stt gotenberg`) so a slow local build of `tts`/`vaf-browser` can never block the
   database the app needs to boot, then starts those optional services best-effort with `--build`.
   `up -d`:
   - Starts new services (e.g., Gotenberg after an update that adds it)
   - Recreates services whose configuration changed
   - Leaves unchanged, running services untouched
   - Does NOT rebuild local images on its own, which is why `tts` and `vaf-browser` are started
     with `--build`: they are built from this repository rather than pulled, so without it a
     checkout that moves ahead of its images keeps running the old ones and nothing says so.
     An unchanged build context is a cache hit and costs seconds. Note that `vaf update` never
     builds an image either, so an image cannot be repaired by updating.
   - The cache hit has a second face for the browser: its apt layer installs an UNPINNED
     Debian Chromium, and a cached `--build` never re-runs that layer, so the browser engine
     would age forever while `--build` reports success. The age gate closes that class: once
     the `vaf-browser` image is older than `browser_image_max_age_days` (default 14, admin-only,
     `0` = off), the start runs one `build --pull --no-cache vaf-browser` first, so the base
     image and Chromium are actually refreshed. A failed fresh build never blocks the start;
     it is recorded as a `browser_image_stale` security event and the old image keeps serving.

> **Note:** Data in named volumes (e.g., `vaf_memory_pgdata`) is never lost during `up -d`. Only container images and configuration are updated.

### When VAF Starts (`vaf tray` or `vaf run`)

When you start VAF (Desktop shortcut, `vaf tray`, or the terminal app `vaf run`), it brings up the Docker stack if Docker is available. The lifecycle lives in one place, `vaf/core/service_stack.py`: engine bootstrap (macOS starts Docker Desktop or Colima, Windows starts Rancher/Docker Desktop), then a two-phase compose up - core registry-image services first (postgres, redis, sandbox, stt, gotenberg), the locally built ones (tts, vaf-browser) best-effort afterwards, so a failed local build can never take the database down with it. `vaf run` starts the stack in the background so booting the model never waits on a compose up.

Note that quitting the TRAY stops the stack (containers and data survive for a fast restart) - which is exactly why the terminal app starts it too: a terminal-only session after a tray quit used to run against a dead memory database. Without a compose file (a pip install ships none) or without Docker, the start is skipped and the memory tools name the unreachable database instead of pretending the memory is empty.

### If Docker Wasn't Running During Install

Start Docker, then apply the latest stack manually:

```bash
docker compose -f docker-compose.memory.yml up -d
```

---

## Status and Repair

```bash
vaf repair --check     # report the status, change nothing
vaf repair             # start what is stopped, restart what does not answer
vaf repair --json      # the same result as machine-readable JSON
```

The same run is available as `/repair` inside the terminal app and under
**Settings -> Advanced -> Update and Repair** in the Web UI.

`vaf/core/service_health.py` answers the two questions that come after starting the
stack: what state is each service in, and what can be done about a broken one. It is
the single implementation behind every surface that asks (the terminal, the TUI, and
the admin dialog in the web UI), and it needs no Docker to be tested because every
probe is injectable.

**What a status snapshot reports** (`collect_service_status()`), per service from
the registry (`SERVICES` in `vaf/core/service_stack.py`, one entry per compose
service):

| Field | Meaning |
|---|---|
| `exists` / `running` | from a single `docker inspect` over all containers |
| `health` | the container's own compose health check (`healthy`, `unhealthy`, `starting`, `none`) |
| `host_ports` | what the container actually publishes |
| `configured_port` / `port_mismatch` | the port VAF is configured to reach it on, and whether the two disagree |
| `probe_ok` | whether the service ANSWERED (TCP connect, HTTP GET, or `SELECT 1` for the database) |
| `starting` / `starting_seconds_left` | the container is inside its own start window: booting, not broken |
| `state` | `ok`, `warn`, `error`, `absent`, or `unknown` when the daemon is down |
| `reason` | one sentence, the same text the dialog and the terminal print |

**Still starting is not broken.** Right after a start a database does not answer
yet, so a status that called that "does not answer" would send someone to a
repair button for something that needs a few more seconds. A container counts as
starting while docker's own health status says `starting`, or while it is inside
the `start_period` from the compose file. Both numbers come from the container:
the start periods differ per service (30s for the database, 120s for the speech
containers), so any single figure VAF invented would be wrong for most of them.
The snapshot repeats this at the top level as `starting` and
`starting_seconds_left` so a caller does not have to re-derive it, which is what
lets the Repair button wait and count down instead of offering to fix a stack
that is already on its way up.

A running container is not a reachable one: the probe is what catches a firewall
dropping loopback traffic, a service still loading, and a port published somewhere
other than where VAF looks. When the Docker daemon is unreachable, nothing else is
attempted - no inspect, no probes - so a status call costs one `docker info` instead
of seven timeouts.

**What a repair run does** (`repair_service_stack()`), in order, reporting every step
as it finishes:

1. **Daemon.** Reachable? If not, why: no docker CLI at all, a socket this user may
   not read (the Linux `docker` group case), or a daemon that is simply down. Only
   the last one is started automatically (macOS Docker Desktop or Colima, Windows
   Rancher or Docker Desktop, never restarting a runtime that is already running);
   it then waits up to 120 seconds for readiness.
2. **Compose file.** None found means a pip install, which manages no stack. That is
   reported honestly, not as a failure to fix.
3. **Missing or stopped containers.** One idempotent `compose up` for the whole
   stack, not one command per container.
4. **Running but unreachable, or unhealthy.** `docker restart -t 5 <container>` -
   unless the container is still starting, which is left alone: restarting
   something that is booting throws away the progress it has made and begins
   the wait again.
5. **Port mismatch.** Reported with both numbers and the config key that carries the
   expectation. Never corrected: which port VAF talks to is a configuration decision,
   and a restart cannot make two different numbers agree anyway.
6. **Still unreachable afterwards.** The OS firewall hint for this platform (the
   `DOCKER-USER` chain on Linux, Defender on Windows, the application firewall on
   macOS). Detection only.

**What repair never does:** no `compose down`, no volume or image removal, no config
writes, no restart of a container runtime that is already running, no restart of a
container that is still inside its start window, and no privilege escalation - a daemon needing `sudo systemctl start docker` gets a named instruction
instead of a sudo attempt.

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

The `vaf-tts` and `vaf-stt` containers are required only for the local speech lane
(`speech_tts_provider` / `speech_stt_provider` empty). When a cloud voice provider is
selected in Settings > Voice (TTS: ElevenLabs or OpenAI; STT: Veyllo, ElevenLabs, or
OpenAI), speech works without the matching container. See
[SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md).

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

# Test Redis (the container knows its own password via $REDIS_PASSWORD)
docker exec -it vaf-redis sh -c 'redis-cli ${REDIS_PASSWORD:+-a "$REDIS_PASSWORD"} ping'
# Should return: PONG
```

A bare `redis-cli ping` answers `NOAUTH Authentication required` whenever the container
was started with a password.

---

## Related Documentation

- [SPEECH_FEATURES.md](../web-ui/SPEECH_FEATURES.md) - Detailed speech integration documentation
- [MEMORY_SYSTEM.md](../memory/MEMORY_SYSTEM.md) - Memory and RAG documentation
- [SANDBOXING.md](../security/SANDBOXING.md) - Sandbox security documentation
