# Debugging and Logging

Where VAF writes its diagnostic output, how to control it, and how to
reconstruct what a run did. Applies to the product and to embedded use
([EMBEDDING.md](EMBEDDING.md)).

---

## Where logs go

The app log directory is the first of these where a `mkdir` succeeds
(`vaf/core/log_helper.py get_app_log_dir`):

1. `$VAF_LOG_DIR` (environment variable)
2. `<repo>/logs` (two levels above `vaf/core/`; for a pip install this is
   inside site-packages)
3. `Platform.data_dir()/logs` (Linux `~/.local/share/vaf`, macOS
   `~/Library/Application Support/vaf`, Windows `%LOCALAPPDATA%/vaf`)
4. `~/.vaf/logs`
5. package `vaf/logs`, then the current working directory as last resort

The tray pins `VAF_LOG_DIR` to the repo `logs/` at startup, so in a normal
checkout everything lands there. A few writers use slightly different orders
and two writers ignore `VAF_LOG_DIR` entirely: the sub-agent debug tree
(repo `logs/debug/`, else `~/.vaf/logs/debug`) and the desktop leak
diagnostics. For everything else, set `VAF_LOG_DIR` explicitly - especially
when embedding, where the default can otherwise land inside your venv's
site-packages.

Dated files are named `<family>_YYYY-MM-DD.<ext>` and garbage-collected after
`gc_max_age_hours` (default 48h; `gc_enabled`, `gc_interval_hours` in config).

## The switch: `debug_logs_enabled`

`debug_logs_enabled` (default `true`, admin-only/global key, no UI toggle)
gates the domain logs, queue metrics, timeline, and channel logs. Notes:

- It is read from the **on-disk** `~/.vaf/config.json`. Passing it via an
  embedded `Agent(config={...})` override does *not* silence file logging -
  edit the on-disk config for that.
- Even with it off, some files still get written (deliberately, they cover
  crashes and process spawning): `crash_*.log`, `tray_startup_*.txt`,
  `faulthandler.log`, `stream_debug_*.txt`, `workflow_debug_*.log`,
  `platform_subprocess_*.log`, the web-interface `[SEND_FAIL]`-style queue
  lines, the sub-agent `events.jsonl` tree, the desktop window's
  `leak_diag_*.log` (disable with `VAF_LEAK_DIAG=0`), and the local server's
  rolling `server_last.log` / `server_last.prev.log` pair.
- `memory_db_echo` (default `false`) additionally enables SQL query logging.

## Log families - which file answers which question

| File | What it answers |
|---|---|
| `queue_*.log` | Was the request queued/started/finished? Per-lane load (`[METRICS]` lines with interactive/automation/background counts); WebSocket delivery failures (`[SEND_FAIL]`, `[PUSH_DROP]`) |
| `backend_*.log` | Provider/API errors per provider, retries, backend swaps |
| `server_*.log`, `server_cmd_*.log` | Local llama-server stdout/stderr and the exact launch command; with debug off, see the undated `server_last.log` pair. A failing local model load is visible here, not in Python stderr |
| `rag_*.log`, `memory_*.log`, `attach_*.log` | RAG search/ingest timing and scope, memory profiler RSS lines, attachment pipeline stages |
| `prompt_*.log` | The full system prompt as sent (`[SYSTEM_FULL]` blocks) and persona (`[SOUL]`) lines |
| `headless_*.log` | Headless runner startup/lifecycle checkpoints |
| `tool_use_*.log` | One line per tool call: timestamp, tool, session id, user scope, 200-char args preview - the first stop for user-isolation questions |
| `timeline_*.jsonl` | Hash-chained event timeline (`tool_start`/`tool_end`, sub-agent and thinking runs); served with chain verification via `GET /api/logs/timeline/events` |
| `vaf_think_*.log` | Proactive/thinking runs, one human-readable block per run |
| `startup_trace_*.txt`, `tray_startup_*.txt`, `tray_debug_*.log` | Startup sequencing; tray issues |
| `crash_*.log`, `faulthandler.log` | Unhandled exceptions in the CLI loop; native crashes (SIGSEGV etc.) |
| `telegram_reply_*` / `discord_reply_*` / `whatsapp_*` | Channel bridge send/receive diagnostics |
| `logs/debug/<agent_type>/<task_id>/events.jsonl` | Per-sub-agent-run structured event stream (sanitized args/results with length/sha256/preview); swept after 14 days by the writer itself, but the periodic GC removes them after `gc_max_age_hours` (48h) in a running app |

Admins can list and tail `.log` files over HTTP: `GET /api/logs` and
`GET /api/logs/<filename>?tail=500`.

## Reading a session

Sessions live in `~/.vaf/sessions/<session_id>.json` (atomic writes; ids look
like `blue378604`). Top-level keys: `id`, `name`, `created_at`, `updated_at`,
`model`, `project_path`, `messages`, `metadata` (carries `user_scope_id`),
`runtime_state`, `state_version`.

To reconstruct a turn: iterate `messages`; an assistant message's
`tool_calls` pair with the following `role: "tool"` messages via
`tool_call_id` (+ `name`); user-turn images are under `metadata.images`;
proactive bubbles are tagged with `kind` (`thinking` | `nudge` | `timer`).

Per-session working state of the main agent (plan/tasks/notes and tiered tool
results) is separate, under `<cwd>/.vaf/main/sessions/<session_id>/`. Sub-agent
IPC state (pending/completed tasks) is under `~/.vaf/subagent_queue/`.

## `vaf debug`: LLM-assisted error analysis

`vaf debug` is an error-explanation CLI (not a log viewer): `explain` for an
error message, `trace` for a stack trace (from `--file` or stdin), `why` and
`fix` for cause/fix suggestions. It uses the configured provider (local mode
needs the local server running, e.g. via `vaf run`).

## Embedding notes

- An embedded agent **writes log files by default**. Set `VAF_LOG_DIR` to
  choose where; disable the gated families via `debug_logs_enabled: false` in
  the on-disk config (see the always-written exceptions above).
- Besides logs, an embedded `CoreAgent` creates `<cwd>/.vaf/main/` (working
  context) and reads `~/.vaf/config.json` (it does not create it).
- `verbose=True` on the facade adds extra stdout diagnostics and un-suppresses
  llama-cpp's own stderr; it does not change file logging.
- Common runtime failures and their first file to check: model fails to load
  -> `server_*.log` / `server_last.log`; provider errors -> `backend_*.log`;
  "queued but nothing happens" -> `queue_*.log`; memory tools returning
  errors -> is the Docker memory stack up? (`docker ps`, see
  [DOCKER_SERVICES.md](setup/DOCKER_SERVICES.md)); port 8080 conflicts ->
  another llama-server instance (one-server rule, see
  [PROVIDER_MODES.md](llm/PROVIDER_MODES.md)).
