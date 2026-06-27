import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ProposedSkill } from "../api";
import { Empty, ErrorState, Loading, PageHeader } from "../components/ui";

type ReviewAction = "approve" | "reject" | null;

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
  const [reviewing, setReviewing] = useState<ReviewAction>(null);
  const [note, setNote] = useState("");

  const approve = useMutation({
    mutationFn: (n: string) => api.approveSkill(skill.id, n || undefined),
    onSuccess: onDone,
  });
  const reject = useMutation({
    mutationFn: (n: string) => api.rejectSkill(skill.id, n || undefined),
    onSuccess: onDone,
  });

  const isPending = approve.isPending || reject.isPending;

  const openReview = (action: ReviewAction) => {
    setNote("");
    setReviewing(action);
  };

  const cancelReview = () => {
    setReviewing(null);
    setNote("");
  };

  const confirmReview = () => {
    if (reviewing === "approve") approve.mutate(note);
    else if (reviewing === "reject") reject.mutate(note);
  };

  const desc: string = skill.manifest?.description ?? "";
  const tools: any[] = skill.manifest?.tools ?? [];
  const proposals: any[] = skill.manifest?.proposals ?? [];

  const mutationError = approve.error || reject.error;

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

      {mutationError && (
        <div className="text-xs text-rose-400">
          {String(mutationError instanceof Error ? mutationError : "Error")}
        </div>
      )}

      {reviewing ? (
        <div className="space-y-2 rounded border border-edge bg-ink p-3">
          <div className="text-xs font-medium">
            {reviewing === "approve" ? "Approve & enable" : "Reject"}{" "}
            <span className="text-muted">"{skill.name}"</span>
          </div>
          <textarea
            className="w-full rounded border border-edge bg-surface px-2 py-1.5 text-xs
                       text-zinc-200 placeholder:text-zinc-600 focus:outline-none
                       focus:ring-1 focus:ring-sky-500"
            rows={3}
            placeholder="Optional note — what did you like or want improved? (fed back to the AI for future proposals)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            disabled={isPending}
          />
          <div className="flex gap-2">
            <button
              className="btn"
              style={{ borderColor: reviewing === "approve" ? "#15803d" : "#9f1239" }}
              disabled={isPending}
              onClick={confirmReview}
            >
              {isPending
                ? reviewing === "approve" ? "Approving…" : "Rejecting…"
                : reviewing === "approve" ? "Confirm approve" : "Confirm reject"}
            </button>
            <button className="btn" disabled={isPending} onClick={cancelReview}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="flex gap-2 pt-1">
          <button
            className="btn"
            style={{ borderColor: "#15803d" }}
            disabled={isPending}
            onClick={() => openReview("approve")}
          >
            Approve & enable
          </button>
          <button
            className="btn"
            style={{ borderColor: "#9f1239" }}
            disabled={isPending}
            onClick={() => openReview("reject")}
          >
            Reject
          </button>
        </div>
      )}
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
