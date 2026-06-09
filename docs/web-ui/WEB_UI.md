# Web UI Documentation

## Overview

The VAF Web UI provides a browser-based interface for interacting with the Veyllo Agentic Framework. It offers real-time communication, session management, and visual feedback for agent operations.

### 3. System Tray (New)
The Web UI can be launched directly from the **VAF System Tray App** (Menu -> Open WebUI). The server runs in the background.

## Architecture

### Backend (Python)

**Location**: `vaf/core/web_server.py`, `vaf/core/web_interface.py`

The backend consists of two main components:

#### 1. FastAPI Server (`web_server.py`)

- **Framework**: FastAPI with Uvicorn
- **Port**: 8001 (default)
- **Protocol**: WebSocket + REST
- **CORS**: Enabled for local development

**Endpoints**:
- `GET /`: Health check endpoint
- `WebSocket /ws`: Real-time bidirectional communication

#### 2. Interface Manager (`web_interface.py`)

Singleton pattern manager that:
- Maintains WebSocket connections
- Manages message queues between CLI and Web UI
- Broadcasts updates to all connected clients
- Handles thread-safe communication between main thread (CLI) and server thread

**Key Methods**:
- `connect(websocket)`: Accept new WebSocket connection
- `disconnect(websocket)`: Remove connection
- `broadcast(message)`: Send message to all clients
- `broadcast_to_session(session_id, message)`: Send only to connections subscribed to that session
- `broadcast_to_user(user_id, message)`: Send only to connections for that user (e.g. session list refresh)
- `push_update(data)`: Thread-safe update from CLI to Web UI
- `register_agent(agent)`: Link agent instance for control

### Frontend (Next.js)

**Location**: `web/`

**Technology Stack**:
- Next.js 16 (React 18)
- TypeScript
- Tailwind CSS
- Lucide React (icons)
- WebSocket API (native browser WebSocket)
- next-intl for UI localization (see [I18N.md](../platform/I18N.md))

**Main Component**: `app/page.tsx`

## Features

### 1. Real-Time Chat Interface

- **Message Display**: User and assistant messages with distinct styling. Messages are shown in conversation order (oldest first). On session reload (`history_update`), messages are sorted by server order (`_order` index), **not** by timestamp — this prevents ordering issues for network clients where client-side and server-side timestamps differ. A final turn-based role sort ensures correct ordering within each turn: system → tool → assistant.
- **Streaming Responses**: Live updates as agent generates responses. When the agent uses a tool, the text *after* the tool is shown in a **separate** assistant bubble (so you see: first answer → tool card → follow-up answer), instead of one bubble that keeps updating.
- **Thinking Process**: Collapsible accordion showing agent's reasoning (`<think>` blocks)
- **System Steps**: Timeline visualization of agent workflow steps

### 2. Session Management

- **Create Sessions**: New chat sessions via sidebar
- **Load Sessions**: Switch between existing conversations
- **Delete Sessions**: Remove unwanted sessions
- **Auto-Save**: Sessions persist automatically
- **Session List**: Displays recent sessions for the current user only (filtered by `user_scope_id`). Loading a session checks ownership; other users' sessions are not accessible.
- **Thinking mode:** When the agent runs in the background (idle thinking), its output is appended to your main chat session (user-scoped default, e.g. `web-default-<scope>`) so you see it in the same conversation. Legacy thinking-only sessions are hidden from the sidebar. The message input stays available so you can reply. See [Thinking-Mode.md](../agents/Thinking-Mode.md).

### 3. Status Indicators

- **Connection Status**: Visual indicator (green/red) in header
- **Local Model Idle**: Shows `Idle` when the local model is unloaded and waiting for a prompt
- **Loading States**: Animated dots during agent processing
- **Workflow Steps**: Real-time display of Router, Workflow, System, and Info events. The **Router** step shows which tools were selected for the turn (e.g. `Router: LLM-based: list_calendar_events` or `Router: Script-based: web_search`), so you can see when and which tools the agent is using.
- **Inline Tool Status**: Visual cards for running/completed tools directly in the chat stream. Tool events (`tool_update`) are always emitted regardless of background thinking mode — they are no longer gated by `_emit_to_web_ui()` to avoid race conditions with the `VAF_THINKING_MODE` environment variable. After page reload, tool cards show the correct status (`completed`/`error`) from the `toolStatus` field in `history_update` messages. A tool that returns **without running** — e.g. a state-changing tool gated by the plan requirement (`[PLAN REQUIRED] …`) — is shown as a **non-success** (red), not a green check, so a gated call is never mistaken for a completed one (the agent did **not**, for example, actually save a memory).

### 4. Sub-Agent Panel & Tool Cards

- **Docked Panel**: Sub-agent output renders in a right-side panel that slides in/out.
- **Auto-Open**: The panel opens when a sub-agent starts (via tool events/logs).
- **Tool Card Toggle**: Clicking a sub-agent tool card expands details and opens the panel; collapsing the card closes the panel.
- **Auto-Close Guard**: The panel does not auto-close while any sub-agent step is still running.

### 5. Message Features

**Thinking Details**:
- Extracted from `<think>...</think>` tags
- Collapsible accordion UI
- Monospace font for technical content

**Long reply collapse**:
- A bot answer longer than ~800 chars collapses to a ~300-char preview with a "Show full response" toggle — but **only once a newer user message exists** (i.e. it is a *past* answer). The current/streaming answer and short replies are never collapsed.
- Collapse is computed at **render time** from the message's position + length; only the user's manual *expand* choices are stored, keyed by the stable message **timestamp**. It is deliberately **not** a set of array indices: those shifted whenever a message was removed (`clear_last_assistant`, dedup) and collapsed the wrong bubble (tiny replies collapsed, long ones stayed open, the streaming reply collapsed mid-stream).

**System Steps**:
- Timeline-style visualization
- Icons for different step types (Router, Workflow, Safety). Router steps show the selected tool name(s) (LLM-based or script-based selection; see [TOOL_ROUTER_ARCHITECTURE.md](../agents/TOOL_ROUTER_ARCHITECTURE.md)).
- Automatic filtering of redundant messages

**Wake / system-activity messages (`kind`)** — *extension point*:
A proactive backend message can carry a `kind` tag: `emit_agent_message_append(content, session_id, role, kind="…")` (`web_interface.py`). The Web UI then renders that message as its own **agent-style row** (avatar + speech bubble) with a kind-specific look, instead of a plain user/assistant bubble. This is how a fired **timer** appears, and it is the hook for other proactive/background activity.

- **Frontend:** `Message.kind` carries the tag; the `agent_message_append` handler stores it; the message render loop has an `_isWake` branch (matched by `msg.kind`, or by the `⏰ Timer fired` content prefix when reloaded from history) that draws the wake row **before** the normal role branches. The trigger is sent as `role="user"` so it still creates a bubble boundary (the agent's reply lands in its own bubble), but `kind` overrides how it is drawn. The row mirrors the agent layout (`justify-center` → `max-w-[85%]` avatar + bubble) so the avatar aligns with the agent's, shows only the user's note (the internal "Act on it…" framing is stripped), and carries the same timestamp as the agent messages. See `web/app/page.tsx` (`_isWake`).
- **Timer — two states (`_wakeDone`):** while the agent is still handling the fired timer it shows an **active** look (the real agent avatar + an amber clock **badge** in the corner + an amber bubble — "look here"); once the agent has replied (a completed assistant message follows and generation has stopped, or a newer user turn exists) it **settles** to a quiet look (neutral dim avatar, no badge, neutral bubble, amber only in the small "TIMER" label). On reload a past timer is already in the settled state.
- **Extending it:** to add a new activity (e.g. `kind="thinking"` or `kind="background"`), emit it from the backend with that `kind` and add a branch in the `_isWake` render. For kind-specific avatars, `AgentAvatar` takes an optional `tint={{ body, dot }}` (added for this) — e.g. the intended **purple agent-avatar** for `thinking`. See [AgentAvatar.md](AgentAvatar.md).

### 6. Settings

**Admin-only tabs:** The following Settings tabs are visible only to admin users: **General**, **AI & Model**, **Advanced**, and **Local Network**. Non-admin users are automatically redirected to the first allowed tab if they land on an admin-only tab. Both the sidebar filter (`adminOnly` flag in the CATEGORIES array) and content rendering guards (`currentUser?.role === 'admin'`) enforce this. The admin role is determined from the stored JWT role on the WebSocket connection.

Under **Settings → Interface** you can set:

- **Language** — UI language (e.g. German, English). Stored in the browser only (`localStorage`). See [I18N.md](../platform/I18N.md) for how translations and new languages are managed.
- **Date & Time** — Timezone, date format, and time format (24h/12h). Stored in your user identity and used in the system prompt and when the agent shows dates and times.

**Settings → Advanced:** Sub-Agents (provider, timeout) and **Thinker (background)**. For the Thinker you can set a separate provider and model (e.g. a cheaper local or API model) for idle background runs; the model dropdown is shown only when a non-inherit provider is selected. See [Thinking-Mode.md](../agents/Thinking-Mode.md). The Advanced tab also has management panels (each a row that opens a sub-modal): **Tools**, **MCP**, and **Workflows**. The **MCP** row ("N connected / M configured") opens a panel to add, edit, or remove MCP servers (admin only) — see [MCP_INTEGRATION.md](../agents/MCP_INTEGRATION.md).

**Settings → AI — Download model from Hugging Face:** This option is only available when the **local** model provider is selected. You can paste a Hugging Face repo id that **contains GGUF files** (e.g. `seanbailey518/Nanbeige4.1-3B-GGUF` or `Edge-Quant/Nanbeige4.1-3B-Q8_0-GGUF`). Base-model repos (safetensors only) do not offer GGUF; if you see "No GGUF files found", search on Hugging Face for "<model name> GGUF" and use that repo. Click Download to open the confirmation dialog. A confirmation dialog opens with the model card (README) and a list of GGUF files to choose from; after you confirm, the download starts. Progress (percentage, bytes, speed) and a Cancel button are shown in Settings. If you close Settings while a download is in progress, the download continues in the background; a compact progress indicator appears on the main chat page so you can see status and cancel from there. When the download finishes, a toast shows success or error and the model list refreshes.

**Settings → Connections:** Manage external integrations (Email, Calendar, Cloud, Discord, Telegram, WhatsApp, GitHub, etc.). A search field at the top filters the list by name or category (e.g. type “GitHub” or “Kalender” to jump to that connection). The **GitHub** category opens a dashboard with a rights-overview strip (toggle read/write per account), connected accounts, an event timeline (agent actions, newest first), and a repositories panel (repos for the selected account with links to GitHub). The **Calendar** category shows Google Calendar and Microsoft Outlook; they use the same OAuth connection as Email (connect Gmail or Outlook under Email first). In the **Mail dashboard**, suspicious emails are marked with a warning icon and tooltip reason; these remain visible to the user, but are hidden from agent mail tools by default for safety. When a calendar is connected, the settings (gear) icon opens the **Calendar Dashboard**: left sidebar lists connected accounts with links to open Google Calendar or Outlook in the browser; main area shows upcoming events from the API with selectable range and refresh. See [CONNECTIONS.md](../integrations/CONNECTIONS.md) and [CALENDAR_INTEGRATION.md](../integrations/CALENDAR_INTEGRATION.md).

**Settings → Automations:** View scheduled automations (user-scoped when multi-user is used; root/global automations such as "Daily calendar check" are also shown so the list matches the agent's `list_automations` tool). To create one manually: click **Create New** (or use the **Automation** entry in the sidebar footer) to open the calendar; choose month, then day, then an hour slot. The sidebar footer also includes **Notifications** (opens the Notifications popup) and **Settings**. Opening the Automation popup (footer) also triggers the calendar ensure-daily-check API when a calendar is connected, so the Daily calendar check appears in the list without opening Settings first. A popup lets you set repeat (once, daily, weekly, monthly, hourly), time, a detailed prompt, and an optional name. Creation is sent via WebSocket (`create_automation`); the list refreshes on success. The agent can also create automations via the `create_automation` tool in chat.

The same automation calendar includes a **per-user planner**:

- **To-do list** (left column): User and agent can add items via an "Add to-do" popup (text and optional due date). Each item has a done checkbox (updates via WebSocket) and a delete button. Data is stored per user and loaded when the calendar opens.
- **Notes** (bottom section, fixed height): User and agent can add notes via an "Add note" popup (optional title, content). Each note shows created-at and can be deleted. The list scrolls inside a fixed-height area so the layout does not grow.

Planner data is loaded with `get_automation_notes` and `get_automation_todos` when the calendar is opened (from the footer or from Settings). Create/update/delete use WebSocket messages; the UI updates optimistically where applicable. The agent can manage the same data via tools: `add_automation_note`, `add_automation_todo`, `list_automation_notes`, `list_automation_todos`, `delete_automation_note`, `delete_automation_todo`.

**Logs window (admin only):** Clicking **Logs** in the sidebar opens a split-pane log viewer (same window size as the Automation window). It is only visible to users with the `admin` role.

The left sidebar has three sections:

- **Timeline** (top) — the agent tool-use timeline (see below). The default view when opening the window.
- **Activity** — the notification feed: thinking-mode results, automation run results (success/error + summary), handoff decisions, and channel replies. Items expand to show the full summary. Loaded from `GET /api/notifications`; new items pushed via WebSocket.
- **Log Files** (collapsible) — lists every `.log` file in the VAF log directory (`~/.vaf/logs/`), grouped by domain (rag, memory, backend, prompt, headless, attach, tool_use, …) with a colour dot per domain. Collapsed by default; only meaningful when **Debug Logs** is enabled in Settings → Advanced.

#### Timeline view

The Timeline is a **horizontal scrubber** modelled after video-editing software. It is split into two vertical sections:

```
┌────────────────────────────────────────────────────────────────────────┐
│  [2/5  Activity panel]  │  [3/5  ReactFlow canvas]                    │
├─────────────────────────┴────────────────────────────────────────────── │
│  [Lane labels]  ████████  ███  ██████████  ████   (timeline bars)      │
│                 ←── older                      newer ──→               │
└────────────────────────────────────────────────────────────────────────┘
```

**Bottom row — timeline bars** (left-to-right = time, lanes = tool category):
- Each bar represents one completed tool call; width = duration.
- Colour = category: blue (web/search), green (files), purple (memory), orange (code/bash), pink (messaging), indigo (sub-agents), teal (tool learning — Whare Wananga training runs).
- **Ruler** at the top of the bar area shows time ticks; a **red "now" line** marks the live position (today only).
- **Mouse wheel** scrolls horizontally. **Ctrl + scroll** zooms in/out. `+`/`−` buttons in the top-right corner also zoom.
- Timeline is anchored to the bottom; lanes grow upward as more categories appear.

**Cursor (playhead):**
- Click anywhere on the bar area to place a **thick black cursor line** with a time badge in the ruler. This marks the inspection point.
- The dashed line follows the mouse as a hover indicator. The red line = live "now".
- Clicking the same position again removes the cursor.
- **`▶ live`** button (appears when scrolled away from the right edge) jumps the cursor to the current time and re-enables auto-scroll.

**Top-left — Activity panel (2/5 width):**
- Empty when no cursor is set ("Click on the timeline to inspect that moment").
- When a cursor is placed, shows all events whose bars are **touched by the cursor line** (start ≤ cursor ≤ start+duration). Point events use ±15 s tolerance.
- Each row: coloured left stripe, tool name, duration, status, args preview (`→`), result preview (`←`).
- **Live mode** (Live toggle active): panel refreshes every 3 s to show what the agent is doing right now.

**Top-right — ReactFlow canvas (3/5 width):**
- Populated when cursor is placed; empty otherwise.
- Shows the same events as the Activity panel as **ProcessNodes**: animated cards with tool name, duration bar, status icon, args snippet. Running nodes pulse.
- Layout: events sorted chronologically left→right, one row per lane, minimum 16 px gap — no overlaps.
- Click a node to **select** it (coloured glow ring); click empty canvas space to deselect.
- **Node detail window** (window-in-window in the Activity panel): clicking a node opens a floating panel over the Activity panel showing full event details and **real log lines** fetched from the server (see Log Context API below). Close with the red ✕ button.

A **date selector** in the header lets you switch between days. A **hash-chain integrity badge** (green shield = intact, red shield = tampered/deleted event) is shown whenever the chain can be verified.

**Live toggle** auto-refreshes the timeline events every 5 s and enables the live cursor mode. The manual Refresh button also applies.

#### Hash-chain tamper detection

Every timeline event is written to `timeline_YYYY-MM-DD.jsonl` by `log_timeline_event()` in `vaf/core/log_helper.py`. Each event object includes:

```
{
  "ts":        "2026-05-28T14:23:01.123",
  "type":      "tool_start" | "tool_end" | "subagent_start",
  "tool":      "<name>",
  "call_id":   "<uuid>",
  "args":      "<preview>",          // tool_start only
  "status":    "ok" | "error",       // tool_end only
  "duration_s": 1.23,                // tool_end only
  "result":    "<preview>",          // tool_end only
  "prev_hash": "<sha256 of previous event>",
  "hash":      "<sha256 of this event>"
}
```

The hash is SHA-256 of the canonical JSON of the event (all fields except `hash` itself, keys sorted). `prev_hash` is `"GENESIS"` for the first event of the day. This forms a forward-linked chain: deleting or modifying any event breaks all subsequent hashes, which the API detects and surfaces as `chain_ok: false`.

#### API endpoints

All timeline and log endpoints require the `admin` role:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/logs` | List all `.log` files with metadata |
| `GET` | `/api/logs/{filename}?tail=500` | Last N lines of a log file |
| `GET` | `/api/logs/timeline/dates` | Dates for which JSONL files exist |
| `GET` | `/api/logs/timeline/events?date=YYYY-MM-DD&merge=true` | Merged timeline events + `chain_ok` |
| `GET` | `/api/logs/timeline/context` | Event-specific log lines (see below) |

The `merge=true` parameter (default) pairs `tool_start` + `tool_end` events by `call_id` into a single merged record. Still-running tools (no matching `tool_end`) appear with `status: "running"`.

**Log Context API** (`GET /api/logs/timeline/context`) returns log entries specific to the clicked event rather than all lines in a time window:

| Query param | Description |
|-------------|-------------|
| `ts` | ISO timestamp of the event (required) |
| `type` | Event type: `thinking_run`, `tool_end`, `tool_start`, … |
| `run_id` | For `thinking_run`: extracts the full multi-line block from `vaf_think_*.log` |
| `call_id` | For tool events: matched against the JSONL timeline entry |
| `tool` | Tool name: filters `tool_use_*.log` lines to this tool |
| `session` | Session ID for additional filtering |
| `window_s` | Time window in seconds (default 30, max 120) |

Response always includes the matching JSONL entry (full call_id/session/scope/duration/result context). For `thinking_run` + `run_id`, the full conversation block (user, assistant, tool turns) is returned. Files larger than 2 MB are skipped.

Path-traversal is prevented on all file endpoints: the resolved path is checked to be a direct child of the log directory (no symlink escape).

#### Log gating

All debug log functions in `vaf/core/log_helper.py` (`append_domain_log`, `append_domain_log_always`, `log_attachment`, `log_thinking_run`, `log_telegram_reply`, `log_discord_reply`, `log_whatsapp_*`, `log_timeline_event`) respect the `debug_logs_enabled` setting — no files are written when it is off.

#### Terminal log viewer

Selecting a domain under **Log Files** opens a **terminal-style viewer** (dark `#0d1117` background, monospace font, blue timestamps, line numbers). Controls:
- **Live toggle** — auto-refreshes every 5 s.
- **Refresh button** — manual reload.
- **Filter input** — client-side line filter with match highlighting.
- **Auto-scroll checkbox** — keeps the view pinned to the bottom.

The viewer shows the last 500 lines of the selected file. The header shows the filename and, when truncated, how many lines are shown vs. total.

### 7. Code Viewer

The Code Viewer is a Monaco-based (VS Code engine) code editor in the right panel. It opens automatically when the agent creates a code file (`.py`, `.js`, `.ts`, `.html`, `.css`, etc.) or when you drag a code file into the chat.

**Features:** Syntax highlighting for 40+ languages, live refresh every 2 s while the agent is generating (shown by a pulsing **LIVE** badge), in-browser editing, and save back to disk via `POST /api/file/save` (Ctrl+S supported). The header shows the detected language, filename, save state, and last-updated time. The footer shows the full file path and unsaved-changes indicator.

**Agent context:** While the Code Viewer is open, the full file content (up to 30 000 chars) is sent with every chat message via `codeViewerFile`. The backend stores it in `session.runtime_state["code_viewer_file"]` and the headless runner injects it into `effective_input` as a numbered-line block (`--- CURRENTLY OPEN IN CODE VIEWER: <name> ---`) before calling the agent. This means the file content is never stored in the message history (avoiding raw-text bleed into the chat UI on reload). Content comes from the already-loaded viewer state, so it works for both server-path files and browser-dragged files. A small chip (filename + line count) appears on the sent user message to indicate which file was attached.

**File routing:** `.html`/`.htm` files created by the agent open in the **HTML Viewer** (see §7a below). Other code files open in the Code Viewer. Documents open in the Document Editor.

### 7a. HTML Viewer

A dedicated viewer for HTML files (reports, generated web pages). Opens automatically when the agent creates a `.html` or `.htm` file — shown as an **orange chip** in the chat (distinct from violet code chips and blue download chips).

**Features:**
- **Preview mode** (default): native iframe render with `allow-scripts allow-forms` — JavaScript-heavy reports (Chart.js, D3, etc.) work correctly.
- **Source mode**: Monaco editor (read-only, HTML syntax highlighting) — toggle with the `Preview / Source` buttons in the header.
- **Download button**: saves the file locally as `text/html`.
- Loads file content via `/api/file?path=…` if only a path is given (no pre-loaded content).

### 8. Document Editor

The Document Editor is a rich-text editor in the right panel (dock or overlay). It supports opening files (HTML, DOCX, etc.), editing, and exporting.

**Editor split:** The Web UI now has two editor paths:

- **Native DOCX editor** for `.docx` files. This path is model-driven and uses a native `DOCX -> NativeDocxDocument -> DOCX` flow instead of the old HTML roundtrip.
- **Legacy HTML editor** for HTML and other non-DOCX editor flows. This path still uses the iframe/contentEditable editor.

See also: [DOCUMENT_EDITOR_NATIVE_DOCX.md](../documents/DOCUMENT_EDITOR_NATIVE_DOCX.md)

**Layout and behaviour:** The editor keeps an A4-like page layout in the right panel. Per-session state stores the open file plus unsaved editor state. For DOCX files this includes the native document model; for legacy flows it includes HTML/text content.

**Agent context:** When the editor is open, its plain-text content is sent with each chat message. The backend prepends it as `--- CURRENT DOCUMENT (Editor): <title> ---` so the agent sees the current document. For native DOCX sessions this plain text is derived from the native document model, not from browser HTML. You can select text in the editor (e.g. placeholders); the selection is added as a chip and sent with the message. The agent can replace that range via the `replace_editor_selection` tool when a marked region exists. Without a manual marking, the agent can still rewrite a specific sentence or paragraph from the open editor document via `replace_editor_text`, which targets an exact snippet from the current editor content.  
For Document Viewer attachments (paperclip), the backend uses a **session-scoped attachment retrieval lane** (scoped by `session_id` + `user_scope_id`, TTL-based) and injects a "document context active" block plus **top-k relevant snippets** into each turn. This keeps context stable for large documents and avoids prepending full attachment text every message.  
If you want durable long-term memory from current attachments, use `learn_attached_knowledge` (requires explicit confirmation).

**Indexing-status indicator:** while an attachment is being indexed into the retrieval lane, the Document Viewer header status reflects it — an amber pulsing dot with "Indexiere…" during indexing, green "Bereit" when ready, red "Fehler" on error (driven by `attachment_indexing` / `attachment_indexed` / `attachment_index_error` WebSocket events). When `learn_attached_knowledge` learns a document, the viewer slowly walks through all pages (~2s per page) until learning finishes, so the long-running operation is visibly in progress.

**Workflow behaviour:** If the message contains the editor document block (`CURRENT DOCUMENT (Editor)`), workflow matching is skipped so the agent uses tools (e.g. `replace_editor_selection`) instead of starting a workflow.

**DOCX behaviour:** The native DOCX editor loads `.docx` through dedicated backend endpoints and saves back to `.docx` from the same native model. This avoids the old lossy `DOCX -> HTML -> DOCX` save path for DOCX editing.

**Preview and PDF:** Gotenberg/LibreOffice remains the high-fidelity Office-to-PDF solution for the Document Viewer and future preview workflows, but it is not the mutable editing engine for the native DOCX editor. The editor's immediate PDF export is generated from the current editor preview state.

**UI:** Closing the editor (X) shows a browser confirm dialog.

**Drafts from agent:** When you ask in the Web UI for the agent to write or compose text (e.g. *"Schreib mir einen Text …"*, *"Verfasse …"*, *"Write me a text …"*), the agent’s reply is also opened in the **Document Editor** as a draft. The draft is saved under the session’s data folder (`data_dir/drafts/<session_id>/entwurf.md`). You can edit the text there, improve it, and use **Save** to overwrite the draft or **Download HTML** to export it. This only applies to Web UI prompts (not e.g. Telegram), and only when the reply is substantial (after stripping `<think>` blocks).

## Local Model Idle Behavior

When the provider is `local`, the tray process only loads the model on real activity (prompt/CLI heartbeat). If there are no active WebUI WebSocket connections for 15 seconds, the model is unloaded from VRAM unless persistence is enabled.

## Wechsel zwischen lokalem Modell und API

Beim Wechsel des Providers (Local ↔ API) in den Einstellungen erscheint ein zentrales Overlay **„Changing model“** für etwa 5 Sekunden; danach lädt die Seite neu. Gleichzeitig entlädt der Tray bei Wechsel von Local zu API das Modell aus dem VRAM (llama-Server wird beendet) bzw. lädt bei Wechsel von API zu Local das Modell in den VRAM. Details: [MODEL_AND_PROVIDER_SWITCHING.md](../llm/MODEL_AND_PROVIDER_SWITCHING.md).

## Local HTTP Backend Reuse

The local LLM runs as a single HTTP backend on `127.0.0.1:8080`. When a prompt arrives, VAF first checks `/health` and reuses the existing backend if it is already running (or still loading). This prevents duplicate `llama-server` processes and keeps WebUI and CLI on the same server instance.

## WebSocket Protocol

### Client → Server Messages

```json
{
  "type": "chat",
  "content": "User message text",
  "sessionId": "uuid",
  "sidebarDocuments": [],
  "editorDocument": { "name": "Document title", "content": "Plain text of editor body" },
  "editorSelections": [{ "start": 0, "end": 10, "text": "selected text" }]
}
```

- `sessionId` is required. Optional: `sidebarDocuments` (Document Viewer attachments), `editorDocument` (when Document Editor is open; plain text only, derived from the current editor state; for native DOCX sessions this is flattened from the native model), `editorSelections` (marked ranges in the editor for `replace_editor_selection`), `codeViewerFile` (when Code Viewer is open; `{ name, path, content }` of the currently displayed file — sent automatically on every message so the agent can answer line-specific questions).

```json
{
  "type": "new_session"
}
```

```json
{
  "type": "load_session",
  "id": "session-uuid"
}
```

```json
{
  "type": "delete_session",
  "id": "session-uuid"
}
```

```json
{
  "type": "get_sessions"
}
```

### Server → Client Messages

```json
{
  "type": "session_list",
  "sessions": [
    {"id": "uuid", "title": "Session Name", "date": "ISO timestamp"}
  ]
}
```

```json
{
  "type": "history_update",
  "messages": [
    {"role": "user|assistant|system", "content": "text", "timestamp": 1234567890}
  ],
  "sessionId": "uuid"
}
```

```json
{
  "type": "agent_message_update",
  "role": "assistant",
  "content": "Partial or complete response",
  "sessionId": "uuid"
}
```

```json
{
  "type": "new_log",
  "entry": {
    "timestamp": "ISO timestamp",
    "message": "Log message",
    "level": "info|warning|error",
    "source": "System|Agent|Router|Step X/Y|Info"
  }
}
```

```json
{
  "type": "tool_update",
  "subType": "start|end|error",
  "toolId": "unique-id",
  "name": "tool_name",
  "data": "arguments (start) or result (end)",
  "timestamp": "ISO timestamp",
  "sessionId": "uuid"
}
```

```json
{
  "type": "editor_apply_edit",
  "sessionId": "uuid",
  "selectionIndex": 0,
  "newText": "replacement text",
  "start": 0,
  "end": 10
}
```
Sent when the agent calls `replace_editor_selection` or when a text-targeted editor edit resolves to a concrete character range. The frontend replaces the character range `[start, end]` in the Document Editor with `newText` and removes the matching selection chip if one existed. For native DOCX sessions the edit is applied to the native document model; for legacy sessions it is still applied to HTML/text content.

```json
{
  "type": "rag_results",
  "query": "The search query used",
  "sources": [
    {
      "text": "Snippet text...",
      "full_text": "Full text...",
      "score": 0.85,
      "metadata": {"source": "file.txt", "title": "My Note", "tags": ["work", "important"]}
    }
  ]
}
```

## Configuration

### Enabling/Disabling Web UI

**Via CLI Flag**:
```bash
vaf run --no-web  # Disable Web UI
vaf run --web     # Enable Web UI (default)
```

**Via Config File** (`config.json` in the VAF app directory):
```json
{
  "web_ui_enabled": true
}
```

### Tray Autostart

Use `tray_autostart` to control whether the tray app starts when the OS logs in:

```json
{
  "tray_autostart": false
}
```

### Sub-Agent Terminals (Global Setting)

`sub_agents_in_separate_terminals` applies to CLI and workflow execution. In the WebUI,
sub-agents still run headless and stream output to the docked panel even when this
setting is enabled.

### Web Search API Keys

Under Settings → General, the section **Web Search (API)** lets you set optional keys for web search:

- **Brave Search API Key** – Used first when set (from [Brave API dashboard](https://api-dashboard.search.brave.com/app/keys)).
- **Google Search API Key** and **Google Search Engine ID (cx)** – Used if both are set (Custom Search API and a Programmable Search Engine that searches the entire web).

If none are set, the tool uses the default path (scrape Google, then DuckDuckGo). Stored in `config.json` as `api_key_brave_search`, `api_key_google_search`, and `google_search_engine_id`.

### Port Configuration

**Backend Port**: Hardcoded to 8001 in `web_server.py`
**Frontend Port**: Auto-detected (starts at 3000, increments if occupied)

**API routing**: Next.js proxies `/api/*` to the backend via the catch-all API route (`app/api/[...path]/route.ts`), which forwards to `http://127.0.0.1:8005` (internal HTTP channel). Next.js also rewrites `/sounds/*` to the backend for notification sound files. Mail dashboard and other Web UI features use the same proxy path (`/api/...`) so frontend calls stay same-origin while backend transport stays internal.

**Local network (other devices):** Enable Local Network in Settings → Local Network (or run `vaf server on`). Network mode is TLS-only and always uses the integrated HTTPS proxy. Access is via `https://127.0.0.1:8443` (or `:443`), and from other devices via `https://<LAN-IP>:8443`. Use `vaf server status` to see active LAN URLs. The tray restarts services automatically when network settings change.

**Entry-point behavior (`3000` vs `8443`)**:
- `:3000` is the frontend runtime/dev entry point.
- `:8443` is the HTTPS proxy entry point (available when Local Network + TLS is enabled).
- Optional strict mode: set `VAF_ENFORCE_8443_ONLY=1` to redirect requests from `:3000` to `https://<host>:8443` via `web/proxy.ts`. Keep this disabled unless `:8443` is guaranteed to be active; otherwise users may see `ERR_CONNECTION_REFUSED`.

## Development

### Running Frontend Locally

```bash
cd web
npm install
npm run dev
```

### Building for Production

```bash
cd web
npm run build
npm start
```

### Frontend Dependencies

See `web/package.json`:
- `next`: 16.1.6
- `react`: ^18
- `lucide-react`: ^0.300.0 (icons)
- `tailwind-merge`: ^2.2.0 (utility merging)
- `clsx`: ^2.1.0 (conditional classes)

## Integration with CLI

The Web UI runs alongside the CLI interface:

1. **Startup**: When `vaf run` executes, it starts:
   - FastAPI backend on port 8001
   - Next.js frontend (auto-detected port)
   - Opens browser automatically

2. **Message Flow**:
   - User types in Web UI → WebSocket → `input_queue` → CLI processes
   - CLI generates response → `push_update()` → WebSocket → Web UI displays

3. **Session Sync**:
   - Web UI session changes → Commands to CLI → Agent reloads history
   - CLI saves messages → Broadcast to Web UI → UI updates

## UI Components

### Message Bubble

- **User**: Right-aligned, indigo background, rounded corners
- **Assistant**: Left-aligned, white background with border, includes bot icon. When the agent uses tools mid-turn, the reply is split: the part before the tool stays in one bubble, the part after the tool appears in a new bubble so tool usage and the follow-up answer are visible separately.
- **System**: Timeline-style with icons, minimal styling
- **Tool**: Card-style component showing tool name, arguments, status (running/completed), and result

### Tool Message

- **Status**: Dynamic border color (Blue=Running, Green=Success, Red=Error)
- **Collapsible**: Details (args/result) are collapsible to save space
- **Live Updates**: Updates in real-time as tool execution progresses

### Sidebar

- **Collapsed**: 64px width (icon only)
- **Expanded**: 288px width (on hover)
- **Smooth Transition**: 300ms duration

### Input Box

- **Features**: Attachment button, text input, voice input, send button; file chips and token stats above the form when relevant. When the document panel is open with attachments (Anhänge), **quote chips** appear above the input: any text selected in the panel is automatically added as a quoted snippet (colored by order: dark, orange, pink, blue, green). Chips show a red hover state; clicking a chip removes that quote only. Sent messages combine the typed input and all quote snippets (joined by blank lines).
- **Layout**: On a **new chat** (no messages), the input bar is shown **centered** in the viewport with a short welcome line (“How can I help you?”). After the first message is sent, the bar **animates** (≈500 ms) to its **fixed position at the bottom** and stays there for the rest of the conversation.
- **States**: Disabled during loading, focus ring on interaction.
- **Submit**: Enter key or click send button.

## Best Practices

### Performance

- **Bounded UI buffers**: UI keeps recent entries bounded for smooth rendering (for example, the sub-agent console panel keeps the latest 500 lines).
- **Session list paging**: Backend session list limit is configured server-side (currently 500 in `web_server.py`).
- **Auto-Scroll**: Smooth scroll to latest message
- **Debouncing**: WebSocket messages processed immediately (no artificial delay)

### Error Handling

- **Connection Loss**: Status indicator shows "disconnected"
- **Reconnection**: Manual page refresh required
- **Invalid Messages**: Silently caught and logged to console

### Security

- **CORS**: Restricted by middleware (localhost and private-LAN origin patterns), not unrestricted `*`.
- **Authentication**: The UI treats `GET /api/auth/me` as the source of truth for a valid session (JWT in `Authorization: Bearer` and/or `vaf_token` cookie, validated server-side). The dashboard redirects to `/login` when that call is not OK.
- **Next.js edge proxy (`web/proxy.ts`)**: Next.js 16 uses this file as the **Proxy** middleware (replacing the older `middleware.ts` name). It redirects browsers to `/login` only when there is **no** `vaf_token` cookie at all. It does **not** redirect `/login` → `/` based on cookie presence alone: a non-empty cookie can still hold an expired or revoked JWT, which would previously fight the client (401 on `/` but 307 from `/login`) and cause a redirect loop. After a successful `/me` on the login page, the app uses a **full navigation** to `/` so session and assets align with the HTTPS entry point.
- **`GET /api/auth/me` (backend)**: When both `Authorization: Bearer` and `vaf_token` are present, the server tries the **Bearer token first**, then the cookie, so a stale cookie cannot shadow a valid in-memory token.
- **Origin scope note**: Auth state can differ between `http://localhost:3000` and `https://localhost:8443` because cookies/storage are origin-scoped.

## Troubleshooting

### Web UI Not Starting

**Check**:
1. npm installed: `npm --version`
2. Backend/proxy status: `vaf server status`
3. Frontend process started and reachable (`http://localhost:3000` in local mode or `https://localhost:8443` in TLS/network mode)

**Logs**: `logs/web_debug.log`

### Server not reachable (full-screen message)

When the backend is down or unreachable, the Web UI shows a full-screen message: *"Server not reachable. Make sure VAF is running (e.g. \"vaf run\")."* with a **Try again** button. This appears when the initial auth/health check fails (for example VAF not started, proxy/backend not reachable, or TLS endpoint unavailable).

**What to do**: Start VAF (`vaf run` or open Web UI from the system tray). Ensure backend/proxy ports are free, then click **Try again** or refresh the page.

### `Bad Gateway` on `https://…:8443` (including `/login`)

The integrated HTTPS proxy forwards page requests to the Next.js process on `http://127.0.0.1:3000`. If the frontend is still starting or was restarted (tray log: stopping/starting frontend), the proxy returns **502** with body `Bad Gateway`. Wait until the tray reports the frontend ready on port 3000, then reload.

### WebSocket Connection Failed

**Causes**:
- Backend not running
- Port 8001 blocked by firewall
- Browser security restrictions

**Solution**: Check browser console for errors, verify backend is running

### Messages Not Appearing

**Causes**:
- Session mismatch (switched sessions during response)
- WebSocket disconnected
- Message filtering (e.g., "Agent Thinking..." is intentionally hidden)

**Solution**: Refresh page, check connection status

## Future Enhancements

Potential improvements:
- File upload support (Paperclip button currently placeholder)
- Multi-user support with authentication
- Persistent WebSocket reconnection
- Message search and filtering
- Export conversation history
- Dark mode toggle
- Mobile-responsive design improvements
