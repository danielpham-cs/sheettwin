"use client";

import { Check, ChevronsUpDown, FolderPlus, Pencil, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { DEFAULT_PROJECT, type Project } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

/** What the sidebar, the chat and the context bar need to show and change projects. */
export type ProjectControls = {
  projects: Project[];
  activeProject: string;
  onProject: (id: string) => void;
  onCreateProject: (name: string) => void;
  onRenameProject: (id: string, name: string) => void;
  onDeleteProject: (id: string) => void;
  /** Move a workbook (all its versions) from the active project to `to`. */
  onMove: (workbookId: string, to: string) => void;
  /** How many files each project holds, counting a file once however many versions of it are stored. */
  fileCounts: Record<string, number>;
};

type T = ReturnType<typeof useI18n>["t"];

export function projectName(p: Project, t: T) {
  return p.id === DEFAULT_PROJECT && p.name === "Default" ? t.projects.defaultName : p.name;
}

// One color per project, the same everywhere it is shown, so the project in view is recognized at a glance.
const COLORS = ["#2563eb", "#7c3aed", "#db2777", "#ea580c", "#16a34a", "#0891b2", "#ca8a04", "#4f46e5"];
export function projectColor(id: string) {
  if (id === DEFAULT_PROJECT) return "#64748b";
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return COLORS[h % COLORS.length];
}

/** A colored square with the project's initial. */
export function ProjectBadge({ project, size = "md" }: { project: Project; size?: "sm" | "md" | "lg" }) {
  const { t } = useI18n();
  const initial = [...projectName(project, t).trim()][0]?.toUpperCase() ?? "?";
  const cls = size === "lg" ? "size-9 text-base rounded-lg" : size === "md" ? "size-7 text-sm rounded-md" : "size-5 text-[11px] rounded";
  return (
    <span className={`grid shrink-0 place-items-center font-semibold text-white shadow-sm ${cls}`} style={{ background: projectColor(project.id) }}>
      {initial}
    </span>
  );
}

/** The project in view, as a button (`variant`: the sidebar's large one, or a breadcrumb), and a menu to switch
 * project, make one, rename or delete the one in view. */
export function ProjectSwitcher({ projects, activeProject, onProject, onCreateProject, onRenameProject, onDeleteProject, fileCounts, variant = "sidebar" }:
  Omit<ProjectControls, "onMove"> & { variant?: "sidebar" | "crumb" }) {
  const { t } = useI18n();
  const s = t.projects;
  const [open, setOpen] = useState(false);
  // What the menu shows: the projects, a name field (for a new project or a rename), or the delete confirmation.
  const [mode, setMode] = useState<"list" | "create" | "rename" | "delete">("list");
  const [draft, setDraft] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const close = () => { setOpen(false); setMode("list"); };
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) { setOpen(false); setMode("list"); } };
    // Esc steps back: from a name field or the delete confirmation to the list, from the list to closed.
    const esc = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (mode === "list") setOpen(false); else setMode("list");
    };
    window.addEventListener("mousedown", away);
    window.addEventListener("keydown", esc);
    return () => { window.removeEventListener("mousedown", away); window.removeEventListener("keydown", esc); };
  }, [open, mode]);
  const active = projects.find((p) => p.id === activeProject);
  if (!active) return null;
  const name = projectName(active, t);

  const act = (f: () => void) => () => { close(); f(); };
  const startCreate = () => { setDraft(""); setMode("create"); };
  const startRename = () => { setDraft(name); setMode("rename"); };
  const name_ = draft.trim();
  const save = () => {
    if (!name_) return;
    if (mode === "create") onCreateProject(name_);
    else if (mode === "rename" && name_ !== name) onRenameProject(active.id, name_);
    close();
  };

  return (
    <div ref={box} className="relative min-w-0">
      {variant === "sidebar" ? (
        <button onClick={() => (open ? close() : setOpen(true))} aria-haspopup="menu" aria-expanded={open} title={s.switch}
          className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left transition hover:bg-white/[0.06]">
          <ProjectBadge project={active} size="lg" />
          <span className="min-w-0 flex-1">
            <span className="block text-[10px] font-medium uppercase tracking-wider text-zinc-500">{s.label}</span>
            <span className="block truncate text-[15px] font-semibold text-white">{name}</span>
          </span>
          <ChevronsUpDown className="size-4 shrink-0 text-zinc-500" />
        </button>
      ) : (
        <button onClick={() => (open ? close() : setOpen(true))} aria-haspopup="menu" aria-expanded={open} title={s.switch}
          className="flex min-w-0 items-center gap-2 rounded-md px-1.5 py-1 transition hover:bg-white/[0.06]">
          <ProjectBadge project={active} size="sm" />
          <span className="truncate font-semibold text-white">{name}</span>
          <ChevronsUpDown className="size-3.5 shrink-0 text-zinc-500" />
        </button>
      )}
      {open && (
        <div role="menu"
          className="absolute left-0 top-full z-50 mt-1 w-72 overflow-hidden rounded-lg border border-white/10 bg-zinc-900 py-1 text-sm shadow-2xl">
          {mode === "create" || mode === "rename" ? (
            <form onSubmit={(e) => { e.preventDefault(); save(); }} className="p-3">
              <label className="text-[10px] font-medium uppercase tracking-wider text-zinc-500">
                {mode === "create" ? s.create : s.renameNamed(name)}
              </label>
              <div className="mt-2 flex items-center gap-2 rounded-md border border-blue-400/50 bg-white/[0.04] px-2 py-1.5 focus-within:ring-2 focus-within:ring-blue-500/30">
                <ProjectBadge project={{ ...active, id: mode === "create" ? DEFAULT_PROJECT : active.id, name: name_ || "?" }} size="sm" />
                <input autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} maxLength={80}
                  onFocus={(e) => e.target.select()} placeholder={s.namePlaceholder}
                  className="min-w-0 flex-1 bg-transparent text-zinc-100 outline-none placeholder:text-zinc-600" />
              </div>
              <p className="mt-1.5 text-[11px] text-zinc-500">{s.keysHint}</p>
              <div className="mt-3 flex items-center justify-end gap-2">
                <button type="button" onClick={() => setMode("list")} className="rounded-md px-2.5 py-1 text-xs text-zinc-300 hover:bg-white/10">{s.cancel}</button>
                <button type="submit" disabled={!name_} className="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-40">
                  {mode === "create" ? s.createButton : s.save}
                </button>
              </div>
            </form>
          ) : mode === "delete" ? (
            <div className="p-3">
              <p className="flex items-center gap-2 font-medium text-zinc-100"><Trash2 className="size-4 text-rose-300" />{s.removeNamed(name)}</p>
              <p className="mt-1.5 text-xs leading-relaxed text-zinc-400">{s.removeConfirm(name)}</p>
              <div className="mt-3 flex justify-end gap-2">
                <button autoFocus onClick={() => setMode("list")} className="rounded-md px-2.5 py-1 text-xs text-zinc-300 hover:bg-white/10">{s.cancel}</button>
                <button onClick={act(() => onDeleteProject(active.id))} className="rounded-md bg-rose-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-rose-500">{s.removeButton}</button>
              </div>
            </div>
          ) : (<>
          <p className="px-3 pb-1 pt-1.5 text-[10px] font-medium uppercase tracking-wider text-zinc-500">{s.all}</p>
          <ul className="max-h-72 overflow-y-auto">
            {projects.map((p) => (
              <li key={p.id}>
                <button role="menuitemradio" aria-checked={p.id === active.id} onClick={act(() => onProject(p.id))}
                  className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left hover:bg-white/[0.07] ${p.id === active.id ? "text-white" : "text-zinc-300"}`}>
                  <ProjectBadge project={p} size="sm" />
                  <span className="min-w-0 flex-1 truncate">{projectName(p, t)}</span>
                  <span className="shrink-0 text-xs text-zinc-500">{s.files(fileCounts[p.id] ?? 0)}</span>
                  <Check className={`size-4 text-blue-400 ${p.id === active.id ? "" : "invisible"}`} />
                </button>
              </li>
            ))}
          </ul>
          <div className="my-1 border-t border-white/10" />
          <button role="menuitem" onClick={startCreate} className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-zinc-200 hover:bg-white/[0.07]">
            <FolderPlus className="size-4 text-zinc-400" />{s.create}
          </button>
          <button role="menuitem" onClick={startRename} className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-zinc-200 hover:bg-white/[0.07]">
            <Pencil className="size-4 text-zinc-400" /><span className="truncate">{s.renameNamed(name)}</span>
          </button>
          {active.id !== DEFAULT_PROJECT && (
            <button role="menuitem" onClick={() => setMode("delete")} className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-rose-300 hover:bg-rose-500/10">
              <Trash2 className="size-4" /><span className="truncate">{s.removeNamed(name)}</span>
            </button>
          )}
          </>)}
        </div>
      )}
    </div>
  );
}
