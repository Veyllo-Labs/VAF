# Workflow UI Components

This document describes the WebUI components for workflow execution and document editing.

## Overview

When a workflow runs in VAF, the WebUI provides several visual components:

1. **WorkflowChatElement** - Inline chat card showing workflow progress
2. **VAFWorkflowRuntime** - Right panel with workflow steps and terminal output
3. **SubAgentWindow** - Panel for sub-agent output (hidden during workflow execution)
4. **DocumentEditor** - Single right-side document panel: workflow-generated documents (edit, save, PDF, download) and attachments (Anhänge) with quote-from-selection and compact document switcher

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

**Kernel editor mode** (when `filePath` is set):
- Displays in the same panel area as SubAgentWindow (dock mode)
- Loads HTML content via `/api/file?path=...`
- Editable iframe with contentEditable
- Save, Export PDF, and Download HTML buttons
- Optional workflow steps on the left
- Status indicator (Ready/Active/Error)
- Opened via `document_ready` WebSocket event from workflow
- Uses `getApiBase()` for API calls
- Optional: text selection in the iframe can be sent as a quote chip (via `onInsertSelection`)

**Attachments mode** (when `documents` are passed, no `filePath`):
- Same panel area; no right-side document list (compact dropdown in header instead)
- Header: title "Anhänge", dropdown to select current document, "Dokument hinzufügen" and per-document remove
- Read-only extracted text (or image) for the selected document with persistent highlights
- **Quote from document**: Selecting text inserts it as a quote chip above the chat input; each selection gets a distinct highlight color (dark, orange, pink, blue, green) and a matching chip; click chip to remove
- Frontend sends `set_sidebar_documents` with document list; backend replies with `sidebar_documents_set` (`contents: [{ name, content }]`); `chat` messages include `sidebarDocuments` so the LLM receives attachment content
- State is stored per session (`sessionViewerState`); not cleared on `history_update`
- User messages show an "Anhänge" indicator (document names) under the bubble when the panel is closed

**Integration:** The right panel renders either DocumentEditor (editor or attachments) or SubAgentWindow. Workflow document opens editor mode; Paperclip (attach) opens DocumentEditor in attachments mode.

**Shared types/constants:** `web/components/DocumentViewer.tsx` no longer renders a panel; it exports only types (`InsertedSelectionRange`, `DocumentViewerDocument`) and constants (`CHIP_BG_CLASSES`, `INSERTION_COLOR_CLASSES`) for use by DocumentEditor and the chat input chips.

_(Document Viewer as a separate panel has been removed; its functionality lives in DocumentEditor attachments mode above.)_

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
| `set_sidebar_documents` | Client → Server: set/clear sidebar documents for DocumentEditor (attachments mode) |
| `sidebar_documents_set` | Server → Client: extracted contents for DocumentEditor attachments display |

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

## Known Issues

1. **Workflow ID Matching**: The `workflowId` in chat messages must match the store's `workflow.id` for real-time updates to work. The workflow system uses the format `[WORKFLOW_ASYNC:{taskId}:{workflowId}]` where `workflowId` is the correct identifier.

2. **Terminal Scroll**: Auto-scroll only works when new lines are added. Manual scrolling up is preserved until new content arrives.

3. **Document Loading**: DocumentEditor requires the `/api/file` endpoint to be accessible. Files must be in allowed directories (Documents, Downloads, or VAF data dir).
