# OpsForge Helm chart

Deploys the OpsForge runtime (one image, three workloads): a migrate `Job`
(Helm pre-install/upgrade hook), the `api` Deployment + Service, and `worker`
Deployment (default 3 replicas). Bundles a pgvector Postgres `StatefulSet` for
quick starts; point `externalDatabaseUrl` at a managed Postgres 16 + pgvector
for production and set `postgres.enabled=false`.

## Install

```bash
FERNET=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
helm install opsforge deploy/helm/opsforge \
  --set-string fernetKey="$FERNET" \
  --set image.repository=<your-registry>/opsforge \
  --set image.tag=0.1.0
```

Secrets (`fernetKey`, `webhookSecret`, `slackBotToken`, `slackSigningSecret`) are
stored in a Kubernetes `Secret` and injected as `OPSFORGE_*` env. The migrate Job
runs `alembic upgrade head` before api/worker start, so they never race the
schema.

> Note: this chart targets standard Kubernetes objects (StatefulSet/Deployment/
> Service/Job/Secret). Lint/validate with `helm lint deploy/helm/opsforge` and
> `helm template opsforge deploy/helm/opsforge --set-string fernetKey=x`.
