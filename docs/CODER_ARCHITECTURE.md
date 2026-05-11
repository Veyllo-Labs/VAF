# Coder Sub-Agent: Comprehensive Code Reference

This document provides an exhaustive, block-by-block explanation of the `vaf/tools/coder.py` module (large multi-thousand-line module). It documents the logic, state transitions, enforcement mechanisms, and control flow.

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
*   **Context Bloat Fix (post write_file):** After a successful `write_file`, the `content` field in the corresponding assistant tool-call message in history is replaced with `[content omitted — N bytes written to disk]`. This prevents large files (30KB+ HTML) from accumulating in the context window, which would cause 400 "context too large" errors and force the model to re-write the file unnecessarily.

### `_NoopLive` Class
Drop-in replacement for `Rich.Live` used when the coding agent runs inside a workflow terminal (`VAF_IN_WORKFLOW_TERMINAL=1`). All method calls (`start`, `stop`, `update`, `refresh`) are no-ops. This ensures code that calls `live.update()` / `live.stop()` doesn't need per-call guards.

### `CoderTUI` Class (The Interface)
Implements a "Mini-IDE" using `rich.live`.
*   **`__init__(simple_mode=False)`**: Initializes state (`files`, `current_action`), locks (`RLock` for thread safety), and the `AnimatedHeader`.
    *   **`simple_mode=True`**: Disables the Rich Live display. `append_stream()` prints `[Coder] text` directly to stdout instead of buffering. `update_file()` prints `[Coder] ✅ Written: file` on completion. All other methods remain silent. Used when running inside a workflow terminal to avoid replacing the workflow's output with the full-screen TUI.
*   **`render()`**: The main draw loop. Constructs a `Layout` with:
    *   **Header:** Agent status.
    *   **Left Panel:** File tree (Icons show status: 📝 Writing, ✅ Done, ❌ Error).
    *   **Right Panel:** Live Token Stream (simulates typing) or Code Preview.
*   **`set_code_preview()`**: Updates the right panel to show syntax-highlighted code currently being written or diffed.
*   **`update_file()`**: Updates file status icons in the left panel safely from background threads.

---

## 2. `CodingAgentTool` Class Structure

### `_determine_base_dir(task, provided_path)` (The Smart Switch)
*   **Logic:** Decides whether to work in the current directory or create a new one.
    1.  **Explcit:** If `provided_path` is set -> Use it.
    2.  **Edit Mode:** If CWD is a project root (`.git`, `.vaf`, etc.) AND task is NOT "create new" -> **Use CWD**.
    3.  **Scaffold Mode:** If user intent is "create new", "scaffold" -> Call `_generate_project_directory`.
    4.  **Fallback:** If unsure, default to creating a safe sandbox in `VAF_Projects`.

### `_generate_project_directory(task)`
*   **Role:** Helper for Scaffold Mode (creates new folders).
*   **Logic:**
    1.  Scans `task` string for keywords to choose the folder prefix:
        -   **"Webseite"** prefix: `website`, `webseite`, `homepage`, `landing page`, `.html`, `index.html`, `html datei`, `html file` — HTML keywords are checked **first** to avoid false matches (e.g. `<script>-Tag` in an HTML task description must not be classified as a Script project).
        -   **"Script"** prefix: only narrow matches like `python script`, `bash script`, `.py script` — bare `script` is intentionally excluded.
        -   **"App"**, **"Tool"**, **"API"** etc. for other common types.
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
    *   **Spawning:** Uses `sys.executable` to spawn a NEW process via `python -m vaf.main subagent run coding_agent`.
    *   **IPC:** Creates a task in `subagent_ipc` and passes the ID.
    *   **Return:** Returns a placeholder string `[SUBAGENT_ASYNC:...]` to the main agent.
*   **IF YES (Sub-Agent Process):**
    *   Proceeds to execute the logic below.

### B. Initialization (Lines ~1700-2200)
*   **TUI Start:** Initializes `CoderTUI` and starts the `Live` context.
    *   **Workflow mode:** If `VAF_IN_WORKFLOW_TERMINAL=1`, `CoderTUI` is created with `simple_mode=True` and `live = _NoopLive()` — no full-screen display, just `[Coder]` print lines.
*   **API Mode Detection (`_is_api_mode`):**
    *   Checks if the active provider is an API backend (OpenAI, Anthropic, DeepSeek, OpenRouter, Google).
    *   **IF API mode:** Templates are **skipped entirely** — capable API models plan and write without scaffolding. The agent still calls `set_todos` itself.
    *   **IF local model:** Template selection logic runs as normal.
*   **Template Logic (local models only):**
    *   Checks task keywords for template type ("website", "html", etc.) with an LLM-based fallback detector. HTML-specific keywords (`index.html`, `.html`, `html datei`) are included to prevent misclassification.
    *   If a matching template exists, copies files to `base_dir`.
    *   Sets `template_files` list for later reference (soft guidance, not enforcement).
*   **System Prompt Generation:**
    *   Generates the **Supervisor System Prompt**.
    *   **Crucial Instruction:** "Your FIRST action MUST be to call `set_todos`".
    *   **Hidden Tools:** Explicitly hides `task_done` from the prompt text to force planning.
    *   **Template language:** Framed as **guidance** ("recommended workflow", "good baseline") — not as hard rules. The agent is free to deviate from template structure if the task calls for it.

### C. Hierarchical Context Setup (Lines ~2200-2400)
*   **`ContextState` Class:** Defined locally to hold `ContextManager`, `history`, `phase`, and `files_created` for a specific scope.
*   **`context_states` Dict:** Stores the state for "main" and every "task_N".
*   **`switch_to_task_context(task_idx)` Helper:**
    *   Saves current state.
    *   If task state exists -> Resumes it.
    *   If new -> Creates **FRESH** `ContextManager` (8k/16k tokens) via `create_fresh_context_for_task()`.
    *   **Completed-Task Glue:** `_build_completed_info()` summarises previously finished tasks and injects them into the new system prompt (prevents "Context Amnesia" without polluting the window).
    *   **Existing-Files Injection:** `create_fresh_context_for_task()` scans `base_dir` at context-creation time and injects a "FILES ALREADY IN PROJECT — do NOT recreate" list. This prevents a fresh task context from re-writing files already created by the main context or an earlier task, which would otherwise produce duplicates or `PARTIAL_*` files when the LLM truncates mid-write.

---

## 4. The Agentic Loop (`while True`) (Lines ~2600+)

This loop runs until the project is complete.

### D. Tool Schema Generation (Dynamic)
Inside the loop, `current_tools` is generated dynamically based on state:
*   **IF `task_mgr.has_plan() == False`:**
    *   **Allowed:** `set_todos`, `read_file`, `list_files`.
    *   **Hidden:** `write_file`, `task_done`.
    *   **Goal:** Force the agent to plan.
*   **IF `task_mgr.has_plan() == True`:**
    *   **Allowed:** `write_file`, `read_file`, `list_files`, `web_search`, `python_sandbox`, `task_done`, `bash` (when loaded), plus plug-and-play runtime tools.
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
*   **Meta-file Guard:** Blocks writing planning documents (`PLAN.md`, `STRUCTURE.md`, `NOTES.md`, `TODO.md`, `read_chunks.py`, etc.) to the project directory. These files pollute the output and are never part of a deliverable.
*   **Template Validation (soft guidance only):**
    *   **IF** target file is a template file:
        *   Reads original file.
        *   Checks for presence of key structural tags (`<nav>`, `id="hero"`, `def main`, etc.).
        *   **IF elements are missing in new content:**
            *   Logs a **warning** to the TUI stream (e.g. "Note: template sections changed in index.html: Navigation").
            *   **Write is allowed to proceed.** Templates are guidance, not a constraint — the agent may deviate if the task calls for it.
        *   **IF structure is preserved:** Logs a confirmation note.
    *   Placeholder check (`{{PLACEHOLDER}}` still present) still **blocks** `task_done` (not `write_file`) to signal incomplete work.
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
*   **Gate 2: "Unresolved Placeholders"**
    *   **IF** any written file still contains `{{PLACEHOLDER}}` markers:
    *   **BLOCK:** Task is not truly done — agent must replace all placeholders.
    *   **Note:** Template *structure* changes no longer block `task_done`. Only unfilled placeholders do.
*   **Gate 3: "Linter Errors"**
    *   **IF** `has_recent_linter_errors` is True:
    *   **BLOCK:** `🚨 TASK_DONE BLOCKED - LINTER ERRORS!`
    *   **Action:** Force agent to fix code.
*   **Gate 4: "Consecutive Calls"**
    *   **IF** agent calls `task_done` > 3 times in a row without doing work:
    *   **BLOCK:** "Stop calling task_done. Do the work."
*   **Success Path:**
    *   Marks task as `completed` in `TaskManager`.
    *   **IF** more tasks remain:
        *   Calculates next task index.
        *   Calls `switch_to_task_context(next_idx)`.
        *   Resets loop.
    *   **IF** all tasks done:
        *   **Executes `break` statement** to exit the main `while True` loop.
        *   Returns final summary string to Main Agent.

### `web_search` (Lines ~5950)
*   **Planning Mode:**
    *   Injects reminder: "Call `set_todos` NOW based on these results."
*   **Execution Mode:**
    *   Injects reminder: "Use these results to call `write_file`."

### `python_sandbox` (Lines ~5850)
*   **Execution:** Runs code in `vaf.tools.python_sandbox`.
*   **Context:** Returns output (stdout/result) to the LLM history.
*   **File-Write Guard:** Before execution, the submitted code is scanned for file-write patterns (`open(..., 'w')`, `.write(...)` on non-stdout handles, direct `base_dir` references). **IF** any are found, the call is **BLOCKED** with a message instructing the model to use `write_file` instead. This prevents the model from using the sandbox as a backdoor to write project files (e.g. creating `read_chunks.py` helper scripts).

### `_existing_note` Injection (in `create_fresh_context_for_task()`)
*   **Purpose:** Tells the model which files already exist in the project at context-switch time.
*   **Filter:** Infrastructure files are excluded from the list: hidden files (starting with `.`), `.git/`, `.vaf/`, `PARTIAL_*` backups, and named infra files (`.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`). Without this filter, the model reads `.gitignore` in a confused loop instead of writing code.
*   **Empty project branch:** If no code files exist, instead of an empty list the note says: *"The project directory is empty — call `write_file` to create the first file. Do NOT read `.gitignore` or any hidden files."* This gives the model an unambiguous directive to start writing immediately.

---

## 6. Cleanup & Exit
*   **Logic:**
    *   Stops TUI thread (`live.stop()`).
    *   Cleans up temporary handles.
    *   Returns the final string (list of created files + instructions) to the user.
