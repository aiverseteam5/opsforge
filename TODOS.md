# OpsForge — Deferred TODOs

This file tracks items deferred from ship reviews, adversarial audits, and pre-landing checks.
Items are added here instead of blocking the ship; they become the first candidates for follow-up PRs.

---

## Phase 4 / A2A Trust — Deferred Items

### Security

- **HMAC/JWT key coupling** (adversarial, MEDIUM): `_signing_key()` in `delegation.py` uses `OPSFORGE_TOKEN_HMAC_SECRET` for both HMAC token hashing and JWT delegation signing. These should be independent secrets so rotating one does not invalidate the other. Deferred: Phase 5 introduces a dedicated `OPSFORGE_DELEGATION_SIGNING_KEY` env var.

- **Unredacted runbook content in `jobs.payload`** (adversarial, LOW): `codify_from_url` stores the full runbook text in `jobs.payload`. If the runbook contains secrets (e.g., embedded tokens in procedural docs), those end up in the DB unredacted. Deferred: add a `redact()` pass on the runbook text before inserting into `jobs.payload`.

- **`/runs/{id}/timeline` delegation scope exposure** (adversarial, LOW): The `timeline` endpoint returns raw `payload` from `run_events`, which may include delegation scope lists (`scope: ["tool_a", "tool_b"]`). For delegation callers, the scope should be stripped from the timeline response. Deferred: add `principal.scope` filter on payload before returning.

- **PinnedTransport HTTPS redesign** (pre-landing, MEDIUM): `_PinnedTransport` in `skills.py` rewrites the URL host to the resolved IP but keeps the `Host` header as the original hostname. This works for most HTTPS servers (SNI uses the `Host` header) but breaks for servers that validate the IP-based URL via TLS certificate CN/SAN. Proper fix: use `httpx.HTTPTransport` with a custom resolver that pins the IP at the socket level (not URL rewrite). Deferred: requires httpx internals change; document known limitation in SSRF guard docstring.

### Observability

- **Thundering herd on health score cache** (adversarial, LOW): `_ORG_CACHE` in `health_score.py` uses an `asyncio.Lock` per org, but under cold-start all requests for the same org acquire the lock simultaneously and only the first populates the cache. Add a `_computing: set[str]` sentinel to coalesce concurrent cache misses. Deferred: low impact until multi-worker deployments under load.

### Operator Runbooks

- **`OPSFORGE_TOKEN_HMAC_SECRET` rotation procedure** (pre-landing): Document in `deploy/README.md`. Steps: (1) re-issue all API tokens, (2) update `OPSFORGE_TOKEN_HMAC_SECRET` in secret store, (3) **full** deployment restart (NOT rolling — `get_settings()` uses `@lru_cache`; mixed-key deployments cause 401s during rollout). Mark `token_version=0` tokens as invalid in the DB to force re-auth.

- **`@lru_cache` + multi-worker deployment note** (pre-landing): `get_settings()` is cached per process. In multi-worker deployments (e.g., `uvicorn --workers 4`), a secret rotation requires a full restart (not SIGHUP) of all workers simultaneously. Add note to Helm chart `values.yaml` and `deploy/README.md`.

### Phase 5 Prerequisites

- **`token_version` migration incompatibility** (pre-landing, deferred): Adding `token_version` column invalidates all pre-existing tokens (no backward-compat bridge for SHA-256 → HMAC upgrade). Phase 5 should provide an opt-in migration window (grace period where both hash schemes are accepted) before hard cutover.

- **Phase 4.3 — Multi-org control plane** (eng-review, deferred): Requires `orgs` table, backfill migration, and ancestor-chain RLS design (`org_ancestors` join table). Design doc required before Phase 5 kickoff.

- **E1 — AI Postmortem Generation** (CEO review, deferred): Requires production incident data to validate LLM output quality. Phase 5 candidate after real-world incident patterns accumulate.

---

## How to use this file

Each item links to its origin review. When picking up a deferred item:
1. Create a GitHub issue referencing the TODO slug (e.g., `TODO: key-coupling`)
2. Remove the item from this file once the issue is open
3. Ship fixes on a dedicated branch, not on the main feature branch

Last updated: 2026-06-28 (Phase 4 ship)
