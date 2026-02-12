# Session Context in the System Prompt

This document describes how the agent gets "last user interaction" and "current channel" (WebUI, Telegram, CLI, Discord) in its system prompt, and where that text comes from.

## Purpose

The model receives a short line that states:

- When the user last interacted and via which channel (if there was a previous turn).
- Which channel the user is using right now.

That gives the model a clear sense of time and place: it knows whether the user just switched from Telegram to the WebUI, or has been chatting only in one place, and can avoid repeating things or assuming the wrong channel.

## How It Works

### 1. Storing the last interaction

After each user message is fully processed (after `chat_step` returns), the headless runner records:

- **Who:** `user_scope_id` (or a default key for single-user).
- **When:** current timestamp.
- **Where:** `source` — `"web"`, `"telegram"`, `"cli"`, or `"discord"`.
- **About what:** a short preview of the user message (single line, max 80 characters, whitespace normalized).

That record is written to a JSON file in the platform data directory (see [Storage](#storage)). The **next** turn will read this as "last interaction"; the current turn does not see itself in "last interaction".

### 2. Setting the current channel

Before each `chat_step`, the headless runner sets on the agent:

- `agent._current_chat_source = task.source` (e.g. `"web"` or `"telegram"`).

So when the system prompt is built inside `chat_step`, it knows the channel for **this** request.

### 3. Building the prompt block

When the system prompt is built (`SystemPromptManager.build_prompt` in `vaf/core/system_prompt.py`), it:

- Reads `current_source` from the agent (`_current_chat_source`).
- Reads the last interaction from the store via `get_last_interaction(user_scope_id)`.

If either is set, it appends a **"## Session context"** block after **"## Current Time"**. The block uses:

- The user’s display name from user identity (or "the user" if none).
- Relative time from the last interaction timestamp. Steps: under 1 h → minutes; 1–24 h → hours; 1 day → "yesterday"; 2–29 days → days; 30+ days → months (≈30 days each); 365+ days → years. Language follows the prompt language (e.g. "5 min ago" / "vor 5 Min.", "2 months ago" / "vor 2 Monaten").
- Channel display names: "WebUI", "Telegram", "CLI", "Discord".

No session-context block is added if both `current_source` and `last_interaction` are missing (e.g. first message ever, or CLI without this feature).

### 3b. Messaging connections block

When the current user has at least one **messaging connection** (Telegram or Discord) and a username or user_scope_id is available, `build_prompt()` also adds a **"## Messaging connections (proactive messages)"** subsection inside the **"## 👤 CURRENT USER CONTEXT"** block. It states:

- Which channels are available for proactive messages (e.g. Telegram, Discord).
- The user’s preferred channel (`main_messenger` from user_identity, if set).
- Instructions: if the user asks to receive something but has not set a preferred channel, ask once and store with `update_user_identity(main_messenger="telegram")` (or discord/slack); then use the matching tool (`send_telegram`, `send_discord`, or `send_slack`) to send the content.

This block is built using `vaf/core/messaging_connections.get_messaging_connections(username, user_scope_id)` and is only shown when the list of available channels is non-empty. See [CONNECTIONS.md](CONNECTIONS.md) and [USER_IDENTITY.md](USER_IDENTITY.md) for proactive messaging and `main_messenger`.

### 4. When the block is not updated

- System commands (`__CMD__:...`) do not update the store.
- Compaction tasks do not update it.
- Only real user chat messages (processed via the headless runner) update the store and set `_current_chat_source`.

## Code Locations

| Responsibility | File | Notes |
|----------------|------|--------|
| Store read/write | `vaf/core/last_interaction.py` | `update_last_interaction()`, `get_last_interaction()`, JSON under data dir |
| Prompt block text | `vaf/core/system_prompt.py` | `build_prompt(..., current_source=..., last_interaction=...)`, section "2b. LAST INTERACTION & CURRENT CHANNEL" |
| Passing data into prompt | `vaf/core/agent.py` | Both `build_prompt` calls pass `current_source` and `last_interaction` |
| Set channel and write store | `vaf/core/headless_runner.py` | Before `chat_step`: set `_current_chat_source`; after `chat_step`: call `update_last_interaction()` |
| Set channel (Gateway/Discord) | `vaf/core/gateway.py` | Before `chat_step`: set `_current_chat_source` from `context.platform` (e.g. `"discord"`). Discord bridge sends `platform: "discord"` in payload. |

## User isolation

The store is **per user**: each user has their own last-interaction entry. User Max never sees Susanne’s last interaction, and the other way around.

- **Write:** The headless runner passes `user_scope_id` from the task metadata (from WebUI connection or Telegram whitelist). The store key is that scope (or `"default"` when there is no scope). So each user’s message updates only their own key.
- **Read:** When building the system prompt, the agent passes its current `_current_user_scope_id` (set from the same session/task). So we only load the last interaction for the user who is chatting right now.

So the "Session context" block in the system prompt always refers to the **same** user: their previous turn and their current channel. No cross-user data is shown.

## Storage

- **Path:** `Platform.data_dir() / "last_interaction.json"` (OS-dependent; see `vaf/core/platform.py`).
- **Shape:** One object keyed by user. Key is `user_scope_id` as string (e.g. UUID), or `"default"` when there is no scope. Each value has:
  - `ts`: Unix timestamp (float)
  - `source`: `"web"` | `"telegram"` | `"cli"` | `"discord"`
  - `preview`: sanitized, truncated preview of the user message (max 80 chars, single line)

Example with two users (Max and Susanne each have their own key):

```json
{
  "default": {
    "ts": 1738840123.456,
    "source": "telegram",
    "preview": "What's the weather in Berlin?"
  },
  "a1b2c3d4-e5f6-7890-abcd-ef1234567890": {
    "ts": 1738840200.0,
    "source": "web",
    "preview": "Check the deployment status"
  }
}
```

## System Prompt Examples (Session context only)

The following are examples of the **"## Session context"** section only. The rest of the system prompt (identity, time, tools, user block, etc.) is unchanged and not shown.

---

**Example 1 — First message in the session (current channel only)**  
No previous interaction is stored yet. Only the current channel is known (e.g. WebUI).

```
## Session context
Currently chatting in WebUI.
```

---

**Example 2 — Second message, same channel (WebUI)**  
The user had sent one message a few minutes ago in the WebUI; the current message is also from the WebUI.

```
## Session context
Last user Alex interaction: 3 min ago via WebUI. Currently chatting in WebUI.
```

---

**Example 3 — User switched from Telegram to WebUI**  
The last interaction was on Telegram (with a short preview); the current one is in the WebUI.

```
## Session context
Last user Alex interaction: 10 min ago via Telegram. (About: Can you check the deployment status?) Currently chatting in WebUI.
```

---

**Example 4 — German UI language**  
Same idea as Example 3, but the prompt language is German, so relative time is in German.

```
## Session context
Last user Mert interaction: vor 10 Min. via Telegram. (About: Kannst du den Deployment-Status prüfen?) Currently chatting in WebUI.
```

(Channel names "WebUI" and "Telegram" stay in English in the prompt; only the relative time string is localized.)

---

## Summary

- **Last interaction** = the previous user turn (timestamp, channel, short preview), read from `last_interaction.json` and shown in the system prompt.
- **Current channel** = where this request came from (WebUI / Telegram / CLI), set on the agent before `chat_step` and passed into `build_prompt`.
- The block is optional: it appears only when at least one of current channel or last interaction is available, and it is kept to one or two short sentences so the model gets clear, minimal context without extra noise.
