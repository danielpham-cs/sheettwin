"use client";

import dagre from "@dagrejs/dagre";
import {
  Background, BackgroundVariant, Controls, Handle, MarkerType, Position, ReactFlow,
  type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import { AlertTriangle, Crosshair, Loader2, MessageSquareText, Network, Search, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useMemo, useState } from "react";
import { api, type GraphData, type GraphNode, type NodeKind } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const NODE_W = 210;
const NODE_H = 62;

const KIND: Record<NodeKind, { color: string; ring: string; dot: string }> = {
  param: { color: "#a78bfa", ring: "ring-violet-400/40", dot: "bg-violet-400" },
  input: { color: "#22d3ee", ring: "ring-cyan-400/40", dot: "bg-cyan-400" },
  formula: { color: "#818cf8", ring: "ring-indigo-400/30", dot: "bg-indigo-400" },
  output: { color: "#34d399", ring: "ring-emerald-400/40", dot: "bg-emerald-400" },
};
const SEV_COLOR = { high: "#fb7185", medium: "#fbbf24", low: "#a1a1aa" };

type BlockData = GraphNode & { dim: boolean; selected: boolean };
type BlockNode = Node<BlockData, "block">;

function BlockNodeView({ data }: NodeProps<BlockNode>) {
  const { t } = useI18n();
  const k = KIND[data.kind];
  return (
    <div
      title={data.label}
      style={{ width: NODE_W, height: NODE_H, opacity: data.dim ? 0.18 : 1 }}
      className={`relative flex flex-col justify-center rounded-2xl border bg-zinc-900/90 px-3.5 py-2 shadow-xl backdrop-blur transition-all duration-200
        ${data.selected ? "border-white/40 ring-2 " + k.ring : "border-white/10"}
        ${data.focus ? "shadow-[0_0_40px_-4px] shadow-violet-500/60 ring-2 ring-violet-400/70" : ""}`}
    >
      <span className="absolute inset-y-2 left-0 w-1 rounded-r-full" style={{ background: k.color }} />
      <Handle type="target" position={Position.Left} className="!size-2 !border-0 !bg-white/30" />
      <div className="flex items-center gap-2">
        <span className="shrink-0 whitespace-nowrap text-[10px] font-medium uppercase tracking-wider" style={{ color: k.color }}>{t.graph.kinds[data.kind]}</span>
        {data.cells > 1 && <span className="shrink-0 rounded bg-white/5 px-1 font-mono text-[10px] text-zinc-400">×{data.cells}</span>}
        <span className="ml-auto truncate font-mono text-[10px] text-zinc-500">{data.range}</span>
      </div>
      <div className="mt-0.5 truncate font-mono text-[12.5px] text-zinc-100">{data.label}</div>
      {data.issues && (
        <span
          className="absolute -right-2 -top-2 flex items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[10px] font-semibold text-zinc-950 shadow-lg"
          style={{ background: SEV_COLOR[data.issues.severity] }}
        >
          <AlertTriangle className="size-2.5" />
          {data.issues.count}
        </span>
      )}
      <Handle type="source" position={Position.Right} className="!size-2 !border-0 !bg-white/30" />
    </div>
  );
}

const nodeTypes = { block: BlockNodeView };

function layout(data: GraphData): Record<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 16, ranksep: 56, marginx: 20, marginy: 20 });
  g.setDefaultEdgeLabel(() => ({}));
  data.nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
  data.edges.forEach((e) => g.setEdge(e.source, e.target));
  dagre.layout(g);
  return Object.fromEntries(data.nodes.map((n) => {
    const p = g.node(n.id);
    return [n.id, { x: p.x - NODE_W / 2, y: p.y - NODE_H / 2 }];
  }));
}

/** Every node upstream or downstream of id (the whole calculation chain through it). */
function chainOf(id: string, edges: GraphData["edges"]): Set<string> {
  const out = new Set([id]);
  for (const [from, to] of [["target", "source"], ["source", "target"]] as const) {
    const stack = [id];
    while (stack.length) {
      const cur = stack.pop()!;
      for (const e of edges) {
        if (e[from] === cur && !out.has(e[to])) { out.add(e[to]); stack.push(e[to]); }
      }
    }
  }
  return out;
}

type Props = {
  workbookId: string | null;
  focusCell: string | null;
  onFocusChange: (cell: string | null) => void;
  onAsk: (cell: string) => void;
};

export function GraphView({ workbookId, focusCell, onFocusChange, onAsk }: Props) {
  const key = `${workbookId}|${focusCell ?? ""}`;
  const [result, setResult] = useState<{ key: string; data?: GraphData; error?: string } | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ key: string; id: string } | null>(null);
  const [draft, setDraft] = useState("");
  const { t } = useI18n();

  useEffect(() => {
    if (!workbookId) return;
    let live = true;
    api.graph(workbookId, focusCell ?? undefined)
      .then((data) => live && setResult({ key, data }))
      .catch((e) => live && setResult({ key, error: e.message }));
    return () => { live = false; };
  }, [workbookId, focusCell, key]);

  const current = result?.key === key ? result : null;
  const data = current?.data ?? null;
  const positions = useMemo(() => (data ? layout(data) : {}), [data]);
  const lit = useMemo(() => (data && hovered ? chainOf(hovered, data.edges) : null), [data, hovered]);
  const selectedNode = data?.nodes.find((n) => selected?.key === key && n.id === selected.id) ?? null;

  const nodes: BlockNode[] = useMemo(() => (data?.nodes ?? []).map((n) => ({
    id: n.id,
    type: "block",
    position: positions[n.id],
    data: { ...n, dim: !!lit && !lit.has(n.id), selected: n.id === selectedNode?.id },
  })), [data, positions, lit, selectedNode]);

  const maxW = Math.max(1, ...(data?.edges.map((e) => e.weight) ?? [1]));
  const edges: Edge[] = useMemo(() => (data?.edges ?? []).map((e) => {
    const on = !!lit && lit.has(e.source) && lit.has(e.target);
    const color = on ? "#c4b5fd" : "rgba(255,255,255,0.22)";
    return {
      id: `${e.source}->${e.target}`,
      source: e.source,
      target: e.target,
      animated: on,
      style: { stroke: color, strokeWidth: 1.2 + 2.2 * Math.log1p(e.weight) / Math.log1p(maxW), opacity: lit && !on ? 0.12 : 1 },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
    };
  }), [data, lit, maxW]);

  const issueCount = data?.nodes.reduce((s, n) => s + (n.issues ? 1 : 0), 0) ?? 0;

  return (
    <div className="relative flex min-h-0 min-w-0 flex-1">
      <div className="relative min-w-0 flex-1">
        {/* Toolbar */}
        <div className="absolute left-4 right-4 top-4 z-10 flex flex-wrap items-center gap-3">
          <div className="flex rounded-2xl border border-white/10 bg-zinc-900/80 p-1 backdrop-blur-xl">
            {[
              { on: !focusCell, label: t.graph.overview, icon: Network, click: () => onFocusChange(null) },
              { on: !!focusCell, label: t.graph.trace, icon: Crosshair, click: () => onFocusChange(focusCell ?? selectedNode?.anchor ?? data?.nodes[0]?.anchor ?? null) },
            ].map(({ on, label, icon: Icon, click }) => (
              <button
                key={label}
                onClick={click}
                className={`flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-sm transition ${on ? "bg-white/10 text-white" : "text-zinc-400 hover:text-white"}`}
              >
                <Icon className="size-4" />{label}
              </button>
            ))}
          </div>
          <form
            onSubmit={(e) => { e.preventDefault(); if (draft.trim()) { onFocusChange(draft.trim()); setDraft(""); } }}
            className="glow-border flex items-center gap-2 rounded-2xl bg-zinc-900/80 px-3 py-2 text-sm backdrop-blur-xl"
          >
            <Search className="size-4 text-zinc-500" />
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={focusCell ?? t.graph.tracePlaceholder}
              className="w-52 bg-transparent font-mono text-zinc-100 outline-none placeholder:font-sans placeholder:text-zinc-500"
            />
          </form>
          {data && (
            <span className="text-xs text-zinc-500">
              {focusCell ? t.graph.traceInfo(focusCell) : ""}
              {t.graph.counts(data.nodes.length, data.edges.length)}
              {issueCount > 0 && <> · <span className="text-rose-300">{t.graph.issueNodes(issueCount)}</span></>}
              {data.truncated && t.graph.truncated}
            </span>
          )}
        </div>

        {/* Legend */}
        <div className="absolute bottom-4 left-4 z-10 flex flex-wrap gap-3 rounded-2xl border border-white/10 bg-zinc-900/80 px-4 py-2 text-xs text-zinc-400 backdrop-blur-xl">
          {(Object.keys(KIND) as NodeKind[]).map((kind) => (
            <span key={kind} className="flex items-center gap-1.5"><span className={`size-2 rounded-full ${KIND[kind].dot}`} />{t.graph.kinds[kind]}</span>
          ))}
          <span className="flex items-center gap-1.5"><AlertTriangle className="size-3 text-rose-300" />{t.graph.hasIssue}</span>
          <span className="text-zinc-600">{t.graph.thickness}</span>
        </div>

        {!workbookId ? (
          <div className="grid h-full place-items-center text-sm text-zinc-500">{t.graph.empty}</div>
        ) : current?.error ? (
          <div className="grid h-full place-items-center px-6 text-center text-sm text-rose-200">{current.error}</div>
        ) : !data ? (
          <div className="grid h-full place-items-center"><Loader2 className="size-8 animate-spin text-violet-300" /></div>
        ) : (
          <ReactFlow
            key={key}
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            colorMode="dark"
            fitView
            fitViewOptions={{ padding: 0.08 }}
            minZoom={0.15}
            nodesConnectable={false}
            proOptions={{ hideAttribution: true }}
            onNodeMouseEnter={(_, n) => setHovered(n.id)}
            onNodeMouseLeave={() => setHovered(null)}
            onNodeClick={(_, n) => setSelected({ key, id: n.id })}
            onPaneClick={() => setSelected(null)}
            style={{ background: "transparent" }}
          >
            <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="rgba(255,255,255,0.07)" />
            <Controls showInteractive={false} position="bottom-right" />
          </ReactFlow>
        )}
      </div>

      {/* Detail panel */}
      <AnimatePresence>
        {selectedNode && (
          <motion.aside
            initial={{ x: 24, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: 24, opacity: 0 }}
            transition={{ type: "spring", stiffness: 380, damping: 34 }}
            className="w-80 shrink-0 overflow-y-auto border-l border-white/10 bg-zinc-950/80 p-5 backdrop-blur-2xl"
          >
            <div className="flex items-start justify-between gap-2">
              <span className="text-[11px] font-medium uppercase tracking-wider" style={{ color: KIND[selectedNode.kind].color }}>
                {t.graph.kinds[selectedNode.kind]}{selectedNode.cells > 1 ? t.graph.cells(selectedNode.cells) : ""}
              </span>
              <button onClick={() => setSelected(null)} className="rounded-lg p-1 text-zinc-500 hover:bg-white/5 hover:text-white">
                <X className="size-4" />
              </button>
            </div>
            <h3 className="mt-1 break-all font-mono text-sm text-white">{selectedNode.label}</h3>
            <p className="mt-1 font-mono text-xs text-zinc-500">{selectedNode.range}</p>

            <dl className="mt-5 space-y-3 text-sm">
              {selectedNode.example && <Field label={selectedNode.cells > 1 ? t.graph.formulaFirst : t.graph.formula} mono>{selectedNode.example}</Field>}
              {selectedNode.formula && (
                <Field label={selectedNode.cells > 1 ? t.graph.r1c1Group : selectedNode.example ? "R1C1" : t.graph.formula} mono>{selectedNode.formula}</Field>
              )}
              {selectedNode.value !== undefined && selectedNode.value !== null && <Field label={t.graph.value} mono>{String(selectedNode.value)}</Field>}
              <Field label={t.graph.upDown}>
                {t.graph.upDownCounts(data!.edges.filter((e) => e.target === selectedNode.id).length, data!.edges.filter((e) => e.source === selectedNode.id).length)}
              </Field>
              {selectedNode.issues && (
                <Field label={t.graph.issues}>
                  <div className="flex flex-wrap gap-1.5">
                    {selectedNode.issues.kinds.map((k) => (
                      <span key={k} className="rounded-md px-1.5 py-0.5 font-mono text-[11px]" style={{ background: `${SEV_COLOR[selectedNode.issues!.severity]}22`, color: SEV_COLOR[selectedNode.issues!.severity] }}>{k}</span>
                    ))}
                  </div>
                </Field>
              )}
            </dl>

            <div className="mt-6 space-y-2">
              {!selectedNode.focus && (
                <button
                  onClick={() => onFocusChange(selectedNode.anchor)}
                  className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-violet-500 to-cyan-400 py-2 text-sm font-medium text-white shadow-lg shadow-violet-500/25 transition hover:brightness-110"
                >
                  <Crosshair className="size-4" />{t.graph.traceThis(selectedNode.anchor)}
                </button>
              )}
              <button
                onClick={() => onAsk(selectedNode.anchor)}
                className="flex w-full items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/[0.03] py-2 text-sm text-zinc-200 transition hover:bg-white/[0.07]"
              >
                <MessageSquareText className="size-4" />{t.graph.ask}
              </button>
            </div>
          </motion.aside>
        )}
      </AnimatePresence>
    </div>
  );
}

function Field({ label, mono, children }: { label: string; mono?: boolean; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] text-zinc-500">{label}</dt>
      <dd className={`mt-0.5 break-all text-zinc-200 ${mono ? "rounded-lg bg-white/[0.04] px-2 py-1 font-mono text-xs" : ""}`}>{children}</dd>
    </div>
  );
}
