"use client";

import {
  Braces, Check, Copy, Database, Download, ExternalLink, Eye, FileCode2, FileSpreadsheet, FileText, GitCompare, Loader2, Sheet, X,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, formatSize, type DiffResult, type ExportFile, type Workbook } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const FORMAT_STYLE: Record<string, { icon: typeof Database; tint: string }> = {
  XLSX: { icon: FileSpreadsheet, tint: "from-emerald-500/30 to-emerald-400/5 text-emerald-300" },
  SQLite: { icon: Database, tint: "from-violet-500/30 to-violet-400/5 text-violet-300" },
  JSON: { icon: Braces, tint: "from-amber-500/30 to-amber-400/5 text-amber-300" },
  CSV: { icon: Sheet, tint: "from-cyan-500/30 to-cyan-400/5 text-cyan-300" },
  HTML: { icon: FileCode2, tint: "from-rose-500/30 to-rose-400/5 text-rose-300" },
  Markdown: { icon: FileText, tint: "from-sky-500/30 to-sky-400/5 text-sky-300" },
};

const PREVIEWABLE = new Set(["JSON", "CSV", "Markdown"]);
const PREVIEW_ROWS = 200;

function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], cell = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { row.push(cell); cell = ""; }
    else if (ch === "\n") { row.push(cell.replace(/\r$/, "")); rows.push(row); row = []; cell = ""; }
    else cell += ch;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}

type Props = {
  open: boolean;
  workbookId: string | null;
  workbookName?: string;
  workbook?: Workbook; // the active result, with its source record
  versions: Workbook[]; // results with the same file name
  onOpenCell: (cell: string) => void;
  onClose: () => void;
};

export function ExportsPanel({ open, workbookId, workbookName, workbook, versions, onOpenCell, onClose }: Props) {
  const { t, lang } = useI18n();
  // Keyed by workbook id so switching workbooks shows the skeleton instead of stale files.
  const [result, setResult] = useState<{ id: string; files?: ExportFile[]; error?: string } | null>(null);
  const current = result && result.id === workbookId ? result : null;
  const files = current?.files ?? null;
  const error = current?.error ?? null;
  const [preview, setPreview] = useState<{ file: ExportFile; text: string } | null>(null);
  const [loadingPreview, setLoadingPreview] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !workbookId) return;
    let live = true;
    api.exports(workbookId, lang)
      .then((f) => live && setResult({ id: workbookId, files: f }))
      .catch((e) => live && setResult({ id: workbookId, error: e.message }));
    return () => { live = false; };
  }, [open, workbookId, lang]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (preview) setPreview(null);
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, preview, onClose]);

  async function openPreview(file: ExportFile) {
    if (!workbookId) return;
    setLoadingPreview(file.name);
    try {
      const text = await (await fetch(api.exportUrl(workbookId, file.name, { lang }))).text();
      setPreview({ file, text: text.replace(/^﻿/, "") });
    } finally {
      setLoadingPreview(null);
    }
  }

  return (
    <>
      <AnimatePresence>
        {open && (
          <motion.section
            initial={{ x: -24, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: -24, opacity: 0 }}
            transition={{ type: "spring", stiffness: 380, damping: 34 }}
            className="absolute inset-y-0 left-[72px] z-40 flex w-[min(440px,calc(100vw-72px))] flex-col border-r border-white/10 bg-zinc-950/85 shadow-2xl shadow-black/60 backdrop-blur-2xl"
          >
            <header className="flex items-start justify-between px-6 pb-4 pt-6">
              <div className="min-w-0">
                <h2 className="text-base font-semibold">{t.exports.title}</h2>
                <p className="mt-0.5 truncate text-xs text-zinc-500">
                  {workbookName ? t.exports.from(workbookName) : t.exports.pick}
                </p>
              </div>
              <button onClick={onClose} className="rounded-lg p-1.5 text-zinc-500 hover:bg-white/5 hover:text-white">
                <X className="size-5" />
              </button>
            </header>

            <div className="flex-1 space-y-2 overflow-y-auto px-4 pb-6">
              {!workbookId && <p className="px-2 py-10 text-center text-sm text-zinc-500">{t.exports.none}</p>}
              {workbook && <SourceCard key={workbook.id} workbook={workbook} versions={versions} onOpenCell={onOpenCell} />}
              {error && <p className="rounded-xl bg-rose-500/10 px-3 py-2 text-sm text-rose-200">{error}</p>}
              {workbookId && !files && !error &&
                Array.from({ length: 5 }).map((_, i) => (
                  <div key={i} className="h-[68px] animate-shimmer rounded-2xl bg-[linear-gradient(90deg,rgb(255_255_255/0.03),rgb(255_255_255/0.07),rgb(255_255_255/0.03))] bg-[length:200%_100%]" />
                ))}
              {files?.map((f, i) => {
                const style = FORMAT_STYLE[f.format] ?? FORMAT_STYLE.CSV;
                const Icon = style.icon;
                return (
                  <motion.div
                    key={f.name}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: i * 0.04 }}
                    className="group flex items-center gap-3 rounded-2xl border border-white/5 bg-white/[0.03] p-3 transition hover:border-white/10 hover:bg-white/[0.06]"
                  >
                    <span className={`grid size-11 shrink-0 place-items-center rounded-xl bg-gradient-to-br ring-1 ring-white/10 ${style.tint}`}>
                      <Icon className="size-5" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium text-zinc-100">{t.exports.labels[f.source ? "source" : f.name] ?? f.label}</span>
                        {f.source && <span className="rounded-md bg-white/5 px-1.5 text-[10px] text-zinc-400">{t.exports.original}</span>}
                      </span>
                      <span className="block truncate font-mono text-[11px] text-zinc-500">
                        {f.format} · {formatSize(f.size)}
                      </span>
                    </span>
                    {f.format === "HTML" && (
                      <a
                        href={api.exportUrl(workbookId!, f.name, { lang, inline: true })}
                        target="_blank"
                        rel="noopener"
                        aria-label={t.exports.openTab}
                        title={t.exports.openTab}
                        className="rounded-lg p-2 text-zinc-400 opacity-70 transition hover:bg-white/10 hover:text-white group-hover:opacity-100"
                      >
                        <ExternalLink className="size-4" />
                      </a>
                    )}
                    {PREVIEWABLE.has(f.format) && (
                      <button
                        onClick={() => openPreview(f)}
                        aria-label={t.exports.preview}
                        className="rounded-lg p-2 text-zinc-400 opacity-70 transition hover:bg-white/10 hover:text-white group-hover:opacity-100"
                      >
                        {loadingPreview === f.name ? <Loader2 className="size-4 animate-spin" /> : <Eye className="size-4" />}
                      </button>
                    )}
                    <a
                      href={api.exportUrl(workbookId!, f.name, { lang })}
                      aria-label={t.exports.download}
                      className="rounded-lg p-2 text-zinc-400 opacity-70 transition hover:bg-white/10 hover:text-white group-hover:opacity-100"
                    >
                      <Download className="size-4" />
                    </a>
                  </motion.div>
                );
              })}
            </div>
          </motion.section>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {preview && (
          <motion.div
            className="fixed inset-0 z-[60] grid place-items-center bg-black/70 p-4 backdrop-blur-sm sm:p-8"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setPreview(null)}
          >
            <motion.div
              onClick={(e) => e.stopPropagation()}
              initial={{ scale: 0.96, y: 10 }}
              animate={{ scale: 1, y: 0 }}
              exit={{ scale: 0.97, y: 6 }}
              className="flex max-h-full w-full max-w-5xl flex-col overflow-hidden rounded-3xl border border-white/10 bg-zinc-950 shadow-2xl"
            >
              <header className="flex items-center justify-between border-b border-white/5 px-6 py-4">
                <div>
                  <h3 className="font-medium">{t.exports.labels[preview.file.source ? "source" : preview.file.name] ?? preview.file.label}</h3>
                  <p className="font-mono text-xs text-zinc-500">{preview.file.name}</p>
                </div>
                <button onClick={() => setPreview(null)} className="rounded-lg p-1.5 text-zinc-500 hover:bg-white/5 hover:text-white">
                  <X className="size-5" />
                </button>
              </header>
              <div className="overflow-auto">
                {preview.file.format === "JSON" ? (
                  <pre className="p-6 font-mono text-xs leading-relaxed text-zinc-300">{preview.text}</pre>
                ) : preview.file.format === "Markdown" ? (
                  <div className="md p-6">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{preview.text}</ReactMarkdown>
                  </div>
                ) : (
                  <CsvTable rows={parseCsv(preview.text)} />
                )}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

function CsvTable({ rows }: { rows: string[][] }) {
  const { t } = useI18n();
  const [head, ...body] = rows;
  return (
    <>
      <table className="w-full border-collapse text-xs">
        <thead className="sticky top-0 bg-zinc-900">
          <tr>{head?.map((h) => <th key={h} className="whitespace-nowrap px-3 py-2 text-left font-medium text-zinc-400">{h}</th>)}</tr>
        </thead>
        <tbody>
          {body.slice(0, PREVIEW_ROWS).map((r, i) => (
            <tr key={i} className="border-t border-white/5 hover:bg-white/[0.03]">
              {r.map((c, j) => <td key={j} className="max-w-xs truncate px-3 py-1.5 font-mono text-zinc-300">{c}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {body.length > PREVIEW_ROWS && (
        <p className="px-4 py-3 text-center text-xs text-zinc-500">{t.exports.previewMore(PREVIEW_ROWS, body.length)}</p>
      )}
    </>
  );
}

/** Where this result came from: the file (name, hash, size, times), how it was parsed, and how it differs from other
 * versions of the same file name. */
function SourceCard({ workbook: w, versions, onOpenCell }: { workbook: Workbook; versions: Workbook[]; onOpenCell: (cell: string) => void }) {
  const { t } = useI18n();
  const s = t.exports.source;
  const others = versions.filter((v) => v.id !== w.id).sort((a, b) => b.version - a.version);
  const [against, setAgainst] = useState<string | null>(w.previous ?? others[0]?.id ?? null);
  const [diff, setDiff] = useState<{ key: string; result: DiffResult } | null>(null);
  const [copied, setCopied] = useState(false);
  const key = against ? `${w.id}|${against}` : null;
  useEffect(() => {
    if (!against) return;
    let live = true;
    const k = `${w.id}|${against}`;
    api.diff(w.id, against).then((r) => live && setDiff({ key: k, result: r }), () => {});
    return () => { live = false; };
  }, [w.id, against]);
  const d = diff && diff.key === key ? diff.result : null;
  const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString(t.locale, { dateStyle: "medium", timeStyle: "short" }) : s.unknown);
  const row = (label: string, value: React.ReactNode) => (
    <><dt className="text-zinc-500">{label}</dt><dd className="min-w-0 text-zinc-200">{value}</dd></>
  );

  return (
    <div className="mb-3 rounded-2xl border border-white/10 bg-white/[0.03] p-4 text-xs">
      <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{s.title}</div>
      <div className="flex items-center gap-2">
        <FileSpreadsheet className="size-4 shrink-0 text-emerald-300" />
        <span className="truncate text-sm font-medium text-zinc-100">{w.name}</span>
        <span className="shrink-0 rounded-md bg-white/5 px-1.5 text-[10.5px] text-zinc-400 ring-1 ring-white/10">{s.version(w.version, w.versions)}</span>
      </div>
      {w.aliases.length > 0 && <p className="mt-1 text-zinc-500">{s.alsoAs(w.aliases.join(", "))}</p>}
      <dl className="mt-3 grid grid-cols-[96px_1fr] gap-x-3 gap-y-1.5">
        {row(s.sha, (
          <span className="flex items-center gap-1.5">
            <span className="truncate font-mono text-[11px]" title={w.sha256}><b className="font-semibold text-violet-200">{w.sha256.slice(0, 12)}</b>{w.sha256.slice(12)}</span>
            <button onClick={() => { navigator.clipboard?.writeText(w.sha256); setCopied(true); setTimeout(() => setCopied(false), 1200); }}
              title={copied ? s.copied : s.copy} className="shrink-0 rounded p-0.5 text-zinc-500 hover:text-white">
              {copied ? <Check className="size-3.5 text-emerald-300" /> : <Copy className="size-3.5" />}
            </button>
          </span>
        ))}
        {row(s.size, w.size != null ? formatSize(w.size) : s.unknown)}
        {row(s.modified, when(w.file_modified))}
        {row(s.firstUploaded, when(w.first_uploaded))}
        {row(s.lastUploaded, <>{when(w.last_uploaded)}{w.uploads > 1 && <span className="text-zinc-500"> · {s.uploads(w.uploads)}</span>}</>)}
        {row(s.parsed, <>{when(w.built_at)}<span className="block text-zinc-500">{s.parsedWith(w.sheetir_version, w.ir_schema, w.table_scorer)}</span></>)}
      </dl>
      <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">{s.idHint}</p>

      <div className="mt-3 border-t border-white/5 pt-3">
        <div className="flex items-center gap-2">
          <GitCompare className="size-3.5 text-cyan-300" />
          <span className="font-medium text-zinc-200">{s.changes}</span>
          {others.length > 0 && (
            <select value={against ?? ""} onChange={(e) => setAgainst(e.target.value)}
              className="ml-auto rounded-md border border-white/10 bg-zinc-900 px-1.5 py-0.5 text-[11px] text-zinc-200">
              {others.map((v) => <option key={v.id} value={v.id}>{s.against} v{v.version} · {v.id.slice(0, 7)}</option>)}
            </select>
          )}
        </div>
        {others.length === 0 ? <p className="mt-1.5 text-zinc-500">{s.onlyOne}</p> : !d ? (
          <Loader2 className="mt-2 size-4 animate-spin text-zinc-500" />
        ) : d.identical ? (
          <p className="mt-1.5 text-emerald-200/90">{s.identical}</p>
        ) : (
          <>
            <p className="mt-1.5 text-zinc-300">{s.counts(d.counts)}</p>
            {d.sheets_added.length > 0 && <p className="text-zinc-400">{s.sheetsAdded(d.sheets_added.join(", "))}</p>}
            {d.sheets_removed.length > 0 && <p className="text-zinc-400">{s.sheetsRemoved(d.sheets_removed.join(", "))}</p>}
            <ul className="mt-2 max-h-52 space-y-0.5 overflow-y-auto">
              {d.cells.map((c) => (
                <li key={c.cell_id} className="flex items-baseline gap-2">
                  {c.change === "removed"
                    ? <span className="font-mono text-zinc-500 line-through">{c.cell_id}</span>
                    : <button onClick={() => onOpenCell(c.cell_id)} className="font-mono text-violet-300 hover:underline">{c.cell_id}</button>}
                  <span className={c.change === "added" ? "text-emerald-300" : c.change === "removed" ? "text-rose-300" : "text-amber-200"}>{s.kinds[c.change]}</span>
                  <span className="min-w-0 truncate font-mono text-[11px] text-zinc-500" title={`${c.old ?? ""} → ${c.new ?? ""}`}>
                    {c.change === "changed" ? `${c.old} → ${c.new}` : c.new ?? c.old}
                  </span>
                </li>
              ))}
            </ul>
            {d.truncated && <p className="mt-1 text-zinc-500">{s.more}</p>}
          </>
        )}
      </div>
    </div>
  );
}
