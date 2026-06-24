# The Agent Turn Loop (`chat_step`)

A faithful high-level map of what happens in **one user turn**, to orient contributors
before they read the ~9.7k-line [vaf/core/agent.py](../../vaf/core/agent.py). The entry
point is the `chat_step` method in [agent.py](../../vaf/core/agent.py). This is a map, not
an exhaustive trace — grep the method/symbol names below to find the real code.

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
║   │    api_backend / local server / library ·          │   ║
║   │    parse tool_calls from the stream                 │   ║
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
│    compress turn · append answer · TTS ·                    │
│    _clean_reasoning() → return                              │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
final answer (reasoning stripped)
```

## Phases and where they live

All phases live in `chat_step` (and its helpers) in [agent.py](../../vaf/core/agent.py) —
grep the symbol names to find them.

| # | Phase | Key methods / symbols |
|---|-------|------------------------|
| 1 | Pre-turn setup / context decay / compress | `decay_state`, `prompt_manager.build_prompt`, `context_manager.should_compress` |
| 2 | Workflow / skill match | `_try_workflow` |
| 3 | Record input + intent | `main_persistence.update_user_intent` |
| 4 | Tool router | `_route_tools` |
| 5 | Adaptive temperature | `analyze_intent` |
| 6 | LLM call (streaming) + parse tool calls | `api_backend.chat_completion`, `_parse_qwen_tool_calls`, `_parse_gemma4_tool_calls` |
| 7 | Guardrails | false-promise, result-grounding, team-await gates |
| 8 | Tool dispatch | `execute_tool`, `_anti_spin_step` |
| 9 | Empty-response recovery + final-answer validation | `_validate_final_answer` |
| 10 | Pending-task auto-continue | `_reply_needs_user`, `_task_stuck_step` |
| 11 | Finalize (compress / append / TTS / clean) | `summarize_tool_turn`, `_clean_reasoning` |

## Loop budgets (so a turn can never spin forever)

| Counter | Purpose |
|---------|---------|
| `empty_retry_count` | retries when the model returns only reasoning / empty |
| `tool_turn_count` | soft reminder ~50, hard stop ~75 tool steps in one turn |
| `_plan_gate_blocks` / `_team_await_blocks` | gate bounces before proceeding anyway |
| `_anti_spin_streak` | consecutive bookkeeping-only calls before tools are disabled for a turn |
| `redundant_block_count` | repeated identical tool calls before a nudge |

The gates above are deliberately **bounded** — each blocks a few times, then lets the turn
proceed so nothing hard-locks. Their config keys are in
[CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md) (the `*_gate_*`, `anti_spin_*`, `result_grounding_*`
families).
