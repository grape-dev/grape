"""组织仓库 AI 分析编排（单仓/全量/每日任务/输出分支命名）。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock

UID = "29777"
ORG_SLUG = "org-analysis-test"
REPO_KEY = "gitcode:ai-org/order-service"
ORG_REPO_KEY = "gitcode:ai-org/org-context"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    def _purge():
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM org_member WHERE org_id IN "
                      "(SELECT id FROM organization WHERE owner_id=?)", (UID,))
            c.execute("DELETE FROM organization WHERE owner_id=?", (UID,))
            c.execute("DELETE FROM managed_repos WHERE repo_key IN (?, ?)",
                      (REPO_KEY, ORG_REPO_KEY))
            c.execute("DELETE FROM grape_t_org_repo_map WHERE org_id IN "
                      "(SELECT id FROM organization WHERE owner_id=?)", (UID,))
            try:
                c.execute("DELETE FROM grape_t_org_repo_ai WHERE org_id IN "
                          "(SELECT id FROM organization WHERE owner_id=?)", (UID,))
            except Exception:
                pass
            c.execute("DELETE FROM autopilot_tasks WHERE repo_key=?", (ORG_REPO_KEY,))
            db.commit()

    _purge()
    yield
    _purge()


def _seed_org():
    from src.domains.mgmt.user.db import add_managed_repo
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(name="AI Org", slug=ORG_SLUG, owner_id=UID)
    assert org
    add_managed_repo(ORG_REPO_KEY, "ai-org", "org-context", webhook_id=1,
                     org_id=org["id"], is_org_repo=1)
    add_managed_repo(REPO_KEY, "ai-org", "order-service", webhook_id=1,
                     org_id=org["id"])
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE organization SET org_repo_key=?, org_scene_id=? WHERE id=?",
                  (ORG_REPO_KEY, "", org["id"]))
        db.commit()
    return org


def test_branch_names():
    from src.domains.mgmt import org_context_repo

    assert org_context_repo.ORG_REPO_DEFAULT_BRANCH == "grape-repo-knowledge"
    assert org_context_repo.ORG_REPO_ANALYSIS_BRANCH == "grape-repo-analysis"


def test_save_ai_result_overwrites_and_records_status():
    from src.domains.mgmt import org_analysis, org_repo_map

    org = _seed_org()
    result = {
        "schema_version": 1,
        "repo_key": REPO_KEY,
        "full_name": "ai-org/order-service",
        "source_commit": "abc123",
        "purpose": "订单域主服务（AI）",
        "domains": ["订单", "履约"],
        "keywords": ["下单", "状态机"],
        "interfaces": ["POST /api/orders"],
        "depends_on": ["ai-org/customer-service"],
        "not_owns": ["支付清结算"],
        "capabilities": [{"name": "下单", "description": "创建订单", "paths": ["src/orders.py"]}],
        "confidence": 0.92,
        "evidence": ["README.md"],
        "risks": ["支付链路需人工确认"],
    }
    saved = org_repo_map.save_ai_result(org["id"], REPO_KEY, result,
                                        engine_id=7, source_commit="abc123",
                                        triggered_by="link", result_hash="deadbeef")
    assert saved and saved["purpose"] == "订单域主服务（AI）"
    assert saved["confidence"] == 0.92
    assert "支付清结算" in saved["not_owns"]
    status = org_analysis.status_for_repo(org["id"], REPO_KEY)
    assert status["status"] == "succeeded"
    assert status["engine_id"] == 7
    files = org_repo_map.repo_map_markdown_files(org["id"])
    assert any("summary.md" in p for p in files)


def test_ensure_daily_task_is_idempotent():
    from src.domains.mgmt import org_analysis

    org = _seed_org()
    first = org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    second = org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    assert first and first == second
    task = org_analysis.automation_task(org["id"])
    assert task["cron"] == "0 0 * * *"
    assert task["enabled"] is True


def test_single_and_all_targets_exclude_org_repo():
    from src.domains.mgmt import org_analysis

    org = _seed_org()
    single = org_analysis.analysis_targets(org["id"], mode="single", repo_key=REPO_KEY)
    assert [r["repo_key"] for r in single] == [REPO_KEY]
    all_repos = org_analysis.analysis_targets(org["id"], mode="all")
    assert [r["repo_key"] for r in all_repos] == [REPO_KEY]


def test_request_all_without_repos_is_skipped():
    from src.domains.mgmt import org_analysis
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(name="Empty AI Org", slug="empty-ai-org", owner_id=UID)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE organization SET org_repo_key=? WHERE id=?",
                  (ORG_REPO_KEY, org["id"]))
        db.commit()
    out = org_analysis.request_all_analysis(org["id"], trigger="manual", actor_id=UID)
    assert out["skipped"] is True
    assert out["reason"] == "no_repos"


def test_request_single_triggers_with_params(monkeypatch):
    from src.domains.mgmt import org_analysis

    org = _seed_org()
    captured = {}

    def _fake_trigger(task_id, params):
        captured["task_id"] = task_id
        captured["params"] = params
        return {"ok": True}

    monkeypatch.setattr(org_analysis, "_trigger_task", _fake_trigger)
    out = org_analysis.request_repo_analysis(org["id"], REPO_KEY, trigger="link", actor_id=UID)
    assert out["ok"] is True
    assert captured["params"]["mode"] == "single"
    assert captured["params"]["repo_key"] == REPO_KEY
    assert captured["params"]["org_id"] == org["id"]
