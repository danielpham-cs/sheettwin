"use client";

import { BookOpen, ChevronDown, FileText, Loader2, Plus, ScrollText, Trash2, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { ProjectBadge, projectName } from "@/components/ProjectMenu";
import { api, ApiError, formatSize, type KnowledgeDoc, type Project, type ProjectKnowledge } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type Props = {
  open: boolean;
  project: Project | null;
  /** The project's knowledge as last fetched (null while loading). */
  knowledge: ProjectKnowledge | null;
  onClose: () => void;
  /** Something changed: fetch the knowledge again. */
  onChanged: () => Promise<void>;
};

const ACCEPT = ".md,.markdown,.txt,.docx,.pdf";

/** The project's context for the AI: instructions the user writes, and documents it reads (a slide-over on the right). */
export function KnowledgePanel({ open, project, knowledge, onClose, onChanged }: Props) {
  const { t } = useI18n();
  const s = t.knowledge;

  useEffect(() => {
    if (!open) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && project && (
        <>
          <motion.div className="fixed inset-0 z-40 bg-black/30" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} />
          <motion.aside
            role="dialog" aria-label={s.title}
            initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }} transition={{ type: "spring", stiffness: 380, damping: 38 }}
            className="fixed inset-y-0 right-0 z-50 flex w-full max-w-md flex-col border-l border-white/10 bg-canvas shadow-2xl"
          >
            <header className="flex items-center gap-3 border-b border-white/5 px-5 py-4">
              <ProjectBadge project={project} size="md" />
              <div className="min-w-0 flex-1">
                <h2 className="truncate font-semibold text-white">{s.title}</h2>
                <p className="truncate text-xs text-zinc-500">{projectName(project, t)}</p>
              </div>
              <button onClick={onClose} aria-label={s.close} className="rounded-md p-1.5 text-zinc-500 hover:bg-white/10 hover:text-white"><X className="size-5" /></button>
            </header>
            <div className="flex-1 overflow-y-auto px-5 py-5">
              <p className="text-sm leading-relaxed text-zinc-400">{s.intro}</p>
              {knowledge ? (
                <>
                  {/* keyed by project: a draft never leaks into another project */}
                  <InstructionsEditor key={project.id} projectId={project.id} saved={knowledge.instructions}
                    limit={knowledge.limits.instructions} onSaved={onChanged} />
                  <Documents projectId={project.id} docs={knowledge.docs} maxBytes={knowledge.limits.doc_bytes} onChanged={onChanged} />
                </>
              ) : (
                <div className="grid h-40 place-items-center"><Loader2 className="size-5 animate-spin text-zinc-500" /></div>
              )}
            </div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}

function InstructionsEditor({ projectId, saved, limit, onSaved }: { projectId: string; saved: string; limit: number; onSaved: () => Promise<void> }) {
  const { t } = useI18n();
  const s = t.knowledge;
  const [draft, setDraft] = useState(saved);
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState<"idle" | "saved" | string>("idle");
  const dirty = draft.trim() !== saved.trim();

  async function save() {
    setBusy(true);
    try {
      const r = await api.setInstructions(projectId, draft);
      setDraft(r.instructions);
      setState("saved");
      await onSaved();
    } catch (e) {
      setState((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-6">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-white"><ScrollText className="size-4 text-blue-300" />{s.instructions}</h3>
      <p className="mt-1 text-xs text-zinc-500">{s.instructionsHint}</p>
      <textarea
        value={draft}
        onChange={(e) => { setDraft(e.target.value); setState("idle"); }}
        onKeyDown={(e) => { if (e.key === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (dirty) save(); } }}
        maxLength={limit}
        rows={7}
        placeholder={s.instructionsPlaceholder}
        className="field mt-2 w-full resize-y rounded-lg bg-white/[0.03] px-3 py-2.5 text-sm leading-relaxed text-zinc-100 outline-none placeholder:text-zinc-600 focus:ring-2 focus:ring-blue-500/30"
      />
      <div className="mt-2 flex items-center gap-2 text-xs">
        <span className="text-zinc-600">{draft.length.toLocaleString()} / {limit.toLocaleString()}</span>
        {state === "saved" && !dirty && <span className="text-emerald-300">{s.saved}</span>}
        {state !== "idle" && state !== "saved" && <span className="text-rose-300">{state}</span>}
        <span className="ml-auto" />
        {dirty && <button onClick={() => { setDraft(saved); setState("idle"); }} className="rounded-md px-2.5 py-1 text-zinc-300 hover:bg-white/10">{s.discard}</button>}
        <button onClick={save} disabled={!dirty || busy}
          className="flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1 font-medium text-white hover:bg-blue-500 disabled:opacity-40">
          {busy && <Loader2 className="size-3.5 animate-spin" />}{s.save}
        </button>
      </div>
    </section>
  );
}

function Documents({ projectId, docs, maxBytes, onChanged }: { projectId: string; docs: KnowledgeDoc[]; maxBytes: number; onChanged: () => Promise<void> }) {
  const { t } = useI18n();
  const s = t.knowledge;
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [adding, setAdding] = useState<string | null>(null);
  const [errors, setErrors] = useState<{ name: string; message: string }[]>([]);

  const reason = (e: unknown) => {
    const kind = e instanceof ApiError ? e.kind : undefined;
    return kind && kind in s.errors ? s.errors[kind as keyof typeof s.errors] : (e as Error).message;
  };

  async function add(files: FileList | null) {
    const list = Array.from(files ?? []);
    if (!list.length) return;
    const failed: { name: string; message: string }[] = [];
    for (const f of list) {  // one after another: each is read and its text extracted on the server
      setAdding(f.name);
      if (f.size > maxBytes) { failed.push({ name: f.name, message: s.errors.too_large }); continue; }
      try { await api.addDoc(projectId, f); } catch (e) { failed.push({ name: f.name, message: reason(e) }); }
    }
    setAdding(null);
    setErrors(failed);
    await onChanged();
  }

  return (
    <section className="mt-8">
      <div className="flex items-center">
        <h3 className="flex flex-1 items-center gap-2 text-sm font-semibold text-white">
          <BookOpen className="size-4 text-blue-300" />{s.documents}<span className="font-normal text-zinc-500">· {docs.length}</span>
        </h3>
        <button onClick={() => input.current?.click()} disabled={!!adding}
          className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-zinc-300 hover:bg-white/10 disabled:opacity-50"><Plus className="size-3.5" />{s.add}</button>
      </div>
      <p className="mt-1 text-xs text-zinc-500">{s.documentsHint}</p>
      <input ref={input} type="file" multiple accept={ACCEPT} hidden onChange={(e) => { add(e.target.files); e.target.value = ""; }} />

      <button
        onClick={() => input.current?.click()}
        disabled={!!adding}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); add(e.dataTransfer.files); }}
        className={`mt-3 flex w-full items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-4 text-xs transition
          ${over ? "border-blue-400 bg-blue-500/10 text-blue-100" : "border-white/10 text-zinc-400 hover:border-blue-400/40 hover:bg-white/[0.03]"}`}
      >
        {adding ? <><Loader2 className="size-4 animate-spin text-blue-300" />{s.reading(adding)}</> : <><FileText className="size-4" />{s.drop}</>}
      </button>

      {errors.map((x) => (
        <p key={x.name} className="mt-2 rounded-md border border-rose-400/20 bg-rose-500/10 px-3 py-1.5 text-xs text-rose-200"><b>{x.name}</b>: {x.message}</p>
      ))}

      <ul className="mt-3 space-y-1.5">
        {docs.map((d) => <DocRow key={d.id} projectId={projectId} doc={d} onChanged={onChanged} />)}
      </ul>
    </section>
  );
}

function DocRow({ projectId, doc: d, onChanged }: { projectId: string; doc: KnowledgeDoc; onChanged: () => Promise<void> }) {
  const { t } = useI18n();
  const s = t.knowledge;
  const [preview, setPreview] = useState<string | null>(null);
  const [showing, setShowing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const ext = d.name.split(".").pop()?.toUpperCase() ?? "";

  async function toggle() {
    if (!showing && preview === null) setPreview((await api.docText(projectId, d.id)).text);
    setShowing((v) => !v);
  }

  return (
    <li className="rounded-lg border border-white/[0.07] bg-white/[0.02]">
      <div className="group flex items-center gap-2.5 px-3 py-2">
        <span className="grid size-8 shrink-0 place-items-center rounded-md bg-blue-500/10 text-[9px] font-bold text-blue-200 ring-1 ring-blue-400/20">{ext}</span>
        <button onClick={toggle} className="min-w-0 flex-1 text-left" title={s.preview}>
          <span className="block truncate text-sm text-zinc-100">{d.name}</span>
          <span className="block text-[11px] text-zinc-500">
            {formatSize(d.size)} · {s.chars(d.chars)}{d.truncated ? ` · ${s.truncated}` : ""}
          </span>
        </button>
        <button onClick={toggle} aria-label={s.preview} className="rounded-md p-1 text-zinc-500 hover:bg-white/10 hover:text-white">
          <ChevronDown className={`size-4 transition-transform ${showing ? "rotate-180" : ""}`} />
        </button>
        <button onClick={() => setConfirming(true)} aria-label={s.remove} title={s.remove}
          className="rounded-md p-1 text-zinc-500 hover:bg-white/10 hover:text-rose-300"><Trash2 className="size-3.5" /></button>
      </div>
      {confirming && (
        <div className="flex items-center gap-2 border-t border-rose-400/20 bg-rose-500/[0.06] px-3 py-2 text-xs" onKeyDown={(e) => e.key === "Escape" && (e.stopPropagation(), setConfirming(false))}>
          <span className="flex-1 text-zinc-200">{s.removeConfirm}</span>
          <button autoFocus onClick={() => setConfirming(false)} className="rounded-md px-2 py-0.5 text-zinc-300 hover:bg-white/10">{t.sidebar.cancel}</button>
          <button onClick={async () => { await api.removeDoc(projectId, d.id); await onChanged(); }}
            className="rounded-md bg-rose-600 px-2 py-0.5 font-medium text-white hover:bg-rose-500">{t.sidebar.removeButton}</button>
        </div>
      )}
      {showing && preview !== null && (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap border-t border-white/5 px-3 py-2 font-sans text-xs leading-relaxed text-zinc-400">
          {preview.slice(0, 6000)}{preview.length > 6000 ? "\n…" : ""}
        </pre>
      )}
    </li>
  );
}
