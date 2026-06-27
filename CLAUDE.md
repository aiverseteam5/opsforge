# OpsForge — Claude Code Guide

## Project overview

Self-hosted agentic operations runtime ("AI SRE"). Agents investigate incidents by
traversing an operational graph, produce evidence-chained RCA reports, and propose
actions gated by a deterministic trust ladder (read_only → reversible → destructive).
The LLM proposes; deterministic Python decides and executes. No LangChain.

**Shipped:** M0–M6 (trust ladder live, knowledge & truth plane, Helm chart).
**Deferred:** Phase 3 codify loop, OIDC/JIT credentials.

## Architecture doctrines (read before editing any code)

1. **LLM never executes.** `agent.py` emits proposals → `policy.py` decides → `actions.py` executes. Never shortcut this chain.
2. **One database.** PostgreSQL only. No Redis, no Kafka, no Neo4j. Add nothing until Postgres measurably fails.
3. **Two processes, one image.** `api` and `worker`. Scale by adding worker replicas.
4. **Connectors are config, not code.** All external systems are MCP servers.
5. **Append-only audit.** `run_events` and `audit_log` are insert-only, enforced by DB trigger. Never UPDATE/DELETE audit data.
6. **Every secret encrypted at rest.** Fernet vault. `redact()` is called at every boundary — never skip it.
7. **Module boundaries enforced.** `import-linter` contracts: `policy` may not import `agent`; `api/*` may not import connector internals.

## Dev setup

```bash
# Generate Fernet key + bring up DB
python -c "from cryptography.fernet import Fernet; print('OPSFORGE_FERNET_KEY='+Fernet.generate_key().decode())" > .env
echo "OPSFORGE_WEBHOOK_SECRET=$(openssl rand -hex 32)" >> .env

# Start DB + run migrations
docker compose up -d db migrate

# Install Python deps
uv venv && uv pip install -e ".[dev]"

# Full stack (api + worker + workbench)
docker compose up -d --build
```

## Commands

```bash
# Run all tests (requires running Postgres)
pytest

# Lint
ruff check . && mypy server && lint-imports

# Run specific test file
pytest tests/test_policy.py -v

# Watch worker logs
docker compose logs -f worker

# Health check
curl http://localhost:8080/healthz
```

## Key files

| File | Purpose |
|---|---|
| `server/opsforge/agent.py` | The agent loop — ONLY place the LLM runs |
| `server/opsforge/policy.py` | Deterministic policy engine — pure functions |
| `server/opsforge/actions.py` | Trust ladder executor + rollback |
| `server/opsforge/models.py` | ALL SQLAlchemy models (one file, by doctrine) |
| `server/opsforge/security.py` | Fernet vault, token auth, `redact()` chokepoint |
| `server/opsforge/knowledge.py` | M6 Knowledge & Truth Plane (ingest + chunks) |
| `server/opsforge/reconcile.py` | M6 conflict resolution + process reconciliation |
| `server/opsforge/config.py` | All env vars in one place (`OPSFORGE_` prefix) |
| `docs/ARCHITECTURE.md` | Original build spec (M0–M5) |
| `docs/AS-BUILT-ARCHITECTURE.md` | As-built reality (includes M6) |

## Testing conventions

- Real Postgres in tests — no DB mocks. Fake MCP servers live in `tests/fake_mcp/`.
- Golden eval scenarios in `skills/*/evals/` — run via `evals/run_evals.py`.
- `test_llm_containment.py` verifies the LLM-never-executes doctrine via import-linter.
- One test file per module. Name: `test_<module>.py`.

## Environment variables (production requirements)

| Var | Required | Notes |
|---|---|---|
| `OPSFORGE_FERNET_KEY` | Yes | Fernet key for credential vault |
| `OPSFORGE_WEBHOOK_SECRET` | Yes (prod) | HMAC-SHA256 for inbound webhooks — MUST be set in production |
| `OPSFORGE_DATABASE_URL` | Yes | PostgreSQL connection string |
| `OPSFORGE_MODEL` | No | Default: `claude-sonnet-4-6` |
| `OPSFORGE_SLACK_BOT_TOKEN` | No | Required for Slack surface |
| `OPSFORGE_ENVIRONMENT` | No | Set to `production` in prod (disables dev fallbacks) |

## Known open items

- OIDC / JIT credential leases — deferred (Phase 3 prerequisite)
- Rate limiting on `/api/v1/webhooks/*` — needed before public exposure
- Multi-tenant RLS on all tables — `org_id` is on every row; RLS policies shipped for `jobs` (migration 0004); others use app-level filtering

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Security audit → invoke /cso
