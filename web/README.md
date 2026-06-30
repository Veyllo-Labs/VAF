# VAF Web Interface

This directory contains the source code for the VAF Web UI, a modern dashboard built with Next.js and Tailwind CSS.

## Technology Stack

- **Frontend Framework**: Next.js (App Router, React)
- **Styling**: Tailwind CSS
- **Communication**: Native WebSocket API
- **Icons**: Lucide React

## Structure

- **app/**: Next.js App Router pages and layouts.
- **components/**: Reusable React components.
- **components/ui/**: Low-level UI primitives (buttons, inputs, etc.).
- **hooks/**: Reusable React hooks (e.g. `useIsMobile`).
- **lib/**: Utility functions and helper modules.
- **messages/**: `next-intl` translation catalogs (`en.json`, `de.json`).
- **scripts/**: Build and locale-generation helpers (e.g. `generate-locales.js`).
- **public/**: Static assets (logo, PDF worker, notification sounds).
- **certificates/**: Local TLS certificates for HTTPS development.
- **proxy.ts**: Next.js middleware for auth guards and the optional HTTPS entry-point redirect.

## Getting Started

To run the Web UI in development mode:

```bash
cd web
npm install
npm run dev
```

The Web UI connects to the VAF WebSocket backend on port 8001.

## Deployment

The Web UI is built and served as a static or server-side rendered application. In the VAF project, it is typically managed and started automatically by the main `vaf run` command unless disabled via settings.

## Dependencies

- Node.js 18+
- npm or yarn
- See `package.json` for the full list of dependencies.
