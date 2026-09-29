"""Token 作用域守卫测试（docs/weekly-03/token-scope-classification-design.md §六·A）。

覆盖：require_no_session_token 对 D 类（账户/平台管理）端点拦截 session_token；
- session_token（tk_）→ 403
- API Key（grape_）→ 放行；JWT → 放行（维持现状）
- 非 D 类端点（tasks/issues/scenes）不被误拦截；scenes 为 VOD 技能用 session 调用，维持 200。
"""
import os, sys

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

import pytest
from fastapi.testclient import TestClient
from src.api.main import app
from src.api.deps import get_current_user_or_api_key, require_no_session_token
from fastapi import HTTPException


def _user(auth_type: str, repo_key: str = "gitcode:o/r") -> dict:
    base = {"id": 1, "username": "tester", "auth_type": auth_type,
            "token": "x", "token_type": "session_token" if auth_type == "session_token" else ("api_key" if auth_type == "api_key" else "jwt")}
    if auth_type == "session_token":
        base.update({"session_key": "s1", "agent_type": "vod", "repo_key": repo_key})
    return base


class TestRequireNoSessionTokenUnit:
    """require_no_session_token 直接单元测试。"""

    def test_rejects_session(self):
        with pytest.raises(HTTPException) as ei:
            require_no_session_token(_user("session_token"))
        assert ei.value.status_code == 403

    def test_passes_api_key(self):
        u = require_no_session_token(_user("api_key"))
        assert u["auth_type"] == "api_key"

    def test_passes_jwt(self):
        u = require_no_session_token(_user("jwt"))
        assert u["auth_type"] == "jwt"


class TestDClassEndpoints:
    """D 类（账户/平台管理）端点：session→403；api_key/jwt 通过守卫。"""

    @pytest.mark.parametrize("path", [
        "/api/skills",
        "/api/skills",
        "/api/repos/member",
        "/api/repos/managed",
        "/api/auth/profile",
        "/api/chat/conversations",
        "/api/agents/vod/o/r",
    ])
    def test_session_rejected(self, path):
        app.dependency_overrides[get_current_user_or_api_key] = lambda: _user("session_token")
        try:
            r = TestClient(app).get(path, headers={"Authorization": "Bearer tk_x"})
        finally:
            app.dependency_overrides.clear()
        assert r.status_code == 403, f"{path} session 应 403 实 {r.status_code}"

    @pytest.mark.parametrize("path", [
        "/api/skills",
        "/api/skills/by-id/1",
        "/api/repos/managed",
        "/api/auth/profile",
        "/api/chat/conversations",
    ])
    @pytest.mark.parametrize("auth_type", ["api_key", "jwt"])
    def test_non_session_allowed_by_guard(self, path, auth_type):
        """api_key / jwt 不被守卫拦截（可能后续业务 400/404，但非 403 守卫）。"""
        app.dependency_overrides[get_current_user_or_api_key] = lambda _t=auth_type: _user(_t)
        try:
            r = TestClient(app).get(path, headers={"Authorization": "Bearer x"})
        finally:
            app.dependency_overrides.clear()
        assert r.status_code != 403, f"{path} {auth_type} 不应被守卫 403"


class TestNonDClassEndpoints:
    """非 D 类（tasks/issues/scenes）不被误拦截。"""

    @pytest.mark.parametrize("path", [
        "/api/tasks/mine",     # A 类
        "/api/issues/o/r",     # B 类
        "/api/knowledge",      # B 类
    ])
    def test_session_allowed(self, path):
        app.dependency_overrides[get_current_user_or_api_key] = lambda: _user("session_token")
        try:
            r = TestClient(app).get(path, headers={"Authorization": "Bearer tk_x"})
        finally:
            app.dependency_overrides.clear()
        assert r.status_code != 403, f"{path} session 不应 403 实 {r.status_code}"


class TestScenesScope:
    """场景 scope：#219 调整——list=user_only（session 403）、detail=dual（session 可读）。

    场景清单不再对 session 开放（agent 无需清单，当前场景经 env GRAPE_SCENE_ID 注入）；
    详情对 session 开放（agent 按 scene_id 取当前场景流水线/节点）。
    """

    def test_scenes_list_session_forbidden(self):
        app.dependency_overrides[get_current_user_or_api_key] = lambda: _user("session_token")
        try:
            r = TestClient(app).get("/api/scenes", headers={"Authorization": "Bearer tk_x"})
        finally:
            app.dependency_overrides.clear()
        assert r.status_code == 403

    def test_scenes_detail_session_allowed(self):
        app.dependency_overrides[get_current_user_or_api_key] = lambda: _user("session_token")
        try:
            r = TestClient(app).get("/api/scenes/default", headers={"Authorization": "Bearer tk_x"})
        finally:
            app.dependency_overrides.clear()
        assert r.status_code != 403