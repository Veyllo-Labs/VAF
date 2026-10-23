/** @type {import('next').NextConfig} */

// Backend API URL - respects environment variables for network/TLS mode
const API_PROTOCOL = process.env.VAF_TLS_ENABLED === 'true' ? 'https' : 'http'
const API_HOST = process.env.VAF_API_HOST || '127.0.0.1'
const API_PORT = process.env.VAF_API_PORT || '8001'
const API_BASE = `${API_PROTOCOL}://${API_HOST}:${API_PORT}`

// When TLS is off, Next.js proxies to 8001. When TLS is on, proxy to internal HTTP channel 8005.
const INTERNAL_API_PORT = process.env.VAF_INTERNAL_API_PORT || '8005'

const nextConfig = {
  reactStrictMode: false,
  // Keeps the image optimizer off, so /_next/image answers 404. web/proxy.ts lets every
  // /_next/ path through without a session check, so an optimizer switched on here would
  // be an unauthenticated endpoint that decodes whatever image it is pointed at.
  images: {
    unoptimized: true,
  },
  experimental: {
    // Next.js 16.4's upgrade reminder fetches the npm advisory list during `next build`
    // and, under a coding agent's environment (CLAUDECODE, CURSOR_AGENT, AI_AGENT and
    // others), stops the build with exit 1 on its first run while the installed Next.js
    // has an open advisory. VAF builds the frontend once at start with the environment it
    // was started from, so that stop would leave the web UI down. Advisories reach this
    // repository through Dependabot instead.
    agentUpgrade: false,
  },
  // Disable the Next.js dev indicator
  devIndicators: false,
  // /api/* is handled by app/api/[...path]/route.ts (proxy with cookie forwarding).
  // /sounds/* is served by the backend for notification sounds.
  async rewrites() {
    return [
      {
        source: '/sounds/:path*',
        destination: `http://127.0.0.1:${INTERNAL_API_PORT}/sounds/:path*`,
      },
    ]
  },
}

module.exports = nextConfig
