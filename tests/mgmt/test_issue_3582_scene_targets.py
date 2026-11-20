# -*- coding: utf-8 -*-
"""Issue #3582：scenes detail 节点清单与 a2a handoff 目标校验同源 + 409 结构化。

收敛验收（Spec 评论 #202434358）：
  T1 同一解析函数：scenes detail 的 valid_targets 与 _scene_node_check 的
     valid_targets 同源一致；
  T2 按 scenes detail 清单对每个合法节点 handoff 校验均放行（in_scene=True）；
  T3 非法目标 409：conflict_reason=not_in_scene + valid_targets 当前有效清单；
  T4 禁用节点不在有效清单，handoff 侧同步拦截（conflict_reason=node_disabled）；
  T5 技能不可解析节点不在有效清单，handoff 侧 conflict_reason=skill_unresolvable；
  T6 场景定义变更后两处同步生效（同一解析 + 缓存失效链路）。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_issue_3582_scene_targets.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

sys.path.insert(0, ROOT)

import json  # noqa: E402

import pytest  # noqa: E402

from src.api.db_setup import init_database  # noqa: E402
from src.kernel.session_manager import db, db_lock  # noqa: E402
from src.domains.runtime.routes.webhook import _scene_node_check  # noqa: E402
from src.api.scene_targets import resolve_scene_targets  # noqa: E402
from src.api.pipeline_config import invalidate_scene_cache  # noqa: E402

init_database()

REPO = "gitcode:org/issue3582"


@pytest.fixture(autouse=True)
def _clean():
    invalidate_scene_cache()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM skill WHERE skill_name LIKE 'issue3582-%'")
        db.commit()
    yield
    invalidate_scene_cache()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM skill WHERE skill_name LIKE 'issue3582-%'")
        db.commit()


@pytest.fixture(autouse=True, scope="module")
def _restore_shared_db_env():
    yield
    os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_sessions.db")


def _set_issue(number, scene_id):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, scene_id) "
            "VALUES (0,'',?,?,?)", (REPO, number, scene_id))
        db.commit()


def _set_scene(scene_id, nodes):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, "
            "is_active) VALUES (?,?,?,?,1)",
            (scene_id, scene_id, REPO, json.dumps(nodes)))
        db.commit()


def _mk_user_skill(sname):
    # user-scope skill：repo 级场景引用 → resolvable=false（scope 冲突）
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, "
            "skill_path, file_path, scope, owner_id, is_active) "
            "VALUES (?,?,?,?,?,?, 'user', 'u1', 1)",
            (sname, sname, "desc", "issue", sname, ""))
        db.commit()


def test_t1_same_source_valid_targets():
    nodes = [{"id": "issue"}, {"id": "coding", "skill": "coding-cli"},
             {"id": "qa", "enabled": False}]
    _set_issue(1, "scene-3582")
    _set_scene("scene-3582", nodes)
    detail = resolve_scene_targets("scene-3582", repo_key=REPO)
    ck = _scene_node_check(REPO, 1, "coding")
    assert ck["in_scene"] is True
    # 同一解析函数：两处 valid_targets 完全一致（禁用节点被同步过滤）
    assert ck["valid_targets"] == detail["valid_targets"] == ["issue", "coding"]


def test_t2_every_valid_target_passes_handoff_check():
    nodes = [{"id": "vod"}, {"id": "issue"}, {"id": "coding"}, {"id": "qa"}]
    _set_issue(2, "scene-3582")
    _set_scene("scene-3582", nodes)
    for target in resolve_scene_targets("scene-3582", repo_key=REPO)["valid_targets"]:
        assert _scene_node_check(REPO, 2, target)["in_scene"] is True, target


def test_t3_invalid_target_409_structured_reason_and_targets():
    _set_issue(3, "scene-3582")
    _set_scene("scene-3582", [{"id": "coding"}, {"id": "qa"}])
    ck = _scene_node_check(REPO, 3, "nonexistent-agent")
    assert ck["in_scene"] is False
    assert ck["conflict_reason"] == "not_in_scene"
    assert sorted(ck["valid_targets"]) == ["coding", "qa"]


def test_t4_disabled_node_excluded_and_conflict_reason():
    _set_issue(4, "scene-3582")
    _set_scene("scene-3582", [{"id": "coding"}, {"id": "qa", "enabled": False}])
    detail = resolve_scene_targets("scene-3582", repo_key=REPO)
    assert "qa" not in detail["valid_targets"]
    ck = _scene_node_check(REPO, 4, "qa")
    assert ck["in_scene"] is False
    assert ck["conflict_reason"] == "node_disabled"
    assert ck["valid_targets"] == ["coding"]


def test_t5_unresolvable_skill_excluded_and_conflict_reason():
    _mk_user_skill("issue3582-user-skill")
    _set_issue(5, "scene-3582")
    _set_scene("scene-3582",
               [{"id": "coding"},
                {"id": "issue", "skill": "issue3582-user-skill"}])
    detail = resolve_scene_targets("scene-3582", repo_key=REPO)
    assert "issue" not in detail["valid_targets"]      # user-scope → resolvable=false
    ck = _scene_node_check(REPO, 5, "issue")
    assert ck["in_scene"] is False
    assert ck["conflict_reason"] == "skill_unresolvable"
    assert ck["valid_targets"] == ["coding"]


def test_t6_scene_change_syncs_both_reads():
    _set_issue(6, "scene-3582")
    _set_scene("scene-3582", [{"id": "coding"}])
    assert _scene_node_check(REPO, 6, "qa")["in_scene"] is False
    # 场景定义变更（新增 qa 节点）→ 缓存失效后两处同步生效
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE devops_pipeline_scene SET nodes_json=? "
                  "WHERE scene_id=? AND repo_key=?",
                  (json.dumps([{"id": "coding"}, {"id": "qa"}]), "scene-3582", REPO))
        db.commit()
    invalidate_scene_cache()
    detail = resolve_scene_targets("scene-3582", repo_key=REPO)
    ck = _scene_node_check(REPO, 6, "qa")
    assert detail["valid_targets"] == ["coding", "qa"]
    assert ck["in_scene"] is True
    assert ck["valid_targets"] == detail["valid_targets"]


# ===== Issue #3582 复审补充：标识别名一致 / 未解析口径 / 路由级契约 =====

def test_t7_node_id_and_tag_are_unified_targets():
    """AC1：id 与 tag 不同时，valid_targets 同时收录两者（= handoff 接受集合）。"""
    _set_issue(7, "scene-3582")
    _set_scene("scene-3582", [
        {"id": "coding", "tag": "coding-agent"},
        {"id": "qa", "tag": "qa"},
    ])
    detail = resolve_scene_targets("scene-3582", repo_key=REPO)
    assert detail["valid_targets"] == ["coding", "coding-agent", "qa"]
    # id 与 tag 别名均放行，清单不再遗漏 tag
    assert _scene_node_check(REPO, 7, "coding")["in_scene"] is True
    assert _scene_node_check(REPO, 7, "coding-agent")["in_scene"] is True
    assert detail["valid_targets"] == _scene_node_check(
        REPO, 7, "coding-agent")["valid_targets"]


def test_t8_unresolved_skill_kept_dispatchable_by_compat_policy():
    """口径注明：技能名不在 skill 表（官方内置包）按存量兼容视为可派发。"""
    _set_issue(8, "scene-3582")
    _set_scene("scene-3582", [
        {"id": "coding", "skill": "issue3582-not-in-skill-table"},
    ])
    detail = resolve_scene_targets("scene-3582", repo_key=REPO)
    assert detail["valid_targets"] == ["coding"]
    assert _scene_node_check(REPO, 8, "coding")["in_scene"] is True


def _client_and_managed_repo(monkeypatch):
    from fastapi.testclient import TestClient
    from src.api.main import app
    from src.api.deps import get_user_or_none, get_current_user_or_api_key
    import src.api.session_store as store
    import src.domains.mgmt.user.db as user_db

    user = {"id": 1, "username": "alice", "auth_type": "jwt"}
    app.dependency_overrides[get_user_or_none] = lambda: user
    app.dependency_overrides[get_current_user_or_api_key] = lambda: user
    monkeypatch.setattr(user_db, "get_managed_repos",
                        lambda uid: [{"repo_key": REPO}])
    monkeypatch.setattr(store, "is_agent_enabled", lambda *a, **k: True)
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR IGNORE INTO managed_repos "
            "(repo_key, repo_owner, repo_name, full_name) VALUES (?,?,?,?)",
            (REPO, "org", "issue3582", "org/issue3582"))
        db.commit()
    return TestClient(app), app


def test_t9_route_scene_detail_exposes_available_targets(monkeypatch):
    """AC3/AC5：GET /api/scenes/{id} 的 available_nodes/valid_targets 与 handoff 同源。"""
    _set_issue(9, "scene-3582")
    _set_scene("scene-3582", [
        {"id": "coding", "tag": "coding-agent"},
        {"id": "qa", "tag": "qa", "enabled": False},
    ])
    client, app = _client_and_managed_repo(monkeypatch)
    try:
        r = client.get("/api/scenes/scene-3582?repo=org/issue3582")
        assert r.status_code == 200, r.text
        body = r.json()
        assert [n["id"] for n in body["available_nodes"]] == ["coding"]
        assert [n["id"] for n in body["nodes"]] == ["coding", "qa"]
        assert body["valid_targets"] == ["coding", "coding-agent"]
        assert body["valid_targets"] == _scene_node_check(
            REPO, 9, "coding")["valid_targets"]
    finally:
        app.dependency_overrides.clear()


def test_t10_route_a2a_409_structured_reason_and_targets(monkeypatch):
    """AC3/AC5：POST /api/a2a 真实 409 响应含 conflict_reason + valid_targets。"""
    _set_issue(10, "scene-3582")
    _set_scene("scene-3582", [
        {"id": "coding", "tag": "coding-agent"},
        {"id": "qa", "tag": "qa", "enabled": False},
    ])
    client, app = _client_and_managed_repo(monkeypatch)
    try:
        payload = {"repository": "org/issue3582", "issue_number": "10",
                   "context": "issue3582 route 409"}
        r = client.post("/api/a2a", json={"agent": "sre", "payload": payload})
        assert r.status_code == 409, r.text
        data = r.json()
        assert data["conflict_reason"] == "not_in_scene"
        assert data["valid_targets"] == ["coding", "coding-agent"]

        r2 = client.post("/api/a2a", json={"agent": "qa", "payload": payload})
        assert r2.status_code == 409, r2.text
        data2 = r2.json()
        assert data2["conflict_reason"] == "node_disabled"
        assert data2["valid_targets"] == ["coding", "coding-agent"]
    finally:
        app.dependency_overrides.clear()
