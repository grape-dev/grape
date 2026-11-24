# -*- coding: utf-8 -*-
"""Issue #4260：技能详情缺 id 的 API 层哨兵拦截。

验证点：
  1. ``''`` / ``'undefined'`` / ``'null'``（含大小写与首尾空白变体）→ 400 invalid_skill_id，
     不再落到名称解析误报 404「技能未找到」。
  2. 该拦截不得扩大为「非数字即 400」——未知技能名仍走 resolve_skill_id 返回 404
     （守住 Issue #630 的名称深链契约）。
  3. ``GET /api/skills/by-id/``（空路径）由专用路由返回 400，而非路由未命中 404。
"""
import os

os.environ.setdefault(
    "DEVOPS_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "test_sessions.db"),
)

import pytest
from fastapi import HTTPException

from src.domains.mgmt.routes import skills as skills_route


@pytest.mark.parametrize(
    "sentinel",
    ["", " ", "undefined", "UNDEFINED", "Undefined", "null", "NULL", "  null  "],
)
def test_sentinel_id_returns_400_invalid_skill_id(sentinel):
    with pytest.raises(HTTPException) as ei:
        skills_route.get_skill_detail_by_id(sentinel, user={"id": "1"})
    assert ei.value.status_code == 400
    assert ei.value.detail == {"code": 400, "message": "invalid_skill_id"}


def test_unknown_name_still_404_not_400(monkeypatch):
    """哨兵拦截不得误伤名称深链：未知名称仍按 #630 语义返回 404。"""
    import src.domains.mgmt.user.db as user_db
    monkeypatch.setattr(user_db, "resolve_skill_id", lambda *a, **kw: None)
    with pytest.raises(HTTPException) as ei:
        skills_route.get_skill_detail_by_id("no-such-skill-4260xyz", user={"id": "1"})
    assert ei.value.status_code == 404
    assert ei.value.detail == {"code": 404, "message": "技能未找到"}


def test_empty_path_route_returns_400():
    """``/by-id/`` 空路径由专用路由直接返回 400（而非 404）。"""
    from fastapi.testclient import TestClient
    from src.api.main import app

    r = TestClient(app).get("/api/skills/by-id/")
    assert r.status_code == 400, f"{r.status_code}: {r.text[:200]}"
    assert r.json().get("detail", {}).get("message") == "invalid_skill_id"