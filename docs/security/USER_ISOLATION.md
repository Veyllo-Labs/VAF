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
│  │  <data_dir>/scopes/<scope>/ per account (mail, messages) │      │
│  │  Automations, reminders, trust: one store per scope      │      │
│  └──────────────────────────────────────────────────────────┘      │
│                              │                                     │
│  Layer 6: Sandbox (Docker)                                         │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  Own container per environment, labelled with its owner  │      │
│  │  Labelled owner, non-root, own network                   │      │
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

**"Localhost" means REALLY local, not just a loopback peer.** The integrated HTTPS proxy terminates TLS on `0.0.0.0` and relays every LAN device to the backend over loopback, so the raw socket peer is `127.0.0.1` for remote users too. Trusting the peer alone therefore treated the whole LAN as local: a tokenless LAN request reached every non-exempt route and the route floors below promoted it to the local admin. The middleware now resolves the client through `vaf.network.binding.effective_client_ip(peer, x_forwarded_for)`, which honors `X-Forwarded-For` **only** when the immediate peer is loopback (a relay on this machine), and reads the RIGHTMOST hop that is not loopback: a relay appends what it saw, a client can only write to the left of it, and a loopback hop names this machine. The polarity is fail-safe: a forwarding hop **removes** trust and never grants it, so a client can make itself look more remote but never more local. Such a relay connects from `127.0.0.1`: a relay on this machine pointed at the frontend port reached the backend through the Next.js `/api` door with no hop at all, because that door passed on no `X-Forwarded-For`, and every visitor had the owner's tokenless trust (measured on a test server: the account list, 200). The door forwards the header now. The HTTPS port and `vaf run --web` switch uvicorn's own proxy-header handling off: it trusted the addresses in the `FORWARDED_ALLOW_IPS` environment variable, and with `*` a LAN device that wrote `X-Forwarded-For: 127.0.0.1` was this machine (measured), so the resolver alone decides there ([The HTTPS door holds](../setup/NETWORK_FEATURES.md#the-https-door-holds)). Requests with no hop at all, or only loopback hops - internal loopback IPC (`/api/subagent/stream`, `/api/workflow/update`, `/api/heartbeat`) and the desktop via the Next.js `/api` route - are unchanged and still pass without a token. NAMED BOUNDARY: a raw TCP forward on this machine (`ssh -R`, socat) writes no header, so its clients are this machine; never put one in front of VAF ([NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md#who-the-client-is)). The two event routes take ONLY that caller: they require an admin identity (`require_admin`), which the tokenless local process is, because they trust what they are sent (the named session, the named file, a broadcast to every connection); an account's token is refused there. The same resolver backs the WebSocket handshake (`web_server._ws_client_ip`), the OAuth callback loopback exception (`oauth_session_binding._real_client_ip`), the admission check (`AdmissionGuard` in `vaf/network/admission.py`, which judges HTTP requests and WebSocket handshakes alike) and the addresses written to the security log, so none of them can disagree about who the client is. They reach it through `binding.connection_client_ip(conn)`, the only place in `vaf/` that reads a connection's client address; `tests/test_client_address_has_one_reader.py` refuses a direct read anywhere else. The IP check was the site that still read the peer: it passed every request the proxy relayed, a public address included.

### The account rows are sealed

The accounts live in `local_users`, in the same PostgreSQL as the memories, and whoever can write that table could otherwise set their own password hash and `requires_2fa_setup`, sign in and enrol a fresh second factor. Writing it takes the database password (the published default until `vaf secure rotate-db`), and the database is reachable from every process on the machine and from the containers on the stack's network. So every row carries `seal`, an HMAC-SHA256 under `account_seal_key` from the data keyring, over the columns that grant a login or authority (`SEALED_COLUMNS` in `vaf/auth/account_seal.py`). A row whose seal does not verify grants nothing: the login (checked after the password, so a stranger learns nothing about a row, and before the terminal door's hash copy and any token), `/setup-2fa`, `/verify-2fa`, `/me`, the per-request standing check (a token whose account was changed answers "tampered" and is refused), the tool permissions (no account) and the account directory (inactive) all ask it, and each refusal is the security event `account_seal_broken`. Every ORM write re-seals the row through a mapper hook, so no writer seals by hand; the hook checks the OLD seal first, so a harmless write (a login time, an admin's edit) cannot launder a row changed outside VAF: such a row is frozen, the admin's edit and resets answer 409, deleting it stays possible, and a terminal on the machine re-establishes it (`vaf users reset-password NAME`, which shows such a record and asks first). Rows that predate the seal are sealed once, at the first start that has it (trust on first use); that this happened, and that the installation has had an admin, is recorded in the keyring, so nulling the seals or deleting every admin row does not reopen anything: the web setup (`/needs-setup`, `/setup-code`, `/bootstrap`, `/test-veyllo-key`) stays closed and only `vaf setup` in a terminal can set an admin up again. Before the first admin exists, a browser that is not on this machine needs the setup code `vaf setup code` prints in a terminal on it ([NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md)). Making a token for programs asks the seal too (`mint_program_token` in `vaf/auth/sessions.py`), so a row changed in the database cannot be turned into a long-lived credential. NAMED BOUNDARY: whoever can read the keyring (the VAF system user, root) can seal, the same boundary as the JWT secret.

### Local mode fallback

The route-level local-admin floors (`config_routes.get_current_user_or_local_admin`, `user_routes._current_user`, `supervisor_routes._caller` and the scope-only floors in the connection/persona/contact routes) are **unconditional**: with no `request.state.user` they return the local admin identity regardless of `local_network_enabled`. That is deliberate - in single-user desktop mode the auth middleware does not run, so the floor is the only identity source for the entire app. What keeps those floors out of LAN reach is `NetworkModeLayers` (`vaf/auth/middleware.py`), registered in every mode and deciding on every request: in network mode the auth middleware challenges any request that is not really local, and with Local Network off any request that is not really local is refused outright (403, a WebSocket with 4003). It used to be the auth middleware alone, added when the backend was imported and only if Local Network was on then, so a VAF started with it off and switched on in the settings served other devices as the local admin until a restart (measured: the whole configuration and the account list, 200). Only `vaf/memory/routes.py` denies instead of flooring when a scope is missing.

**"Really local" also means "not another web page".** A browser on this machine is a loopback client whatever page it shows, and the `vaf_token` cookie goes to every localhost port. So a page served from another localhost port, a LAN address, or a DNS name rebound to `127.0.0.1` reached these floors, and the cookie, as the owner. `ForeignOriginGuard` (`vaf/auth/middleware.py`) is registered in every mode, single-user included, and refuses what the browser marks as coming from a page that is not VAF's own (`Origin`, `Sec-Fetch-Site`, the dialled `Host`) before any identity is resolved, on HTTP and on the WebSocket handshake. Clients that send no browser marks (CLI, IPC, the tray) are not judged. The rules are in [NETWORK_FEATURES.md](../setup/NETWORK_FEATURES.md#origin-guard-every-mode).

When running locally without authentication (CLI or Web UI without JWT), VAF uses the scope and username from config:

- **`local_admin_scope_id`**: Default `00000000-0000-0000-0000-000000000001` (legacy placeholder). After the first admin is created - in the browser via `POST /api/auth/bootstrap`, or in the terminal via `vaf setup` - the shared layer `vaf/auth/user_admin.py` writes that admin's UUID here so CLI and localhost use the same identity as the logged-in admin.
- **`local_admin_username`**: Default `admin`; set to the first admin's username when that account is created (either route). It is a real username, not the literal `admin` - the agent workspace under `~/.vaf/users/<username>/` is keyed on it.

Use `get_local_admin_scope_id()` and `get_local_admin_username()` from `vaf.core.config` instead of reading config directly. This keeps data scoped consistently and avoids a split between "logged-in" and "local" identities.

**Who counts as an admin: `is_admin_identity(role, user_scope_id)`.** Admin is two halves and both are load-bearing. The **role** half covers every admin account: VAF supports more than one admin (user management refuses only to delete the *last* one) and each carries their own scope UUID. The **scope** half covers the machine owner when there is no role claim at all - the tokenless desktop, the CLI and automations resolve to `local_admin_scope_id`. Only one scope can ever be the local admin, so a scope-only check silently demotes every additional admin, and a role-only check locks the owner out. The role is read exclusively from a signature-verified JWT claim (issued from `LocalUser.role` at login) and, for tools, is **assigned** by the dispatcher over whatever the model put in the arguments - never honored as model input. Use this helper rather than rebuilding the comparison: the file gates below were the three places that had rebuilt it scope-only and drifted (guarded by `tests/test_admin_identity_is_role_aware.py`).

**Where the binding happens.** For channel and WebSocket clients the headless runner applies this fallback through its turn binding (`bind_turn` in `vaf/core/turn_binding.py`, which calls `bind_identity`; the queued task's identity comes from `_task_turn_binding` in `vaf/core/headless_runner.py`). All three runner lanes bind that way before their turn: a queued task as the task's identity, an agent room's turn as the account whose room it is, and the summary of finished sub-agent work as the chat's owner, with admin rights from the account (`config.is_admin_account`), because a session file carries no role. The interactive CLI (`vaf run` and `vaf prompt`) does **not** go through the queue - it calls `Agent.chat_step()` directly - so it binds the local-admin scope and username explicitly at agent creation, via `_make_cli_agent()` in `vaf/cli/cmd/run.py`. Without this the CLI would run under scope `None` (the `"default"` bucket) and diverge from the WebUI admin: a stale `last_interaction` and memory/RAG that cannot see the admin's data. The binding is re-applied on every agent (re)creation, because `Agent.__init__` does not set a scope.

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

`SessionManager.search()` runs on the same walker and takes a mandatory scope; it previously globbed the whole store with no scope at all. Cross Chat Hint (`vaf/core/cross_chat.py`) is the first feature that lifts message text out of one chat into another chat's prompt, so it also refuses conversations with **other people**: a channel session whose endpoint matches a known contact is skipped, and the engine does not ask for hints at all on a front-office turn, a background or automation run, or a voice call. Its push to the browser (`cross_chat_hints`) goes through `push_update_to_user` and is dropped when the scope is unknown, exactly like the snippet push below. Long-term memory treats a contact's chat the other way round: it IS learned, but only into that chat's own namespace, which no other lane reads (the second partition above). The snippets a Front Office turn retrieves from it are pushed to the owner's own RAG-Snippets panel, which is the owner's data. A contact's turn runs under the OWNER's identity, so the identity gates cannot bound what it reaches (an admin owner passes `admin_only` and is exempt from the account allowlist): it is held to the Front Office tools by the engine's allowed set instead (`agent.set_allowed_tools`), which the schema and the dispatch funnel both enforce, for every identity. A tool outside the set is neither offered nor run, so no contact reaches the owner's memory, contact book, chats or mail through a tool (see [FRONT_OFFICE.md](../agents/FRONT_OFFICE.md#tool-restriction)). The same set tells the engine the turn is not the owner speaking, although it runs on the owner's scope: a contact's turn neither sees nor answers the owner's open background question (its proposal, details and handoff stay out of the prompt, the request stays open), does not set the ask-first latch (kept per account on the agent, and a contact's turn runs on the owner's account; such a turn's prompt also carries no profile digest without the owner's switch, no machine paths and no cross-chat hints; one agent serves every account of a worker, so one account's message neither answers nor holds another account's open question), and carries no line about the owner's last interaction in its system prompt (`Agent._turn_is_from_the_user`). A failed turn's internal error never comes back into a contact chat's context either: `chat_step` does not commit it, and the stored copy the web UI shows is marked so no replay, compaction or Telegram and Discord message index reads it, the index `contact_history` answers from on the contact's own turn (`session.model_text`, see [FRONT_OFFICE.md](../agents/FRONT_OFFICE.md#when-front-office-activates)).

**Retrieval scope alone is not enough - the UI push must be scoped too.** Retrieved snippets are shown in the web UI's "RAG-Snippets" panel via a WebSocket event. That push was previously a *global* broadcast to every connected client, so a correctly-scoped search under user B's scope (including a background thinking or automation run) surfaced B's snippets in user A's open panel even though B's data at rest stayed correctly isolated. The push is now routed to the owning user's connections only (`push_update_to_user(user_scope_id, ...)`) and dropped when the scope is unknown (fail-closed); the same applies to the `real_context_payload` X-ray, which carries the full prompt including the memory-context block. Real-time events that carry user content must be scoped at the emit site, not only at retrieval.

### CRUD Operations (`vaf/memory/rag.py`)

All memory access methods accept and enforce `user_scope_id`:

| Method | Scope enforcement |
|--------|-------------------|
| `get_memory(id)` | Filters by `Memory.user_scope_id == user_scope_id` |
| `update_memory(id)` | Filters by scope before allowing update |
| `delete_memory(id)` | Filters by scope before soft-delete |
| `search_memories()` | Filters query results by scope; an empty scope returns `[]` (fail-closed) in both vector and lexical/hybrid lanes - never "search all". Both lanes also leave every `chat/<session id>` namespace out unless the caller names one with `chat_key`, which no request body can do |
| `store_memory()` | Stamps `user_scope_id` on new records |
| `get_all_memories()` | Filters listing by scope |

If a user tries to access a memory ID that belongs to another user, the query returns `None` (not found) - the same response as if the memory doesn't exist. This prevents information leakage through error messages.

**Scope is not the only partition.** Inside one scope, what the agent learned in a messenger chat with a
contact lives in that chat's own namespace (`meta.source = chat/<session id>`, see
[MEMORY_SYSTEM.md](../memory/MEMORY_SYSTEM.md#chat-memory-namespaces-what-a-contact-chat-teaches-stays-in-that-chat)).
Every ordinary lookup of the owner's own agent leaves those rows out in SQL; only a turn inside that chat
and the Composer for that chat name the namespace, `update_memory` cannot move a row across the boundary,
and the namespace is deleted hard, as one unit, from the Memory page.

### Graph Connections (`vaf/memory/graph.py`)

Memory auto-connect (which links semantically similar memories) is scoped:

```python
# Only find candidates within the same user's memories
if memory.user_scope_id is not None:
    scope_filters.append(Memory.user_scope_id == memory.user_scope_id)
else:
    scope_filters.append(Memory.user_scope_id.is_(None))
# Never wire an ordinary memory to what was learned inside a messenger chat.
scope_filters.append(not_chat_lane())
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
- **Session-command ownership:** A single shared ownership gate runs before the first side effect of every Web UI session command - chat (before subscribing to the session stream), load, delete, rename, hide, and artifact edit. The session's `metadata.user_scope_id` must match the current user, or the connection must be admin (connection role `admin` or the local-admin scope). A session with no recorded `user_scope_id` is treated as admin-only for these commands. On denial the server logs and replies with `{"type":"error","message":"Access denied"}` and keeps the connection open. **`delete_session` and `hide_session` have a second refusal that is not a denial**, reached only AFTER the gate has passed: without `confirmed`, a chat the record says is not empty (`is_untouched` in `vaf/core/session.py`) is not removed but answered with `session_delete_result` / `needsConfirm`, so the browser has to show its dialog first. It is a data-loss guard rather than an isolation one, and it never reports anything about a session the caller does not own, because the gate has already refused those.
- **What may name a session (`check_session_id`, `session_access_allowed` in `vaf/core/session.py`):** an id is the name of the session file, so one that is not a single plain entry (a path separator, `.` or `..`, a leading dot, a control character, a drive prefix such as `D:x`, surrounding spaces, more than 250 bytes, the file-name limit minus `.json`) is refused by the gate for everyone, the admin included, and by every read and write of the store, so `../escape` never reaches a file outside it. It is refused, not rewritten: a rewritten id names another chat. For an id that has no file yet, a non-admin passes only with an id the server hands out, a minted `<colour><6 digits>` or their own default chat; the first message would otherwise create a free id with the caller's scope and keep it, so `telegram_<id>` before that chat's first message, another account's default chat or a variant of an existing chat's id could be claimed. The admin keeps every missing id. A chat claimed that way before this rule stays with whoever holds its file, and reaches only its own working memory (see the agent context store below). Named boundary: account names are free text, so an account named with `/`, `\` or a tab gets WhatsApp chat ids the rule refuses, and one named with `:` gets ids that name an NTFS alternate data stream on Windows; that takes a username grammar, not a wider id rule.
- **Owner re-stamp (defense-in-depth):** When a queued chat is processed, the runner stamps `user_scope_id` onto the session only if the session has none yet; it never relabels an already-owned session, so a queued chat cannot take over another user's session behind the gate.
- **Default session:** When no session is selected, the fallback session ID is per-user (`web-default-<scope>`, built by `default_session_id` in `vaf/core/session.py`), not a shared global ID.

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
- **Agent context store:** Each chat's working memory - intent, plan, tasks, notes, and team state - is stored per session under `.vaf/main/sessions/<folder>/`, and no two session ids share a folder, so it is isolated between chats (and therefore between users). The folder is the id itself when the id is a plain name (only `A-Z a-z 0-9 _ . -`, no `..`, a letter or digit first and last, at most 128 characters), otherwise `_` plus 40 hex digits of the id's SHA-256 (`MainPersistenceManager._safe_session_dir`). The earlier mapping trimmed and rewrote characters, so `red654321.` and `red654321` shared one folder while the ownership gate read them as two chats: an account that held the variant read and wrote the other account's goal, plan, tasks and notes. Two server-minted WhatsApp ids folded the same way (accounts named `anna m` and `anna_m` writing to one contact). Every id the old mapping kept unchanged keeps its folder; a chat whose id it folded starts with an empty working memory once, and the old shared folder ages out with the collector's sweep. An agent room's turn has a store of its own per room and account (`<room_id>.<peer_id>`), declared for its tools too, in a folder apart from the chats' (`.vaf/main/conversations/`): a web chat names its own id and a room's key is visible to its members, so a store under the chats' folder could be opened as a chat by another account. A note written in a room turn never lands in a chat's store or in the global one that every sessionless lane reads, and no chat id reaches the room's. The context manager's state (the compressed summary, key decisions, goal, and the files and errors noted) is per chat too, although one agent and its one manager serve every chat of a runner worker: `load_session_context` clears it before the chat's own saved state is restored, so a chat without one, a contact's first message on a channel included, starts empty. It used to keep the previous chat's state, which then went to the model with every message of the new chat and was saved as that chat's own, across accounts; the start-up sweep drops such stored copies once (any context state identical in two or more chats). See [SESSION_MANAGEMENT.md](../memory/SESSION_MANAGEMENT.md) and [CONTEXT_GLUE.md](../memory/CONTEXT_GLUE.md).

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

The graph key carries no chat dimension: a chat namespace is part of the owner's graph, so emptying one (`DELETE /api/memory/chat/{chat_key}`) invalidates the graph cache the same way a single delete does, and the node is gone on the next fetch.

### The local model server's prompt cache (named boundary)

The one local model server is shared by every account of the installation, and so is its
prompt cache: llama.cpp keeps a prompt cache (`--cache-ram`) and per-slot KV state, and the
vLLM engine an automatic prefix cache ([VLLM_ENGINE.md](../llm/VLLM_ENGINE.md)). A request
whose prompt starts like an earlier one reuses that prefix's KV cache, whoever sent it. No
text of another account is returned from it; what can leak is timing: a prefix seen before
answers its first token sooner. VAF sends no per-account cache key (vLLM's `cache_salt`;
llama.cpp has none) and keeps the server on 127.0.0.1, reached by the accounts of one
installation over the local or VPN network only. An API provider's prompt cache is that
provider's.

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
<data_dir>/scopes/<user_scope_id>/     # data_dir = Platform.data_dir(): ~/.local/share/vaf on Linux, see PLATFORM docs
├── email_sync.db              # Synced email messages, legacy store (SQLite)
├── mail.db                    # Synced email messages, v2 mail store (SQLite; MailStore, see EMAIL_CLIENT.md)
├── calendar.db                # The user's calendar (SQLite; CalendarStore, see CALENDAR_INTEGRATION.md): events,
│                              # the per-account sync state, the push target and default reminder
├── contacts.json              # User's contact list: per contact the channels, personal file, status, company, role, tags,
│                              # source and created_at, dated notes, events and channel links. The contact timeline and
│                              # key figures read only this same user's message and mail stores, keyed by the same scope
├── channel_messages.db        # Message history of the messaging channels (SQLite); whatsapp_messages.db is the legacy
│                              # file name, migrated into channel_messages.db once on first open
└── ...
```

The local admin's data remains at the legacy root paths (`<data_dir>/email_sync.db`, `<data_dir>/contacts.json`, `<data_dir>/channel_messages.db`) since `local_admin_scope_id` maps to the global location. The two newer stores do not follow that rule: `mail.db` and `calendar.db` give the local admin a scope directory like everyone else (`MailStore`, `CalendarStore`), so a store is always keyed by an explicit scope and fails closed without one; the calendar's sync supervisor collects accounts per scope from `email_config` and `email_config_by_scope`, never from the legacy `email_config_by_user` lane. That root file is a read candidate for the local admin ONLY: `contacts_store._contacts_path_candidates` walks a caller's own scope and username paths and appends `data_dir/contacts.json` only when the caller is the local admin (by scope when a scope is given, by username otherwise). A tenant with no or an empty contacts file therefore reads an empty book; it never falls through to the admin's, and no write copies the admin's book into the tenant's path. The bulk routes ignore ids outside the caller's own file for the same reason. The WhatsApp bridge's inbound allow list follows the same rule: `_get_allowed_phones_for_user` reads the caller's scope and legacy username file and never adds the admin's scope on top (it did, so the admin's Front Office contacts could write to every user's agent number), and the reply gate resolves a missing scope from the account name rather than defaulting to the admin's.

### User workspace (legacy)

Each user also has a username-based directory tree. This is the legacy layout, preserved for backward compatibility. New features should prefer scope-based paths above.

Because this tree is keyed by the **username** string, a background run must bind the user's **real** account username. Thinking Mode and scheduled Automations resolve the username from `local_users` by `user_scope_id` (and fall back to a synthetic `scope_<8hex>` on an unknown scope) - **never** the literal `"admin"`. Handing a non-admin run the username `"admin"` would make `get_user_workspace("admin")` read `~/.vaf/users/admin/user_identity.json` and inject the admin's personal identity/profile (name, preferences, dos/don'ts, timezone) into that user's system prompt and RAG query seed - exposing the admin's data to that user, even though the memory database itself stays correctly scope-isolated.

```
~/.vaf/users/<username>/       # Config.APP_DIR (vaf/auth/user_workspace.py)
├── user_identity.json         # Personal preferences
├── identity.json              # Agent persona config
├── soul.md                    # The agent's character for this person
├── logs/
├── whatsapp/                  # The linked WhatsApp login (vaf/core/whatsapp_auth.py)
└── whatsapp_avatars/          # Contact pictures, rebuilt on demand
```

The other per-person stores are not in this tree. Automations are keyed by scope, not by
name: `~/.vaf/automations/<user_scope_id>/`, one `.json` per task. The Telegram and Discord
bot logins are in the data keyring, the mail, cloud and GitHub sign-ins in the OS keyring or
the encrypted `*.enc` files in `<data_dir>`. And `<data_dir>/users/<username>/` is a second,
older tree with the same name: the legacy per-name contacts, message and mail databases.

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
- **The folder name is not one per id:** `<session_id>` above is the id without the characters outside `A-Z a-z 0-9 _ -`, cut to 32 (`get_session_workspace_dir`). Two chats of ONE account can share a folder (every thinking run of an account maps to one; a WhatsApp chat keeps only 6 digits of its number once the account name has 16 characters). It does not cross accounts: the folder sits under the session owner's `<uid[:8]>`, and the flat legacy root `VAF_Projects/<folder>` (from before the per-account level) is searched only for a chat without a recorded scope, or for one with a scope when `<folder>` is the id itself, so a lossy id such as `red654321.` never reaches the legacy folder of `red654321`. An id that cannot name a session has no workspace at all. A legacy folder records no owner: an account that starts a chat under exactly the id of a deleted chat whose legacy folder still holds files reaches it (it would have to know that id; a fresh installation has no legacy folders). Named boundary: the derivation has 8 copies and the folders hold the user's own files, so changing it means moving real folders.

The prefix is derived from `session.metadata["user_scope_id"]` at project creation time. Existing projects are never moved; only newly created directories use the prefix.

**Unsafe-directory guard:** `is_unsafe_project_dir()` (`vaf/core/workspace_guard.py`) rejects the user's home directory itself, the standard user directories (Documents, Desktop, ...), `~/.vaf` and the VAF program tree as agent work directories - for the CWD heuristic, explicit `project_path` arguments, paths extracted from task text and `git init`. Unsafe paths fall back to the `VAF_Projects` flow. When a passed or extracted path names a FILE (existing file, or a nonexistent path with a known file extension), the coder first splits it into directory + target-file hint (`_split_explicit_path`) and the guard judges the DIRECTORY part - so "create `~/report.html`" falls back to `VAF_Projects` (home itself is unsafe) while keeping `report.html` as the deliverable name. `git init` additionally refuses any path that is not an existing directory.

**Workspace window endpoints:** `GET /api/session/workspace`, `POST /api/session/workspace/upload`, `POST /api/session/workspace/mkdir` and the workspace file-delete endpoint (`vaf/core/web_server.py`, all via `_resolve_session_workspace`) enforce session ownership with the SAME policy as the WebSocket gate `_ws_session_owner_ok`: the session's `metadata.user_scope_id` must match the requesting user, and a session with NO recorded scope is **admin-only** (legacy/pre-isolation sessions belong to the local admin, they are never open to every authenticated user), otherwise 403. Admin is detected role-aware via `is_admin_identity` (role `admin` OR the local-admin scope). An earlier version treated scopeless sessions as owned by everyone here; that hole is closed, and `/api/image/describe`'s session check follows the same rule. `GET /api/file` additionally refuses downloads from another user's `VAF_Projects/<uid[:8]>/` subtree (any admin exempt; legacy flat projects unaffected); this check is **fail-closed**: if ownership cannot be verified it denies. Containment inside the workspace is decided by the framework primitive `vaf.contained_path` (`vaf/core/path_jail.py`) on RESOLVED paths, so a symlink planted inside a chat's workspace cannot carry a browse, an upload, a delete or a mkdir to its target; an earlier version compared normalized strings, which accepted exactly that. Entry names go through `vaf.safe_entry_name`, so a separator, a relative marker, a hidden name or a control character is a 400 rather than an internal error.

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
  `analyze_image` sits under the same `read` jail (`file_access = "read"`): its
  `image_path` hands a file to the vision model, which is a read by another name, so it may
  name exactly the files `read_file` may and no others. It used to be held to the chat
  workspace alone, which was safe and too narrow: a screenshot in the caller's own tree could
  be read but not looked at. Guarded by `tests/test_analyze_image_path.py`.

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

**And what the USER is offered, not just what the agent may call.** Enforcing at dispatch alone still let a restricted account SEE the tool: the `tools_list` the web server pushes fed the `/` suggestions and the sub-agent hotbar, and it carried the whole registry. The read side of the same answer is `account_allows_tool(name, scope, role)` (facade primitive, `vaf/core/tool_dispatch.py`); all three `tools_list` producers in `vaf/core/web_server.py` filter through it and fail CLOSED per entry, so a resolver that crashed drops an entry rather than widening the menu. The exemptions (scopeless caller, admin) live inside that primitive precisely so a listing surface cannot rebuild them wrongly and strip an admin's own tools. Guard: `tests/test_tool_list_respects_account.py`.

The same record can carry `permissions["confirmation_bypass"]` (default absent =
off): the admin-granted hands-off switch that lets the agent run
confirmation-gated tools for THIS user without asking. It is resolved through the
same registered-resolver lane, fails CLOSED (an unreachable DB grants nothing),
can only skip the human question - `admin_only`, the account allowlist and an
authorizer's `ask()` are decided earlier and are never widened - and every use
is announced as a `gate_bypassed` event.

Pinned semantics: no ACTIVE account with the scope (deleted, deactivated) means NOTHING is
allowed - a missing row used to read as "no restriction", so a deleted account's live turn
had every tool and deactivating did not touch the tools at all. For an active account, no
`"tools"` key or an EMPTY stored list mean UNRESTRICTED - `[]` is the API model's creation
default, and "block every tool" is deliberately not expressible here (deactivate the
account instead). The machine owner is unaffected: the local-admin scope is an admin
identity and never consults the list. Admins are never restricted. A
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

### Taking access away reaches work already running

Deactivating, deleting or narrowing an account used to change the store and nothing else.
Now each of the three takes effect at once (`vaf/core/revocation.py`, called by the admin
routes in `vaf/api/user_routes.py` AFTER the commit, with the permission cache cleared
there too - cleared before the commit, a lookup in between re-cached the old answer):

- **The token stops working.** The HTTP middleware, the WebSocket handshake and the A2A room
  door check that the token's account still exists, is active and still holds the token's
  role (`permissions.account_standing_async`, cached a few seconds and cleared with the
  allowlists), and that the token's own session has not ended (`vaf/auth/sessions.py`; a
  sign-in from another device also ends after `local_network_session_idle_days` without
  use, the window on this machine and tokens for programs do not; a session of the window on
  this machine stands only for this machine and is refused when another device presents it,
  also while the auth store cannot be asked: the token carries that it is the machine's. A
  token minted before that claim existed is judged by its row alone: an old desktop cookie
  presented from another device is still accepted while the database is down, until it
  expires). A
  network client then gets 401 (a socket: close code 4001, and a `ws_rejected` event); the
  real localhost client falls back to the tokenless lane as before. An unreachable auth
  store keeps the token: the same desktop default as the allowlist lookup. A session this
  process ended (a logout, a password change, a reset, a sign-out) stays refused even then:
  the hashes of the sessions it ended are remembered in the process.
- **Sessions end.** Every completed sign-in opens one session row for exactly its token
  (`open_session`, the one writer; each token carries a unique `jti`). A new sign-in in the
  same browser ends the session of the cookie it replaces. A logout ends the
  presented token's session; the account's own password change ends every other session
  and keeps the one that made it; an admin's reset of the password or the second factor, a
  deactivation and the terminal's `vaf users reset-password` end every session of the
  account. A reactivation revives none. After a password change or an admin reset the
  account's open sockets are closed too (`WebInterfaceManager.close_account_connections`),
  so a device signed in with an ended token cannot reconnect; the device that changed the
  password reconnects with its own. The 2FA steps make the same check (they are exempt from
  the middleware), and a token from before the second factor is bound to the password it was
  minted with, so neither an ended token nor one minted before a reset can enroll a new
  second factor. Before, none of these ended a token: it kept working for
  its whole life (measured: 200 after a logout and after a password change). A logout also
  closes the sockets opened with the tokens it ended (each socket remembers the token that
  opened it). The account itself sees its sessions in Settings, Account and signs one device
  out, or every other one (tokens for programs included), and the sockets of what it ended
  close; an admin signs an account out everywhere in the user editor, and a terminal on the
  machine with `vaf users sign-out NAME`. The terminal's `vaf users reset-password` and
  `vaf users sign-out` end the rows in the database and
  then asks the running service (`POST /api/users/<id>/sign-out`, admin only, over the
  loopback lane the other commands use, never through a proxy from the environment) to
  close the account's sockets, since a socket is checked only at its handshake and only the
  service holds it. Only a refused connection counts as "no VAF runs, nothing is open"; when
  the service does not confirm (a timeout, a TLS or proxy failure, any other answer), the
  command says so and tells the owner to restart VAF. NAMED BOUNDARY: the
  session rows are not sealed, so whoever can write the database can reactivate one.
- **Running work stops.** Deactivation and deletion call `revoke_account`; a demotion or a
  narrowed tool, workflow or hands-off grant calls `stop_account_work`. Both stop every chat
  the account has queued or running (generation, queued follow-ups, a waiting confirmation,
  sub-agents), end its background host commands - which the Stop button spares - stop its
  sandbox environments and the processes in them (a revocation listener in
  `vaf/core/environments.py`; stopped, not deleted), and close its sockets, which carry the
  old role. Deactivation and deletion also end the account's interactive browser lease on
  every browser, which closes its stream sockets (a revocation listener in
  `vaf/core/browser_interactive.py`); a demotion or a narrowed allowlist leaves the browser
  alone, because a lease carries neither the role nor the allowlist.
- **Nothing more runs.** While the mark stands, the tool funnel refuses the account's calls
  BEFORE the admin exemption (a turn queued before a demotion still carries the old role),
  a tool call already running ends through the funnel's own stop check, and the runner drops
  the account's queued turns. Queued turns and automations also ask `account_stands`, which
  reads the account directory, so a deactivated account's paired messenger and scheduled
  automations stay silent after a restart as well.
- **Standing grants can be taken back.** "Always" and "for this chat" skip the question
  before any event is emitted, so a grant nobody can see only grows. Each account lists and
  revokes its own under Settings, Connections (`GET /api/security/grants`,
  `POST /api/security/grants/revoke`); an admin does it for another account in the user
  editor (`/api/users/<id>/grants`); `vaf trust --list` and `--revoke-tool` on the command
  line. The overview's list shows the CALLER's grants (it showed the local admin's,
  whoever looked).

NAMED BOUNDARY: the revoked mark lives in the process that revoked. Another VAF process on
the machine (a `vaf run` session) learns of it through its own permission lookup, which
answers "nothing" for a missing or inactive account within the cache lifetime.

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

### A person's credentials for commands are theirs alone

The credentials a person stores for their agent's commands (`vaf/core/user_secrets.py`:
Settings, Connections, "Credentials for commands"; `vaf secrets`; `/api/secrets`) are
addressed by the same shared key builder, namespace `secret`: a scope wins over a name, the
machine owner is the unscoped form. The owner's prefix `secret:` starts every scoped key too,
so a listing takes only the keys with no further segment; a tenant never sees the owner's
names and the owner never sees a tenant's. A direct consumer with no identity (the coder, a
workflow step, an automation) is the machine owner, which is the jail's rule for the same
lanes. `host_bash` (foreground and background) and `python_exec` hand a command exactly the
`VAF_SECRET_<NAME>` variables its text names, never the whole store, and scrub those values
out of what it prints before the model reads it; a background command's owner-only log keeps
the raw output and is scrubbed when read back. No route, command or prompt returns a value.
A value the person hands over in a chat is stored for the person the chat belongs to
(`store_credential` declares `user_scope_id` and `username`), and its removal from the chat
touches only that chat and that person's own sinks: the saved chat of THAT session, that
person's channel store and word-suggestion corpus (`vaf/core/forget_secrets.py`).
Guarded by `tests/test_user_secrets.py`.

### A person's SSH identity and servers are theirs alone

The `ssh` tool (`vaf/core/ssh.py`) keeps, per account, an OpenSSH key, its passphrase and the
list of servers that account confirmed: `~/.vaf/ssh/<account>/` (0700, the key 0600) and
the passphrase in the account's credential store under the namespace `ssh` (never listed as a
command credential, never handed to a command). `<account>` is trust's scope key, ONE
derivation for the folder and the passphrase's address: no scope and the machine owner's
scope are the owner (`default`), any other scope is its own folder. So a server one account
confirmed is new to the next, one account's key never logs in as another, and the machine
owner's `~/.ssh`, config and ssh-agent are used by nobody (`-F none`, `IdentityAgent=none`,
`IdentitiesOnly`); `host_bash` refuses `ssh`, `scp` and `sftp` for the same reason. The login
password is a credential of the person's own store, passed by NAME. NAMED BOUNDARY: an account
that also has `host_bash` can read files on this computer, the key folder included - it is an
account permission outside the file jail. The same holds for the surfaces: `/api/ssh`
(Settings, Connections, SSH) answers for the connection's own account and refuses an account
the tool is not enabled for; `vaf ssh` acts as the machine owner. Guarded by
`tests/test_ssh.py`.

The `ftp` tool keeps its confirmed servers the same way: per account under
`~/.vaf/ftp/<account>/` (owner-only, the same account key as ssh's), so a server one account
confirmed is new to the next, and the FTP password is a credential of the person's own store,
passed by NAME; `host_bash` refuses the FTP clients and `curl`/`wget` with an `ftp://` address
for the same reason ssh's are refused. The surfaces too: `/api/ftp` (Settings, Connections, FTP)
answers for the connection's own account and refuses with 403 an account the tool is not enabled
for; `vaf ftp` acts as the machine owner. Guarded by `tests/test_ftp_core.py` and
`tests/test_ftp_tool.py`.

Cloud DOWNLOADS follow the same rule. Both download actions wrote to
`Platform.downloads_dir()` - process global, so every tenant's download landed in the
owner's home, in one of the four roots the file routes serve. A tenant now receives a
`Downloads` folder inside their own project root; the owner keeps `~/Downloads`.
  All jailed tools enter the jail through the shared `user_jail(...)` context manager, so the
  reset-in-`finally` cannot be forgotten in one of them. Which tools receive the identity at
  all is no longer a hardcoded list of names in the dispatcher: each tool DECLARES it via
  `BaseTool.identity_kwargs`, so a tool registered by an embedder is treated exactly like a
  built-in one. A jail entered inside another keeps the intersection of the two: an inner
  root inside an outer one whole, an outer root inside an inner one as the overlap, nothing
  else (`_intersect_roots` in `vaf/tools/filesystem.py`). That is what lets a room turn
  somebody else woke run every file tool inside the room's shared folder alone
  (`narrowed_jail`), the account's own jail nested within it. Both `BaseTool` and `user_jail`
  are on the public surface (`from vaf import
  BaseTool, user_jail`); see [EMBEDDING.md](../EMBEDDING.md). Guarded by `tests/test_read_jail.py`.
- **Outgoing attachments enter the same jail, on every channel.** All five senders that resolve a local path - `send_mail`, `send_telegram`, `send_discord`, `send_whatsapp` and `send_to_user` - declare `file_access = "write"`, so a non-admin can only attach files from their own tree; before 2026-08-02 only `send_mail` was confined, and the four messengers would attach any path the static checks allow. The mode is `write` although attaching READS: the mode names the root set, and `read` would make skill files shared by other users sendable out. Guarded by `tests/test_messenger_attachment_jail.py` and `tests/test_mail_config_and_jail_guards.py`.
- **The web file routes ask the same jail.** `_allowed_file_path` in `vaf/core/web_server.py` is the one decision for every route that reads or writes a file for the caller: `GET /api/file`, the converters `/api/file/as-html` and `/api/file/docx-model`, `POST /api/image/describe` and the five save routes (`/api/file/save`, `/save-docx`, `/save-docx-native`, `/save-xlsx`, `/save-pptx`). A path must lie under one of four served roots (`Platform.served_file_roots`: Documents, Downloads, the data directory, the output directory), and for an admin (`caller_is_admin`) that is the whole answer. Every other account is additionally held to its own file jail through the framework primitive `vaf.jail_allows` - read mode for the read routes, write mode for the saves - so the web UI hands an account exactly what its agent tools may reach: its own `VAF_Projects/<uid[:8]>/`, the shared folders of its rooms and, for reading, its visible skills. The owner's Documents and Downloads and the data directory, which holds every account's stores and loads `custom_tools/*.py` as code, are out of reach. Before this, the read routes served all four roots to every signed-in account with a hand-copied `VAF_Projects` ownership check on top, and the save routes asked no identity at all. An account whose token carries no scope is refused (the shared identity helper would fill in the local admin's scope). The decision refuses BEFORE it looks at the disk, so 404 against 403 no longer tells an account which files exist. The editor's draft moved with it, from the data directory into the chat's workspace (`drafts/entwurf.md`). The desktop window's native save bridge copies only from the same four roots and refuses when it cannot resolve them. `GET /api/download`, which served files inline and checked no owner, is gone. Guarded by `tests/test_file_routes_account_jail.py`, `tests/test_file_read_routes_ownership.py`, `tests/test_api_file_allowed_roots.py` and `tests/test_jail_allows.py`.

### Automations (`vaf/core/automation.py`)

Each `AutomationManager` instance can be created with a `user_scope_id`; tasks are stored in `automations/` (global) or `automations/<user_scope_id>/` (per-user). Tasks carry `user_scope_id` so that when an automation runs (prompt-based or workflow-based), the agent and workflow engine use that scope: RAG/memory, calendar, messaging, contacts, mail, and automation notes/todos all run with the owner's credentials and data. The agent injects `user_scope_id` into automation tools (`create_automation`, `list_automations`, etc.) so new tasks are stored in the correct user directory. The CLI/scheduler uses an aggregated manager that loads from all scope dirs and saves/deletes/restores via the task's scope path.

**Background-run live-emit isolation.** A scheduled automation runs silently and must not surface in another user's live session. With `VAF_IN_AUTOMATION=1`, `_emit_to_web_ui()` is `False` (no status/context/retry emits). Tool start/end updates are not gated by that env, because a concurrent real user's tool updates must keep flowing. Since a background automation agent has no web session of its own, a naive tool emit would fall back to the process-wide "current session" and could surface in whichever user's web session is currently active. To prevent this, a per-agent flag `agent._background_run = True` (set in `run_task`) is checked at both `emit_tool_update` sites so a background run broadcasts no tool bubbles. The flag is per-instance and therefore race-free; gating on the process-wide env would also suppress a concurrent real user's updates.

**Handoff bundle isolation.** When a background automation must ask the user something it cannot decide, it stores its full working history as a *handoff bundle* under `Platform.vaf_dir() / "handoff_bundles" / <user_scope_id> /<id>.json`, keyed by the scope id (aligned with `thinking_requests`). The linked tracked request and the bundle are written under the same resolved scope (`user_scope_id or local_admin_scope_id`), and both stores resolve the machine owner's aliases themselves (no scope, `"default"` and the configured local-admin scope; `thinking_mode.scope_data_id`, the owner test of the waiting record), so the owner's main agent finds request and bundle whether its turn carries the owner's scope or none, and any other account's scope stays its own directory: only the **same** user's main agent can load the bundle and continue the task; a bundle written for user A is unreadable for user B. See [AUTOMATIONS.md](../platform/AUTOMATIONS.md#silent-background-execution--context-handoff).

**Global slot limit:** A given time slot (same HH:MM + frequency, e.g. daily 08:15) may be used by at most **3 users**. If three users already have an automation at that slot, a fourth gets an error: *"Too many other users have already booked this time slot. Please choose another slot at least 15 minutes apart."* This avoids overloading the scheduler at popular times while keeping automations user-specific.

### Automation planner – notes and todos (`vaf/core/automation_planner.py`)

Notes and to-dos for the automation calendar are stored per user under `Platform.vaf_dir() / "automation_planner" / <user_scope_id> /` (or `_default` when no scope): `notes.json` and `todos.json`. All planner API functions take `user_scope_id`; the Web UI and agent tools use the same scope so that the calendar shows only the current user's data.

### Thinking workspace (`vaf/core/thinking_workspace.py`)

Thinking workspace data is stored per user under `Platform.data_dir() / "workspaces" / <scope_key> /`, where `scope_key` uses the same normalization as Thinking Mode (`local_admin_scope_id` -> `default`, otherwise user scope id). Tasks, run artifacts, handoff proposals, and approval archives are isolated by this key. Workspace path resolution is boundary-checked to prevent cross-scope traversal.

### Sandbox (`vaf/core/environments.py`, `vaf/tools/python_sandbox.py`, `vaf/tools/sandbox_test_runner.py`)

Code runs in the caller's own sandbox environment: `python_sandbox` and `run_tests` in the
person's scratch environment (`vaf-env-<scope hash>-scratch`), the environment tools in the
temporary or project environments the person created. Each is a container of its own,
labelled with its owner, running non-root on a network of its own. Another person's
environment answers like a missing one, and an admin may list and delete it but never run
code in it. A run without a scope belongs to the machine owner; with no owner configured it
is refused. There is no shared "no scope" container.

Before the sandbox environments, every account shared one container (`vaf-sandbox`, root), and the runs were
kept apart only by directory names under `/tmp`: a concurrent run could list and read another
account's working directory and copy files out of it through `export_files`. See
[SANDBOXING.md](SANDBOXING.md#sandbox-environments).

### Browser agent session store (`vaf/tools/browser_agent.py`)

The persistent cookie/login store for the browser agent is keyed by user scope at `~/.vaf/browser_sessions/<scope_seg>/<session>.json` (not the old flat shared path), so one user's saved logins are never readable by another even on the same OS account. The files are encrypted at rest (`vaf/core/data_files`, see [ENCRYPTION_AT_REST.md](ENCRYPTION_AT_REST.md)); the agent lane stages a decrypted owner-only temp beside the store for the duration of a run, because browser_use reads and auto-saves the handed path itself, and folds it back encrypted afterwards. The agent injects the caller's `user_scope_id` for `browser_agent` calls and propagates it to the killable child process via the `VAF_USER_SCOPE_ID` environment variable, so the child writes and reads under the correct per-user store. See [BROWSER_AGENT.md](../agents/BROWSER_AGENT.md).

### Interactive browser (`vaf/core/browser_interactive.py`)

**The default is a partition.** `browser_pool_max` (default `"auto"`, admin-only: as
many as free memory holds at 2 GB each, at least 2 where memory allows, at most 16)
gives each user scope the pool can serve a browser CONTAINER of their own, with its own
profile volume and its own docker network - see the pool bullet at the end of this
section. Everything in between describes the SHARED container, which is what the pool
falls back to: for everyone beyond what the pool can serve, and whenever the pool
cannot serve (free memory below `browser_pool_min_free_mb`, docker unreachable, the
shared container missing as the image template, or `browser_pool_max` set to `0`). On that container the interactive
browser and the agent drive the same Chromium, and the isolation is a lease, not a
partition:

- **One lease at a time**, bound to the `user_scope_id` of the WebSocket CONNECTION
  (never the message). A different user asking while a lease stands gets `busy`,
  deliberately without learning whose lease it is; only an admin may evict. With the
  pool serving, a second user does not reach this arbitration at all: they get their
  own browser instead of `busy`.
- **Handover wipe, verified and fail-closed:** when the browser changes hands to a
  DIFFERENT scope - an interactive lease, an agent run (`stop_for_agent_run` carries
  the run's scope), a `render_check` probe (`hold_for_probe`, the agent run's
  handover with its jar kept), or an owner nobody can vouch for - the whole
  Chromium profile is wiped before the new scope's stored logins are optionally loaded:
  cookies, every site's stored state, history, Chromium-saved passwords, autofill,
  bookmarks, downloads, the HTTP disk cache and the NSS certificate/key database
  (client certificates AND their private keys) die together. The wipe is VERIFIED (the
  supervisor's consumption of the wipe marker is confirmed), and a handover whose
  wipe cannot be confirmed is REFUSED - no lease, no run, a
  `browser_handover_failed` security event - instead of proceeding over the previous
  holder's state (a refused probe opens nothing and answers an error). This is not
  configurable.
- **Whose state the profile holds is one record for every VAF process**
  (`vaf/core/browser_jar.py`). The shared container is driven from several processes:
  the web server (leases, chat runs, the main agent's `render_check`), a coder child
  (its `render_check`, a piped child process in the web UI), a spawned browser agent
  child (workflows) and the terminal app. Each has its own manager, and a manager's
  memory of the owner used to be all a lane checked: measured with two processes, a
  coder child's render wiped the profile under a person's live lease and pointed their
  tab at another user's page, the web process skipped the wipe after a child's render
  so the child's mirrored files sat in the next person's file picker, and a spawned
  run left its logins to the next lease of another scope. Every handover now reads the
  record's owner, a scope HASH bound to the container instance and its last start, and
  wipes for anything the record cannot vouch for: no record, one without its format
  tag, one for another container or an earlier start of this one (a restarted stack
  starts unknown), or a handover cut short - the owner is cleared before the wipe and
  named only after it is confirmed. The container survives restarts of VAF, and the
  record is how a restarted process knows whose state is in it. The record also lists
  who holds the browser now, one entry per lease, run or probe with its process's pid
  and a mark of when that process started (on Linux the boot-relative tick count from
  `/proc/<pid>/stat`: psutil's start time follows the wall clock, and a clock step of
  15 ms once made every live holder of the web server look gone to the processes
  started after it): a live holder of another process makes a probe answer busy, a
  lease or a run wait out a probe there (up to 60 seconds) and refuse anything else,
  and an entry whose process is gone, or whose pid now belongs to another process, is
  dropped, so a crash never locks the browser. A run stopped while it waits releases
  its claim when the claim lands. It lives on the host and not in the container,
  because only the host can tell whether a process is alive, and because a marker
  inside the container would be written by the browser user, the account a
  compromised page runs as. Hashes, pids and timestamps only, owner-only, written
  under the same cross-process lock and through the same atomic writer secure_store
  uses. A record file that exists but cannot be used (unreadable, not JSON, without
  the format tag or with another one) is never read as "nobody holds the browser"
  and never overwritten: the shared browser answers busy and refuses a change of
  hands until the file, which the `webui` log names, is removed with VAF stopped.
- **The same scope, and serialised handovers.** A non-persistent agent run of the SAME
  scope still gets a quick scrub (its documented promise is a clean start;
  `VAF_BROWSER_SCRUB=full` deepens that to the verified profile wipe, which here does not
  refuse the run when it cannot be confirmed). Saved logins
  live in the same per-scope storage-state files as the agent's persistent sessions,
  above. Within a process, concurrent handovers are serialized by a dedicated mutex,
  so two starts can never interleave their wipe-and-restore sequences on the shared
  jar; across processes the holder record above does the same. A probe holds the
  mutex and its record entry until it has rendered and parked the page, and takes
  both without waiting (a held mutex, a lease, a run - queued at the browser's
  concurrency gate or driving, each one counted until it ends - a spawned agent run,
  or any holder in another process answers busy): a lease or a run that started
  between its wipe and its navigation would otherwise get its jar restored under the
  page the probe reads. The same count refuses an interactive start while any run of
  that browser is queued or driving - checked when the start begins and again in the
  lock step that installs its lease, so a start still in its handover when a run
  began is refused as well; it used to be one flag, and the end of the first of two
  queued runs cleared it, so the second drove while another person's start and a
  probe were let in. A run on a pooled browser registers that browser's manager
  before its first hook, so it is counted on the browser it drives, never on the
  shared one.
- **Stream access is a per-lease ticket in the URL path** (`/api/browser-vnc/t/<ticket>/`),
  validated on every asset request, at the stream websocket's connect, and on every message
  the stream relays in either direction. When the lease or the run's watch grant ends (a
  stop, a takeover, a newer window, an admin, the run's end, the account's revocation), its
  open sockets are closed with 4403 at once, and a quiet one within half a second; a revoked
  account's watch grant ends with its run, which the revocation stops within seconds, and the
  account gets no new lease, not even from a start that was already under way
  (`STREAM_RECHECK_S`); no frame or keystroke crosses after the end. Measured before the
  check: an open socket kept streaming after every one of those ends, each frame showed the
  next holder's browser, and its input reached that browser. The grant is this process's,
  and it is dropped before its holder entry in the cross-process record is released, so
  another process can only become the next holder, never take the browser under a grant
  still live here. The run's watch grant is minted only after the run's handover: before
  it, the browser still shows the previous holder, or a page check of another process the
  handover waits out, and a watch socket granted that early received that page check's
  frames. The path is auth-middleware-exempt for the same reason the A2A room seat
  lane is: the credential is in the request itself.
- **The old profile residual is closed; what remains is named.** History, Chromium-saved
  passwords, autofill, bookmarks and the HTTP disk cache used to survive a quick-mode
  handover on the shared container; the mandatory verified wipe above deletes them with
  the profile on every change of hands, on every lane that drives that browser (lease,
  agent run, `render_check` probe), so the next holder inherits nothing. The
  password manager itself is pinned ON by managed policy - deliberate: agent runs and
  interactive re-login depend on it, and cross-user isolation is carried by the
  mandatory scope-change wipe (shared container) and per-user profile volumes (pool),
  not by disabling the feature. A policy rather than a profile preference, because the
  wipe now deletes preferences at every change of hands, and because a stray toggle in
  the browser's own settings had silenced it once already. Note the at-rest weakness
  underneath: with no keyring in the container Chromium uses its `basic` password
  store, so saved passwords belong on a dedicated per-user instance, never on the
  shared fallback. Its saved passwords therefore live only until the next
  cross-user handover on the shared container, and indefinitely on a dedicated pool
  instance. What remains on the shared container is named in `hand_jar_to_run`:
  within one process `VAF_BROWSER_MAX_PARALLEL` above 1 lets that many runs share one
  jar and one holder entry (the first run to end releases it, so other processes see
  the browser free while the second run still drives it), and a second VAF
  installation under another OS account that drives the same container keeps a
  record of its own - the pool is the answer to both.
  Downloads are not a residual: finished ones are swept to their owner's
  `VAF_Projects/<uid8>/Downloads`
  through the threat funnel (origin `browser_download`), and a scope change purges the
  container folder unread instead of delivering a previous holder's files onward. The
  workspace MIRROR (`/home/browser/Workspace`, the upload lane) is treated the same
  way: it is the holder's own files by construction, it is purged unread on a scope
  change so the next person can neither read nor upload it, and the profile wipe wipes
  it with the profile. A `render_check` of a project file mirrors into it too; the
  probe takes the jar first and mirrors after its handover, so its files are the
  probe's scope's in the record every process reads, and leave with the next change
  of hands, in whichever process that happens. Before, the probe left the jar on the
  previous owner (in its own process, and in every other process always), whose next
  lease saw no change of hands and purged nothing, so the probe's files sat in that
  person's file picker.
- **The genuine partition is the per-user pool** (`browser_pool_max`, default
  `"auto"`, at least 2 where memory allows, admin-only, overridable by
  `VAF_BROWSER_POOL_MAX`; see the pool
  section in [BROWSER_AGENT.md](../agents/BROWSER_AGENT.md)): each user scope gets a
  browser container with its own profile volume AND its own docker network, so nothing
  is shared with anyone and users browse in PARALLEL; hand-over scrubbing then only
  guards the shared fallback container, and a dedicated
  instance is never scrubbed or profile-wiped. An instance keeps the settings of its
  `docker run` for life, so on adoption its proxy, timezone and Safe Browsing key are
  compared with the current ones and a drifted instance is recreated on its own volume
  (a per-user browser created before a proxy was set kept browsing direct). More free memory under `"auto"`, or a
  higher fixed `browser_pool_max`, raises the number of people who are partitioned rather than scrubbed, which is why the residual
  above is a property of the fallback and not of the product. With the pool active,
  every fallback onto the shared container is recorded as a `browser_pool_fallback`
  security event, and `browser_pool_strict` (admin-only) turns the fallback into a
  refusal: no dedicated instance means busy, on every lane - the setup for
  deployments where two users' sessions must never meet. The per-instance network is load-bearing
  rather than tidiness: inside the container CDP listens on 0.0.0.0 with NO
  authentication (safe only because the host publishes it on loopback), so on one
  shared bridge a page in user A's browser could dial user B's container directly.
  The KasmVNC stream on the same container now requires a credential VAF holds in
  its keyring, which closed a real gap: it accepted any Origin, so a page in the
  user's ordinary browser could read the framebuffer AND inject key and pointer
  events (measured, and measured again as refused afterwards). CDP has no such
  page vector - it rejects any Origin - so for THAT half the network isolation is
  the whole defence. Container, volume and network names carry a scope hash, never the scope.
- **Container hardening, both lanes:** the compose service and every pool instance
  start with `cap_drop ALL` + `cap_add SYS_CHROOT`, `no-new-privileges:true`, and the
  seccomp profile `docker/browser/chromium-seccomp.json` (Docker's default plus the
  user-namespace syscalls), which is what lets Chromium run WITH its own sandbox
  inside the container instead of `--no-sandbox`. A renderer exploit from a malicious
  page therefore meets Chromium's namespace sandbox first and a capability-stripped
  container second. Pinned by `tests/test_browser_entrypoint_supervise.py` and
  `tests/test_browser_pool.py`; details in
  [BROWSER_AGENT.md](../agents/BROWSER_AGENT.md).

## 6. Connection-Level Isolation

### WhatsApp

Each user runs a **separate Node.js subprocess** with its own authentication directory (`~/.vaf/users/<username>/whatsapp/`). The linked account is that user's agent number; there is no fallback to another user's credentials (two Baileys sockets on one credential set evict each other). Sessions are completely isolated at the process level, and the sender loop stores outbound messages in the same per-scope message store as the inbound lane, so the read tools and the window's own state see one conversation per scope.

### Telegram

Uses a **whitelist-based routing model**. The bot is shared, but messages are routed to the correct user based on the Telegram chat ID whitelist stored per user.

Setting the bot up is an admin's: `start-verification`, `verification-status` and `whitelist-add` answer only an admin (`require_admin`). They used to answer any signed-in account, and `whitelist-add` took the Telegram id from the request body and replaced an entry with that id whoever it belonged to: measured, an account that sent the admin's id became the owner of the admin's Telegram chat, so its agent answered the admin and its window read the conversation. The verification state is one per process, so it is an admin's too. A relay contact belongs to the account that added it: `relay-whitelist-add` refuses (409) an id that is another account's relay contact, where it used to move it silently; an admin may still move one. Guarded by `tests/test_shared_bot_admin_rights.py`.

Every account can pair its own Telegram without the wizard: `POST /api/telegram/pair` hands the signed-in account a one-time code (72 random bits, ten minutes, one per account, spent by its first use; `vaf/core/channel_pairing.py`), the account sends it to the RUNNING bot as `/start <code>` (a t.me link fills it in), and the bot links the Telegram account that sent it to the account that asked. The code is shown only to that account and only the Telegram account that sends it can be linked, so the pairing proves both ends; the bot answers only a live code, so a stranger learns nothing from trying, and a code never moves a Telegram account that another account has paired (only an admin can, through the wizard). An account signed in without a scope gets no code (400) instead of being read as the local admin. `pair_telegram_account` is the one writer of an owner entry, for the wizard and for a code.

Each account's own Telegram switch is enforced, by one rule (`messaging_connections.channel_enabled_for_scope`, which reads `channels.Channel.accounts`): nothing is on while the bot is off; the local admin and every admin account ride the bot's switch (an admin's save writes the bot's switch, never one of their own, so a rule by scope alone would have cut a second admin off; the account directory's `role` says who is an admin, `config.is_admin_account`); every other account needs its own switch on. The bridge answers a paired account, its relay contacts and its contacts only while that account's lane is on, and `get_telegram_chat_id` delivers to it only then (`telegram_chat_id_of` still finds the chat for reading). It used to be the switch shown and stored while the bot answered everybody. A pairing by code turns the account's switch on.

Where VAF reaches an account on Telegram (`messaging_endpoints.json`, `by_scope` and `by_username`) is only ever one of that account's own paired Telegram ids (`messaging_connections.telegram_paired_ids`: its whitelist entries, by scope alone, a missing scope on the entry or the lane read as the local admin's, as the bot's lane switch reads it (`is_local_admin_lane`); a name never decides, so neither a lane that falls back to another name nor the Discord bridge's "admin" lane without a scope is handed an account's pairing by its name; never a relay entry, never a contact, and for the local admin never another account's pairing). Both halves of the store hold it. `save_telegram_chat_id` writes nothing else, and it writes only under the name the account is paired under, never the name a caller passed, so a background lane of one account that falls back to the local admin's name cannot file its chat under the admin's key. `telegram_chat_id_of` returns nothing else, and only reads or clears name keys that are the account's own: a name that another account's entry is filed under is not (an entry without a scope is the local admin's), and for every account but the local admin neither is the local admin's name or "admin", which stay the admin's keys even after the admin's own pairing was removed, unless the account's own pairing is filed under that name (an ordinary account named "admin" keeps its own key, and the local admin's lane leaves it alone). Before this, the bridge stored the chat of every sender it admitted under the OWNER's scope and name, because an allowed contact, a relay contact and a stranger the open Front Office answers all resolve to an entry carrying them: measured through the real bridge, after one Telegram message from an allowed contact the owner's stored endpoint was the contact's chat id, and every owner delivery lane (reminders, automations, background questions, `send_to_user`, the Front Office back-channel `send_telegram`) went to the contact and was mirrored into the contact's own chat session. A stored chat that is not a paired id of the account is removed from the account's keys on the first read after the update, before anything is sent, and recorded once as the security event `owner_endpoint_reset` (`detail` names the contact when the account's book has one). The inbox and the Telegram window decide "owner" from the whitelist itself (`owner_endpoints`), not from this store, which is why nothing on screen showed it. Guarded by `tests/test_telegram_owner_endpoint.py`. WhatsApp's writer stores only the owner's registered number; its reader makes no such repair, a named boundary in the docstrings of `save_whatsapp_chat_jid` and `get_whatsapp_chat_jid`.

### Discord

Currently **single-admin only** - one Discord bot per VAF instance. Not multi-tenant. The lane is the local admin's (`messaging_connections.is_local_admin_lane`: the local admin's scope, or none, which is what the Discord bridge's own turns carry), and every Discord lane asks `get_discord_user_id`, which answers nobody else: `send_to_user` with Discord as main messenger, `send_discord`, the availability list, and `read_discord_chat`, which also refuses an explicit `chat_id` from another account. It used to return the admin's Discord id for any identity: measured, another account's agent messaged the admin's Discord and read the admin's Discord conversation. The Connections page shows the Discord card to admins only.

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

### MCP servers with a sign-in per account

A remote MCP server with `"auth": "oauth"` (`vaf/core/mcp_oauth.py`) is set up once by an admin,
and every account signs in to it on its own (Connections, "MCP services"; the routes under
`/api/mcp/sign-in` act on the caller's own account only). Each account's tokens are a record of
their own in the key ring (`mcp_server.<name>.oauth.<scope>`), and each account has its own
session at the server, keyed by the account in the session pool. The server's tools declare
`identity_kwargs = ("user_scope_id",)`, so the dispatcher assigns the caller's scope, a
model-supplied value is overwritten, and a call runs in that account's session or not at all: an
account that has not signed in is refused before any request, never served by another account's
session. The callback that finishes a sign-in must be reached by the account that started it
(`enforce_callback_actor_binding`, as for the mail sign-in), and a sign-in's `state` is used once.
The editor's test of such a server runs as the tester's own account. The tool LIST is read with one
signed-in account (the local admin's when it has one): it is the server's, the same for everybody.
A missing scope is the local admin's here too (`is_local_admin_lane`).

The raw `mcp_call` carries no caller identity, so it refuses such a server and names its own
tools; it could otherwise only run as the local admin's session. It also starts only a server an
administrator registered: the command that starts a local server is the admin's decision, never
an account's or a model's, so an unregistered command is refused for every account (it used to
run on the host for any account that had the tool). The account's tool allowlist is asked for
the `mcp_<server>_<tool>` a call reaches, not only for `mcp_call`, so a list that leaves a
server's tool out cannot reach it through the raw tool (`tool_dispatch.caller_allows_tool`; in
the coder's child the list that crossed as `VAF_ALLOWED_TOOLS`). See
[MCP_INTEGRATION.md](../agents/MCP_INTEGRATION.md).

### Config: global vs user-scoped

- **Global (admin-only to change):** Backend and network settings apply to all users. Only admins can edit them. This includes: Network tab (local network, ports, TLS, hosting), Advanced tab (server, tray, timeouts, etc.), API keys and provider/model settings, OAuth client IDs, TTS/STT engines, URLs and enable toggles (`stt_enabled` included), the voice provider selection (`speech_tts_provider`, `speech_stt_provider` and their model/voice keys, plus `api_key_elevenlabs`), and similar server-wide options. The auto-speak preference `tts_auto_speak` is deliberately user-writable (playback preference; billing exposure is bounded by the admin-gated enable and provider keys). Stored in the single `config.json`; non-admin PATCH and WebSocket `save_config` are filtered so these keys are not overwritten. To reduce accidental data loss, config merge also preserves existing sensitive values when an incoming update contains empty API key strings or `null` connection configs.
- **User-specific:** Connections (Mail, WhatsApp, Telegram, Discord, Cloud, Calendar, GitHub), language/interface preferences, and automations are per user. Non-admins can change only the keys that are not in the global set (e.g. language, time format). Connection data is already keyed by `user_scope_id` or username where applicable. The three messenger blocks (`telegram_config`, `whatsapp_config`, `discord_config`) are one per installation, so a non-admin's save never carries one: `Config.extract_connection_toggles_for_scope` removes them from every non-admin body, whether or not they carry a toggle, and keeps only the person's own on/off in `connection_enabled_by_scope`. It used to remove them only when an `enabled` toggle came along, so a block without one passed through and replaced the global config - measured, a non-admin could make themselves the Telegram bot's owner through the whitelist and replace its bot token. The bot token has a second lock: `api_keys.absorb_config_keys(..., is_admin=...)` stores a channel credential only for an admin. Starting and stopping the bot is the same decision, because each is one bot for the whole installation: `POST /api/telegram/start`, `/api/telegram/stop`, `/api/discord/start` and `/api/discord/stop` answer only an admin (`require_admin`). They used to answer any signed-in user, and the Connections switch called them, so a non-admin turning their own Telegram off stopped the bot for everyone. A non-admin's switch, and the Disconnect button for them, now changes only their own entry in `connection_enabled_by_scope`. Who counts as an admin is ONE rule, `config.is_admin_identity` (the admin role, or the local admin's scope), which every route asks through `user_routes.caller_is_admin` and `require_admin`, and a lane that has only a scope asks through `config.is_admin_account`: the messenger routes used to carry three checks of their own and disagreed about a second admin account (an admin for Discord and the config save, an ordinary user for Telegram and WhatsApp, and `require_admin` knew only the role). `tests/test_policy_admin_matches_the_shared_rule.py` fails on any function named like an admin check that does not ask the shared rule. The inline copies are gone too (26 in the WebSocket handlers, one in the brain routes); `tests/test_policy_admin_matches_the_shared_rule.py` fails on any comparison of a role to `"admin"` that is not the shared rule or a listed exception with its reason (queries over account rows, the target account's role in user management, and the localhost 2FA exemption, which decides on the stored role alone). Whose switch `<channel>_config.enabled` is follows the same registry field: on WhatsApp, where every account has its own connection, it is the local admin's own, so both save paths route a second admin's WhatsApp switch into their own `connection_enabled_by_scope` entry (`Config.split_connection_toggles`, the one step both paths run) and `config_for_user` shows it back to them; it used to replace the global one, which turned the local admin's number off.

The Settings UI shows the **General**, **AI & Model**, **Advanced**, and **Local Network** tabs only to admins (controlled by the `adminOnly` flag in the `CATEGORIES` array and per-tab content rendering guards). Non-admin users are automatically redirected away from admin-only tabs. All users see Persona, Voice, Interface, Connections, Automations, and About, and receive the global config they need for display/behavior, but it is **credential-redacted**: for non-admins the backend strips secret values (`api_key_*`, `*_client_secret`, `*_secret`, `*_credentials_key`, `*_encryption_key`, `*_kek`, `*_password`, plus `secure_store_kek`, `memory_db_url`, `redis_url`) from every config read (`GET /api/config` and the WebSocket config push) via `Config.config_for_user()` / `Config.is_secret_config_key()`. This read-redaction is intentionally narrower than the admin-only *write* denylist (`is_secret_config_key` vs `is_global_config_key`): non-secret admin-only keys the UI needs (model/provider names, non-secret network settings) stay readable, only credentials are removed. Admins receive the full config.

## 7. Watchdog and Admin Security APIs

### Sub-agent watchdog (`vaf/api/supervisor_routes.py`)

`GET /api/supervisor/status` and `POST /api/supervisor/cancel` are caller-scoped. Unit payloads carry user-authored task text, and a task id is all `/cancel` needs, so before this gate any authenticated user could list every user's running sub-agents, read their task text, and kill them. Now a non-admin sees, and can cancel, only units belonging to sessions owned by their own scope; a foreign or unowned `?session` yields an empty list (never a 403, because the web tool bubble polls generically), and `/cancel` on a foreign unit returns "not permitted". Sessions without a recorded scope count as admin-only, matching the WebSocket ownership policy, and the ownership lookup is fail-closed: if it fails, a non-admin sees nothing. Admins and the tokenless localhost desktop (which resolves to the local admin) keep the full watchdog view with per-unit username attribution. Details in [TOOL_SUPERVISION.md](../agents/TOOL_SUPERVISION.md); guarded by `tests/test_supervisor_scoping.py`.

### Security dashboard (`vaf/api/security_routes.py`)

The `/api/security/*` surface (overview, events, skill actions) is admin-only (`Depends(require_admin)`) by design: it deliberately aggregates cross-user data for the admin's Logs Overview dashboard, including per-user workspace sizes and per-scope memory/chunk counts, with usernames attached server-side. Full scope UUIDs never leave the backend (the overview shortens them to the first 8 characters for display). This aggregate lane, including the owner-connection read path described in Section 4, must never be reachable from a per-user route.

### Network routes (`vaf/api/network_routes.py`)

The connection map (`/api/network/connections`) lists every connected device's address and user name, so it is cross-user data like the security overview and answers an admin only (`Depends(require_admin)`), as do `/status`, `/access-url` and `/remote-access`. They were open to any signed-in account while the settings tab that reads them was admin-only. `/ws-config` stays open because the login page needs it before anybody is signed in. Pinned by `tests/test_remote_access_routes.py`.

## Isolation Summary Table

| Component | Isolation mechanism | Level |
|-----------|---------------------|-------|
| Memory CRUD | `user_scope_id` filter on every query | Application |
| Memory graph | Scope filter on auto-connect and manual operations | Application |
| Gateway | Server-side scope extraction, client scope stripped | Transport |
| Config read (`GET /api/config`) | Secret keys (API keys, OAuth client secrets, JWT/encryption keys, DB URLs) redacted for non-admins; admins get full config | Application |
| Redis cache | Scope-prefixed cache keys | Caching |
| Tool confirmation grants | Per-scope trust store `<config_dir>/trust/<scope>.json` (`~/.config/vaf` on Linux, local admin -> `default.json`); one tenant's "always" never answers for another. A "for this chat" answer is kept in memory keyed on the person AND the chat (`vaf/core/trust.py`), so it never reaches another chat or another account; "once" remembers nothing (it used to arm the tool for every chat and account of the process). `tool_confirmation_bypass_admins` (admin-only, default off) lets an admin skip the dialog and emits `gate_bypassed` each time - it cannot widen `admin_only` or the account allowlist | Application |
| PostgreSQL | Fail-closed RLS policy (ENABLED + FORCED); active enforcement pending the app-role cutover to `vaf_app` (owner role bypasses RLS today) | Database |
| Filesystem | Scope-based paths (`<data_dir>/scopes/<user_scope_id>/`) preferred; legacy `<data_dir>/users/<username>/` as fallback | OS |
| Generated projects (VAF_Projects) | `~/Documents/VAF_Projects/<uid[:8]>/<session_id>/` when session context is present; legacy flat root otherwise | OS |
| Session workspace | `Session.project_path` anchored to first `VAF_Projects` creation; `[SESSION WORKSPACE]` injected per turn | Application |
| Central Data Explorer (`/api/workspaces`) | Per-user root derived from authenticated scope; lists/searches/renames/deletes only the caller's own workspaces (incl. orphans); opaque handles, not paths; search takes only a query string, never a path | Application |
| File tools (read AND write) | Per-user jail (contextvar over `is_safe_path`, entered via `user_jail`): non-admin confined to own `VAF_Projects/<uid[:8]>/` - plus, for READS only, the folders of skills visible to them; admin (`is_admin_identity`) full; another user's tree always denied, fail-closed | OS |
| Web file routes (`/api/file`, converters, `/api/image/describe`, the five save routes) | One decision (`_allowed_file_path`): the four served roots for an admin; every other account only where its own file tools reach (`vaf.jail_allows`, read or write mode); refused before the disk is looked at, so a refusal says nothing about existence; a token without a scope is refused | Application |
| Outgoing attachments (mail + messengers) | All five senders declare `file_access = "write"`: a non-admin attaches only from their own tree; symlink targets are re-checked by `is_safe_path` itself | OS |
| Sandbox | Every environment its own container, volume and network, labelled with its owner; a person has up to `sandbox_env_max_per_user` plus one scratch environment, never a container shared with someone else | Container |
| Sub-agent watchdog (`/api/supervisor/status`, `/cancel`) | Non-admins see and can cancel only units of sessions owned by their scope; unscoped sessions admin-only; fail-closed ownership lookup; admins get all units with username attribution | Application |
| Security dashboard (`/api/security/*`) | Admin-only by design (`require_admin`); aggregates cross-scope metrics server-side; full scope UUIDs never leave the backend | Application |
| Browser sessions (cookies/logins) | Per-user `~/.vaf/browser_sessions/<scope>/` store keyed by user_scope_id, encrypted at rest | OS |
| Live browser (interactive + agent) | Per-scope container, profile volume and docker network for every scope the pool can serve (`browser_pool_max`, default `"auto"`: at least 2 where memory allows, at most 16, admin-only); beyond that a shared container with one lease at a time and a verified, fail-closed profile wipe on every handover to another scope | Container / Application |
| WhatsApp | Separate subprocess per user | Process |
| Telegram | Whitelist-based routing | Application |
| Email | Per-user encrypted credentials + scope-based config lookup chain | Application |
| Calendar (Google/Microsoft) | Same OAuth and `user_scope_id` as Email; no separate credentials | Application |
| Automations | Per-user task storage and scoped RAG access; max 3 users per time slot (global cap) | Application |
| Local model server | One server per installation; its prompt/prefix cache is shared across accounts (timing only, no content; named boundary, section 3) | Application |
| Automation planner (notes/todos) | Per-user `automation_planner/<scope>/notes.json`, `todos.json` | Application |
| Thinking workspace | Per-user `workspaces/<scope_key>/` with boundary-checked file access and handoff approvals | Application |
| Word suggestions in the chat input | One corpus per account (`vaf/cli/autosuggest.py`, `autosuggest_for`): the machine owner keeps `~/.vaf/autosuggest.json`, the file the terminal lanes use, every other account `~/.vaf/autosuggest/<scope>.json`; a web connection learns into and is suggested from its own account's corpus only, and a connection with no account gets neither. Before, one process-wide corpus learned every account's messages and answered every connection, so one person's typed words (a name, a password) could be offered to another. Encrypted at rest, owner-only | Application |
| Config (global vs user) | Backend/network/API keys: admin-only write; non-admins can change only user-scoped settings | Application |
| Turn binding | Every runner lane binds identity, conversation, source and markers through `bind_turn` (`vaf/core/turn_binding.py`) before its turn and clears the markers with `end_turn` after it; per-chat state is swapped by the loader; the ask-first latch is kept per account; the room reply streak is not yet (named boundary in `docs/CORE_AGENT.md`) | Application |
| Agent rooms (A2A) | One account per room by default (`_check_tenant`); a room appears in the sidebar only for accounts that are members (`joined_rooms`); each room turn runs bound to the account whose room it is, in a conversation of its own (its working memory kept per room and account), never on the chat the worker served last, and with a prompt that leaves out the account's profile digest and details, its last message elsewhere and the machine's paths, because every member of the room reads what the turn writes (`Agent._answers_somebody_else`); for a message from anybody but its user, the turn reads only the room, the web and the room's shared folder and changes only its posts in that room and files in that folder, in every mode, and its room tools reach only that room (`Agent._room_mode_gate_decision`, `Agent._room_file_scope`; an automation a room message starts is bound the same way); an account that no longer stands gets no room turn; its live events route to that account rather than to the room's owner | Application |
| Rooms across accounts (`multi_scope`) | Opt-in per room (`--shared` at creation, or `Room.open_to_accounts` by the host later), and it takes only the accounts it ADMITTED - directly (`Room.admit`, host or leader only, logged as `room_account_admitted`) or by INVITATION the account accepts itself (`Room.invite_account`, same rule, logged as `room_account_invited`; the invitee sees the room's name, kind, member labels and who asked, and not one line of it until it accepts, when `room_account_admitted` is logged). An account invitation is refused on the wire door without being consumed, so its id is not a credential. Knowing the id admits nobody. Inside such a room isolation is deliberately relaxed and the relaxation is the point: every member reads every frame, and members reach the room's shared folder through one clause inside the file jail's cross-account invariant. A newcomer starts reading at its own join, so it never receives the history of people it has never met | Application |

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
| Sandbox | One container per environment, never shared between people; no disk quota per environment (overlay2 without xfs project quotas) | The number of environments per person is limited (`sandbox_env_max_per_user`) |
| Rate limiting | No per-user rate limits | Add per-user rate limiting to prevent abuse |
| Audit logging | Perimeter/auth trail exists: the always-on security event log (`vaf/core/security_events.py`) records blocked IPs, invalid tokens, failed logins/2FA, rejected WebSocket handshakes, messenger pairing changes, and skill blocks/quarantines; admin-readable via `GET /api/security/events`, the `security_<date>.log` file, and the Logs Overview dashboard. The activity timeline (`timeline_<date>.jsonl`) is attributed per user: `vaf/core/identity_binding.py` binds the scope for the turn and `log_timeline_event` stamps it, so an admin can read one user's tool calls via `GET /api/logs/timeline/events?user=<username>` (the name is resolved to a scope server-side; scope ids are never handed to a client). Records written before that stamp existed, and work that belongs to no user, carry no scope and are reported as unattributed rather than folded into somebody's list | Log application-level cross-scope DATA access denials (e.g. a scoped memory query returning "not found" for a foreign id, or a WS session-ownership denial), which are still unrecorded |
| Memory encryption keys | Shared key across users | Consider per-user encryption keys for stronger data separation |
| WebSocket connections | Shared event loop | Monitor for resource exhaustion by single user |

## Related Documentation

- [USER_IDENTITY.md](../memory/USER_IDENTITY.md) - User profile and preferences system
- [MEMORY_SYSTEM.md](../memory/MEMORY_SYSTEM.md) - Memory storage and RAG pipeline
- [CONNECTIONS.md](../integrations/CONNECTIONS.md) - External service connections (WhatsApp, Telegram, etc.)
- [SANDBOXING.md](SANDBOXING.md) - Docker sandbox for code execution
