import type { NextConfig } from "next";

// The API lives on a separate origin. The browser only ever talks to this
// Next.js server; /v1/*, /v2/* and /api/* are proxied so the s0_session cookie is
// first-party. SCRAPER0_API_URL is read at build time (rewrites are compiled
// into the standalone server), so set it in the build environment too.
const API_URL = (process.env.SCRAPER0_API_URL || "http://127.0.0.1:5001").replace(/\/$/, "");

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  // Next's gzip buffers proxied text/event-stream responses, which would hold
  // back live job progress until the job ends. Compress at the edge instead.
  compress: false,
  async rewrites() {
    return [
      { source: "/v1/:path*", destination: `${API_URL}/v1/:path*` },
      { source: "/v2/:path*", destination: `${API_URL}/v2/:path*` },
      { source: "/api/:path*", destination: `${API_URL}/api/:path*` },
    ];
  },
};

export default nextConfig;
