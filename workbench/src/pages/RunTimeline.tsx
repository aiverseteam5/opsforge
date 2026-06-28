import { useQuery } from "@tanstack/react-query";
import { useParams, Link } from "react-router-dom";
import { api, TimelineEvent } from "../api";
import { PageHeader, Skeleton } from "../components/ui";
import { IconActivity, IconCpu, IconZap } from "../components/icons";

function kindIcon(kind: string): React.ReactNode {
  switch (kind) {
    case "tool_call":
    case "tool_result":
      return <IconCpu size={14} />;
    case "proposal":
      return <IconZap size={14} />;
    case "report":
      return <IconActivity size={14} />;
    default:
      return <span className="text-[10px] font-mono opacity-60">{kind[0].toUpperCase()}</span>;
  }
}

function kindColor(kind: string): string {
  switch (kind) {
    case "tool_call": return "text-sky-400";
    case "tool_result": return "text-emerald-400";
    case "proposal": return "text-amber-400";
    case "report": return "text-violet-400";
    case "error": return "text-rose-400";
    default: return "text-muted";
  }
}

function EventRow({ ev }: { ev: TimelineEvent }) {
  return (
    <div className="flex gap-3 py-2 border-b border-edge/30 last:border-0">
      <div className="flex flex-col items-center gap-1 pt-0.5 w-5 shrink-0">
        <span className={kindColor(ev.kind)}>{kindIcon(ev.kind)}</span>
        <div className="w-px flex-1 bg-edge/40" />
      </div>
      <div className="flex-1 min-w-0 pb-1">
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className={`text-xs font-mono font-semibold ${kindColor(ev.kind)}`}>
            {ev.kind}
          </span>
          <span className="text-[11px] text-muted">{ev.actor}</span>
          {ev.ts && (
            <span className="text-[11px] text-zinc-600 ml-auto tabular-nums">
              {new Date(ev.ts).toLocaleTimeString()}
            </span>
          )}
        </div>
        <p className="mt-0.5 text-sm text-zinc-300 break-words">{ev.summary}</p>
      </div>
    </div>
  );
}

export function RunTimeline() {
  const { id } = useParams<{ id: string }>();

  const timeline = useQuery({
    queryKey: ["run-timeline", id],
    queryFn: () => api.getRunTimeline(id!),
    enabled: !!id,
    refetchInterval: 5_000,
  });

  return (
    <div className="max-w-3xl space-y-4">
      <PageHeader title="Run Timeline" sub={id ? `Run ${id.slice(0, 8)}` : ""} />
      <Link to={`/runs/${id}`} className="text-sky-400 hover:underline text-xs block -mt-2">
        ← Back to run
      </Link>

      {timeline.isLoading && (
        <div className="space-y-2">
          {[...Array(5)].map((_, i) => <Skeleton key={i} className="h-12" />)}
        </div>
      )}

      {timeline.isError && (
        <div className="card border-rose-900/40 text-rose-300 text-sm">
          Failed to load timeline.
        </div>
      )}

      {timeline.data && (
        <div className="card p-0 divide-y divide-edge/20">
          {timeline.data.events.length === 0 ? (
            <div className="p-6 text-center text-sm text-muted">
              No events yet — run may still be starting.
            </div>
          ) : (
            <div className="p-4">
              {timeline.data.events.map((ev) => (
                <EventRow key={ev.seq} ev={ev} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
