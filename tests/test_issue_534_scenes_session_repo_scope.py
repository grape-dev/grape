"""Issue #534: scenes detail/list 缺省按会话上下文（token/session 绑定仓库）解析场景归属。

覆盖 Spec 回归场景：
1. 无 --repo：会话级 token（tk_）绑定仓库的场景直接命中（不再 404）
2. 显式 --repo 优先：指向无该场景的仓库时仍 404（不被会话仓库兜底误命中）
3. 不存在的 scene_id 仍 404，报错信息附「跨仓库场景请附 --repo」变通提示
4. scenes list：缺省按请求上下文 repo_var 解析（JWT 用户），显式 repo 行为不变
5. scenes list：tk_ 会话 token 仍 403（user_only 口径维持，结论见 docs/issue-534/plan.md §4）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from unittest.mock import patch  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402
from src.api.deps import get_current_user_or_api_key, require_no_session_token  # noqa: E402
from src.session_manager import db, db_lock  # noqa: E402
from src.config import repo_var  # noqa: E402

REPO_A = "gitcode:i534/repo-a"
REPO_B = "gitcode:i534/repo-b"
SID = "custom-i534-scene"
UID = 98934


def _session_user(repo_key):
    """模拟会话级临时 token（tk_）认证上下文：携带绑定的 repo_key。"""
    return {"id": UID, "username": "", "token": "tk_test", "token_type": "session_token",
            "auth_type": "session_token", "session_key": "sess-i534",
            "repo_key": repo_key, "agent_type": "coding"}


def _jwt_user():
    """模拟 Web JWT 认证上下文（无仓库绑定，靠 repo_var 请求上下文）。"""
    return {"id": UID, "username": "i534-user", "token": "jwt-test", "token_type": "user",
            "auth_type": "jwt", "login_provider": ""}


NODES = [
    {"id": "vod", "tag": "vod", "skill": "vod-cli", "mode": "emit", "next": "issue",
     "handoff": "a2a", "skip": [], "enabled": True},
    {"id": "issue", "tag": "issue", "skill": "issue-cli", "mode": "emit", "next": "coding",
     "handoff": "a2a", "skip": [], "enabled": True},
    {"id": "coding", "tag": "coding", "skill": "coding-cli-1", "mode": "emit", "next": "qa",
     "handoff": "a2a", "skip": [], "enabled": True},
    {"id": "qa", "tag": "qa", "skill": "qa-cli", "mode": "emit", "next": "sre",
     "handoff": "a2a", "skip": [], "enabled": True},
]


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _purge():
    from src.api.pipeline_config import invalidate_scene_cache
    for rk in (REPO_A, REPO_B, ""):
        invalidate_scene_cache("", rk)
        invalidate_scene_cache(SID, rk)
        invalidate_scene_cache("nope-i534", rk)
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key IN (?, ?)", (REPO_A, REPO_B))
        db.commit()


def _seed(scene_id, repo_key, nodes):
    from src.api.pipeline_config import create_custom_scene
    create_custom_scene(scene_id=scene_id, name=f"scene-{scene_id}", description="i534 test",
                        repo_key=repo_key, nodes=nodes)


@pytest.fixture()
def client():
    """会话级 token（tk_）客户端：绑定 REPO_A。"""
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _session_user(REPO_A)
    app.dependency_overrides[require_no_session_token] = _session_user(REPO_A)
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def jwt_client():
    """Web JWT 客户端：无仓库绑定（scenes list 走 repo_var 请求上下文）。"""
    app.dependency_overrides[get_current_user_or_api_key] = _jwt_user
    app.dependency_overrides[require_no_session_token] = _jwt_user
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


# ===== 1. scenes detail：无 --repo 命中会话所属场景 =====

def test_detail_without_repo_hits_session_scene(client):
    """无 --repo：会话所属仓库（tk_ 绑定 REPO_A）的场景直接命中（修复前 404）。"""
    _seed(SID, REPO_A, NODES)
    r = client.get(f"/api/scenes/{SID}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == SID
    tags = [n["tag"] for n in body["nodes"]]
    assert tags == ["vod", "issue", "coding", "qa"]


# ===== 2. 显式 --repo 优先 =====

def test_detail_explicit_repo_takes_precedence(client):
    """显式 --repo 优先：指向无该场景的仓库仍 404（不被会话仓库兜底误命中）。"""
    _seed(SID, REPO_A, NODES)
    with patch("src.api.pipeline_config._load_pipelines_file", return_value={}), \
         patch("src.api.pipeline_config._load_official_scene_from_market", return_value=None):
        # 显式 repo 指向 REPO_B（无该场景）→ 404
        r = client.get(f"/api/scenes/{SID}", params={"repo": "i534/repo-b"})
        assert r.status_code == 404
    # 显式 repo 指向 REPO_A → 命中
    r = client.get(f"/api/scenes/{SID}", params={"repo": "i534/repo-a"})
    assert r.status_code == 200
    assert any(n["tag"] == "qa" for n in r.json()["nodes"])


# ===== 3. 不存在 scene_id 仍 404（附 --repo 提示） =====

def test_detail_nonexistent_scene_still_404_with_hint(client):
    """不存在的 scene_id 仍 404，报错附 --repo 变通提示（屏蔽文件/市场兜底以确定性复现 404）。"""
    with patch("src.api.pipeline_config._load_pipelines_file", return_value={}), \
         patch("src.api.pipeline_config._load_official_scene_from_market", return_value=None):
        r = client.get("/api/scenes/nope-i534")
        assert r.status_code == 404
        assert "--repo" in r.json()["detail"]["message"]


# ===== 4. scenes list：缺省按请求上下文解析 =====

def test_list_without_repo_resolves_request_context(jwt_client):
    """scenes list 无显式 repo：repo_var 请求上下文解析（替换 app_current_repo placeholder）。

    说明：scenes 路由的 repo_var 由请求中间件设置；TestClient 跨线程 + 同步依赖走
    threadpool，主线程/依赖内 contextvar 不回传路由上下文，故此处对解析入口
    `_resolve_scene_repo_key` 做单元断言（与路由调用同一函数，行为等价），
    端到端显式 repo 路径由 test_list_explicit_repo_unchanged 覆盖。
    """
    from src.api.routes.scenes import _resolve_scene_repo_key
    # 无 repo + JWT 用户 + repo_var 上下文 → 解析为该 repo_key
    token = repo_var.set(REPO_A)
    try:
        assert _resolve_scene_repo_key("", _jwt_user()) == REPO_A
    finally:
        repo_var.reset(token)
    # 无 repo + JWT 用户 + 无上下文 → 空串（查不到 repo 场景，与修复前显式行为一致）
    assert _resolve_scene_repo_key("", _jwt_user()) == ""
    # 会话级 token 绑定仓库优先于 repo_var（同一函数供 detail/list 共用）
    token = repo_var.set(REPO_B)
    try:
        assert _resolve_scene_repo_key("", _session_user(REPO_A)) == REPO_A
    finally:
        repo_var.reset(token)


def test_list_explicit_repo_unchanged(jwt_client):
    """scenes list 显式 repo 行为不变：仅返回该仓库场景。"""
    _seed(SID, REPO_A, NODES)
    _seed("custom-i534-other", REPO_B, NODES)
    r = jwt_client.get("/api/scenes", params={"repo": "i534/repo-b"})
    assert r.status_code == 200, r.text
    ids = [s["id"] for s in r.json()["scenes"]]
    assert "custom-i534-other" in ids
    assert SID not in ids


# ===== 5. scenes list：tk_ 会话 token 维持 user_only（口径结论回归锚点） =====

def test_list_session_token_still_403():
    """scenes list 对 tk_ 会话 token 维持 403 user_only（口径结论：不放开，#529 联动）。

    覆盖 require_no_session_token 真实逻辑（不做 dependency override），
    防止未来误放开导致口径漂移而无测试感知。
    """
    app.dependency_overrides[get_current_user_or_api_key] = lambda: _session_user(REPO_A)
    c = TestClient(app)
    try:
        r = c.get("/api/scenes")
        assert r.status_code == 403
        assert "not allowed" in r.json()["detail"]["message"]
    finally:
        app.dependency_overrides.clear()
