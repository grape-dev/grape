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
    """AC1/AC2（复审 #202444109 / QA 复审 #955/#2118）：valid_targets 仅含实际可投递角色。

    节点角色取 ``id`` 与 ``tag`` 中**任一**落在 DISPATCHABLE_AGENTS 白名单内的
    名字（id OR tag，非「id 优先、tag 兜底」；与基线
    ``node_matches_agent_role`` 口径一致）：展示用 tag 阶段名（可为中文）/
    非白名单角色（如 chat）不进清单——它们会被 POST /api/a2a 的白名单校验
    400 拒绝，清单不得宣告 handoff 实际不收的目标。
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


def test_t7b_node_with_id_and_tag_both_dispatchable_emits_both_roles():
    """口径固化（QA 复审）：id 与 tag 同时命中白名单时产出两个目标。

    实现为 id OR tag（不是「id 优先、tag 兜底」）：``id=sre/tag=qa`` 的节点
    同时产出 ``sre`` 与 ``qa``，二者均可作为 handoff 目标；与基线
    ``node_matches_agent_role`` 的 id OR tag 放行一致，非回归。
    """
    _set_issue(70, "scene-3582")
    _set_scene("scene-3582", [
        {"id": "sre", "tag": "qa"},                # id 与 tag 均命中白名单
    ])
    detail = resolve_scene_targets("scene-3582", repo_key=REPO)
    assert detail["valid_targets"] == ["sre", "qa"]
    assert _scene_node_check(REPO, 70, "sre")["in_scene"] is True
    assert _scene_node_check(REPO, 70, "qa")["in_scene"] is True


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
        # QA 复审回归（阻塞项）：skipped 保持稳定枚举 not_in_scene（旧消费方
        # 与 help 文档按此字面判定），粒度归因放 conflict_reason；
        # 既有 409 契约字段（ok/reason/detail）经 _a2a_skip_response 补齐。
        assert data["skipped"] == "not_in_scene"
        assert data["ok"] is False
        assert data["reason"] and isinstance(data["detail"], dict)
        assert data["handoff_result"] == "rejected"

        r2 = client.post("/api/a2a", json={"agent": "qa", "payload": payload})
        assert r2.status_code == 409, r2.text
        data2 = r2.json()
        # 粒度归因在 conflict_reason；skipped 仍是稳定枚举 not_in_scene
        assert data2["conflict_reason"] == "node_disabled"
        assert data2["skipped"] == "not_in_scene"
        assert data2["valid_targets"] == ["coding"]
        assert data2["ok"] is False and data2["reason"]
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


# ===== Issue #3582 QA 复审回归（2026-11-23）：组织/存量场景 #955/#2118 =====

ORG_SCENE_NODES = [
    {"id": "org_intake", "tag": "issue", "skill": "org-intake-cli",
     "enabled": True, "next": "org_dispatch", "handoff": "a2a"},
    {"id": "org_dispatch", "tag": "coding", "skill": "org-dispatch-cli",
     "enabled": True, "next": "org_aggregate", "handoff": "a2a"},
    {"id": "org_aggregate", "tag": "qa", "skill": "org-aggregate-cli",
     "enabled": True, "next": "done", "handoff": "complete"},
]


def test_t13_org_scene_tag_roles_resolve_and_route_accepts(monkeypatch):
    """QA 阻塞项 1/4（#955/#2118）：id 非白名单、tag 为角色的组织节点
    按 tag 产出目标；available_nodes / valid_targets / POST 接受集合一致；
    POST /api/a2a 交办 issue/coding/qa 不再 409（此前 valid_targets=[]）。"""
    _set_issue(13, "org-scene-3582")
    _set_scene("org-scene-3582", ORG_SCENE_NODES)
    detail = resolve_scene_targets("org-scene-3582", repo_key=REPO)
    # 组织节点按 tag 落在白名单的名字产出目标，与 POST 接受集合一致
    assert detail["valid_targets"] == ["issue", "coding", "qa"]
    # available_nodes 与 valid_targets 一致：每个可用节点都产出目标
    assert [n["id"] for n in detail["available_nodes"]] == [
        "org_intake", "org_dispatch", "org_aggregate"]
    # _scene_node_check：tag 交办命中 + node 键恢复 + org id 直达别名
    for role, node_id in (("issue", "org_intake"),
                           ("coding", "org_dispatch"),
                           ("qa", "org_aggregate")):
        ck = _scene_node_check(REPO, 13, role)
        assert ck["in_scene"] is True, role
        assert ck["node"]["id"] == node_id, role
        assert ck["valid_targets"] == ["issue", "coding", "qa"]
    assert _scene_node_check(REPO, 13, "org_dispatch")["in_scene"] is True
    ck_sre = _scene_node_check(REPO, 13, "sre")
    assert ck_sre["in_scene"] is False
    assert ck_sre["conflict_reason"] == "not_in_scene"
    assert ck_sre["node"] is None

    # 路由级（QA 要求）：POST /api/a2a 对组织场景 tag 角色全部接受
    client, app = _client_and_managed_repo(monkeypatch)
    import src.api.a2a_terminal_guard as _tg
    monkeypatch.setattr(_tg, "guard_terminal_dispatch", lambda *a, **k: None)
    monkeypatch.setattr("src.domains.runtime.routes.webhook.run_a2a_dispatch",
                        lambda *a, **k: {"status": "ok", "seq": 0})
    try:
        for target in ("issue", "coding", "qa"):
            rr = client.post("/api/a2a", json={"agent": target, "payload": {
                "repository": "org/issue3582", "issue_number": "13",
                "context": f"t13 org {target}"}})
            assert rr.status_code not in (400, 409), (target, rr.status_code, rr.text)
        # 场景外目标仍 409，skipped 保持稳定枚举 not_in_scene
        r409 = client.post("/api/a2a", json={"agent": "sre", "payload": {
            "repository": "org/issue3582", "issue_number": "13",
            "context": "t13 org sre"}})
        assert r409.status_code == 409, r409.text
        body = r409.json()
        assert body["skipped"] == "not_in_scene"
        assert body["conflict_reason"] == "not_in_scene"
        assert body["valid_targets"] == ["issue", "coding", "qa"]
    finally:
        app.dependency_overrides.clear()


def test_t13b_org_scene_409_contract_fields_restored(monkeypatch):
    """QA 阻塞项 2：组织场景 409 经 _a2a_skip_response 组装——ok/reason/
    detail/handoff_result/source_seq 齐全，skipped=not_in_scene 稳定枚举。"""
    _set_issue(14, "org-scene-3582")
    _set_scene("org-scene-3582", [
        ORG_SCENE_NODES[0],
        dict(ORG_SCENE_NODES[2], enabled=False),   # org_aggregate/tag=qa 禁用
    ])
    client, app = _client_and_managed_repo(monkeypatch)
    try:
        r = client.post("/api/a2a", json={"agent": "qa", "payload": {
            "repository": "org/issue3582", "issue_number": "14",
            "context": "t13b disabled org qa"}})
        assert r.status_code == 409, r.text
        body = r.json()
        # skipped 稳定枚举 + 粒度归因分离（node_disabled 只出现在 conflict_reason）
        assert body["skipped"] == "not_in_scene"
        assert body["conflict_reason"] == "node_disabled"
        assert body["ok"] is False
        assert body["reason"]
        assert isinstance(body["detail"], dict)
        assert body["detail"]["conflict_reason"] == "node_disabled"
        assert body["handoff_result"] == "rejected"
        assert body["source_seq"] == 0
        assert body["valid_targets"] == ["issue"]
    finally:
        app.dependency_overrides.clear()
