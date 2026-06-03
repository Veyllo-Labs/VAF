# Whare Wananga -- Tool Self-Learning

Whare Wananga (Maori: "house of learning") is VAF's subsystem for learning **tool
know-how**: how to correctly operate a single tool -- its argument contract, its dangers,
and the correct call sequence. "Matauranga" is the poetic name for that knowledge; the
code term is `tool_knowledge`.

It is distinct from two neighbouring concepts:

- It is **not** a "skill" in the Agent-Skills sense (a task-level cookbook that spans
  several tools); that layer maps to VAF workflows.
- It is **not** the tool itself; it is the learned knowledge *about* operating one tool.

## Status

- **Built:** the `tool_knowledge` store (persistence + schema).
- **Design (not yet implemented):** the learning loop (predict-then-verify sandbox
  practice), the producers, safety gating, triggers, and know-how injection.

Sections below are marked accordingly.

## The artefact: tool_knowledge (built)

One record per tool, stored **globally** (a tool's mechanics are objective, not per-user)
at `~/.vaf/whare_wananga/<tool>.json`. It is structured by the three baskets of knowledge
(Nga Kete o te Wananga):

| Facet | Holds |
|-------|-------|
| `aronui` | what the tool returns / when to use it (`output_shape`, `when_to_use`, `notes`) |
| `tuatea` | the dangers: `pitfalls`, side-effects, error/validation behaviour |
| `tuarua` | the correct ritual: `procedure` (argument form / steps) + `verification` checks |

Plus a `predict_records` list (the predict-then-verify catalogue that measures "learned")
and lifecycle metadata: `status` (`draft` / `confirmed` / `stale`), `confidence`, `uses`,
`success`, `fail`, `source` (`whare_wananga` / `teacher` / `runtime`), `tool_schema_hash`,
`side_effect_class`, and timestamps. The canonical skeleton is `new_record()` in the store.

## The store (built)

`vaf/whare_wananga/store.py` -- persistence only, no learning or injection:

| Function | Purpose |
|----------|---------|
| `new_record(tool, side_effect_class=, tool_schema_hash=, source=)` | empty skeleton |
| `load(tool)` | read a record, or `None` |
| `save(record)` | atomic write (temp file + replace); requires `record["tool"]` |
| `list_tools()` | tool names that have a stored record |
| `delete(tool)` | remove a record |
| `compute_tool_hash(tool_def)` | stable short hash for change-invalidation |

Records are plain JSON under `~/.vaf/whare_wananga/`. `tool_schema_hash` lets a later step
invalidate stored know-how when a tool's definition changes.

### Learned-state in the UI (built)

The Settings tool list shows each tool's learned state as a neutral badge
(`Learned` / `Learning` / `Stale` / `Not learned`), derived from `learned_state()` and
attached to the tools list the backend sends (`web_server.py` `_attach_learned_states`).
Until a tool has been trained, it shows "Not learned".

The tool detail (code viewer) shows one of three buttons depending on the tool's state:
red **"Tool not configured"** (a connection that isn't set up; does not open the dashboard),
green **"Tool trained"** (already learned; opens the dashboard to view metrics), or amber
**"Train tool now"** (configured + not yet learned; POSTs `/api/whare_wananga/train/{name}`,
which starts the predict-then-verify background job, and opens the dashboard). The button
flips to green as soon as training confirms the tool. Default depth is 21 attempts
(`whare_wananga_max_attempts` in config); a fast tool trains in roughly 1-2 minutes
(LLM latency dominates).

That button opens a **training dashboard** (`web/components/TrainingDashboard.tsx`) -- a
large panel that reads `GET /api/whare_wananga/tool_knowledge/{name}` and shows the tool's
status, error rate, predictions, and the three baskets (Aronui / Tuatea / Tuarua). Live
metrics (duration, error rate over attempts) populate once the runner streams a training
pass; until then those areas are placeholders.

**Preconditions.** A tool is only trainable once its dependency is configured.
`vaf/whare_wananga/preconditions.py` (`tool_precondition`) maps connection tools to the
existing per-integration config flags (`telegram_config` / `discord_config` /
`whatsapp_config` / `email_config`); tools with no connection dependency are always
configured. The backend attaches `requires_config` + `configured` to each tools-list entry.
In the UI, a not-yet-configured connection tool shows a red **"Tool not configured"** label
instead of the training button, and does not open the dashboard. (calendar / github / cloud
currently default to configured; their checks can be added to the resolver later.)

## Learning loop

The **core predict-then-verify loop is built** (`vaf/whare_wananga/runner.py`,
`train_tool()`) for probe-safe tools, verified live, and **wired to the UI**: the
"Train tool now" button POSTs `train/{name}`, which starts a background job
(`vaf/whare_wananga/jobs.py`); the dashboard polls `training_status/{name}`, shows the live
predict-then-verify attempts, and refreshes the record on completion (badge -> "Learned").
The training "sandbox" is class-scoped (not OS isolation): the trainer may only call the
tool being trained plus its connection-class siblings -- e.g. all `whatsapp_*` tools share
the whatsapp class; non-connection tools are singletons (`preconditions.tool_class`,
enforced by a guard in the runner). Side-effecting tools are tiered by `side_effect_class`:
**reversible** tools (e.g. `create_agent_tool`) are learned via the **error/validation path**
-- probed only with invalid/incomplete inputs the tool rejects before acting (no real effect),
halting if an invalid probe is unexpectedly accepted; **irreversible** tools (e.g. `send_mail`,
payments, deletion) are **not probed at all** (gated), since one accepted probe would be a real
irreversible effect. Still pending: the online Teacher, and (optional) real-success observation
for file-writing tools via an isolated context. Training currently runs on the shared agent
instance.

How a record gets filled:

- **predict-then-verify (built):** before each practice call the model predicts the tool's response
  (a success shape OR a specific expected error), calls it, then compares. "Learned" =
  predictions stop being wrong (a correctly predicted error counts as learned). This both
  produces the `verification` checks and terminates practice automatically.
- **Two producers, one loop:**
  - *offline* -- a single local model practises a tool in a controlled sandbox (trial and
    error) and distils `tool_knowledge`; no cloud model needed;
  - *online (Teacher)* -- a stronger model co-learns over several rounds and confirms or
    corrects the predictions.
- **Safety gating** (reuses the Declarative Tool Contract `side_effect_class`): read-only /
  idempotent tools are practised freely; side-effecting tools are never exercised through
  their effect path -- their interface is learned via read/validate calls and deliberately
  triggered, *expected* validation errors.
- **Triggers:** eager when a tool is first connected (a background pass), plus a short
  corrective re-probe when an already-learned tool hits a *surprising* runtime error.

## Delivery (how know-how reaches the agent)

Detailed in [ACTION_TAG.md](ACTION_TAG.md). In short: proactively for side-effecting /
known-quirky tools (the Action-Tag parser matches the agent's committed intent to a tool
and injects that tool's know-how before the call), and reactively on a surprising tool
error for everything else. Independently of either path, the agent's actual actions always
remain in context via the real tool calls and their results.

## Files

| Path | Role |
|------|------|
| `vaf/whare_wananga/store.py` | `tool_knowledge` store + schema (built) |
| `vaf/whare_wananga/__init__.py` | package exports |
| `docs/ACTION_TAG.md` | the `<Action>` tag and the delivery side |

## Related

- [ACTION_TAG.md](ACTION_TAG.md) -- the `<Action>` tag, backend parser, and delivery
- [TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md) -- tool routing, the
  Declarative Tool Contract, and `side_effect_class`
- [SELF_LEARNING.md](SELF_LEARNING.md) -- VAF's self-learning index. Whare Wananga will be
  registered there once the learning loop is implemented and genuinely learns from use.

---

*Last updated: 2026-06-03*
