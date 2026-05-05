# Intelligent Workflow Selection

## Overview

VAF uses an **intelligent, LLM-powered workflow selection system** that understands user intent and routes requests to the optimal workflow. Unlike traditional pattern-matching systems, VAF's workflow selector **thinks** about what the user wants to achieve, not just which keywords they used.

With the **Agatic vNext** update, the Workflow Router is now fully dynamic and "Plug & Play". It discovers available workflows at runtime and maps user intent to capabilities without rigid hardcoded rules.

## Key Innovation: Dynamic Intent-Based Routing

### Old Approach (Static Priorities)

Previous versions relied on a hardcoded "Priority 1-8" list in the prompt.
- **Problem:** New workflows required code changes in the router prompt.
- **Problem:** Rigid hierarchy made it hard to choose between similar workflows.

### New Approach (Dynamic Discovery)

The new router works like a smart "App Store":
1. **Discovery:** It calls `list_templates()` to get *all* currently available workflows.
2. **Context:** It reads the descriptions of each workflow dynamically.
3. **Reasoning:** It maps the User's Intent to the best matching Description.

**Result:** You can add a new workflow file, and the router "knows" it instantly without code changes!

## Architecture

### The Routing Process

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
│      Returns ID                       Returns 'none'            │
│      (e.g., 'create_website')         (Main Agent handles it)   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Intent Locking (New!)

To ensure the agent doesn't lose focus during a long workflow execution, VAF applies an **Intent Lock** immediately after selection:

1. **Snapshot:** The original user prompt is saved to `.vaf/main/user_intent.md`.
2. **Guidance:** This "North Star" is injected into every step of the workflow.
3. **Drift Prevention:** Even if a sub-agent reports "Task Done", the main agent remembers *why* the task was started.

### The New Router Prompt

The routing logic is now pure reasoning, no arbitrary rules:

```
ROUTING INSTRUCTIONS:
1. Analyze the User Request for INTENT.
2. Check if a Workflow matches that intent EXACTLY.
3. Return the `workflow_id` if a strong match exists.
4. Return `none` if:
   - The request is a simple lookup (weather, news).
   - The request is generic chat.
   - You would rather use individual tools directly.
```

## Benefits

### 1. True "Plug & Play"

Developers can add new workflows by simply creating a template file. The router automatically picks it up and understands when to use it based on its description.

### 2. Intent-Based Routing

The system understands what the user **means**:

```
User: "Ich brauche einen Arbeitsvertrag"
LLM: "Intent is legal document creation. Matches 'legal_contract_research'."
-> Selects 'legal_contract_research'
```

### 3. Smart Fallback ("None")

The router is smart enough to say **"No"**.
- User: "What is the weather?"
- Router: "This is too simple for a workflow. Return `none`."
- Result: Main Agent uses `web_search` tool directly (faster!).

### 4. Document Editor Mode (Skip Workflow)

When the user message contains the **Document Editor** context block (`--- CURRENT DOCUMENT (Editor): ... ---`), workflow matching is **skipped entirely**. The agent is given the editor content and optional marked selections; it should use tools such as `replace_editor_selection` or `document_editor` instead of starting a workflow (e.g. code_review). This avoids the router selecting an inappropriate workflow when the user is editing a document and has marked text to replace.

## Configuration

Workflow selection behavior can be tuned in `~/.vaf/config.json`:

```json
{
  "workflows_enabled": true
}
```

- **`workflows_enabled`**: Toggle the entire system.
- **Routing backend note**: Workflow routing follows the active backend mode (`api_backend` / local-server path / selector fallback). `force_server` is primarily a local model loading/runtime flag, not a dedicated "workflow router" switch.

## Related Documentation

- [Context Management](CONTEXT_MANAGEMENT.md) - Intent Locking details
- [Sub-Agent IPC](SUBAGENT_IPC.md) - How workflows execute tasks
