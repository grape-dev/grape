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

def test_t7_valid_targets_are_dispatchable_roles_only():
    """AC1/AC2（复审 #202444109）：valid_targets 仅含实际可投递角色。

    节点角色取 id 优先、tag 兜底（engine_select.node_agent_role），并与
    DISPATCHABLE_AGENTS 取交集：展示用 tag 阶段名（可为中文）/ 非白名单角色
    （如 chat）不进清单——它们会被 POST /api/a2a 的白名单校验 400 拒绝，
    清单不得宣告 handoff 实际不收的目标。
    """
    _set_issue(7, "scene-3582")
    _set_scene("scene-3582", [
        {"id": "coding", "tag": "开发编码"},       # 展示用中文 tag
        {"id": "qa", "tag": "qa"},                 # id == tag
        {"tag": "sre"},                            # 存量 tag-only 节点兜底
        {"id": "chat", "tag": "chat"},             # 非白名单角色
    ])
    detail = resolve_scene_targets("scene-3582", repo_key=REPO)
    assert detail["valid_targets"] == ["coding", "qa", "sre"]
    # 白名单角色放行；展示用 tag / 非白名单角色不是 handoff 目标
    assert _scene_node_check(REPO, 7, "coding")["in_scene"] is True
    assert _scene_node_check(REPO, 7, "开发编码")["in_scene"] is False
    assert _scene_node_check(REPO, 7, "chat")["in_scene"] is False


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
                        lambda *a, **k: [{"repo_key": REPO}])
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
        # 展示用 tag（coding-agent）不是可投递角色，不在 valid_targets 内
        assert body["valid_targets"] == ["coding"]
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
        assert data["valid_targets"] == ["coding"]
        # 复审 #202444109（AC3）：skipped 与 conflict_reason 对齐（旧契约恒为
        # not_in_scene，与机读归因矛盾；机读消费方以 conflict_reason 为准）
        assert data["skipped"] == data["conflict_reason"] == "not_in_scene"

        r2 = client.post("/api/a2a", json={"agent": "qa", "payload": payload})
        assert r2.status_code == 409, r2.text
        data2 = r2.json()
        assert data2["conflict_reason"] == "node_disabled"
        assert data2["skipped"] == data2["conflict_reason"] == "node_disabled"
        assert data2["valid_targets"] == ["coding"]
    finally:
        app.dependency_overrides.clear()


def test_t11_every_detail_valid_target_is_post_accepted(monkeypatch):
    """阻塞项（复审 #202444109 AC1/AC2）：detail.valid_targets 的每个值
    POST /api/a2a 均不返回 400/409（清单 = POST 实际接受集合）。"""
    _set_issue(11, "scene-3582")
    _set_scene("scene-3582", [
        {"id": "vod"}, {"id": "issue"},
        {"id": "coding", "tag": "开发编码"},
        {"id": "qa"}, {"id": "sre"},
    ])
    client, app = _client_and_managed_repo(monkeypatch)
    # 真实派发会触发引擎选择/入队，与本验收无关：stub 掉派发与终态 guard，
    # 只验证「场景校验 + 400 白名单」不再拒绝清单内目标。
    import src.api.a2a_terminal_guard as _tg
    monkeypatch.setattr(_tg, "guard_terminal_dispatch", lambda *a, **k: None)
    monkeypatch.setattr("src.domains.runtime.routes.webhook.run_a2a_dispatch",
                        lambda *a, **k: {"status": "ok", "seq": 0})
    try:
        r = client.get("/api/scenes/scene-3582?repo=org/issue3582")
        assert r.status_code == 200, r.text
        targets = r.json()["valid_targets"]
        assert sorted(targets) == ["coding", "issue", "qa", "sre", "vod"]
        for target in targets:
            rr = client.post("/api/a2a", json={"agent": target, "payload": {
                "repository": "org/issue3582", "issue_number": "11",
                "context": f"t11 {target}"}})
            assert rr.status_code not in (400, 409), (target, rr.status_code, rr.text)
    finally:
        app.dependency_overrides.clear()


def test_t12_node_write_paths_invalidate_scene_cache():
    """AC4（复审 #202444109）：节点写路径立即失效 pipeline 配置缓存。

    此前仅 create/update/delete_custom_scene 失效，set_scene_nodes /
    scene_nodes_write_by_id 写后最多 60s TTL 陈旧；现在真实写路径立即失效，
    scenes detail 与 handoff 同步读到新节点（无需手工 invalidate）。"""
    from src.domains.mgmt.public import scene_nodes_write_by_id
    _set_issue(12, "scene-3582")
    _set_scene("scene-3582", [{"id": "coding"}])
    assert resolve_scene_targets("scene-3582", repo_key=REPO)["valid_targets"] == ["coding"]
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
                  ("scene-3582", REPO))
        row_id = c.fetchone()[0]
        # 真实写路径（按行号覆写）：不手工失效缓存，resolver 立即读到新节点
        changed = scene_nodes_write_by_id(
            json.dumps([{"id": "coding"}, {"id": "qa"}]),
            "2026-11-20T00:00:00", row_id, c)
        db.commit()
    assert changed == 1
    assert resolve_scene_targets("scene-3582", repo_key=REPO)["valid_targets"] == ["coding", "qa"]
    assert _scene_node_check(REPO, 12, "qa")["in_scene"] is True


def test_t12b_set_scene_nodes_write_path_invalidates_cache(monkeypatch):
    """AC4：set_scene_nodes 写路径失效缓存（自有 DSN 生产路径，stub 域游标验证）。"""
    from contextlib import contextmanager
    import src.kernel.db_registry as dbr
    import src.domains.mgmt.pipeline_config as pc
    from src.domains.mgmt import public as mgmt_pub

    class _FakeCursor:
        rowcount = 1

        def __init__(self):
            self.queries = []

        def execute(self, sql, params=()):
            self.queries.append(sql)

        def fetchone(self):
            return ('[{"id": "coding"}]',)  # 旧节点内容 → 与新内容哈希不同 → updated

    fake = _FakeCursor()

    @contextmanager
    def _fake_cursor_ctx(domain):
        yield fake

    invalidated = []
    monkeypatch.setattr(dbr, "cursor", _fake_cursor_ctx)
    monkeypatch.setattr(pc, "invalidate_scene_cache",
                        lambda scene_id="", repo_key="": invalidated.append((scene_id, repo_key)))
    out = mgmt_pub.set_scene_nodes(
        json.dumps([{"id": "coding"}, {"id": "qa"}]),
        scene_id="scene-3582", repo_key=REPO)
    assert out["action"] == "updated"
    assert invalidated == [("scene-3582", REPO)]
