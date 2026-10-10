"use client";

import { useCallback, useEffect, useState } from "react";
import { Chat, type Message } from "@/components/Chat";
import { ExportsPanel } from "@/components/ExportsPanel";
import { GraphView } from "@/components/GraphView";
import { ChartsView } from "@/components/ChartsView";
import { Sidebar, type View } from "@/components/Sidebar";
import { UploadDialog } from "@/components/UploadDialog";
import { api, type Summary, type Workbook } from "@/lib/api";
import { I18nProvider, useI18n } from "@/lib/i18n";

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
  const [backendDown, setBackendDown] = useState(false);

  const refresh = useCallback(
    (select?: string) =>
      api.workbooks().then(
        (list) => {
          setWorkbooks(list);
          setBackendDown(false);
          setActiveId((cur) => select ?? cur ?? list[0]?.id ?? null);
        },
        () => setBackendDown(true),
      ),
    [],
  );

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(chats));
    } catch {}
  }, [chats]);

  // Summary of the active workbook; a stale one (other id) counts as not loaded yet.
  const summary = loaded && loaded.id === activeId ? loaded : null;
  useEffect(() => {
    if (!activeId) return;
    let live = true;
    api.summary(activeId).then((s) => live && setLoaded(s)).catch(() => {});
    return () => { live = false; };
  }, [activeId]);

  async function send(text: string) {
    if (!activeId) return;
    const wid = activeId;
    const push = (m: Message) => setChats((c) => ({ ...c, [wid]: [...(c[wid] ?? []), m] }));
    push({ id: uid(), role: "user", content: text });
    setPending(true);
    try {
      // Short floor so the typing indicator reads as a reply, not a flicker.
      const [res] = await Promise.all([api.chat(wid, text, lang), new Promise((r) => setTimeout(r, 450))]);
      push({ id: uid(), role: "assistant", content: res.reply, cells: res.cells });
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
        onSelect={(id) => { setActiveId(id); setGraphFocus(null); }}
        onNewData={() => setUploadOpen(true)}
        onToggleExports={() => setPanel((p) => (p === "exports" ? null : "exports"))}
      />

      <ExportsPanel
        open={panel === "exports"}
        workbookId={activeId}
        workbookName={summary?.name}
        onClose={() => setPanel(null)}
      />

      <main className="relative flex min-w-0 flex-1 flex-col">
        {backendDown && (
          <div className="mx-6 mt-4 rounded-xl border border-amber-400/20 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-100">
            {t.app.backendDown}
          </div>
        )}
        {view === "charts" ? (
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
          />
        )}
      </main>

      <UploadDialog open={uploadOpen} onClose={() => setUploadOpen(false)} onDone={(id) => refresh(id)} />
    </div>
  );
}
