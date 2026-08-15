# User Isolation in VAF (Multi-Tenant Security)

This document explains how VAF isolates users from each other when running as a cloud service with multiple authenticated users. It covers every layer of the stack, documents the security mechanisms in place, and provides guidelines for developers building new features.

## Overview

VAF uses a **`user_scope_id`** (UUID) as the universal isolation key. Every user who authenticates receives a unique `user_scope_id` from the auth database. This ID flows through the entire stack - from the WebSocket handshake down to the database row - ensuring that one user can never access another user's data.

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
│  │  RLS forced; app data role cutover to vaf_app pending    │      │
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

All API route handlers read `request.state.user` to extract the authenticated user's identity and scope. The WebSocket lane resolves the same identity **once, at the handshake**: the presented token is decoded and the scope from its payload is stored on the connection (`web_interface.set_connection_user`). Every later handler asks the connection, never the message:

```python
# vaf/core/web_server.py - inside a WebSocket handler
user_scope_id = manager.get_connection_user(websocket)
```

**Critical rule**: identity is never read from message content. No field of a chat payload is consulted for `user_scope_id`, so a client cannot present another user's scope - there is nothing to strip, because nothing client-sent is ever trusted as identity in the first place. This is the same rule the tool dispatcher applies to model output, where a tool's declared `identity_kwargs` are **assigned over** whatever the model produced rather than defaulted.

The integrated HTTPS proxy relays WebSocket traffic to the backend with `max_size=None` so large `history_update` frames are not truncated; this loopback-proxy path is where the JWT-over-loopback identity handling lives. See [NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md) and [WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md) for the transport detail.

The resolved scope is then bound onto the agent for the turn (`vaf.core.identity_binding.bind_identity`, called by the headless runner) and propagated to all downstream services (memory, tools, automations).

**Token before localhost short-circuit.** `AuthMiddleware` extracts and honors a presented access JWT **before** any localhost short-circuit, so a valid token always establishes the real identity regardless of peer IP. A tokenless localhost request leaves `request.state.user` unset (internal IPC / single-user desktop). This is what lets a LAN user proxied over loopback by the integrated HTTPS proxy get **their** scope instead of the local admin's: because the token is read before the localhost branch, a remote user arriving over the loopback proxy is identified by their own token rather than inheriting the local-admin identity.

**"Localhost" means REALLY local, not just a loopback peer.** The integrated HTTPS proxy terminates TLS on `0.0.0.0` and relays every LAN device to the backend over loopback, so the raw socket peer is `127.0.0.1` for remote users too. Trusting the peer alone therefore treated the whole LAN as local: a tokenless LAN request reached every non-exempt route and the route floors below promoted it to the local admin. The middleware now resolves the client through `vaf.network.binding.effective_client_ip(peer, x_forwarded_for)`, which honors `X-Forwarded-For` **only** when the immediate peer is loopback (i.e. our own proxy relayed it). The polarity is fail-safe: a forwarding hop **removes** trust and never grants it, so a client can make itself look more remote but never more local. The proxy strips any client-supplied `X-Forwarded-For` before setting its own, so the value the backend reads is always the proxy's. Requests with no hop at all - internal loopback IPC (`/api/subagent/stream`, `/api/workflow/update`, `/api/heartbeat`) and the desktop via the Next.js `/api` route - are unchanged and still pass without a token. The same resolver backs the WebSocket handshake (`web_server._ws_client_ip`) and the OAuth callback loopback exception (`oauth_session_binding._real_client_ip`), so the three paths cannot disagree about who the client is.

### Local mode fallback

The route-level local-admin floors (`config_routes.get_current_user_or_local_admin`, `user_routes._current_user`, `supervisor_routes._caller` and the scope-only floors in the connection/persona/contact routes) are **unconditional**: with no `request.state.user` they return the local admin identity regardless of `local_network_enabled`. That is deliberate - in single-user desktop mode the auth middleware is not installed at all, so the floor is the only identity source for the entire app. What keeps those floors out of LAN reach is the middleware: it is installed whenever `local_network_enabled` is true, and after the fix above it challenges any request that is not really local. Only `vaf/memory/routes.py` denies instead of flooring when a scope is missing.

When running locally without authentication (CLI or Web UI without JWT), VAF uses the scope and username from config:

- **`local_admin_scope_id`**: Default `00000000-0000-0000-0000-000000000001` (legacy placeholder). After the first admin is created - in the browser via `POST /api/auth/bootstrap`, or in the terminal via `vaf setup` - the shared layer `vaf/auth/user_admin.py` writes that admin's UUID here so CLI and localhost use the same identity as the logged-in admin.
- **`local_admin_username`**: Default `admin`; set to the first admin's username when that account is created (either route). It is a real username, not the literal `admin` - the agent workspace under `~/.vaf/users/<username>/` is keyed on it.

Use `get_local_admin_scope_id()` and `get_local_admin_username()` from `vaf.core.config` instead of reading config directly. This keeps data scoped consistently and avoids a split between "logged-in" and "local" identities.

**Who counts as an admin: `is_admin_identity(role, user_scope_id)`.** Admin is two halves and both are load-bearing. The **role** half covers every admin account: VAF supports more than one admin (user management refuses only to delete the *last* one) and each carries their own scope UUID. The **scope** half covers the machine owner when there is no role claim at all - the tokenless desktop, the CLI and automations resolve to `local_admin_scope_id`. Only one scope can ever be the local admin, so a scope-only check silently demotes every additional admin, and a role-only check locks the owner out. The role is read exclusively from a signature-verified JWT claim (issued from `LocalUser.role` at login) and, for tools, is **assigned** by the dispatcher over whatever the model put in the arguments - never honored as model input. Use this helper rather than rebuilding the comparison: the file gates below were the three places that had rebuilt it scope-only and drifted (guarded by `tests/test_admin_identity_is_role_aware.py`).

**Where the binding happens.** For channel and WebSocket clients the headless runner applies this fallback (`bind_identity(agent, identity)` in `vaf/core/headless_runner.py`, from the identity the queued task carries). The interactive CLI (`vaf run` and `vaf prompt`) does **not** go through the queue - it calls `Agent.chat_step()` directly - so it binds the local-admin scope and username explicitly at agent creation, via `_make_cli_agent()` in `vaf/cli/cmd/run.py`. Without this the CLI would run under scope `None` (the `"default"` bucket) and diverge from the WebUI admin: a stale `last_interaction` and memory/RAG that cannot see the admin's data. The binding is re-applied on every agent (re)creation, because `Agent.__init__` does not set a scope.

### Hybrid Scoping Strategy (Local Mode Stability)

To bridge the gap between strict multi-tenant isolation and a low-friction local experience, VAF uses a **Hybrid Scoping Strategy**. This is especially important for long-lived connections like Email and WhatsApp.

**The Problem:** In local mode, a user might set up Email under one UUID, then clear their browser cache, getting a new UUID. Without fallbacks, the Agent would think no accounts are connected.

**The Solution:**
- **Read Operations (Lookup):** Tools follow a lookup chain (Scope → Legacy → Single-other-scope). This makes the system "self-healing" against UUID changes in local mode.
- **Write Operations (Update/Auth):** Refreshed tokens are written back under the **requested scope** (the scope the caller passed in, with the local-admin scope normalized to the legacy bucket); see `get_valid_access_token()` in `vaf/core/oauth_pkce.py`. The lookup chain makes subsequent reads find the fresh entry; a credential found under a legacy fallback key is not migrated or deleted, so a stale legacy entry can linger until the account is removed.

**Best Practices for Developers:**
1.  **Trust the Fallbacks:** Use helpers like `get_valid_access_token()` or `_get_email_config()` which already implement the fallback logic. Do not implement manual string comparisons with `"admin"`.
2.  **Propagate the ID:** Always pass the `user_scope_id` down to internal transport functions so they can choose the correct credential bucket.

## 2. Memory System Isolation

The memory system is the most data-sensitive component. Every memory operation is scoped.

### Fail-closed scope resolution

Memory reads fail **closed** when no scope is available. `RagPipeline.search()` returns `[]` for an empty scope in **both** the vector and the lexical/hybrid lanes - a missing scope means **no results**, never "search all". `run_memory_search_sync` resolves a concrete scope up front and **denies** (returns nothing) when no scope is present in server/multi-user mode, flooring to the local-admin scope only in genuine single-user/local mode. An unparseable scope is treated as a deny as well, so a missing or malformed scope yields no results rather than searching across all users.

**Reading ACROSS a user's own chats has a stricter ownership rule than listing them.** `SessionManager.list()` filters leniently on purpose: a session with no `user_scope_id` is shown to every scope, because a pre-scoping session can only belong to the machine owner and its name in a sidebar is harmless. Reading its **content** is a different question, so `SessionManager.iter_owned_sessions()` / `list_owned()` answer the strict one: the caller's scope and the session's scope must both be non-empty and equal. Three consequences that are easy to get wrong, and are pinned by tests:

- **An unowned session belongs to nobody**, not to everybody.
- **Being an admin does not widen it.** The websocket ownership gate answers "may this identity act on that session" (for an admin: on all of them); "is that session this scope's" is a different question and must not be answered with the gate's verdict.
- **Scopes are compared as strings, never parsed.** The configured local-admin scope is bound unparsed on purpose and real stores carry non-UUID spellings, so a UUID gate would silently return nothing on those installations.

`SessionManager.search()` runs on the same walker and takes a mandatory scope; it previously globbed the whole store with no scope at all. Cross Chat Hint (`vaf/core/cross_chat.py`) is the first feature that lifts message text out of one chat into another chat's prompt, so it also refuses conversations with **other people**: a channel session whose endpoint matches a known contact is skipped, and the engine does not ask for hints at all on a front-office turn, a background or automation run, or a voice call. Its push to the browser (`cross_chat_hints`) goes through `push_update_to_user` and is dropped when the scope is unknown, exactly like the snippet push below.

**Retrieval scope alone is not enough - the UI push must be scoped too.** Retrieved snippets are shown in the web UI's "RAG-Snippets" panel via a WebSocket event. That push was previously a *global* broadcast to every connected client, so a correctly-scoped search under user B's scope (including a background thinking or automation run) surfaced B's snippets in user A's open panel even though B's data at rest stayed correctly isolated. The push is now routed to the owning user's connections only (`push_update_to_user(user_scope_id, ...)`) and dropped when the scope is unknown (fail-closed); the same applies to the `real_context_payload` X-ray, which carries the full prompt including the memory-context block. Real-time events that carry user content must be scoped at the emit site, not only at retrieval.

### CRUD Operations (`vaf/memory/rag.py`)

All memory access methods accept and enforce `user_scope_id`:

| Method | Scope enforcement |
|--------|-------------------|
| `get_memory(id)` | Filters by `Memory.user_scope_id == user_scope_id` |
| `update_memory(id)` | Filters by scope before allowing update |
| `delete_memory(id)` | Filters by scope before soft-delete |
| `search_memories()` | Filters query results by scope; an empty scope returns `[]` (fail-closed) in both vector and lexical/hybrid lanes - never "search all" |
| `store_memory()` | Stamps `user_scope_id` on new records |
| `get_all_memories()` | Filters listing by scope |

If a user tries to access a memory ID that belongs to another user, the query returns `None` (not found) - the same response as if the memory doesn't exist. This prevents information leakage through error messages.

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

- **Session list:** `SessionManager.list(limit, user_scope_id=...)` is called with the connection's user scope (from `manager.get_connection_user(websocket)`). Users only see sessions that have matching `metadata.user_scope_id` or no scope (legacy/local admin). Note: session-list visibility and command authorization apply different rules to legacy (no-scope) sessions. The list still shows a no-scope session to every user, but the ownership gate treats a no-scope session as admin-only when acting on it (subscribe/chat/delete/rename/hide/edit). Scope-less sessions CAN occur on disk: an automation delivering to a messenger contact before that user ever wrote inbound used to create the channel session without a scope (an audit finding; the two outbound-first creators, `_record_outbound` in `messaging_connections.py` and `send_discord.py`, now stamp the owner scope at creation, matching the inbound lane). Sessions created before that fix stay admin-only under this gate.
- **Session-command ownership:** A single shared ownership gate runs before the first side effect of every Web UI session command - chat (before subscribing to the session stream), load, delete, rename, hide, and artifact edit. The session's `metadata.user_scope_id` must match the current user, or the connection must be admin (connection role `admin` or the local-admin scope). A session with no recorded `user_scope_id` is treated as admin-only for these commands. On denial the server logs and replies with `{"type":"error","message":"Access denied"}` and keeps the connection open.
- **Owner re-stamp (defense-in-depth):** When a queued chat is processed, the runner stamps `user_scope_id` onto the session only if the session has none yet; it never relabels an already-owned session, so a queued chat cannot take over another user's session behind the gate.
- **Default session:** When no session is selected, the fallback session ID is per-user (`web-default-<scope>`), not a shared global ID.

**Which session is a run for.** Three answers, and they are deliberately different:

- **A named session.** A chat turn or a channel turn declares it (`set_current_session_id`) before
  it dispatches anything, and everything it touches resolves to that one.
- **Nobody.** A scheduled automation has no browser tab behind it and says so, by declaring `None`.
  That is a real answer, not a missing one: it means the run addresses no session, so it has no live
  view and no Stop button - correct for something nobody is watching. Before this was explicit, such
  a run inherited whatever a live chat turn had left in the process and wrote its deliverable into
  that tenant's workspace, notified their browser and persisted the path into their session record.
- **Not yet told.** Only a spawned child is in this state, and only until it bootstraps: it reads
  `VAF_SESSION_ID` from the environment its parent built for it and declares it into its own
  context. In the parent, "not told" resolves to nothing.

**The per-user file jail is not a backstop for a misplaced write.** It is consulted only from inside
`is_safe_path`, and `document_writer`, `document_agent`, `research_agent` and `python_sandbox` call
that zero times - they write with a raw `open()` or a container copy into a directory they resolved
themselves. `filesystem.py` calls it fourteen times, which is why `write_file` IS covered. So a
wrong session id upstream is not caught downstream: the directory decision is the boundary.
- **Broadcasting:** Updates are sent only to connections subscribed to that session (`broadcast_to_session`); session list refreshes are sent only to that user's connections (`broadcast_to_user`). See [SESSION_MANAGEMENT.md](../memory/SESSION_MANAGEMENT.md).
- **Agent context store:** Each chat's working memory - intent, plan, tasks, notes, and team state - is stored per session under `.vaf/main/sessions/<session_id>/`, so it is isolated between chats (and therefore between users). See [SESSION_MANAGEMENT.md](../memory/SESSION_MANAGEMENT.md) and [CONTEXT_GLUE.md](../memory/CONTEXT_GLUE.md).

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

PostgreSQL Row-Level Security (RLS) is enabled and forced on the `memories` table.

**Status (important):** on a default install the application data connection (`memory_db_url`) still runs as the table **owner role `vaf` (superuser), which bypasses RLS** (`config.py`; `database.py` notes both DSNs are the owner role until the cutover). So today the application-layer scope filter (Section 2, also fail-closed) is the active enforcement; the RLS policy below is created and fail-closed by design but only becomes the enforced **second** line after the **cutover**: set `memory_db_url` to a non-superuser role (`vaf_app`, `NOSUPERUSER`, `NOBYPASSRLS`) and `memory_db_owner_url` to the owner DSN. The owner connection is used for DDL, migrations, global maintenance, and one admin-gated aggregate lane: `get_admin_isolation_metrics()` (`vaf/memory/database.py`) runs on it to serve `GET /api/security/overview` with cross-scope metadata only (per-scope memory/chunk counts and sizes, never memory content); that lane bypasses RLS by nature and must never be exposed on a per-user route. The policy is fail-closed:

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
3. A row is visible or writable only when its `user_scope_id` equals the per-transaction GUC. With a concrete scope set, other users' rows are invisible even if application-level filtering has a bug. With no scope set the GUC is empty, so the policy matches no rows and an unscoped transaction sees and writes nothing (fail-closed); a row whose `user_scope_id` is NULL is not blanket-visible. After the cutover (see the status note above), when the data connection runs as the non-superuser `vaf_app` role, the database enforces this independently of the application filter; until then the owner role bypasses the policy and the fail-closed application filter is the enforcement.

### Policy logic

| `app.current_user_scope_id` | Row `user_scope_id` | Visible / writable? |
|------------------------------|---------------------|----------|
| Not set / empty              | Any                 | **No** (unscoped session is denied all rows) |
| Set to UUID                  | NULL                | **No** (a NULL-scope row is not blanket-visible) |
| Set to UUID                  | Same UUID           | Yes |
| Set to UUID                  | Different UUID      | **No** |

**Important**: The GUC is set with `set_config(..., true)` (transaction-scoped) on every memory data path - `get_db(user_scope_id=...)` threads the scope through all callers - so it is scoped to the current transaction and never leaks between concurrent requests sharing the connection pool. After the cutover to the non-superuser `vaf_app` role, an unscoped transaction is denied at the database, not merely filtered in the application; before it, the fail-closed application filter is what denies the unscoped transaction.

**Note**: the RLS policy is fail-closed by design - a row is visible or writable only when its `user_scope_id` exactly equals the per-transaction GUC; an unset or empty GUC matches nothing (an unscoped transaction sees and writes zero rows), and a row with `user_scope_id IS NULL` is not blanket-visible; RLS is `ENABLE`d and `FORCE`d on `memories`. **But on a default install the application data connection is still the owner role `vaf` (superuser), which bypasses RLS**, so today the fail-closed application-layer scope filter (Section 2) is the active line of defense and the RLS policy is a genuine second line only after the cutover switches `memory_db_url` to the non-superuser `vaf_app` role. Until then, do not rely on RLS as an independent backstop.

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

Because this tree is keyed by the **username** string, a background run must bind the user's **real** account username. Thinking Mode and scheduled Automations resolve the username from `local_users` by `user_scope_id` (and fall back to a synthetic `scope_<8hex>` on an unknown scope) - **never** the literal `"admin"`. Handing a non-admin run the username `"admin"` would make `get_user_workspace("admin")` read `~/.vaf/users/admin/user_identity.json` and inject the admin's personal identity/profile (name, preferences, dos/don'ts, timezone) into that user's system prompt and RAG query seed - exposing the admin's data to that user, even though the memory database itself stays correctly scope-isolated.

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
│   └── <session_id>/            # per-chat subdirectory (e.g. green123456)
│       ├── My Website/          # project created in this chat
│       └── Game Space Shooter/
└── Demo Website/                # legacy path (local/admin, no session context)
```

- **Authenticated users** (`user_scope_id` present in session metadata): projects are placed under `VAF_Projects/<first-8-chars-of-uuid>/<session_id>/`.
- **Per-chat isolation:** with a session id, each chat gets its own folder, so projects from different chats never mix. The workflow engine builds its project paths the same way.
- **All file-creating sub-agents use the chat folder:** `get_session_workspace_dir` / `resolve_agent_output_dir` (`vaf/core/session.py`) is the shared resolver - the document writer and document agent (previously `VAF_Documents/`), the research agent (previously `VAF_Research/`) and the WebUI workspace browser all resolve through it. Without session context the agents fall back to their legacy directories.
- **Local/admin mode** (no `user_scope_id`): projects go into `VAF_Projects/` (with the `<session_id>/` level when a session id is available).

The prefix is derived from `session.metadata["user_scope_id"]` at project creation time. Existing projects are never moved; only newly created directories use the prefix.

**Unsafe-directory guard:** `is_unsafe_project_dir()` (`vaf/tools/coder.py`) rejects the user's home directory itself, the standard user directories (Documents, Desktop, ...), `~/.vaf` and the VAF program tree as agent work directories - for the CWD heuristic, explicit `project_path` arguments, paths extracted from task text and `git init`. Unsafe paths fall back to the `VAF_Projects` flow. When a passed or extracted path names a FILE (existing file, or a nonexistent path with a known file extension), the coder first splits it into directory + target-file hint (`_split_explicit_path`) and the guard judges the DIRECTORY part - so "create `~/report.html`" falls back to `VAF_Projects` (home itself is unsafe) while keeping `report.html` as the deliverable name. `git init` additionally refuses any path that is not an existing directory.

**Workspace window endpoints:** `GET /api/session/workspace`, `POST /api/session/workspace/upload` and the workspace file-delete endpoint (`vaf/core/web_server.py`, all via `_resolve_session_workspace`) enforce session ownership with the SAME policy as the WebSocket gate `_ws_session_owner_ok`: the session's `metadata.user_scope_id` must match the requesting user, and a session with NO recorded scope is **admin-only** (legacy/pre-isolation sessions belong to the local admin, they are never open to every authenticated user), otherwise 403. Admin is detected role-aware via `is_admin_identity` (role `admin` OR the local-admin scope). An earlier version treated scopeless sessions as owned by everyone here; that hole is closed, and `/api/image/describe`'s session check follows the same rule. `GET /api/file` additionally refuses downloads from another user's `VAF_Projects/<uid[:8]>/` subtree (any admin exempt; legacy flat projects unaffected); this check is **fail-closed**: if ownership cannot be verified it denies.

**Central Data Explorer endpoints:** `GET /api/workspaces`, `GET /api/workspaces/search`, `POST /api/workspaces/rename`, and `POST /api/workspaces/delete` (`vaf/core/web_server.py`) back the WebUI "all my workspaces" view. They derive the per-user root `VAF_Projects/<uid[:8]>/` solely from the authenticated user's scope (never a client value), so a user can only ever list, rename, or delete their own workspaces, and they return opaque session-id handles rather than absolute paths. The list includes **orphaned** workspaces (folders left behind when a chat is deleted; deleting a chat removes only the session JSON, not the files), detected by diffing the folder set against the live session ids. Rename is **display-label only**: it writes a `.vaf_workspace.json` label inside the folder (the on-disk folder name stays the session id, which the resolver keys on) and survives session deletion, so orphans stay renamable. Delete removes the whole folder, boundary-checked to the caller's own root.

### Session workspace (`vaf/core/session.py`, `vaf/core/web_server.py`)

Each chat session has a **stable workspace root** stored in `Session.project_path`. This field is set once (on the first `file_created` event for that session) and never overwritten, giving the session a permanent home directory regardless of how many sub-projects are created later.

- `session.project_path` is only set for paths inside `VAF_Projects/` (temp dirs and one-off outputs are excluded).
- **The anchor is written by ONE shared setter** (`record_created_file`, `vaf/core/session.py`), called from BOTH notification paths: the `/api/workflow/update` HTTP endpoint (subprocess sub-agents) and `notify_file_created`'s in-process branch (main-agent `write_file`, workflow engine). Historically only the HTTP path anchored, so chats whose files were written in-process never got the `[SESSION WORKSPACE]` note; the headless runner additionally derives the workspace deterministically (`get_session_workspace_dir(task.session_id)`) when the anchor is missing but the folder exists.
- **Session-derived prompt content must key on the session id, never a process-global pointer**: a process-global belongs to whichever lane touched it last, and that needs no non-default configuration - the chat worker, thinking and the automation scheduler are all unconditional threads in one process. There is no such pointer any more (`get_current_session_id()` answers per context; `VAF_SESSION_ID` is the process-boundary channel for spawned children only), but the rule stands for anything that would reintroduce one. `build_prompt(session_id=...)` carries the chat's own id for the "this chat's workspace" line, and `document_writer` receives `_session_id` from the tool dispatcher for its output-dir resolution (a fresh chat's prompt once advertised ANOTHER chat's folder and the model saved the deliverable there).
- `runtime_state["last_project_path"]` continues to track the most recently created or edited project within the session. Unsafe directories (home dir, `~/.vaf`, ...) are never recorded - and never re-injected into prompts - so sessions that stored such a path before the guard existed self-heal (`is_unsafe_project_dir` checks in `web_server.py` and `headless_runner.py`).
- The agent receives both values as `[SESSION WORKSPACE]` and `[ACTIVE PROJECT]` context lines at the start of each turn (injected by `vaf/core/headless_runner.py`).

### Librarian agent (`vaf/tools/librarian.py`, `vaf/tools/filesystem.py`)

The `librarian_agent` reads the local filesystem to answer "find / list / summarize my files" tasks. By default `is_safe_path` (`vaf/tools/filesystem.py`) only blocks the VAF program tree and a few system directories - it is not user-aware - so without an extra guard the librarian - and the main agent's own read tools - could read across every user's `VAF_Projects/<uid[:8]>/` tree and the whole home directory. `is_safe_path` also **resolves symlinks and re-asks itself about the real target**, for every caller, jailed or not: the static checks alone ran against the unresolved path, so a link inside an allowed folder used to smuggle in a protected target (the downstream `open()` follows the link). Guarded by `tests/test_is_safe_path_symlink_recheck.py`. A **per-user jail** is additionally layered on top of `is_safe_path`:

- The agent's tool dispatcher injects the caller's `user_scope_id` **and `user_role`** into the `librarian_agent` call (`vaf/core/agent.py`); `LibrarianTool.run` installs it as a **contextvar** (`set_librarian_scope`) for the duration of the run only, so a caller that installs no jail of its own (the coder) is unaffected. `document_viewer` and `learn_document` now enter the same jail themselves via `user_jail(..., mode="read")` before deciding on a path. When the librarian runs in a separate sub-agent terminal, both values travel to the child as `VAF_USER_SCOPE_ID` / `VAF_USER_ROLE` so the two lanes reach the same verdict.
- While the jail is active, `is_safe_path` additionally enforces: a **non-admin user** may read only inside their own `VAF_Projects/<uid[:8]>/`; an **admin** (see `is_admin_identity` above - the DB role or the local-admin scope) keeps full access (their personal `Downloads`/`Documents`/… included). Any path under another user's `VAF_Projects/<other-uid[:8]>/` is **always denied**.
- The check is **fail-closed**: if the scope cannot be resolved, access is denied. The contextvar is reset in a `finally`, so the jail never leaks into a later run.
- **The main agent's `write_file` reuses the same jail:** `execute_tool` injects the caller's `user_scope_id` and `user_role` (plus the chat workspace for relative paths and the session id for the Web-UI emits) into main-agent `write_file` calls; `WriteFileTool.run` installs the jail contextvar itself, inside the tool. The reason is not that a dispatcher-set contextvar would fail to arrive - `vaf/core/bounded_run.py` copies the caller's context into the worker thread precisely so that it does - but that a dispatcher is not always in the picture: the coder, the librarian and automations call the tool directly. A non-admin user can therefore only write inside their own `VAF_Projects/<uid[:8]>/`; an **admin keeps home-wide access** - decided by the shared `compute_user_jail`, so the librarian, `write_file` and `send_mail` attachments cannot disagree about who an admin is. Both injected keys are **assigned, never defaulted**: `tool_args` starts out as the arguments the model produced, so a prompt-injected `user_role: "admin"` is overwritten with the session's real role before the tool runs. Direct `WriteFileTool()` consumers (coder, librarian, automations) pass none of these kwargs and are unaffected. The **workflow engine is no longer one of them**: it assigns the same declared identity keys from the workflow's owner, controlled by `workflow_identity_injection` (`legacy` / `declared` / `off`). Under `legacy` it uses the historical hardcoded name list, which never carried `user_role` at all; under `declared` (the default) it reads each tool's `identity_kwargs`, so a workflow step is jailed exactly like the chat turn that a step's tool would otherwise have been called from. **Known limit - the role does not reach every lane.** `is_admin_identity` says yes for an admin ROLE or for the local admin's SCOPE, so the role only decides for a *second* administrator: someone with the role but not that scope. Seven places construct a `WorkflowEngine` with an identity. **Two pass the role**, because they hold a live agent: the saved-template lane behind `execute_workflow` (`vaf/tools/workflow_executor.py`) and the router / `@workflow` lane (`vaf/core/agent.py`). **Five do not**, and each for the same reason - they resolve identity from a store that has no role in it: the workflow CLI subprocess and the headless resume path read session metadata and the paused record, the CLI resume path asks a `PausedWorkflow` that has no such field (so its `getattr` is always `None`), and the `create_agent_workflow` and automation lanes pass scope and username only. In those five, a second administrator is jailed to their own tree inside a workflow. The same applies under `workflow_identity_injection="legacy"`: that byte-frozen rollback lane injects scope and username but no role into the messenger senders, so a second administrator attaching a file there is confined to their own tree until the lane is retired. The direction is restrictive, never permissive - nobody is freed by a missing role - which is why it is recorded here rather than fixed by widening what those stores persist.
- **`edit_file` is jailed the same way, around its WHOLE body.** It is the other half of the main agent's file-writing surface (surgical search/replace on an existing file) and delegates the actual write to a nested `WriteFileTool()` call that carries no scope of its own - so for a while the jailed and the unjailed path sat next to each other in the same tool list, and a tenant confined to their own tree could rewrite anyone's file. Editing is the sharper end of that: the denied `write_file` could only have created a new file in a foreign tree, while `edit_file` silently changes content that is already there. It is also a **read** primitive - when a search block misses, the tool answers with a "nearest region" slice of the file to help the model retarget - so the contextvar is installed in `EditFileTool.run` before the target is opened, not merely around the delegate. Guarded by `tests/test_edit_file_jail.py`.
- **The read tools are jailed too, and their allowed roots are WIDER.** `read_file`,
  `list_files`, `tree`, `find_files` and `folder_size` were the unjailed half of the file
  surface: a tenant who could not write outside their own tree could still read any path the
  static checks allow, another tenant's included. Reading is also where listing leaks - a
  folder's file names are a leak without a single file being opened. `compute_user_jail` now
  takes a `mode`: `write` (the default, and what every earlier caller keeps) stays at the
  caller's own `VAF_Projects/<uid[:8]>/`, while `read` adds **the folders of the skills
  visible to that user**. That addition is required, not generous: `use_skill` prints a
  skill's bundled files as absolute paths and tells the model to open them with `read_file`.
  It is also carefully bounded - every skill folder lives in one directory, so allowing
  "skills" wholesale would hand over a skill kept private to somebody else;
  `get_visible_skill_ids_for_user` (the `shared_with` manifest) decides which folders enter
  the jail, and the lookup fails closed to none of them. Those roots stay out of `write`
  mode: seeing a shared skill is not authority to rewrite it (that is `can_user_edit_skill`).
  Uploaded attachments need no special case - they land inside the caller's own root
  (`get_session_attachments_dir`) - and cloud-synced files need none either, because
  `cloud_storage` is a separate tool that never hands out an absolute `cloud_sync` path.

### The coder acts as its caller

The coding sub-agent runs in its own process, and until 2026-08-01 nothing identity-shaped
crossed that boundary: the child executed every inner tool with no scope and no role, which
the shared jail rule answers as "the machine owner" - for every caller. Seven of the
coder's eight inner tools already declared `identity_kwargs` and `file_access`; the
machinery was attached and permanently resolving as the owner.

The caller's scope and role now travel as DATA in the spawn environment
(`VAF_USER_SCOPE_ID` / `VAF_USER_ROLE`, the librarian's pattern) and are ASSIGNED to every
inner tool that declares them - never `setdefault`, so a prompt-injected admin role is
overwritten. For the machine owner nothing changes; a tenant's coder run is confined to
their own tree for the first time.

**`bash` is a named exception, in both directions.** It declares no identity and no
`file_access`, deliberately: the coder must be able to build, test and install at full
strength, and the containment for a user who should not have that power is the per-user
tool permission - which IS enforced now (see "The per-user tool permission is enforced"
below). An admin can withhold the coder entirely, or allow the coder and withhold `bash`
by name. What remains true by design: a tenant who is allowed `bash` has an unjailed
shell, so granting it is the decision.

### The per-user tool permission is enforced

The admin's per-user tool selection (`LocalUser.permissions["tools"]`, an ALLOWLIST built
by the user manager's presets) was stored, displayed and read by nothing - the JWT never
carried it, and the only readers were the admin routes mirroring it back. It is now
enforced in the dispatch funnel for every lane the funnel serves - after the hard policy
block, BEFORE the embedder's authorizer, so an `allow()` cannot override an account-level
ban. The funnel gets the list through the framework's account-allowlist resolver
(`set_account_allowlist_resolver`, a facade primitive): the harness registers its auth-DB
resolver (`vaf/auth/permissions.py`, resolved per TURN with a short TTL, invalidated by
the admin update route so a revocation beats the cache) at `vaf/main.py` import, which
every product process passes through - web/tray, CLI and every subagent child (wiring
pinned by `tests/test_account_allowlist_wiring.py`). An embedded library process that
registers nothing runs unrestricted, by contract. Inside the coder the same allowlist
crosses the process boundary as data (`VAF_ALLOWED_TOOLS`, names only, never a secret):
blocked tools are removed from the schema the model sees, with a dispatch-side refusal
as backstop for hallucinated names. Coder-internal tools (`bash`, the git tools) are
offered to the picker via `GET /api/users/tool-universe`, sourced from the coder
module's own declaration so the picker cannot drift from what the child runs.

The same record can carry `permissions["confirmation_bypass"]` (default absent =
off): the admin-granted hands-off switch that lets the agent run
confirmation-gated tools for THIS user without asking. It is resolved through the
same registered-resolver lane, fails CLOSED (an unreachable DB grants nothing),
can only skip the human question - `admin_only`, the account allowlist and an
authorizer's `ask()` are decided earlier and are never widened - and every use
is announced as a `gate_bypassed` event.

Pinned semantics: no row, no `"tools"` key or an EMPTY stored list mean UNRESTRICTED -
`[]` is the API model's creation default, and "block every tool" is deliberately not
expressible here (deactivate the account instead). Admins are never restricted. A
REGISTERED resolver that raises refuses the call (fail-closed; a broken guard must not
quietly become no guard) - the harness resolver itself never raises: it catches its own
DB errors and resolves unreachable as unrestricted, and on the desktop that is correct
rather than merely safe, because no reachable auth DB means no tenant can authenticate
either. A hypothetical deployment that served `vaf.core.web_server:app` without importing
`vaf.main` would run unregistered; no product path does that, and the wiring test is the
fence.

The WORKFLOW half is enforced the same way, in two pieces. Non-spawn workflow steps run
through the same dispatch funnel (one `ToolCaller` per engine run: hard policy, the tool
allowlist above, the embedder authorizer; the confirmation gate stays off for that lane,
and spawn-mode sub-agent steps stay off the funnel - their inner tools remain constrained
by `VAF_ALLOWED_TOOLS` in the child). And saved workflow TEMPLATES pass a START gate:
`WorkflowEngine.execute()` checks `permissions["workflows"]` once, at the point all seven
entry lanes converge, resume included - so a revocation between pause and resume bites.
Same pinned semantics as the tool list (absent or empty stored list = unrestricted,
admins never restricted, a raising registered resolver refuses, the harness resolver
itself never raises); the resolver is registered in `vaf/main.py` next to the tool one.
Ad-hoc runs without a template id (run_temp, automation inline steps) are governed by the
TOOL permission of the lane that builds them. The rollback switch
(`workflow_identity_injection` = legacy/off) restores the entire pre-funnel step lane -
identity name list and absence of per-step policy alike; the start gate is not behind the
switch.

### Cloud credentials are addressed by scope

Mail and GitHub key their stored credentials on the caller's `user_scope_id`; the cloud
lane keyed on a NAME until 2026-08-01. A name is resolved per lane, so any lane supplying
none collapsed onto `cloud:<provider>:<account>` - the machine owner's key - and a tenant
reached the owner's connected Google Drive, OneDrive, Dropbox, Nextcloud and iCloud
accounts through it.

The scope now travels the whole chain: `CloudProvider` and its five subclasses carry it,
`get_valid_access_token` takes it and writes the refreshed token back WITH it (a refresh
that dropped the scope would quietly return a tenant's credential to the ownerless key),
and the four functions in `credential_cloud` address the key with it. The provider classes
learned it BEFORE the key format changed, because the hole is a read: changing the write
key first would have hidden the credentials from the user who had just connected an
account while a tenant carried on reading the owner's.

Three properties are worth knowing rather than rediscovering:

- **A scoped caller never falls back to the ownerless form.** That form is the hole.
  Exactly one legacy probe is permitted - the caller's own non-empty name - and a hit is
  re-keyed onto the scoped form and the old entry deleted, so the branch drains instead of
  becoming a second permanent lookup. An empty name yields no probe at all, because the
  name key with an empty name collapses onto the ownerless form.
- **The owner is unaffected.** Their scope collapses to the no-identity form in the shared
  key builder, so their existing entries answer unchanged.
- **The background sync worker has no scope of its own**, and that boundary is named
  rather than papered over: it runs without a request or a session, and no name-to-scope
  resolution exists in the repository. The route that CONNECTS an account records the
  scope it already holds in the account entry, and the worker reads it back. An account
  connected before this change carries none; that account stays a name-keyed lookup, which
  is what it was, and the legacy probe finds it.

Cloud DOWNLOADS follow the same rule. Both download actions wrote to
`Platform.downloads_dir()` - process global, so every tenant's download landed in the
owner's home, in one of the four roots `GET /api/file` serves. A tenant now receives a
`Downloads` folder inside their own project root; the owner keeps `~/Downloads`.
  All jailed tools enter the jail through the shared `user_jail(...)` context manager, so the
  reset-in-`finally` cannot be forgotten in one of them. Which tools receive the identity at
  all is no longer a hardcoded list of names in the dispatcher: each tool DECLARES it via
  `BaseTool.identity_kwargs`, so a tool registered by an embedder is treated exactly like a
  built-in one. Both `BaseTool` and `user_jail` are on the public surface (`from vaf import
  BaseTool, user_jail`); see [EMBEDDING.md](../EMBEDDING.md). Guarded by `tests/test_read_jail.py`.
- **Outgoing attachments enter the same jail, on every channel.** All five senders that resolve a local path - `send_mail`, `send_telegram`, `send_discord`, `send_whatsapp` and `send_to_user` - declare `file_access = "write"`, so a non-admin can only attach files from their own tree; before 2026-08-02 only `send_mail` was confined, and the four messengers would attach any path the static checks allow. The mode is `write` although attaching READS: the mode names the root set, and `read` would make skill files shared by other users sendable out. The two `VAF_Projects/<uid[:8]>` ownership gates on `GET /api/file` and `POST /api/image/describe` answer the same question through `is_admin_identity`. Guarded by `tests/test_messenger_attachment_jail.py` and `tests/test_mail_config_and_jail_guards.py`.

### Automations (`vaf/core/automation.py`)

Each `AutomationManager` instance can be created with a `user_scope_id`; tasks are stored in `automations/` (global) or `automations/<user_scope_id>/` (per-user). Tasks carry `user_scope_id` so that when an automation runs (prompt-based or workflow-based), the agent and workflow engine use that scope: RAG/memory, calendar, messaging, contacts, mail, and automation notes/todos all run with the owner's credentials and data. The agent injects `user_scope_id` into automation tools (`create_automation`, `list_automations`, etc.) so new tasks are stored in the correct user directory. The CLI/scheduler uses an aggregated manager that loads from all scope dirs and saves/deletes/restores via the task's scope path.

**Background-run live-emit isolation.** A scheduled automation runs silently and must not surface in another user's live session. With `VAF_IN_AUTOMATION=1`, `_emit_to_web_ui()` is `False` (no status/context/retry emits). Tool start/end updates are not gated by that env, because a concurrent real user's tool updates must keep flowing. Since a background automation agent has no web session of its own, a naive tool emit would fall back to the process-wide "current session" and could surface in whichever user's web session is currently active. To prevent this, a per-agent flag `agent._background_run = True` (set in `run_task`) is checked at both `emit_tool_update` sites so a background run broadcasts no tool bubbles. The flag is per-instance and therefore race-free; gating on the process-wide env would also suppress a concurrent real user's updates.

**Handoff bundle isolation.** When a background automation must ask the user something it cannot decide, it stores its full working history as a *handoff bundle* under `Platform.vaf_dir() / "handoff_bundles" / <user_scope_id> /<id>.json`, keyed by the raw scope id (aligned with `thinking_requests`). The linked tracked request and the bundle are written under the same resolved scope (`user_scope_id or local_admin_scope_id`), so only the **same** user's main agent - finding the request under its own scope - can load the bundle and continue the task; a bundle written for user A is unreadable for user B. See [AUTOMATIONS.md](../platform/AUTOMATIONS.md#silent-background-execution--context-handoff).

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

Currently **single-admin only** - one Discord bot per VAF instance. Not multi-tenant.

### Email

Uses **per-user credential keys** in a two-tier store: the OS keyring when available (entries protected by the OS keyring itself, not by VAF encryption), with an AES-256-GCM envelope-encrypted fallback file (`email_credentials.enc`, shared DEK per file; see `vaf/core/secure_store.py`) when no keyring is usable. Each user's IMAP/SMTP sessions use their own stored credentials. Credential keys include the `user_scope_id` when set (format: `email:{provider}:{scope_id}:{account_id}`), falling back to username-based keys for legacy data; non-admin scopes never fall back to admin/legacy keys.

### Calendar (Google / Microsoft)

Calendar uses the **same OAuth credentials and the same `user_scope_id`** as Email. There are no separate calendar credential keys. The calendar client (`vaf/core/calendar_client.py`) and calendar tools call `get_valid_access_token(..., user_scope_id=user_scope_id)` and use the same account list from `email_config` / `email_config_by_scope`. All calendar API calls are therefore scoped per user.

Email config lookup follows a three-tier chain:
1. `email_config_by_scope[user_scope_id]` - preferred, UUID-based
2. `email_config_by_user[username]` - legacy per-user
3. `email_config` - legacy global/admin fallback

When the primary lookup returns no accounts (e.g. chat session uses local admin but accounts were added under a JWT scope), the tools fall back to legacy `email_config` and, in single-scope setups, to the single scope in `email_config_by_scope`, so the mail client and agent see the same accounts. The sync store (messages) uses the same idea: the tool tries the primary store, then legacy and single-scope stores, so it reads from the same DB as the mail client.

Synced messages are stored per-scope in `scopes/<user_scope_id>/email_sync.db` (or legacy path for local admin).

### Config: global vs user-scoped

- **Global (admin-only to change):** Backend and network settings apply to all users. Only admins can edit them. This includes: Network tab (local network, ports, TLS, hosting), Advanced tab (server, tray, timeouts, etc.), API keys and provider/model settings, OAuth client IDs, TTS/STT engines, URLs and enable toggles (`stt_enabled` included), the voice provider selection (`speech_tts_provider`, `speech_stt_provider` and their model/voice keys, plus `api_key_elevenlabs`), and similar server-wide options. The auto-speak preference `tts_auto_speak` is deliberately user-writable (playback preference; billing exposure is bounded by the admin-gated enable and provider keys). Stored in the single `config.json`; non-admin PATCH and WebSocket `save_config` are filtered so these keys are not overwritten. To reduce accidental data loss, config merge also preserves existing sensitive values when an incoming update contains empty API key strings or `null` connection configs.
- **User-specific:** Connections (Mail, WhatsApp, Telegram, Discord, Cloud, Calendar, GitHub), language/interface preferences, and automations are per user. Non-admins can change only the keys that are not in the global set (e.g. language, time format). Connection data is already keyed by `user_scope_id` or username where applicable.

The Settings UI shows the **General**, **AI & Model**, **Advanced**, and **Local Network** tabs only to admins (controlled by the `adminOnly` flag in the `CATEGORIES` array and per-tab content rendering guards). Non-admin users are automatically redirected away from admin-only tabs. All users see Persona, Voice, Interface, Connections, Automations, and About, and receive the global config they need for display/behavior, but it is **credential-redacted**: for non-admins the backend strips secret values (`api_key_*`, `*_client_secret`, `*_secret`, `*_credentials_key`, `*_encryption_key`, `*_kek`, `*_password`, plus `secure_store_kek`, `memory_db_url`, `redis_url`) from every config read (`GET /api/config` and the WebSocket config push) via `Config.config_for_user()` / `Config.is_secret_config_key()`. This read-redaction is intentionally narrower than the admin-only *write* denylist (`is_secret_config_key` vs `is_global_config_key`): non-secret admin-only keys the UI needs (model/provider names, non-secret network settings) stay readable, only credentials are removed. Admins receive the full config.

## 7. Watchdog and Admin Security APIs

### Sub-agent watchdog (`vaf/api/supervisor_routes.py`)

`GET /api/supervisor/status` and `POST /api/supervisor/cancel` are caller-scoped. Unit payloads carry user-authored task text, and a task id is all `/cancel` needs, so before this gate any authenticated user could list every user's running sub-agents, read their task text, and kill them. Now a non-admin sees, and can cancel, only units belonging to sessions owned by their own scope; a foreign or unowned `?session` yields an empty list (never a 403, because the web tool bubble polls generically), and `/cancel` on a foreign unit returns "not permitted". Sessions without a recorded scope count as admin-only, matching the WebSocket ownership policy, and the ownership lookup is fail-closed: if it fails, a non-admin sees nothing. Admins and the tokenless localhost desktop (which resolves to the local admin) keep the full watchdog view with per-unit username attribution. Details in [TOOL_SUPERVISION.md](../agents/TOOL_SUPERVISION.md); guarded by `tests/test_supervisor_scoping.py`.

### Security dashboard (`vaf/api/security_routes.py`)

The `/api/security/*` surface (overview, events, skill actions) is admin-only (`Depends(require_admin)`) by design: it deliberately aggregates cross-user data for the admin's Logs Overview dashboard, including per-user workspace sizes and per-scope memory/chunk counts, with usernames attached server-side. Full scope UUIDs never leave the backend (the overview shortens them to the first 8 characters for display). This aggregate lane, including the owner-connection read path described in Section 4, must never be reachable from a per-user route.

## Isolation Summary Table

| Component | Isolation mechanism | Level |
|-----------|---------------------|-------|
| Memory CRUD | `user_scope_id` filter on every query | Application |
| Memory graph | Scope filter on auto-connect and manual operations | Application |
| Gateway | Server-side scope extraction, client scope stripped | Transport |
| Config read (`GET /api/config`) | Secret keys (API keys, OAuth client secrets, JWT/encryption keys, DB URLs) redacted for non-admins; admins get full config | Application |
| Redis cache | Scope-prefixed cache keys | Caching |
| Tool confirmation grants | Per-scope trust store `~/.vaf/trust/<scope>.json` (local admin -> `default.json`); one tenant's "always" never answers for another. `tool_confirmation_bypass_admins` (admin-only, default off) lets an admin skip the dialog and emits `gate_bypassed` each time - it cannot widen `admin_only` or the account allowlist | Application |
| PostgreSQL | Fail-closed RLS policy (ENABLED + FORCED); active enforcement pending the app-role cutover to `vaf_app` (owner role bypasses RLS today) | Database |
| Filesystem | Scope-based paths (`~/.vaf/scopes/<user_scope_id>/`) preferred; legacy `~/.vaf/users/<username>/` as fallback | OS |
| Generated projects (VAF_Projects) | `~/Documents/VAF_Projects/<uid[:8]>/<session_id>/` when session context is present; legacy flat root otherwise | OS |
| Session workspace | `Session.project_path` anchored to first `VAF_Projects` creation; `[SESSION WORKSPACE]` injected per turn | Application |
| Central Data Explorer (`/api/workspaces`) | Per-user root derived from authenticated scope; lists/searches/renames/deletes only the caller's own workspaces (incl. orphans); opaque handles, not paths; search takes only a query string, never a path | Application |
| File tools (read AND write) | Per-user jail (contextvar over `is_safe_path`, entered via `user_jail`): non-admin confined to own `VAF_Projects/<uid[:8]>/` - plus, for READS only, the folders of skills visible to them; admin (`is_admin_identity`) full; another user's tree always denied, fail-closed | OS |
| Outgoing attachments (mail + messengers) | All five senders declare `file_access = "write"`: a non-admin attaches only from their own tree; symlink targets are re-checked by `is_safe_path` itself | OS |
| Sandbox | Per-user working directory in Docker | Container |
| Sub-agent watchdog (`/api/supervisor/status`, `/cancel`) | Non-admins see and can cancel only units of sessions owned by their scope; unscoped sessions admin-only; fail-closed ownership lookup; admins get all units with username attribution | Application |
| Security dashboard (`/api/security/*`) | Admin-only by design (`require_admin`); aggregates cross-scope metrics server-side; full scope UUIDs never leave the backend | Application |
| Browser sessions (cookies/logins) | Per-user `~/.vaf/browser_sessions/<scope>/` store keyed by user_scope_id | OS |
| WhatsApp | Separate subprocess per user | Process |
| Telegram | Whitelist-based routing | Application |
| Email | Per-user encrypted credentials + scope-based config lookup chain | Application |
| Calendar (Google/Microsoft) | Same OAuth and `user_scope_id` as Email; no separate credentials | Application |
| Automations | Per-user task storage and scoped RAG access; max 3 users per time slot (global cap) | Application |
| Automation planner (notes/todos) | Per-user `automation_planner/<scope>/notes.json`, `todos.json` | Application |
| Thinking workspace | Per-user `workspaces/<scope_key>/` with boundary-checked file access and handoff approvals | Application |
| Config (global vs user) | Backend/network/API keys: admin-only write; non-admins can change only user-scoped settings | Application |
| Agent rooms (A2A) | One account per room by default (`_check_tenant`); a room appears in the sidebar only for accounts that are members (`joined_rooms`); each room turn runs bound to the account whose room it is, and its live events route to that account rather than to the room's owner | Application |
| Rooms across accounts (`multi_scope`) | Opt-in per room, and it takes only the accounts it ADMITTED (`Room.admit`, host or leader only, logged as `room_account_admitted`) - knowing the id admits nobody. Inside such a room isolation is deliberately relaxed and the relaxation is the point: every member reads every frame, and members reach the room's shared folder through one clause inside the file jail's cross-account invariant. A newcomer starts reading at its own join, so it never receives the history of people it has never met | Application |

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

# Wrong - no scope filtering
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

# Wrong - shared across users
cache_key = f"my_feature:{item_id}"
```

### Rule 5: Scope database queries in new tables

When creating new tables that hold user data:

1. Add a `user_scope_id` column (UUID, nullable for system/shared data).
2. Add a fail-closed RLS policy mirroring the current `memories` table pattern, and `FORCE` RLS so the owner does not bypass it.
3. Grant the non-superuser application role (`vaf_app`) `SELECT, INSERT, UPDATE, DELETE` on the new table - the application data connection runs as `vaf_app`, not the table owner.
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

# Wrong - shared location
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

Scheduled tasks, cron jobs, and background workers must carry `user_scope_id` through the entire execution chain. Don't assume scope from the task registration context - store it explicitly in the task definition.

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
| Audit logging | Perimeter/auth trail exists: the always-on security event log (`vaf/core/security_events.py`) records blocked IPs, invalid tokens, failed logins/2FA, rejected WebSocket handshakes, rejected channel senders, and skill blocks/quarantines; admin-readable via `GET /api/security/events`, the `security_<date>.log` file, and the Logs Overview dashboard | Log application-level cross-scope DATA access denials (e.g. a scoped memory query returning "not found" for a foreign id, or a WS session-ownership denial), which are still unrecorded |
| Memory encryption keys | Shared key across users | Consider per-user encryption keys for stronger data separation |
| WebSocket connections | Shared event loop | Monitor for resource exhaustion by single user |

## Related Documentation

- [USER_IDENTITY.md](../memory/USER_IDENTITY.md) - User profile and preferences system
- [MEMORY_SYSTEM.md](../memory/MEMORY_SYSTEM.md) - Memory storage and RAG pipeline
- [CONNECTIONS.md](../integrations/CONNECTIONS.md) - External service connections (WhatsApp, Telegram, etc.)
- [SANDBOXING.md](SANDBOXING.md) - Docker sandbox for code execution
