# Automations

VAF supports **scheduled automations**: the agent runs a prompt on a schedule (once, daily, weekly, monthly, or hourly). Automations are **VAF-internal** (stored and executed by VAF); they are separate from external calendars (Google/Microsoft). See [CALENDAR_INTEGRATION.md](../integrations/CALENDAR_INTEGRATION.md) for external calendar tools.

## Overview

- **Scheduled tasks:** Create automations with a repeat rule, time, and a detailed prompt. The agent executes the prompt at the scheduled time. Run times use HH:MM; the system enforces a minimum 10-minute interval between any two automations and returns an error if a new or updated time is too close to an existing one.
- **Per-user:** Automations are scoped by `user_scope_id`; each user sees and runs only their own tasks. Tasks are stored under `Platform.vaf_dir() / "automations" / <user_scope_id> /` (one `.json` file per task). 
- **Admin Access:** Users with the `admin` role (including the local admin) have an **aggregated view**: they see their own scoped tasks, legacy "root" tasks in `automations/`, and tasks under `local_admin_scope` (e.g. Daily calendar check). Admin detection uses `role == "admin"` or scope match to `local_admin_scope`; this ensures system automations appear even when the admin's JWT has a different `user_scope_id`. Regular users see only their own scoped directory.
- **Tools and Manager:** The agent's automation tools (`create_automation`, `list_automations`, etc.) use a scoped `AutomationManager`. When an admin lists tasks, the backend merges scoped and root tasks automatically.
- **Execution isolation:** Every automation run (prompt-based or workflow-based) uses the **task owner's** `user_scope_id`. The agent and workflow engine are set to that scope before execution, so RAG/memory, calendar, messaging (`send_telegram`, `send_whatsapp`, etc.), contacts, mail, and automation notes/todos all use the owner's data and credentials. Reminders created by e.g. the Daily calendar check therefore run in the same user context.
- **CLI and scheduler:** `vaf automation list`, `vaf automation run <id>`, and `vaf automation start` use an aggregated view: the global manager loads tasks from `automations/` and from every `automations/<uuid>/` subdir. All automations are listed and scheduled; save, delete, and restore write to the task's scope path (`_path_for_task`).
- **Web UI:** **Settings → Automations** lists and manages automations. The **Automation** button in the main footer opens the automation calendar: pick month, day, and hour slot to create a new automation (repeat, time, prompt, optional name). Creation is sent via WebSocket (`create_automation`); the list refreshes on success.
- **Agent tool:** The agent can create (and manage) automations via the `create_automation` tool in chat. The Tool Router adds `create_automation` when the user message contains words like "automate", "schedule", "daily", "weekly" (see [TOOL_ROUTER_ARCHITECTURE.md](../agents/TOOL_ROUTER_ARCHITECTURE.md)).
- **Result delivery:** After an automation completes, the result is appended as a **standalone** chat message (status marker + summary) to the user's active web session — it is added as its own bubble, not merged into a previous reply, and if that session is not the one currently open it is flagged unread. If a main messenger (Telegram, WhatsApp, Discord) is configured, the result is also delivered there, in addition to the Web UI. If no web session exists, a new one is created.
- **Model for execution:** Automations run with `VAF_IN_AUTOMATION=1`, which causes `deepseek-auto` to resolve to the pro model (same as workflows). This ensures tool-heavy automations use the most capable model.
- **Thinking Workspace bridge (MVP):** automation lifecycle is mirrored into per-user thinking workspace tasks (`source=automation:<id>`). Run status (`success/error`), last/next run, `last_completed_local_date` (when present), and enabled state are synced so Thinking Mode can reason over current automation health. Approved workspace handoffs can optionally trigger `create`/`update` automation actions (approval-gated).

## Short in-chat timers (`set_timer`) vs automations

For a **short, one-off delay that should fire proactively in the current conversation** (e.g. "in 1 minute say test", "in 90 seconds check the deploy"), the agent uses **`set_timer`**, not `create_automation`. The two cover different needs:

| | `set_timer` | `create_automation` (`frequency='once'`) |
|---|---|---|
| Timing | **Relative delay in seconds** (second-precise) | **Clock time `HH:MM`**, minute granularity, ≥10 min apart |
| Delivery | **Proactive message in the live chat** (CLI + WebUI) | Detached run; result delivered to the active web session and `main_messenger` |
| Persistence | **In-memory, per process — lost on restart** | Persisted to disk; survives restarts |
| Use for | Short timers/reminders that should appear in *this* chat now | Longer/persistent reminders, specific clock times, recurring schedules |

**Tools:** `set_timer` (provide `seconds` plus exactly one of `message` — a short note/reminder — or `task` — a concrete instruction), `list_timers`, and `cancel_timer`. `set_timer` is part of the agent's always-available core tools.

**On fire, the agent is WOKEN UP** and runs a real turn (it reads the note/task, can think and call tools, and replies) — a self-wakeup, not a passive text post. A `message` timer feeds the note in as the turn input; a `task` timer feeds the instruction.

**Delivery mechanism:** on fire, the timer enqueues an `AgentTask` (with `metadata.timer`) into the same process's `TaskQueue` — the queue the CLI input loop and the headless worker already consume. Two details make it work in the Web UI:
- **Session:** the timer attaches to the live chat session (`current_session_id`, resolved by `_resolve_session`), NOT the agent instance's random per-process `_session_id` (a `uuid4` from `_register_session`) — that earlier delivered the fire into a session the Web UI never listened on, so nothing showed.
- **Own bubble:** a timer turn has no preceding user message, so before the turn the headless emits the trigger as a **wake message** (`kind="timer"`) that the Web UI renders in its own left-side area (see [WEB_UI.md](../web-ui/WEB_UI.md) → "Wake / system-activity messages"). That both shows the trigger and creates a bubble boundary, so the agent's reply lands in its own new bubble with a correct timestamp.

The older passive `__TIMER__:` proactive-delivery path still exists (it now appends rather than overwrites) but is currently unused. `set_timer` is blocked on messaging channels (Telegram/WhatsApp/Discord) — use `create_automation` there.

**Code:** `vaf/core/timers.py` (store + scheduler + the `_fire` wake-turn framing), `vaf/tools/timer.py` (the tools + `_resolve_session`), `vaf/core/headless_runner.py` (the `kind="timer"` wake emit before the turn), `web/app/page.tsx` (the `kind`-based wake card).

## Today status, persisted completion, and catch-up runs

- **Task JSON fields:** On each **successful** run, the task file stores `last_run` (ISO timestamp) and **`last_completed_local_date`** (`YYYY-MM-DD` in the **host’s local** calendar). The latter is the source of truth for “already finished today” and **survives tray/VAF restarts** until the local date rolls over (e.g. automation at 06:00 completes → still **Done (today)** after a restart at 11:00; the next day it is no longer “today” and the status follows schedule vs clock again).
- **Older tasks:** Files without `last_completed_local_date` still work: tools fall back to the calendar date inferred from `last_run`.
- **Agent tools:** `list_automations` and `read_automation` include a **Today (local)** line for the model: **Done (today)**, **Scheduled (later today)**, **Due (not yet run today)**, or **In progress** (automation lock held). `read_automation` also shows **Last completed (local date)** when set.
- **One-time (`once`) lifecycle:** A `once` automation fires exactly one time and is then removed — whether the run **succeeded or failed** — so it never recurs (a failed one-time run is not silently retried the next day). The scheduler additionally refuses to re-arm a one-time task that has already run, which guards against a stale task file being reloaded after a restart or scheduler refresh. Only successful runs stamp `last_run` / `last_completed_local_date` before removal.
- **Immediate run after `create_automation`:** If the chosen clock time has **already passed today**, a daily automation is normally started **once** right after creation. That **immediate catch-up is skipped** when another **enabled daily** automation in the same **family** already completed today: same name (case-insensitive), or both names match a small briefing-style heuristic (e.g. “briefing”, “Morgenbrief”, “morning brief”). The tool response includes **Same-day catch-up skipped** with a short reason so duplicate morning jobs do not run twice the same calendar day.
- **Web UI:** The automations list in Settings still centers on schedule and `last_run`; the explicit **today** wording above is primarily surfaced through **agent tools** until the UI is extended to show the same labels.

## create_automation tool — parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | yes | Short identifier (e.g. `daily_news`) |
| `prompt` | yes | Full task prompt the agent executes |
| `frequency` | yes | **Must be explicitly confirmed with the user.** One of: `once`, `hourly`, `daily`, `weekly`, `monthly`. Default is `once` (runs a single time, then auto-removed — it never repeats; see *One-time lifecycle* below). The agent must never assume `daily` or any other frequency. |
| `time` | yes | HH:MM format. Must be confirmed with the user. Minimum 10 min gap from any existing automation. |
| `weekday` | for `weekly` | e.g. `monday` |
| `day` | for `monthly` | Day of month (1–31) |
| `output_path` | no | Save location (default: `Documents`) |
| `parameters` | no | Extra context: `city`, `category`, etc. |
| `max_retries` | no | Times to retry on failure (0–5). Only set if user explicitly requests retry behaviour. |
| `retry_delay_minutes` | no | Minutes between retries. Only relevant with `max_retries > 0`. |

**Frequency rule (important):** The agent errors if an unknown frequency is passed. It never silently defaults to `daily`. The tool enforces: if not one of the five valid values, return an error and ask the user to specify.

## Automation planner (notes and to-dos)

The same automation calendar UI includes a **per-user planner**:

- **To-do list:** Add items (text, optional due date); check off or delete. Data in `automation_planner/<user_scope_id>/todos.json`.
- **Notes:** Add notes (title, content); delete when done. Data in `automation_planner/<user_scope_id>/notes.json`.

Planner data is loaded when the calendar is opened (footer or Settings). Create/update/delete use WebSocket messages; the agent can manage the same data via tools: `add_automation_note`, `add_automation_todo`, `list_automation_notes`, `list_automation_todos`, `delete_automation_note`, `delete_automation_todo`. See [WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md) for message formats.

## Related

- [WEB_UI.md](../web-ui/WEB_UI.md) – Settings → Automations and automation calendar UI.
- [USER_ISOLATION.md](../security/USER_ISOLATION.md) – Per-user automations and planner storage.
- [WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md) – WebSocket messages for automations and planner.
- [TOOL_ROUTER_ARCHITECTURE.md](../agents/TOOL_ROUTER_ARCHITECTURE.md) – When `create_automation` is forced by the router.
