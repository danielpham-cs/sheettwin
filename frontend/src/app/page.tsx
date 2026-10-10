"use client";

import dynamic from "next/dynamic";

// Client-only, like the app: the language choice lives in localStorage.
const Landing = dynamic(() => import("@/components/Landing"), { ssr: false });

export default function Home() {
  return <Landing />;
}
