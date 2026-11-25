"use client";

import {
  Bot, Check, ChevronDown, ChevronRight, CircleAlert, Download, FlaskConical, GitBranch, GitCompare, History, Loader2,
  Pencil, Play, RefreshCw, RotateCcw, Save, Search, ShieldCheck, Target, Undo2, Waypoints, X,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useId, useMemo, useState } from "react";
import {
  ACTOR_KEY, api, type CompareResult, type CellChange, type GoalSeekResult, type HistoryRecord, type ModelCell, type ModelCells,
  type ScenarioResult, type SensitivityResult, type TwinCheck, type VersionInfo, type Workbook,
} from "@/lib/api";
import { useDialogFocus } from "@/lib/dialog-focus";
import { useI18n } from "@/lib/i18n";

export type TwinTab = "verify" | "whatif" | "sensitivity" | "goal" | "history";
const TABS: { key: TwinTab; icon: typeof ShieldCheck }[] = [
  { key: "verify", icon: ShieldCheck },
  { key: "whatif", icon: FlaskConical },
  { key: "sensitivity", icon: Waypoints },
  { key: "goal", icon: Target },
  { key: "history", icon: History },
];

type Props = {
  projectId: string | null;
  workbookId: string | null;
  /** The versions of the file on screen in this project (any order). */
  versions: Workbook[];
  /** A history record to show (e.g. one a chat answer made); `onOpened` clears it. */
  openRef?: string | null;
  onOpened?: () => void;
  onOpenCell: (cell: string) => void;
  /** An approved correction stored a new version: open it. */
  onNewVersion: (id: string) => Promise<void>;
  /** Something in the history changed (e.g. a correction was proposed or reviewed). */
  onHistoryChanged: () => void;
};

/** Where a what-if continues from: a recorded analysis and the changes it holds. */
type Draft = { values: Record<string, string>; parent: HistoryRecord | null };

const BY_KEY = ACTOR_KEY;

/** Who acts: the signed-in account, or the name typed here (kept in this browser). */
function useActor() {
  const [name, setName] = useState<string>(() => {
    try { return localStorage.getItem(BY_KEY) ?? ""; } catch { return ""; }
  });
  const [account, setAccount] = useState<string | null>(null);
  useEffect(() => { api.me().then((m) => setAccount(m.separated ? m.user : null), () => {}); }, []);
  const save = (v: string) => { setName(v); try { localStorage.setItem(BY_KEY, v); } catch {} };
  return { by: account ?? (name.trim() || undefined), name, setName: save, account };
}

export function fmtValue(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return v.toLocaleString(undefined, { maximumFractionDigits: 6 });
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  return String(v);
}

function asTyped(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  return String(v);
}

const PILL: Record<string, string> = {
  verified: "bg-emerald-400/10 text-emerald-300 ring-emerald-400/25",
  partly_verified: "bg-sky-400/10 text-sky-300 ring-sky-400/25",
  passed: "bg-emerald-400/10 text-emerald-300 ring-emerald-400/25",
  translated: "bg-emerald-400/10 text-emerald-300 ring-emerald-400/25",
  v1_passed: "bg-sky-400/10 text-sky-300 ring-sky-400/25",
  v2_passed: "bg-sky-400/10 text-sky-300 ring-sky-400/25",
  failed: "bg-rose-500/10 text-rose-300 ring-rose-400/25",
  error: "bg-rose-500/10 text-rose-300 ring-rose-400/25",
  depends_on_failed: "bg-rose-500/10 text-rose-300 ring-rose-400/25",
  not_recomputed: "bg-amber-400/10 text-amber-200 ring-amber-300/25",
  untranslated: "bg-amber-400/10 text-amber-200 ring-amber-300/25",
  pending: "bg-amber-400/10 text-amber-200 ring-amber-300/25",
  approved: "bg-emerald-400/10 text-emerald-300 ring-emerald-400/25",
  rejected: "bg-rose-500/10 text-rose-300 ring-rose-400/25",
};

function Pill({ status, label, title }: { status: string; label?: string; title?: string }) {
  const { t } = useI18n();
  return (
    <span title={title ?? t.twin.statusHelp[status]}
      className={`inline-flex shrink-0 items-center whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${PILL[status] ?? "bg-white/5 text-zinc-300 ring-white/10"}`}>
      {label ?? t.twin.status[status] ?? status}
    </span>
  );
}

function Delta({ d }: { d: number | null | undefined }) {
  if (d === null || d === undefined || d === 0) return <span className="text-zinc-500">{d === 0 ? "0" : "—"}</span>;
  return (
    <span className="whitespace-nowrap tabular-nums text-zinc-100">
      <span className={d > 0 ? "text-sky-300" : "text-amber-300"}>{d > 0 ? "▲" : "▼"}</span> {d > 0 ? "+" : ""}{fmtValue(d)}
    </span>
  );
}

function ErrorLine({ text }: { text: string | null }) {
  if (!text) return null;
  return <p role="alert" className="mt-3 flex items-start gap-1.5 rounded-md bg-rose-500/10 px-3 py-2 text-xs text-rose-200"><CircleAlert className="mt-px size-3.5 shrink-0" />{text}</p>;
}

// Buttons. Text on solid accent buttons is `snow`, which stays white in both themes (`white` turns to ink in light mode).
const focus = "outline-none focus-visible:ring-2 focus-visible:ring-blue-400/70 focus-visible:ring-offset-0";
const btn = `inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${focus}`;
const btnPrimary = `${btn} bg-blue-600 text-snow shadow-sm hover:bg-blue-500 active:bg-blue-700`;
const btnDanger = `${btn} bg-rose-600 text-snow shadow-sm hover:bg-rose-500 active:bg-rose-700`;
const btnGhost = `${btn} bg-white/5 text-zinc-200 ring-1 ring-white/10 hover:bg-white/10 hover:text-white active:bg-white/[0.14]`;
const btnQuiet = `${btn} text-zinc-400 hover:bg-white/5 hover:text-white`;
const btnLink = `inline-flex items-center gap-1 rounded text-xs font-medium text-blue-300 underline-offset-2 transition hover:text-blue-200 hover:underline disabled:opacity-50 ${focus}`;
const input = "rounded-md border border-white/10 bg-black/20 px-2.5 py-1.5 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-blue-400/50";
const card = "rounded-lg border border-white/[0.07] bg-surface/90 p-4";
const th = "px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wider text-zinc-500";
const td = "px-3 py-1.5 align-top";

/** A button that stays in place when it cannot be used: greyed out, with the reason on hover and for screen readers. */
function Action({ onClick, href, disabled, why, busy, icon: Icon, label, tone = "ghost", className = "" }: {
  onClick?: () => void; href?: string; disabled?: boolean; why?: string | null; busy?: boolean;
  icon: typeof Check; label: string; tone?: "ghost" | "primary" | "quiet"; className?: string;
}) {
  const cls = `${tone === "primary" ? btnPrimary : tone === "quiet" ? btnQuiet : btnGhost} ${className}`;
  const whyId = useId();
  const blocked = !!why;
  const inner = <>{busy ? <Loader2 className="size-3.5 animate-spin" /> : <Icon className="size-3.5" />}{label}</>;
  if (href && !blocked && !disabled) return <a href={href} download className={cls}>{inner}</a>;
  return (
    // The wrapper carries the tooltip: a disabled button gets no mouse events, so its own title would not show.
    <span title={why ?? undefined} className={`inline-flex ${blocked ? "cursor-help" : ""}`}>
      <button onClick={onClick} disabled={disabled || blocked} aria-describedby={blocked ? whyId : undefined}
        className={`${cls} ${blocked ? "pointer-events-none" : ""}`}>{inner}</button>
      {blocked && <span id={whyId} className="sr-only">{why}</span>}
    </span>
  );
}

/** A dialog over the page (the app's own pattern: a dimmed backdrop, focus kept inside, Escape and a click outside close
 * it unless `busy`). */
function Modal({ open, title, onClose, busy, wide, children }: {
  open: boolean; title: React.ReactNode; onClose: () => void; busy?: boolean; wide?: boolean; children: React.ReactNode;
}) {
  const { t } = useI18n();
  const box = useDialogFocus<HTMLDivElement>(open);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onClose]);
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={() => !busy && onClose()}
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <motion.div ref={box} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined} tabIndex={-1}
            onClick={(e) => e.stopPropagation()}
            initial={{ opacity: 0, scale: 0.96, y: 10 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.97, y: 6 }}
            transition={{ type: "spring", stiffness: 380, damping: 30 }}
            className={`field flex max-h-[85vh] w-full flex-col rounded-xl bg-zinc-950/95 shadow-2xl ${wide ? "max-w-3xl" : "max-w-md"}`}>
            <div className="flex items-start gap-3 border-b border-white/[0.07] px-5 py-4">
              <h3 className="min-w-0 flex-1 text-sm font-semibold text-white">{title}</h3>
              <button onClick={onClose} disabled={busy} aria-label={t.app.close} className={`${btnQuiet} -mr-2 -mt-1 px-1.5`}><X className="size-4" /></button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/** Ask before an action that changes the data or the history; `note` adds a text box (required when it says so). */
function ConfirmDialog({ open, title, body, confirm, tone = "primary", note, onConfirm, onClose }: {
  open: boolean; title: string; body?: React.ReactNode; confirm: string; tone?: "primary" | "danger";
  note?: { placeholder: string; required?: boolean; initial?: string };
  /** Runs the action; a thrown error is shown in the dialog, which stays open. */
  onConfirm: (note: string) => Promise<void>;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Each time it opens, the note starts from `note.initial` (state adjusted while rendering).
  const [wasOpen, setWasOpen] = useState(false);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setText(note?.initial ?? "");
  }
  const close = () => { setText(""); setError(null); onClose(); };
  async function go() {
    setBusy(true); setError(null);
    try { await onConfirm(text.trim()); setText(""); onClose(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  return (
    <Modal open={open} title={title} onClose={close} busy={busy}>
      {body && <div className="text-sm leading-relaxed text-zinc-300">{body}</div>}
      {note && (
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} placeholder={note.placeholder} autoFocus
          className={`${input} mt-3 w-full resize-y text-sm`} />
      )}
      <ErrorLine text={error} />
      <div className="mt-4 flex justify-end gap-2">
        <button onClick={close} disabled={busy} className={btnGhost}>{t.twin.history.cancel}</button>
        <button onClick={go} disabled={busy || (!!note?.required && !text.trim())} className={tone === "danger" ? btnDanger : btnPrimary}>
          {busy && <Loader2 className="size-3.5 animate-spin" />}{confirm}
        </button>
      </div>
    </Modal>
  );
}

export function TwinView({ projectId, workbookId, versions, openRef, onOpened, onOpenCell, onNewVersion, onHistoryChanged }: Props) {
  const { t } = useI18n();
  const s = t.twin;
  const [tab, setTab] = useState<TwinTab>("verify");
  const [draft, setDraft] = useState<Draft>({ values: {}, parent: null });
  const actor = useActor();
  // The current version is the one uploaded (or made by an approval) last, not the highest number: an approval whose
  // result has the bytes of an earlier version reuses that version, which becomes current again.
  const latest = useMemo(() => [...versions].sort((a, b) => (b.last_uploaded ?? "").localeCompare(a.last_uploaded ?? ""))[0], [versions]);

  // A record to show (from a chat answer): open the history at it (state adjusted while rendering, not in an effect).
  const [seenRef, setSeenRef] = useState<string | null>(null);
  if ((openRef ?? null) !== seenRef) {
    setSeenRef(openRef ?? null);
    if (openRef) setTab("history");
  }

  const continueFrom = (rec: HistoryRecord) => {
    setDraft({ values: Object.fromEntries(Object.entries(rec.changes).map(([c, v]) => [c, asTyped(v)])), parent: rec });
    setTab("whatif");
  };

  if (!projectId || !workbookId) return <div className="grid flex-1 place-items-center text-sm text-zinc-500">{s.noWorkbook}</div>;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4 md:p-6">
      <div className="mx-auto max-w-6xl">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="flex items-center gap-2 text-base font-semibold text-white"><GitBranch className="size-4 text-blue-300" />{s.title}</h2>
            <p className="mt-0.5 max-w-3xl text-xs text-zinc-500">{s.sub}</p>
          </div>
          <label className="flex items-center gap-2 text-xs text-zinc-400" title={s.byHint}>
            {s.by}
            {actor.account ? (
              <span className="rounded-md bg-white/5 px-2 py-1 text-zinc-200 ring-1 ring-white/10">{actor.account}</span>
            ) : (
              <input value={actor.name} onChange={(e) => actor.setName(e.target.value)} placeholder={s.byPlaceholder} className={`${input} w-32 py-1 text-xs`} />
            )}
          </label>
        </div>

        <div role="tablist" className="mt-4 flex flex-wrap gap-1 border-b border-white/[0.07]">
          {TABS.map(({ key, icon: Icon }) => (
            <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}
              className={`-mb-px flex items-center gap-1.5 rounded-t-md border-b-2 px-3 py-2 text-sm transition ${focus} ${tab === key ? "border-blue-400 text-white" : "border-transparent text-zinc-400 hover:text-zinc-200"}`}>
              <Icon className="size-4" />{s.tabs[key]}
            </button>
          ))}
        </div>

        <div className="mt-5">
          {tab === "verify" && <VerifyTab key={workbookId} workbookId={workbookId} onOpenCell={onOpenCell} />}
          {tab === "whatif" && (
            <WhatIfTab key={workbookId} pid={projectId} wid={workbookId} by={actor.by} draft={draft} setDraft={setDraft}
              isLatest={latest?.id === workbookId} onOpenCell={onOpenCell} onHistoryChanged={onHistoryChanged} />
          )}
          {tab === "sensitivity" && <SensitivityTab key={workbookId} pid={projectId} wid={workbookId} by={actor.by} onOpenCell={onOpenCell} onHistoryChanged={onHistoryChanged} />}
          {tab === "goal" && (
            <GoalTab key={workbookId} pid={projectId} wid={workbookId} by={actor.by} onOpenCell={onOpenCell}
              onUse={continueFrom} onHistoryChanged={onHistoryChanged} />
          )}
          {tab === "history" && (
            <HistoryTab pid={projectId} wid={workbookId} versions={versions} latest={latest} by={actor.by}
              openRef={openRef ?? null} onOpened={onOpened} onOpenCell={onOpenCell} onContinue={continueFrom}
              onNewVersion={onNewVersion} onHistoryChanged={onHistoryChanged} />
          )}
        </div>
      </div>
    </div>
  );
}

// ---- Verify (V1 / V2) ---------------------------------------------------------------------------------------------

function VerifyTab({ workbookId, onOpenCell }: { workbookId: string; onOpenCell: (cell: string) => void }) {
  const { t } = useI18n();
  const s = t.twin.verify;
  const [data, setData] = useState<TwinCheck | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showGroups, setShowGroups] = useState(false);
  const [scenarios, setScenarios] = useState(10);

  useEffect(() => {
    let live = true;
    api.twin(workbookId).then((d) => live && setData(d), (e) => live && setError((e as Error).message));
    return () => { live = false; };
  }, [workbookId]);

  async function run() {
    setRunning(true); setError(null);
    try { setData(await api.runTwin(workbookId, scenarios)); } catch (e) { setError((e as Error).message); } finally { setRunning(false); }
  }

  const runBar = (
    <div className="flex flex-wrap items-center gap-2">
      <button onClick={run} disabled={running} className={btnPrimary}>
        {running ? <Loader2 className="size-3.5 animate-spin" /> : data?.status === "not_run" ? <Play className="size-3.5" /> : <RefreshCw className="size-3.5" />}
        {running ? s.running : data?.status === "not_run" ? s.run : s.rerun}
      </button>
      <label className="flex items-center gap-1.5 text-xs text-zinc-400">
        V2
        <input type="number" min={0} max={100} value={scenarios} onChange={(e) => setScenarios(Math.max(0, Math.min(100, Number(e.target.value) || 0)))}
          className={`${input} w-16 py-1 text-xs`} aria-label={s.scenarios(scenarios)} />
        {s.scenarios(scenarios).replace(/^\d+\s*/, "")}
      </label>
    </div>
  );

  if (!data) return error ? <ErrorLine text={error} /> : <Loader2 className="size-5 animate-spin text-zinc-500" />;
  if (data.status === "not_run") {
    return (
      <div className={`${card} max-w-2xl`}>
        <p className="text-sm text-zinc-300">{s.never}</p>
        <p className="mt-2 text-xs text-zinc-500">{s.v1Help}</p>
        <p className="mt-1 text-xs text-zinc-500">{s.v2Help}</p>
        <div className="mt-4">{runBar}</div>
        <ErrorLine text={error} />
      </div>
    );
  }

  const cov = data.coverage!;
  const v1 = data.v1, v2 = data.v2;
  const pct = cov.cells ? Math.round((100 * cov.cells_translated) / cov.cells) : 100;
  const when = data.at ? new Date(data.at).toLocaleString(t.locale, { dateStyle: "medium", timeStyle: "short" }) : "";
  const groups = [...(data.group_results ?? [])].sort((a, b) => Number(a.status === "verified") - Number(b.status === "verified"));

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        <Pill status={data.status} />
        <span className="text-xs text-zinc-500">{s.at(when, data.seconds ?? 0)}</span>
        <span className="flex-1" />
        <a href={api.exportUrl(workbookId, "twin.py")} download className={btnGhost}><Download className="size-3.5" />{s.download}</a>
        {runBar}
      </div>
      <ErrorLine text={error} />

      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <div className={card}>
          <p className="text-xs text-zinc-500">{s.coverage}</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums text-white">{pct}%</p>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/10"><div className="h-full rounded-full bg-emerald-400" style={{ width: `${pct}%` }} /></div>
          <p className="mt-2 text-xs text-zinc-400">{s.cells(cov.cells_translated, cov.cells)}</p>
          <p className="text-xs text-zinc-500">{s.groups(cov.groups_translated, cov.groups)}</p>
        </div>
        <div className={card} title={s.v1Help}>
          <div className="flex items-center justify-between gap-2"><p className="text-xs text-zinc-500">{s.v1}</p>{v1 && <Pill status={v1.status} />}</div>
          {v1?.isolated && v1.end_to_end ? (
            <dl className="mt-2 space-y-1 text-xs">
              <div className="flex justify-between gap-2"><dt className="text-zinc-400">{s.isolated}</dt><dd className="tabular-nums text-zinc-100">{s.cellsMatch(v1.isolated.passed, v1.isolated.checked)}</dd></div>
              <div className="flex justify-between gap-2"><dt className="text-zinc-400">{s.endToEnd}</dt><dd className="tabular-nums text-zinc-100">{s.cellsMatch(v1.end_to_end.passed, v1.end_to_end.checked)}</dd></div>
            </dl>
          ) : (
            <p className="mt-2 text-xs text-zinc-400">{v1?.status === "no_baseline" ? s.noBaseline : v1?.summary ?? "—"}</p>
          )}
          <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">{s.v1Help}</p>
        </div>
        <div className={card} title={s.v2Help}>
          <div className="flex items-center justify-between gap-2"><p className="text-xs text-zinc-500">{s.v2}</p>{v2 && <Pill status={v2.status} />}</div>
          {v2 && v2.checked !== undefined ? (
            <dl className="mt-2 space-y-1 text-xs">
              <div className="flex justify-between gap-2"><dt className="text-zinc-400">{s.scenarios(v2.scenarios_run ?? 0)}</dt><dd className="tabular-nums text-zinc-100">{s.comparisons(v2.passed ?? 0, v2.checked ?? 0)}</dd></div>
            </dl>
          ) : (
            <p className="mt-2 text-xs text-zinc-400">{v2?.summary ?? "—"}</p>
          )}
          {!!v2?.known_differences && Object.keys(v2.known_differences).length > 0 && (
            <p className="mt-1 text-[11px] text-zinc-500" title={s.known}>
              {s.known}: {Object.entries(v2.known_differences).map(([k, n]) => `${k} ${n}`).join(", ")}
            </p>
          )}
          <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">{s.v2Help}</p>
        </div>
      </div>

      {cov.untranslated.length > 0 && (
        <section className="mt-5">
          <h3 className="text-sm font-medium text-zinc-200">{s.untranslated}</h3>
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {cov.untranslated.map((u) => (
              <li key={u.reason} className="rounded-md bg-amber-400/[0.07] px-2.5 py-1 text-xs text-amber-100 ring-1 ring-amber-300/20">
                <span className="font-mono">{u.reason}</span> <span className="text-amber-200/70">· {u.cells.toLocaleString()}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {!!data.mismatches?.length && (
        <section className="mt-5">
          <h3 className="flex items-center gap-1.5 text-sm font-medium text-rose-200"><CircleAlert className="size-4" />{s.mismatches} · {data.mismatches.length}</h3>
          <div className="mt-2 overflow-x-auto rounded-lg border border-white/[0.07]">
            <table className="w-full text-xs">
              <thead className="bg-white/[0.03]"><tr><th className={th}>V</th><th className={th}>{t.twin.whatif.cell}</th><th className={th}>{s.expected}</th><th className={th}>{s.got}</th></tr></thead>
              <tbody>
                {data.mismatches.slice(0, 50).map((m, i) => (
                  <tr key={i} className="border-t border-white/[0.05]">
                    <td className={`${td} text-zinc-400`}>{m.level.toUpperCase()}{m.scenario !== null ? ` #${m.scenario}` : ""}</td>
                    <td className={td}><button onClick={() => onOpenCell(m.cell_id)} className={`${btnLink} font-mono`}>{m.cell_id}</button></td>
                    <td className={`${td} font-mono text-zinc-200`}>{m.expected}</td>
                    <td className={`${td} font-mono text-rose-200`}>{m.got}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.mismatches.length > 50 && <p className="mt-1 text-xs text-zinc-500">{s.more(data.mismatches.length - 50)}</p>}
        </section>
      )}

      {groups.length > 0 && (
        <section className="mt-5">
          <button onClick={() => setShowGroups((o) => !o)} className="flex items-center gap-1 text-sm font-medium text-zinc-200">
            {showGroups ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}{s.groupsTitle} · {groups.length.toLocaleString()}
            <span className="ml-2 flex gap-1">{Object.entries(data.groups ?? {}).map(([k, n]) => <Pill key={k} status={k} label={`${t.twin.status[k] ?? k} ${n}`} />)}</span>
          </button>
          {showGroups && (
            <div className="mt-2 overflow-x-auto rounded-lg border border-white/[0.07]">
              <table className="w-full text-xs">
                <thead className="bg-white/[0.03]"><tr><th className={th}>{t.twin.whatif.cell}</th><th className={th}>#</th><th className={th}>{t.twin.whatif.status}</th><th className={th}>R1C1</th><th className={th}>V1</th><th className={th}>V2</th></tr></thead>
                <tbody>
                  {groups.slice(0, 200).map((g) => (
                    <tr key={g.group_id} className="border-t border-white/[0.05]">
                      <td className={td}>
                        <button onClick={() => onOpenCell(g.anchor)} className={`${btnLink} font-mono`}>{g.anchor}</button>
                        {g.last !== g.anchor && <span className="font-mono text-zinc-500">:{g.last.split("!").pop()}</span>}
                        {g.semantic && <span className="block text-[11px] text-zinc-500">{g.semantic}</span>}
                      </td>
                      <td className={`${td} tabular-nums text-zinc-400`}>{g.n_cells}</td>
                      <td className={td}><Pill status={g.status} title={g.reason ?? undefined} />{g.reason && <span className="mt-0.5 block font-mono text-[11px] text-amber-200/80">{g.reason}</span>}</td>
                      <td className={`${td} max-w-md break-all font-mono text-zinc-300`}>{g.r1c1}</td>
                      <td className={`${td} tabular-nums text-zinc-400`}>{g.v1_checked ? `${g.v1_passed}/${g.v1_checked}` : "—"}</td>
                      <td className={`${td} tabular-nums text-zinc-400`}>{g.v2_checked ? `${g.v2_passed}/${g.v2_checked}` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {groups.length > 200 && <p className="px-3 py-2 text-xs text-zinc-500">{s.more(groups.length - 200)}</p>}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

// ---- Cells to pick from ---------------------------------------------------------------------------------------------

/** The workbook's inputs and result cells (searchable), as the twin knows them. */
function useModelCells(pid: string, wid: string, query: string) {
  const [data, setData] = useState<ModelCells | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const timer = setTimeout(() => {
      api.modelCells(pid, wid, query.trim() || undefined).then((d) => live && (setData(d), setError(null)), (e) => live && setError((e as Error).message));
    }, query ? 250 : 0);
    return () => { live = false; clearTimeout(timer); };
  }, [pid, wid, query]);
  return { data, error };
}

function CellPicker({ pid, wid, kind, value, onChange, placeholder }: {
  pid: string; wid: string; kind: "inputs" | "targets"; value: string; onChange: (cell: string) => void; placeholder: string;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const { data } = useModelCells(pid, wid, query);
  const list: ModelCell[] = data ? data[kind] : [];
  const chosen = list.find((c) => c.cell === value);
  return (
    <div className="relative">
      <div className={`${input} flex items-center gap-2`}>
        <Search className="size-3.5 shrink-0 text-zinc-500" />
        <input value={open ? query : value ? `${value}${chosen?.label ? ` · ${chosen.label}` : ""}` : ""} placeholder={placeholder}
          onFocus={() => { setOpen(true); setQuery(""); }} onBlur={() => setTimeout(() => setOpen(false), 150)}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && list[0]) { onChange(list[0].cell); setOpen(false); (e.target as HTMLInputElement).blur(); } }}
          className="min-w-0 flex-1 bg-transparent outline-none" />
      </div>
      {open && (
        <ul className="absolute z-30 mt-1 max-h-72 w-full overflow-y-auto rounded-md border border-white/10 bg-zinc-900 py-1 text-xs shadow-2xl">
          {list.length === 0 && <li className="px-3 py-2 text-zinc-500">—</li>}
          {list.map((c) => (
            <li key={c.cell}>
              <button onMouseDown={(e) => e.preventDefault()} onClick={() => { onChange(c.cell); setOpen(false); }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-white/10">
                <span className="font-mono text-blue-300">{c.cell}</span>
                <span className="truncate text-zinc-300">{c.label}</span>
                <span className="ml-auto tabular-nums text-zinc-400">{fmtValue(c.value)}</span>
                {kind === "targets" && <Pill status={c.status} />}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ChangeTable({ rows, onOpenCell, empty }: { rows: CellChange[]; onOpenCell: (cell: string) => void; empty: string }) {
  const { t } = useI18n();
  const w = t.twin.whatif;
  if (!rows.length) return <p className="text-xs text-zinc-500">{empty}</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-white/[0.07]">
      <table className="w-full text-xs">
        <thead className="bg-white/[0.03]"><tr><th className={th}>{w.cell}</th><th className={th}>{w.label}</th><th className={`${th} text-right`}>{w.base}</th><th className={`${th} text-right`}>{w.value}</th><th className={`${th} text-right`}>{w.delta}</th><th className={th}>{w.status}</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.cell} className="border-t border-white/[0.05]">
              <td className={td}><button onClick={() => onOpenCell(r.cell)} className={`${btnLink} font-mono`}>{r.cell}</button></td>
              <td className={`${td} text-zinc-300`} title={r.label ?? ""}><span className="block max-w-[9rem] truncate 2xl:max-w-[14rem]">{r.label}</span></td>
              <td className={`${td} text-right tabular-nums text-zinc-400`}>{fmtValue(r.base)}</td>
              <td className={`${td} text-right tabular-nums font-medium text-white`}>{fmtValue(r.value)}</td>
              <td className={`${td} text-right`}><Delta d={r.delta} /></td>
              <td className={td}><Pill status={r.status} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A scenario's warnings, worded from their codes when the backend gives them. */
function Warnings({ items, notices }: { items: string[]; notices?: { code: string; n: number }[] }) {
  const { t } = useI18n();
  const text = notices ? notices.map((x) => t.twin.notices[x.code]?.(x.n) ?? x.code) : items;
  if (!text.length) return null;
  return (
    <ul className="space-y-1">
      {text.map((w) => <li key={w} className="flex items-start gap-1.5 rounded-md bg-amber-400/[0.07] px-3 py-2 text-xs text-amber-100 ring-1 ring-amber-300/20"><CircleAlert className="mt-px size-3.5 shrink-0" />{w}</li>)}
    </ul>
  );
}

// ---- What-if ----------------------------------------------------------------------------------------------------------

function WhatIfTab({ pid, wid, by, draft, setDraft, isLatest, onOpenCell, onHistoryChanged }: {
  pid: string; wid: string; by?: string; draft: Draft; setDraft: (d: Draft) => void; isLatest: boolean;
  onOpenCell: (cell: string) => void; onHistoryChanged: () => void;
}) {
  const { t } = useI18n();
  const w = t.twin.whatif;
  const [query, setQuery] = useState("");
  const { data, error: listError } = useModelCells(pid, wid, query);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"preview" | "save" | null>(null);
  const [result, setResult] = useState<ScenarioResult | null>(null);
  const [savedRef, setSavedRef] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [proposing, setProposing] = useState(false);
  const [proposed, setProposed] = useState<string | null>(null);

  // A continued record from another file version cannot continue here: start from the stored data.
  const parent = draft.parent && draft.parent.workbook === wid ? draft.parent : null;
  const values = draft.values;
  const set = (cell: string, v: string) => {
    const next = { ...values };
    if (v === "") delete next[cell]; else next[cell] = v;
    setDraft({ values: next, parent: draft.parent });
    setSavedRef(null);
  };
  const changes = Object.entries(values);
  // Known cells, plus the ones set but not in the list (continued from a record, or typed after a search).
  const listed = data?.inputs ?? [];
  const extra = changes.map(([c]) => c).filter((c) => !listed.some((x) => x.cell === c));

  function body(save: boolean) {
    const base = parent ? Object.fromEntries(Object.entries(parent.changes).map(([c, v]) => [c, asTyped(v)])) : {};
    const delta = Object.fromEntries(changes.filter(([c, v]) => base[c] !== v));
    const reset = Object.keys(base).filter((c) => !(c in values));
    return { workbook: wid, changes: delta, parent: parent?.ref ?? null, reset: reset.length ? reset : undefined, note: note || undefined, save, by };
  }

  async function run(save: boolean) {
    setBusy(save ? "save" : "preview"); setError(null);
    try {
      if (save) {
        const rec = await api.scenario(pid, { ...body(true), save: true as const });
        setResult(rec.result); setSavedRef(rec.ref); onHistoryChanged();
        setDraft({ values, parent: rec });
      } else {
        const res = await api.scenario(pid, { ...body(false), save: false as const });
        setResult(res.result); setSavedRef(null);
      }
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
      <section>
        <h3 className="text-sm font-medium text-zinc-200">{w.inputs}</h3>
        <label className={`${input} mt-2 flex items-center gap-2`}>
          <Search className="size-3.5 text-zinc-500" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={w.search} className="min-w-0 flex-1 bg-transparent text-sm outline-none" />
        </label>
        <ErrorLine text={listError} />
        <ul className="mt-2 max-h-[60vh] space-y-1 overflow-y-auto pr-1">
          {[...extra.map((c) => ({ cell: c, label: null, value: undefined, status: "input", number: true }) as ModelCell), ...listed].map((c) => (
            <li key={c.cell} className={`rounded-md px-2.5 py-2 ring-1 ${c.cell in values ? "bg-blue-500/[0.08] ring-blue-400/30" : "bg-white/[0.02] ring-white/[0.06]"}`}>
              <div className="flex items-baseline gap-2">
                <button onClick={() => onOpenCell(c.cell)} className={`${btnLink} font-mono`}>{c.cell}</button>
                <span className="min-w-0 flex-1 truncate text-xs text-zinc-300" title={c.label ?? ""}>{c.label}</span>
              </div>
              <div className="mt-1.5 flex items-center gap-2">
                <span className="w-24 shrink-0 truncate text-xs tabular-nums text-zinc-500" title={w.current}>{c.value === undefined ? "" : fmtValue(c.value)}</span>
                <input value={values[c.cell] ?? ""} onChange={(e) => set(c.cell, e.target.value)} placeholder={w.newValue}
                  aria-label={`${c.cell} ${w.newValue}`} className={`${input} min-w-0 flex-1 py-1 text-xs tabular-nums`} />
                {c.cell in values && <button onClick={() => set(c.cell, "")} aria-label={w.clear} className={`${btnQuiet} px-1 py-1`}><X className="size-3.5" /></button>}
              </div>
            </li>
          ))}
          {data && !listed.length && !extra.length && <li className="px-1 py-3 text-xs text-zinc-500">{w.noInputs}</li>}
        </ul>
        {data && data.n_inputs > data.inputs.length && <p className="mt-1 text-xs text-zinc-500">{w.more(data.n_inputs - data.inputs.length)}</p>}
      </section>

      <section className="min-w-0">
        {draft.parent && (
          <div className="mb-3 flex flex-wrap items-center gap-2 rounded-md bg-blue-500/[0.07] px-3 py-2 text-xs text-blue-100 ring-1 ring-blue-400/20">
            <GitBranch className="size-3.5" />{w.continueFrom(draft.parent.ref)}
            {draft.parent.note && <span className="truncate text-blue-200/70">· {draft.parent.note}</span>}
            <button onClick={() => setDraft({ values, parent: null })} className={`${btnLink} ml-auto`}>{w.stopContinue}</button>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-zinc-300">{w.changes(changes.length)}</span>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder={w.note} className={`${input} min-w-[12rem] flex-1 py-1 text-xs`} />
          <button onClick={() => run(false)} disabled={!!busy || (!changes.length && !parent)} className={btnGhost}>
            {busy === "preview" ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}{w.preview}
          </button>
          <button onClick={() => run(true)} disabled={!!busy || (!changes.length && !parent)} className={btnPrimary}>
            {busy === "save" ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}{w.save}
          </button>
          {changes.length > 0 && <button onClick={() => { setDraft({ values: {}, parent: null }); setResult(null); setSavedRef(null); }} className={btnQuiet}><X className="size-3.5" />{w.clear}</button>}
        </div>
        <ErrorLine text={error} />
        {savedRef && (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-md bg-emerald-400/[0.07] px-3 py-2 text-xs text-emerald-100 ring-1 ring-emerald-400/20">
            <Check className="size-3.5" />{w.saved(savedRef)}
            {isLatest && <button onClick={() => setProposing(true)} className={`${btnGhost} ml-auto py-1`}><Check className="size-3.5" />{w.propose}</button>}
          </div>
        )}
        {proposed && <p className="mt-3 rounded-md bg-amber-400/[0.07] px-3 py-2 text-xs text-amber-100 ring-1 ring-amber-300/20">{t.twin.history.proposed(proposed)}</p>}
        {savedRef && (
          <ConfirmDialog open={proposing} title={`${t.twin.history.proposeTitle}（${savedRef}）`} body={t.twin.history.proposeBody}
            confirm={t.twin.history.propose} note={{ placeholder: t.twin.history.proposeReason, required: true }}
            onClose={() => setProposing(false)}
            onConfirm={async (reason) => {
              const rec = await api.propose(pid, { workbook: wid, from_scenario: savedRef, reason, by });
              setProposed(rec.ref); onHistoryChanged();
            }} />
        )}

        {!result ? (
          <p className="mt-6 text-sm text-zinc-500">{w.hint}</p>
        ) : (
          <div className="mt-4 space-y-4">
            <Warnings items={result.warnings} notices={result.notices} />
            {result.outputs.length > 0 && (
              <div>
                <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">{w.outputs}</h4>
                <ChangeTable rows={result.outputs} onOpenCell={onOpenCell} empty={w.noChange} />
              </div>
            )}
            <div>
              <h4 className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-500">{w.changed(result.n_changed)}</h4>
              <ChangeTable rows={result.changed} onOpenCell={onOpenCell} empty={w.noChange} />
              {result.truncated && <p className="mt-1 text-xs text-zinc-500">{w.truncated}</p>}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

// ---- Sensitivity ------------------------------------------------------------------------------------------------------

function SensitivityTab({ pid, wid, by, onOpenCell, onHistoryChanged }: {
  pid: string; wid: string; by?: string; onOpenCell: (cell: string) => void; onHistoryChanged: () => void;
}) {
  const { t } = useI18n();
  const s = t.twin.sensitivity;
  const [target, setTarget] = useState("");
  const [step, setStep] = useState(10);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<{ ref: string; result: SensitivityResult } | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setBusy(true); setError(null);
    try {
      const rec = await api.sensitivity(pid, { workbook: wid, target, step: step / 100, by });
      setRes({ ref: rec.ref, result: rec.result }); onHistoryChanged();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  return (
    <div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="min-w-[18rem] flex-1 text-xs text-zinc-400">{s.target}
          <div className="mt-1"><CellPicker pid={pid} wid={wid} kind="targets" value={target} onChange={setTarget} placeholder={s.targetPlaceholder} /></div>
        </label>
        <label className="text-xs text-zinc-400">{s.step}
          <span className="mt-1 flex items-center gap-1"><input type="number" min={1} max={100} value={step} onChange={(e) => setStep(Number(e.target.value) || 10)} className={`${input} w-20`} />%</span>
        </label>
        <button onClick={run} disabled={busy || !target} className={btnPrimary}>{busy ? <Loader2 className="size-3.5 animate-spin" /> : <Waypoints className="size-3.5" />}{busy ? s.running : s.run}</button>
      </div>
      <ErrorLine text={error} />
      {res && <SensitivityResultView ref_={res.ref} result={res.result} onOpenCell={onOpenCell} />}
    </div>
  );
}

function SensitivityResultView({ ref_, result: r, onOpenCell }: { ref_?: string; result: SensitivityResult; onOpenCell: (cell: string) => void }) {
  const { t } = useI18n();
  const s = t.twin.sensitivity;
  const rows = r.inputs.filter((x) => typeof x.target_low === "number" && typeof x.target_high === "number");
  const base = r.target.value;
  const lo = Math.min(base, ...rows.map((x) => Math.min(x.target_low as number, x.target_high as number)));
  const hi = Math.max(base, ...rows.map((x) => Math.max(x.target_low as number, x.target_high as number)));
  const span = hi - lo || 1;
  const pos = (v: number) => `${((v - lo) / span) * 100}%`;
  return (
    <div className="mt-5 space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-400">
        {ref_ && <span className="rounded-md bg-white/5 px-2 py-0.5 font-mono text-zinc-200 ring-1 ring-white/10">{ref_}</span>}
        <span>{s.chartTitle(r.target.cell, Math.round(r.step * 1000) / 10)}</span>
        <span>· {s.base} <span className="tabular-nums text-zinc-100">{fmtValue(base)}</span></span>
        <Pill status={r.target.status} />
      </div>
      {rows.length === 0 ? (
        <p className="text-sm text-zinc-500">{s.none}</p>
      ) : (
        <div className={card}>
          <ul className="space-y-2">
            {rows.map((x) => {
              const a = Math.min(x.target_low as number, x.target_high as number);
              const b = Math.max(x.target_low as number, x.target_high as number);
              return (
                <li key={x.cell} className="grid grid-cols-[minmax(0,12rem)_1fr_auto] items-center gap-3 text-xs">
                  <span className="min-w-0 truncate" title={x.label ?? x.cell}>
                    <button onClick={() => onOpenCell(x.cell)} className={`${btnLink} font-mono`}>{x.cell}</button>
                    {x.label && <span className="ml-1.5 text-zinc-400">{x.label}</span>}
                  </span>
                  <span className="relative h-5 rounded bg-white/[0.03]">
                    <span className="absolute top-0 h-full w-px bg-zinc-400" style={{ left: pos(base) }} />
                    <span className="absolute top-1 h-3 rounded-sm bg-blue-500/70" style={{ left: pos(a), width: `max(2px, calc(${pos(b)} - ${pos(a)}))` }}
                      title={`${fmtValue(x.target_low)} … ${fmtValue(x.target_high)}`} />
                  </span>
                  <span className="w-28 text-right tabular-nums text-zinc-300">±{fmtValue(x.swing !== null ? x.swing / 2 : null)}</span>
                </li>
              );
            })}
          </ul>
          <div className="mt-2 grid grid-cols-[minmax(0,12rem)_1fr_auto] gap-3 text-[11px] tabular-nums text-zinc-500">
            <span /><span className="flex justify-between"><span>{fmtValue(lo)}</span><span>{fmtValue(hi)}</span></span><span className="w-28" />
          </div>
        </div>
      )}
      {rows.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-white/[0.07]">
          <table className="w-full text-xs">
            <thead className="bg-white/[0.03]"><tr>
              <th className={th}>{s.input}</th><th className={`${th} text-right`}>{t.twin.whatif.current}</th>
              <th className={`${th} text-right`}>{s.targetLow}</th><th className={`${th} text-right`}>{s.targetHigh}</th>
              <th className={`${th} text-right`}>{s.swing}</th><th className={`${th} text-right`}>{s.elasticity}</th>
            </tr></thead>
            <tbody>
              {r.inputs.map((x) => (
                <tr key={x.cell} className="border-t border-white/[0.05]">
                  <td className={td}><span className="font-mono text-blue-300">{x.cell}</span>{x.label && <span className="ml-1.5 text-zinc-400">{x.label}</span>}</td>
                  <td className={`${td} text-right tabular-nums text-zinc-300`}>{fmtValue(x.value)}</td>
                  <td className={`${td} text-right tabular-nums text-zinc-200`}>{fmtValue(x.target_low)}</td>
                  <td className={`${td} text-right tabular-nums text-zinc-200`}>{fmtValue(x.target_high)}</td>
                  <td className={`${td} text-right tabular-nums text-white`}>{fmtValue(x.swing)}</td>
                  <td className={`${td} text-right tabular-nums text-zinc-400`}>{x.elasticity === null ? "—" : x.elasticity.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {r.skipped.length > 0 && (
        <p className="text-xs text-zinc-500">{s.skipped}: {r.skipped.slice(0, 12).map((x) => `${x.cell} (${x.reason})`).join(" · ")}</p>
      )}
    </div>
  );
}

// ---- Goal seek --------------------------------------------------------------------------------------------------------

function GoalTab({ pid, wid, by, onOpenCell, onUse, onHistoryChanged }: {
  pid: string; wid: string; by?: string; onOpenCell: (cell: string) => void; onUse: (rec: HistoryRecord) => void; onHistoryChanged: () => void;
}) {
  const { t } = useI18n();
  const g = t.twin.goal;
  const [target, setTarget] = useState("");
  const [goal, setGoal] = useState("");
  const [changing, setChanging] = useState("");
  const [busy, setBusy] = useState(false);
  const [rec, setRec] = useState<(HistoryRecord & { result: GoalSeekResult }) | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setBusy(true); setError(null);
    try {
      const r = await api.goalSeek(pid, { workbook: wid, target, goal: Number(goal.replace(/,/g, "")), changing, by });
      setRec(r); onHistoryChanged();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const r = rec?.result;

  return (
    <div className="max-w-3xl">
      <div className="grid gap-3 md:grid-cols-[1fr_9rem]">
        <label className="text-xs text-zinc-400">{g.target}<div className="mt-1"><CellPicker pid={pid} wid={wid} kind="targets" value={target} onChange={setTarget} placeholder={t.twin.sensitivity.targetPlaceholder} /></div></label>
        <label className="text-xs text-zinc-400">{g.goal}<input value={goal} onChange={(e) => setGoal(e.target.value)} inputMode="decimal" className={`${input} mt-1 w-full tabular-nums`} /></label>
        <label className="text-xs text-zinc-400">{g.changing}<div className="mt-1"><CellPicker pid={pid} wid={wid} kind="inputs" value={changing} onChange={setChanging} placeholder={t.twin.whatif.search} /></div></label>
        <div className="flex items-end"><button onClick={run} disabled={busy || !target || !changing || goal.trim() === "" || Number.isNaN(Number(goal.replace(/,/g, "")))} className={`${btnPrimary} w-full justify-center`}>{busy ? <Loader2 className="size-3.5 animate-spin" /> : <Target className="size-3.5" />}{busy ? g.running : g.run}</button></div>
      </div>
      <ErrorLine text={error} />
      {rec && r && (
        <div className={`${card} mt-5`}>
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-md bg-white/5 px-2 py-0.5 font-mono text-xs text-zinc-200 ring-1 ring-white/10">{rec.ref}</span>
            <Pill status={r.found ? "verified" : "untranslated"} label={r.found ? g.found : g.notFound} title="" />
            <Pill status={r.target.status} />
            <span className="text-xs text-zinc-500">{g.evaluations(r.evaluations)}</span>
          </div>
          <p className="mt-3 text-lg font-semibold text-white">{g.answer(r.changing.cell, fmtValue(r.changing.value))}</p>
          <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
            <div className="rounded-md bg-white/[0.03] px-3 py-2">
              <dt className="text-xs text-zinc-500"><button onClick={() => onOpenCell(r.changing.cell)} className={`${btnLink} font-mono`}>{r.changing.cell}</button> {r.changing.label}</dt>
              <dd className="mt-0.5 tabular-nums text-zinc-200">{g.before} {fmtValue(r.changing.before)} → <span className="font-medium text-white">{fmtValue(r.changing.value)}</span></dd>
            </div>
            <div className="rounded-md bg-white/[0.03] px-3 py-2">
              <dt className="text-xs text-zinc-500"><button onClick={() => onOpenCell(r.target.cell)} className={`${btnLink} font-mono`}>{r.target.cell}</button> {r.target.label}</dt>
              <dd className="mt-0.5 tabular-nums text-zinc-200">{g.before} {fmtValue(r.target.before)} → {g.achieved} <span className="font-medium text-white">{fmtValue(r.target.achieved)}</span></dd>
            </div>
          </dl>
          {r.note && <p className="mt-2 text-xs text-amber-200">{r.note}</p>}
          <button onClick={() => onUse(rec)} className={`${btnGhost} mt-4`}><FlaskConical className="size-3.5" />{g.useIt}</button>
        </div>
      )}
    </div>
  );
}

// ---- History and corrections ----------------------------------------------------------------------------------------

const ANALYSES = new Set(["scenario", "sensitivity", "goal_seek"]);

function HistoryTab({ pid, wid, versions, latest, by, openRef, onOpened, onOpenCell, onContinue, onNewVersion, onHistoryChanged }: {
  pid: string; wid: string; versions: Workbook[]; latest?: Workbook; by?: string; openRef: string | null; onOpened?: () => void;
  onOpenCell: (cell: string) => void; onContinue: (rec: HistoryRecord) => void; onNewVersion: (id: string) => Promise<void>;
  onHistoryChanged: () => void;
}) {
  const { t } = useI18n();
  const h = t.twin.history;
  const [records, setRecords] = useState<HistoryRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState<string | null>(null);
  const [onlyFile, setOnlyFile] = useState(true);
  const [open, setOpen] = useState<string | null>(null);
  const [comparing, setComparing] = useState<HistoryRecord | null>(null);
  const [reloads, setReloads] = useState(0);
  const fileIds = useMemo(() => new Set(versions.map((v) => v.id)), [versions]);
  const versionOf = useCallback((id: string) => versions.find((v) => v.id === id), [versions]);

  useEffect(() => {
    let live = true;
    api.history(pid).then((d) => live && (setRecords(d.records), setError(null)), (e) => live && setError((e as Error).message));
    return () => { live = false; };
  }, [pid, reloads]);

  // A record to show (from a chat answer): open it, whatever the filters (state adjusted while rendering).
  const [seenRef, setSeenRef] = useState<string | null>(null);
  if (openRef !== seenRef) {
    setSeenRef(openRef);
    if (openRef) { setOpen(openRef); setOnlyFile(false); setKind(null); }
  }
  useEffect(() => {
    if (openRef) onOpened?.();
  }, [openRef, onOpened]);

  const changed = () => { setReloads((n) => n + 1); onHistoryChanged(); };

  if (!records) return error ? <ErrorLine text={error} /> : <Loader2 className="size-5 animate-spin text-zinc-500" />;

  const pending = records.filter((r) => r.status === "pending");
  const matches = (r: HistoryRecord) =>
    !kind || (kind === "corrections" ? !ANALYSES.has(r.kind) : kind === "pending" ? r.status === "pending" : r.kind === kind);
  const shown = records.filter((r) => matches(r) && (!onlyFile || fileIds.has(r.workbook) || r.ref === open));
  const kinds = ["pending", "scenario", "sensitivity", "goal_seek", "corrections"];

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_18rem]">
      <section className="min-w-0">
        {pending.length > 0 && kind !== "pending" && (
          <button onClick={() => setKind("pending")}
            className={`mb-3 flex w-full items-center gap-2 rounded-md bg-amber-400/[0.08] px-3 py-2 text-left text-sm text-amber-100 ring-1 ring-amber-300/25 transition hover:bg-amber-400/[0.14] ${focus}`}>
            <CircleAlert className="size-4" />{h.pending(pending.length)}<ChevronRight className="ml-auto size-4" />
          </button>
        )}
        <div className="flex flex-wrap items-center gap-1.5" role="group">
          {[null, ...kinds].map((k) => (
            <button key={k ?? "all"} onClick={() => setKind(k)} aria-pressed={kind === k}
              className={`rounded-full border px-2.5 py-1 text-xs transition ${focus} ${kind === k ? "border-blue-400/50 bg-blue-500/15 text-white" : "border-white/10 text-zinc-300 hover:border-white/20 hover:bg-white/5 hover:text-white"}`}>
              {k === null ? h.all : k === "pending" ? `${h.states.pending}${pending.length ? ` ${pending.length}` : ""}` : k === "corrections" ? h.kinds.correction : h.kinds[k]}
            </button>
          ))}
          <label className="ml-auto flex cursor-pointer items-center gap-1.5 text-xs text-zinc-400 hover:text-zinc-200">
            <input type="checkbox" checked={onlyFile} onChange={(e) => setOnlyFile(e.target.checked)} className="accent-blue-500" />{h.thisFile}
          </label>
        </div>
        <ErrorLine text={error} />

        {shown.length === 0 ? (
          <p className="mt-8 text-center text-sm text-zinc-500">{h.empty}</p>
        ) : (
          <ul className="mt-3 space-y-1.5">
            {shown.map((r) => (
              <RecordRow key={r.ref} pid={pid} rec={r} open={open === r.ref} onToggle={() => setOpen(open === r.ref ? null : r.ref)}
                version={versionOf(r.workbook)} versions={versions} currentId={wid} latest={latest} by={by}
                onCompare={() => setComparing(r)} onContinue={onContinue} onOpenCell={onOpenCell}
                onChanged={changed} onNewVersion={onNewVersion} />
            ))}
          </ul>
        )}
      </section>

      <VersionsPanel pid={pid} wid={wid} versions={versions} latest={latest} by={by} onChanged={changed} onOpen={onNewVersion}
        onOpenCell={onOpenCell} onOpenRecord={(ref) => { setOpen(ref); setOnlyFile(false); setKind(null); }} />
      <ComparePicker pid={pid} base={comparing} records={records} fileIds={fileIds} versionOf={versionOf}
        onClose={() => setComparing(null)} onOpenCell={onOpenCell} />
    </div>
  );
}

/** One line for a history row, in the UI's language (from the record's kind and parameters). */
function useSummary() {
  const { t } = useI18n();
  const m = t.twin.summary;
  return (r: HistoryRecord): string => {
    const p = r.params ?? {};
    const n = Object.keys(r.changes).length;
    if (r.kind === "scenario") return r.note || m.scenario(n);
    if (r.kind === "sensitivity") return r.note || m.sensitivity(String(p.target), Math.round(Number(p.step ?? 0) * 1000) / 10);
    if (r.kind === "goal_seek") return r.note || m.goal_seek(String(p.target), fmtValue(p.goal), String(p.changing));
    if (r.kind === "revert") return `${m.revert(String(p.to_name ?? p.to))}${p.reason ? ` — ${p.reason}` : ""}`;
    if (r.kind === "edit") return m.edit(n, String(p.via ?? "edit"));
    return p.reason ? String(p.reason) : m.correction(n);
  };
}

const KIND_ICON = { scenario: FlaskConical, sensitivity: Waypoints, goal_seek: Target, correction: Check, revert: Undo2, edit: Pencil } as const;

function RecordMeta({ rec, version }: { rec: HistoryRecord; version?: Workbook }) {
  const { t } = useI18n();
  const h = t.twin.history;
  const when = new Date(rec.created).toLocaleString(t.locale, { dateStyle: "short", timeStyle: "short" });
  return (
    <span className="block truncate text-[11px] text-zinc-500">
      {h.kinds[rec.kind]} · {rec.author} · {when}
      {version ? ` · v${version.version}` : rec.workbook_stored === false ? ` · ${h.gone}` : ` · ${rec.workbook_name ?? rec.workbook}`}
      {rec.parent ? ` · ${h.parent(`S${rec.parent}`)}` : ""}
    </span>
  );
}

function RecordRow({ pid, rec, open, onToggle, version, versions, currentId, latest, by, onCompare, onContinue, onOpenCell, onChanged, onNewVersion }: {
  pid: string; rec: HistoryRecord; open: boolean; onToggle: () => void; version?: Workbook; versions: Workbook[]; currentId: string; latest?: Workbook;
  by?: string; onCompare: () => void; onContinue: (rec: HistoryRecord) => void;
  onOpenCell: (cell: string) => void; onChanged: () => void; onNewVersion: (id: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const h = t.twin.history;
  const Icon = KIND_ICON[rec.kind] ?? History;
  const summarize = useSummary();
  return (
    <li className={`rounded-md ring-1 transition ${open ? "bg-white/[0.04] ring-blue-400/30" : rec.status === "pending" ? "bg-amber-400/[0.04] ring-amber-300/20 hover:ring-amber-300/40" : "bg-white/[0.02] ring-white/[0.06] hover:ring-white/15"}`}>
      <button onClick={onToggle} aria-expanded={open} className={`flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left ${focus}`}>
        {open ? <ChevronDown className="size-4 shrink-0 text-zinc-500" /> : <ChevronRight className="size-4 shrink-0 text-zinc-500" />}
        <Icon className="size-4 shrink-0 text-blue-300" />
        <span className="w-10 shrink-0 font-mono text-xs text-zinc-200">{rec.ref}</span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="truncate text-sm text-zinc-100">{summarize(rec)}</span>
            {rec.source === "agent" && <span title={h.agentNote} className="flex shrink-0 items-center gap-0.5 rounded bg-violet-400/10 px-1.5 text-[11px] text-violet-200 ring-1 ring-violet-300/20"><Bot className="size-3" />AI</span>}
          </span>
          <RecordMeta rec={rec} version={version} />
        </span>
        {rec.status !== "recorded" && <Pill status={rec.status} label={h.states[rec.kind === "edit" ? "applied" : rec.status]} title="" />}
      </button>
      {open && (
        <RecordDetail pid={pid} rec={rec} currentId={currentId} latest={latest} versions={versions} by={by} onCompare={onCompare}
          onContinue={onContinue} onOpenCell={onOpenCell} onChanged={onChanged} onNewVersion={onNewVersion} />
      )}
    </li>
  );
}

function RecordDetail({ pid, rec, currentId, latest, versions, by, onCompare, onContinue, onOpenCell, onChanged, onNewVersion }: {
  pid: string; rec: HistoryRecord; currentId: string; latest?: Workbook; versions: Workbook[]; by?: string; onCompare: () => void;
  onContinue: (rec: HistoryRecord) => void; onOpenCell: (cell: string) => void; onChanged: () => void; onNewVersion: (id: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const h = t.twin.history;
  const [full, setFull] = useState<HistoryRecord | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [ask, setAsk] = useState<"approve" | "reject" | "withdraw" | "reapply" | "repropose" | null>(null);
  const [diff, setDiff] = useState<CompareResult | null>(null);
  const [reapplyProblems, setReapplyProblems] = useState<{ cell: string; ok: boolean; problem?: string }[] | null>(null);
  const [reloads, setReloads] = useState(0);
  const analysis = ANALYSES.has(rec.kind);
  const correction = !analysis;

  useEffect(() => {
    let live = true;
    api.historyRecord(pid, rec.ref).then((r) => live && setFull(r), (e) => live && setError((e as Error).message));
    return () => { live = false; };
  }, [pid, rec.ref, rec.status, reloads]);

  async function act(name: string, fn: () => Promise<unknown>) {
    setBusy(name); setError(null); setMsg(null);
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  const r = full ?? rec;
  const result = (full?.result ?? {}) as Record<string, unknown>;
  const old = (r.params?.old ?? {}) as Record<string, string>;
  const impact = correction ? (result.impact as ScenarioResult | null) : null;
  const outputs = (correction ? (impact?.outputs ?? []) : ((result.outputs as CellChange[] | undefined) ?? [])) as CellChange[];
  const reason = r.params?.reason as string | undefined;
  const stored = r.workbook_stored !== false;
  const versionNo = (id: string | null | undefined) => versions.find((w) => w.id === id)?.version;
  const madeOn = versionNo(r.workbook) ? `v${versionNo(r.workbook)}` : null;
  const latestName = latest ? `v${latest.version}` : null;
  const appliedVersion = versionNo(r.applied_as);
  const appliedStored = !!appliedVersion;
  // A pending correction made on a version that is no longer current cannot be approved: propose it again instead.
  const stale = r.status === "pending" && !!latest && latest.id !== r.workbook;
  const nChanges = Object.keys(r.changes).length;
  const changeList = (
    <ul className="mt-2 space-y-0.5 rounded-md bg-white/[0.03] px-3 py-2 font-mono text-xs">
      {Object.entries(r.changes).slice(0, 6).map(([cell, v]) => (
        <li key={cell} className="truncate"><span className="text-blue-300">{cell}</span> <span className="text-zinc-500">{old[cell] || "—"} →</span> <span className="text-white">{fmtValue(v)}</span></li>
      ))}
      {nChanges > 6 && <li className="text-zinc-500">{t.twin.verify.more(nChanges - 6)}</li>}
    </ul>
  );

  return (
    <div className="space-y-4 border-t border-white/[0.06] px-4 py-4 text-xs">
      {(reason || r.note) && (
        <p className="text-sm text-zinc-200">{reason ? <><span className="text-zinc-500">{h.reason}</span>{reason}</> : r.note}</p>
      )}
      {r.kind === "edit" && <p className="text-sm text-zinc-300">{h.editNote(String(r.params?.via ?? "edit"))}</p>}

      {nChanges > 0 && (
        <div>
          <h4 className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{h.changes}</h4>
          <div className="overflow-x-auto rounded-md border border-white/[0.07]">
            <table className="w-full">
              <thead className="bg-white/[0.03]"><tr><th className={th}>{t.twin.whatif.cell}</th>{correction && <th className={th}>{h.old}</th>}<th className={th}>{h.new}</th></tr></thead>
              <tbody>
                {Object.entries(r.changes).slice(0, 100).map(([cell, v]) => (
                  <tr key={cell} className="border-t border-white/[0.05]">
                    <td className={td}><button onClick={() => onOpenCell(cell)} className={`${btnLink} font-mono`}>{cell}</button></td>
                    {correction && <td className={`${td} font-mono text-zinc-400`}>{old[cell] === "" ? "—" : old[cell] ?? "—"}</td>}
                    <td className={`${td} font-mono text-white`}>{fmtValue(v)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {rec.kind === "sensitivity" && full?.result && <SensitivityResultView result={full.result as unknown as SensitivityResult} onOpenCell={onOpenCell} />}
      {rec.kind === "goal_seek" && full?.result && (() => {
        const g = full.result as unknown as GoalSeekResult;
        return <p className="text-sm text-zinc-200">{t.twin.goal.answer(g.changing.cell, fmtValue(g.changing.value))} → {g.target.cell} = {fmtValue(g.target.achieved)} {!g.found && <span className="text-amber-200">({t.twin.goal.notFound})</span>}</p>;
      })()}
      {outputs.length > 0 && (
        <div>
          <h4 className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{correction ? h.impact : h.outputs}</h4>
          <ChangeTable rows={outputs.filter((o) => o.delta !== 0)} onOpenCell={onOpenCell} empty={t.twin.whatif.noChange} />
        </div>
      )}
      {correction && typeof result.formula_edits === "number" && result.formula_edits > 0 && <p className="text-zinc-400">{h.formulaEdits(result.formula_edits)}</p>}

      {r.events.length > 0 && (
        <div>
          <h4 className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{h.events}</h4>
          <ol className="space-y-1 border-l border-white/10 pl-3">
            {r.events.map((e, i) => (
              <li key={i} className="text-zinc-400">
                <span className="text-zinc-200">{h.actions[r.kind === "edit" && e.action === "approved" ? "edited" : e.action] ?? e.action}</span> · {e.by} · {new Date(e.at).toLocaleString(t.locale, { dateStyle: "short", timeStyle: "short" })}
                {e.note && <span className="text-zinc-300"> — {e.note}</span>}
                {e.action === "replayed" && e.data && <span className={e.data.same ? "text-emerald-300" : "text-amber-200"}> · {e.data.same ? h.replaySame : h.replayDiff}</span>}
              </li>
            ))}
          </ol>
        </div>
      )}

      {msg && <p className="rounded-md bg-emerald-400/[0.07] px-3 py-2 text-emerald-100 ring-1 ring-emerald-400/20">{msg}</p>}
      <ErrorLine text={error} />

      <div className="flex flex-wrap items-center gap-2 border-t border-white/[0.06] pt-3">
        {analysis ? (
          <>
            <Action icon={RefreshCw} label={h.replay} busy={busy === "replay"} disabled={!!busy} why={!stored ? h.why.gone : null}
              onClick={() => act("replay", async () => {
                const res = await api.replay(pid, rec.ref, by);
                setMsg(res.same ? h.replaySame : h.replayDiff); setReloads((n) => n + 1);
              })} />
            <Action icon={Download} label={h.export} why={!stored ? h.why.gone : null}
              href={api.recordExportUrl(pid, rec.ref) + (by ? `?by=${encodeURIComponent(by)}` : "")} />
            <Action icon={GitBranch} label={h.continue} onClick={() => onContinue(r)}
              why={!stored ? h.why.gone : r.workbook !== currentId ? h.why.otherVersion(madeOn ?? "?") : null} />
            <Action icon={GitCompare} label={h.compare} onClick={onCompare} />
            <Action icon={RotateCcw} label={h.reapply} busy={busy === "reapply"} disabled={!!busy}
              why={r.workbook === currentId ? h.why.sameVersion : null}
              onClick={() => act("reapply", async () => {
                const res = await api.reapply(pid, rec.ref, currentId, false, by);
                if (res.reapplied && res.record) { setMsg(t.twin.whatif.saved(res.record.ref)); onChanged(); }
                else { setReapplyProblems(res.checks.filter((c) => !c.ok)); setAsk("reapply"); }
              })} />
          </>
        ) : (
          <>
            {r.status === "pending" && (
              <>
                <Action icon={Check} label={h.approve} tone="primary" onClick={() => setAsk("approve")}
                  why={stale ? h.why.stale(madeOn ?? "?", latestName ?? "?") : null} />
                <Action icon={X} label={h.reject} onClick={() => setAsk("reject")} />
              </>
            )}
            <Action icon={Download} label={r.status === "approved" ? h.download : h.downloadPreview} why={!stored ? h.why.gone : null}
              href={api.recordExportUrl(pid, rec.ref) + (by ? `?by=${encodeURIComponent(by)}` : "")} />
            <Action icon={GitCompare} label={h.diffPrev} busy={busy === "diff"} disabled={!!busy}
              why={r.status === "pending" ? h.why.notApplied : r.status !== "approved" ? h.why.notAppliedEver : !stored || !appliedStored ? h.why.gone : null}
              onClick={() => act("diff", async () => setDiff(await api.correctionDiff(pid, rec.ref)))} />
            <Action icon={ChevronRight} label={appliedVersion ? `${h.openVersion} v${appliedVersion}` : h.openVersion}
              why={r.status === "pending" ? h.why.notApplied : r.status !== "approved" ? h.why.notAppliedEver : !appliedStored ? h.why.gone : null}
              onClick={() => onNewVersion(r.applied_as!)} />
            <Action icon={RotateCcw} label={h.repropose} onClick={() => setAsk("repropose")}
              why={!latest ? h.why.gone : r.status === "approved" ? h.why.applied : r.status === "pending" && !stale ? h.why.stillPending : null} />
            {r.status === "pending" && <Action icon={Undo2} label={h.withdraw} tone="quiet" className="ml-auto" onClick={() => setAsk("withdraw")} />}
          </>
        )}
      </div>

      <Modal open={!!diff} wide title={diff ? h.diffTitle(diff.a.ref, diff.b.ref) : ""} onClose={() => setDiff(null)}>
        {diff && <CompareBody data={diff} inputsTitle={h.diffInputs} onOpenCell={(c) => { setDiff(null); onOpenCell(c); }} />}
      </Modal>
      <ConfirmDialog open={ask === "repropose"} title={h.reproposeTitle(rec.ref, latestName ?? "?")} confirm={h.propose}
        body={<>{h.reproposeBody(latestName ?? "?")}{changeList}</>}
        note={{ placeholder: h.proposeReason, required: true, initial: reason ?? "" }} onClose={() => setAsk(null)}
        onConfirm={async (note) => {
          if (!latest) return;
          const edits = Object.fromEntries(Object.entries(r.changes).map(([c, v]) => [c, asTyped(v)]));
          const res = await api.propose(pid, { workbook: latest.id, edits, reason: note, by });
          setMsg(h.proposed(res.ref)); onChanged();
        }} />
      <ConfirmDialog open={ask === "approve"} title={h.approveTitle(rec.ref)} confirm={h.approve}
        body={<>{h.confirmApprove(nChanges)}{changeList}{latest && latest.id !== r.workbook && <p className="mt-2 text-amber-200">{h.onlyLatest}</p>}</>}
        note={{ placeholder: h.approveNote }} onClose={() => setAsk(null)}
        onConfirm={async (note) => {
          const res = await api.review(pid, rec.ref, "approve", note || undefined, by);
          onChanged();
          if (res.applied_as) {
            const reused = res.events.findLast((e) => e.action === "approved")?.data?.duplicate === true;
            const v = versions.find((w) => w.id === res.applied_as)?.version;
            setMsg(reused && v ? h.appliedSame(v) : h.applied(res.applied_as.slice(0, 7)));
            await onNewVersion(res.applied_as);
          }
        }} />
      <ConfirmDialog open={ask === "reject"} title={h.rejectTitle(rec.ref)} confirm={h.reject} tone="danger"
        body={<>{h.rejectBody}{changeList}</>} note={{ placeholder: h.rejectNote, required: true }} onClose={() => setAsk(null)}
        onConfirm={async (note) => { await api.review(pid, rec.ref, "reject", note, by); onChanged(); }} />
      <ConfirmDialog open={ask === "withdraw"} title={h.withdrawTitle(rec.ref)} confirm={h.withdraw} tone="danger"
        body={h.withdrawBody} onClose={() => setAsk(null)}
        onConfirm={async () => { await api.review(pid, rec.ref, "withdraw", undefined, by); onChanged(); }} />
      <ConfirmDialog open={ask === "reapply"} title={h.reapply} confirm={h.reapplyForce}
        body={<>{h.reapplyProblems}<ul className="mt-2 space-y-0.5 rounded-md bg-white/[0.03] px-3 py-2 text-xs">{(reapplyProblems ?? []).map((c) => <li key={c.cell}><span className="font-mono text-blue-300">{c.cell}</span> <span className="text-zinc-400">{c.problem}</span></li>)}</ul></>}
        onClose={() => setAsk(null)}
        onConfirm={async () => {
          const res = await api.reapply(pid, rec.ref, currentId, true, by);
          if (res.record) { setMsg(t.twin.whatif.saved(res.record.ref)); onChanged(); }
        }} />
    </div>
  );
}

/** Pick another analysis to compare with `base` (searchable, in a dialog), then show both side by side. */
function ComparePicker({ pid, base, records, fileIds, versionOf, onClose, onOpenCell }: {
  pid: string; base: HistoryRecord | null; records: HistoryRecord[]; fileIds: Set<string>; versionOf: (id: string) => Workbook | undefined;
  onClose: () => void; onOpenCell: (cell: string) => void;
}) {
  const { t } = useI18n();
  const h = t.twin.history;
  const summarize = useSummary();
  const [query, setQuery] = useState("");
  const [onlyFile, setOnlyFile] = useState(true);
  const [result, setResult] = useState<CompareResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const close = () => { setResult(null); setQuery(""); setError(null); onClose(); };
  async function pick(other: HistoryRecord) {
    if (!base) return;
    setBusy(other.ref); setError(null);
    try { setResult(await api.compare(pid, base.ref, other.ref)); } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }
  const q = query.trim().toLowerCase();
  const candidates = base ? records.filter((r) => ANALYSES.has(r.kind) && r.ref !== base.ref && (!onlyFile || fileIds.has(r.workbook))
    && (!q || r.ref.toLowerCase().includes(q) || summarize(r).toLowerCase().includes(q) || r.author.toLowerCase().includes(q))) : [];

  return (
    <Modal open={!!base} wide title={result ? h.compareTitle(result.a.ref, result.b.ref) : h.pickCompare(base?.ref ?? "")} onClose={close} busy={!!busy}>
      {result ? (
        <>
          <button onClick={() => setResult(null)} className={`${btnQuiet} -ml-2 mb-2`}><ChevronRight className="size-3.5 rotate-180" />{h.back}</button>
          <CompareBody data={result} onOpenCell={(c) => { close(); onOpenCell(c); }} />
        </>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <label className={`${input} flex min-w-[14rem] flex-1 items-center gap-2 py-1.5`}>
              <Search className="size-3.5 text-zinc-500" />
              <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={h.compareSearch} autoFocus className="min-w-0 flex-1 bg-transparent text-sm outline-none" />
            </label>
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-zinc-400 hover:text-zinc-200">
              <input type="checkbox" checked={onlyFile} onChange={(e) => setOnlyFile(e.target.checked)} className="accent-blue-500" />{h.thisFile}
            </label>
          </div>
          <ErrorLine text={error} />
          {candidates.length === 0 ? (
            <p className="py-8 text-center text-sm text-zinc-500">{h.compareNone}</p>
          ) : (
            <ul className="mt-3 space-y-1">
              {candidates.map((r) => {
                const Icon = KIND_ICON[r.kind] ?? History;
                return (
                  <li key={r.ref}>
                    <button onClick={() => pick(r)} disabled={!!busy}
                      className={`flex w-full items-center gap-3 rounded-md bg-white/[0.02] px-3 py-2 text-left ring-1 ring-white/[0.06] transition hover:bg-blue-500/[0.08] hover:ring-blue-400/30 disabled:opacity-60 ${focus}`}>
                      <Icon className="size-4 shrink-0 text-blue-300" />
                      <span className="w-10 shrink-0 font-mono text-xs text-zinc-200">{r.ref}</span>
                      <span className="min-w-0 flex-1"><span className="block truncate text-sm text-zinc-100">{summarize(r)}</span><RecordMeta rec={r} version={versionOf(r.workbook)} /></span>
                      {busy === r.ref ? <Loader2 className="size-4 animate-spin text-zinc-400" /> : <GitCompare className="size-4 text-zinc-500" />}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </Modal>
  );
}

function CompareBody({ data, onOpenCell, inputsTitle }: { data: CompareResult; onOpenCell: (cell: string) => void; inputsTitle?: string }) {
  const { t } = useI18n();
  const h = t.twin.history;
  if (!data.inputs.length && !data.cells.length) return <p className="text-sm text-zinc-400">{h.sameAll}</p>;
  return (
    <div className="space-y-4 text-xs">
      {data.note && <p className="text-zinc-500">{data.note}</p>}
      {[{ title: inputsTitle ?? h.compareInputs, rows: data.inputs }, { title: h.compareCells(data.n_cells), rows: data.cells }].map(({ title, rows }) => rows.length > 0 && (
        <div key={title}>
          <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{title}</p>
          <div className="overflow-x-auto rounded-md border border-white/[0.07]">
            <table className="w-full">
              <thead className="bg-white/[0.03]"><tr><th className={th}>{t.twin.whatif.cell}</th><th className={th}>{t.twin.whatif.label}</th><th className={`${th} text-right`}>{data.a.ref}</th><th className={`${th} text-right`}>{data.b.ref}</th><th className={`${th} text-right`}>{t.twin.whatif.delta}</th></tr></thead>
              <tbody>
                {rows.slice(0, 100).map((x) => (
                  <tr key={x.cell} className="border-t border-white/[0.05]">
                    <td className={td}><button onClick={() => onOpenCell(x.cell)} className={`${btnLink} font-mono`}>{x.cell}</button></td>
                    <td className={`${td} text-zinc-400`}><span className="block max-w-[12rem] truncate">{x.label}</span></td>
                    <td className={`${td} text-right tabular-nums text-zinc-200`}>{fmtValue(x.a)}</td>
                    <td className={`${td} text-right tabular-nums text-white`}>{fmtValue(x.b)}</td>
                    <td className={`${td} text-right`}><Delta d={"delta" in x && typeof x.delta === "number" ? x.delta : typeof x.a === "number" && typeof x.b === "number" ? x.b - x.a : null} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}

/** The file's versions, newest first: each opens what it holds (how it came about, what changed from the one before),
 * with a way to propose bringing an older one back. */
function VersionsPanel({ pid, wid, versions, latest, by, onChanged, onOpen, onOpenCell, onOpenRecord }: {
  pid: string; wid: string; versions: Workbook[]; latest?: Workbook; by?: string; onChanged: () => void;
  onOpen: (id: string) => Promise<void>; onOpenCell: (cell: string) => void; onOpenRecord: (ref: string) => void;
}) {
  const { t } = useI18n();
  const h = t.twin.history;
  const [target, setTarget] = useState<Workbook | null>(null);
  const [viewing, setViewing] = useState<Workbook | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const sorted = [...versions].sort((a, b) => b.version - a.version);
  return (
    <aside className={`${card} h-fit`}>
      <h3 className="flex items-center gap-1.5 text-sm font-medium text-zinc-200"><History className="size-4 text-blue-300" />{h.versions}</h3>
      <ul className="mt-3 space-y-1.5 text-xs">
        {sorted.map((v) => (
          <li key={v.id} className="rounded-md bg-white/[0.02] ring-1 ring-white/[0.06] transition hover:bg-white/[0.04] hover:ring-white/15">
            <button onClick={() => setViewing(v)} title={h.version.open} aria-label={`${h.version.open} v${v.version}`}
              className={`group w-full rounded-md px-2.5 pb-1.5 pt-2 text-left ${focus}`}>
              <span className="flex items-center gap-2">
                <span className="font-medium text-zinc-100">v{v.version}</span>
                <span className="font-mono text-zinc-500">{v.id.slice(0, 7)}</span>
                {v.id === wid && v.id !== latest?.id && <span className="size-1.5 rounded-full bg-blue-400" title={h.version.alreadyOpen} />}
                <span className="ml-auto flex items-center gap-1">
                  {v.id === latest?.id && <span className="rounded bg-emerald-400/10 px-1.5 text-[11px] text-emerald-300 ring-1 ring-emerald-400/20">{h.currentVersion}</span>}
                  <ChevronRight className="size-3.5 text-zinc-500 transition group-hover:translate-x-0.5 group-hover:text-zinc-200" />
                </span>
              </span>
              <span className="mt-0.5 block text-zinc-500">{v.first_uploaded ? new Date(v.first_uploaded).toLocaleString(t.locale, { dateStyle: "short", timeStyle: "short" }) : ""}</span>
            </button>
            {v.id !== latest?.id && (
              <div className="px-2.5 pb-2">
                <button onClick={() => { setTarget(v); setDone(null); }} className={`${btnGhost} w-full py-1`}><Undo2 className="size-3.5" />{h.revertTo(v.version)}</button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {done && <p className="mt-2 rounded-md bg-amber-400/[0.07] px-2.5 py-1.5 text-xs text-amber-100 ring-1 ring-amber-300/20">{h.proposed(done)}</p>}
      <VersionDetail pid={pid} version={viewing} wid={wid} latest={latest} onClose={() => setViewing(null)}
        onOpen={(id) => { setViewing(null); void onOpen(id); }}
        onRevert={(v) => { setViewing(null); setTarget(v); setDone(null); }}
        onOpenRecord={(ref) => { setViewing(null); onOpenRecord(ref); }}
        onOpenCell={(c) => { setViewing(null); onOpenCell(c); }} />
      <ConfirmDialog open={!!target} title={target ? h.revertTitle(target.version) : ""} confirm={h.propose}
        body={target && latest ? h.revertBody(target.version, latest.version) : null}
        note={{ placeholder: h.revertReason, required: true }} onClose={() => setTarget(null)}
        onConfirm={async (reason) => {
          if (!target || !latest) return;
          const rec = await api.proposeRevert(pid, { workbook: latest.id, to: target.id, reason, by });
          setDone(rec.ref); onChanged();
        }} />
    </aside>
  );
}

/** One version in a dialog: the history records that made it (or that none did), and what changed from the version
 * before it — the cells whose contents differ, then the results that differ. */
function VersionDetail({ pid, version, wid, latest, onClose, onOpen, onRevert, onOpenRecord, onOpenCell }: {
  pid: string; version: Workbook | null; wid: string; latest?: Workbook; onClose: () => void; onOpen: (id: string) => void;
  onRevert: (v: Workbook) => void; onOpenRecord: (ref: string) => void; onOpenCell: (cell: string) => void;
}) {
  const { t } = useI18n();
  const h = t.twin.history;
  const vt = h.version;
  const m = t.twin.summary;
  const [loaded, setLoaded] = useState<{ id: string; info?: VersionInfo; error?: string } | null>(null);
  const id = version?.id;
  useEffect(() => {
    if (!id) return;
    let live = true;
    api.versionInfo(pid, id).then((info) => live && setLoaded({ id, info }), (e) => live && setLoaded({ id, error: (e as Error).message }));
    return () => { live = false; };
  }, [pid, id]);
  const mine = loaded && loaded.id === id ? loaded : null;
  const info = mine?.info;
  const when = (iso: string | null) => iso ? new Date(iso).toLocaleString(t.locale, { dateStyle: "short", timeStyle: "short" }) : "";
  const label = (x: VersionInfo["made_by"][number]) => {
    const p = x.params ?? {};
    if (x.kind === "edit") return m.edit(x.n_changes, String(p.via ?? "edit"));
    if (x.kind === "revert") return `${m.revert(String(p.to_name ?? p.to))}${p.reason ? ` — ${p.reason}` : ""}`;
    return p.reason ? String(p.reason) : m.correction(x.n_changes);
  };
  const d = info?.diff;
  const heading = "mb-1.5 text-[11px] font-medium text-zinc-500";
  return (
    <Modal open={!!version} wide onClose={onClose}
      title={version ? <>{vt.title(version.version, version.id.slice(0, 7))}<span className="ml-2 font-normal text-zinc-500">{when(version.first_uploaded)}</span></> : ""}>
      {version && (
        <div className="space-y-5 text-xs">
          <section>
            <h4 className={heading}>{vt.origin}</h4>
            {!info ? (
              mine?.error ? <ErrorLine text={mine.error} /> : <Loader2 className="size-4 animate-spin text-zinc-500" />
            ) : info.made_by.length > 0 ? (
              <ul className="space-y-1.5">
                {info.made_by.map((x) => {
                  const Icon = KIND_ICON[x.kind] ?? History;
                  return (
                    <li key={x.ref} className="flex items-center gap-2.5 rounded-md bg-white/[0.03] px-3 py-2 ring-1 ring-white/[0.06]">
                      <Icon className="size-4 shrink-0 text-blue-300" />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm text-zinc-100">{label(x)}</span>
                        <span className="block truncate text-[11px] text-zinc-500">
                          {vt.madeBy(h.kinds[x.kind], x.ref, x.author, when(x.created))} · {vt.changes(x.n_changes)}
                          {x.source === "agent" ? " · AI" : ""}
                        </span>
                      </span>
                      <button onClick={() => onOpenRecord(x.ref)} className={`${btnGhost} shrink-0 py-1`}>{vt.showRecord}<ChevronRight className="size-3.5" /></button>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="text-sm text-zinc-300">{info.version === 1 ? vt.first : vt.unknown}</p>
            )}
          </section>

          {info && info.previous && (
            <section>
              <h4 className={heading}>{vt.vsPrev(info.previous.version)}</h4>
              {d ? (
                <div className="space-y-3">
                  {!!d.sheets_added?.length && <p className="text-zinc-300">{vt.sheetsAdded(d.sheets_added.join(", "))}</p>}
                  {!!d.sheets_removed?.length && <p className="text-zinc-300">{vt.sheetsRemoved(d.sheets_removed.join(", "))}</p>}
                  {!d.inputs.length && !d.cells.length && !d.sheets_added?.length && !d.sheets_removed?.length
                    ? <p className="text-sm text-zinc-300">{vt.sameAsPrev(info.previous.version)}</p>
                    : <CompareBody data={d} inputsTitle={vt.inputs(d.n_inputs ?? d.inputs.length)} onOpenCell={onOpenCell} />}
                  {d.truncated && <p className="text-amber-200">{vt.truncated}</p>}
                </div>
              ) : (
                <p className="text-zinc-400">{vt.noPrev[info.no_diff ?? "gone"]}</p>
              )}
            </section>
          )}
          {info && !info.previous && info.version > 1 && <p className="text-zinc-400">{vt.noPrev[info.no_diff ?? "gone"]}</p>}

          <div className="flex flex-wrap items-center gap-2 border-t border-white/[0.06] pt-3">
            <Action icon={ChevronRight} label={`${h.openVersion} v${version.version}`} onClick={() => onOpen(version.id)}
              why={version.id === wid ? vt.alreadyOpen : null} />
            <Action icon={Undo2} label={h.revertTo(version.version)} onClick={() => onRevert(version)}
              why={version.id === latest?.id ? h.why.current : null} />
          </div>
        </div>
      )}
    </Modal>
  );
}
