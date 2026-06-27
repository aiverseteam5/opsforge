import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ProposedSkill } from "../api";
import { Empty, ErrorState, Loading, PageHeader } from "../components/ui";

function SkillManifestPreview({ manifest }: { manifest: Record<string, any> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-2">
      <button className="text-xs text-sky-400 hover:underline" onClick={() => setOpen((o) => !o)}>
        {open ? "hide manifest" : "view manifest"}
      </button>
      {open && (
        <pre className="mt-2 max-h-64 overflow-auto rounded border border-edge bg-ink p-3 text-xs text-zinc-300">
          {JSON.stringify(manifest, null, 2)}
        </pre>
      )}
    </div>
  );
}

function ProposedSkillCard({ skill, onDone }: { skill: ProposedSkill; onDone: () => void }) {
  const approve = useMutation({
    mutationFn: () => api.approveSkill(skill.id),
    onSuccess: onDone,
  });
  const reject = useMutation({
    mutationFn: () => api.rejectSkill(skill.id),
    onSuccess: onDone,
  });

  const onApprove = () => {
    if (!window.confirm(
      `Approve "${skill.name}" (${skill.slug})?\n\nThis will enable it for agent runs.`
    )) return;
    approve.mutate();
  };

  const onReject = () => {
    if (!window.confirm(
      `Reject "${skill.name}" (${skill.slug})?\n\nIt will be hidden from the skill list and not run.`
    )) return;
    reject.mutate();
  };

  const desc: string = skill.manifest?.description ?? "";
  const tools: any[] = skill.manifest?.tools ?? [];
  const proposals: any[] = skill.manifest?.proposals ?? [];

  return (
    <div className="card space-y-3">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="font-medium">{skill.name}</div>
          <div className="text-xs text-muted">
            {skill.slug} · v{skill.version} ·{" "}
            <span className="text-amber-300">codified</span>
          </div>
        </div>
        <span className="chip shrink-0 border-amber-700 text-amber-300">awaiting review</span>
      </div>

      {desc && <p className="text-sm text-muted">{desc}</p>}

      <div className="flex flex-wrap gap-3 text-xs text-muted">
        <span>{tools.length} tool{tools.length !== 1 ? "s" : ""}</span>
        <span>{proposals.length} proposal{proposals.length !== 1 ? "s" : ""}</span>
        {skill.manifest?.run_id && (
          <span>
            from run{" "}
            <code className="text-sky-400">{String(skill.manifest.run_id).slice(0, 8)}</code>
          </span>
        )}
      </div>

      <SkillManifestPreview manifest={skill.manifest} />

      {(approve.isError || reject.isError) && (
        <div className="text-xs text-rose-400">
          {String((approve.error || reject.error) instanceof Error
            ? (approve.error || reject.error)!
            : "Error")}
        </div>
      )}

      <div className="flex gap-2 pt-1">
        <button
          className="btn"
          style={{ borderColor: "#15803d" }}
          disabled={approve.isPending || reject.isPending}
          onClick={onApprove}
        >
          {approve.isPending ? "Approving…" : "Approve & enable"}
        </button>
        <button
          className="btn"
          style={{ borderColor: "#9f1239" }}
          disabled={approve.isPending || reject.isPending}
          onClick={onReject}
        >
          {reject.isPending ? "Rejecting…" : "Reject"}
        </button>
      </div>
    </div>
  );
}

export function ProposedSkills() {
  const [page, setPage] = useState(1);
  const pageSize = 10;
  const qc = useQueryClient();

  const proposed = useQuery({
    queryKey: ["skills-proposed", page],
    queryFn: () => api.listProposed(page, pageSize),
    refetchInterval: 10_000,
  });

  const onDone = () => {
    qc.invalidateQueries({ queryKey: ["skills-proposed"] });
    qc.invalidateQueries({ queryKey: ["skills"] });
  };

  const total = proposed.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div>
      <PageHeader
        title="Proposed skills"
        sub="AI-codified skills awaiting human review before activation"
      />

      {proposed.isError && <ErrorState error={proposed.error} />}

      {proposed.isLoading ? (
        <Loading what="proposed skills" />
      ) : (proposed.data?.items ?? []).length === 0 ? (
        <Empty>
          No skills awaiting review.{" "}
          <span className="text-zinc-500">
            Skills are proposed automatically when an agent run completes successfully and its
            confidence is not low.
          </span>
        </Empty>
      ) : (
        <div className="space-y-4">
          <div className="text-sm text-muted">{total} skill{total !== 1 ? "s" : ""} pending</div>
          {(proposed.data?.items ?? []).map((s) => (
            <ProposedSkillCard key={s.id} skill={s} onDone={onDone} />
          ))}
        </div>
      )}

      {totalPages > 1 && (
        <div className="mt-4 flex items-center gap-2 text-sm">
          <button
            className="btn"
            disabled={page <= 1}
            onClick={() => setPage((p) => p - 1)}
          >
            Prev
          </button>
          <span className="text-muted">
            page {page} / {totalPages}
          </span>
          <button
            className="btn"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
