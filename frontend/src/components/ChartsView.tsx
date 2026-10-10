"use client";

import { ChartColumn, ChartPie, Info, Loader2, Table2, TriangleAlert } from "lucide-react";
import { motion } from "motion/react";
import { useEffect, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Legend, Pie, PieChart, Rectangle, ResponsiveContainer, Tooltip, XAxis, YAxis,
  type TooltipContentProps,
} from "recharts";
import { api, type ChartData, type ChartField } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

// Dataviz reference palette, dark steps; validated (all checks pass) against the card surface #141419.
const CAT = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
// Status colors are reserved for severity and always shown with an icon + label.
const SEV = { high: "#d03b3b", medium: "#fab219", low: "#898781" } as const;
const INK = { primary: "#ffffff", secondary: "#c3c2b7", muted: "#898781", grid: "#2c2c2a", surface: "#141419" };

// Color follows the entity: each role keeps its slot whatever the counts are.
const ROLE_ORDER = ["input", "derived", "output", "label", "orphan"] as const;
const MAX_SLICES = 6;
const MAX_BARS = 20;

type Datum = { name: string; value: number; fill: string };

function fmt(v: number, percent = false) {
  if (percent) return `${(v * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function ChartTooltip({ active, payload, percent, unit }: TooltipContentProps<number, string> & { percent?: boolean; unit?: string | null }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-xl border border-white/10 bg-zinc-950/95 px-3 py-2 text-xs shadow-2xl backdrop-blur">
      {payload.map((p) => {
        const d = p.payload as Datum & Record<string, unknown>;
        return (
          <div key={String(p.dataKey) + p.name} className="flex items-center gap-2">
            <span className="size-2.5 rounded-sm" style={{ background: (p.color as string) ?? d.fill }} />
            <span className="text-zinc-300">{d.name ?? p.name}{payload.length > 1 ? ` · ${p.name}` : ""}</span>
            <span className="ml-auto pl-3 font-medium tabular-nums text-white">
              {fmt(Number(p.value), percent)}{unit ? ` ${unit}` : ""}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function Card({ title, subtitle, children, className = "" }: { title: string; subtitle?: string; children: React.ReactNode; className?: string }) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className={`rounded-3xl border border-white/[0.07] bg-[#141419]/90 p-5 shadow-xl backdrop-blur ${className}`}
    >
      <h3 className="text-sm font-medium text-white">{title}</h3>
      {subtitle && <p className="mt-0.5 text-xs text-zinc-500">{subtitle}</p>}
      <div className="mt-4">{children}</div>
    </motion.section>
  );
}

/** Donut with the total in the middle and a legend that carries labels, counts and shares. */
function Donut({ data, center, percent, unit, icon }: { data: Datum[]; center: string; percent?: boolean; unit?: string | null; icon?: (d: Datum) => React.ReactNode }) {
  const total = data.reduce((s, d) => s + d.value, 0);
  return (
    <div className="flex flex-col items-center gap-5 sm:flex-row">
      <div className="relative size-44 shrink-0">
        <ResponsiveContainer>
          <PieChart>
            <Pie data={data} dataKey="value" nameKey="name" innerRadius="62%" outerRadius="100%" paddingAngle={data.length > 1 ? 2 : 0}
              cornerRadius={4} stroke={INK.surface} strokeWidth={2} isAnimationActive animationDuration={700} />
            <Tooltip content={(p) => <ChartTooltip {...(p as TooltipContentProps<number, string>)} percent={percent} unit={unit} />} />
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 grid place-items-center text-center">
          <div>
            <div className="text-2xl font-semibold text-white">{fmt(total, percent)}</div>
            <div className="text-[11px] text-zinc-500">{center}</div>
          </div>
        </div>
      </div>
      <ul className="w-full max-w-sm space-y-2 text-sm">
        {data.map((d) => (
          <li key={d.name} className="flex items-center gap-2.5">
            {icon ? icon(d) : <span className="size-2.5 shrink-0 rounded-sm" style={{ background: d.fill }} />}
            <span className="truncate text-zinc-300">{d.name}</span>
            <span className="ml-auto tabular-nums text-white">{fmt(d.value, percent)}</span>
            <span className="w-12 text-right tabular-nums text-xs text-zinc-500">{total ? Math.round((d.value / total) * 100) : 0}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function HBar({ data, percent, unit, height, counts }: { data: Datum[]; percent?: boolean; unit?: string | null; height?: number; counts?: boolean }) {
  // Room for the longest category label (CJK glyphs are about twice as wide as Latin ones).
  const labelWidth = Math.min(220, Math.max(70, ...data.map((d) => [...d.name].reduce((w, ch) => w + (ch.charCodeAt(0) > 0x2e80 ? 12.5 : 7), 0) + 12)));
  return (
    <ResponsiveContainer width="100%" height={height ?? Math.max(120, data.length * 30 + 30)}>
      <BarChart data={data} layout="vertical" margin={{ left: 8, right: 48, top: 0, bottom: 0 }} barCategoryGap={4}>
        <CartesianGrid horizontal={false} stroke={INK.grid} />
        <XAxis type="number" tick={{ fill: INK.muted, fontSize: 11 }} axisLine={false} tickLine={false}
          allowDecimals={!counts} tickFormatter={(v) => fmt(v, percent)} />
        <YAxis type="category" dataKey="name" width={labelWidth} tick={{ fill: INK.secondary, fontSize: 12 }} axisLine={{ stroke: "#383835" }} tickLine={false} />
        <Tooltip cursor={{ fill: "rgba(255,255,255,0.04)" }} content={(p) => <ChartTooltip {...(p as TooltipContentProps<number, string>)} percent={percent} unit={unit} />} />
        <Bar dataKey="value" radius={[0, 4, 4, 0]} maxBarSize={22} isAnimationActive animationDuration={600}
          label={{ position: "right", fill: INK.secondary, fontSize: 11, formatter: (v: unknown) => fmt(Number(v), percent) }}
          shape={(props: React.ComponentProps<typeof Rectangle> & { payload?: Datum }) => <Rectangle {...props} fill={props.payload?.fill} />} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function Segmented<T extends string>({ value, options, onChange }: { value: T; options: { value: T; label: string; icon?: typeof ChartPie }[]; onChange: (v: T) => void }) {
  return (
    <div className="flex flex-wrap rounded-xl border border-white/10 bg-white/[0.03] p-0.5">
      {options.map(({ value: v, label, icon: Icon }) => (
        <button key={v} onClick={() => onChange(v)}
          className={`flex items-center gap-1.5 rounded-[10px] px-2.5 py-1 text-xs transition ${v === value ? "bg-white/10 text-white" : "text-zinc-400 hover:text-white"}`}>
          {Icon && <Icon className="size-3.5" />}{label}
        </button>
      ))}
    </div>
  );
}

function fieldLabel(f: ChartField) {
  return f.unit && !f.header.includes(f.unit) ? `${f.header} (${f.unit})` : f.header;
}

function TableChart({ table }: { table: ChartData["tables"][number] }) {
  const { t } = useI18n();
  const numeric = table.fields.filter((f) => f.numeric);
  const groupable = table.fields.filter((f) => !f.numeric && f.distinct > 1 && f.distinct <= 12 && f.distinct < table.rows.length);
  const [valueName, setValueName] = useState(numeric[0]?.name ?? "");
  const [groupName, setGroupName] = useState<string>(groupable[0]?.name ?? "");
  const [form, setForm] = useState<"pie" | "bar">("pie");
  const [showTable, setShowTable] = useState(false);
  const field = numeric.find((f) => f.name === valueName) ?? numeric[0];

  const data: Datum[] = (() => {
    if (!field) return [];
    const val = (r: (typeof table.rows)[number]) => (typeof r.values[field.name] === "number" ? (r.values[field.name] as number) : null);
    if (groupName) {
      const sums = new Map<string, number>();
      for (const r of table.rows) {
        const v = val(r);
        if (v === null) continue;
        const g = String(r.values[groupName] ?? t.charts.blank);
        sums.set(g, (sums.get(g) ?? 0) + v);
      }
      // Slot order follows first appearance in the sheet, so a group keeps its color across value fields.
      const order = [...new Set(table.rows.map((r) => String(r.values[groupName] ?? t.charts.blank)))];
      let items = order.filter((g) => sums.has(g)).map((g, i) => ({ name: g, value: sums.get(g)!, fill: CAT[i % CAT.length] }));
      if (items.length > MAX_SLICES) {
        const sorted = [...items].sort((a, b) => b.value - a.value);
        const keep = new Set(sorted.slice(0, MAX_SLICES - 1).map((d) => d.name));
        const rest = items.filter((d) => !keep.has(d.name)).reduce((s, d) => s + d.value, 0);
        items = [...items.filter((d) => keep.has(d.name)), { name: t.charts.other, value: rest, fill: INK.muted }];
      }
      return items;
    }
    return table.rows.flatMap((r) => { const v = val(r); return v === null ? [] : [{ name: r.key, value: v, fill: CAT[0] }]; })
      .sort((a, b) => b.value - a.value).slice(0, MAX_BARS);
  })();

  if (!numeric.length) return <p className="text-sm text-zinc-500">{t.charts.noNumeric}</p>;
  const empty = data.length === 0;
  const negative = data.some((d) => d.value < 0);
  const effectiveForm = groupName && !negative && !field.percent ? form : "bar";
  const rowsTotal = table.rows.length;

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-3 text-xs text-zinc-500">
        <label className="flex items-center gap-2">{t.charts.value}
          <select value={field.name} onChange={(e) => setValueName(e.target.value)}
            className="rounded-lg border border-white/10 bg-zinc-900 px-2 py-1 text-sm text-zinc-100 outline-none focus:border-violet-400/50">
            {numeric.map((f) => <option key={f.name} value={f.name}>{fieldLabel(f)}{f.missing === rowsTotal ? t.charts.noValue : ""}</option>)}
          </select>
        </label>
        <label className="flex items-center gap-2">{t.charts.group}
          <select value={groupName} onChange={(e) => setGroupName(e.target.value)}
            className="rounded-lg border border-white/10 bg-zinc-900 px-2 py-1 text-sm text-zinc-100 outline-none focus:border-violet-400/50">
            <option value="">{t.charts.noGroup}</option>
            {groupable.map((f) => <option key={f.name} value={f.name}>{t.charts.groupBy(f.header)}</option>)}
          </select>
        </label>
        <div className="ml-auto flex items-center gap-2">
          {groupName && !negative && !field.percent && (
            <Segmented value={form} onChange={setForm} options={[{ value: "pie", label: t.charts.pie, icon: ChartPie }, { value: "bar", label: t.charts.bar, icon: ChartColumn }]} />
          )}
          <button onClick={() => setShowTable((s) => !s)}
            className={`flex items-center gap-1.5 rounded-xl border px-2.5 py-1.5 transition ${showTable ? "border-violet-400/40 bg-violet-500/10 text-violet-200" : "border-white/10 text-zinc-400 hover:text-white"}`}>
            <Table2 className="size-3.5" />{t.charts.dataTable}
          </button>
        </div>
      </div>

      <div className="mt-5">
        {empty ? (
          <div className="flex items-start gap-3 rounded-2xl border border-amber-400/20 bg-amber-500/[0.07] p-4 text-sm text-amber-100">
            <Info className="mt-0.5 size-4 shrink-0 text-amber-300" />
            <div>
              <p>{t.charts.missingTitle(<b>{field.header}</b>)}</p>
              <p className="mt-1 text-amber-100/70">{t.charts.missingBody}</p>
            </div>
          </div>
        ) : effectiveForm === "pie" ? (
          <Donut data={data} center={t.charts.total(fieldLabel(field))} unit={field.unit} />
        ) : (
          <HBar data={data} percent={field.percent} unit={field.unit} />
        )}
        {!empty && (
          <p className="mt-3 text-[11px] text-zinc-600">
            {groupName ? t.charts.noteGroup(table.fields.find((f) => f.name === groupName)?.header ?? groupName, fieldLabel(field)) : t.charts.noteRows(fieldLabel(field))}
            {!groupName && rowsTotal > MAX_BARS ? t.charts.noteTop(MAX_BARS) : ""}
            {field.missing > 0 && field.missing < rowsTotal ? t.charts.noteMissing(field.missing) : ""}
            {groupName && field.percent ? t.charts.notePercent : ""}
          </p>
        )}
      </div>

      {showTable && !empty && (
        <div className="mt-4 max-h-72 overflow-auto rounded-xl border border-white/[0.07]">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-zinc-900 text-zinc-400"><tr><th className="px-3 py-2 text-left font-medium">{groupName ? t.charts.colGroup : t.charts.colRow}</th><th className="px-3 py-2 text-right font-medium">{fieldLabel(field)}</th></tr></thead>
            <tbody>
              {data.map((d) => (
                <tr key={d.name} className="border-t border-white/5"><td className="px-3 py-1.5 text-zinc-300">{d.name}</td><td className="px-3 py-1.5 text-right tabular-nums text-white">{fmt(d.value, field.percent)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function ChartsView({ workbookId, workbookName }: { workbookId: string | null; workbookName?: string }) {
  const [result, setResult] = useState<{ id: string; data?: ChartData; error?: string } | null>(null);
  const [tableId, setTableId] = useState<number | null>(null);
  const { t } = useI18n();

  useEffect(() => {
    if (!workbookId) return;
    let live = true;
    api.charts(workbookId)
      .then((data) => live && setResult({ id: workbookId, data }))
      .catch((e) => live && setResult({ id: workbookId, error: e.message }));
    return () => { live = false; };
  }, [workbookId]);

  const current = result?.id === workbookId ? result : null;
  const data = current?.data;

  if (!workbookId) return <div className="grid flex-1 place-items-center text-sm text-zinc-500">{t.graph.empty}</div>;
  if (current?.error) return <div className="grid flex-1 place-items-center text-sm text-rose-200">{current.error}</div>;
  if (!data) return <div className="grid flex-1 place-items-center"><Loader2 className="size-8 animate-spin text-violet-300" /></div>;

  const roles: Datum[] = ROLE_ORDER.flatMap((r, i) => {
    const n = data.roles.find((x) => x.role === r)?.n;
    return n ? [{ name: t.charts.roles[r], value: n, fill: CAT[i] }] : [];
  });
  const severity: (Datum & { sev: keyof typeof SEV })[] = (["high", "medium", "low"] as const).flatMap((s) => {
    const n = data.severity.find((x) => x.severity === s)?.n;
    return n ? [{ name: t.charts.sev[s], value: n, fill: SEV[s], sev: s }] : [];
  });
  const kinds: Datum[] = data.kinds.map((k) => ({ name: k.kind, value: k.n, fill: SEV[k.severity] }));
  const totalIssues = severity.reduce((s, d) => s + d.value, 0);
  const formulas = data.sheets.reduce((s, x) => s + x.formulas, 0);
  const cells = data.sheets.reduce((s, x) => s + x.cells, 0);
  const tables = data.tables.filter((t) => t.kind === "data" && t.fields.some((f) => f.numeric));
  // Default to the biggest table: it is usually the one the workbook is about.
  const table = tables.find((t) => t.table_id === tableId)
    ?? tables.reduce<(typeof tables)[number] | undefined>((a, t) => (!a || t.rows.length > a.rows.length ? t : a), undefined);

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-6xl px-6 pb-16 pt-6">
        <h2 className="text-xl font-semibold tracking-tight">{t.charts.title}</h2>
        <p className="mt-0.5 text-sm text-zinc-500">{workbookName}</p>

        {/* KPI row */}
        <div className="mt-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
          {[
            [t.charts.kpi.cells, cells, ""],
            [t.charts.kpi.formulas, formulas, cells ? t.charts.share(Math.round((formulas / cells) * 100)) : ""],
            [t.charts.kpi.sheets, data.sheets.length, ""],
            [t.charts.kpi.issues, totalIssues, severity.find((s) => s.sev === "high") ? t.charts.highCount(severity.find((s) => s.sev === "high")!.value) : t.charts.noHigh],
          ].map(([label, value, note]) => (
            <div key={label as string} className="rounded-2xl border border-white/[0.07] bg-[#141419]/90 px-5 py-4">
              <div className="text-xs text-zinc-500">{label}</div>
              <div className="mt-1 text-3xl font-semibold text-white">{(value as number).toLocaleString(t.locale)}</div>
              {note && <div className="mt-0.5 text-xs text-zinc-500">{note}</div>}
            </div>
          ))}
        </div>

        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <Card title={t.charts.rolesTitle} subtitle={t.charts.rolesSub}>
            <Donut data={roles} center={t.charts.cellsCenter} />
          </Card>
          <Card title={t.charts.sevTitle} subtitle={t.charts.sevSub}>
            {totalIssues ? (
              <Donut data={severity} center={t.charts.issuesCenter}
                icon={(d) => <TriangleAlert className="size-3.5 shrink-0" style={{ color: d.fill }} />} />
            ) : <p className="py-10 text-center text-sm text-zinc-500">{t.charts.noIssues}</p>}
          </Card>
          <Card title={t.charts.kindsTitle} subtitle={t.charts.kindsSub}>
            {kinds.length ? <HBar data={kinds} counts /> : <p className="py-6 text-center text-sm text-zinc-500">{t.charts.none}</p>}
          </Card>
          <Card title={t.charts.sheetsTitle} subtitle={t.charts.sheetsSub}>
            <ResponsiveContainer width="100%" height={Math.max(160, data.sheets.length * 52 + 40)}>
              <BarChart data={data.sheets} layout="vertical" margin={{ left: 8, right: 40 }} barGap={2} barCategoryGap={10}>
                <CartesianGrid horizontal={false} stroke={INK.grid} />
                <XAxis type="number" tick={{ fill: INK.muted, fontSize: 11 }} axisLine={false} tickLine={false} allowDecimals={false} />
                <YAxis type="category" dataKey="sheet" width={90} tick={{ fill: INK.secondary, fontSize: 12 }} axisLine={{ stroke: "#383835" }} tickLine={false} />
                <Tooltip cursor={{ fill: "rgba(255,255,255,0.04)" }} content={(p) => <ChartTooltip {...(p as TooltipContentProps<number, string>)} />} />
                <Legend iconType="square" iconSize={10} wrapperStyle={{ fontSize: 12, color: INK.secondary }} />
                <Bar dataKey="cells" name={t.charts.kpi.cells} fill={CAT[0]} radius={[0, 4, 4, 0]} maxBarSize={16}
                  label={{ position: "right", fill: INK.secondary, fontSize: 11 }} />
                <Bar dataKey="formulas" name={t.charts.kpi.formulas} fill={CAT[1]} radius={[0, 4, 4, 0]} maxBarSize={16}
                  label={{ position: "right", fill: INK.secondary, fontSize: 11 }} />
              </BarChart>
            </ResponsiveContainer>
          </Card>
        </div>

        {/* Table data */}
        <div className="mt-10 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold tracking-tight">{t.charts.tableData}</h2>
            <p className="mt-0.5 text-sm text-zinc-500">{t.charts.tableDataSub}</p>
          </div>
          {tables.length > 1 && (
            <Segmented value={String(table?.table_id)} onChange={(v) => setTableId(Number(v))}
              options={tables.map((t) => ({ value: String(t.table_id), label: `${t.name}（${t.sheet}）` }))} />
          )}
        </div>
        {table ? (
          <Card title={`${table.name}`} subtitle={t.charts.tableMeta(table.sheet, table.rows.length, table.fields.length)} className="mt-4">
            <TableChart key={table.table_id} table={table} />
          </Card>
        ) : (
          <p className="mt-4 text-sm text-zinc-500">{t.charts.noTables}</p>
        )}
      </div>
    </div>
  );
}
