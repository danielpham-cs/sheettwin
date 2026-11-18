"use client";

import {
  ChartPie, ChevronRight, Database, FilePlus2, FileSpreadsheet, FolderInput, FolderOutput, Grid3x3, Languages,
  ListChecks, MessageSquareText, Moon, Network, Plus, Sun, Search, Trash2,
} from "lucide-react";
import { motion } from "motion/react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import type { Project, Workbook } from "@/lib/api";
import { ProjectBadge, projectName, ProjectSwitcher, type ProjectControls } from "@/components/ProjectMenu";
import { Logo } from "@/components/Logo";
import { StatusBadge } from "@/components/ParseStatus";
import { useI18n } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";

export type View = "chat" | "sheets" | "issues" | "query" | "graph" | "charts";

/** A small menu of the other projects, to move a workbook there. */
function MoveMenu({ projects, current, onMove }: { projects: Project[]; current: string; onMove: (to: string) => void }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  const others = projects.filter((p) => p.id !== current);
  if (!others.length) return null;
  return (
    <div ref={box} className="relative">
      <button onClick={() => setOpen((o) => !o)} title={t.projects.moveTo} aria-label={t.projects.moveTo}
        className={`rounded-md p-1 text-zinc-500 hover:bg-white/10 hover:text-white ${open ? "block" : "hidden group-hover:block"}`}>
        <FolderInput className="size-3.5" />
      </button>
      {open && (
        <ul className="absolute right-0 top-full z-50 mt-1 w-48 rounded-md border border-white/10 bg-zinc-900 py-1 text-sm shadow-xl">
          <li className="px-3 py-1 text-[10px] uppercase tracking-wider text-zinc-500">{t.projects.moveTo}</li>
          {others.map((p) => (
            <li key={p.id}>
              <button onClick={() => { setOpen(false); onMove(p.id); }} className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-zinc-200 hover:bg-white/10">
                <ProjectBadge project={p} size="sm" /><span className="truncate">{projectName(p, t)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type Props = ProjectControls & {
  workbooks: Workbook[];
  activeId: string | null;
  panel: "exports" | null;
  view: View;
  onView: (view: View) => void;
  onSelect: (id: string) => void;
  onNewData: () => void;
  onToggleExports: () => void;
  onRemove: (id: string) => void;
};

function RailButton({
  label, active, onClick, children, accent,
}: { label: string; active?: boolean; onClick: () => void; children: React.ReactNode; accent?: boolean }) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      aria-current={active ? "page" : undefined}
      className={`group relative grid size-11 place-items-center rounded-lg transition-all duration-200
        ${accent
          ? "bg-blue-600 text-snow shadow-lg hover:bg-blue-500"
          : active
            ? "bg-blue-500/20 text-blue-100 ring-1 ring-blue-400/50 shadow-md"
            : "text-zinc-400 hover:bg-white/5 hover:text-white"}`}
    >
      {/* Marker on the rail's left edge (the rail is 72px, the button 44px: 14px each side) */}
      {active && !accent && (
        <span className="absolute -left-[14px] top-1/2 h-6 w-1 -translate-y-1/2 rounded-r-full bg-blue-500" />
      )}
      {children}
      <span className="pointer-events-none absolute left-full z-50 ml-3 whitespace-nowrap rounded-lg border border-white/10 bg-zinc-900/95 px-2.5 py-1 text-xs text-zinc-100 opacity-0 shadow-xl transition-all duration-150 group-hover:translate-x-0.5 group-hover:opacity-100">
        {label}
      </span>
    </button>
  );
}

export function Sidebar({ workbooks, activeId, panel, view, onView, onSelect, onNewData, onToggleExports, onRemove, ...pc }: Props) {
  const { t, lang, setLang } = useI18n();
  const [theme, toggleTheme] = useTheme();
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const shown = workbooks.filter((w) => [w.name, ...w.aliases].some((n) => n.toLowerCase().includes(query.toLowerCase())) || w.id.startsWith(query.toLowerCase()));
  // One row per file name (the list comes most recently uploaded first); its results are the versions.
  const groups: { key: string; versions: Workbook[] }[] = [];
  for (const w of shown) {
    const key = w.name.toLowerCase();
    const g = groups.find((x) => x.key === key);
    if (g) g.versions.push(w); else groups.push({ key, versions: [w] });
  }
  for (const g of groups) g.versions.sort((a, b) => b.version - a.version);
  const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString(t.locale, { dateStyle: "short", timeStyle: "short" }) : "");
  const toggle = (key: string) => setExpanded((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  // The version waiting for the user to confirm its removal (asked under its row, not in a browser dialog).
  const [confirming, setConfirming] = useState<Workbook | null>(null);
  const remove = (w: Workbook) => setConfirming(w);

  return (
    <div className="flex h-full shrink-0">
      {/* Icon rail */}
      <nav className="flex w-[72px] flex-col items-center gap-3 border-r border-white/5 bg-black/30 py-5">
        <Link href="/" aria-label="SheetTwin" title="SheetTwin" className="mb-3 grid size-10 place-items-center rounded-md transition hover:opacity-85">
          <Logo className="size-9" />
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
        <RailButton label={t.nav.sheets} onClick={() => onView("sheets")} active={view === "sheets" && !panel}>
          <Grid3x3 className="size-5" />
        </RailButton>
        <RailButton label={t.nav.issues} onClick={() => onView("issues")} active={view === "issues" && !panel}>
          <ListChecks className="size-5" />
        </RailButton>
        <RailButton label={t.nav.query} onClick={() => onView("query")} active={view === "query" && !panel}>
          <Database className="size-5" />
        </RailButton>
        <RailButton label={t.nav.graph} onClick={() => onView("graph")} active={view === "graph" && !panel}>
          <Network className="size-5" />
        </RailButton>
        <RailButton label={t.nav.charts} onClick={() => onView("charts")} active={view === "charts" && !panel}>
          <ChartPie className="size-5" />
        </RailButton>
        <div className="mt-auto flex flex-col gap-3">
          <RailButton label={theme === "light" ? t.nav.toDark : t.nav.toLight} onClick={toggleTheme}>
            {theme === "light" ? <Moon className="size-5" /> : <Sun className="size-5" />}
          </RailButton>
          <RailButton label={`${t.nav.language}: ${t.toggle}`} onClick={() => setLang(lang === "zh" ? "en" : "zh")}>
            <span className="flex flex-col items-center leading-none">
              <Languages className="size-4" />
              <span className="mt-1 text-[10px] font-semibold tracking-wide">{lang === "zh" ? "EN" : "中"}</span>
            </span>
          </RailButton>
        </div>
      </nav>

      {/* Workbook list */}
      <aside className={`${view === "graph" || view === "sheets" ? "md:hidden" : "md:flex"} hidden w-72 flex-col border-r border-white/5 bg-white/[0.02]`}>
        <div className="border-b border-white/5 px-3 pb-3 pt-4">
          <ProjectSwitcher {...pc} />
        </div>
        <div className="h-3" />
        <div className="px-4">
          <label className="flex items-center gap-2 rounded-md border border-white/5 bg-white/[0.03] px-3 py-2 text-sm text-zinc-400 focus-within:border-blue-400/40">
            <Search className="size-4" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t.sidebar.search}
              className="w-full bg-transparent text-zinc-200 outline-none placeholder:text-zinc-600"
            />
          </label>
        </div>
        <div className="mt-4 flex items-center px-5">
          <span className="flex-1 text-[11px] font-medium uppercase tracking-wider text-zinc-500">
            {t.projects.filesHeading} <span className="text-zinc-600">· {groups.length}</span>
          </span>
          <button onClick={onNewData} title={t.projects.addFiles} aria-label={t.projects.addFiles}
            className="rounded-md p-1 text-zinc-500 hover:bg-white/10 hover:text-white"><Plus className="size-4" /></button>
        </div>
        <ul className="mt-2 flex-1 space-y-1 overflow-y-auto px-3 pb-4">
          {groups.length === 0 && (workbooks.length ? (
            <li className="px-3 py-6 text-center text-sm text-zinc-600">{t.sidebar.noMatch}</li>
          ) : (
            <li className="mx-1 mt-1 rounded-lg border border-dashed border-white/15 px-4 py-6 text-center">
              <p className="text-sm text-zinc-400">{t.projects.noFiles}</p>
              <button onClick={onNewData} className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500">
                <Plus className="size-3.5" />{t.projects.addFiles}
              </button>
            </li>
          ))}
          {groups.map(({ key, versions }) => {
            const current = versions.find((w) => w.id === activeId);
            const shownVersion = current ?? versions[0];
            const active = !!current;
            const open = expanded.has(key);
            return (
              <li key={key}>
                <div className={`group relative flex w-full items-center rounded-md transition-colors
                    ${active ? "text-white" : "text-zinc-400 hover:bg-white/[0.04] hover:text-zinc-200"}`}>
                  {active && (
                    <motion.span
                      layoutId="wb-active"
                      className="absolute inset-0 rounded-md bg-blue-500/10 ring-1 ring-blue-400/30"
                      transition={{ type: "spring", stiffness: 400, damping: 32 }}
                    />
                  )}
                  <button onClick={() => onSelect(shownVersion.id)} className="relative flex min-w-0 flex-1 items-center gap-3 px-3 py-2.5 text-left">
                    <FileSpreadsheet className={`size-4 shrink-0 ${active ? "text-emerald-300" : ""}`} />
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1.5">
                        <StatusBadge status={shownVersion.status} compact />
                        <span className="truncate text-sm">{shownVersion.name}</span>
                        {versions.length > 1 && (
                          <span className="shrink-0 rounded-md bg-white/5 px-1.5 text-[10px] text-zinc-400 ring-1 ring-white/10">v{shownVersion.version}/{versions.length}</span>
                        )}
                      </span>
                      <span className="block truncate text-[11px] text-zinc-500">
                        {when(shownVersion.last_uploaded ?? shownVersion.built_at)} · <span className="font-mono">{shownVersion.id.slice(0, 7)}</span>
                      </span>
                    </span>
                    {shownVersion.high_issues > 0 && (
                      <span className="rounded-full bg-rose-500/15 px-2 py-0.5 text-[11px] font-medium text-rose-300 ring-1 ring-rose-400/20">
                        {shownVersion.high_issues}
                      </span>
                    )}
                  </button>
                  <span className="relative">
                    <MoveMenu projects={pc.projects} current={pc.activeProject} onMove={(to) => pc.onMove(shownVersion.id, to)} />
                  </span>
                  {versions.length > 1 ? (
                    <button onClick={() => toggle(key)} title={t.sidebar.showVersions} aria-label={t.sidebar.showVersions}
                      className="relative mr-1.5 rounded-md p-1 text-zinc-500 hover:bg-white/10 hover:text-white">
                      <ChevronRight className={`size-4 transition-transform ${open ? "rotate-90" : ""}`} />
                    </button>
                  ) : (
                    <button onClick={() => remove(shownVersion)} title={t.sidebar.remove} aria-label={t.sidebar.remove}
                      className="relative mr-1.5 hidden rounded-md p-1 text-zinc-500 hover:bg-white/10 hover:text-rose-300 group-hover:block">
                      <Trash2 className="size-3.5" />
                    </button>
                  )}
                </div>
                {open && (
                  <ul className="ml-6 mt-1 space-y-0.5 border-l border-white/10 pl-2">
                    <li className="px-2 pb-0.5 text-[10px] uppercase tracking-wider text-zinc-600">{t.sidebar.versions(versions.length)}</li>
                    {versions.map((w) => (
                      <li key={w.id} className={`group/v flex items-center rounded-lg ${w.id === activeId ? "bg-white/[0.06] text-white" : "text-zinc-400 hover:bg-white/[0.04]"}`}>
                        <button onClick={() => onSelect(w.id)} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1 text-left text-xs">
                          <StatusBadge status={w.status} compact />
                          <span className="font-medium">v{w.version}</span>
                          <span className="truncate text-zinc-500">{when(w.first_uploaded)}</span>
                          <span className="ml-auto font-mono text-[10.5px] text-zinc-600">{w.id.slice(0, 7)}</span>
                        </button>
                        <button onClick={() => remove(w)} title={t.sidebar.remove} aria-label={t.sidebar.remove}
                          className="mr-1 hidden rounded p-0.5 text-zinc-500 hover:text-rose-300 group-hover/v:block">
                          <Trash2 className="size-3" />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {confirming && versions.some((w) => w.id === confirming.id) && (
                  <RemoveConfirm workbook={confirming} single={versions.length === 1}
                    onCancel={() => setConfirming(null)} onConfirm={() => { setConfirming(null); onRemove(confirming.id); }} />
                )}
              </li>
            );
          })}
        </ul>
      </aside>
    </div>
  );
}

/** Asks, under a file's row, whether to remove one stored version; Esc or Cancel keeps it. */
function RemoveConfirm({ workbook: w, single, onCancel, onConfirm }: {
  workbook: Workbook; single: boolean; onCancel: () => void; onConfirm: () => void;
}) {
  const { t } = useI18n();
  const s = t.sidebar;
  return (
    <div role="alertdialog" aria-label={s.removeTitle(w.name, w.version, single)}
      onKeyDown={(e) => { if (e.key === "Escape") onCancel(); }}
      className="mx-1 mb-1 mt-1 rounded-md border border-rose-400/30 bg-rose-500/[0.06] p-3 text-xs">
      <p className="flex items-start gap-1.5 font-medium text-zinc-100">
        <Trash2 className="mt-px size-3.5 shrink-0 text-rose-300" /><span className="min-w-0 break-words">{s.removeTitle(w.name, w.version, single)}</span>
      </p>
      <p className="mt-1 leading-relaxed text-zinc-400">{s.removeBody}</p>
      <div className="mt-2.5 flex justify-end gap-2">
        <button autoFocus onClick={onCancel} className="rounded-md px-2.5 py-1 text-zinc-300 hover:bg-white/10">{s.cancel}</button>
        <button onClick={onConfirm} className="rounded-md bg-rose-600 px-2.5 py-1 font-medium text-white hover:bg-rose-500">{s.removeButton}</button>
      </div>
    </div>
  );
}
