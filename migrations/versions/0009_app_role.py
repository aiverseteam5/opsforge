"""M6.6: restricted opsforge_app role — make RLS isolation enforced, not just enforceable

Revision ID: 0009_app_role
Revises: 0008_validated_processes
Create Date: 2026-06-21

The api + worker processes connect as this NOSUPERUSER / NOBYPASSRLS role so the
FORCE-RLS net on `jobs` and the M6 knowledge tables is actually ENFORCED — a
superuser bypasses RLS, which is why the net was dormant. Only migrate/admin uses
the superuser role (it needs DDL). The app role gets DML on every table + USAGE on
every sequence (incl. the *_seq columns) but no DDL, so the running app can never
create/alter schema or cross orgs.

Idempotent. Dev-grade password — production provisions the role and its secret
out of band; this migration just guarantees the role exists with the right grants.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_app_role"
down_revision: str | None = "0008_validated_processes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "opsforge_app"
APP_PW = "opsforge_app"  # dev-grade; override in real deployments

_UP = f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PW}'
      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  ELSE
    ALTER ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PW}' NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO {APP_ROLE};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};
-- future tables/sequences created by the owner (i.e. later migrations) too
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE};
"""

_DOWN = f"""
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE};
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE};
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE};
REVOKE ALL ON SCHEMA public FROM {APP_ROLE};
DROP ROLE IF EXISTS {APP_ROLE};
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
