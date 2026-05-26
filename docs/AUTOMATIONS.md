# Automations

VAF supports **scheduled automations**: the agent runs a prompt on a schedule (once, daily, weekly, monthly, or hourly). Automations are **VAF-internal** (stored and executed by VAF); they are separate from external calendars (Google/Microsoft). See [CALENDAR_INTEGRATION.md](CALENDAR_INTEGRATION.md) for external calendar tools.

## Overview

- **Scheduled tasks:** Create automations with a repeat rule, time, and a detailed prompt. The agent executes the prompt at the scheduled time. Run times use HH:MM; the system enforces a minimum 10-minute interval between any two automations and returns an error if a new or updated time is too close to an existing one.
- **Per-user:** Automations are scoped by `user_scope_id`; each user sees and runs only their own tasks. Tasks are stored under `Platform.vaf_dir() / "automations" / <user_scope_id> /` (one `.json` file per task). 
- **Admin Access:** Users with the `admin` role (including the local admin) have an **aggregated view**: they see their own scoped tasks, legacy "root" tasks in `automations/`, and tasks under `local_admin_scope` (e.g. Daily calendar check). Admin detection uses `role == "admin"` or scope match to `local_admin_scope`; this ensures system automations appear even when the admin's JWT has a different `user_scope_id`. Regular users see only their own scoped directory.
- **Tools and Manager:** The agent's automation tools (`create_automation`, `list_automations`, etc.) use a scoped `AutomationManager`. When an admin lists tasks, the backend merges scoped and root tasks automatically.
- **Execution isolation:** Every automation run (prompt-based or workflow-based) uses the **task owner's** `user_scope_id`. The agent and workflow engine are set to that scope before execution, so RAG/memory, calendar, messaging (`send_telegram`, `send_whatsapp`, etc.), contacts, mail, and automation notes/todos all use the owner's data and credentials. Reminders created by e.g. the Daily calendar check therefore run in the same user context.
- **CLI and scheduler:** `vaf automation list`, `vaf automation run <id>`, and `vaf automation start` use an aggregated view: the global manager loads tasks from `automations/` and from every `automations/<uuid>/` subdir. All automations are listed and scheduled; save, delete, and restore write to the task's scope path (`_path_for_task`).
- **Web UI:** **Settings → Automations** lists and manages automations. The **Automation** button in the main footer opens the automation calendar: pick month, day, and hour slot to create a new automation (repeat, time, prompt, optional name). Creation is sent via WebSocket (`create_automation`); the list refreshes on success.
- **Agent tool:** The agent can create (and manage) automations via the `create_automation` tool in chat. The Tool Router adds `create_automation` when the user message contains words like "automate", "schedule", "daily", "weekly" (see [TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md)).
- **Result delivery:** After an automation completes, the result is pushed as a chat message (✅/❌ + summary) to the user's latest web session. If a main messenger (Telegram, WhatsApp, Discord) is configured, the messenger handles delivery instead of the Web UI. If no web session exists, a new one is created.
- **Model for execution:** Automations run with `VAF_IN_AUTOMATION=1`, which causes `deepseek-auto` to resolve to the pro model (same as workflows). This ensures tool-heavy automations use the most capable model.
- **Thinking Workspace bridge (MVP):** automation lifecycle is mirrored into per-user thinking workspace tasks (`source=automation:<id>`). Run status (`success/error`), last/next run, `last_completed_local_date` (when present), and enabled state are synced so Thinking Mode can reason over current automation health. Approved workspace handoffs can optionally trigger `create`/`update` automation actions (approval-gated).

## Today status, persisted completion, and catch-up runs

- **Task JSON fields:** On each **successful** run, the task file stores `last_run` (ISO timestamp) and **`last_completed_local_date`** (`YYYY-MM-DD` in the **host’s local** calendar). The latter is the source of truth for “already finished today” and **survives tray/VAF restarts** until the local date rolls over (e.g. automation at 06:00 completes → still **Done (today)** after a restart at 11:00; the next day it is no longer “today” and the status follows schedule vs clock again).
- **Older tasks:** Files without `last_completed_local_date` still work: tools fall back to the calendar date inferred from `last_run`.
- **Agent tools:** `list_automations` and `read_automation` include a **Today (local)** line for the model: **Done (today)**, **Scheduled (later today)**, **Due (not yet run today)**, or **In progress** (automation lock held). `read_automation` also shows **Last completed (local date)** when set.
- **Immediate run after `create_automation`:** If the chosen clock time has **already passed today**, a daily automation is normally started **once** right after creation. That **immediate catch-up is skipped** when another **enabled daily** automation in the same **family** already completed today: same name (case-insensitive), or both names match a small briefing-style heuristic (e.g. “briefing”, “Morgenbrief”, “morning brief”). The tool response includes **Same-day catch-up skipped** with a short reason so duplicate morning jobs do not run twice the same calendar day.
- **Web UI:** The automations list in Settings still centers on schedule and `last_run`; the explicit **today** wording above is primarily surfaced through **agent tools** until the UI is extended to show the same labels.

## create_automation tool — parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | yes | Short identifier (e.g. `daily_news`) |
| `prompt` | yes | Full task prompt the agent executes |
| `frequency` | yes | **Must be explicitly confirmed with the user.** One of: `once`, `hourly`, `daily`, `weekly`, `monthly`. Default is `once` (single run, deleted after execution). The agent must never assume `daily` or any other frequency. |
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

Planner data is loaded when the calendar is opened (footer or Settings). Create/update/delete use WebSocket messages; the agent can manage the same data via tools: `add_automation_note`, `add_automation_todo`, `list_automation_notes`, `list_automation_todos`, `delete_automation_note`, `delete_automation_todo`. See [WEBUI_WEBSOCKET_FLOW.md](WEBUI_WEBSOCKET_FLOW.md) for message formats.

## Related

- [WEB_UI.md](WEB_UI.md) – Settings → Automations and automation calendar UI.
- [USER_ISOLATION.md](USER_ISOLATION.md) – Per-user automations and planner storage.
- [WEBUI_WEBSOCKET_FLOW.md](WEBUI_WEBSOCKET_FLOW.md) – WebSocket messages for automations and planner.
- [TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md) – When `create_automation` is forced by the router.
