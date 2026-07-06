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
│  │  Access JWT honored before any localhost short-circuit   │      │
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
│  │  Row-Level Security (forced, fail-closed) on memories    │      │
│  │  App connects as non-superuser vaf_app (RLS enforced)    │      │
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

The integrated HTTPS proxy relays WebSocket traffic to the backend with `max_size=None` so large `history_update` frames are not truncated; this loopback-proxy path is where the JWT-over-loopback identity handling lives. See [NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md) and [WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md) for the transport detail.

The `server_user_scope_id` is then passed into `run_agent_step()` and propagated to all downstream services (memory, tools, automations).

**Token before localhost short-circuit.** `AuthMiddleware` extracts and honors a presented access JWT **before** any localhost short-circuit, so a valid token always establishes the real identity regardless of peer IP. A tokenless localhost request leaves `request.state.user` unset (internal IPC / single-user desktop). This is what lets a LAN user proxied over loopback by the integrated HTTPS proxy get **their** scope instead of the local admin's: because the token is read before the localhost branch, a remote user arriving over the loopback proxy is identified by their own token rather than inheriting the local-admin identity.

### Local mode fallback

The local-admin floor for a missing scope applies **only in genuine single-user/local mode** (`local_network_enabled` false). In server/multi-user mode a missing scope **denies** rather than flooring to the admin scope, so an unauthenticated request never inherits the admin's data.

When running locally without authentication (CLI or Web UI without JWT), VAF uses the scope and username from config:

- **`local_admin_scope_id`**: Default `00000000-0000-0000-0000-000000000001` (legacy placeholder). After the first admin is created via `POST /api/auth/bootstrap`, the backend writes that admin's UUID here so CLI and localhost use the same identity as the logged-in admin.
- **`local_admin_username`**: Default `admin`; updated by bootstrap to the first admin's username.

Use `get_local_admin_scope_id()` and `get_local_admin_username()` from `vaf.core.config` instead of reading config directly. This keeps data scoped consistently and avoids a split between "logged-in" and "local" identities.

**Where the binding happens.** For channel and WebSocket clients the gateway (`run_agent_step` in `vaf/core/gateway.py`) applies this fallback. The interactive CLI (`vaf run` and `vaf prompt`) does **not** pass through the gateway — it calls `Agent.chat_step()` directly — so it binds the local-admin scope and username explicitly at agent creation, via `_make_cli_agent()` in `vaf/cli/cmd/run.py`. Without this the CLI would run under scope `None` (the `"default"` bucket) and diverge from the WebUI admin: a stale `last_interaction` and memory/RAG that cannot see the admin's data. The binding is re-applied on every agent (re)creation, because `Agent.__init__` does not set a scope.

### Hybrid Scoping Strategy (Local Mode Stability)

To bridge the gap between strict multi-tenant isolation and a low-friction local experience, VAF uses a **Hybrid Scoping Strategy**. This is especially important for long-lived connections like Email and WhatsApp.

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

### Fail-closed scope resolution

Memory reads fail **closed** when no scope is available. `RagPipeline.search()` returns `[]` for an empty scope in **both** the vector and the lexical/hybrid lanes — a missing scope means **no results**, never "search all". `run_memory_search_sync` resolves a concrete scope up front and **denies** (returns nothing) when no scope is present in server/multi-user mode, flooring to the local-admin scope only in genuine single-user/local mode. An unparseable scope is treated as a deny as well, so a missing or malformed scope yields no results rather than searching across all users.

### CRUD Operations (`vaf/memory/rag.py`)

All memory access methods accept and enforce `user_scope_id`:

| Method | Scope enforcement |
|--------|-------------------|
| `get_memory(id)` | Filters by `Memory.user_scope_id == user_scope_id` |
| `update_memory(id)` | Filters by scope before allowing update |
| `delete_memory(id)` | Filters by scope before soft-delete |
| `search_memories()` | Filters query results by scope; an empty scope returns `[]` (fail-closed) in both vector and lexical/hybrid lanes — never "search all" |
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

The `get_current_user_scope` dependency reads the scope from `request.state.user` when a JWT identity was established. When no user is authenticated, the fallback is mode-dependent: in server/multi-user mode (`local_network_enabled` true) it returns `None` so the RAG layer fails **closed** (an unscoped request must never see another user's data), and only in genuine single-user/local mode does it fall back to `local_admin_scope_id`. A malformed scope is treated defensively as no scope.

### Web UI session isolation

Chat sessions in the Web UI are isolated by `user_scope_id`:

- **Session list:** `SessionManager.list(limit, user_scope_id=...)` is called with the connection's user scope (from `manager.get_connection_user(websocket)`). Users only see sessions that have matching `metadata.user_scope_id` or no scope (legacy/local admin). Note: session-list visibility and command authorization apply different rules to legacy (no-scope) sessions. The list still shows a no-scope session to every user, but the ownership gate treats a no-scope session as admin-only when acting on it (subscribe/chat/delete/rename/hide/edit). In practice no scope-less sessions exist on disk, so this is a strict default rather than a change in observed behavior.
- **Session-command ownership:** A single shared ownership gate runs before the first side effect of every Web UI session command — chat (before subscribing to the session stream), load, delete, rename, hide, and artifact edit. The session's `metadata.user_scope_id` must match the current user, or the connection must be admin (connection role `admin` or the local-admin scope). A session with no recorded `user_scope_id` is treated as admin-only for these commands. On denial the server logs and replies with `{"type":"error","message":"Access denied"}` and keeps the connection open.
- **Owner re-stamp (defense-in-depth):** When a queued chat is processed, the runner stamps `user_scope_id` onto the session only if the session has none yet; it never relabels an already-owned session, so a queued chat cannot take over another user's session behind the gate.
- **Default session:** When no session is selected, the fallback session ID is per-user (`web-default-<scope>`), not a shared global ID.
- **Broadcasting:** Updates are sent only to connections subscribed to that session (`broadcast_to_session`); session list refreshes are sent only to that user's connections (`broadcast_to_user`). See [SESSION_MANAGEMENT.md](../memory/SESSION_MANAGEMENT.md).
- **Agent context store:** Each chat's working memory — intent, plan, tasks, notes, and team state — is stored per session under `.vaf/main/sessions/<session_id>/`, so it is isolated between chats (and therefore between users). See [SESSION_MANAGEMENT.md](../memory/SESSION_MANAGEMENT.md) and [CONTEXT_GLUE.md](../memory/CONTEXT_GLUE.md).

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

PostgreSQL Row-Level Security (RLS) is enabled and forced on the `memories` table and is enforced for every memory data path. The application data connection uses a non-superuser role (`vaf_app`, `NOSUPERUSER`, `NOBYPASSRLS`) via `memory_db_url`, so the policy cannot be bypassed by the app; a separate owner connection (`memory_db_owner_url`, superuser role `vaf`) is used only for DDL, migrations, and global maintenance. The policy is fail-closed:

```sql
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE  ROW LEVEL SECURITY;

CREATE POLICY user_isolation_memories ON memories
    USING      (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid)
    WITH CHECK (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid);
```

### How it works

1. Before each database transaction, the application sets a transaction-scoped variable:
   ```python
   await session.execute(
       text("SELECT set_config('app.current_user_scope_id', :scope, true)"),
       {"scope": str(user_scope_id)}
   )
   ```
   `set_config(..., true)` is the transaction-scoped form of `SET LOCAL`. It is used instead of a literal `SET LOCAL app.current_user_scope_id = :scope` because asyncpg rejects bind parameters in a literal `SET LOCAL` statement.
2. The RLS policy checks this variable against each row's `user_scope_id`.
3. A row is visible or writable only when its `user_scope_id` equals the per-transaction GUC. With a concrete scope set, other users' rows are invisible even if application-level filtering has a bug. With no scope set the GUC is empty, so the policy matches no rows and an unscoped transaction sees and writes nothing (fail-closed); a row whose `user_scope_id` is NULL is not blanket-visible. Because the data connection runs as the non-superuser `vaf_app` role, the database enforces this independently of the application filter.

### Policy logic

| `app.current_user_scope_id` | Row `user_scope_id` | Visible / writable? |
|------------------------------|---------------------|----------|
| Not set / empty              | Any                 | **No** (unscoped session is denied all rows) |
| Set to UUID                  | NULL                | **No** (a NULL-scope row is not blanket-visible) |
| Set to UUID                  | Same UUID           | Yes |
| Set to UUID                  | Different UUID      | **No** |

**Important**: The GUC is set with `set_config(..., true)` (transaction-scoped) on every memory data path — `get_db(user_scope_id=...)` threads the scope through all callers — so it is scoped to the current transaction and never leaks between concurrent requests sharing the connection pool. Because the data connection runs as the non-superuser `vaf_app` role, an unscoped transaction is denied at the database, not merely filtered in the application.

**Note**: this RLS policy is fail-closed and is genuinely enforced, not just a best-effort backstop. A row is visible or writable only when its `user_scope_id` exactly equals the per-transaction GUC; an unset or empty GUC matches nothing (an unscoped transaction sees and writes zero rows), and a row with `user_scope_id IS NULL` is not blanket-visible. RLS is `ENABLE`d and `FORCE`d on `memories`, and the application's data connection uses a non-superuser role (`NOSUPERUSER`, `NOBYPASSRLS`), so the database enforces isolation independently of the application-layer scope filter. The owner role (`vaf`, superuser) bypasses RLS and is used only for DDL, migrations, and global maintenance via the owner engine. The application filter (also fail-closed, see Section 2) is the first line of defense; RLS is a real second one.

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

Because this tree is keyed by the **username** string, a background run must bind the user's **real** account username. Thinking Mode and scheduled Automations resolve the username from `local_users` by `user_scope_id` (and fall back to a synthetic `scope_<8hex>` on an unknown scope) — **never** the literal `"admin"`. Handing a non-admin run the username `"admin"` would make `get_user_workspace("admin")` read `~/.vaf/users/admin/user_identity.json` and inject the admin's personal identity/profile (name, preferences, dos/don'ts, timezone) into that user's system prompt and RAG query seed — exposing the admin's data to that user, even though the memory database itself stays correctly scope-isolated.

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

### Generated projects (`vaf/tools/coder.py`)

When the Coding Agent creates a new project (website, script, document, etc.), it writes to a user- and chat-scoped subdirectory inside the `VAF_Projects` root:

```
~/Documents/VAF_Projects/
├── <user_scope_id[:8]>/         # per-user subdirectory (authenticated users)
│   └── <session_id>/            # per-chat subdirectory (e.g. green432633)
│       ├── My Website/          # project created in this chat
│       └── Game Space Shooter/
└── Demo Website/                # legacy path (local/admin, no session context)
```

- **Authenticated users** (`user_scope_id` present in session metadata): projects are placed under `VAF_Projects/<first-8-chars-of-uuid>/<session_id>/`.
- **Per-chat isolation:** with a session id, each chat gets its own folder, so projects from different chats never mix. The workflow engine builds its project paths the same way.
- **All file-creating sub-agents use the chat folder:** `get_session_workspace_dir` / `resolve_agent_output_dir` (`vaf/core/session.py`) is the shared resolver — the document writer and document agent (previously `VAF_Documents/`), the research agent (previously `VAF_Research/`) and the WebUI workspace browser all resolve through it. Without session context the agents fall back to their legacy directories.
- **Local/admin mode** (no `user_scope_id`): projects go into `VAF_Projects/` (with the `<session_id>/` level when a session id is available).

The prefix is derived from `session.metadata["user_scope_id"]` at project creation time. Existing projects are never moved; only newly created directories use the prefix.

**Unsafe-directory guard:** `is_unsafe_project_dir()` (`vaf/tools/coder.py`) rejects the user's home directory itself, the standard user directories (Documents, Desktop, ...), `~/.vaf` and the VAF program tree as agent work directories — for the CWD heuristic, explicit `project_path` arguments, paths extracted from task text and `git init`. Unsafe paths fall back to the `VAF_Projects` flow.

**Workspace window endpoints:** `GET /api/session/workspace` and `POST /api/session/workspace/upload` (`vaf/core/web_server.py`) enforce session ownership: the session's `metadata.user_scope_id` must match the requesting user (legacy sessions without a scope and the local admin are allowed), otherwise 403. `GET /api/file` additionally refuses downloads from another user's `VAF_Projects/<uid[:8]>/` subtree (local admin exempt; legacy flat projects unaffected); this check is **fail-closed** — if ownership cannot be verified it denies.

**Central Data Explorer endpoints:** `GET /api/workspaces`, `POST /api/workspaces/rename`, and `POST /api/workspaces/delete` (`vaf/core/web_server.py`) back the WebUI "all my workspaces" view. They derive the per-user root `VAF_Projects/<uid[:8]>/` solely from the authenticated user's scope (never a client value), so a user can only ever list, rename, or delete their own workspaces, and they return opaque session-id handles rather than absolute paths. The list includes **orphaned** workspaces — folders left behind when a chat is deleted (deleting a chat removes only the session JSON, not the files) — detected by diffing the folder set against the live session ids. Rename is **display-label only**: it writes a `.vaf_workspace.json` label inside the folder (the on-disk folder name stays the session id, which the resolver keys on) and survives session deletion, so orphans stay renamable. Delete removes the whole folder, boundary-checked to the caller's own root.

### Session workspace (`vaf/core/session.py`, `vaf/core/web_server.py`)

Each chat session has a **stable workspace root** stored in `Session.project_path`. This field is set once (on the first `file_created` event for that session) and never overwritten, giving the session a permanent home directory regardless of how many sub-projects are created later.

- `session.project_path` is only set for paths inside `VAF_Projects/` (temp dirs and one-off outputs are excluded).
- `runtime_state["last_project_path"]` continues to track the most recently created or edited project within the session. Unsafe directories (home dir, `~/.vaf`, ...) are never recorded — and never re-injected into prompts — so sessions that stored such a path before the guard existed self-heal (`is_unsafe_project_dir` checks in `web_server.py` and `headless_runner.py`).
- The agent receives both values as `[SESSION WORKSPACE]` and `[ACTIVE PROJECT]` context lines at the start of each turn (injected by `vaf/core/headless_runner.py`).

### Librarian agent (`vaf/tools/librarian.py`, `vaf/tools/filesystem.py`)

The `librarian_agent` reads the local filesystem to answer "find / list / summarize my files" tasks. By default `is_safe_path` (`vaf/tools/filesystem.py`) only blocks the VAF program tree and a few system directories — it is not user-aware — so without an extra guard the librarian could read across every user's `VAF_Projects/<uid[:8]>/` tree and the whole home directory. A **per-user jail** is therefore layered on top of `is_safe_path`:

- The agent's tool dispatcher injects the caller's `user_scope_id` into the `librarian_agent` call (`vaf/core/agent.py`); `LibrarianTool.run` installs it as a **contextvar** (`set_librarian_scope`) for the duration of the run only, so every other caller (coder, document tools) is unaffected.
- While the jail is active, `is_safe_path` additionally enforces: a **remote (non-admin) user** may read only inside their own `VAF_Projects/<uid[:8]>/`; the **local admin / machine owner** keeps full access (their personal `Downloads`/`Documents`/… included). Any path under another user's `VAF_Projects/<other-uid[:8]>/` is **always denied**.
- The check is **fail-closed**: if the scope cannot be resolved, access is denied. The contextvar is reset in a `finally`, so the jail never leaks into a later run.

### Automations (`vaf/core/automation.py`)

Each `AutomationManager` instance can be created with a `user_scope_id`; tasks are stored in `automations/` (global) or `automations/<user_scope_id>/` (per-user). Tasks carry `user_scope_id` so that when an automation runs (prompt-based or workflow-based), the agent and workflow engine use that scope: RAG/memory, calendar, messaging, contacts, mail, and automation notes/todos all run with the owner's credentials and data. The agent injects `user_scope_id` into automation tools (`create_automation`, `list_automations`, etc.) so new tasks are stored in the correct user directory. The CLI/scheduler uses an aggregated manager that loads from all scope dirs and saves/deletes/restores via the task's scope path.

**Background-run live-emit isolation.** A scheduled automation runs silently and must not surface in another user's live session. With `VAF_IN_AUTOMATION=1`, `_emit_to_web_ui()` is `False` (no status/context/retry emits). Tool start/end updates are not gated by that env, because a concurrent real user's tool updates must keep flowing. Since a background automation agent has no web session of its own, a naive tool emit would fall back to the process-wide "current session" and could surface in whichever user's web session is currently active. To prevent this, a per-agent flag `agent._background_run = True` (set in `run_task`) is checked at both `emit_tool_update` sites so a background run broadcasts no tool bubbles. The flag is per-instance and therefore race-free; gating on the process-wide env would also suppress a concurrent real user's updates.

**Handoff bundle isolation.** When a background automation must ask the user something it cannot decide, it stores its full working history as a *handoff bundle* under `Platform.vaf_dir() / "handoff_bundles" / <user_scope_id> /<id>.json`, keyed by the raw scope id (aligned with `thinking_requests`). The linked tracked request and the bundle are written under the same resolved scope (`user_scope_id or local_admin_scope_id`), so only the **same** user's main agent — finding the request under its own scope — can load the bundle and continue the task; a bundle written for user A is unreadable for user B. See [AUTOMATIONS.md](../platform/AUTOMATIONS.md#silent-background-execution--context-handoff).

**Global slot limit:** A given time slot (same HH:MM + frequency, e.g. daily 08:15) may be used by at most **3 users**. If three users already have an automation at that slot, a fourth gets an error: *"Too many other users have already booked this time slot. Please choose another slot at least 15 minutes apart."* This avoids overloading the scheduler at popular times while keeping automations user-specific.

### Automation planner – notes and todos (`vaf/core/automation_planner.py`)

Notes and to-dos for the automation calendar are stored per user under `Platform.vaf_dir() / "automation_planner" / <user_scope_id> /` (or `_default` when no scope): `notes.json` and `todos.json`. All planner API functions take `user_scope_id`; the Web UI and agent tools use the same scope so that the calendar shows only the current user's data.

### Thinking workspace (`vaf/core/thinking_workspace.py`)

Thinking workspace data is stored per user under `Platform.data_dir() / "workspaces" / <scope_key> /`, where `scope_key` uses the same normalization as Thinking Mode (`local_admin_scope_id` -> `default`, otherwise user scope id). Tasks, run artifacts, handoff proposals, and approval archives are isolated by this key. Workspace path resolution is boundary-checked to prevent cross-scope traversal.

### Sandbox (`vaf/tools/python_sandbox.py`)

Code execution in the Docker sandbox uses per-user working directories:

```python
scope_prefix = str(user_scope_id).replace("-", "")[:12] if user_scope_id else "shared"
workdir = f"/tmp/vaf_{scope_prefix}_{exec_id}"
```

This prevents users from reading each other's temporary files within the shared sandbox container.

### Browser agent session store (`vaf/tools/browser_agent.py`)

The persistent cookie/login store for the browser agent is keyed by user scope at `~/.vaf/browser_sessions/<scope_seg>/<session>.json` (not the old flat shared path), so one user's saved logins are never readable by another even on the same OS account. The agent injects the caller's `user_scope_id` for `browser_agent` calls and propagates it to the killable child process via the `VAF_USER_SCOPE_ID` environment variable, so the child writes and reads under the correct per-user store. See [BROWSER_AGENT.md](../agents/BROWSER_AGENT.md).

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

- **Global (admin-only to change):** Backend and network settings apply to all users. Only admins can edit them. This includes: Network tab (local network, ports, TLS, hosting), Advanced tab (server, tray, timeouts, etc.), API keys and provider/model settings, OAuth client IDs, TTS/STT URLs, and similar server-wide options. Stored in the single `config.json`; non-admin PATCH and WebSocket `save_config` are filtered so these keys are not overwritten. To reduce accidental data loss, config merge also preserves existing sensitive values when an incoming update contains empty API key strings or `null` connection configs.
- **User-specific:** Connections (Mail, WhatsApp, Telegram, Discord, Cloud, Calendar, GitHub), language/interface preferences, and automations are per user. Non-admins can change only the keys that are not in the global set (e.g. language, time format). Connection data is already keyed by `user_scope_id` or username where applicable.

The Settings UI shows the **General**, **AI & Model**, **Advanced**, and **Local Network** tabs only to admins (controlled by the `adminOnly` flag in the `CATEGORIES` array and per-tab content rendering guards). Non-admin users are automatically redirected away from admin-only tabs. All users see Persona, Voice, Interface, Connections, Automations, and About, and receive the global config they need for display/behavior, but it is **credential-redacted**: for non-admins the backend strips secret values (`api_key_*`, `*_client_secret`, `*_secret`, `*_credentials_key`, `*_encryption_key`, `*_kek`, `*_password`, plus `secure_store_kek`, `memory_db_url`, `redis_url`) from every config read (`GET /api/config` and the WebSocket config push) via `Config.config_for_user()` / `Config.is_secret_config_key()`. This read-redaction is intentionally narrower than the admin-only *write* denylist (`is_secret_config_key` vs `is_global_config_key`): non-secret admin-only keys the UI needs (model/provider names, non-secret network settings) stay readable, only credentials are removed. Admins receive the full config.

## Isolation Summary Table

| Component | Isolation mechanism | Level |
|-----------|---------------------|-------|
| Memory CRUD | `user_scope_id` filter on every query | Application |
| Memory graph | Scope filter on auto-connect and manual operations | Application |
| Gateway | Server-side scope extraction, client scope stripped | Transport |
| Config read (`GET /api/config`) | Secret keys (API keys, OAuth client secrets, JWT/encryption keys, DB URLs) redacted for non-admins; admins get full config | Application |
| Redis cache | Scope-prefixed cache keys | Caching |
| PostgreSQL | Fail-closed Row-Level Security (ENABLED + FORCED), enforced via non-superuser `vaf_app` role | Database |
| Filesystem | Scope-based paths (`~/.vaf/scopes/<user_scope_id>/`) preferred; legacy `~/.vaf/users/<username>/` as fallback | OS |
| Generated projects (VAF_Projects) | `~/Documents/VAF_Projects/<uid[:8]>/<session_id>/` when session context is present; legacy flat root otherwise | OS |
| Session workspace | `Session.project_path` anchored to first `VAF_Projects` creation; `[SESSION WORKSPACE]` injected per turn | Application |
| Central Data Explorer (`/api/workspaces`) | Per-user root derived from authenticated scope; lists/renames/deletes only the caller's own workspaces (incl. orphans); opaque handles, not paths | Application |
| Librarian agent (filesystem read) | Per-user jail (contextvar over `is_safe_path`): remote user confined to own `VAF_Projects/<uid[:8]>/`, local admin full; another user's tree always denied, fail-closed | OS |
| Sandbox | Per-user working directory in Docker | Container |
| Browser sessions (cookies/logins) | Per-user `~/.vaf/browser_sessions/<scope>/` store keyed by user_scope_id | OS |
| WhatsApp | Separate subprocess per user | Process |
| Telegram | Whitelist-based routing | Application |
| Email | Per-user encrypted credentials + scope-based config lookup chain | Application |
| Calendar (Google/Microsoft) | Same OAuth and `user_scope_id` as Email; no separate credentials | Application |
| Automations | Per-user task storage and scoped RAG access; max 3 users per time slot (global cap) | Application |
| Automation planner (notes/todos) | Per-user `automation_planner/<scope>/notes.json`, `todos.json` | Application |
| Thinking workspace | Per-user `workspaces/<scope_key>/` with boundary-checked file access and handoff approvals | Application |
| Config (global vs user) | Backend/network/API keys: admin-only write; non-admins can change only user-scoped settings | Application |

## Developer Guidelines: Building New Features

When adding new functionality to VAF, follow these rules to maintain user isolation.

### Rule 1: Always accept and propagate `user_scope_id`

Every function that touches user data must accept `user_scope_id` as a parameter:

```python
# Correct
async def my_new_feature(data: dict, user_scope_id: Optional[UUID] = None):
    results = await db.execute(
        select(MyModel).where(MyModel.user_scope_id == user_scope_id)
    )

# Wrong — no scope filtering
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
# Correct
cache_key = f"my_feature:{user_scope_id}:{item_id}"

# Wrong — shared across users
cache_key = f"my_feature:{item_id}"
```

### Rule 5: Scope database queries in new tables

When creating new tables that hold user data:

1. Add a `user_scope_id` column (UUID, nullable for system/shared data).
2. Add a fail-closed RLS policy mirroring the current `memories` table pattern, and `FORCE` RLS so the owner does not bypass it.
3. Grant the non-superuser application role (`vaf_app`) `SELECT, INSERT, UPDATE, DELETE` on the new table — the application data connection runs as `vaf_app`, not the table owner.
4. In `get_db(user_scope_id=...)`, the per-transaction GUC `app.current_user_scope_id` is already set globally, so the new table's policy is enforced automatically.

```sql
-- Example for a new table (fail-closed, mirrors the memories pattern)
ALTER TABLE my_new_table ENABLE ROW LEVEL SECURITY;
ALTER TABLE my_new_table FORCE  ROW LEVEL SECURITY;

CREATE POLICY user_isolation_my_new_table ON my_new_table
    USING      (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid)
    WITH CHECK (user_scope_id = NULLIF(current_setting('app.current_user_scope_id', true), '')::uuid);

-- Grant DML on the new table to the app role so vaf_app can use it:
--   GRANT SELECT, INSERT, UPDATE, DELETE ON my_new_table TO vaf_app;
```

### Rule 6: Scope filesystem access

If your feature writes files, place them under the user's directory:

```python
# Correct
path = Path.home() / ".vaf" / "users" / username / "my_feature" / filename

# Wrong — shared location
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

- [USER_IDENTITY.md](../memory/USER_IDENTITY.md) — User profile and preferences system
- [MEMORY_SYSTEM.md](../memory/MEMORY_SYSTEM.md) — Memory storage and RAG pipeline
- [GATEWAY.md](../setup/GATEWAY.md) — WebSocket gateway architecture
- [CONNECTIONS.md](../integrations/CONNECTIONS.md) — External service connections (WhatsApp, Telegram, etc.)
- [SANDBOXING.md](SANDBOXING.md) — Docker sandbox for code execution
