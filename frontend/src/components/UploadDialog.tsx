"use client";

import { CircleAlert, CircleCheck, Copy, FlaskConical, Loader2, UploadCloud, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, type IngestResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

type Props = {
  open: boolean;
  project: string;
  onClose: () => void;
  /** One file (or the sample) was added. */
  onDone: (result: IngestResult) => void;
  /** Several files were added: the ones the server stored, in upload order, and how many were refused. */
  onDoneMany: (results: IngestResult[], refused: number) => void;
};

/** A file of a multi-file upload: waiting, being parsed, stored (parsed, a duplicate, or unparseable), or refused. */
type Item = { name: string; state: "queued" | "parsing" | "done" | "refused"; result?: IngestResult; error?: string };

export function UploadDialog({ open, project, onClose, onDone, onDoneMany }: Props) {
  const { t } = useI18n();
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  function close() {
    setItems([]);
    setError(null);
    onClose();
  }

  const reason = (e: unknown) =>
    e instanceof ApiError && e.kind === "unsupported_type" ? t.failure.unsupported(String(e.info?.ext ?? "")) : (e as Error).message;

  async function run(label: string, task: () => Promise<IngestResult>) {
    setBusy(label);
    setError(null);
    setItems([]);
    try {
      onDone(await task());
      close();
    } catch (e) {
      setError(reason(e));
    } finally {
      setBusy(null);
    }
  }

  // Several files: one after the other (parsing is heavy, and the server numbers uploads in the order they arrive).
  async function runMany(files: File[]) {
    setError(null);
    const list: Item[] = files.map((f) => ({ name: f.name, state: "queued" }));
    setItems(list);
    const update = (i: number, patch: Partial<Item>) => { list[i] = { ...list[i], ...patch }; setItems([...list]); };
    for (let i = 0; i < files.length; i++) {
      setBusy(t.upload.parsingMany(i + 1, files.length, files[i].name));
      update(i, { state: "parsing" });
      try {
        update(i, { state: "done", result: await api.upload(files[i], project) });
      } catch (e) {
        update(i, { state: "refused", error: reason(e) });
      }
    }
    setBusy(null);
    const stored = list.flatMap((x) => (x.result ? [x.result] : []));
    const refused = list.length - stored.length;
    onDoneMany(stored, refused);
    // Close when everything went in cleanly; otherwise keep the list open so the user sees which files did not.
    if (!refused && stored.every((r) => !r.failed)) close();
  }

  function upload(files: FileList | null | undefined) {
    const all = Array.from(files ?? []);
    if (all.length === 1) run(t.upload.parsing(all[0].name), () => api.upload(all[0], project));
    else if (all.length > 1) runMany(all);
  }

  const finished = !busy && items.length > 0;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={() => !busy && close()}
        >
          <motion.div
            onClick={(e) => e.stopPropagation()}
            initial={{ opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ type: "spring", stiffness: 380, damping: 30 }}
            className="field w-full max-w-lg rounded-xl bg-zinc-950/90 p-6 shadow-2xl"
          >
            <div className="mb-5 flex items-start justify-between">
              <div>
                <h2 className="text-lg font-semibold">{t.upload.title}</h2>
                <p className="mt-1 text-sm text-zinc-400">{t.upload.intro}</p>
              </div>
              <button onClick={close} disabled={!!busy} className="rounded-lg p-1.5 text-zinc-500 hover:bg-white/5 hover:text-white">
                <X className="size-5" />
              </button>
            </div>

            <button
              disabled={!!busy}
              onClick={() => input.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setOver(true); }}
              onDragLeave={() => setOver(false)}
              onDrop={(e) => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files); }}
              className={`relative flex w-full flex-col items-center gap-3 overflow-hidden rounded-lg border-2 border-dashed px-6 transition-all
                ${items.length ? "py-6" : "py-12"}
                ${over ? "border-blue-400 bg-blue-500/10" : "border-white/10 bg-white/[0.02] hover:border-blue-400/50 hover:bg-white/[0.04]"}`}
            >
              {busy ? (
                <>
                  <Loader2 className="size-10 animate-spin text-blue-300" />
                  <span className="text-sm text-zinc-300">{busy}</span>
                </>
              ) : (
                <>
                  <span className="grid size-14 place-items-center rounded-lg bg-blue-500/10 ring-1 ring-blue-400/20">
                    <UploadCloud className="size-7 text-blue-200" />
                  </span>
                  <span className="text-sm text-zinc-200">{t.upload.drop}<span className="text-blue-300">{t.upload.choose}</span></span>
                  <span className="text-xs text-zinc-500">{t.upload.hint}</span>
                </>
              )}
            </button>
            <input ref={input} type="file" multiple accept=".xlsx,.xlsm,.xls,.ods,.csv,.tsv" hidden
              onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />

            {items.length > 0 && (
              <ul className="mt-4 max-h-60 space-y-1 overflow-y-auto rounded-lg border border-white/[0.07] bg-white/[0.02] p-2 text-sm">
                {items.map((x, i) => (
                  <li key={i} className="flex items-start gap-2 rounded-md px-2 py-1.5">
                    <ItemIcon item={x} />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-zinc-200">{x.name}</span>
                      <span className={`block text-xs ${x.state === "refused" || x.result?.failed ? "text-rose-300" : "text-zinc-500"}`}>
                        {itemText(x, t)}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}

            {error && (
              <p className="mt-3 rounded-md border border-rose-400/20 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">{error}</p>
            )}

            {finished ? (
              <button onClick={close} className="mt-4 w-full rounded-md bg-blue-600 py-2.5 text-sm font-medium text-white hover:bg-blue-500">
                {t.upload.done}
              </button>
            ) : (
              <>
                <div className="mt-5 flex items-center gap-3">
                  <span className="h-px flex-1 bg-white/10" />
                  <span className="text-xs text-zinc-500">{t.upload.or}</span>
                  <span className="h-px flex-1 bg-white/10" />
                </div>
                <button
                  disabled={!!busy}
                  onClick={() => run(t.upload.sampling, () => api.sample(project))}
                  className="mt-4 flex w-full items-center justify-center gap-2 rounded-md border border-white/10 bg-white/[0.03] py-2.5 text-sm text-zinc-200 transition hover:border-cyan-300/40 hover:bg-cyan-400/5 disabled:opacity-50"
                >
                  <FlaskConical className="size-4 text-cyan-300" />
                  {t.upload.sample}
                </button>
              </>
            )}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function ItemIcon({ item: x }: { item: Item }) {
  const cls = "mt-0.5 size-4 shrink-0";
  if (x.state === "queued") return <span className={`${cls} rounded-full border border-white/20`} />;
  if (x.state === "parsing") return <Loader2 className={`${cls} animate-spin text-blue-300`} />;
  if (x.state === "refused" || x.result?.failed) return <CircleAlert className={`${cls} text-rose-300`} />;
  if (x.result?.duplicate) return <Copy className={`${cls} text-zinc-400`} />;
  return <CircleCheck className={`${cls} text-emerald-300`} />;
}

function itemText(x: Item, t: ReturnType<typeof useI18n>["t"]) {
  const s = t.upload.item;
  if (x.state === "queued") return s.queued;
  if (x.state === "parsing") return s.parsing;
  if (x.state === "refused") return x.error ?? s.failed;
  if (x.result?.failed) return s.failed;
  if (x.result?.duplicate) return s.duplicate;
  return s.done;
}
