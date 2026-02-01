# VAF Docker Services

VAF uses **one** Docker Compose file for auxiliary services: **`docker-compose.memory.yml`** (in the project root).

## Which containers exist?

| Service   | Container name   | Port(s) | Description                          |
|-----------|------------------|---------|--------------------------------------|
| Postgres  | `vaf-memory-db`  | 5432    | Database (pgvector) for Memory/RAG    |
| Redis     | `vaf-redis`      | 6379    | Cache (embeddings, sessions)          |
| Sandbox   | `vaf-sandbox`    | —       | Python sandbox for safe code execution |

These three containers are started **only** via `docker-compose.memory.yml`. There is no separate `docker-compose.yml` for them.

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

## Auto-start with VAF

When you start VAF (Desktop shortcut or `vaf tray`), the tray will try to bring up the memory stack automatically if Docker is available. You can also start it manually with the command above.

## Volumes (data is preserved)

- `vaf_memory_pgdata` – PostgreSQL data
- `vaf_redis_data` – Redis data
- `vaf_sandbox_workspace` – Sandbox working directory

Running `docker compose -f docker-compose.memory.yml down` removes only the containers, not the volumes. Data is kept.
