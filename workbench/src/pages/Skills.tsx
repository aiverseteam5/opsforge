import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { Empty, PageHeader, StatusBadge } from "../components/ui";

export function Skills() {
  const skills = useQuery({ queryKey: ["skills"], queryFn: api.listSkills });
  return (
    <div>
      <PageHeader title="Skills" sub="Installed capability packs and their trust surface" />
      {skills.data?.length === 0 && <Empty>No skills installed.</Empty>}
      <div className="grid grid-cols-2 gap-4">
        {(skills.data ?? []).map((s) => (
          <div key={s.slug} className="card space-y-2">
            <div className="flex items-center justify-between">
              <div className="font-medium">{s.name}</div>
              <StatusBadge status={s.enabled ? "healthy" : "cancelled"} />
            </div>
            <div className="text-xs text-muted">
              {s.slug} · v{s.version} · {s.source}
            </div>
            <div className="flex flex-wrap gap-1">
              {s.triggers.map((t) => (
                <span key={t} className="chip">{t}</span>
              ))}
            </div>
            <div className="text-sm text-muted">
              {s.tool_count} read-only tools · {s.proposal_count} proposals
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
