"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Chat, type Message } from "@/components/Chat";
import { ExportsPanel } from "@/components/ExportsPanel";
import { FailureView, ParseReport } from "@/components/ParseStatus";
import { GraphView } from "@/components/GraphView";
import { ChartsView } from "@/components/ChartsView";
import { IssuesView } from "@/components/IssuesView";
import { QueryView } from "@/components/QueryView";
import { ResolveDialog, type ResolveTarget } from "@/components/ResolveDialog";
import { StructureView } from "@/components/StructureView";
import { TwinView } from "@/components/TwinView";
import { Sidebar, type View } from "@/components/Sidebar";
import { KnowledgePanel } from "@/components/KnowledgePanel";
import { ProjectSwitcher, type ProjectControls } from "@/components/ProjectMenu";
import { UploadDialog } from "@/components/UploadDialog";
import { api, DEFAULT_PROJECT, type AgentStep, type IngestResult, type LlmStatus, type Project, type ProjectKnowledge, type Summary, type Workbook } from "@/lib/api";
import { I18nProvider, useI18n } from "@/lib/i18n";
import { ChevronRight, FileSpreadsheet } from "lucide-react";

const STORE_KEY = "sheettwin.chats"; // chats by project: "project:<id>"
const PROJECT_KEY = "sheettwin.project";

function loadProject(): string {
  try {
    return localStorage.getItem(PROJECT_KEY) || DEFAULT_PROJECT;
  } catch {
    return DEFAULT_PROJECT;
  }
}

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
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string>(loadProject);
  const [chosenId, setChosenId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<Summary | null>(null);
  const [chats, setChats] = useState<Record<string, Message[]>>(loadChats);
  const [pending, setPending] = useState(false);
  const [liveSteps, setLiveSteps] = useState<AgentStep[]>([]);
  const [llm, setLlm] = useState<LlmStatus | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [panel, setPanel] = useState<"exports" | null>(null);
  const [view, setView] = useState<View>("chat");
  const [graphFocus, setGraphFocus] = useState<string | null>(null);
  const [sheetFocus, setSheetFocus] = useState<string | null>(null);
  const [backendDown, setBackendDown] = useState(false);
  const [notice, setNotice] = useState<{ text: string; report?: boolean; download?: string } | null>(null);
  const [reportOpen, setReportOpen] = useState(false);
  const [resolving, setResolving] = useState<ResolveTarget | null>(null);
  const [twinRef, setTwinRef] = useState<string | null>(null); // a history record to open in the twin view

  const refresh = useCallback(
    (select?: string): Promise<Workbook[]> =>
      Promise.all([api.workbooks(), api.projects()]).then(
        ([list, ps]) => {
          setWorkbooks(list);
          setProjects(ps);
          setBackendDown(false);
          if (select) setChosenId(select);
          return list;
        },
        () => { setBackendDown(true); return []; },
      ),
    [],
  );

  // The project in view (it may have been deleted elsewhere: then "default"), its workbooks, and the one on screen.
  const project = projects.find((p) => p.id === projectId) ?? projects.find((p) => p.id === DEFAULT_PROJECT) ?? null;
  const inProject = useMemo(() => workbooks.filter((w) => project?.workbooks.includes(w.id)), [workbooks, project]);
  const activeId = chosenId && inProject.some((w) => w.id === chosenId) ? chosenId : inProject[0]?.id ?? null;
  const chatKey = project ? `project:${project.id}` : null;

  // The project's instructions and documents for the AI (kept with the project they were fetched for).
  const [knowledgeOpen, setKnowledgeOpen] = useState(false);
  const [fetchedKnowledge, setFetchedKnowledge] = useState<{ pid: string; data: ProjectKnowledge } | null>(null);
  const pid = project?.id ?? null;
  const loadKnowledge = useCallback(async () => {
    if (!pid) return;
    const data = await api.knowledge(pid);
    setFetchedKnowledge({ pid, data });
  }, [pid]);
  useEffect(() => {
    if (!pid) return;
    let live = true;
    api.knowledge(pid).then((data) => live && setFetchedKnowledge({ pid, data }), () => {});
    return () => { live = false; };
  }, [pid]);
  // Corrections waiting for approval in this project (the twin view's badge), counted again when the history changes.
  const [pendingFor, setPendingFor] = useState<{ pid: string; n: number } | null>(null);
  const [historyTick, setHistoryTick] = useState(0);
  useEffect(() => {
    if (!pid) return;
    let live = true;
    api.history(pid, { status: "pending" }).then((d) => live && setPendingFor({ pid, n: d.records.length }), () => {});
    return () => { live = false; };
  }, [pid, historyTick]);
  const pendingCount = pendingFor && pendingFor.pid === pid ? pendingFor.n : 0;
  const knowledgeData = fetchedKnowledge && fetchedKnowledge.pid === pid ? fetchedKnowledge.data : null;
  const knowledge = {
    summary: t.knowledge.summary(!!knowledgeData?.instructions, knowledgeData?.docs.length ?? 0),
    set: !!knowledgeData && (!!knowledgeData.instructions || knowledgeData.docs.length > 0),
    onOpen: () => setKnowledgeOpen(true),
  };
  // The files the project's AI answers from: the latest version of each (the list is most recently uploaded first).
  const latestFiles = useMemo(() => {
    const seen = new Set<string>();
    return inProject.filter((w) => w.status !== "failed" && !seen.has(w.name.toLowerCase()) && !!seen.add(w.name.toLowerCase()));
  }, [inProject]);

  function switchProject(id: string) {
    setProjectId(id);
    try { localStorage.setItem(PROJECT_KEY, id); } catch {}
    setChosenId(null); setGraphFocus(null); setSheetFocus(null); setNotice(null);
  }
  const fileCounts = useMemo(() => {
    const nameOf = new Map(workbooks.map((w) => [w.id, w.name.toLowerCase()]));
    return Object.fromEntries(projects.map((p) => [p.id, new Set(p.workbooks.flatMap((id) => nameOf.get(id) ?? [])).size]));
  }, [workbooks, projects]);
  const projectControls: ProjectControls = {
    projects,
    fileCounts,
    activeProject: project?.id ?? DEFAULT_PROJECT,
    onProject: switchProject,
    onCreateProject: async (name) => { const p = await api.createProject(name); await refresh(); switchProject(p.id); },
    onRenameProject: async (id, name) => { await api.renameProject(id, name); await refresh(); },
    onDeleteProject: async (id) => {
      await api.deleteProject(id);
      setChats((c) => { const next = { ...c }; delete next[`project:${id}`]; return next; });
      switchProject(DEFAULT_PROJECT);
      await refresh();
    },
    onMove: async (wid, to) => { await api.moveToProject(to, wid, project?.id); await refresh(); },
  };

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

  // After a multi-file upload: open the last file that parsed, and sum up how it went.
  async function uploadedMany(results: IngestResult[], refused: number) {
    const ok = results.filter((r) => !r.failed);
    await refresh(ok.at(-1)?.id ?? results.at(-1)?.id);
    const dup = results.filter((r) => r.duplicate).length;
    setNotice({ text: t.upload.many(results.length - dup, dup, results.length - ok.length, refused) });
  }

  async function remove(id: string) {
    await api.remove(id);
    await refresh();
  }

  useEffect(() => {
    // The landing page's "try the sample" button links to /app/?sample=1.
    if (new URLSearchParams(window.location.search).has("sample")) {
      window.history.replaceState(null, "", window.location.pathname);
      api.sample(loadProject()).then(({ id }) => refresh(id), () => refresh());
    } else {
      refresh();
    }
  }, [refresh]);

  useEffect(() => {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(chats));
    } catch {}
  }, [chats]);

  const activeWorkbook = inProject.find((w) => w.id === activeId);
  const selectWorkbook = (id: string) => { setChosenId(id); setGraphFocus(null); setSheetFocus(null); setNotice(null); };
  // A cell named by a chat answer, in the workbook it is in (in a project chat, maybe not the one on screen).
  const openCell = (cell: string, wid?: string) => {
    if (wid && wid !== activeId) selectWorkbook(wid);
    setSheetFocus(cell); setView("sheets");
  };
  const traceCell = (cell: string, wid?: string) => {
    if (wid && wid !== activeId) selectWorkbook(wid);
    setGraphFocus(cell); setView("graph");
  };
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
    if (!activeId || !project || !chatKey) return;
    const wid = activeId, pid = project.id, key = chatKey;
    const push = (m: Message) => setChats((c) => ({ ...c, [key]: [...(c[key] ?? []), m] }));
    push({ id: uid(), role: "user", content: text });
    setPending(true);
    setLiveSteps([]);
    try {
      // Steps arrive as the agent takes them; a short floor keeps the typing indicator from flickering.
      const stream = api.projectChatStream(pid, text, lang, wid, (s) => setLiveSteps((prev) => [...prev, s]));
      const [res] = await Promise.all([stream, new Promise((r) => setTimeout(r, 450))]);
      const { reply, cells, ...meta } = res;
      push({ id: uid(), role: "assistant", content: reply, cells, meta, workbook: res.workbook ?? wid });
      if (res.records?.length) setHistoryTick((n) => n + 1);
    } catch (e) {
      push({ id: uid(), role: "assistant", content: t.app.error((e as Error).message), error: true });
    } finally {
      setPending(false);
      setLiveSteps([]);
    }
  }

  useEffect(() => {
    api.llm().then(setLlm).catch(() => setLlm(null));
  }, []);

  return (
    <div className="relative flex h-full overflow-hidden">

      <Sidebar
        {...projectControls}
        knowledge={knowledge}
        workbooks={inProject}
        activeId={activeId}
        panel={panel}
        view={view}
        pending={pendingCount}
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
        versions={inProject.filter((w) => w.name.toLowerCase() === activeWorkbook?.name.toLowerCase())}
        onOpenCell={(cell) => { setPanel(null); setSheetFocus(cell); setView("sheets"); }}
        onClose={() => setPanel(null)}
      />

      <main className="relative flex min-w-0 flex-1 flex-col">
        {/* Always mounted, so screen readers announce the notices put in it (WCAG 4.1.3) */}
        <div aria-live="polite">
        {backendDown && (
          <div className="mx-6 mt-4 rounded-md border border-amber-400/20 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-100">
            {t.app.backendDown}
          </div>
        )}
        {notice && (
          <div className="mx-6 mt-4 flex items-start gap-3 rounded-md border border-cyan-300/20 bg-cyan-400/10 px-4 py-2.5 text-sm text-cyan-50">
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
            <button onClick={() => setNotice(null)} aria-label={t.app.close} className="text-cyan-200/70 hover:text-white">✕</button>
          </div>
        )}
        </div>
        {project && (
          <ContextBar
            // Views about one file show where they are (project › file); the chat is about the whole project and says
            // so in its own header, so there the bar is only for small screens, where the sidebar is hidden.
            className={view === "chat" ? "flex md:hidden" : "flex"}
            workbooks={inProject}
            active={activeWorkbook}
            onSelect={selectWorkbook}
            projectControls={projectControls}
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
            onTraceGraph={(cell) => traceCell(cell)}
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
        ) : view === "twin" ? (
          <TwinView
            projectId={project?.id ?? null}
            workbookId={activeId}
            versions={inProject.filter((w) => w.name.toLowerCase() === activeWorkbook?.name.toLowerCase())}
            openRef={twinRef}
            onOpened={() => setTwinRef(null)}
            onOpenCell={(cell) => { setSheetFocus(cell); setView("sheets"); }}
            onNewVersion={async (id) => { await refresh(id); setNotice(null); }}
            onHistoryChanged={() => setHistoryTick((n) => n + 1)}
          />
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
            messages={(chatKey && chats[chatKey]) || []}
            pending={pending}
            liveSteps={liveSteps}
            llm={llm}
            onSend={send}
            onNewData={() => setUploadOpen(true)}
            onShowGraph={traceCell}
            onOpenSource={openCell}
            knowledge={project ? knowledge : undefined}
            context={project ? {
              project, files: latestFiles,
              activeFile: latestFiles.find((w) => w.name.toLowerCase() === activeWorkbook?.name.toLowerCase())?.id ?? null,
            } : undefined}
            onNavigate={(to) => (to === "exports" ? setPanel("exports") : setView(to))}
            onShowReport={() => setReportOpen(true)}
            onOpenRecord={(ref) => { setTwinRef(ref); setView("twin"); }}
          />
        )}
      </main>

      <KnowledgePanel open={knowledgeOpen} project={project} knowledge={knowledgeData}
        onClose={() => setKnowledgeOpen(false)} onChanged={loadKnowledge} />
      <UploadDialog open={uploadOpen} project={project?.id ?? DEFAULT_PROJECT} onClose={() => setUploadOpen(false)} onDone={uploaded} onDoneMany={uploadedMany} />
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

/** Where the view is: the project (with its switcher) › the file on screen (with a switcher among the project's files). */
function ContextBar({ className, workbooks, active, onSelect, projectControls }: {
  className: string; workbooks: Workbook[]; active?: Workbook; onSelect: (id: string) => void; projectControls: ProjectControls;
}) {
  const { t } = useI18n();
  const label = (w: Workbook) => (w.versions > 1 ? `${w.name} · v${w.version}/${w.versions}` : w.name);
  return (
    <nav aria-label="breadcrumb" className={`${className} mx-4 mt-4 min-w-0 items-center gap-1 rounded-md border border-white/10 bg-white/[0.03] px-2 py-1.5 text-sm md:mx-6`}>
      <ProjectSwitcher {...projectControls} variant="crumb" />
      <ChevronRight className="size-4 shrink-0 text-zinc-600" />
      {active ? (
        <label className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md px-1.5 py-1 hover:bg-white/[0.06]">
          <FileSpreadsheet className="size-4 shrink-0 text-emerald-300" />
          <select
            value={active.id}
            onChange={(e) => onSelect(e.target.value)}
            aria-label={t.sidebar.current}
            title={`${active.name} · ${active.id}`}
            className="min-w-0 flex-1 cursor-pointer truncate bg-transparent font-medium text-white outline-none focus-visible:ring-1 focus-visible:ring-blue-400/50"
          >
            {workbooks.map((w) => (
              <option key={w.id} value={w.id} className="bg-zinc-900 text-zinc-100">{label(w)}</option>
            ))}
          </select>
        </label>
      ) : (
        <span className="px-1.5 text-zinc-500">{t.projects.noFiles}</span>
      )}
    </nav>
  );
}
