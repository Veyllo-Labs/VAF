# The Agent Turn Loop (`chat_step`)

A faithful high-level map of what happens in **one user turn**, to orient contributors
before they read the ~9.7k-line [vaf/core/agent.py](../../vaf/core/agent.py). The entry
point is the `chat_step` method in [agent.py](../../vaf/core/agent.py). This is a map, not
an exhaustive trace - grep the method/symbol names below to find the real code.

A turn is not a single LLM call: `chat_step` runs an inner loop that streams the model,
dispatches any tool calls, feeds results back, and repeats until the model produces a
final answer (or a budget is hit). The companion subsystems each have their own doc:
[TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md),
[TOOL_INPUT_REPAIR.md](TOOL_INPUT_REPAIR.md),
[TOOL_SUPERVISION.md](TOOL_SUPERVISION.md),
[CONTEXT_MANAGEMENT.md](../memory/CONTEXT_MANAGEMENT.md),
[SUBAGENT_IPC.md](SUBAGENT_IPC.md).

## Flow

```
user input
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ 1. PRE-TURN SETUP                                            │
│    decay state · rebuild dynamic system prompt ·            │
│    compress context if over threshold                       │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. WORKFLOW / SKILL MATCH                                   │
│    _try_workflow(): if a workflow matches (≥ confidence),   │
│    run it and return; else surface a hint to the model      │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. RECORD INPUT + INTENT                                    │
│    append user msg · reset per-turn gate budgets ·          │
│    update_user_intent()                                     │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. TOOL ROUTER                                              │
│    _route_tools(): pick the active tool set for this turn   │
│    (capped at router_max_tools; safety-net fallback)        │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. ADAPTIVE STATE                                          │
│    analyze_intent() → adaptive temperature                  │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
╔═════════════════════════════ INNER LOOP ════════════════════╗
║   ┌─────────────────────────────────────────────────────┐   ║
║   │ 6. LLM CALL (streaming)                             │   ║
║   │    _prepare_messages -> api_backend / server /      │   ║
║   │    library · parse tool_calls from the stream       │   ║
║   └─────────────────────────────────────────────────────┘   ║
║       │                                                      ║
║       ▼                                                      ║
║   ┌─────────────────────────────────────────────────────┐   ║
║   │ 7. GUARDRAILS                                       │   ║
║   │    false-promise · result-grounding · team-await    │   ║
║   └─────────────────────────────────────────────────────┘   ║
║       │                                                      ║
║       ├── tool calls present ──┐                            ║
║       ▼                        ▼                            ║
║   (final answer)        ┌─────────────────────────────┐     ║
║       │                 │ 8. TOOL DISPATCH            │     ║
║       │                 │   per call: read-cap ·      │     ║
║       │                 │   redundancy/anti-spin ·    │     ║
║       │                 │   execute_tool() →          │     ║
║       │                 │   inject result · compress  │     ║
║       │                 │   large output              │     ║
║       │                 └─────────────────────────────┘     ║
║       │                        │  (tool_turn_count budget)  ║
║       │                        └──────── loop back to 6 ────╫──┐
║       ▼                                                      ║  │
║   ┌─────────────────────────────────────────────────────┐   ║  │
║   │ 9. EMPTY-RESPONSE RECOVERY + FINAL-ANSWER VALIDATION │   ║  │
║   │    retry on reasoning-only / drift                  │   ║  │
║   └─────────────────────────────────────────────────────┘   ║  │
╚═════════════════════════════════════════════════════════════╝  │
    │                                                            │
    │  10. PENDING-TASK AUTO-CONTINUE ──────────────────────────┘
    │      (tasks remain → re-inject step nudge, keep working)
    ▼
┌─────────────────────────────────────────────────────────────┐
│ 11. FINALIZE                                                │
│    compress turn · append answer · host TTS (opt-in) ·      │
│    _clean_reasoning() → return                              │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
final answer (reasoning stripped)
```

## Phases and where they live

All phases live in `chat_step` (and its helpers) in [agent.py](../../vaf/core/agent.py) -
grep the symbol names to find them.

| # | Phase | Key methods / symbols |
|---|-------|------------------------|
| 1 | Pre-turn setup / context decay / compress | `decay_state`, `prompt_manager.build_prompt`, `context_manager.should_compress` |
| 1b | Who is speaking: the two "the user replied" latches | `_turn_is_from_the_user` decides whether this turn is the user in their own chat; only then is the ask-first latch (`_pending_user_question`) cleared and a waiting background question (`thinking_mode.get_waiting_for_reply`) picked up as answered and cleared. A synthetic drain turn (`_synthetic_drain_turn`: runner drain, A2A room wake), a queue turn the harness marked as not a person (`_turn_is_human=False`: timer, automation) and a background run's own prompt (`run_kind` thinking/automation) fail the test. See [Thinking-Mode.md](Thinking-Mode.md) (Automatic Cleanup) |
| 2 | Workflow / skill match | `_try_workflow` |
| 3 | Record input + intent | `main_persistence.update_user_intent` |
| 4 | Tool router | `_route_tools` |
| 5 | Adaptive temperature | `analyze_intent` |
| 6 | LLM call (streaming) + parse tool calls | `_prepare_messages` runs on ALL THREE lanes (api_backend / local llama-server / llama-cpp-python in-process) before the call - it strips dangling `tool_calls`, drops orphaned `role:tool` messages, converts images to text and downgrades synthetic tool ids; the memory block is spliced into the first system message right after. The server lane re-prepares per retry attempt, because its 400/500 recovery rebinds the history between attempts. Then `api_backend.chat_completion`, `_parse_qwen_tool_calls`, `_parse_gemma4_tool_calls` |
| 6b | Recover tool calls leaked as TEXT | `vaf/core/tool_call_recovery.py` - see below |
| 7 | Guardrails | false-promise, result-grounding gates; team-await note (a reply claiming completion while a sub-agent runs is KEPT - never erased - and a history note keeps the next turn honest); outbound messenger sends (normal headless path AND runner drain) apply the shared `_prepare_channel_outbound` chain incl. a conservative untagged-CoT prefix guard, with the drain text based on chat_step's reasoning-stripped return value |
| 8 | Tool dispatch | `execute_tool`, `_anti_spin_step` |
| 9 | Empty-response recovery + final-answer validation (an EMPTY reply only; a reply the provider cut off at the output limit has content, so this phase passes it through and the turn ends there. The provider's `finish_reason` is recorded as `cut=` in `logs/usage_*.log` and acted on nowhere) | `_final_answer_probe` decides what counts as an answer - closed AND unclosed `<think>` blocks are thinking, not answer (an unclosed one once passed as the reply and suppressed the retry) - then `_validate_final_answer` |
| 10 | Pending-task auto-continue (a continuation round APPENDS to the turn's reply, never replaces it: every answer that passed validation and reached the final history append is collected in `kept_turn_answers`, `_join_turn_answers` builds the returned reply from all of them, and `_restream_kept_answers` puts them back into the web stream buffer after every clear, so the bubble keeps what the user was already shown. Live incident: a 2259-character deliverable was displaced by a 227-character auto-continue confirmation, on the return value AND on screen) | `_reply_needs_user`, `_task_stuck_step` |
| 11 | Finalize (compress / append / TTS / clean) | `summarize_tool_turn`, `_clean_reasoning` |

### Step 6b - tool calls the model wrote as text

A model does not always put a tool call in the structured `tool_calls` field. It may write
it into its own content, in whatever dialect it was trained on, and the call then never runs
while the raw markup is shown to the user. **The agent is not made to prefer one format** -
the fallback chain reads the dialects that actually turn up, in this order, each tried only
once nothing above it matched:

| # | Dialect | Parser |
|---|---|---|
| 1-4 | streamed / paren / Qwen-Hermes / Gemma | `_parse_paren_tool_calls`, `_parse_qwen_tool_calls`, `_parse_gemma4_tool_calls` (`agent.py`) |
| 5 | Anthropic `<invoke name>` + `<parameter name>`, including DeepSeek's `<｜｜DSML｜｜invoke …>` token-wrapped form; Morph `<tool_use name>`; tool-as-tag `<write_to_file>…` | `extract_xml_tool_calls` |
| 6 | Bare OpenAI wire JSON `{"tool_calls": [...]}` leaked as content | `extract_wire_json_tool_calls` |

Whatever is recovered is dispatched, and `strip_tool_call_markup` removes the raw markup from
what is displayed and persisted.

**The batch is taken whole.** DeepSeek emits several `<invoke>` blocks inside one
`<｜｜DSML｜｜tool_calls>` wrapper; recovering only the first left the rest to be erased by
`strip_tool_call_markup`, so one of four files was read and nothing said the other three were
not (live incident). Sibling blocks are independent, so an entry naming an unknown tool is
skipped while its neighbours still run - unlike the wire-JSON lane, which refuses its whole
object for one foreign name because that format is a SINGLE structure and a bad name makes
all of it suspect.

**Every lane knows every dialect.** The main agent, the librarian and the coder each parse
model content on their own fallback path; a dialect only one of them knows means the same
leak "works in chat but hangs the coder". The coder is the one deliberate difference: it
dispatches one call per loop iteration and re-prompts after each result, so a batched leak is
asked for again rather than lost - the main agent's turn ends after its fallback, which is why
it takes everything.

Host-speaker TTS (final answer, thinking fillers, answer chime) only fires for agents
constructed with `host_audio=True`, which is exclusively the interactive CLI
(`_make_cli_agent`). Headless queue turns, automations, thinking runs, `vaf run -p`,
and embedders are fail-closed silent on the host; browser TTS is a
separate frontend-pulled lane (`message_complete` -> `speak` WebSocket command) and
is not affected by this gate.

## Loop budgets (so a turn can never spin forever)

| Counter | Purpose |
|---------|---------|
| `empty_retry_count` | retries when the model returns only reasoning / empty |
| `tool_turn_count` | soft reminder ~50, hard stop ~75 tool steps in one turn |
| `_plan_gate_blocks` | gate bounces before proceeding anyway. Workflow launches (`execute_workflow`, `create_agent_workflow` with steps) are never bounced: the call itself IS the plan the gate demands, so the gate SEEDS working memory's plan from the call (template id + variables, or the steps list) and allows it - a bounce here cost a weak model the thread (live incident: bounce, plan set, workflow forgotten, manual steps). Launch calls without a usable plan payload (e.g. `action='list'`) still bounce normally |
| `_anti_spin_streak` | consecutive bookkeeping-only calls before tools are disabled for a turn. The forced text turn explicitly FORBIDS claiming results (an earlier wording said "state your result" - with tools off, a weak model answered by fabricating one, live incident). Two sibling self-poisoning guards close the same loop end to end: the working-memory NOTE FIREWALL (`_working_memory_note_gate`) refuses outcome/progress-claiming notes ("Workflow wurde erfolgreich gestartet", "Web-Suche: läuft") while no non-bookkeeping tool has run this turn - a model that narrates fiction into its notes gets that fiction re-injected as trusted context next generation and then coherently reports work it never did; and RESULT GROUNDING gained a deterministic rule: a final reply asserting tool outcomes after a bookkeeping-only turn is UNGROUNDED without consulting the LLM judge (which had waved exactly this case through). Working-memory note timestamps are now rendered in the user's timezone (they were UTC, misdating the model's own recent actions) |
| `_nonprogress_streak` | consecutive read-only/verify-only tool turns (`list_*`/`read_*`/`get_*`, `list_automations`, `read_automation`, …; NOT `web_search`/`memory_search`, which are genuine gathering) before a nudge then a forced text answer - catches a "verify forever" loop where the work is already done; any mutating/producing tool resets it (`nonprogress_max_turns`, default 6) |
| `redundant_block_count` | repeated identical tool calls before a nudge. Three layers share it: (a) the adjacent check against the newest tool message (failure retry + immediate repetition; streamed-call lane), and in one CROSS-LANE filter that runs after EVERY parsing lane (streamed AND the XML/JSON/paren/recovery fallbacks - the in-loop-only version was bypassed by fallback-parsed batches, live incident with `streaming_tools=0`) and before the assistant tool_calls message is built: (b) a WINDOWED exact-duplicate check for pure lookups (`_find_redundant_read_call`): an identical read call that already SUCCEEDED this turn is refused with a pointer to its result, unless a mutating tool succeeded in between (fail-open), and (c) an in-batch dedupe that silently drops an exact (name, args) duplicate WITHIN one model response (for a send tool that hole meant a double-send) |
| wall-clock backstop | a generous per-turn deadline (`chat_step_wall_clock_seconds`, default **3600s = 1h**) checked at each tool-turn boundary; independent of tool count or provider speed, it is the last-resort stop for a true infinite/zombie loop and never aborts legitimate long work (the no-progress guard + per-tool timeouts stop the common case far earlier). The 75-turn cap is a secondary guard. |

The gates above are deliberately **bounded** - each blocks a few times, then lets the turn
proceed so nothing hard-locks. The two universal backstops (`_nonprogress_streak` and the wall-clock)
exist because a *slow* runaway (a reasoning provider grinding many varied tool turns, e.g. an agent that
kept re-verifying an already-created automation) evades the count- and 5-second-based guards. Their
config keys are in [CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md) (the `*_gate_*`, `anti_spin_*`,
`nonprogress_*`, `chat_step_wall_clock_seconds`, `result_grounding_*` families).
