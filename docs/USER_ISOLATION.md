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

When running locally without authentication (CLI or Web UI without JWT), VAF uses the scope and username from config:

- **`local_admin_scope_id`**: Default `00000000-0000-0000-0000-000000000001` (legacy placeholder). After the first admin is created via `POST /api/auth/bootstrap`, the backend writes that admin's UUID here so CLI and localhost use the same identity as the logged-in admin.
- **`local_admin_username`**: Default `admin`; updated by bootstrap to the first admin's username.

Use `get_local_admin_scope_id()` and `get_local_admin_username()` from `vaf.core.config` instead of reading config directly. This keeps data scoped consistently and avoids a split between "logged-in" and "local" identities.

### Hybrid Scoping Strategy (Local Mode Stability)

To bridge the gap between strict multi-tenant isolation and a seamless local UX, VAF uses a **Hybrid Scoping Strategy**. This is especially important for long-lived connections like Email and WhatsApp.

**The Problem:** In local mode, a user might set up Email under one UUID, then clear their browser cache, getting a new UUID. Without fallbacks, the Agent would think no accounts are connected.

**The Solution:**
- **Read Operations (Lookup):** Tools follow a lookup chain (Scope → Legacy → Single-other-scope). This makes the system "self-healing" against UUID changes in local mode.
- **Write Operations (Update/Auth):** When tokens are refreshed or new data is synced, the system writes back to the **effective scope** (where the credentials were actually found). This prevents data fragmentation.

**Best Practices for Developers:**
1.  **Trust the Fallbacks:** Use helpers like `get_valid_access_token()` or `_get_email_config()` which already implement the fallback logic. Do not implement manual string comparisons with `"admin"`.
2.  **Propagate the ID:** Always pass the `user_scope_id` down to internal transport functions so they can choose the correct credential bucket.
3.  **Use Effective Scopes:** If you find data in a fallback scope, ensure any updates (like token refreshes) are saved back to that same fallback scope to maintain consistency.

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

### Web UI session isolation

Chat sessions in the Web UI are isolated by `user_scope_id`:

- **Session list:** `SessionManager.list(limit, user_scope_id=...)` is called with the connection's user scope (from `manager.get_connection_user(websocket)`). Users only see sessions that have matching `metadata.user_scope_id` or no scope (legacy/local admin).
- **Load session:** Before subscribing to a session, the backend verifies ownership: the session's `metadata.user_scope_id` must match the current user, or the user must be the local admin. Otherwise the server responds with "Access denied".
- **Default session:** When no session is selected, the fallback session ID is per-user (`web-default-<scope>`), not a shared global ID.
- **Broadcasting:** Updates are sent only to connections subscribed to that session (`broadcast_to_session`); session list refreshes are sent only to that user's connections (`broadcast_to_user`). See [SESSION_MANAGEMENT.md](SESSION_MANAGEMENT.md).

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

### Scope-based paths (preferred)

User-scoped data stores use UUID-based directories. This is the preferred path for all data isolation:

```
~/.vaf/scopes/<user_scope_id>/
├── email_sync.db              # Synced email messages (SQLite)
├── contacts.json              # User's contact list
├── whatsapp_messages.db       # WhatsApp message history (SQLite)
└── ...
```

The local admin's data remains at the legacy root paths (`~/.vaf/email_sync.db`, `~/.vaf/contacts.json`) since `local_admin_scope_id` maps to the global location.

### User workspace (legacy)

Each user also has a username-based directory tree. This is the legacy layout, preserved for backward compatibility. New features should prefer scope-based paths above.

```
~/.vaf/users/<username>/
├── user_identity.json      # Personal preferences
├── identity.json            # Agent persona config
├── automations/             # Automation tasks (global + per-user)
│   └── <user_scope_id>/    # One .json file per task
├── auth/                    # Connection credentials
│   ├── whatsapp/
│   ├── telegram/
│   └── email/
└── ...
```

A one-time migration script (`scripts/migrate_users_to_scopes.py`) copies data from `users/<username>/` to `scopes/<user_scope_id>/`. The old directories are preserved for verification and can be removed manually.

### Automations (`vaf/core/automation.py`)

Each `AutomationManager` instance can be created with a `user_scope_id`; tasks are stored in `automations/<user_scope_id>/` (per-user). 

**Role-Based Access:**
- **Admins:** Users with the `admin` role (including the local admin) see a **merged view** of their own scoped tasks and any legacy "root" tasks stored directly in `automations/`. This ensures backward compatibility for existing installations.
- **Regular Users:** Restricted to their own `user_scope_id` subdirectory. They cannot see or modify root tasks or other users' tasks.

**Execution & Tools:**
Tasks carry `user_scope_id` so that when an automation runs (prompt-based or workflow-based), the agent and workflow engine use that scope: RAG/memory, calendar, messaging, contacts, mail, and automation notes/todos all run with the owner's credentials and data. The agent injects `user_scope_id` into automation tools (`create_automation`, `list_automations`, etc.) so new tasks are stored in the correct user directory. The CLI/scheduler uses an aggregated manager that loads from all scope dirs and saves/deletes/restores via the task's scope path.

**Global slot limit:** A given time slot (same HH:MM + frequency, e.g. daily 08:15) may be used by at most **3 users**. If three users already have an automation at that slot, a fourth gets an error: *"Too many other users have already booked this time slot. Please choose another slot at least 15 minutes apart."* This avoids overloading the scheduler at popular times while keeping automations user-specific.

### Automation planner – notes and todos (`vaf/core/automation_planner.py`)

Notes and to-dos for the automation calendar are stored per user under `Platform.vaf_dir() / "automation_planner" / <user_scope_id> /` (or `_default` when no scope): `notes.json` and `todos.json`. All planner API functions take `user_scope_id`; the Web UI and agent tools use the same scope so that the calendar shows only the current user's data.

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

Uses **per-user keyring credentials** encrypted with AES-256-GCM. Each user's IMAP/SMTP sessions use their own stored credentials. Credential keys include the `user_scope_id` when set (format: `email:{provider}:{scope_id}:{account_id}`), falling back to username-based keys for legacy data.

### Calendar (Google / Microsoft)

Calendar uses the **same OAuth credentials and the same `user_scope_id`** as Email. There are no separate calendar credential keys. The calendar client (`vaf/core/calendar_client.py`) and calendar tools call `get_valid_access_token(..., user_scope_id=user_scope_id)` and use the same account list from `email_config` / `email_config_by_scope`. All calendar API calls are therefore scoped per user.

Email config lookup follows a three-tier chain:
1. `email_config_by_scope[user_scope_id]` — preferred, UUID-based
2. `email_config_by_user[username]` — legacy per-user
3. `email_config` — legacy global/admin fallback

When the primary lookup returns no accounts (e.g. chat session uses local admin but accounts were added under a JWT scope), the tools fall back to legacy `email_config` and, in single-scope setups, to the single scope in `email_config_by_scope`, so the Mail dashboard and agent see the same accounts. The sync store (messages) uses the same idea: the tool tries the primary store, then legacy and single-scope stores, so it reads from the same DB as the Mail dashboard.

Synced messages are stored per-scope in `scopes/<user_scope_id>/email_sync.db` (or legacy path for local admin).

### Config: global vs user-scoped

- **Global (admin-only to change):** Backend and network settings apply to all users. Only admins can edit them. This includes: Network tab (local network, ports, TLS, hosting), Advanced tab (server, tray, timeouts, etc.), API keys and provider/model settings, OAuth client IDs, TTS/STT URLs, and similar server-wide options. Stored in the single `config.json`; non-admin PATCH and WebSocket `save_config` are filtered so these keys are not overwritten.
- **User-specific:** Connections (Mail, WhatsApp, Telegram, Discord, Cloud, Calendar, GitHub), language/interface preferences, and automations are per user. Non-admins can change only the keys that are not in the global set (e.g. language, time format). Connection data is already keyed by `user_scope_id` or username where applicable.

The Settings UI shows the **Network** and **Advanced** tabs only to admins; all users see Connections, Automations, Interface, etc., and receive the same global config for display/behavior without being able to change it.

## Isolation Summary Table

| Component | Isolation mechanism | Level |
|-----------|---------------------|-------|
| Memory CRUD | `user_scope_id` filter on every query | Application |
| Memory graph | Scope filter on auto-connect and manual operations | Application |
| Gateway | Server-side scope extraction, client scope stripped | Transport |
| Redis cache | Scope-prefixed cache keys | Caching |
| PostgreSQL | Row-Level Security policy | Database |
| Filesystem | Scope-based paths (`~/.vaf/scopes/<user_scope_id>/`) preferred; legacy `~/.vaf/users/<username>/` as fallback | OS |
| Sandbox | Per-user working directory in Docker | Container |
| WhatsApp | Separate subprocess per user | Process |
| Telegram | Whitelist-based routing | Application |
| Email | Per-user encrypted credentials + scope-based config lookup chain | Application |
| Calendar (Google/Microsoft) | Same OAuth and `user_scope_id` as Email; no separate credentials | Application |
| Automations | Per-user task storage and scoped RAG access; max 3 users per time slot (global cap) | Application |
| Automation planner (notes/todos) | Per-user `automation_planner/<scope>/notes.json`, `todos.json` | Application |
| Config (global vs user) | Backend/network/API keys: admin-only write; non-admins can change only user-scoped settings | Application |

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
