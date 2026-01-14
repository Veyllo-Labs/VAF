# Coder Sub-Agent: Comprehensive Code Reference

This document provides an exhaustive, block-by-block explanation of the `vaf/tools/coder.py` module (~6600 lines). It documents the logic, state transitions, enforcement mechanisms, and control flow.

---

## 1. Global Infrastructure & Helpers (Lines 1-600)

### Imports
Standard libraries (`os`, `json`, `re`, `threading`) and `rich` components are imported.
*   **`vaf.tools.base`**: Base class for all tools.
*   **`vaf.tools.filesystem`**: Low-level file operations (`WriteFileTool`, `ReadFileTool`).
*   **`vaf.tools.coder_templates`**: Manages template assets.

### Helper Functions
*   **`_get_clickable_path(path)`**: Converts a path to a `file://` URI for terminal clickability.
*   **`_open_folder(path)`**: OS-independent folder opening (`startfile` on Win, `open` on Mac, `xdg-open` on Linux).
*   **`_get_open_instructions(files, base_dir)`**: Analyzes created files to generate context-aware help (e.g., "To run this Python script...").
*   **`_run_linter_for_files(files, history, local_tools)`**:
    *   Iterates through provided files.
    *   Maps extensions (`.py`, `.js`) to linter types.
    *   Executes the `linter` tool.
    *   **CRITICAL:** Appends linter results directly to `history` as a `system` message. This ensures the LLM sees errors immediately.

### `CoderTUI` Class (The Interface)
Implements a "Mini-IDE" using `rich.live`.
*   **`__init__`**: Initializes state (`files`, `current_action`), locks (`RLock` for thread safety), and the `AnimatedHeader`.
*   **`render()`**: The main draw loop. Constructs a `Layout` with:
    *   **Header:** Agent status.
    *   **Left Panel:** File tree (Icons show status: 📝 Writing, ✅ Done, ❌ Error).
    *   **Right Panel:** Live Token Stream (simulates typing) or Code Preview.
*   **`set_code_preview()`**: Updates the right panel to show syntax-highlighted code currently being written or diffed.
*   **`update_file()`**: Updates file status icons in the left panel safely from background threads.

---

## 2. `CodingAgentTool` Class Structure

### `_generate_project_directory(task)`
*   **Logic:**
    1.  Scans `task` string for keywords ("website", "app", "script").
    2.  Extracts semantic keywords (removing stop words like "the", "create").
    3.  Sanitizes the name using Regex to be OS-safe (removes `/ \ : * ?`).
    4.  Constructs path: `~/Documents/VAF_Projects/{Prefix}_{Name}`.
    5.  **Duplicate Check:** If folder exists, appends timestamp `_{HHMMSS}`.

### `_ensure_git_repo(base_dir)`
*   **Logic:**
    1.  Checks for `.git` folder.
    2.  If missing, runs `git init`.
    3.  Writes a default `.gitignore` (Python/Node/IDE patterns).
    4.  Runs `git add .` and `git commit -m "Initial commit"` to secure the starting state.

---

## 3. The `run()` Execution Flow (The Core)

This is the massive entry point method.

### A. Process Isolation & IPC (Lines ~1600-1700)
*   **Check:** Is `VAF_IN_SUBAGENT_TERMINAL` env var set?
*   **IF NOT (Main Process):**
    *   Check `Config.sub_agents_in_separate_terminals`.
    *   **Spawning:** Uses `sys.executable` to spawn a NEW process running `vaf.main subagent run`.
    *   **IPC:** Creates a task in `subagent_ipc` and passes the ID.
    *   **Return:** Returns a placeholder string `[SUBAGENT_ASYNC:...]` to the main agent.
*   **IF YES (Sub-Agent Process):**
    *   Proceeds to execute the logic below.

### B. Initialization (Lines ~1700-2200)
*   **TUI Start:** Initializes `CoderTUI` and starts the `Live` context.
*   **Template Logic:**
    *   Checks if user asked for a specific template (e.g. "create react app").
    *   If yes, copies files to `base_dir`.
    *   Sets `template_files` list for later protection logic.
*   **System Prompt Generation:**
    *   Generates the **Supervisor System Prompt**.
    *   **Crucial Instruction:** "Your FIRST action MUST be to call `set_todos`".
    *   **Hidden Tools:** Explicitly hides `task_done` from the prompt text to force planning.

### C. Hierarchical Context Setup (Lines ~2200-2400)
*   **`ContextState` Class:** Defined locally to hold `ContextManager`, `history`, `phase`, and `files_created` for a specific scope.
*   **`context_states` Dict:** Stores the state for "main" and every "task_N".
*   **`switch_to_task_context(task_idx)` Helper:**
    *   Saves current state.
    *   If task state exists -> Resumes it.
    *   If new -> Creates **FRESH** `ContextManager` (8k/16k tokens).
    *   **Glue Injection:** Generates `_build_completed_info()` (summary of previous tasks) and injects it into the *new* system prompt. This prevents "Context Amnesia" without polluting the window.

---

## 4. The Agentic Loop (`while True`) (Lines ~2600+)

This loop runs until the project is complete.

### D. Tool Schema Generation (Dynamic)
Inside the loop, `current_tools` is generated dynamically based on state:
*   **IF `task_mgr.has_plan() == False`:**
    *   **Allowed:** `set_todos`, `web_search`, `read_file`.
    *   **Hidden:** `write_file`, `task_done`.
    *   **Goal:** Force the agent to plan.
*   **IF `task_mgr.has_plan() == True`:**
    *   **Allowed:** `write_file`, `read_file`, `web_search`, `python_sandbox`, `task_done`, `bash`.
    *   **Hidden:** `set_todos` (to prevent re-planning loops).

### E. LLM Interaction & Safety Nets
*   **Call:** `self.llm.chat_completion(...)`.
*   **Zombie Detection:**
    *   Tracks `idle_loop_count` (loops with text but no tool calls).
    *   **IF count > 3:** Injects `🛑 SYSTEM OVERRIDE: STOP THINKING. CALL A TOOL.` logic.
*   **Fake Completion:**
    *   Scans text for "I am done", "Finished".
    *   **IF** text says "done" **AND** no `task_done` tool call:
    *   **ACTION:** Injects `⚠️ You claimed completion but didn't call task_done. Call it now.`

---

## 5. Tool Implementation Logic (The Big IF/ELIF Block)

### `set_todos` (Lines ~5180)
*   **Validation:** Checks if `tasks` list is valid.
*   **Phase Check:**
    *   **IF** called during execution phase: **BLOCK** ("Cannot modify TODOs during execution").
    *   **ELSE:** Parses tasks into `TaskManager`.
*   **Context Switch:** Immediately switches context to `task_0`.

### `write_file` (Lines ~5900)
*   **Pre-Check:**
    *   **IF** no TODOs set: **BLOCK** ("Call set_todos first").
*   **Template Protection (Crucial):**
    *   **IF** target file is a template file:
        *   Reads original file.
        *   Checks for presence of key tags (`<nav>`, `id="hero"`, `def main`).
        *   **IF elements missing in new content:**
            *   **BLOCK:** `🚨 BLOCKED: Template structure destroyed!`.
            *   Returns error instructing agent to use `read_file` and preserve structure.
*   **Diff Generation:**
    *   Calculates diff between old and new content.
    *   Updates TUI Code Preview.
*   **Execution:** Calls `filesystem.write_file`.
*   **Post-Action Linting:**
    *   Calls `_run_linter_for_files`.
    *   **IF Errors:** Sets `current_state.linter_errors_active = True`.
    *   Injects system message with error details.

### `task_done` (Lines ~5287-5823) - The Enforcement Gate
*   **Gate 1: "No Files Created"**
    *   **IF** task type implies creation (e.g. "create script") **AND** `files_created` is empty:
    *   **BLOCK:** `🚨 CRITICAL ERROR: HALLUCINATION DETECTED! No files created.`
    *   **Action:** Force agent to retry loop.
*   **Gate 2: "Linter Errors"**
    *   **IF** `has_recent_linter_errors` is True:
    *   **BLOCK:** `🚨 TASK_DONE BLOCKED - LINTER ERRORS!`
    *   **Action:** Force agent to fix code.
*   **Gate 3: "Consecutive Calls"**
    *   **IF** agent calls `task_done` > 3 times in a row without doing work:
    *   **BLOCK:** "Stop calling task_done. Do the work."
*   **Success Path:**
    *   Marks task as `completed` in `TaskManager`.
    *   **IF** more tasks remain:
        *   Calculates next task index.
        *   Calls `switch_to_task_context(next_idx)`.
        *   Resets loop.
    *   **IF** all tasks done:
        *   **Executes `break` statement** (Lines 4793, 5782, 5821) to exit the main `while True` loop.
        *   Returns final summary string to Main Agent.

### `web_search` (Lines ~5950)
*   **Planning Mode:**
    *   Injects reminder: "Call `set_todos` NOW based on these results."
*   **Execution Mode:**
    *   Injects reminder: "Use these results to call `write_file`."

### `python_sandbox` (Lines ~5850)
*   **Execution:** Runs code in `vaf.tools.python_sandbox`.
*   **Context:** Returns output (stdout/result) to the LLM history.

---

## 6. Cleanup & Exit
*   **Logic:**
    *   Stops TUI thread (`live.stop()`).
    *   Cleans up temporary handles.
    *   Returns the final string (list of created files + instructions) to the user.
