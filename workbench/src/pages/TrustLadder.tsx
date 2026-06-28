import { useQuery } from "@tanstack/react-query";
import { api, TrustLadderEntry } from "../api";
import { PageHeader, Skeleton } from "../components/ui";

function GraduationBar({ clean, threshold }: { clean: number; threshold: number }) {
  const pct = Math.min(100, Math.round((clean / Math.max(threshold, 1)) * 100));
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 flex-1 rounded-full bg-edge overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${pct >= 100 ? "bg-emerald-400" : "bg-sky-500"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-[11px] tabular-nums text-muted w-10 text-right">
        {clean}/{threshold}
      </span>
    </div>
  );
}

function classChip(cls: string) {
  const map: Record<string, string> = {
    read_only: "bg-sky-900/40 text-sky-300",
    reversible: "bg-amber-900/40 text-amber-300",
    destructive: "bg-rose-900/40 text-rose-300",
  };
  return (
    <span className={`chip text-[11px] ${map[cls] ?? "bg-zinc-800 text-zinc-400"}`}>
      {cls.replace("_", " ")}
    </span>
  );
}

function ToolRow({ entry }: { entry: TrustLadderEntry }) {
  return (
    <tr className="tr-hover border-b border-edge/30">
      <td className="py-2.5 px-3 text-sm font-mono text-zinc-200">{entry.tool}</td>
      <td className="py-2.5 px-3">{classChip(entry.action_class)}</td>
      <td className="py-2.5 px-3 text-sm tabular-nums text-right text-zinc-300">
        {entry.total_executions}
      </td>
      <td className="py-2.5 px-3 text-sm tabular-nums text-right text-rose-300">
        {entry.rollbacks}
      </td>
      <td className="py-2.5 px-3 min-w-[140px]">
        {entry.action_class === "destructive" ? (
          <span className="text-[11px] text-zinc-600">never gradable</span>
        ) : (
          <GraduationBar clean={entry.clean_executions} threshold={entry.graduation_threshold} />
        )}
      </td>
      <td className="py-2.5 px-3 text-center">
        {entry.eligible_for_graduation ? (
          <span className="chip bg-emerald-900/40 text-emerald-300 text-[11px]">eligible</span>
        ) : null}
      </td>
    </tr>
  );
}

export function TrustLadder() {
  const ladder = useQuery({
    queryKey: ["trust-ladder"],
    queryFn: api.getTrustLadder,
    refetchInterval: 30_000,
  });

  return (
    <div className="space-y-4">
      <PageHeader
        title="Trust Ladder"
        sub={`Graduation threshold: ${ladder.data?.graduation_threshold ?? "…"} clean executions`}
      />

      {ladder.isLoading && (
        <div className="space-y-2">
          {[...Array(4)].map((_, i) => <Skeleton key={i} className="h-10" />)}
        </div>
      )}

      {ladder.data && ladder.data.items.length === 0 && (
        <div className="card p-8 text-center text-sm text-muted">
          No actions executed yet. Actions appear here once the trust ladder is used.
        </div>
      )}

      {ladder.data && ladder.data.items.length > 0 && (
        <div className="card p-0 overflow-hidden">
          <table className="w-full text-left">
            <thead>
              <tr className="border-b border-edge/40 bg-panel/60">
                <th className="py-2 px-3 text-[11px] font-semibold uppercase tracking-wide text-muted">Tool</th>
                <th className="py-2 px-3 text-[11px] font-semibold uppercase tracking-wide text-muted">Class</th>
                <th className="py-2 px-3 text-[11px] font-semibold uppercase tracking-wide text-muted text-right">Executions</th>
                <th className="py-2 px-3 text-[11px] font-semibold uppercase tracking-wide text-muted text-right">Rollbacks</th>
                <th className="py-2 px-3 text-[11px] font-semibold uppercase tracking-wide text-muted">Progress</th>
                <th className="py-2 px-3 text-[11px] font-semibold uppercase tracking-wide text-muted text-center">Status</th>
              </tr>
            </thead>
            <tbody>
              {ladder.data.items.map((entry) => (
                <ToolRow key={entry.tool} entry={entry} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
