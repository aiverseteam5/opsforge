# OpsForge — Agentic Operations Runtime
## Complete Architecture & Build Specification (v1.0)

> Feed this document to your coding agent as the single source of truth.
> Build milestones in order (M0 → M5). Do not skip ahead. Every milestone has
> acceptance criteria; do not start the next until the current one passes.

---

## 1. Product summary

OpsForge is a self-hosted agentic operations runtime ("AI SRE"). An organization
installs it in their own infrastructure, plugs in **connectors** (MCP servers for
their clouds, Kubernetes, observability, ITSM), installs **skills** (versioned
capability packs encoding domain knowledge + allowed actions + policy), and
dispatches **agents** — instantly ("why is payment-svc throwing 5xx"),
on events (alert webhook → investigate), or on schedules (nightly cert sweep).

Agents investigate by traversing an **operational graph** (topology fused with a
change timeline), produce evidence-chained RCA reports, and propose actions that
pass through a **trust ladder** (read_only → reversible → destructive) with
approval gates, dry-run, and an immutable audit trail. Resolved incidents can be
**codified** into new installed skills (the flywheel).

MVP scope (Phase 1): read-only investigation. One cloud + Kubernetes + one
observability connector. Reports to Slack and the workbench. No execution.
Phase 2 adds reversible actions behind approvals. Phase 3 adds the codify loop.

---

## 2. Architecture doctrine (read before writing any code)

1. **One database.** PostgreSQL 16 + pgvector is the relational store, the
   graph store (nodes/edges tables), the vector store, the job queue
   (`FOR UPDATE SKIP LOCKED`), and the audit log. No Redis, no Kafka, no Neo4j,
   no dedicated vector DB. Add nothing until Postgres measurably fails.
2. **Two processes.** `api` (FastAPI, serves REST + SSE + the SPA) and `worker`
   (queue consumer + scheduler tick). Same codebase, same image, different
   entrypoint. Scale by adding worker replicas later.
3. **The LLM never executes anything.** The agent loop emits *proposals*
   (rows in `actions`). A deterministic policy engine + executor — plain Python,
   no LLM — decides and performs. LLM output is data, never code paths.
4. **Connectors are configuration, not code.** All external systems are MCP
   servers. OpsForge ships zero bespoke integrations; it ships an MCP client,
   a connector registry, and per-connector tool allowlists.
5. **Skills are directories, not plugins.** A skill is a folder with a YAML
   manifest, markdown instructions, optional scripts, and eval scenarios.
   Loaded and validated at startup / install time. No dynamic Python imports
   from skills in MVP (scripts run only in Phase 2+, subprocess-sandboxed).
6. **Append-only where it matters.** `run_events`, `actions` transitions, and
   `audit_log` are insert-only. No UPDATE/DELETE on audit data, enforced by a
   DB trigger.
7. **Minimum code.** Prefer a 200-line hand-rolled component over a framework
   dependency. No LangChain/LangGraph. Target for M0–M3: ≤ ~6k lines of Python,
   ≤ ~3k lines of TypeScript.
8. **Every secret encrypted at rest** (Fernet, master key from env via KMS
   later). Plaintext credentials must never touch logs, run_events, or LLM
   context. Centralize redaction in one function; call it on every boundary.
9. **Modular monolith, enforced.** One deployable, many modules. Module
   boundaries are enforced with `import-linter` contracts (e.g. `policy` may
   not import `agent`; `api/*` may not import `connectors` internals — only
   public functions). The extraction seams — worker, ModelGateway, graph sync —
   must stay clean so a future split is a deployment change, not a rewrite.
   Scaffolding belongs at the extension layer, not the core: the `opsforge
   skill new <slug>` CLI generates a manifest, INSTRUCTIONS.md, policy block,
   and eval stub (this is the project's cookiecutter — skills and connectors
   are the things we make many of, services are not).

---

## 3. Tech stack (locked — do not substitute)

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12, fully typed, Pydantic v2 | Ecosystem for MCP + LLM clients |
| Package mgmt | `uv`, single `pyproject.toml` | Fast, lockfile, no poetry overhead |
| API | FastAPI + uvicorn | Async, SSE support, OpenAPI for free |
| ORM/migrations | SQLAlchemy 2 (async) + Alembic | Boring and correct |
| DB | PostgreSQL 16 + pgvector extension | Doctrine #1 |
| Job queue | Postgres table + `SKIP LOCKED` polling | Zero extra infra |
| Scheduler | Worker tick (30s) over `schedules` table, cron parsing via `croniter` | Zero extra infra |
| LLM gateway | `litellm` behind a 1-file `ModelGateway` protocol | BYO model = config string |
| Connectors | `mcp` (official Python SDK), stdio + streamable-http transports | Doctrine #4 |
| Embeddings | Through ModelGateway (`embedding()` call), pgvector storage | One gateway for all model I/O |
| Frontend | Vite + React 18 + TypeScript + Tailwind, SPA served by FastAPI static | One repo, one deploy |
| Live updates | SSE (`/runs/{id}/events`), no websockets | Simpler, proxy-friendly |
| Slack surface | Raw Events API webhook + Web API via httpx (no Bolt) | ~150 lines, fewer deps |
| Auth (MVP) | Single org; hashed API tokens; session cookie for workbench | OIDC is M5 |
| Crypto | `cryptography` Fernet for credential envelopes | Swappable Vault interface later |
| Tests | pytest + pytest-asyncio; recorded MCP fixtures; golden eval scenarios | See §12 |
| Deploy | Docker Compose (single compose file: db, api, worker) | Helm chart is M5 |

---

## 4. Monorepo layout

```
opsforge/
├── pyproject.toml            # uv-managed; one Python package
├── docker-compose.yml        # db, api, worker
├── Dockerfile                # multi-stage: build SPA → python image
├── server/
│   └── opsforge/
│       ├── main.py           # FastAPI app factory; mounts routers + SPA
│       ├── worker.py         # queue consumer + scheduler tick (entrypoint)
│       ├── config.py         # pydantic-settings; all env in one place
│       ├── db.py             # engine, session, SKIP LOCKED queue helpers
│       ├── models.py         # ALL SQLAlchemy models in one file (~12 tables)
│       ├── security.py       # token auth, Fernet vault, redact()
│       ├── gateway.py        # ModelGateway protocol + litellm impl
│       ├── connectors.py     # MCP client lifecycle, tool discovery, allowlists
│       ├── graph.py          # graph ingest (per-connector mappers) + queries
│       ├── skills.py         # manifest schema, loader, validator
│       ├── policy.py         # deterministic policy engine (pure functions)
│       ├── agent.py          # the agent loop (see §9)
│       ├── actions.py        # action lifecycle + executors (Phase 2)
│       ├── reports.py        # report assembly + rendering (md + Slack blocks)
│       ├── api/
│       │   ├── runs.py       # dispatch, list, SSE stream
│       │   ├── connectors.py # CRUD + test-connection
│       │   ├── skills.py     # list, install, detail
│       │   ├── schedules.py  # CRUD
│       │   ├── actions.py    # list, approve, dry-run (Phase 2)
│       │   └── webhooks.py   # alert ingest + Slack events
│       └── surfaces/
│           └── slack.py      # event handling, block kit rendering, approvals
├── workbench/                # Vite React SPA
│   └── src/
│       ├── pages/            # MissionControl, RunDetail, Skills, Connectors,
│       │                     # Schedules, Audit
│       ├── api.ts            # typed client (generate from OpenAPI)
│       └── sse.ts            # EventSource helper
├── skills/                   # built-in packs (copied into image)
│   └── incident-investigation/
│       ├── skill.yaml
│       ├── INSTRUCTIONS.md
│       └── evals/
│           └── pool_exhaustion.yaml
├── evals/                    # runner: replay scenarios against a model
│   └── run_evals.py
└── migrations/               # alembic
```

---

## 5. Data model (DDL-level; implement exactly, extend cautiously)

All tables get `id UUID PK default gen_random_uuid()`, `created_at timestamptz
default now()`. `org_id UUID` on every row even though MVP is single-org
(multi-tenancy must not require a migration rewrite later).

```sql
-- identity & config
users(id, org_id, email, name, role)                  -- role: admin|operator|viewer
api_tokens(id, org_id, user_id, token_hash, name, last_used_at)

connectors(id, org_id, name, kind,                    -- kind: aws|kubernetes|datadog|slack|custom
  transport,                                          -- stdio|http
  endpoint,                                           -- command or URL
  credentials_enc bytea,                              -- Fernet envelope, NEVER plaintext
  tool_allowlist jsonb,                               -- ["list_pods","get_logs",...]
  status, last_health_at)

skills(id, org_id, slug, version, manifest jsonb,     -- parsed skill.yaml
  instructions text, source,                          -- builtin|org|codified
  trust_overrides jsonb,                              -- org-granted graduations per action
  enabled bool)

-- dispatch & execution
schedules(id, org_id, skill_id, name,
  trigger_kind,                                       -- cron|event
  cron_expr, event_filter jsonb,
  enabled bool, next_run_at, last_run_id)

runs(id, org_id, skill_id, status,                    -- queued|running|reporting|done|failed|cancelled
  parent_run_id,                                      -- null for top-level; set for sub-agent runs (delegation tree)
  trigger jsonb,                                      -- {kind: manual|event|schedule|subagent, payload, surface, user_id}
  model, started_at, finished_at,
  report_md text, report_json jsonb,
  tokens_in int, tokens_out int, cost_usd numeric)

run_events(id, run_id, seq int,                       -- APPEND-ONLY; SSE streams this table
  kind,                                               -- thought|tool_call|tool_result|evidence|proposal|report|error
  payload jsonb)                                      -- redact() before insert

jobs(id, org_id, kind, payload jsonb, status,         -- the queue. kind: run_agent|graph_sync|execute_action
  run_after timestamptz, locked_by, locked_at, attempts int)

-- trust ladder (Phase 2; create tables in M1 anyway)
actions(id, org_id, run_id, skill_id,
  action_class,                                       -- read_only|reversible|destructive
  tool, params jsonb, target_ref,                     -- e.g. "k8s://prod/deploy/payment-svc"
  rollback jsonb,                                     -- tool+params to undo, null if N/A
  state,                                              -- proposed|denied|awaiting_approval|approved|dry_run_done|executing|succeeded|failed|rolled_back
  policy_trace jsonb,                                 -- which rules fired; deterministic, replayable
  approved_by, approved_at, executed_at, result jsonb)

audit_log(id, org_id, seq bigserial,                  -- APPEND-ONLY, DB trigger blocks UPDATE/DELETE
  actor,                                              -- user:<id>|agent:<run_id>|system
  event,                                              -- action.approved, connector.created, run.dispatched, ...
  subject_ref, detail jsonb)

-- operational graph
graph_nodes(id, org_id, kind,                         -- service|pod|node|vm|lb|db|namespace|cluster
  natural_key text UNIQUE,                            -- "k8s://prod/pod/payment-7f9c"
  props jsonb, source_connector_id, last_seen_at)
graph_edges(id, org_id, src_id, dst_id, kind,         -- runs_on|routes_to|depends_on|member_of
  props jsonb, last_seen_at)
changes(id, org_id, kind,                             -- deploy|config|infra|ticketed_change
  ref text, summary text, diff text,
  target_keys text[],                                 -- natural_keys this change touched
  occurred_at timestamptz, source_connector_id)

-- learning (Phase 3; create in M1, populate later)
patterns(id, org_id, run_id, summary text,
  embedding vector(1536), resolution text, outcome jsonb)
feedback(id, org_id, run_id, action_id, verdict,      -- accepted|edited|ignored
  edit_diff text, user_id)
```

Indexes: `runs(org_id,status)`, `jobs(status,run_after)`,
`graph_nodes(natural_key)`, `changes(occurred_at)`, ivfflat on
`patterns.embedding`, `run_events(run_id,seq)`.

---

## 6. Skill manifest (the central contract)

`skills/<slug>/skill.yaml` — validate with a Pydantic model; reject installs
that don't conform. Tools not listed under `tools:` are invisible to the agent.

```yaml
schema: opsforge/skill/v1
slug: incident-investigation
version: 0.1.0
name: Incident investigation
description: >
  Investigates an alert or a question about a misbehaving service. Traverses the
  ops graph, correlates the change timeline, gathers telemetry, and produces an
  evidence-chained RCA report. Read-only.
triggers: [manual, event]            # event => bind via schedules.event_filter
inputs:
  - {name: query, type: string, required: true}
  - {name: incident_ref, type: string, required: false}
context:
  graph: true                        # inject graph neighborhood of involved nodes
  change_window_hours: 24            # inject changes in window
  similar_patterns: 3                # Phase 3: top-k from patterns table
tools:                               # connector_kind.tool_name → action_class
  - {tool: kubernetes.list_pods,        class: read_only}
  - {tool: kubernetes.get_events,       class: read_only}
  - {tool: kubernetes.get_logs,         class: read_only, redact: true}
  - {tool: datadog.query_metrics,       class: read_only}
  - {tool: aws.describe_instances,      class: read_only}
proposals:                           # actions the skill MAY propose (not execute)
  - {tool: kubernetes.rollback_deploy,  class: reversible,
     rollback: {tool: kubernetes.rollback_deploy, note: "roll forward"}}
  - {tool: kubernetes.restart_pod,      class: reversible}
policy:
  max_tool_calls: 25
  max_runtime_seconds: 420
  forbidden_targets: []              # org overrides merge in, e.g. ["k8s://prod/ns/vault*"]
report:
  format: rca_v1                     # hypothesis + confidence + evidence[] + proposals[]
evals:
  - evals/pool_exhaustion.yaml
```

**Trust resolution (deterministic, in `policy.py`):**
`effective_trust(action_class, skill, org)` →
`read_only`: auto-allow. `reversible`: `awaiting_approval` unless
`skills.trust_overrides` grants `auto_with_notify` for that exact tool (granted
manually by an admin after ≥ N clean approved executions — graduation is a
human act recorded in audit_log, never automatic). `destructive`: always
`awaiting_approval`, never gradable in v1.

---

## 7. Trust-ladder state machine (`actions.state`)

```
proposed ─► policy_check ─┬─► denied                      (rule violation; terminal)
                          ├─► auto_approved ─► executing   (read_only only)
                          └─► awaiting_approval ─► approved ─► [dry_run_done] ─► executing
executing ─┬─► succeeded                                   (terminal)
           └─► failed ─► [rolled_back]                     (auto-rollback if manifest defines one)
```

Rules: every transition inserts an `audit_log` row; `policy_trace` is written at
`policy_check` and is sufficient to replay the decision; approval requires
`role in (admin, operator)` and binds `approved_by`; dry_run renders the exact
tool + params + target diff without calling mutating tools; Phase 1 ships the
machine but every proposal terminates at `awaiting_approval` with the Approve
control hidden ("suggested fix" only).

---

## 8. Connector layer (`connectors.py`)

- One async MCP client session per enabled connector, lazily created, pooled,
  health-checked every 60s (`last_health_at`).
- On connect: `list_tools()` → intersect with `tool_allowlist` → expose to
  agent loop as `{connector_kind}.{tool_name}`.
- `call(tool_fqn, params, run_id)`: resolves connector, decrypts credentials
  into the MCP server env/headers at spawn time only, executes, `redact()`s the
  result, inserts a `tool_call`/`tool_result` pair into `run_events`, returns.
- Graph sync: a `graph_sync` job per connector every 10 min runs a per-kind
  mapper (`graph.py`) that upserts `graph_nodes`/`graph_edges` by `natural_key`
  and emits `changes` rows (K8s: deployments→deploy changes; AWS:
  CloudTrail-lite via describe diffs in v1; CI/CD webhook is the better source —
  accept `POST /webhooks/change` for deploy events from day one).

---

## 9. The agent loop (`agent.py`) — the only place the LLM runs

~300 lines. No framework. Signature:

```python
async def run_agent(run: Run, skill: Skill, gateway: ModelGateway,
                    tools: ToolBelt) -> Report
```

1. **Assemble context** (deterministic): skill INSTRUCTIONS.md; trigger payload;
   graph neighborhood (2 hops from any node whose natural_key matches the
   query/alert, rendered as compact text); `changes` in the manifest window;
   (Phase 3) top-k similar patterns. Hard token budget; truncate oldest first.
2. **Loop** (max `policy.max_tool_calls`): call `gateway.chat()` with the tool
   schemas from the manifest only. For each tool call: policy pre-check
   (read_only? allowed target?) → `connectors.call()` → append result. Every
   step inserts `run_events` (SSE picks these up live).
3. **Proposals**: the model emits proposals via a reserved `propose_action`
   tool (defined by OpsForge, not connectors). Each becomes an `actions` row in
   `proposed` state — the loop never executes them.
3b. **Sub-agents (Phase 2+, structured delegation only)**: a second reserved
   tool, `dispatch_subagent(skill_slug, inputs)`, available only if the parent
   manifest lists the target under a `subagents:` allowlist. Implementation is
   recursion over `run_agent` with a child `runs` row (`parent_run_id` set),
   inherited remaining budgets (tool calls, runtime, tokens), max depth 2.
   The child returns its `rca_v1` report as the tool result. No shared
   scratchpads, no sibling-to-sibling messaging, no free-form agent dialogue —
   delegation is a tree of contracts, serialized like everything else.
4. **Report**: final structured output validated against `rca_v1` Pydantic
   model: `{hypothesis, confidence: high|medium|low, evidence: [{claim,
   source_tool, raw_ref}], proposals: [action_id], next_checks: []}`. If the
   model can't reach `medium`, the report must say so and list what evidence is
   missing — never bluff. Render to markdown + Slack blocks in `reports.py`.

`ModelGateway` protocol: `chat(messages, tools, model) -> (text, tool_calls,
usage)` and `embedding(texts) -> vectors`. One litellm implementation; model
string comes from org settings or per-skill override.

---

## 10. API surface (FastAPI; all under `/api/v1`; OpenAPI is the contract for the SPA)

```
POST   /runs                      # {skill_slug, inputs, model?} → run_id (enqueues run_agent job)
GET    /runs?status=&skill=       # list
GET    /runs/{id}                 # detail incl. report
GET    /runs/{id}/events          # SSE stream of run_events (live + replay from seq=0)
POST   /runs/{id}/cancel

GET    /connectors                # list (credentials never serialized)
POST   /connectors                # create; immediately health-check
POST   /connectors/{id}/test
DELETE /connectors/{id}

GET    /skills                    # installed, with trust summary
POST   /skills/install            # upload tar/zip of a skill dir; validate manifest
GET    /skills/{slug}

GET    /schedules  POST /schedules  PATCH /schedules/{id}  DELETE /schedules/{id}

GET    /actions?state=awaiting_approval
POST   /actions/{id}/approve      # Phase 2 enables execution; Phase 1 returns 409
POST   /actions/{id}/dry-run      # Phase 2
GET    /audit?subject=&actor=

POST   /webhooks/alert            # generic alert ingest → matches schedules.event_filter → dispatch
POST   /webhooks/change           # deploy/config events → changes table
POST   /webhooks/slack            # Slack Events API (url_verification + events + interactivity)
GET    /graph/neighborhood?key=&hops=2
GET    /healthz
```

---

## 11. Surfaces

**Slack (`surfaces/slack.py`, M3):** slash command `/ops <query>` and
app-mention → `POST /runs`; on `run.done`, post the report as Block Kit (header,
hypothesis, numbered evidence with source tags, proposal section). Approve /
Dry-run / Dismiss buttons → interactivity payload → actions API (Phase 1 renders
"suggested fix", no buttons). Teams adapter follows the same 4-function
interface (`on_message`, `on_action`, `render_report`, `notify`) in M5.

**Workbench (SPA, M4):** six pages mapping 1:1 to the API: Mission Control
(live runs via SSE, schedules, connector chips), Run Detail (streamed
events timeline — the trust-building screen), Skills, Connectors, Schedules,
Audit. Command palette (⌘K) = `POST /runs` with skill `incident-investigation`
and free-text query. No state management library; React Query only.

---

## 12. Testing & evals (quality is the moat)

- Unit: policy engine (every transition + denial path), manifest validation,
  redaction, queue claim semantics.
- Integration: agent loop against a **fake MCP server** (in-repo, serves
  recorded fixtures) — runs in CI with a stub gateway, no real LLM.
- **Golden evals** (`evals/run_evals.py`): each scenario YAML defines trigger,
  fixture set, and assertions (`hypothesis_must_mention`,
  `must_cite_change_ref`, `max_tool_calls`). Run nightly against each
  configured model; output a per-model scorecard. A model is "certified" for a
  skill when its scorecard passes. This is the BYO-model answer — build it in M2,
  not later.

---

## 13. Security non-negotiables (enforced in code review, not docs)

1. Credentials: Fernet-encrypted at rest, decrypted only at MCP spawn, never in
   `run_events`/logs/LLM context. `redact()` runs on every tool result.
2. The executor refuses any `actions` row whose `policy_trace` is absent or
   whose state machine path is invalid — defense in depth against API misuse.
3. Tool exposure = manifest ∩ connector allowlist. No wildcard tools.
4. `audit_log` + `run_events`: DB triggers reject UPDATE/DELETE.
5. SPA auth: httpOnly session cookie; API: bearer tokens, hashed at rest.
6. All webhooks signature-verified (Slack signing secret; HMAC header for
   alert/change webhooks).

---

## 14. Build order (each milestone is shippable; do not parallelize)

**M0 — Skeleton (≈ day 1–3).** Repo layout, pyproject, compose (db+api+worker),
models.py + initial migration, config, healthz, token auth, jobs queue with
SKIP LOCKED claim + worker loop, audit triggers. *Accept:* `docker compose up`
green; a `noop` job enqueued via psql is claimed and completed exactly once
with 3 workers racing.

**M1 — Connectors + graph (≈ day 4–8).** MCP client lifecycle, connector CRUD +
test, allowlists, redaction, graph mappers for kubernetes + one cloud + one
observability kind, change webhook, graph_sync jobs, neighborhood query.
*Accept:* connect a kind cluster + Prometheus-compatible fixture; graph query
for a service returns pods/nodes/edges; a posted deploy webhook appears in
`changes`.

**M2 — Agent loop + first skill + evals (≈ day 9–16).** ModelGateway, skill
loader/validator, `opsforge skill new` scaffolding CLI, incident-investigation
pack, agent loop with run_events streaming, rca_v1 report, runs API + SSE, eval
runner with 3 golden scenarios.
*Accept:* `POST /runs` with "why is payment-svc failing" against fixtures yields
a report whose hypothesis cites the seeded deploy change; evals pass on the
default model; zero mutating tool calls possible (none exposed).

**M3 — Slack surface + schedules + alert ingest (≈ day 17–22).** Slack webhook
+ Block Kit reports, `/ops` command, schedules CRUD + scheduler tick, alert
webhook → event-filter dispatch. *Accept:* a fired test alert produces an
unprompted RCA report in a Slack channel in < 5 min. **This is the Phase-1 MVP.**

**M4 — Workbench (≈ day 23–30).** SPA, six pages, SSE run detail, ⌘K dispatch.
*Accept:* full demo flow without touching curl.

**M5 — Phase 2: trust ladder live (≈ day 31–60).** Executor + dry-run +
rollback, approval API + Slack buttons, reversible tools added to manifest
exposure, JIT credential leases, graduation flow (admin-granted), Helm chart,
OIDC. *Accept:* approved rollback executes against a staging cluster, auto-
rollback on failed health check, every step visible in audit; denied-path tests
pass.

Phase 3 (codify loop: postmortem draft → proposed skill → install) is specced in
the manifest (`source: codified`) and patterns/feedback tables and is the next
spec document — do not improvise it.

---

## 15. Explicit non-goals for this codebase (v1) — read the distinctions

- **Multi-org control-plane UI** is out. Multi-tenancy itself is NOT out: every
  table carries `org_id` from M0, and the v1 deployment model is one self-hosted
  runtime per org (isolation by deployment — the strongest tenancy). A hosted
  multi-org control plane (licensing, skill distribution, fleet mgmt) is Phase 4.
- **Free-form agent-to-agent dialogue** is out (shared scratchpads, sibling
  chatter, emergent group chat). Structured sub-agent delegation is IN from
  Phase 2 via `dispatch_subagent` + `parent_run_id` (§9, 3b). **A2A protocol**
  compatibility at the boundary (exposing runs as A2A tasks so external
  orchestrators can dispatch OpsForge agents) is Phase 3/4 roadmap, not v1 code.
- Also out for v1: skills marketplace, WhatsApp/Teams surfaces (Slack first;
  the surface adapter interface in §11 is built for them), Neo4j/Kafka/Redis,
  fine-tuning, autonomous destructive actions, building our own observability.
Anything not in §14 is out.

---

## M6 — Knowledge & Truth Plane (shipped, post-spec)

M6 was not in the original M0→M5 spec. It grew out of a hard lesson from M5:
the agent loop was retrieving conflicting or stale facts from different connectors
with no principled way to reconcile them. An action proposed on outdated topology
can be worse than no action. M6 solves this.

**What it adds (~1 750 lines):**

| Module | Role |
|---|---|
| `knowledge.py` | Chunk store: every ingestable fact is a typed, source-ranked, timestamped chunk in `knowledge_chunks` (pgvector). |
| `confidence.py` | Deterministic confidence formula: `clamp01(w_source·rank + w_fresh·decay(age) + w_corroborate·sat(N_agree) − w_contradict·sat(N_conflict))`. Weights live in `config.py`, not code. |
| `reconcile.py` + `reconciliations.py` | Conflict detection and resolution: two chunks that contradict each other within the same topic are surfaced as a `reconciliation` row. Auto-resolved (superseded) when the newer chunk is ≥ `reconcile_staleness_days` fresher AND same source kind; otherwise escalated for human review. |
| `processes.py` + `findings.py` | Validated runbook steps and per-run grounding findings: each step carries a confidence score; steps below `validated_process_low_confidence_threshold` are flagged "look hard" at sign-off. |
| `dispositions.py` | Disposition record for resolved reconciliations (human-accepted, auto-superseded, or dismissed). |
| `knowledge_sources.py` | Per-connector ingest adapters that normalise raw MCP tool output into typed chunks with provenance and source rank. |

**Why it's a product moat, not scope creep:**
OpsForge's trust ladder only works if the agent acts on correct information.
Confidence-gated actions (`context_grounding_threshold`) and reconciliation
before execution are what separate OpsForge from a script with an LLM bolted on.
Competitors (PagerDuty, Datadog AI) act on raw telemetry with no epistemic layer.

**Acceptance criteria (complete):**
- `knowledge_chunks` ingested on every graph sync (connector → source adapter → chunk store)
- Confidence scores attached to every `action` proposal row before policy evaluation
- Conflicting chunks produce `reconciliation` rows; auto-resolution triggers on staleness rule
- Agent loop gates consequential actions: if best-available confidence < `context_grounding_threshold`, force human approval regardless of trust ladder level
- 100% test coverage of confidence formula and reconciliation logic (deterministic, no LLM involvement)

---

## Implementation decisions (build-time, not in original spec)

These resolve ambiguities in the spec; recorded here so they survive sessions.

1. **`org_id`** is a plain constant UUID column on every table (from
   `Settings.org_id`). **No `orgs` table, no FK** in v1 (single-org by
   deployment). org_id = tenancy/ownership tag, not environment detection.
2. **Enum-like columns** = `VARCHAR` + DB `CHECK` constraint + Python
   `Literal[...]`. No native Postgres `ENUM` types (painful to evolve under
   Alembic as status sets grow across milestones).
3. **DB driver** = `postgresql+psycopg://` (psycopg3 unified async). DB image =
   `pgvector/pgvector:pg16`.
4. **Migrations** = hand-written initial migration (extensions pgcrypto+vector,
   tables, append-only triggers, ivfflat). A one-shot `migrate` compose service
   runs `alembic upgrade head` so api + N workers never race.
5. **Append-only enforcement** = one `reject_mutation()` plpgsql function +
   BEFORE UPDATE/DELETE/TRUNCATE triggers on `audit_log` and `run_events`.
6. **LLM** = provider-agnostic via `ModelGateway`/`litellm`; model is a config
   string (cloud, private OpenAI-compatible, or local Ollama/vLLM/LM Studio).
