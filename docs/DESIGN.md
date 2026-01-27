# VAF Web UI Design System

This document outlines the design tokens and component styles used in the Veyllo Agent Framework Web UI to ensure consistency.

## 1. Typography
*   **Font Family**: `font-sans` (System stack / Inter)
*   **Headings**: `text-xl font-bold text-gray-800` (e.g., Header)
*   **Subheadings**: `font-semibold text-sm` (e.g., Message sender, Workflow steps)
*   **Body Text**: `text-sm leading-relaxed` (e.g., Chat messages)
*   **Meta Text**: `text-xs text-gray-500` (e.g., Timestamps, Tool status)
*   **Monospace**: `font-mono text-xs` (e.g., Code snippets, IDs)

## 2. Colors (Light Mode)
*   **Backgrounds**:
    *   Main App: `bg-gray-50`
    *   Panels/Cards: `bg-white`
    *   Active/Hover: `bg-gray-100` or `bg-gray-50`
    *   User Message: `bg-gray-800`
*   **Borders**: `border-gray-200`
*   **Text**:
    *   Primary: `text-gray-900` or `text-gray-800`
    *   Secondary: `text-gray-600`
    *   Muted: `text-gray-400` or `text-gray-500`
*   **Accents (Status)**:
    *   Success: `text-green-600`, `bg-green-100`, `border-green-500`
    *   Error: `text-red-600`, `bg-red-100`, `border-red-500`
    *   Running/Active: `text-indigo-600` (or Blue), `bg-indigo-50`, `border-indigo-500`
    *   Warning: `text-yellow-600`, `bg-yellow-100`

## 3. Components

### Containers & Panels
*   **Rounded**: `rounded-2xl` (Chat bubbles), `rounded-xl` (Buttons/Cards), `rounded-lg` (Sidebar items)
*   **Shadows**: `shadow-sm` (Cards), `shadow-lg` (Overlays), `shadow-xl` (Input bar)
*   **Borders**: `border border-gray-200` generally used for separation.

### Workflow Nodes (Vertical List)
To match the Chat UI:
*   **Shape**: `w-72` (fixed width), `rounded-xl`
*   **Style**: `bg-white border border-gray-200 shadow-sm`
*   **Active State**: `border-indigo-500 shadow-md ring-1 ring-indigo-500/20`
*   **Typography**:
    *   Step Name: `font-medium text-sm text-gray-900`
    *   Tool Name: `text-[10px] uppercase font-bold tracking-wider`

### Workflow Overlay
*   **Position**: Fixed Right
*   **Animation**: Slide-in (`translate-x-0`)
*   **Background**: `bg-white border-l border-gray-200 shadow-2xl`
*   **Header**: `h-16 border-b border-gray-200` (Matches Main Header height)

## 4. Spacing
*   **Padding**: `p-4` or `p-6` for main containers.
*   **Gap**: `gap-3` or `gap-4` for lists.
*   **Margins**: `my-2` for separation.
