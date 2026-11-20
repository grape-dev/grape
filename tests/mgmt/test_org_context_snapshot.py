"""组织上下文快照：L0-L3 选取、预算/凭证过滤、DB 只存引用。"""
from __future__ import annotations

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.mgmt import org_context_snapshot as snap_mod
from src.kernel.config import WORKSPACE_DIR
from src.kernel.session_manager import db, db_lock

ORG = "01998d6a-7b1e-7cc2-9f6c-3f2a1d4e5b70"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _cleanup() -> None:
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_t_org_context_snapshot WHERE org_id=?", (ORG,))
        db.commit()
    shutil.rmtree(os.path.join(WORKSPACE_DIR, "context-snapshots", f"org-{ORG}"),
                  ignore_errors=True)


def _files() -> dict[str, str]:
    return {
        "org-context/rules.md": "# 组织硬规则\n\n安全合规",
        "org-context/profile.md": "# 组织简介\n\nN",
        "org-context/architecture.md": "# 架构\n\n跨仓契约",
        "org-context/standards/no-force.md": "# 禁止强推",
        "org-context/repos/gitcode/a/b/context.md": "# 目标仓库上下文",
        "org-context/repos/gitcode/a/c/context.md": "# 其他仓库，不应注入",
        "org-context/knowledge/1-org.md": "# 组织知识",
        "org-context/knowledge/2-secret-token.md": "# token=xxx",
        "org-context/demands/1/brief.md": "# 需求 brief\n\n验收标准",
        "org-context/demands/1/dispatch-plan.md": "# 下派计划",
        "org-context/demands/1/acceptance.md": "# 验收",
    }


def test_snapshot_selects_l0_l1_l2_l3_and_filters_credentials(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.org_context_repo._render_files",
                        lambda org_id: _files())
    dispatch = {"id": 901, "org_issue_number": 1,
                "targets_requested": ["gitcode:a/b"]}

    snap_id = snap_mod.build_org_snapshot(ORG, dispatch)
    assert snap_id
    snap = snap_mod.get_snapshot(snap_id)
    assert snap["org_id"] == ORG
    assert snap["dispatch_id"] == "901"
    assert snap["total_bytes"] > 0
    manifest = snap["manifest"]
    assert "org-context/rules.md" in manifest
    assert "org-context/repos/gitcode/a/b/context.md" in manifest
    assert "org-context/repos/gitcode/a/c/context.md" not in manifest
    assert "org-context/knowledge/2-secret-token.md" not in manifest
    assert "org-context/demands/1/brief.md" in manifest
    # DB manifest 不能内嵌正文
    assert all("content" not in meta for meta in manifest.values())

    loaded = snap_mod.load_snapshot_files(snap_id)
    assert "安全合规" in loaded["org-context/rules.md"]
    assert "其他仓库" not in "".join(loaded.values())


def test_snapshot_budget_truncates_per_file(monkeypatch):
    huge = {"org-context/rules.md": "# rules\n" + "x" * (70 * 1024),
            "org-context/profile.md": "# profile"}
    monkeypatch.setattr("src.domains.mgmt.org_context_repo._render_files",
                        lambda org_id: huge)
    snap_id = snap_mod.build_org_snapshot(
        ORG, {"id": 902, "org_issue_number": 1, "targets_requested": []})
    loaded = snap_mod.load_snapshot_files(snap_id)
    assert len(loaded["org-context/rules.md"].encode("utf-8")) <= 8 * 1024 + 100
    assert "已截断" in loaded["org-context/rules.md"]


def test_dispatch_context_bundle(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.org_context_repo._render_files",
                        lambda org_id: _files())
    dispatch = {"id": 903, "org_issue_number": 1,
                "targets_requested": ["gitcode:a/b"]}
    snap_id = snap_mod.build_org_snapshot(ORG, dispatch)

    from src.domains.mgmt import org_dispatch as od

    monkeypatch.setattr(od, "_get_dispatch_row", lambda c, did: {
        "id": 903, "org_id": ORG, "org_issue_number": 1, "status": "running",
        "plan_json": '{"title": "需求一"}',
    })
    monkeypatch.setattr(od, "_get_target_rows", lambda c, did: [{
        "repo_key": "gitcode:a/b", "child_issue_number": 7,
        "status": "running", "pr_number": 0, "error": "",
    }])
    bundle = snap_mod.build_dispatch_context_bundle(ORG, 903)
    assert bundle["snapshot"]["id"] == snap_id
    assert bundle["org_issue_number"] == 1
    assert "org-context/rules.md" in bundle["files"]
    assert bundle["targets"][0]["repo_key"] == "gitcode:a/b"


def test_bundle_overlays_current_acceptance(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.org_context_repo._render_files",
                        lambda org_id: _files())
    dispatch = {"id": 906, "org_issue_number": 1,
                "targets_requested": ["gitcode:a/b"]}
    snap_mod.build_org_snapshot(ORG, dispatch)
    current = dict(_files())
    current["org-context/demands/1/acceptance.md"] = "# 最新验收\n\n结论"
    monkeypatch.setattr("src.domains.mgmt.org_context_repo._render_files",
                        lambda org_id: current)

    from src.domains.mgmt import org_dispatch as od

    monkeypatch.setattr(od, "_get_dispatch_row", lambda c, did: {
        "id": 906, "org_id": ORG, "org_issue_number": 1, "status": "needs_review",
        "plan_json": '{"title": "需求一"}',
    })
    monkeypatch.setattr(od, "_get_target_rows", lambda c, did: [])
    bundle = snap_mod.build_dispatch_context_bundle(ORG, 906)
    assert bundle["files"]["org-context/demands/1/acceptance.md"].startswith("# 最新验收")


def test_snapshot_is_idempotent_per_dispatch(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.org_context_repo._render_files",
                        lambda org_id: _files())
    dispatch = {"id": 904, "org_issue_number": 1, "targets_requested": []}
    first = snap_mod.build_org_snapshot(ORG, dispatch)
    second = snap_mod.build_org_snapshot(ORG, dispatch)
    assert first == second
    with db_lock:
        cnt = db.cursor().execute(
            "SELECT COUNT(*) FROM grape_t_org_context_snapshot WHERE org_id=? AND dispatch_id=?",
            (ORG, 904)).fetchone()[0]
    assert cnt == 1


def test_collect_knowledge_files_injects_org_snapshot(monkeypatch):
    monkeypatch.setattr("src.domains.mgmt.org_context_repo._render_files",
                        lambda org_id: _files())
    dispatch = {"id": 905, "org_issue_number": 1,
                "targets_requested": ["gitcode:a/b"]}
    snap_id = snap_mod.build_org_snapshot(ORG, dispatch)
    repo_key = "gitcode:ctx/snapshot-consumer"
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (repo_key,))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "status, org_context_snapshot_id) VALUES (?,?,?,?,?, 'active', ?)",
            ("1", "", repo_key, 7, "snapshot consumer", snap_id),
        )
        db.commit()
    try:
        from src.domains.runtime.engine.service import collect_knowledge_files

        collected = collect_knowledge_files(repo_key, "coding", "#7")
        knowledge = collected.get("knowledge") or {}
        assert "org-context/rules.md" in knowledge
        assert "安全合规" in knowledge["org-context/rules.md"]["content"]
        assert "org-context/repos/gitcode/a/c/context.md" not in knowledge
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM issue WHERE repo_key=?", (repo_key,))
            db.commit()