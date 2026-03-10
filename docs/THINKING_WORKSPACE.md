# Thinking Workspace (MVP)

The Thinking Workspace is a per-user virtual desktop for Thinking Mode.  
It gives the agent a structured place to persist work artifacts and prepare approval-required handoffs.

## Goals

- Keep Thinking Mode work auditable and organized.
- Isolate workspace data by `user_scope_id`.
- Enforce safe write behavior (no direct destructive external actions).
- Support a handoff-first review flow.

## Storage and isolation

Workspace root per user:

`Platform.data_dir() / "workspaces" / <scope_key>/`

`scope_key` follows Thinking Mode normalization:
- local admin scope maps to `default`
- other users map to their UUID/string scope id

## Directory layout

```
workspaces/<scope_key>/
├── inbox/
├── tasks/
│   └── <task_id>/
│       ├── workspace/
│       ├── handoff/
│       │   ├── <handoff_id>.json
│       │   └── <handoff_id>.md
│       ├── meta/
│       │   └── task.json
│       └── events.log
├── archive/
│   ├── approved/
│   └── rejected/
└── trash/
```

## Task model (`task.json`)

`task.json` fields include:
- `id`, `title`, `source`, `description`
- `status`: `open | pending_approval | approved | rejected | archived`
- `created_at`, `updated_at`
- `policy`:
  - `allow_write` (default `true`)
  - `requires_approval` (default `true`)
  - `allow_external_send` (default `false`)

## Handoff lifecycle

1. Agent writes drafts/artifacts under `tasks/<task_id>/workspace/`.
2. Agent creates a handoff proposal under `tasks/<task_id>/handoff/`.
3. Handoff status starts as `pending`.
4. User/system approves or rejects:
   - Approve: copies handoff files to `archive/approved/<task_id>/`, task -> `approved`
   - Reject: copies handoff files to `archive/rejected/<task_id>/`, task -> `rejected`

If a handoff includes `automation_action`, approval can trigger:
- `create` automation
- `update` existing automation

The result is stored in the handoff JSON as `automation_action_result`.

## Thinking Mode integration (MVP)

- At run start, Thinking Mode loads:
  - existing task candidates (automation todos/notes, thinking notes)
  - open workspace tasks
- At run end, Thinking Mode saves:
  - run artifact (`run_summary.md`) into a new workspace task
  - a pending handoff proposal with the run summary

This keeps each background run reviewable without directly applying external actions.

## Automation bridges

The workspace is bridged with the automation system in three ways:

1. **Run result mirror (Bridge A):** automation runs mirror status and summary into workspace task metadata/events.
2. **Approve -> automation action (Bridge B):** approved handoffs may trigger `create` or `update` automation actions when `automation_action` is present.
3. **Lifecycle state mirror (Bridge C):** automation create/update/delete/run updates are reflected in workspace task metadata (`automation.*`) so Thinking Mode can reason over live state.

## Working memory bridge

`update_working_memory` remains the fast operational scratchpad tool.  
In Thinking Mode, each update also mirrors the latest snapshot into Thinking Workspace:

- `tasks/<task_id>/workspace/working_memory/latest.json`
- `tasks/<task_id>/workspace/working_memory/history/<timestamp>.json`

This creates an auditable bridge between short-term planning (`working_memory.json`) and persistent workspace artifacts.

## Tools (Thinking Mode only)

- `thinking_workspace_read`
- `thinking_workspace_write`
- `thinking_workspace_handoff`

These tools are loaded only when `VAF_THINKING_MODE=1` and are scope-injected by the agent.

## WebSocket review endpoints (MVP)

- `get_thinking_workspace_handoffs` -> `thinking_workspace_handoffs_list`
- `approve_thinking_workspace_handoff` -> `thinking_workspace_handoff_result`
- `reject_thinking_workspace_handoff` -> `thinking_workspace_handoff_result`

All operations are scoped to the current connection user.

