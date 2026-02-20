# Thinking Mode

Thinking mode runs the main agent in the background when the user has been idle for a configurable period. It acts on the user’s behalf: processes todos and notes, creates or adjusts automations, and can ask the user a question via their main messenger (Telegram, WhatsApp, Discord). A run continues for multiple turns until the agent calls the `thinking_done` tool (or a turn limit is reached). For the local admin, thinking output is appended to the main Web UI chat session (`web-default`) so it appears in one place; legacy thinking-only sessions are hidden from the chat list.

---

## Overview

- **When it runs:** After `thinking_idle_minutes` of no user activity (chat, Web UI open, headless input). Opening the Web UI or sending a message counts as activity and resets the idle timer.
- **What it does:** One run = multiple agent turns. The agent gets a prompt, can call tools (todos, notes, automations, email, send message), and must call the **`thinking_done`** tool when finished. It may send at most one message to the user per run; if it asks a question, the system waits for a reply (nudge after 3 min, skip after 10 min).
- **Per user:** Idle is tracked per `user_scope_id`; scope is normalized so "default" and the local admin scope count as one user (see Idle detection). One run at a time per user; runs are serialized by a lock.
- **Sessions:** For the **local admin**, each run is appended to the session `web-default` (same as the main Web UI chat). Tool names are stored so the UI does not show "Unknown Tool". Legacy sessions with `metadata.source === "thinking"` or id starting with `thinking_` are excluded from the chat list.

---

## Configuration

All keys live in the main config (e.g. `config.json` or Settings in the Web UI). Defaults are in `vaf/core/config.py`.

| Key | Default | Description |
|-----|---------|-------------|
| `thinking_enabled` | `true` | Master switch for thinking mode. |
| `thinking_idle_minutes` | `10` | Minutes without activity before a run may start. |
| `thinking_check_interval_seconds` | `60` | How often the background loop checks for idle users. |
| `thinking_automation_buffer_minutes` | `10` | Do not start a run if an automation is scheduled within this many minutes. |
| `thinking_max_duration_minutes` | `30` | Max run duration; lock is released after this (stale lock can be replaced). |
| `thinking_max_turns` | `10` | Max agent turns per run; the run ends when the agent calls `thinking_done` or this limit is reached (cap 30). |
| `thinking_wait_nudge_minutes` | `3` | If the user has not replied to a question, send a nudge after this many minutes. |
| `thinking_wait_skip_minutes` | `10` | If still no reply after this many minutes, clear the waiting state and allow the next run without that answer. |
| `thinking_gc_hours` | `12` | Garbage collector deletes thinking-mode sessions older than this many hours. |

---

## Idle detection

- **Source of truth:** `last_interaction.json` in the platform data dir (see `vaf/core/last_interaction.py`). Each key is a user scope (e.g. `default` or UUID); value has `ts`, `source`, `preview`.
- **Activity updates:**  
  - Web: when the user sends a chat message and when the WebSocket connects (so opening the Web UI counts as activity).  
  - Headless: when a user task is processed.  
- **Idle list:** `get_idle_user_scope_ids(idle_minutes)` returns users whose last interaction is older than `idle_minutes`. The list is normalized so that the key `"default"` and the local admin scope (e.g. `local_admin_scope_id`) are treated as one user; duplicates are removed. The thinking loop uses this with `thinking_idle_minutes`.

---

## Run flow

1. **Loop:** A background thread runs `thinking_loop_iteration()` every `thinking_check_interval_seconds`. Started from the tray or from the web server startup (so it runs even without the tray).
2. **Eligibility:** For each idle user, skip if they are in “waiting for reply” and still within nudge/skip window; skip if an automation runs within `thinking_automation_buffer_minutes`.
3. **Lock:** `acquire_lock(user_scope_id)` returns a `run_id` or `None` if already locked (or lock is not yet stale). Prevents overlapping runs per user.
4. **Run:** `_run_thinking_for_user(user_scope_id, run_id, started_at_ts)` runs in a daemon thread: set `VAF_THINKING_MODE=1` (so the agent loads the `thinking_done` tool), load model, init chat, append “THINKING MODE” notice + last run summary + any stored user reply to the system message. Then a **loop** runs: each iteration calls `chat_step()` (first with `THINKING_PROMPT` and RAG context, then with a short “Continue. When finished, call thinking_done.” prompt). The loop exits when the agent’s history contains a call to `thinking_done` or when `thinking_max_turns` is reached.
5. **After run:** Run log is written under `thinking_mode_logs/<scope_key>/`. For the **local admin**, the run is appended to the session `web-default` via `SessionManager.append_thinking_run_to_session("web-default", ...)` so it appears in the main chat. For other users, the run is appended to a daily thinking session via `append_to_thinking_session()`. If the agent sent a message (e.g. Telegram/WhatsApp/Discord), `set_waiting_for_reply()` is called so the next iteration can nudge or skip.
6. **Unlock:** `release_lock(user_scope_id)` is called in a `finally` so the next run can start after the interval.

---

## Waiting for user reply

- When the agent sends a message to the user (e.g. a question) during a run, the code calls `set_waiting_for_reply(user_scope_id, username, display_name)`.
- **Nudge:** After `thinking_wait_nudge_minutes`, a short message (e.g. “Hey &lt;name&gt;, bist du da?”) is sent via the user’s main messenger. Sent at most once per waiting period.
- **Skip:** After `thinking_wait_skip_minutes`, `clear_waiting_for_reply(user_scope_id)` is called so the next run can start without that answer.
- **User replies:** When the user sends a message (Web or headless), the backend calls `clear_waiting_for_reply(user_scope_id, user_reply_text=...)` if there was a waiting state. The reply is then:
  - Stored for the **next thinking run** (injected as “User reply to your last question” in the system prompt, then consumed).
  - Stored for the **thinking session UI**: associated with the last thinking session id and shown as a user message when that session is loaded in the Web UI (then removed so it is shown only once).

---

## Sessions and Web UI

- **Local admin:** Thinking output is appended to the session **`web-default`** (the main Web UI chat) via `SessionManager.append_thinking_run_to_session()`. No separate thinking session is created for the list; the user sees thinking and normal chat in one place.
- **Other users:** For non-local-admin users, runs are appended to a daily thinking session (`thinking_<scope_key>_<date>`) via `append_to_thinking_session()`; "last thinking session" is recorded for attaching user replies.
- **Chat list:** The Web UI session list (`_web_ui_sessions`) excludes sessions whose id starts with `thinking_` or whose `metadata.source === "thinking"`, so legacy thinking-only sessions do not appear in the sidebar.
- **Hide vs delete:** For any session, the trash action can send `hide_session`; the backend sets `metadata.hidden_from_list = true`. The garbage collector deletes old thinking sessions by age (see below).
- **System prompt hidden:** When viewing a session that contains thinking content (e.g. `web-default` after a run), the long thinking-mode system prompt is not shown in the UI; only the agent's steps, tool calls, and replies are displayed.
- **Message input:** The message box stays available. The user can reply in the same chat (e.g. to answer the agent's question); the backend enqueues it and stores it for the next thinking run.
- **Tool names:** Appending a thinking run creates assistant and `role="tool"` messages with `metadata.toolName`, `toolId`, `toolStatus` so the UI shows real tool names instead of "Unknown Tool".
- **User reply:** For the local admin, replies are sent in `web-default` and handled as normal chat; they are also passed to `clear_waiting_for_reply()` so the next thinking run sees "User reply to your last question" in the system prompt.

---

## Garbage collection

- The garbage collector (started from the tray or from web server startup) runs periodically. In addition to logs and temp files, it deletes **thinking-mode sessions** older than `thinking_gc_hours` (default 12). Only sessions with `metadata.source === "thinking"` and `updated_at` (or `created_at`) older than the cutoff are deleted. Implementation: `GarbageCollector._clean_old_thinking_sessions(stats)` in `vaf/core/garbage_collector.py`.

---

## Data files (platform data dir)

| File | Purpose |
|------|---------|
| `thinking_mode_locks.json` | Per-user run locks (run_id, started_at_ts). |
| `thinking_waiting_reply.json` | Per-user “waiting for reply” state (question_sent_at_ts, nudge_sent_at_ts, username, display_name). |
| `thinking_last_reply.json` | Per-user last reply preview for the next thinking run (reply_preview, reply_at_ts). Consumed when the next run starts. |
| `thinking_last_session_id.json` | Per-user last thinking session id; used to attach the next user reply to that session for the UI. |
| `thinking_user_replies.json` | Map session_id → { reply, at }; shown once when loading that thinking session, then removed. |
| `last_interaction.json` | Last activity per user (ts, source, preview); used for idle detection (see `vaf/core/last_interaction.py`). |

Run logs are under `Platform.vaf_dir() / "thinking_mode_logs" / <scope_key> /` (e.g. `runid_20260219_143022.json`).

---

## Relevant code

- **Entry and loop:** `vaf/core/thinking_mode.py` — `start_thinking_mode_background()`, `thinking_loop_iteration()`, `maybe_start_thinking_for_user()`, `_run_thinking_for_user()`; `_history_has_thinking_done()`; scope normalization in `get_idle_user_scope_ids()` and `_key()`.
- **Tool:** `vaf/tools/thinking_done.py` — `ThinkingDoneTool`; registered in the agent only when `VAF_THINKING_MODE=1` (see `vaf/core/agent.py` `_load_tools()`).
- **Sessions:** `vaf/core/session.py` — `append_thinking_run_to_session()` (append run to a given session, e.g. `web-default`), `append_to_thinking_session()` (daily thinking session for non–local-admin), `list()` (skips `hidden_from_list`), `hide(session_id)`.
- **Web:** `vaf/core/web_server.py` — `_web_ui_sessions()` filters out sessions with `metadata.source === "thinking"` or id starting with `thinking_`; WebSocket handlers `load_session`, `hide_session`; `update_last_interaction` on connect; startup starts the garbage collector.
- **GC:** `vaf/core/garbage_collector.py` — `_clean_old_thinking_sessions()`.
- **Idle/reply:** `vaf/core/last_interaction.py` (activity); `vaf/core/thinking_mode.py` (waiting state, last reply, last session id, user replies per session).
- **Frontend:** `web/app/page.tsx` — brain icon for thinking sessions; trash sends `hide_session` for `source === 'thinking'`; thinking-mode system prompt is hidden when viewing a thinking session; message input stays enabled for replying.

---

## See also

- **AUTOMATIONS.md** — Scheduled automations and planner (todos/notes), which thinking mode uses.
- **SESSION_MANAGEMENT.md** — Session lifecycle and Web UI sync.
- **TELEGRAM_INTEGRATION.md**, **WHATSAPP_INTEGRATION.md** — Messaging channels used when the agent sends the single allowed message or the nudge.
