/** @type {import('next').NextConfig} */

// Backend API URL - respects environment variables for network/TLS mode
const API_PROTOCOL = process.env.VAF_TLS_ENABLED === 'true' ? 'https' : 'http'
const API_HOST = process.env.VAF_API_HOST || '127.0.0.1'
const API_PORT = process.env.VAF_API_PORT || '8001'
const API_BASE = `${API_PROTOCOL}://${API_HOST}:${API_PORT}`

const nextConfig = {
  reactStrictMode: false,
  images: {
    unoptimized: true,
  },
  // Disable the Next.js dev indicator (bottom-left corner)
  devIndicators: false,
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${API_BASE}/api/:path*`,
      },
    ]
  },
}

module.exports = nextConfig