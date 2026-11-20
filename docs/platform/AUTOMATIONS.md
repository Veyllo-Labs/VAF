# Automations

VAF supports **scheduled automations**: the agent runs a prompt on a schedule (once, daily, weekly, monthly, or hourly). Automations are **VAF-internal** (stored and executed by VAF). They share the calendar window with the appointments of the VAF calendar, which has its own store, sync and reminder lane; see [CALENDAR_INTEGRATION.md](../integrations/CALENDAR_INTEGRATION.md).

## Overview

- **Scheduled tasks:** Create automations with a repeat rule, time, and a detailed prompt. The agent executes the prompt at the scheduled time. Run times use HH:MM; the system enforces a minimum 10-minute interval between any two automations and returns an error if a new or updated time is too close to an existing one. A stored record whose time cannot be read as HH:MM, or whose frequency this version does not know, has **no next run**: it lists last, shows `-` where a next run would be, and never raises, so one such record cannot take the listing down for the others (the interval check steps aside for a time without a colon, so the create path can admit exactly such a record).
- **Per-user:** Automations are scoped by `user_scope_id`; each user sees and runs only their own tasks. Tasks are stored under `Platform.vaf_dir() / "automations" / <user_scope_id> /` (one `.json` file per task, plain JSON, written to a temp file and then put in place, so a save that fails half way, such as on a full disk, keeps the previous file instead of leaving an empty one that every later load skipped). Every write of a task file holds the store's lock, `.automations.lock` in the task's directory (a kernel lock through `filelock`, which the operating system releases when its holder dies; `AutomationManager._store_lock`). A change is applied to the record on disk at that moment, not to the copy the lane loaded earlier (`_mutate_record`): two lanes' edits of different fields both stay, and a change to a task deleted or moved to the trash in the meantime is not written, so it does not come back. A restore from the trash never replaces a live task of the same id. A restore arms the task with the running scheduler at once, like a create or an update (a clock task gets its next run counted from now, no missed slot is caught up; an event task is watched again), and tells the thinking workspace it is back with its stored on/off state. One rule decides who may see, read, change or delete a task: `AutomationAccess` in `vaf/core/automation.py`. Every network lane asks it (the WebSocket commands, the agent's automation tools, the thinking review, the next-run lookup and the calendar's daily-check route), and an id the caller does not see answers exactly like an id that does not exist, so ids are names, not keys. Before this rule the WebSocket `update_automation` and `delete_automation` retried a miss on the unscoped store, which holds every account's tasks, so any account that knew or guessed an 8-character id could rewrite or delete another account's automation, including the machine owner's.
- **Admin Access:** An admin is whoever `is_admin_identity` says is one: the `admin` role claim, or the machine owner's scope (`local_admin_scope_id`) without a role. An admin sees, and acts on, exactly what its list shows: legacy "root" tasks in `automations/`, its own scoped tasks, and the tasks under `local_admin_scope` (e.g. Daily calendar check), so system automations appear even when the admin's token carries a different `user_scope_id`. An admin does **not** see or change another account's automations: those run with that account's credentials, so an admin rewriting one would make that account act on words it never wrote. Regular users see only their own scoped directory. A caller with no scope at all is the machine owner (the CLI, an embedder that passes none); a scope that is present but blank is never an admin and sees nothing.
- **Tools and Manager:** The agent's automation tools (`create_automation`, `list_automations`, `read_automation`, `update_automation`, `delete_automation`, `restore_automation`, `list_trash`) ask `AutomationAccess` for every task they show or change, so they answer with the same set as the Web UI list. `delete_automation` deletes through the store that holds the task (`AutomationAccess.get`), and `list_trash` and `restore_automation` read every trash the caller's tasks can be in (`AutomationAccess.trash`: the own scope's, and for an admin also the machine owner's scoped trash, where its own deletes land, and the root trash), so an admin deletes, lists and restores a root or machine-owner task exactly as it reads and updates one. Create, read and update choose their store with `_manager_for_scope`. The duplicate check of `create_automation` reads only that set too, so its replies quote no other account's task. `tests/test_automation_access.py` holds every WebSocket branch and every tool to the rule.
- **Execution isolation:** Every automation run (prompt-based or workflow-based) uses the **task owner's** `user_scope_id`. The agent and workflow engine are set to that scope before execution, so RAG/memory, calendar, messaging (`send_telegram`, `send_whatsapp`, etc.), contacts, mail, and automation notes/todos all use the owner's data and credentials. Reminders created by e.g. the Daily calendar check therefore run in the same user context.
- **CLI and scheduler (named boundary):** these are the machine owner at the terminal, who can read the store directly, and the scheduler runs every task as its own owner, so they do not ask `AutomationAccess`. `vaf automation list`, `vaf automation run <id>`, `vaf automation start` and the TUI screens use an aggregated view: the global manager loads tasks from `automations/` and from every `automations/<uuid>/` subdir. All automations are listed and scheduled; save, delete, and restore write to the task's scope path (`_path_for_task`).
- **Web UI:** **Settings → Automations** lists and manages automations. The **Kalender** button in the main footer opens the calendar window: pick month, day and an hour slot in the automations lane (the grey-tinted right column, headed "Automatisierungen" with the lightning icon; an empty hovered hour says "+ Automatisierung") to create a new automation (repeat, time, prompt, optional name); the events lane beside it holds the appointments of the VAF calendar, and on today the current hour is framed in red with the time under its label (see [CALENDAR_INTEGRATION.md](../integrations/CALENDAR_INTEGRATION.md)). Creation is sent via WebSocket (`create_automation`); the list refreshes on success.
- **Agent tool:** The agent can create (and manage) automations via the `create_automation` tool in chat. The Tool Router adds `create_automation` when the user message contains words like "automate", "schedule", "daily", "weekly" (see [TOOL_ROUTER_ARCHITECTURE.md](../agents/TOOL_ROUTER_ARCHITECTURE.md)).
- **Result delivery:** After an automation completes, the result is appended as a **standalone** chat message (status marker + summary) to the user's active web session - it is added as its own bubble, not merged into a previous reply, and if that session is not the one currently open it is flagged unread. Standalone is carried by the message's `kind` tag (`"automation"`), emitted AND persisted: the chat groups a turn's assistant messages into one actions timeline, and a proactive message it cannot recognise is folded into the turn before it and takes over its answer bubble (see [WEB_UI.md](../web-ui/WEB_UI.md) -> "Actions Timeline"). If a main messenger (Telegram, WhatsApp, Discord) is configured, the result is also delivered there, in addition to the Web UI. **If the run produced an output file, it is attached** on the messenger (full text first, then the file as a document - so the message is never truncated by a caption limit) and referenced in the Web UI message. Messenger delivery goes through the one canonical "reach the user on their main channel" helper (`send_to_main_messenger`, shared with Thinking Mode), resolved to the **task owner's** account - a per-user automation result never lands on the local admin's messenger. The result is also recorded in the Web UI **Notifications** list (`kind: 'automation'`). If no web session exists, a new one is created. **No double delivery:** when the run already delivered in-run via a send tool (confirmed by its "sent to the user via ..." result - workflow lane: send step result; prompt lane: send-tool success in the agent history), the post-run messenger TEXT push is skipped - the Web UI trace and the notification still happen. A produced output file is then still delivered as an **attachment-only follow-up** (just the document with a filename caption, no second summary): the prompt lane writes the file after the run, so an in-run send can never have attached it (a run on 2026-08-21 delivered its Telegram text in-run and the report file reached nobody). Only when the in-run send itself already carried a document (result prefix "Message and document ...") is the file follow-up suppressed as well - the workflow lane's send steps attach the file in-run, so this keeps them at one document. Detection is deliberately conservative: a failed or unclear send keeps the push ON (a duplicate beats a lost message). Messenger deliveries from the router path are mirrored into the channel session history so the channel main agent has context when the user replies to them. The Web UI "saved file" reference is only added when the last step's output actually is a file on disk (previously the raw last-step result string, e.g. a send tool's status text, was printed as the saved path). The delivered text is the agent's **final answer only**: the same canonical reasoning cleaner used for live chat (`Agent._clean_reasoning`) removes the reasoning the model marked (`<think>` and `<thinking>` blocks, `<redacted_reasoning>` blocks) from the run's stream, so a model's marked chain-of-thought never appears in the result (see *Silent background execution* below). When nothing is left, because the run's text was all marked reasoning or its last words went to the history only (such as the note that a background task started), the result is the newest assistant message of the run that the same cleaner leaves non-empty and that is not one of the engine's placeholders for a round without text (`Thinking...`, `[Empty]`), else empty and shown as "Completed". Plain text is the answer as written: no phrase guess cuts it, and reasoning is never delivered with only its tags removed. The history holds reasoning only marked: wherever a round's reasoning is stored (the empty-response retry, and a round with no answer when that retry did not run), a local engine's reasoning channel, which arrives without tags, is stored as a `<think>` block (see [AGENT_LOOP.md](../agents/AGENT_LOOP.md), phases 9 and 11). Reasoning that quotes a closing tag and ends in something shaped like a tool call is removed whole too: the cleaner removes the marked reasoning before the leaked tool-call JSON. Named boundary: the stream is cleaned as one text, so an answer the run gave before a later round of marked reasoning can be lost from the result, and a run whose failure note never reached the stream reports success.
- **File vs chat-only output:** A file is written **only when the user actually asked for one** (an explicit `output_path`, or file/save intent in the prompt - e.g. "als HTML", "save", "datei", "export", "pdf", in German or English). Otherwise the automation is **chat-only**: it delivers the result as a message to the Web UI and the main messenger and writes nothing to disk. There is no longer a forced default of saving an HTML file to a folder.
- **Model for execution:** Automations run with `VAF_IN_AUTOMATION=1`, which causes `deepseek-auto` to resolve to the pro model (same as workflows). This ensures tool-heavy automations use the most capable model.
- **Time limit (prompt runs):** a prompt-based run is bounded by `automation_run_timeout_seconds` (default 600). On timeout the worker thread is abandoned, not killed - the runner therefore waits a bounded grace window for it to finish and then treats the run as a normal completion (both live timeout incidents would have recovered this way). Only when the grace expires too does the user get ONE honest timeout note: no partial stream as result, no output-file wrap of it, status `error`. The in-run delivery dedup still applies, so a send the abandoned worker already made keeps the messenger clean.
- **Thinking Workspace bridge (MVP):** automation lifecycle is mirrored into per-user thinking workspace tasks (`source=automation:<id>`). Run status (`success/error`), last/next run, `last_completed_local_date` (when present), and enabled state are synced so Thinking Mode can reason over current automation health. Approved workspace handoffs can optionally trigger `create`/`update` automation actions (approval-gated).

## Silent background execution & context handoff

An automation runs a real agent **in the background**. It must be invisible to whoever happens to be the active web user - only the final result is delivered (see *Result delivery*). Two mechanisms enforce this, mirroring Thinking Mode:

- **No live status/progress emits.** While `VAF_IN_AUTOMATION=1`, `_emit_to_web_ui()` returns `False`, so the run pushes no context/token-status, tool-list, retry, or clear-bubble updates into any chat session - exactly as a background thinking pass does. The result channel (`_push_result_to_web_ui`) is independent of this gate and still delivers.
- **No live tool bubbles.** Tool start/end updates are normally broadcast by `session_id`, falling back to the process-wide "current session" when the agent has none. A scheduled automation has no own web session, so that fallback would otherwise route its `web_search`/tool bubbles into the **active web user's** chat (a cross-user routing issue this guard prevents). The automation agent therefore carries a per-instance flag, `agent._background_run = True`, and both `emit_tool_update` sites skip the broadcast when it is set (logged once as `[SILENT-RUN] tool_update … SUPPRESSED (background agent)`). The flag is per-agent and so **race-free** - unlike gating on the process-wide env, it cannot suppress a concurrent real user's tool updates.

**Context handoff (genuine blocker only).** A background automation that truly cannot finish on its own - or has an important clarification it cannot decide - may contact the user **once** via `ask_user`. The handoff branch keys on the calling agent's **per-instance run kind** (`run_kind="automation"`, stamped at construction in `run_task`), never on the process-wide `VAF_IN_AUTOMATION` env var: env is shared across threads, so a concurrent automation once flipped a thinking-mode question into a handoff bundle (the env vars remain for process-wide concerns like model routing and emit gating). Bundles are data-minimized: multimodal content is stored text-only with a per-message cap, and a bundle marked `resolved` drops its history snapshot (the pickup digest was already rendered; only metadata and summary remain for audit). This is a high bar: never a status note ("starting", "working on it"); if a reasonable assumption is possible the agent proceeds and notes it in the result. On a real handoff the run stores its **full working history** as a per-scope *handoff bundle* (`Platform.vaf_dir() / "handoff_bundles" / <user_scope_id> / <id>.json`; the machine owner's bundles under the configured owner scope, also when the answering turn carries no scope, such as Discord or an agent embedded without `user_scope`) and records a tracked request linking that bundle. When the user replies, their **main agent** loads the bundle and validates it at pickup: only a bundle with `source="automation"` AND curated findings (a summary) gets the automation framing, and continuation is reply-conditional - clear agreement continues the task with the bounded digest (a concise note plus the run's findings and recent steps, never a raw dump), a decline changes nothing, and an ambiguous reply triggers exactly one confirming question before any action. A mislabeled or finding-less bundle degrades to the plain-question note (the digest is dropped) and is consumed. Each pickup writes one `[REPLY_CTX]` line to the prompt domain log (lane, request id, question head) so reply-routing is auditable. The bundle is per-scope, so a handoff reaches only the same user's main agent; it expires after 7 days if never answered. Code: `vaf/core/handoff_bundle.py` (`create`/`load`/`deliver_handoff`), pickup in `Agent.chat_step` (`_render_handoff_bundle`).

**An account without access runs nothing.** `run_task` first asks
`revocation.account_stands(task.user_scope_id)`: an account revoked in this process, or one the
account directory lists as inactive or no longer lists (deleted), is skipped with a
`[SKIPPED]` line in the backend log, so a deactivated account's schedule stays silent after a
restart too. A run that is already going when the account is revoked is stopped at once - the prompt
lane's bounded run and the workflow engine's stop check both poll the mark - and its result
is not delivered, on either lane. Nor is it recorded as done: the prompt lane asks before it
writes the output file or stamps the run as successful, so the schedule does not count it. See "Taking access away" in
[USER_ISOLATION.md](../security/USER_ISOLATION.md#taking-access-away-reaches-work-already-running).

## Short in-chat timers (`set_timer`) vs automations

For a **short, one-off delay that should fire proactively in the current conversation** (e.g. "in 1 minute say test", "in 90 seconds check the deploy"), the agent uses **`set_timer`**, not `create_automation`. The two cover different needs:

| | `set_timer` | `create_automation` (`frequency='once'`) |
|---|---|---|
| Timing | **Relative delay in seconds** (second-precise) | **Clock time `HH:MM`**, minute granularity, ≥10 min apart |
| Delivery | **Proactive message in the live chat** (CLI + WebUI) | Detached run; result delivered to the active web session and `main_messenger` (with any produced file attached) |
| Persistence | **In-memory, per process - lost on restart** | Persisted to disk; survives restarts |
| Use for | Short timers/reminders that should appear in *this* chat now | Recurring schedules and tasks that need an agent run |

**One-shot reminders (`schedule_reminder`)** sit between the two: a reminder is stored **data**, not an executable automation - the scheduler loop delivers the stored message **verbatim** at `fire_at` via the canonical main-channel router (Web UI notification fallback), with no agent run, no tools and no prompt. Persistent across restarts, per-user scoped, bounded (pending cap, 14-day horizon, and a 6-hour delivery grace after downtime - older ones are honestly marked missed with a notification). Because a reminder cannot spawn anything, the daily calendar check may schedule reminders even though `create_automation` stays stripped from automation runs (runaway guard intact); previously the calendar agent fell back to `set_timer`, which is in-memory only and session-anchored. Thinking runs cannot schedule reminders (propose-only lane). Code: `vaf/core/reminders.py`, tool `vaf/tools/schedule_reminder.py`. **Calendar reminders** are the same lane for the events of the VAF calendar: `fire_due_calendar_reminders()` (`vaf/core/calendar_sync.py`) rides the same scheduler tick, composes the text deterministically from the event (title, time in the user's format, location, linked contacts, in the user's language) and delivers it the same way, with the same six-hour grace and the same honest missed notification. An event's reminder is set on the event itself (the calendar window, the contact book, `create_calendar_event`), not through `schedule_reminder`, which stays the lane for a free-text reminder.

**Tools:** `set_timer` (provide `seconds` plus exactly one of `message` - a short note/reminder - or `task` - a concrete instruction), `list_timers`, and `cancel_timer`. `set_timer` is part of the agent's always-available core tools.

**On fire, the agent is WOKEN UP** and runs a real turn (it reads the note/task, can think and call tools, and replies) - a self-wakeup, not a passive text post. A `message` timer feeds the note in as the turn input; a `task` timer feeds the instruction.

**Delivery mechanism:** on fire, the timer enqueues a **wake turn** (`task_queue.enqueue_wake_turn`, which sets `metadata.wake = "timer"` and the `metadata.timer` it has always carried) into the same process's `TaskQueue`. A finished background command (`host_bash(background=true)`, see [TOOL_SUPERVISION.md](../agents/TOOL_SUPERVISION.md)) uses the same lane with `wake = "process"`; consumers read the kind with `task_queue.wake_kind`. Either lands in the queue the headless worker and the full-screen terminal app consume. The modern CLI's input loop (`vaf run --web`, `tui_mode: modern`) takes it too and declines it with a note, so a timer set from a chat in that lane does not fire there: that loop cannot bind a wake turn (see [TUI.md](TUI.md)). A background command is refused there for that reason before it starts; the lanes that run wake turns declare themselves (`task_queue.declare_wake_runner`). Two details make it work in the Web UI:
- **Session:** the timer attaches to the live chat session (`current_session_id`, resolved by `_resolve_session`), NOT the agent instance's random per-process `_session_id` (a `uuid4` from `_register_session`) - that earlier delivered the fire into a session the Web UI never listened on, so nothing showed.
- **Own bubble:** a timer turn has no preceding user message, so before the turn the lane emits the trigger as a **wake message** (`kind="timer"`) - the Web UI renders it in its own left-side area (see [WEB_UI.md](../web-ui/WEB_UI.md) → "Wake / system-activity messages"), the terminal app renders it as an amber-barred card above the reply. Both show the trigger and create a bubble boundary, so the agent's reply lands in its own new bubble with a correct timestamp. Both gate on the wake kind in the metadata, never on the text.

There is no passive text-marker delivery path: an older one was documented, never produced by `_fire`, and has been removed along with the two dead consumer branches that still stripped its prefix. `set_timer` is blocked on messaging channels (Telegram/WhatsApp/Discord) - use `create_automation` there. From the Veyllo App, the account holder's own device, a timer is set only after a tap on a card there ([VEYLLO_APP.md](../integrations/VEYLLO_APP.md#tools-in-the-app-chat)); the timer's own turn later keeps the channel's rules.

**In the terminal app** the lane polls its own process's queue once a second and turns a task whose session matches the open conversation. A task for a DIFFERENT session is dropped with a note rather than swapping the transcript underneath the reader, and it is gone rather than deferred - the scheduler removed it from its in-memory store before firing.

**Code:** `vaf/core/timers.py` (store + scheduler + the `_fire` wake-turn framing), `vaf/tools/timer.py` (the tools + `_resolve_session`), `vaf/core/headless_runner.py` (the `kind="timer"` wake emit before the turn), `vaf/cli/tui_app/agent_bridge.py` (`queue_tick` / `_drain_queue_once`), `web/app/page.tsx` (the `kind`-based wake card).

## The clock

A clock automation fires at its own next run: `AutomationTask.calculate_next_run()`, the value the automations list, the calendar window and the thinking-mode start gate already show. It is computed in the owner's timezone (`user_identity.timezone` through `vaf/core/user_time.py`, the standard library's `zoneinfo`, server-local when unset) and asked again after every run, counted from the later of the tick and the end of the run, so a run that outlasts its interval skips the missed slot rather than firing again at once. The scheduler loop checks the armed tasks on its 30-second tick (`AutomationManager._run_due_clock_jobs`); a run that raises is logged and the task stays armed. A monthly task on a day the current month does not have (the 31st in September) runs in the next month that has it.

**A clock task runs only while it still stands on disk.** The scheduler holds its own copy of every task, and every lane deletes, moves to the trash or switches off through a manager of its own, also from another process (the terminal while the web app runs), so before each run the tick reads the task's record (`_clock_record_verdict`, without the store lock: every write is a temp file put in place). The task runs only when the record is still the task that was armed (same `incarnation`), switched on and a clock task; otherwise the job is dropped without a run (`CLOCK_DROPPED` with the reason: task gone, created anew, disabled, no longer a clock task). Before, a daily task deleted in the app or by the agent ran its full prompt at its next time and was armed again for the day after, every day, until a refresh or restart. A record that is there but cannot be read (a manual edit half written) costs that one slot and not the task (`CLOCK_SKIPPED`): it stays armed for its next run; a one-time task, which has no next run, is disarmed and armed again from its file at the next refresh or restart. The delete itself does not tell the scheduler: `_schedule_task` deletes a spent one-time task while the refresh holds the scheduler's non-reentrant lock, so a refresh from the delete would deadlock it (`tests/test_automation_scheduler_sync.py` pins both).

Named boundary: a create, an edit or a restore made in ANOTHER process (the terminal while the web app runs, or the other way round) reaches the running scheduler at its next refresh or restart; there is no signal between processes, the refresh after a change is in-process. The check above closes the dangerous half of that boundary, a change that removes work is honoured at the next due time; a change that adds work arrives late, never early. The run uses the scheduler's copy of the task, so a prompt or a time edited in another process applies from that refresh on.

A tenant's task runs in its account's time zone, so the clock needs the account NAME behind the task's `user_scope_id` (the identity file is kept per name). It comes from the account directory the application registered (`set_account_directory_resolver`; VAF registers its `local_users` table through `vaf.auth.permissions.list_accounts`), resolved by `config.resolve_caller_username` and never the literal `"admin"`; the machine owner's tasks and the root tasks need no lookup. Every listing reads that directory ONCE, however many tasks and accounts it names: `AutomationManager.list()`, `get_next_automation_run_utc`, the scheduler start and `refresh_scheduler_from_disk`, `vaf automation list`, the agent's `list_automations` and the WebSocket `get_automations` open `tool_dispatch.account_directory_once()` around the sort and every per-task read. Before, a listing named the account three times per task (the sort, the next run on the wire, today's status line), and each time asked the auth store; measured with the store unreachable, one listing of six tenant tasks made 18 refused connects and the next one 18 again (about two seconds each on Windows), and the WebSocket listing ran them on the event loop. It now runs in a worker thread. A directory name is used only when it cannot be another account's (`config.account_name_for_scope`: one entry name that opens only itself on Linux, Windows and macOS, not "admin" or the machine owner's, not `scope_`-prefixed, carried by exactly one scope); otherwise the task gets the synthetic name.

Named boundary: while the auth store cannot be reached, VAF's resolver does not ask it again for 30 seconds (`permissions._store_down`, forgotten at the first success and whenever an admin saves a user), and a tenant's next run then reads in server-local time, because the synthetic `scope_<8 hex>` name has no identity file. That is the answer the failed lookup always gave, now without the wait; on the desktop the store being down means the stack is down, so no tenant can sign in then either. Once the store answers again (after at most the 30 seconds, or at once after an admin saves a user), the next listing reads the tenant's own time zone again. The scheduler arms a task from the same answer and corrects it at the task's next run or the next refresh. A failed name lookup shares that outage memory with the per-account permission rows: for those 30 seconds a tool allowlist answers as for an unreachable store (unrestricted, see `vaf/auth/permissions.py`), exactly as after any other failed directory or permission read in the process. A store that is only slow is not remembered: a directory read on a running event loop that gives up after `_DIRECTORY_JOIN_S` (5 seconds) answers the empty directory once and marks nothing.

There is no third-party scheduler behind it any more. The `schedule` package used to keep a second clock, and for any timezone it imports `pytz`, which was never a dependency of VAF: once an owner had a timezone set, registering the first task raised and the scheduler did not start at all (measured: `Automation scheduler start error: No module named 'pytz'` on every start). The suite had not seen it because no test gave an owner a timezone; `tests/test_automation_clock.py` does, with `pytz` blocked.

## Today status, persisted completion, and catch-up runs

- **Task JSON fields:** On each **successful** run, the task file stores `last_run` (ISO timestamp) and **`last_completed_local_date`** (`YYYY-MM-DD` in the **task owner's** calendar: the owner's timezone from `user_identity.timezone`, the host's local date when none is set). So **Done (today)** follows the owner's date: when the owner is already on the next day and the host is not, the run counts for the owner's day. They are written onto the record as it is when the run ends (`_record_successful_run`), and nothing else is: a time, switch, prompt or name changed while the run went stays, and a task deleted during its run stays deleted. They are written only onto the task that ran: every task gets an `incarnation` when it is created, kept by every edit and by the trash and a restore, and a task created anew under the same id while an old run of it was still going (a retried approval creates its planned id again) is neither stamped nor removed by that run. A record written before the field existed has none; two missing values are taken for the same task. `last_completed_local_date` is the source of truth for “already finished today” and **survives tray/VAF restarts** until the local date rolls over (e.g. automation at 06:00 completes → still **Done (today)** after a restart at 11:00; the next day it is no longer “today” and the status follows schedule vs clock again).
- **Schedule fields, one coercion:** `day`, `weekday` and `enabled` are read by one function, `coerce_schedule_fields` in `vaf/core/automation.py`, at the two places every lane reaches the store through: building a task (`AutomationTask.__post_init__`, which also reads every file loaded) and changing one (`AutomationManager.update`). A day is a whole number from 1 to 31 (text such as "15" is read, a value that does not parse is dropped), `enabled` sent as text is folded (a full-width or zero-width-spaced word reads as plain) and parsed ("false", "0", "no", "off", "", "nein", "aus", "disabled" are off, "ein" and "aktiv" on; a word that says neither on nor off is off too, so it fails closed, and so are NaN, infinity and a text longer than 64 characters, which is not read at all), a weekday is lower-cased with German names mapped. Before, the WebSocket, the agent's tools and an approved thinking-workspace proposal each did this by hand, and the proposal's update did not: a monthly day stored as text made the clock rule raise inside `list()`, so the scheduler's refresh after that update cleared every account's clock jobs and armed none, and a scheduler start failed. `calculate_next_run` also answers None, never an exception, for a field of the wrong type set past the coercion. An update writes only the fields it was given (never `id` or `user_scope_id`, and nothing that is not a stored field), onto the current record under the store's lock, so two edits of the same automation saved at the same moment (two connections, a lane and an approved proposal, two processes) keep each other's fields. Named boundaries: two edits of the SAME field, the one saved last stands; the 10-minute spacing check runs before the lock, so two lanes setting nearby times at the same instant can both pass it; without the `filelock` package only the threads of one process are serialized; the WebSocket waits for the lock on its event loop (its holders keep it for well under a millisecond for a task of a few KB, about 5 ms more per MB of task file (measured: 1 MB 5 ms, 64 MB 311 ms); after five seconds the answer is "automation store is busy"); a process still running an older version writes without the lock.
- **Older tasks:** Files without `last_completed_local_date` still work: tools fall back to the calendar date inferred from `last_run`.
- **Agent tools:** `list_automations` and `read_automation` include a **Today (local)** line for the model: **Done (today)**, **Scheduled (later today)**, **Due (not yet run today)**, or **In progress** (automation lock held). `read_automation` also shows **Last completed (local date)** when set.
- **One-time (`once`) lifecycle:** A `once` automation fires exactly one time and is then removed - whether the run **succeeded or failed** - so it never recurs (a failed one-time run is not silently retried the next day). The scheduler additionally refuses to re-arm a one-time task that has already run, which guards against a stale task file being reloaded after a restart or scheduler refresh. Only successful runs stamp `last_run` / `last_completed_local_date` before removal, with one exception: a prompt run abandoned after its time limit and the grace window answers with the timeout note and is still stamped like a completed run, so it counts as done for its day. Named boundary: this predates the store lock; taking the stamp away from an abandoned run is its own change, because what an abandoned run that later finishes should count as is part of it. Named boundaries: the run decides from the frequency it fired with, so a one-time task whose frequency is changed during its only run is still removed when that run ends. The removal and the stamp are writes under the store's lock, each of which gives up after five seconds of waiting or when the store cannot be written; the task then stays on disk enabled and unstamped, and the scheduler arms it again on its next refresh or restart. For a run that succeeded that takes the stamp and both removal attempts failing; for a run that failed before its stamp, the one removal attempt at its end is enough. The lock's holders keep it for milliseconds for a task of normal size (3.4 ms at most under a deliberate hammer of four writer threads and two writer processes, measured) and longer for a very large one (a stamp of a 64 MB task held it 311 ms, a removal 106 ms), so that takes a holder stuck in I/O, as an unlink that fails could already cause before the lock existed.
- **Immediate run after `create_automation`:** If the chosen clock time has **already passed today**, a daily automation is normally started **once** right after creation. That **immediate catch-up is skipped** when another **enabled daily** automation in the same **family** already completed today: same name (case-insensitive), or both names match a small briefing-style heuristic (e.g. “briefing”, “Morgenbrief”, “morning brief”). The tool response includes **Same-day catch-up skipped** with a short reason so duplicate morning jobs do not run twice the same calendar day.
- **Web UI:** The automations list in Settings still centers on schedule and `last_run`; the explicit **today** wording above is primarily surfaced through **agent tools** until the UI is extended to show the same labels.

## create_automation tool - parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `name` | yes | Short identifier (e.g. `daily_news`) |
| `prompt` | yes | Full task prompt the agent executes |
| `frequency` | yes | **Must be explicitly confirmed with the user.** One of: `once`, `hourly`, `daily`, `weekly`, `monthly`. Default is `once` (runs a single time, then auto-removed - it never repeats; see *One-time lifecycle* below). The agent must never assume `daily` or any other frequency. |
| `time` | yes | HH:MM format. Must be confirmed with the user. Minimum 10 min gap from any existing automation. |
| `weekday` | for `weekly` | e.g. `monday` |
| `day` | for `monthly` | Day of month (1–31). Text and numbers are read alike and clamped to 1 to 31; a value that does not parse is dropped |
| `output_path` | no | Save location for a produced file. **Omit it for a chat-only run** (the result is delivered to the Web UI/messenger without writing a file). When file output is wanted but no path is given, it falls back to `Documents`. See *File vs chat-only output*. |
| `parameters` | no | Extra context: `city`, `category`, etc. |
| `max_retries` | no | Times to retry on failure (0–5). Only set if user explicitly requests retry behaviour. |
| `retry_delay_minutes` | no | Minutes between retries. Only relevant with `max_retries > 0`. |

**Frequency rule (important):** The agent errors if an unknown frequency is passed. It never silently defaults to `daily`. The tool enforces: if not one of the five valid values, return an error and ask the user to specify.

## Automation planner (notes and to-dos)

The same calendar window includes a **per-user planner**:

- **To-do list:** Add items (text, optional due date); check off or delete. Data in `automation_planner/<user_scope_id>/todos.json`.
- **Notes:** Add notes (title, content); delete when done. Data in `automation_planner/<user_scope_id>/notes.json`.

Planner data is loaded when the calendar is opened (footer or Settings). Create/update/delete use WebSocket messages; the agent can manage the same data via tools: `add_automation_note`, `add_automation_todo`, `list_automation_notes`, `list_automation_todos`, `delete_automation_note`, `delete_automation_todo`. See [WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md) for message formats.

## Run log, and what may be said about an automation

Until recently the only trace an automation left was `last_run`, and
`_stamp_successful_run` writes it **only on success**. A job that errored every
morning and a machine that was switched off for a week therefore left
byte-identical records, and nothing could honestly be said about either.

Each task now keeps a bounded run log beside its own file -
`<task_id>.runs.json` in the same per-user directory, format `autorun-1-7c41d9`,
the last 50 outcomes with status, start time and duration. It is written where
the outcome is known (`append_run_log`, both the ordinary path and the
workflow-success path, which returns earlier), follows the task into and out of
the trash, and is read back with `load_run_log`. A missing log is normal: it
only starts existing once the task has run again.

NAMED BOUNDARY: an automation that runs a workflow whose run FAILED is still recorded as a
success. Its report says `# Workflow Failed`, but that path falls through to
`_stamp_successful_run`, and the run log's status is read from the report's opening (`Error:`),
which that report does not have. A failed step (a refused write, a helper that crashed) makes
the workflow fail and the report say so; the record has to take its status from the workflow
result instead, and what happens to a failed one-time task is part of that change.

A run log is never a task. Every reader of an automation directory takes its files through
`_task_files`, which leaves out `*.runs.json`, and `_read_task_record` refuses a record without
an `id`. Before, every reader globbed `*.json`, and a run log loaded as a nameless daily 06:00
automation with a new random id on every load: it could not be deleted (the list could never
name it twice the same way), it came back after each restart, and the scheduler armed it with
an empty prompt. The one that surfaced was the log of a one-time task, written after the task
had already been deleted at the end of its run; `append_run_log` now writes only beside a task
file that still exists.

`review_findings()` in [vaf/core/automation.py](../../vaf/core/automation.py)
computes what can be checked from those records: never completed, no successful
run since a date, disabled and forgotten, two automations in the same time slot,
near-identical instructions, and - once the log has entries - repeated recorded
errors. The background agent's automation-review rung phrases ONE of them and
proposes a fix; it cannot edit an automation itself (the write tools are refused
on that node). See [Thinking-Mode.md](../agents/Thinking-Mode.md#proactive-intelligence-level-2).

**What is deliberately NOT a finding:** a "dead output path". `run_task`
resolves folder aliases (`Desktop`, `downloads`) against the home directory and
creates a missing output directory before writing, so a bare existence check
flags a relative alias that works and an absolute path the next run would simply
create. The one case that is a real problem - a path VAF cannot create - makes
the run fail, and a failed run is now in the log, where it is reported from
evidence rather than from a guess.

## Event triggers: an automation that runs when a room says something

A task with `frequency: on_event` is not due by the clock. It carries a `trigger`:

| Field | Meaning |
|---|---|
| `kind` | `room_message` (something is SAID in the room) or `room_reaction` (an emoji lands on a message) |
| `room_id` | the agent room to watch; the task's owner must be a member on some lane |
| `match` | `room_message` only: run only when the text contains this (folded, so "Deploy" finds "deploy") |
| `emoji` | `room_reaction` only: run only for this emoji; absent means any reaction |
| `from` | optional: only frames from this peer handle. WITHOUT it any member of the room can fire the automation, an invited foreign agent included |
| `on_frame` | `room_reaction` only: run only for a reaction on THIS message id |
| `cursor` | written by the scheduler after a fire: how far the trigger has read |

The "is it due" decision lives in `vaf/core/automation_triggers.py` (`RoomTriggerWatch`,
exported on the facade), and the scheduler loop asks it on the same 30-second tick the
reminders ride, so only the process singleton ever fires one. The run itself is the
ordinary scheduled run (`_run_scheduled_task`): in-process, background, `set_current_session_id(None)`,
the owner's scope. What triggered it reaches the run as text appended to the prompt on the
prompt lane and as `{trigger_text}`, `{trigger_room}`, `{trigger_kind}`, `{trigger_frame_id}` and
`{trigger_from}` on the workflow lane; it is never stored on the task.

Three rules, each written after the failure it prevents. **The owner's own agent never fires a
trigger**: the run is that agent, and a run that posts into the room and re-triggers itself is a
loop nothing else would stop - the owner's own PERSON does fire one, which is what makes a
reaction trigger an approval button - but only once it is NARROWED. A trigger with neither
`from` nor `on_frame` fires on any reaction by any member on any message, which is a
room-wide signal and a different thing; `from` the person plus `on_frame` the report is
what makes it an approval on that one report by that one person. This is a start trigger
either way: it begins a run, it does not release a run that is waiting. A workflow step
that WAITS for approval does not exist (`vaf/workflows/resume.py` records that continuing
a paused multi-step run outside the interactive CLI was considered and refused for want of
an owner process), so do not describe this as a workflow gate. **Membership is checked every tick**: a trigger naming a room the
account is not in is nothing, not an error. **The cursor starts at the room's newest frame**
the first time a process sees the task when the task carries no position, never at zero, so a trigger created today does not fire
on last week's transcript; after a fire the position is persisted, so a restart cannot fire the
same frames twice and frames that arrived while the process was down are caught up in one run.
**A hit runs only once its position is on disk.** The position is written onto the task's
record as it is at that moment, under the store's lock, and that is also where the hit is judged
against the task as it is now. Nothing is written and nothing runs when the task is gone or was
created anew under the id, is no longer an event task (changed to a clock schedule with its old
trigger still on the record), or has already been read this far (another process wrote a position
at or past this one: a commit never writes a position below the stored one). A task whose rule on
disk is no longer the one the scheduler's copy matched (another room, text or emoji; every field
but the cursor is compared) is not run either: the watch is moved back (below), so the frames are
read again under the rule on disk. A task switched off gets its position written and no run. The run gets the task as it is on disk, not the scheduler's older copy, and
only the frames past the position the record carried.

When the position cannot be written (the store busy for five seconds, not writable, or the task
file not readable while it is there), nothing is kept in memory: the watch is moved back to where
that hit's read started (`RoomTriggerWatch.rewind`), so the next tick reports the same frames
again and every check runs afresh, membership included. Once one account's store is busy in a
tick, that account's other hits are moved back without a second wait, so a busy store costs one
wait per tick, not one per hit. Each hit's run is started on its own: one that cannot start does
not touch the others (its position is written, so its frames count as consumed). The watch keeps
each position bound to the room it counts in and to the task it was read for: positions count per
room, and a task created anew under an id is set again as on first sight.

Where the position goes when the task changes: `create` writes a task without one, so a task
created from a stored one (a replacement) starts at the room's newest frame and never runs what
was said before it existed. An update that keeps the trigger in the same room keeps the higher of
the stored and the given position (a lane that sends the trigger again, such as a prompt edit
through the agent's tool, does not move it back); one that moves the trigger to another room
drops it, the old room's number means nothing there. Named boundary: a trigger moved to another
room and back between the ticks of two scheduler processes (the web app and a terminal that
started its own) comes back without a position, so the process whose watch is behind can run
frames the other has already run.

A task restored from the trash keeps its position: the trash keeps the record whole, with its
`incarnation` and its trigger's cursor, so what was said in the room while it was in the trash
can run it once after the restore, as after a restart.

The per-task run lock keeps a trigger that fires again mid-run from overlapping it. Named
boundary: such a hit's position is already written when the run is refused as "already
running", so its frames are consumed without a run of their own.

**A prompt-lane run a room message started is a room turn.** The prompt is the user's, but the
words steering the run are whoever spoke in the room, so the run is bound like the agent's own
room turn (`_room_trigger_binding` in `vaf/core/automation.py`, through `bind_turn`): the room's
mode for the account's agent decides the writes (`room_send` posts nothing in an `observe`
room); a trigger whose frames are not all the user's own reads only the room, the web and the
room's shared folder, changes only its posts in that room and files in that folder, and no tool that reads the user's mail, chats, calendar, contacts or
memory runs (the memory the run is handed up front from what was said stays, the retrieval
boundary of a room turn); the system prompt leaves out the user's profile; and the run's
working memory is the room's conversation
of its own, not the account's global notes. Who spoke is asked of the room
(`Room.wake_authority`), the same answer the wake uses; a room the run cannot read binds the
closed answer (`observe`, not the user's words, no shared folder). A run that outlives its time
limit and grace is abandoned with its bound in place: its worker may still call tools, so the
agent's room marker is not cleared under it. It stays unattended: an
outward send it was told to make is not held for a person. A trigger narrowed with `from` to
the user's own handle keeps the user's reach. The rule is in
[A2A_PROTOCOL.md](../agents/A2A_PROTOCOL.md), "Who woke the turn".

NAMED BOUNDARY - the workflow lane. Its steps are the ones the user wrote, and the frames reach
them only as `{trigger_*}` text; a model step there (for example `coding_agent`) reads that
text without the room's bound. Clock automations and `vaf automation run <id>` (no trigger) are
unchanged.

Creating one: the agent's `create_automation` with `frequency: on_event`, `trigger_room` and
`trigger_match` or `trigger_emoji`; the terminal's `vaf automation create --on-room <id>
[--on-match <text> | --on-reaction <emoji|any>]`; the browser accepts a `trigger` object on
`create_automation` and `update_automation` and shows the rule where a clock task shows its
time. The browser's create form is clock-only for now: the first creation lane is the agent,
the way `create_automation` itself started, and a form field for a room, a text and an emoji
in seven languages is its own round. Task files carry the format tag `automation-1-4e9b27`;
untagged files from before it are read as before, and a version that does not know the field
keeps such a task with no next run and never fires it.

## Related

- [WEB_UI.md](../web-ui/WEB_UI.md) – Settings → Automations and automation calendar UI.
- [USER_ISOLATION.md](../security/USER_ISOLATION.md) – Per-user automations and planner storage.
- [WEBUI_WEBSOCKET_FLOW.md](../web-ui/WEBUI_WEBSOCKET_FLOW.md) – WebSocket messages for automations and planner.
- [TOOL_ROUTER_ARCHITECTURE.md](../agents/TOOL_ROUTER_ARCHITECTURE.md) – When `create_automation` is forced by the router.
