import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  ...(process.env.WORKMATE_STANDALONE === "1" ? { output: "standalone" as const } : {}),
  async rewrites() {
    // Ordinary JSON APIs fall back to gateway.  Keep this in the
    // ``fallback`` phase so the dedicated filesystem SSE Route Handler is
    // matched first; the shorthand rewrite form intercepted and buffered it.
    const api = process.env.PRODUCT_API_URL ?? "http://localhost:8080";
    return {
      beforeFiles: [],
      afterFiles: [],
      fallback: [
        { source: "/api/:path*", destination: `${api}/api/:path*` },
      ],
    };
  },
};

export default nextConfig;
