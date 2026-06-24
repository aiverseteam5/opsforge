import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, CatalogDetail, ConfigField } from "../api";
import { ErrorState, Loading, PageHeader, StatusBadge } from "../components/ui";

// A2 — the real config + credential-capture + test flow. Honours the same capability truth
// as the catalog grid (stubs are not configurable), and the credential-safety contract:
// secret fields are WRITE-ONLY — password-masked, never prefilled from the API (the stored
// secret is never returned), and on edit a blank secret keeps the existing one.
export function CatalogConnect() {
  const { key = "" } = useParams();
  const detail = useQuery({ queryKey: ["catalog", key], queryFn: () => api.getCatalogEntry(key) });

  return (
    <div>
      <PageHeader title="Connect" sub={`Configure ${detail.data?.display_name ?? key}`} />
      {detail.isLoading ? (
        <Loading what="connector" />
      ) : detail.isError ? (
        <ErrorState error={detail.error} />
      ) : detail.data!.implementation_status === "stub_coming_soon" ? (
        <ComingSoon name={detail.data!.display_name} />
      ) : detail.data!.transport === "local" ? (
        <LocalIngestForm d={detail.data!} />
      ) : (
        <InstanceConfigForm d={detail.data!} />
      )}
    </div>
  );
}

const Back = () => <Link to="/catalog" className="btn mt-4 inline-block">← Back to catalog</Link>;

function ComingSoon({ name }: { name: string }) {
  return (
    <div className="card max-w-xl">
      <div className="text-sm font-medium">{name}</div>
      <div className="mt-3 rounded border border-amber-700 bg-amber-950/30 p-3 text-sm text-amber-200">
        <b>Coming soon.</b> This connector isn’t implemented yet — there’s nothing to configure.
      </div>
      <Back />
    </div>
  );
}

// local-files: a folder to ingest, NO credential, NO connector instance.
function LocalIngestForm({ d }: { d: CatalogDetail }) {
  const qc = useQueryClient();
  const [path, setPath] = useState("");
  const ingest = useMutation({
    mutationFn: () => api.ingestKnowledge(path),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["catalog"] }),
  });
  return (
    <div className="card max-w-xl">
      <div className="text-sm font-medium">{d.display_name}</div>
      <div className="mt-1 text-xs text-muted">{d.description}</div>
      <label className="mt-4 block text-sm text-muted">Folder path (server-visible)</label>
      <input className="input mt-1 w-full" placeholder="/data/runbooks" value={path}
        onChange={(e) => setPath(e.target.value)} />
      <div className="mt-3 flex items-center gap-2">
        <button className="btn" disabled={!path.trim() || ingest.isPending} onClick={() => ingest.mutate()}>
          {ingest.isPending ? "Starting…" : "Ingest"}
        </button>
        {ingest.isSuccess && <span className="text-xs text-emerald-300">✓ ingest started — status goes green on the catalog</span>}
        {ingest.isError && <span className="text-xs text-rose-300">{String(ingest.error)}</span>}
      </div>
      <Back />
    </div>
  );
}

const isCredField = (f: ConfigField) => f.name !== "endpoint" && f.name !== "tool_allowlist";

function InstanceConfigForm({ d }: { d: CatalogDetail }) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const editing = !!d.instance_id;

  // For edit, prefill only the PLAIN columns (endpoint, tool_allowlist) from the existing
  // instance. Credential fields are NEVER prefilled — the stored secret is never returned.
  const existing = useQuery({
    queryKey: ["connectors"],
    queryFn: () => api.listConnectors(),
    enabled: editing,
  });
  const inst = existing.data?.find((c) => c.id === d.instance_id);

  const [values, setValues] = useState<Record<string, string>>({});
  const get = (name: string) =>
    values[name] ?? (name === "endpoint" ? inst?.endpoint ?? ""
      : name === "tool_allowlist" ? (inst?.tool_allowlist ?? []).join(", ") : "");

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["catalog"] });
    qc.invalidateQueries({ queryKey: ["connectors"] });
  };

  const credentials = () => {
    const out: Record<string, string> = {};
    for (const f of d.config_fields) if (isCredField(f) && values[f.name]) out[f.name] = values[f.name];
    return out;
  };
  const toolList = () =>
    get("tool_allowlist").split(",").map((s) => s.trim()).filter(Boolean);
  // Connect is enabled only when every REQUIRED field (incl. the credential) is filled —
  // you cannot create a credential-bearing connector with a blank secret (fail closed).
  const canCreate = d.config_fields
    .filter((f) => f.required)
    .every((f) => (isCredField(f) ? !!values[f.name] : !!get(f.name)));

  const create = useMutation({
    mutationFn: () => api.createConnector({
      name: d.display_name, kind: d.instance_kind, endpoint: get("endpoint"),
      transport: d.transport === "mcp_stdio" ? "stdio" : "http",
      tool_allowlist: toolList(), credentials: credentials(),
    }),
    onSuccess: refresh,
  });
  const test = useMutation({
    mutationFn: () => api.testConnector(d.instance_id!),
    onSuccess: refresh,
  });
  const update = useMutation({
    mutationFn: () => {
      const creds = credentials();
      return api.updateConnector(d.instance_id!, {
        endpoint: get("endpoint"), tool_allowlist: toolList(),
        // omit credentials entirely when no new secret was entered → keep the stored one
        ...(Object.keys(creds).length ? { credentials: creds } : {}),
      });
    },
    // a config/credential change invalidates the prior test verdict → drop the stale green
    // banner (the backend also resets the instance status to 'unknown' until a fresh test).
    onSuccess: () => { test.reset(); refresh(); },
  });
  const disconnect = useMutation({
    mutationFn: () => api.deleteConnector(d.instance_id!),
    onSuccess: () => { refresh(); nav("/catalog"); },
  });

  return (
    <div className="card max-w-xl">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">{d.display_name}</div>
        <StatusBadge status={d.status} />
      </div>
      <div className="mt-1 text-xs text-muted">{d.description}</div>

      <div className="mt-4 space-y-3">
        {d.config_fields.map((f) => (
          <div key={f.name}>
            <label className="block text-sm text-muted">
              {f.label}{f.required ? "" : " (optional)"}
              {f.secret && <span className="ml-2 text-[11px] text-amber-300">write-only — never shown</span>}
            </label>
            <input
              className="input mt-1 w-full"
              // secret + credential fields are password-masked and NEVER prefilled
              type={f.secret ? "password" : "text"}
              autoComplete="off"
              placeholder={f.placeholder ?? (editing && isCredField(f) ? "leave blank to keep existing" : "")}
              value={isCredField(f) ? values[f.name] ?? "" : get(f.name)}
              onChange={(e) => {
                if (test.data) test.reset(); // editing a field invalidates a stale test verdict
                setValues((v) => ({ ...v, [f.name]: e.target.value }));
              }}
            />
          </div>
        ))}
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        {editing ? (
          <>
            <button className="btn" disabled={update.isPending} onClick={() => update.mutate()}>
              {update.isPending ? "Saving…" : "Save changes"}
            </button>
            <button className="btn" style={{ borderColor: "#0369a1" }}
              disabled={test.isPending} onClick={() => test.mutate()}>
              {test.isPending ? "Testing…" : "Test connection"}
            </button>
            <button className="btn" style={{ borderColor: "#9f1239" }}
              disabled={disconnect.isPending}
              onClick={() => window.confirm("Disconnect and purge this credential from the vault?") && disconnect.mutate()}>
              Disconnect
            </button>
          </>
        ) : (
          <button className="btn" style={{ borderColor: "#15803d" }}
            disabled={create.isPending || !canCreate} onClick={() => create.mutate()}>
            {create.isPending ? "Connecting…" : "Connect"}
          </button>
        )}
      </div>

      {/* honest result of the test — connected / failed with the real reason, never a false green */}
      {test.data && (
        <div className={`mt-3 rounded border p-2 text-xs ${test.data.status === "healthy"
          ? "border-emerald-700 text-emerald-300" : "border-rose-600 bg-rose-950/30 text-rose-200"}`}>
          {test.data.status === "healthy"
            ? <>✓ connected — tools: {(test.data.tools ?? []).join(", ") || "none"}</>
            : <>✗ {test.data.status}{test.data.error ? `: ${test.data.error}` : ""}</>}
        </div>
      )}
      {(create.isError || update.isError || test.isError || disconnect.isError) && (
        <div className="mt-2 text-xs text-rose-300">
          {String(create.error || update.error || test.error || disconnect.error)}
        </div>
      )}
      <Back />
    </div>
  );
}
