# User Isolation in VAF (Multi-Tenant Security)

This document explains how VAF isolates users from each other when running as a cloud service with multiple authenticated users. It covers every layer of the stack, documents the security mechanisms in place, and provides guidelines for developers building new features.

## Overview

VAF uses a **`user_scope_id`** (UUID) as the universal isolation key. Every user who authenticates receives a unique `user_scope_id` from the auth database. This ID flows through the entire stack — from the WebSocket handshake down to the database row — ensuring that one user can never access another user's data.

```
┌────────────────────────────────────────────────────────────────────┐
│                     USER ISOLATION LAYERS                          │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  Layer 1: Authentication & Identity                                │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  JWT token → request.state.user → user_scope_id (UUID)  │      │
│  │  Server-side extraction only (never trust client)        │      │
│  └──────────────────────────────────────────────────────────┘      │
│                              │                                     │
│  Layer 2: Application Logic (FastAPI)                              │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  Depends(get_current_user_scope) on every route          │      │
│  │  All CRUD operations filter by user_scope_id             │      │
│  └──────────────────────────────────────────────────────────┘      │
│                              │                                     │
│  Layer 3: Caching (Redis)                                          │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  Cache keys prefixed with user_scope_id                  │      │
│  │  e.g. "memory_graph:<scope>:<limit>"                     │      │
│  └──────────────────────────────────────────────────────────┘      │
│                              │                                     │
│  Layer 4: Database (PostgreSQL + RLS)                              │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  Row-Level Security on memories table                    │      │
│  │  SET LOCAL app.current_user_scope_id per transaction     │      │
│  └──────────────────────────────────────────────────────────┘      │
│                              │                                     │
│  Layer 5: Filesystem                                               │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  ~/.vaf/users/<username>/ per user                       │      │
│  │  Automations stored in per-user subdirectories           │      │
│  └──────────────────────────────────────────────────────────┘      │
│                              │                                     │
│  Layer 6: Sandbox (Docker)                                         │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  Per-user working directory: /tmp/vaf_<scope>_<exec_id>  │      │
│  │  Filesystem isolation within shared container            │      │
│  └──────────────────────────────────────────────────────────┘      │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

## 1. Authentication & User Scope Extraction

### How `user_scope_id` is established

When network mode is enabled and a user logs in, the auth system issues a JWT containing the user's `user_scope_id` (UUID), `username`, and `role`. The `AuthMiddleware` (in `vaf/auth/middleware.py`) validates the JWT on every HTTP request and populates `request.state.user` as a consolidated dict:

```python
# Set by AuthMiddleware after JWT validation
request.state.user = {
    "user_id": "<sub>",
    "username": "<username>",
    "role": "<role>",
    "user_scope_id": "<uuid>",
}
```

All API route handlers read `request.state.user` to extract the authenticated user's identity and scope. The WebSocket handler in the gateway accesses the same dict:

```python
# In vaf/core/gateway.py — WebSocket handler
user_state = getattr(websocket.state, "user", None)
if user_state and isinstance(user_state, dict):
    server_user_scope_id = user_state.get("user_scope_id")
```

**Critical rule**: The gateway **strips** any `user_scope_id` sent by the client in the context payload. This prevents impersonation attacks where a malicious client sends another user's scope ID:

```python
context.pop("user_scope_id", None)  # Never trust client-sent scope
```

The `server_user_scope_id` is then passed into `run_agent_step()` and propagated to all downstream services (memory, tools, automations).

### Local mode fallback

When running locally without authentication, VAF uses a fixed scope:

- **`local_admin_scope_id`**: `00000000-0000-0000-0000-000000000001`
- **`local_admin_username`**: `admin`

This ensures that even in local mode, all data is scoped consistently.

## 2. Memory System Isolation

The memory system is the most data-sensitive component. Every memory operation is scoped.

### CRUD Operations (`vaf/memory/rag.py`)

All memory access methods accept and enforce `user_scope_id`:

| Method | Scope enforcement |
|--------|-------------------|
| `get_memory(id)` | Filters by `Memory.user_scope_id == user_scope_id` |
| `update_memory(id)` | Filters by scope before allowing update |
| `delete_memory(id)` | Filters by scope before soft-delete |
| `search_memories()` | Filters query results by scope |
| `store_memory()` | Stamps `user_scope_id` on new records |
| `get_all_memories()` | Filters listing by scope |

If a user tries to access a memory ID that belongs to another user, the query returns `None` (not found) — the same response as if the memory doesn't exist. This prevents information leakage through error messages.

### Graph Connections (`vaf/memory/graph.py`)

Memory auto-connect (which links semantically similar memories) is scoped:

```python
# Only find candidates within the same user's memories
if memory.user_scope_id is not None:
    scope_filters.append(Memory.user_scope_id == memory.user_scope_id)
else:
    scope_filters.append(Memory.user_scope_id.is_(None))
```

Manual connection operations (`update_connections`, `move_memory`, `get_tree_children`) all validate that both source and target memories belong to the same user.

### Routes (`vaf/memory/routes.py`)

Every memory API route uses FastAPI dependency injection to extract the scope:

```python
@router.get("/{memory_id}")
async def get_memory(
    memory_id: UUID,
    user_scope_id: Optional[UUID] = Depends(get_current_user_scope),
    ...
):
```

The `get_current_user_scope` dependency reads from `request.state.user` (the consolidated dict set by `AuthMiddleware`). When no user is authenticated (localhost mode), this dict is absent and the dependency falls back to the local admin scope.

## 3. Cache Isolation (Redis)

All Redis cache keys include the user scope to prevent cross-user cache poisoning:

```python
# In vaf/memory/cache.py
scope_key = user_scope_id or "global"
key = f"{CacheKeys.MEMORY_GRAPH}{scope_key}:{limit}"
```

This applies to:
- Memory graph cache
- RAG query cache
- Embedding cache

Without this, User A could receive cached search results or graph data that was generated for User B.

## 4. Database-Level Security (PostgreSQL RLS)

As a defense-in-depth measure, PostgreSQL Row-Level Security (RLS) is enabled on the `memories` table:

```sql
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;

CREATE POLICY user_isolation_memories ON memories
    USING (
        COALESCE(current_setting('app.current_user_scope_id', true), '') = ''
        OR user_scope_id IS NULL
        OR user_scope_id = current_setting('app.current_user_scope_id', true)::uuid
    );
```

### How it works

1. Before each database transaction, the application sets a session-local variable:
   ```python
   await session.execute(
       text("SET LOCAL app.current_user_scope_id = :scope"),
       {"scope": str(user_scope_id)}
   )
   ```
2. The RLS policy checks this variable against each row's `user_scope_id`.
3. Rows belonging to other users are invisible — even if application-level filtering has a bug.

### Policy logic

| `app.current_user_scope_id` | Row `user_scope_id` | Visible? |
|------------------------------|---------------------|----------|
| Not set / empty              | Any                 | Yes (admin/system access) |
| Set to UUID                  | NULL                | Yes (shared/global memories) |
| Set to UUID                  | Same UUID           | Yes |
| Set to UUID                  | Different UUID      | **No** |

**Important**: The RLS policy uses `SET LOCAL`, which is scoped to the current transaction. This prevents scope leakage between concurrent requests sharing a connection pool.

## 5. Filesystem Isolation

### User workspace

Each user has an isolated directory tree:

```
~/.vaf/users/<username>/
├── user_identity.json      # Personal preferences
├── identity.json            # Agent persona config
├── automations/             # Per-user automation tasks
│   └── <user_scope_id>/
│       └── tasks.json
├── auth/                    # Connection credentials
│   ├── whatsapp/
│   ├── telegram/
│   └── email/
└── ...
```

### Automations (`vaf/core/automation.py`)

Each `AutomationManager` instance is created with a `user_scope_id` and stores tasks in a per-user subdirectory. Tasks carry their `user_scope_id` so that scheduled RAG searches only access the owner's memories.

### Sandbox (`vaf/tools/python_sandbox.py`)

Code execution in the Docker sandbox uses per-user working directories:

```python
scope_prefix = str(user_scope_id).replace("-", "")[:12] if user_scope_id else "shared"
workdir = f"/tmp/vaf_{scope_prefix}_{exec_id}"
```

This prevents users from reading each other's temporary files within the shared sandbox container.

## 6. Connection-Level Isolation

### WhatsApp

Each user runs a **separate Node.js subprocess** with its own authentication directory (`~/.vaf/users/<username>/auth/whatsapp/`). Sessions are completely isolated at the process level.

### Telegram

Uses a **whitelist-based routing model**. The bot is shared, but messages are routed to the correct user based on the Telegram chat ID whitelist stored per user.

### Discord

Currently **single-admin only** — one Discord bot per VAF instance. Not multi-tenant.

### Email

Uses **per-user keyring credentials** encrypted with AES-256-GCM. Each user's IMAP/SMTP sessions use their own stored credentials.

## Isolation Summary Table

| Component | Isolation mechanism | Level |
|-----------|---------------------|-------|
| Memory CRUD | `user_scope_id` filter on every query | Application |
| Memory graph | Scope filter on auto-connect and manual operations | Application |
| Gateway | Server-side scope extraction, client scope stripped | Transport |
| Redis cache | Scope-prefixed cache keys | Caching |
| PostgreSQL | Row-Level Security policy | Database |
| Filesystem | Per-user directory tree (`~/.vaf/users/<username>/`) | OS |
| Sandbox | Per-user working directory in Docker | Container |
| WhatsApp | Separate subprocess per user | Process |
| Telegram | Whitelist-based routing | Application |
| Email | Per-user encrypted credentials | Application |
| Automations | Per-user task storage and scoped RAG access | Application |

## Developer Guidelines: Building New Features

When adding new functionality to VAF, follow these rules to maintain user isolation.

### Rule 1: Always accept and propagate `user_scope_id`

Every function that touches user data must accept `user_scope_id` as a parameter:

```python
# ✅ Correct
async def my_new_feature(data: dict, user_scope_id: Optional[UUID] = None):
    results = await db.execute(
        select(MyModel).where(MyModel.user_scope_id == user_scope_id)
    )

# ❌ Wrong — no scope filtering
async def my_new_feature(data: dict):
    results = await db.execute(select(MyModel))
```

### Rule 2: Use `Depends(get_current_user_scope)` on routes

Every FastAPI route that accesses user-specific data must include the dependency:

```python
@router.get("/my-endpoint")
async def my_endpoint(
    user_scope_id: Optional[UUID] = Depends(get_current_user_scope),
    db: AsyncSession = Depends(get_db),
):
    ...
```

### Rule 3: Never trust client-sent scope

The `user_scope_id` must always come from the server-side session (JWT / `request.state.user`). Never read it from request body, query parameters, or WebSocket message payloads.

### Rule 4: Scope your cache keys

If you add any caching (Redis or in-memory), include `user_scope_id` in the cache key:

```python
# ✅ Correct
cache_key = f"my_feature:{user_scope_id}:{item_id}"

# ❌ Wrong — shared across users
cache_key = f"my_feature:{item_id}"
```

### Rule 5: Scope database queries in new tables

When creating new tables that hold user data:

1. Add a `user_scope_id` column (UUID, nullable for system/shared data).
2. Add an RLS policy mirroring the `memories` table pattern.
3. In `get_db()`, ensure `SET LOCAL app.current_user_scope_id` is called (already handled globally).

```sql
-- Example for a new table
ALTER TABLE my_new_table ENABLE ROW LEVEL SECURITY;

CREATE POLICY user_isolation_my_new_table ON my_new_table
    USING (
        COALESCE(current_setting('app.current_user_scope_id', true), '') = ''
        OR user_scope_id IS NULL
        OR user_scope_id = current_setting('app.current_user_scope_id', true)::uuid
    );
```

### Rule 6: Scope filesystem access

If your feature writes files, place them under the user's directory:

```python
# ✅ Correct
path = Path.home() / ".vaf" / "users" / username / "my_feature" / filename

# ❌ Wrong — shared location
path = Path.home() / ".vaf" / "my_feature" / filename
```

### Rule 7: Validate cross-references

When a feature links two resources (like memory graph connections), validate that both resources belong to the same user:

```python
source = await get_memory(source_id, user_scope_id=scope)
target = await get_memory(target_id, user_scope_id=scope)
if source is None or target is None:
    raise HTTPException(404, "Memory not found")  # Appears as "not found", not "access denied"
```

### Rule 8: Return "not found" instead of "access denied"

When a user tries to access a resource that belongs to another user, return a 404 (not found) response, not a 403 (forbidden). This prevents attackers from discovering that a resource exists.

### Rule 9: Be careful with background tasks

Scheduled tasks, cron jobs, and background workers must carry `user_scope_id` through the entire execution chain. Don't assume scope from the task registration context — store it explicitly in the task definition.

### Rule 10: Test with multiple users

When testing new features, create at least two test users and verify:

- [ ] User A cannot see User B's data
- [ ] User A cannot modify User B's data
- [ ] User A cannot delete User B's data
- [ ] Cache from User A doesn't leak to User B
- [ ] Background tasks for User A don't affect User B

## Known Limitations & Future Work

| Area | Current state | Recommendation |
|------|---------------|----------------|
| Discord | Single-admin only | Implement per-user Discord bot or multi-guild routing |
| Sandbox | Shared Docker container with per-user dirs | Consider per-user containers for stronger isolation |
| Rate limiting | No per-user rate limits | Add per-user rate limiting to prevent abuse |
| Audit logging | No isolation audit trail | Log cross-scope access attempts for security monitoring |
| Memory encryption keys | Shared key across users | Consider per-user encryption keys for stronger data separation |
| WebSocket connections | Shared event loop | Monitor for resource exhaustion by single user |

## Related Documentation

- [USER_IDENTITY.md](USER_IDENTITY.md) — User profile and preferences system
- [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md) — Memory storage and RAG pipeline
- [GATEWAY.md](GATEWAY.md) — WebSocket gateway architecture
- [CONNECTIONS.md](CONNECTIONS.md) — External service connections (WhatsApp, Telegram, etc.)
- [SANDBOXING.md](SANDBOXING.md) — Docker sandbox for code execution
