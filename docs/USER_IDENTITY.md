# User Identity

This document describes how VAF stores and uses the **current human user's** profile (name, language, preferences, do's and don'ts). This is separate from the agent's own persona (Soul and `identity.json`).

## Purpose

The model needs to know who it is talking to so it can:
- Greet the user by name (e.g. "Hey Mert").
- Prefer the user's language.
- Follow stated preferences and rules (e.g. "always be concise", "don't use emojis").

That data is stored per user and injected into the system prompt each turn.

## Storage

- **File**: `~/.vaf/users/<username>/user_identity.json`
- **Contents**: `name`, `preferred_language`, `preferences` (list of strings), `dos` (list), `donts` (list), `main_messenger` (optional: `"telegram"` | `"discord"` | `"slack"`), `change_log` (list of `{ "at": "<ISO8601>", "action": "<summary>" }`).
- **Created**: When the workspace is first used; default `name` is the username, other fields empty.

Do not confuse with `identity.json` in the same directory: that file holds the **agent's** display name, emoji, and theme (used in the Soul block). User identity is only about the human user.

## System prompt injection

In `vaf/core/system_prompt.py`, `build_prompt()` adds a block **"## User identity (current user)"** when a username (or user_scope_id) is available. It reads `user_identity.json` via `UserWorkspace.get_user_identity()` and appends:

- Name and preferred language.
- Preferences, Do, and Don't lists.
- Optional `main_messenger` (preferred channel for proactive messages: telegram, discord, or slack).

When the user has at least one messaging connection (Telegram, Discord), a **"## Messaging connections (proactive messages)"** subsection is added: it lists available channels and the preferred channel (if set), and instructs the agent to ask once if the user wants to receive something but has not set `main_messenger`, then to use `update_user_identity(main_messenger=...)` and the matching channel tool (`send_telegram`, `send_discord`, or `send_slack`). Only tools for configured connections are exposed to the agent. See [CONNECTIONS.md](CONNECTIONS.md) for proactive messaging.

That block is rebuilt every turn (dynamic system prompt), so the model always sees the latest user identity.

## Tool: update_user_identity

- **Module**: `vaf/tools/user_identity.py`
- **Name**: `update_user_identity`
- **When to use**: When the user says their name, language, or rules (e.g. "call me Mert", "I prefer German", "always be concise", "don't use emojis"). Also when the user says which channel to use for proactive messages (e.g. "send it via Telegram" → `main_messenger="telegram"`).

Parameters (all optional): `name`, `language`, `main_messenger` (`"telegram"` | `"discord"` | `"slack"`), `add_preference` / `remove_preference`, `add_do` / `remove_do`, `add_dont` / `remove_dont`. At least one must be provided.

On each successful run, the tool appends one entry to `change_log` with the current time (same source as the system prompt clock) and a short action summary (e.g. "name", "language", "preference"). The log is trimmed to the last 50 entries.

The agent injects the current username into the tool call so the correct `user_identity.json` is updated.

## Proactive messaging (send_telegram, send_discord)

When the user asks the agent to send them something (e.g. a summary or a result "via Telegram"), the agent uses:

1. **Messaging connections** (in the system prompt): which channels are available (Telegram, Discord) and whether `main_messenger` is set. Only tools for configured connections are exposed (e.g. `send_telegram` only if the user has Telegram).
2. If preferred channel is not set, the agent asks once (e.g. "Should I send it via Discord, Telegram or Slack?") and stores the answer with `update_user_identity(main_messenger="telegram")` (or discord/slack).
3. The agent uses the matching tool: **`send_telegram`** (`vaf/tools/send_telegram.py`), **`send_discord`** (`vaf/tools/send_discord.py`), or **`send_slack`** (`vaf/tools/send_slack.py`) depending on `main_messenger` or the user's request. For Telegram, the user must have sent at least one message from Telegram first so VAF can associate their chat ID (stored in `messaging_endpoints.json` under the platform data directory).

## API

- **GET /api/user/persona**  
  Returns `identity` (agent), `user_identity` (human user profile), and `soul`. The Settings UI uses this to show the User Identity modal.

## Settings UI

Under **Settings → Persona & Memory → Long-term Memory (RAG Source)** there is a **User Identity** button (amber). It opens a modal with:

- **Left**: Contents of `user_identity.json` (name, language, main messenger, preferences, do's, don'ts). Read-only; updates happen via the tool or when the user tells the agent.
- **Right**: Timeline of `change_log` entries (time and action), newest first.

The modal size matches the Memory Graph modal (95vw × 90vh).

## Local vs network (DB and user separation)

When **local only** (no network, no auth):

- **RAG / DB (memories)** use a fixed scope so entries are scoped, not global:
  - Config: `local_admin_scope_id` = `00000000-0000-0000-0000-000000000001` (default).
  - WebSocket and HTTP Memory API both use this scope when there is no authenticated user.
- **User identity (user_identity.json)** uses a fixed username so Chat and Settings show the same data:
  - Config: `local_admin_username` = `admin` (default).
  - WebSocket (chat) and HTTP API (GET /api/user/persona) both use this username when there is no auth.
  - File: `~/.vaf/users/admin/user_identity.json`.

So in local mode, one logical “local user” is used everywhere: same `user_scope_id` for RAG/DB and same username for user identity. The WebUI User Identity modal and the model’s `update_user_identity` tool read/write the same file.

When **network is enabled** and users log in:

- Each user has a `user_scope_id` (UUID from auth DB) for RAG/memories and a `username` for `~/.vaf/users/<username>/user_identity.json`.
- WebSocket sets these from the JWT; HTTP API would get them from auth middleware (`request.state.user`) when implemented.

## Best practices

- Use `update_user_identity` only for the **current user's** profile. The tool receives the username from the session; do not use it to write another user's file.
- Prefer one clear action per call (e.g. set name, or add one preference) so the change_log stays readable.
- Keep preferences and do's/don'ts short and actionable so they fit well in the system prompt.
