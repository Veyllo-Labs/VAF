# Workflow UI Components

This document describes the WebUI components for workflow execution and document editing.

## Overview

When a workflow runs in VAF, the WebUI provides several visual components:

1. **WorkflowChatElement** - Inline chat card showing workflow progress
2. **VAFWorkflowRuntime** - Right panel with workflow steps and terminal output
3. **SubAgentWindow** - Panel for sub-agent output (hidden during workflow execution)
4. **DocumentViewer** - Attachments panel (paperclip): PDF, DOCX, Office, and text display with quote-from-selection
5. **DocumentEditor** - Single right-side document panel: workflow-generated documents (edit, save, PDF, download) and optional attachments mode with compact document switcher

## Component Architecture

### WorkflowChatElement

Located at `web/components/workflows/WorkflowChatElement.tsx`

A compact card displayed inline in the chat showing:
- Workflow name
- Progress bar with percentage
- Current step name
- Status icon (running/completed/failed)

**Key behavior:**
- Connects to `useWorkflowStore` to get real-time status updates
- Uses `workflowId` to match with the active workflow in store
- Clicking opens the VAFWorkflowRuntime panel

```typescript
<WorkflowChatElement
    workflowId={workflow.id}  // Must match store's workflow.id
    name="Deep Research"
    initialSteps={4}
/>
```

### VAFWorkflowRuntime

Located at `web/components/workflows/VAFWorkflowRuntime.tsx`

A slide-out panel on the right side containing:
- React Flow visualization of workflow steps
- Terminal output section with auto-scroll
- Status footer with step count

**Features:**
- Auto-scrolls terminal output to bottom when new lines arrive
- Auto-closes 2.5 seconds after workflow completion
- Receives output via `appendWorkflowLine()` from the store

### SubAgentWindow

Located at `web/components/SubAgentWindow.tsx`

A docked panel for displaying sub-agent activity. Supports two modes:
- `dock` - Embedded in the right panel area
- `overlay` - Full-screen modal overlay

**Workflow Integration:**
- Automatically hidden when a workflow is running (`isWorkflowRunningRef.current`)
- Sub-agent output is routed to VAFWorkflowRuntime terminal instead
- Can still be manually opened during workflow if needed

### DocumentEditor

Located at `web/components/DocumentEditor.tsx`

A single right-side panel used for (1) workflow-generated documents and (2) attachments (Anhänge). Styled identically to SubAgentWindow.

**Kernel editor mode** (when `filePath` is set) now has two sub-paths:

#### Native DOCX editor path

Used for `.docx` files.

- Loads a native DOCX model from the backend (`/api/file/docx-model`)
- Saves back to `.docx` through `/api/file/save-docx-native`
- Uses a model-driven editor instead of `iframe + contentEditable`
- Keeps DOCX save logic out of the old HTML roundtrip
- Exposes the current document to the agent as flattened plain text derived from the native model

#### Legacy editor path

Used for HTML and the older non-DOCX editor flows.

- Displays in the same panel area as SubAgentWindow (dock mode)
- Loads HTML content via `/api/file?path=...`
- Editable iframe with contentEditable
- Save, Export PDF, and Download HTML buttons
- Optional workflow steps on the left
- Status indicator (Ready/Active/Error)
- Opened via `document_ready` WebSocket event from workflow
- Uses `getApiBase()` for API calls
- Optional: text selection in the iframe can be sent as a quote chip (via `onInsertSelection`)

See also: [DOCUMENT_EDITOR_NATIVE_DOCX.md](DOCUMENT_EDITOR_NATIVE_DOCX.md)

**Attachments mode** (when `documents` are passed, no `filePath`):
- Same panel area; no right-side document list (compact dropdown in header instead)
- Header: title "Anhänge", dropdown to select current document, "Dokument hinzufügen" and per-document remove
- Read-only extracted text (or image) for the selected document with persistent highlights
- **Quote from document**: Selecting text inserts it as a quote chip above the chat input; each selection gets a distinct highlight color (dark, orange, pink, blue, green) and a matching chip; click chip to remove
- Frontend sends `set_sidebar_documents` with document list; backend replies with `sidebar_documents_set`; `chat` messages include `sidebarDocuments` so the LLM receives attachment content
- State is stored per session (`sessionViewerState`); not cleared on `history_update`
- User messages show an "Anhänge" indicator (document names) under the bubble when the panel is closed

**Integration:** The right panel renders either DocumentViewer, DocumentEditor, or SubAgentWindow. Paperclip (attach) opens DocumentViewer with the attachments list. Workflow document opens DocumentEditor with the generated file.

### DocumentViewer

Located at `web/components/DocumentViewer.tsx`

The attachments panel (paperclip). Displays uploaded documents with:
- **PDF**: Original PDF via react-pdf with text selection and highlights
- **Office** (.docx, .xlsx, .pptx, .odt, .ods, .odp): When Gotenberg is running, the backend converts to PDF and returns `mimeType: application/pdf` with `data` (PDF base64), so the frontend uses the PDF viewer for full design fidelity. Without Gotenberg: DOCX via client-side mammoth.js when `data` is present; .xlsx/.pptx via backend `htmlContent` (HTML)
- **Markdown, HTML, plain text**: Rendered accordingly
- **Document list**: Collapsible sidebar (overlays when expanded; compact strip when collapsed)
- **Quote from document**: Text selection inserts a chip; each selection gets a distinct highlight color

Backend `sidebar_documents_set` payload: `contents: [{ name, content, data?, mimeType?, htmlContent? }]`. With Gotenberg, Office docs are converted to PDF for native display. Fallback: `htmlContent` for Office, or mammoth.js for DOCX when `data` is present.

**Shared exports:** `InsertedSelectionRange`, `DocumentViewerDocument`, `CHIP_BG_CLASSES`, `INSERTION_COLOR_CLASSES` for use by DocumentEditor and chat input chips.

## Data Flow

### Workflow Execution

```
1. User triggers workflow (@deep_research topic)
2. Backend sends `workflow_start` WebSocket event
3. `loadWorkflow()` initializes store with steps
4. VAFWorkflowRuntime panel opens
5. Backend sends `workflow_update` events for step progress
6. Backend sends `subagent_output_stream` events
7. Output routed to workflow terminal (not SubAgentWindow)
8. Backend sends `document_ready` when HTML created
9. DocumentEditor opens with file path
10. Backend sends `workflow_update` with completion status
11. VAFWorkflowRuntime auto-closes after 2.5s
```

### WebSocket Events

| Event | Purpose |
|-------|---------|
| `workflow_start` | Initialize workflow in store, open panel |
| `workflow_update` | Update step status/progress |
| `workflow_output_stream` | Add line to terminal output |
| `subagent_output_stream` | Sub-agent output (routed to workflow terminal when running) |
| `document_ready` | Open DocumentEditor with generated file |
| `set_sidebar_documents` | Client → Server: set/clear sidebar documents for Document Viewer (attachments panel) |
| `sidebar_documents_set` | Server → Client: extracted contents (`name`, `content`, optional `data`, `mimeType`, `htmlContent`) for Document Viewer display |

## Store Structure

The `useWorkflowStore` (Zustand) manages:

```typescript
{
    isOpen: boolean;
    workflow: {
        id: string;
        name: string;
        steps: VAFStep[];
        currentStepId: string | null;
        status: 'idle' | 'running' | 'paused' | 'completed' | 'failed';
    } | null;
    nodes: Node[];      // React Flow nodes
    edges: Edge[];      // React Flow edges
    consoleLines: string[];  // Terminal output (max 400 lines)
}
```

## Styling

All panels follow a consistent design:
- Rounded corners (`rounded-2xl`)
- White background with gray borders
- Header with icon, title, and status indicator
- Presence dot (green=active, gray=idle, red=error)
- File info bar below header
- Content area with internal rounded container

## Workflow Creator (Admin)

Admins can create, edit, and delete user-defined workflows directly in the WebUI — no file upload or server restart required.

### Entry point

The **Workflows** tab in the Settings modal (`SettingsModal.tsx → showWorkflowsModal`) shows a grid of workflow cards. When the logged-in user is an admin, a **"+ Create Workflow"** dashed card appears first in the grid (hidden when the search filter is active).

User-created workflows show a purple **"Custom"** badge and open the creator in edit mode when clicked. Built-in workflows still open the read-only ReactFlow visualization.

### WorkflowCreator component

**File:** `web/components/settings/WorkflowCreator.tsx`

A full-screen overlay modal (z-[80], above code viewer at z-[70]):

| Field | Details |
|-------|---------|
| **Name** | Human-readable display name |
| **ID** | Lowercase snake_case identifier, auto-generated from name in create mode, read-only in edit mode |
| **Description** | Short description shown on the workflow card |
| **Trigger phrases** | Tag-style input — phrases that activate the workflow router |
| **Steps** | Ordered step chain (see below) |

**Step chain:**
Each step is a card with:
- **Prompt** textarea — instruction for the agent. Supports `{variable}` substitution from previous step outputs.
- **Tool** dropdown — which agent tool to call (`coding_agent`, `web_search`, `research_agent`, etc.). Populated from the full live tool list.
- **Description** (optional) — label shown in workflow progress output.

Between each step card (and after the last) an **"Add Step"** button inserts a new blank step. Steps can be removed individually (disabled when only one step remains).

The component auto-generates an `output` variable name (`step_1_output`, `step_2_output`, …) for each step before sending to the backend.

### WebSocket protocol

| Message (client → server) | Payload | Description |
|--------------------------|---------|-------------|
| `create_workflow` | `{workflow_id, name, description, triggers, steps}` | Save new workflow to `~/.vaf/workflows/` |
| `update_workflow` | `{workflow_id, name, description, triggers, steps}` | Overwrite an existing user workflow |
| `delete_workflow` | `{workflow_id}` | Remove a user workflow |

| Message (server → client) | Payload | Description |
|--------------------------|---------|-------------|
| `workflow_created` | `{workflow_id}` | Confirms creation; followed immediately by `workflows_list` |
| `workflow_updated` | `{workflow_id}` | Confirms update; followed immediately by `workflows_list` |
| `workflow_deleted` | `{workflow_id}` | Confirms deletion; followed immediately by `workflows_list` |
| `workflow_error` | `{error}` | Validation or permission error |
| `workflows_list` | `{workflows: [...]}` | Refreshed list (each entry includes `is_custom: bool`) |

All three write operations are **admin-only** — the server checks the session's user role/scope before executing. Built-in workflows (those in `vaf/workflows/workflows/`) cannot be modified or deleted via the WebUI; attempting to do so returns a `workflow_error`.

### Storage

User workflows are stored as `.py` files in `~/.vaf/workflows/`. Agent-created workflows carry a `# created_by: agent` marker on the first line (so the agent can only edit/delete its own). The `list_templates()` function sets `is_custom: True` for any workflow whose file exists in the user directory. After every write, `reload_workflows()` is called so the router and `execute_workflow` tool see the change immediately.

## Known Issues

1. **Workflow ID Matching**: The `workflowId` in chat messages must match the store's `workflow.id` for real-time updates to work. The workflow system uses the format `[WORKFLOW_ASYNC:{taskId}:{workflowId}]` where `workflowId` is the correct identifier.

2. **Terminal Scroll**: Auto-scroll only works when new lines are added. Manual scrolling up is preserved until new content arrives.

3. **Document Loading**: DocumentEditor requires the `/api/file` endpoint to be accessible. Files must be in allowed directories (Documents, Downloads, or VAF data dir).
