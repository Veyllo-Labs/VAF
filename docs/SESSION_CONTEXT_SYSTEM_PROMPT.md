# Session Context in the System Prompt

This document describes how the agent gets "last user interaction" and "current channel" (WebUI, Telegram, WhatsApp, CLI, Discord) in its system prompt, and where that text comes from.

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
- **Where:** `source` — `"web"`, `"telegram"`, `"whatsapp"`, `"cli"`, or `"discord"`.
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

If either is set, it adds `last_interaction` and `current_channel` lines inside the **`<context>`** block (which also contains the current datetime and environment info). The block uses:

- The user’s display name from user identity (or "the user" if none).
- Relative time from the last interaction timestamp. Steps: under 1 h → minutes; 1–24 h → hours; 1 day → "yesterday"; 2–29 days → days; 30+ days → months (≈30 days each); 365+ days → years. Language follows the prompt language (e.g. "5 min ago" / "vor 5 Min.", "2 months ago" / "vor 2 Monaten").
- Channel display names: "WebUI", "Telegram", "CLI", "Discord".

No session-context block is added if both `current_source` and `last_interaction` are missing (e.g. first message ever, or CLI without this feature).

### 3a. Channel capabilities (text-only channels)

When the current channel is **Telegram**, **WhatsApp**, **Discord**, or **CLI**, the system prompt adds a **channel capabilities** block that instructs the model:

- The user does **not** have access to the Web UI on this channel.
- They cannot view documents, attachment lists, or pages in a browser.
- Provide all relevant information **directly in the answer** – extract and quote content.
- **Never** tell the user to "look at" something (e.g. "Schau dir die Seiten an", "Look at the document in the attachments").

This prevents the model from giving unhelpful responses such as "The document is in the attachments – look at the pages" when the user is on Telegram or Discord and has no Web UI.

The block is shown only when `current_source` is one of `telegram`, `whatsapp`, `discord`, or `cli`. When the user is in the **Web UI** (`source="web"`), this block is omitted.

### 3b. Messaging connections block

When the current user has at least one **messaging connection** (Telegram, Discord, or WhatsApp) and a username or user_scope_id is available, `build_prompt()` also adds `messaging_channels` and `preferred_messenger` lines inside the **`<user_context>`** block. It states:

- Which channels are available for proactive messages (e.g. Telegram, Discord, WhatsApp).
- The user’s preferred channel (`main_messenger` from user_identity, if set).
- Instructions: if the user asks to receive something but has not set a preferred channel, ask once and store with `update_user_identity(main_messenger="telegram")` (or discord/slack/whatsapp); then use the matching tool (`send_telegram`, `send_discord`, `send_slack`, or `send_whatsapp`) to send the content.

This block is built using `vaf/core/messaging_connections.get_messaging_connections(username, user_scope_id)` and is only shown when the list of available channels is non-empty. See [CONNECTIONS.md](CONNECTIONS.md) and [USER_IDENTITY.md](USER_IDENTITY.md) for proactive messaging and `main_messenger`.

### 3c. Web UI attachment context (session-scoped lane)

For Web UI turns with sidebar attachments, the headless runner injects a `DOCUMENT CONTEXT ACTIVE` block plus retrieved attachment snippets from a **session-scoped ephemeral attachment lane** (scoped by `session_id + user_scope_id`, TTL-based). This is separate from long-term RAG memories.

If the user asks to keep attachment knowledge for future chats, the agent should suggest `learn_attached_knowledge` and ask for explicit confirmation before transfer.

### 4. When the block is not updated

- System commands (`__CMD__:...`) do not update the store.
- Compaction tasks do not update it.
- Only real user chat messages (processed via the headless runner) update the store and set `_current_chat_source`.

## Code Locations

| Responsibility | File | Notes |
|----------------|------|--------|
| Store read/write | `vaf/core/last_interaction.py` | `update_last_interaction()`, `get_last_interaction()`, JSON under data dir |
| Prompt block text | `vaf/core/system_prompt.py` | `build_prompt(..., current_source=..., last_interaction=...)`, section "2b. LAST INTERACTION & CURRENT CHANNEL" |
| Channel capabilities | `vaf/core/system_prompt.py` | Section "2c. CHANNEL CAPABILITIES" – added when `current_source` is telegram/whatsapp/discord/cli |
| Passing data into prompt | `vaf/core/agent.py` | Both `build_prompt` calls pass `current_source` and `last_interaction` |
| Set channel and write store | `vaf/core/headless_runner.py` | Before `chat_step`: set `_current_chat_source`; after `chat_step`: call `update_last_interaction()` |
| Set channel (Gateway/Discord) | `vaf/core/gateway.py` | Before `chat_step`: set `_current_chat_source` from `context.platform` (e.g. `"discord"`). Discord bridge sends `platform: "discord"` in payload. |

## User isolation

The store is **per user**: each user has their own last-interaction entry. User Max never sees Susanne’s last interaction, and the other way around.

- **Write:** The headless runner passes `user_scope_id` from the task metadata (from WebUI connection or Telegram whitelist). The store key is that scope (or `"default"` when there is no scope). So each user’s message updates only their own key.
- **Read:** When building the system prompt, the agent passes its current `_current_user_scope_id` (set from the same session/task). So we only load the last interaction for the user who is chatting right now.

So the `last_interaction` / `current_channel` lines inside `<context>` always refer to the **same** user: their previous turn and their current channel. No cross-user data is shown.

## Storage

- **Path:** `Platform.data_dir() / "last_interaction.json"` (OS-dependent; see `vaf/core/platform.py`).
- **Shape:** One object keyed by user. Key is `user_scope_id` as string (e.g. UUID), or `"default"` when there is no scope. Each value has:
  - `ts`: Unix timestamp (float)
  - `source`: `"web"` | `"telegram"` | `"whatsapp"` | `"cli"` | `"discord"`
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

The following are examples of the session-related lines inside the **`<context>`** block only. The rest of the system prompt (identity, memory instructions, workspace, user block, etc.) is not shown.

---

**Example 1 — First message in the session (current channel only)**  
No previous interaction is stored yet. Only the current channel is known (e.g. WebUI).

```
<context>
Today is Thursday, 2026-05-28 10:03:09.
os: linux | home: /home/mert | new projects: /home/mert/Documents/VAF_Projects/
current_channel: WebUI
</context>
```

---

**Example 2 — Second message, same channel (WebUI)**  
The user had sent one message a few minutes ago in the WebUI; the current message is also from the WebUI.

```
<context>
Today is Thursday, 2026-05-28 10:03:09.
os: linux | home: /home/mert | new projects: /home/mert/Documents/VAF_Projects/
last_interaction: Alex 3 min ago via WebUI
current_channel: WebUI
</context>
```

---

**Example 3 — User switched from Telegram to WebUI**  
The last interaction was on Telegram (with a short preview); the current one is in the WebUI.

```
<context>
Today is Thursday, 2026-05-28 10:03:09.
os: linux | home: /home/mert | new projects: /home/mert/Documents/VAF_Projects/
last_interaction: Alex 10 min ago via Telegram
prior_topic: "Can you check the deployment status?" (previous chat — current message may be unrelated)
current_channel: WebUI
</context>
```

---

**Example 4 — German UI language**  
Same idea as Example 3, but the prompt language is German, so relative time is in German.

```
<context>
Heute ist Donnerstag, 28.05.2026 10:03:09.
os: linux | home: /home/mert | new projects: /home/mert/Documents/VAF_Projects/
last_interaction: Mert vor 10 Min. via Telegram
prior_topic: "Kannst du den Deployment-Status prüfen?" (previous chat — current message may be unrelated)
current_channel: WebUI
</context>
```

(Channel names "WebUI" and "Telegram" stay in English in the prompt; only the relative time string is localized.)

---

## 3d. Thinking-mode reply context injection

When the agent reaches out to the user during a background **Thinking Mode** pass (e.g. "Hey Mert, bist du da?") and waits for a reply, the question text is stored via `set_waiting_for_reply(question_text=...)` in `vaf/core/thinking_mode.py`.

When the user replies, `chat_step()` reads this stored question and stashes it on `self._thinking_reply_context`. Then `_prepare_messages()` injects it as a `role:system` message **directly before the final user message** in the list sent to the LLM — so the model knows what it originally asked.

**Why not modify `user_input` directly?**  
Earlier versions prepended `[Context: ...]` to `user_input` itself. This caused the prefix to be stored in `self.history` and to appear in WebUI chat bubbles on page reload. The current approach keeps `self.history` clean: the user's message is stored as typed, and the context only lives in the ephemeral messages list passed to the LLM for that single turn.

**Scope:** works across all channels (WebUI, Telegram, WhatsApp, Discord) — the state is keyed by `user_scope_id`, not by channel or session.

**Code locations:**

| Responsibility | File | Notes |
|---|---|---|
| Store outbound question | `vaf/core/thinking_mode.py` | `set_waiting_for_reply(question_text=...)` |
| Read + stash on reply | `vaf/core/agent.py` | `chat_step()` — sets `self._thinking_reply_context` |
| Inject into LLM messages | `vaf/core/agent.py` | `_prepare_messages()` — system msg before last user msg; clears after use |
| Clear waiting state | `vaf/core/thinking_mode.py` | `clear_waiting_for_reply()` called in `chat_step()` after stash |

---

## Summary

- **Last interaction** = the previous user turn (timestamp, channel, short preview), read from `last_interaction.json` and shown in the system prompt.
- **Current channel** = where this request came from (WebUI / Telegram / CLI), set on the agent before `chat_step` and passed into `build_prompt`.
- **Thinking-mode reply context** = when the agent sent a proactive question during a background pass, the question text is injected as a system message in `_prepare_messages()` so the LLM understands vague replies like "yes" or "I'm here". History stays clean.
- The session context block is optional: it appears only when at least one of current channel or last interaction is available, and it is kept to one or two short sentences so the model gets clear, minimal context without extra noise.
