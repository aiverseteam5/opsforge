# Changelog

All notable changes to OpsForge are documented here.

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
