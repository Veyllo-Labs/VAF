# Thinking Mode

Thinking mode runs the main agent in the background while the user is idle. It acts on the user's behalf: processes todos, creates automations, sends proactive messages, and can ask the user a question via their main messenger (Telegram, WhatsApp, Discord). Runs are multi-turn until the agent calls `thinking_done` (or a turn limit is reached).

---

## Overview

- **When it runs:** After `thinking_idle_minutes` of no activity across ALL linked channels (Web UI, Telegram, WhatsApp, etc.).
- **What it does:** One run = multiple agent turns with full tool access (except `memory_save` and Git tools). The agent must call `thinking_done` when finished.
- **Max 1 Message & History Sync:** The agent may send at most one message per run. **New:** Any question asked by the Thinking Agent is automatically persisted to the user's main chat history. This ensures that when the user replies, the normal agent has the full context of what was asked.
- **Per user:** Idle is tracked per logical user (handling all UUID/username aliases). One run at a time per user (serialized by lock). Cooldown between runs: `thinking_cooldown_minutes` (default 60 min).
- **Safety Abort:** If the user becomes active on any channel during a run, the thinking process is immediately aborted to prevent dual-agent responses.
- **Context:** The agent loads the user's full chat session — it has the same context as the normal agent.
- **Output:** Runs are logged to `logs/vaf_denk.log` (human-readable) and to JSON run logs. Messages sent to the user are also mirrored in the Web UI / main chat history.

---

## Configuration
... [Table remains the same] ...

---

## Run flow

1. **Loop:** Background thread runs `thinking_loop_iteration()` every `thinking_check_interval_seconds`.
2. **Quiet hours:** Checks if current local time is inside the prohibited window.
3. **Eligibility & Alias Mapping:** For each user, the system finds the *absolute newest* activity timestamp across all their known aliases (Web UUID, Telegram ID, Admin name). If any alias is active, the user is NOT idle.
4. **Lock:** `acquire_lock(user_scope_id)` returns a `run_id`.
5. **Run:** `_run_thinking_for_user()` runs in a daemon thread:
   - Sets environment flags and creates an `Agent`.
   - **Loads user's chat session** to ensure context parity.
   - **History Synchronization:** If the agent sends a message (e.g., via `send_telegram`), that message is instantly appended to the main session's history on disk.
   - **Real-time Abort Check:** Before each turn, the agent checks if the user's logical ID has seen new activity. If so, it breaks the loop immediately.
6. **After run:** Logs are saved and waiting states are updated.
7. **Unlock:** Lock is released; cooldown recorded.

---

## Tools available in Thinking Mode

The agent has **the same tools as the normal agent**, with these exceptions:

| Tool | Status | Reason |
|------|--------|--------|
| `thinking_done` | **Only in Thinking Mode** | Signals end of the run |
| `thinking_note_add` | **Only in Thinking Mode** | Saves persistent notes for next run |
| `memory_save` | ❌ Excluded | Thinking should read memory, not write to it |
| `git_add_commit` / `git_status` / `git_log` | ❌ Excluded | VAF is the user's project, not the agent's |

---

## Persistent Thinking Notes (`thinking_note_add`)

The agent can call `thinking_note_add` to save notes for its next run — e.g.:

> *"User confirmed Yasin birthday automation is fully handled — do not mention it again"*
> *"User wants to keep Daily calendar check, it is intentional"*

Notes are stored in `thinking_notes.db` (SQLite, per-user isolated) and injected into every subsequent system prompt. They auto-expire after **30 days** (max 50 notes per user).

**System prompt section:**
```
**Deine eigenen Notizen aus früheren Thinking-Runs:**
- [2026-02-20 14:30] User confirmed Yasin birthday automation handled — do not ask again
- [2026-02-19 09:15] User wants to keep Daily calendar check, it is intentional
```

---

## Declined questions

When the user refuses a question the agent asked (replies with "Nein", "no", "nicht", etc.), the agent:
- Records the question text + user reply in `thinking_declined_questions.json` (auto-expire 30 days, max 20 entries)
- Injects a "DO NOT ask these again" section into the next run's system prompt

The actual sent question text is captured from the `send_telegram` / `send_whatsapp` / `send_discord` tool call arguments, not from a summary.

---

## Waiting for user reply

- When the agent sends a message during a run → `set_waiting_for_reply()` is called with the question text
- **Nudge:** After `thinking_wait_nudge_minutes`, a short "Hey, bist du da?" is sent
- **Skip:** After `thinking_wait_skip_minutes`, the waiting state is cleared
- **User replies:** When the user next sends a message, `clear_waiting_for_reply(user_reply_text=...)` is called. The reply is:
  - Injected as "User reply to your last question" in the next run's system prompt
  - If it is a refusal: saved to the declined questions log

---

## Output: logs only, not in Web UI

Thinking mode output is **not shown in the Web UI chat list**. It is logged to:

| Location | Format | Purpose |
|----------|--------|---------|
| `logs/vaf_denk.log` | Human-readable text blocks | Debugging — readable with any text editor |
| `~/.vaf/thinking_mode_logs/<scope_key>/<run_id>_<ts>.json` | JSON | Internal — used by `_get_last_thinking_summary()` for context injection |

**`vaf_denk.log` format:**
```
================================================================================
[THINKING RUN] 2026-02-20T14:30:45.123
  run_id:    a1b2c3d4
  user:      default
  started:   2026-02-20T14:28:00
  ended:     2026-02-20T14:30:45
  duration:  165.0s
  turns:     3

  [system] (system prompt, 4200 chars)
  [user] ## THINKING MODE\nYou are the main agent...
  [assistant] Tools: list_automation_todos, list_automations
  [tool] (completed)
  [assistant] Ich habe deine Todos gecheckt...
```

---

## Idle detection

- **Source:** `last_interaction.json` in platform data dir (see `vaf/core/last_interaction.py`)
- **Activity sources:** Web UI WebSocket connect, chat message sent, headless task processed
- **Scope normalization:** `"default"` and the local admin UUID are treated as one user; duplicates removed

---

## Data files (platform data dir)

| File | Purpose |
|------|---------|
| `thinking_mode_locks.json` | Per-user run locks (run_id, started_at_ts) |
| `thinking_waiting_reply.json` | Per-user "waiting for reply" state (question_sent_at_ts, nudge_sent_at_ts, username, question_text) |
| `thinking_last_reply.json` | Per-user last reply preview for the next run (consumed on read) |
| `thinking_last_session_id.json` | Per-user last thinking session id (for attaching user replies) |
| `thinking_last_completed.json` | Per-user timestamp of last completed run (for cooldown) |
| `thinking_declined_questions.json` | Per-user list of refused questions (auto-expire 30 days) |
| `thinking_notes.db` | Per-user SQLite DB of persistent agent notes (auto-expire 30 days) |
| `last_interaction.json` | Last activity per user; used for idle detection |

Run logs: `Platform.vaf_dir() / "thinking_mode_logs" / <scope_key> / <run_id>_<ts>.json`
Debug log: `logs/vaf_denk.log` (human-readable, all users in one file)

---

## Relevant code

| Component | File | Key functions |
|-----------|------|---------------|
| Loop & run | `vaf/core/thinking_mode.py` | `start_thinking_mode_background()`, `thinking_loop_iteration()`, `maybe_start_thinking_for_user()`, `_run_thinking_for_user()` |
| Scope / key | `vaf/core/thinking_mode.py` | `_key()`, `get_idle_user_scope_ids()` |
| Cooldown | `vaf/core/thinking_mode.py` | `_last_completed_path()`, `_set_last_run_completed()`, `_minutes_since_last_run()` |
| Declined questions | `vaf/core/thinking_mode.py` | `_declined_path()`, `_save_declined_entry()`, `_is_refusal()`, `_get_declined_questions_prompt()` |
| Waiting for reply | `vaf/core/thinking_mode.py` | `set_waiting_for_reply()`, `clear_waiting_for_reply()`, `get_waiting_for_reply()` |
| Persistent notes | `vaf/core/thinking_notes.py` | `add_note()`, `get_notes()`, `build_notes_prompt()` |
| `thinking_done` tool | `vaf/tools/thinking_done.py` | `ThinkingDoneTool` |
| `thinking_note_add` tool | `vaf/tools/thinking_note_add.py` | `ThinkingNoteAddTool` |
| Tool loading | `vaf/core/agent.py` | `_load_tools()` — thinking-mode-only tools gated by `VAF_THINKING_MODE=1` |
| Debug log | `vaf/core/log_helper.py` | `log_thinking_run()` → `logs/vaf_denk_YYYY-MM-DD.log` |
| Session context | `vaf/core/agent.py` | `load_session_context()` |
| GC | `vaf/core/garbage_collector.py` | `_clean_old_thinking_sessions()`; dated log files deleted by date in filename (older than gc_max_age_hours) |

---

## See also

- **AUTOMATIONS.md** — Scheduled automations and todos that thinking mode can create/modify
- **MEMORY_SYSTEM.md** — RAG memory that thinking mode can search (read-only)
- **TELEGRAM_INTEGRATION.md**, **WHATSAPP_INTEGRATION.md** — Channels used for agent messages and nudges
