"""Central configuration. All environment access lives here (doctrine: one place).

Both the `api` and `worker` processes import `get_settings()` and receive the
same env block from Docker Compose. Env vars are prefixed `OPSFORGE_` to avoid
clobbering generic names (e.g. a stray `DATABASE_URL` injected by an image).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fixed single-org id for the MVP. Every row carries org_id so multi-tenancy is
# a no-migration-rewrite change later; in v1 it is simply this constant.
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="OPSFORGE_",
        extra="ignore",
    )

    # Connection string for SQLAlchemy async engine (psycopg3 driver).
    database_url: str = Field(
        default="postgresql+psycopg://opsforge:opsforge@db:5432/opsforge"
    )
    # urlsafe-base64 32-byte key for the Fernet credential vault. Required.
    fernet_key: str = Field(default="")
    # Shared secret for HMAC-verifying inbound alert/change webhooks.
    webhook_secret: str = Field(default="")

    org_id: str = DEFAULT_ORG_ID

    # Re-sync each connector's graph this often (seconds). Default 10 min.
    graph_sync_interval_s: int = 600

    # Default model string for the ModelGateway (any litellm-routable id:
    # cloud, private OpenAI-compatible, or local). Per-skill/per-run overrides win.
    model: str = "claude-sonnet-4-6"
    embedding_model: str = "text-embedding-3-small"

    # Directory of built-in skill packs (relative to the working dir).
    skills_dir: str = "skills"

    # Slack surface. Bot token posts reports; signing secret verifies inbound
    # events/commands. Empty in dev → posting is skipped (rendered, not sent).
    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    # Trust graduation: an admin may grant auto_with_notify to a reversible tool
    # only after this many clean (succeeded) executions of it.
    graduation_min_executions: int = 3

    # Knowledge confidence formula (M6.2). Confidence is a deterministic function
    # of evidence — never an LLM judgement — so weights live in config, not code.
    # confidence = clamp01(w_source*(rank/3) + w_fresh*decay(age)
    #                      + w_corroborate*sat(corroborated) - w_contradict*sat(contradicted))
    confidence_w_source: float = 0.40
    confidence_w_fresh: float = 0.25
    confidence_w_corroborate: float = 0.25
    confidence_w_contradict: float = 0.30
    # Half-life (days) of the freshness decay: a chunk this old scores 0.5 on
    # freshness, twice as old 0.25, etc.
    confidence_freshness_halflife_days: int = 180
    # Half-saturation count for corroboration/contradiction: this many agreeing
    # (or conflicting) chunks contributes 0.5 of that term.
    confidence_saturation_k: float = 3.0

    # Reconciliation (M6.3): when two chunks conflict, the newer is treated as
    # superseding the older (staleness, auto) only if it is at least this many
    # days newer AND of the same source kind. Below this gap (or across kinds) the
    # conflict is surfaced for resolution instead.
    reconcile_staleness_days: int = 30

    # Behaviour pattern threshold (M7.5): a TICKET-SOURCED (origin-bearing) behaviour
    # claim reaches behaviour-rank trust — and may override a document — only if its
    # agreeing cluster spans at least this many DISTINCT, provenance-disjoint origins.
    # Behaviour is a pattern, not an event: a single ticket, or volume from one origin
    # (repetition is not corroboration), stays below threshold and is demoted to a
    # "seen once — not yet a pattern" finding. Origin-less (human-asserted) behaviour
    # is not gated. Bias to the safe error: when uncertain, demote. Default 2 = at
    # least one independent corroborating origin.
    behaviour_pattern_min_origins: int = 2

    # (M7.5's behaviour_origin_min_processes corpus-breadth attestation was replaced in
    # M7.6 by connector-VERIFIED identity: an origin's provenance root is its real
    # directory id, set at ingest, so the forgeable breadth heuristic is no longer needed.)

    # LLM credential (M7.6 Job A): production resolves the per-workspace credential from
    # the vault (llm_providers). A `.env` provider key is a LOCAL-DEV-ONLY fallback, used
    # only when this flag is on AND the workspace has no active vault provider. Production
    # sets this False so a workspace with no credential falls to the lexical floor (never a
    # shared global key) — LLM isolation holds per workspace.
    dev_llm_fallback: bool = True

    # Validated process (M6.4): a generated step whose grounding scores below this
    # is flagged low-confidence ("look hard") on the signoff screen.
    validated_process_low_confidence_threshold: float = 0.5

    # Context grounding (M6.5): the agent may act autonomously only on knowledge
    # at or above this confidence. If the best available knowledge for the run's
    # process is below it, a consequential action is forced to a human gate.
    context_grounding_threshold: float = 0.5

    # Worker queue loop.
    worker_poll_interval_ms: int = 500
    worker_max_attempts: int = 5

    # API server.
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    log_level: str = "INFO"
    environment: str = "dev"

    @model_validator(mode="after")
    def _require_secrets_in_prod(self) -> Settings:
        if self.environment != "dev":
            missing = [
                name
                for name, val in [
                    ("OPSFORGE_WEBHOOK_SECRET", self.webhook_secret),
                    ("OPSFORGE_FERNET_KEY", self.fernet_key),
                ]
                if not val
            ]
            # Slack signing secret is required whenever the bot token is set —
            # an unsigned Slack surface can accept forged trust-ladder approvals.
            if self.slack_bot_token and not self.slack_signing_secret:
                missing.append(
                    "OPSFORGE_SLACK_SIGNING_SECRET "
                    "(required when OPSFORGE_SLACK_BOT_TOKEN is set)"
                )
            if missing:
                raise ValueError(
                    f"Required env vars not set for environment={self.environment!r}: "
                    + ", ".join(missing)
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
