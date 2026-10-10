"use client";

import { AlertTriangle, ArrowUp, Bot, Crosshair, ListTree, Network, Settings2, Table2, User } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Summary } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export type Message = { id: string; role: "user" | "assistant"; content: string; error?: boolean; cells?: string[] };

const CELL_RE = /^(?:'[^']+'|[A-Za-z_][\w.]*)![A-Z]{1,3}\d+$/;

const SUGGESTION_ICONS = [ListTree, AlertTriangle, Settings2, Table2];

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
                className="cursor-pointer transition hover:!bg-violet-500/30 hover:!text-white"
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

function Typing() {
  return (
    <div className="flex items-center gap-1.5 px-1 py-2">
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          className="size-2 rounded-full bg-gradient-to-br from-violet-300 to-cyan-300"
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
  onSend: (text: string) => void;
  onNewData: () => void;
  onShowGraph: (cell: string) => void;
};

export function Chat({ summary, messages, pending, onSend, onNewData, onShowGraph }: Props) {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages, pending]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [draft]);

  const disabled = !summary || pending;
  function submit(text = draft) {
    const t = text.trim();
    if (!t || disabled) return;
    onSend(t);
    setDraft("");
  }

  return (
    <div className="relative flex h-full min-w-0 flex-1 flex-col">
      {/* Header */}
      <header className="flex items-center gap-4 border-b border-white/5 px-6 py-4 backdrop-blur-xl">
        {summary ? (
          <>
            <div className="min-w-0">
              <h2 className="truncate font-medium">{summary.name}</h2>
              <p className="truncate text-xs text-zinc-500">{summary.sheets.join(" · ")}</p>
            </div>
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
        <div className="mx-auto w-full max-w-3xl px-6 pb-40 pt-8">
          {messages.length === 0 ? (
            <div className="flex min-h-[60vh] flex-col items-center justify-center text-center">
              <motion.div
                initial={{ scale: 0.8, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                className="relative mb-6 grid size-20 place-items-center rounded-3xl bg-gradient-to-br from-violet-500 via-fuchsia-500 to-cyan-400 shadow-2xl shadow-violet-500/40"
              >
                <Bot className="size-10 text-white" />
                <span className="absolute -inset-3 -z-10 rounded-[2rem] bg-violet-500/30 blur-2xl" />
              </motion.div>
              <h3 className="text-3xl font-semibold tracking-tight">
                {summary ? t.chat.askAbout(<span className="text-gradient">{summary.name}</span>) : <span className="text-gradient">{t.chat.hero}</span>}
              </h3>
              <p className="mt-3 max-w-md text-sm text-zinc-400">
                {summary
                  ? t.chat.introReady
                  : t.chat.introEmpty}
              </p>
              {summary ? (
                <div className="mt-10 grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
                  {t.chat.suggestions.map(({ title, prompt }, i) => {
                    const Icon = SUGGESTION_ICONS[i];
                    return (
                    <motion.button
                      key={title}
                      initial={{ opacity: 0, y: 10 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: 0.1 + i * 0.06 }}
                      onClick={() => submit(prompt)}
                      className="glow-border group flex items-center gap-3 rounded-2xl bg-white/[0.03] p-4 text-left transition hover:-translate-y-0.5 hover:bg-white/[0.06]"
                    >
                      <span className="grid size-9 place-items-center rounded-xl bg-white/5 text-violet-300 transition group-hover:text-cyan-300">
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
                  className="mt-8 rounded-full bg-gradient-to-r from-violet-500 to-cyan-400 px-6 py-2.5 text-sm font-medium text-white shadow-lg shadow-violet-500/30 transition hover:scale-105"
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
                      className={`grid size-8 shrink-0 place-items-center rounded-xl ${
                        m.role === "user"
                          ? "bg-white/10 text-zinc-300"
                          : "bg-gradient-to-br from-violet-500 to-cyan-400 text-white shadow-lg shadow-violet-500/20"
                      }`}
                    >
                      {m.role === "user" ? <User className="size-4" /> : <Bot className="size-4" />}
                    </span>
                    {m.role === "user" ? (
                      <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl rounded-tr-md bg-gradient-to-br from-violet-600 to-indigo-600 px-4 py-2.5 text-[14.5px] text-white shadow-lg shadow-violet-900/30">
                        {m.content}
                      </div>
                    ) : (
                      <div
                        className={`min-w-0 max-w-[92%] rounded-2xl rounded-tl-md border px-5 py-4 backdrop-blur ${
                          m.error ? "border-rose-400/20 bg-rose-500/10 text-rose-100" : "border-white/[0.06] bg-white/[0.035]"
                        }`}
                      >
                        <Markdown text={m.content} onCell={(id) => submit(id)} />
                        {!!m.cells?.length && (
                          <div className="mt-4 flex flex-wrap gap-2 border-t border-white/5 pt-3">
                            {m.cells.map((c) => (
                              <button
                                key={c}
                                onClick={() => onShowGraph(c)}
                                className="flex items-center gap-1.5 rounded-lg border border-violet-400/20 bg-violet-500/10 px-2.5 py-1 text-xs text-violet-200 transition hover:bg-violet-500/20"
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
                    <span className="grid size-8 place-items-center rounded-xl bg-gradient-to-br from-violet-500 to-cyan-400 text-white">
                      <Bot className="size-4" />
                    </span>
                    <div className="rounded-2xl rounded-tl-md border border-white/[0.06] bg-white/[0.035] px-4 py-2">
                      <Typing />
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}
        </div>
      </div>

      {/* Composer */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-[#07080d] via-[#07080d]/90 to-transparent pb-6 pt-16">
        <div className="pointer-events-auto mx-auto w-full max-w-3xl px-6">
          <div className="glow-border flex items-end gap-2 rounded-3xl bg-zinc-900/80 p-2 pl-5 shadow-2xl shadow-black/50 backdrop-blur-xl">
            <Crosshair className="mb-3 size-4 shrink-0 text-zinc-600" />
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
              placeholder={summary ? t.chat.placeholder : t.chat.placeholderEmpty}
              className="max-h-[180px] flex-1 resize-none bg-transparent py-2.5 text-[15px] text-zinc-100 outline-none placeholder:text-zinc-600 disabled:cursor-not-allowed"
            />
            <button
              onClick={() => submit()}
              disabled={disabled || !draft.trim()}
              aria-label={t.chat.send}
              className="grid size-10 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-violet-500 to-cyan-400 text-white shadow-lg shadow-violet-500/30 transition enabled:hover:scale-105 disabled:from-zinc-700 disabled:to-zinc-700 disabled:text-zinc-500 disabled:shadow-none"
            >
              <ArrowUp className="size-5" />
            </button>
          </div>
          <p className="mt-2 text-center text-[11px] text-zinc-600">{t.chat.footer}</p>
        </div>
      </div>
    </div>
  );
}
