/**
 * C3: Chat workbench — the "Cursor for Ops" chat-first interface.
 *
 * Three-column layout:
 *   - Left: conversation list (create + select)
 *   - Center: message thread with run-event streaming
 *   - Right: (future) context panel
 *
 * User sends a natural-language message → NL dispatch fires → run is queued →
 * assistant message shows the run link → client streams run events inline.
 */

import React, { useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, Message, AssistantPayload } from "../api";
import { streamRunEvents } from "../sse";
import { IconChat, IconSend, IconLoader, IconX } from "../components/icons";

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

function parseAssistantPayload(content: string): AssistantPayload | null {
  try {
    return JSON.parse(content) as AssistantPayload;
  } catch {
    return null;
  }
}

function RunStatus({ runId }: { runId: string }) {
  const [events, setEvents] = useState<{ kind: string; summary: string }[]>([]);
  const [done, setDone] = useState(false);

  useEffect(() => {
    const stop = streamRunEvents(
      runId,
      (ev) => {
        const payload = ev.data?.payload ?? {};
        const kind = ev.event;
        const summary =
          payload.summary || payload.tool || payload.hypothesis || kind;
        setEvents((prev) => [...prev, { kind, summary: String(summary).slice(0, 120) }]);
      },
      () => setDone(true),
    );
    return stop;
  }, [runId]);

  return (
    <div className="mt-2 rounded border border-edge/50 bg-ink/40 px-3 py-2 text-xs">
      <a
        href={`/runs/${runId}/timeline`}
        className="text-sky-400 hover:underline font-mono"
      >
        Run {runId.slice(0, 8)}
      </a>
      {!done && (
        <span className="ml-2 text-muted animate-pulse">• running</span>
      )}
      {done && <span className="ml-2 text-green-400">• done</span>}
      {events.length > 0 && (
        <div className="mt-1 space-y-0.5 text-muted">
          {events.slice(-5).map((e, i) => (
            <div key={i} className="truncate">
              <span className="text-zinc-500">[{e.kind}]</span> {e.summary}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AssistantBubble({ message }: { message: Message }) {
  const parsed = parseAssistantPayload(message.content);

  if (!parsed) {
    return (
      <div className="rounded-lg bg-panel px-4 py-3 text-sm">{message.content}</div>
    );
  }

  if (parsed.type === "run_dispatched") {
    return (
      <div className="rounded-lg bg-panel px-4 py-3 text-sm">
        <span className="text-sky-400">Dispatching run...</span>
        <RunStatus runId={parsed.run_id} />
      </div>
    );
  }

  if (parsed.type === "ambiguous") {
    return (
      <div className="rounded-lg bg-panel px-4 py-3 text-sm">
        <span className="text-amber-400">Ambiguous request — did you mean one of these?</span>
        <ul className="mt-2 space-y-1 list-disc list-inside text-muted">
          {parsed.candidates.map((c) => (
            <li key={c.slug}>
              <span className="font-mono text-zinc-300">{c.slug}</span> — {c.name}
            </li>
          ))}
        </ul>
      </div>
    );
  }

  if (parsed.type === "error") {
    return (
      <div className="rounded-lg border border-red-800/50 bg-red-950/30 px-4 py-3 text-sm text-red-300">
        {parsed.detail}
      </div>
    );
  }

  return (
    <div className="rounded-lg bg-panel px-4 py-3 text-sm">{message.content}</div>
  );
}

function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[80%] ${isUser ? "order-1" : ""}`}>
        {isUser ? (
          <div className="rounded-lg bg-sky-900/40 border border-sky-800/50 px-4 py-3 text-sm text-zinc-100">
            {message.content}
          </div>
        ) : (
          <AssistantBubble message={message} />
        )}
        <div className="mt-1 text-[10px] text-zinc-600">
          {new Date(message.created_at).toLocaleTimeString()}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Conversation list panel
// --------------------------------------------------------------------------- //

function ConversationList({
  selectedId,
  onSelect,
}: {
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const qc = useQueryClient();
  const { data: convs = [], isLoading } = useQuery({
    queryKey: ["conversations"],
    queryFn: api.listConversations,
    refetchInterval: 10_000,
  });

  const create = useMutation({
    mutationFn: () => api.createConversation(),
    onSuccess: (conv) => {
      qc.invalidateQueries({ queryKey: ["conversations"] });
      onSelect(conv.id);
    },
  });

  return (
    <aside className="flex w-60 flex-col border-r border-edge bg-panel">
      <div className="flex items-center justify-between border-b border-edge/60 px-3 py-3">
        <span className="text-xs font-semibold uppercase tracking-widest text-zinc-500">
          Conversations
        </span>
        <button
          className="btn-primary rounded px-2 py-1 text-xs"
          onClick={() => create.mutate()}
          disabled={create.isPending}
        >
          + New
        </button>
      </div>
      <div className="flex-1 overflow-y-auto py-1">
        {isLoading && (
          <div className="px-3 py-2 text-xs text-muted">Loading…</div>
        )}
        {convs.map((c) => (
          <button
            key={c.id}
            className={`w-full truncate px-3 py-2 text-left text-sm transition-colors ${
              selectedId === c.id
                ? "bg-edge text-white"
                : "text-muted hover:bg-edge/40 hover:text-zinc-200"
            }`}
            onClick={() => onSelect(c.id)}
          >
            <div className="truncate">{c.title}</div>
            <div className="text-[10px] text-zinc-600">
              {new Date(c.created_at).toLocaleDateString()}
            </div>
          </button>
        ))}
        {!isLoading && convs.length === 0 && (
          <div className="px-3 py-4 text-center text-xs text-muted">
            No conversations yet.
            <br />
            Click <strong>+ New</strong> to start.
          </div>
        )}
      </div>
    </aside>
  );
}

// --------------------------------------------------------------------------- //
// Thread panel
// --------------------------------------------------------------------------- //

function Thread({ conversationId }: { conversationId: string }) {
  const qc = useQueryClient();
  const bottomRef = useRef<HTMLDivElement>(null);
  const [input, setInput] = useState("");

  const { data: messages = [], isLoading } = useQuery({
    queryKey: ["messages", conversationId],
    queryFn: () => api.listMessages(conversationId),
    refetchInterval: 3_000,
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  const send = useMutation({
    mutationFn: (content: string) => api.sendMessage(conversationId, content),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["messages", conversationId] });
    },
  });

  const handleSend = () => {
    const text = input.trim();
    if (!text || send.isPending) return;
    setInput("");
    send.mutate(text);
  };

  const handleKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {isLoading && (
          <div className="text-center text-xs text-muted py-8">Loading…</div>
        )}
        {!isLoading && messages.length === 0 && (
          <div className="text-center text-sm text-muted py-16">
            <IconChat size={32} className="mx-auto mb-3 opacity-30" />
            <div>Ask OpsForge anything about your infrastructure.</div>
            <div className="mt-1 text-xs opacity-60">
              Try: "why is payment-svc throwing 5xx errors?"
            </div>
          </div>
        )}
        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}
        {send.isPending && (
          <div className="flex justify-start">
            <div className="rounded-lg bg-panel px-4 py-3 text-xs text-muted animate-pulse">
              Dispatching…
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="border-t border-edge/60 px-4 py-3">
        {send.isError && (
          <div className="mb-2 rounded border border-red-800/50 bg-red-950/30 px-3 py-2 text-xs text-red-300">
            {String(send.error)}
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            className="flex-1 resize-none rounded border border-edge/60 bg-ink px-3 py-2 text-sm
                       text-zinc-100 placeholder-zinc-600 focus:border-sky-700 focus:outline-none"
            rows={2}
            placeholder="Ask about your infrastructure… (Enter to send, Shift+Enter for newline)"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKey}
            disabled={send.isPending}
          />
          <button
            className="btn-primary flex items-center gap-1.5 px-3 py-2 text-sm disabled:opacity-40"
            onClick={handleSend}
            disabled={!input.trim() || send.isPending}
          >
            {send.isPending ? (
              <IconLoader size={14} className="animate-spin" />
            ) : (
              <IconSend size={14} />
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Main Chat page
// --------------------------------------------------------------------------- //

export function Chat() {
  const [selectedConvId, setSelectedConvId] = useState<string | null>(null);

  return (
    <div className="flex h-full -m-6 overflow-hidden">
      <ConversationList
        selectedId={selectedConvId}
        onSelect={setSelectedConvId}
      />

      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-2 border-b border-edge/60 px-4 py-3">
          <IconChat size={16} className="text-sky-400 opacity-80" />
          <span className="text-sm font-semibold">Chat</span>
          {selectedConvId && (
            <button
              className="ml-auto text-xs text-muted hover:text-zinc-300"
              onClick={() => setSelectedConvId(null)}
              title="Close conversation"
            >
              <IconX size={14} />
            </button>
          )}
        </div>

        {selectedConvId ? (
          <Thread key={selectedConvId} conversationId={selectedConvId} />
        ) : (
          <div className="flex flex-1 items-center justify-center text-center text-muted">
            <div>
              <IconChat size={40} className="mx-auto mb-4 opacity-20" />
              <div className="text-sm">Select a conversation or create a new one.</div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
