"use client";

import { Database, Download, Loader2, Play } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SqlEditor, type SqlEditorHandle } from "@/components/SqlEditor";
import { api, type SqlResult } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

// Example queries over the SheetIR schema (sheetir/store.py). Each works on any workbook and starts with
// SELECT or WITH, the only statements the backend accepts. "trace": every cell the first output depends on.
const PRESETS: [string, string][] = [
  ["semantic", "SELECT semantic, cell_id, role, value, formula\nFROM cells\nWHERE semantic IS NOT NULL\nORDER BY sheet, row, col\nLIMIT 200"],
  ["fields", "SELECT t.name AS table_name, f.name AS field, f.header, f.unit, f.kind, f.r1c1\nFROM fields f JOIN tables t USING (table_id)\nORDER BY t.table_id, f.col"],
  ["constants", "SELECT value, COUNT(*) AS n_cells, MIN(cell_id) AS example\nFROM literals\nWHERE value NOT IN (0, 1, 100)\n  AND (function IS NULL OR function NOT IN ('VLOOKUP', 'INDEX', 'MATCH', 'ROUND'))\nGROUP BY value\nORDER BY n_cells DESC"],
  ["high", "SELECT cell_id, kind, detail\nFROM issues\nWHERE severity = 'high'"],
  ["params", "SELECT p.name, p.cell_id, COUNT(DISTINCT d.dst_cell) AS direct_dependents\nFROM params p JOIN deps d ON d.src_cell = p.cell_id\nGROUP BY p.name\nORDER BY direct_dependents DESC"],
  ["trace", "WITH RECURSIVE trace(cell_id, depth) AS (\n  SELECT (SELECT cell_id FROM cells WHERE role = 'output' ORDER BY sheet, row, col LIMIT 1), 0\n  UNION\n  SELECT d.src_cell, t.depth + 1 FROM deps d JOIN trace t ON d.dst_cell = t.cell_id\n)\nSELECT t.cell_id, MIN(depth) AS depth, c.semantic, c.formula\nFROM trace t LEFT JOIN cells c USING (cell_id)\nGROUP BY t.cell_id\nORDER BY depth, t.cell_id"],
  ["schema", "SELECT type, name, sql\nFROM sqlite_master\nWHERE type IN ('table', 'view')\nORDER BY type, name"],
];

// A value that looks like a cell id (Quote!J13, 'My Sheet'!A1) can be opened in the sheet structure view.
const CELL_RE = /^(?:'[^']+'|[A-Za-z_][\w.]*)![A-Z]{1,3}\d+$/;

type Schema = { name: string; type: string; columns: string[] }[];

/** Column names from a CREATE TABLE statement (top-level comma split of the column list). */
function columnsOf(sql: string | null): string[] {
  if (!sql || !/^\s*CREATE\s+TABLE/i.test(sql)) return [];
  const body = sql.slice(sql.indexOf("(") + 1, sql.lastIndexOf(")"));
  const out: string[] = [];
  let depth = 0, part = "";
  for (const ch of body) {
    if (ch === "(") depth++;
    if (ch === ")") depth--;
    if (ch === "," && depth === 0) { out.push(part); part = ""; } else part += ch;
  }
  out.push(part);
  return out.map((p) => p.replace(/--.*$/gm, "").trim().split(/\s+/)[0]).filter(Boolean);
}

function toCsv(r: SqlResult) {
  const q = (v: unknown) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [r.columns.map(q).join(","), ...r.rows.map((row) => row.map(q).join(","))].join("\n");
}

type Props = { workbookId: string | null; workbookName?: string; onOpenCell: (cell: string) => void };

export function QueryView({ workbookId, workbookName, onOpenCell }: Props) {
  const { t } = useI18n();
  const [query, setQuery] = useState(PRESETS[0][1]);
  const [result, setResult] = useState<SqlResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [schema, setSchema] = useState<Schema>([]);
  const [lintError, setLintError] = useState<string | null>(null);
  const editor = useRef<SqlEditorHandle>(null);

  async function run(q = query) {
    if (!workbookId) return;
    setRunning(true);
    try {
      setResult(await api.sql(workbookId, q));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setResult(null);
    } finally {
      setRunning(false);
    }
  }

  // On mount (App remounts this view per workbook): load the schema and run the first example.
  useEffect(() => {
    if (!workbookId) return;
    let live = true;
    api.sql(workbookId, PRESETS.find(([k]) => k === "schema")![1]).then((r) => {
      if (!live) return;
      setSchema(r.rows.map(([type, name, sql]) => ({ type: String(type), name: String(name), columns: columnsOf(sql as string | null) })));
    }, () => {});
    api.sql(workbookId, PRESETS[0][1]).then((r) => live && setResult(r), (e) => live && setError((e as Error).message));
    return () => { live = false; };
  }, [workbookId]);

  const insert = (text: string) => editor.current?.insert(text);
  const completionSchema = useMemo(() => Object.fromEntries(schema.map((s) => [s.name, s.columns])), [schema]);
  const check = useCallback(
    async (q: string) => (workbookId ? (await api.sqlCheck(workbookId, q)).error : null),
    [workbookId],
  );

  function downloadCsv() {
    if (!result) return;
    const url = URL.createObjectURL(new Blob(["﻿" + toCsv(result)], { type: "text/csv" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `${(workbookName ?? "query").replace(/\.\w+$/, "")}.query.csv` });
    a.click();
    URL.revokeObjectURL(url);
  }

  if (!workbookId) {
    return <div className="grid flex-1 place-items-center text-sm text-zinc-500">{t.query.empty}</div>;
  }

  return (
    <div className="flex min-h-0 flex-1 gap-4 p-4 md:p-6">
      <section className="flex min-w-0 flex-1 flex-col">
        <div className="mb-3">
          <h2 className="flex items-center gap-2 text-base font-semibold text-white"><Database className="size-4 text-violet-300" />{t.query.title}</h2>
          <p className="mt-0.5 text-xs text-zinc-500">{t.query.sub}</p>
        </div>

        <div className="mb-2 flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-zinc-500">{t.query.presets}:</span>
          {PRESETS.map(([key, sql]) => (
            <button key={key} onClick={() => { setQuery(sql); run(sql); }}
              className={`rounded-full border px-2.5 py-0.5 text-xs transition ${query === sql ? "border-violet-400/50 bg-violet-500/15 text-white" : "border-white/10 text-zinc-300 hover:bg-white/5"}`}>
              {t.query.presetNames[key] ?? key}
            </button>
          ))}
        </div>

        <div className="glow-border rounded-2xl bg-[#141419]/90">
          <SqlEditor ref={editor} value={query} onChange={setQuery} onRun={() => run()} check={check} onError={setLintError}
            schema={completionSchema} didYouMean={t.query.didYouMean} />
          <div className="flex items-center gap-3 border-t border-white/5 px-3 py-2">
            <button onClick={() => run()} disabled={running}
              className="flex items-center gap-1.5 rounded-lg bg-gradient-to-br from-violet-500 to-cyan-400 px-3 py-1 text-sm font-medium text-white shadow-lg shadow-violet-500/20 disabled:opacity-60">
              {running ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}{t.query.run}
            </button>
            {lintError
              ? <span className="min-w-0 truncate font-mono text-xs text-rose-300" title={lintError}>{lintError}</span>
              : <span className="text-xs text-zinc-500">{t.query.shortcut}</span>}
            {result && (
              <>
                <span className="ml-auto text-xs text-zinc-400">{t.query.rows(result.rows.length, result.truncated)}</span>
                <button onClick={downloadCsv} className="flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-zinc-300 hover:bg-white/5">
                  <Download className="size-3.5" />{t.query.csv}
                </button>
              </>
            )}
          </div>
        </div>

        <div className="mt-3 min-h-0 flex-1 overflow-auto rounded-2xl border border-white/[0.07] bg-[#141419]/90">
          {error && <pre className="whitespace-pre-wrap p-4 font-mono text-xs text-rose-300">{error}</pre>}
          {result && (result.rows.length === 0 ? <p className="p-4 text-sm text-zinc-500">{t.query.noRows}</p> : (
            <table className="w-full border-collapse text-xs">
              <thead className="sticky top-0 bg-[#1b1b22]">
                <tr>{result.columns.map((c, i) => <th key={i} className="whitespace-nowrap px-3 py-2 text-left font-medium text-zinc-400">{c}</th>)}</tr>
              </thead>
              <tbody>
                {result.rows.map((row, i) => (
                  <tr key={i} className="border-t border-white/5 hover:bg-white/[0.03]">
                    {row.map((v, j) => {
                      const s = v === null || v === undefined ? "" : String(v);
                      return (
                        <td key={j} className="max-w-md truncate px-3 py-1.5 font-mono text-zinc-300" title={s}>
                          {CELL_RE.test(s)
                            ? <button onClick={() => onOpenCell(s)} title={t.query.openCell(s)} className="text-violet-300 hover:text-violet-200 hover:underline">{s}</button>
                            : v === null ? <span className="text-zinc-600">NULL</span> : s}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
        </div>
      </section>

      <aside className="hidden w-64 shrink-0 overflow-y-auto rounded-2xl border border-white/[0.07] bg-[#141419]/90 p-4 lg:block">
        <h3 className="text-sm font-medium text-white">{t.query.schema}</h3>
        <p className="mb-3 text-[11px] text-zinc-500">{t.query.schemaHint}</p>
        <div className="space-y-3">
          {schema.map((s) => (
            <div key={s.name}>
              <button onClick={() => insert(s.name)} className="font-mono text-[12.5px] text-violet-300 hover:text-violet-200">
                {s.name}{s.type === "view" && <span className="ml-1 font-sans text-[10px] text-zinc-500">view</span>}
              </button>
              {s.columns.length > 0 && (
                <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 pl-2">
                  {s.columns.map((c) => (
                    <button key={c} onClick={() => insert(c)} className="font-mono text-[11.5px] text-zinc-500 hover:text-zinc-200">{c}</button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </aside>
    </div>
  );
}
