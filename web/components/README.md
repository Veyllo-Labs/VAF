# React Components

This directory contains the reusable React components used throughout the VAF Web UI.

## Structure

- **SettingsModal.tsx**: The interface for modifying VAF configuration from the web.
- **ui/**: Atomic UI components (Buttons, Inputs, Dialogs) primarily sourced from Shadcn UI.

## Usage

Components in this directory should be modular and focus on specific UI features. High-level features like the Chat interface or Session List should be organized here or in dedicated subdirectories as the project grows.

## Adding Components

- Place new components in this directory or a subfolder if they are complex.
- Export components as named exports.
- Use the `ui/` components for consistent look and feel.

## Dependencies

- **Radix UI**: Used for accessible UI primitives.
- **Lucide React**: The standard icon set.
