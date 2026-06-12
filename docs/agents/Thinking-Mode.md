# Thinking Mode

Thinking mode runs the main agent in the background while the user is idle. It acts on the user's behalf: processes todos, creates automations, sends proactive messages, and can ask the user a question via their configured `main_messenger` (Telegram, WhatsApp, Discord, Slack) — or, when no messenger is configured, as a plain-text question delivered to the user's Web UI chat (e-mail is never used). Runs are multi-turn until the agent calls `thinking_done` (or a turn limit is reached).

---

## Overview

- **When it runs:** After `thinking_idle_minutes` of no activity across ALL linked channels (Web UI, Telegram, WhatsApp, etc.).
- **What it does:** One run = multiple agent turns with full tool access (except `memory_save` and Git tools). The agent must call `thinking_done` when finished.
- **Max 1 Message & History Sync:** The agent may send at most one message per run. **New:** Any question asked by the Thinking Agent is automatically persisted to the user's main chat history. This ensures that when the user replies, the normal agent has the full context of what was asked.
- **Outbound channel guard:** before the run starts, `_filter_thinking_send_tools()` removes every `send_*` tool that does not match the user's configured `main_messenger` (User Identity). Without a configured messenger ALL send tools are removed — the agent writes its question as plain reply text, which the Web UI fallback (`_maybe_emit_web_question`) delivers to the user's latest web session. `send_mail` is never available in a background run: e-mail is not a `main_messenger` value, and an unguarded run once tried to mail a hallucinated address (`mert@example.com`). The prompt carries the same rule, the registry filter enforces it.
- **Per user:** Idle is tracked per logical user (handling all UUID/username aliases). One run at a time per user (serialized by lock). Cooldown between runs: `thinking_cooldown_minutes` (default 60 min).
- **Dead-session cap:** A non-admin scope ID that has been silent longer than `thinking_max_idle_age_hours` (default 7 days) is treated as a dead/orphan session, not an idle user, and is skipped. Without this, stale web-session scope IDs left in `last_interaction.json` are each seen as a separate idle user and generate a phantom run every cooldown window indefinitely. The local admin is exempt so a genuinely long-away admin still runs.
- **Safety Abort:** If the user becomes active on any channel during a run, the thinking process is immediately aborted to prevent dual-agent responses.
- **Workflow Guard:** Thinking mode does not start while a workflow is executing (`VAF_IN_WORKFLOW_TERMINAL=1`). This prevents idle messages from interrupting long-running workflow steps.
- **Local-server Guard:** When the background run would share the **same local model** as the main chat (main `provider=local` and `thinking_provider` is `inherit` or `local`), a run does **not** start while the main agent is busy on that server (`TaskQueue` in-flight or queued). Idle-by-last-message is not enough: the main agent may still be mid-task from an older message, so its activity is treated as 'not idle' and the background run never contends with the user for the one local model. If the background run uses a **different** provider (e.g. thinking via API while the main chat is local, or vice versa) there is no contention and it runs concurrently as before.
- **Locking:** Uses a global file-based lock system with PID verification to prevent parallel runs. See [Singleton Task Locking in PROCESS_MANAGEMENT.md](../setup/PROCESS_MANAGEMENT.md#singleton-task-locking).
- **Context:** The agent loads the user's full chat session — it has the same context as the normal agent.
- **Output:** Runs are logged to `logs/vaf_think_YYYY-MM-DD.log` (human-readable) and to JSON run logs. Messages sent to the user are also mirrored in the Web UI / main chat history.
- **Workspace (MVP):** Runs can persist artifacts to a per-user Thinking Workspace (`Platform.data_dir()/workspaces/<scope_key>/`). Externally visible actions should be prepared as **handoffs** for approval first.
- **Working memory bridge:** `update_working_memory` is still the fast scratchpad. In Thinking Mode, updates are mirrored to workspace snapshots (`working_memory/latest.json` + timestamped history) for auditability.

---

## Configuration

Key options (in `config.json` or via Web UI **Settings → Advanced → Thinker**):

| Key | Default | Purpose |
|-----|---------|---------|
| `thinking_enabled` | `true` | Enable thinking mode when idle |
| `thinking_idle_minutes` | `10` | Start after this many minutes without activity |
| `thinking_max_idle_age_hours` | `168` | Upper bound on idle age (default 7 days). A non-admin scope silent longer than this is treated as a dead/orphan session and never runs. `0` disables the cap. |
| `thinking_check_interval_seconds` | `60` | How often to check for idle users |
| `thinking_cooldown_minutes` | `60` | Minutes to wait after a run before starting another |
| `thinking_max_duration_minutes` | `30` | Max duration per run (then release lock) |
| `thinking_wait_nudge_minutes` | `3` | If user does not reply: send nudge after this many minutes |
| `thinking_wait_skip_minutes` | `10` | If still no reply: clear waiting state after this many minutes |
| `thinking_nudge_activity_minutes` | `5` | Do not nudge if user was active on any channel in the last N minutes |
| `thinking_provider` | `"inherit"` | AI provider for thinking mode (`inherit` = same as main chat, or `openai`, `anthropic`, `deepseek`, `local`) |
| `thinking_model` | `null` | Specific model for thinking mode (empty = use provider default) |
| `thinking_quiet_hours_enabled` | `false` | Do not run during quiet hours (local time) |
| `thinking_quiet_hours_start` / `_end` | `"23:00"` / `"07:00"` | Quiet period (HH:MM, 24h); overnight span supported |
| `thinking_startup_grace_seconds` | `300` | Seconds to skip thinking-mode checks after VAF starts. Prevents idle triggers immediately on startup. |

**Cost efficiency:** Set `thinking_provider` and optionally `thinking_model` to use a cheaper model for background runs (e.g. a small local model or a low-cost API tier) while keeping the main chat on a more capable model. Configurable in the Web UI under **Settings → Advanced → Thinker (background)**.

---

## Interruption persistence

When a run is aborted because the user became active (e.g. sent a message), the process does not simply stop. A short summary of the current run state (last tools used, last assistant message) is saved via `thinking_note_add` (e.g. *"Run unterbrochen (Turn 2). Letzte Tools: list_automations. Letzter Gedanke: …"*). The next run receives this note so the agent can continue from context instead of starting from scratch.

---

## Intel gathering (pre-computation)

The thinking-mode prompt allows the agent to perform **at most one** targeted web search per run when the conversation history shows a clear topic (e.g. an important package or event). The agent may call `web_search` and save the result with `thinking_note_add` so that an answer is ready before the user asks again. This is constrained to one search per run to limit cost and noise.

---

## Proactive profile evolution (`save_thinking_suggestion`)

The agent can call **`save_thinking_suggestion`** (thinking-mode only) to propose updates to the user profile (e.g. *"User cares about package tracking"*). Suggestions are stored per user and presented in Settings for review; the agent does not overwrite identity or preferences without the user approving. See `vaf/tools/thinking_suggestion.py` and `vaf/core/thinking_suggestions.py`.

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

## Loop protection (API cost safety)

To prevent runaway API usage (e.g. the model repeatedly calling `thinking_done` or the same tool with the same arguments), the following safeguards apply:

- **`thinking_done` hard break:** When the model calls `thinking_done`, the agent’s internal tool loop exits immediately. No further API request is made for that turn; the tool result is written to history and the run ends. Implemented in `vaf/core/agent.py` (chat_step tool loop).
- **Max tool turns per step:** A single user turn (or one thinking-mode turn) is limited to **15** tool-result cycles. If the model keeps calling tools without producing a final answer, the run stops after the 15th cycle and a clear message is returned. Enforced in the same chat_step tool loop in `vaf/core/agent.py`.
- **Redundant tool call block:** If the model calls the same tool again with the same arguments (already executed in context), that call is blocked and the internal retry counter is incremented so the run can hit the empty/fallback stop logic sooner.
- **Logging:** When any of these triggers, `[LOOP_PROTECTION]` is written to `logs/backend_YYYY-MM-DD.log` (and visible in run summaries). Examples: `thinking_done detected - breaking loop`, `Exceeded 15 tool turns`, `blocked redundant tool call`.

These apply to both normal chat and thinking mode. Run logs in `logs/vaf_think_YYYY-MM-DD.log` remain the main place to inspect thinking runs.

---

## Tools available in Thinking Mode

The agent has **the same tools as the normal agent**, with these exceptions:

| Tool | Status | Reason |
|------|--------|--------|
| `thinking_done` | **Only in Thinking Mode** | Signals end of the run |
| `thinking_note_add` | **Only in Thinking Mode** | Saves persistent notes for next run |
| `save_thinking_suggestion` | **Only in Thinking Mode** | Proposes user-profile updates for review in Settings |
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
- **Nudge:** After `thinking_wait_nudge_minutes`, a short "Hey, bist du da?" is sent.
  - **Inactivity Protection:** No nudge is sent if the user was active on ANY channel within the last `thinking_nudge_activity_minutes` (default 5 min).
- **Skip:** After `thinking_wait_skip_minutes`, the waiting state is cleared
- **User replies:** When the user next sends a message, `clear_waiting_for_reply(user_reply_text=...)` is called.

### Automatic Cleanup ("Nudge Killer")

To ensure the background agent doesn't keep waiting (and nudging) while the user is already interacting with the Main Agent, the "waiting for reply" state is cleared automatically:

1. **Centralized Sync (`vaf/core/agent.py`):** In `chat_step()`, the state is cleared with the full `user_reply_text`. This covers all input channels (Web, CLI, Messenger). 
   - **Context Injection:** If the user was being waited on, the Main Agent receives a context hint explaining which background question the user is likely responding to. This prevents "I'm not sure what you mean" replies to short answers like "Yes, why?".
2. **Early Cleanup:** To avoid race conditions where a nudge might be triggered while a message is being processed, both the **Web Server** and **Headless Runner** attempt to clear the state as soon as a message is received.

The reply is:
- Injected as "User reply to your last question" in the next run's system prompt
- If it is a refusal: saved to the declined questions log

---

## Output: logs only, not in Web UI

Thinking mode output is **not shown in the Web UI chat list**. It is logged to:

| Location | Format | Purpose |
|----------|--------|---------|
| `logs/vaf_think_YYYY-MM-DD.log` | Human-readable text blocks | Debugging — readable with any text editor. Cleaned by GC after `gc_max_age_hours`. |
| `~/.vaf/thinking_mode_logs/<scope_key>/<run_id>_<ts>.json` | JSON | Internal — used by `_get_last_thinking_summary()` for context injection. Cleaned by GC after `gc_max_age_hours`. |

**`vaf_think_YYYY-MM-DD.log` format:**
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
| `thinking_suggestions/` | Per-user directory for profile suggestions from `save_thinking_suggestion` (review in Settings) |
| `workspaces/<scope_key>/` | Per-user Thinking Workspace (tasks, workspace files, handoffs, archives) |
| `last_interaction.json` | Last activity per user; used for idle detection |

Run logs: `Platform.vaf_dir() / "thinking_mode_logs" / <scope_key> / <run_id>_<ts>.json`
Debug log: `logs/vaf_think_YYYY-MM-DD.log` (human-readable, all users in one file)

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
| `save_thinking_suggestion` tool | `vaf/tools/thinking_suggestion.py` | Proposes profile updates; stored via `vaf/core/thinking_suggestions.py` |
| Thinking workspace core | `vaf/core/thinking_workspace.py` | `create_task()`, `write_workspace_file()`, `create_handoff()`, `approve_handoff()`, `reject_handoff()` |
| Thinking workspace tools | `vaf/tools/thinking_workspace_*.py` | Read/write/handoff operations (Thinking Mode only) |
| Tool loading | `vaf/core/agent.py` | `_load_tools()` — thinking-mode-only tools gated by `VAF_THINKING_MODE=1` |
| Loop protection | `vaf/core/agent.py` | `chat_step()` — `thinking_done` hard break, max 15 tool turns, redundant call block; see [Loop protection (API cost safety)](#loop-protection-api-cost-safety) |
| Debug log | `vaf/core/log_helper.py` | `log_thinking_run()` → `logs/vaf_think_YYYY-MM-DD.log` |
| Session context | `vaf/core/agent.py` | `load_session_context()` |
| GC | `vaf/core/garbage_collector.py` | `_clean_old_thinking_sessions()`; dated log files deleted by date in filename (older than gc_max_age_hours) |

---

## See also

- **AUTOMATIONS.md** — Scheduled automations and todos that thinking mode can create/modify
- **THINKING_WORKSPACE.md** — Per-user virtual desktop, handoff flow, and safety model
- **MEMORY_SYSTEM.md** — RAG memory that thinking mode can search (read-only)
- **TELEGRAM_INTEGRATION.md**, **WHATSAPP_INTEGRATION.md** — Channels used for agent messages and nudges
