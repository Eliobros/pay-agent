/** @type {import('next').NextConfig} */
// The browser never calls the API host directly: every request goes to
// /api/backend/* on this origin and the Next.js server proxies it. That keeps
// the owner session cookie first-party — a cross-site cookie is dropped by the
// browser, which logged the owner out right after signing in.
const API_ORIGIN = (
  process.env.API_ORIGIN || process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
).replace(/\/+$/, '');

const nextConfig = {
  allowedDevOrigins: ['161.156.83.104'],
  async rewrites() {
    return [{ source: '/api/backend/:path*', destination: `${API_ORIGIN}/:path*` }];
  },
};

export default nextConfig;
