"use client";

import { useCallback, useEffect, useState } from "react";
import { Chat, type Message } from "@/components/Chat";
import { ExportsPanel } from "@/components/ExportsPanel";
import { FailureView, ParseReport } from "@/components/ParseStatus";
import { GraphView } from "@/components/GraphView";
import { ChartsView } from "@/components/ChartsView";
import { IssuesView } from "@/components/IssuesView";
import { QueryView } from "@/components/QueryView";
import { ResolveDialog, type ResolveTarget } from "@/components/ResolveDialog";
import { StructureView } from "@/components/StructureView";
import { Sidebar, type View } from "@/components/Sidebar";
import { UploadDialog } from "@/components/UploadDialog";
import { api, type IngestResult, type Summary, type Workbook } from "@/lib/api";
import { I18nProvider, useI18n } from "@/lib/i18n";
import { FileSpreadsheet } from "lucide-react";

const STORE_KEY = "sheettwin.chats";

function loadChats(): Record<string, Message[]> {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) ?? "{}");
  } catch {
    return {};
  }
}

const uid = () => Math.random().toString(36).slice(2);

export default function App() {
  return (
    <I18nProvider>
      <Shell />
    </I18nProvider>
  );
}

function Shell() {
  const { t, lang } = useI18n();
  const [workbooks, setWorkbooks] = useState<Workbook[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<Summary | null>(null);
  const [chats, setChats] = useState<Record<string, Message[]>>(loadChats);
  const [pending, setPending] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [panel, setPanel] = useState<"exports" | null>(null);
  const [view, setView] = useState<View>("chat");
  const [graphFocus, setGraphFocus] = useState<string | null>(null);
  const [sheetFocus, setSheetFocus] = useState<string | null>(null);
  const [backendDown, setBackendDown] = useState(false);
  const [notice, setNotice] = useState<{ text: string; report?: boolean; download?: string } | null>(null);
  const [reportOpen, setReportOpen] = useState(false);
  const [resolving, setResolving] = useState<ResolveTarget | null>(null);

  const refresh = useCallback(
    (select?: string): Promise<Workbook[]> =>
      api.workbooks().then(
        (list) => {
          setWorkbooks(list);
          setBackendDown(false);
          setActiveId((cur) => {
            const keep = select ?? cur;
            return keep && list.some((w) => w.id === keep) ? keep : list[0]?.id ?? null;
          });
          return list;
        },
        () => { setBackendDown(true); return []; },
      ),
    [],
  );

  // After an upload, say whether it was a file seen before or a new version of a known one. `made`: how this version
  // was made from the previous one by an edit in SheetTwin (given its version number); it also offers the download.
  async function uploaded(res: IngestResult, made?: (version: number) => string) {
    const list = await refresh(res.id);
    const w = list.find((x) => x.id === res.id);
    const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString(t.locale, { dateStyle: "medium", timeStyle: "short" }) : "?");
    const parts: string[] = [];
    if (made) parts.push(made(w?.version ?? 1));
    else if (res.duplicate) parts.push(t.upload.duplicate(res.name, when(res.first_uploaded)));
    else if (w && w.versions > 1) parts.push(t.upload.newVersion(w.name, w.version));
    if (res.failed) parts.push(t.upload.parseFailed(res.name));
    else if (res.status === "verified") parts.push(t.upload.parsedVerified(res.name, res.counts?.cells ?? 0));
    else if (res.status === "partial") {
      const s = await api.summary(res.id).catch(() => null);
      parts.push(t.upload.parsedPartial(res.name, s?.verification?.coverage?.n_not_understood ?? 0));
    } else if (res.status === "check_failed") parts.push(t.upload.parsedCheckFailed(res.name));
    setNotice(parts.length ? { text: parts.join(" "), report: !res.failed, download: made ? api.exportUrl(res.id, res.name) : undefined } : null);
  }

  async function remove(id: string) {
    await api.remove(id);
    setChats((c) => { const next = { ...c }; delete next[id]; return next; });
    await refresh();
  }

  useEffect(() => {
    // The landing page's "try the sample" button links to /app/?sample=1.
    if (new URLSearchParams(window.location.search).has("sample")) {
      window.history.replaceState(null, "", window.location.pathname);
      api.sample().then(({ id }) => refresh(id), () => refresh());
    } else {
      refresh();
    }
  }, [refresh]);

  useEffect(() => {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(chats));
    } catch {}
  }, [chats]);

  const activeWorkbook = workbooks.find((w) => w.id === activeId);
  const selectWorkbook = (id: string) => { setActiveId(id); setGraphFocus(null); setSheetFocus(null); setNotice(null); };
  // Summary of the active workbook; a stale one (other id) counts as not loaded yet.
  const summary = loaded && loaded.id === activeId ? loaded : null;
  const [reloads, setReloads] = useState(0); // bumped after a re-parse, to fetch the new summary
  useEffect(() => {
    if (!activeId) return;
    let live = true;
    api.summary(activeId).then((s) => live && setLoaded(s)).catch(() => {});
    return () => { live = false; };
  }, [activeId, reloads]);

  async function send(text: string) {
    if (!activeId) return;
    const wid = activeId;
    const push = (m: Message) => setChats((c) => ({ ...c, [wid]: [...(c[wid] ?? []), m] }));
    push({ id: uid(), role: "user", content: text });
    setPending(true);
    try {
      // Short floor so the typing indicator reads as a reply, not a flicker.
      const [res] = await Promise.all([api.chat(wid, text, lang), new Promise((r) => setTimeout(r, 450))]);
      const { reply, cells, ...meta } = res;
      push({ id: uid(), role: "assistant", content: reply, cells, meta });
    } catch (e) {
      push({ id: uid(), role: "assistant", content: t.app.error((e as Error).message), error: true });
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="relative flex h-full overflow-hidden">
      {/* Aurora backdrop */}
      <div aria-hidden className="pointer-events-none absolute inset-0 -z-10 overflow-hidden">
        <div className="absolute -left-40 -top-40 size-[640px] animate-aurora rounded-full bg-violet-600/25 blur-[140px]" />
        <div className="absolute -bottom-52 right-[-10%] size-[620px] animate-aurora rounded-full bg-cyan-500/15 blur-[140px] [animation-delay:-6s]" />
        <div className="absolute left-1/2 top-1/3 size-[420px] animate-aurora rounded-full bg-fuchsia-600/10 blur-[120px] [animation-delay:-12s]" />
        <div className="absolute inset-0 bg-[radial-gradient(rgb(255_255_255/0.035)_1px,transparent_1px)] [background-size:22px_22px] [mask-image:radial-gradient(ellipse_at_center,black,transparent_75%)]" />
      </div>

      <Sidebar
        workbooks={workbooks}
        activeId={activeId}
        panel={panel}
        view={view}
        onView={(v) => { setView(v); setPanel(null); }}
        onSelect={selectWorkbook}
        onRemove={remove}
        onNewData={() => setUploadOpen(true)}
        onToggleExports={() => setPanel((p) => (p === "exports" ? null : "exports"))}
      />

      <ExportsPanel
        open={panel === "exports"}
        workbookId={activeId}
        workbookName={summary?.name}
        workbook={workbooks.find((w) => w.id === activeId)}
        versions={workbooks.filter((w) => w.name.toLowerCase() === workbooks.find((x) => x.id === activeId)?.name.toLowerCase())}
        onOpenCell={(cell) => { setPanel(null); setSheetFocus(cell); setView("sheets"); }}
        onClose={() => setPanel(null)}
      />

      <main className="relative flex min-w-0 flex-1 flex-col">
        {backendDown && (
          <div className="mx-6 mt-4 rounded-xl border border-amber-400/20 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-100">
            {t.app.backendDown}
          </div>
        )}
        {notice && (
          <div className="mx-6 mt-4 flex items-start gap-3 rounded-xl border border-cyan-300/20 bg-cyan-400/10 px-4 py-2.5 text-sm text-cyan-50">
            <span className="flex-1">{notice.text}</span>
            {notice.download && (
              <a href={notice.download} download className="shrink-0 rounded-md bg-cyan-300/15 px-2 py-0.5 text-xs text-white ring-1 ring-cyan-200/30 hover:bg-cyan-300/25">
                {t.fixes.download}
              </a>
            )}
            {notice.report && (
              <button onClick={() => setReportOpen(true)} className="shrink-0 rounded-md bg-cyan-300/15 px-2 py-0.5 text-xs text-white ring-1 ring-cyan-200/30 hover:bg-cyan-300/25">
                {t.upload.seeReport}
              </button>
            )}
            <button onClick={() => setNotice(null)} aria-label="close" className="text-cyan-200/70 hover:text-white">✕</button>
          </div>
        )}
        {activeWorkbook && (
          <WorkbookBar
            // The sidebar's workbook list is hidden on small screens, and on wide ones in the grid-heavy views.
            className={view === "sheets" || view === "graph" ? "flex" : "flex md:hidden"}
            workbooks={workbooks}
            active={activeWorkbook}
            onSelect={selectWorkbook}
          />
        )}
        {activeWorkbook?.status === "failed" ? (
          <FailureView
            key={activeWorkbook.id}
            workbook={activeWorkbook}
            onRetry={async () => { await api.rebuild(activeWorkbook.id); setReloads((n) => n + 1); await refresh(); }}
            onRemove={() => remove(activeWorkbook.id)}
            onUpload={() => setUploadOpen(true)}
          />
        ) : view === "sheets" ? (
          <StructureView
            key={activeId ?? "none"}
            workbookId={activeId}
            summary={summary}
            focusCell={sheetFocus}
            onTraceGraph={(cell) => { setGraphFocus(cell); setView("graph"); }}
            onAsk={(cell) => { setView("chat"); send(cell); }}
            onReparsed={() => { setReloads((n) => n + 1); refresh(); }}
            onResolve={setResolving}
          />
        ) : view === "issues" ? (
          <IssuesView
            key={activeId ?? "none"}
            workbookId={activeId}
            reloads={reloads}
            onResolve={setResolving}
            onOpenCell={(cell) => { setSheetFocus(cell); setView("sheets"); }}
            onChanged={async () => { setReloads((n) => n + 1); await refresh(); }}
          />
        ) : view === "query" ? (
          <QueryView
            key={activeId ?? "none"}
            workbookId={activeId}
            workbookName={summary?.name}
            onOpenCell={(cell) => { setSheetFocus(cell); setView("sheets"); }}
          />
        ) : view === "charts" ? (
          <ChartsView workbookId={activeId} workbookName={summary?.name} />
        ) : view === "graph" ? (
          <GraphView
            workbookId={activeId}
            focusCell={graphFocus}
            onFocusChange={setGraphFocus}
            onAsk={(cell) => { setView("chat"); send(cell); }}
          />
        ) : (
          <Chat
            summary={summary}
            messages={(activeId && chats[activeId]) || []}
            pending={pending}
            onSend={send}
            onNewData={() => setUploadOpen(true)}
            onShowGraph={(cell) => { setGraphFocus(cell); setView("graph"); }}
            onNavigate={(to) => (to === "exports" ? setPanel("exports") : setView(to))}
            onShowReport={() => setReportOpen(true)}
          />
        )}
      </main>

      <UploadDialog open={uploadOpen} onClose={() => setUploadOpen(false)} onDone={uploaded} />
      <ParseReport
        open={reportOpen}
        summary={summary}
        onClose={() => setReportOpen(false)}
        onReparse={async () => { if (activeId) { await api.rebuild(activeId); setReloads((n) => n + 1); await refresh(); } }}
        onOpenCell={(cell) => { setReportOpen(false); setSheetFocus(cell); setView("sheets"); }}
        onFixed={async (res) => { setReportOpen(false); await uploaded(res, (v) => t.fixes.applied(res.applied.length, res.name, v)); }}
      />
      <ResolveDialog
        workbookId={activeId}
        target={resolving}
        onClose={() => setResolving(null)}
        onEdited={async (res) => { setResolving(null); await uploaded(res, (v) => t.resolve.applied(res.edited.length, res.name, v)); }}
        onIgnored={async () => { setResolving(null); setReloads((n) => n + 1); await refresh(); }}
      />
    </div>
  );
}

/** Which workbook the current view shows, with a switcher, for views where the sidebar's list is hidden. */
function WorkbookBar({ className, workbooks, active, onSelect }: {
  className: string; workbooks: Workbook[]; active: Workbook; onSelect: (id: string) => void;
}) {
  const { t } = useI18n();
  const label = (w: Workbook) => (w.versions > 1 ? `${w.name} · v${w.version}/${w.versions}` : w.name);
  return (
    <div className={`${className} mx-4 mt-4 items-center gap-2 rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2 text-sm backdrop-blur md:mx-6`}>
      <FileSpreadsheet className="size-4 shrink-0 text-emerald-300" />
      <span className="hidden shrink-0 text-xs text-zinc-500 sm:inline">{t.sidebar.current}</span>
      <select
        value={active.id}
        onChange={(e) => onSelect(e.target.value)}
        aria-label={t.sidebar.current}
        title={`${active.name} · ${active.id}`}
        className="min-w-0 flex-1 cursor-pointer truncate rounded-md bg-transparent py-0.5 font-medium text-white outline-none hover:bg-white/5 focus-visible:ring-1 focus-visible:ring-violet-400/50"
      >
        {workbooks.map((w) => (
          <option key={w.id} value={w.id} className="bg-zinc-900 text-zinc-100">{label(w)}</option>
        ))}
      </select>
    </div>
  );
}
