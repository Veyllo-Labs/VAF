# Whare Wananga -- Tool Self-Learning

Whare Wananga (Maori: "house of learning") is VAF's subsystem for learning **tool
know-how**: how to correctly operate a single tool -- its argument contract, its dangers,
and the correct call sequence. "Matauranga" is the poetic name for that knowledge; the
code term is `tool_knowledge`.

It is distinct from two neighbouring concepts:

- It is **not** a "skill" in the Agent-Skills sense (a task-level cookbook that spans
  several tools); that layer maps to VAF [Skills](../agents/SKILLS.md) and workflows.
- It is **not** the tool itself; it is the learned knowledge *about* operating one tool.

## Status

- **Built:** the `tool_knowledge` store; the predict-then-verify learning loop (LLM-judged
  validation + a final challenge); its safety tiering (full-probe / error-path / declare / gated);
  the producers (training dashboard + the `vaf ww` CLI, plus a one-pass sweep over all tools); the
  **proactive** delivery (router-driven pitfalls injection into the tool schema); the
  **reactive** delivery (re-feed a failed tool's know-how on error, with a known-vs-novel surprise
  signal; may deliver gate-failing records tagged UNVERIFIED); **runtime re-learning** (a novel
  runtime error is distilled into a new pitfall from the real observation); **eager training**
  (opt-in background scanner + serialized queue that auto-trains safe, configured, unlearned
  tools); schema-hash invalidation (a changed tool definition flips its record to `stale`, so
  outdated know-how is no longer schema-injected until re-trained); the **re-training queue**
  (gate-failing records - stale/draft/declare/interrupted - are enqueued instead of rotting
  silently; drained manually via `vaf ww retrain --pending` or automatically in the eager worker);
  and **Teacher/Noho co-learning** (opt-in: after a weak local run, a stronger configured API
  model co-learns the tool over the same loop).
- **Planned:** declared-vs-actual via the Action tag; auto-training agent-created tools; teacher
  continuity (per-tool session memory).

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
flips to green as soon as training confirms the tool. Training depth is adaptive (see the
learning loop below): a tool whose behaviour is predictable confirms in one validation round
(~15 probes, roughly 1 minute, LLM latency dominates); a flaky tool runs further rounds up to
a cap.

That button opens a **training dashboard** (`web/components/TrainingDashboard.tsx`) -- a
large panel that reads `GET /api/whare_wananga/tool_knowledge/{name}` and shows the tool's
status, error rate, predictions, and the three baskets (Aronui / Tuatea / Tuarua). While a
training job runs, the metric cards and the predict-then-verify grid update live from the
job's streamed events (confidence, error rate, correct/total, run duration) rather than only at
the end.

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
predict-then-verify attempts (stats update during the run, not only at the end), and
refreshes the record on completion (badge -> "Learned").

The loop is **adaptive**: an initial learning batch (`LEARN_N` = 21 probes) builds the
tool_knowledge and distils the three baskets, then a **validation batch** (`VALIDATE_N` = 9
probes) re-predicts each outcome **from the distilled document** (every prediction prompt --
learning, validation, challenge -- includes a compact view of the three baskets, so the agent
predicts from the document, not just from raw recent attempts). If all nine are predicted
correctly, the tool is **confirmed** (`status=confirmed`) and training stops. If any prediction
is wrong, the runner **re-distils the document with all data so far** (so it corrects from the
wrong predictions), runs another 6 learning probes from that fresher document, **re-distils
again**, then validates a fresh batch of 9 -- i.e. the document is refreshed *before* each repeat
and *before* each retry. This repeats until a full 9/9 batch passes or `MAX_ROUNDS` (4) is reached
(then `status=draft`). Confidence is the
overall hit rate across all probes. The dashboard banner reflects the current phase
("Learning" vs. "Validating -- round R/4") and the last validation batch's score.

**The judge.** In the validation phase each call is graded by an **LLM judge** rather than a
string heuristic: the agent states what it expects, the tool is called, and the judge decides
`pass` / `fail` from the prediction and the real response. The judge prompt encodes that
**transient infrastructure problems are not a tool failure** -- a rate limit, quota, timeout,
or network error returns `pass` (the agent's understanding stands; the environment hiccuped),
so flaky infrastructure does not block confirmation. Learning probes keep the cheap heuristic
(their match is only informational). The judge's verdict and one-line reason are stored on each
validation `predict_record` and streamed to the UI.

**The challenge (final test of the test).** A clean 9/9 only proves the agent can predict probes
*it chose itself*. So after confirmation a **challenge phase** runs where the **judge invents the
inputs** (`CHALLENGE_PASS` = 3 scenarios the agent must pass). To pose informed, non-obvious
challenges the judge is given the tool's own metadata (name, `description`, parameter schema --
straight from the tool) plus the distilled `aronui` + `tuatea` pitfalls and the already-tried
inputs (to avoid repeats); it picks a fresh input (respecting the same safety mode), the agent
predicts the outcome, the tool runs, the judge grades pass/fail. The agent must reach 3 passes to
be **mastered**. `CHALLENGE_ROUND_FAILS` = 3 fails within a round trigger a re-distil + a fresh
round; `CHALLENGE_MAX_FAILS` = 10 total fails end the challenge -- the tool then stays `confirmed`
(from the 9/9) but `challenge_passed=False` (deemed not truly learned). This removes the agent's
ability to self-select easy probes.

**The training stage.** The dashboard shows a live stage at the bottom: the **agent** on the
left, the **tool under test** in the middle, and -- during validation/challenge -- the **judge**
on the right. Both are the living dot (shared `AgentAvatar`) with an **expressive emotion range**
(squash & stretch character states):
- *Agent:* in **Stage 1 (learn)** it shows the `learn` **activity state** (the body+eye model with a
  progress spinner and knowledge orbs absorbed into the dot), and a quick `success` (jump + check +
  ring) or `error` (shake + "!") activity beat per probe; in **Stage 2 (validate)** `thinking` and
  **Stage 3 (challenge)** `listening`, with lighter `nod` / `confused` beats per result; `idea` when
  it re-distils; `celebrate` when mastered, `sad` on a draft/halt, `idle` at rest.
- *Judge:* the same avatar **inverted** (dark dot on a light container, dark glow): `thinking`
  while grading, `talking` while posing a challenge, `nod` / `shake` per verdict; below it a `pass`
  (green) / `fail` (red) pill and its one-line reason.

The tool in the middle is a fixed-size card styled like the chat tool bubble, fed by the latest
probe's args and response. The links between them animate a **data flow** (grey shapes/digits
forward; green/red shapes back per result), stopping when the run ends. During plain learning it
is just agent -> tool; the judge slides in for the final test. Each avatar's dark square also
reacts subtly per emotion (the `body*` keyframes), the idle dot winks, and state changes use the
**settle-to-neutral** transition (the agent stays persistent; the running animation is briefly
dropped so body+eye ease to rest, then the next animation starts from neutral -- see
`docs/web-ui/AgentAvatar.md` "Same-position switches") so states flow into one another instead of
snapping. (Emotion / body / wink keyframes live in `globals.css`,
mirrored from the `docs/animations/agent_avatar` reference.)

Beyond the dashboard, each training run is also surfaced in the Web UI's visual **Timeline** log
(Notifications -> Timeline) as a *Tool Learning* lane entry -- start, tool, mode, and the
confirmed/challenge outcome -- and as `[WHARE-WANANGA] training started/done` lines in
`backend_*.log`. Both require Debug Logs enabled (on by default; disable via `debug_logs_enabled: false` in `~/.vaf/config.json`);
`jobs.train_started` / `jobs.train_ended` emit a `ww_train_start` + `ww_train_end` pair (merged by
`run_id`) via `log_timeline_event`.
The training "sandbox" is class-scoped (not OS isolation): the trainer may only call the
tool being trained plus its connection-class siblings -- e.g. all `whatsapp_*` tools share
the whatsapp class; non-connection tools are singletons (`preconditions.tool_class`,
enforced by a guard in the runner). Side-effecting tools are tiered by `side_effect_class`:
**reversible** tools (e.g. `create_agent_tool`, `update_intent`) are learned via the
**error/validation path** -- the runner forces every probe invalid (`_force_invalid` drops all
required fields) so the tool rejects it before acting, learning the argument contract with no real
effect and regardless of what the model proposes. A reversible tool that **cannot be probed
safely** is instead learned **from its declaration only** (`mode="declare"`): the three baskets are
distilled from the description + schema, no probing. Two cases trigger it: (a) **no required
fields** (e.g. `add_task`, `update_working_memory`: nothing to invalidate, and they never reject --
they just mutate state and return a soft message); (b) a tool that **declares required fields but
does not enforce them** (e.g. creates with defaults on empty) -- detected by a single forced-invalid
**canary** probe: if the tool does not reject it, probing would only ever be accepted side effects,
so we fall back to declaration learning instead of the safety halt.
**irreversible** tools (e.g. `send_mail`,
payments, deletion) are **not probed at all** (gated), since one accepted probe would be a real
irreversible effect. A sandboxed/ephemeral **executor** can opt out of the error path by
declaring `whare_wananga_full_probe = True` (e.g. `python_sandbox`: Docker-isolated, the host
bridge `with_vaf_tools` is opt-in) -- the error path is wrong for a tool whose whole job is to
*accept and run* input (it would halt the instant a probe is accepted), so it is probed in full
with harmless, self-contained snippets that leave nothing permanent. Still pending: the online Teacher, and (optional) real-success observation
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
- **Triggers:** manual (dashboard / `vaf ww`); an opt-in **eager** background scanner that
  auto-trains safe, configured, not-yet-attempted tools one at a time (`whare_wananga_eager_enabled`,
  default off; never send/irreversible tools); **runtime re-learning** when an already-learned
  tool hits a *surprising* runtime error (a new pitfall is distilled from the real observation); and
  opt-in **Teacher/Noho co-learning** (`whare_wananga_teacher_enabled`, default off) after a weak
  local run -- see "Teacher/Noho" below.

## Delivery (how know-how reaches the agent)

**Proactive (built).** Router-driven: after the tool router scopes the turn's tool set
(`Agent._active_tools`), the learned **pitfalls** (`tuatea`) of each selected tool are appended to
that tool's description in the LLM tool schema (`Agent.TOOLS`), so the model sees them inline
*before* it forms the call -- no extra generation, independent of the Action tag. Only `tuatea` is
delivered (`aronui` overlaps the static description; `tuarua` is a later phase), and only for
reliable knowledge: gated on `status=confirmed` + `challenge_passed` + a probed `learn_mode`
(declare-mode/draft excluded). Injection happens only when the router has scoped the set (the
all-tools fallback is skipped to bound tokens), and the hook is hard fail-safe (never breaks
tool-calling). The lookup lives in `vaf/whare_wananga/delivery.py` (`tool_pitfalls`, cached per
tool + file mtime). Defaults (gate fields, max pitfalls/chars, optional confidence floor) are
calibratable there. **Vacuous "pitfalls"** -- the model apologising about the training process
("no probe attempts were provided", "cannot quote the error") rather than giving a real warning --
are dropped both at distillation and at delivery (`store.is_vacuous_pitfall`); informative negative
facts ("no required arguments", "requires an admin session", "limit is optional") are kept.

**Reactive (built).** When a tool call fails at runtime, the agent loop re-feeds the failed tool's
*fuller* know-how -- pitfalls + procedure + verification (`delivery.tool_knowhow`) -- as a deferred
system nudge (the same `_post_tc_messages` channel used for other tool-error nudges), so the loop's
natural re-generation retries informed. Once per tool per turn (to avoid an inject->fail->inject
loop), hard fail-safe. The error is re-checked from the raw result, and a cheap
`delivery.known_pitfall_hit` classifies it as a **known pitfall** (the agent saw it via the schema
and fell for it anyway -> put the procedure first) vs a **novel error** (logged `[WW-SURPRISE]`).

Unlike the A-track, the reactive lane runs **relaxed** (`allow_unverified=True`): a record that
fails the quality gate (declare-mode, stale, draft) is still delivered, prefixed with an explicit
`UNVERIFIED - <reason>` tag. Rationale (blue378604 incident): the call has ALREADY failed, so a
possibly-imperfect hint costs little -- while the withheld record often holds exactly the missing
knowledge (document_writer's declare-mode record contained the fix for the live failure and was
never delivered). The vacuous-pitfall filter still applies. The A-track schema injection stays
strictly gated on `status=confirmed` + `challenge_passed` + a probed `learn_mode`.

**Re-training queue (built).** A record that fails the delivery gate no longer rots: every gate
reject (and every record newly marked `stale` by the schema-hash invalidation) is enqueued in
`~/.vaf/whare_wananga_retrain.json` -- deliberately OUTSIDE the store dir, which `store.list_tools`
globs. The queue is drained one tool at a time via the shared jobs runner: manually
(`vaf ww retrain --pending`, `vaf ww queue [--scan]`) or automatically inside the **eager worker
thread** when `whare_wananga_eager_enabled` is on (never in the scanner thread -- an empty eager
queue does not mean the worker is idle). Limits: 3 attempts per tool, 24h cooldown between
attempts. Declare-mode records are excluded from draining by default (re-training deterministically
reproduces declare; `--include-declare` is the escape hatch). Cross-process caveat: job status is
process-local, so drain from one side at a time (a CLI drain cannot see an in-app training).
Implementation: `vaf/whare_wananga/retrain.py`.

**Runtime re-learning (built).** A novel, *learnable* runtime error (environmental/transient errors
like DNS/timeout/5xx are filtered out) is turned into a new learned pitfall from the real
observation (the call args + the error) via a single background LLM distil -- no tool re-execution
(`vaf/whare_wananga/runtime.py`, `maybe_relearn`). It only ever *appends* a deduped, capped pitfall
to a `confirmed` record, is rate-limited per tool and serialized, and is fire-and-forget (never
blocks the turn). The new pitfall then flows back into proactive (A) and reactive (B) delivery on
the next turn, closing the learn-from-use loop.

The **Action tag** is NOT the injection trigger; its role stays transparency / verify
(declared-vs-actual) / learn-signal (see [ACTION_TAG.md](../agents/ACTION_TAG.md)). Independently of any path,
the agent's actual actions always remain in context via the real tool calls and their results.

## Teacher/Noho co-learning (opt-in)

When a LOCAL (student) run leaves a tool below the delivery bar -- `challenge_passed` is not True OR
`confidence < 0.5` -- a STRONGER configured API model can co-learn the tool with the student over the
SAME predict-then-verify loop. It is **off by default** (`whare_wananga_teacher_enabled`) and only
activates when the main `provider` is `local` AND an API is configured (otherwise there is no stronger
teacher). It is automatic, **serialized** (one session at a time), and **rate-limited** (24h per tool).

The loop is reused, not re-implemented (`vaf/whare_wananga/teacher.py` + the `teacher_llm` /
`seed_record` / `source` parameters on `runner.train_tool`):

1. **Demonstrate** -- the teacher writes an initial three-basket draft from the tool schema and the
   student's failing attempts; it preloads the baskets so the student predicts *from* it.
2. **Co-learn** -- the STUDENT keeps PREDICTING (`tool.query_llm`); the teacher takes over the JUDGE,
   DISTIL and challenge-INVENT calls. The existing validate -> refine -> challenge rounds are the
   co-learning rounds (capped at 3).
3. **Gate** -- the result is saved only if it passes the same challenge gate, as a normal record with
   `source="teacher"`, `learn_mode="teacher"` -- delivered (A + B) like any other learned tool.

The teacher model is the **strongest available** model: each configured provider's models are
**live-discovered** from its API (`APIBackendManager.list_models`, cached ~12h) and ranked by a small
static capability tier, so a stronger model the provider offers is used even if the configured main
model is weaker. Offline it falls back to the configured model; an explicit `whare_wananga_teacher_model`
= `provider:model` override is the escape hatch. **Safety is unchanged:**
the session runs the same safe runner path, so irreversible tools are gated (never escalated) and
reversible tools stay on the error path -- nothing send/irreversible is ever really executed.

## CLI

The web dashboard runs training as a background job; the CLI runs the same `train_tool` loop
**synchronously in the foreground**, so a run (with its live phase/probe trace) can be driven and
read straight from a shell -- for testing the learner, re-training a single tool, or training every
tool in one queue. Both `vaf ww <cmd>` and `python -m vaf.whare_wananga.cli <cmd>` work:

```
vaf ww train create_contact          # train one tool (live trace, final summary)
vaf ww train memory_search --quick    # small batches -- fast smoke test
vaf ww train --all                    # queue: every not-yet-learned tool (--force includes learned)
vaf ww retrain update_intent          # alias for train (a run is a fresh assessment)
vaf ww retrain --pending              # drain the re-training queue (gate-failing records)
vaf ww retrain --pending --include-declare   # also re-train declare-mode records
vaf ww queue                          # show the re-training queue (reason, attempts, drainable)
vaf ww queue --scan                   # seed the queue from gate-failing store records
vaf ww list                           # learned tools + state + confidence
vaf ww show create_contact            # the three baskets
vaf ww delete create_contact          # drop the stored knowledge
vaf ww eager status                   # eager on/off + learned count
vaf ww eager on | off                 # toggle opt-in proactive training (whare_wananga_eager_enabled)
vaf ww eager scan                     # train all eligible SAFE tools now (foreground)
vaf ww teacher status                 # teacher on/off + which API model would teach
vaf ww teacher on | off               # toggle opt-in co-learning (whare_wananga_teacher_enabled)
```

`--all` skips tools whose connection is not configured and (without `--force`) tools already
learned. Probing runs in training mode (`_ww_training`) so the live plan/confirmation gates are
bypassed and probes reach the tool's own validation.

## Files

| Path | Role |
|------|------|
| `vaf/whare_wananga/store.py` | `tool_knowledge` store + schema (built) |
| `vaf/whare_wananga/runner.py` | adaptive predict-then-verify loop + LLM judge (built) |
| `vaf/whare_wananga/jobs.py` | background training jobs + live status (built) |
| `vaf/whare_wananga/preconditions.py` | trainability + class sandbox resolver (built) |
| `vaf/whare_wananga/cli.py` | training CLI (runs the loop synchronously in the foreground) |
| `vaf/whare_wananga/delivery.py` | delivery read-path: gated lookups for runtime injection -- `tool_pitfalls` (proactive), `tool_knowhow` + `known_pitfall_hit` (reactive) |
| `vaf/whare_wananga/runtime.py` | LAZY-corrective: distil a new pitfall from a novel runtime error (`maybe_relearn`) |
| `vaf/whare_wananga/eager.py` | opt-in eager scanner + serialized training queue (`scan`, `enqueue`, `start`); its worker also drains the re-training queue |
| `vaf/whare_wananga/retrain.py` | persistent re-training queue for gate-failing records (`enqueue`, `pending`, `scan_store`, `drain_one`) |
| `vaf/whare_wananga/teacher.py` | opt-in Teacher/Noho co-learning: trigger, gating, serial worker, teacher-model selection (`maybe_teach`) |
| `vaf/cli/cmd/ww.py` | `vaf ww` Typer wrapper around the CLI |
| `vaf/whare_wananga/__init__.py` | package exports |
| `web/components/TrainingDashboard.tsx` | dashboard + live training stage (agent/tool/judge) |
| `web/components/AgentAvatar.tsx` | shared living-white-dot agent avatar |
| `docs/agents/ACTION_TAG.md` | the `<Action>` tag and the delivery side |

## Related

- [ACTION_TAG.md](../agents/ACTION_TAG.md) -- the `<Action>` tag, backend parser, and delivery
- [TOOL_ROUTER_ARCHITECTURE.md](../agents/TOOL_ROUTER_ARCHITECTURE.md) -- tool routing, the
  Declarative Tool Contract, and `side_effect_class`
- [SELF_LEARNING.md](SELF_LEARNING.md) -- VAF's self-learning index. Whare Wananga's learning loop
  is built (it learns tool know-how from sandbox practice); learning from real runtime use is the
  planned next stage.

---

*Last updated: 2026-06-04*
