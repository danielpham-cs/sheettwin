"use client";

import dynamic from "next/dynamic";

// Client-only: chat history lives in localStorage, which the server cannot read.
const App = dynamic(() => import("@/components/App"), { ssr: false });

export default function AppPage() {
  return <App />;
}
