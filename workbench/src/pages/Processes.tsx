import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ProcessStep, ValidatedProcess } from "../api";
import { ConfidenceTag, Empty, ErrorState, Loading, PageHeader } from "../components/ui";

function StepRow({ s }: { s: ProcessStep }) {
  return (
    <div className={`rounded-md border p-3 ${s.low_confidence ? "border-rose-600 bg-rose-950/30" : "border-edge"}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="text-sm">
          <span className="mr-2 text-muted">{s.index + 1}.</span>
          {s.low_confidence && <span className="mr-2 font-semibold text-rose-300">⚠ LOW-CONFIDENCE</span>}
          {s.text}
        </div>
        <ConfidenceTag value={s.confidence} low={s.low_confidence} />
      </div>
      <div className="mt-1 text-xs text-muted">
        {s.kind} · sources: {s.source_kinds.join(", ")} · freshness {s.freshness_days}d ·{" "}
        {s.source_chunks.length} chunk(s)
      </div>
    </div>
  );
}

// Structural diff vs the prior version: which steps are new/removed. The step text +
// its provenance come straight from the backend; only the added/removed framing is UI.
function Diff({ versions }: { versions: ValidatedProcess[] }) {
  if (versions.length < 2) return <Empty>No prior version to compare.</Empty>;
  const cur = new Set(versions[0].steps.map((s) => s.text));
  const prev = new Set(versions[1].steps.map((s) => s.text));
  const added = versions[0].steps.filter((s) => !prev.has(s.text));
  const removed = versions[1].steps.filter((s) => !cur.has(s.text));
  return (
    <div className="space-y-2">
      <div className="text-xs text-muted">v{versions[1].version} → v{versions[0].version}</div>
      {added.length === 0 && removed.length === 0 && <Empty>No step changes between versions.</Empty>}
      {added.map((s, i) => (
        <div key={`a${i}`} className="rounded border border-emerald-700 bg-emerald-950/20 p-2 text-sm">
          <span className="font-semibold text-emerald-300">+ added</span> {s.text}
          <div className="text-xs text-muted">sources: {s.source_kinds.join(", ")} · conf {s.confidence.toFixed(2)}</div>
        </div>
      ))}
      {removed.map((s, i) => (
        <div key={`r${i}`} className="rounded border border-rose-700 bg-rose-950/20 p-2 text-sm">
          <span className="font-semibold text-rose-300">− removed</span> {s.text}
        </div>
      ))}
    </div>
  );
}

function ProcessDetail({ proc }: { proc: ValidatedProcess }) {
  const qc = useQueryClient();
  const [showDiff, setShowDiff] = useState(false);
  const versions = useQuery({
    queryKey: ["versions", proc.process_key],
    queryFn: () => api.processVersions(proc.process_key),
    enabled: showDiff,
  });
  const signoff = useMutation({
    mutationFn: () => api.signoffProcess(proc.process_key),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["processes"] }),
  });

  const hasLow = proc.steps.some((s) => s.low_confidence);
  const onSignoff = () => {
    // Friction added where the stakes demand it: signing off a process that contains
    // low-confidence steps requires a deliberate confirmation, not a reflexive click.
    if (proc.status !== "draft") return;
    if (hasLow && !window.confirm(
      "This process contains LOW-CONFIDENCE steps (weak grounding). Sign off anyway?")) return;
    signoff.mutate();
  };

  return (
    <div className="card">
      <div className="flex items-center justify-between">
        <div>
          <span className="font-medium">{proc.process_key}</span>{" "}
          <span className="text-xs text-muted">v{proc.version} · {proc.status}</span>
        </div>
        <div className="flex items-center gap-2">
          {/* the summary trust chip is driven by the backend per-step verdict (hasLow),
              never a UI-recomputed threshold */}
          <ConfidenceTag value={proc.min_confidence} low={hasLow} label="min" />
          <button className="btn" onClick={() => setShowDiff((v) => !v)}>
            {showDiff ? "Hide diff" : "Diff vs prior"}
          </button>
          {proc.status === "draft" && (
            <button className="btn" style={{ borderColor: "#15803d" }}
              disabled={signoff.isPending} onClick={onSignoff}>
              {signoff.isPending ? "Signing…" : "Sign off"}
            </button>
          )}
        </div>
      </div>
      {hasLow && (
        <div className="mt-2 rounded border border-rose-600 bg-rose-950/30 p-2 text-xs text-rose-200">
          ⚠ This process has low-confidence steps — look hard before signoff.
        </div>
      )}
      {signoff.isError && <div className="mt-2 text-xs text-rose-300">{String(signoff.error)}</div>}

      <div className="mt-3 space-y-2">
        {proc.steps.map((s) => <StepRow key={s.index} s={s} />)}
      </div>

      {showDiff && (
        <div className="mt-4 border-t border-edge pt-3">
          <div className="mb-2 text-sm font-medium text-muted">Version diff</div>
          {versions.isLoading ? <Loading what="versions" />
            : versions.isError ? <ErrorState error={versions.error} />
            : <Diff versions={versions.data ?? []} />}
        </div>
      )}
    </div>
  );
}

export function Processes() {
  const procs = useQuery({ queryKey: ["processes"], queryFn: () => api.listProcesses() });

  return (
    <div>
      <PageHeader title="Processes" sub="Validated current-version process — review, then sign off" />
      {procs.isLoading ? (
        <Loading what="processes" />
      ) : procs.isError ? (
        <ErrorState error={procs.error} />
      ) : (procs.data ?? []).length === 0 ? (
        <Empty>No validated processes yet. Reconcile a process to draft one.</Empty>
      ) : (
        <div className="space-y-4">
          {(procs.data ?? []).map((p) => <ProcessDetail key={p.id} proc={p} />)}
        </div>
      )}
    </div>
  );
}
