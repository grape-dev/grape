"""组织仓库地图构建分析编排（单仓/全量/每日任务/输出分支命名）。"""
from __future__ import annotations

import json
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

    assert org_context_repo.ORG_REPO_DEFAULT_BRANCH == "main"
    assert org_context_repo.ORG_REPO_COMBINED_BRANCH == "grape-org"
    assert org_context_repo.ORG_REPO_CONTENT_BRANCH == "grape-org"


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
    rows = org_repo_map.list_repo_map(org["id"])["repos"]
    assert rows[0]["ai_status"] == "succeeded"
    assert rows[0]["ai_analyzed_at"]
    docs = org_analysis.analysis_docs(org["id"])
    assert docs["repos"][0]["core_functions"][0]["name"] == "下单"
    assert docs["repos"][0]["core_responsibilities"]["not_owns"] == ["支付清结算"]


def test_ensure_daily_task_is_idempotent():
    from src.domains.mgmt import org_analysis

    org = _seed_org()
    first = org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    second = org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    assert first and first == second
    task = org_analysis.automation_task(org["id"])
    assert task["cron"] == "0 0 * * *"
    assert task["enabled"] is True


def test_ensure_daily_task_writes_state_columns_as_empty():
    """关联仓库建每日分析任务时，状态三列必须显式落空串（PR #2157 门禁复审 ④）。

    同 `routes/autopilot.py::create_task`：MySQL strict 下三列 NOT NULL 且默认值未归一
    时，这条 INSERT 不给值直接 1364 → 「关联组织仓库」500。
    """
    from src.domains.mgmt import org_analysis

    org = _seed_org()
    task_id = org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    row = db.execute(
        "SELECT dispatch_state, dispatch_state_reason, dispatch_state_at "
        "FROM autopilot_tasks WHERE id=?", (task_id,)).fetchone()
    assert tuple(row) == ("", "", ""), f"三列未落空串: {tuple(row)}"


def test_ensure_daily_task_insert_column_list_is_exact():
    """列清单按逗号切分后与期望集合精确相等（子串断言会把 dispatch_state 误判为命中）。"""
    import inspect
    import re

    from src.domains.mgmt import org_analysis

    src = inspect.getsource(org_analysis.ensure_analysis_task)
    sql = " ".join(re.findall(r'"([^"]*)"', src))
    m = re.search(r"INSERT INTO autopilot_tasks\s*\(([^)]*)\)", sql)
    assert m, "未找到 ensure_analysis_task 的 INSERT 列清单"
    cols = {c.strip() for c in m.group(1).split(",") if c.strip()}
    expected = {
        "name", "repo_key", "agent_type", "instruction", "skill_name", "cron",
        "max_retry", "enabled", "last_run_at", "next_run_at", "created_at",
        "updated_at", "created_by", "engine_runtime_ids", "custom_fields",
        "work_branch", "engine_runtime_uids",
        "dispatch_state", "dispatch_state_reason", "dispatch_state_at",
    }
    assert cols == expected, f"列清单不符: 缺={sorted(expected-cols)} 多={sorted(cols-expected)}"


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


def test_managed_task_name_is_user_readable():
    from src.domains.mgmt.org_analysis import managed_task_name

    name = managed_task_name({"name": "grape-gitcode", "slug": "grape-gitcode"})
    assert name == "组织仓库地图构建分析"
    assert "__org_analysis__" not in name


def test_disable_analysis_automation_removes_task():
    from src.domains.mgmt import org_analysis

    org = _seed_org()
    assert org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    assert org_analysis.automation_task(org["id"])
    assert org_analysis.disable_analysis_automation(org["id"]) == 1
    assert org_analysis.automation_task(org["id"]) == {}


def test_request_all_skips_when_task_running(monkeypatch):
    from src.domains.mgmt import org_analysis

    org = _seed_org()
    task_id = org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO autopilot_executions (task_id, status, retry_count, started_at, created_at, agent_type) "
            "VALUES (?, 'running', 0, datetime('now'), datetime('now'), 'issue')",
            (task_id,),
        )
        db.commit()

    called = []
    monkeypatch.setattr(org_analysis, "_trigger_task", lambda *a, **k: called.append(a) or {"ok": True})
    out = org_analysis.request_all_analysis(org["id"], trigger="link", actor_id=UID)
    assert out["skipped"] is True
    assert out["reason"] == "running"
    assert not called


def test_analysis_routes_write_audit_logs(monkeypatch):
    """C3：AI 分析三条写路由必须落审计（结果回写 / 手动触发 / 开启每日任务）。"""
    import json as _json

    from src.domains.mgmt import org_analysis as service
    from src.domains.mgmt.routes import org_analysis as routes

    org = _seed_org()
    user = {"id": UID, "auth_type": "api_key"}

    # 避免真实触发 autopilot 引擎：只验证路由的审计落点。
    monkeypatch.setattr(service, "request_all_analysis",
                        lambda *a, **k: {"skipped": True, "reason": "no_repos"})
    monkeypatch.setattr(service, "request_repo_analysis",
                        lambda *a, **k: {"ok": True, "task_id": 1})
    monkeypatch.setattr(service, "ensure_analysis_automation",
                        lambda *a, **k: {"ok": True, "task_id": 1})

    result = {
        "schema_version": 1, "repo_key": REPO_KEY, "full_name": "ai-org/order-service",
        "purpose": "审计用例", "domains": [], "keywords": [], "interfaces": [],
        "depends_on": [], "not_owns": [], "capabilities": [], "evidence": [],
        "risks": [], "confidence": 0.5,
    }
    routes.save_ai_result(
        org["id"], REPO_KEY,
        routes.AIResultBody(result=result, engine_id=7, source_commit="abc123"),
        user=user,
    )
    routes.run_analysis(org["id"], routes.RunAnalysisBody(mode="all"), user=user)
    routes.ensure_automation(org["id"], user=user)

    with db_lock:
        rows = db.cursor().execute(
            "SELECT event_type, operator_id, detail FROM audit_logs WHERE event_type IN (?,?,?) "
            "ORDER BY seq",
            ("org_repo_ai_result", "org_repo_analysis_run",
             "org_repo_analysis_automation_ensure"),
        ).fetchall()
    expected = {
        "org_repo_ai_result",
        "org_repo_analysis_run",
        "org_repo_analysis_automation_ensure",
    }
    got = set()
    for event_type, operator_id, detail in rows:
        parsed = _json.loads(detail or "{}")
        if parsed.get("org_id") != org["id"]:
            continue
        got.add(event_type)
        assert str(operator_id) == str(UID)
        if event_type == "org_repo_ai_result":
            assert parsed.get("trigger") == "worker"
            assert parsed.get("result_hash")
        else:
            assert parsed.get("trigger") == "manual"
    assert got == expected, f"missing audit types: {expected - got}"


def test_analysis_task_does_not_store_reserved_custom_fields():
    """自动创建的分析任务不能把 GRAPE_* 写进 custom_fields（前端保存会报保留冲突）。"""
    from src.domains.mgmt import org_analysis
    from src.kernel.scene_custom_env import is_reserved_name

    org = _seed_org()
    task_id = org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    with db_lock:
        row = db.cursor().execute(
            "SELECT custom_fields FROM autopilot_tasks WHERE id=?", (task_id,)
        ).fetchone()
    fields = json.loads((row[0] if row else "") or "[]")
    assert fields == []
    assert not [f for f in fields if is_reserved_name(str(f.get("name") or ""))]


def test_ensure_preserves_user_custom_fields():
    """ensure 只校正受管字段，不能清掉用户为该自动化配置的自定义变量。"""
    from src.domains.mgmt import org_analysis

    org = _seed_org()
    task_id = org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    user_fields = [{"name": "TEAM_FLAG", "value": "1", "is_secret": False, "scope": ["all"]}]
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE autopilot_tasks SET custom_fields=? WHERE id=?",
            (json.dumps(user_fields, ensure_ascii=False), task_id),
        )
        db.commit()

    assert org_analysis.ensure_analysis_task(org["id"], actor_id=UID) == task_id
    with db_lock:
        row = db.cursor().execute(
            "SELECT custom_fields FROM autopilot_tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert json.loads(row[0]) == user_fields


def test_migration_strips_reserved_org_analysis_custom_fields():
    """存量任务里的 GRAPE_MANAGED_BY/GRAPE_ORG_ID/GRAPE_ANALYSIS_MODE 应被清理。"""
    from src.api.db_setup import _fn_strip_org_analysis_reserved_fields
    from src.domains.mgmt import org_analysis

    org = _seed_org()
    task_id = org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    stale = [
        {"name": "GRAPE_MANAGED_BY", "value": "org-analysis"},
        {"name": "GRAPE_ORG_ID", "value": org["id"]},
        {"name": "GRAPE_ANALYSIS_MODE", "value": "all"},
        {"name": "TEAM_FLAG", "value": "keep-me"},
    ]
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE autopilot_tasks SET custom_fields=? WHERE id=?",
            (json.dumps(stale, ensure_ascii=False), task_id),
        )
        db.commit()

    _fn_strip_org_analysis_reserved_fields()

    with db_lock:
        row = db.cursor().execute(
            "SELECT custom_fields FROM autopilot_tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert json.loads(row[0]) == [{"name": "TEAM_FLAG", "value": "keep-me"}]


def test_ensure_task_uses_scene_work_branch():
    """每日分析任务的 work_branch 必须跟随组织场景（新组织=grape-org）。"""
    from src.domains.mgmt import org_analysis
    from src.kernel.session_manager import db, db_lock

    org = _seed_org()
    scene_id = "custom-org-branch-test"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, advanced_options, is_active) "
            "VALUES (?,?,?,'[]','{\"work_branch\":\"grape-org\"}',1)",
            (scene_id, scene_id, ORG_REPO_KEY),
        )
        c.execute("UPDATE organization SET org_scene_id=? WHERE id=?", (scene_id, org["id"]))
        db.commit()

    task_id = org_analysis.ensure_analysis_task(org["id"], actor_id=UID)
    assert task_id
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT work_branch FROM autopilot_tasks WHERE id=?", (task_id,)
        ).fetchone()
    assert row and row[0] == "grape-org"
