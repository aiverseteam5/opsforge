import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Empty, PageHeader, StatusBadge, useToast } from "../components/ui";

function FromUrlPanel() {
  const [url, setUrl] = useState("");
  const toast = useToast();
  const qc = useQueryClient();

  const codify = useMutation({
    mutationFn: () => api.codifySkillFromUrl(url.trim()),
    onSuccess: (d) => {
      toast(d.message, "success");
      setUrl("");
      qc.invalidateQueries({ queryKey: ["skills-proposed-count"] });
    },
    onError: (e: Error) => {
      toast(e.message, "error");
    },
  });

  return (
    <div className="card space-y-3">
      <div className="text-sm font-medium">Codify from URL</div>
      <p className="text-xs text-muted">
        Paste a runbook URL (must return <code className="rounded bg-edge/60 px-1 text-sky-400">text/*</code>,
        max 256 KB). OpsForge fetches it securely and proposes a skill for review.
      </p>
      <div className="flex gap-2">
        <input
          className="input flex-1 text-sm"
          placeholder="https://docs.example.com/runbooks/disk-full.md"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && url.trim()) codify.mutate(); }}
        />
        <button
          className="btn-primary px-4 text-sm"
          disabled={!url.trim() || codify.isPending}
          onClick={() => codify.mutate()}
        >
          {codify.isPending ? "Queuing…" : "Codify"}
        </button>
      </div>
    </div>
  );
}

export function Skills() {
  const skills = useQuery({ queryKey: ["skills"], queryFn: api.listSkills });
  return (
    <div className="space-y-4">
      <PageHeader title="Skills" sub="Installed capability packs and their trust surface" />
      <FromUrlPanel />
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
