import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ChatAction, ChatMessage } from "../api";
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
          : msgs.map((m) => <Bubble key={m.id} m={m} convId={convId} />)}
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

function Bubble({ m, convId }: { m: ChatMessage; convId: string }) {
  const mine = m.role === "user";
  return (
    <div className={`flex ${mine ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[75%] rounded-lg px-3 py-2 text-sm ${
        mine ? "bg-sky-900/40 text-sky-100" : "card"}`}>
        {mine ? m.content : <AssistantBody m={m} convId={convId} />}
      </div>
    </div>
  );
}

// G4: a legible state line for an action the agent took (or proposed).
function stateLabel(a: ChatAction): string {
  if (a.awaiting) return "⏸ awaiting your approval";
  if (a.state === "succeeded") return a.auto_executed ? "✓ auto-executed" : "✓ executed";
  if (a.state === "rolled_back") return "↩ undone";
  if (a.state === "rolling_back") return "↩ undoing…";
  if (a.state === "denied") return "✕ denied";
  if (a.state === "failed") return "⚠ failed";
  return a.state;
}

// G4 "legible & reversible": each action the agent took shows WHAT it did, the decision +
// WHY, and — only where the deterministic engine allows — inline approve/deny (gated) or
// undo (an executed reversible action). The buttons call the SAME action endpoints the
// approval gate uses; the surface cannot fabricate a state.
function ActionCard({ a, convId }: { a: ChatAction; convId: string }) {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["messages", convId] });
  const approve = useMutation({ mutationFn: () => api.approveAction(a.id), onSuccess: refresh });
  const deny = useMutation({ mutationFn: () => api.denyAction(a.id), onSuccess: refresh });
  const undo = useMutation({ mutationFn: () => api.undoAction(a.id), onSuccess: refresh });
  const busy = approve.isPending || deny.isPending || undo.isPending;
  const err = approve.error || deny.error || undo.error;
  const tone = a.awaiting ? "border-amber-500 bg-amber-950/20"
    : a.state === "rolled_back" || a.state === "denied" ? "border-zinc-600 bg-zinc-900/40"
    : a.state === "failed" ? "border-rose-600 bg-rose-950/20"
    : "border-sky-600 bg-sky-950/20";
  return (
    <div className={`rounded-md border-l-2 px-3 py-2 text-xs ${tone}`}>
      <div className="flex items-center justify-between gap-2">
        <code className="text-sky-300">{a.tool}</code>
        <span className="chip border-edge text-[10px]">{a.action_class}</span>
      </div>
      {a.target_ref && <div className="text-muted">target: {a.target_ref}</div>}
      <div className="mt-1">
        <span className="font-semibold">{stateLabel(a)}</span>
        {a.reason && <span className="text-muted"> — {a.reason}</span>}
      </div>
      {a.awaiting && (
        <div className="mt-2 flex gap-2">
          <button className="btn" disabled={busy} onClick={() => approve.mutate()}>Approve</button>
          <button className="btn" disabled={busy} onClick={() => deny.mutate()}>Deny</button>
        </div>
      )}
      {a.undoable && (
        <button className="btn mt-2" disabled={busy} onClick={() => undo.mutate()}>Undo</button>
      )}
      {err && <div className="mt-1 text-rose-300">{String(err)}</div>}
    </div>
  );
}

// A run's terminal statuses (mirrors the backend _TERMINAL in api/runs.py). A reopened thread
// whose run is already terminal must NOT re-open an SSE stream just to replay it.
const TERMINAL_RUN_STATUS = ["done", "failed", "cancelled"];

// The assistant turn streams the agent's run events live, then settles on the report
// (answer + evidence + confidence). Legible: you see what it did and the basis for it.
function AssistantBody({ m, convId }: { m: ChatMessage; convId: string }) {
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [done, setDone] = useState(TERMINAL_RUN_STATUS.includes(m.run_status ?? ""));
  const report = m.report;
  const actions = m.actions ?? [];

  useEffect(() => {
    if (!m.run_id || done) return;
    const stop = streamRunEvents(m.run_id, (ev) => setEvents((e) => [...e, ev]),
      () => setDone(true));
    return stop;
  }, [m.run_id, done]);

  const actionsBlock = actions.length > 0 && (
    <div className="mt-3 space-y-2">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Actions</div>
      {actions.map((a) => <ActionCard key={a.id} a={a} convId={convId} />)}
    </div>
  );

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
        {actionsBlock}
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
      {actionsBlock}
    </div>
  );
}
