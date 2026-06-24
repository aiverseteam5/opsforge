import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ChatMessage } from "../api";
import { streamRunEvents, StreamEvent } from "../sse";
import { Empty, ErrorState, Loading, PageHeader } from "../components/ui";

// The conversational front door (G1). The operator types; a turn spawns the existing agent
// run; its work streams back legibly (thought / tool / report) — the Cursor feel.
export function Chat() {
  const qc = useQueryClient();
  const [convId, setConvId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const conversations = useQuery({ queryKey: ["conversations"], queryFn: () => api.listConversations() });
  // auto-select / create the first conversation
  useEffect(() => {
    if (convId || conversations.isLoading) return;
    const first = conversations.data?.[0];
    if (first) setConvId(first.id);
  }, [conversations.data, conversations.isLoading, convId]);

  const newConv = useMutation({
    mutationFn: () => api.createConversation(),
    onSuccess: (c) => { qc.invalidateQueries({ queryKey: ["conversations"] }); setConvId(c.id); },
  });

  return (
    <div className="flex h-[calc(100vh-3rem)] gap-4">
      <aside className="w-56 shrink-0 space-y-2 overflow-auto">
        <button className="btn w-full" onClick={() => newConv.mutate()}>+ New chat</button>
        {(conversations.data ?? []).map((c) => (
          <button key={c.id}
            className={`block w-full truncate rounded-md px-3 py-2 text-left text-sm ${
              c.id === convId ? "bg-edge text-white" : "text-muted hover:bg-edge/40"}`}
            onClick={() => setConvId(c.id)}>
            {c.title}
          </button>
        ))}
      </aside>
      <main className="flex flex-1 flex-col">
        <PageHeader title="Assistant" sub="Ask about your operations — answers grounded in validated knowledge" />
        {convId ? <Thread key={convId} convId={convId} draft={draft} setDraft={setDraft} />
          : <Empty>Start a new chat to begin.</Empty>}
      </main>
    </div>
  );
}

function Thread({ convId, draft, setDraft }: {
  convId: string; draft: string; setDraft: (s: string) => void;
}) {
  const qc = useQueryClient();
  const messages = useQuery({
    queryKey: ["messages", convId], queryFn: () => api.getChatMessages(convId),
  });
  const send = useMutation({
    mutationFn: (content: string) => api.postChatMessage(convId, content),
    onSuccess: () => { setDraft(""); qc.invalidateQueries({ queryKey: ["messages", convId] }); },
  });
  const msgs = messages.data ?? [];
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => { bottom.current?.scrollIntoView?.(); }, [msgs.length]);

  return (
    <>
      <div className="flex-1 space-y-3 overflow-auto py-2">
        {messages.isLoading ? <Loading what="messages" />
          : messages.isError ? <ErrorState error={messages.error} />
          : msgs.length === 0 ? <Empty>Say hello — ask what’s inconsistent in your processes.</Empty>
          : msgs.map((m) => <Bubble key={m.id} m={m} />)}
        <div ref={bottom} />
      </div>
      <form className="mt-2 flex gap-2"
        onSubmit={(e) => { e.preventDefault(); if (draft.trim()) send.mutate(draft.trim()); }}>
        <input className="input flex-1" placeholder="Ask the operations assistant…"
          value={draft} onChange={(e) => setDraft(e.target.value)} disabled={send.isPending} />
        <button className="btn" disabled={!draft.trim() || send.isPending}>
          {send.isPending ? "…" : "Send"}
        </button>
      </form>
      {send.isError && <div className="mt-1 text-xs text-rose-300">{String(send.error)}</div>}
    </>
  );
}

function Bubble({ m }: { m: ChatMessage }) {
  const mine = m.role === "user";
  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[75%] rounded-lg px-3 py-2 text-sm ${
        mine ? "bg-sky-900/40 text-sky-100" : "card"}`}>
        {mine ? m.content : <AssistantBody m={m} />}
      </div>
    </div>
  );
}

// A run's terminal statuses (mirrors the backend _TERMINAL in api/runs.py). A reopened thread
// whose run is already terminal must NOT re-open an SSE stream just to replay it.
const TERMINAL_RUN_STATUS = ["done", "failed", "cancelled"];

// The assistant turn streams the agent's run events live, then settles on the report
// (answer + evidence + confidence). Legible: you see what it did and the basis for it.
function AssistantBody({ m }: { m: ChatMessage }) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [done, setDone] = useState(TERMINAL_RUN_STATUS.includes(m.run_status ?? ""));
  const report = m.report;

  useEffect(() => {
    if (!m.run_id || done) return;
    const stop = streamRunEvents(m.run_id, (ev) => setEvents((e) => [...e, ev]),
      () => setDone(true));
    return stop;
  }, [m.run_id, done]);

  if (report?.hypothesis) {
    return (
      <div>
        <div className="whitespace-pre-wrap">{report.hypothesis}</div>
        {report.confidence && (
          <span className={`chip mt-1 inline-block ${report.confidence === "low"
            ? "text-rose-200 border-rose-600 bg-rose-950/40 font-semibold"
            : "text-zinc-300 border-edge"}`}>
            {report.confidence === "low" ? "⚠ " : ""}confidence: {report.confidence}
          </span>
        )}
        {Array.isArray(report.evidence) && report.evidence.length > 0 && (
          <details className="mt-2 text-xs text-muted">
            <summary className="cursor-pointer">evidence ({report.evidence.length})</summary>
            <ul className="mt-1 list-inside list-disc">
              {report.evidence.map((e: any, i: number) => (
                <li key={i}>{e.claim ?? JSON.stringify(e)}{e.raw_ref ? ` — ${e.raw_ref}` : ""}</li>
              ))}
            </ul>
          </details>
        )}
      </div>
    );
  }
  // still working: show the streamed agent activity
  return (
    <div className="space-y-1 text-xs text-muted">
      {events.length === 0 ? <span>thinking…</span>
        : events.slice(-6).map((ev, i) => (
          <div key={i}><span className="text-sky-400">{ev.event}</span>{" "}
            {typeof ev.data === "string" ? ev.data
              : (ev.data?.thought ?? ev.data?.tool ?? JSON.stringify(ev.data)).toString().slice(0, 140)}
          </div>
        ))}
    </div>
  );
}
