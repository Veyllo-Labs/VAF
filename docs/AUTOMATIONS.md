# Automations

VAF supports **scheduled automations**: the agent runs a prompt on a schedule (once, daily, weekly, monthly, or hourly). Automations are **VAF-internal** (stored and executed by VAF); they are separate from external calendars (Google/Microsoft). See [CALENDAR_INTEGRATION.md](CALENDAR_INTEGRATION.md) for external calendar tools.

## Overview

- **Scheduled tasks:** Create automations with a repeat rule, time, and a detailed prompt. The agent executes the prompt at the scheduled time.
- **Per-user:** Automations are scoped by `user_scope_id`; each user sees and runs only their own tasks. Stored under `vaf/core/automation.py` (per-user task directories).
- **Web UI:** **Settings → Automations** lists and manages automations. The **Automation** button in the main footer opens the automation calendar: pick month, day, and hour slot to create a new automation (repeat, time, prompt, optional name). Creation is sent via WebSocket (`create_automation`); the list refreshes on success.
- **Agent tool:** The agent can create (and manage) automations via the `create_automation` tool in chat. The Tool Router adds `create_automation` when the user message contains words like "automate", "schedule", "daily", "weekly" (see [TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md)).

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
