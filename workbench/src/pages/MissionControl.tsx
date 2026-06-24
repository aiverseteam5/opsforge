import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Empty, PageHeader, StatusBadge, fmt } from "../components/ui";

export function MissionControl() {
  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 3000, // live
  });
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: api.listConnectors });
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: api.listSchedules });

  return (
    <div>
      <PageHeader
        title="Mission Control"
        sub="Live runs, connectors, and schedules · press ⌘K to dispatch"
      />

      <div className="mb-5 grid grid-cols-3 gap-4">
        <div className="card">
          <div className="text-xs uppercase text-muted">Connectors</div>
          <div className="mt-2 flex flex-wrap gap-1">
            {(connectors.data ?? []).map((c) => (
              <span key={c.id} className="chip">
                {c.kind}:{c.name} <StatusBadge status={c.status} />
              </span>
            ))}
            {connectors.data?.length === 0 && <span className="text-sm text-muted">none</span>}
          </div>
        </div>
        <div className="card">
          <div className="text-xs uppercase text-muted">Schedules</div>
          <div className="mt-2 text-2xl">{schedules.data?.length ?? "—"}</div>
        </div>
        <div className="card">
          <div className="text-xs uppercase text-muted">Active runs</div>
          <div className="mt-2 text-2xl">
            {(runs.data ?? []).filter((r) => ["queued", "running"].includes(r.status)).length}
          </div>
        </div>
      </div>

      <h2 className="mb-2 text-sm font-medium text-muted">Recent runs</h2>
      {runs.data?.length === 0 ? (
        <Empty>No runs yet. Press ⌘K to start an investigation.</Empty>
      ) : (
        <div className="card overflow-hidden p-0">
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Run</th>
                <th className="th">Status</th>
                <th className="th">Model</th>
                <th className="th">Started</th>
                <th className="th">Finished</th>
              </tr>
            </thead>
            <tbody>
              {(runs.data ?? []).map((r) => (
                <tr key={r.id} className="hover:bg-edge/20">
                  <td className="td">
                    <Link className="text-sky-400 hover:underline" to={`/runs/${r.id}`}>
                      {r.id.slice(0, 8)}
                    </Link>
                  </td>
                  <td className="td"><StatusBadge status={r.status} /></td>
                  <td className="td text-muted">{r.model ?? "—"}</td>
                  <td className="td text-muted">{fmt(r.started_at)}</td>
                  <td className="td text-muted">{fmt(r.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
