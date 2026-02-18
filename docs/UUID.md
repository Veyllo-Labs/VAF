# UUID-Based User Identity in VAF

This document defines how user identity works across all layers of the VAF stack. It serves as the authoritative reference for any developer (human or AI) building or refactoring multi-user features.

> **Current state (Phases 1–4 complete):** All user-scoped stores (email, contacts, WhatsApp, credentials) accept `user_scope_id` and use scope-based paths (`scopes/<uuid>/`). The config lookup chain is `email_config_by_scope[uuid]` → `email_config_by_user[username]` → `email_config` (legacy). Username-based paths are kept as backward-compatible fallbacks. Phases 5–6 (remove legacy username scoping, enforce role-based auth) remain future work.

---

## Core Principle

**`user_scope_id` (UUID) is the single source of truth for user identity and data isolation.** Every piece of user-owned data must be associated with a `user_scope_id`. The `username` string is a human-readable label for display and filesystem paths only — never for authorization or data scoping decisions.

---

## Identity Fields

| Field | Type | Purpose | Mutable? | Example |
|-------|------|---------|----------|---------|
| `user_scope_id` | UUID v4 | **Tenant isolation key**. Used in DB queries, cache keys, RAG filtering, and all data ownership checks. | No (immutable after creation) | `f01a10fe-e959-4c71-b93f-6bc4073d2072` |
| `user_id` | UUID v4 | Database primary key (`local_users.id`). Internal only. | No | `a8b3c1d2-...` |
| `username` | String | Human-readable login name. Used for filesystem paths and display. | Yes (rename possible) | `Mert`, `alice` |
| `role` | String | Authorization level: `admin`, `user`, `guest`. | Yes (promotable) | `admin` |

### Local Admin Defaults

When running in single-user / localhost mode (no network auth):

| Config Key | Default | Purpose |
|------------|---------|---------|
| `local_admin_scope_id` | `00000000-0000-0000-0000-000000000001` | Fixed UUID for the local admin user |
| `local_admin_username` | `admin` | Display name for the local admin |

**Important:** The local admin is identified by `local_admin_scope_id`, NOT by the string `"admin"`. A network user whose username happens to be `"admin"` is a different user with a different `user_scope_id`.

---

## How Identity Flows Through the Stack

```
                     ┌──────────────────────────────────┐
                     │         Auth Database            │
                     │  local_users table               │
                     │  ┌─────────────────────────────┐ │
                     │  │ id (PK)     : UUID          │ │
                     │  │ username    : String UNIQUE │ │
                     │  │ user_scope_id: UUID UNIQUE  │ │
                     │  │ role        : String        │ │
                     │  │ password_hash: String       │ │
                     │  └─────────────────────────────┘ │
                     └──────────────┬───────────────────┘
                                    │ Login
                                    ▼
                     ┌─────────────────────────────────┐
                     │            JWT Token            │
                     │  {                              │
                     │    "sub": user_id,              │
                     │    "username": username,        │
                     │    "role": role,                │
                     │    "user_scope_id": scope_uuid, │
                     │  }                              │
                     └──────────────┬──────────────────┘
                                    │
                 ┌──────────────────┼──────────────────┐
                 │                  │                  │
                 ▼                  ▼                  ▼
          ┌─────────────┐   ┌─────────────┐    ┌──────────────┐
          │  HTTP API   │   │  WebSocket  │    │  Messaging   │
          │  Middleware │   │  Connect    │    │  Bridges     │
          │  sets       │   │  extracts   │    │  (Telegram,  │
          │  request.   │   │  user from  │    │   WhatsApp,  │
          │  state.user │   │  JWT/state  │    │   Discord)   │
          └──────┬──────┘   └──────┬──────┘    └──────┬───────┘
                 │                  │                   │
                 │    ┌─────────────┘                   │
                 │    │                                 │
                 ▼    ▼                                 ▼
          ┌─────────────────────────────────────────────────┐
          │              Agent Instance                     │
          │  _current_user_scope_id : UUID  ← data scoping  │
          │  _current_username      : str   ← display/paths │
          └──────────────────┬──────────────────────────────┘
                             │ Tool execution
                             ▼
          ┌──────────────────────────────────────────────────┐
          │                  Tool Layer                      │
          │  tool_args["user_scope_id"] → data operations    │
          │  tool_args["username"]      → filesystem/config  │
          └──────────────────┬───────────────────────────────┘
                             │
          ┌──────────┬───────┼──────────┬──────────┐
          ▼          ▼       ▼          ▼          ▼
     ┌─────────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────────┐
     │ Memory  │ │Redis │ │Email │ │Files │ │Credential│
     │  (PG)   │ │Cache │ │Store │ │System│ │  Store   │
     │  scope  │ │scope │ │scope │ │user  │ │  scope   │
     │  =UUID  │ │=UUID │ │=UUID │ │dirs  │ │  =UUID   │
     └─────────┘ └──────┘ └──────┘ └──────┘ └──────────┘
```

---

## Layer-by-Layer Rules

### Layer 1: JWT & Authentication

**File:** `vaf/auth/crypto.py`

The JWT payload MUST contain all four identity fields:

```python
payload = {
    "sub": str(user.id),                 # DB primary key
    "username": user.username,            # Human-readable
    "role": user.role,                    # Authorization
    "user_scope_id": str(user.user_scope_id),  # Tenant key (CRITICAL)
}
```

**Rules:**
- `user_scope_id` MUST be present in every access token.
- Refresh tokens inherit the same `user_scope_id`.
- Token validation failure = reject request. Never fall back to admin scope for authenticated requests.

### Layer 2: Middleware & Request State

**File:** `vaf/auth/middleware.py`

After JWT validation, `AuthMiddleware` sets `request.state.user` as a dict:

```python
request.state.user = {
    "user_id": payload.get("sub"),
    "username": payload.get("username"),
    "role": payload.get("role"),
    "user_scope_id": payload.get("user_scope_id"),
}
```

**Rules:**
- All downstream code reads `request.state.user` — never re-parses the JWT.
- Localhost bypass (no JWT) means `request.state.user` is NOT set. Routes must handle this and fall back to `local_admin_scope_id`.
- Never trust `username` from the request body or query params for scoping. Always use `request.state.user`.

### Layer 3: WebSocket & Task Queue

**Files:** `vaf/core/web_server.py`, `vaf/core/headless_runner.py`

WebSocket connections extract identity from the JWT or use local admin defaults:

```python
# web_server.py — WebSocket connect
metadata = {
    "user_scope_id": user_context.get("user_scope_id"),
    "username": user_context.get("username"),
}
task_queue.add(session_id=sid, input_text=text, metadata=metadata)
```

```python
# headless_runner.py — Before agent.chat_step()
agent._current_user_scope_id = meta.get("user_scope_id")
agent._current_username = meta.get("username")
```

**Rules:**
- Both `user_scope_id` and `username` MUST be propagated through task metadata.
- The agent's `_current_user_scope_id` drives all data operations (memory, RAG, cache).
- The agent's `_current_username` drives filesystem paths and display.
- `user_scope_id` in client-sent WebSocket payloads is ALWAYS stripped (security).

### Layer 4: Agent Tool Execution

**File:** `vaf/core/agent.py` (tool dispatch, ~line 6100)

The agent injects identity into tool arguments before calling `tool.run()`:

```python
# Memory tools → user_scope_id (UUID)
if name in ("memory_save", "memory_search"):
    tool_args["user_scope_id"] = getattr(self, "_current_user_scope_id", None)

# Email/messaging tools → username (string)
if name in ("mail_inbox", "send_mail", ...):
    tool_args["username"] = getattr(self, "_current_username", None) or "admin"
```

**Rules:**
- Memory tools receive `user_scope_id` (UUID). They never use `username`.
- Communication tools (email, WhatsApp, contacts) receive `username` (string) for config/credential lookup.
- **Target state:** Communication tools should ALSO receive `user_scope_id` and use it as the primary key, with `username` only for filesystem paths.

### Layer 5: Data Storage

#### PostgreSQL (Memories)

```sql
-- Every memory belongs to exactly one user
SELECT * FROM memories WHERE user_scope_id = :scope;

-- Row-Level Security enforces this at the DB level
CREATE POLICY user_isolation_memories ON memories
    USING (
        COALESCE(current_setting('app.current_user_scope_id', true), '') = ''
        OR user_scope_id IS NULL
        OR user_scope_id = current_setting('app.current_user_scope_id', true)::uuid
    );
```

**Rules:**
- Every user-owned table MUST have a `user_scope_id UUID` column.
- Every query MUST filter by `user_scope_id` at the application level.
- RLS provides defense-in-depth, not primary enforcement.

#### Redis (Cache)

```python
# Cache key format includes scope
key = f"rag_query:{hash}:scope={user_scope_id}"
key = f"memory_graph:{user_scope_id}:{limit}"
```

**Rules:**
- Every cache key that holds user data MUST include `user_scope_id`.
- A missing scope means "global" data (no user association).

#### SQLite (Email Sync, WhatsApp, Contacts)

```python
# Current: path determined by username
~/.vaf/users/<username>/email_sync.db
~/.vaf/users/<username>/whatsapp_messages.db
~/.vaf/users/<username>/contacts.json
```

**Target state (migration):**
```python
# Better: path determined by user_scope_id
~/.vaf/users/<user_scope_id>/email_sync.db
~/.vaf/users/<user_scope_id>/whatsapp_messages.db
~/.vaf/users/<user_scope_id>/contacts.json
```

This makes user data independent of username changes.

#### Config (JSON)

```json
// Current: keyed by username
{
  "email_config_by_user": {
    "Mert": { "accounts": [...] },
    "alice": { "accounts": [...] }
  }
}
```

**Target state (migration):**
```json
// Better: keyed by user_scope_id
{
  "email_config_by_scope": {
    "f01a10fe-e959-4c71-b93f-6bc4073d2072": { "accounts": [...] },
    "b2c3d4e5-...": { "accounts": [...] }
  }
}
```

#### Credential Store (Keyring / Encrypted File)

```python
# Current: keyed by username
key = f"email:{provider}:{username}:{account_id}"

# Target: keyed by user_scope_id
key = f"email:{provider}:{user_scope_id}:{account_id}"
```

---

## Current Problems (Why This Migration Matters)

### Problem 1: Username ≠ Admin Role

The current code checks `if username.lower() == local_admin_username` to determine admin access. This breaks when:
- A network user has admin role but a different username (e.g., "Mert" with role=admin)
- The local admin username is changed in config
- Two users named "Admin" and "admin" exist (case sensitivity)

**Fix:** Check `role == "admin"` for authorization. Check `user_scope_id == local_admin_scope_id` for data fallback.

### Problem 2: Username Rename Breaks Data

If a user's username is changed, all of the following break:
- Filesystem paths (`~/.vaf/users/<old_name>/` orphaned)
- Config keys (`email_config_by_user["old_name"]` orphaned)
- Credential keys (`email:email:old_name:...` orphaned)
- Session metadata (cached `username` in old sessions)

**Fix:** Use `user_scope_id` (immutable) for all data paths and keys. Keep `username` only for display.

### Problem 3: Local Mode Identity Mismatch

When the system is in local mode (no auth), the WebSocket assigns `username="admin"`. But when network mode is enabled with the same user, the JWT provides the real username (e.g., "Mert"). Now the same person has data split across two identities.

**Fix:** Always use `user_scope_id` for data lookup. Local admin has a fixed `user_scope_id` (`00000000-0000-0000-0000-000000000001`) that maps correctly regardless of username.

### Problem 4: Email Config Fallback Chain

Currently, email config lookup follows this chain:
1. `email_config_by_user[username]` (per-user)
2. `email_config` (legacy/admin fallback)

This means a user who logs in as "Mert" won't find email accounts stored under legacy config unless a fallback is explicitly coded. Every new tool that reads email config must remember to implement this fallback.

**Fix:** Store under `email_config_by_scope[user_scope_id]`. One lookup, no fallback needed.

---

## Migration Plan

### Phase 1: Add `user_scope_id` Alongside `username` (Non-Breaking) — ✅ Done

All functions that previously accepted only `username` now also accept `user_scope_id: Optional[str] = None`. Legacy `username` parameter is preserved for backward compatibility.

**Migrated files:**
- `vaf/tools/mail_utils.py` — `store_scope_from_kwargs()`, `cred_scope_from_kwargs()`
- `vaf/core/email_sync_store.py` — All CRUD functions accept `user_scope_id`
- `vaf/core/whatsapp_message_store.py` — `_db_path()`, `append_message()`, etc.
- `vaf/core/contacts_store.py` — All CRUD + lookup functions accept `user_scope_id`
- `vaf/core/credential_store.py` — `_credential_key()`, get/set/delete functions
- `vaf/core/email_transport.py` — `_get_email_config()`, `fetch_mail()`, `send_mail()`, etc.
- `vaf/api/email_routes.py` — All endpoints extract `user_scope_id` from `_get_current_user()`
- `vaf/api/contact_routes.py` — All CRUD endpoints pass `user_scope_id`
- `vaf/core/oauth_pkce.py` — `get_valid_access_token()` passes scope to credential operations

### Phase 2: Add `email_config_by_scope` Config Key — ✅ Done

```json
{
  "email_config_by_scope": {
    "<uuid>": { "accounts": [...] }
  },
  "email_config_by_user": { ... },
  "email_config": { ... }
}
```

Lookup priority (implemented in `email_transport._get_email_config()` and `mail_utils.list_accounts_with_labels_for_user()`):

1. `email_config_by_scope[user_scope_id]` — preferred, UUID-based
2. `email_config_by_user[username]` — legacy per-user
3. `email_config` — legacy global/admin fallback

### Phase 3: Migrate Filesystem Paths — ✅ Done

```
~/.vaf/scopes/<user_scope_id>/       # New scope-based paths (preferred)
~/.vaf/users/<username>/             # Legacy paths (fallback)
```

Stores use scope-based paths when `user_scope_id` is provided:
- `email_sync_store.py` → `scopes/<uuid>/email_sync.db`
- `whatsapp_message_store.py` → `scopes/<uuid>/whatsapp_messages.db`
- `contacts_store.py` → `scopes/<uuid>/contacts.json`

Migration script: `scripts/migrate_users_to_scopes.py`
- Reads `local_users` table to map `username → user_scope_id`
- Copies directories (does not delete originals)
- Migrates `email_config_by_user` → `email_config_by_scope`
- Supports `--dry-run` and `--config-only` flags

### Phase 4: Migrate Credential Store Keys — ✅ Done

```python
# Legacy key format (still supported as fallback)
"email:email:alice:alice@example.com"

# Scope-based key format (preferred when user_scope_id is set)
"email:email:b2c3d4e5-...:alice@example.com"

# Local admin key format (no scope prefix, matches legacy)
"email:email:alice@example.com"
```

Implemented in `credential_store._credential_key()`. Both formats are supported; scope-based keys take priority when `user_scope_id` is provided.

### Phase 5: Remove Username-Based Scoping — TODO

Once all data is keyed by `user_scope_id`:
- Remove `_local_admin()` string comparison functions
- Remove `email_config_by_user` config key (deprecated)
- Remove `store_username_from_kwargs` / `cred_username_from_kwargs` (replaced by scope-based equivalents)
- Simplify all `_get_email_config()` functions to single-path lookup

### Phase 6: Enforce Role-Based Authorization — TODO

Replace all `if username == local_admin_username` checks with:
```python
if role == "admin":
    # Admin-level permissions
```

And for data fallback:
```python
if user_scope_id == Config.get("local_admin_scope_id"):
    # Local admin data path
```

---

## Developer Checklist: New Feature

When building a new feature that handles user data:

- [ ] **Accept `user_scope_id: UUID`** as parameter for all data operations
- [ ] **Filter queries** by `user_scope_id` (never by `username`)
- [ ] **Include `user_scope_id` in cache keys** if caching user data
- [ ] **Use `username` only** for filesystem paths and display text
- [ ] **Never compare `username` to `"admin"`** for authorization — use `role`
- [ ] **Never compare `username` to `local_admin_username`** for data scoping — use `user_scope_id == local_admin_scope_id`
- [ ] **Handle `user_scope_id = None`** gracefully (means local/unauthenticated mode)
- [ ] **Add `user_scope_id` column** to any new database table holding user data
- [ ] **Add RLS policy** mirroring the `memories` table pattern for new tables
- [ ] **Test with 2+ users** to verify isolation
- [ ] **Return 404 (not 403)** when a user tries to access another user's resource

---

## File Reference: Current Identity Patterns

### Files That Correctly Use UUID (`user_scope_id`)

| Component | File | Pattern |
|-----------|------|---------|
| Memory CRUD | `vaf/memory/rag.py` | `WHERE Memory.user_scope_id == scope` |
| Memory Model | `vaf/memory/models.py` | `user_scope_id` column on `memories` table |
| Graph Operations | `vaf/memory/graph.py` | Scope filter on auto-connect |
| Cache Keys | `vaf/memory/cache.py` | `scope={user_scope_id}` in key |
| Database RLS | `vaf/memory/database.py` | `SET LOCAL app.current_user_scope_id` |
| Memory Routes | `vaf/memory/routes.py` | `Depends(get_current_user_scope)` |
| Agent RAG | `vaf/core/agent.py` | `_current_user_scope_id` for memory_save/search |
| Sandbox | `vaf/tools/python_sandbox.py` | `/tmp/vaf_{scope_prefix}_{exec_id}` |
| Automation | `vaf/core/automation.py` | Tasks carry `user_scope_id` |
| Email Config | `vaf/tools/mail_utils.py` | `email_config_by_scope[scope]` → `email_config_by_user` → `email_config` |
| Email Transport | `vaf/core/email_transport.py` | `_get_email_config(username, user_scope_id)` with scope-first lookup |
| Email Sync Store | `vaf/core/email_sync_store.py` | `scopes/{scope_id}/email_sync.db` |
| Email Routes | `vaf/api/email_routes.py` | `_get_current_user()` returns `user_scope_id`, all endpoints pass it |
| WhatsApp Store | `vaf/core/whatsapp_message_store.py` | `scopes/{scope_id}/whatsapp_messages.db` |
| Contact Store | `vaf/core/contacts_store.py` | `scopes/{scope_id}/contacts.json`, all CRUD + lookup functions |
| Credential Store | `vaf/core/credential_store.py` | `email:{provider}:{scope_id}:{account_id}` |
| Config Routes | `vaf/api/config_routes.py` | `get_current_scope_id()`, `user_scope_id` in user dict |
| Contact Routes | `vaf/api/contact_routes.py` | All CRUD endpoints pass `user_scope_id` |
| OAuth PKCE | `vaf/core/oauth_pkce.py` | `get_valid_access_token(user_scope_id=...)` for token refresh |
| All Mail Tools | `vaf/tools/mail_inbox.py`, `send_mail.py`, etc. | `cred_scope_from_kwargs()` / `store_scope_from_kwargs()` |

### Files That Still Need Migration (Phase 5+)

| Component | File | Current Pattern | Target Pattern |
|-----------|------|-----------------|----------------|
| User Workspace | `vaf/auth/user_workspace.py` | `users/{username}/` | `scopes/{scope_id}/` |
| WhatsApp Auth | `vaf/core/whatsapp_auth.py` | `username` for session dir | `scope_id` for session dir |

### Hardcoded Admin String Comparisons (Phase 5 Cleanup)

> **Note:** The current implementation uses a **hybrid approach** for backward compatibility: each function checks `user_scope_id` first, then falls back to `username` string comparison. This is intentional during the transition period. Pure scope-only checks (eliminating username comparisons entirely) are Phase 5 work.

| File | Current Hybrid Pattern | Phase 5 Target |
|------|----------------------|----------------|
| `vaf/tools/mail_utils.py` | Scope check → `local_admin_username` fallback | Scope-only |
| `vaf/core/email_sync_store.py` | `_is_per_user_db(username, user_scope_id)` | Scope-only |
| `vaf/core/whatsapp_message_store.py` | `_is_per_user_db(username, user_scope_id)` | Scope-only |
| `vaf/core/contacts_store.py` | `_contacts_path(username, user_scope_id)` | Scope-only |
| `vaf/core/credential_store.py` | `_credential_key(…, user_scope_id)` | Scope-only |
| `vaf/core/email_transport.py` | `_get_email_config(username, user_scope_id)` | Scope-only |
| `vaf/api/email_routes.py` | `_store_and_cred_from_user()` returns both | Scope-only |
| `vaf/api/config_routes.py` | `get_current_scope_id()` added | Already scope-aware |

---

## Testing Multi-User Isolation

After any identity-related change, verify with at least two users:

```
User A (scope: aaa-..., username: alice)
User B (scope: bbb-..., username: bob)

1. Alice saves a memory         → scoped to aaa-...
2. Bob searches memories        → must NOT find Alice's memory
3. Alice connects Gmail         → stored under scope aaa-...
4. Bob calls mail_inbox         → must NOT see Alice's emails
5. Alice saves a contact        → stored under scope aaa-...
6. Bob lists contacts           → must NOT see Alice's contacts
7. Alice's username is renamed  → all data remains accessible
8. Cache is populated for Alice → Bob must get his own cache entry
```

---

## Related Documentation

- [USER_ISOLATION.md](USER_ISOLATION.md) — Multi-tenant security architecture (current state)
- [NETWORK_FEATURES.md](NETWORK_FEATURES.md) — Auth middleware and JWT details
- [USER_IDENTITY.md](USER_IDENTITY.md) — User profile and preferences system
