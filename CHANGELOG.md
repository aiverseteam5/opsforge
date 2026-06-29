# Changelog

All notable changes to OpsForge are documented here.

## [0.4.0] — 2026-06-28 — phase-5b/chat-and-postmortem (unreleased)

### Added
- **C1+C2 — Conversations API** — `POST /conversations`, `GET /conversations`, `GET /conversations/{id}/messages`, `POST /conversations/{id}/messages`. Posting a user message triggers `resolve_nl()` automatically and returns an assistant reply containing the dispatched run ID (or disambiguation candidates). Full FORCE RLS via `opsforge.current_org` GUC.
- **C3 — `/chat` workbench page** — Three-panel chat-first interface (conversation list + thread + input). User messages trigger NL dispatch; assistant replies render dispatched run status with live event streaming. Nav item added to sidebar.
- **C4/E1 — AI Postmortem** — `POST /runs/{id}/postmortem` enqueues a `postmortem` worker job. The job reads run events, calls the LLM to write a blameless postmortem (timeline, root cause, action items, confidence), and stores the result in the `patterns` table for future similarity search. Returns 409 for non-terminal runs.
- **C5 — Postmortem Slack delivery** — `handle_postmortem` optionally posts a Block Kit postmortem summary to a Slack channel after storing the pattern. Uses `skill_review_channel` from settings or the optional `channel` override in the payload.
- **C6 — Process discovery setup endpoint** — `POST /knowledge/setup` orchestrates all ingestion jobs in one call: local markdown paths (`ingest`), knowledge connectors (`ingest_knowledge` + `ingest_tickets`), and Slack history (`ingest_slack_history`). Returns a job manifest for polling.
- **C7 — Slack history ingestion** — `handle_ingest_slack_history` worker job fetches past incident threads from a Slack channel via the Web API and stores them as `behaviour` knowledge chunks with provenance, ready for process discovery.
- **C8 — `org_ancestors` RLS** — Migration `0030_org_ancestors_rls.py` enables `FORCE ROW LEVEL SECURITY` on `org_ancestors` with an ancestor-chain isolation policy (`USING: org_id = GUC OR ancestor_id = GUC`; `WITH CHECK: org_id = GUC` only — prevents an org from claiming to be another org's ancestor). Re-grants `INSERT, UPDATE` to `opsforge_app` (revoked in Phase 5a until this policy was ready).
- **Org Ancestors API** — `GET /api/v1/orgs/{id}/ancestors` lists the ancestor chain; `POST /api/v1/orgs/{id}/ancestors` adds an ancestor relationship (admin role required, idempotent). Both endpoints enforce org-scoped access via RLS and the application-layer org-match check.

### Changed
- `JobKind` Literal extended with `"postmortem"` and `"ingest_slack_history"`.
- Worker `HANDLERS` dict updated with `handle_postmortem` and `handle_ingest_slack_history`.
- `conversations` and `postmortem` API types added to the workbench `api.ts` client.
- Sidebar navigation now includes a **Chat** link above the Knowledge section.

## [0.3.0] — 2026-06-28 — phase-5/multi-org

### Added
- **`orgs` table** — gives `org_id` UUIDs a real home with a PK, name, and parent reference. FORCE RLS with org-isolation policy. Backfills from all 26 existing tables via UNION ALL so all existing org identities are registered.
- **`org_ancestors` join table** — pre-materialized ancestor chains for the multi-org control plane (schema-only; INSERT/UPDATE revoked from `opsforge_app` until Phase 5b policy lands).
- **Dedicated delegation signing key** — `OPSFORGE_DELEGATION_SIGNING_KEY` decouples JWT signing from API token HMAC; rotating one no longer invalidates the other. Falls back to `OPSFORGE_TOKEN_HMAC_SECRET` with a startup WARNING when unset.

### Changed
- `GET /api/v1/runs/{id}/timeline` — delegation token callers no longer receive the `scope` field in event payloads (scope belongs to the issuing run's trust context, not the caller's view). Regular API token callers see full payloads unchanged.
- `GET /api/v1/runs/{id}/events` SSE — same scope-strip applied consistently to both timeline and streaming endpoints for delegation callers.
- `POST /api/v1/skills/from-url` — runbook content is redacted (`redact()`) before storage in `jobs.payload`; inline `key=value` secrets and Fernet tokens are masked before leaving the fetch boundary.

### Security
- Delegation signing key minimum-length validation: `OPSFORGE_DELEGATION_SIGNING_KEY` and `OPSFORGE_TOKEN_HMAC_SECRET` must be ≥ 32 bytes; shorter keys are rejected at startup with a clear error rather than silently used.
- `delegation_tokens` jti lookup now includes explicit `AND org_id = :org_id` predicate as defense-in-depth alongside the RLS GUC.
- `_verify_delegation_jwt` — RuntimeError (key misconfiguration) now surfaces as 500 rather than being swallowed as 401 "Invalid delegation token"; operators see key problems in logs immediately.
- `org_ancestors` INSERT/UPDATE revoked from `opsforge_app` to prevent table pre-poisoning before Phase 5b RLS policy lands.
- All 401 responses on Bearer-protected endpoints now include `WWW-Authenticate: Bearer error="invalid_token"` per RFC 6750.
- `OPSFORGE_DELEGATION_SIGNING_KEY` added to production startup validator — deploy without it fails loudly at boot rather than silently falling back.

## [0.2.0] — 2026-06-28 — phase-4/a2a-trust

### Added
- **HMAC-SHA256 API token hashing** — `OPSFORGE_TOKEN_HMAC_SECRET` env var replaces plain SHA-256 token hashing; falls back to SHA-256 in dev when unset (`token_version` column tracks hash scheme per token).
- **Signed delegation tokens** — `POST /orgs/{org_id}/delegation-tokens` issues short-lived HS256 JWTs (max 15 min, `jti` revocation, org-scoped) for A2A trust across agent dispatch boundaries. `delegation_tokens` table with FORCE RLS.
- **`scope` enforcement on `Principal`** — delegation tokens carry a `scope: list[str]` of allowed tool IDs; the policy engine enforces it on every tool dispatch (403 on scope violation).
- **Premium workbench UI** — inline SVG icon system, toast notifications, confirm-dialog hook (replaces `window.confirm`), skeleton loading, hero stat cards, live nav badges.
- **E2 — Incident War Room / Timeline** — `GET /api/v1/runs/{id}/timeline` with seq/cursor pagination; workbench page `/runs/:id/timeline` with live 5s refresh and per-kind icons.
- **E3 — Predictive Health Scoring** — `GET /api/v1/health-score` uses ANN similarity on `patterns` (HNSW) against 24h event activity; 5-min in-memory cache per org; graceful empty when < 3 patterns; Mission Control health widget.
- **E4 — Runbook URL → Skill Codification** — `POST /api/v1/skills/from-url` with SSRF guard (resolve-once + RFC1918 blocklist + DNS-rebinding via IP-direct httpx transport); `codify_from_url` worker job; "Codify from URL" panel on Skills page.
- **E5 — Slack /opsforge command** — `/opsforge investigate <target>` slash command handler; immediate 200 ack (3-second constraint); async `response_url` dispatch post; timestamp replay-attack protection.
- **E6 — Trust Ladder visibility** — `GET /api/v1/trust-ladder` with per-tool execution/graduation stats; `/trust-ladder` workbench page with progress bars; all-time clean execution counts (no time window).

### Changed
- `hash_token()` in `security.py` now uses HMAC-SHA256 keyed on `OPSFORGE_TOKEN_HMAC_SECRET` when set; all new tokens get `token_version=1`.
- `require_token()` validates `token_version`; version-0 (SHA-256) tokens are rejected when `OPSFORGE_TOKEN_HMAC_SECRET` is configured.
- Workbench buttons: `.btn-primary` / `.btn-danger` CSS classes replace all inline `style={{ borderColor }}` hacks.

### Security
- F1: `require_token()` now rejects SHA-256 tokens when HMAC is configured.
- F2: `delegation.verify_delegation_token()` validates `org_id` claim against the request context.
- F3/F4: `delegation_tokens` table has `FORCE ROW LEVEL SECURITY` with org-isolation policy.
- Pre-landing: `health_score.py` SQL → parameterized query (vector similarity was f-string injectable).
- Pre-landing: `security.py` JWT revocation check now includes `AND expires_at > now()`.
- Pre-landing: `_verify_delegation_jwt` removed circular self-validation; org_id now extracted from verified claims only.
- Pre-landing: `_signing_key()` raises `RuntimeError` in non-dev environments when `OPSFORGE_TOKEN_HMAC_SECRET` is unset.
- Adversarial: `_ssrf_safe_fetch` enforces `https://` only before DNS resolution; streaming body with 256 KB cap prevents unbounded memory.
- Adversarial: Slack dispatch failure returns generic error (no internal detail leak); background asyncio tasks held via strong reference to prevent GC.
- Adversarial: `_handle_propose()` scope check now also blocks delegation callers from out-of-scope proposals via `RESERVED_PROPOSE`.
- Migration 0027: `ix_actions_org` and `ix_run_events_run_created` perf indexes.

---

## [0.1.0] — phase-3/codify-loop

### Added
- Codify loop: agent runs that complete successfully produce a candidate `ValidatedProcess`; low-confidence steps are flagged for human review before signoff.
- Knowledge & Truth Plane (M6): vector-search over ingested runbook chunks, grounding confidence scoring per process step.
- Conflict resolution: `reconcile.py` merges overlapping chunk evidence; `min_confidence` propagated to the process record.
- Workbench: Processes page (draft/signoff flow), Proposed Skills page (approve/reject with notes).
- RLS on `delegation_tokens`, `jobs`, `run_events` (FORCE RLS enforced on restricted role).
- Helm chart (`deploy/helm/`): securityContext, resource limits, pod anti-affinity.

### Changed
- GUC isolation hardened: `is_local=True` on all `opsforge.current_org` SET calls; tests verify GUC does not leak between connections.
- Trusted-proxy gate: `X-Forwarded-For` only trusted when `OPSFORGE_TRUSTED_PROXY=true`.
- Worker startup asserts `opsforge_app` restricted role before processing any jobs.

---

## [0.1.0-alpha] — phase-1 / phase-2

### Added
- M0: schema, `SKIP LOCKED` job queue, append-only audit triggers.
- M1: MCP connector client, operational graph (topology + change timeline).
- M2: Agent loop, `incident-investigation` skill, eval scorecard, provider-agnostic `ModelGateway`.
- M3: Slack surface, schedule-based dispatch, alert ingest webhook.
- M4: Workbench SPA — Mission Control, Approvals, Processes, Catalog, Tokens, Proposed Skills.
- M5: Trust ladder live — executor, dry-run, auto-rollback, graduation, sub-agents.

### Security
- Fernet vault for all connector credentials.
- `redact()` called at every API boundary.
- HMAC-SHA256 webhook verification.
- Append-only `run_events` + `audit_log` enforced by DB trigger.
