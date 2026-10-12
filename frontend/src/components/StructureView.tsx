"use client";

import { AlertTriangle, Loader2, MessageSquareText, Network, PencilRuler, RefreshCw, Table2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  api, colName, sheetOf, type CellDetail, type SheetCell, type SheetData, type Summary, type TableInfo, type TableSpec,
} from "@/lib/api";
import { apiErrorText, specToInfo, TableEditor, toSpec, type Box } from "@/components/TableEditor";
import { useI18n } from "@/lib/i18n";
import type { ResolveTarget } from "@/components/ResolveDialog";

const MAX_ROWS = 400;
const MAX_COLS = 52;
// Table colors: kept apart from the severity colors (red, amber), inputs (blue), outputs (green) and the selection.
const TABLE_COLORS = ["#9085e9", "#d55181", "#2bb3a3", "#7c93c9", "#9bbf3b", "#c084fc"];
const tableColor = (t: { table_id: number }) => TABLE_COLORS[(t.table_id - 1) % TABLE_COLORS.length];
const SEV_RING = { high: "#d03b3b", medium: "#fab219" } as const;

type Mode = "structure" | "roles";
type Part = "hdr" | "body" | "tot";
type Slot = { t: TableInfo; part: Part; top: boolean; bottom: boolean; left: boolean; right: boolean };

const tableTop = (t: TableInfo) => t.header_top || t.header_row || t.row_start;
const tableBottom = (t: TableInfo) => t.total_row || t.row_end;
const rangeA1 = (t: TableInfo) => `${colName(t.col_start)}${tableTop(t)}:${colName(t.col_end)}${tableBottom(t)}`;
const shown = (c: SheetCell) => (c.vtype === "formula" ? c.cached : c.value);

function fmt(v: unknown, nf?: string | null) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") {
    if (nf && nf.includes("%")) return `${(v * 100).toFixed((nf.split(".")[1] || "").replace(/[^0]/g, "").length)}%`;
    return Number.isInteger(v) ? v.toLocaleString() : (+v.toFixed(4)).toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  return String(v);
}

function cellIdFor(sheet: string, row: number, col: number) {
  const s = /^[A-Za-z_][A-Za-z0-9_.]*$/.test(sheet) ? sheet : `'${sheet.replace(/'/g, "''")}'`;
  return `${s}!${colName(col)}${row}`;
}

type Props = {
  workbookId: string | null;
  summary: Summary | null;
  focusCell: string | null;
  onTraceGraph: (cell: string) => void;
  onAsk: (cell: string) => void;
  onReparsed: () => void;
  onResolve: (target: ResolveTarget) => void;
};

export function StructureView({ workbookId, summary, focusCell, onTraceGraph, onAsk, onReparsed, onResolve }: Props) {
  const { t } = useI18n();
  const [picked, setSheet] = useState<string | null>(focusCell ? sheetOf(focusCell) : null);
  const [data, setData] = useState<{ key: string; sheet: SheetData } | null>(null);
  const [mode, setMode] = useState<Mode>("structure");
  const [showFormulas, setShowFormulas] = useState(false);
  const [selected, setSelected] = useState<string | null>(focusCell);
  const [fetched, setFetched] = useState<{ id: string; detail: CellDetail } | null>(null);
  const [focusTable, setFocusTable] = useState<number | null>(null);
  const [reparsing, setReparsing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  // Fixing tables: a draft of the sheet's tables, and a range dragged across the grid.
  const [draft, setDraft] = useState<TableSpec[] | null>(null);
  const [sel, setSel] = useState<Box | null>(null);
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const dragging = useRef<{ r: number; c: number } | null>(null);
  useEffect(() => {
    const up = () => { dragging.current = null; };
    window.addEventListener("mouseup", up);
    return () => window.removeEventListener("mouseup", up);
  }, []);
  const editing = draft !== null;

  // A cell to open from elsewhere (query results, links): switch to its sheet and select it.
  const [lastFocus, setLastFocus] = useState(focusCell);
  if (focusCell !== lastFocus) {
    setLastFocus(focusCell);
    if (focusCell) { setSheet(sheetOf(focusCell)); setSelected(focusCell); setFocusTable(null); }
  }
  const sheets = summary?.sheets ?? [];
  const sheet = picked && sheets.includes(picked) ? picked : sheets[0] ?? null;

  const key = workbookId && sheet ? `${workbookId}|${sheet}` : null;
  useEffect(() => {
    if (!workbookId || !sheet) return;
    let live = true;
    const k = `${workbookId}|${sheet}`;
    api.sheet(workbookId, sheet).then(
      (d) => { if (live) { setData({ key: k, sheet: d }); setError(null); } },
      (e) => live && setError((e as Error).message),
    );
    return () => { live = false; };
  }, [workbookId, sheet, summary]);
  const sd = data && data.key === key ? data.sheet : null;

  useEffect(() => {
    if (!workbookId || !selected) return;
    let live = true;
    api.cell(workbookId, selected).then((d) => live && setFetched({ id: selected, detail: d }), () => {});
    return () => { live = false; };
  }, [workbookId, selected]);
  const detail = fetched && fetched.id === selected ? fetched.detail : null;

  // Keep the selected cell or focused table in view once the grid is drawn.
  useEffect(() => {
    if (!sd || !gridRef.current) return;
    const target = selected
      ? gridRef.current.querySelector(`[data-id="${CSS.escape(selected)}"]`)
      : focusTable != null ? gridRef.current.querySelector(`[data-t0="${focusTable}"]`) : null;
    target?.scrollIntoView({ block: "center", inline: "nearest" });
  }, [sd, selected, focusTable]);

  // While fixing tables, the grid shows the draft (and the Excel Tables, which cannot be changed) instead.
  const shownTables = useMemo(() => {
    if (!sd || !draft || !sheet) return sd?.tables;
    const declared = sd.tables.filter((x) => x.source === "listobject");
    const drafts = draft.map((d, i) => specToInfo(d, declared.length + i + 1, sheet)).filter((x): x is TableInfo => !!x);
    return [...declared, ...drafts];
  }, [sd, draft, sheet]);
  const grid = useMemo(() => (sd && shownTables ? buildGrid(sd, shownTables) : null), [sd, shownTables]);

  if (!workbookId || !summary) {
    return <div className="grid flex-1 place-items-center text-sm text-zinc-500">{t.sheets.empty}</div>;
  }

  async function reparse() {
    if (!workbookId) return;
    setReparsing(true);
    try { await api.rebuild(workbookId); onReparsed(); } finally { setReparsing(false); }
  }

  function startEditing() {
    if (!sd) return;
    setDraft(sd.tables.filter((x) => x.source !== "listobject").map(toSpec));
    setMode("structure"); setSel(null); setSelected(null); setFocusTable(null); setEditError(null);
  }
  const stopEditing = () => { setDraft(null); setSel(null); setEditError(null); };
  async function saveTables() {
    if (!workbookId || !sheet || !draft) return;
    setSaving(true); setEditError(null);
    try { await api.setUserTables(workbookId, sheet, draft); stopEditing(); onReparsed(); }
    catch (e) { setEditError(apiErrorText(t, e, draft)); }
    finally { setSaving(false); }
  }
  async function resetTables() {
    if (!workbookId || !sheet) return;
    setSaving(true);
    try { await api.resetUserTables(workbookId, sheet); stopEditing(); onReparsed(); }
    catch (e) { setEditError((e as Error).message); }
    finally { setSaving(false); }
  }
  const dragStart = (r: number, c: number) => { dragging.current = { r, c }; setSel({ r1: r, c1: c, r2: r, c2: c }); };
  const dragOver = (r: number, c: number) => {
    const o = dragging.current;
    if (o) setSel({ r1: Math.min(o.r, r), c1: Math.min(o.c, c), r2: Math.max(o.r, r), c2: Math.max(o.c, c) });
  };
  const edited = !!(sheet && summary.user_tables?.[sheet]);

  const selectTable = (id: number) => { setSelected(null); setFocusTable(id); };
  const selectCell = (id: string) => { setFocusTable(null); setSelected(id); };
  const direct = new Set(detail?.precedents.filter((p) => p.depth === 1).map((p) => p.cell_id) ?? []);
  const group = detail?.group && detail.group.n_cells > 1 ? detail.group.group_id : null;
  const focused = sd?.tables.find((x) => x.table_id === focusTable) ?? null;

  return (
    <div className="flex min-h-0 flex-1 gap-4 p-4 md:p-6">
      <section className="flex min-w-0 flex-1 flex-col">
        {summary.stale && (
          <div className="mb-3 flex items-center gap-3 rounded-xl border border-amber-400/20 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-100">
            <AlertTriangle className="size-4 shrink-0" />
            <span className="flex-1">{t.sheets.stale}</span>
            <button onClick={reparse} disabled={reparsing}
              className="flex items-center gap-1.5 rounded-lg bg-amber-400/15 px-3 py-1 text-amber-50 ring-1 ring-amber-300/30 hover:bg-amber-400/25 disabled:opacity-60">
              {reparsing ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
              {reparsing ? t.sheets.reparsing : t.sheets.reparse}
            </button>
          </div>
        )}

        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div className="flex flex-wrap gap-1">
            {sheets.map((s) => (
              <button key={s} onClick={() => { setSheet(s); setSelected(null); setFocusTable(null); stopEditing(); }}
                className={`rounded-lg px-3 py-1 text-sm transition ${s === sheet ? "bg-violet-500/25 text-white ring-1 ring-violet-400/40" : "text-zinc-400 hover:bg-white/5 hover:text-zinc-200"}`}>
                {s}
              </button>
            ))}
          </div>
          <button onClick={editing ? stopEditing : startEditing} disabled={!sd} title={t.sheets.edit.startHint}
            className={`ml-auto flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs ring-1 transition ${editing ? "bg-violet-500/25 text-white ring-violet-400/40" : "text-zinc-300 ring-white/10 hover:bg-white/5"}`}>
            <PencilRuler className="size-3.5" />{t.sheets.edit.start}
          </button>
          <label className="flex items-center gap-1.5 text-xs text-zinc-400">
            <input type="checkbox" checked={showFormulas} onChange={(e) => setShowFormulas(e.target.checked)} className="accent-violet-500" />
            {t.sheets.formulas}
          </label>
          <div className="flex rounded-xl border border-white/10 bg-white/[0.03] p-0.5">
            {(["structure", "roles"] as const).map((m) => (
              <button key={m} onClick={() => setMode(m)} title={m === "structure" ? t.sheets.structureHint : t.sheets.rolesHint}
                className={`rounded-lg px-3 py-1 text-xs transition ${mode === m ? "bg-white/10 text-white" : "text-zinc-400 hover:text-zinc-200"}`}>
                {m === "structure" ? t.sheets.structure : t.sheets.roles}
              </button>
            ))}
          </div>
        </div>

        <div className="mb-2 flex flex-wrap items-center gap-1.5 text-xs">
          {sd && (sd.tables.length ? (
            <>
              <span className="text-zinc-500">{t.sheets.tablesOnSheet}</span>
              {sd.tables.map((tb) => (
                <button key={tb.table_id} onClick={() => selectTable(tb.table_id)}
                  className={`flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 transition ${focusTable === tb.table_id ? "bg-white/10 text-white" : "border-white/10 text-zinc-300 hover:bg-white/5"}`}
                  style={focusTable === tb.table_id ? { borderColor: tableColor(tb) } : undefined}>
                  <span className="size-2 rounded-full" style={{ background: tableColor(tb) }} />
                  {tb.name}
                  <span className="text-zinc-500">{rangeA1(tb)} · {t.sheets.rows(tb.row_end - tb.row_start + 1)}</span>
                  {tb.source === "user" && <span className="rounded bg-violet-500/20 px-1 text-[10px] text-violet-200">{t.sheets.edit.edited}</span>}
                </button>
              ))}
            </>
          ) : <span className="text-zinc-500">{t.sheets.noTables}</span>)}
        </div>

        <Legend mode={mode} />

        <div ref={gridRef} className={`sgrid min-h-0 flex-1 overflow-auto rounded-2xl border bg-[#141419]/90 ${editing ? "editing border-violet-400/40" : "border-white/[0.07]"}`}>
          {error && <p className="p-4 text-sm text-rose-300">{error}</p>}
          {!sd && !error && <div className="grid h-40 place-items-center"><Loader2 className="size-5 animate-spin text-zinc-500" /></div>}
          {sd && grid && (
            <>
              <table>
                <thead>
                  <tr>
                    <th />
                    {Array.from({ length: grid.maxC }, (_, i) => <th key={i}>{colName(i + 1)}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {Array.from({ length: grid.maxR }, (_, ri) => {
                    const r = ri + 1;
                    return (
                      <tr key={r}>
                        <th className="rh">{r}</th>
                        {Array.from({ length: grid.maxC }, (_, ci) => {
                          const c = ci + 1;
                          const cell = grid.byPos.get(`${r}:${c}`);
                          const slot = grid.tables.get(`${r}:${c}`);
                          return (
                            <GridCell key={c} r={r} c={c} cell={cell} slot={slot} mode={mode} showFormulas={showFormulas}
                              editing={editing} inSel={!!sel && r >= sel.r1 && r <= sel.r2 && c >= sel.c1 && c <= sel.c2}
                              onDown={dragStart} onOver={dragOver}
                              severity={cell ? grid.issues.get(cell.cell_id) : undefined}
                              param={cell ? grid.params.get(cell.cell_id) : undefined}
                              selected={!!cell && cell.cell_id === selected}
                              precedent={!!cell && direct.has(cell.cell_id)}
                              inGroup={!!cell && group != null && cell.group_id === group}
                              focused={!!slot && slot.t.table_id === focusTable}
                              onClick={cell ? () => selectCell(cell.cell_id) : undefined} />
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {grid.capped && <p className="px-3 py-2 text-xs text-zinc-500">{t.sheets.capped(MAX_ROWS, MAX_COLS)}</p>}
            </>
          )}
        </div>
      </section>

      <aside className="hidden w-[380px] shrink-0 overflow-y-auto rounded-2xl border border-white/[0.07] bg-[#141419]/90 p-5 text-sm lg:block">
        {editing && sd && draft ? (
          <TableEditor sheet={sheet!} draft={draft} declared={sd.tables.filter((x) => x.source === "listobject")} selection={sel}
            edited={edited} saving={saving} error={editError}
            onChange={(d) => { setDraft(d); setEditError(null); }} onSave={saveTables} onCancel={stopEditing} onReset={resetTables} />
        ) : focused && sd ? (
          <TableInspector table={focused} sheet={sheet!} onCell={selectCell} />
        ) : detail && sd ? (
          <CellInspector detail={detail} sheet={sd} onCell={(id) => (sheetOf(id) === sheet ? selectCell(id) : (setSheet(sheetOf(id)), selectCell(id)))}
            onTraceGraph={onTraceGraph} onAsk={onAsk} onResolve={onResolve} />
        ) : (
          <p className="text-zinc-500">{t.sheets.inspectorHint}</p>
        )}
      </aside>
    </div>
  );
}

function buildGrid(sd: SheetData, shownTables: TableInfo[]) {
  const byPos = new Map(sd.cells.map((c) => [`${c.row}:${c.col}`, c]));
  const issues = new Map<string, "high" | "medium" | "low">();
  for (const i of sd.issues) if (!issues.has(i.cell_id) || i.severity === "high") issues.set(i.cell_id, i.severity);
  const params = new Map(sd.params.map((p) => [p.cell_id, p]));
  const tables = new Map<string, Slot>();
  for (const tb of shownTables) {
    const top = tableTop(tb), bottom = tableBottom(tb);
    for (let r = top; r <= bottom; r++) for (let c = tb.col_start; c <= tb.col_end; c++) {
      const part: Part = r <= (tb.header_row ?? 0) ? "hdr" : r === tb.total_row ? "tot" : "body";
      tables.set(`${r}:${c}`, { t: tb, part, top: r === top, bottom: r === bottom, left: c === tb.col_start, right: c === tb.col_end });
    }
  }
  const extentR = Math.max(1, ...sd.cells.map((c) => c.row), ...shownTables.map(tableBottom));
  const extentC = Math.max(1, ...sd.cells.map((c) => c.col), ...shownTables.map((x) => x.col_end));
  return {
    byPos, issues, params, tables,
    maxR: Math.min(extentR, MAX_ROWS), maxC: Math.min(extentC, MAX_COLS),
    capped: extentR > MAX_ROWS || extentC > MAX_COLS,
  };
}

type CellProps = {
  r: number; c: number; editing: boolean; inSel: boolean;
  onDown: (r: number, c: number) => void; onOver: (r: number, c: number) => void;
  cell?: SheetCell; slot?: Slot; mode: Mode; showFormulas: boolean; severity?: "high" | "medium" | "low";
  param?: SheetData["params"][number]; selected: boolean; precedent: boolean; inGroup: boolean; focused: boolean;
  onClick?: () => void;
};

function GridCell({ r, c, editing, inSel, onDown, onOver, cell, slot, mode, showFormulas, severity, param, selected, precedent, inGroup, focused, onClick }: CellProps) {
  const cls: string[] = [];
  const shadows: string[] = [];
  const style: React.CSSProperties & Record<string, string> = {};
  if (slot) {
    style["--tc"] = tableColor(slot.t);
    const w = focused ? 3 : 2;
    if (slot.top) shadows.push(`inset 0 ${w}px 0 var(--tc)`);
    if (slot.bottom) shadows.push(`inset 0 -${w}px 0 var(--tc)`);
    if (slot.left) shadows.push(`inset ${w}px 0 0 var(--tc)`);
    if (slot.right) shadows.push(`inset -${w}px 0 0 var(--tc)`);
    if (mode === "structure") {
      cls.push(`s-${slot.part}`);
      if (slot.part === "body" && slot.t.key_col === cell?.col) cls.push("s-key");
    } else if (slot.part === "hdr") cls.push("s-key");
  }
  if (cell) {
    if (mode === "roles") cls.push(`r-${cell.role}`);
    else if (!slot) cls.push(param ? "r-input" : cell.role === "output" || cell.role === "label" || cell.role === "orphan" ? `r-${cell.role}` : "");
    if (typeof shown(cell) === "number") cls.push("num");
    if (severity === "high" || severity === "medium") shadows.push(`inset 0 0 0 2px ${SEV_RING[severity]}`);
  }
  if (selected) cls.push("sel");
  if (precedent) cls.push("prec");
  if (inGroup) cls.push("grp");
  if (shadows.length) style.boxShadow = shadows.join(",");
  if (inSel) cls.push("insel");
  const mouse = editing
    ? { onMouseDown: (e: React.MouseEvent) => { e.preventDefault(); onDown(r, c); }, onMouseEnter: () => onOver(r, c) }
    : {};
  if (!cell) return <td className={cls.join(" ")} style={style} {...mouse} />;

  const formulaShown = cell.vtype === "formula" && (showFormulas || cell.cached === null || cell.cached === undefined);
  if (formulaShown) cls.push("f");
  return (
    <td className={cls.join(" ")} style={style} data-id={cell.cell_id}
      data-t0={slot && slot.top && slot.left ? slot.t.table_id : undefined}
      title={cell.semantic || (param ? `params.${param.name}` : cell.a1)} onClick={editing ? undefined : onClick} {...mouse}>
      {formulaShown ? (showFormulas ? cell.formula : `ƒ ${cell.formula?.slice(1)}`) : fmt(shown(cell), cell.number_format)}
    </td>
  );
}

function Legend({ mode }: { mode: Mode }) {
  const { t } = useI18n();
  const l = t.sheets.legend;
  const items: [string, string][] = mode === "structure"
    ? [["s-hdr", l.header], ["s-body", l.body], ["s-tot", l.total], ["r-input", l.param], ["r-output", l.output], ["iss", l.issue], ["grp", l.group]]
    : [["r-input", l.input], ["r-derived", l.derived], ["r-output", l.output], ["r-orphan", l.orphan], ["iss", l.issue], ["grp", l.group], ["prec", l.prec]];
  return (
    <div className="sgrid-legend mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500" style={{ "--tc": TABLE_COLORS[0] } as React.CSSProperties}>
      {items.map(([k, label]) => <span key={k} className="flex items-center gap-1.5"><i className={k} />{label}</span>)}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-5">
      <h4 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-zinc-500">{title}</h4>
      {children}
    </div>
  );
}

function CellLink({ id, onCell, children }: { id: string; onCell: (id: string) => void; children?: React.ReactNode }) {
  return <button onClick={() => onCell(id)} className="font-mono text-[12.5px] text-violet-300 hover:text-violet-200 hover:underline">{children ?? id}</button>;
}

function whereText(t: ReturnType<typeof useI18n>["t"], c: SheetCell, sd: SheetData) {
  const w = t.sheets.where;
  const p = sd.params.find((x) => x.cell_id === c.cell_id);
  if (p) return w.param(p.name, p.unit, p.label);
  const tb = c.table_id != null ? sd.tables.find((x) => x.table_id === c.table_id) : undefined;
  if (!tb) return c.role === "output" ? w.output : c.role === "label" ? w.label : c.role === "orphan" ? w.orphan : w.outside;
  const f = tb.fields.find((x) => x.col === c.col);
  if (c.row <= (tb.header_row ?? 0)) return w.header(tb.name, f?.name ?? null);
  if (c.row === tb.total_row) return w.total(tb.name, f?.name ?? null);
  const key = tb.key_col ? sd.cells.find((x) => x.row === c.row && x.col === tb.key_col) : undefined;
  return w.body(tb.name, key ? `“${fmt(shown(key))}”` : String(c.row), f?.name ?? colName(c.col), f?.kind === "derived");
}

function CellInspector({ detail, sheet, onCell, onTraceGraph, onAsk, onResolve }: {
  detail: CellDetail; sheet: SheetData; onCell: (id: string) => void; onTraceGraph: (c: string) => void; onAsk: (c: string) => void;
  onResolve: (target: ResolveTarget) => void;
}) {
  const { t } = useI18n();
  const s = t.sheets;
  const c = detail.cell;
  const tb = c.table_id != null ? sheet.tables.find((x) => x.table_id === c.table_id) : undefined;
  const roleLabel = (t.charts.roles as Record<string, string>)[c.role] ?? c.role;
  return (
    <div>
      <div className="flex items-center gap-2">
        <h3 className="font-mono text-base font-semibold text-white">{c.cell_id}</h3>
        <span className="rounded-full bg-white/5 px-2 py-0.5 text-[11px] text-zinc-400 ring-1 ring-white/10">{roleLabel}</span>
      </div>
      <p className="mt-1 break-all font-mono text-[12.5px] text-violet-300">{c.semantic || "—"}</p>
      {c.vtype && (
        <p className="mt-3 rounded-xl bg-white/[0.04] px-3 py-2 text-[13px] text-zinc-200">
          {tb && <span className="mr-1.5 inline-block size-2 rounded-full align-middle" style={{ background: tableColor(tb) }} />}
          {whereText(t, c, sheet)}
        </p>
      )}
      <dl className="mt-4 grid grid-cols-[84px_1fr] gap-x-3 gap-y-2 text-[13px]">
        {c.vtype === "formula" ? (
          <>
            <dt className="text-zinc-500">{s.formula}</dt><dd className="break-all font-mono text-[12.5px] text-zinc-100">{c.formula}</dd>
            <dt className="text-zinc-500">{s.pattern}</dt>
            <dd className="break-all font-mono text-[12.5px] text-zinc-300">{c.r1c1} <span className="font-sans text-[11px] text-zinc-500">({s.patternHint})</span></dd>
            <dt className="text-zinc-500">{s.value}</dt>
            <dd className="text-zinc-200">{c.cached === null || c.cached === undefined ? <span className="text-zinc-500">{s.notCalculated}</span> : fmt(c.cached, c.number_format)}</dd>
          </>
        ) : c.vtype ? (
          <>
            <dt className="text-zinc-500">{s.value}</dt><dd className="text-zinc-200">{fmt(c.value, c.number_format)}</dd>
            <dt className="text-zinc-500">{s.type}</dt><dd className="text-zinc-300">{c.vtype}</dd>
          </>
        ) : null}
        {detail.group && (
          <>
            <dt className="text-zinc-500">{s.calculation}</dt>
            <dd className="text-zinc-300">
              {detail.group.n_cells > 1 ? (
                <>{s.sameAs(detail.group.n_cells - 1)}: <CellLink id={detail.group.anchor} onCell={onCell} /> → <CellLink id={detail.group.last} onCell={onCell} /></>
              ) : s.onlyHere}
              <span className="text-zinc-500"> (#{detail.group.group_id})</span>
            </dd>
          </>
        )}
      </dl>

      <div className="mt-4 flex flex-wrap gap-2">
        <button onClick={() => onTraceGraph(c.cell_id)} className="flex items-center gap-1.5 rounded-lg bg-white/5 px-2.5 py-1 text-xs text-zinc-300 ring-1 ring-white/10 hover:bg-white/10">
          <Network className="size-3.5" />{s.traceGraph}
        </button>
        <button onClick={() => onAsk(c.cell_id)} className="flex items-center gap-1.5 rounded-lg bg-white/5 px-2.5 py-1 text-xs text-zinc-300 ring-1 ring-white/10 hover:bg-white/10">
          <MessageSquareText className="size-3.5" />{s.askChat}
        </button>
      </div>

      {detail.issues.length > 0 && (
        <Section title={s.issues}>
          <div className="space-y-2">
            {detail.issues.map((i, n) => (
              <div key={n} className="rounded-r-lg border-l-2 bg-white/[0.03] px-3 py-2" style={{ borderColor: i.severity === "high" ? SEV_RING.high : i.severity === "medium" ? SEV_RING.medium : "#898781" }}>
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-medium text-zinc-100">{i.kind.replace(/_/g, " ")}</span>
                  <button onClick={() => onResolve({ cell: c.cell_id, kind: i.kind, detail: i.detail })}
                    className="ml-auto rounded-md bg-white/5 px-2 py-0.5 text-[11px] text-zinc-200 ring-1 ring-white/10 hover:bg-white/10">
                    {t.issues.resolve}
                  </button>
                </div>
                <div className="mt-0.5 text-xs text-zinc-400">{t.issueHelp[i.kind] ?? ""}</div>
                <div className="mt-1 break-all font-mono text-[11.5px] text-zinc-500">{i.detail}</div>
              </div>
            ))}
          </div>
        </Section>
      )}

      {detail.precedents.length > 0 && (
        <Section title={s.from(detail.precedents.length)}>
          <div className="space-y-0.5">
            {detail.precedents.slice(0, 150).map((p) => (
              <div key={p.cell_id} className="truncate text-[12.5px]" style={{ paddingLeft: (p.depth - 1) * 14 }}>
                <CellLink id={p.cell_id} onCell={onCell} /> <span className="text-zinc-500">{p.semantic}</span>
              </div>
            ))}
            {detail.precedents.length > 150 && <div className="text-xs text-zinc-500">{s.more(detail.precedents.length - 150)}</div>}
          </div>
        </Section>
      )}

      {detail.dependents.length > 0 && (
        <Section title={s.usedBy(detail.dependents.length)}>
          <div className="space-y-0.5">
            {detail.dependents.slice(0, 80).map((p) => (
              <div key={p.cell_id} className="truncate text-[12.5px]"><CellLink id={p.cell_id} onCell={onCell} /> <span className="text-zinc-500">{p.semantic}</span></div>
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

function TableInspector({ table, sheet, onCell }: { table: TableInfo; sheet: string; onCell: (id: string) => void }) {
  const { t } = useI18n();
  const s = t.sheets.table;
  return (
    <div>
      <div className="flex items-center gap-2">
        <Table2 className="size-4" style={{ color: tableColor(table) }} />
        <h3 className="text-base font-semibold text-white">{table.name}</h3>
      </div>
      <p className="mt-1 font-mono text-[12.5px] text-violet-300">{sheet}!{rangeA1(table)}</p>
      <p className="mt-3 rounded-xl bg-white/[0.04] px-3 py-2 text-[13px] text-zinc-200">
        {table.source === "listobject" ? s.listobject : table.source === "user" ? s.user : s.detected(table.confidence)}
        {table.kind === "parameter_list" ? ` ${s.paramList}` : ""}
      </p>
      <dl className="mt-4 grid grid-cols-[84px_1fr] gap-x-3 gap-y-2 text-[13px]">
        <dt className="text-zinc-500">{s.header}</dt>
        <dd className="text-zinc-200">{table.header_row ? s.rowsRange(table.header_top || table.header_row, table.header_row) : s.none}</dd>
        <dt className="text-zinc-500">{s.body}</dt><dd className="text-zinc-200">{s.rowsRange(table.row_start, table.row_end)}</dd>
        <dt className="text-zinc-500">{s.total}</dt>
        <dd className="text-zinc-200">{table.total_row ? <CellLink id={cellIdFor(sheet, table.total_row, table.col_start)} onCell={onCell}>{s.rowsRange(table.total_row, table.total_row)}</CellLink> : s.none}</dd>
        <dt className="text-zinc-500">{s.key}</dt>
        <dd className="text-zinc-200">{table.key_col ? s.keyCol(colName(table.key_col), table.name) : s.byPosition}</dd>
      </dl>
      <Section title={s.fields(table.fields.length)}>
        <table className="w-full text-[12.5px]">
          <thead>
            <tr className="text-left text-zinc-500">{s.colHead.map((h) => <th key={h} className="pb-1.5 font-medium">{h}</th>)}</tr>
          </thead>
          <tbody>
            {table.fields.map((f) => (
              <tr key={f.col} className="border-t border-white/5">
                <td className="py-1.5 text-zinc-500">{colName(f.col)}</td>
                <td className="py-1.5 font-mono text-zinc-100">{f.name}{f.unit && <span className="font-sans text-zinc-500"> ({f.unit})</span>}</td>
                <td className="py-1.5 text-zinc-300">{f.header}</td>
                <td className="py-1.5 text-zinc-400">{f.kind === "derived" ? s.calc : s.data}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </div>
  );
}
