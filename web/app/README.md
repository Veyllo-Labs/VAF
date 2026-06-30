# Next.js App Router

This directory contains the main pages, layouts, and global styles for the VAF Web UI using the Next.js App Router.

## Key Files

- **layout.tsx**: The root layout shared across all pages (HTML/body setup).
- **page.tsx**: The main dashboard page where the chat interface and session management reside.
- **globals.css**: Global CSS styles and Tailwind imports.

## Development

The current routes are `login/`, `memory/`, `settings/` (an OAuth-callback redirect to the main page), and the `api/[...path]/` catch-all proxy.

When adding new routes:
- Create a new directory for the route (e.g., `app/profile/`).
- Add a `page.tsx` within that directory.
- Use `layout.tsx` for shared UI elements like sidebars or headers.

## Coding Conventions

- Use TypeScript for all components and pages.
- Prefer Functional Components with Hooks.
- Use Tailwind CSS classes for styling.
