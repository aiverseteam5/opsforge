"""All SQLAlchemy models in one file (doctrine: one ~15-table schema).

Conventions:
- Every table has `id` (UUID PK, `gen_random_uuid()`), `created_at`, and
  `org_id` (a plain constant UUID in v1 — tenancy tag, no FK, no orgs table).
- Enum-like columns are VARCHAR + a CHECK constraint, typed in Python with
  `Literal[...]`. No native Postgres ENUM types (they are painful to evolve
  under Alembic as status sets grow across milestones).
- `run_events` and `audit_log` are APPEND-ONLY; DB triggers (created in the
  initial migration) reject UPDATE/DELETE/TRUNCATE.

The tables are CREATEd by the hand-written Alembic migration, not by metadata
autogenerate; this module is the typed ORM surface the app uses.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any, Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --------------------------------------------------------------------------- #
# Literal aliases (mirror the CHECK constraints below)
# --------------------------------------------------------------------------- #
UserRole = Literal["admin", "operator", "viewer"]
ConnectorKind = Literal[
    "aws", "kubernetes", "datadog", "servicenow", "jira", "pagerduty", "slack", "custom"
]
ConnectorTransport = Literal["stdio", "http"]
SkillSource = Literal["builtin", "org", "codified"]
TriggerKind = Literal["cron", "event"]
RunStatus = Literal[
    "queued", "running", "reporting", "done", "failed", "cancelled"
]
RunEventKind = Literal[
    "thought", "tool_call", "tool_result", "evidence", "proposal", "report", "error"
]
JobKind = Literal[
    "noop", "run_agent", "graph_sync", "execute_action", "ingest", "reconcile", "ingest_tickets"
]
JobStatus = Literal["queued", "running", "done", "failed"]
ActionClass = Literal["read_only", "reversible", "destructive"]
ActionState = Literal[
    "proposed",
    "denied",
    "awaiting_approval",
    "approved",
    "dry_run_done",
    "executing",
    "succeeded",
    "failed",
    "rolled_back",
]
FeedbackVerdict = Literal["accepted", "edited", "ignored"]
# Knowledge & Truth Plane (M6). source_kind encodes the precedence ladder
# behaviour > document > research; disposition is human-declared per process.
KnowledgeSourceKind = Literal["behaviour", "document", "research"]
ProcessDisposition = Literal["descriptive", "prescriptive", "undeclared"]
# A human declaration only ever sets one of the two real dispositions;
# "undeclared" is the absence of a declaration, never a stored value.
DispositionDeclaration = Literal["descriptive", "prescriptive"]
# Reconciliation findings (M6.3) — the mirror surfaced in the approval queue.
FindingKind = Literal["contradiction", "drift", "gap", "violation", "stale"]
FindingState = Literal["open", "acknowledged", "resolved", "dismissed"]
# Validated process (M6.4) — generated current-version, versioned + signed off.
ValidatedProcessStatus = Literal["draft", "signed_off", "superseded"]

_NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=_NAMING)


class PkMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        server_default=text("now()")
    )


class OrgMixin:
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


def _enum_ck(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


# --------------------------------------------------------------------------- #
# identity & config
# --------------------------------------------------------------------------- #
class User(PkMixin, OrgMixin, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="viewer")
    __table_args__ = (
        _enum_ck("role", ("admin", "operator", "viewer"), "role"),
    )


class ApiToken(PkMixin, OrgMixin, Base):
    __tablename__ = "api_tokens"
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    last_used_at: Mapped[datetime.datetime | None] = mapped_column()


class Connector(PkMixin, OrgMixin, Base):
    __tablename__ = "connectors"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    transport: Mapped[str] = mapped_column(String(20), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    credentials_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    tool_allowlist: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Declarative native→canonical map (ops connectors) + cached describe output.
    field_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    discovered_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="unknown")
    last_health_at: Mapped[datetime.datetime | None] = mapped_column()
    __table_args__ = (
        _enum_ck(
            "kind",
            (
                "aws", "kubernetes", "datadog", "servicenow", "jira",
                "pagerduty", "slack", "confluence", "custom",
            ),
            "kind",
        ),
        _enum_ck("transport", ("stdio", "http"), "transport"),
    )


class Skill(PkMixin, OrgMixin, Base):
    __tablename__ = "skills"
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), nullable=False, server_default="builtin")
    trust_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    __table_args__ = (
        _enum_ck("source", ("builtin", "org", "codified"), "source"),
        Index("ix_skills_org_slug", "org_id", "slug", unique=True),
    )


# --------------------------------------------------------------------------- #
# dispatch & execution
# --------------------------------------------------------------------------- #
class Schedule(PkMixin, OrgMixin, Base):
    __tablename__ = "schedules"
    skill_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    cron_expr: Mapped[str | None] = mapped_column(String(120))
    event_filter: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    next_run_at: Mapped[datetime.datetime | None] = mapped_column()
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (
        _enum_ck("trigger_kind", ("cron", "event"), "trigger_kind"),
    )


class Run(PkMixin, OrgMixin, Base):
    __tablename__ = "runs"
    skill_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    trigger: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime.datetime | None] = mapped_column()
    finished_at: Mapped[datetime.datetime | None] = mapped_column()
    report_md: Mapped[str | None] = mapped_column(Text)
    report_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    __table_args__ = (
        _enum_ck(
            "status",
            ("queued", "running", "reporting", "done", "failed", "cancelled"),
            "status",
        ),
        Index("ix_runs_org_status", "org_id", "status"),
    )


class RunEvent(PkMixin, OrgMixin, Base):
    """APPEND-ONLY. SSE streams this table ordered by (run_id, seq)."""

    __tablename__ = "run_events"
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (
        _enum_ck(
            "kind",
            (
                "thought",
                "tool_call",
                "tool_result",
                "evidence",
                "proposal",
                "report",
                "error",
            ),
            "kind",
        ),
        Index("ix_run_events_run_seq", "run_id", "seq", unique=True),
    )


class Job(PkMixin, OrgMixin, Base):
    """The queue. Claimed with FOR UPDATE SKIP LOCKED (see db.py)."""

    __tablename__ = "jobs"
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    run_after: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    locked_by: Mapped[str | None] = mapped_column(String(120))
    locked_at: Mapped[datetime.datetime | None] = mapped_column()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    __table_args__ = (
        _enum_ck("status", ("queued", "running", "done", "failed"), "status"),
        Index("ix_jobs_status_run_after", "status", "run_after"),
    )


class Action(PkMixin, OrgMixin, Base):
    __tablename__ = "actions"
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    skill_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action_class: Mapped[str] = mapped_column(String(20), nullable=False)
    tool: Mapped[str] = mapped_column(String(200), nullable=False)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    target_ref: Mapped[str | None] = mapped_column(Text)
    rollback: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(30), nullable=False, server_default="proposed")
    policy_trace: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_at: Mapped[datetime.datetime | None] = mapped_column()
    executed_at: Mapped[datetime.datetime | None] = mapped_column()
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (
        _enum_ck(
            "action_class", ("read_only", "reversible", "destructive"), "action_class"
        ),
        _enum_ck(
            "state",
            (
                "proposed",
                "denied",
                "awaiting_approval",
                "approved",
                "dry_run_done",
                "executing",
                "succeeded",
                "failed",
                "rolled_back",
            ),
            "state",
        ),
    )


class AuditLog(PkMixin, OrgMixin, Base):
    """APPEND-ONLY. `seq` is a monotonic bigserial for total ordering."""

    __tablename__ = "audit_log"
    seq: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("nextval('audit_log_seq')"),
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    event: Mapped[str] = mapped_column(String(120), nullable=False)
    subject_ref: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (Index("ix_audit_log_seq", "seq"),)


# --------------------------------------------------------------------------- #
# operational graph
# --------------------------------------------------------------------------- #
class GraphNode(PkMixin, OrgMixin, Base):
    __tablename__ = "graph_nodes"
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    natural_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    props: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_connector_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_seen_at: Mapped[datetime.datetime | None] = mapped_column()
    __table_args__ = (Index("ix_graph_nodes_natural_key", "natural_key", unique=True),)


class GraphEdge(PkMixin, OrgMixin, Base):
    __tablename__ = "graph_edges"
    src_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    dst_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    props: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_seen_at: Mapped[datetime.datetime | None] = mapped_column()
    __table_args__ = (Index("ix_graph_edges_src_dst", "src_id", "dst_id"),)


class Change(PkMixin, OrgMixin, Base):
    __tablename__ = "changes"
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    ref: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    diff: Mapped[str | None] = mapped_column(Text)
    target_keys: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    occurred_at: Mapped[datetime.datetime | None] = mapped_column()
    source_connector_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (Index("ix_changes_occurred_at", "occurred_at"),)


# --------------------------------------------------------------------------- #
# learning (created now, populated in Phase 3)
# --------------------------------------------------------------------------- #
class Pattern(PkMixin, OrgMixin, Base):
    __tablename__ = "patterns"
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    summary: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    resolution: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Feedback(PkMixin, OrgMixin, Base):
    __tablename__ = "feedback"
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    edit_diff: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (
        _enum_ck("verdict", ("accepted", "edited", "ignored"), "verdict"),
    )


# --------------------------------------------------------------------------- #
# knowledge & truth plane (M6)
# --------------------------------------------------------------------------- #
class KnowledgeChunk(PkMixin, OrgMixin, Base):
    """One chunk of ingested org knowledge with its provenance envelope.

    No chunk enters the store without a full envelope (source_kind/ref/rank,
    observed_at≠ingested_at), exactly as the kernel rejects an action without a
    policy_trace. Reconciliation columns are NULL until a reconciliation run
    scores the chunk (M6.2/M6.3); `process_key` is NULL until clustering. The
    ivfflat index on `embedding` is created in the migration (not expressible in
    ORM metadata), mirroring `patterns`.
    """

    __tablename__ = "knowledge_chunks"
    process_key: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    # provenance envelope (assigned at ingest)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    source_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime.datetime] = mapped_column(nullable=False)
    ingested_at: Mapped[datetime.datetime] = mapped_column(nullable=False)
    # reconciliation output (NULL until reconciled)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    corroborated_by: Mapped[int | None] = mapped_column(Integer)
    contradicted_by: Mapped[int | None] = mapped_column(Integer)
    # M7.2: confidence is lifted only by provenance-DISJOINT corroboration.
    # provenance_root is the origin disjointness is judged on (today = source_ref;
    # NULL = indeterminate → fails safe). The *_roots columns record the distinct
    # roots that actually lifted/lowered the score, keeping it explainable.
    provenance_root: Mapped[str | None] = mapped_column(Text)
    # M7.5: the behavioural origin (group/actor/job) of a ticket-sourced chunk;
    # NULL for documents and human-asserted behaviour. Drives the origin-aware
    # provenance root and the behaviour-pattern (distinct-origin) threshold.
    origin: Mapped[str | None] = mapped_column(Text)
    corroborating_roots: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    contradicting_roots: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    process_disposition: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="undeclared"
    )
    reconciliation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (
        _enum_ck(
            "source_kind", ("behaviour", "document", "research"), "source_kind"
        ),
        _enum_ck(
            "process_disposition",
            ("descriptive", "prescriptive", "undeclared"),
            "process_disposition",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence",
        ),
        Index("ix_knowledge_chunks_process_key", "process_key"),
        Index("ix_knowledge_chunks_source_kind", "source_kind"),
        Index("ix_knowledge_chunks_confidence", "confidence"),
    )


class ProcessDispositionRecord(PkMixin, OrgMixin, Base):
    """Human declaration of whether a process is descriptive (the doc should match
    reality → behaviour wins) or prescriptive (reality should match the doc →
    document is law). Append-only; the latest row per (org, process_key) is
    current, and the table is its own audited history (doctrine #4, #7)."""

    __tablename__ = "process_dispositions"
    process_key: Mapped[str] = mapped_column(Text, nullable=False)
    disposition: Mapped[str] = mapped_column(Text, nullable=False)
    declared_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    declared_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    rationale: Mapped[str | None] = mapped_column(Text)
    # Monotonic insertion order — the unambiguous latest-wins tie-break.
    seq: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("nextval('process_dispositions_seq')"),
    )
    __table_args__ = (
        _enum_ck("disposition", ("descriptive", "prescriptive"), "disposition"),
        Index("ix_process_dispositions_org_process", "org_id", "process_key", "seq"),
    )


class Reconciliation(PkMixin, OrgMixin, Base):
    """One row per reconcile_process run (M7.4). Records which detector actually ran
    — `lexical_fallback` means the LLM detector failed and the lexical floor stood
    in — so a degraded production run is visible, never silent."""

    __tablename__ = "reconciliations"
    process_key: Mapped[str] = mapped_column(Text, nullable=False)
    detector: Mapped[str] = mapped_column(Text, nullable=False)
    scored: Mapped[int] = mapped_column(Integer, nullable=False)
    superseded: Mapped[int] = mapped_column(Integer, nullable=False)
    findings: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        _enum_ck(
            "detector",
            ("llm", "lexical", "lexical_fallback", "scripted", "unknown"),
            "detector",
        ),
        Index("ix_reconciliations_org_process", "org_id", "process_key"),
    )


class Finding(PkMixin, OrgMixin, Base):
    """A reconciliation finding surfaced in the approval queue (M6.3). Append-only;
    `seq` gives stable ordering. evidence_refs lists the chunk ids that prove it."""

    __tablename__ = "findings"
    process_key: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    reconciliation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("nextval('findings_seq')")
    )
    __table_args__ = (
        _enum_ck(
            "kind",
            ("contradiction", "drift", "gap", "violation", "stale"),
            "kind",
        ),
        _enum_ck(
            "state",
            ("open", "acknowledged", "resolved", "dismissed"),
            "state",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence",
        ),
        Index("ix_findings_org_state", "org_id", "state", "seq"),
        Index("ix_findings_org_process", "org_id", "process_key"),
    )


class ValidatedProcess(PkMixin, OrgMixin, Base):
    """A generated current-version process (M6.4). Each entry in `steps` carries
    its own provenance (source chunk ids, kinds, freshness, confidence). Versioned
    and append-only: regenerating supersedes the prior version. Signoff reuses the
    kernel approval + audit."""

    __tablename__ = "validated_processes"
    process_key: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    steps: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    reconciliation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    min_confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    # surviving reconciled chunks the drafter left unrepresented (M7.1 coverage)
    uncovered_chunks: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    signed_off_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    signed_off_at: Mapped[datetime.datetime | None] = mapped_column()
    seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("nextval('validated_processes_seq')")
    )
    __table_args__ = (
        _enum_ck("status", ("draft", "signed_off", "superseded"), "status"),
        CheckConstraint(
            "min_confidence IS NULL OR (min_confidence >= 0 AND min_confidence <= 1)",
            name="min_conf",
        ),
        Index(
            "ix_validated_processes_org_process",
            "org_id",
            "process_key",
            "version",
            unique=True,
        ),
    )


class LlmProvider(PkMixin, OrgMixin, Base):
    """Per-workspace LLM provider binding (M7.6 Job A): {provider, model} + a vault
    credential. `status` runs proposed → active (promoted only if it holds the M7.3
    baseline) / rejected; at most one active per org. The gateway resolves the active
    row's credential at call time. No active row → keyless lexical floor (not a shared key)."""

    __tablename__ = "llm_providers"
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    credential_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    scorecard: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (
        _enum_ck("status", ("proposed", "active", "rejected"), "status"),
    )


class Conversation(PkMixin, OrgMixin, Base):
    """A chat thread (Cursor-for-Ops, G1). Org-scoped + FORCE RLS like the rest of the
    plane; each user turn spawns an agent run linked to its messages."""

    __tablename__ = "conversations"
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="New conversation")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Message(PkMixin, OrgMixin, Base):
    """One turn in a conversation. `role` user|assistant|system; an assistant message links
    to the `run_id` that produced it (the streamed agent work lives in run_events)."""

    __tablename__ = "messages"
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(nullable=False, server_default="0")
    __table_args__ = (
        _enum_ck("role", ("user", "assistant", "system"), "role"),
        Index("ix_messages_conversation_seq", "conversation_id", "seq"),
    )


# The canonical set of table names — asserted by tests to catch a dropped table.
ALL_TABLES: tuple[str, ...] = (
    "users",
    "api_tokens",
    "connectors",
    "skills",
    "schedules",
    "runs",
    "run_events",
    "jobs",
    "actions",
    "audit_log",
    "graph_nodes",
    "graph_edges",
    "changes",
    "patterns",
    "feedback",
    "knowledge_chunks",
    "process_dispositions",
    "reconciliations",
    "findings",
    "validated_processes",
    "llm_providers",
    "conversations",
    "messages",
)
