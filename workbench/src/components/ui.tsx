import { ReactNode } from "react";

const STATUS_COLORS: Record<string, string> = {
  queued: "text-amber-300 border-amber-700",
  running: "text-sky-300 border-sky-700",
  reporting: "text-sky-300 border-sky-700",
  done: "text-emerald-300 border-emerald-700",
  succeeded: "text-emerald-300 border-emerald-700",
  healthy: "text-emerald-300 border-emerald-700",
  failed: "text-rose-300 border-rose-700",
  unhealthy: "text-rose-300 border-rose-700",
  cancelled: "text-zinc-400 border-zinc-600",
  awaiting_approval: "text-amber-300 border-amber-700",
};

export function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_COLORS[status] ?? "text-zinc-300 border-edge";
  return <span className={`chip ${cls}`}>{status}</span>;
}

export function PageHeader({ title, sub, right }: { title: string; sub?: string; right?: ReactNode }) {
  return (
    <div className="mb-5 flex items-end justify-between">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {sub && <div className="text-sm text-muted">{sub}</div>}
      </div>
      {right}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="card text-sm text-muted">{children}</div>;
}

export function Loading({ what = "data" }: { what?: string }) {
  return <div className="card text-sm text-muted">Loading {what}…</div>;
}

export function ErrorState({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="card border-rose-700 text-sm text-rose-300">
      <div className="font-medium">Couldn’t load this.</div>
      <div className="mt-1 break-words text-xs text-rose-400">{msg}</div>
    </div>
  );
}

export function fmt(ts: string | null): string {
  if (!ts) return "—";
  return new Date(ts).toLocaleString();
}

// Confidence/grounding shown as the BACKEND computed it — never recomputed or
// softened. The `low` verdict is the BACKEND's boolean; the UI must NOT invent a
// threshold of its own. Three honest states:
//   low === true   → the backend flagged this low-confidence → loud rose warning
//   low === false  → the backend judged it acceptable        → emerald
//   low undefined  → no backend verdict for this shape        → neutral (just the
//                    raw number, no safe/low assertion the server didn't make)
export function ConfidenceTag({
  value,
  low,
  label = "confidence",
}: {
  value: number | null;
  low?: boolean;
  label?: string;
}) {
  if (value === null || value === undefined) {
    return <span className="chip text-zinc-400 border-edge">unscored</span>;
  }
  const cls =
    low === true
      ? "text-rose-200 border-rose-600 bg-rose-950/40 font-semibold"
      : low === false
        ? "text-emerald-300 border-emerald-700"
        : "text-zinc-300 border-edge";
  return (
    <span className={`chip ${cls}`} title={`${label}: ${value}`}>
      {low === true ? "⚠ low " : ""}
      {label} {value.toFixed(2)}
    </span>
  );
}

export function ResidencyBadge({ residency }: { residency: string }) {
  const enterprise = residency === "enterprise";
  const cls = enterprise
    ? "text-emerald-300 border-emerald-700"
    : "text-amber-200 border-amber-600 bg-amber-950/40 font-semibold";
  return (
    <span className={`chip ${cls}`} title={`data residency: ${residency}`}>
      {enterprise ? "enterprise" : `⚠ ${residency} · non-residency`}
    </span>
  );
}
