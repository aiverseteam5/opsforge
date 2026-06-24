import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Action } from "../api";
import { Empty, ErrorState, Loading, PageHeader, StatusBadge, fmt } from "../components/ui";

// The M6.5 low-grounding gate, made visible. The number + the reason are the backend's
// (policy_trace.grounding / rules) — shown as-is, never recomputed or softened.
function Grounding({ a }: { a: Action }) {
  const g = a.policy_trace?.grounding;
  const gated = (a.policy_trace?.rules ?? []).includes("low_grounding_gate");
  if (!g && !gated) return null;
  const conf: number | null = g?.grounding_confidence ?? null;
  const low = !!g?.low_confidence || gated;
  return (
    <div className={`mt-2 rounded border p-2 text-xs ${low ? "border-rose-600 bg-rose-950/30 text-rose-200" : "border-edge text-muted"}`}>
      <span className="font-semibold">grounding {conf === null ? "—" : conf.toFixed(2)}</span>
      {" · "}process {g?.process_key ?? "—"} · {g?.chunk_count ?? 0} chunk(s)
      {gated && <span className="ml-2 font-semibold">⚠ GATED: low_grounding_gate forced a human review</span>}
    </div>
  );
}

function isLowGrounding(a: Action): boolean {
  return (
    (a.policy_trace?.rules ?? []).includes("low_grounding_gate") ||
    !!a.policy_trace?.grounding?.low_confidence
  );
}

export function Approvals() {
  const qc = useQueryClient();
  const [plan, setPlan] = useState<Record<string, any>>({});
  // Trust-ladder queue: everything not yet terminal, plus recent outcomes.
  const actions = useQuery({
    queryKey: ["actions"],
    queryFn: () => api.listActions(),
    refetchInterval: 3000,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["actions"] });
  const approve = useMutation({ mutationFn: api.approveAction, onSuccess: invalidate });

  // Friction where the stakes demand it: approving a low-grounding (gated) or destructive
  // action requires a deliberate confirmation — the UI never presents it as a safe one-click.
  const onApprove = (a: Action) => {
    const risky = isLowGrounding(a) || a.action_class === "destructive";
    if (risky && !window.confirm(
      `This action is ${isLowGrounding(a) ? "LOW-GROUNDING (weak knowledge)" : "DESTRUCTIVE"}. ` +
      `Approve and execute anyway?`)) return;
    approve.mutate(a.id);
  };
  const deny = useMutation({ mutationFn: api.denyAction, onSuccess: invalidate });
  const dryRun = useMutation({
    mutationFn: (id: string) => api.dryRunAction(id),
    onSuccess: (res, id) => {
      setPlan((p) => ({ ...p, [id]: res.plan }));
      invalidate();
    },
  });

  const pending = (actions.data ?? []).filter((a) =>
    ["awaiting_approval", "dry_run_done"].includes(a.state),
  );
  const history = (actions.data ?? []).filter(
    (a) => !["awaiting_approval", "dry_run_done"].includes(a.state),
  );

  return (
    <div>
      <PageHeader
        title="Approvals"
        sub="The trust ladder — proposed actions await a human (admin/operator)"
      />

      {actions.isError && <ErrorState error={actions.error} />}

      <h2 className="mb-2 text-sm font-medium text-muted">Awaiting approval</h2>
      {actions.isLoading ? (
        <Loading what="approvals" />
      ) : pending.length === 0 ? (
        <Empty>Nothing awaiting approval.</Empty>
      ) : (
        <div className="space-y-3">
          {pending.map((a) => (
            <div key={a.id} className="card">
              <div className="flex items-center justify-between">
                <div>
                  <code className="text-sky-400">{a.tool}</code>{" "}
                  <span className="text-muted">{a.target_ref}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="chip">{a.action_class}</span>
                  <StatusBadge status={a.state} />
                </div>
              </div>
              <div className="mt-1 text-xs text-muted">
                params: {JSON.stringify(a.params)}
              </div>
              <Grounding a={a} />
              {plan[a.id] && (
                <div className="mt-2 rounded border border-edge bg-ink p-2 text-xs">
                  dry-run plan: {JSON.stringify(plan[a.id])}
                </div>
              )}
              <div className="mt-3 flex gap-2">
                <button className="btn" style={{ borderColor: "#15803d" }}
                  disabled={approve.isPending} onClick={() => onApprove(a)}>
                  Approve &amp; execute
                </button>
                <button className="btn" onClick={() => dryRun.mutate(a.id)}>Dry-run</button>
                <button className="btn" onClick={() => deny.mutate(a.id)}>Dismiss</button>
              </div>
            </div>
          ))}
        </div>
      )}

      <h2 className="mb-2 mt-6 text-sm font-medium text-muted">Recent outcomes</h2>
      {history.length === 0 ? (
        <Empty>No executed actions yet.</Empty>
      ) : (
        <div className="card overflow-hidden p-0">
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Tool</th><th className="th">Class</th>
                <th className="th">State</th><th className="th">Executed</th><th className="th">Result</th>
              </tr>
            </thead>
            <tbody>
              {history.map((a) => (
                <tr key={a.id}>
                  <td className="td"><code className="text-sky-400">{a.tool}</code></td>
                  <td className="td text-muted">{a.action_class}</td>
                  <td className="td"><StatusBadge status={a.state} /></td>
                  <td className="td text-muted">{fmt(a.executed_at)}</td>
                  <td className="td text-muted">{JSON.stringify(a.result)?.slice(0, 80)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
