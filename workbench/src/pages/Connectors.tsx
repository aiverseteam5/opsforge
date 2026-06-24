import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, Connector } from "../api";
import { Empty, PageHeader, StatusBadge } from "../components/ui";

const KINDS = ["kubernetes", "aws", "datadog", "servicenow", "jira", "pagerduty", "slack", "custom"];

export function Connectors() {
  const qc = useQueryClient();
  const connectors = useQuery({ queryKey: ["connectors"], queryFn: api.listConnectors });
  // The mapping editor: edits the SAME field_mapping JSON the API/CLI write (doctrine #10).
  const [mapping, setMapping] = useState<{ id: string; text: string; error: string } | null>(null);
  const [form, setForm] = useState({
    name: "",
    kind: "kubernetes",
    transport: "stdio",
    endpoint: "",
    tool_allowlist: "",
  });

  const discover = useMutation({
    mutationFn: (id: string) => api.discoverConnector(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["connectors"] }),
  });
  const saveMapping = useMutation({
    mutationFn: (v: { id: string; fm: Record<string, any> }) => api.putMapping(v.id, v.fm),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["connectors"] });
      setMapping(null);
    },
    onError: (e: any) =>
      setMapping((m) => (m ? { ...m, error: String(e.message ?? e) } : m)),
  });

  function openMapping(c: Connector) {
    setMapping({ id: c.id, text: JSON.stringify(c.field_mapping ?? {}, null, 2), error: "" });
  }
  function commitMapping() {
    if (!mapping) return;
    try {
      saveMapping.mutate({ id: mapping.id, fm: JSON.parse(mapping.text) });
    } catch (e: any) {
      setMapping({ ...mapping, error: `invalid JSON: ${e.message}` });
    }
  }

  const create = useMutation({
    mutationFn: () =>
      api.createConnector({
        name: form.name,
        kind: form.kind,
        transport: form.transport,
        endpoint: form.endpoint,
        tool_allowlist: form.tool_allowlist
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["connectors"] });
      setForm({ ...form, name: "", endpoint: "", tool_allowlist: "" });
    },
  });

  const test = useMutation({
    mutationFn: (id: string) => api.testConnector(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["connectors"] }),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteConnector(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["connectors"] }),
  });

  return (
    <div>
      <PageHeader title="Connectors" sub="MCP servers OpsForge can investigate through" />

      <div className="card mb-5 grid grid-cols-6 gap-2">
        <input className="input" placeholder="name" value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <select className="input" value={form.kind}
          onChange={(e) => setForm({ ...form, kind: e.target.value })}>
          {KINDS.map((k) => <option key={k}>{k}</option>)}
        </select>
        <select className="input" value={form.transport}
          onChange={(e) => setForm({ ...form, transport: e.target.value })}>
          <option>stdio</option>
          <option>http</option>
        </select>
        <input className="input col-span-2" placeholder="endpoint (command or URL)"
          value={form.endpoint} onChange={(e) => setForm({ ...form, endpoint: e.target.value })} />
        <button className="btn" disabled={!form.name || !form.endpoint || create.isPending}
          onClick={() => create.mutate()}>Add</button>
        <input className="input col-span-6" placeholder="tool allowlist (comma-separated)"
          value={form.tool_allowlist} onChange={(e) => setForm({ ...form, tool_allowlist: e.target.value })} />
      </div>

      {connectors.data?.length === 0 ? (
        <Empty>No connectors yet.</Empty>
      ) : (
        <div className="card overflow-hidden p-0">
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Name</th><th className="th">Kind</th>
                <th className="th">Status</th><th className="th">Tools</th><th className="th"></th>
              </tr>
            </thead>
            <tbody>
              {(connectors.data ?? []).map((c) => (
                <tr key={c.id}>
                  <td className="td">{c.name}</td>
                  <td className="td text-muted">{c.kind} / {c.transport}</td>
                  <td className="td"><StatusBadge status={c.status} /></td>
                  <td className="td text-muted">
                    {c.tool_allowlist.length}
                    {c.field_mapping && <span className="ml-2 chip">mapped</span>}
                  </td>
                  <td className="td text-right">
                    <button className="btn mr-2" onClick={() => test.mutate(c.id)}>Test</button>
                    <button className="btn mr-2" onClick={() => discover.mutate(c.id)}>Discover</button>
                    <button className="btn mr-2" onClick={() => openMapping(c)}>Map</button>
                    <button className="btn" onClick={() => remove.mutate(c.id)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {mapping && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
          onClick={() => setMapping(null)}>
          <div className="card w-[640px]" onClick={(e) => e.stopPropagation()}>
            <div className="mb-2 text-sm font-medium">Field mapping (native → canonical)</div>
            <div className="mb-2 text-xs text-muted">
              This edits the same <code>field_mapping</code> JSON the API and CLI write.
            </div>
            <textarea
              className="input h-72 font-mono text-xs"
              value={mapping.text}
              onChange={(e) => setMapping({ ...mapping, text: e.target.value })}
            />
            {mapping.error && <div className="mt-2 text-sm text-rose-300">{mapping.error}</div>}
            <div className="mt-3 flex justify-end gap-2">
              <button className="btn" onClick={() => setMapping(null)}>Cancel</button>
              <button className="btn" disabled={saveMapping.isPending} onClick={commitMapping}>
                Save mapping
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
