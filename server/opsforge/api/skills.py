"""Skills API: list installed skills (with trust summary) and detail."""

from __future__ import annotations

import io
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
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
    # Irreversible (no declared rollback) is an always-gate property in the consequential
    # boundary (G3); a grant must never auto-execute an action that cannot be rolled back.
    if proposal.get("class") == "reversible" and not proposal.get("rollback"):
        raise HTTPException(
            status_code=400,
            detail="only reversible proposals with a declared rollback are gradable",
        )

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
