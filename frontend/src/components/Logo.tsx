// The SheetTwin mark: a spreadsheet grid with a spark breaking out of its corner. Same drawing as app/icon.svg
// (the favicon); keep the two in sync.
export function Logo({ className = "size-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <rect width="32" height="32" rx="7" fill="#2563eb" />
      <g fill="none" stroke="#fff" strokeWidth="2.2" strokeLinejoin="round">
        <rect x="5.5" y="10" width="18" height="16" rx="2.5" />
        <path d="M5.5 15.5h18M5.5 20.75h18M11.5 15.5V26" />
      </g>
      <path
        d="M23.5 3.2c.6 3.5 1.8 4.7 5.3 5.3-3.5.6-4.7 1.8-5.3 5.3-.6-3.5-1.8-4.7-5.3-5.3 3.5-.6 4.7-1.8 5.3-5.3z"
        fill="#fff" stroke="#2563eb" strokeWidth="2.4" strokeLinejoin="round" paintOrder="stroke"
      />
    </svg>
  );
}
