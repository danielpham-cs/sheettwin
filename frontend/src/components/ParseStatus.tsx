"use client";

import { AlertTriangle, CheckCircle2, CircleDashed, CircleHelp, FileX2, Loader2, RefreshCw, ShieldCheck, Trash2, UploadCloud, Wand2, X, XCircle } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { api, type FixSuggestion, type FixSuggestions, type IngestResult, type ParseError, type ParseStatus, type Summary, type Workbook } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const STYLE: Record<ParseStatus, { icon: typeof CheckCircle2; tone: string }> = {
  verified: { icon: CheckCircle2, tone: "text-emerald-300 bg-emerald-400/10 ring-emerald-400/25" },
  partial: { icon: AlertTriangle, tone: "text-amber-200 bg-amber-400/10 ring-amber-300/25" },
  check_failed: { icon: XCircle, tone: "text-rose-300 bg-rose-500/10 ring-rose-400/25" },
  unverified: { icon: CircleDashed, tone: "text-zinc-300 bg-white/5 ring-white/10" },
  failed: { icon: FileX2, tone: "text-rose-300 bg-rose-500/10 ring-rose-400/25" },
};

/** The parse status as a pill (with label) or a bare icon. */
export function StatusBadge({ status, compact, onClick }: { status: ParseStatus; compact?: boolean; onClick?: () => void }) {
  const { t } = useI18n();
  const { icon: Icon, tone } = STYLE[status] ?? STYLE.unverified;
  const label = t.status.label[status] ?? status;
  if (compact) return <span title={`${label}: ${t.status.hint[status] ?? ""}`}><Icon className={`size-3.5 ${tone.split(" ")[0]}`} aria-label={label} /></span>;
  const body = <><Icon className="size-3.5" />{label}</>;
  const cls = `inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs ring-1 ${tone}`;
  return onClick
    ? <button onClick={onClick} title={t.status.hint[status]} className={`${cls} transition hover:brightness-125`}>{body}</button>
    : <span title={t.status.hint[status]} className={cls}>{body}</span>;
}

export function Modal({ open, onClose, children }: { open: boolean; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-[60] grid place-items-center bg-black/70 p-4 backdrop-blur-sm"
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose}>
          <motion.div onClick={(e) => e.stopPropagation()} initial={{ scale: 0.96, y: 10 }} animate={{ scale: 1, y: 0 }} exit={{ scale: 0.97, y: 6 }}
            className="flex max-h-full w-full max-w-2xl flex-col overflow-hidden rounded-3xl border border-white/10 bg-zinc-950 shadow-2xl">
            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function Line({ ok, children }: { ok: boolean | "warn" | undefined; children: React.ReactNode }) {
  return (
    <li className="flex items-start gap-2.5">
      {ok === undefined ? <CircleHelp className="mt-0.5 size-4 shrink-0 text-zinc-500" />
        : ok === "warn" ? <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-200" />
        : ok ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-300" /> : <XCircle className="mt-0.5 size-4 shrink-0 text-rose-300" />}
      <span className="min-w-0">{children}</span>
    </li>
  );
}

/** What the independent check found, how much of the workbook SheetIR understands, and what it leaves out. */
export function ParseReport({ open, summary, onClose, onReparse, onOpenCell, onFixed }: {
  open: boolean; summary: Summary | null; onClose: () => void; onReparse: () => Promise<void>; onOpenCell: (cell: string) => void;
  onFixed: (result: IngestResult & { applied: string[] }) => Promise<void>;
}) {
  const { t } = useI18n();
  const r = t.report;
  const [busy, setBusy] = useState(false);
  const v = summary?.verification ?? null;
  const status = summary?.status ?? "unverified";
  const c = v?.checks ?? {};
  const differences = [
    ...(c.content?.wrong ?? []).map((w) => [w.cell, `Excel: ${w.excel}`, `IR: ${w.ir}`]),
    ...(c.content?.missing ?? []).map((m) => [m, "Excel", "IR: —"]),
    ...(c.references?.differ ?? []).map((d) => [d.cell, d.only_tokenizer.join(" "), d.only_ir.join(" ") || "—"]),
    ...(c.groups?.bad ?? []).map((b) => [b.cell, b.expected, b.actual ?? "—"]),
  ];
  return (
    <Modal open={open} onClose={onClose}>
      <header className="flex items-start gap-3 border-b border-white/5 px-6 py-4">
        <ShieldCheck className="mt-0.5 size-5 shrink-0 text-violet-300" />
        <div className="min-w-0 flex-1">
          <h3 className="font-medium">{r.title}{summary ? ` · ${summary.name}` : ""}</h3>
          <p className="mt-0.5 text-xs text-zinc-500">{r.sub}</p>
        </div>
        <button onClick={onClose} className="rounded-lg p-1.5 text-zinc-500 hover:bg-white/5 hover:text-white"><X className="size-5" /></button>
      </header>
      <div className="space-y-5 overflow-y-auto px-6 py-5 text-sm">
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge status={status} />
          <span className="text-zinc-400">{t.status.hint[status]}</span>
          {v?.seconds != null && <span className="text-xs text-zinc-600">{r.seconds(v.seconds)}</span>}
        </div>

        {v?.error && <p className="rounded-xl bg-rose-500/10 px-3 py-2 text-rose-200">{r.notRun(v.error)}</p>}

        {v?.checks && c.content && (
          <section>
            <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{r.checks}</h4>
            <ul className="space-y-1.5 text-zinc-200">
              <Line ok={c.content.pass}>{r.content(c.content.matched, c.content.cells)}</Line>
              {c.rebuild && <Line ok={c.rebuild.pass}>{r.rebuild(c.rebuild.identical, c.rebuild.cells)}</Line>}
              {c.references && <Line ok={c.references.pass}>{c.references.checked ? r.references(c.references.agree, c.references.checked, c.references.skipped) : r.referencesNone}</Line>}
              {c.groups && <Line ok={c.groups.pass}>{c.groups.members ? r.groups(c.groups.ok, c.groups.members, c.groups.groups) : r.groupsNone}</Line>}
            </ul>
          </section>
        )}

        {differences.length > 0 && (
          <section>
            <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-rose-300">{r.differences}</h4>
            <div className="overflow-x-auto rounded-xl border border-white/5">
              <table className="w-full text-xs">
                <tbody>{differences.map(([cell, a, b], i) => (
                  <tr key={i} className="border-t border-white/5 first:border-0">
                    <td className="px-3 py-1.5"><button onClick={() => onOpenCell(cell)} className="font-mono text-violet-300 hover:underline">{cell}</button></td>
                    <td className="px-3 py-1.5 font-mono text-zinc-400">{a}</td><td className="px-3 py-1.5 font-mono text-zinc-400">{b}</td>
                  </tr>))}</tbody>
              </table>
            </div>
          </section>
        )}

        {v?.coverage && (
          <section>
            <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{r.coverage}</h4>
            <ul className="space-y-1.5 text-zinc-200">
              <Line ok={v.coverage.understood === v.coverage.formulas || "warn"}>{r.understood(v.coverage.understood, v.coverage.formulas)}</Line>
            </ul>
            {v.coverage.not_understood.length > 0 && summary && (
              <FixList key={summary.id} workbookId={summary.id} onOpenCell={onOpenCell} onFixed={onFixed} fallback={
              <div className="mt-3">
                <div className="mb-1.5 text-xs text-zinc-400">{r.notUnderstood}</div>
                <ul className="space-y-1.5">
                  {v.coverage.not_understood.map((n) => (
                    <li key={n.cell + n.kind} className="rounded-xl bg-amber-400/[0.06] px-3 py-2 ring-1 ring-amber-300/15">
                      <button onClick={() => onOpenCell(n.cell)} className="font-mono text-xs text-violet-300 hover:underline">{n.cell}</button>
                      <span className="ml-2 text-xs text-amber-200">{n.kind.replace(/_/g, " ")}</span>
                      <div className="mt-0.5 text-xs text-zinc-400">{t.issueHelp[n.kind] ?? ""} <span className="font-mono text-zinc-500">{n.detail}</span></div>
                    </li>
                  ))}
                </ul>
              </div>} />
            )}
            <div className="mt-3">
              <div className="mb-1.5 text-xs text-zinc-400">{r.ignored}</div>
              {v.coverage.ignored.length ? (
                <ul className="space-y-1 text-xs text-zinc-300">
                  {v.coverage.ignored.map((x) => <li key={x.kind}>· {x.count} × {r.ignoredKinds[x.kind] ?? x.kind}</li>)}
                </ul>
              ) : <p className="text-xs text-zinc-500">{r.none}</p>}
            </div>
          </section>
        )}

        {(summary?.load_warnings ?? []).map((w) => (
          <p key={w} className="flex items-start gap-2 rounded-xl bg-amber-400/10 px-3 py-2 text-xs text-amber-100">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />{r.warnings[w] ?? w}
          </p>
        ))}
      </div>
      <footer className="flex justify-end border-t border-white/5 px-6 py-3">
        <button disabled={busy} onClick={async () => { setBusy(true); try { await onReparse(); } finally { setBusy(false); } }}
          className="flex items-center gap-1.5 rounded-lg bg-white/5 px-3 py-1.5 text-xs text-zinc-200 ring-1 ring-white/10 hover:bg-white/10 disabled:opacity-60">
          {busy ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}{r.reparse}
        </button>
      </footer>
    </Modal>
  );
}

const show = (v: unknown) => (typeof v === "string" ? `"${v}"` : String(v));

/** For each formula SheetIR cannot follow: what it does now and the suggested replacement, with a button that writes
 * the chosen ones into a copy of the workbook, parsed and verified as its next version. `fallback` is shown if the
 * suggestions cannot be loaded. */
function FixList({ workbookId, onOpenCell, onFixed, fallback }: {
  workbookId: string; onOpenCell: (cell: string) => void; onFixed: (result: IngestResult & { applied: string[] }) => Promise<void>;
  fallback: React.ReactNode;
}) {
  const { t } = useI18n();
  const f = t.fixes;
  const [data, setData] = useState<FixSuggestions | null>(null);
  const [failed, setFailed] = useState(false);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const usable = (x: FixSuggestion) => !!x.fix && !x.blocked && !!data?.can_apply;

  useEffect(() => {
    let live = true;
    api.fixes(workbookId).then((d) => {
      if (!live) return;
      setData(d);
      setChosen(new Set(d.fixes.filter((x) => x.fix && !x.blocked && d.can_apply).map((x) => x.cell)));
    }, () => live && setFailed(true));
    return () => { live = false; };
  }, [workbookId]);

  if (failed) return <>{fallback}</>;
  if (!data) return <p className="mt-3 flex items-center gap-2 text-xs text-zinc-500"><Loader2 className="size-3.5 animate-spin" />{f.loading}</p>;

  const toggle = (cell: string) => setChosen((s) => { const n = new Set(s); if (n.has(cell)) n.delete(cell); else n.add(cell); return n; });
  async function applyChosen() {
    setBusy(true);
    setError(null);
    try {
      await onFixed(await api.applyFixes(workbookId, [...chosen]));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs text-zinc-400"><Wand2 className="size-3.5 text-violet-300" />{f.title}</div>
      <ul className="space-y-2">
        {data.fixes.map((x) => {
          const dynamic = x.changes.some((c) => c.kind === "dynamic_ref");
          const inputs = x.inputs.map((i) => `${i.cell} = ${show(i.value)}`).join(", ");
          return (
            <li key={x.cell} className="rounded-xl bg-amber-400/[0.06] px-3 py-2.5 ring-1 ring-amber-300/15">
              <div className="flex items-start gap-2.5">
                {x.fix && (
                  <input type="checkbox" className="mt-0.5 size-3.5 shrink-0 accent-violet-400" disabled={!usable(x) || busy}
                    checked={chosen.has(x.cell)} onChange={() => toggle(x.cell)} aria-label={x.cell} />
                )}
                <div className="min-w-0 flex-1 text-xs">
                  <div>
                    <button onClick={() => onOpenCell(x.cell)} className="font-mono text-violet-300 hover:underline">{x.cell}</button>
                    <span className="ml-2 text-amber-200">{[...new Set(x.issues.map((i) => i.kind.replace(/_/g, " ")))].join(" · ")}</span>
                  </div>
                  {x.changes.map((c, i) => (
                    <p key={i} className="mt-1 text-zinc-300">
                      {c.kind === "dynamic_ref" ? <>{f.pointsTo(c.function ?? "", c.target ?? "")}{inputs && f.because(inputs)}</>
                        : c.kind === "unresolved_name" ? f.nameTypo(c.old ?? "", c.new ?? "")
                        : f.sheetTypo(c.old ?? "", c.new ?? "")}
                    </p>
                  ))}
                  {x.formula && (
                    <div className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 font-mono">
                      <span className="text-zinc-500">{f.now}</span><span className="break-all text-zinc-400">{x.formula}</span>
                      {x.fix && <><span className="text-zinc-500">{f.fixed}</span><span className="break-all text-emerald-300">{x.fix}</span></>}
                    </div>
                  )}
                  {x.fix && dynamic && x.inputs.length > 0 && (
                    <p className="mt-1.5 text-zinc-500">{f.frozen(x.inputs.map((i) => i.cell).join(", "))}</p>
                  )}
                  {x.blocked && <p className="mt-1.5 text-amber-200/80">{f.blocked[x.blocked]}</p>}
                  {x.advice.map((k) => <p key={k} className="mt-1.5 text-zinc-400">{f.advice[k] ?? t.issueHelp[k] ?? k}</p>)}
                </div>
              </div>
            </li>
          );
        })}
      </ul>
      {!data.can_apply && data.fixes.some((x) => x.fix) && <p className="mt-2 text-xs text-zinc-500">{f.cannotApply}</p>}
      {error && <p className="mt-2 rounded-lg bg-rose-500/10 px-3 py-1.5 text-xs text-rose-200">{error}</p>}
      {data.fixes.some(usable) && (
        <button disabled={busy || chosen.size === 0} onClick={applyChosen}
          className="mt-3 flex items-center gap-1.5 rounded-lg bg-gradient-to-br from-violet-500 to-cyan-400 px-3 py-1.5 text-xs font-medium text-white shadow-lg shadow-violet-500/20 disabled:opacity-50">
          {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Wand2 className="size-3.5" />}{busy ? f.applying : f.apply(chosen.size)}
        </button>
      )}
    </div>
  );
}

/** Shown in place of a view when the selected upload could not be parsed: why, what to do, and the actions. */
export function FailureView({ workbook, onRetry, onRemove, onUpload }: {
  workbook: Workbook; onRetry: () => Promise<void>; onRemove: () => void; onUpload: () => void;
}) {
  const { t } = useI18n();
  const f = t.failure;
  const [busy, setBusy] = useState(false);
  const err: ParseError | null = workbook.error;
  const k = f.kinds[err?.kind ?? "parse_failed"] ?? f.kinds.parse_failed;
  return (
    <div className="grid flex-1 place-items-center p-6">
      <div className="w-full max-w-xl rounded-3xl border border-rose-400/20 bg-rose-500/[0.04] p-6">
        <div className="flex items-center gap-3">
          <span className="grid size-10 place-items-center rounded-2xl bg-rose-500/15 text-rose-300 ring-1 ring-rose-400/25"><FileX2 className="size-5" /></span>
          <div className="min-w-0">
            <h2 className="font-semibold">{f.title}</h2>
            <p className="truncate text-xs text-zinc-500">{workbook.name} · <span className="font-mono">{workbook.id}</span></p>
          </div>
        </div>
        <p className="mt-5 text-sm text-zinc-200">{k.why}</p>
        <p className="mt-2 rounded-xl bg-white/[0.04] px-3 py-2 text-sm text-zinc-300">{k.fix}</p>
        {err && (
          <details className="mt-4 text-xs text-zinc-500">
            <summary className="cursor-pointer">{f.technical}</summary>
            <p className="mt-1.5 break-all font-mono">{err.kind}{err.detail ? `: ${err.detail}` : ""}</p>
            <p className="mt-1">{f.when(new Date(err.at).toLocaleString(t.locale), err.sheetir_version)}</p>
          </details>
        )}
        <div className="mt-5 flex flex-wrap gap-2">
          <button onClick={onUpload} className="flex items-center gap-1.5 rounded-lg bg-gradient-to-br from-violet-500 to-cyan-400 px-3 py-1.5 text-sm font-medium text-white">
            <UploadCloud className="size-4" />{f.upload}
          </button>
          <button disabled={busy} onClick={async () => { setBusy(true); try { await onRetry(); } finally { setBusy(false); } }}
            className="flex items-center gap-1.5 rounded-lg bg-white/5 px-3 py-1.5 text-sm text-zinc-200 ring-1 ring-white/10 hover:bg-white/10 disabled:opacity-60">
            {busy ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}{busy ? f.retrying : f.retry}
          </button>
          <button onClick={onRemove} className="ml-auto flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-zinc-400 hover:bg-white/5 hover:text-rose-300">
            <Trash2 className="size-4" />{f.remove}
          </button>
        </div>
      </div>
    </div>
  );
}
