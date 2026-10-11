import type { NextConfig } from "next";
import { PHASE_DEVELOPMENT_SERVER } from "next/constants";

// FastAPI backend started with `sheetir serve`; override with SHEETIR_API.
const API = process.env.SHEETIR_API ?? "http://127.0.0.1:8765";

const turbopack: NextConfig["turbopack"] = {
  rules: {
    "*.css": {
      loaders: ["@tailwindcss/turbopack"],
      as: "*.css",
    },
  },
};

export default function config(phase: string): NextConfig {
  // `next dev`: proxy /api to the backend so the UI hot-reloads on :3000.
  if (phase === PHASE_DEVELOPMENT_SERVER) {
    return {
      turbopack,
      rewrites: () => [{ source: "/api/:path*", destination: `${API}/api/:path*` }],
    };
  }
  // `next build`: plain static files in out/ (landing page at /, the app at /app/), copied into sheetir/web/static/app
  // (scripts/copy-to-backend.mjs) and served by FastAPI on the same origin as /api.
  // trailingSlash writes app/index.html, which FastAPI serves at /app/ (dev keeps it off: it would redirect /api calls).
  return { turbopack, output: "export", trailingSlash: true };
}
