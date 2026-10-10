// Thin client for the SheetIR FastAPI backend (proxied under /api by next.config.ts).

export type Workbook = { id: string; name: string; built_at: string | null; high_issues: number };

export type Summary = {
  id: string;
  name: string;
  sheets: string[];
  counts: Record<"cells" | "formulas" | "groups" | "tables" | "params" | "outputs", number>;
  checks: { name: string; passed: number; severity: string; detail: string }[];
  issues_by_kind: { kind: string; severity: string; n: number }[];
};

export type ExportFile = { name: string; label: string; format: string; size: number; source: boolean };

export type ChatReply = { reply: string; cells: string[] };

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

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new Error(detail);
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
    return call<{ id: string }>("/api/workbooks", { method: "POST", body: form });
  },
  sample: () => call<{ id: string }>("/api/workbooks/sample", { method: "POST" }),
  chat: (id: string, message: string, lang: string) => call<ChatReply>(`/api/workbooks/${id}/chat`, json({ message, lang })),
  graph: (id: string, cell?: string) =>
    call<GraphData>(`/api/workbooks/${id}/graph${cell ? `?cell=${encodeURIComponent(cell)}` : ""}`),
  charts: (id: string) => call<ChartData>(`/api/workbooks/${id}/charts`),
  exports: (id: string) => call<ExportFile[]>(`/api/workbooks/${id}/exports`),
  exportUrl: (id: string, name: string) => `/api/workbooks/${id}/exports/${encodeURIComponent(name)}`,
};

export function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
