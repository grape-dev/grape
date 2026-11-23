"""Issue #2118: static contracts for organization skills."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".grape" / "skills" / "org"


def _read(relative: str) -> str:
    path = SKILL_ROOT / relative
    assert path.is_file(), f"missing skill file: {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("skill", "reference"),
    [
        ("org-intake-cli", "references/dispatch-plan.md"),
        ("org-dispatch-cli", "references/api-contract.md"),
        ("org-aggregate-cli", "references/aggregation.md"),
    ],
)
def test_org_skill_documents_exist(skill, reference):
    body = _read(f"{skill}/SKILL.md")
    assert f"name: {skill}" in body
    assert "tools:" in body
    assert "$GRAPE_SESSION_TOKEN" in body
    _read(f"{skill}/{reference}")


def test_intake_dispatch_plan_contract_matches_issue_2112():
    body = _read("org-intake-cli/SKILL.md")
    reference = _read("org-intake-cli/references/dispatch-plan.md")
    combined = body + "\n" + reference

    for field in (
        "org_issue_number",
        "targets",
        "repo_key",
        "scene_id",
        "title",
        "description",
        "context",
        "summary",
        "constraints",
        "decisions",
        "knowledge_refs",
        "acceptance",
    ):
        assert field in combined
    assert "explicit" in combined.lower() or "显式" in combined
    assert "all linked" in combined.lower() or "全部已关联" in combined
    assert "do not fabricate" in combined.lower() or "不得伪造成功" in combined
    assert "grape git-repo issues get" in combined
    assert "--owner" in combined and "--repo" in combined and "--number" in combined
    assert "grape git-repo issues comments create" in combined
    assert "grape commands \"org repo map\"" in combined
    assert "grape org repo map recall" in combined
    assert "grape org repos list" in combined
    assert "dual scope" in combined
    assert "--json" in combined
    assert "grape a2a handoff" in combined
    assert '"agent": "coding"' in combined
    assert "409 not_in_scene" in combined or "not_in_scene" in combined


def test_dispatch_contract_covers_api_and_errors():
    body = _read("org-dispatch-cli/SKILL.md")
    reference = _read("org-dispatch-cli/references/api-contract.md")
    combined = body + "\n" + reference

    for endpoint in (
        "POST /api/orgs/{id}/dispatches",
        "GET /api/orgs/{id}/dispatches/{did}",
        "POST /api/orgs/{id}/dispatches/{did}/cancel",
        "POST /api/orgs/{id}/dispatches/{did}/targets/{repo_key}/retry",
    ):
        assert endpoint in combined
    for error_code in ("INVALID_TARGET", "NO_TARGET_SCENE", "PERMISSION_DENIED"):
        assert error_code in combined
    assert "dispatch_id" in combined
    assert "idempot" in combined.lower() or "幂等" in combined
    assert "not print" in combined.lower() or "不得打印" in combined
    for command in (
        "grape org dispatches create",
        "grape org dispatches list",
        "grape org dispatches detail",
        "grape org dispatches cancel",
        "grape org dispatches target retry",
    ):
        assert command in combined
    assert "grape refresh" in combined
    assert "grape commands \"org dispatches\"" in combined
    assert "--org_id" in combined
    assert "--dispatch_id" in combined
    assert "cli_contract_unavailable" in combined.lower()
    assert "curl" in combined.lower()
    assert "dual scope" in combined
    assert "grape a2a handoff" in combined
    assert 'qa' in combined


def test_aggregate_contract_has_remaining_semantics():
    body = _read("org-aggregate-cli/SKILL.md")
    reference = _read("org-aggregate-cli/references/aggregation.md")
    combined = body + "\n" + reference

    assert "remaining" in combined
    assert "completed" in combined
    assert "failed" in combined
    assert "cancelled" in combined
    assert "needs_review" in combined
    assert "不得提前完成" in combined or "must not complete early" in combined.lower()
    assert "child_issue_number" in combined
    assert "pr_number" in combined
    assert "grape org dispatches detail" in combined
    assert "grape git-repo issues comments create" in combined
    assert "--owner" in combined and "--repo" in combined and "--number" in combined
    assert "--json" in combined
    assert "handoff=complete" in combined
    assert "不得" in combined and "grape a2a handoff" in combined
    assert "pending_review" in combined
