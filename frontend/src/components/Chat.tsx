"use client";

import {
  AlertTriangle, ArrowUp, BookOpen, Bot, ChartLine, ChartPie, CircleCheck, Cloud, Crosshair, Database, FileSpreadsheet, FolderOutput, Grid3x3,
  ListTree, Lock, MessageSquareText, Network, Search, Settings2, Sparkles, Table2, User,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, Pie, PieChart, ResponsiveContainer, Scatter, ScatterChart,
  Tooltip, XAxis, YAxis,
} from "recharts";
import type { AgentChart, AgentStep, ChatReply, LlmStatus, Project, Provenance, Summary, Workbook } from "@/lib/api";
import { ProjectBadge, projectName } from "@/components/ProjectMenu";
import { StatusBadge } from "@/components/ParseStatus";
import { useI18n } from "@/lib/i18n";
import { useChartPalette } from "@/lib/theme";

const TOOL_ICONS: Record<string, typeof Search> = {
  search_catalog: Search, describe_table: Table2, run_sql: Database, make_chart: ChartLine, final_answer: CircleCheck,
};

/** "Sheet!A1" for the first cell of a source range, quoted when the sheet name needs it. */
function firstCell(p: Provenance) {
  const sheet = /^[A-Za-z_][\w.]*$/.test(p.sheet) ? p.sheet : `'${p.sheet.replace(/'/g, "''")}'`;
  return `${sheet}!${p.range.split(":")[0]}`;
}

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  error?: boolean;
  cells?: string[];
  /** In a project chat: the workbook the reply's cell ids are in. */
  workbook?: string;
  /** Where an assistant reply came from and how it was checked (absent in chats saved before this existed). */
  meta?: Omit<ChatReply, "reply" | "cells">;
};

const CELL_RE = /^(?:'[^']+'|[A-Za-z_][\w.]*)![A-Z]{1,3}\d+$/;

const SUGGESTION_ICONS = [ListTree, AlertTriangle, Settings2, Table2];

// Same icons and order as the left rail, so the description doubles as a map of it.
const FEATURES = [
  { key: "chat", icon: MessageSquareText, tint: "text-blue-300" },
  { key: "sheets", icon: Grid3x3, tint: "text-amber-200" },
  { key: "query", icon: Database, tint: "text-sky-300" },
  { key: "graph", icon: Network, tint: "text-cyan-300" },
  { key: "charts", icon: ChartPie, tint: "text-rose-300" },
  { key: "exports", icon: FolderOutput, tint: "text-emerald-300" },
] as const;

export type Destination = (typeof FEATURES)[number]["key"];

function Markdown({ text, onCell }: { text: string; onCell: (id: string) => void }) {
  const { t } = useI18n();
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ({ children }) => <div className="table-wrap"><table>{children}</table></div>,
          code: ({ children }) => {
            const s = String(children);
            return CELL_RE.test(s) ? (
              <code
                role="button"
                title={t.chat.traceCell}
                onClick={() => onCell(s)}
                className="cursor-pointer transition hover:!bg-blue-500/30 hover:!text-white"
              >
                {s}
              </code>
            ) : (
              <code>{children}</code>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

/** Who answered and what was checked: the model and its number check (with the query it ran), or a fallback notice. */
function ReplyMeta({ meta }: { meta: NonNullable<Message["meta"]> }) {
  const { t } = useI18n();
  if (meta.fallback) {
    return (
      <p title={meta.fallback} className="mb-3 flex items-center gap-1.5 text-xs text-amber-300/90">
        <AlertTriangle className="size-3.5 shrink-0" />{t.chat.fallback}
      </p>
    );
  }
  if (meta.source !== "llm") return null;
  return (
    <div className="mt-4 space-y-2 border-t border-white/5 pt-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1.5 rounded-lg bg-blue-500/10 px-2 py-0.5 text-blue-200">
          <Sparkles className="size-3.5" />{t.chat.ai(meta.model ?? "")}
        </span>
        {meta.grounded ? (
          <span className="flex items-center gap-1.5 rounded-lg bg-emerald-500/10 px-2 py-0.5 text-emerald-300">
            <CircleCheck className="size-3.5" />{t.chat.grounded}
          </span>
        ) : (
          <span className="flex items-center gap-1.5 rounded-lg bg-amber-500/10 px-2 py-0.5 text-amber-300">
            <AlertTriangle className="size-3.5" />{t.chat.ungrounded((meta.ungrounded ?? []).join(", "))}
          </span>
        )}
      </div>
      {meta.sql && (
        <details className="group">
          <summary className="cursor-pointer select-none text-zinc-500 transition hover:text-zinc-300">{t.chat.showSql}</summary>
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap rounded-lg bg-black/40 p-3 font-mono text-xs text-zinc-300">{meta.sql}</pre>
        </details>
      )}
    </div>
  );
}

/** One agent step on a line: its tool, the thought, and for a query its SQL and how many rows came back. */
function StepLine({ step }: { step: AgentStep }) {
  const { t } = useI18n();
  const Icon = TOOL_ICONS[step.tool] ?? Sparkles;
  const err = step.result?.error as string | undefined;
  const sql = step.tool === "run_sql" ? String(step.args.sql ?? "") : null;
  const detail = step.tool === "search_catalog" ? String(step.args.query ?? "")
    : step.tool === "describe_table" ? String(step.args.card_id ?? "") : null;
  const rows = typeof step.result?.row_count === "number" ? (step.result.row_count as number) : null;
  return (
    <li className="flex gap-2.5">
      <span className={`mt-0.5 grid size-5 shrink-0 place-items-center rounded-md ${err ? "bg-amber-500/15 text-amber-300" : "bg-white/5 text-cyan-300"}`}>
        <Icon className="size-3" />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-zinc-300">
          <b className="font-medium text-zinc-100">{t.chat.agent.tools[step.tool] ?? step.tool}</b>
          {detail && <span className="text-zinc-500"> · {detail}</span>}
          {rows !== null && <span className="text-zinc-500"> · {t.chat.agent.rows(rows)}</span>}
          <span className="text-zinc-600"> · {step.seconds}s</span>
        </p>
        {step.thought && <p className="text-zinc-500">{step.thought}</p>}
        {sql && <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded-md bg-black/40 p-2 font-mono text-xs text-zinc-300">{sql}</pre>}
        {err && <p className="mt-1 text-amber-300/90">{t.chat.agent.error}: {err}</p>}
      </div>
    </li>
  );
}

function ChartBlock({ chart }: { chart: AgentChart }) {
  const { CAT, INK } = useChartPalette();
  const data = chart.data.map((d) => ({ ...d, [chart.x]: d[chart.x] === null ? "—" : String(d[chart.x]) }));
  const axis = { stroke: INK.muted, tick: { fill: INK.secondary, fontSize: 11 } };
  const tooltip = <Tooltip contentStyle={{ background: INK.popover, border: `1px solid ${INK.border}`, borderRadius: 12, fontSize: 12 }} />;
  let body: React.ReactElement;
  if (chart.type === "pie") {
    body = (
      <PieChart>
        <Pie data={data} dataKey={chart.y[0]} nameKey={chart.x} outerRadius="80%" innerRadius="45%" stroke={INK.surface}>
          {data.map((_, i) => <Cell key={i} fill={CAT[i % CAT.length]} />)}
        </Pie>
        {tooltip}<Legend wrapperStyle={{ fontSize: 11, color: INK.secondary }} />
      </PieChart>
    );
  } else if (chart.type === "scatter") {
    body = (
      <ScatterChart>
        <CartesianGrid stroke={INK.grid} strokeDasharray="3 3" />
        <XAxis dataKey={chart.x} name={chart.x} {...axis} /><YAxis dataKey={chart.y[0]} name={chart.y[0]} {...axis} />
        {tooltip}<Scatter data={data} fill={CAT[0]} />
      </ScatterChart>
    );
  } else if (chart.type === "line") {
    body = (
      <LineChart data={data}>
        <CartesianGrid stroke={INK.grid} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey={chart.x} {...axis} /><YAxis {...axis} width={70} />
        {tooltip}{chart.y.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {chart.y.map((y, i) => <Line key={y} dataKey={y} stroke={CAT[i % CAT.length]} strokeWidth={2} dot={{ r: 2.5 }} />)}
      </LineChart>
    );
  } else {
    body = (
      <BarChart data={data}>
        <CartesianGrid stroke={INK.grid} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey={chart.x} {...axis} /><YAxis {...axis} width={70} />
        {tooltip}{chart.y.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {chart.y.map((y, i) => <Bar key={y} dataKey={y} fill={CAT[i % CAT.length]} radius={[4, 4, 0, 0]} />)}
      </BarChart>
    );
  }
  return (
    <div className="mt-4 rounded-lg border border-white/[0.06] bg-surface/80 p-3">
      {chart.title && <p className="mb-2 text-sm font-medium text-zinc-200">{chart.title}</p>}
      <div className="h-64 w-full"><ResponsiveContainer>{body}</ResponsiveContainer></div>
    </div>
  );
}

/** The agent's answer: model and number check, its chart, the sheets it read, and its steps. */
function AgentMeta({ meta, onOpenSource }: { meta: NonNullable<Message["meta"]>; onOpenSource: (cell: string, workbook?: string) => void }) {
  const { t } = useI18n();
  const steps = meta.steps ?? [];
  return (
    <>
      {meta.chart && <ChartBlock chart={meta.chart} />}
      <div className="mt-4 space-y-3 border-t border-white/5 pt-3 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1.5 rounded-lg bg-blue-500/10 px-2 py-0.5 text-blue-200">
            <Sparkles className="size-3.5" />{t.chat.ai(meta.model ?? "")}
          </span>
          {meta.grounded ? (
            <span className="flex items-center gap-1.5 rounded-lg bg-emerald-500/10 px-2 py-0.5 text-emerald-300">
              <CircleCheck className="size-3.5" />{t.chat.grounded}
            </span>
          ) : (
            <span className="flex items-center gap-1.5 rounded-lg bg-amber-500/10 px-2 py-0.5 text-amber-300">
              <AlertTriangle className="size-3.5" />{t.chat.ungrounded((meta.ungrounded ?? []).join(", "))}
            </span>
          )}
          {meta.stopped && (
            <span className="flex items-center gap-1.5 rounded-lg bg-amber-500/10 px-2 py-0.5 text-amber-300">
              <AlertTriangle className="size-3.5" />{t.chat.agent.stopped}
            </span>
          )}
        </div>
        {!!meta.provenance?.length && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-zinc-500">{t.chat.agent.sources}:</span>
            {meta.provenance.slice(0, 16).map((p) => (
              <button
                key={`${p.workbook_id ?? ""}|${p.sheet}!${p.range}`}
                title={`${p.workbook ? `${p.workbook} · ` : ""}${p.table_title ?? ""} · ${t.chat.agent.openSource}`}
                onClick={() => onOpenSource(firstCell(p), p.workbook_id)}
                className="flex items-center gap-1 rounded-md border border-emerald-400/15 bg-emerald-500/[0.07] px-2 py-0.5 font-mono text-xs text-emerald-200 transition hover:bg-emerald-500/20"
              >
                <FileSpreadsheet className="size-3" />{p.workbook && <span className="font-sans text-emerald-300/70">{p.workbook} ·</span>}{p.sheet}!{p.range}
              </button>
            ))}
            {meta.provenance.length > 16 && <span className="text-zinc-500">+{meta.provenance.length - 16}</span>}
          </div>
        )}
        {!!steps.length && (
          <details className="group">
            <summary className="cursor-pointer select-none text-zinc-500 transition hover:text-zinc-300">
              {t.chat.agent.steps(steps.length)}{meta.seconds ? ` · ${meta.seconds}s` : ""}
            </summary>
            <ol className="mt-2 space-y-2.5">{steps.map((s) => <StepLine key={s.n} step={s} />)}</ol>
          </details>
        )}
      </div>
    </>
  );
}

function Typing() {
  return (
    <div className="flex items-center gap-1.5 px-1 py-2">
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          className="size-1.5 rounded-full bg-zinc-400"
          animate={{ y: [0, -5, 0], opacity: [0.4, 1, 0.4] }}
          transition={{ duration: 0.9, repeat: Infinity, delay: i * 0.15 }}
        />
      ))}
    </div>
  );
}

type Props = {
  summary: Summary | null;
  messages: Message[];
  pending: boolean;
  /** The agent's steps so far, while an answer is on its way. */
  liveSteps?: AgentStep[];
  llm?: LlmStatus | null;
  onSend: (text: string) => void;
  onNewData: () => void;
  onShowGraph: (cell: string, workbook?: string) => void;
  onOpenSource: (cell: string, workbook?: string) => void;
  /** The project the chat belongs to: its files (one per file name) are what the AI answers from; `activeFile` is
   * the one on screen, which cell addresses and questions about structure or issues are about. */
  context?: { project: Project; files: Workbook[]; activeFile: string | null };
  /** The project's instructions and documents for the AI: a summary ("Instructions · 2 docs"), whether any is set,
   * and how to open them. */
  knowledge?: { summary: string; set: boolean; onOpen: () => void };
  onNavigate: (to: Exclude<Destination, "chat">) => void;
  onShowReport: () => void;
};

export function Chat({ summary, messages, pending, liveSteps = [], llm, onSend, onNewData, onShowGraph, onOpenSource, onNavigate, onShowReport, context, knowledge }: Props) {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    // Follow new messages; the welcome screen (no messages) stays at the top.
    if (messages.length) scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages, pending]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [draft]);

  const disabled = !summary || pending;
  const pname = context ? projectName(context.project, t) : null;
  function submit(text = draft) {
    const t = text.trim();
    if (!t || disabled) return;
    onSend(t);
    setDraft("");
  }

  return (
    <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
      {/* Header */}
      <header className="flex items-center gap-3 border-b border-white/5 px-3 py-3 sm:gap-4 sm:px-6 sm:py-3.5">
        {context ? (
          <>
            <ProjectBadge project={context.project} size="lg" />
            <div className="min-w-0">
              <h2 className="truncate font-semibold text-white">{pname}</h2>
              <p className="truncate text-xs text-zinc-500">{t.projects.chatSub(context.files.length)}</p>
            </div>
            {knowledge && (
              <button onClick={knowledge.onOpen} title={t.knowledge.open}
                className={`ml-auto flex shrink-0 items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition ${knowledge.set
                  ? "border-blue-400/30 bg-blue-500/10 text-blue-100 hover:bg-blue-500/20" : "border-white/10 text-zinc-300 hover:bg-white/5"}`}>
                <BookOpen className="size-3.5" />{t.knowledge.button}
                <span className={knowledge.set ? "text-blue-300/80" : "text-zinc-500"}>· {knowledge.summary}</span>
              </button>
            )}
            {llm && <AiBadge llm={llm} className={`${knowledge ? "" : "ml-auto"} hidden md:flex`} />}
          </>
        ) : summary ? (
          <>
            <div className="min-w-0">
              <h2 className="truncate font-medium">{summary.name}</h2>
              <p className="truncate text-xs text-zinc-500">{summary.sheets.join(" · ")}</p>
            </div>
            <StatusBadge status={summary.status ?? "unverified"} onClick={onShowReport} />
            {summary.catalog?.built && summary.catalog.stats && (
              <span
                title={(summary.catalog.cards ?? []).map((c) => `${c.title} (${c.row_count})`).join("\n")}
                className="hidden items-center gap-1.5 rounded-full border border-cyan-300/15 bg-cyan-400/[0.06] px-3 py-1 text-xs text-cyan-100 md:flex"
              >
                <Table2 className="size-3.5" />{t.chat.catalog(summary.catalog.stats.cards, summary.catalog.stats.families)}
              </span>
            )}
            {!!summary.catalog?.stats?.low_confidence && (
              <span className="hidden items-center gap-1.5 rounded-full bg-amber-500/10 px-3 py-1 text-xs text-amber-300 md:flex">
                <AlertTriangle className="size-3.5" />{t.chat.lowConfidence(summary.catalog.stats.low_confidence)}
              </span>
            )}
            {llm && <AiBadge llm={llm} className="hidden xl:flex" />}
            <div className="ml-auto hidden gap-2 lg:flex">
              {[
                [t.chat.stats.formulas, summary.counts.formulas],
                [t.chat.stats.groups, summary.counts.groups],
                [t.chat.stats.tables, summary.counts.tables],
                [t.chat.stats.params, summary.counts.params],
              ].map(([k, v]) => (
                <span key={k} className="rounded-full border border-white/5 bg-white/[0.03] px-3 py-1 text-xs text-zinc-400">
                  <b className="mr-1 font-semibold text-zinc-100">{v}</b>{k}
                </span>
              ))}
            </div>
          </>
        ) : (
          <h2 className="font-medium text-zinc-400">{t.chat.noWorkbook}</h2>
        )}
      </header>

      {/* Messages */}
      <div ref={scroller} className="flex-1 overflow-y-auto">
        {/* A live log: screen readers read each new message; aria-busy holds the answer back until it is complete */}
        <div role="log" aria-live="polite" aria-busy={pending} className="mx-auto w-full max-w-3xl px-3 pb-40 pt-6 sm:px-6 sm:pt-8">
          {messages.length === 0 ? (
            <div className="flex min-h-[60vh] flex-col items-center justify-center text-center">
              <div className="mb-5 grid size-12 place-items-center rounded-lg bg-white/[0.04] ring-1 ring-white/10">
                <Bot className="size-6 text-blue-300" />
              </div>
              <h3 className="text-2xl font-semibold tracking-tight sm:text-3xl">
                {context && summary ? <span className="text-accent">{t.projects.askAbout(pname!)}</span>
                  : summary ? t.chat.askAbout(<span className="text-accent">{summary.name}</span>) : <span className="text-accent">{t.chat.hero}</span>}
              </h3>
              <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.05 }}
                className="mt-6 w-full rounded-xl border border-white/[0.07] bg-white/[0.025] p-5 text-left"
              >
                <p className="text-sm leading-relaxed text-zinc-300">{t.chat.about.desc}</p>
                <div className="mt-4 grid gap-1 sm:grid-cols-2">
                  {FEATURES.map(({ key, icon: Icon, tint }) => {
                    const f = t.chat.about.features[key];
                    return (
                      <button
                        key={key}
                        onClick={() => (key === "chat" ? box.current?.focus() : onNavigate(key))}
                        className="group flex items-start gap-3 rounded-lg p-2.5 text-left transition hover:bg-white/[0.05]"
                      >
                        <span className={`grid size-8 shrink-0 place-items-center rounded-md bg-white/5 ring-1 ring-white/10 ${tint}`}>
                          <Icon className="size-4" />
                        </span>
                        <span className="min-w-0">
                          <span className="block text-sm font-medium text-zinc-100">{f.title}</span>
                          <span className="block text-xs leading-relaxed text-zinc-500 group-hover:text-zinc-400">{f.body}</span>
                        </span>
                      </button>
                    );
                  })}
                </div>
                <p className="mt-3 flex items-center gap-2 border-t border-white/5 pt-3 text-xs text-zinc-500">
                  <Lock className="size-3.5 shrink-0 text-emerald-300/80" />
                  {t.chat.about.privacy}
                </p>
              </motion.div>
              {summary ? (
                <div className="mt-4 grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
                  {t.chat.suggestions.map(({ title, prompt }, i) => {
                    const Icon = SUGGESTION_ICONS[i];
                    return (
                    <motion.button
                      key={title}
                      initial={{ opacity: 0, y: 10 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: 0.1 + i * 0.06 }}
                      onClick={() => submit(prompt)}
                      className="field group flex items-center gap-3 rounded-lg bg-white/[0.03] p-4 text-left transition hover:-translate-y-0.5 hover:bg-white/[0.06]"
                    >
                      <span className="grid size-9 place-items-center rounded-md bg-white/5 text-blue-300 transition group-hover:text-cyan-300">
                        <Icon className="size-4" />
                      </span>
                      <span>
                        <span className="block text-sm font-medium">{title}</span>
                        <span className="block text-xs text-zinc-500">{prompt}</span>
                      </span>
                    </motion.button>
                    );
                  })}
                </div>
              ) : (
                <button
                  onClick={onNewData}
                  className="mt-6 rounded-full bg-blue-600 px-6 py-2.5 text-sm font-medium text-snow shadow-lg transition hover:bg-blue-500"
                >
                  {t.chat.newData}
                </button>
              )}
            </div>
          ) : (
            <div className="space-y-6">
              <AnimatePresence initial={false}>
                {messages.map((m) => (
                  <motion.div
                    key={m.id}
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ type: "spring", stiffness: 400, damping: 34 }}
                    className={`flex gap-3 ${m.role === "user" ? "flex-row-reverse" : ""}`}
                  >
                    <span
                      className={`grid size-8 shrink-0 place-items-center rounded-md ${
                        m.role === "user"
                          ? "bg-white/10 text-zinc-300"
                          : "bg-blue-600 text-snow shadow-lg"
                      }`}
                    >
                      {m.role === "user" ? <User className="size-4" /> : <Bot className="size-4" />}
                    </span>
                    {m.role === "user" ? (
                      <div className="max-w-[80%] whitespace-pre-wrap rounded-lg rounded-tr-sm bg-blue-600 px-4 py-2.5 text-[14.5px] text-snow shadow-lg">
                        {m.content}
                      </div>
                    ) : (
                      <div
                        className={`min-w-0 max-w-[92%] rounded-lg rounded-tl-md border px-5 py-4 ${
                          m.error ? "border-rose-400/20 bg-rose-500/10 text-rose-100" : "border-white/[0.06] bg-white/[0.035]"
                        }`}
                      >
                        {m.meta?.fallback && <ReplyMeta meta={m.meta} />}
                        <Markdown text={m.content} onCell={(id) => submit(id)} />
                        {m.meta?.source === "llm" && <ReplyMeta meta={m.meta} />}
                        {m.meta?.source === "agent" && <AgentMeta meta={m.meta} onOpenSource={onOpenSource} />}
                        {!!m.meta?.knowledge?.length && (
                          <p className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-zinc-500">
                            <BookOpen className="size-3.5 text-blue-300" />{t.knowledge.used}
                            {m.meta.knowledge.map((d) => (
                              <button key={d.id} onClick={knowledge?.onOpen}
                                className="rounded-md border border-blue-400/20 bg-blue-500/[0.07] px-1.5 py-0.5 text-blue-200 hover:bg-blue-500/15">
                                {d.name}{d.whole ? "" : ` (${t.knowledge.excerpt})`}
                              </button>
                            ))}
                          </p>
                        )}
                        {!!m.cells?.length && (
                          <div className="mt-4 flex flex-wrap gap-2 border-t border-white/5 pt-3">
                            {m.cells.map((c) => (
                              <button
                                key={c}
                                onClick={() => onShowGraph(c, m.workbook)}
                                className="flex items-center gap-1.5 rounded-lg border border-blue-400/20 bg-blue-500/10 px-2.5 py-1 text-xs text-blue-200 transition hover:bg-blue-500/20"
                              >
                                <Network className="size-3.5" />{t.chat.showInGraph(c)}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </motion.div>
                ))}
                {pending && (
                  <motion.div key="typing" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="flex gap-3">
                    <span className="grid size-8 place-items-center rounded-md bg-blue-600 text-snow">
                      <Bot className="size-4" />
                    </span>
                    <div className="min-w-0 max-w-[92%] rounded-lg rounded-tl-md border border-white/[0.06] bg-white/[0.035] px-4 py-2">
                      <div className="flex items-center gap-3">
                        <Typing />
                        <span className="text-xs text-zinc-500">{t.chat.thinking}</span>
                      </div>
                      {!!liveSteps.length && (
                        <ol className="mb-2 mt-1 space-y-2.5 text-xs">{liveSteps.map((s) => <StepLine key={s.n} step={s} />)}</ol>
                      )}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}
        </div>
      </div>

      {/* Composer */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-canvas via-canvas/90 to-transparent pb-3 pt-16 sm:pb-6">
        <div className="pointer-events-auto mx-auto w-full max-w-3xl px-3 sm:px-6">
          {context && context.files.length > 0 && (
            <div className="mb-2 flex flex-wrap items-center gap-1.5 text-xs">
              <span className="flex items-center gap-1.5 text-zinc-500"><ProjectBadge project={context.project} size="sm" />{t.projects.context}:</span>
              {context.files.map((w) => {
                const inView = w.id === context.activeFile;
                return (
                  <span key={w.id} title={inView ? t.projects.inViewHint : w.name}
                    className={`inline-flex max-w-[16rem] items-center gap-1 rounded-md border px-2 py-0.5 ${inView ? "border-blue-400/40 bg-blue-500/10 text-blue-100" : "border-white/10 bg-white/[0.04] text-zinc-300"}`}>
                    <FileSpreadsheet className="size-3 shrink-0 text-emerald-300" />
                    <span className="truncate">{w.name}</span>
                    {inView && context.files.length > 1 && <span className="shrink-0 text-xs text-blue-300">· {t.projects.inView}</span>}
                  </span>
                );
              })}
              {knowledge?.set && (
                <button onClick={knowledge.onOpen} title={t.knowledge.open}
                  className="inline-flex items-center gap-1 rounded-md border border-blue-400/30 bg-blue-500/10 px-2 py-0.5 text-blue-100 hover:bg-blue-500/20">
                  <BookOpen className="size-3 shrink-0" />{knowledge.summary}
                </button>
              )}
            </div>
          )}
          <div className="field flex items-end gap-2 rounded-xl bg-zinc-900/80 p-1.5 pl-3 shadow-2xl shadow-black/50 sm:p-2 sm:pl-5">
            <Crosshair className="mb-3 hidden size-4 shrink-0 text-zinc-600 sm:block" />
            <textarea
              ref={box}
              rows={1}
              value={draft}
              disabled={!summary}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder={summary ? (pname ? t.projects.placeholder(pname) : t.chat.placeholder) : t.chat.placeholderEmpty}
              className="max-h-[180px] flex-1 resize-none bg-transparent py-2.5 text-[15px] text-zinc-100 outline-none placeholder:text-zinc-600 disabled:cursor-not-allowed"
            />
            <button
              onClick={() => submit()}
              disabled={disabled || !draft.trim()}
              aria-label={t.chat.send}
              className="grid size-9 shrink-0 place-items-center rounded-lg bg-blue-600 sm:size-10 text-snow shadow-lg transition enabled:hover:bg-blue-500 disabled:from-zinc-700 disabled:to-zinc-700 disabled:text-zinc-500 disabled:shadow-none"
            >
              <ArrowUp className="size-5" />
            </button>
          </div>
          <p className="mt-2 hidden text-center text-xs text-zinc-600 sm:block">{t.chat.footer}</p>
        </div>
      </div>
    </div>
  );
}

/** Shown only when the user should know how answers are made: no model is configured (the built-in rules answer), or
 * the model is a cloud service (questions and data excerpts leave the network). A model on this machine or the LAN is
 * the normal case and needs no badge: each answer names its model. Details on hover. */
function AiBadge({ llm, className }: { llm: LlmStatus; className: string }) {
  const { t } = useI18n();
  const s = t.chat.ai_status;
  if (llm.enabled && llm.local) return null;
  // "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q5_K_M" -> "Qwen3.6-35B-A3B"
  const short = (llm.model ?? "").split("/").pop()!.split(":")[0].replace(/-GGUF$/i, "") || (llm.model ?? "");
  const [label, title, tone] = !llm.enabled
    ? [s.off, s.offHint(llm.reason ?? ""), "bg-white/[0.04] text-zinc-400 ring-white/10"]
    : [s.cloud(short), s.cloudHint(llm.model ?? "", llm.endpoint ?? ""), "bg-sky-500/10 text-sky-300 ring-sky-400/20"];
  return (
    <span title={title} className={`${className} max-w-[18rem] shrink-0 cursor-help items-center gap-1.5 rounded-full px-3 py-1 text-xs ring-1 ${tone}`}>
      {!llm.enabled ? <Bot className="size-3.5 shrink-0" /> : <Cloud className="size-3.5 shrink-0" />}
      <span className="truncate">{label}</span>
    </span>
  );
}
