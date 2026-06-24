import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, KnowledgeChunk } from "../api";
import { ConfidenceTag, Empty, ErrorState, Loading, PageHeader, StatusBadge, fmt } from "../components/ui";

const KIND_CLS: Record<string, string> = {
  document: "text-sky-300 border-sky-700",
  behaviour: "text-violet-300 border-violet-700",
  research: "text-zinc-300 border-edge",
};

// The verified-identity signal, shown honestly. A ticket-sourced behaviour chunk is
// trustworthy only if the connector resolved its origin to a real directory identity
// (provenance_root). Origin present but root null ⇒ UNVERIFIED → demoted; we say so.
function OriginCell({ c }: { c: KnowledgeChunk }) {
  if (!c.origin) return <span className="text-muted">—</span>;
  if (c.provenance_root) {
    return (
      <span className="chip text-emerald-300 border-emerald-700" title={`verified id: ${c.provenance_root}`}>
        ✓ {c.origin}
      </span>
    );
  }
  return (
    <span className="chip text-rose-200 border-rose-600 bg-rose-950/40 font-semibold"
      title="origin could not be verified against the directory — demoted">
      ⚠ {c.origin} · unverified
    </span>
  );
}

export function Knowledge() {
  const qc = useQueryClient();
  const [path, setPath] = useState("");
  const [job, setJob] = useState<string | null>(null);

  const chunks = useQuery({ queryKey: ["chunks"], queryFn: () => api.listChunks() });

  // Poll the triggered job's status until it leaves a running state — honest progress,
  // not a spinner-forever; refresh the chunk list when it lands.
  const jobQ = useQuery({
    queryKey: ["job", job],
    queryFn: () => api.jobStatus(job!),
    enabled: !!job,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      if (s && !["queued", "running"].includes(s)) {
        qc.invalidateQueries({ queryKey: ["chunks"] });
        return false;
      }
      return 1500;
    },
  });

  const ingest = useMutation({
    mutationFn: (p: string) => api.ingestKnowledge(p),
    onSuccess: (r) => setJob(r.job_id),
  });

  return (
    <div>
      <PageHeader title="Knowledge" sub="Ingest a source, then see what landed — with its provenance" />

      <div className="card mb-5">
        <div className="text-sm font-medium">Ingest a source folder</div>
        <div className="mt-2 flex gap-2">
          <input
            className="input flex-1"
            placeholder="/path/to/markdown (server-visible)"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && path.trim() && ingest.mutate(path.trim())}
          />
          <button className="btn" disabled={!path.trim() || ingest.isPending}
            onClick={() => ingest.mutate(path.trim())}>
            {ingest.isPending ? "Queuing…" : "Ingest"}
          </button>
        </div>
        {ingest.isError && <div className="mt-2 text-xs text-rose-300">{String(ingest.error)}</div>}
        {job && (
          <div className="mt-3 flex items-center gap-2 text-sm">
            <span className="text-muted">job {job.slice(0, 8)}…</span>
            {jobQ.data ? <StatusBadge status={jobQ.data.status} /> : <span className="text-muted">…</span>}
            {jobQ.data?.status === "failed" && (
              <span className="text-xs text-rose-300">ingest failed — check the source path/logs</span>
            )}
          </div>
        )}
      </div>

      <h2 className="mb-2 text-sm font-medium text-muted">Ingested knowledge</h2>
      {chunks.isLoading ? (
        <Loading what="knowledge" />
      ) : chunks.isError ? (
        <ErrorState error={chunks.error} />
      ) : (chunks.data ?? []).length === 0 ? (
        <Empty>No knowledge ingested yet. Point at a source above to begin.</Empty>
      ) : (
        <div className="card overflow-hidden p-0">
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Process</th><th className="th">Kind</th><th className="th">Source</th>
                <th className="th">Origin (verified?)</th><th className="th">Observed</th>
                <th className="th">Ingested</th><th className="th">Confidence</th>
              </tr>
            </thead>
            <tbody>
              {(chunks.data ?? []).map((c) => (
                <tr key={c.id}>
                  <td className="td">{c.process_key ?? <span className="text-muted">—</span>}</td>
                  <td className="td"><span className={`chip ${KIND_CLS[c.source_kind] ?? ""}`}>{c.source_kind}</span></td>
                  <td className="td"><code className="text-xs text-sky-400">{c.source_ref}</code></td>
                  <td className="td"><OriginCell c={c} /></td>
                  <td className="td text-xs text-muted">{fmt(c.observed_at)}</td>
                  <td className="td text-xs text-muted">{fmt(c.ingested_at)}</td>
                  <td className="td"><ConfidenceTag value={c.confidence} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
