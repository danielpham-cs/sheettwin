// URL prefix when SheetTwin is served under a sub-path (e.g. behind jupyter-server-proxy at /6/proxy/8765).
// Set NEXT_PUBLIC_BASE_PATH at build time; empty (the default) serves at the site root as before.
export const BASE = (process.env.NEXT_PUBLIC_BASE_PATH ?? "").replace(/\/$/, "");
