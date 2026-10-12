// Thin client for the SheetIR FastAPI backend (proxied under /api by next.config.ts).

/** One stored result: the workbook file it came from (by content hash) and how it was parsed. */
export type Workbook = {
  id: string; // first 12 hex digits of the file's SHA-256: the same bytes always give the same result
  name: string; // file name at the first upload
  aliases: string[]; // other names the same bytes were uploaded under
  sha256: string;
  size: number | null;
  file_modified: string | null; // last-modified time on the uploader's machine
  first_uploaded: string | null;
  last_uploaded: string | null;
  uploads: number;
  built_at: string | null;
  sheetir_version: string | null;
  ir_schema: string | null;
  table_scorer: string;
  high_issues: number;
  status: ParseStatus;
  error: ParseError | null; // why the last parse failed
  version: number; // among results with the same file name, by first upload
  versions: number;
  previous: string | null; // id of the previous version
};

export type IngestResult = {
  id: string; name: string; duplicate: boolean; first_uploaded: string | null; reparsed: boolean;
  failed: boolean; status: ParseStatus; error?: ParseError; counts: Record<string, number> | null;
};

/** A suggested edit for a formula SheetIR cannot follow fully (sheetir/fixes.py). `fix` is the replacement formula,
 * when there is one; `advice` lists the issue kinds left for the user to handle; `blocked` says why a fix cannot be
 * written into the file. */
export type FixSuggestion = {
  cell: string;
  formula: string | null;
  issues: { kind: string; detail: string }[];
  fix: string | null;
  changes: { kind: string; function?: string; target?: string; old?: string; new?: string }[];
  inputs: { cell: string; value: unknown }[];
  advice: string[];
  blocked?: "shared_formula" | "array_formula" | "not_found";
};
export type FixSuggestions = { can_apply: boolean; fixes: FixSuggestion[] };

/** One issue, with the fix SheetIR recommends (sheetir/fixes.py `recommend`): the cells it would change and their new
 * contents, as typed in Excel. `current` holds those cells' contents now (and the issue's own cell). */
export type IssueEntry = {
  cell: string; group_id: number | null; kind: string; severity: "high" | "medium" | "low"; detail: string;
  recommendation: { type: string; params: Record<string, unknown>; edits: Record<string, string> } | null;
  current: Record<string, string>;
  ignorable: boolean;
  blocked?: Record<string, "shared_formula" | "array_formula" | "not_found">;
};
export type IgnoredIssue = { cell: string; kind: string; severity: string; detail: string; note: string | null; at: string | null };
export type ResolveData = { can_apply: boolean; issues: IssueEntry[]; ignored: IgnoredIssue[] };
export type IssueKey = { cell: string; kind: string; detail: string };

export type DiffResult = {
  counts: { added: number; removed: number; changed: number };
  identical: boolean;
  sheets_added: string[];
  sheets_removed: string[];
  cells: { cell_id: string; change: "added" | "removed" | "changed"; old: string | null; new: string | null }[];
  truncated: boolean;
};

export type Summary = {
  id: string;
  name: string;
  sheets: string[];
  counts: Record<"cells" | "formulas" | "groups" | "tables" | "params" | "outputs", number>;
  checks: { name: string; passed: number; severity: string; detail: string }[];
  issues_by_kind: { kind: string; severity: string; n: number }[];
  stale?: boolean; // built with an older IR schema: re-parse to get tables as the current SheetIR detects them
  status?: ParseStatus;
  verification?: Verification | null;
  load_warnings?: string[]; // e.g. "styles_dropped": the formatting part was unreadable and ignored
  user_tables?: Record<string, TableSpec[]>; // sheets whose tables the user defined
};

/** A table the user defined: its range, how many of its first rows are the header, whether the last is a total. */
export type TableSpec = { range: string; header_rows: number; total: boolean; name?: string };

/** How a parse went. verified: the IR was checked against the workbook re-read independently and every formula is
 * understood; partial: checked, but some formulas cannot be followed fully; check_failed: the check found a difference;
 * unverified: not checked (older IR, or the check could not run); failed: the file could not be parsed. */
export type ParseStatus = "verified" | "partial" | "check_failed" | "unverified" | "failed";

export type Verification = {
  status: ParseStatus;
  error?: string;
  seconds?: number;
  checks: {
    content?: { cells: number; formulas: number; matched: number; n_missing: number; n_extra: number; n_wrong: number;
      missing: string[]; extra: string[]; wrong: { cell: string; excel: string; ir: string }[]; pass: boolean };
    rebuild?: { cells: number; identical: number; pass: boolean };
    references?: { formulas: number; agree: number; checked: number; skipped: number;
      differ: { cell: string; formula: string; only_tokenizer: string[]; only_ir: string[] }[]; pass: boolean };
    groups?: { groups: number; members: number; ok: number; bad: { cell: string; expected: string; actual: string | null }[]; pass: boolean };
  };
  coverage: {
    formulas: number; understood: number; n_not_understood: number;
    not_understood: { cell: string; kind: string; detail: string }[];
    ignored: { kind: string; count: number }[];
  } | null;
};

export type ParseError = { kind: string; detail: string; at: string; sheetir_version: string };

export type Severity = "high" | "medium" | "low";
export type Role = "input" | "derived" | "output" | "label" | "orphan";

export type SheetCell = {
  cell_id: string;
  row: number;
  col: number;
  a1: string;
  vtype: string;
  role: Role;
  value: unknown;
  cached: unknown;
  formula: string | null;
  group_id: number | null;
  semantic: string | null;
  number_format: string | null;
  table_id: number | null;
};

export type TableInfo = {
  table_id: number;
  sheet: string;
  name: string;
  kind: string;
  source: "detected" | "listobject" | "user";
  header_row: number | null;
  header_top?: number | null; // first row of a multi-row header (newer IR only)
  row_start: number;
  row_end: number;
  col_start: number;
  col_end: number;
  key_col: number | null;
  total_row: number | null;
  confidence?: number | null;
  fields: { col: number; header: string; name: string; unit: string | null; kind: "data" | "derived" }[];
};

export type SheetData = {
  cells: SheetCell[];
  issues: { cell_id: string; kind: string; severity: Severity }[];
  tables: TableInfo[];
  params: { cell_id: string; name: string; label: string | null; unit: string | null }[];
};

export type CellDetail = {
  cell: SheetCell & { r1c1?: string | null };
  group: { group_id: number; axis: string; anchor: string; last: string; n_cells: number; r1c1: string } | null;
  issues: { kind: string; severity: Severity; detail: string }[];
  precedents: { cell_id: string; depth: number; role: Role | null; semantic: string | null; formula: string | null; value: unknown }[];
  dependents: { cell_id: string; semantic: string | null }[];
};

export type SqlResult = { columns: string[]; rows: unknown[][]; truncated: boolean };

export type ExportFile = { name: string; label: string; format: string; size: number; source: boolean };

/** `source` says who answered: the language model (grounded in the IR) or the built-in rules. */
export type ChatReply = {
  reply: string;
  cells: string[];
  source?: "llm" | "rules";
  model?: string;
  sql?: string | null;
  grounded?: boolean;
  ungrounded?: string[];
  /** Set when the model failed and the rules answered instead: why. */
  fallback?: string;
};

export type NodeKind = "param" | "input" | "formula" | "output";

export type GraphNode = {
  id: string;
  kind: NodeKind;
  label: string;
  anchor: string; // a representative cell, used to trace or ask about the node
  range: string;
  sheet: string;
  cells: number;
  formula?: string | null; // R1C1 for groups, A1 for single cells
  example?: string | null; // A1 formula of a group's anchor cell
  value?: unknown;
  depth?: number; // focus mode: >0 precedent, <0 dependent
  focus?: boolean;
  issues: { count: number; severity: "high" | "medium" | "low"; kinds: string[] } | null;
};

export type GraphData = {
  mode: "overview" | "focus";
  focus?: string;
  nodes: GraphNode[];
  edges: { source: string; target: string; weight: number }[];
  truncated?: boolean;
};

export type ChartField = {
  col: number;
  name: string;
  header: string;
  unit: string | null;
  kind: "data" | "derived";
  numeric: boolean;
  missing: number; // rows with no value (formula cells never recalculated by Excel)
  distinct: number;
  percent: boolean;
};

export type ChartData = {
  roles: { role: string; n: number }[];
  severity: { severity: "high" | "medium" | "low"; n: number }[];
  kinds: { kind: string; severity: "high" | "medium" | "low"; n: number }[];
  sheets: { sheet: string; cells: number; formulas: number }[];
  tables: {
    table_id: number;
    name: string;
    sheet: string;
    kind: string;
    fields: ChartField[];
    rows: { key: string; values: Record<string, unknown> }[];
  }[];
};

/** An API error; `kind` is set when the backend names the problem (e.g. "unsupported_type"). */
export class ApiError extends Error {
  constructor(message: string, readonly kind?: string, readonly info?: Record<string, unknown>) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail: unknown = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    if (detail && typeof detail === "object") {
      const d = detail as Record<string, unknown>;
      throw new ApiError(String(d.message ?? d.kind ?? res.statusText), d.kind as string | undefined, d);
    }
    throw new ApiError(String(detail));
  }
  return res.json();
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  workbooks: () => call<Workbook[]>("/api/workbooks"),
  summary: (id: string) => call<Summary>(`/api/workbooks/${id}/summary`),
  upload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    form.append("last_modified", String(file.lastModified)); // recorded as the file's provenance
    return call<IngestResult>("/api/workbooks", { method: "POST", body: form });
  },
  sample: () => call<IngestResult>("/api/workbooks/sample", { method: "POST" }),
  remove: (id: string) => call<{ deleted: string }>(`/api/workbooks/${id}`, { method: "DELETE" }),
  diff: (id: string, against: string) => call<DiffResult>(`/api/workbooks/${id}/diff?against=${encodeURIComponent(against)}`),
  chat: (id: string, message: string, lang: string) => call<ChatReply>(`/api/workbooks/${id}/chat`, json({ message, lang })),
  graph: (id: string, cell?: string) =>
    call<GraphData>(`/api/workbooks/${id}/graph${cell ? `?cell=${encodeURIComponent(cell)}` : ""}`),
  charts: (id: string) => call<ChartData>(`/api/workbooks/${id}/charts`),
  exports: (id: string, lang?: string) => call<ExportFile[]>(`/api/workbooks/${id}/exports${lang ? `?lang=${lang}` : ""}`),
  sheet: (id: string, name: string) => call<SheetData>(`/api/workbooks/${id}/sheet?name=${encodeURIComponent(name)}`),
  cell: (id: string, cell: string) => call<CellDetail>(`/api/workbooks/${id}/cell?id=${encodeURIComponent(cell)}`),
  sql: (id: string, query: string) => call<SqlResult>(`/api/workbooks/${id}/sql`, json({ query })),
  fixes: (id: string) => call<FixSuggestions>(`/api/workbooks/${id}/fixes`),
  applyFixes: (id: string, cells: string[]) =>
    call<IngestResult & { applied: string[] }>(`/api/workbooks/${id}/fixes/apply`, json({ cells })),
  resolve: (id: string) => call<ResolveData>(`/api/workbooks/${id}/resolve`),
  editCells: (id: string, edits: Record<string, string>) =>
    call<IngestResult & { edited: string[] }>(`/api/workbooks/${id}/edits`, json({ edits })),
  ignoreIssue: (id: string, key: IssueKey, note?: string) =>
    call<{ status: ParseStatus }>(`/api/workbooks/${id}/ignored`, json({ ...key, note })),
  unignoreIssue: (id: string, key: IssueKey) => call<{ status: ParseStatus }>(`/api/workbooks/${id}/ignored/remove`, json(key)),
  sqlCheck: (id: string, query: string) => call<{ error: string | null }>(`/api/workbooks/${id}/sql/check`, json({ query })),
  setUserTables: (id: string, sheet: string, tables: TableSpec[]) =>
    call<{ status: ParseStatus }>(`/api/workbooks/${id}/user-tables/${encodeURIComponent(sheet)}`,
      { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tables }) }),
  resetUserTables: (id: string, sheet: string) =>
    call<{ status: ParseStatus }>(`/api/workbooks/${id}/user-tables/${encodeURIComponent(sheet)}`, { method: "DELETE" }),
  rebuild: (id: string) => call<{ id: string; failed: boolean; status: ParseStatus; error?: ParseError }>(`/api/workbooks/${id}/rebuild`, { method: "POST" }),
  /** `lang` picks the language of a report; `inline` opens it in the browser instead of downloading it. */
  exportUrl: (id: string, name: string, opts: { lang?: string; inline?: boolean } = {}) => {
    const q = new URLSearchParams();
    if (opts.lang) q.set("lang", opts.lang);
    if (opts.inline) q.set("inline", "true");
    const query = q.toString();
    return `/api/workbooks/${id}/exports/${encodeURIComponent(name)}${query ? `?${query}` : ""}`;
  },
};

export function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** Spreadsheet column letters: 1 -> A, 27 -> AA. */
export function colName(n: number) {
  let s = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

/** Sheet name of a cell id such as Quote!J13 or 'My Sheet'!A1. */
export function sheetOf(cellId: string) {
  const s = cellId.slice(0, cellId.lastIndexOf("!"));
  return s.startsWith("'") ? s.slice(1, -1).replace(/''/g, "'") : s;
}
