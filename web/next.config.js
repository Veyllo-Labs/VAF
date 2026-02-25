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
  images: {
    unoptimized: true,
  },
  // Disable the Next.js dev indicator
  devIndicators: false,
  // /api/* is handled by app/api/[...path]/route.ts (proxy with cookie forwarding). No rewrites.
}

module.exports = nextConfig
