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


def test_engines_meta_returns_descriptor_data():
    """/api/engines/meta 返回 descriptor 派生的引擎元数据（含颜色/别名）。"""
    r = client.get("/api/engines/meta")
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


def test_engines_meta_matches_registry_exactly():
    """端点输出与 registry 全集一致（新引擎落 descriptor 即自动出现在 meta）。"""
    from src.domains.runtime.engine.descriptor import get_registry
    r = client.get("/api/engines/meta")
    got = {e["canonical"] for e in r.json()["engines"]}
    assert got == set(get_registry().all().keys())


def test_engines_meta_is_readonly():
    """只读端点：非 GET 方法 405。"""
    assert client.post("/api/engines/meta").status_code == 405
