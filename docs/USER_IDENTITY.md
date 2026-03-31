# User Identity

This document describes how VAF stores and uses the **current human user's** profile (name, language, preferences, do's and don'ts). This is separate from the agent's own persona (Soul and `identity.json`).

## Purpose

The model needs to know who it is talking to so it can:
- Greet the user by name (e.g. "Hey Mert").
- Prefer the user's language.
- Use the user's location (city/country) for context-aware answers (e.g. weather, local time).
- Follow stated preferences and rules (e.g. "always be concise", "don't use emojis").

That data is stored per user and injected into the system prompt each turn. Because the agent updates it from conversation (via the `update_user_identity` tool), the user profile is part of VAF’s **self-learning** system: the model learns the user better over time. See [SELF_LEARNING.md](SELF_LEARNING.md) for an overview of all self-learning mechanisms.

## Storage

- **File**: `~/.vaf/users/<username>/user_identity.json`
- The path is determined by the **username** of the current session (from JWT when authenticated, or in local mode from `local_admin_username` in config).
- **Contents**: `name`, `preferred_language`, `city`, `country` (location), `preferences` (list of strings), `dos` (list), `donts` (list), `main_messenger` (optional: `"telegram"` | `"discord"` | `"slack"` | `"whatsapp"`), `timezone` (optional IANA e.g. `Europe/Berlin`), `date_format` (optional e.g. `dd.mm.yyyy`), `time_format` (optional `24h` | `12h`), `change_log` (list of `{ "at": "<ISO8601>", "action": "<summary>" }`).
- **Created**: When the workspace is first used; default `name` is the username, other fields empty.

Do not confuse with `identity.json`: that file holds the **agent's** display name, emoji, and theme (persona data). User identity is only about the human user.

## System prompt injection

In `vaf/core/system_prompt.py`, `build_prompt()` adds a user-identity block when a username (or user_scope_id) is available. It reads `user_identity.json` via `UserWorkspace.get_user_identity()` and appends:

- Name, preferred language, and location (city, country) when set.
- Preferences, Do, and Don't lists.
- Optional `main_messenger` (preferred channel for proactive messages: telegram, discord, or slack).
- Optional `timezone`, `date_format`, and `time_format` (used for the **"## Current Time"** block and so the model can show dates/times in the user's preferred format).

The **"## Current Time"** sentence in the system prompt uses the user's `timezone` (if set) and `date_format`/`time_format` so the model sees the correct local time and format.

When the user has at least one messaging connection (Telegram, Discord, WhatsApp), a **"## Messaging connections (proactive messages)"** subsection is added: it lists available channels and the preferred channel (if set), and instructs the agent to ask once if the user wants to receive something but has not set `main_messenger`, then to use `update_user_identity(main_messenger=...)` and the matching channel tool (`send_telegram`, `send_discord`, `send_slack`, or `send_whatsapp`). Only tools for configured connections are exposed to the agent. See [CONNECTIONS.md](CONNECTIONS.md) for proactive messaging.

That block is rebuilt every turn (dynamic system prompt), so the model always sees the latest user identity.

## Tool: update_user_identity

- **Module**: `vaf/tools/user_identity.py`
- **Name**: `update_user_identity`
- **When to use**: When the user says their name, language, location, or rules (e.g. "call me Mert", "I prefer German", "I'm in Berlin" / "I'm based in Munich, Germany", "always be concise", "don't use emojis"). Also when the user says which channel to use for proactive messages (e.g. "send it via Telegram" → `main_messenger="telegram"`).

Parameters (all optional): `name`, `language`, `city`, `country`, `main_messenger` (`"telegram"` | `"discord"` | `"slack"` | `"whatsapp"`), `timezone` (IANA e.g. `Europe/Berlin`), `date_format` (e.g. `dd.mm.yyyy`), `time_format` (`24h` | `12h`), `add_preference` / `remove_preference`, `add_do` / `remove_do`, `add_dont` / `remove_dont`. At least one must be provided.

On each successful run, the tool appends one entry to `change_log` with the current time (same source as the system prompt clock) and a short action summary (e.g. "name", "language", "preference"). The log is trimmed to the last 50 entries.

The agent injects the current username into the tool call so the correct `user_identity.json` is updated.

## Proactive messaging (send_telegram, send_discord, send_whatsapp)

When the user asks the agent to send them something (e.g. a summary or a result "via Telegram"), the agent uses:

1. **Messaging connections** (in the system prompt): which channels are available (Telegram, Discord, WhatsApp) and whether `main_messenger` is set. Only tools for configured connections are exposed (e.g. `send_telegram` only if the user has Telegram).
2. If preferred channel is not set, the agent asks once (e.g. "Should I send it via Discord, Telegram, or WhatsApp?") and stores the answer with `update_user_identity(main_messenger="telegram")` (or discord/slack/whatsapp).
3. The agent uses the matching tool: **`send_telegram`** (`vaf/tools/send_telegram.py`), **`send_discord`** (`vaf/tools/send_discord.py`), **`send_slack`** (`vaf/tools/send_slack.py`), or **`send_whatsapp`** (`vaf/tools/send_whatsapp.py`) depending on `main_messenger` or the user's request. For Telegram, the user must have sent at least one message from Telegram first so VAF can associate their chat ID. For WhatsApp, the whitelist phone number is used. Chat IDs / endpoints are stored in `messaging_endpoints.json` under the platform data directory.
4. **Voice messages:** Both `send_telegram` and `send_whatsapp` support optional `voice_lang` (e.g. `"de"`, `"en"`) to send as a voice message instead of text when the user requests it.

## API

- **GET /api/user/persona**  
  Returns `identity` (agent), `user_identity` (human user profile), and `soul`. The Settings UI uses this to show the User Identity modal.

## Settings UI

- **Settings → Interface** includes a **Date & Time** section where you can set timezone, date format, and time format (24h/12h). These are stored in `user_identity.json` and used in the system prompt and for displaying dates/times.

Under **Settings → Persona & Memory → Long-term Memory (RAG Source)** there is a **User Identity** button (amber). It opens a modal with:

- **Left**: Contents of `user_identity.json` (name, language, location: city/country, main messenger, preferences, do's, don'ts). Read-only; updates happen via the tool or when the user tells the agent.
- **Right**: Timeline of `change_log` entries (time and action), newest first.

The modal size matches the Memory Graph modal (95vw × 90vh).

## Local vs network (DB and user separation)

When **local only** (no network, no auth):

- After the first admin is created via `POST /api/auth/bootstrap`, `local_admin_scope_id` and `local_admin_username` in config are set to that admin's UUID and username so that local mode and CLI use the same identity as that admin (see [UUID.md](UUID.md)).
- When there is no auth, the HTTP API and WebSocket use `get_local_admin_username()` and the backend uses `get_local_admin_scope_id()` for RAG/DB, so the same `user_identity.json` and scoped data (memories, mail DB, contacts) are used as for the local admin.
- **RAG / DB (memories)** use that scope; **user identity (user_identity.json)** uses that username (e.g. `~/.vaf/users/<local_admin_username>/user_identity.json`).

So in local mode, one logical “local user” is used everywhere: same `user_scope_id` for RAG/DB and same username for user identity. The WebUI User Identity modal and the model’s `update_user_identity` tool read/write the same file.

When **network is enabled** and users log in:

- Each user has a `user_scope_id` (UUID from auth DB) for RAG/memories and a `username` for `~/.vaf/users/<username>/user_identity.json`.
- WebSocket sets these from the JWT; HTTP API routes read them from `request.state.user` — a consolidated dict set by `AuthMiddleware` after JWT validation (see [NETWORK_FEATURES.md](NETWORK_FEATURES.md#layer-3-jwt-authentication-middleware)).

## Per-user loading

Each user's `user_identity.json` (and change log) is loaded because the backend uses the **username** from the current request/session (JWT or local admin config) for all persona and user-identity endpoints and for the system prompt; that same username is passed to the `update_user_identity` tool. RAG, mail DB, and contacts are scoped by **user_scope_id** (same session/JWT or config). Together, username + user_scope_id form the effective "user profile" for the request.

| Key | What is keyed by it |
|-----|---------------------|
| **username** | `user_identity.json`, change log, workspace dir `~/.vaf/users/<username>/` |
| **user_scope_id** | RAG/memories (PostgreSQL), email_sync.db path (when not legacy admin path), contacts, credentials, cache keys |

Note: the shared Soul/persona block is currently loaded from the admin workspace in `system_prompt.py`; it is not a per-user `soul.md` swap for every request.

As long as both keys are set correctly for the session (from JWT or config), each user gets their own identity file and scoped data.

## Best practices

- Use `update_user_identity` only for the **current user's** profile. The tool receives the username from the session; do not use it to write another user's file.
- Prefer one clear action per call (e.g. set name, or add one preference) so the change_log stays readable.
- Keep preferences and do's/don'ts short and actionable so they fit well in the system prompt.
