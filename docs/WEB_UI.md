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
- next-intl for UI localization (see [I18N.md](I18N.md))

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
- **Thinking mode:** When the agent runs in the background (idle thinking), its output is appended to your main chat session (user-scoped default, e.g. `web-default-<scope>`) so you see it in the same conversation. Legacy thinking-only sessions are hidden from the sidebar. The message input stays available so you can reply. See [Thinking-Mode.md](Thinking-Mode.md).

### 3. Status Indicators

- **Connection Status**: Visual indicator (green/red) in header
- **Local Model Idle**: Shows `Idle` when the local model is unloaded and waiting for a prompt
- **Loading States**: Animated dots during agent processing
- **Workflow Steps**: Real-time display of Router, Workflow, System, and Info events. The **Router** step shows which tools were selected for the turn (e.g. `Router: LLM-based: list_calendar_events` or `Router: Script-based: web_search`), so you can see when and which tools the agent is using.
- **Inline Tool Status**: Visual cards for running/completed tools directly in the chat stream. Tool events (`tool_update`) are always emitted regardless of background thinking mode — they are no longer gated by `_emit_to_web_ui()` to avoid race conditions with the `VAF_THINKING_MODE` environment variable. After page reload, tool cards show the correct status (`completed`/`error`) from the `toolStatus` field in `history_update` messages.

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

**System Steps**:
- Timeline-style visualization
- Icons for different step types (Router, Workflow, Safety). Router steps show the selected tool name(s) (LLM-based or script-based selection; see [TOOL_ROUTER_ARCHITECTURE.md](TOOL_ROUTER_ARCHITECTURE.md)).
- Automatic filtering of redundant messages

### 6. Settings

**Admin-only tabs:** The following Settings tabs are visible only to admin users: **General**, **AI & Model**, **Advanced**, and **Local Network**. Non-admin users are automatically redirected to the first allowed tab if they land on an admin-only tab. Both the sidebar filter (`adminOnly` flag in the CATEGORIES array) and content rendering guards (`currentUser?.role === 'admin'`) enforce this. The admin role is determined from the stored JWT role on the WebSocket connection.

Under **Settings → Interface** you can set:

- **Language** — UI language (e.g. German, English). Stored in the browser only (`localStorage`). See [I18N.md](I18N.md) for how translations and new languages are managed.
- **Date & Time** — Timezone, date format, and time format (24h/12h). Stored in your user identity and used in the system prompt and when the agent shows dates and times.

**Settings → Advanced:** Sub-Agents (provider, timeout) and **Thinker (background)**. For the Thinker you can set a separate provider and model (e.g. a cheaper local or API model) for idle background runs; the model dropdown is shown only when a non-inherit provider is selected. See [Thinking-Mode.md](Thinking-Mode.md).

**Settings → AI — Download model from Hugging Face:** This option is only available when the **local** model provider is selected. You can paste a Hugging Face repo id that **contains GGUF files** (e.g. `seanbailey518/Nanbeige4.1-3B-GGUF` or `Edge-Quant/Nanbeige4.1-3B-Q8_0-GGUF`). Base-model repos (safetensors only) do not offer GGUF; if you see "No GGUF files found", search on Hugging Face for "<model name> GGUF" and use that repo. Click Download to open the confirmation dialog. A confirmation dialog opens with the model card (README) and a list of GGUF files to choose from; after you confirm, the download starts. Progress (percentage, bytes, speed) and a Cancel button are shown in Settings. If you close Settings while a download is in progress, the download continues in the background; a compact progress indicator appears on the main chat page so you can see status and cancel from there. When the download finishes, a toast shows success or error and the model list refreshes.

**Settings → Connections:** Manage external integrations (Email, Calendar, Cloud, Discord, Telegram, WhatsApp, GitHub, etc.). A search field at the top filters the list by name or category (e.g. type “GitHub” or “Kalender” to jump to that connection). The **GitHub** category opens a dashboard with a rights-overview strip (toggle read/write per account), connected accounts, an event timeline (agent actions, newest first), and a repositories panel (repos for the selected account with links to GitHub). The **Calendar** category shows Google Calendar and Microsoft Outlook; they use the same OAuth connection as Email (connect Gmail or Outlook under Email first). In the **Mail dashboard**, suspicious emails are marked with a warning icon and tooltip reason; these remain visible to the user, but are hidden from agent mail tools by default for safety. When a calendar is connected, the settings (gear) icon opens the **Calendar Dashboard**: left sidebar lists connected accounts with links to open Google Calendar or Outlook in the browser; main area shows upcoming events from the API with selectable range and refresh. See [CONNECTIONS.md](CONNECTIONS.md) and [CALENDAR_INTEGRATION.md](CALENDAR_INTEGRATION.md).

**Settings → Automations:** View scheduled automations (user-scoped when multi-user is used; root/global automations such as "Daily calendar check" are also shown so the list matches the agent's `list_automations` tool). To create one manually: click **Create New** (or use the **Automation** entry in the sidebar footer) to open the calendar; choose month, then day, then an hour slot. The sidebar footer also includes **Notifications** (opens the Notifications popup) and **Settings**. Opening the Automation popup (footer) also triggers the calendar ensure-daily-check API when a calendar is connected, so the Daily calendar check appears in the list without opening Settings first. A popup lets you set repeat (once, daily, weekly, monthly, hourly), time, a detailed prompt, and an optional name. Creation is sent via WebSocket (`create_automation`); the list refreshes on success. The agent can also create automations via the `create_automation` tool in chat.

The same automation calendar includes a **per-user planner**:

- **To-do list** (left column): User and agent can add items via an "Add to-do" popup (text and optional due date). Each item has a done checkbox (updates via WebSocket) and a delete button. Data is stored per user and loaded when the calendar opens.
- **Notes** (bottom section, fixed height): User and agent can add notes via an "Add note" popup (optional title, content). Each note shows created-at and can be deleted. The list scrolls inside a fixed-height area so the layout does not grow.

Planner data is loaded with `get_automation_notes` and `get_automation_todos` when the calendar is opened (from the footer or from Settings). Create/update/delete use WebSocket messages; the UI updates optimistically where applicable. The agent can manage the same data via tools: `add_automation_note`, `add_automation_todo`, `list_automation_notes`, `list_automation_todos`, `delete_automation_note`, `delete_automation_todo`.

**Notifications popup:** Clicking **Notifications** in the sidebar opens a modal the same size as the Automation window. It shows a unified list of background activity: **thinking mode** run results, **automation** run results (success/error and summary), **thinking workspace handoff decisions** (approve/reject including optional automation-action outcome), and **channel replies** (when the agent sent a message via Telegram, Discord, or WhatsApp). Each entry shows status (Success/Skipped/Error), title, relative time, and a compact one-line summary directly in the list. For handoff items, compact badges show action (`approve`/`reject`), optional automation operation (`create`/`update`), and result (`ok`/`failed`). Click the row or the expand control to show the full summary/details. The list is loaded from `GET /api/notifications` when the popup opens; new items are pushed live via WebSocket (`notification`). Data is stored per user and trimmed to the last 100 items or 7 days.

### 7. Document Editor

The Document Editor is a rich-text editor in the right panel (dock or overlay). It supports opening files (HTML, DOCX, etc.), editing, and exporting.

**Layout and behaviour:** A4 page layout (210×297 mm, Word-style; overflow flows to the next page below with a separator line every 297 mm). Per-session state: editor content and open file are stored per chat session; switching sessions restores the correct document and unsaved content.

**Agent context:** When the editor is open, its plain-text content is sent with each chat message. The backend prepends it as `--- CURRENT DOCUMENT (Editor): <title> ---` so the agent sees the current document. You can select text in the editor (e.g. placeholders); the selection is added as a chip and sent with the message. The agent can replace that range via the `replace_editor_selection` tool (only available when there are marked selections); the UI applies the replacement and removes the chip.

**Workflow behaviour:** If the message contains the editor document block (`CURRENT DOCUMENT (Editor)`), workflow matching is skipped so the agent uses tools (e.g. `replace_editor_selection`) instead of starting a workflow.

**UI:** Closing the editor (X) shows a browser confirm dialog. PDF export preserves formatting (font size, bold, italic) by cloning content into the main document before conversion (html2pdf.js/html2canvas).

**Drafts from agent:** When you ask in the Web UI for the agent to write or compose text (e.g. *"Schreib mir einen Text …"*, *"Verfasse …"*, *"Write me a text …"*), the agent’s reply is also opened in the **Document Editor** as a draft. The draft is saved under the session’s data folder (`data_dir/drafts/<session_id>/entwurf.md`). You can edit the text there, improve it, and use **Save** to overwrite the draft or **Download HTML** to export it. This only applies to Web UI prompts (not e.g. Telegram), and only when the reply is substantial (after stripping `<think>` blocks).

## Local Model Idle Behavior

When the provider is `local`, the tray process only loads the model on real activity (prompt/CLI heartbeat). If there are no active WebUI WebSocket connections for 15 seconds, the model is unloaded from VRAM unless persistence is enabled.

## Wechsel zwischen lokalem Modell und API

Beim Wechsel des Providers (Local ↔ API) in den Einstellungen erscheint ein zentrales Overlay **„Changing model“** für etwa 5 Sekunden; danach lädt die Seite neu. Gleichzeitig entlädt der Tray bei Wechsel von Local zu API das Modell aus dem VRAM (llama-Server wird beendet) bzw. lädt bei Wechsel von API zu Local das Modell in den VRAM. Details: [MODELL_UND_PROVIDER_WECHSEL.md](MODELL_UND_PROVIDER_WECHSEL.md).

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

- `sessionId` is required. Optional: `sidebarDocuments` (Document Viewer attachments), `editorDocument` (when Document Editor is open; plain text only), `editorSelections` (marked ranges in the editor for `replace_editor_selection`).

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
Sent when the agent calls `replace_editor_selection`; the frontend replaces the character range `[start, end]` in the Document Editor with `newText` and removes that selection chip.

```json
{
  "type": "rag_results",
  "query": "The search query used",
  "sources": [
    {
      "text": "Snippet text...",
      "full_text": "Full text...",
      "score": 0.85,
      "metadata": {"source": "file.txt", "title": "My Note"}
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
- **Authentication**: Implemented. The UI verifies session state via `GET /api/auth/me` and requires a valid auth token/cookie. Unauthenticated clients are redirected to `/login`.
- **Login UX guard**: `web/proxy.ts` redirects authenticated requests away from `/login` to `/`, so logged-in users do not stay on the login page.
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
