import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Finding, FindingState } from "../api";
import { ConfidenceTag, Empty, ErrorState, Loading, PageHeader } from "../components/ui";

const KIND_CLS: Record<string, string> = {
  contradiction: "text-rose-300 border-rose-700",
  drift: "text-amber-300 border-amber-700",
  violation: "text-rose-300 border-rose-700",
  gap: "text-sky-300 border-sky-700",
  stale: "text-zinc-300 border-edge",
};

// "Why this finding": the precedence rule + action the deterministic engine recorded,
// shown verbatim. The UI explains the backend's verdict; it does not invent one.
function Why({ f }: { f: Finding }) {
  const d = f.detail ?? {};
  const bits = [d.action, d.rule, d.reason, d.precedence].filter(Boolean);
  return (
    <div className="mt-1 text-xs text-muted">
      {bits.length ? bits.join(" · ") : "see evidence"} · evidence:{" "}
      {(f.evidence_refs ?? []).map((r) => (
        <code key={r} className="mr-1 text-[11px] text-sky-400">{String(r).slice(0, 8)}</code>
      ))}
    </div>
  );
}

export function Findings() {
  const qc = useQueryClient();
  const [state, setState] = useState<FindingState | "all">("open");

  const findings = useQuery({
    queryKey: ["findings", state],
    // "all" is sent verbatim; the backend maps it to "no state filter" (every
    // lifecycle state). Sending "" here would become a `state = ''` SQL predicate
    // that matches nothing — a dishonest "mirror is clear" empty state.
    queryFn: () => api.listFindings(undefined, state),
  });
  const triage = useMutation({
    mutationFn: ({ id, s }: { id: string; s: FindingState }) => api.triageFinding(id, s),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["findings"] }),
  });

  return (
    <div>
      <PageHeader title="Findings" sub="The reconciliation mirror — drift, contradiction, gap, violation, stale" />

      <div className="mb-4 flex gap-2">
        {(["open", "acknowledged", "resolved", "dismissed", "all"] as const).map((s) => (
          <button key={s}
            className={`chip ${state === s ? "border-sky-600 text-sky-300" : "text-muted border-edge"}`}
            onClick={() => setState(s)}>
            {s}
          </button>
        ))}
      </div>

      {findings.isLoading ? (
        <Loading what="findings" />
      ) : findings.isError ? (
        <ErrorState error={findings.error} />
      ) : (findings.data ?? []).length === 0 ? (
        <Empty>No {state === "all" ? "" : state} findings. The mirror is clear.</Empty>
      ) : (
        <div className="space-y-3">
          {(findings.data ?? []).map((f) => (
            <div key={f.id} className="card">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className={`chip ${KIND_CLS[f.kind] ?? ""}`}>{f.kind}</span>
                  <span className="text-sm">{f.process_key ?? "—"}</span>
                  <ConfidenceTag value={f.confidence} />
                </div>
                <span className="chip text-muted border-edge">{f.state}</span>
              </div>
              <Why f={f} />
              {f.state !== "resolved" && f.state !== "dismissed" && (
                <div className="mt-3 flex gap-2">
                  {f.state === "open" && (
                    <button className="btn" disabled={triage.isPending}
                      onClick={() => triage.mutate({ id: f.id, s: "acknowledged" })}>Acknowledge</button>
                  )}
                  <button className="btn" disabled={triage.isPending}
                    onClick={() => triage.mutate({ id: f.id, s: "resolved" })}>Resolve</button>
                  <button className="btn" disabled={triage.isPending}
                    onClick={() => triage.mutate({ id: f.id, s: "dismissed" })}>Dismiss</button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {triage.isError && <div className="mt-2 text-xs text-rose-300">{String(triage.error)}</div>}
    </div>
  );
}
