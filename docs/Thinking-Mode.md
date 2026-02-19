# Thinking Mode

Thinking mode runs the main agent in the background when the user has been idle for a configurable period. It acts on the user’s behalf: processes todos and notes, creates or adjusts automations, and can ask the user a single question via their main messenger (Telegram, WhatsApp, Discord). Runs are persisted as sessions in the Web UI chat list and can be inspected or hidden.

---

## Overview

- **When it runs:** After `thinking_idle_minutes` of no user activity (chat, Web UI open, headless input). Opening the Web UI or sending a message counts as activity and resets the idle timer.
- **What it does:** One pass per run: the agent gets a fixed prompt, can call tools (todos, notes, automations, email, send message), and must send at most one message to the user. If it asks a question, the system waits for a reply (nudge after 3 min, skip after 10 min).
- **Per user:** Idle is tracked per `user_scope_id` (see `last_interaction.json`). One run at a time per user; runs are serialized by a lock.
- **Sessions:** Each run is saved as a session with `metadata.source === "thinking"` and appears in the chat list with a brain icon. Tool names are stored so the UI does not show "Unknown Tool". User replies to thinking questions are stored and shown in the next run’s context and in the thinking session’s history when loaded.

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
| `thinking_wait_nudge_minutes` | `3` | If the user has not replied to a question, send a nudge after this many minutes. |
| `thinking_wait_skip_minutes` | `10` | If still no reply after this many minutes, clear the waiting state and allow the next run without that answer. |
| `thinking_gc_hours` | `12` | Garbage collector deletes thinking-mode sessions older than this many hours. |

---

## Idle detection

- **Source of truth:** `last_interaction.json` in the platform data dir (see `vaf/core/last_interaction.py`). Each key is a user scope (e.g. `default` or UUID); value has `ts`, `source`, `preview`.
- **Activity updates:**  
  - Web: when the user sends a chat message and when the WebSocket connects (so opening the Web UI counts as activity).  
  - Headless: when a user task is processed.  
- **Idle list:** `get_idle_user_scope_ids(idle_minutes)` returns users whose last interaction is older than `idle_minutes`. The thinking loop uses this with `thinking_idle_minutes`.

---

## Run flow

1. **Loop:** A background thread runs `thinking_loop_iteration()` every `thinking_check_interval_seconds`. Started from the tray or from the web server startup (so it runs even without the tray).
2. **Eligibility:** For each idle user, skip if they are in “waiting for reply” and still within nudge/skip window; skip if an automation runs within `thinking_automation_buffer_minutes`.
3. **Lock:** `acquire_lock(user_scope_id)` returns a `run_id` or `None` if already locked (or lock is not yet stale). Prevents overlapping runs per user.
4. **Run:** `_run_thinking_for_user(user_scope_id, run_id, started_at_ts)` runs in a daemon thread: load model, init chat, append “THINKING MODE” notice + last run summary + any stored user reply to the system message, then one `chat_step(THINKING_PROMPT)` with optional RAG context.
5. **After run:** Run log is written under `thinking_mode_logs/<scope_key>/`. Session is saved via `SessionManager.save_thinking_run()` (session id like `thinking_<scope>_<run_id>`). If the agent sent a message (e.g. Telegram/WhatsApp/Discord), `set_waiting_for_reply()` is called so the next iteration can nudge or skip.
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

- **Session id:** `thinking_<scope_key>_<run_id>` (e.g. `thinking_default_abc12345`). Stored by `SessionManager.save_thinking_run()`; that method returns the session id so it can be recorded as “last thinking session” for the user.
- **Chat list:** Thinking sessions appear with a brain icon and `source === 'thinking'`. They are normal sessions on disk; `list()` excludes those with `metadata.hidden_from_list === true`.
- **Hide vs delete:** In the Web UI, the trash action on a thinking session sends `hide_session` (not `delete_session`). The backend sets `metadata.hidden_from_list = true` and broadcasts the updated session list. The session file remains; it is only hidden from the list. The garbage collector still deletes old thinking sessions by age (see below).
- **Tool names:** Saving a thinking run creates one assistant message and one `role="tool"` message per tool call, with `metadata.toolName`, `toolId`, `toolStatus`. The Web server sends these in `frontend_messages` so the UI shows real tool names instead of “Unknown Tool”.
- **User reply in session:** When loading a session whose id starts with `thinking_`, the backend checks for a stored user reply for that session id. If present, it appends a user message “User replied: &lt;preview&gt;” to the history and removes the stored reply so it is only shown once.

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

- **Entry and loop:** `vaf/core/thinking_mode.py` — `start_thinking_mode_background()`, `thinking_loop_iteration()`, `maybe_start_thinking_for_user()`, `_run_thinking_for_user()`.
- **Sessions:** `vaf/core/session.py` — `save_thinking_run()`, `list()` (skips `hidden_from_list`), `hide(session_id)`.
- **Web:** `vaf/core/web_server.py` — WebSocket handlers `load_session`, `hide_session`; `update_last_interaction` on connect; injection of user reply when loading a thinking session; startup starts the garbage collector.
- **GC:** `vaf/core/garbage_collector.py` — `_clean_old_thinking_sessions()`.
- **Idle/reply:** `vaf/core/last_interaction.py` (activity); `vaf/core/thinking_mode.py` (waiting state, last reply, last session id, user replies per session).
- **Frontend:** `web/app/page.tsx` — brain icon for thinking sessions; trash sends `hide_session` for `source === 'thinking'`.

---

## See also

- **AUTOMATIONS.md** — Scheduled automations and planner (todos/notes), which thinking mode uses.
- **SESSION_MANAGEMENT.md** — Session lifecycle and Web UI sync.
- **TELEGRAM_INTEGRATION.md**, **WHATSAPP_INTEGRATION.md** — Messaging channels used when the agent sends the single allowed message or the nudge.
