"""Skill manifest schema, loader, validator, and install.

A skill is a directory: skill.yaml (the contract), INSTRUCTIONS.md (the domain
knowledge handed to the model), and evals/. Manifests are validated against
`opsforge/skill/v1`; non-conforming installs are rejected. Tools not listed
under `tools:` are invisible to the agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import text

from .config import get_settings
from .db import session_factory

SCHEMA_ID = "opsforge/skill/v1"
ActionClass = Literal["read_only", "reversible", "destructive"]


class SkillInput(BaseModel):
    name: str
    type: str = "string"
    required: bool = False


class SkillContext(BaseModel):
    graph: bool = True
    change_window_hours: int = 24
    similar_patterns: int = 0


class ToolDecl(BaseModel):
    tool: str
    class_: ActionClass = Field(alias="class")
    redact: bool = False

    model_config = {"populate_by_name": True}


class ProposalDecl(BaseModel):
    tool: str
    class_: ActionClass = Field(alias="class")
    rollback: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}


class SkillPolicy(BaseModel):
    max_tool_calls: int = 25
    max_runtime_seconds: int = 420
    forbidden_targets: list[str] = Field(default_factory=list)
    # GAP 3 — ops rules as config: weekly change-freeze windows + per-priority
    # approval escalation (e.g. {"P1": "admin"}). Enforced by the executor / approval.
    freeze_windows: list[dict[str, Any]] = Field(default_factory=list)
    requires_role_for_priority: dict[str, str] = Field(default_factory=dict)


class SkillReport(BaseModel):
    format: str = "rca_v1"


class KnowledgeSource(BaseModel):
    """A source the commission step bootstraps from to LEARN this operation (the M6 loop).
    `kind` local_dir → ingest a server-visible folder; connector → pull from a configured
    connector of that kind. `process_key` groups the ingested chunks for reconciliation. The
    manifest only NAMES sources — it encodes no domain logic (the operation stays learned, not
    coded)."""

    kind: Literal["local_dir", "connector"]
    ref: str
    process_key: str | None = None


class SkillManifest(BaseModel):
    schema_: str = Field(alias="schema")
    slug: str
    version: str
    name: str
    description: str = ""
    # The commissioning charter: this workspace's agents' role/purpose. Operation-agnostic — a
    # human-readable statement, never domain logic in code.
    charter: str = ""
    triggers: list[Literal["manual", "event", "schedule"]] = Field(default_factory=list)
    inputs: list[SkillInput] = Field(default_factory=list)
    context: SkillContext = Field(default_factory=SkillContext)
    tools: list[ToolDecl] = Field(default_factory=list)
    proposals: list[ProposalDecl] = Field(default_factory=list)
    subagents: list[str] = Field(default_factory=list)  # skill slugs this may delegate to
    # Sources the commission step ingests+reconciles so the agent learns this operation.
    knowledge_sources: list[KnowledgeSource] = Field(default_factory=list)
    policy: SkillPolicy = Field(default_factory=SkillPolicy)
    report: SkillReport = Field(default_factory=SkillReport)
    evals: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _empty_sections_to_lists(cls, data: Any) -> Any:
        # A YAML section left with only comments parses as None; treat it as empty.
        if isinstance(data, dict):
            for key in ("triggers", "inputs", "tools", "proposals", "subagents", "evals",
                        "knowledge_sources"):
                if data.get(key) is None and key in data:
                    data[key] = []
        return data


class SkillValidationError(ValueError):
    pass


def parse_manifest(raw: dict[str, Any]) -> SkillManifest:
    if raw.get("schema") != SCHEMA_ID:
        raise SkillValidationError(
            f"manifest schema must be {SCHEMA_ID!r}, got {raw.get('schema')!r}"
        )
    try:
        return SkillManifest.model_validate(raw)
    except ValidationError as exc:
        raise SkillValidationError(str(exc)) from exc


def load_skill_dir(directory: str | Path) -> tuple[SkillManifest, str]:
    """Load + validate skill.yaml and INSTRUCTIONS.md from a directory."""
    directory = Path(directory)
    manifest_path = directory / "skill.yaml"
    if not manifest_path.exists():
        raise SkillValidationError(f"no skill.yaml in {directory}")
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest = parse_manifest(raw)
    instructions_path = directory / "INSTRUCTIONS.md"
    instructions = (
        instructions_path.read_text(encoding="utf-8")
        if instructions_path.exists()
        else ""
    )
    return manifest, instructions


def manifest_dump(manifest: SkillManifest) -> dict[str, Any]:
    """JSON-able manifest with `schema`/`class` keys (not the python aliases)."""
    return json.loads(manifest.model_dump_json(by_alias=True))


_UPSERT_SKILL = text(
    """
    INSERT INTO skills (org_id, slug, version, manifest, instructions, source, enabled)
    VALUES (:org, :slug, :version, CAST(:manifest AS jsonb), :instructions, :source, true)
    ON CONFLICT (org_id, slug) DO UPDATE
        SET version = EXCLUDED.version,
            manifest = EXCLUDED.manifest,
            instructions = EXCLUDED.instructions,
            source = EXCLUDED.source,
            enabled = true
    RETURNING id
    """
)


async def install_skill_dir(directory: str | Path, source: str = "builtin") -> str:
    manifest, instructions = load_skill_dir(directory)
    async with session_factory().begin() as s:
        skill_id = (
            await s.execute(
                _UPSERT_SKILL,
                {
                    "org": get_settings().org_id,
                    "slug": manifest.slug,
                    "version": manifest.version,
                    "manifest": json.dumps(manifest_dump(manifest)),
                    "instructions": instructions,
                    "source": source,
                },
            )
        ).scalar_one()
    return str(skill_id)


async def install_builtin_skills(skills_dir: str | None = None) -> list[str]:
    """Install every skill pack under the skills directory (idempotent)."""
    root = Path(skills_dir or get_settings().skills_dir)
    installed: list[str] = []
    if not root.exists():
        return installed
    for child in sorted(root.iterdir()):
        if (child / "skill.yaml").exists():
            installed.append(await install_skill_dir(child, source="builtin"))
    return installed


_SKILL_COLS = (
    "id, slug, version, manifest, instructions, source, enabled, trust_overrides"
)


async def get_skill(slug: str) -> dict[str, Any] | None:
    async with session_factory().begin() as s:
        row = (
            await s.execute(
                text(
                    f"SELECT {_SKILL_COLS} FROM skills "
                    "WHERE slug = :slug AND org_id = :org"
                ),
                {"slug": slug, "org": get_settings().org_id},
            )
        ).first()
    return dict(row._mapping) if row else None


async def get_skill_by_id(skill_id: Any) -> dict[str, Any] | None:
    async with session_factory().begin() as s:
        row = (
            await s.execute(
                text(f"SELECT {_SKILL_COLS} FROM skills WHERE id = :id"),
                {"id": skill_id},
            )
        ).first()
    return dict(row._mapping) if row else None


async def list_skills() -> list[dict[str, Any]]:
    async with session_factory().begin() as s:
        rows = (
            await s.execute(
                text(
                    f"SELECT {_SKILL_COLS} FROM skills WHERE org_id = :org "
                    "ORDER BY slug"
                ),
                {"org": get_settings().org_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


# --------------------------------------------------------------------------- #
# Scaffolding: `opsforge skill new <slug>` — the project's cookiecutter
# --------------------------------------------------------------------------- #
_MANIFEST_TEMPLATE = """\
schema: {schema}
slug: {slug}
version: 0.1.0
name: {title}
description: >
  TODO: what this skill investigates or does. Read-only unless it declares
  proposals.
triggers: [manual]
inputs:
  - {{name: query, type: string, required: true}}
context:
  graph: true
  change_window_hours: 24
tools:
  # connector_kind.tool_name -> action_class. Tools not listed are invisible.
  # - {{tool: kubernetes.list_pods, class: read_only}}
proposals:
  # Actions the skill MAY propose (never auto-executed in v1).
  # - {{tool: kubernetes.restart_pod, class: reversible}}
policy:
  max_tool_calls: 25
  max_runtime_seconds: 420
  forbidden_targets: []
report:
  format: rca_v1
evals:
  - evals/example.yaml
"""

_INSTRUCTIONS_TEMPLATE = """\
# {title}

You are an OpsForge agent running the **{slug}** skill.

## Goal
TODO: describe the investigation goal.

## How to work
1. Use the provided graph neighborhood and change timeline first.
2. Call only the read-only tools you were given. Gather evidence before concluding.
3. Submit an rca_v1 report. If you cannot reach at least `medium` confidence,
   say so and list what evidence is missing — never bluff.
"""

_EVAL_TEMPLATE = """\
name: example
trigger:
  query: "TODO: example question"
assertions:
  hypothesis_must_mention: []
  max_tool_calls: 25
"""


def scaffold_skill(slug: str, dest_root: str | Path) -> Path:
    """Generate manifest + INSTRUCTIONS.md + eval stub for a new skill."""
    title = slug.replace("-", " ").replace("_", " ").title()
    directory = Path(dest_root) / slug
    if directory.exists():
        raise SkillValidationError(f"{directory} already exists")
    (directory / "evals").mkdir(parents=True)
    (directory / "skill.yaml").write_text(
        _MANIFEST_TEMPLATE.format(schema=SCHEMA_ID, slug=slug, title=title),
        encoding="utf-8",
    )
    (directory / "INSTRUCTIONS.md").write_text(
        _INSTRUCTIONS_TEMPLATE.format(slug=slug, title=title), encoding="utf-8"
    )
    (directory / "evals" / "example.yaml").write_text(_EVAL_TEMPLATE, encoding="utf-8")
    return directory
