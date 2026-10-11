"use client";

import { Braces, Database, Download, Eye, FileSpreadsheet, Loader2, Sheet, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { api, formatSize, type ExportFile } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const FORMAT_STYLE: Record<string, { icon: typeof Database; tint: string }> = {
  XLSX: { icon: FileSpreadsheet, tint: "from-emerald-500/30 to-emerald-400/5 text-emerald-300" },
  SQLite: { icon: Database, tint: "from-violet-500/30 to-violet-400/5 text-violet-300" },
  JSON: { icon: Braces, tint: "from-amber-500/30 to-amber-400/5 text-amber-300" },
  CSV: { icon: Sheet, tint: "from-cyan-500/30 to-cyan-400/5 text-cyan-300" },
};

const PREVIEWABLE = new Set(["JSON", "CSV"]);
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

type Props = { open: boolean; workbookId: string | null; workbookName?: string; onClose: () => void };

export function ExportsPanel({ open, workbookId, workbookName, onClose }: Props) {
  const { t } = useI18n();
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
    api.exports(workbookId)
      .then((f) => live && setResult({ id: workbookId, files: f }))
      .catch((e) => live && setResult({ id: workbookId, error: e.message }));
    return () => { live = false; };
  }, [open, workbookId]);

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
      const text = await (await fetch(api.exportUrl(workbookId, file.name))).text();
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
            className="absolute inset-y-0 left-[72px] z-40 flex w-[min(400px,calc(100vw-72px))] flex-col border-r border-white/10 bg-zinc-950/85 shadow-2xl shadow-black/60 backdrop-blur-2xl"
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
                      href={api.exportUrl(workbookId!, f.name)}
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
