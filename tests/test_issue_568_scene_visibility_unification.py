"""Issue #568: scenes detail 与 a2a handoff 场景可见性统一（404 vs 409 nodes）。

覆盖 Spec 验收场景：
1. 两路一致：同一 scene_id 下 `scenes detail` 与 a2a `_scene_node_check` 返回一致节点集合
   （DB 项目场景行 / market+pipelines 官方兜底两来源；统一走 get_pipeline_config）
2. 会话绑定二级解析：会话 token 绑定仓库 ≠ 场景归属仓库（repo 作用域 miss）时，
   按 agent_tasks → issue 绑定场景命中（对齐 a2a 口径），节点集合一致
3. 权限不放松：显式 --repo 指向无该场景的仓库仍 404（不被会话绑定兜底误命中）；
   issue 绑定场景与请求 scene_id 不一致时不兜底
4. cross_scene_handoff：`_refresh_issue_scene_after_handoff` 刷新 issue 绑定场景为
   目标场景有效 ID（`_resolve_scene_id` / GRAPE_SCENE_ID 注入源可读到且节点有效）；
   目标场景无有效 DB 行时不刷新（返回空串）
5. a2a handoff 目标场景不存在 → target_scene_not_found 拦截
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import json  # noqa: E402
import pytest  # noqa: E402
from unittest.mock import patch  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402
from src.api.deps import get_current_user_or_api_key  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402
from src.api.db_setup import init_database  # noqa: E402

init_database()

from src.api.routes.webhook import _scene_node_check, _refresh_issue_scene_after_handoff  # noqa: E402
from src.api.pipeline_config import invalidate_scene_cache  # noqa: E402

REPO_A = "gitcode:i568/repo-a"   # issue 归属仓库（场景归属）
REPO_B = "gitcode:i568/repo-b"   # 会话 token 绑定仓库（repo 作用域 miss）
SID = "custom-i568-scene"
SID2 = "custom-i568-scene-b"
UID = 98968
ISSUE_NO = 5681
SESSION_KEY = "sess-i568-task"

NODES = [
    {"id": "vod", "tag": "vod", "skill": "vod-cli", "mode": "emit", "next": "issue",
     "handoff": "a2a", "skip": [], "enabled": True},
    {"id": "issue", "tag": "issue", "skill": "issue-cli", "mode": "emit", "next": "coding",
     "handoff": "a2a", "skip": [], "enabled": True},
    {"id": "coding", "tag": "coding", "skill": "coding-cli-1", "mode": "emit", "next": "qa",
     "handoff": "a2a", "skip": [], "enabled": True},
    {"id": "qa", "tag": "qa", "skill": "qa-cli", "mode": "emit", "next": "done",
     "handoff": "a2a", "skip": [], "enabled": True},
]


def _session_user(repo_key, session_key=SESSION_KEY):
    return {"id": UID, "username": "", "token": "tk_test", "token_type": "session_token",
            "auth_type": "session_token", "session_key": session_key,
            "repo_key": repo_key, "agent_type": "coding"}


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _purge():
    invalidate_scene_cache()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key IN (?, ?)", (REPO_A, REPO_B))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key IN (?, ?)", (REPO_A, REPO_B))
        c.execute("DELETE FROM agent_tasks WHERE id=?", (SESSION_KEY,))
        db.commit()


def _seed_scene(repo_key, scene_id, nodes):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active) "
            "VALUES (?,?,?,?,1)",
            (scene_id, scene_id, repo_key, json.dumps(nodes)))
        db.commit()


def _seed_issue(repo_key, number, scene_id):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, scene_id) "
            "VALUES (0,'',?,?,?)",
            (repo_key, number, scene_id))
        db.commit()


def _seed_agent_task(session_key, repo_key, issue_id):
    with db_lock:
        db.cursor().execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, issue_id, repo_key) "
            "VALUES (?, 'sess-row', 0, 'x', ?, ?)",
            (session_key, issue_id, repo_key))
        db.commit()


def _no_file_or_market_fallback():
    """屏蔽 market/pipelines 兜底（确定性复现 repo 作用域 miss / 404）。"""
    return patch("src.api.pipeline_config._load_pipelines_file", return_value={}), \
           patch("src.api.pipeline_config._load_official_scene_from_market", return_value=None)


# ===== 1. 两路节点集合一致 =====

def test_nodes_consistent_between_detail_and_a2a():
    """DB 场景行：同一 scene_id 下 scenes detail 与 _scene_node_check 节点集合一致。"""
    _seed_scene(REPO_A, SID, NODES)
    _seed_issue(REPO_A, ISSUE_NO, SID)
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _session_user(REPO_A)
    c = TestClient(app)
    try:
        r = c.get(f"/api/scenes/{SID}")
        assert r.status_code == 200, r.text
        detail_nodes = r.json()["nodes"]
        a2a_nodes = _scene_node_check(REPO_A, ISSUE_NO, "coding")["nodes"]
        # Issue #1721：detail 节点附加 resolvable/scope_check 增量字段，
        # 既有节点字段与 a2a 路径保持一致。
        assert len(detail_nodes) == len(a2a_nodes)
        for detail_node, a2a_node in zip(detail_nodes, a2a_nodes):
            for key, value in a2a_node.items():
                assert detail_node[key] == value
            assert "resolvable" in detail_node
            assert "scope_check" in detail_node
        assert [n["id"] for n in a2a_nodes] == ["vod", "issue", "coding", "qa"]
        assert _scene_node_check(REPO_A, ISSUE_NO, "coding")["in_scene"] is True
    finally:
        app.dependency_overrides.clear()


def test_nodes_consistent_official_fallback_source():
    """无 DB 行时两路同走 market/pipelines 官方兜底，节点集合仍一致（统一 get_pipeline_config）。"""
    _seed_issue(REPO_A, ISSUE_NO, "nope-i568-scene")
    # 两路均兜底到 pipelines.json default（or 语义）→ 一致节点集合
    a2a = _scene_node_check(REPO_A, ISSUE_NO, "vod")
    assert a2a["scene_id"] == "nope-i568-scene"
    from src.api.pipeline_config import get_pipeline_config
    fallback_cfg = get_pipeline_config("nope-i568-scene", repo_key=REPO_A)
    assert a2a["nodes"] == (fallback_cfg or {}).get("nodes") or []


# ===== 2. 会话绑定二级解析（repo 作用域 miss → issue 绑定场景命中） =====

def test_detail_session_bound_scene_fallback_when_repo_scope_miss():
    """会话 token 绑定 REPO_B，场景归属 REPO_A：无 --repo 时按 issue 绑定场景命中。

    修复前：repo 作用域 miss → 404（或 default 兜底回显），与 a2a 409 nodes 口径分歧。
    """
    _seed_scene(REPO_A, SID, NODES)
    _seed_issue(REPO_A, ISSUE_NO, SID)
    _seed_agent_task(SESSION_KEY, REPO_A, f"#{ISSUE_NO}")
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _session_user(REPO_B)
    c = TestClient(app)
    try:
        with _no_file_or_market_fallback()[0], _no_file_or_market_fallback()[1]:
            r = c.get(f"/api/scenes/{SID}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == SID
        # 与 a2a 路径（issue 绑定场景 → get_pipeline_config）既有节点字段一致；
        # Issue #1721 后 detail 额外携带 resolvable/scope_check 增量字段。
        a2a_nodes = _scene_node_check(REPO_A, ISSUE_NO, "coding")["nodes"]
        assert len(body["nodes"]) == len(a2a_nodes)
        for detail_node, a2a_node in zip(body["nodes"], a2a_nodes):
            for key, value in a2a_node.items():
                assert detail_node[key] == value
            assert "resolvable" in detail_node
            assert "scope_check" in detail_node
    finally:
        app.dependency_overrides.clear()


def test_detail_session_bound_scene_with_empty_binding_repo():
    """会话 token 绑定 repo_key 为空：agent_tasks 提供归属仓库，仍按 issue 绑定命中。"""
    _seed_scene(REPO_A, SID, NODES)
    _seed_issue(REPO_A, ISSUE_NO, SID)
    _seed_agent_task(SESSION_KEY, REPO_A, f"#{ISSUE_NO}")
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _session_user("")
    c = TestClient(app)
    try:
        with _no_file_or_market_fallback()[0], _no_file_or_market_fallback()[1]:
            r = c.get(f"/api/scenes/{SID}")
        assert r.status_code == 200, r.text
        assert [n["id"] for n in r.json()["nodes"]] == ["vod", "issue", "coding", "qa"]
    finally:
        app.dependency_overrides.clear()


def test_detail_no_fallback_when_issue_scene_mismatch():
    """issue 绑定场景与请求 scene_id 不一致 → 不兜底（维持 miss 行为，权限不放松）。"""
    _seed_scene(REPO_A, SID, NODES)
    _seed_issue(REPO_A, ISSUE_NO, SID)          # issue 绑定 SID
    _seed_scene(REPO_A, SID2, NODES)            # 请求 SID2（未被 issue 绑定）
    _seed_agent_task(SESSION_KEY, REPO_A, f"#{ISSUE_NO}")
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _session_user(REPO_B)
    c = TestClient(app)
    try:
        with _no_file_or_market_fallback()[0], _no_file_or_market_fallback()[1]:
            r = c.get(f"/api/scenes/{SID2}")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ===== 3. 显式 --repo 优先（权限不放松，#534 口径保持） =====

def test_detail_explicit_repo_still_404():
    """显式 --repo 指向无该场景的仓库仍 404（不被会话绑定兜底误命中）。"""
    _seed_scene(REPO_A, SID, NODES)
    _seed_issue(REPO_A, ISSUE_NO, SID)
    _seed_agent_task(SESSION_KEY, REPO_A, f"#{ISSUE_NO}")
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _session_user(REPO_A)
    c = TestClient(app)
    try:
        with _no_file_or_market_fallback()[0], _no_file_or_market_fallback()[1]:
            r = c.get(f"/api/scenes/{SID}", params={"repo": "i568/repo-b"})
        assert r.status_code == 404
        assert "--repo" in r.json()["detail"]["message"]
    finally:
        app.dependency_overrides.clear()


# ===== 4. cross_scene_handoff 移交刷新注入 GRAPE_SCENE_ID =====

def test_refresh_issue_scene_after_handoff_updates_binding():
    """移交后 issue 绑定场景刷新为目标场景有效 ID（GRAPE_SCENE_ID 注入源可读到且节点有效）。"""
    _seed_scene(REPO_A, SID, NODES)
    _seed_issue(REPO_A, ISSUE_NO, "stale-i568-scene")   # 移交前指向失效场景
    effective = _refresh_issue_scene_after_handoff(REPO_A, str(ISSUE_NO), SID)
    assert effective == SID
    # _resolve_scene_id（event_pipeline GRAPE_SCENE_ID 注入源）读到新场景
    from src.api.event_pipeline import _resolve_scene_id
    assert _resolve_scene_id(REPO_A, f"#{ISSUE_NO}") == SID
    # 刷新后的 scene_id 可查且节点有效
    from src.api.pipeline_config import get_pipeline_config
    cfg = get_pipeline_config(SID, repo_key=REPO_A)
    assert [n["id"] for n in cfg["nodes"]] == ["vod", "issue", "coding", "qa"]


def test_refresh_issue_scene_after_handoff_no_row_no_refresh():
    """目标场景无有效 DB 行 → 不刷新（返回空串，软失败不阻断移交）。"""
    _seed_issue(REPO_A, ISSUE_NO, SID)
    assert _refresh_issue_scene_after_handoff(REPO_A, str(ISSUE_NO), "nope-i568-scene") == ""
    from src.api.event_pipeline import _resolve_scene_id
    assert _resolve_scene_id(REPO_A, f"#{ISSUE_NO}") == SID  # 原绑定不被破坏


# ===== 5. a2a handoff 目标场景不存在 → 显式拦截 =====

def test_cross_pipeline_handoff_target_scene_not_found():
    """目标场景不存在（DB/市场/官方兜底均未命中）→ target_scene_not_found 拦截。"""
    from src.api.routes.webhook import _cross_pipeline_handoff
    with patch("src.api.pipeline_config.get_pipeline_config", return_value=None):
        res = _cross_pipeline_handoff(
            REPO_A, str(ISSUE_NO), "nope-i568-scene", "vod",
            {}, "", None, "", "i568/repo-a", "i568")
    assert res == {"ok": False, "error": "target_scene_not_found"}


def test_cross_pipeline_handoff_refreshes_issue_scene():
    """移交主流程：issue 绑定场景被刷新为目标场景有效 ID（软失败不阻断移交）。"""
    from src.api.routes.webhook import _cross_pipeline_handoff
    _seed_scene(REPO_A, SID, NODES)
    _seed_issue(REPO_A, ISSUE_NO, "stale-i568-scene")
    with patch("src.api.event_store.append_event", return_value=1), \
         patch("src.api.event_bus.get_event_bus", side_effect=RuntimeError("no bus in test")), \
         patch("src.api.event_pipeline.run_pipeline", side_effect=None):
        res = _cross_pipeline_handoff(
            REPO_A, str(ISSUE_NO), SID, "vod",
            {}, "", None, "", "i568/repo-a", "i568")
    assert res.get("ok") is True, res
    from src.api.event_pipeline import _resolve_scene_id
    assert _resolve_scene_id(REPO_A, f"#{ISSUE_NO}") == SID
