"use client";

import { Eye, ListChecks, Loader2, Wand2 } from "lucide-react";
import { useEffect, useState } from "react";
import type { ResolveTarget } from "@/components/ResolveDialog";
import { api, type ResolveData } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const ORDER = { high: 0, medium: 1, low: 2 } as Record<string, number>;
const DOT = { high: "bg-rose-400", medium: "bg-amber-300", low: "bg-zinc-500" } as Record<string, string>;

type Props = {
  workbookId: string | null;
  reloads: number; // bumped when the workbook was re-parsed (an issue ignored or reported again)
  onResolve: (target: ResolveTarget) => void;
  onOpenCell: (cell: string) => void;
  onChanged: () => Promise<void>;
};

/** Every open issue, most severe first, each with a way to resolve it; and the issues the user ignored. */
export function IssuesView({ workbookId, reloads, onResolve, onOpenCell, onChanged }: Props) {
  const { t } = useI18n();
  const s = t.issues;
  const [data, setData] = useState<ResolveData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string | null>(null);
  const [restoring, setRestoring] = useState<string | null>(null);

  useEffect(() => {
    if (!workbookId) return;
    let live = true;
    api.resolve(workbookId).then((d) => live && (setData(d), setError(null)), (e) => live && setError((e as Error).message));
    return () => { live = false; };
  }, [workbookId, reloads]);

  if (!workbookId) return <div className="grid flex-1 place-items-center text-sm text-zinc-500">{t.query.empty}</div>;
  if (error) return <p className="m-6 rounded-xl bg-rose-500/10 px-3 py-2 text-sm text-rose-200">{error}</p>;
  if (!data) return <div className="grid flex-1 place-items-center"><Loader2 className="size-5 animate-spin text-zinc-500" /></div>;

  const issues = [...data.issues].sort((a, b) => ORDER[a.severity] - ORDER[b.severity]);
  const counts = issues.reduce<Record<string, number>>((n, i) => ({ ...n, [i.severity]: (n[i.severity] ?? 0) + 1 }), {});
  const shown = filter ? issues.filter((i) => i.severity === filter) : issues;

  async function reportAgain(key: { cell: string; kind: string; detail: string }) {
    setRestoring(key.cell + key.kind);
    try {
      await api.unignoreIssue(workbookId!, key);
      await onChanged();
    } finally {
      setRestoring(null);
    }
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-4xl">
        <h2 className="flex items-center gap-2 text-base font-semibold text-white"><ListChecks className="size-4 text-violet-300" />{s.title}</h2>
        <p className="mt-0.5 text-xs text-zinc-500">{s.sub}</p>

        <div className="mt-4 flex flex-wrap gap-1.5">
          {[null, "high", "medium", "low"].map((k) => (
            <button key={k ?? "all"} onClick={() => setFilter(k)} disabled={k !== null && !counts[k]}
              className={`flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs transition disabled:opacity-40 ${filter === k ? "border-violet-400/50 bg-violet-500/15 text-white" : "border-white/10 text-zinc-300 hover:bg-white/5"}`}>
              {k && <span className={`size-1.5 rounded-full ${DOT[k]}`} />}
              {k ? s.severity[k] : s.all} <span className="text-zinc-500">{k ? counts[k] ?? 0 : issues.length}</span>
            </button>
          ))}
        </div>

        {shown.length === 0 ? (
          <p className="mt-8 text-center text-sm text-zinc-400">{s.empty}</p>
        ) : (
          <ul className="mt-4 space-y-2">
            {shown.map((i) => (
              <li key={`${i.cell}|${i.kind}|${i.detail}`} className="flex items-start gap-3 rounded-xl border border-white/[0.07] bg-[#141419]/90 px-4 py-3">
                <span className={`mt-1.5 size-2 shrink-0 rounded-full ${DOT[i.severity]}`} title={s.severity[i.severity]} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="text-sm font-medium text-zinc-100">{i.kind.replace(/_/g, " ")}</span>
                    <button onClick={() => onOpenCell(i.cell)} className="font-mono text-xs text-violet-300 hover:underline">{i.cell}</button>
                    {i.recommendation && (
                      <span className="flex items-center gap-1 rounded-full bg-emerald-400/10 px-2 py-0.5 text-[11px] text-emerald-300 ring-1 ring-emerald-400/20">
                        <Wand2 className="size-3" />{s.hasFix}
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 text-xs text-zinc-400">{t.issueHelp[i.kind] ?? ""}</p>
                  <p className="mt-1 break-all font-mono text-[11.5px] text-zinc-500">{i.detail}</p>
                </div>
                <button onClick={() => onResolve({ cell: i.cell, kind: i.kind, detail: i.detail })}
                  className="shrink-0 rounded-lg bg-white/5 px-3 py-1 text-xs text-zinc-200 ring-1 ring-white/10 hover:bg-white/10">
                  {s.resolve}
                </button>
              </li>
            ))}
          </ul>
        )}

        {data.ignored.length > 0 && (
          <section className="mt-8">
            <h3 className="text-sm font-medium text-zinc-300">{s.ignoredTitle(data.ignored.length)}</h3>
            <p className="text-xs text-zinc-500">{s.ignoredHint}</p>
            <ul className="mt-2 space-y-1.5">
              {data.ignored.map((i) => (
                <li key={`${i.cell}|${i.kind}|${i.detail}`} className="flex items-start gap-3 rounded-xl bg-white/[0.02] px-4 py-2 ring-1 ring-white/5">
                  <div className="min-w-0 flex-1 text-xs">
                    <span className="text-zinc-400">{i.kind.replace(/_/g, " ")}</span>
                    <button onClick={() => onOpenCell(i.cell)} className="ml-2 font-mono text-violet-300/80 hover:underline">{i.cell}</button>
                    {i.note && <p className="mt-0.5 text-zinc-500">“{i.note}”</p>}
                  </div>
                  <button onClick={() => reportAgain(i)} disabled={restoring !== null}
                    className="flex shrink-0 items-center gap-1 rounded-lg px-2 py-1 text-xs text-zinc-400 hover:bg-white/5 hover:text-white disabled:opacity-50">
                    {restoring === i.cell + i.kind ? <Loader2 className="size-3.5 animate-spin" /> : <Eye className="size-3.5" />}{s.reportAgain}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}
