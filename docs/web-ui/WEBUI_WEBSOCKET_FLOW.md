# WebUI WebSocket Flow & Debugging

This document explains how the WebUI connects to the backend over WebSocket, how session
scoping works, and how to debug missing responses. It is intended to prevent regressions
like "no answers shown in WebUI while CLI works."

## Architecture Overview

```mermaid
flowchart TB
    subgraph WebUI
        FE_WS[WebSocket Client]
        FE_State[currentSessionId]
        FE_Render[Message Renderer]
    end

    subgraph Backend
        WS_Server[FastAPI WebSocket /ws]
        WebIface[WebInterfaceManager]
        TaskQueue[TaskQueue]
        Scheduler[QueuePolicy(legacy or weighted_fair)]
        Agent[Headless Agent Worker(s)]
    end

    FE_WS -->|"chat + sessionId"| WS_Server
    WS_Server -->|"tq.add(session_id,input,metadata)"| TaskQueue
    TaskQueue --> Scheduler
    Scheduler -->|"tq.get(worker_id)"| Agent
    Agent -->|"emit_agent_message"| WebIface
    WebIface -->|"sessionId tag + WS send"| FE_WS
    FE_WS --> FE_Render
```

## Connection Sequence (Happy Path)

1. WebUI opens its WebSocket using the transport from `/api/network/ws-config`: `ws://localhost:8001/ws` when TLS is off; `wss://<host>:<effective proxy port>/ws` for a LAN client behind the HTTPS proxy; and plain `ws://127.0.0.1:8005/ws` for the local desktop (which loads `http://127.0.0.1:3000` and cannot use the proxy's self-signed cert).
2. Backend sends `session_list` and then `history_update` (auto-load latest session).
3. Frontend sets `currentSessionId` and starts sending `chat` messages with session ID.
4. Backend queues the task with metadata (`origin_channel`, `task_class`, `enqueue_session_id`).
5. Headless agent processes and streams `agent_message_update`.
6. Frontend renders the assistant response and the `<think>` block if present.

## WebSocket Authentication

The `/ws` endpoint (`websocket_endpoint` in `vaf/core/web_server.py`) authenticates **inside the
handler**, not via the HTTP `AuthMiddleware` — Starlette's `BaseHTTPMiddleware` never sees
websocket-scope connections, so HTTP middleware cannot gate the handshake. The token is read from the
`?token=<jwt>` query param or the `vaf_token` cookie.

**Trust model.** Localhost (`127.0.0.1`/`::1`, and the Docker bridge `172.16.0.0/12`) is trusted,
consistent with the HTTP localhost bypass. In **network mode** (`local_network_enabled=true`) a
non-localhost client must come from an RFC1918 IP, present a valid `access` JWT, and have passed 2FA
(unless it is an admin on localhost).

**Gating (first match wins):**

| Condition | Result |
|-----------|--------|
| Network disabled + non-localhost IP | reject — close `4003` |
| Network disabled + localhost, missing/invalid token | reject — close `4001` |
| Network enabled + non-RFC1918 IP | reject — close `4003` |
| Network enabled + no token | reject — close `4001` |
| Token expired / invalid | reject — close `4001` |
| 2FA required and not verified (and not admin+localhost) | reject — close `4003` |
| Auth-phase error (secret/import/other) for a **non-localhost** client | reject — close `4003` (fail-closed) |
| Auth success | `manager.connect` + `set_connection_user(user_scope_id, username, role)` |

**Client reconnect (`web/app/page.tsx`).** The browser only opens `/ws` once `GET /api/auth/me` reports an
authenticated session, and reconnects with **exponential backoff + jitter (capped 30s)** rather than a fixed
interval. When a socket closes **without ever opening** — the handshake was rejected (`4001`/`4003`, e.g. an
expired token mid-session) — the client re-checks `/api/auth/me`; a `401`/`403` clears the token and routes
to `/login` instead of retrying. This stops a stale/expired token from hammering `/ws` once per reload.

**Fail-closed guarantee.** A non-localhost connection is **never** established without a resolved
`user_scope_id`. RAG no longer treats a missing scope as global/admin: for an unscoped/`None` request
`RagPipeline.search()` returns `[]`, and `run_memory_search_sync` denies in server mode (and floors to
the local-admin scope in single-user mode), so an unscoped RAG query yields nothing rather than the
whole corpus. As a defense-in-depth guard the handler still closes (`4003`) any non-localhost client
that reaches the connect step without auth context (`user_context is None`), before `manager.connect`,
since `session.list()` would otherwise see no scope. A localhost
client whose auth phase hits a transient error still connects (trusted), so a desktop session is never
bricked by a transient secret/import failure. Note: an `Upgrade: websocket` header on a plain HTTP
request does **not** bypass HTTP auth — real WS handshakes are websocket-scope and never reach the HTTP
middleware, so that header is treated as a normal (authenticated) HTTP request.

On success, the connection's `user_scope_id` and role (from the JWT, via `set_connection_user`) scope
all subsequent RAG/memory/session/automation operations for that socket (see the per-user handlers
below).

## Session Scoping Rules

All updates are scoped by `sessionId`. The frontend **must** keep `currentSessionId`
in sync with backend session updates; otherwise the frontend will filter out messages.

Key rules:
- `chat` must include `sessionId`.
- WebSocket responses are tagged with `sessionId`.
- Frontend ignores messages where `data.sessionId !== currentSessionId`, **except**
  events that intentionally target other sessions: `session_list`, `history_update`,
  `contact_reply_pending`, `session_unread`, and `agent_message_append`. These pass the
  cross-session filter so the UI can refresh lists, show an unread badge, or surface a
  proactive message instead of silently dropping it.
- WebUI enqueue path rejects implicit fallback into messenger sessions (`telegram_*`, `discord_*`, `whatsapp_*`) when `sessionId` is missing, to prevent cross-channel routing.
- **Server-side ownership gate:** Beyond frontend `sessionId` filtering, the backend enforces ownership before the first side effect of every session command — `chat` (before subscribing to the session stream), `load_session`, `delete_session`, `rename_session`, `hide_session`, and `artifact_edit`. The caller must own the session (`metadata.user_scope_id` matches) or be admin (connection role `admin` or local-admin scope); a session with no recorded scope is admin-only. On denial the server sends `{"type":"error","message":"Access denied"}` and continues the receive loop (it does not close the socket). This is stricter than `session_list` visibility, which still shows no-scope sessions to all users.

## Message Types

### Client → Server

- `chat`: user input (must include `sessionId`). Optional: `sidebarDocuments` (`Array<{ name, data, mimeType? }>`, same format as `set_sidebar_documents`); `editorDocument` (`{ name, content }`, plain text of Document Editor when open); `editorSelections` (`Array<{ start, end, text }>`, marked ranges in the editor for `replace_editor_selection`); `codeViewerFile` (`{ name, path, content }`, full text of the file currently open in the Code Viewer, sent automatically on every chat message while the Code Viewer is open — content is NOT stored in the message history); `files` (`Array<{ name, data (base64 data-URI), mimeType }>`, used for **vision/image input** — files with `mimeType` starting with `image/` bypass Librarian text extraction and are passed directly to the LLM as multimodal content blocks via `task.metadata["images"]`; non-image files are extracted as text as before). If present, the backend stores sidebar docs in `session.runtime_state["sidebar_documents"]`, editor doc in `session.runtime_state["editor_document"]`, refreshes the attachment index for that session/scope, prepends the editor doc to the user turn as `--- CURRENT DOCUMENT (Editor): ... ---`, stores `codeViewerFile` in `session.runtime_state["code_viewer_file"]` (the headless runner then injects it into `effective_input` as a numbered-line block per turn and clears it afterwards — keeping it out of persisted message history), and stores editor selections in `session.runtime_state["editor_selections"]` for the selection-based tool path. For native DOCX sessions, `editorDocument.content` is flattened from the native DOCX model instead of browser HTML.
- `set_sidebar_documents`: set documents shown in the Document Viewer (attachments panel) for the current session. Payload: `{ sessionId?, documents: Array<{ name, data (base64/data-URL), mimeType? }> }`. Backend stores extracted text in `session.runtime_state["sidebar_documents"]`, rebuilds the session-scoped attachment index, and injects attachment **top-k snippets** into the next user turn (with an explicit "document context active" header). Send `documents: []` to clear (this also clears session attachment index entries).
- `get_sessions`, `new_session`, `load_session`, `delete_session`. For `load_session`, the server also enqueues `__CMD__:LOAD_SESSION:{id}` so the headless runner immediately loads that session’s context (history and runtime state); the agent stays in sync when the user only switches sessions without sending a message. `load_session`, `delete_session`, `rename_session`, and `hide_session` each verify session ownership before acting and reply with `Access denied` (without closing the socket) if the caller does not own the session and is not admin.
- `get_config`, `get_models`, `get_tools`, `get_workflows`
- **MCP servers (admin):** `get_mcp_servers` (no payload) lists configured servers with live status; `create_mcp_server` / `update_mcp_server` (`name`, `command`, `transport?`, `url?`, `enabled?`, `permission_level?`, `env?` — a `{ key: value }` map merged onto the server process) upsert a server in `mcp_servers.json` and hot-reload the tools; `delete_mcp_server` (`name`) removes one; `test_mcp_server` (`command`, `transport?`, `url?`, `env?`) probes a config without saving. See [MCP_INTEGRATION.md](../agents/MCP_INTEGRATION.md).
- **Automations:** `get_automations` (no payload). Server responds with `automations_list` (`automations: []`). Admins see root + own scope + local_admin_scope tasks; regular users see only their scope. Same scoping applies to `create_automation`, `update_automation`, `delete_automation`.
- **Thinking workspace handoffs (per-user):** `get_thinking_workspace_handoffs` (no payload) to list pending handoffs for review. Actions: `approve_thinking_workspace_handoff` (`task_id`, `handoff_id`) and `reject_thinking_workspace_handoff` (`task_id`, `handoff_id`, `reason?`). All operations use the connection's `user_scope_id`.
- **Automation planner (per-user):** `get_automation_notes`, `get_automation_todos` (no payload). Create/update/delete: `create_automation_note` (`title?`, `content`), `create_automation_todo` (`text`, `due_at?`), `update_automation_todo` (`id`, `text?`, `done?`, `due_at?`), `delete_automation_note` (`id`), `delete_automation_todo` (`id`). Server uses `user_scope_id` from the connection (same as `get_automations`).
- `get_notifications`: optional. Payload: `{ limit?: number }` (default 50). Server responds with `notifications_list` for the connection’s user.
- `contact_reply_decision`: approve or reject a pending contact reply (Front Office). Payload: `{ replyId: string, decision: "approve" | "reject" }`. Server responds with `contact_reply_result` (`ok`, `decision`, `replyId`, optional `error`).
- `speaker_confirm_reply`: answer to a speaker-confirmation card. Payload: `{ confirmId, answer: "yes" | "no", name? }` (name only meaningful with `"no"`: stores a named third-party voice profile). The scope is taken from the CONNECTION identity, never from the client. Server responds with `speaker_confirm_result` (`ok`, `outcome`, `ack`, `confirmId`, optional `error`).
- **Speech:** `process_audio` (`{ audio: base64, format?: "wav" }`; the frontend records via MediaRecorder, converts to 16 kHz mono WAV client-side and sets `format: "wav"`; if conversion fails it sends the raw WebM/OGG recording without a `format` field). The backend transcribes via the configured STT lane (cloud provider, Docker Whisper, or local faster-whisper). `speak` (`{ text }`) requests TTS synthesis; `stop_speech` (no payload) stops playback state.

### Server → Client

- `sidebar_documents_set`: sent after processing `set_sidebar_documents`. Payload: `{ contents: Array<{ name, content, data?, mimeType?, htmlContent? }>, sessionId?, error? }`. Each entry has `name` and `content` (extracted text for the LLM); `data` (base64) and `mimeType` for display. When Gotenberg is available, Office docs (.docx, .xlsx, .pptx, .odt, .ods, .odp) are converted to PDF on the backend and returned as `mimeType: application/pdf` with `data` (PDF base64), so the frontend uses the PDF viewer for original layout. Without Gotenberg, the backend provides `htmlContent` or the frontend falls back to client-side mammoth.js for DOCX.
- `editor_apply_edit`: sent when the agent calls `replace_editor_selection` or when `replace_editor_text` resolves an exact text match in the current editor document. Payload: `{ sessionId, selectionIndex, newText, start, end }`. The frontend replaces the character range `[start, end]` in the Document Editor with `newText` and removes that selection chip when `selectionIndex >= 0`. For native DOCX sessions, this is applied to the native document model; for legacy editor sessions it is applied to HTML/text content.
- `document_ready`: opens a newly created/generated file in the right panel. Payload: `{ sessionId, filePath, title }`. Sent by `notify_document_created()` (`vaf/core/web_interface.py`) — from the main process via WebSocket, from sub-agent terminals via HTTP POST `/api/workflow/update`. Routing in the frontend: document extensions (`.md`, `.mdx`, `.html`, `.htm`, `.docx`, `.xlsx`, `.pptx`, `.txt`, `.rtf`) open in the Document Editor (native DOCX editor path for `.docx`); genuine code files open in the CodeViewer. The document-extension check runs BEFORE `isCodeFile()` — Markdown and HTML count as CodeViewer languages, so research/document reports would otherwise land in the code view. The handler reads the active session from `currentSessionIdRef` (not the closure variable, which is stale/null on the first connect).
- **MCP servers:** `mcp_servers` (`servers: Array<{ name, command, transport, url, enabled, permission_level, env, connected, tool_count, error }>`) in response to `get_mcp_servers`; `mcp_server_saved` / `mcp_server_deleted` (`name`) on success (the frontend re-fetches `get_mcp_servers` to refresh status); `mcp_server_error` (`error`) on failure; `mcp_server_test_result` (`connected`, `tool_count`, `tools`, `error`) in response to `test_mcp_server`.
- **Speech:** `stt_result` (`{ text }`) and `stt_error` (`{ error }`) answer `process_audio` (per-connection). With speaker identification enabled, `stt_result` additionally carries `speaker_label` / `speaker_score` and the `text` is prefixed with the speaker label. `tts_audio` (`{ audio: base64, format: "wav" }`) answers `speak`, always WAV regardless of the configured voice provider. `tts_state` (`{ status: "loading" | "playing" | "stopped", text? }`) drives the loading/playing indicators; note that the host-speech playback callbacks currently broadcast `tts_state` to ALL connections (pre-existing; host speech itself is CLI-only since the host-audio gate).
- **Live call (voice agent):** client sends `voice_call_start` / `voice_call_turn` / `voice_call_end` / `voice_call_speak`; server answers `voice_call_reply` / `voice_call_error` / `speaker_enroll_tts`. Enrollment uses the `speaker_enroll_*` / `speaker_profile_*` family. Payloads, guards and the delegation protocol are documented in [VOICE_AGENT.md](../agents/VOICE_AGENT.md).

## Native DOCX Editor Endpoints

The native DOCX editor uses dedicated backend endpoints instead of the legacy HTML roundtrip:

- `GET /api/file/docx-model`
  - Loads `.docx` into VAF's native DOCX model.
- `POST /api/file/save-docx-native`
  - Saves the native DOCX model back to `.docx`.

These endpoints are used only for the native DOCX editor path. The legacy HTML editor endpoints remain available for non-DOCX editor flows.
- `session_list`: available sessions
- `history_update`: session history (also sets active session). The frontend does not clear document-panel attachment state for that session, so per-session attachment documents persist across repeated switches. Tool messages now include `toolName`, `toolId`, and `toolStatus` (either from `metadata.*` or from top-level keys `name`/`tool_call_id`; status defaults to `"completed"` if content is present). The frontend extracts these fields when parsing server messages to ensure tool cards display correctly after reload.
- `agent_message_update`: streaming assistant text (full content so far). The frontend shows a **separate** assistant bubble when the last message is a tool card: only the text after the previous assistant content is shown in the new bubble, so tool use and the follow-up answer appear distinctly.
- `agent_message_append`: a complete, **standalone** assistant message that is always appended as its own new bubble — never streamed or merged in-place. Used for proactive messages (e.g. automation results) where there is no live agent turn to attach to; the streaming `agent_message_update` path would otherwise overwrite the previous reply or drop the text. If it targets the active session the frontend appends it; if it targets another session the frontend shows an unread badge instead; if no session is active yet the frontend adopts and loads the target session.
- `session_unread`: marks a session as having a new unread agent message (e.g. an automation result delivered to a session that is not currently open). Broadcast to all of the user's connections; the frontend shows an unread badge on that session in the sidebar when it is not the active one.
- `clear_last_assistant`: request to remove the last assistant message (used before empty-response retries — and false-promise retries when `false_promise_detection_enabled` is on — so only the retry response is shown). **Guard:** the frontend only removes the message if its timestamp is from the current turn (≥ the user's last send time). This prevents repeated retries from eating completed assistant messages from earlier turns.
- `new_log`: system/status timeline entries. When the agent gives up after API empty-response delayed retries, it sends the final message only via `new_log` (return value `[SYSTEM_LOG_ONLY]...`); the headless runner does **not** send `agent_message_update` for that response, so the UI shows a system timeline entry only.
- `tool_update`: tool start/end/error. On `start`, `data` is the call arguments as a JSON string (`json.dumps(arguments)`) and the tool card renders it as the **input** line; on `end`/`error`, `data` is the result. `ToolMessage.extractMainInput` shows the first human-readable string value (a curated key such as `query`/`content`/`text`, then any string field); when the arguments carry no string value — structured tools such as `update_working_memory`, whose args are arrays/numbers/booleans (`tasks`, `mark_task_done`, `mark_all_done`) — it falls back to a compact `key: value` summary (arrays as `[N items]`, long strings truncated) so the input line is never blank. **Note:** Tool events are always emitted — they are NOT gated by `_emit_to_web_ui()`. The previous gating caused a race condition where the process-wide `VAF_THINKING_MODE` env var (set by background thinking) would block tool updates for active WebUI sessions. Tool events use `broadcast_to_session(session_id)` for safe, session-scoped delivery.
- `stats`: token/usage metrics (used/total, percent; can include input/output from API). Filtered by session when `sessionId` is set.
- `queue_stats`: queue/worker metrics from headless runner (`interactive`, `automation`, `background`, `inflight_total`, `inflight_sessions`, oldest wait per class, `queue_policy`).
- `context_status`: detailed context stats (tokens, max_tokens, percent, system/history/tools breakdown, compaction progress). Sent only to connections subscribed to that session so the context bar stays correct per tab.
- `subagent_update`: sub-agent window payload
- `subagent_output`: final sub-agent output block
- `subagent_output_stream`: live stdout/stderr lines from headless sub-agents
- **Mirrored output is rate-capped at the emit site** (`vaf/core/web_ticker.py`). Every lane
  that mirrors a stream into the browser - the in-chat workflow executor, the workflow
  builder, the separate workflow terminal and the piped sub-agent drain - feeds the ONE
  shared ticker: ANSI stripped, repeats dropped, at most 15 lines per 0.25 s, a per-run
  ceiling, and suppressed volume surfaced as `[... N lines skipped]` rather than hidden.
  This is not cosmetic. Rich draws its Live panels by redrawing them 15 times a second, and
  an unfiltered mirror turns that into one WebSocket frame, one React render and one HTTP
  POST per redraw. It has caused two incidents: 2026-07-16 the tray browser froze, and
  2026-07-20 the in-chat lane pushed 48,359 frames in 181 s and the socket died mid-run,
  after which every event that would have closed the Workflow Runtime panel was broadcast to
  zero subscribers. The mirrors used in-process also report `isatty()` as false, so Rich
  never starts the animation in the first place; the separate-terminal lane keeps the real
  stream's value, because that window exists to be watched.
- **SubAgent window vs. running workflow:** while a workflow is running, the SubAgent window
  never opens - the Workflow Runtime panel's terminal is the single display for embedded
  sub-agent steps. The stream/output handlers route their lines into the workflow terminal
  (`isWorkflowRunningRef`), `openSubAgentWindow(false)` is a no-op, and the `subagent_update`
  heartbeat handler's direct `isOpen` set carries the same guard (it used to bypass it, so the
  coder heartbeat opened a duplicate window next to the runtime panel - live incident). A
  MANUAL open by the user still works.
- `subagent_update.status` may include heartbeat age (`Running sub-agent tasks... (heartbeat Ns ago)`) even when no new stdout line is emitted.
- `coder_state`: live project state from the coding agent, enables the VS-Code view in the SubAgent window. Payload: `{ fileTree: [{name, size, status: "W"|"A"|"M"|""}], git: {branch, dirty, commits: [{sha, when, msg}]}, tasks: [{title, status: "pending"|"running"|"completed"|"failed"|"skipped"}], loop, taskProgress, linterOk, projectName, projectPath, diffs: { "<path>": "<unified git diff vs the run-start snapshot>" }, activity: "<current action, e.g. 'Loop 26'>" }`. Emitted by `emit_coder_state()` (`vaf/core/web_interface.py`) from the coder loop (run start, every loop iteration, after each `write_file`, after the final commit), hash-throttled so unchanged states are not resent. `diffs` carries the per-file unified diff (vs the run-start snapshot, so a previous run's changes are not shown) that the editor renders red/green for the file currently being edited, taking priority over the streamed/read buffer; `activity` is the agent's current action, surfaced as a live phase signal (Planning / Building / Finalizing) so file-less phases (docs, verify) do not look stuck. The editor is multi-tab: a persistent Live tab always follows the agent, and files opened from the Explorer each get their own closable read-only tab. `tasks` is the coder's REAL plan from its TaskManager with live per-task status — the Tasks section renders it directly; the generic heartbeat steps are only the fallback. The window renders: left column with file tabs, live code editor (line numbers, lightweight syntax highlighting, blinking cursor while presence is online, dark-theme toggle) and console; right sidebar with Explorer (file status badges: W=writing, A=added, M=modified), Tasks and Source Control (branch, dirty count, recent commits, rollback hint); bottom status bar (branch, head commit, linter, task progress, loop). Without `coder_state` data the window keeps the classic layout — browser/research/document agents are unaffected.
- `research_state`: live state from the research agent, enables the paper-style research view in the SubAgent window. Payload: `{ topic, stage, sections: [{title, status: "planned"|"searching"|"writing"|"done"|"error", words, targetWords}], sectionsHtml: [..], sources: [{url, title, domain}], wordsTarget, loop }`. Emitted by `emit_research_state()` (`vaf/core/web_interface.py`) from the research loop (plan ready, per-section transitions, retries, finalize), hash+time throttled. The window renders: a paper-like document viewer where the report grows section by section (the newest section types out client-side, then swaps to the rendered HTML), a sidebar with outline progress (per-section word bars), clickable source citations and the activity feed, plus a status bar (stage, sections done/total, words, sources, loop). Coder, browser and research agents use the wide dock window (max 1400px), derived from the presence of custom data (coder/research state or a browser frame) so the width never flips mid-run; other sub-agents keep the classic width. For these custom-view agents the window stays CLOSED until their first custom data arrives — the generic window never flashes first; agents without a custom view open the classic narrow window immediately at task start.
- **Coder live code feed:** while the model streams a `write_file` call, the coder emits the partial file content via `emit_coder_code()` as a minimal `subagent_update` carrying only `file` + `code` (all other window state keeps its previous values). Time-throttled to one post per 0.35s, content tail-capped at 6 KB; a final unthrottled post with the full content fires when `write_file` dispatches. This is what makes the editor pane type live instead of showing "Waiting for code". The same feed also mirrors a file the agent *reads* (so reads are visible in the editor during orientation/verify) and is cleared after an `edit_file` so the editor falls through to that file's `diffs`. Telemetry: `live_code_emitted` events in `logs/debug/coding_agent/<run>/events.jsonl`.
- **Console fragment merging:** Rich-TUI redraws of streaming text arrive as progressively longer versions of the same line. `appendSubAgentLine` (`web/app/page.tsx`) replaces the previous console line in place when the new content extends it (prefix match, min. 4 chars), instead of stacking fragments. The console also stays pinned to the bottom during the typewriter animation (per-tick scroll unless the user scrolled up).
- `model_state`: status of the local model (`loaded`, `persistent`, `provider`)
- `config_saved`: confirmation that the settings were saved; on a provider change the response includes `requires_refresh: true`, after which the Web UI shows the "Changing model" overlay and reloads after 5 seconds (see [MODEL_AND_PROVIDER_SWITCHING.md](../llm/MODEL_AND_PROVIDER_SWITCHING.md)).
- **Automations:** `automations_list` — response to `get_automations`. Payload: `{ automations: [] }`. Each item has `id`, `name`, `description`, `prompt`, `frequency`, `time`, `weekday?`, `day?`, `enabled`, `next_run`, `last_run`.
- **Thinking workspace handoffs:** `thinking_workspace_handoffs_list` — response to `get_thinking_workspace_handoffs`. Payload: `{ handoffs: [] }` (pending items for current user). `thinking_workspace_handoff_result` — response to approve/reject actions. Payload: `{ ok, action: "approve" | "reject", task_id?, handoff_id?, automation_action_result?, error? }`. For approve, `automation_action_result` contains the optional bridge result (`{ ok, operation, task_id?, error? }`) when the handoff requested an automation create/update action.
- **Automation planner responses:** `automation_notes_list` (`notes: []`), `automation_todos_list` (`todos: []`); `create_automation_note_result` (`ok`, `note?`), `create_automation_todo_result` (`ok`, `todo?`), `update_automation_todo_result` (`ok`, `todo?`), `delete_automation_note_result` (`ok`, `id?`), `delete_automation_todo_result` (`ok`, `id?`). The frontend updates lists optimistically using returned `note`/`todo`/`id` when present.
- **Notifications:** `notification` — live push of a single notification (thinking run, automation result, workspace handoff decision, or channel reply). Payload: `{ notification: { id, kind, title, status, timestamp, summary?, sessionId?, channel?, task_name?, run_id?, action?, task_id?, handoff_id?, automation_action_result? } }`. For workspace handoff notifications, `action` is typically `approve`/`reject`, and `automation_action_result` may include `{ ok, operation, task_id?, error? }` when approval triggered automation create/update. `notifications_list` — response to `get_notifications`. Payload: `{ notifications: [] }`. Items are scoped to the user; the Notifications popup loads via `GET /api/notifications` or `get_notifications` and appends on `notification`.
- `contact_reply_pending`: a reply to a contact (Front Office) is waiting for approval. Payload: `{ replyId, source ("telegram"|"whatsapp"), contactName, preview, sessionId }`. The UI shows Approve/Reject; the client sends `contact_reply_decision` with the same `replyId` and `decision: "approve"` or `"reject"`.
- `contact_reply_result`: response to `contact_reply_decision`. Payload: `{ ok, decision?, replyId, error? }`. Used to remove the pending item from the UI or show an error.
- `speaker_confirm_pending`: a voice segment scored "unsure" and the owner should confirm (web fallback lane; the primary lane is the main messenger). Payload: `{ confirmId, question, audioPath, score }` - no `sessionId`, so it passes the session filter globally. Emitted per-user via `push_update_to_user` (never broadcast). The UI shows an audio player + yes/no buttons + an optional name field and replies with `speaker_confirm_reply`.
- `speaker_confirm_result`: response to `speaker_confirm_reply`. Payload: `{ ok, outcome ("self"|"other"|"named"|"expired"), ack, confirmId, error? }`. Removes the card.

## Troubleshooting Checklist

### 0) Log Locations (Debug Builds)

WebUI debug traces are written to the first writable location in this order:

1. `VAF_LOG_DIR` (if set)
2. Repo `logs/` (preferred, so WebUI debug files sit next to `queue.log` etc.)
3. `Platform.data_dir()/logs` (OS-specific app data dir)
4. `Platform.vaf_dir()/logs` (user home)
5. Package `vaf/logs` (last resort; the current working directory if none is writable)

Useful files when debugging WebUI / LLM / queue (all under the log dir above):

| File | Contents |
|------|----------|
| `queue.log` | **QUEUE_ADD**, **QUEUE_GET**, **QUEUE_CHAT_START** / **QUEUE_CHAT_END**, **QUEUE_CHAT_FAIL**, **QUEUE_DONE**, **[METRICS]** (class depths, inflight, oldest wait, policy) |
| `backend.log` | Backend per chat_step `[api(...)` / `server(8080)` / `library(...)]`, **503 model_loading retry**, **unavailable_after_retries**, **calling_8080**, **read_timeout no_data_60s** / **heartbeat_timeout no_data_30s** / **read_timeout_during_stream**, **\[CHUNK\]** / **\[CONTENT\]** (API stream) |
| `webui.log` | **\[WARNING\]** only when a message is dropped (no server loop). Stream/emit logging is disabled to avoid UI lag. |
| `rag.log` | RAG timing, search debug, embed calls, snippet count, user scope, failures |
| `memory.log` | **\[COMPACTION\]**, **\[USAGE\]** RSS, **\[EMBED\]** load, **\[PROFILER\]**, **\[WHISPER\]** (all timestamped) |
| `headless.log` | **\[STARTUP\]** Headless PID, log dir, Memory Profiler status; **\[LIFECYCLE\]** checkpoints from IPC cleanup through Agent init to main loop entry (always written, independent of `debug_logs_enabled`); **\[FATAL\]** full traceback if the worker thread crashes before entering the main loop |
| `prompt.log` | **\[SOUL\]** persona block, **\[SYSTEM_FULL\]** full prompt dump (multi-line) |

### 1) WebSocket Connected, But No Answer

**Expected logs in WebUI timeline:**
- `Queued input for session ...`
- `Processing task for session ...`
- `Starting chat_step for session ...`

If `Queued input...` is present but no further logs appear:
- **Headless agent is not consuming the queue.**
- Open `headless.log` and check `[LIFECYCLE]` entries — they trace every startup phase (IPC cleanup → Agent init → TaskQueue → SessionManager → main loop). The last `[LIFECYCLE]` entry shows where the worker stalled or crashed.
- If `[FATAL]` appears, it contains the full traceback of an unhandled exception that killed the worker thread.
- If no `[LIFECYCLE]` entries appear at all, `headless_runner.py` was never called — verify the tray starts the `HeadlessAgent` thread.
- On startup the headless runner calls `tq.reset_runtime_state()` to clear orphaned in-flight session locks from a previous crash. If tasks are queued but the worker loops without picking them up, check for stale `session_inflight` entries via `tq.get_queue_stats()`.

If enqueue is accepted but task appears in a messenger session unexpectedly:
- Check for routing guard logs: `[ROUTING_WARN]` / `[ROUTING_BLOCK]` in `headless.log`/`backend.log`.
- Ensure frontend sends explicit `sessionId` and connection subscription is correct.

If `Starting chat_step...` appears but no response:
- **chat_step crashed or hung**.
- Check if the error mentions encoding (`charmap`); fix with UTF-8 output.
- For local backend: if `backend.log` shows `calling_8080 attempt=1` and nothing after, the server may have stopped sending data. The agent now applies a 5‑minute read timeout; after that it ends the step and the queue continues. Check `server.log` and machine load if timeouts repeat.

If the agent replies with "I cannot see/open the attachment" even though files were uploaded:
- Confirm attachment indexing exists in `rag.log` (`ATTACH_INDEX session=... indexed=...`).
- Check the session file (`~/.vaf/sessions/<session>.json`) and verify `runtime_state.sidebar_documents` is not empty.
- Root cause seen in production: `SessionManager.save(sync_state=True)` overwrote `runtime_state` with provider snapshot data and dropped non-provider keys such as `sidebar_documents` and `editor_selections`.
- Fix: merge snapshot keys into existing `runtime_state` instead of replacing it; preserve non-provider runtime keys.

### 1b) Local Backend Not Reachable

If you see `LLM Call Failed: HTTPConnectionPool(127.0.0.1:8080)`:
- The local HTTP backend is not running or was stopped.
- Ensure the tray is running and the backend is reused instead of starting a second process.
- If multiple `llama-server.exe` instances appear, close all of them and restart the tray.

### 1c) 503 "Loading model" on first prompt / local model no thinking / RAM 15–20 GB

- **503 on first prompt**: Headless now waits for `http://127.0.0.1:8080/v1/models` to return 200 (up to 2 min) before the first chat when using the server backend; the WebUI shows "Model is loading, please wait..." during that time.
- **Local model thinking**: Once the first request no longer hits 503, the server path streams `reasoning_content` (thinking) correctly. Tool calls emitted inside `<think>` are still parsed (agent searches `full_response` + `full_reasoning` for `<tool_call>...</tool_call>`); the system prompt instructs the model to place tool calls in the main response.
- **RAM spike (double model)**: On Windows, `force_server` defaults to **true** so the agent uses the HTTP backend (8080) only and does not load the library in-process. If the server block was skipped (e.g. server failed to start), the agent checks 8080 again before loading the library and reuses the server if reachable.

### 2) Messages Filtered on Frontend

If `agent_message_update` appears in WS frames but UI is empty:
- Check `currentSessionId` in `web/app/page.tsx`.
- Ensure `history_update` was received after connect.

### 3) No `agent_message_update` in WS Frames

If only `stats` or `new_log` arrives:
- The agent likely failed before streaming.
- Check for `Chat_step failed` log in the WebUI timeline.

### 3b) API Tool Calls Loop / “False promise detected”

This is **off by default** (`false_promise_detection_enabled`, default `false`). When enabled: when the agent detects a **false promise** (model claimed to use a tool in text but did not emit a tool call), it forces a retry and sends `clear_last_assistant` so the Web UI removes the faulty assistant message—same behaviour as empty-response retry. The user sees only the system notice and the retry response, not a duplicate bubble.

If responses loop with `False promise detected` without recovery:
- The API may be emitting tool-call chunks without a function name.
- The agent should drop invalid tool calls and fail fast instead of retrying.
- Check `backend.log` for `[CHUNK]` / `[CONTENT]` and `tool_calls` entries where `name` is missing.

### 4) Sub-Agent Panel Does Not Open

Expected triggers for the docked panel:
- `subagent_update` (preferred)
- `subagent_output` / `subagent_output_stream`
- `tool_update` with sub-agent tool name (e.g., `librarian_agent`)
- `new_log` with source/message containing "Sub-Agent"

If the tool card expands but the panel does not open:
- Confirm the WebSocket payloads include `sessionId` matching `currentSessionId`.

## Known Failure Modes and Fixes

| Symptom | Likely Cause | Fix |
|---|---|---|
| `Chat_step failed ... charmap` | Windows console encoding | Set `PYTHONIOENCODING=utf-8` and reconfigure stdout/stderr |
| `LLM Call Failed: HTTPConnectionPool(127.0.0.1:8080)` | Backend not running or duplicate server start | Restart tray; ensure only one `llama-server` is running |
| Chat stuck after `calling_8080` (no `QUEUE_CHAT_END`) | Local server stopped sending stream data | Agent now times out after 5 min and ends the step. If it keeps happening, check model load and RAM; see **Local Server: Request Timeouts** in `docs/llm/API_INTEGRATION.md`. |
| Prompt is processed (`QUEUE_CHAT_END`) but Web UI shows only loader/no live answer | Stale `_server_loop` in `web_interface` (pushes are dropped with `PUSH_DROP _server_loop is NOT RUNNING`) | `WebInterfaceManager.connect()` re-binds to the active loop, invalid loop refs are auto-cleared, and a HTTP fallback push path is used when no loop is available. Subprocess bridge events are also posted to the internal non-SSL API channel (`127.0.0.1:8005`) when TLS is enabled. |
| Messages appear in CLI only | Headless agent not running | Ensure tray starts `run_headless_agent()` thread |
| `QUEUE_ADD` in queue.log but no `QUEUE_GET` | Worker thread crashed during startup (e.g. `UnboundLocalError` from local re-import shadowing a module-level import) | Check `headless.log` for `[FATAL]` traceback; avoid `from ... import X` inside `run_headless_agent()` when `X` is already imported at module level |
| Attachments are indexed (`ATTACH_INDEX`) but agent says it cannot see document | `runtime_state` overwrite during `SessionManager.save(sync_state=True)` dropped `sidebar_documents` | Preserve non-provider runtime keys when syncing provider snapshot (merge instead of replace) |
| WebUI shows only system logs | `agent_message_update` filtered by session | Fix session sync and auto-load `history_update` |
| Sub-agent window never appears | No `subagent_update` emitted | Send periodic sub-agent status updates from headless loop |
| `False promise detected` loop in API mode | Tool calls missing function name in stream | Drop invalid tool calls; do not retry |
| Tool cards stuck on "Executing..." | `_emit_to_web_ui()` gated by `VAF_THINKING_MODE` env var, blocking tool events for active sessions during background thinking | Removed `_emit_to_web_ui()` gate from tool start/end in `agent.py`; events now always emit via `broadcast_to_session` |
| Tool cards show "Executing..." after reload | Backend `history_update` did not include `toolName`/`toolId`/`toolStatus` for tool messages | Fixed `web_server.py` to read top-level `name`/`tool_call_id` keys and default `toolStatus` to `"completed"` |
| Messages in wrong order after reload | Timestamp sort mixed client/server times; orphan cache messages misplaced | Removed timestamp sort; added turn-based role sort as safety net |
| Automation result not shown in WebUI (only created files appear) | Result delivered via streaming `agent_message_update`, which overwrote/dropped the bubble when no live turn was active; the `session_unread` fallback was filtered out by the cross-session guard | Deliver results via `agent_message_append` (always appends a new bubble); allow `session_unread` and `agent_message_append` through the cross-session filter |
| Chat empty after app start until switching sessions and back | `session_list` auto-select sent `load_session` via the `ws` STATE variable, which the `onmessage` closure captured as `null` on the first connect — the send silently did nothing | Handlers send via `wsSocketRef.current` (set before `onopen`) instead of the captured state |
| LAN WebUI keeps reconnecting / connection lost on a heavy session | Oversized `history_update` frame (inline base64 images) exceeded the proxy relay's default 1 MB per-frame cap (`PayloadTooBig`), tearing down the relay | Proxy relay connects to the backend with `max_size=None`; backend bounded by uvicorn `ws_max_size` |

## Message Ordering (history_update)

When the frontend receives `history_update`, it applies a multi-step pipeline to produce correct message order:

1. **Idle reload fast path (source of truth)**: If the session is not active (`isActive=false`), the frontend uses backend `history_update` as-is and skips cache/orphan merge. This avoids stale client fragments and improves reload stability.
2. **Server order (`_order`)**: For active-session merge paths, server messages are indexed by response order.
3. **Restricted orphan merge**: Only cache messages that are safe to re-inject are considered. Assistant/user cache orphans are not re-injected to prevent out-of-turn fragments after reload.
4. **Reorder + dedup**: System/tool messages are normalized into turn order and duplicates are removed.
5. **Thinking merge (parser-based)**: Thinking-only assistant orphans are detected via `parseContent()` (works for complete and incomplete think blocks) and merged into adjacent answer assistant messages.
6. **Turn-based role sort (safety net)**: Within each turn (between user messages), messages are sorted by role weight: `system (0) → tool (1) → assistant (2)`, using stable ordering within the same role.

**Important**: The pipeline does NOT sort by timestamp. Timestamp-based sorting was removed because network clients have different client-side vs. server-side timestamps, causing messages to appear in the wrong order (e.g., system messages above user prompts).

## WebSocket Connection Role Storage

When a WebSocket connection is established, the user's role from the JWT token is stored via `manager.set_connection_user(websocket, user_id, username=..., role=...)`. The `get_config` and `save_config` handlers use `manager.get_connection_user_role(websocket)` to determine admin status, in addition to the legacy scope-based check. This ensures that the admin role from the JWT is respected even if the scope ID does not match the local admin scope. The session ownership gate also reads this stored role, treating role `admin` (or a connection scope equal to the local-admin scope) as admin, so the desktop/admin connection is not locked out of session commands even when its connection scope is `None`.

## WebSocket Frame Sizing

The proxy WS relay (`vaf/network/https_proxy.py`) connects to the backend with `max_size=None`.
A `history_update` frame can embed inline base64 images, which pushes a single frame past the
`websockets` library default of 1 MB per frame. Without `max_size=None`, the relay raises
`PayloadTooBig` and tears down the connection, so a LAN client reconnect-loops with "connection
lost" on a heavy session. The backend side remains bounded by uvicorn's `ws_max_size`
(`vaf/core/web_server.py`), so frames are not unbounded — the relay just stops capping below the
backend's limit.

## Key Files

- `vaf/core/web_server.py` (WebSocket server & routing)
- `vaf/core/web_interface.py` (broadcast manager)
- `vaf/core/headless_runner.py` (WebUI agent loop)
- `vaf/network/https_proxy.py` (integrated HTTPS reverse proxy with connection pooling)
- `web/app/page.tsx` (frontend session filtering, message ordering & render)

*Last updated: 2026-06-02*
