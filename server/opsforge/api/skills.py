"""Skills API: list installed skills (with trust summary) and detail."""

from __future__ import annotations

import io
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import text

from ..config import get_settings
from ..db import record_audit, session_factory
from ..security import Principal, require_token
from ..skills import (
    SkillValidationError,
    get_skill,
    install_skill_dir,
    list_skills,
)

_WRITER_ROLES = {"admin", "operator"}


def _require_writer(principal: Principal) -> None:
    if principal.role not in _WRITER_ROLES:
        raise HTTPException(status_code=403, detail="requires admin or operator")

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


def _find_skill_dir(root: Path) -> Path | None:
    if (root / "skill.yaml").exists():
        return root
    return next((p.parent for p in root.rglob("skill.yaml")), None)


def _summary(skill: dict[str, Any]) -> dict[str, Any]:
    manifest = skill.get("manifest") or {}
    return {
        "slug": skill["slug"],
        "version": skill["version"],
        "name": manifest.get("name", skill["slug"]),
        "source": skill["source"],
        "enabled": skill["enabled"],
        "triggers": manifest.get("triggers", []),
        "tool_count": len(manifest.get("tools", []) or []),
        "proposal_count": len(manifest.get("proposals", []) or []),
    }


@router.get("")
async def list_installed(principal: Principal = Depends(require_token)):
    return [_summary(s) for s in await list_skills()]


@router.post("/install")
async def install_skill(
    file: UploadFile = File(...), principal: Principal = Depends(require_token)
):
    """Install a skill from an uploaded tar/zip of its directory (config-as-code).

    Admin only; the manifest is validated before install (invalid → 400).
    """
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="skill install requires admin")
    data = await file.read()
    name = (file.filename or "").lower()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            if name.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    zf.extractall(root)
            else:
                with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                    tf.extractall(root, filter="data")  # path-traversal safe
        except (zipfile.BadZipFile, tarfile.TarError) as exc:
            raise HTTPException(status_code=400, detail=f"bad archive: {exc}") from exc

        skill_dir = _find_skill_dir(root)
        if skill_dir is None:
            raise HTTPException(status_code=400, detail="no skill.yaml in archive")
        try:
            skill_id = await install_skill_dir(skill_dir, source="org")
        except SkillValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    actor = f"user:{principal.user_id}" if principal.user_id else "system"
    await record_audit(
        principal.org_id, actor, "skill.installed", subject_ref=skill_id,
        detail={"filename": file.filename},
    )
    return {"installed": skill_id}


@router.get("/proposed")
async def list_proposed(
    principal: Principal = Depends(require_token),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List codified skills awaiting human review (source=codified, enabled=false,
    not yet rejected). Registered before /{slug} to avoid route shadowing."""
    offset = (page - 1) * page_size
    base_where = (
        "org_id = :org AND source = 'codified' AND enabled = false AND rejected_at IS NULL"
    )
    async with session_factory().begin() as s:
        total: int = (
            await s.execute(
                text(f"SELECT count(*) FROM skills WHERE {base_where}"),
                {"org": principal.org_id},
            )
        ).scalar_one()
        rows = (
            await s.execute(
                text(
                    f"SELECT id, slug, version, manifest, source, enabled, created_at "
                    f"FROM skills WHERE {base_where} "
                    f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                ),
                {"org": principal.org_id, "limit": page_size, "offset": offset},
            )
        ).all()
    return {
        "items": [
            {
                "id": str(r.id),
                "slug": r.slug,
                "version": r.version,
                "name": (r.manifest or {}).get("name", r.slug),
                "description": (r.manifest or {}).get("description", ""),
                "source": r.source,
                "enabled": r.enabled,
                "manifest": r.manifest,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/{skill_id}/approve")
async def approve_skill(
    skill_id: str,
    principal: Principal = Depends(require_token),
):
    """Approve a proposed codified skill: set enabled=true so it becomes active."""
    _require_writer(principal)
    async with session_factory().begin() as s:
        result = await s.execute(
            text(
                "UPDATE skills SET enabled = true, updated_at = now() "
                "WHERE id = :id AND org_id = :org AND source = 'codified' "
                "AND rejected_at IS NULL "
                "RETURNING id, slug"
            ),
            {"id": skill_id, "org": principal.org_id},
        )
        row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="proposed skill not found")
    actor = f"user:{principal.user_id}" if principal.user_id else "system"
    await record_audit(
        principal.org_id, actor, "skill.approved",
        subject_ref=str(row.id), detail={"slug": row.slug},
    )
    return {"id": str(row.id), "slug": row.slug, "enabled": True}


@router.post("/{skill_id}/reject")
async def reject_skill(
    skill_id: str,
    principal: Principal = Depends(require_token),
):
    """Reject a proposed codified skill: set rejected_at=now(). Patterns are retained."""
    _require_writer(principal)
    async with session_factory().begin() as s:
        result = await s.execute(
            text(
                "UPDATE skills SET rejected_at = now(), updated_at = now() "
                "WHERE id = :id AND org_id = :org AND source = 'codified' "
                "AND rejected_at IS NULL "
                "RETURNING id, slug"
            ),
            {"id": skill_id, "org": principal.org_id},
        )
        row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="proposed skill not found")
    actor = f"user:{principal.user_id}" if principal.user_id else "system"
    await record_audit(
        principal.org_id, actor, "skill.rejected",
        subject_ref=str(row.id), detail={"slug": row.slug},
    )
    return {"id": str(row.id), "slug": row.slug, "rejected": True}


@router.get("/{slug}")
async def skill_detail(slug: str, principal: Principal = Depends(require_token)):
    skill = await get_skill(slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return {
        **_summary(skill),
        "manifest": skill["manifest"],
        "instructions": skill["instructions"],
        "trust_overrides": skill["trust_overrides"],
    }


@router.post("/{slug}/graduate")
async def graduate_tool(
    slug: str,
    tool: str = Body(..., embed=True),
    principal: Principal = Depends(require_token),
):
    """Grant a reversible tool `auto_with_notify` after enough clean executions.

    Graduation is a deliberate human act (admin only), recorded in the audit log,
    and never automatic. Destructive tools are never gradable.
    """
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="graduation requires admin")
    skill = await get_skill(slug)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")

    manifest = skill["manifest"]
    proposal = next(
        (p for p in manifest.get("proposals", []) or [] if p["tool"] == tool), None
    )
    if proposal is None:
        raise HTTPException(status_code=400, detail=f"{tool} is not a declared proposal")
    if proposal.get("class") == "destructive":
        raise HTTPException(status_code=400, detail="destructive tools are never gradable")

    min_runs = get_settings().graduation_min_executions
    async with session_factory().begin() as s:
        clean = (
            await s.execute(
                text(
                    "SELECT count(*) FROM actions WHERE org_id=:org AND tool=:tool "
                    "AND state='succeeded'"
                ),
                {"org": principal.org_id, "tool": tool},
            )
        ).scalar_one()
        if clean < min_runs:
            raise HTTPException(
                status_code=409,
                detail=f"needs {min_runs} clean executions, have {clean}",
            )
        await s.execute(
            text(
                "UPDATE skills SET trust_overrides = "
                "trust_overrides || jsonb_build_object(CAST(:tool AS text), "
                "'auto_with_notify') WHERE org_id=:org AND slug=:slug"
            ),
            {"tool": tool, "org": principal.org_id, "slug": slug},
        )
    await record_audit(
        principal.org_id,
        f"user:{principal.user_id}",
        "skill.graduated",
        subject_ref=slug,
        detail={"tool": tool, "clean_executions": clean},
    )
    return {"slug": slug, "tool": tool, "trust": "auto_with_notify", "clean_executions": clean}
