# Workflow Selection & Recommendation Architecture

## Overview

VAF uses a **two-phase workflow system**: an LLM-powered router detects whether the user's request matches a workflow, and the **main agent** decides whether to actually execute it. This prevents false-positive auto-executions (e.g. triggering `create_website` when the user asks to *change* an existing site) while keeping the full power of multi-step workflows available.

With the **Agatic vNext** update, the Workflow Router is fully dynamic and "Plug & Play" — it discovers available workflows at runtime and maps user intent to capabilities without hardcoded rules.

The same router pass also covers **skills** as a second tier: if no workflow matches, the router checks whether an Agent Skill (SKILL.md) matches and, if so, suggests it. See [Skills](SKILLS.md).

---

## Architecture

### Old Flow (auto-execution)

```
User message
    → _try_workflow() [LLM router]
        → Match found → execute immediately → return result (agent never runs)
        → No match   → return None → agent LLM runs
```

**Problem:** The main agent — which has full conversation history, `[SESSION WORKSPACE]`, and user tone — was never consulted. A fast LLM call made a binary, irreversible decision. Paths containing words like "Create" in the folder name could trip the router even for pure edit requests.

### New Flow (recommendation)

```
User message + [SESSION WORKSPACE] context
    → _try_workflow() [LLM router — same detection logic]
        → Match found → store as self._pending_workflow_hint → return None
        → No match   → return None
    → Agent LLM runs WITH full context:
        - [SESSION WORKSPACE]      → "is this an existing project?"
        - [WORKFLOW SUGGESTION]    → "should I use this workflow?"
        - Conversation history     → "user said 'Farben ändern' → this is an edit!"
        → Agent decides: call execute_workflow() OR call coding_agent() directly
```

**Exception — `@workflow_id` explicit prefix:** If the user types `@create_website make me a site`, the workflow is still executed directly without going through the hint system. This is an unambiguous user command.

---

## Routing Process

### Step 1 — Skip Conditions

Before the LLM router runs, `_try_workflow()` checks cheap fast guards. If any matches, the function returns `None` immediately and the main agent handles everything.

| Condition | Reason |
|-----------|--------|
| `VAF_IN_AUTOMATION` env var is set | Automation tasks have their own `workflow_steps`; routing would double-execute. |
| `"CURRENT DOCUMENT (Editor)"` in input | User is editing a document; use `replace_editor_selection` / `document_editor` tools instead. |

### Step 2 — LLM Router

```
┌─────────────────────────────────────────────────────────────────┐
│                  DYNAMIC WORKFLOW ROUTING                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────┐      ┌──────────────────────────────┐      │
│  │   User Input    │      │  Workflow Registry (Live)    │      │
│  └────────┬────────┘      └──────────────┬───────────────┘      │
│           │                              │                      │
│           ▼                              ▼                      │
│  ┌──────────────────────────────────────────────────────┐       │
│  │  ROUTER LLM (Intent Analysis)                       │       │
│  │  "Does this input match any workflow description?"   │       │
│  └──────────────────────────┬───────────────────────────┘       │
│                             │                                   │
│              ┌──────────────┴──────────────┐                    │
│              ▼                             ▼                    │
│      [MATCH FOUND]                    [NO MATCH]                │
│   Store as _pending_workflow_hint     Return None               │
│   Return None (agent always runs)     (Main Agent handles it)   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

The router prompt includes **negative examples** to catch vague phrasings that might otherwise match creation workflows:

```
- User: "Die Webseite ist buggy, schau dir das an"  → none  (fix/debug request)
- User: "Fix the layout issue on the site"          → none  (fix/debug request)
- User: "Analyze this website for errors"           → none  (analysis request)
```

The router returns a single token: `workflow:<id>`, `skill:<id>`, or `none`. A
`skill:<id>` result is surfaced as a `[SKILL SUGGESTION]` hint instead of a
workflow hint (the two are mutually exclusive). Skill matching is described in
[Skills](SKILLS.md); the rest of this document covers the workflow path.

### Step 3 — Workflow Hint Injection

If the router matched a workflow, `chat_step()` prepends a `[WORKFLOW SUGGESTION]` block to the user input before calling the LLM (built by `_build_workflow_suggestion_note`, a pure module-level function so tests can pin the exact note):

```
[WORKFLOW SUGGESTION] The workflow "Create Website" (create_website) looks relevant to this request.
Pre-extracted variables: description="Restaurant website"
To start it call: execute_workflow(workflow_id="create_website", variables={...})
IMPORTANT: If the user is asking to edit or modify an existing project
(see [SESSION WORKSPACE] above), use coding_agent with project_path instead
— do NOT start a creation workflow.

<original user message>
```

When the user's own message mentions a workflow (same shared detection as Step 3b, `_mentions_workflow`), the note additionally offers `create_agent_workflow(action="run_temp")` as the fallback: a WRONG template match must not eat an explicit workflow request. Live incident: the suggestion was the only workflow path shown, the model rightly declined the mismatched template - and then did every step manually, because the run_temp hint lived only in the no-match branch. Advisory wording, like everything this router emits.

The hint is **one-shot** — it is consumed and cleared immediately. The next message starts fresh with no pre-set hint.

**Routing runs on the RAW user message.** The WebUI lane enriches the user input (the `[SESSION WORKSPACE]` preamble, front-office blocks) BEFORE `chat_step()`, and the router used to route on that enriched text: the preamble's wording (`coding_agent`, `projects`, `write_file`) steered a plain websearch request to a CODE workflow, and the variable extractor stuffed the entire preamble into `query=` (the same incident). `chat_step(raw_user_input=...)` now carries the pre-enrichment message; `_try_workflow(route_input=...)` uses it for the router match, variable extraction, the explicit `@workflow` parse, the workflow-mention detection, language detection and the intent lock. The LLM still sees the enriched text; gate checks that look for enrichment markers (the editor block) deliberately keep reading the enriched input. Lanes that pass the raw message as `user_input` already (CLI) omit the parameter.

### Step 3b - No SAVED Template Matched

When the router finds nothing (most requests), `_try_workflow()` appends a short system hint instead of the `[WORKFLOW SUGGESTION]` block. This hint is NOT a "workflows are usually unnecessary" note - a saved-template miss says nothing about whether an **ad-hoc** workflow (`create_agent_workflow(action="run_temp")`) would help, since the router only ever matches against saved templates.

The hint branches on whether the user's own message mentions a workflow at all (`_mentions_workflow` in `agent.py`, the ONE shared detection also used by the Step 3 suggestion advisory: prefix-matched on `workf`, excluding "workforce" - case-insensitive and typo-tolerant on purpose, since a live incident's real request had "workflow" transposed to "workflwo", which a whole-word match would have missed):

- **Mentions a workflow, no template fits**: `create_agent_workflow(action="run_temp")` is surfaced as the option to reach for, not `list_workflows` (which would only surface saved templates - the wrong tool here).
- **No mention**: a lighter hint still offers `run_temp` for genuinely multi-step work, alongside `list_workflows`.

The substring match is deliberately cheap and imprecise (it can also fire on an unrelated mention - small talk about "my daily workflow", a document literally named "workflow doc"), so **both branches stay advisory, not directive** - matching this file's own "agent decides" principle from the New Flow above. Neither branch instructs the model that it *must* call `run_temp`; both explicitly defer to its judgment and note the tool's own 2+-step requirement, so a false match cannot push an unwanted call and a genuine but single-step "workflow" request does not walk into the tool's own rejection with no warning. Domain examples are avoided entirely (the old fixed wording used "weather" as its example of something that never needs a workflow - directly contradicting a user who explicitly asked to run a weather lookup AS a workflow, and the model complied by doing every step manually instead of building one, per its own instruction).

### Step 4 — Main Agent Decides

The main agent sees:

- `[SESSION WORKSPACE]` — stable workspace path for this chat session
- `[ACTIVE PROJECT]` — most recently created/edited project
- `[WORKFLOW SUGGESTION]` — pre-detected workflow + pre-extracted variables
- Full conversation history

It can then make an informed decision:

| Situation | Agent action |
|-----------|-------------|
| User wants **new** creation, no workspace | `execute_workflow(workflow_id=..., variables={...})` |
| User wants to **edit** an existing project | `coding_agent(task=..., project_path=<workspace>)` |
| Not sure what workflows exist | `list_workflows()` |
| No workflow needed | Use any other tool directly |

---

## Agent Tools

| Tool | Purpose |
|------|---------|
| `execute_workflow(workflow_id, variables)` | Start a workflow by ID with optional pre-filled variables |
| `list_workflows()` | Browse all available workflows with descriptions |
| `create_agent_workflow(action, ...)` | Create and run workflows at runtime (see below) |

Both `execute_workflow` and `list_workflows` are available to the main agent. The agent can also **adjust the pre-extracted variables** before calling `execute_workflow` — the hint is a starting point, not a constraint.

A weak model can confuse the two: `execute_workflow`'s `workflow_id` must be a **saved template id** (from `list_workflows`), never the name of a tool - in particular never `"create_agent_workflow"` itself, which is the *other* tool (builds/runs a workflow, does not look one up by id). Both tools' descriptions now say this explicitly, and `execute_workflow` detects a live tool-name collision and redirects to the right tool instead of just repeating the template list (`vaf/tools/workflow_executor.py`).

The redirect is an ECHO-BACK when possible: a model that merged the two hints usually delivers a complete, correct run_temp payload inside `variables` (live incident: `execute_workflow(workflow_id="create_agent_workflow", variables={action: "run_temp", steps: [...]})` with perfectly good steps - after a prose-only redirect the model gave up on workflows and did every step manually). When `variables` carries `steps`, the error message hands back the exact `create_agent_workflow(...)` call to copy, with the model's own arguments verbatim (action defaulted to `run_temp`, oversized payloads fall back to the generic advice). Weak models copy reliably; they rephrase poorly. The redirect stays a MESSAGE - it never auto-forwards the call (dispatch gates and the "agent decides" principle stay intact).

### A workflow can end in THREE ways, not two

`WorkflowEngine.execute()` returns success, failure, or **paused**. A step that hands its work
to an async sub-agent (a document or browser step spawning a child) does not finish the run:
the engine saves the state and returns `WorkflowResult(success=False, error=None, paused=True)`.

Every consumer must branch on `paused` BEFORE it looks at `success`. A consumer that knows
only two outcomes reports a healthy run as a crash: on 2026-07-20 the user was told
`Workflow 'Research & Document' failed: None` (None because `error` is unset precisely when
nothing failed) while the document was still being written, and the assistant then apologised
for a crash that never happened. Three of the six consumers had the branch missing.

All lanes share one wording, `paused_tool_message()` in `vaf/workflows/engine.py`. It avoids
the words "failed" and "error" on purpose: both the repo's own result classifier
(`context.tool_result_is_error`) and a skimming local model key on them, and this text is the
opposite of a failure. `tests/test_workflow_paused_not_failed.py` pins the contract for every
consumer and freezes the consumer list, so a new lane cannot join without a paused branch.

Who continues a paused run is documented in
[SUBAGENT_IPC.md](SUBAGENT_IPC.md#who-resumes-and-how-far): the CLI drain runs the remaining
steps, while the Web UI drain currently only closes a run whose awaited step was the last one.

### `create_agent_workflow` — runtime workflow creation

The agent can define and run its own workflows at runtime without any human involvement. Two modes:

#### `run_temp` — ephemeral plan execution

```python
create_agent_workflow(
    action="run_temp",
    name="Research and summarize",
    steps=[
        {"input": "Search for {topic} news",        "tool": "web_search",   "output": "news"},
        {"input": "Write a 3-paragraph brief:\n{news}", "tool": "coding_agent", "output": "brief"},
    ],
    variables={"topic": "quantum computing"},
)
```

- No file is written to disk. Nothing is saved after execution.
- Ideal for complex one-off tasks: the agent designs a multi-step plan, executes it, and returns the result.
- Available to the agent in **any session** (not admin-only).
- The `WorkflowEngine` runs synchronously using the agent's **full live tool registry** — all tools currently loaded, including custom ones.
- Each step's `output` is available as `{variable}` in subsequent steps.
- **Weak-model step repair (`_repair_raw_step`):** the canonical step shape is `input` + `tool`,
  but a weak model reliably mangles the FIELD NAMES while getting the plan right - live incident:
  `{"action": "web_search", "description": "Wetter suchen (Berlin)", "name": "step_1_wetter"}`,
  which the old nested schema requirement (`required: ["input"]`) rejected wholesale
  ("'input' is a required property"); the model could not act on that message and regressed into
  planning spin until the loop guards ended the turn. Steps are now repaired before validation:
  `tool` also accepts step-level `action`/`agent_id`, `input` falls back through
  `code`/`task`/`prompt`/`instruction`/`query`/`description`/`name`, and an args-only step gets a
  synthesized label (the engine runs it via `args` anyway). A step with truly nothing usable
  still errors, now with a targeted message. Applies to `run_temp` AND `create`.
- **Weak-model safety net (auto-attach):** a weak model reliably NAMES step outputs but never
  references them - a live incident's final coding_agent step said "use the results from the
  previous searches" in prose, with no `{placeholder}` anywhere, so substitution had nothing to do,
  the coder received zero data, and the strict factual-data anchor made it render
  `[DATA NOT FOUND]` into every field of the deliverable. When a task-consuming agent step
  (`coding_agent` / `document_writer` / `document_agent` / `librarian_agent` / `browser_agent` -
  every agent tool whose primary arg is a free-text `task`; builders and analyzers alike)
  references NO prior step output in its template, the engine now appends a bounded
  "RESULTS FROM PREVIOUS WORKFLOW STEPS" digest of the actual results (3000 chars per result,
  9000 total) to the step's instruction. `research_agent` is deliberately excluded: its primary
  arg is a short `topic` query - attaching result data would pollute the search profile, and its
  job is to produce data, not consume it. Templates that DO reference an output - every saved
  template - are never touched. This applies in EVERY lane that runs the engine (run_temp, saved
  templates via `execute_workflow`, the CLI `@workflow` lane, automations with workflow steps).
  The full authored step list is logged as a `[RUN_TEMP]` line (backend log) so the next forensic
  is a grep, not an inference.
- **`input` + partial `args` merge:** a step may carry its instruction in `input` and only the
  EXTRA parameters in `args` (`{"max_results": 3}`) - exactly the shape the tool schema teaches.
  The engine used to build the call ONLY from `args` when present, silently dropping the input;
  web_search ran query-less and the whole run failed with "Error: No query provided." (live
  incident). The resolved `input` now fills the tool's missing PRIMARY parameter
  (`_PRIMARY_ARG_BY_TOOL`); steps whose `args` already carry it - every saved template - are
  untouched (their `input` stays a display label).
- **Completion carries every step's result:** both completion messages (saved workflows via
  `execute_workflow` and temporary ones) append a bounded "Step results" summary - one line per
  step with tool, status and result head (`engine.summarize_run_steps`). The completion used to
  show only the LAST step's output; when a template's final step produced garbage, the model read
  it next to "completed successfully", concluded the run produced nothing, and redid all the work
  manually (live incident; see also the removed librarian completion-message anti-pattern in the
  templates).
- **Minimum two steps.** A single-step `run_temp` is rejected with an error — a lone step has no output to chain and gains nothing from the engine, so the agent should call that tool directly instead. The only exception is a single `create_automation` step (scheduling a task, as the built-in "Create Scheduled Task" workflow does), which is allowed.

##### Step fields

| Field | Type | Description |
|-------|------|-------------|
| `input` | string | Prompt/instruction. Supports `{variable}` substitution. **Required.** |
| `tool` | string | Tool name (e.g. `coding_agent`, `web_search`). Default: `coding_agent`. |
| `output` | string | Variable name for this step's result. Default: `step_N_output`. |
| `description` | string | Label shown in progress display. |
| `on_success` | string | Jump to this step's `output` name on success. |
| `on_failure` | string | Jump to this step's `output` name on failure (suppresses abort). |
| `optional` | bool | Skip on failure instead of aborting. |
| `assertions` | list | Output checks — failed assertions retry only this step (not the whole workflow). |
| `max_assertion_retries` | int | How many times to retry on assertion failure. Default: `1`. |
| `validate` | bool | Opt-in: LLM-check this content/agent step's output against its goal, retry with a correction up to 3× then accept. See **Per-step output validation** below. Default: `false`. |

##### Available tools per step

| Tool | Best for |
|------|----------|
| `coding_agent` | Write/edit code, generate HTML/CSS/JS, structured files, analysis scripts. **Default.** |
| `research_agent` | Deep research (10+ sources), patent analysis, market studies, technical reports. |
| `document_writer` | Simple structured documents as .txt/.md/.docx ONLY (contracts, reports, letters); other extensions are rejected. Writes into the chat workspace (the injected `project_path` is ignored). |
| `librarian_agent` | File system: read/list/search files in directories. |
| `web_search` | Quick single lookup (news, facts, prices). |
| `write_file` | Write raw content to a specific path. |
| `read_file` | Read a file (e.g. output from a previous step). |
| `python_sandbox` | Data processing, calculations, Python scripts. |
| `send_to_user` | Delivery step: send the final message (plus optional produced file) to the user's main messenger, resolved at run time; Web UI fallback. See *Delivery steps* below. |

**Rule:** Use `research_agent` for patent/market/technical research needing many sources. Use `coding_agent` for file generation and scripts.

The table above lists the common ones, but a step can call **any tool the user has in chat** — `search_tools`, `list_tools`, calendar/memory/GitHub tools, custom tools, etc. Both `run_temp` and **saved** workflows (`execute_workflow`) run on the agent's full live registry, plus the workflow primitives (`bash`, `move_file`) that the Main Agent normally delegates to sub-agents (`write_file` is registered to the Main Agent directly as well). (Saved workflows previously used a fixed subset, which is why a step like `search_tools` could report "Tool not found" — they now overlay the same live registry as `run_temp`.)

##### Shared project path (`{workflow_project_path}`)

At workflow start the engine creates **one shared directory** for the run (e.g. `VAF_Projects/<uid[:8]>/<session_id>/Patent Workflow/` — same user/chat scoping as coder projects) and injects it automatically as `project_path` for every `coding_agent` and `document_writer` step (`document_writer` currently ignores the injected `project_path` and writes into the session workspace itself). Relative new-artifact paths in `write_file` (`path`) and `move_file` (`src`/`dst`) steps are resolved against the same directory, so a bare filename like `draft.md` never resolves against the backend process cwd. Explicit absolute or `~`-anchored paths, folder aliases the filesystem tools resolve themselves (`Desktop/…`, `Documents/…`), and relative paths pointing at an existing file (in-place updates) are left untouched. All steps therefore write to the same folder — no scattered timestamp-suffixed directories.

The path is also available as `{workflow_project_path}` in step input templates:

```python
{"input": "Read the JSON from {workflow_project_path}/patent.json and build an HTML report.",
 "tool": "coding_agent", "output": "report"}
```

##### Built-in variables (`{date}`, `{time}`, …)

Every run seeds a fixed set of **temporal built-ins** into the variable scope, so a step template (or a model-generated workflow) can reference them without the caller declaring anything:

| Variable | Example | Notes |
|----------|---------|-------|
| `{date}`, `{today}`, `{current_date}` | `2026-06-29` | filename-safe |
| `{time}` | `14-05` | filename-safe (no `:`) |
| `{timestamp}` | `20260629_140512` | filename-safe; ideal for unique filenames |
| `{now}`, `{datetime}` | `2026-06-29 14:05` | human-readable |
| `{iso_date}` | `2026-06-29T14:05:12` | ISO 8601 |
| `{year}`, `{month}`, `{day}` | `2026` / `06` / `29` | individual parts |

A real user-supplied variable of the same name always wins (built-ins are seeded with `setdefault`). These exist because LLM-generated automation workflows routinely write filenames like `report_{date}.html`; without a value, the run used to abort.

**Unknown placeholders are non-fatal.** A simple `{var}` the engine cannot resolve (not a step output, not a built-in) is left **literally** in the text (`"{var}"`) instead of aborting the workflow — so one hallucinated placeholder can no longer fail an entire scheduled automation. Nested access to a missing object (`{step.field}`) still raises, surfaced as a per-step `Missing variable` error.

##### Delivery steps (`send_to_user`)

When a workflow should notify the user, the LAST step is `send_to_user(message,
file_path)` - channel-agnostic: the platform comes from the user's `main_messenger`
at run time (see [CONNECTIONS.md](../integrations/CONNECTIONS.md), *Channel model*),
never from the workflow definition.

**Rule:** send steps are deterministic - they deliver their arguments VERBATIM. No
LLM sits between template resolution and delivery, so a raw step output
(`{search_results}`) or an instruction ("summarize this") in `message` reaches the
user's phone as-is (live incident: a generated weather automation did exactly that).
Produce the final text in a preceding CONTENT_ONLY generation step, then send that
step's `{output}`.

**Rule:** never put `assertions` or `validate` on a delivery step - an assertion
retry re-sends the message to the user. Validation belongs on the step that
produces the content.

`file_path` is best-effort by contract (the text send decides success; the router
skips a missing file silently), and a relative `file_path` resolves against
`{workflow_project_path}` exactly like `write_file`'s `path` did, so a bare
filename written earlier in the run is found. If no messenger is configured the
step still succeeds: the tool reports the Web UI fallback honestly in its result.
In the automation lane, a confirmed in-run delivery suppresses the post-run
messenger push (no double message; see
[AUTOMATIONS.md](../platform/AUTOMATIONS.md), *Result delivery*).

##### Assertions (selective step-retry)

```python
{"input": "Write a patent valuation report for {patent_id}.",
 "tool": "research_agent",
 "output": "report",
 "assertions": [
     {"contains": "{patent_id}", "error": "Patent ID missing from report"},
     {"not_contains": "EP 3 456",  "error": "Wrong patent cited"},
 ],
 "max_assertion_retries": 2}
```

On assertion failure the engine retries **only that step** with a correction hint prepended — previous steps are not re-run.

##### Per-step output validation (opt-in, `validate: true`)

Assertions are deterministic substring checks. For content/agent steps you often want a *semantic* check: did the output actually fulfil the step's goal? Set `"validate": true` on the step and an LLM judges the output against the step's goal (its `description`/`input`). On a mismatch the step is re-run with a correction hint, up to **3** times (`workflow_step_validation_max_retries`); after that the last version is **accepted** and the workflow continues — validation never hard-fails the step, and a validator error is treated as a pass.

```python
{"input": "Write a one-page summary of {research} focused on pricing.",
 "tool": "document_agent",
 "output": "summary",
 "validate": True}
```

- **Eligible tools only:** `document_agent`, `document_writer`, `research_agent`, `coding_agent`, `browser_agent`, `librarian_agent` (a correction-retry can't change a deterministic tool's output, so `validate` is ignored elsewhere).
- **No lenient fast-path:** unlike the Main Agent's direct sub-agent validation, this judges the *content* — a step that merely reports "saved successfully" but produced the wrong/empty document is caught.
- **Auto-enable (was a confirmation gate):** if a workflow has eligible steps but **none** sets `validate`, `run_temp` now enables validation on those steps automatically and RUNS; `skip_validation: true` is the explicit opt-out. The old behavior returned a `[VALIDATION CHECK]` bounce asking the agent to re-call with flags - a live incident showed a weak model bouncing twice (retrying without the flags both times) and then doing every step manually while its correctly authored workflow never ran. Validation-on is exactly what the bounce text recommended, so the system decides it itself.
- Globally toggled via `workflow_step_validation_enabled` (default on). See [Sub-Agent IPC](SUBAGENT_IPC.md#per-step-output-validation-opt-in).

##### Variable Anchoring (automatic)

For `coding_agent`, `research_agent`, `document_writer`, and `librarian_agent` steps the engine **automatically prepends** all original workflow `variables` as `## IMMUTABLE DESIGN PILLARS` to the task text. This prevents later steps from losing track of values like `patent_id` or `genre` that were set at workflow start.

##### Living Document Pattern (for 5+ step workflows)

Instead of chaining `{prev_step_output}` (which causes drift in long workflows), write a shared file all steps read and update:

```python
steps=[
    {"input": "Read {patent_id} spec. Write findings to /tmp/{wf_id}/report.json",
     "tool": "coding_agent", "output": "step1"},
    {"input": "Read /tmp/{wf_id}/report.json, add valuation section, write it back.",
     "tool": "coding_agent", "output": "step2"},
    # Step N always reads the full, growing report.json — nothing is lost.
]
```

#### `create` — persistent workflow (admin-only)

```python
create_agent_workflow(
    action="create",
    workflow_id="daily_brief",
    name="Daily Briefing",
    description="Searches news and writes a daily brief",
    triggers=["daily brief", "morning summary"],
    steps=[...],
)
```

- Saves to `~/.vaf/workflows/{workflow_id}.py` with a `# created_by: agent` marker.
- Immediately reloads `WORKFLOW_TEMPLATES` — available via `execute_workflow()` and visible in the WebUI Workflows tab.
- Agent can only delete workflows it created itself (ownership enforced by first-line marker).
- Requires an **admin session** (same gate as `create_agent_tool`).

#### Other actions

| Action | Description |
|--------|-------------|
| `list` | List all agent-created persistent workflows |
| `delete` | Remove an agent-created workflow (admin-only) |

---

## System Prompt Guidance

The `workflow` module is activated in the system prompt whenever the conversation contains workflow-related keywords (`workflow`, `website`, `webseite`, `erstell`, `create`, `generate`, `build`, `automation`, etc.).

The module instructs the agent:

1. Check `[SESSION WORKSPACE]` — if a workspace exists and the user is asking to **edit/improve**, use `coding_agent`. **Do NOT start a creation workflow** that would discard the user's work.
2. If creating something new, call `execute_workflow` with the suggested (or adjusted) variables.
3. If unsure what's available, call `list_workflows` first.

---

## Dynamic Discovery

Workflows are discovered at runtime via `list_templates()`. Adding a new workflow file makes it automatically available to the router and to `list_workflows` — no code changes required.

---

## Intent Locking

After `execute_workflow` is called and a workflow begins execution, VAF applies an **Intent Lock**:

1. **Snapshot:** The original user prompt is saved to `.vaf/main/user_intent.md`.
2. **Guidance:** This "North Star" is injected into every step of the workflow.
3. **Drift Prevention:** Even if a sub-agent reports "Task Done", the main agent remembers *why* the task was started.

---

## Configuration

```json
{
  "workflows_enabled": true
}
```

- **`workflows_enabled`**: Toggle the entire workflow system. When `false`, `_try_workflow()` returns `None` immediately and the agent handles all requests with its standard tool set.

---

## Example Decisions

| User message | Workspace | Agent decision |
|--------------|-----------|---------------|
| "Erstelle eine neue Website für ein Restaurant" | none | `execute_workflow("create_website", {description: "Restaurant"})` |
| "Mach die Farben der Seite dunkler" | `/VAF_Projects/Webseite Foo` | `coding_agent(task="...", project_path="/VAF_Projects/Webseite Foo")` |
| "Kannst du den Titel ändern?" | `/VAF_Projects/Webseite Foo` | `coding_agent` — no workflow |
| "@create_website Portfolio-Seite" | any | Direct execution (explicit command, no hint) |
| "Welche Workflows gibt es?" | any | `list_workflows()` |

---

## Related Documentation

- [Skills](SKILLS.md) — Agent Skills (SKILL.md), the second routing tier sharing this router
- [Session Management](../memory/SESSION_MANAGEMENT.md) — `project_path` / `[SESSION WORKSPACE]`
- [Coder Architecture](CODER_ARCHITECTURE.md) — `coding_agent` tool internals
- [Context Management](../memory/CONTEXT_MANAGEMENT.md) — Intent Locking details
- [Sub-Agent IPC](SUBAGENT_IPC.md) — How workflows execute tasks
- [User Isolation](../security/USER_ISOLATION.md) — Per-user project directories

---

*Last updated: 2026-07-13*
