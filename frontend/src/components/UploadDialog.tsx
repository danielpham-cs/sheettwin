"use client";

import { FlaskConical, Loader2, UploadCloud, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, type IngestResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type Props = { open: boolean; onClose: () => void; onDone: (result: IngestResult) => void };

export function UploadDialog({ open, onClose, onDone }: Props) {
  const { t } = useI18n();
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onClose]);

  async function run(label: string, task: () => Promise<IngestResult>) {
    setBusy(label);
    setError(null);
    try {
      onDone(await task());
      onClose();
    } catch (e) {
      setError(e instanceof ApiError && e.kind === "unsupported_type" ? t.failure.unsupported(String(e.info?.ext ?? "")) : (e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const upload = (file?: File) => file && run(t.upload.parsing(file.name), () => api.upload(file));

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={() => !busy && onClose()}
        >
          <motion.div
            onClick={(e) => e.stopPropagation()}
            initial={{ opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ type: "spring", stiffness: 380, damping: 30 }}
            className="glow-border w-full max-w-lg rounded-3xl bg-zinc-950/90 p-6 shadow-2xl shadow-violet-900/30"
          >
            <div className="mb-5 flex items-start justify-between">
              <div>
                <h2 className="text-lg font-semibold">{t.upload.title}</h2>
                <p className="mt-1 text-sm text-zinc-400">{t.upload.intro}</p>
              </div>
              <button onClick={onClose} disabled={!!busy} className="rounded-lg p-1.5 text-zinc-500 hover:bg-white/5 hover:text-white">
                <X className="size-5" />
              </button>
            </div>

            <button
              disabled={!!busy}
              onClick={() => input.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setOver(true); }}
              onDragLeave={() => setOver(false)}
              onDrop={(e) => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files[0]); }}
              className={`relative flex w-full flex-col items-center gap-3 overflow-hidden rounded-2xl border-2 border-dashed px-6 py-12 transition-all
                ${over ? "scale-[1.01] border-violet-400 bg-violet-500/10" : "border-white/10 bg-white/[0.02] hover:border-violet-400/50 hover:bg-white/[0.04]"}`}
            >
              {busy ? (
                <>
                  <Loader2 className="size-10 animate-spin text-violet-300" />
                  <span className="text-sm text-zinc-300">{busy}</span>
                </>
              ) : (
                <>
                  <span className="grid size-14 place-items-center rounded-2xl bg-gradient-to-br from-violet-500/30 to-cyan-400/20 ring-1 ring-white/10">
                    <UploadCloud className="size-7 text-violet-200" />
                  </span>
                  <span className="text-sm text-zinc-200">{t.upload.drop}<span className="text-violet-300">{t.upload.choose}</span></span>
                  <span className="text-xs text-zinc-500">{t.upload.hint}</span>
                </>
              )}
            </button>
            <input ref={input} type="file" accept=".xlsx,.xlsm,.xls,.ods,.csv,.tsv" hidden onChange={(e) => { upload(e.target.files?.[0]); e.target.value = ""; }} />

            {error && (
              <p className="mt-3 rounded-xl border border-rose-400/20 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">{error}</p>
            )}

            <div className="mt-5 flex items-center gap-3">
              <span className="h-px flex-1 bg-white/10" />
              <span className="text-xs text-zinc-500">{t.upload.or}</span>
              <span className="h-px flex-1 bg-white/10" />
            </div>
            <button
              disabled={!!busy}
              onClick={() => run(t.upload.sampling, api.sample)}
              className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/[0.03] py-2.5 text-sm text-zinc-200 transition hover:border-cyan-300/40 hover:bg-cyan-400/5 disabled:opacity-50"
            >
              <FlaskConical className="size-4 text-cyan-300" />
              {t.upload.sample}
            </button>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
