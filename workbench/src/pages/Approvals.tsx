import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Action } from "../api";
import { Empty, ErrorState, Loading, PageHeader, StatusBadge, fmt, useConfirm, useToast } from "../components/ui";

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
  const { confirm, dialog } = useConfirm();
  const toast = useToast();

  const actions = useQuery({
    queryKey: ["actions"],
    queryFn: () => api.listActions(),
    refetchInterval: 3000,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["actions"] });
  const approve = useMutation({
    mutationFn: api.approveAction,
    onSuccess: () => { invalidate(); toast("Action approved and queued for execution"); },
    onError: (e) => toast(String(e), "error"),
  });

  const onApprove = async (a: Action) => {
    const risky = isLowGrounding(a) || a.action_class === "destructive";
    if (risky) {
      const kind = isLowGrounding(a) ? "LOW-GROUNDING (weak knowledge backing)" : "DESTRUCTIVE";
      const ok = await confirm(
        `This action is ${kind}.\n\nTool: ${a.tool}\nTarget: ${a.target_ref ?? "—"}\n\nApprove and execute anyway?`
      );
      if (!ok) return;
    }
    approve.mutate(a.id);
  };

  const deny = useMutation({
    mutationFn: api.denyAction,
    onSuccess: () => { invalidate(); toast("Action dismissed"); },
  });
  const dryRun = useMutation({
    mutationFn: (id: string) => api.dryRunAction(id),
    onSuccess: (res, id) => {
      setPlan((p) => ({ ...p, [id]: res.plan }));
      invalidate();
      toast("Dry-run complete — review the plan below", "info");
    },
    onError: (e) => toast(String(e), "error"),
  });

  const pending = (actions.data ?? []).filter((a) =>
    ["awaiting_approval", "dry_run_done"].includes(a.state),
  );
  const history = (actions.data ?? []).filter(
    (a) => !["awaiting_approval", "dry_run_done"].includes(a.state),
  );

  return (
    <div>
      {dialog}
      <PageHeader
        title="Approvals"
        sub="The trust ladder — proposed actions await a human (admin/operator)"
      />

      {actions.isError && <ErrorState error={actions.error} />}

      <h2 className="mb-2 text-sm font-medium text-muted">
        Awaiting approval
        {pending.length > 0 && (
          <span className="ml-2 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-amber-600/70 px-1.5 text-[10px] font-semibold text-white">
            {pending.length}
          </span>
        )}
      </h2>
      {actions.isLoading ? (
        <Loading what="approvals" />
      ) : pending.length === 0 ? (
        <Empty>Nothing awaiting approval.</Empty>
      ) : (
        <div className="space-y-3">
          {pending.map((a) => {
            const risky = isLowGrounding(a) || a.action_class === "destructive";
            return (
              <div key={a.id} className={`card ${risky ? "border-amber-700 bg-amber-950/10" : ""}`}>
                <div className="flex items-center justify-between">
                  <div>
                    <code className="text-sky-400">{a.tool}</code>{" "}
                    <span className="text-muted">{a.target_ref}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`chip ${risky ? "border-amber-700 text-amber-300" : ""}`}>
                      {a.action_class}
                    </span>
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
                  <button
                    className="btn-primary"
                    disabled={approve.isPending}
                    onClick={() => onApprove(a)}
                  >
                    Approve &amp; execute
                  </button>
                  <button className="btn" onClick={() => dryRun.mutate(a.id)}>
                    Dry-run
                  </button>
                  <button className="btn-danger" onClick={() => deny.mutate(a.id)}>
                    Dismiss
                  </button>
                </div>
              </div>
            );
          })}
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
                <tr key={a.id} className="tr-hover">
                  <td className="td"><code className="text-sky-400">{a.tool}</code></td>
                  <td className="td text-muted">{a.action_class}</td>
                  <td className="td"><StatusBadge status={a.state} /></td>
                  <td className="td text-muted text-xs">{fmt(a.executed_at)}</td>
                  <td className="td text-muted text-xs">{JSON.stringify(a.result)?.slice(0, 80)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
