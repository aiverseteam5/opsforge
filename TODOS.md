# OpsForge — Deferred TODOs

This file tracks items deferred from ship reviews, adversarial audits, and pre-landing checks.
Items are added here instead of blocking the ship; they become the first candidates for follow-up PRs.

---

## Phase 4 / A2A Trust — Deferred Items

### Security

- **PinnedTransport HTTPS redesign** (pre-landing, MEDIUM): `_PinnedTransport` in `skills.py` rewrites the URL host to the resolved IP but keeps the `Host` header as the original hostname. This works for most HTTPS servers (SNI uses the `Host` header) but breaks for servers that validate the IP-based URL via TLS certificate CN/SAN. Proper fix: use `httpx.HTTPTransport` with a custom resolver that pins the IP at the socket level (not URL rewrite). Deferred: requires httpx internals change; document known limitation in SSRF guard docstring.

### Observability

- **Thundering herd on health score cache** (adversarial, LOW): `_ORG_CACHE` in `health_score.py` uses an `asyncio.Lock` per org, but under cold-start all requests for the same org acquire the lock simultaneously and only the first populates the cache. Add a `_computing: set[str]` sentinel to coalesce concurrent cache misses. Deferred: low impact until multi-worker deployments under load.

### Operator Runbooks

- **`OPSFORGE_TOKEN_HMAC_SECRET` rotation procedure** (pre-landing): Document in `deploy/README.md`. Steps: (1) re-issue all API tokens, (2) update `OPSFORGE_TOKEN_HMAC_SECRET` in secret store, (3) **full** deployment restart (NOT rolling — `get_settings()` uses `@lru_cache`; mixed-key deployments cause 401s during rollout). Mark `token_version=0` tokens as invalid in the DB to force re-auth.

- **`@lru_cache` + multi-worker deployment note** (pre-landing): `get_settings()` is cached per process. In multi-worker deployments (e.g., `uvicorn --workers 4`), a secret rotation requires a full restart (not SIGHUP) of all workers simultaneously. Add note to Helm chart `values.yaml` and `deploy/README.md`.

---

## Phase 5b Candidates

- **E1 — AI Postmortem Generation** (CEO review, deferred): Requires production incident data to validate LLM output quality. Phase 5b candidate after real-world incident patterns accumulate.

- **Multi-org hierarchy activation**: `org_ancestors` table and `org_ancestors` RLS policy (schema landed in Phase 5a; policies and control plane API deferred to Phase 5b when real parent→child org use case arrives).

---

## Resolved in Phase 5a (removed)

The following items were resolved and removed on 2026-06-28:
- HMAC/JWT key coupling → shipped `OPSFORGE_DELEGATION_SIGNING_KEY` with fallback
- Unredacted runbook content in `jobs.payload` → shipped `redact(text_content)` in `codify_from_url`
- `/runs/{id}/timeline` delegation scope exposure → shipped `scope` strip for delegation callers
- `token_version` migration incompatibility → rejected lazy migration (migration 0024 security decision stands); shipped improved 401 `WWW-Authenticate` header

---

## How to use this file

Each item links to its origin review. When picking up a deferred item:
1. Create a GitHub issue referencing the TODO slug (e.g., `TODO: key-coupling`)
2. Remove the item from this file once the issue is open
3. Ship fixes on a dedicated branch, not on the main feature branch

Last updated: 2026-06-28 (Phase 5a: multi-org + security hardening)
