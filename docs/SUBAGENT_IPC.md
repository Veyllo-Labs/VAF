# VAF Sub-Agent IPC System

## Overview

The **Inter-Process Communication (IPC)** system enables communication between the Main Agent and Sub-Agents (e.g. `librarian_agent`, `research_agent`, `document_agent`) that run as **separate processes**. Results flow back through a file-based task queue. A sub-agent only opens its own *terminal window* in CLI mode — see **Execution modes** below for how it actually runs in the WebUI/desktop app and inside workflows.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         VAF Sub-Agent Architecture                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│    ┌──────────────┐                              ┌──────────────┐           │
│    │              │         IPC Queue            │              │           │
│    │  Main Agent  │◄─────────────────────────────│  Sub-Agent   │           │
│    │   (Terminal  │                              │  (Separate   │           │
│    │    Window)   │─────────────────────────────►│   Terminal)  │           │
│    │              │       Task Creation          │              │           │
│    └──────────────┘                              └──────────────┘           │
│           │                                             │                   │
│           │                                             │                   │
│           ▼                                             ▼                   │
│    ┌──────────────┐                              ┌──────────────┐           │
│    │   User       │                              │   LLM        │           │
│    │   Interface  │                              │   Server     │           │
│    └──────────────┘                              └──────────────┘           │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Cross-Platform Support:** Works on Windows, Linux, and macOS.

### Execution modes (important)

How a sub-agent actually runs depends on the context. The IPC queue described in this
document is used in the first two (separate-process) modes:

| Mode | How the sub-agent runs | Blocks the main turn? |
|------|------------------------|-----------------------|
| **CLI** (terminal session) | a new **terminal window** running `vaf subagent run …` | No — result picked up via IPC on the next turn |
| **WebUI / desktop app** | a **piped child process** (no visible terminal); the parent drains its stdout (`stderr` is merged in) | No — result picked up via IPC on the next turn |
| **Inside a workflow** | **in-process** — the engine sets `VAF_IN_SUBAGENT_TERMINAL=1` to avoid nested spawns, so the step runs the sub-agent directly and **waits** for its result | Yes — the step waits (so step N can feed step N+1) |

The ASCII diagrams below depict the **CLI terminal** mode. In WebUI/desktop the "Separate
Terminal" box is a headless child process; inside a workflow there is no child at all.

In every mode the call is **time-bounded** (`vaf/core/bounded_run.py`, config keys
`subagent_timeout_seconds` / `tool_timeout_seconds`) and **stop-aware**: a stuck sub-agent
can no longer freeze the backend, and the Stop button cancels in-flight work. The legacy
`subagent_timeout_minutes` only governs IPC zombie cleanup, not the in-line wait.

**Live supervision (watchdog).** The active IPC units are exposed read-only at
`GET /api/supervisor/status` (agent type, runtime, heartbeat age, and staleness vs
`subagent_liveness_timeout_seconds`) and can be force-killed individually via
`POST /api/supervisor/cancel` `{task_id}` → `Platform.stop_webui_subagent_process_by_task`
kills the child process tree and the IPC task is failed so any engine wait unblocks. The WebUI
shows this inline in the sub-agent's tool bubble (gated on a live unit, so it stays while the
delegated subprocess runs), and as lines in the Workflow Runtime terminal for sub-agents that have
no bubble (workflow steps). See [Workflow UI Components](WORKFLOW_UI_COMPONENTS.md).

---

## Workflow Diagram

### Step 1: Task Creation

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            TASK CREATION                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   User: "Show me the files on my Desktop"                                   │
│                          │                                                  │
│                          ▼                                                  │
│                  ┌───────────────┐                                          │
│                  │  Main Agent   │                                          │
│                  │  recognizes:  │                                          │
│                  │  librarian    │                                          │
│                  │  needed       │                                          │
│                  └───────┬───────┘                                          │
│                          │                                                  │
│                          ▼                                                  │
│              ┌───────────────────────┐                                      │
│              │   IPC.create_task()   │                                      │
│              │   → task_id: "a1b2"   │                                      │
│              └───────────┬───────────┘                                      │
│                          │                                                  │
│                          ▼                                                  │
│   ┌──────────────────────────────────────────────────────────┐              │
│   │                 pending_tasks.json                       │              │
│   │  [                                                       │              │
│   │    {                                                     │              │
│   │      "task_id": "a1b2c3d4",                              │              │
│   │      "agent_type": "librarian_agent",                    │              │
│   │      "task_description": "Show files...",                │              │
│   │      "status": "pending",                                │              │
│   │      "created_at": "2025-01-01T12:00:00",                │              │
│   │      "session_id": "sess_xyz123"  ◄── Tracks session     │              │
│   │    }                                                     │              │
│   │  ]                                                       │              │
│   └──────────────────────────────────────────────────────────┘              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Step 2: Sub-Agent Start

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          SUB-AGENT START                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   Main Agent Terminal                    New Terminal                       │
│   ┌─────────────────────┐               ┌─────────────────────┐             │
│   │                     │               │                     │             │
│   │  > vaf run          │   ──────►     │  VAF Librarian      │             │
│   │                     │   opens       │  Agent [a1b2c3d4]   │             │
│   │  | Sub-Agent        │               │                     │             │
│   │  | started in new   │               │  | Analyzing...     │             │
│   │  | terminal         │               │  | Listing files... │             │
│   │  | [Task: a1b2]     │               │                     │             │
│   │                     │               └─────────────────────┘             │
│   │  | Async Task       │                                                   │
│   │  | Task [a1b2]      │  ◄── Task-ID is displayed                         │
│   │  | delegated to     │                                                   │
│   │  | librarian_agent  │                                                   │
│   │                     │                                                   │
│   │  "The librarian_    │  ◄── Main Agent does NOT fabricate                │
│   │   agent [Task:      │      answers, it clearly says: "working on it"    │
│   │   a1b2] is working  │                                                   │
│   │   on it. I'll let   │                                                   │
│   │   you know when     │                                                   │
│   │   the result is     │                                                   │
│   │   ready. Is there   │                                                   │
│   │   anything else I   │                                                   │
│   │   can do for you    │                                                   │
│   │   in the meantime?" │                                                   │
│   │                     │                                                   │
│   └─────────────────────┘                                                   │
│                                                                             │
│   IMPORTANT: Main Agent must NOT fabricate any answers!                     │
│                                                                             │
│   Tool-Response in History:                                                 │
│   ┌──────────────────────────────────────────────────────────┐              │
│   │  ⏳ ASYNC TASK STARTED - NO RESULT AVAILABLE YET         │              │
│   │                                                          │              │
│   │  Task-ID: a1b2c3d4                                       │              │
│   │  Agent: librarian_agent                                  │              │
│   │  Status: Running in separate terminal                    │              │
│   │                                                          │              │
│   │  Result will be reported via IPC...                      │              │
│   │  UNTIL THEN: No data available!                          │              │
│   └──────────────────────────────────────────────────────────┘              │
│                                                                             │
│   Status changes: pending → running                                         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Step 3: Sub-Agent Result

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       SUB-AGENT RESULT                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   Sub-Agent Terminal                                                        │
│   ┌─────────────────────┐                                                   │
│   │                     │                                                   │
│   │  ### Librarian      │                                                   │
│   │  Report             │                                                   │
│   │                     │                                                   │
│   │  Folder: Desktop    │                                                   │
│   │  Files: 15          │     ────────►  IPC.complete_task()                │
│   │  Folders: 3         │                                                   │
│   │  ...                │                                                   │
│   │                     │                                                   │
│   │  ✓ Result sent to   │                                                   │
│   │    Main Agent       │                                                   │
│   │                     │                                                   │
│   └─────────────────────┘                                                   │
│                                                                             │
│   Status changes: running → completed                                       │
│                                                                             │
│   ┌──────────────────────────────────────────────────────────┐              │
│   │                 completed_results.json                   │              │
│   │  [                                                       │              │
│   │    {                                                     │              │
│   │      "task_id": "a1b2c3d4",                              │              │
│   │      "status": "completed",                              │              │
│   │      "completed_at": "2025-01-01T12:00:15",              │              │
│   │      "result": "### Librarian Report\n..."               │              │
│   │    }                                                     │              │
│   │  ]                                                       │              │
│   └──────────────────────────────────────────────────────────┘              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Team State Synchronization

In addition to the IPC queue, Sub-Agents synchronize their status with the **Main Persistence Layer** (`{current_working_directory}/.vaf/main/team_state.json`). This ensures the Main Agent's "brain" (System Prompt) is always aware of the team's status, even if the IPC message hasn't been processed yet.

**Synchronization Bridge:**
1. IPC receives result.
2. Main Agent's `_process_subagent_result` reads result.
3. **Result validation** — direct sub-agent calls only (see [Context Management](CONTEXT_MANAGEMENT.md)): An LLM judges whether the result fulfills the user's intent. If not (`</false>`), a retry instruction is injected and the Main Agent calls the sub-agent again. Max 20 retries; then the agent is instructed to inform the user of the actual status. (Sub-agents *inside a workflow* use the separate opt-in per-step validation described under [Workflow Integration](#per-step-output-validation-opt-in).)
4. Automatically updates `team_state.json`:
   - `status`: `completed`
   - `result_summary`: First 500 chars of result

**Clarification Flow:**
Sub-Agents can now request help instead of failing blindly:
1. Sub-Agent calls `request_clarification(question="...")`.
2. Updates `team_state.json` with status `needs_clarification`.
3. Main Agent sees this status in the next turn and asks the user.

**Team-await gate (don't declare done early):** When a Main Agent reply asserts overall completion, it checks the live running state via `get_active_tasks_for_current_session()` and bounces the claim while any sub-agent is genuinely running, so it waits for the real result instead of finishing early. It is anti-stuck by design: `check_zombies()` reaps crashed/stale sub-agents first (so they never block), a finished sub-agent has already left `active_tasks.json`, and a per-turn block cap lets the claim through after a few bounces. See the plan-enforcement section in [Context Management](CONTEXT_MANAGEMENT.md).

### Step 4: Non-Blocking Processing (Chat Mode)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    NON-BLOCKING CHAT MODE                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   Main Agent responds IMMEDIATELY and does NOT block:                       │
│                                                                             │
│   Main Agent Terminal                    Sub-Agent Terminal                 │
│   ┌─────────────────────┐               ┌─────────────────────┐             │
│   │                     │               │                     │             │
│   │  | Sub-Agent        │               │  VAF Librarian      │             │
│   │  | 🚀 librarian     │   ──────►     │  Agent [a1b2c3d4]   │             │
│   │  | [Task: a1b2]     │               │                     │             │
│   │  | running in       │               │  | Analyzing...     │             │
│   │  | background       │               │  | Listing files... │             │
│   │                     │               │                     │             │
│   │  "The librarian_    │               │                     │             │
│   │   agent is working  │               │  ...                │             │
│   │   in the background.│               │                     │             │
│   │   The result will   │               │  ### Report         │             │
│   │   be displayed when │               │  Size: 3.39 GB      │             │
│   │   it's ready."      │               │                     │             │
│   │                     │               │  ✓ Result sent to   │             │
│   │  "What else can I   │               │    Main Agent       │             │
│   │   do for you in     │               │                     │             │
│   │   the meantime?"    │               └─────────────────────┘             │
│   │                     │                                                   │
│   └─────────────────────┘                                                   │
│                                                                             │
│   The user can IMMEDIATELY ask other things!                                │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Step 5: Status Banner & Result Display

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                   STATUS BANNER & RESULT DISPLAY                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   At each new input, the system automatically checks:                       │
│                                                                             │
│   Main Agent Terminal                                                       │
│   ┌─────────────────────────────────────────────────────────────┐           │
│   │                                                             │           │
│   │  ╭──────────── 🚀 Active Sub-Agents ───────────╮            │           │
│   │  │   🔄 librarian_agent [a1b2c3d4] running     │            │           │
│   │  │      for 25s                                │            │           │
│   │  ╰─────────────────────────────────────────────╯            │           │
│   │                                                             │           │
│   │  Message: _                                                 │           │
│   │                                                             │           │
│   └─────────────────────────────────────────────────────────────┘           │
│                                                                             │
│   When Sub-Agent is finished:                                               │
│                                                                             │
│   ┌─────────────────────────────────────────────────────────────┐           │
│   │                                                             │           │
│   │  ╭──────────── 🎉 Sub-Agent Complete ──────────╮            │           │
│   │  │   ✓ Sub-Agent result received!              │            │           │
│   │  │                                             │            │           │
│   │  │   Task: a1b2c3d4                            │            │           │
│   │  │   Agent: librarian_agent                    │            │           │
│   │  │   Duration: 28s                             │            │           │
│   │  │                                             │            │           │
│   │  │   Result:                                   │            │           │
│   │  │   ### Folder Size                           │            │           │
│   │  │   Path: /home/user/Downloads                │            │           │
│   │  │   Total size: **3.39 GB**                   │            │           │
│   │  │   ...                                       │            │           │
│   │  ╰─────────────────────────────────────────────╯            │           │
│   │                                                             │           │
│   │  💡 Sub-Agent results shown above.                          │           │
│   │     Tell me what you'd like to do with them!                │           │
│   │                                                             │           │
│   │  Message: _                                                 │           │
│   │                                                             │           │
│   └─────────────────────────────────────────────────────────────┘           │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## File Structure

```
~/.vaf/                              (or %APPDATA%\vaf\ on Windows)
└── subagent_queue/
    ├── pending_tasks.json           # Waiting tasks
    ├── active_tasks.json            # Running tasks
    ├── completed_results.json       # Completed results
    ├── paused_workflows.json        # Paused workflows waiting for sub-agents
    └── task_payloads/               # Full task content for long tasks
        └── {task_id}.txt            # Used when task exceeds command-line limit (~3K chars)
```

**Task Payloads:** When a task description exceeds ~3000 characters (e.g., detailed document requests), the full text is stored in `task_payloads/{task_id}.txt`. The sub-agent is spawned with `--task-id` only and retrieves the task via `ipc.get_task_payload(task_id)`. This avoids Windows command-line limits (~8191 chars).

---

## Status Transitions

```
                    ┌─────────────────────────────────────────┐
                    │           TASK STATUS FLOW              │
                    └─────────────────────────────────────────┘

                              create_task()
                                   │
                                   ▼
                           ┌───────────────┐
                           │    PENDING    │
                           │   (waiting)   │
                           └───────┬───────┘
                                   │
                          mark_task_running()
                                   │
                                   ▼
                           ┌───────────────┐
                           │    RUNNING    │
                           │  (executing)  │
                           └───────┬───────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
     complete_task()          fail_task()         (timeout)
              │                    │                    │
              ▼                    ▼                    ▼
      ┌───────────────┐    ┌───────────────┐    ┌───────────────┐
      │   COMPLETED   │    │    FAILED     │    │    TIMEOUT    │
      │   (success)   │    │   (error)     │    │   (expired)   │
      └───────────────┘    └───────────────┘    └───────────────┘
```

---

## API Reference

### Main Agent Methods

```python
from vaf.core.subagent_ipc import get_ipc

ipc = get_ipc()

# Create a task (full description is stored in task_payloads for long tasks)
task_id = ipc.create_task("librarian_agent", "Show files...")

# Mark task as running
ipc.mark_task_running(task_id)

# Query results
results = ipc.get_pending_results()  # List of SubAgentTask

# Consume a single result (remove from queue)
task = ipc.consume_result(task_id)

# Check status
has_results = ipc.has_pending_results()  # bool
status = ipc.get_task_status(task_id)    # "pending", "running", "completed", etc.
```

### Sub-Agent Methods

```python
from vaf.core.subagent_ipc import get_ipc

ipc = get_ipc()

# Retrieve full task payload when launched with --task-id only (long tasks)
task_text = ipc.get_task_payload(task_id)  # Returns None if not found

# Report success
ipc.complete_task(task_id, result="### Report\n...")

# Report failure
ipc.fail_task(task_id, error="Connection timeout")
```

### Paused Workflow Methods

```python
from vaf.core.subagent_ipc import get_ipc, PausedWorkflow

ipc = get_ipc()

# Save paused workflow state
paused_wf = PausedWorkflow(
    workflow_id="abc123",
    waiting_for_task_id="task_xyz",
    current_step_index=1,
    outputs={"step1_result": "..."},
    variables={"query": "..."},
    steps_data=[...],  # Serialized workflow steps
    workflow_name="deep_research",
    created_at="2025-01-01T12:00:00"
)
ipc.pause_workflow(paused_wf)

# Check for paused workflow waiting for a task
paused = ipc.get_paused_workflow_for_task("task_xyz")

# Get all paused workflows
all_paused = ipc.get_all_paused_workflows()

# Remove a paused workflow (after resuming)
ipc.remove_paused_workflow("abc123")
```

### Session Tracking

Sub-agent tasks are now **session-aware**. Each task stores the session ID that created it,
preventing stale tasks from previous sessions from appearing in the current session's banner.

```python
from vaf.core.subagent_ipc import (
    get_ipc, 
    set_current_session_id, 
    get_current_session_id,
    cleanup_other_sessions
)

# Set the current session ID (called when a new session starts)
set_current_session_id("session_abc123")

# Clean up tasks from previous sessions
cleanup_other_sessions()

# Get only active tasks for current session
ipc = get_ipc()
tasks = ipc.get_active_tasks_for_current_session()

# Or explicitly filter by session
tasks = ipc.get_active_tasks(session_id="session_abc123")
```

**Automatic Cleanup:** When a new session starts, `cleanup_other_sessions()` is called
automatically. This moves any active tasks from previous sessions to the results queue
with status `timeout` and an appropriate error message.

---

## CLI Commands

```bash
# Show sub-agent status
vaf subagent status

# Clear all queues (for debugging)
vaf subagent clear

# Start sub-agent manually (used internally)
vaf subagent run librarian_agent --task "..." --task-id "a1b2c3d4"

# Start sub-agent without auto-close (for debugging)
vaf subagent run librarian_agent --task "..." --no-auto-close
```

---

## Workflow Integration

Workflows now support **async sub-agents with pause/resume**. When a workflow step calls
a sub-agent running in a separate terminal, the workflow **pauses** and returns control
to the user. When the sub-agent finishes, the workflow **automatically resumes**.

### Per-step output validation (opt-in)

A workflow step can opt into an LLM check that its **output actually fulfils the step's goal** —
distinct from the Main Agent's direct-call validation above (which workflow steps bypass). Set
`"validate": true` on a content/agent step (`document_agent`, `research_agent`, `coding_agent`,
`browser_agent`, `document_writer`, `librarian_agent`). After the step runs,
`Agent._validate_step_output` judges the output against the step's goal (its `description`/`input`);
on a mismatch the step re-runs with a correction hint up to `workflow_step_validation_max_retries`
(default 3) times, then the last version is **accepted** and the workflow continues. It never
hard-fails on validation, and a validator error is treated as a pass. Unlike the direct-call
validator there is **no** lenient "saved successfully → accept" fast-path — the content is judged.
If a workflow has content steps but none set `validate`, `run_temp` returns a `[VALIDATION CHECK]`
prompt so the agent either flags the critical steps or confirms `skip_validation: true`. See
[Workflow Selection](WORKFLOW_SELECTION.md).

### Step execution and conditions

Steps run in **strict sequence** by default. Each step supports three optional control-flow fields:

**`condition`** — skip a step unless the expression is truthy. Supports AND / OR / NOT operators (left-to-right, no parentheses):

```python
# Simple: run only when research_content is non-empty
"condition": "{research_content}"

# AND: both must be truthy
"condition": "{research_content} AND {user_wants_report}"

# OR: at least one truthy
"condition": "{step1_ok} OR {fallback_data}"

# NOT: invert
"condition": "NOT {error_occurred}"

# Combined
"condition": "{a} AND NOT {b} OR {c}"
```

**`on_success`** — after a step succeeds, jump to the step whose `output` name matches (or a 0-based index string). Normal sequential flow if omitted.

**`on_failure`** — after a step fails, jump to the named step instead of aborting the workflow. The failed step is reclassified as *skipped* so the workflow is not marked failed overall. Requires no `optional: true`.

```python
"steps": [
    {
        "tool": "web_search",
        "input": "{topic}",
        "output": "research",
        "on_failure": "notify_empty",       # jump here when search fails
    },
    {
        "tool": "quality_check_agent",
        "input": "{research}",
        "output": "quality_ok",
        "condition": "{research} AND NOT {skip_check}",
        "on_success": "write_report",       # skip straight to write when check passes
        "on_failure": "notify_quality",
    },
    {"tool": "notify", "input": "Quality check failed for {topic}", "output": "notify_quality"},
    {"tool": "write_file", "input": "{research}", "output": "write_report"},
    {"tool": "notify", "input": "No results for {topic}", "output": "notify_empty"},
]
```

An infinite-loop guard aborts the workflow if the number of step-jumps exceeds `len(steps) × 3`.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    WORKFLOW WITH ASYNC SUB-AGENT (PAUSE/RESUME)             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   Main Agent Terminal                    Sub-Agent Terminal                 │
│   ┌─────────────────────┐               ┌─────────────────────┐             │
│   │                     │               │                     │             │
│   │  Workflow Step 1    │               │                     │             │
│   │  ✓ Done             │               │                     │             │
│   │                     │               │                     │             │
│   │  Workflow Step 2    │   ──────►     │  Research Agent     │             │
│   │  → Sub-Agent        │   opens       │  [Task: abc123]     │             │
│   │                     │               │                     │             │
│   │⏸️  Workflow paused  │               │  | Searching...     │             │
│   │  You can continue   │               │  | Analyzing...     │             │
│   │  using VAF!         │               │  | Writing...       │             │
│   │                     │               │                     │             │
│   │ ╭── 🚀 Background ─╮│               │                     │             │
│   │ │ ⏸️  deep_research││               │                     │             │
│   │ │ waiting abc123   ││               │                     │             │
│   │ ╰──────────────────╯│               │                     │             │
│   │                     │               │                     │             │
│   │  User: "What's 2+2?"│               │                     │             │
│   │  Agent: "4"         │               │  ✓ Result ready     │             │
│   │                     │◄─── IPC ──────│                     │             │
│   │  (next user input)  │               └─────────────────────┘             │
│   │                     │                                                   │
│   │  ╭── ▶️ Resuming ───╮                                                   │
│   │  │ ✓ Got result!    │                                                   │
│   │  ╰──────────────────╯                                                   │
│   │                     │                                                   │
│   │  Workflow Step 3    │                                                   │
│   │  ✓ Done             │                                                   │
│   │                     │                                                   │
│   │  Workflow Step 4    │                                                   │
│   │  ✓ Done             │                                                   │
│   │                     │                                                   │
│   │  ╭── ✅ Complete ───╮                                                   │
│   │  │ Workflow done!   │                                                   │
│   │  ╰──────────────────╯                                                   │
│   │                     │                                                   │
│   └─────────────────────┘                                                   │
│                                                                             │
│   KEY: Workflows are now NON-BLOCKING!                                      │
│   - User can ask other questions while workflow is paused                   │
│   - Workflow automatically resumes when sub-agent finishes                  │
│   - Status banner shows paused workflows                                    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Both Modes are Now Non-Blocking:

| Feature | Chat Mode | Workflow Mode |
|---------|-----------|---------------|
| **Blocking** | ❌ No (non-blocking) | ❌ No (pause/resume) |
| **User Interaction** | ✅ Immediately possible | ✅ Immediately possible |
| **Result Display** | On next input | Auto-resumes workflow |
| **Status Banner** | ✅ Yes (active tasks) | ✅ Yes (paused workflows) |
| **On Complete** | Shows result panel | Auto-continues workflow |

---

## Full Workflow in Separate Terminal (NEW!)

When `sub_agents_in_separate_terminals` is enabled, **entire workflows run in a separate terminal** - not just individual sub-agents. This prevents context overflow because large intermediate results (like HTML reports) **never touch the main agent's context**.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    FULL WORKFLOW IN SEPARATE TERMINAL                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   Main Agent Terminal                    Workflow Terminal                  │
│   ┌─────────────────────┐               ┌─────────────────────┐             │
│   │                     │               │                     │             │
│   │  User: "Research    │               │                     │             │
│   │  topic X"           │               │                     │             │
│   │                     │   ──────►     │  VAF Workflow:      │             │
│   │  | Brain matched:   │   opens       │  deep_research      │             │
│   │  | deep_research    │               │  [Task: abc123]     │             │
│   │                     │               │                     │             │
│   │  | Workflow running │               │  Step 1/4: filename │             │
│   │  | in separate      │               │  ✓ Done             │             │
│   │  | terminal         │               │                     │             │
│   │                     │               │  Step 2/4: research │             │
│   │  [>>] Workflow:     │               │  | Searching...     │             │
│   │  deep_research      │               │  | Analyzing...     │             │
│   │  [abc123] 15s       │               │  ✓ Done             │             │
│   │                     │               │                     │             │
│   │  (User can still    │               │  Step 3/4: repair   │             │
│   │   interact!)        │               │  ✓ Done             │             │
│   │                     │               │                     │             │
│   │  User: "What's 2+2?"│               │  Step 4/4: write    │             │
│   │  Agent: "4"         │               │  ✓ Done             │             │
│   │                     │               │                     │             │
│   │                     │◄─── IPC ──────│  [OK] Workflow done │             │
│   │                     │   short       │  Result sent!       │             │
│   │  [OK] Workflow      │   summary     │                     │             │
│   │  completed          │   only!       │  [*] Terminal       │             │
│   │                     │               │  closing in 5s...   │             │
│   │  Result: Report     │               │                     │             │
│   │  saved to: X.html   │               └─────────────────────┘             │
│   │                     │                                                   │
│   └─────────────────────┘                                                   │
│                                                                             │
│   KEY BENEFITS:                                                             │
│   - NO context overflow (full HTML stays in workflow terminal)              │
│   - Main agent receives SHORT summary only                                  │
│   - User can interact while workflow runs                                   │
│   - Each workflow has its own LLM context                                   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### CLI Command

```bash
# Run a workflow directly (used internally by the system)
vaf workflow run deep_research --variables '{"topic": "AI"}' --task-id abc123

# List available workflows
vaf workflow list
```

### How it Works

1. **Workflow Matched**: Brain detects user wants `deep_research`
2. **Spawn Terminal**: Opens new terminal running `vaf workflow run ...`
3. **Execute All Steps**: All 4 workflow steps run in the separate terminal
4. **Return Summary**: Only a short summary is sent back via IPC:
   ```
   Workflow 'Deep Research' completed successfully.
   Output saved to: C:\Users\...\Documents\topic_research.html
   ```
5. **Main Agent Receives**: Shows completion message, no full content!

### Configuration

Enable in settings:
```bash
vaf settings
# → Enable "Sub-Agents: Separate Terminals"
```

Or in config file (`~/.vaf/config.json`):
```json
{
  "sub_agents_in_separate_terminals": true
}
```

---

## Auto-Close Feature

The Sub-Agent terminal **automatically closes after 5 seconds** when the task is complete:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          AUTO-CLOSE COUNTDOWN                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   Sub-Agent Terminal                                                        │
│   ┌─────────────────────────────────────────────────────────┐               │
│   │                                                         │               │
│   │  ### Folder Size                                        │               │
│   │  Path: /home/user/Downloads                             │               │
│   │  Total size: **3.39 GB**                                │               │
│   │  ...                                                    │               │
│   │                                                         │               │
│   │  ✓ Result sent to Main Agent [Task: a1b2c3d4]           │               │
│   │                                                         │               │
│   │  ⏱️  Terminal closing in 5 seconds...                   │               │
│   │  ⏱️  Terminal closing in 4 seconds...                   │               │
│   │  ⏱️  Terminal closing in 3 seconds...                   │               │
│   │  ⏱️  Terminal closing in 2 seconds...                   │               │
│   │  ⏱️  Terminal closing in 1 second...                    │               │
│   │  ✓ Terminal closing.                                    │               │
│   │                                                         │               │
│   └─────────────────────────────────────────────────────────┘               │
│                              │                                              │
│                              ▼                                              │
│                    [Terminal closes]                                        │
│                                                                             │
│   Cross-Platform: Works on Windows, Linux, macOS                            │
│                                                                             │
│   Disable: Use --no-auto-close flag                                         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Sequence Diagrams

### Chat Mode (Non-Blocking)

```
┌─────────┐          ┌───────────┐          ┌───────────┐          ┌─────────┐
│  User   │          │   Main    │          │    IPC    │          │   Sub   │
│         │          │   Agent   │          │   Queue   │          │  Agent  │
└────┬────┘          └─────┬─────┘          └─────┬─────┘          └────┬────┘
     │                     │                      │                     │
     │  "Show Desktop"     │                      │                     │
     │────────────────────►│                      │                     │
     │                     │                      │                     │
     │                     │   create_task()      │                     │
     │                     │─────────────────────►│                     │
     │                     │                      │                     │
     │                     │   task_id: "a1b2"    │                     │
     │                     │◄─────────────────────│                     │
     │                     │                      │                     │
     │                     │           Open new terminal                │
     │                     │───────────────────────────────────────────►│
     │                     │                      │                     │
     │  "Sub-Agent running │                      │                     │
     │   in background.    │◄──────────┐          │                     │
     │   What else?"       │           │          │                     │
     │◄────────────────────│           │          │    Working...       │
     │                     │ IMMEDIATE!│          │◄────────────────────│
     │  (other question)   │           │          │                     │
     │────────────────────►│◄──────────┘          │                     │
     │                     │                      │                     │
     │  (answer)           │                      │   complete_task()   │
     │◄────────────────────│                      │◄────────────────────│
     │                     │                      │                     │
     │  (new input)        │                      │                     │
     │────────────────────►│                      │                     │
     │                     │                      │                     │
     │                     │  check_results()     │                     │
     │                     │─────────────────────►│                     │
     │                     │                      │                     │
     │  ╭─────────────╮    │  [Task a1b2 result]  │                     │
     │  │ 🎉 Result   │    │◄─────────────────────│                     │
     │  │ displayed!  │◄───│                      │                     │
     │  ╰─────────────╯    │                      │                     │
     │                     │                      │                     │
     │  "Here are the      │                      │                     │
     │   files"            │                      │                     │
     │◄────────────────────│                      │                     │
     │                     │                      │                     │
```

### Workflow Mode (Pause/Resume)

```
┌─────────┐        ┌───────────┐        ┌───────────┐        ┌─────────┐
│  User   │        │ Workflow  │        │    IPC    │        │   Sub   │
│         │        │  Engine   │        │   Queue   │        │  Agent  │
└────┬────┘        └─────┬─────┘        └─────┬─────┘        └────┬────┘
     │                   │                    │                   │
     │  start workflow   │                    │                   │
     │──────────────────►│                    │                   │
     │                   │                    │                   │
     │                   │  Step 1: ✓ Done    │                   │
     │                   │                    │                   │
     │                   │  Step 2: sub-agent │                   │
     │                   │───────────────────────────────────────►│
     │                   │                    │                   │
     │                   │  pause_workflow()  │                   │
     │                   │───────────────────►│                   │
     │                   │                    │                   │
     │  "Workflow paused"│◄──────────┐        │    Working...     │
     │◄──────────────────│           │        │◄──────────────────│
     │                   │ IMMEDIATE │        │                   │
     │  (other question) │           │        │                   │
     │──────────────────►│◄──────────┘        │                   │
     │                   │                    │                   │
     │  (answer)         │                    │   complete_task() │
     │◄──────────────────│                    │◄──────────────────│
     │                   │                    │                   │
     │  (new input)      │                    │                   │
     │──────────────────►│                    │                   │
     │                   │  check_results()   │                   │
     │                   │───────────────────►│                   │
     │                   │                    │                   │
     │                   │  [result + paused  │                   │
     │                   │   workflow found]  │                   │
     │                   │◄───────────────────│                   │
     │                   │                    │                   │
     │                   │  resume_workflow() │                   │
     │                   │                    │                   │
     │                   │  Step 3: ✓ Done    │                   │
     │                   │  Step 4: ✓ Done    │                   │
     │                   │                    │                   │
     │  "Workflow done!" │                    │                   │
     │◄──────────────────│                    │                   │
     │                   │                    │                   │
```

---

## Error Handling

### Timeout (Sub-Agent not responding)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TIMEOUT HANDLING                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   After the configured timeout without a result:                            │
│                                                                             │
│   cleanup_stale_active_tasks() is called                                    │
│                                                                             │
│   ┌─────────────────────┐                    ┌─────────────────────┐        │
│   │   active_tasks      │                    │  completed_results  │        │
│   │                     │                    │                     │        │
│   │   Task a1b2c3d4     │ ──── timeout ────► │  Task a1b2c3d4      │        │
│   │   (stale > timeout) │                    │  status: "timeout"  │        │
│   │                     │                    │  error: "Sub-agent  │        │
│   └─────────────────────┘                    │   task timed out"   │        │
│                                              └─────────────────────┘        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Error During Execution

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           ERROR HANDLING                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   Sub-Agent Terminal                                                        │
│   ┌─────────────────────┐                                                   │
│   │                     │                                                   │
│   │  Error: Connection  │                                                   │
│   │  refused to LLM     │     ────────►  ipc.fail_task(task_id,             │
│   │  server             │                  "Connection refused")            │
│   │                     │                                                   │
│   └─────────────────────┘                                                   │
│                                                                             │
│   Main Agent receives:                                                      │
│   ┌─────────────────────┐                                                   │
│   │                     │                                                   │
│   │  ✗ Sub-Agent        │                                                   │
│   │  [a1b2c3d4]         │                                                   │
│   │  failed:            │                                                   │
│   │  Connection refused │                                                   │
│   │                     │                                                   │
│   └─────────────────────┘                                                   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Configuration

In `~/.vaf/config.json`:

```json
{
  "sub_agents_in_separate_terminals": true,
  "subagent_timeout_enabled": true,
  "subagent_timeout_minutes": 120
}
```

### Settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `sub_agents_in_separate_terminals` | `true` | Run sub-agents in separate terminal windows |
| `subagent_timeout_enabled` | `true` | Enable/disable timeout for stale sub-agents |
| `subagent_timeout_minutes` | `120` | Timeout duration in minutes (1-480) |

### Configuring via Settings Menu:

```
vaf run → S (Settings) → Sub-Agents: Timeout [ON - 120 min]
```

Options:
- **Toggle timeout**: Turn timeout on/off
- **Set duration**: 1-480 minutes (0 = disable)

### When to Disable Timeout:

Disable timeout (`subagent_timeout_enabled: false`) for:
- Very long research tasks
- Large codebase analysis
- Complex multi-step operations

The sub-agent will run indefinitely until it completes or is manually stopped.

---

## Technical Details

### File Locking

The IPC system uses platform-specific file locking mechanisms:

| Platform | Method |
|----------|--------|
| Linux/macOS | `fcntl.flock()` |
| Windows | `msvcrt.locking()` |

### Atomic Write Operations

Write operations are atomic through:
1. Writing to a temporary file (`.tmp`)
2. Atomic rename via `Path.replace()`

### Queue Files

| File | Purpose |
|------|---------|
| `pending_tasks.json` | Waiting tasks (not yet started) |
| `active_tasks.json` | Running tasks |
| `completed_results.json` | Completed results |

---

## Related Files

- `vaf/core/subagent_ipc.py` - IPC Implementation
- `vaf/cli/cmd/subagent.py` - CLI Commands
- `vaf/tools/librarian.py` - Librarian Sub-Agent
- `vaf/tools/research_agent.py` - Research Sub-Agent
- `vaf/tools/coder.py` - Coding Sub-Agent
- `vaf/core/agent.py` - Main Agent Integration
- `vaf/workflows/engine.py` - Workflow Engine (paused/resume integration for async sub-agent steps)
