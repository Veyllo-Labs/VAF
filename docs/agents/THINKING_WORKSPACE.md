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
├── handoff.lock
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

**One decision at a time, decided once.** Every approve and reject of an account holds the account's decision lock, `handoff.lock` at the workspace root (`_decision_lock`). It is a kernel lock (`flock`, or `msvcrt.locking` on Windows, through `filelock`), which the operating system releases when its holder dies, so a crashed process never blocks the next decision, and two connections, the tray's two listeners or a second process cannot both pass the pending check. A caller that waits longer than five seconds (`_DECISION_LOCK_TIMEOUT_S`) is answered "busy"; a decision itself takes milliseconds.

An approval with an `automation_action` is claimed before the action runs: the pending record gets a `claim` (for a `create`, with the 8-hex id the new automation will carry) and is saved, then the action runs, then the claim is replaced by `approved`. A crash or a failed save in between leaves the claim, and the next approve finishes from it: a create whose task already exists is reported and not created again, an update is applied again with the same values. A reject of a handoff whose approval was claimed finishes that approval and answers "already decided"; it never stores `rejected` over an automation that may already exist. A claim whose id is not 8 hex characters is not trusted as a file name and is replaced.

Every handoff save encodes first and then writes a temp file and replaces (`write_json_atomic`), so a value that does not encode leaves the old file as it was. A lone surrogate in a reject reason is stored as `?`.

Named boundaries: without the `filelock` package only the threads of one process are serialized. A wait for the lock blocks that connection's WebSocket handler for up to five seconds, which happens only while the same account decides on another connection at that moment. A retried update overwrites a manual edit of that automation made between the crash and the retry; a created task deleted in between is created again, because that is what the approval asked for; another task created in the same account with the same random id in between would be taken for the planned one (a 2^-32 chance).

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
Inside a thinking run (the call's own context, `in_thinking_run` in `vaf/core/subagent_ipc.py`), each update also mirrors the latest snapshot into Thinking Workspace:

- `tasks/<task_id>/workspace/working_memory/latest.json`
- `tasks/<task_id>/workspace/working_memory/history/<timestamp>.json`

This creates an auditable bridge between short-term planning (`working_memory.json`) and persistent workspace artifacts.

## Tools (Thinking Mode only)

- `thinking_workspace_read`
- `thinking_workspace_write`
- `thinking_workspace_handoff`

These tools are loaded only on an agent built with `run_kind="thinking"` (`_load_tools` in `vaf/core/agent.py`, the instance truth rather than the process environment) and are scope-injected by the agent.

## WebSocket review endpoints (MVP)

- `get_thinking_workspace_handoffs` -> `thinking_workspace_handoffs_list`
- `approve_thinking_workspace_handoff` -> `thinking_workspace_handoff_result`
- `reject_thinking_workspace_handoff` -> `thinking_workspace_handoff_result`

All operations are scoped to the current connection user. `task_id` and `handoff_id` arrive from the client, so each must be a single entry name (`safe_entry_name`) of at most 128 UTF-8 bytes; one that is not, such as a walk out of the workspace, or one longer than the id limit, is answered exactly like a missing handoff, and nothing is read, written or archived. Only a `pending` handoff is decided: approving or rejecting one decided before answers `handoff already decided` and applies nothing again, also when two connections decide at the same instant (the decision lock above). A decision that waited past the lock timeout answers `handoff is being decided`; one that could not be saved, or whose automation write failed, answers `handoff decision not saved`, and a claimed approval is then finished by the next approve or reject. A refused decision sends only the `thinking_workspace_handoff_result` frame: no log line and no notification. After a decision is saved, the archive copy, task status, event and notification are best effort: a failure there is logged and the reply still reports the decision. The workspace tools answer an operating-system error with its reason only (`describe_os_error`), never with the server path it quotes. File paths inside a task's workspace go through `contained_path`. An approved `automation_action` updates only an automation the account sees (`AutomationAccess`) and is refused for a caller without an account.

