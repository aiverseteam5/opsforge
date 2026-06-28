import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, RcaReport } from "../api";
import { StreamEvent, streamRunEvents } from "../sse";
import { PageHeader, StatusBadge, fmt } from "../components/ui";

const KIND_LABEL: Record<string, string> = {
  thought: "💭 thought",
  tool_call: "→ tool call",
  tool_result: "← tool result",
  evidence: "🔎 evidence",
  proposal: "🛠 proposal",
  report: "📋 report",
  error: "⚠ error",
};

function summarize(ev: StreamEvent): string {
  const p = ev.data?.payload ?? {};
  if (ev.event === "thought") return p.text ?? `context (${p.context_chars ?? 0} chars)`;
  if (ev.event === "tool_call") return `${p.tool} ${JSON.stringify(p.params ?? {})}`;
  if (ev.event === "tool_result") return `${p.tool}${p.is_error ? " (error)" : ""}`;
  if (ev.event === "proposal") return `${p.tool ?? ""} → ${p.state ?? ""}`;
  if (ev.event === "report") return p.hypothesis ?? "report submitted";
  if (ev.event === "error") return JSON.stringify(p);
  return JSON.stringify(p);
}

function Report({ report }: { report: RcaReport }) {
  return (
    <div className="card space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">RCA</span>
        <StatusBadge status={report.confidence} />
      </div>
      <div className="text-base">{report.hypothesis}</div>
      <div>
        <div className="mb-1 text-xs uppercase text-muted">Evidence</div>
        <ol className="ml-5 list-decimal space-y-1 text-sm">
          {report.evidence.map((e, i) => (
            <li key={i}>
              {e.claim}
              {e.source_tool && <span className="text-muted"> · {e.source_tool}</span>}
              {e.raw_ref && <code className="ml-1 text-sky-400">{e.raw_ref}</code>}
            </li>
          ))}
        </ol>
      </div>
      {report.proposals.length > 0 && (
        <div>
          <div className="mb-1 text-xs uppercase text-muted">Suggested fixes (not executed)</div>
          {report.proposals.map((p) => (
            <div key={p} className="chip">proposal {p.slice(0, 8)}</div>
          ))}
        </div>
      )}
      {report.missing_evidence && (
        <div className="text-sm text-amber-300">Missing: {report.missing_evidence}</div>
      )}
      {report.next_checks.length > 0 && (
        <div>
          <div className="mb-1 text-xs uppercase text-muted">Next checks</div>
          <ul className="ml-5 list-disc text-sm">
            {report.next_checks.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

export function RunDetail() {
  const { id } = useParams();
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const run = useQuery({ queryKey: ["run", id], queryFn: () => api.getRun(id!), enabled: !!id });

  useEffect(() => {
    if (!id) return;
    setEvents([]);
    const stop = streamRunEvents(
      id,
      (e) => setEvents((prev) => [...prev, e]),
      () => run.refetch(),
    );
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const r = run.data;
  return (
    <div>
      <PageHeader
        title={`Run ${id?.slice(0, 8)}`}
        sub={r ? `${r.trigger?.kind ?? ""} · ${r.model ?? "default model"}` : ""}
        right={
          r && ["queued", "running"].includes(r.status) ? (
            <button className="btn" onClick={() => api.cancelRun(id!).then(() => run.refetch())}>
              Cancel
            </button>
          ) : (
            r && <StatusBadge status={r.status} />
          )
        }
      />

      <div className="grid grid-cols-2 gap-5">
        <div>
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-medium text-muted">Investigation timeline</h2>
            <Link to={`/runs/${id}/timeline`} className="text-xs text-sky-400 hover:underline">
              War room →
            </Link>
          </div>
          <div className="card space-y-2 p-3">
            {events.length === 0 && <div className="text-sm text-muted">Waiting for events…</div>}
            {events.map((ev, i) => (
              <div key={i} className="flex gap-2 border-b border-edge/40 pb-2 text-sm last:border-0">
                <span className="w-28 shrink-0 text-muted">{KIND_LABEL[ev.event] ?? ev.event}</span>
                <span className="break-all">{summarize(ev)}</span>
              </div>
            ))}
          </div>
        </div>

        <div>
          <h2 className="mb-2 text-sm font-medium text-muted">Report</h2>
          {r?.report_json ? (
            <Report report={r.report_json} />
          ) : (
            <div className="card text-sm text-muted">
              {r?.status === "done" ? "No structured report." : "Investigation in progress…"}
            </div>
          )}
          {r && (
            <div className="mt-3 text-xs text-muted">
              tokens in/out: {r.tokens_in ?? 0}/{r.tokens_out ?? 0} · finished {fmt(r.finished_at)}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
