/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  async rewrites() {
    // Proxy verso il backend Python (Railway) per evitare problemi di CORS.
    // In produzione imposta BACKEND_URL, es: https://quotaverace-backend.up.railway.app
    const target = process.env.BACKEND_URL || 'http://localhost:8000'
    return [
      {
        source: '/api/backend/:path*',
        destination: `${target}/:path*`,
      },
    ]
  },
}
module.exports = nextConfig
