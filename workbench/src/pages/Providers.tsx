import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Empty, ErrorState, Loading, PageHeader, ResidencyBadge } from "../components/ui";

const STATUS_CLS: Record<string, string> = {
  active: "text-emerald-300 border-emerald-700",
  proposed: "text-amber-300 border-amber-700",
  rejected: "text-zinc-400 border-edge",
};

function ProposeForm({ onDone }: { onDone: () => void }) {
  const [provider, setProvider] = useState("openai");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [warn, setWarn] = useState<string | null>(null);

  const propose = useMutation({
    mutationFn: () =>
      api.proposeProvider({
        provider, model,
        api_key: apiKey || undefined,
        api_base: apiBase || undefined,
      }),
    onSuccess: (r) => {
      setWarn(r.residency_warning);
      setModel(""); setApiKey(""); setApiBase("");
      onDone();
    },
  });

  return (
    <div className="card mb-5">
      <div className="text-sm font-medium">Propose a provider binding</div>
      <div className="mt-2 grid grid-cols-2 gap-2">
        <input className="input" placeholder="provider (openai, azure, anthropic, ollama, openrouter…)"
          value={provider} onChange={(e) => setProvider(e.target.value)} />
        <input className="input" placeholder="model (e.g. gpt-4o-mini)"
          value={model} onChange={(e) => setModel(e.target.value)} />
        {/* Credential is WRITE-ONLY: password-masked, sent once, never read back from the
            API (the API never returns it), so it cannot land in the rendered DOM. */}
        <input className="input" type="password" autoComplete="off" placeholder="api key (write-only)"
          value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
        <input className="input" placeholder="api base (optional, self-hosted)"
          value={apiBase} onChange={(e) => setApiBase(e.target.value)} />
      </div>
      <div className="mt-2 flex items-center gap-3">
        <button className="btn" disabled={!provider.trim() || !model.trim() || propose.isPending}
          onClick={() => propose.mutate()}>
          {propose.isPending ? "Proposing…" : "Propose"}
        </button>
        {warn && <span className="text-xs text-amber-300">⚠ {warn}</span>}
        {propose.isError && <span className="text-xs text-rose-300">{String(propose.error)}</span>}
      </div>
      <div className="mt-2 text-xs text-muted">
        Proposing does not activate. A binding is promoted to active ONLY after a server-side
        measurement holds the baseline — there is no way to assert that from here.
      </div>
    </div>
  );
}

export function Providers() {
  const qc = useQueryClient();
  const providers = useQuery({ queryKey: ["providers"], queryFn: () => api.listProviders() });
  const [result, setResult] = useState<Record<string, string>>({});

  const promote = useMutation({
    mutationFn: (id: string) => api.promoteProvider(id),
    onSuccess: (r, id) => {
      // DISPLAY the backend's verdict verbatim — promoted, or the 409 not-held reason.
      setResult((m) => ({ ...m, [id]: r.promoted ? "✓ promoted to active" : `✗ ${r.detail}` }));
      qc.invalidateQueries({ queryKey: ["providers"] });
    },
  });

  return (
    <div>
      <PageHeader title="LLM Providers" sub="Per-workspace, vault-credentialed — promoted only by the measured gate" />
      <ProposeForm onDone={() => qc.invalidateQueries({ queryKey: ["providers"] })} />

      {providers.isLoading ? (
        <Loading what="providers" />
      ) : providers.isError ? (
        <ErrorState error={providers.error} />
      ) : (providers.data ?? []).length === 0 ? (
        <Empty>No providers configured. Propose one above; the workspace falls back to the keyless lexical floor until a binding is promoted.</Empty>
      ) : (
        <div className="space-y-3">
          {(providers.data ?? []).map((p) => {
            const acc = p.scorecard?.contradiction_accuracy ?? p.scorecard?.accuracy;
            const holds = p.scorecard?.holds;
            return (
              <div key={p.id} className="card">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{p.provider}</span>
                    <code className="text-xs text-sky-400">{p.model}</code>
                    <ResidencyBadge residency={p.residency} />
                    <span className={`chip ${STATUS_CLS[p.status] ?? ""}`}>{p.status}</span>
                  </div>
                  {p.status === "proposed" && (
                    <button className="btn" disabled={promote.isPending}
                      onClick={() => promote.mutate(p.id)}>
                      {promote.isPending ? "Promoting…" : "Promote"}
                    </button>
                  )}
                </div>
                <div className="mt-1 text-xs text-muted">
                  {p.scorecard
                    ? <>measured scorecard: accuracy {acc ?? "—"} · holds baseline: {String(holds)}</>
                    : <>no measured scorecard yet — run scoring (eval runner) before promotion</>}
                </div>
                {result[p.id] && (
                  <div className={`mt-2 text-xs ${result[p.id].startsWith("✓") ? "text-emerald-300" : "text-rose-300"}`}>
                    {result[p.id]}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
