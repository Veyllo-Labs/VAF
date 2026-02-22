# Context Glue: Dynamic State Tracking & Injection

This document describes VAF's solution to the **Context Overflow Problem** in long-running agentic workflows. We call this architecture **"Context Glue"** (technically: *High-Density Dynamic State Tracking with Injection*).

## 🛑 The Problem: Context Amnesia vs. Overflow

When an LLM agent works on a complex project (e.g., building a website step-by-step), it faces a dilemma:

1.  **Keep Everything:** The context grows with every file read, every code block generated.
    *   *Result:* **Crash** (Context Limit Exceeded) or **Stupidity** (Model forgets start of prompt).
2.  **Clear Context:** We wipe the history after each task.
    *   *Result:* **Amnesia**. The agent forgets what file it just created ("I don't see any files").

## 💡 The Solution: Context Glue

"Context Glue" is a mechanism that extracts critical **facts** from the conversation stream *before* they are deleted, stores them in a structured state object, and **re-injects** them into every future prompt.

It "glues" independent tasks together, allowing the agent to have infinite long-term memory with a fixed short-term context window.

### Architecture Diagram

```mermaid
graph TD
    User[User Input] --> ToolExec[Tool Execution]
    
    subgraph "Seamless Compression Cycle"
        ToolExec -->|Raw Output (Huge)| Extractor[Fact Extractor]
        Extractor -->|Extracted Facts| StateDB[(State Context)]
        Extractor -->|Pruned Output| History[Short-Term History]
    end
    
    subgraph "Glue Injection"
        StateDB -->|Build Summary| GlueBlock[Context Glue Block]
        GlueBlock -->|Inject| SysPrompt[System Prompt]
        SysPrompt -->|Next Request| LLM[LLM]
    end
    
    subgraph "Task Checkpointing"
        TaskDone[Task Done] -->|Archive| Archive[Disk Archive]
        TaskDone -->|Wipe| History
        TaskDone -->|Keep| StateDB
    end
```

---

## 1. Dynamic State Tracking

Instead of relying on the LLM to "read back" through 100 messages to find out what files exist, VAF proactively tracks the **Project State** in a Python object (`StateContext`).

This tracking happens **in real-time** (inside `vaf/core/context.py`):

*   **Files:** Created, Modified, Read (e.g., `index.html`, `styles.css`)
*   **Errors:** Unique error messages encountered (e.g., `ModuleNotFound: numpy`)
*   **Decisions:** Key architectural choices made by the agent
*   **Tools:** Which tools were used recently

### How Extraction Works (`process_tool_output`)

When a tool returns a massive result (e.g., `read_file` returning 2000 lines of code), VAF intercepts it:

1.  **Scan:** Regex scans the output for keywords (`Error`, `Created`, `...`).
2.  **Update:** The `StateContext` is updated immediately.
3.  **Prune:** The actual output added to the LLM history is **truncated** (e.g., "File read, 2000 lines. Facts stored in State.").

---

## 2. Glue Injection

The **Context Glue** is a high-density summary block that is dynamically generated and injected into the **System Prompt** on every turn.

**Note on Hybrid Architecture (Agatic vNext):**
VAF now uses a dual-glue system:
1.  **Main Agent:** Uses `MainPersistenceManager` to inject **Persistent Glue** from disk (`.vaf/main/user_intent.md`, `.vaf/main/team_state.json`). This survives restarts.
2.  **Coder Agent:** Uses the **Dynamic RAM Glue** (described below) for high-speed, task-specific state tracking during a coding session.

Even if we delete the last 50 messages, the Agent still "knows" what happened because of this block.

**Example Glue Block (What the LLM sees):**

```text
╔═══════════════════════════════════════════════════════════════════════╗
║ COMPRESSED CONTEXT STATE (STABLE PROGRESS GLUE)                       ║
╚═══════════════════════════════════════════════════════════════════════╝

### 🧠 MAIN AGENT PERSISTENCE
**User Intent:** "Create a portfolio website"
**Team Status:** 
🟢 Coder (Running)
✅ Researcher (Done)

### 📁 PROJECT STATE (Local RAM)
**Created:** index.html, script.js, styles.css
**Modified:** styles.css
**Read:** README.md

### ⚠️ ERRORS ENCOUNTERED
• 404 Not Found: /api/data
• SyntaxError: Unexpected token <

### 🎯 DECISIONS & PROGRESS
• Decided to use Bootstrap 5 for layout
• Switched to fetch API for data loading

═════════════════════════════════════════════════════════════════════════
```

---

## 3. Task Checkpointing

For the **Coding Agent**, we implement aggressive checkpointing.

When `task_done` is called:
1.  **Snapshot:** The current full history is saved to disk (archive).
2.  **Compress:** The `StateContext` is updated one last time.
3.  **Wipe:** The conversation history is **deleted**.
4.  **Restart:** The next task starts with an empty history, but the **Glue Block** (System Prompt) contains all the knowledge from the previous task.

### Why this is game-changing

*   **Infinite Loops:** The agent can run for 1000 steps without OOM.
*   **Stability:** The prompt size stays constant (~2k-4k tokens), regardless of project size.
*   **Accuracy:** The agent doesn't "hallucinate" files; it sees them listed in the Glue Block.

### Main Agent: Plan-Act-Summarize with `checkpoint_context`

The Coder Agent has used task checkpointing from the start (`create_fresh_context_for_task`). The **Main Agent** now has the same capability via the `checkpoint_context` tool and the **Orchestrator Prompt Module**. When a complex task requires multiple steps, the agent writes a plan to `working_memory.json`, executes one step at a time, persists each result as a working memory note, and calls `checkpoint_context(summary="...")` to archive the history and start fresh. The plan and notes survive because they live on disk, not in chat history. This enables unbounded multi-step execution even on small-context models. For small contexts (`n_ctx` ≤ 12k) with the orchestrator active, the main agent uses **conditional enforcement**: a plan is required before heavy tools (e.g. `read_file`, `web_search`, sub-agents) can be used, and at most 2 heavy tool calls per turn are allowed to avoid overflow. See `CONTEXT_MANAGEMENT.md` → "Plan-Act-Summarize Pattern" for the full architecture and enforcement details.

---

## Implementation Details

*   **Files:** `vaf/core/context.py`, `vaf/core/agent.py`, `vaf/tools/coder.py`
*   **Class:** `ContextManager`, `StateContext`
*   **Methods:** `process_tool_output`, `update_state`, `compress`
