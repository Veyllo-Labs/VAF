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

### History Management: Tool-Call Content Stripping
After a successful `write_file` call, the agent walks backwards through `history` to find the corresponding `assistant` message. The `content` field inside the tool-call's JSON arguments is replaced with `[content omitted — N bytes written to disk]`. The rest of the tool-call (path, id) is preserved. This keeps the history size bounded regardless of file size.

### `_NoopLive` Class
A drop-in replacement for `rich.Live`. All methods (`start`, `stop`, `update`, `refresh`) are no-ops. Used as the `live` object when `CoderTUI` runs in `simple_mode`, so all call sites that reference `live.update()` / `live.stop()` need no per-call guards.

### `CoderTUI` Class (The Interface)
Implements a "Mini-IDE" using `rich.live`.
*   **`__init__(simple_mode=False)`**: Initializes state (`files`, `current_action`), locks (`RLock` for thread safety), and the `AnimatedHeader`.
    *   **`simple_mode=True`**: The Rich Live display is not started. `append_stream()` prints `[Coder] text` directly to stdout. `update_file(..., status="done")` prints `[Coder] ✅ Written: filename (N bytes)`. All other methods are silent no-ops. Active when `VAF_IN_WORKFLOW_TERMINAL=1`.
*   **`render()`**: The main draw loop. Constructs a `Layout` with:
    *   **Header:** Agent status.
    *   **Left Panel:** File tree (Icons show status: 📝 Writing, ✅ Done, ❌ Error).
    *   **Right Panel:** Live Token Stream (simulates typing) or Code Preview.
*   **`set_code_preview()`**: Updates the right panel to show syntax-highlighted code currently being written or diffed.
*   **`update_file()`**: Updates file status icons in the left panel safely from background threads.

---

## 2. `CodingAgentTool` Class Structure

### `_determine_base_dir(task, provided_path)` (The Smart Switch)
*   **Safety guard:** `is_unsafe_project_dir(path)` rejects the user's home directory itself, the standard user dirs (Documents, Desktop, ... — their subdirectories are fine), `~/.vaf` and the VAF program tree as work directories. Applied to every path source below; unsafe paths fall through to `_generate_project_directory`. Sub-agent terminals spawn with CWD=$HOME, where `~/.vaf` (and a stray `~/.git`) would otherwise make home look like a project root.
*   **Logic:** Decides whether to work in the current directory or create a new one.
    1.  **Explicit:** If `provided_path` is set (and safe) -> Use it.
    2.  **Edit Mode:** If CWD is a project root (`.git`, `.vaf`, etc.), safe, AND task is NOT "create new" -> **Use CWD**.
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
    4.  **User isolation:** Reads `user_scope_id` from the current session (via `get_current_session_id()` + `SessionManager.load()`). If a scope ID is found, the project root becomes `~/Documents/VAF_Projects/{uid[:8]}/`. Without a scope ID (local/admin mode) the root is `~/Documents/VAF_Projects/` as before.
    5.  **Per-chat isolation:** with a session id, each chat gets its own folder below the user root (`VAF_Projects/[uid]/[session_id]/<ProjectName>`), so projects from different chats never mix. The workflow engine builds its project paths the same way.
    6.  Constructs path: `{projects_root}/{Prefix} {Name}`.
    7.  **Duplicate Check:** If folder exists, appends timestamp `_{HHMMSS}`.

### `_ensure_git_repo(base_dir)`
*   **Logic:**
    1.  Refuses unsafe locations (`is_unsafe_project_dir`) — a `.git` in e.g. the home directory would make it look like a project root forever after.
    2.  Checks for `.git` folder.
    3.  If missing, runs `git init`.
    4.  Writes a default `.gitignore` (Python/Node/IDE patterns).
    5.  Runs `git add .` and `git commit -m "Initial commit"` to secure the starting state.

---

## 3. The `run()` Execution Flow (The Core)

This is the massive entry point method.

### A0. History/Rollback Delegation Fast Path
The coder owns each project's version history (built up by the final commit on every run, see section 6). The Main Agent has no git tools of its own for projects — it talks to the coder instead:

*   `coding_agent(task="history", project_path=...)` — the coder answers directly with the formatted version list (commit id, date, description, changed files).
*   `coding_agent(task="rollback auf <id>", project_path=...)` — the coder restores that version.

`_detect_history_rollback_intent()` (`vaf/tools/project_git.py`) classifies these tasks. Creation verbs always win ("Erstelle eine Seite über die History von Rom" runs the normal loop). A rollback request that names a concrete commit id matches REGARDLESS of task length — the main agent often wraps the delegation in long explanatory text, and routing that into the agentic loop made a small model plan "check git status" tasks instead of simply rolling back. History requests and rollbacks without an id stay conservative (max 200 chars). Matching tasks return immediately: no agentic loop, no terminal spawn, no LLM call. A rollback request without a version id returns the history plus the instruction to ask the user.

Rollback safety (`ProjectRollbackTool`): uncommitted work is committed as a backup first, then the target state is restored via `git revert` as a NEW commit — history is never rewritten and every rollback can itself be rolled back. Unsafe directories and non-git folders are refused.

Inside the agentic loop the same two tools are registered as base_dir-wrapped local tools (`project_history`, `project_rollback`), so the coder can also restore a known-good state at its own discretion after breaking something.

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
*   **TUI Start:** Checks `VAF_IN_WORKFLOW_TERMINAL`. If set, `CoderTUI` is created with `simple_mode=True` and `live = _NoopLive()`. Otherwise, a full `rich.Live` context is started at 12 FPS.
*   **API Mode Detection (`_is_api_mode`):**
    *   Checks if the active provider is an API backend (OpenAI, Anthropic, DeepSeek, OpenRouter, Google).
    *   **IF API mode:** Templates are **skipped entirely** — capable API models plan and write without scaffolding. The agent still calls `set_todos` itself.
    *   **IF local model:** Template selection logic runs as normal.
*   **Template Logic (local models only):**
    *   **Edit-mode guard:** templates are skipped entirely when `base_dir` already contains code files (html/css/js/py/...). `TemplateManager.generate_files()` writes into `base_dir` and would overwrite the user's work — a follow-up task whose text merely mentions "Website" must never replace a finished site with placeholder scaffolding. Existing projects always go through normal planning, where the fresh task context injects the existing file list for editing. Telemetry event: `template_skipped_existing_project`.
    *   Checks task keywords for template type ("website", "html", etc.) with an LLM-based fallback detector. HTML-specific keywords (`index.html`, `.html`, `html datei`) are included to prevent misclassification.
    *   If a matching template exists, copies files to `base_dir`.
    *   Sets `template_files` list for later reference (soft guidance, not enforcement).
*   **System Prompt Generation:**
    *   Generates the **Supervisor System Prompt**.
    *   **Crucial Instruction:** "Your FIRST action MUST be to call `set_todos`".
    *   **Hidden Tools:** Explicitly hides `task_done` from the prompt text to force planning.
    *   **Template language:** Framed as **guidance** ("recommended workflow", "good baseline") — not as hard rules. The agent is free to deviate from template structure if the task calls for it.
    *   **Task planning rules (injected into system prompt):**
        -   Single-file deliverable → exactly 1 task. Multi-file → one task per output file.
        -   No planning tasks (e.g. "Design the layout") — every task must produce at least one `write_file` call.
        -   No meta-files (PLAN.md, STRUCTURE.md, etc.) written to the project directory.

### C. Hierarchical Context Setup (Lines ~2200-2400)
*   **`ContextState` Class:** Defined locally to hold `ContextManager`, `history`, `phase`, and `files_created` for a specific scope.
*   **`context_states` Dict:** Stores the state for "main" and every "task_N".
*   **`switch_to_task_context(task_idx)` Helper:**
    *   Saves current state.
    *   If task state exists -> Resumes it.
    *   If new -> Creates **FRESH** `ContextManager` (8k/16k tokens) via `create_fresh_context_for_task()`.
    *   **Completed-Task Glue:** `_build_completed_info()` summarises previously finished tasks and injects them into the new system prompt (prevents "Context Amnesia" without polluting the window).
    *   **Existing-Files Injection:** `create_fresh_context_for_task()` scans `base_dir` and injects a file list into the task system prompt. Infrastructure entries are excluded: hidden files (`.`-prefix), `.git/`, `.vaf/`, `PARTIAL_*` backups, and named infra files (`.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`). When no code files exist, the note reads: *"The project directory is empty — call `write_file` to create the first file."*

### Critical: Context Switch + Tool Result Ordering

`switch_to_task_context()` calls `sync_legacy_vars()` which **reassigns the local `history` variable** to the new task context's history. This means any code that runs *after* the context switch and appends to `history` will write into the **new** task context, not the one where the tool was called.

**The bug this caused:** `set_todos` triggers a context switch to `task_0` (fresh history: `[system, user]`). The tool result for `set_todos` was then appended to the now-switched `history` → task_0 got an orphaned `role: tool` message with no preceding `assistant+tool_calls` → DeepSeek 400 on Loop 2.

**The fix:** At the start of the `for tc in tool_calls:` loop, `_history_at_dispatch = history` captures the reference *before* execution. The tool result is always appended to `_history_at_dispatch`, not `history`. This ensures the result lands in the context where the tool was invoked, regardless of any context switches during execution.

**Safety net:** `clean_history` building pre-computes `_valid_tool_call_ids` (all IDs present in `assistant.tool_calls` entries) and silently drops any `role: tool` message whose `tool_call_id` is not in that set. This prevents any residual orphaned messages from reaching the API.

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
    *   **Allowed:** `write_file`, `read_file`, `list_files`, `web_search`, `python_sandbox`, `run_tests`, `task_done`, `bash` (kernel-jailed; see 5.x), plus plug-and-play runtime tools.
    *   **Hidden:** `set_todos` (to prevent re-planning loops).

### E. LLM Interaction & Safety Nets
*   **Call:** `self.llm.chat_completion(...)`.
*   **Zombie Detection:**
    *   Tracks `idle_loop_count` (loops with text but no tool calls).
    *   **IF count > 3:** Injects `🛑 SYSTEM OVERRIDE: STOP THINKING. CALL A TOOL.` logic.
*   **Fake Completion:**
    *   Scans text for "I am done", "Finished".
    *   **IF** text says "done" **AND** no `task_done` tool call:
    *   **ACTION:** Injects `You claimed completion but didn't call task_done. Call it now.`

### F. Stuck Detection with Goal Verification and Retry Stages
A task that stays on the same index for more than 15 loops is never blindly marked completed. The flow is:

1.  **Goal verification** via `_verify_task_goal(task_title, task_files, base_dir, linter_active, llm_verify)`:
    *   Deterministic first: if the task wrote files (`task_file_map[idx]`), they must exist, contain no template placeholders and no linter error may be active.
    *   Without file evidence (the goal may already be implemented by an earlier task), one bounded LLM check runs (non-streaming, temperature 0, 1000 tokens, 90s timeout): "Is this goal already fully implemented? YES/NO plus one line of evidence" against the main deliverable (`_pick_main_deliverable`). Reasoning models may spend their whole budget thinking and leave `content` empty — the call falls back to `reasoning_content`, and the verdict parser takes the LAST standalone YES/NO in the text (a chain of thought ends with its conclusion). Any error or ambiguity counts as NOT verified.
2.  **Verified:** task completes with result "Auto-completed after stuck detection - goal verified: ...".
3.  **Not verified, retry budget free:** one immediate retry — the task resets to `pending`, the poisoned task context is dropped (`context_states.pop`), a fresh context is created and a system hint describes the failed attempt. The loop budget restarts.
4.  **Retry exhausted:** the task is marked **failed** (`TaskManager.fail_current_task`) with the reason. The run continues with the remaining tasks.
5.  **Final retry round:** at every all-done exit point, `_maybe_start_final_retry()` runs once per run: failed tasks are reset to pending and re-attempted with enriched context (completed-task summaries, project file list, failure history). Tasks failing again stay failed.

`TaskManager.is_all_done()` uses terminal semantics (completed, failed or skipped) so failed tasks cannot keep the loop alive; `is_all_completed()` distinguishes the strict success case. The final summary reports failed tasks explicitly and signals `[VAF_CODING_AGENT_STATUS: PARTIAL]` — a stuck task never produces a silent fake COMPLETE.

The inactivity auto-complete (idle with files present) runs the same deterministic verification before completing; unverifiable tasks stay open and escalate into the stuck flow above.

---

## 5. Tool Implementation Logic (The Big IF/ELIF Block)

### `set_todos`
*   **Single-File Rule (code-enforced):** `_detect_single_file_deliverable(task)` checks the original task for explicit single-file phrasings (German and English, e.g. "einzelne HTML-Datei", "single html file", "everything in one file"). If the model submits more than one task for a single-file deliverable:
    *   First violation: **REJECT** with the instruction to submit exactly one task.
    *   Second violation: **AUTO-COERCE** — the supervisor replaces the plan with exactly one task derived from the original task text. No planning loop is possible.
    *   The auto-generated TODO path applies the same rule (exactly one auto task for single-file deliverables).
*   **Validation:** Checks if `tasks` list is valid.
*   **Phase Check:**
    *   **IF** called during execution phase: **BLOCK** ("Cannot modify TODOs during execution").
    *   **ELSE:** Parses tasks into `TaskManager`.
*   **Context Switch:** Immediately switches context to `task_0`.

### `write_file` (Lines ~5900)
*   **Pre-Check:**
    *   **IF** no TODOs set: **BLOCK** ("Call set_todos first").
*   **Meta-file Guard (phase-aware):** `_meta_file_block_reason(path, phase)`. Scratch/planning files (`plan.md`, `structure.md`, `notes.md`, `todo.md`, `design.md`, `layout.md`, `read_chunks.py`) are **always** blocked, in every phase. `README.md` is **doc-gated**: blocked during planning/build (`main`/`task_N`) but written by the dedicated DOCUMENT phase (see section 6a). Arbitrary docs (`docs/api.md`, `article.md`) are normal deliverables and are not gated. Returns a blocked error to the LLM.
*   **Template Validation (soft guidance only):**
    *   **IF** target file is a template file:
        *   Reads original file.
        *   Checks for presence of key structural tags (`<nav>`, `id="hero"`, `def main`, etc.).
        *   **IF elements are missing in new content:** Logs a **warning** to the TUI stream. Write is allowed to proceed.
        *   **IF structure is preserved:** Logs a confirmation note.
    *   Placeholder check (`{{PLACEHOLDER}}` still present) **blocks** `task_done`, not `write_file`.
*   **Diff Generation:**
    *   Calculates diff between old and new content.
    *   Updates TUI Code Preview.
*   **Execution:** Calls `filesystem.write_file`.
*   **History Content Strip:** After a successful write, the `content` argument in the matching assistant tool-call history entry is replaced with `[content omitted — N bytes written to disk]`.
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
    *   Template *structure* changes do not block `task_done`. Only unfilled placeholders do.
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
*   **File-Write Guard:** Before execution, the submitted code is scanned for file-write patterns: `open(..., 'w'/'a')`, `.write(...)` on non-stdout/stderr/StringIO handles, and direct references to `base_dir`. If any pattern matches, the call is **BLOCKED** and the LLM is instructed to use `write_file` instead.

### `bash` — kernel-jailed workspace shell (`vaf.tools.bash` → `vaf.tools.workspace_exec`)
*   **Purpose:** The coder needs a real shell for its project (run scripts, `npm`/`pip install`, run the app), but must never be able to touch VAF's own source or itself and break the running system.
*   **Registration:** `BashTool(base_dir)` is bound to the coder's workspace at registration (like the git tools), so the shell defaults to the project and confinement is scoped to exactly that directory. With no workspace bound it **refuses** rather than fall back to the process cwd.
*   **Confinement (kernel, not string-filtering):** `run_in_workspace` runs the command inside a **bubblewrap** jail on Linux — the workspace is bind-mounted read-write (edits persist); the system (`/usr`, `/bin`, `/etc`, ...) is read-only; the VAF repo, `~/.vaf`, secrets and the docker socket are **not mounted** (they do not exist for the command); env is `--clearenv`'d (tray API keys never leak) and the network is `--unshare-net`'d (host loopback services like the memory DB are unreachable). Without bubblewrap it falls back to a container with only the workspace mounted and `--network none`; with neither it **refuses** (never a raw host shell).
*   **Docker is refused:** the host docker socket is host-root-equivalent and cannot be safely policed by inspecting the command string, so `bash` refuses any `docker` invocation up front. Host/docker tasks are the *main agent's* `host_bash` (below), under explicit confirmation.
*   **Blocklist:** a cheap `is_command_safe` blocklist (fork bomb, `rm -rf /`, `mkfs`, `curl|bash`, ...) is defense-in-depth; the real safety is the jail.

### `run_tests` (`vaf.tools.sandbox_test_runner`)
*   **Purpose:** Give the coder a sanctioned way to actually run its project's tests and get the **real** pass/fail, instead of guessing "tests pass".
*   **Execution:** Copies the project (tar-pipe) into a fresh `/workspace/testrun_...` dir in the `vaf-sandbox` container, runs `python3 -m pytest -q` under an in-container `timeout -s KILL`, streams the summary back, and cleans up the run dir in a `finally`.
*   **History budget:** `run_tests` output shares `read_file`'s larger char limit so the pytest summary is not truncated out of the LLM history.

---

## 5a. Deterministic guardrail phases (ORIENT → PLAN → BUILD → DOCUMENT)

Two always-run, deterministic phases wrap the planning/execution loop. They are guardrails (like the guided/template rails): fixed stages that lead even a weak model, rather than prompt hints it can ignore. Both are gated `not skip_template` (skipped in CONTENT_ONLY).

*   **ORIENT (before planning) — `_build_orientation_summary(base_dir)`:** a bounded, pure-Python project scan (no LLM). It lists the existing file inventory (in-place-pruned `os.walk`, depth/file caps) and the heads of existing docs, then injects that summary into the planner's `system_prompt` via the `orientation_summary` variable in the `<context>` block. This fixes the previously **dead** `existing_files_info` (the planner used to be blind to an existing project, causing zero-change doom-loops on edit tasks). A fresh/empty project yields a short no-op notice. Deterministic by construction: the inventory is baked into the planner's first request, and the scan cannot loop.
*   **DOCUMENT (after the task loop, before `_final_commit`) — `self._run_document_phase(...)`:** creates or updates the README to reflect this run's real changes.
    *   **Change detection:** `_detect_run_changes(base_dir, run_start_sha)` diffs the working tree against `_run_start_sha` (HEAD captured right after `_ensure_git_repo`; the git-empty-tree when there is no baseline) plus untracked files — `git diff --diff-filter=ACMR` + `git ls-files --others`. If only docs changed, it is a no-op.
    *   **Single-shot, not a loop:** the model is asked **once** (`self.query_llm`) for the README content; Python then writes it. The model has **no tools** in this phase, so it cannot derail or touch source.
    *   **Positive allowlist:** writes only a top-level README (exact `readme` stem + doc extension via `_is_readme_name`) or `docs/**`; the target is symlink- and containment-checked (`_doc_target_is_safe`) so the write can never follow a link out of the project.
    *   **Non-destructive:** create-mode without an LLM answer writes a minimal deterministic README; update-mode never overwrites a good README with a stub or a materially shorter/truncated regeneration (kept-existing guard). Leaked `<think>` reasoning and wrapping code fences are stripped only when unambiguous.
    *   The written doc lands in the same `_final_commit`. The whole phase is exception-isolated so a failure never skips that commit.

---

## 6. Cleanup & Exit
*   **Final Commit (every exit path):** before the final summary is built, `_final_commit(base_dir, message)` runs `git add -A` and commits when changes exist. The commit message is `VAF Coder: <task excerpt>` plus a status line (`Status: COMPLETE|PARTIAL (n/m tasks)`); runs with failed or remaining tasks commit too, so no work is ever left untracked. If no git identity is configured, the commit retries once with a one-off VAF identity (`-c user.name=... -c user.email=...`, the user's git config is never modified). Unsafe directories and CONTENT_ONLY temp dirs are excluded. The result line appears in the final summary. These commits are what powers the history/rollback delegation (section 3.A0).
*   **Logic:**
    *   Stops TUI thread (`live.stop()`).
    *   Cleans up temporary handles.
    *   Returns the final string (list of created files + instructions + task status incl. failed tasks) to the user.

## 7. WebUI Live Feed (VS-Code SubAgent Window)

During a run the coder feeds the WebUI's VS-Code style SubAgent window through two emit closures in `run()`:

*   **`_emit_coder_state()`** — full project state: file tree (`_build_file_tree`, per-file status W/A/M), git state (`_build_git_state`: branch, dirty count, recent commits), the REAL task list from the TaskManager with live per-task status, loop count, task progress and linter flag. Sent at run start, every loop iteration, after each `write_file`, on task completion/failure and after the final commit — hash-throttled, so unchanged states are not resent. Event type: `coder_state`.
*   **`_emit_live_code()`** — the partial file content while the model is still streaming a `write_file` call (hooked into the same stream parser that drives the terminal code preview). Sent as a minimal `subagent_update` with only `file` + `code`; throttled to one post per 0.35s, tail-capped at 6 KB, plus one unthrottled full-content post when `write_file` dispatches. This drives the live-typing editor pane.

Both resolve the session id from `VAF_SESSION_ID` / the IPC context and stay silent without one (plain CLI runs emit nothing). Transport is `emit_coder_state()` / `emit_coder_code()` in `vaf/core/web_interface.py` (subprocess bridge or direct WebSocket push). See `docs/web-ui/WEBUI_WEBSOCKET_FLOW.md` for the payloads and the frontend rendering.

## 8. Telemetry (logs/debug)
Loop-level telemetry (`loop_start`, `tool_start`, `coder_debug`, `task_stuck_verification`, `final_commit`, ...) persists as `logs/debug/<agent_type>/<run_id>/events.jsonl` in **every** run mode. `get_subagent_logger_from_env(create_fallback=True, agent_type=...)` no longer depends on the IPC spawn path: without a `VAF_TASK_ID` a local run id (`local-<timestamp>-<pid>`) is generated. The `vaf subagent run` CLI sets this id in its own (single-task) process environment so the runner and the hosted tool log into one directory. Run directories older than 14 days are swept best-effort on logger startup.
