"use client";

import { ChartPie, FilePlus2, FileSpreadsheet, FolderOutput, Languages, MessageSquareText, Network, Search, Sparkles } from "lucide-react";
import { motion } from "motion/react";
import Link from "next/link";
import { useState } from "react";
import type { Workbook } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export type View = "chat" | "graph" | "charts";

type Props = {
  workbooks: Workbook[];
  activeId: string | null;
  panel: "exports" | null;
  view: View;
  onView: (view: View) => void;
  onSelect: (id: string) => void;
  onNewData: () => void;
  onToggleExports: () => void;
};

function RailButton({
  label, active, onClick, children, accent,
}: { label: string; active?: boolean; onClick: () => void; children: React.ReactNode; accent?: boolean }) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      className={`group relative grid size-11 place-items-center rounded-2xl transition-all duration-200
        ${accent
          ? "bg-gradient-to-br from-violet-500 to-cyan-400 text-white shadow-lg shadow-violet-500/30 hover:scale-105 hover:shadow-violet-500/50"
          : active
            ? "bg-white/10 text-white ring-1 ring-white/15"
            : "text-zinc-400 hover:bg-white/5 hover:text-white"}`}
    >
      {children}
      <span className="pointer-events-none absolute left-full z-50 ml-3 whitespace-nowrap rounded-lg border border-white/10 bg-zinc-900/95 px-2.5 py-1 text-xs text-zinc-100 opacity-0 shadow-xl backdrop-blur transition-all duration-150 group-hover:translate-x-0.5 group-hover:opacity-100">
        {label}
      </span>
    </button>
  );
}

export function Sidebar({ workbooks, activeId, panel, view, onView, onSelect, onNewData, onToggleExports }: Props) {
  const { t, lang, setLang } = useI18n();
  const [query, setQuery] = useState("");
  const shown = workbooks.filter((w) => w.name.toLowerCase().includes(query.toLowerCase()));

  return (
    <div className="flex h-full shrink-0">
      {/* Icon rail */}
      <nav className="flex w-[72px] flex-col items-center gap-3 border-r border-white/5 bg-black/30 py-5 backdrop-blur-xl">
        <Link href="/" aria-label="SheetTwin" title="SheetTwin" className="mb-3 grid size-10 place-items-center rounded-xl bg-white/5 ring-1 ring-white/10 transition hover:bg-white/10">
          <Sparkles className="size-5 text-violet-300" />
        </Link>
        <RailButton label={t.nav.newData} onClick={onNewData} accent>
          <FilePlus2 className="size-5" />
        </RailButton>
        <RailButton label={t.nav.exports} onClick={onToggleExports} active={panel === "exports"}>
          <FolderOutput className="size-5" />
        </RailButton>
        <span className="my-1 h-px w-8 bg-white/10" />
        <RailButton label={t.nav.chat} onClick={() => onView("chat")} active={view === "chat" && !panel}>
          <MessageSquareText className="size-5" />
        </RailButton>
        <RailButton label={t.nav.graph} onClick={() => onView("graph")} active={view === "graph" && !panel}>
          <Network className="size-5" />
        </RailButton>
        <RailButton label={t.nav.charts} onClick={() => onView("charts")} active={view === "charts" && !panel}>
          <ChartPie className="size-5" />
        </RailButton>
        <div className="mt-auto">
          <RailButton label={`${t.nav.language}: ${t.toggle}`} onClick={() => setLang(lang === "zh" ? "en" : "zh")}>
            <span className="flex flex-col items-center leading-none">
              <Languages className="size-4" />
              <span className="mt-1 text-[10px] font-semibold tracking-wide">{lang === "zh" ? "EN" : "中"}</span>
            </span>
          </RailButton>
        </div>
      </nav>

      {/* Workbook list */}
      <aside className={`${view === "graph" ? "md:hidden" : "md:flex"} hidden w-72 flex-col border-r border-white/5 bg-white/[0.02] backdrop-blur-xl`}>
        <div className="px-5 pb-3 pt-6">
          <h1 className="text-lg font-semibold tracking-tight">
            <span className="text-gradient">SheetTwin</span>
          </h1>
          <p className="mt-0.5 text-xs text-zinc-500">{t.sidebar.tagline}</p>
        </div>
        <div className="px-4">
          <label className="flex items-center gap-2 rounded-xl border border-white/5 bg-white/[0.03] px-3 py-2 text-sm text-zinc-400 focus-within:border-violet-400/40">
            <Search className="size-4" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t.sidebar.search}
              className="w-full bg-transparent text-zinc-200 outline-none placeholder:text-zinc-600"
            />
          </label>
        </div>
        <div className="mt-4 px-5 text-[11px] font-medium uppercase tracking-wider text-zinc-600">{t.sidebar.workbooks}</div>
        <ul className="mt-2 flex-1 space-y-1 overflow-y-auto px-3 pb-4">
          {shown.length === 0 && (
            <li className="px-3 py-6 text-center text-sm text-zinc-600">
              {workbooks.length ? t.sidebar.noMatch : t.sidebar.empty}
            </li>
          )}
          {shown.map((w) => {
            const active = w.id === activeId;
            return (
              <li key={w.id}>
                <button
                  onClick={() => onSelect(w.id)}
                  className={`relative flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-colors
                    ${active ? "text-white" : "text-zinc-400 hover:bg-white/[0.04] hover:text-zinc-200"}`}
                >
                  {active && (
                    <motion.span
                      layoutId="wb-active"
                      className="absolute inset-0 rounded-xl bg-gradient-to-r from-violet-500/20 to-cyan-400/10 ring-1 ring-violet-400/30"
                      transition={{ type: "spring", stiffness: 400, damping: 32 }}
                    />
                  )}
                  <FileSpreadsheet className={`relative size-4 shrink-0 ${active ? "text-emerald-300" : ""}`} />
                  <span className="relative min-w-0 flex-1">
                    <span className="block truncate text-sm">{w.name}</span>
                    <span className="block text-[11px] text-zinc-500">
                      {w.built_at ? new Date(w.built_at).toLocaleString(t.locale, { dateStyle: "short", timeStyle: "short" }) : w.id}
                    </span>
                  </span>
                  {w.high_issues > 0 && (
                    <span className="relative rounded-full bg-rose-500/15 px-2 py-0.5 text-[11px] font-medium text-rose-300 ring-1 ring-rose-400/20">
                      {w.high_issues}
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      </aside>
    </div>
  );
}
