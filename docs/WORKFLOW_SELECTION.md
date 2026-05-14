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

Both tools are available to the main agent. The agent can also **adjust the pre-extracted variables** before calling `execute_workflow` — the hint is a starting point, not a constraint.

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

*Last updated: 2026-05-14*
