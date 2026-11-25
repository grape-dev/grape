"""P0b B3/B6（D-P0-3）：/api/engines/meta 只读元数据端点。

根因：前端 agents.js:355-356 硬编码引擎颜色表 + includes 判定链——新增引擎
前端必改；settings 展示名同病。D-P0-3 拍板：新建只读路由（settings.py 已
1800+ 行），前端从 meta 端点读 descriptor 派生数据，删字符串 includes。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import src.domains.runtime.engine.registry  # noqa: E402,F401

from src.api.main import app


client = TestClient(app)


@pytest.fixture
def member_client():
    """A9 鉴权：端点须组织成员凭据，测试客户端统一走成员身份覆写。"""
    from src.api.deps import get_current_user_or_api_key
    user = {"id": "1", "username": "engmeta_t", "auth_type": "jwt", "org_id": "1"}
    app.dependency_overrides[get_current_user_or_api_key] = lambda: user
    yield client
    app.dependency_overrides.clear()


def test_engines_meta_returns_descriptor_data(member_client):
    """/api/engines/meta 返回 descriptor 派生的引擎元数据（含颜色/别名）。"""
    r = member_client.get("/api/engines/meta")
    assert r.status_code == 200
    data = r.json()
    engines = {e["canonical"]: e for e in data["engines"]}
    # 9 引擎全量
    for canonical in ("opencode", "hermes", "hwcloud", "aishell-dsh", "cursor",
                      "codearts", "codex", "dsh", "atomcode"):
        assert canonical in engines, f"meta 缺 {canonical}"
    # 字段与 descriptor 对齐
    assert engines["hermes"]["display"]["dot_color"] == "#8b5cf6"
    assert engines["opencode"]["display"]["name"] == "OpenCode"
    assert "aishell" in engines["hwcloud"]["aliases"]
    assert engines["hermes"]["protocol"] == "acp"


def test_engines_meta_matches_registry_exactly(member_client):
    """端点输出与 registry 全集一致（新引擎落 descriptor 即自动出现在 meta）。"""
    from src.domains.runtime.engine.descriptor import get_registry
    r = member_client.get("/api/engines/meta")
    got = {e["canonical"] for e in r.json()["engines"]}
    assert got == set(get_registry().all().keys())


def test_engines_meta_is_readonly(member_client):
    """只读端点：非 GET 方法 405。"""
    assert member_client.post("/api/engines/meta").status_code == 405


def test_engines_meta_requires_auth():
    """A9 鉴权双态：匿名 401/403 拒绝；组织成员 200。

    架构守护复审 R 项：本端点初版无 Depends(require_org_member)，且旧测试用
    无凭据 TestClient 断言 200——把「匿名可读引擎清单」写进契约。修复后：
    无凭据拒绝 + 有凭据 200。
    """
    from src.api.main import app as _app
    from src.api.deps import get_current_user_or_api_key

    # 1) 匿名（无依赖覆写、无凭据）→ 必须拒绝
    r = client.get("/api/engines/meta")
    assert r.status_code in (401, 403), \
        f"/api/engines/meta 匿名请求应被鉴权拒绝，got {r.status_code}"

    # 2) 组织成员 → 200
    user = {"id": "1", "username": "engmeta_t", "auth_type": "jwt", "org_id": "1"}
    _app.dependency_overrides[get_current_user_or_api_key] = lambda: user
    try:
        r2 = client.get("/api/engines/meta")
        assert r2.status_code == 200, f"成员凭据应 200，got {r2.status_code}: {r2.text[:200]}"
        assert r2.json()["engines"]
    finally:
        _app.dependency_overrides.clear()
