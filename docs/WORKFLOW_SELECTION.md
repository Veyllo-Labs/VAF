# Workflow Selection & Recommendation Architecture

## Overview

VAF uses a **two-phase workflow system**: an LLM-powered router detects whether the user's request matches a workflow, and the **main agent** decides whether to actually execute it. This prevents false-positive auto-executions (e.g. triggering `create_website` when the user asks to *change* an existing site) while keeping the full power of multi-step workflows available.

With the **Agatic vNext** update, the Workflow Router is fully dynamic and "Plug & Play" — it discovers available workflows at runtime and maps user intent to capabilities without hardcoded rules.

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
│  │  🧠 ROUTER LLM (Intent Analysis)                     │       │
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

### Step 3 — Workflow Hint Injection

If the router matched a workflow, `chat_step()` prepends a `[WORKFLOW SUGGESTION]` block to the user input before calling the LLM:

```
[WORKFLOW SUGGESTION] The workflow "Create Website" (create_website) looks relevant to this request.
Pre-extracted variables: description="Restaurant website"
To start it call: execute_workflow(workflow_id="create_website", variables={...})
IMPORTANT: If the user is asking to edit or modify an existing project
(see [SESSION WORKSPACE] above), use coding_agent with project_path instead
— do NOT start a creation workflow.

<original user message>
```

The hint is **one-shot** — it is consumed and cleared immediately. The next message starts fresh with no pre-set hint.

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
| `document_writer` | Professional Word/PDF documents (contracts, reports, letters). |
| `librarian_agent` | File system: read/list/search files in directories. |
| `web_search` | Quick single lookup (news, facts, prices). |
| `write_file` | Write raw content to a specific path. |
| `read_file` | Read a file (e.g. output from a previous step). |
| `python_sandbox` | Data processing, calculations, Python scripts. |

**Rule:** Use `research_agent` for patent/market/technical research needing many sources. Use `coding_agent` for file generation and scripts.

The table above lists the common ones, but a step can call **any tool the user has in chat** — `search_tools`, `list_tools`, calendar/memory/GitHub tools, custom tools, etc. Both `run_temp` and **saved** workflows (`execute_workflow`) run on the agent's full live registry, plus the workflow primitives (`write_file`, `bash`, `move_file`) that the Main Agent normally delegates to sub-agents. (Saved workflows previously used a fixed subset, which is why a step like `search_tools` could report "Tool not found" — they now overlay the same live registry as `run_temp`.)

##### Shared project path (`{workflow_project_path}`)

At workflow start the engine creates **one shared directory** for the run (e.g. `VAF_Projects/Patent Workflow/`) and injects it automatically as `project_path` for every `coding_agent` and `document_writer` step. All steps therefore write to the same folder — no scattered timestamp-suffixed directories.

The path is also available as `{workflow_project_path}` in step input templates:

```python
{"input": "Read the JSON from {workflow_project_path}/patent.json and build an HTML report.",
 "tool": "coding_agent", "output": "report"}
```

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
- **Confirmation gate:** if a workflow has eligible steps but **none** sets `validate`, `run_temp` does not execute — it returns a `[VALIDATION CHECK]` asking you to either flag the critical steps or pass the top-level `skip_validation: true` to run without validation on purpose.
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

- [Session Management](SESSION_MANAGEMENT.md) — `project_path` / `[SESSION WORKSPACE]`
- [Coder Architecture](CODER_ARCHITECTURE.md) — `coding_agent` tool internals
- [Context Management](CONTEXT_MANAGEMENT.md) — Intent Locking details
- [Sub-Agent IPC](SUBAGENT_IPC.md) — How workflows execute tasks
- [User Isolation](USER_ISOLATION.md) — Per-user project directories

---

*Last updated: 2026-05-22*
