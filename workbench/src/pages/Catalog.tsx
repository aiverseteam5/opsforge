import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, CatalogConnector, CatalogStatus } from "../api";
import { ErrorState, Loading, PageHeader } from "../components/ui";

// Honest status badge — the catalog never overstates capability. `coming_soon` is
// visibly distinct (amber, "coming soon") and never reads like a live connection.
const STATUS: Record<CatalogStatus, { cls: string; label: string }> = {
  connected: { cls: "text-emerald-300 border-emerald-700", label: "connected" },
  configured: { cls: "text-sky-300 border-sky-700", label: "configured" },
  available: { cls: "text-zinc-300 border-edge", label: "available" },
  error: { cls: "text-rose-200 border-rose-600 bg-rose-950/40 font-semibold", label: "⚠ error" },
  coming_soon: { cls: "text-amber-200 border-amber-700 bg-amber-950/30", label: "coming soon" },
};

const INGEST_CLS: Record<string, string> = {
  knowledge: "text-sky-300 border-sky-700",
  behaviour: "text-violet-300 border-violet-700",
  telemetry: "text-emerald-300 border-emerald-700",
  actions: "text-amber-300 border-amber-700",
};

function Card({ c }: { c: CatalogConnector }) {
  const nav = useNavigate();
  const s = STATUS[c.status];
  const stub = c.implementation_status === "stub_coming_soon";
  return (
    <div className={`card ${stub ? "opacity-70" : ""}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="font-medium">{c.display_name}</div>
        <span className={`chip ${s.cls}`}>{s.label}</span>
      </div>
      <div className="mt-1 text-xs text-muted">{c.description}</div>
      <div className="mt-2 flex flex-wrap items-center gap-1">
        {c.ingests.map((i) => (
          <span key={i} className={`chip ${INGEST_CLS[i] ?? ""}`}>{i}</span>
        ))}
        <span className="chip text-muted border-edge">auth: {c.auth_type}</span>
      </div>
      <div className="mt-3">
        {c.connectable ? (
          // A1 only NAVIGATES toward the (A2) config flow — it captures no credentials.
          <button className="btn" onClick={() => nav(`/catalog/${c.key}/connect`)}>
            {c.status === "error" ? "Reconnect" : "Connect"}
          </button>
        ) : stub ? (
          <button className="btn cursor-not-allowed opacity-50" disabled title="not yet available">
            Coming soon
          </button>
        ) : c.status === "connected" ? (
          <span className="text-xs text-emerald-300">✓ connected — nothing to do</span>
        ) : (
          <span className="text-xs text-muted">configured</span>
        )}
      </div>
    </div>
  );
}

export function Catalog() {
  const catalog = useQuery({ queryKey: ["catalog"], queryFn: () => api.getCatalog() });
  const zones = catalog.data?.zones ?? [];
  const anyConnected = zones.some((z) => z.connectors.some((c) =>
    ["connected", "configured", "error"].includes(c.status)));

  return (
    <div>
      <PageHeader
        title="Connector Catalog"
        sub="What OpsForge can connect, by zone — connect your first source to light it up"
      />
      {/* Outcome-visible-early: the page opens on the full catalog, never an empty state.
          When nothing is wired yet, guide the next action rather than showing emptiness. */}
      {!catalog.isLoading && !catalog.isError && !anyConnected && (
        <div className="card mb-5 border-sky-800 bg-sky-950/20 text-sm">
          Nothing connected yet. Pick a source below and hit <b>Connect</b> to begin — your
          status badge goes green when it’s live.
        </div>
      )}

      {catalog.isLoading ? (
        <Loading what="catalog" />
      ) : catalog.isError ? (
        <ErrorState error={catalog.error} />
      ) : (
        <div className="space-y-6">
          {zones.map((z) => (
            <div key={z.zone}>
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted">
                {z.zone}
              </h2>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
                {z.connectors.map((c) => <Card key={c.key} c={c} />)}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
