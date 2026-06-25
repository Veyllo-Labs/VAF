# Tool and Sub-Agent Supervision

VAF runs tools, sub-agents, and workflow steps under a supervision layer whose job is simple to
state: **a single tool, sub-agent, or workflow step must never be able to freeze the backend, and
the Stop button must always work.** A blocking call cannot be allowed to hang the worker, and a
sub-agent that dies or goes silent must be detected and reaped.

There is no single `Supervisor` class — the supervision is a set of cooperating pieces. This
document maps them so the behavior is understandable and maintainable as one system.

## Two kinds of supervised work

| Kind | What it is | How it runs | How it is stopped |
|---|---|---|---|
| In-process unit | a pure-Python tool that runs in the worker thread (`tool.run()`) | wrapped in `run_bounded` on a daemon worker thread | cooperative stop check + hard deadline; on timeout/stop the thread is abandoned (Python cannot force-kill a thread) |
| Subprocess unit | a heavy sub-agent (coding/research/document/librarian/browser) | spawned as a child process; result returned via the file-based IPC queue | killable: SIGTERM then SIGKILL of the process tree |

The rule of thumb: genuinely blocking or long-running work belongs in a subprocess, because a
subprocess can actually be killed. Pure-Python tools run in-process and are bounded.

## In-process units: bounded, stop-aware execution

`vaf/core/bounded_run.py` wraps every in-process tool call so it can never block the worker
forever. `run_bounded(fn, *, timeout, stop_check, poll)`:

- runs `fn()` on a daemon worker thread and waits with a hard deadline,
- polls `stop_check()` every `poll` seconds (default `tool_stop_poll_seconds`),
- on timeout or stop, returns a recognizable **sentinel string** (`[VAF_TOOL_TIMEOUT]` /
  `[VAF_TOOL_STOPPED]`) instead of hanging; callers detect it with `is_abort_sentinel(...)`.

Python caveat: a thread cannot be force-killed, so on timeout/stop the worker is freed but the
underlying thread is *abandoned* (it keeps running until it finishes on its own). The backend stays
responsive regardless. Long pure-Python tools should therefore check the stop flag at safe points
and return early — see "Cooperative cancel" below.

The per-call budget is chosen per agent by `agent_timeout_seconds(tool_name)`: a filesystem agent
is not forced to wait the full research budget. A small set of tools manage their own lifecycle and
are deliberately **not** wrapped (`SELF_SUPERVISED_TOOLS`): `browser_agent` (its own stop monitor +
`max_steps`), the workflow orchestrators `create_agent_workflow` / `execute_workflow` (the engine
already bounds each step, so bounding them again would double-bound), and `python_sandbox` (it runs
a stop-aware poll loop with its own deadline that kills the Docker exec the moment Stop is requested
— being abandoned by `run_bounded` would race that kill against the stop flag being cleared).

### Cooperative cancel

Because an abandoned thread keeps running, the longest in-process tools cut themselves short on Stop
rather than being abandoned, in one of two ways:

- `librarian` (wrapped by `run_bounded`): its filesystem walk is a pruned `os.walk` with a wall-clock
  deadline, so it returns before the bound would abandon it.
- `python_sandbox` (self-supervised, so `run_bounded` does not wrap/abandon it): its execution is a
  stop-aware poll loop that checks `TaskQueue().should_stop(get_current_session_id())` and, on Stop,
  kills the Docker exec (and the in-container process) immediately instead of letting it run to the
  timeout.

## Subprocess units: bounded IPC wait, liveness, real kill

Heavy sub-agents run as their own process and report back through the file-based IPC queue
(`vaf/core/subagent_ipc.py`; see [Sub-Agent IPC](SUBAGENT_IPC.md)). Inside a workflow,
`WorkflowEngine._await_subagent` waits for the result **bounded and stop-aware**, in priority order
each tick:

1. return as soon as the IPC result arrives (`consume_result`),
2. on Stop, kill the child and abort,
3. **liveness**: a spawned sub-agent pulses a heartbeat roughly every 3 s; if none arrives for
   `subagent_liveness_timeout_seconds`, the unit is treated as dead/stuck and reaped
   (`subagent_ipc.check_zombies`),
4. a worst-case hard deadline (the per-agent budget) as the absolute ceiling.

The kill is real: spawned children are tracked in `Platform` and stopped with a SIGTERM then SIGKILL
of the process tree (`Platform.stop_webui_subagent_processes(session)` for a whole session,
`Platform.stop_webui_subagent_process_by_task(task_id)` for one unit). On kill the IPC task is failed
so any waiter unblocks.

## Stopping work (`cancel_session`)

The Stop button cancels everything for a session through two cooperating calls
(`vaf/core/web_server.py`):

1. `TaskQueue.request_stop(session_id)` — sets a stop flag that the in-process bounded waits and the
   engine's subprocess wait both poll, so in-flight work aborts within roughly the poll interval.
2. `Platform.stop_webui_subagent_processes(session_id)` — kill-tree of that session's child
   processes.

Queued (not yet running) tasks for the session are dropped with
`TaskQueue.drop_queued_tasks_for_session`.

## Watchdog: live status and per-unit kill

The currently-running subprocess units are exposed read-only and can be killed individually
(`vaf/api/supervisor_routes.py`):

- `GET /api/supervisor/status` — live units from the IPC active queue: agent type, runtime,
  heartbeat age, and whether a unit is stale (no heartbeat for longer than the liveness window).
- `POST /api/supervisor/cancel {task_id}` — kill one unit's process tree and fail its IPC task.

The WebUI shows this **inline in the sub-agent's own tool bubble** — gated on a live supervised unit
(matched by task id), so it stays visible while the delegated subprocess runs even though the tool
call itself already returned. A sub-agent that has no tool bubble (a **workflow step**) instead
surfaces its heartbeat and runtime as lines in the **Workflow Runtime terminal**, emitted by the
engine's wait loop. See [Workflow UI Components](../web-ui/WORKFLOW_UI_COMPONENTS.md).

## Worker and queue model

Requests are processed by worker threads pulling from a single shared `TaskQueue`
(`vaf/core/task_queue.py`):

- **Per-session serialization.** The queue tracks in-flight sessions (`_session_inflight`) and never
  hands the same session to two workers at once — a session's turns run strictly in order.
- **Fairness.** With `queue_policy: weighted_fair`, interactive / automation / background task
  classes are scheduled with weighted fairness; the default `legacy` policy is a single priority heap.
- **Pool.** `vaf/core/headless_runner.py` runs up to the **effective** worker count: the configured
  `parallel_main_workers` clamped per provider — API providers to `max_parallel_api_workers` (default 5),
  `provider=local` to `max_parallel_local_workers` (default 2, further clamped to the llama-server
  `--parallel` slots) to avoid VRAM exhaustion. Default is 1 (serialized). With more than one worker,
  **different** sessions are processed concurrently while each session stays serialized — the per-session
  lock holds in **both** policies, so `weighted_fair` only adds lane fairness across task classes (recommended
  alongside `parallel_main_workers > 1`).

Stop requests are per-session (`request_stop` / `should_stop` / `clear_stop`), so stopping one
session never affects another.

Known limitations with more than one worker (only worker #1 owns the web-interface registration):
editing a **custom tool** in Settings hot-reloads worker #1 only — workers 2..N keep the old tool
definition until the next restart; and the "session active" hint on reconnect reflects worker #1's last
task (cosmetic — live status comes from `manager.latest_state`, and Stop is per-session).

### Concurrency and session isolation

`parallel_main_workers` defaults to 1, so by default exactly one turn runs at a time and there is no
shared-state concern. When set higher (with `queue_policy: weighted_fair`), different sessions run
on different workers at once, and the session context must not leak between them. It does not,
because:

- **User context is per worker.** Each worker has its own `Agent` instance; the per-task user scope,
  username, and role live on that instance, not in shared globals.
- **Session id is per context.** `subagent_ipc` keeps the current session id in a `ContextVar`, so
  each worker thread reads and writes its own value. `run_bounded` runs each in-process tool inside a
  copy of the caller's context, so the session id propagates into the tool's worker thread — and an
  *abandoned* (timed-out/stopped) thread keeps its own session, so its late writes are tagged
  correctly rather than with whatever a later turn is doing.
- **Sub-agent spawns carry context explicitly.** The spawn sites pass session/task/agent context to
  the child through `Platform.open_new_terminal(..., extra_env=...)` (the child's own environment)
  instead of mutating the parent's process-global `os.environ`, so concurrent spawns cannot clobber
  one another. A child is registered and killed under the session it was actually spawned for.

The remaining process-global readers of `VAF_SESSION_ID` are a few best-effort UI notifications
(e.g. "file created"); they read the current process environment and are therefore the only spot
that can mis-tag under heavy multi-session concurrency. They do not affect routing, kill, or result
delivery.

## Timeouts and configuration

| Key | Default | Governs |
|---|---|---|
| `tool_timeout_seconds` | 120 | generic in-process tool call |
| `librarian_timeout_seconds` | 60 | filesystem agent (should be fast) |
| `subagent_timeout_seconds` | 300 | coding / research / document sub-agent |
| `browser_timeout_seconds` | 1800 | worst-case browser ceiling (liveness is the real guard) |
| `subagent_liveness_timeout_seconds` | 60 | no-heartbeat window before a child is reaped |
| `tool_stop_poll_seconds` | 0.5 | how often the bounded wait checks stop/deadline |
| `parallel_main_workers` | 1 | number of worker threads |
| `queue_policy` | `legacy` | `legacy` or `weighted_fair` |

## Source files

| File | Role |
|---|---|
| [vaf/core/bounded_run.py](../../vaf/core/bounded_run.py) | bounded, stop-aware in-process execution + per-agent timeouts |
| [vaf/workflows/engine.py](../../vaf/workflows/engine.py) | `_await_subagent` — bounded IPC wait + liveness + kill for subprocess steps |
| [vaf/core/platform.py](../../vaf/core/platform.py) | spawned-child registry; `stop_webui_subagent_processes` / `_by_task` kill-tree |
| [vaf/core/subagent_ipc.py](../../vaf/core/subagent_ipc.py) | IPC queue, heartbeats, `check_zombies`, active-task status |
| [vaf/api/supervisor_routes.py](../../vaf/api/supervisor_routes.py) | watchdog status + per-unit cancel API |
| [vaf/core/task_queue.py](../../vaf/core/task_queue.py) | per-session serialization, fairness, per-session stop flags |
| [vaf/core/headless_runner.py](../../vaf/core/headless_runner.py) | worker pool (`parallel_main_workers`) |

See also [Sub-Agent IPC](SUBAGENT_IPC.md) and [Browser Agent](BROWSER_AGENT.md).
