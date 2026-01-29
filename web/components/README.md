# React Components

This directory contains the reusable React components used throughout the VAF Web UI.

## Structure

- **ActiveToolsPanel.tsx**: Legacy tools panel (kept for compatibility).
- **SettingsModal.tsx**: Web UI configuration dialog.
- **SubAgentWindow.tsx**: Large modal showing sub-agent progress.
- **ToolMessage.tsx**: Inline tool execution cards in chat.
- **workflows/**: Workflow runtime UI components and store.
- **ui/**: Low-level UI primitives used by higher-level components.

## Usage

Components in this directory should be modular and focus on specific UI features. High-level features like the Chat interface or Session List should be organized here or in dedicated subdirectories as the project grows.

## Adding Components

- Place new components in this directory or a subfolder if they are complex.
- Export components as named exports.
- Use the `ui/` components for consistent look and feel.

## Dependencies

- **Lucide React**: The standard icon set.
