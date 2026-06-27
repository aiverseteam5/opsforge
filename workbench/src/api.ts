// Typed client for the OpsForge API. Hand-written (kept in lockstep with the
// FastAPI routes) rather than codegen — fewer moving parts, doctrine #7.

const TOKEN_KEY = "opsforge_token";

export const getToken = () => localStorage.getItem(TOKEN_KEY) ?? "";
export const setToken = (t: string) => localStorage.setItem(TOKEN_KEY, t.trim());
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api/v1${path}`, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
      ...(opts.headers || {}),
    },
  });
  if (res.status === 401) {
    clearToken();
    location.reload();
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ---- types -------------------------------------------------------------
export interface RunSummary {
  id: string;
  skill_id: string | null;
  status: string;
  model: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}
export interface Evidence { claim: string; source_tool?: string; raw_ref?: string }
export interface RcaReport {
  hypothesis: string;
  confidence: string;
  evidence: Evidence[];
  proposals: string[];
  next_checks: string[];
  missing_evidence?: string | null;
}
export interface RunDetail extends RunSummary {
  trigger: any;
  report_md: string | null;
  report_json: RcaReport | null;
  tokens_in: number | null;
  tokens_out: number | null;
}
export interface Connector {
  id: string; name: string; kind: string; transport: string; endpoint: string;
  tool_allowlist: string[]; field_mapping: Record<string, any> | null;
  discovered_schema: Record<string, any> | null;
  status: string; last_health_at: string | null;
}
export interface NlResult {
  run_id?: string; status?: string; skill_slug?: string;
  candidates?: { slug: string; name: string }[]; nl?: string;
}
export interface Skill {
  slug: string; version: string; name: string; source: string; enabled: boolean;
  triggers: string[]; tool_count: number; proposal_count: number;
}
export interface Schedule {
  id: string; name: string; skill_id: string; trigger_kind: string;
  cron_expr: string | null; event_filter: any; enabled: boolean;
  next_run_at: string | null; last_run_id: string | null;
}
export interface AuditEntry {
  seq: number; actor: string; event: string; subject_ref: string | null;
  detail: any; created_at: string;
}
export interface Action {
  id: string; run_id: string | null; action_class: string; tool: string;
  params: any; target_ref: string | null; rollback: any; state: string;
  policy_trace: any; approved_by?: string | null;
  approved_at: string | null; executed_at: string | null; result: any; created_at: string;
}

// ---- M8: the knowledge-loop surface -----------------------------------
// Every type below mirrors a backend shape. The UI DISPLAYS these verdicts; it
// never recomputes confidence, grounding, or a promotion hold (doctrine #1).
export interface KnowledgeChunk {
  id: string; process_key: string | null; content: string;
  source_kind: "document" | "behaviour" | "research";
  source_ref: string; observed_at: string; ingested_at: string;
  confidence: number | null;
  // For ticket-sourced behaviour: the free-text origin and its connector-VERIFIED
  // identity. provenance_root === null with an origin present ⇒ UNVERIFIED → demoted.
  origin: string | null; provenance_root: string | null;
  corroborated_by: number | null; contradicted_by: number | null;
  superseded_by: string | null;
}
export interface JobStatus {
  id: string; kind: string; status: string; attempts: number;
  created_at: string; updated_at: string | null;
}
export interface ProcessStep {
  index: number; kind: "step" | "decision" | "gate"; text: string;
  source_chunks: string[]; source_kinds: string[];
  freshness_days: number; confidence: number; low_confidence: boolean;
}
export interface ValidatedProcess {
  id: string; process_key: string; version: number; status: string;
  steps: ProcessStep[]; min_confidence: number | null;
  uncovered_chunks: any[]; signed_off_by: string | null;
}
export type FindingState = "open" | "acknowledged" | "resolved" | "dismissed";
export interface Finding {
  id: string; process_key: string | null;
  kind: "contradiction" | "drift" | "gap" | "violation" | "stale";
  detail: any; evidence_refs: string[]; confidence: number | null;
  state: FindingState; seq: number;
}
// Token management: list/create/revoke API tokens. token field is write-once
// and only present on the CreatedToken response (never returned on list).
export interface Token {
  id: string;
  name: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  created_at: string;
}
export interface CreatedToken extends Token {
  token: string; // raw token shown once — copy it now, it cannot be retrieved
}

// Phase 3: codified skill awaiting human review before activation.
export interface ProposedSkill {
  id: string;
  slug: string;
  version: string;
  name: string;
  source: string;
  enabled: boolean;
  manifest: Record<string, any>;
}

// Provider: the credential is WRITE-ONLY — there is deliberately no api_key field
// on this read shape. It is never returned by the API, so it can never reach the DOM.
export interface Provider {
  id: string; provider: string; model: string;
  status: "proposed" | "active" | "rejected";
  residency: "enterprise" | "aggregator" | "unknown";
  scorecard: Record<string, any> | null;
}

// ---- A1: the connector catalog (read-only over the registry + this workspace's status)
export type CatalogStatus = "available" | "configured" | "connected" | "error" | "coming_soon";
export interface CatalogConnector {
  key: string; display_name: string; zone: string;
  auth_type: "api_key" | "oauth" | "vault_credential" | "none";
  ingests: ("knowledge" | "behaviour" | "telemetry" | "actions")[];
  transport: "mcp_stdio" | "mcp_http" | "local";
  implementation_status: "implemented" | "stub_coming_soon";
  description: string;
  status: CatalogStatus;
  connectable: boolean;
}
export interface CatalogZone { zone: string; connectors: CatalogConnector[] }
// A2: a declared config input. `secret` fields are the credential — the form renders them
// write-only (password, never prefilled, never sent back down).
export interface ConfigField {
  name: string; label: string; secret: boolean; required: boolean; placeholder?: string | null;
}
export interface CatalogDetail extends CatalogConnector {
  config_requirements: string[];
  config_fields: ConfigField[];
  instance_kind: string | null;
  instance_id: string | null; // this workspace's configured instance for this kind, or null
}

// ---- endpoints ---------------------------------------------------------
export const api = {
  listRuns: () => req<RunSummary[]>("/runs"),
  getRun: (id: string) => req<RunDetail>(`/runs/${id}`),
  createRun: (skill_slug: string, inputs: Record<string, unknown>) =>
    req<{ run_id: string; status: string }>("/runs", {
      method: "POST",
      body: JSON.stringify({ skill_slug, inputs }),
    }),
  // Natural-language dispatch: resolves to a skill + entities, or returns candidates.
  createRunNl: (nl: string) =>
    req<NlResult>("/runs", { method: "POST", body: JSON.stringify({ nl }) }),
  cancelRun: (id: string) => req(`/runs/${id}/cancel`, { method: "POST" }),

  discoverConnector: (id: string) =>
    req<{ discovered_schema: any }>(`/connectors/${id}/discover`, { method: "POST" }),
  putMapping: (id: string, field_mapping: Record<string, any>) =>
    req<{ status: string }>(`/connectors/${id}/mapping`, {
      method: "PUT",
      body: JSON.stringify({ field_mapping }),
    }),

  listConnectors: () => req<Connector[]>("/connectors"),
  createConnector: (body: Record<string, unknown>) =>
    req<Connector>("/connectors", { method: "POST", body: JSON.stringify(body) }),
  // A2 edit: rotate-or-keep credential. Omit `credentials` to keep the stored secret;
  // the stored secret is never returned, so this is write-only.
  updateConnector: (id: string, body: Record<string, unknown>) =>
    req<Connector>(`/connectors/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  testConnector: (id: string) =>
    req<{ status: string; tools?: string[]; error?: string }>(
      `/connectors/${id}/test`, { method: "POST" }),
  deleteConnector: (id: string) => req(`/connectors/${id}`, { method: "DELETE" }),

  listSkills: () => req<Skill[]>("/skills"),
  listProposed: (page = 1, pageSize = 20) =>
    req<{ items: ProposedSkill[]; total: number; page: number; page_size: number }>(
      `/skills/proposed?page=${page}&page_size=${pageSize}`
    ),
  approveSkill: (id: string, note?: string) =>
    req(`/skills/${id}/approve`, { method: "POST", body: JSON.stringify({ note: note || null }) }),
  rejectSkill: (id: string, note?: string) =>
    req(`/skills/${id}/reject`, { method: "POST", body: JSON.stringify({ note: note || null }) }),

  listTokens: () => req<Token[]>("/tokens"),
  createToken: (body: { name?: string; expires_at?: string }) =>
    req<CreatedToken>("/tokens", { method: "POST", body: JSON.stringify(body) }),
  revokeToken: (id: string) => req(`/tokens/${id}`, { method: "DELETE" }),

  listSchedules: () => req<Schedule[]>("/schedules"),
  createSchedule: (body: Record<string, unknown>) =>
    req<Schedule>("/schedules", { method: "POST", body: JSON.stringify(body) }),
  patchSchedule: (id: string, body: Record<string, unknown>) =>
    req(`/schedules/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteSchedule: (id: string) => req(`/schedules/${id}`, { method: "DELETE" }),

  listAudit: () => req<AuditEntry[]>("/audit"),

  listActions: (state?: string) =>
    req<Action[]>(`/actions${state ? `?state=${state}` : ""}`),
  approveAction: (id: string) => req(`/actions/${id}/approve`, { method: "POST" }),
  dryRunAction: (id: string) => req<{ plan: any }>(`/actions/${id}/dry-run`, { method: "POST" }),
  denyAction: (id: string) => req(`/actions/${id}/deny`, { method: "POST" }),

  // ---- M8 knowledge loop ----
  listChunks: (process_key?: string) =>
    req<KnowledgeChunk[]>(`/knowledge/chunks${process_key ? `?process_key=${encodeURIComponent(process_key)}` : ""}`),
  ingestKnowledge: (path: string) =>
    req<{ job_id: string; kind: string }>("/knowledge/ingest", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  reconcile: (process_key: string, disposition?: string, rationale?: string) =>
    req<{ job_id: string; kind: string }>("/knowledge/reconcile", {
      method: "POST",
      body: JSON.stringify({ process_key, disposition, rationale }),
    }),
  jobStatus: (id: string) => req<JobStatus>(`/jobs/${id}`),

  listFindings: (process_key?: string, state?: string) => {
    const q = new URLSearchParams();
    if (process_key) q.set("process_key", process_key);
    if (state !== undefined) q.set("state", state);
    const s = q.toString();
    return req<Finding[]>(`/findings${s ? `?${s}` : ""}`);
  },
  triageFinding: (id: string, state: FindingState) =>
    req<{ id: string; state: FindingState }>(`/findings/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ state }),
    }),

  listProcesses: () => req<ValidatedProcess[]>("/processes"),
  getProcess: (key: string) => req<ValidatedProcess>(`/processes/${encodeURIComponent(key)}`),
  processVersions: (key: string) =>
    req<ValidatedProcess[]>(`/processes/${encodeURIComponent(key)}/versions`),
  signoffProcess: (key: string) =>
    req<{ signed_off: string; version: number }>(
      `/processes/${encodeURIComponent(key)}/signoff`, { method: "POST" }),

  // ---- M8 LLM providers ----
  // NOTE: there is NO attachScorecard call. Scorecards are server-measured; the UI
  // has no path to assert `holds`. Promotion is gated by the backend (409 if not held).
  listProviders: () => req<Provider[]>("/llm/providers"),
  proposeProvider: (body: {
    provider: string; model: string; api_key?: string; api_base?: string;
  }) =>
    req<{ id: string; residency: string; residency_warning: string | null }>(
      "/llm/providers", { method: "POST", body: JSON.stringify(body) }),
  // ---- A1 catalog (read-only) ----
  getCatalog: () => req<{ zones: CatalogZone[] }>("/catalog"),
  getCatalogEntry: (key: string) => req<CatalogDetail>(`/catalog/${encodeURIComponent(key)}`),

  // Returns the backend's verdict verbatim — promoted, or the 409 not-held message.
  // The UI DISPLAYS this; it cannot manufacture a hold.
  promoteProvider: async (id: string): Promise<{ promoted: boolean; detail?: string }> => {
    const res = await fetch(`/api/v1/llm/providers/${id}/promote`, {
      method: "POST",
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    if (res.ok) return { promoted: true };
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail ?? detail; } catch { /* keep status */ }
    return { promoted: false, detail };
  },
};
