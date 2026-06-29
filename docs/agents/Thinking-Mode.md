# Thinking Mode

Thinking mode runs the main agent in the background while the user is idle. It acts on the user's behalf: processes todos, creates automations, sends proactive messages, and can ask the user a question via their configured `main_messenger` (Telegram, WhatsApp, Discord, Slack) — or, when no messenger is configured, as a tracked `ask_user` message delivered to the user's Web UI chat (e-mail is never used). Runs are multi-turn until the agent calls `thinking_done` (or a turn limit is reached).

---

## Overview

- **When it runs:** After `thinking_idle_minutes` of no activity across ALL linked channels (Web UI, Telegram, WhatsApp, etc.).
- **What it does:** One run = a short multi-turn gather→decide→act pass. The agent reads context (incl. memory) but runs with **background-safe** limits: no `memory_save`/Git, and no direct `update_user_identity`/`set_timer` (those are *proposed*, not applied — see [Background requests](#background-requests-handoff--no-re-processing)). The per-turn tool-cycle budget is far tighter than the main chat (`thinking_max_tool_turns`, default 15) to prevent tool-spin. The agent must call `thinking_done` when finished.
- **Contacting the user (`ask_user`):** The agent reaches the user with the explicit **`ask_user`** tool — a clean, user-facing `message` (so chain-of-thought can no longer leak into the chat) + an optional `proposed_action` + optional `details` (the real content behind a teaser). The message is delivered to the user's configured **main channel** (Telegram/WhatsApp/Discord, or the Web UI if none) and **tracked as a request** — the agent does NOT pick the channel, `ask_user` routes it. If it stays unanswered on a messenger, it is escalated **once** to the Web UI with a note that it was already asked there. The run then **ends immediately** (`run_has_open_request` → break) — max one message per run, then it waits for the reply; continuing would leave the background run racing the main agent on the shared local model. Any question is persisted to the user's main chat history so the main agent has full context when they reply.
- **Fallback delivery via `thinking_done(message=...)`:** Contacting the user must always go through a **tool call** — a question written as plain assistant text is never delivered (it would otherwise leak reasoning). Because a weak local model sometimes composes the question but forgets to call `ask_user`, `thinking_done` accepts the same optional `message`/`proposed_action`/`source_note_id`/`source_todo_id`; both routes share one delivery path (`deliver_tracked_message`) that records the request, sets `waiting_for_reply`, and delivers via the user's main channel (`send_to_main_messenger`) — Telegram/WhatsApp/Discord, or the Web UI when no messenger is configured. A `run_has_open_request` guard prevents a second message when `ask_user` already raised one this run.
- **Outbound channel guard:** before the run starts, `_filter_thinking_send_tools()` removes **all** `send_*` tools. The agent contacts the user only via `ask_user`, which routes the tracked message to the configured `main_messenger` (Telegram/WhatsApp/Discord) or the Web UI — so a raw send tool is never needed and can't cause an untracked or duplicate send by a weak model. `send_mail` is likewise never available: e-mail is not a `main_messenger` value, and an unguarded run once tried to mail a hallucinated address (`mert@example.com`). The prompt carries the same rule, the registry filter enforces it.
- **Per user:** Idle is tracked per logical user (handling all UUID/username aliases). One run at a time per user (serialized by lock). Cooldown between runs: `thinking_cooldown_minutes` (default 110 min).
- **Dead-session cap:** A scope ID that has been silent longer than `thinking_max_idle_age_hours` (default 7 days) is treated as a dead/orphan session, not an idle user, and is skipped. Without this, stale web-session scope IDs left in `last_interaction.json` are each seen as a separate idle user and generate a phantom run every cooldown window indefinitely. **Registered accounts** (any user present in `local_users`, including the admin) are exempt — only an unknown orphan UUID is dropped — so a genuinely long-away but registered LAN user still gets runs, not just the admin.
- **Safety Abort:** If the user becomes active on any channel during a run, the thinking process is immediately aborted to prevent dual-agent responses.
- **Workflow Guard:** Thinking mode does not start while a workflow is executing (`VAF_IN_WORKFLOW_TERMINAL=1`). This prevents idle messages from interrupting long-running workflow steps.
- **Workflow router off:** during a background run the workflow router does not run on the thinking prompt (`chat_step` gates it on `not thinking_mode`). Otherwise the router would match the run's own proactive prompt (which mentions e.g. "automatisch um 7:00" / "create automation"), inject a `[WORKFLOW SUGGESTION]` nudge with mis-extracted variables, and steer the run toward a fabricated "create a timer" proposal. A background run has no user request to route; it only does its own housekeeping.
- **Busy gate (per user) + shared-model guard:** A background run is gated by the **same user's** own activity: it does **not** start, and a proactive question is not delivered, while that user has a turn in flight or queued (`TaskQueue`, matched per scope) — so a user's own live turn never collides with their background run. **Another user's activity does not block them**; different users' thinking runs are independent (important for multi-user/LAN). **Exception — one shared local model:** when the run would share the **same local model** as the main chat (main `provider=local` and `thinking_provider` is `inherit`/`local`), a global guard also applies — a run does not start while *any* main turn is busy on that single server, since there is real model contention. With a **different** provider (thinking via API while main is local, or vice versa) there is no contention and it runs concurrently.
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

- **Lifecycle:** `asked` → `replied` → `done` / `declined` (a `replied` outcome the next run finds
  ambiguous re-opens to `asked` with `needs_reconfirm` for ONE soft check-back). Stored per user in
  `thinking_requests/<scope>/requests.json` (see `vaf/core/thinking_requests.py`). `confirmed` is a legacy
  status kept valid for old entries.
- **Handoff to the main agent:** the request is linked to `waiting_for_reply`. When the user replies in
  chat, `chat_step` loads the request, injects its `proposed_action` as context ("if they confirm, carry
  it out now") — so the **main agent still carries out** a clear "yes" immediately — and **captures** the
  exchange: it records the user's reply (at pickup) and its own reply (at end-of-turn), moving the status
  to `replied`. The main agent does NOT decide accepted-vs-declined here.
- **Outcome classification (next run):** the background run that owns the question classifies each
  `replied` request from the full triple {its question, the user's reply, the main agent's own reply
  (capped)} via one small LLM call (`_classify_replied_requests` → `agent._generate_for_classification`):
  `ACCEPTED` → `done` (+ any source note/todo marked handled), `DECLINED` → `declined` (+ the
  declined-questions log), `UNCLEAR` → re-open to `asked` with `needs_reconfirm` so the follow-up node
  asks ONE soft retrospective recap ("Hey, sorry — hatten wir das eigentlich gemacht?"). This LLM-based
  decision replaces a former brittle keyword guess (substring match — "no" matched "noch"), which
  silently mis-classified keyword-free declines as agreements.
  - **NOTE:** the WebUI message handler must NOT clear `waiting_for_reply` first — the main agent's
    `chat_step` needs it to build the reply context. (It was clearing it pre-emptively, which left the
    main agent with no context and an off-topic answer; observed 2026-06-22.)
  - **Reply context overrides stale state:** the injected reply note states that the user's message is a
    reply to *that* background question, and that the earlier `<user_intent>`/working-memory `<Plan>`
    (which may concern an unrelated topic) must be ignored for this turn. Without the override, a stale and
    more prominent intent/plan block could dominate the prompt and the main agent would answer about the
    wrong topic (observed: a reply to a background question was answered as if it were about an unrelated
    earlier request).
- **Content handoff (`details`):** when a message teases something the run found (e.g. "I found 15
  cooling methods, want the list?"), the run passes the ACTUAL content in `ask_user(..., details="…")`.
  `details` is stored on the request (not shown to the user) and injected into the main agent's reply
  context, so a follow-up ("which ones? list them") is answered with the **real findings** instead of a
  re-derived/made-up version. If `details` is absent, the context tells the main agent to look facts up
  (e.g. `web_search`) rather than invent them.
- **Don't re-ask:** every run injects the requests raised in the last `thinking_recent_request_runs`
  runs (default 6) with their status, so the agent does not repeat a question it already asked, follows
  up on still-open ones, treats a `replied` one as awaiting classification (does not re-ask it), and
  never re-proposes a `declined` one. This is also enforced at the gate
  level: `thinking_ledger.item_resolved(..., recent_runs=N)` treats an item with a request raised in the
  last N runs as handled-for-now, so the forced-resolution node does not re-ask it (and the completion
  gate does not block on it) while the user has not yet replied; it re-surfaces after the window. These
  prompts are **text-based** (they block the same wording); a **semantic** layer additionally blocks the
  same *topic* reworded — see [Semantic de-duplication](#proactive-intelligence-stufe-2).
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

Key options (in `config.json` or via Web UI **Settings → AI & Model → Thinker (background)**):

| Key | Default | Purpose |
|-----|---------|---------|
| `thinking_enabled` | `true` | Enable thinking mode when idle |
| `thinking_idle_minutes` | `10` | Start after this many minutes without activity |
| `thinking_max_idle_age_hours` | `168` | Upper bound on idle age (default 7 days). A non-admin scope silent longer than this is treated as a dead/orphan session and never runs. `0` disables the cap. |
| `thinking_check_interval_seconds` | `60` | How often to check for idle users |
| `thinking_cooldown_minutes` | `110` | Minutes to wait after a run before starting another |
| `thinking_max_duration_minutes` | `30` | Max duration per run (then release lock) |
| `thinking_wait_nudge_minutes` | `3` | If user does not reply: send nudge after this many minutes |
| `thinking_followup_max` | `3` | When a proactive question is unanswered, re-ask the SAME one (pointed follow-up) up to N times, then let the topic rest (no question, no nudge) until the user reacts. |
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
| `thinking_proactive_evidence_min_chars` | `24` | Evidence-gate **backstop** (local model): a proactive suggestion's `message` or `details` must quote ≥ this many chars verbatim from real retrieved memory/history, or it is dropped. Secondary to the un-forced prompt that forbids inventing — see [Proactive intelligence](#proactive-intelligence-stufe-2). |
| `thinking_proactive_evidence_min_chars_api` | `12` | Evidence-gate backstop when the thinking run uses a hosted model (lenient bar; selected automatically by provider). |
| `thinking_proactive_min_runs` | `6` | **Deprecated** — rate-limiting no longer silences runs; repeats are prevented by the recent/declined dedup prompts. Unused. |
| `thinking_proactive_memory_k` | `4` | Per-query top-K when the proactive step pre-fetches real memories to hand the model (it may also `memory_search` once itself). |
| `thinking_question_dedup_enabled` | `true` | Semantic de-duplication of proactive questions so they vary in topic instead of repeating the same subject. Reuses the shared embedding singleton; fails open; also requires `memory_enabled`. See [Proactive intelligence](#proactive-intelligence-stufe-2). |
| `thinking_question_similarity_threshold` | `0.80` | Cosine ≥ this vs a recent/declined question → rejected as too similar (MiniLM runs ~0.78–0.85; tune per deployment). |
| `thinking_question_similarity_runs` | `12` | Compare a candidate question against questions asked within this many recent runs. |
| `thinking_question_similarity_max_compare` | `12` | Hard cap on how many recent questions are embedded/compared per turn (cost/leak bound). |
| `thinking_getto_max_attempts` | `3` | Get-to-know retries enforcing dedup before the final attempt bypasses it (never end a run in silence; must be < the turn limit). |

**Cost efficiency:** Set `thinking_provider` and optionally `thinking_model` to use a cheaper model for background runs (e.g. a small local model or a low-cost API tier) while keeping the main chat on a more capable model. Configurable in the Web UI under **Settings → AI & Model → Thinker (background)**.

**DeepSeek auto routing:** When the thinking provider resolves to DeepSeek with the `deepseek-auto` model, the run is treated as a background task and routes to `deepseek-v4-pro` (the best model), matching the "background task = pro" design. `_run_thinking_for_user` sets `VAF_BACKGROUND_PRO=1` (a pro-context trigger for the `deepseek-auto` resolver in `api_backend.py`) for the duration of the run and clears it in its `finally`. A dedicated flag is used rather than `VAF_IN_WORKFLOW_TERMINAL` (which would make the thinking loop **skip** the run) or `VAF_IN_AUTOMATION` (which carries other automation semantics).

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
(`thinking_proactive_enabled`), the run ends by asking the user *something* — but the kind of message is
constrained so it can never be fabricated. **A fact-containing suggestion may only be delivered when it is
genuinely grounded in a real retrieved memory.** When nothing is grounded, the run does not invent: it falls
back to a **fact-free get-to-know question** (which states no fact, so it cannot be a fabrication). So "the
run always asks something" still holds, while invention is structurally impossible. There is no rate-limit
that silences a run; a REPEAT is prevented by the recent/declined dedup prompts, and frequency is bounded by
`thinking_cooldown_minutes` + `thinking_idle_minutes` + quiet hours.

> **Why the proactive step is not forced.** An earlier design *forced* a fact-containing suggestion on every
> clear floor (`tool_choice="required"` + a "silence is never the goal" mandate). On a thin or empty desk
> that mandate pressured a strong model to **invent** a plausible fact to satisfy it — in one run it
> fabricated a daily "routine" from unrelated identity memories and proposed an automation for it. The
> proactive step is therefore **no longer forced**: the model is given a genuine choice — ground a
> suggestion in real memory, or defer to the fact-free question — and is told explicitly never to invent.
> This mirrors a normal main-agent / automation turn, which is reliable precisely because it executes a real
> task and never has to manufacture a goal.

- **Real memories are handed to the model:** before the proactive step, `_build_proactive_memory_digest`
  retrieves a targeted sample of the user's REAL memories **in code** — several queries aimed at proactive
  value (recurring routine, current work, preferences), `thinking_proactive_memory_k` (default **4**) each,
  deduped. The digest is injected into the proactive prompt, which requires that **every fact in a
  suggestion come from these memories** (quoted in `details`), and is seeded into the evidence pool that the
  backstop gate checks against.

1. **Grounded suggestion (offered, not forced — ONE pass):** the model is shown the real-memory digest and
   may `memory_search` **once** itself (`allow_memory_search` → `_thinking_allow_search`; read-capped, so no
   churn). It offers ONE suggestion **only if** the real memories genuinely support it — prioritising an
   **automation opportunity** (something the user does/asks for *repeatedly*, not already covered →
   `ask_user(message="…", proposed_action="create automation: …", details="<the real memory it is based
   on>")`; on "yes" the **main agent builds the automation**). There is **no `tool_choice="required"`** and
   no pressure to produce a suggestion: if the memories do not support one — or a suggestion would require
   inventing any detail — the model calls `thinking_done` and the run defers to the get-to-know question.
   Every stated fact must come from the digest/memory; a half-remembered or paraphrased "fact" counts as
   invention and must not be sent.
2. **Get-to-know question (fact-free fallback):** if the proactive step grounded nothing, the run does
   **not** finish silently — it asks ONE specific, friendly question to get to know the user better (their
   focus/work, a routine they'd like automated, an interest), so future runs can help. A question states no
   fact, so it can never be a fabrication; this is the safe way to honour "always ask one question". The
   completion gate keeps the run from finishing while this is still pending (`_proactive_step < 3`).

   - **Semantic de-duplication (topic breadth):** the text-based recent/declined prompts only block the same
     *wording*, so the model used to re-ask the same *topic* reworded (always "work/VAF"). Before a proactive
     question (open or grounded) is delivered, `deliver_tracked_message` runs a semantic gate
     (`_question_too_similar`): it embeds the candidate and compares it by cosine similarity against the last
     `thinking_question_similarity_runs` (12) asked/declined questions; at ≥ `thinking_question_similarity_threshold`
     (0.80) the delivery is rejected (`return None`), `ask_user` tells the model the question is too similar and to
     pick a clearly different area, and the loop re-asks. The candidate is embedded via the **shared embedding
     singleton** the run already uses (`get_embedding_service().embed_sync`, ≤ `thinking_question_similarity_max_compare`
     (12) comparisons per turn) — no new model load, nothing persisted, and the check **fails open** (any embedding
     error → delivered). The final get-to-know attempt (after `thinking_getto_max_attempts`, default 3) bypasses the
     gate so a run never ends in silence. Toggle with `thinking_question_dedup_enabled` (also requires `memory_enabled`).

- **Follow up on the open question, then rest (anti-repetition):** before proposing a NEW topic, the run
  checks for the most recent **unanswered** free proactive request (`thinking_requests.get_open_proactive_request`
  — status `asked`, not note/todo-sourced, within the recency window). If one exists, the run re-asks **that**
  question as a short, pointed yes/no **follow-up** (`_build_followup_prompt`) instead of inventing a new
  topic, and the delivery **updates that request** (bumps a `followups` counter via `bump_followup`,
  `set_followup_context`) rather than logging a duplicate. After `thinking_followup_max` (default 3) unanswered
  follow-ups the topic **rests**: the run does not ask anything (so no new question and no nudge) until the
  user reacts on their own. This replaces the old "always a brand-new topic every run" behaviour, which
  produced near-duplicate suggestions plus repeated nudges.

- **Message gate (3 modes, set per turn in `deliver_tracked_message`):** a FREE message (no
  `source_note_id`/`source_todo_id`) is governed by the run's mode (`thinking_mode.set_proactive_mode`):
  - **`off`** (gather / forced-resolution): a free message is **blocked** — this kills the generic turn-0
    floskel ("no tasks, I'm ready when you need me") that previously slipped through before the proactive
    flow even ran.
  - **`grounded`** (proactive step): a **backstop** check — the suggestion is delivered only if `message` OR
    `details` quotes a verbatim, normalized substring of ≥ the evidence bar from this run's REAL retrieved
    memory/history (the per-run **evidence pool** = turn-0 `memory_context` + the pre-fetched digest +
    recent user messages + every `memory_search` result captured live). The normalizer folds
    punctuation/hyphens (so "Three-Second-Loop" matches "Three-Second Loop") but never paraphrase. **This is
    a secondary safety net, not the primary defense:** a substring match only proves that *some* phrase is
    real, not that the whole message is grounded — a message that quotes one real phrase and invents the
    rest can still pass it (this is exactly how a fabricated suggestion once slipped through). The real
    protection is upstream — the proactive step is un-forced and the prompt forbids inventing, so a strong
    model declines rather than fabricates. An ungrounded free message is still dropped here, and the run then
    falls to the fact-free get-to-know step. Bars: `thinking_proactive_evidence_min_chars` (24) local,
    `thinking_proactive_evidence_min_chars_api` (12) hosted.
  - **`open`** (get-to-know step): delivered (a question states no fact, so it cannot fabricate).
  Housekeeping deliveries (carrying a source id) are always exempt. When the `grounded`/`open` gate drops a
  FREE message, `ask_user` returns **mode-aware feedback**: in `off` it tells the model to call
  `thinking_done` (do not retry); in `grounded` it tells the model to quote the real memory in
  `message`/`details` — so the weak model stops churning.
- **No spam ≠ silence:** repeats are prevented by the recent-requests + declined-questions prompts (injected
  into the persistent system message), NOT by suppressing a whole run. `thinking_proactive_min_runs` is
  **deprecated** (no longer silences). Frequency is bounded by cooldown + quiet hours.
- **Race protection (start + deliver gate):** a thinking run does not START while the main agent is handling
  a user turn (`_main_agent_busy` via `TaskQueue`, ALL providers), and if a user turn begins mid-run the
  proactive message is recorded (request + `waiting_for_reply`) but its live push is **deferred** (it
  surfaces on the user's next load) — never dropped.
- **Honest limit:** the *usefulness* of a proactive suggestion is bounded by what is genuinely in memory —
  a clear floor with thin memory correctly yields a fact-free get-to-know question, not a manufactured
  suggestion. Quality rises as real, citable memories accumulate.

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
- **Forced-resolution node (the enforceable gate-tree):** a weak local model tends to narrate its intended action as prose instead of emitting the tool call (observed repeatedly: it wrote "I'll call ask_user…" and even composed the full suggestion as text, but only ever called `web_search`). So housekeeping is **enforced**, not requested: for each open ledger item, the loop drives a forced node — it calls `chat_step(..., force_tool_choice="required")` with a per-item prompt, and during that step the read-cap blocks ALL gather tools from the first call (`_thinking_force_progress`). The model therefore **must** emit a decisive tool (`ask_user` / `delete_automation_*`) for that item — it can no longer escape into search or prose. The force applies to the first generation of the step only, then reverts to `auto` so the model can finish. `tool_choice` is honoured by the local llama-server (verified). **On DeepSeek** (flash/pro), the API rejects `tool_choice="required"` with a 400, so `APIBackendManager.chat_completion` auto-downgrades it to `"auto"` (see `docs/llm/LLM_BACKEND_FACTS.md`); the forced node still works because the per-item prompt + the read-cap (which blocks all gather tools on the forced step) compel the decisive tool call without the API-level force. In `vaf/core/thinking_mode.py` (`_build_forced_item_prompt` + the outer loop) and `vaf/core/agent.py` (`chat_step(force_tool_choice=...)`).
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
| `memory_save` | Excluded | Thinking should read memory, not write to it |
| `update_user_identity` | Excluded | Do not mutate the profile in the background — propose via `save_thinking_suggestion` |
| `update_intent` | No-op | A background run must not overwrite the main chat's `user_intent`. The tool returns a no-op in thinking mode (`VAF_THINKING_MODE`), so a run cannot write itself a directive that the next run would read back as a standing order |
| `set_timer` | Excluded | Do not schedule user-facing actions directly — propose via `ask_user`; the main agent sets it on confirm |
| `git_add_commit` / `git_status` / `git_log` | Excluded | VAF is the user's project, not the agent's |

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

When the next thinking run classifies a replied request as **DECLINED** (see *Outcome classification*
above), it:
- Records the question text + user reply in `thinking_declined_questions.json` (auto-expire 30 days, max 20 entries)
- Injects a "DO NOT ask these again" section into the next run's system prompt

This is no longer a keyword guess at reply time. A reply that asks a question back (e.g. "nein habe ich
nicht, wie kommst du darauf?") is therefore never silently dropped as a decline: the LLM classifier weighs
both the user's reply and the main agent's answer, and an outcome it cannot decide re-opens the request for
one soft reconfirm instead of logging a refusal. The question text comes from the tracked request.

---

## Waiting for user reply

- When the agent sends a message during a run → `set_waiting_for_reply()` is called with the question text
- **Nudge:** After `thinking_wait_nudge_minutes`, a short "are you there?" nudge is sent. The wording is
  **varied and multilingual** — picked (rotating, so it differs each time) from the backend **vocabulary
  book** (`vaf/core/vocab`, key `nudge`) in the user's `preferred_language`, falling back to English. The
  phrasings are generated/expanded across languages by `scripts/generate_vocab.py` (dev-time; the runtime
  never calls an LLM for a nudge).
  - **Inactivity Protection:** No nudge is sent if the user was active on ANY channel within the last `thinking_nudge_activity_minutes` (default 5 min).
- **Skip:** After `thinking_wait_skip_minutes`, the waiting state is cleared
- **User replies:** When the user next sends a message, `clear_waiting_for_reply(user_reply_text=...)` is called.
- **Presence re-ask:** If a nudge was already sent (`nudge_sent_at_ts` set) and the user's reply is a
  bare "I'm here" acknowledgement (`_is_presence_ack` — `ja`/`yes`/`da`/`bin wieder da`/👋, exact match
  only), the reply is treated as a presence signal, NOT as the answer: `chat_step` re-arms the wait
  (resets the nudge timer) and the main agent warmly **re-asks the original question**. A bare "ja" sent
  straight to the question (no nudge yet), or any reply with real content (`ja mach das`, `nein!`), is
  still handled as a normal answer. This stops a "yes" to "are you there?" from being mis-recorded as the
  answer to the actual question.

### Automatic Cleanup ("Nudge Killer")

To ensure the background agent doesn't keep waiting (and nudging) while the user is already interacting with the Main Agent, the "waiting for reply" state is cleared automatically:

1. **Centralized Sync (`vaf/core/agent.py`):** In `chat_step()`, the state is cleared with the full `user_reply_text`. This covers all input channels (Web, CLI, Messenger). 
   - **Context Injection:** If the user was being waited on, the Main Agent receives a context hint explaining which background question the user is likely responding to. This prevents "I'm not sure what you mean" replies to short answers like "Yes, why?".
2. **Early Cleanup:** To avoid race conditions where a nudge might be triggered while a message is being processed, both the **Web Server** and **Headless Runner** attempt to clear the state as soon as a message is received.

The reply is:
- Injected as "User reply to your last question" in the next run's system prompt
- Captured onto the tracked request (status → `replied`); the next run classifies the outcome, and a
  `DECLINED` outcome is then saved to the declined-questions log

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
| `thinking_requests/<scope>/requests.json` | Per-user background requests + status (`asked`/`replied`/`done`/`declined`; `confirmed` legacy) from `ask_user` or the `thinking_done(message=)` fallback. Fields incl. `proposed_action`, `details` (real content for the handoff), `source_note_id`/`source_todo_id`, `user_reply`/`main_reply` (the captured triple for outcome classification), `needs_reconfirm` |
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
| Declined questions | `vaf/core/thinking_mode.py` | `_declined_path()`, `_save_declined_entry()`, `_get_declined_questions_prompt()` |
| Reply classification | `vaf/core/thinking_mode.py` | `_classify_replied_requests()`, `_classify_reply_outcome()`; `agent._generate_for_classification()` |
| Waiting for reply | `vaf/core/thinking_mode.py` | `set_waiting_for_reply()`, `clear_waiting_for_reply()`, `get_waiting_for_reply()` |
| Persistent notes | `vaf/core/thinking_notes.py` | `add_note()`, `get_notes()`, `build_notes_prompt()` |
| Background requests | `vaf/core/thinking_requests.py` | `add_request()`, `record_reply()`, `reopen_for_reconfirm()`, `update_request_status()`, `list_requests()`, `recent_requests_prompt()` |
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
