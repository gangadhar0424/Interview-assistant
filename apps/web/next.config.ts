import path from "node:path";
import { loadEnvConfig } from "@next/env";
import type { NextConfig } from "next";

// Load the monorepo-root .env (shared with the API). Next has already loaded apps/web/.env*
// and caches that result, so forceReload (4th argument) is required to read the root file.
loadEnvConfig(
  path.resolve(__dirname, "../.."),
  process.env.NODE_ENV !== "production",
  console,
  true,
);

// Server-side only (not NEXT_PUBLIC_*): where the Next.js server forwards /api/* requests.
// NOTE: rewrites are resolved at build time for `next start`; rebuild after changing this.
const apiInternalUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The browser only ever talks to the web origin. Proxying /api/* keeps the HttpOnly refresh
  // cookie same-origin (no CORS, no third-party-cookie issues, SameSite=Lax is sufficient).
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiInternalUrl}/api/:path*` }];
  },
};

export default nextConfig;
