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
After a successful `write_file` call, the agent walks backwards through `history` to find the corresponding `assistant` message. The `content` field inside the tool-call's JSON arguments is replaced with `[content omitted - N bytes written to disk]`. The rest of the tool-call (path, id) is preserved. This keeps the history size bounded regardless of file size.

### Who owns the screen (`UI.live`)
The coder never builds a `rich.Live` itself. It asks `vaf.cli.tui.UI.live(...)`, which returns a real `Live` when the terminal is free and a silent `_NoopLive` (all methods no-ops, so call sites need no per-call guards) when a full-screen app owns it. The same factory serves the librarian and the research agent - each of the three used to decide this alone and each got it wrong differently, the researcher's `isatty()` check most instructively: it is True under a full-screen app, so the guard passed and the screen was overwritten anyway. Guarded by `tests/test_live_display_ownership.py`, which greps for direct `Live(` construction because `research_agent` imports it function-locally.

### Narration in simple mode (`CoderTUI._say`)
Simple mode replaces the panel with plain lines. `_say` is the only way they leave: with the screen free it prints `[Coder] ...` to stdout; with a full-screen app up it goes through `UI.event`, which the app already subscribes to, so the line lands in the transcript rather than on top of the app's rendering.

### `CoderTUI` Class (The Interface)
Implements a "Mini-IDE" using `rich.live`.
*   **`__init__(simple_mode=False)`**: Initializes state (`files`, `current_action`), locks (`RLock` for thread safety), and the `AnimatedHeader`.
    *   **`simple_mode=True`**: No panel. `append_stream()` and `update_file(..., status="done")` narrate through `_say()` (stdout, or the app's event channel - see above). All other methods are silent no-ops. Active when `VAF_IN_WORKFLOW_TERMINAL=1` or while a full-screen app owns the terminal.
*   **`render()`**: The main draw loop. Constructs a `Layout` with:
    *   **Header:** Agent status.
    *   **Left Panel:** File tree (Icons show status: 📝 Writing, ✅ Done, ❌ Error).
    *   **Right Panel:** Live Token Stream (simulates typing) or Code Preview.
*   **`set_code_preview()`**: Updates the right panel to show syntax-highlighted code currently being written or diffed.
*   **`update_file()`**: Updates file status icons in the left panel safely from background threads.

---

## 2. `CodingAgentTool` Class Structure

### Explicit-path resolution in `run()` (before `_determine_base_dir`)

`run()` resolves the project directory in this order (normal mode):

1.  **`project_path` kwarg** - expanded/absolutized first. If it names a FILE (see the
    file-vs-directory rule below), it is split into directory + target-file hint and the
    directory becomes the candidate. Unsafe candidates are ignored (fall through to 2-4).
2.  **Explicit path in the task text** - `_extract_explicit_task_path()` matches phrase forms
    ("im Verzeichnis /x", "in directory /x", "path: /x") and bare absolute paths
    (`/home|/tmp|/mnt|/root/...` or Windows drive paths). Dots are part of the match (filenames
    keep their extension) and quotes end it. The match is split file-vs-directory the same way;
    the unsafe guard judges the DIRECTORY part, so a file directly in `$HOME` falls back to
    `VAF_Projects` while the filename hint survives.
3.  **Session's `last_project_path`** (`_get_session_project_path`) when the task looks like an
    edit request.
4.  **`_determine_base_dir`** (below) as the fallback.

**File-vs-directory rule (`_looks_like_file_path` / `_split_explicit_path`):** an existing
filesystem entry decides directly (a directory named like a file stays a directory - the
continue-project case). A path that does not exist yet counts as a file only when its basename
has a known file extension (`_FILE_TARGET_EXTENSIONS`, curated: `project.v2` stays a directory).
A file target is split into `(dirname, basename)`; the basename becomes the **target-file hint**,
injected as a `target_file:` line into the planner and per-task system prompts so the model
writes exactly that file. This prevents two real incidents: `os.makedirs` on an existing file
crashed the run, and a nonexistent file path became a DIRECTORY named like the file with the
deliverable nested inside. `os.makedirs` failures (`FileExistsError` / `NotADirectoryError`)
return an actionable error string instead of a traceback.

### `_determine_base_dir(task, provided_path)` (The Smart Switch)
*   **Safety guard:** `is_unsafe_project_dir(path)` rejects the user's home directory itself, the standard user dirs (Documents, Desktop, ... - their subdirectories are fine), `~/.vaf` and the VAF program tree as work directories. Applied to every path source below; unsafe paths fall through to `_generate_project_directory`. Sub-agent terminals historically spawned with CWD=$HOME on Linux/macOS (terminal emulators do not inherit the spawner's cwd), where `~/.vaf` (and a stray `~/.git`) would otherwise make home look like a project root - the guard stays for exactly that shape. Since the VAF_PARENT_CWD handoff (`Platform.open_new_terminal` stamps the caller's cwd, the sub-agent entry adopts it via `Platform.adopt_parent_cwd()`), a coder spawned from a project checkout actually STARTS in that checkout, so the Edit-Mode branch below fires for the directory the user launched `vaf run` in.
*   **Logic:** Decides whether to work in the current directory or create a new one.
    1.  **Explicit:** If `provided_path` is set (and safe) -> Use it.
    2.  **Edit Mode:** If CWD is a project root (`.git`, `.vaf`, etc.), safe, AND task is NOT "create new" -> **Use CWD**.
    3.  **Scaffold Mode:** If user intent is "create new", "scaffold" -> Call `_generate_project_directory`.
    4.  **Fallback:** If unsure, default to creating a safe sandbox in `VAF_Projects`.

### `_generate_project_directory(task)`
*   **Role:** Helper for Scaffold Mode (creates new folders).
*   **Logic:**
    1.  Scans `task` string for keywords to choose the folder prefix:
        -   **"Webseite"** prefix (the folder prefix the code emits): `website`, `webseite`, `homepage`, `landing page`, `.html`, `index.html`, `html datei`, `html file` - HTML keywords are checked **first** to avoid false matches (e.g. a `<script>` tag in an HTML task description must not be classified as a Script project).
        -   **"Script"** prefix: only narrow matches like `python script`, `bash script`, `.py script` - bare `script` is intentionally excluded.
        -   **"App"**, **"Tool"**, **"API"** etc. for other common types.
    2.  Extracts semantic keywords (removing stop words like "the", "create").
    3.  Sanitizes the name using Regex to be OS-safe (removes `/ \ : * ?`).
    4.  **User isolation:** Reads `user_scope_id` from the current session (via `get_current_session_id()` + `SessionManager.load()`). If a scope ID is found, the project root becomes `~/Documents/VAF_Projects/{uid[:8]}/`. Without a scope ID (local/admin mode) the root is `~/Documents/VAF_Projects/` as before.
    5.  **Per-chat isolation:** with a session id, each chat gets its own folder below the user root (`VAF_Projects/[uid]/[session_id]/<ProjectName>`), so projects from different chats never mix. The workflow engine builds its project paths the same way.
    6.  Constructs path: `{projects_root}/{Prefix} {Name}`.
    7.  **Duplicate Check:** If folder exists, appends timestamp `_{HHMMSS}`.

### `_ensure_git_repo(base_dir)`
*   **Logic:**
    1.  Refuses unsafe locations (`is_unsafe_project_dir`) - a `.git` in e.g. the home directory would make it look like a project root forever after. Also refuses any `base_dir` that is not an existing directory (a file path would raise `NotADirectoryError` inside subprocess, which the git error handling does not catch).
    2.  Checks for `.git` folder.
    3.  If missing, runs `git init`.
    4.  Writes a default `.gitignore` (Python/Node/IDE patterns).
    5.  Runs `git add .` and `git commit -m "Initial commit"` to secure the starting state.

---

## 3. The `run()` Execution Flow (The Core)

This is the massive entry point method.

### A0. History/Rollback Delegation Fast Path
The coder owns each project's version history (built up by the final commit on every run, see section 6). The Main Agent has no git tools of its own for projects - it talks to the coder instead:

*   `coding_agent(task="history", project_path=...)` - the coder answers directly with the formatted version list (commit id, date, description, changed files).
*   `coding_agent(task="rollback to <id>", project_path=...)` - the coder restores that version.

`_detect_history_rollback_intent()` (`vaf/tools/project_git.py`) classifies these tasks. Creation verbs always win ("Create a page about the history of Rome" runs the normal loop). A rollback request that names a concrete commit id matches REGARDLESS of task length - the main agent often wraps the delegation in long explanatory text, so matching regardless of length keeps a wrapped rollback request routed straight to the rollback path rather than into the agentic loop. History requests and rollbacks without an id stay conservative (max 200 chars). Matching tasks return immediately: no agentic loop, no terminal spawn, no LLM call. A rollback request without a version id returns the history plus the instruction to ask the user.

Rollback safety (`ProjectRollbackTool`): uncommitted work is committed as a backup first, then the target state is restored via `git revert` as a NEW commit - history is never rewritten and every rollback can itself be rolled back. Unsafe directories and non-git folders are refused.

Inside the agentic loop the same two tools are registered as base_dir-wrapped local tools (`project_history`, `project_rollback`), so the coder can also restore a known-good state at its own discretion after breaking something.

### A. Process Isolation & IPC (Lines ~1600-1700)
*   **Check:** Is `VAF_IN_SUBAGENT_TERMINAL` env var set?
*   **Caller identity crosses the boundary as data (2026-08-01).** `CodingAgentTool`
    declares `identity_kwargs = ("user_scope_id", "user_role")`, the dispatcher assigns
    them, and the spawn env carries them as `VAF_USER_SCOPE_ID` / `VAF_USER_ROLE` - the
    librarian's proven pattern. In the child, `_caller_identity()` resolves kwargs-or-env
    once at the top of `run()`, and `_assign_caller_identity()` hands the pair to every
    inner tool that declares it (seven of eight do) at the dispatch site - by ASSIGNMENT,
    never `setdefault`, so a model-written `user_role: "admin"` is overwritten rather than
    honoured. Before this, the child ran every inner tool as
    `compute_user_jail(None, None)` = the machine owner, for every caller.
    **`bash` is the named exception**, deliberately: it declares nothing and stays at
    full strength - a shell confined to a per-user jail is not a shell. The containment for
    a user who should not have that power is the per-user tool permission, now ENFORCED:
    the funnel refuses blocked tools per turn, and the account allowlist crosses into this
    child as `VAF_ALLOWED_TOOLS` (names only) - blocked tools are filtered out of the
    schema the model sees, with a dispatch-side refusal as backstop. On the parent side
    the list comes from the registered account-allowlist resolver
    (`set_account_allowlist_resolver` in `vaf/core/tool_dispatch.py` - the same registry
    the funnel consults, so the coder cannot disagree with the chat lane; the harness
    registers its resolver in `vaf/main.py`). `bash` and the git tools are offered to the
    admin's picker via `GET /api/users/tool-universe`.
*   **IF NOT (Main Process):**
    *   Check `Config.sub_agents_in_separate_terminals`.
    *   **Spawning:** Uses `sys.executable` to spawn a NEW process via `python -m vaf.main subagent run coding_agent`.
    *   **IPC:** Creates a task in `subagent_ipc` and passes the ID.
    *   **Return:** Returns a placeholder string `[SUBAGENT_ASYNC:...]` to the main agent.
*   **IF YES (Sub-Agent Process):**
    *   Proceeds to execute the logic below.

### B. Initialization (Lines ~1700-2200)
*   **TUI Start:** Two independent questions. `simple_mode` (plain lines instead of a panel) is on when `VAF_IN_WORKFLOW_TERMINAL` is set OR a full-screen app owns the terminal. Whether a real `rich.Live` runs at all is decided by `UI.live()`, not here - it yields a no-op stand-in while the screen belongs to someone else, and a 12 FPS `Live` otherwise.
*   **API Mode Detection (`_is_api_mode`):**
    *   The coder talks plain OpenAI wire format over raw HTTP, so it resolves its endpoint from `coder_api_providers()` (module level), which is built from the central provider registry (`vaf/core/provider_registry.py`) and therefore covers every API provider by construction - OpenAI, Anthropic (OpenAI-compat URL), DeepSeek, OpenRouter, Google (OpenAI-compat URL), Veyllo (base URL from config `veyllo_base_url`, resolved at call time).
    *   **Sync guard:** the map MUST cover every provider in `config.PROVIDER_MODELS` - enforced by `tests/test_coder_provider_map.py`. An API provider missing from the map returns a clear "coder configuration error" instead of falling through to the local branch, which would otherwise route an API provider's work to a local model.
    *   **IF API mode:** Templates are **skipped entirely** - capable API models plan and write without scaffolding. The agent still calls `set_todos` itself.
    *   **IF local model (`provider == "local"` only):** Template selection logic runs as normal; the `:8080` health check applies only here.
*   **Template Logic (local models only):**
    *   **Edit-mode guard:** templates are skipped entirely when `base_dir` already contains code files (html/css/js/py/...). `TemplateManager.generate_files()` writes into `base_dir` and would overwrite the user's work - a follow-up task whose text merely mentions "Website" must never replace a finished site with placeholder scaffolding. Existing projects always go through normal planning, where the fresh task context injects the existing file list for editing. Telemetry event: `template_skipped_existing_project`.
    *   Checks task keywords for template type ("website", "html", etc.) with an LLM-based fallback detector. HTML-specific keywords (`index.html`, `.html`, `html datei`) are included to prevent misclassification.
    *   If a matching template exists, copies files to `base_dir`.
    *   Sets `template_files` list for later reference (soft guidance, not enforcement).
*   **System Prompt Generation:**
    *   Generates the **Supervisor System Prompt**.
    *   **Crucial Instruction:** "Your FIRST action MUST be to call `set_todos`".
    *   **Hidden Tools:** Explicitly hides `task_done` from the prompt text to force planning.
    *   **Template language:** Framed as **guidance** ("recommended workflow", "good baseline") - not as hard rules. The agent is free to deviate from template structure if the task calls for it.
    *   **Task planning rules (injected into system prompt):**
        -   Single-file deliverable → exactly 1 task. Multi-file → one task per output file.
        -   No planning tasks (e.g. "Design the layout") - every task must produce at least one `write_file` call.
        -   No meta-files (PLAN.md, STRUCTURE.md, etc.) written to the project directory.

### C. Hierarchical Context Setup (Lines ~2200-2400)
*   **`ContextState` Class:** Defined locally to hold `ContextManager`, `history`, `phase`, and `files_created` for a specific scope.
*   **`context_states` Dict:** Stores the state for "main" and every "task_N".
*   **`switch_to_task_context(task_idx)` Helper:**
    *   Saves current state.
    *   If task state exists -> Resumes it.
    *   If new -> Creates **FRESH** `ContextManager` (8k/16k tokens) via `create_fresh_context_for_task()`.
    *   **Completed-Task Glue:** `_build_completed_info()` summarises previously finished tasks and injects them into the new system prompt (prevents "Context Amnesia" without polluting the window).
    *   **Existing-Files Injection:** `create_fresh_context_for_task()` scans `base_dir` and injects a file list into the task system prompt. Infrastructure entries are excluded: hidden files (`.`-prefix), `.git/`, `.vaf/`, `PARTIAL_*` backups, and named infra files (`.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`). When no code files exist, the note reads: *"The project directory is empty - call `write_file` to create the first file."*

### Critical: Context Switch + Tool Result Ordering

`switch_to_task_context()` calls `sync_legacy_vars()` which **reassigns the local `history` variable** to the new task context's history. This means any code that runs *after* the context switch and appends to `history` will write into the **new** task context, not the one where the tool was called.

**Why this matters:** `set_todos` triggers a context switch to `task_0` (fresh history: `[system, user]`). If the tool result for `set_todos` were appended to the now-switched `history`, task_0 would get an orphaned `role: tool` message with no preceding `assistant+tool_calls`, which strict providers (e.g. DeepSeek) reject with a `400`.

**How the code prevents it:** At the start of the `for tc in tool_calls:` loop, `_history_at_dispatch = history` captures the reference *before* execution. The tool result is always appended to `_history_at_dispatch`, not `history`. This ensures the result lands in the context where the tool was invoked, regardless of any context switches during execution.

**Safety net (orphans + dangling):** `clean_history` building pre-computes `_valid_tool_call_ids` (all IDs present in `assistant.tool_calls` entries) and silently drops any `role: tool` message whose `tool_call_id` is not in that set, and strips `tool_calls` that never got a response. This prevents residual orphaned/dangling messages from reaching the API.

**Safety net (ordering):** the pass above does not fix *order*. Order can break when a status nudge (the idle-progress message) is appended into `history` mid-tool-loop, wedging a `role: system` message *between* an `assistant + tool_calls` and its `role: tool` results. Strict providers (DeepSeek, OpenAI) reject this with `400 "insufficient tool messages following tool_calls"`. `_normalize_tool_adjacency()` (`vaf/tools/coder.py`) runs on `clean_history` before every send and guarantees the contract: each `assistant + tool_calls` is immediately followed by exactly one `role: tool` per `tool_call_id`, in order, relocating any wedged system/user message to *after* the tool block. It pairs responses via FIFO queues, so a provider's positional ids (`call_0`, `call_1`) that recur across turns bind to the assistant that owns them. It is idempotent and provider-agnostic (harmless on providers that were already lenient). The mid-loop nudge sites (the idle-progress nudge and the final-retry `task_done` branches) also answer the pending `tool_call` before they `continue`. Regression cover: `tests/test_tool_adjacency_normalizer.py`.

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
    *   **Allowed:** `write_file`, `edit_file`, `read_file`, `list_files`, `web_search`, `python_sandbox`, `run_tests`, `render_check`, `browser_agent`, `git_log`, `project_history`, `project_rollback`, `task_done`, plus plug-and-play runtime tools. `render_check` and `browser_agent` are the two halves of the visual verify loop: one look (errors, console, rendered text) versus driving the page (click, fill forms, walk a flow) - `render_check`'s own description defers anything interactive to `browser_agent`, which is why the task branch carries its own `browser_agent` schema entry instead of relying on the main-context plug-and-play copy. `edit_file` (surgical search/replace) is preferred over `write_file` for changing an existing file; `git_log`/`project_history`/`project_rollback` run against the real project repo (not the `run_tests` sandbox), so a task step can find a known-good version and restore it.
    *   **Planning/main context only:** `bash` (kernel-jailed workspace shell; see 5.x), `git_init`, `git_add_commit`, `git_status`, `web_fetch`.
    *   **Hidden:** `set_todos` (to prevent re-planning loops).

#### The coder tool allow-list (whitelist)

Above the per-phase rules sits one question the phases do not answer: **which tools does
a coding agent work with at all?** That is a whitelist,
[`CODER_ALLOWED_TOOLS`](../../vaf/core/coder_tools.py) - files, code, git, shell, tests
and lookups - and it is what the plug-and-play discovery is filtered against.

It replaced a blacklist, and the reason is measurable rather than aesthetic. Discovery
walks `vaf/tools/`, instantiates every `BaseTool` subclass it finds and excluded exactly
three by name, so every tool the product gained anywhere landed in the coder's request.
A captured live request carried **130 tools**: 11 for mail, 20 for messengers, 9 for
calendars and contacts. OpenAI refuses more than **128** functions per request, so that
request was answered with `Invalid 'tools': array too long. Expected an array with
maximum length 128, but got an array with length 130 instead.` on the FIRST loop of every
OpenAI run. Veyllo, DeepSeek and the local server enforce no such limit, which is why it
stayed invisible until the provider changed. The same request after the whitelist carries
42 tools.

**Where it is applied is load-bearing.** `_apply_tool_allowlists()` runs at the ONE
chokepoint where the finished list exists: after the ~24 tools the loop appends by hand
and after `tools_schema.extend(plug_and_play_tools)`. Filtering the context schema alone
leaves both allow-lists out of everything appended afterwards - the first attempt at this
change did exactly that and the captured request was still 130 tools. The same chokepoint
is what now also applies the **account** allow-list (`caller_allowed`) to the appended
tools; before, an account-forbidden tool still reached the model's schema and was only
stopped by the dispatch-side refusal, which reads as a hallucinated call rather than as a
tool that should never have been offered.

Configurable and admin-only: `coder_tool_allowlist` replaces the set,
`coder_tool_allowlist_extra` adds to it (see
[CONFIG_SCHEMA.md](../setup/CONFIG_SCHEMA.md)). `CODER_REQUIRED_TOOLS` is unioned back in
regardless, so a typo in an override costs optional tools instead of producing a coder
that cannot call `set_todos` or write a file. Pinned by
`tests/test_coder_tool_allowlist.py`, including the ordering above and a guard that every
tool the coder advertises is one the list actually permits.

### E. LLM Interaction & Safety Nets
*   **Call:** `self.llm.chat_completion(...)`.
*   **429 (rate limit):** the provider names its own wait (`Retry-After` header or "try
    again in 186ms" in the body); the coder parses it with the shared
    `api_backend.rate_limit_wait_seconds()`, sleeps, and re-enters the loop, bounded by the
    same wall-clock budget as the SDK lane (`api_rate_limit_wait_max`). Past the budget the
    run ends with the provider's own message (`rate_limit_exhausted` in `events.jsonl`); a
    successful request resets the budget. Before this branch existed, one 429 killed the
    whole run via the generic non-200 return.
*   **400 (context):** a 400 whose text mentions context/length/tokens triggers the
    compression ladder (3 levels, down to system prompt plus one sentence), capped by
    `_MAX_COMPRESSION_LEVELS`: a 400 that survives the last level was never a context error,
    so the run stops and reports the provider's message instead of re-sending it forever (a
    live run re-sent an identical 75-character request 64 times). The word test over-matches
    deliberately survivable-ly: e.g. OpenAI's tools-array-too-long 400 contains "length" and
    "maximum". The response body is logged as `llm_request_failed` in `events.jsonl` - the
    status alone answers nothing.
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
    *   Without file evidence (the goal may already be implemented by an earlier task), one bounded LLM check runs (non-streaming, temperature 0, 1000 tokens, 90s timeout): "Is this goal already fully implemented? YES/NO plus one line of evidence" against the main deliverable (`_pick_main_deliverable`). Reasoning models may spend their whole budget thinking and leave `content` empty - the call falls back to `reasoning_content`, and the verdict parser takes the LAST standalone YES/NO in the text (a chain of thought ends with its conclusion). Any error or ambiguity counts as NOT verified.
2.  **Verified:** task completes with result "Auto-completed after stuck detection - goal verified: ...".
3.  **Not verified, retry budget free:** one immediate retry - the task resets to `pending`, the failed task context is dropped (`context_states.pop`), a fresh context is created and a system hint describes the failed attempt. The loop budget restarts.
4.  **Retry exhausted:** the task is marked **failed** (`TaskManager.fail_current_task`) with the reason. The run continues with the remaining tasks.
5.  **Final retry round:** at every all-done exit point, `_maybe_start_final_retry()` runs once per run: failed tasks are reset to pending and re-attempted with enriched context (completed-task summaries, project file list, failure history). Tasks failing again stay failed.

`TaskManager.is_all_done()` uses terminal semantics (completed, failed or skipped) so failed tasks cannot keep the loop alive; `is_all_completed()` distinguishes the strict success case. The final summary reports failed tasks explicitly and signals `[VAF_CODING_AGENT_STATUS: PARTIAL]` - a stuck task never produces a silent fake COMPLETE.

The inactivity auto-complete (idle with files present) runs the same deterministic verification before completing; unverifiable tasks stay open and escalate into the stuck flow above.

---

## 5. Tool Implementation Logic (The Big IF/ELIF Block)

### `set_todos`
*   **Single-File Rule (code-enforced):** `_detect_single_file_deliverable(task)` checks the original task for explicit single-file phrasings (German and English, e.g. "einzelne HTML-Datei", "single html file", "everything in one file"). If the model submits more than one task for a single-file deliverable:
    *   First violation: **REJECT** with the instruction to submit exactly one task.
    *   Second violation: **AUTO-COERCE** - the supervisor replaces the plan with exactly one task derived from the original task text. No planning loop is possible.
    *   The auto-generated TODO path applies the same rule (exactly one auto task for single-file deliverables).
*   **Title normalization (data-model invariant):** todo items must be plain strings, but models (esp. DeepSeek) sometimes send dicts like `{"text": "...", "status": "pending"}`. Titles are coerced to text at the data-model boundary - `coerce_task_title()` + `Task.__post_init__` (`vaf/core/persistence.py`) - so BOTH a fresh `set_todos` call and loading/resuming a previously-persisted plan (`ProjectState.from_dict`) get a string title. A raw dict otherwise crashes any downstream `title.lower()` / `title[:N]`; on Python 3.12+, where slices became hashable, `dict[:50]` raises `KeyError: slice(None, 50, None)` rather than a TypeError. A malformed `tasks.json` self-heals on the next save; `coder._todo_item_text` delegates to the same helper.
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
*   **History Content Strip:** After a successful write, the `content` argument in the matching assistant tool-call history entry is replaced with `[content omitted - N bytes written to disk]`.
*   **Post-Action Linting:**
    *   Calls `_run_linter_for_files`.
    *   **IF Errors:** Sets `current_state.linter_errors_active = True`.
    *   Injects system message with error details.

### Final verdict: edits count as outcomes
The end-of-run SUCCESS/FAILED split keys on `files_created` (the create
bookkeeping), which stays empty for a run that only MODIFIED existing files -
the everyday "add a section to the README" case reported "Task Failed - No
files were created" over a committed, successful edit. Before the split,
`_rescue_edited_outcome` re-checks an empty list against git
(`_detect_run_changes` since the run baseline); a non-empty answer IS the
outcome, and only a run that truly changed nothing keeps the failure message.

### `task_done` (Lines ~5287-5823) - The Enforcement Gate
*   **Gate 1: "No Files Created"**
    *   **IF** task type implies creation (e.g. "create script") **AND** `files_created` is empty:
    *   **BLOCK:** `🚨 CRITICAL ERROR: HALLUCINATION DETECTED! No files created.`
    *   **Action:** Force agent to retry loop.
*   **Gate 2: "Unresolved Placeholders"**
    *   **IF** any written file still contains `{{PLACEHOLDER}}` markers:
    *   **BLOCK:** Task is not truly done - agent must replace all placeholders.
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

### `bash` - kernel-jailed workspace shell (`vaf.tools.bash` → `vaf.tools.workspace_exec`)
*   **Purpose:** The coder needs a real shell for its project (run scripts, `npm`/`pip install`, run the app), but must never be able to touch VAF's own source or itself and break the running system.
*   **Registration:** `BashTool(base_dir)` is bound to the coder's workspace at registration (like the git tools), so the shell defaults to the project and confinement is scoped to exactly that directory. With no workspace bound it **refuses** rather than fall back to the process cwd.
*   **Confinement (kernel, not string-filtering):** `run_in_workspace` runs the command inside a **bubblewrap** jail on Linux - the workspace is bind-mounted read-write (edits persist); the system (`/usr`, `/bin`, `/etc`, ...) is read-only; the VAF repo, `~/.vaf`, secrets and the docker socket are **not mounted** (they do not exist for the command); env is `--clearenv`'d (tray API keys never leak) and the network is `--unshare-net`'d (host loopback services like the memory DB are unreachable). Without bubblewrap it falls back to a container with only the workspace mounted and `--network none`; with neither it **refuses** (never a raw host shell).
*   **Docker is refused:** the host docker socket is host-root-equivalent and cannot be safely policed by inspecting the command string, so `bash` refuses any `docker` invocation up front. Host/docker tasks are the *main agent's* `host_bash` (below), under explicit confirmation.
*   **Command classifier:** `vaf/core/command_policy.py` runs with the `jailed` profile - it refuses only what would reach the machine or the jail root (fork bomb, block-device write, recursive delete of a protected root). A network fetch piped into a shell is NOT refused here, because `--unshare-net` means it fetches nothing, and `rm -rf node_modules` is ordinary work in a disposable workspace. Defense in depth; the real safety is the jail.

### `run_tests` (`vaf.tools.sandbox_test_runner`)
*   **Purpose:** Give the coder a sanctioned way to actually run its project's tests and get the **real** pass/fail, instead of guessing "tests pass".
*   **Execution:** Copies the project (tar-pipe) into a fresh `/workspace/testrun_...` dir in the `vaf-sandbox` container, runs `python3 -m pytest -q` under an in-container `timeout -s KILL`, streams the summary back, and cleans up the run dir in a `finally`. The copy excludes `.git` and is network-less, so it is not a host shell: a `git ...` or OS-package-install command sent as the `command` is rejected up front with a redirect to `git_log`/`project_history`/`project_rollback` (real repo) or `edit_file`/`write_file`, instead of failing silently and burning loops.
*   **History budget:** `run_tests` output shares `read_file`'s larger char limit so the pytest summary is not truncated out of the LLM history.

### `render_check` (`vaf.tools.render_check`, core `vaf/core/browser_render.py`)
*   **Purpose:** The visual half of the verify loop: `run_tests` proves the logic, `render_check` proves the page. The coder opens an HTML file it just wrote (or a URL) in the sandbox browser and gets back a developer's report - page errors, console output, failed requests (HTTP 400+), final URL/title, and the rendered text - so "the page works" is an observation, not a claim. Text-first by design: the coder has no vision lane, and the report must carry the loop on its own.
*   **Registration:** `RenderCheckTool(base_dir)` next to `run_tests`, unconditionally (the schema is advertised in every mode). Relative targets resolve against the project directory. The same class is the main agent's registry tool (default constructor), so the account allowlist governs both lanes under one name.
*   **Execution:** Project files ride the existing workspace mirror into the browser container (`file:///home/browser/Workspace/...`, jailed to the caller's own project root); `localhost` URLs are rewritten to `host.docker.internal` and reach host dev servers that listen on `0.0.0.0`. While a user or agent run holds the browser it answers busy instead of hijacking the visible tab. See the render_check section in `BROWSER_AGENT.md`.

---

## 5a. Deterministic guardrail phases (ORIENT → PLAN → BUILD → DOCUMENT)

Two always-run, deterministic phases wrap the planning/execution loop. They are guardrails (like the guided/template rails): fixed stages that lead even a weak model, rather than prompt hints it can ignore. Both are gated `not skip_template` (skipped in CONTENT_ONLY).

*   **ORIENT (before planning) - `_build_orientation_summary(base_dir)`:** a bounded, pure-Python project scan (no LLM). It lists the existing file inventory (in-place-pruned `os.walk`, depth/file caps) and the heads of existing docs, then injects that summary into the planner's `system_prompt` via the `orientation_summary` variable in the `<context>` block. This supplies the existing-project file context through `existing_files_info`, so the planner sees the real files and edit tasks start grounded in the project rather than producing no changes. A fresh/empty project yields a short no-op notice. Deterministic by construction: the inventory is baked into the planner's first request, and the scan cannot loop.
*   **DOCUMENT (after the task loop, before `_final_commit`) - `self._run_document_phase(...)`:** creates or updates the README to reflect this run's real changes.
    *   **Change detection:** `_detect_run_changes(base_dir, run_start_sha)` diffs the working tree against `_run_start_sha` (HEAD captured right after `_ensure_git_repo`; the git-empty-tree when there is no baseline) plus untracked files - `git diff --diff-filter=ACMR` + `git ls-files --others`. If only docs changed, it is a no-op.
    *   **Single-shot, not a loop:** the model is asked **once** (`self.query_llm`) for the README content; Python then writes it. The model has **no tools** in this phase, so it cannot derail or touch source.
    *   **Positive allowlist:** writes only a top-level README (exact `readme` stem + doc extension via `_is_readme_name`) or `docs/**`; the target is symlink- and containment-checked (`_doc_target_is_safe`) so the write can never follow a link out of the project.
    *   **Non-destructive:** create-mode without an LLM answer writes a minimal deterministic README; update-mode never overwrites a good README with a stub or a materially shorter/truncated regeneration (kept-existing guard). Leaked `<think>` reasoning and wrapping code fences are stripped only when unambiguous.
    *   The written doc lands in the same `_final_commit`. The whole phase is exception-isolated so a failure never skips that commit.

## 5b. Deterministic gates (dispatch-level, code not prompt)

The prompt has always stated these rules ("run_tests must be green before `task_done`", "read before you edit") and the measured reality is that models ignore stated rules under pressure - the linter gate exists as code for exactly that reason. Each gate's decision logic is a **module-level pure function** (the `_verify_task_goal` pattern), unit- and mutation-tested in `tests/test_coder_gates.py`; the dispatch hooks answer the pending tool call before `continue` (tool-call adjacency).

*   **Verify-before-done** (`_unverified_done_reason`): `task_done` is blocked while something was written after the last GREEN verification - `run_tests` answering `TESTS PASSED`, or `render_check` with no page errors. Order is tracked per task context (`ContextState.last_write_seq` / `last_green_verify_seq`, fed from the central result-append), so a retry starts clean. Two containment rules keep the gate from becoming a dead loop: it only demands lanes that exist (render_check when web pages were written, run_tests when the project has test infrastructure - `_project_has_test_infra`), and after two blocks it stands down with an explicit "passes as UNTESTED" system note instead of blocking forever (sandbox down, browser busy).
*   **Read-before-edit** (`_unread_edit_reason`): `edit_file` on an existing file this run has never read (or written) is refused with "read it first". Editing from memory produces search strings that do not match - the measured doom-loop opener on existing repos. The known-files set lives on the run (`loop.files_known`), so reading in task 1 and editing in task 3 is fine.
*   **Search-before-build** (`_sibling_file_note`): creating a NEW file whose stem matches an existing one (case/`-`/`_`-insensitive) gets the sibling named in a post-tool note - once per basename, never blocking; `test_utils.py` next to `utils.py` is legitimate.
*   **Immediate lint feedback, both write lanes** (`_lint_feedback_message`): every successful `write_file` AND `edit_file` lints the file at once and posts the `LINTER CHECK PASSED`/`FAILED` verdict as a post-tool message - a hidden syntax error surfaces ONE reply after the write, not a whole task later (the loop-escape small local models need; `edit_file` previously stayed unlinted until `task_done`). The `task_done` linter gate decides on the LATEST verdict per file (`_active_lint_failure_files`): a fixed file (FAILED, then PASSED) stops blocking immediately instead of waiting for the stale message to scroll out of the window.
*   **Guards feed**: every gate block, loop intervention (stuck detection, context resets, inactivity auto-complete, premature-completion blocks) and note reports one row into the `coder_state` payload's `guards` list, rendered in the SubAgent window's bottom-panel **Guards tab** (see `WEBUI_WEBSOCKET_FLOW.md`). The run's self-corrections are a first-class UI signal, not terminal-only output.
*   **Lifecycle stepper**: the payload's `phases` list (plan → build → document → commit, with live status) renders as a stepper above the Tasks section - the Tasks list shows WHAT is being built, the stepper shows WHERE the run is. Anchors: build on the first loop inside a task context (covers model plan, auto plan and retries through one line), document before `_run_document_phase`, commit around `_final_commit`. `run_tests` and `render_check` also set the live action line ("Running tests...", "Render check..."), so file-less verify stretches no longer read as frozen.

---

## 6. Cleanup & Exit
*   **Final Commit (every exit path):** before the final summary is built, `_final_commit(base_dir, message)` runs `git add -A` and commits when changes exist. The commit message is `VAF Coder: <task excerpt>` plus a status line (`Status: COMPLETE|PARTIAL (n/m tasks)`); runs with failed or remaining tasks commit too, so no work is ever left untracked. If no git identity is configured, the commit retries once with a one-off VAF identity (`-c user.name=... -c user.email=...`, the user's git config is never modified). Unsafe directories and CONTENT_ONLY temp dirs are excluded. The result line appears in the final summary. These commits are what powers the history/rollback delegation (section 3.A0).
*   **Logic:**
    *   Stops TUI thread (`live.stop()`).
    *   Cleans up temporary handles.
    *   Returns the final string (list of created files + instructions + task status incl. failed tasks) to the user.

## 7. WebUI Live Feed (VS-Code SubAgent Window)

During a run the coder feeds the WebUI's VS-Code style SubAgent window through two emit closures in `run()`:

*   **`_emit_coder_state()`** - full project state: file tree (`_build_file_tree`, per-file status W/A/M), git state (`_build_git_state`: branch, dirty count, recent commits), the REAL task list from the TaskManager with live per-task status, loop count, task progress and linter flag. Sent at run start, every loop iteration, after each `write_file`, on task completion/failure and after the final commit. Published through `StatePublisher("coder_state", dedupe=True)`: duplicate payloads are not resent, and there is deliberately no time floor, because the payload itself is the change signal and a window could swallow the terminal emit. Event type: `coder_state`.
*   **`_emit_live_code()`** - the partial file content while the model is still streaming a `write_file` call (hooked into the same stream parser that drives the terminal code preview). Sent as a minimal `subagent_update` with only `file` + `code`; published through `StatePublisher("subagent_update", min_interval=0.35)`, tail-capped at 6 KB at the call site, plus one **forced** full-content post when `write_file` dispatches. `force` bypasses the clock and never a duplicate check; this publisher has none. This drives the live-typing editor pane.

Both resolve the session id through `vaf.core.progress.resolve_ui_session_id()` (environment `VAF_SESSION_ID` first, then the IPC context) **before building the payload**, and stay silent without one. That ordering is load-bearing, not tidy: the state payload shells out to `git` six or more times per call, and a plain CLI run must not pay that for nobody.

Both publish through `vaf.core.progress.StatePublisher`, constructed per `run()`: `StatePublisher("coder_state", dedupe=True)` for the state (no clock at all - the payload IS the change signal, and `activity` is what keeps it moving in file-less phases, so a time window would swallow the terminal emit after the final commit) and `StatePublisher("subagent_update", min_interval=0.35)` for the live code feed. `force` bypasses the clock and never a duplicate check. The publisher is per run because a cell that outlived it would suppress the next run's first frame, and that frame is the one that opens the window.

Transport is `emit_coder_state()` / `emit_coder_code()` in `vaf/core/web_interface.py`, both one-line consumers of the single `_bridge_or_push()` fork (subprocess HTTP bridge or direct WebSocket push). See `docs/web-ui/WEBUI_WEBSOCKET_FLOW.md` for the payloads and the frontend rendering.

## 8. Telemetry (logs/debug)
Loop-level telemetry (`loop_start`, `tool_start`, `coder_debug`, `task_stuck_verification`, `final_commit`, ...) persists as `logs/debug/<agent_type>/<run_id>/events.jsonl` in **every** run mode. `get_subagent_logger_from_env(create_fallback=True, agent_type=...)` no longer depends on the IPC spawn path: without a `VAF_TASK_ID` a local run id (`local-<timestamp>-<pid>`) is generated. The `vaf subagent run` CLI sets this id in its own (single-task) process environment so the runner and the hosted tool log into one directory. Run directories older than 14 days are swept best-effort on logger startup.
