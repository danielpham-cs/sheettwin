"use client";

import { EyeOff, Loader2, RotateCcw, Wand2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Modal } from "@/components/ParseStatus";
import { api, type IngestResult, type IssueEntry } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

/** The issue to resolve: its cell and kind, and its detail when several issues of that kind sit in the same cell. */
export type ResolveTarget = { cell: string; kind: string; detail?: string };

const SEV = { high: "text-rose-300 bg-rose-500/10 ring-rose-400/25", medium: "text-amber-200 bg-amber-400/10 ring-amber-300/25", low: "text-zinc-300 bg-white/5 ring-white/10" };

type Props = {
  workbookId: string | null;
  target: ResolveTarget | null;
  onClose: () => void;
  onEdited: (result: IngestResult & { edited: string[] }) => Promise<void>;
  onIgnored: () => Promise<void>;
};

/** One issue: why it is flagged, the recommended fix as editable cell contents, and the choice to apply the contents
 * (as typed, recommended or not: a new version, re-verified) or to ignore the issue. */
export function ResolveDialog({ workbookId, target, onClose, ...rest }: Props) {
  return (
    <Modal open={!!target && !!workbookId} onClose={onClose}>
      {target && workbookId && <Body key={`${workbookId}|${target.cell}|${target.kind}|${target.detail ?? ""}`}
        workbookId={workbookId} target={target} onClose={onClose} {...rest} />}
    </Modal>
  );
}

function Body({ workbookId, target, onClose, onEdited, onIgnored }: Props & { workbookId: string; target: ResolveTarget }) {
  const { t } = useI18n();
  const r = t.resolve;
  const [issue, setIssue] = useState<IssueEntry | null | undefined>(undefined); // null: not found
  const [canApply, setCanApply] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [label, setLabel] = useState("");
  const [ignoring, setIgnoring] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"apply" | "ignore" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.resolve(workbookId).then((d) => {
      if (!live) return;
      const hit = d.issues.find((i) => i.cell === target.cell && i.kind === target.kind && (target.detail == null || i.detail === target.detail)) ?? null;
      setIssue(hit);
      setCanApply(d.can_apply);
      if (hit) setValues(proposed(hit));
    }, (e) => live && (setIssue(null), setError((e as Error).message)));
    return () => { live = false; };
  }, [workbookId, target]);

  const rec = issue?.recommendation ?? null;
  const labelCell = rec?.type === "extract_constant" ? (rec.params.label_cell as string | null) : null;
  const changed = issue ? Object.keys(values).filter((c) => values[c] !== (issue.current[c] ?? "")) : [];
  const edits = Object.fromEntries(changed.map((c) => [c, values[c]]));
  if (labelCell && label.trim() && changed.length) edits[labelCell] = label.trim();
  const blockedChanged = changed.filter((c) => issue?.blocked?.[c]);

  async function apply() {
    setBusy("apply");
    setError(null);
    try {
      await onEdited(await api.editCells(workbookId, edits));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function ignore() {
    if (!issue) return;
    setBusy("ignore");
    setError(null);
    try {
      await api.ignoreIssue(workbookId, { cell: issue.cell, kind: issue.kind, detail: issue.detail }, note);
      await onIgnored();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <header className="flex items-start gap-3 border-b border-white/5 px-6 py-4">
        <Wand2 className="mt-0.5 size-5 shrink-0 text-violet-300" />
        <div className="min-w-0 flex-1">
          <h3 className="flex flex-wrap items-center gap-2 font-medium">
            {target.kind.replace(/_/g, " ")}
            <span className="font-mono text-sm text-violet-300">{target.cell}</span>
            {issue && <span className={`rounded-full px-2 py-0.5 text-[11px] ring-1 ${SEV[issue.severity]}`}>{t.issues.severity[issue.severity]}</span>}
          </h3>
        </div>
        <button onClick={onClose} className="rounded-lg p-1.5 text-zinc-500 hover:bg-white/5 hover:text-white"><X className="size-5" /></button>
      </header>

      <div className="space-y-5 overflow-y-auto px-6 py-5 text-sm">
        {issue === undefined && <p className="flex items-center gap-2 text-zinc-500"><Loader2 className="size-4 animate-spin" /></p>}
        {issue === null && !error && <p className="text-zinc-400">{r.notFound}</p>}
        {issue && (
          <>
            <section>
              <h4 className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{r.why}</h4>
              <p className="text-zinc-300">{t.issueHelp[issue.kind] ?? ""}</p>
              <p className="mt-1 break-all font-mono text-xs text-zinc-500">{issue.detail}</p>
            </section>

            <section>
              <h4 className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{rec ? r.recommendation : r.change}</h4>
              <p className="text-zinc-200">{rec ? <RecText rec={rec} /> : (t.fixes.advice[issue.kind] ?? r.manual)}</p>
              {!rec && t.fixes.advice[issue.kind] && <p className="mt-1 text-xs text-zinc-500">{r.manual}</p>}

              <div className="mt-3 max-h-72 overflow-y-auto rounded-xl border border-white/5">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-zinc-950 text-left text-zinc-500">
                    <tr><th className="px-3 py-1.5 font-normal">{r.cell}</th><th className="px-3 py-1.5 font-normal">{r.now}</th><th className="px-3 py-1.5 font-normal">{r.change}</th></tr>
                  </thead>
                  <tbody>
                    {Object.keys(values).map((c) => (
                      <tr key={c} className="border-t border-white/5 align-top">
                        <td className="whitespace-nowrap px-3 py-1.5 font-mono text-violet-300">{c}</td>
                        <td className="break-all px-3 py-1.5 font-mono text-zinc-500">{issue.current[c] || <span className="font-sans italic">{r.emptyCell}</span>}</td>
                        <td className="w-1/2 px-2 py-1">
                          <input value={values[c]} onChange={(e) => setValues((v) => ({ ...v, [c]: e.target.value }))} spellCheck={false}
                            disabled={!!busy} placeholder={r.emptyCell}
                            className={`w-full rounded-md bg-white/[0.04] px-2 py-1 font-mono text-xs outline-none ring-1 focus:ring-violet-400/60 ${values[c] !== (issue.current[c] ?? "") ? "text-emerald-300 ring-emerald-400/30" : "text-zinc-200 ring-white/10"}`} />
                          {issue.blocked?.[c] && <p className="mt-1 text-amber-200/80">{t.fixes.blocked[issue.blocked[c]]}</p>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {labelCell && (
                <label className="mt-2 block text-xs text-zinc-400">
                  {r.label(labelCell)}
                  <input value={label} onChange={(e) => setLabel(e.target.value)} disabled={!!busy}
                    className="mt-1 w-full rounded-md bg-white/[0.04] px-2 py-1 text-xs text-zinc-200 outline-none ring-1 ring-white/10 focus:ring-violet-400/60" />
                </label>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-zinc-500">
                <span>{r.contentsHint}</span>
                {rec && (
                  <button onClick={() => setValues(proposed(issue))} disabled={!!busy} className="ml-auto flex items-center gap-1 text-zinc-400 hover:text-white">
                    <RotateCcw className="size-3" />{r.reset}
                  </button>
                )}
              </div>
              {!canApply && <p className="mt-2 text-xs text-amber-200/80">{t.fixes.cannotApply}</p>}
            </section>

            {ignoring && (
              <section className="rounded-xl bg-white/[0.03] p-3 ring-1 ring-white/10">
                <p className="text-xs text-zinc-400">{r.ignoreHint}</p>
                <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} placeholder={r.notePlaceholder} disabled={!!busy}
                  className="mt-2 w-full resize-none rounded-md bg-white/[0.04] px-2 py-1.5 text-xs text-zinc-200 outline-none ring-1 ring-white/10 focus:ring-violet-400/60" />
                <div className="mt-2 flex justify-end gap-2">
                  <button onClick={() => setIgnoring(false)} disabled={!!busy} className="rounded-lg px-3 py-1 text-xs text-zinc-400 hover:bg-white/5">✕</button>
                  <button onClick={ignore} disabled={!!busy} className="flex items-center gap-1.5 rounded-lg bg-white/10 px-3 py-1 text-xs text-white ring-1 ring-white/15 hover:bg-white/15">
                    {busy === "ignore" ? <Loader2 className="size-3.5 animate-spin" /> : <EyeOff className="size-3.5" />}{r.confirmIgnore}
                  </button>
                </div>
              </section>
            )}
          </>
        )}
        {error && <p className="rounded-xl bg-rose-500/10 px-3 py-2 text-xs text-rose-200">{error}</p>}
      </div>

      {issue && (
        <footer className="flex flex-wrap items-center gap-2 border-t border-white/5 px-6 py-3">
          {issue.ignorable
            ? !ignoring && (
              <button onClick={() => setIgnoring(true)} disabled={!!busy} className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs text-zinc-400 hover:bg-white/5 hover:text-white">
                <EyeOff className="size-3.5" />{r.ignore}
              </button>
            )
            : <span className="text-xs text-zinc-500">{r.cannotIgnore}</span>}
          <button onClick={apply} disabled={!!busy || !canApply || changed.length === 0 || blockedChanged.length > 0}
            title={changed.length === 0 ? r.nothing : undefined}
            className="ml-auto flex items-center gap-1.5 rounded-lg bg-gradient-to-br from-violet-500 to-cyan-400 px-3 py-1.5 text-xs font-medium text-white shadow-lg shadow-violet-500/20 disabled:opacity-50">
            {busy === "apply" ? <Loader2 className="size-3.5 animate-spin" /> : <Wand2 className="size-3.5" />}
            {busy === "apply" ? r.applying : changed.length ? r.apply(Object.keys(edits).length) : r.nothing}
          </button>
        </footer>
      )}
    </>
  );
}

/** The recommended contents, or the issue's cell as it is now when there is no recommendation. */
function proposed(issue: IssueEntry): Record<string, string> {
  return issue.recommendation ? { ...issue.recommendation.edits } : { [issue.cell]: issue.current[issue.cell] ?? "" };
}

function RecText({ rec }: { rec: NonNullable<IssueEntry["recommendation"]> }) {
  const { t } = useI18n();
  const r = t.resolve.rec;
  const p = rec.params;
  switch (rec.type) {
    case "follow_column": return <>{r.follow_column(String(p.donor))}</>;
    case "extract_constant": return <>{r.extract_constant(String(p.value), String(p.target), Number(p.cells))}</>;
    case "exact_match": return <>{r.exact_match(Number(p.cells))}</>;
    case "clear": return <>{r.clear}</>;
    case "fixed_value": return <>{r.fixed_value(String(p.value))}</>;
    case "rewrite": {
      const changes = (p.changes ?? []) as { kind: string; function?: string; target?: string; old?: string; new?: string }[];
      const inputs = ((p.inputs ?? []) as { cell: string; value: unknown }[]).map((i) => `${i.cell} = ${typeof i.value === "string" ? `"${i.value}"` : String(i.value)}`).join(", ");
      return (
        <>
          {changes.map((c, i) => (
            <span key={i} className="block">
              {c.kind === "dynamic_ref" ? <>{t.fixes.pointsTo(c.function ?? "", c.target ?? "")}{inputs && t.fixes.because(inputs)}</>
                : c.kind === "unresolved_name" ? t.fixes.nameTypo(c.old ?? "", c.new ?? "") : t.fixes.sheetTypo(c.old ?? "", c.new ?? "")}
            </span>
          ))}
        </>
      );
    }
    default: return null;
  }
}
