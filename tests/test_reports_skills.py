"""M2: rca_v1 report model + skill manifest validation/scaffold — pure, no DB."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from opsforge.reports import RcaReport, render_markdown
from opsforge.skills import (
    SCHEMA_ID,
    SkillValidationError,
    load_skill_dir,
    parse_manifest,
    scaffold_skill,
)


def test_rca_report_valid_and_renders():
    report = RcaReport(
        hypothesis="payment-svc down due to deploy payment-svc@rev7",
        confidence="high",
        evidence=[{"claim": "deploy rev7 preceded failures", "source_tool": "k8s"}],
        next_checks=["roll back"],
    )
    md = render_markdown(report)
    assert "payment-svc@rev7" in md
    assert "Confidence:** high" in md
    assert "deploy rev7 preceded failures" in md


def test_rca_report_rejects_bad_confidence():
    with pytest.raises(ValidationError):
        RcaReport(hypothesis="x", confidence="certain")


def test_builtin_manifest_is_valid():
    manifest, instructions = load_skill_dir("skills/incident-investigation")
    assert manifest.slug == "incident-investigation"
    assert any(t.tool == "kubernetes.get_logs" and t.redact for t in manifest.tools)
    assert "read-only" in instructions.lower()


def test_manifest_rejects_wrong_schema():
    with pytest.raises(SkillValidationError):
        parse_manifest({"schema": "wrong/v1", "slug": "x", "version": "0.1.0", "name": "X"})


def test_scaffold_roundtrips(tmp_path):
    directory = scaffold_skill("my-new-skill", tmp_path)
    assert (directory / "skill.yaml").exists()
    assert (directory / "INSTRUCTIONS.md").exists()
    assert (directory / "evals" / "example.yaml").exists()
    manifest, _ = load_skill_dir(directory)
    assert manifest.schema_ == SCHEMA_ID
    assert manifest.slug == "my-new-skill"
