# Thinking Mode

Thinking mode runs the main agent in the background while the user is idle. It acts on the user's behalf: processes todos, creates automations, sends proactive messages, and can ask the user a question via their configured `main_messenger` (Telegram, WhatsApp, Discord, Slack) — or, when no messenger is configured, as a tracked `ask_user` message delivered to the user's Web UI chat (e-mail is never used). Runs are multi-turn until the agent calls `thinking_done` (or a turn limit is reached).

---

## Overview

- **When it runs:** After `thinking_idle_minutes` of no activity across ALL linked channels (Web UI, Telegram, WhatsApp, etc.).
- **What it does:** One run = a short multi-turn gather→decide→act pass. The agent reads context (incl. memory) but runs with **background-safe** limits: no `memory_save`/Git, and no direct `update_user_identity`/`set_timer` (those are *proposed*, not applied — see [Background requests](#background-requests-handoff--no-re-processing)). The per-turn tool-cycle budget is far tighter than the main chat (`thinking_max_tool_turns`, default 15) to prevent tool-spin. The agent must call `thinking_done` when finished.
- **Contacting the user (`ask_user`):** The agent reaches the user with the explicit **`ask_user`** tool — a clean, user-facing `message` (so chain-of-thought can no longer leak into the chat) + an optional `proposed_action` + optional `details` (the real content behind a teaser). The message is delivered and **tracked as a request**. The run then **ends immediately** (`run_has_open_request` → break) — max one message per run, then it waits for the reply; continuing would leave the background run racing the main agent on the shared local model. Any question is persisted to the user's main chat history so the main agent has full context when they reply.
- **Fallback delivery via `thinking_done(message=...)`:** Contacting the user must always go through a **tool call** — a question written as plain assistant text is never delivered (it would otherwise leak reasoning). Because a weak local model sometimes composes the question but forgets to call `ask_user`, `thinking_done` accepts the same optional `message`/`proposed_action`/`source_note_id`/`source_todo_id`; both routes share one delivery path (`deliver_tracked_message`) that records the request, sets `waiting_for_reply`, and emits to the Web UI. A `run_has_open_request` guard prevents a second message when `ask_user` already raised one this run.
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

## Background requests, handoff & no re-processing

**Notes/todos are actionable by default.** The prompt treats every automation note or todo as a task
the user *deliberately saved* — the agent must act on it (e.g. research + a concrete suggestion) or ask
ONE specific question; it must never dismiss a note as "just venting/a complaint", and it only concludes
"Nothing actionable" when the lists are genuinely empty. (Note: a small local model may still judge
conservatively — a stronger `thinking_model` improves this.)

When the agent asks the user something (via `ask_user`) it is recorded as a **request** with a status
lifecycle, so the background run and the main agent stay coordinated and nothing is asked or done twice:

- **Lifecycle:** `asked` → `confirmed` / `declined` → `done`. Stored per user in
  `thinking_requests/<scope>/requests.json` (see `vaf/core/thinking_requests.py`).
- **Handoff to the main agent:** the request is linked to `waiting_for_reply`. When the user replies in
  chat, `chat_step` loads the request, injects its `proposed_action` as context ("if they confirm, carry
  it out now"), and advances the status — refusal → `declined`, agreement → `confirmed` → `done` once
  handled. So the **main agent carries out** what the background agent proposed.
  - **NOTE:** the WebUI message handler must NOT clear `waiting_for_reply` first — the main agent's
    `chat_step` needs it to build the reply context. (It was clearing it pre-emptively, which left the
    main agent with no context and an off-topic answer; observed 2026-06-22.)
- **Content handoff (`details`):** when a message teases something the run found (e.g. "I found 15
  cooling methods, want the list?"), the run passes the ACTUAL content in `ask_user(..., details="…")`.
  `details` is stored on the request (not shown to the user) and injected into the main agent's reply
  context, so a follow-up ("which ones? list them") is answered with the **real findings** instead of a
  re-derived/made-up version. If `details` is absent, the context tells the main agent to look facts up
  (e.g. `web_search`) rather than invent them.
- **Don't re-ask:** every run injects the requests raised in the last `thinking_recent_request_runs`
  runs (default 6) with their status, so the agent does not repeat a question it already asked, follows
  up on `confirmed` ones, and never re-proposes a `declined` one. This is also enforced at the gate
  level: `thinking_ledger.item_resolved(..., recent_runs=N)` treats an item with a request raised in the
  last N runs as handled-for-now, so the forced-resolution node does not re-ask it (and the completion
  gate does not block on it) while the user has not yet replied; it re-surfaces after the window.
- **Multiple notes/todos:** the ledger lists **todos before notes** (a todo usually converts into an
  automation — an act with no message; a note usually resolves by sending help/a question, which ends the
  run — max 1 message). The forced node resolves the first open item each turn; act continues to the next
  item, an `ask_user` ends the run. So a run does as much act-able work as it can, then asks at most once,
  and the remaining items are handled in subsequent runs.
- **Todos become automations:** a todo is a task with a `due_at` deadline that the agent should turn into
  an automation so it isn't forgotten. The forced node gives the agent the deadline as **scheduling
  context** and resolves the todo by:
  - **Reminder** (just notify the user near the deadline) → the agent builds it **autonomously**
    (`create_automation(frequency, time, prompt="Remind …")`, schedule chosen to fit the deadline) and then
    clears the todo (`delete_automation_todo`).
  - **Action automation** (does something externally visible) → the agent does **not** build it; it asks
    via `ask_user(..., proposed_action=, source_todo_id=)` and the main agent builds it on confirm.
  - **Known limitation:** `create_automation` schedules by time-of-day/frequency (once = next occurrence),
    not a specific future date (`automation.py calculate_next_run`), so a one-time reminder cannot be
    pinned to an arbitrary deadline date — the agent picks from `once/daily/weekly/monthly` and names the
    real deadline in the reminder text. A date-aware one-time trigger is a possible future enhancement.
- **Processed notes/todos disappear:** automation **todos** marked `done` and automation **notes**
  marked `handled` are filtered out of the gather **and** the user's list (`list_notes` excludes handled
  by default), so a processed item never re-surfaces and the agent cannot loop on it. If a question
  stems from a note/todo, pass `source_note_id`/`source_todo_id` to `ask_user`; on confirm the linked
  note is marked handled / todo marked done **automatically** — even if the model forgets to clear it
  (`automation_planner.set_note_handled`).

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
| `thinking_max_tool_turns` | `15` | Hard cap on tool-result cycles per background turn (the main chat uses 75). Stops weak models from tool-spinning in the background. |
| `thinking_recent_request_runs` | `6` | A question/proposal counts as "recently asked" for this many runs, so the agent does not re-ask it. |
| `thinking_gate_enabled` | `true` | Completion gate: nudge once if a captured note/todo is still unhandled before `thinking_done` is accepted. |
| `thinking_read_cap_enabled` | `true` | Block excessive read/gather tool calls in a thinking run (`memory_search` / `web_search` spin etc.). |
| `thinking_read_cap_per_tool` | `3` | Nth call of a read tool (`memory_search` / `web_search` / `list_*`) within one step is blocked. |
| `thinking_no_progress_turns` | `5` | After this many turns with no decisive (act/ask/clear) tool, force a one-tool decision (no more searching). The run's turn budget is sized to give this room. |
| `model_unload_idle_minutes` | `30` | **Desktop only.** Unload the local model after the user is *really* away (no message) this long — even with the WebUI open. Server/headless never unloads (no watchdog). |
| `thinking_proactive_enabled` | `true` | When the floor (notes/todos) is clear, run a proactive memory-mined suggestion scan (Stufe 2). |
| `thinking_proactive_evidence_min_chars` | `24` | Evidence-gate: a proactive suggestion's `details` must quote ≥ this many chars verbatim from real retrieved memory/history, or it is dropped. |
| `thinking_proactive_min_runs` | `6` | Min runs between proactive outreaches (anti-spam). |
| `thinking_proactive_memory_k` | `4` | Per-query top-K when the proactive step pre-fetches real memories to hand the model (it may also `memory_search` once itself). |

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

## Proactive intelligence (Stufe 2)

When the housekeeping floor is clear (no open notes/todos) and proactivity is enabled
(`thinking_proactive_enabled`) and not rate-limited, the run runs a **forced proactive flow** —
**silence is not the goal**, the run always ends by asking the user *something*:

- **Real memories are handed to the model (not left to the weak 4B to fetch):** before the grounded
  turns, `_build_proactive_memory_digest` retrieves a targeted sample of the user's REAL memories **in
  code** — several queries aimed at proactive value (recurring routine, current work, preferences),
  `thinking_proactive_memory_k` (default **4**) each, deduped. This is necessary because the local model
  rarely searches on its own and the forced grounding turn cannot gather. The digest is injected into the
  grounded prompt AND seeded into the evidence pool (so a verbatim quote of it passes the gate).

1. **Grounded suggestion (forced, evidence-gated — TWO passes):** find ONE genuinely useful thing —
   prioritising an **automation opportunity** (something the user does/asks for *repeatedly*, not already
   covered by an existing automation → `ask_user(message="…", proposed_action="create automation: …",
   details="<quote>")`; on "yes" the **main agent builds the automation**). The turn is forced
   (`tool_choice="required"`) so the weak model can't churn on prose — but, unlike the housekeeping
   forced node, **`memory_search` is allowed here** (`allow_memory_search` → `_thinking_allow_search`):
   the model may dig into ONE specific thing itself (still read-capped, so no churn), and its results are
   added to the evidence pool live. Two passes so a pass-1 search can become a grounded `ask_user` in
   pass 2. The `ask_user` is evidence-gated; otherwise it falls to the get-to-know question.
2. **Get-to-know question (forced fallback, NOT gated):** if the grounded passes produced nothing, the run
   does **not** finish silently — it asks ONE specific question to get to know the user better (their
   focus/work, a routine they'd like automated, an interest), so future runs can help. A question states
   no fact, so it is exempt from the evidence-gate. The completion gate keeps the run from finishing while
   this is still pending (`_proactive_step < 3`).

- **Message gate (3 modes, set per turn in `deliver_tracked_message`):** a FREE message (no
  `source_note_id`/`source_todo_id`) is governed by the run's mode (`thinking_mode.set_proactive_mode`):
  - **`off`** (gather / forced-resolution): a free message is **blocked** — this kills the generic turn-0
    floskel ("no tasks, I'm ready when you need me") that previously slipped through before the proactive
    flow even ran.
  - **`grounded`** (proactive grounding passes): delivered only if `details` quote a verbatim, normalized
    substring of ≥ `thinking_proactive_evidence_min_chars` from this run's REAL retrieved memory/history
    (the per-run **evidence pool** = turn-0 `memory_context` + the pre-fetched proactive digest + recent
    user messages + every `memory_search` result captured live, including the model's own searches).
    Otherwise silently dropped — anti-fabrication.
  - **`open`** (get-to-know step): delivered (a question states no fact, so it cannot fabricate).
  Housekeeping deliveries (carrying a source id) are always exempt. Better silent/blocked than fabricated.
- **Anti-spam:** at most one proactive outreach per `thinking_proactive_min_runs` (a proactive request is
  one with no source ids); the existing recent-requests + declined-questions prompts prevent repeats.
- **Honest limit:** with the local 4B the *cleverness* is model-bound — realistic output is surfacing a
  real, citable thing + one concrete step, kept safe by the strict gate (a stronger thinking model would
  raise quality; not enabled).

---

## Run flow

1. **Loop:** Background thread runs `thinking_loop_iteration()` every `thinking_check_interval_seconds`.
2. **Quiet hours:** Checks if current local time is inside the prohibited window.
3. **Eligibility & Alias Mapping:** For each user, the system finds the *absolute newest* activity timestamp across all their known aliases (Web UUID, Telegram ID, Admin name). If any alias is active, the user is NOT idle.
4. **Lock:** `acquire_lock(user_scope_id)` returns a `run_id`.
5. **Run:** `_run_thinking_for_user()` runs in a daemon thread:
   - Sets environment flags and creates an `Agent`.
   - **Loads user's chat session** to ensure context parity.
   - **Stufe-0 ledger:** a deterministic snapshot of the open notes/todos is built at run start
     (`thinking_ledger.build_ledger`). It is the housekeeping floor the run must clear.
   - **Content-driven ladder (not turn-count):** turn 0 gathers; from turn 1, each open note/todo is driven
     through a **forced-resolution node** (the model is compelled via `tool_choice="required"` + disabled
     gather to emit `ask_user`/`delete_*` for that item — see Loop protection). Once the ledger is resolved
     the prompt switches to optional proactive upkeep (Stufe 1). No "wrap up now / FINAL TURN" pressure.
   - **Completion gate:** before a `thinking_done` is accepted, each ledger item must be resolved
     (acted-and-cleared, or a tracked question raised this run carrying its `source_note_id`/`source_todo_id`).
     If not, the run gets ONE targeted nudge naming the specific items, then continues; a second unresolved
     check accepts termination and logs `[THINKING_GATE] incomplete`.
   - **History Synchronization:** If the agent sends a message (e.g., via `send_telegram`), that message is instantly appended to the main session's history on disk.
   - **Real-time Abort Check:** Before each turn, the agent checks if the user's logical ID has seen new activity. If so, it breaks the loop immediately.
6. **After run:** Logs are saved and waiting states are updated.
7. **Unlock:** Lock is released; cooldown recorded.

---

## Loop protection (API cost safety)

To prevent runaway API usage (e.g. the model repeatedly calling `thinking_done` or the same tool with the same arguments), the following safeguards apply:

- **`thinking_done` hard break:** When the model calls `thinking_done`, the agent’s internal tool loop exits immediately. The dispatch is special-cased in `vaf/core/agent.py` (chat_step tool loop) and returns before the normal tool execution — so it also runs the `thinking_done(message=...)` delivery inline via `deliver_thinking_done_fallback` (otherwise the message fallback would be silently dropped). No further API request is made for that turn; the tool result is written to history and the run ends.
- **Completion gate (guards the exit):** the OUTER thinking loop (`thinking_mode.py`) does not accept the first `thinking_done` while a captured note/todo is unresolved — it injects ONE targeted nudge and continues. Single-shot per run (`thinking_gate_enabled`). See [Run flow](#run-flow).
- **Forced-resolution node (the enforceable gate-tree):** a weak local model tends to narrate its intended action as prose instead of emitting the tool call (observed repeatedly: it wrote "I'll call ask_user…" and even composed the full suggestion as text, but only ever called `web_search`). So housekeeping is **enforced**, not requested: for each open ledger item, the loop drives a forced node — it calls `chat_step(..., force_tool_choice="required")` with a per-item prompt, and during that step the read-cap blocks ALL gather tools from the first call (`_thinking_force_progress`). The model therefore **must** emit a decisive tool (`ask_user` / `delete_automation_*`) for that item — it can no longer escape into search or prose. The force applies to the first generation of the step only, then reverts to `auto` so the model can finish. `tool_choice` is honoured by the local llama-server (verified). In `vaf/core/thinking_mode.py` (`_build_forced_item_prompt` + the outer loop) and `vaf/core/agent.py` (`chat_step(force_tool_choice=...)`).
- **Progress-gate (backstop against spinning):** the completion gate only fires *when* the model calls `thinking_done`. As a backstop for the proactive rung, the progress-gate counts consecutive turns with no decisive tool (`ask_user`, `delete_automation_*`, `create_automation`, `save_thinking_suggestion`, `thinking_done`) and, after `thinking_no_progress_turns` (default **5**), forces a single-tool decision. In `vaf/core/thinking_mode.py` (`_turn_used_progress_tool` + the outer loop).
- **Read-tool cap (anti-churn):** in a thinking run a read/gather tool (`memory_search`, `web_search`, `list_automation_notes/todos`, `list_automations`) is blocked after `thinking_read_cap_per_tool` (default **3**) calls within one step, returning a result that tells the model to act. This catches the varied-query `memory_search`/`web_search` spin the redundant block misses (it needs exact args). Gated by `VAF_THINKING_MODE` (`_thinking_read_cap_step` in `vaf/core/agent.py`); the main chat is unaffected.
- **Max tool turns per step:** A background thinking turn is capped at `thinking_max_tool_turns` (default **15**) tool-result cycles; the main chat uses a higher cap (75). If the model keeps calling tools without finishing, the run stops at the cap. Enforced in the chat_step tool loop in `vaf/core/agent.py` — this tighter background cap stops the tool-spin observed on weak local models.
- **Redundant tool call block:** If the model calls the same tool again with the same arguments (already executed in context), that call is blocked and the internal retry counter is incremented so the run can hit the empty/fallback stop logic sooner.
- **Logging:** When any of these triggers, `[LOOP_PROTECTION]` / `[THINKING_GATE]` / `[THINKING_READ_CAP]` is written to `logs/backend_YYYY-MM-DD.log` (and visible in run summaries). Examples: `thinking_done detected - breaking loop`, `blocked memory_search`, `GATE nudge`.

These apply to both normal chat and thinking mode. Run logs in `logs/vaf_think_YYYY-MM-DD.log` remain the main place to inspect thinking runs.

---

## Tools available in Thinking Mode

The agent has **the same tools as the normal agent**, with these exceptions:

| Tool | Status | Reason |
|------|--------|--------|
| `ask_user` | **Only in Thinking Mode** | The single, tracked channel to contact the user (clean `message`; no chain-of-thought leak); records a request |
| `thinking_done` | **Only in Thinking Mode** | Signals end of the run; optional `message`/`proposed_action`/`source_note_id` deliver a final question as a fallback for `ask_user` (same tracked path) |
| `thinking_note_add` | **Only in Thinking Mode** | Saves persistent notes for next run |
| `save_thinking_suggestion` | **Only in Thinking Mode** | Proposes user-profile updates for review in Settings |
| `memory_save` | ❌ Excluded | Thinking should read memory, not write to it |
| `update_user_identity` | ❌ Excluded | Do not mutate the profile in the background — propose via `save_thinking_suggestion` |
| `set_timer` | ❌ Excluded | Do not schedule user-facing actions directly — propose via `ask_user`; the main agent sets it on confirm |
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
- **Model-unload coordination (desktop only):** the desktop tray's local-model unload watchdog
  (`vaf/tray.py` `check_activity_loop`) shares this idle signal. It unloads the local model only once the
  user is really away (`model_unload_idle_minutes`, by last message — even with the WebUI open) AND
  `thinking_mode.should_defer_model_unload()` is false (no run active or due). So the model stays loaded
  while a thinking run is happening or imminent — think first, then unload. Server/headless runs no
  watchdog, so the model is never unloaded there.

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
| `thinking_requests/<scope>/requests.json` | Per-user background requests + status (`asked`/`confirmed`/`done`/`declined`) from `ask_user` or the `thinking_done(message=)` fallback. Fields incl. `proposed_action`, `details` (real content for the handoff), `source_note_id`/`source_todo_id` |
| `thinking_run_seq.json` | Per-user monotonic run counter (drives the "recently asked" window and the gate's "this run" boundary) |
| `automation_planner/<scope>/notes.json` · `todos.json` | Per-user notes/todos — the Stufe-0 ledger source; a note is cleared by delete or `handled`, a todo by delete or `done` |
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
| Background requests | `vaf/core/thinking_requests.py` | `add_request()`, `update_request_status()`, `list_requests()`, `recent_requests_prompt()` |
| Run counter | `vaf/core/thinking_mode.py` | `next_run_seq()`, `current_run_seq()` |
| Stufe-0 ledger & gate | `vaf/core/thinking_ledger.py` | `build_ledger()`, `item_resolved()`, `unresolved_items()`, `build_gate_nudge()` — the completion-gate logic (pure, unit-testable) |
| Completion gate (loop) | `vaf/core/thinking_mode.py` | `_run_thinking_for_user()` outer loop — single-shot nudge before accepting `thinking_done` |
| Forced-resolution node | `vaf/core/thinking_mode.py` · `vaf/core/agent.py` | `_build_forced_item_prompt()` + outer loop; `chat_step(force_tool_choice="required")` — compels a decisive tool call per open item |
| Progress-gate | `vaf/core/thinking_mode.py` | `_turn_used_progress_tool()` + outer loop — backstop one-tool decision after N gather/analyse-only turns |
| Read-tool cap | `vaf/core/agent.py` | `_thinking_read_cap_step()` — per-step anti-churn incl. `web_search`; blocks ALL gather on a forced node (`_thinking_force_progress`) |
| `ask_user` tool | `vaf/tools/ask_user.py` | `AskUserTool` — tracked user contact + request creation (Thinking Mode only) |
| Shared delivery | `vaf/core/thinking_mode.py` | `deliver_tracked_message()`, `run_has_open_request()`, `deliver_thinking_done_fallback()` — one path for `ask_user` + the `thinking_done` fallback (also called from the agent's thinking_done dispatch) |
| Notes handled flag | `vaf/core/automation_planner.py` | `set_note_handled()`, `list_notes(include_handled=False)` |
| Main-agent pickup | `vaf/core/agent.py` | `chat_step()` — loads the request, carries out `proposed_action`, advances status |
| `thinking_done` tool | `vaf/tools/thinking_done.py` | `ThinkingDoneTool` — end of run; optional `message=` fallback delivery |
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
