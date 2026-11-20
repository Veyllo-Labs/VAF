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

**One decision at a time, decided once.** Every approve and reject of an account holds the account's decision lock, `handoff.lock` at the workspace root (`_decision_lock`). It is a kernel lock (`flock`, or `msvcrt.locking` on Windows, through `filelock`), which the operating system releases when its holder dies, so a crashed process never blocks the next decision, and two connections, the tray's two listeners or a second process cannot both pass the pending check. A caller that waits longer than five seconds (`_DECISION_LOCK_TIMEOUT_S`) is answered "busy"; a decision itself takes milliseconds. An id that is not one entry is answered "not found" before any wait, decided without touching the filesystem. A lock file that cannot be opened (a permission error), or a workspace that cannot be set up, answers "not saved", since the handoff may exist. Without the `filelock` package each workspace gets an in-process lock of its own (the machine owner's aliases share one), so one account's decision lock never makes another account wait for it.

An approval with an `automation_action` is claimed before the action runs: the pending record gets a `claim` (for a `create`, with the 8-hex id the new automation will carry) and is saved, then the action runs, then the claim is replaced by `approved`. A crash or a failed save in between leaves the claim, and the next approve finishes from it: a create whose task already exists is reported and not created again, an update is applied again with the same values. The planned task is looked up first, before the type check below, so a create that already ran with a value an older version accepted is approved with its task, not refused afterwards. That lookup reads the task file strictly (`scoped_task_record`): a file that exists but cannot be read or parsed answers "not saved" and keeps the claim, where the loaders would skip it and a decision would take it for missing. A reject of a claimed handoff wins while the approval has not taken effect (the planned automation does not exist yet, or the action never ran): it is stored as `rejected` and announced like any reject. Once the approval may have taken effect (its planned automation exists, or it is an update), the reject finishes the approval instead, answers "already approved" with the result and sends the approval's notification, so the account learns what was created; it never stores `rejected` over an automation that exists. A create the reject found on disk is finished from that observation and never created again, also when the task is deleted in the same moment. A claim whose id is not 8 hex characters is not trusted as a file name and is replaced.

An action whose fields have the wrong type (a number as the prompt, a list as the name) is refused by `create_handoff`, so the model's `thinking_workspace_handoff` tool answers with the reason. A record that already carries one is approved with the result `invalid automation action` and is never claimed, like the action's own refusals (`create requires prompt`). Any other error the action raises after its claim, other than the automation store's `OSError`, ends the decision too, in one of two ways: when the create stored its planned task before the error, the handoff is approved with that task (`ok: true`, its `task_id`), so look for it rather than proposing it again; otherwise with `invalid automation action`. No error of the action itself can keep a handoff pending for every later approve and reject. An automation store that stays unwritable does keep a claimed approval pending: every approve, and every reject of a claimed update (which applies it), answers "not saved" until the store can be written again. The schedule fields of an action are read like every other lane's (`coerce_schedule_fields` in `vaf/core/automation.py`): a `day` sent as text ("15") is the number, `enabled` sent as text is folded and parsed ("false", "nein", "aus", a full-width "false" are off), a word that says neither on nor off is refused by `create_handoff` and reads as off anywhere else, a weekday is lower-cased.

Every handoff save encodes first and then writes a temp file and replaces (`write_json_atomic`), so a value that does not encode leaves the old file as it was. A lone surrogate in a reject reason is stored as `?`.

Named boundaries: without the `filelock` package only the threads of one process are serialized. The WebSocket runs each decision on its own pool of two threads (`_handoff_decision_pool`), never on the event loop and never on the loop's default executor that speech-to-text shares. A worker is held only for one attempt that does not wait for the decision lock (it may wait for the automation store's lock, `.automations.lock`, which its holders keep for well under a millisecond; after five seconds the decision answers "not saved" and keeps its claim, so the next approve finishes it); the wait for another decision's lock (up to five seconds, counted from the first attempt) happens on the event loop between attempts (`_decide_handoff`), and an account has at most one decision queued or running, so a second one answers "being decided" at once. A locked or busy account therefore never holds up another account's decision or another socket's commands. What remains: decisions of more than two accounts at the same instant queue for the length of the decisions ahead of them, which takes milliseconds (with a very large automation store, up to about a second each), never a wait for another decision's lock. An approved update changes only its own fields, on the automation's current record under the automation store's lock (see [AUTOMATIONS.md](../platform/AUTOMATIONS.md)), so an edit of another field saved over another connection in the same moment stays; an edit of the same field stands if it is saved after the approval. The decision lock is always taken before the store lock, never after, and the store lock is released before the scheduler is refreshed. Whether a claimed update already ran cannot be told from the store, so a reject of a claimed update finishes it. A retried update overwrites a manual edit of that automation made between the crash and the retry; a created task deleted in between is created again, because that is what the approval asked for; another task created in the same account with the same random id in between would be taken for the planned one (a 2^-32 chance).

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

All operations are scoped to the current connection user. `task_id` and `handoff_id` arrive from the client, so each must be a single entry name (`safe_entry_name`) of at most 128 UTF-8 bytes; one that is not, such as a walk out of the workspace, or one longer than the id limit, is answered exactly like a missing handoff, and nothing is read, written or archived. Only a `pending` handoff is decided: approving or rejecting one decided before answers `handoff already decided` and applies nothing again, also when two connections decide at the same instant (the decision lock above). A decision that waited past the lock timeout answers `handoff is being decided`; one that could not be made or saved (the lock file or the handoff could not be written, or the automation write failed) answers `handoff decision not saved`, and a claimed approval is then finished by the next approve, or given up by a reject while it has not taken effect. A reject that finished an approval in effect answers `handoff already approved` with its `automation_action_result` and sends the approval's log line and notification. A refused decision sends only the `thinking_workspace_handoff_result` frame: no log line and no notification. After a decision is saved, the archive copy, task status, event and notification are best effort: a failure there is logged and the reply still reports the decision. The workspace tools answer an operating-system error with its reason only (`vaf.describe_error`), never with the server path it quotes. File paths inside a task's workspace go through `contained_path`. An approved `automation_action` updates only an automation the account sees (`AutomationAccess`) and is refused for a caller without an account.

