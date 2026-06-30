# React Components

This directory contains the reusable React components used throughout the VAF Web UI.

## Structure

The directory holds the top-level components plus several feature subfolders. The lists below are representative, not exhaustive.

### Subfolders

- **connections/**: Messaging and integration dashboards (Telegram, WhatsApp, Discord, Mail/Email, GitHub, Cloud, Calendar, Contacts) and their setup wizards.
- **memory/**: Memory graph, memory detail panel, and RAG query UI, plus the memory store.
- **settings/**: Editors used by the settings dialog (custom tools, MCP servers, skills, TTS, and workflow creation).
- **workflows/**: Workflow runtime UI components and store.
- **ui/**: Low-level UI primitives used by higher-level components.
- **__tests__/**: Component unit tests.

### Top-level components

Grouped by area (representative examples, not a full list):

- **Chat and tool execution**: `ToolMessage.tsx` (inline tool cards), `ActiveToolsPanel.tsx` (legacy tools panel, kept for compatibility), `TurnActionsTimeline.tsx`, `SubAgentWindow.tsx` (sub-agent progress modal).
- **Document editing and viewing**: `DocumentEditor.tsx`, `DocumentViewer.tsx`, `NativeDocxEditor.tsx`, `CodeViewer.tsx`, `HtmlViewer.tsx`, `ImageViewer.tsx`, `PdfWithHighlights.tsx`.
- **Agent presence**: `AgentAvatar.tsx`, `BrowserLiveTile.tsx`.
- **Notifications and announcements**: `NotificationsModal.tsx`, `AnnouncementModal.tsx`.
- **Automation**: `CreateAutomationPopup.tsx`, `AutomationCalendarModal.tsx`.
- **Onboarding and configuration**: `SoulWizard.tsx`, `SettingsModal.tsx` (Web UI configuration dialog), `TrainingDashboard.tsx`.
- **Utilities and providers**: `CustomCursor.tsx`, `CopyOnRightClick.tsx`, `HostnameNormalizer.tsx`, `IntlProviderWrapper.tsx`.

## Usage

Components in this directory should be modular and focus on specific UI features. High-level features like the Chat interface or Session List should be organized here or in dedicated subdirectories as the project grows.

## Adding Components

- Place new components in this directory or a subfolder if they are complex.
- Export components as named exports.
- Use the `ui/` components for consistent look and feel.

## Dependencies

- **Lucide React**: The standard icon set.
