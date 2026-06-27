# OpsForge — Agentic Operations Runtime

Self-hosted "AI SRE": plug in **connectors** (MCP servers for your cloud/Kubernetes/
observability), install **skills** (versioned capability packs), and dispatch **agents**
that investigate an **operational graph** (topology + change timeline), produce
evidence-chained RCA reports, and propose actions gated by a **trust ladder**
(read_only → reversible → destructive) with approvals and an immutable audit trail.

Bring your own model — cloud, private, or local — via a provider-agnostic `ModelGateway`.

The full architecture & build specification is the single source of truth:
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Status

**M0 → M5 complete.** Phase 1 (read-only investigation → Slack/workbench) and
Phase 2 (the live trust ladder: approve / dry-run / execute / auto-rollback,
graduation, sub-agents) are shipped.

- **M0** Skeleton — schema, migrations, `SKIP LOCKED` job queue, append-only audit triggers
- **M1** Connectors + operational graph (MCP client, per-kind mappers, change webhook)
- **M2** Agent loop + `incident-investigation` skill + eval scorecard (provider-agnostic gateway)
- **M3** Slack surface + schedules + alert ingest — **Phase-1 MVP**
- **M4** Workbench SPA (six pages, live SSE run detail, ⌘K dispatch)
- **M5** Trust ladder live — executor, approvals, graduation, sub-agents, Helm chart

Deferred (documented): OIDC and JIT credential leases. Phase 3 (codify loop) is a
separate spec.

## Quickstart (local stack)

```bash
# 1. Generate required secrets into .env
python -c "
from cryptography.fernet import Fernet
import secrets
print('OPSFORGE_FERNET_KEY=' + Fernet.generate_key().decode())
print('OPSFORGE_WEBHOOK_SECRET=' + secrets.token_hex(32))
" > .env

# 2. Bring up db + migrate + api + 3 workers
docker compose up -d --build

# 3. Health
curl http://localhost:8080/healthz        # {"status":"ok"}
```

## Layout

```
server/opsforge/   FastAPI app, worker, models, queue, security, agent (one package)
migrations/        Alembic (hand-written initial migration)
skills/            built-in skill packs (M2+)
evals/             golden-scenario eval runner (M2+)
workbench/         Vite + React SPA (M4+)
docs/ARCHITECTURE.md   the spec
```

## Development

```bash
uv venv && uv pip install -e ".[dev]"
docker compose up -d db migrate           # tests need Postgres
pytest                                     # unit + integration
ruff check . && mypy server && lint-imports
```
