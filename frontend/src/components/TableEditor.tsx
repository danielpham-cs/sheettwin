"use client";

import { Loader2, Plus, RotateCcw, Save, Trash2, X } from "lucide-react";
import { ApiError, colName, type TableInfo, type TableSpec } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export type Box = { r1: number; c1: number; r2: number; c2: number };

const COL = (s: string) => [...s].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);

/** "B3:F20" -> its box, or null if it is not a two-corner range. */
export function parseRange(text: string): Box | null {
  const m = text.replace(/\$/g, "").trim().toUpperCase().match(/^([A-Z]{1,3})(\d+):([A-Z]{1,3})(\d+)$/);
  if (!m) return null;
  const [a, b, c, d] = [COL(m[1]), +m[2], COL(m[3]), +m[4]];
  return { r1: Math.min(b, d), c1: Math.min(a, c), r2: Math.max(b, d), c2: Math.max(a, c) };
}

export const boxRange = (b: Box) => `${colName(b.c1)}${b.r1}:${colName(b.c2)}${b.r2}`;

/** A parsed table as an editable spec. */
export function toSpec(t: TableInfo): TableSpec {
  const top = t.header_top || t.header_row || t.row_start;
  const bottom = t.total_row || t.row_end;
  return {
    range: `${colName(t.col_start)}${top}:${colName(t.col_end)}${bottom}`,
    header_rows: t.header_row ? t.header_row - top + 1 : 0,
    total: !!t.total_row,
    ...(t.source === "user" && t.name ? { name: t.name } : {}),
  };
}

/** A draft spec drawn like a parsed table (no fields yet), so the grid can preview it. */
export function specToInfo(spec: TableSpec, id: number, sheet: string): TableInfo | null {
  const b = parseRange(spec.range);
  if (!b) return null;
  const n = Math.max(0, spec.header_rows);
  return {
    table_id: id, sheet, name: spec.name || `#${id}`, kind: "data", source: "user",
    header_row: n ? b.r1 + n - 1 : null, header_top: n > 1 ? b.r1 : null,
    row_start: b.r1 + n, row_end: spec.total ? b.r2 - 1 : b.r2, total_row: spec.total ? b.r2 : null,
    col_start: b.c1, col_end: b.c2, key_col: null, confidence: 1, fields: [],
  };
}

/** The first problem with a draft, checked as the backend will check it. */
export function draftError(t: ReturnType<typeof useI18n>["t"], draft: TableSpec[]): string | null {
  const e = t.sheets.edit.errors;
  const boxes: Box[] = [];
  for (const [i, s] of draft.entries()) {
    const b = parseRange(s.range);
    if (!b) return e.bad_range(s.range);
    if (b.r2 - b.r1 + 1 - s.header_rows - (s.total ? 1 : 0) < 1) return e.too_small(s.range);
    const j = boxes.findIndex((o) => b.c1 <= o.c2 && o.c1 <= b.c2 && b.r1 <= o.r2 && o.r1 <= b.r2);
    if (j >= 0) return e.overlap(s.range, draft[j].range);
    boxes[i] = b;
  }
  return null;
}

export function apiErrorText(t: ReturnType<typeof useI18n>["t"], err: unknown, draft: TableSpec[]): string {
  const e = t.sheets.edit.errors;
  if (err instanceof ApiError && err.info) {
    const i = Number(err.info.index ?? 0), o = Number(err.info.other ?? 0);
    if (err.kind === "bad_range") return e.bad_range(draft[i]?.range ?? "");
    if (err.kind === "too_small") return e.too_small(draft[i]?.range ?? "");
    if (err.kind === "overlap") return e.overlap(draft[i]?.range ?? "", draft[o]?.range ?? "");
  }
  return (err as Error).message;
}

type Props = {
  sheet: string;
  draft: TableSpec[];
  declared: TableInfo[]; // Excel Tables on the sheet: shown, not editable
  selection: Box | null;
  edited: boolean; // the sheet already uses the user's tables
  saving: boolean;
  error: string | null;
  onChange: (draft: TableSpec[]) => void;
  onSave: () => void;
  onCancel: () => void;
  onReset: () => void;
};

export function TableEditor({ sheet, draft, declared, selection, edited, saving, error, onChange, onSave, onCancel, onReset }: Props) {
  const { t } = useI18n();
  const s = t.sheets.edit;
  const set = (i: number, patch: Partial<TableSpec>) => onChange(draft.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  const problem = error ?? draftError(t, draft);
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-base font-semibold text-white">{s.title(sheet)}</h3>
        <p className="mt-1 text-xs leading-relaxed text-zinc-400">{s.how}</p>
      </div>

      <div className="flex items-center gap-2 rounded-xl bg-white/[0.04] px-3 py-2 text-xs">
        <span className="font-mono text-zinc-300">{selection ? s.selection(boxRange(selection)) : s.noSelection}</span>
        <button disabled={!selection} onClick={() => selection && onChange([...draft, { range: boxRange(selection), header_rows: 1, total: false }])}
          className="ml-auto flex items-center gap-1 rounded-lg bg-violet-500/20 px-2 py-1 text-violet-100 ring-1 ring-violet-400/30 hover:bg-violet-500/30 disabled:opacity-40">
          <Plus className="size-3.5" />{s.add}
        </button>
      </div>

      {declared.map((d) => (
        <p key={d.table_id} className="rounded-xl bg-white/[0.03] px-3 py-2 text-xs text-zinc-500">{s.declared(d.name, `${colName(d.col_start)}${d.header_row ?? d.row_start}:${colName(d.col_end)}${d.total_row ?? d.row_end}`)}</p>
      ))}

      {draft.length === 0 && <p className="text-xs text-zinc-500">{s.empty}</p>}
      <ol className="space-y-3">
        {draft.map((d, i) => (
          <li key={i} className="rounded-xl border border-white/10 p-3 text-xs">
            <div className="flex items-center gap-2">
              <span className="font-mono text-zinc-500">#{i + 1}</span>
              <label className="flex flex-1 items-center gap-1.5">
                <span className="text-zinc-500">{s.range}</span>
                <input id={`tbl-range-${i}`} value={d.range} onChange={(e) => set(i, { range: e.target.value.toUpperCase() })}
                  className="w-28 rounded-md border border-white/10 bg-zinc-900 px-2 py-1 font-mono text-zinc-100 outline-none focus:border-violet-400/60" />
              </label>
              <button disabled={!selection} onClick={() => selection && set(i, { range: boxRange(selection) })} title={s.useSelection}
                className="rounded-md px-1.5 py-1 text-zinc-400 hover:bg-white/10 hover:text-white disabled:opacity-30">{s.useSelection}</button>
              <button onClick={() => onChange(draft.filter((_, j) => j !== i))} title={s.remove} aria-label={s.remove}
                className="rounded-md p-1 text-zinc-500 hover:bg-white/10 hover:text-rose-300"><Trash2 className="size-3.5" /></button>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2">
              <label className="flex items-center gap-1.5">
                <span className="text-zinc-500">{s.headerRows}</span>
                <input id={`tbl-header-${i}`} type="number" min={0} max={10} value={d.header_rows}
                  onChange={(e) => set(i, { header_rows: Math.max(0, Math.min(10, Number(e.target.value) || 0)) })}
                  className="w-14 rounded-md border border-white/10 bg-zinc-900 px-2 py-1 text-zinc-100 outline-none focus:border-violet-400/60" />
              </label>
              <label className="flex items-center gap-1.5 text-zinc-300">
                <input id={`tbl-total-${i}`} type="checkbox" checked={d.total} onChange={(e) => set(i, { total: e.target.checked })} className="accent-violet-500" />
                {s.total}
              </label>
              <label className="flex min-w-0 flex-1 items-center gap-1.5">
                <span className="shrink-0 text-zinc-500">{s.name}</span>
                <input id={`tbl-name-${i}`} value={d.name ?? ""} onChange={(e) => set(i, { name: e.target.value || undefined })}
                  className="min-w-0 flex-1 rounded-md border border-white/10 bg-zinc-900 px-2 py-1 text-zinc-100 outline-none focus:border-violet-400/60" />
              </label>
            </div>
          </li>
        ))}
      </ol>

      {problem && <p className="rounded-xl bg-rose-500/10 px-3 py-2 text-xs text-rose-200">{problem}</p>}

      <div className="flex flex-wrap gap-2">
        <button disabled={saving || !!draftError(t, draft)} onClick={onSave}
          className="flex items-center gap-1.5 rounded-lg bg-gradient-to-br from-violet-500 to-cyan-400 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">
          {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}{saving ? s.saving : s.save}
        </button>
        <button disabled={saving} onClick={onCancel} className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs text-zinc-300 hover:bg-white/5">
          <X className="size-3.5" />{s.cancel}
        </button>
        {edited && (
          <button disabled={saving} onClick={onReset} className="ml-auto flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs text-zinc-400 hover:bg-white/5 hover:text-white">
            <RotateCcw className="size-3.5" />{s.reset}
          </button>
        )}
      </div>
    </div>
  );
}
