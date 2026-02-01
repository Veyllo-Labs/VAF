# VAF Docker Services

VAF uses **one** Docker Compose file for auxiliary services: **`docker-compose.memory.yml`** (in the project root).

## Which containers exist?

| Service   | Container name   | Port(s) | Description                          |
|-----------|------------------|---------|--------------------------------------|
| Postgres  | `vaf-memory-db`  | 5432    | Database (pgvector) for Memory/RAG **and Auth/User DB** – do not remove volume |
| Redis     | `vaf-redis`      | 6379    | Cache (embeddings, sessions)          |
| Sandbox   | `vaf-sandbox`    | —       | Python sandbox for safe code execution |
| TTS (DE)  | `vaf-tts`        | 5002    | Piper TTS German (Thorsten) |
| TTS (EN)  | `vaf-tts-en`     | 5004    | Piper TTS English (Kusal) |
| TTS (FR)  | `vaf-tts-fr`     | 5006    | Piper TTS French (Siwis) |
| STT       | `vaf-stt`        | 5003    | Whisper STT HTTP |

All services (Postgres, Redis, Sandbox, TTS DE/EN/FR, STT) are started by default when you run `up -d`.

## Starting DB, Redis, and Sandbox

From the project root (where `docker-compose.memory.yml` lives):

```bash
docker compose -f docker-compose.memory.yml up -d
```

**Windows (PowerShell):**
```powershell
docker compose -f docker-compose.memory.yml up -d
```

After starting you should see:

- `vaf-memory-db` (PostgreSQL)
- `vaf-redis` (Redis)
- `vaf-sandbox` (Python sandbox)

Check:

```bash
docker ps --filter "name=vaf-"
```

## Stopping

```bash
docker compose -f docker-compose.memory.yml down
```

## If Docker wasn’t running during install

The installer starts the memory containers only when **Docker is running** during installation. If you start Docker later or the containers are missing:

1. Start Docker Desktop (Windows/macOS) or the Docker daemon (Linux).
2. From the VAF project root run:
   ```bash
   docker compose -f docker-compose.memory.yml up -d
   ```

DB, Redis, and Sandbox will then be available.

## TTS and STT

TTS (Piper) and STT (Whisper) start with the rest of the stack. VAF Tray brings up the full stack (DB, Redis, Sandbox, TTS, STT). This avoids extra memory use if you use local TTS/STT.

- **TTS (multi-language):** One user German, another English, another French – VAF detects language and calls the right TTS container. Run `docker compose -f docker-compose.memory.yml up -d` to start **tts** (German, port 5002), **tts-en** (English, 5004), **tts-fr** (French, 5006). In Settings → Voice set optional URLs: Docker TTS URL (default), URL German (5002), URL English (5004), URL French (5006). Defaults in config: `speech_tts_docker_url_de`, `_en`, `_fr`. Other languages use the default URL.
- **STT:** Set `speech_stt_engine` to `"docker"` and `speech_stt_docker_url` to `http://localhost:5003`. The backend sends recorded audio to the STT container (POST /asr, multipart file). No local faster-whisper or ffmpeg required when using Docker STT.

TTS/STT use their own volumes and do **not** touch Postgres. Do **not** remove `vaf_memory_pgdata` (user/auth DB).

## Auto-start with VAF

When you start VAF (Desktop shortcut or `vaf tray`), the tray will try to bring up the memory stack automatically if Docker is available. You can also start it manually with the command above.

## Volumes (data is preserved)

- `vaf_memory_pgdata` – **PostgreSQL (Memory + Auth/User DB). Do not delete this volume or you lose user accounts and memories.**
- `vaf_redis_data` – Redis data
- `vaf_sandbox_workspace` – Sandbox working directory
- `vaf_tts_models` – TTS German voice cache
- `vaf_tts_models_en` – TTS English voice cache
- `vaf_tts_models_fr` – TTS French voice cache
- `vaf_stt_models` – STT model cache (optional)

Running `docker compose -f docker-compose.memory.yml down` removes only the containers, not the volumes. Data is kept. To remove **only** the speech containers and keep DB/Redis/Sandbox:

```bash
docker compose -f docker-compose.memory.yml stop tts stt
```
