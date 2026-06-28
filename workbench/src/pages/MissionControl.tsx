import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Empty, PageHeader, StatusBadge, fmt, fmtRelative } from "../components/ui";
import { IconActivity, IconClock, IconZap, IconStar, IconPlug } from "../components/icons";

function StatCard({
  label,
  value,
  sub,
  accent,
  icon,
  href,
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: string;
  icon: React.ReactNode;
  href?: string;
}) {
  return (
    <div className="card flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-muted">{label}</span>
        <span className="opacity-40">{icon}</span>
      </div>
      <div className={`text-3xl font-semibold tabular-nums ${accent ?? "text-white"}`}>
        {value}
      </div>
      {sub && href ? (
        <Link className={`text-xs hover:underline ${accent ?? "text-muted"}`} to={href}>
          {sub}
        </Link>
      ) : sub ? (
        <span className="text-xs text-muted">{sub}</span>
      ) : null}
    </div>
  );
}

function ConnectorStatus() {
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: api.listConnectors });
  const items = connectors.data ?? [];
  const healthy = items.filter((c) => c.status === "healthy").length;
  const total = items.length;

  const statusColor =
    total === 0 ? "text-zinc-400"
    : healthy === total ? "text-emerald-300"
    : healthy === 0 ? "text-rose-300"
    : "text-amber-300";

  return (
    <div className="card flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-muted">Connectors</span>
        <span className="opacity-40"><IconPlug /></span>
      </div>
      <div className={`text-3xl font-semibold tabular-nums ${statusColor}`}>
        {total === 0 ? "—" : `${healthy}/${total}`}
      </div>
      <div className="flex flex-wrap gap-1 min-h-[20px]">
        {items.length === 0 ? (
          <span className="text-xs text-muted">none configured</span>
        ) : (
          items.map((c) => (
            <span key={c.id} className="chip text-[11px]">
              {c.kind} <StatusBadge status={c.status} />
            </span>
          ))
        )}
      </div>
    </div>
  );
}

export function MissionControl() {
  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: api.listRuns,
    refetchInterval: 3000,
  });
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: api.listSchedules });
  const proposed = useQuery({
    queryKey: ["skills-proposed-count"],
    queryFn: () => api.listProposed(1, 1),
    refetchInterval: 10_000,
  });

  const runsData = runs.data ?? [];
  const activeRuns = runsData.filter((r) => ["queued", "running", "reporting"].includes(r.status)).length;
  const lastRun = runsData[0];
  const proposedCount = proposed.data?.total ?? 0;

  return (
    <div>
      <PageHeader
        title="Mission Control"
        sub="Live runs, connectors, and schedules · press ⌘K to dispatch"
      />

      <div className="mb-6 grid grid-cols-4 gap-4">
        <ConnectorStatus />

        <StatCard
          label="Schedules"
          value={schedules.data?.length ?? "—"}
          icon={<IconClock />}
          sub={schedules.data?.filter((s) => s.enabled).length
            ? `${schedules.data!.filter((s) => s.enabled).length} active`
            : "none active"}
        />

        <StatCard
          label="Active runs"
          value={activeRuns}
          icon={<IconActivity />}
          accent={activeRuns > 0 ? "text-sky-300" : "text-white"}
          sub={lastRun ? `Last: ${fmtRelative(lastRun.started_at ?? lastRun.created_at)}` : undefined}
        />

        <StatCard
          label="Proposed skills"
          value={proposedCount === 0 ? "—" : proposedCount}
          icon={<IconStar />}
          accent={proposedCount > 0 ? "text-amber-300" : undefined}
          sub={proposedCount > 0 ? `${proposedCount} awaiting review →` : "none pending"}
          href={proposedCount > 0 ? "/skills/proposed" : undefined}
        />
      </div>

      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-medium text-muted">Recent runs</h2>
        <span className="flex items-center gap-1.5 text-xs text-muted">
          <IconZap size={12} className="text-sky-500" />
          live · refreshes every 3s
        </span>
      </div>

      {runsData.length === 0 ? (
        <Empty>No runs yet. Press ⌘K to start an investigation.</Empty>
      ) : (
        <div className="card overflow-hidden p-0">
          <table className="w-full">
            <thead>
              <tr className="border-b border-edge/60">
                <th className="th">Run</th>
                <th className="th">Status</th>
                <th className="th">Model</th>
                <th className="th">Started</th>
                <th className="th">Finished</th>
              </tr>
            </thead>
            <tbody>
              {runsData.map((r) => (
                <tr key={r.id} className="tr-hover">
                  <td className="td">
                    <Link className="font-mono text-sky-400 hover:text-sky-300 hover:underline transition-colors" to={`/runs/${r.id}`}>
                      {r.id.slice(0, 8)}
                    </Link>
                  </td>
                  <td className="td">
                    <StatusBadge status={r.status} />
                  </td>
                  <td className="td text-muted text-xs">{r.model ?? "—"}</td>
                  <td className="td text-muted text-xs">{fmt(r.started_at)}</td>
                  <td className="td text-muted text-xs">{fmt(r.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
