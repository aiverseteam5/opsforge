import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { Empty, PageHeader, fmt } from "../components/ui";

export function Audit() {
  const audit = useQuery({ queryKey: ["audit"], queryFn: api.listAudit });
  return (
    <div>
      <PageHeader title="Audit" sub="Immutable trail — append-only, never mutated" />
      {audit.data?.length === 0 ? (
        <Empty>No audit entries yet.</Empty>
      ) : (
        <div className="card overflow-hidden p-0">
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">#</th><th className="th">When</th><th className="th">Actor</th>
                <th className="th">Event</th><th className="th">Subject</th><th className="th">Detail</th>
              </tr>
            </thead>
            <tbody>
              {(audit.data ?? []).map((a) => (
                <tr key={a.seq}>
                  <td className="td text-muted">{a.seq}</td>
                  <td className="td text-muted">{fmt(a.created_at)}</td>
                  <td className="td">{a.actor}</td>
                  <td className="td"><code className="text-sky-400">{a.event}</code></td>
                  <td className="td text-muted">{a.subject_ref?.slice(0, 8) ?? "—"}</td>
                  <td className="td text-muted">{JSON.stringify(a.detail)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
