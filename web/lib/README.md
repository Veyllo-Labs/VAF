# Utilities and Helpers

This directory contains utility functions, shared logic, and helper modules used across the Web UI.

## Key Files

- **utils.ts**: General-purpose helpers — `cn` (Tailwind class merging with `clsx` and `tailwind-merge`) plus `getApiBase` / `getWsBase` for resolving the API and WebSocket base URLs.
- **changelog.ts**: The product changelog data shown in the announcement modal; entries are versioned and ordered newest-first.
- **cursorStore.ts**: Zustand store for the custom-cursor preference (VAF's dot cursor vs. the system mouse), persisted in `localStorage`.
- **docxNative.ts**: Type definitions for the native DOCX document model (paragraphs, runs, tables, images, warnings) used by the document editor.
- **languages.ts**: Single source of truth for supported UI locales, plus helpers to validate and resolve a locale from the browser language.
- **licenses_data.ts**: Static list of third-party dependencies and their licenses, shown in the about/licenses view.
- **localeStore.ts**: Zustand store for the active UI locale, read from `localStorage` (or browser language) and persisted on change.
- **oauth_defaults.ts**: Built-in OAuth client IDs and a helper to hide the default value in the UI when the user hasn't set their own.
- **sessionCache.ts**: Session cache persistence in `localStorage` with quota limits, trimming by session count, messages per session, and total bytes.
- **version.ts**: Helpers to parse and format the app version (`formatVersion`, `parseMajorMinor`, `compareMajorMinor`) for the version badge and changelog gating.

## Usage

Import utilities and stores from this directory to avoid code duplication and keep logic consistent — for example resolving the API base with `getApiBase()` from `utils.ts`, or reading the active locale via `useLocaleStore()` from `localeStore.ts`.

## Conventions

- Keep functions pure and well-tested where possible.
- Use descriptive names for helpers.
- Group related utilities into separate files as the library grows.
