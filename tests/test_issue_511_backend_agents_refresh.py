"""Issue #511: POST /api/settings/backend-agents/refresh 返回 500（await 非 awaitable）。

兼容别名 refresh_backend_agents 为 async def，内部 `return await refresh_agent_runtimes(user)`，
而 refresh_agent_runtimes 是同步 def（返回 dict）→
TypeError: object dict can't be used in 'await' expression → 500。
修复：别名去 await，直接返回同步结果（与 /agent-runtimes/refresh 行为一致）。
关联 #414（QA 回归同根因）。
"""
import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

import pytest

from src.api.routes import settings as settings_routes


@pytest.mark.atomic
async def test_backend_agents_refresh_should_not_500(monkeypatch):
    """兼容别名应与 /agent-runtimes/refresh 行为一致：直接返回同步结果，不 await 非 awaitable。"""
    sentinel = {"runtimes": [{"name": "t511-engine", "installed": True}]}
    monkeypatch.setattr(settings_routes, "refresh_agent_runtimes", lambda user: sentinel)
    resp = await settings_routes.refresh_backend_agents(user={"id": 1, "username": "t511"})
    assert resp == sentinel
