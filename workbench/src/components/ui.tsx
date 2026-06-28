import { ReactNode, createContext, useCallback, useContext, useRef, useState } from "react";
import { IconCheckCircle, IconInfo, IconX, IconXCircle } from "./icons";

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
        {sub && <div className="mt-0.5 text-sm text-muted">{sub}</div>}
      </div>
      {right}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="card flex flex-col items-center py-8 text-center text-sm text-muted">
      <div className="mb-2 text-2xl opacity-30">—</div>
      {children}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton h-4 ${className}`} />;
}

export function SkeletonCard() {
  return (
    <div className="card space-y-3">
      <Skeleton className="w-1/3" />
      <Skeleton className="w-2/3" />
      <Skeleton className="w-1/2" />
    </div>
  );
}

export function Loading({ what = "data" }: { what?: string }) {
  return (
    <div className="space-y-3">
      <SkeletonCard />
      <SkeletonCard />
      <div className="card text-xs text-muted">Loading {what}…</div>
    </div>
  );
}

export function ErrorState({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="card border-rose-700 bg-rose-950/20 text-sm text-rose-300">
      <div className="font-medium">Couldn't load this.</div>
      <div className="mt-1 break-words text-xs text-rose-400">{msg}</div>
    </div>
  );
}

export function fmt(ts: string | null): string {
  if (!ts) return "—";
  return new Date(ts).toLocaleString();
}

export function fmtRelative(ts: string | null): string {
  if (!ts) return "—";
  const diff = Date.now() - new Date(ts).getTime();
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

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

// ─── Toast ──────────────────────────────────────────────────────────────────

export type ToastKind = "success" | "error" | "info";
interface ToastItem { id: number; kind: ToastKind; message: string }

const ToastCtx = createContext<(msg: string, kind?: ToastKind) => void>(() => {});

export const useToast = () => useContext(ToastCtx);

const TOAST_BORDER: Record<ToastKind, string> = {
  success: "border-emerald-700 bg-emerald-950/95 text-emerald-200",
  error:   "border-rose-700 bg-rose-950/95 text-rose-200",
  info:    "border-sky-700 bg-sky-950/95 text-sky-200",
};
const TOAST_ICON: Record<ToastKind, ReactNode> = {
  success: <IconCheckCircle size={14} />,
  error:   <IconXCircle size={14} />,
  info:    <IconInfo size={14} />,
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(0);

  const toast = useCallback((message: string, kind: ToastKind = "success") => {
    const id = ++nextId.current;
    setToasts((t) => [...t, { id, kind, message }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3500);
  }, []);

  return (
    <ToastCtx.Provider value={toast}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 pointer-events-none">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`toast-in pointer-events-auto flex items-center gap-2 rounded-lg border px-3 py-2.5 text-sm shadow-xl ${TOAST_BORDER[t.kind]}`}
          >
            {TOAST_ICON[t.kind]}
            <span>{t.message}</span>
            <button
              className="ml-2 opacity-60 hover:opacity-100 transition-opacity"
              onClick={() => setToasts((ts) => ts.filter((x) => x.id !== t.id))}
            >
              <IconX size={12} />
            </button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ─── Confirm dialog ──────────────────────────────────────────────────────────

interface ConfirmState { message: string; resolve: (v: boolean) => void }

export function useConfirm() {
  const [state, setState] = useState<ConfirmState | null>(null);

  const confirm = useCallback((message: string): Promise<boolean> =>
    new Promise((resolve) => setState({ message, resolve })), []);

  const close = (val: boolean) => {
    state?.resolve(val);
    setState(null);
  };

  const dialog = state ? (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70"
      onClick={() => close(false)}
      style={{ animation: "fade-in 0.15s ease-out" }}
    >
      <div
        className="card w-[400px] space-y-4 border-zinc-600 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="text-sm leading-relaxed">{state.message}</p>
        <div className="flex justify-end gap-2">
          <button className="btn" onClick={() => close(false)}>Cancel</button>
          <button className="btn-danger" onClick={() => close(true)}>Confirm</button>
        </div>
      </div>
    </div>
  ) : null;

  return { confirm, dialog };
}
