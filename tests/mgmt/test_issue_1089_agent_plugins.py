"""Issue #1089 Slice D：agent-description.json agent.plugins（挂点③）测试。

Spec：agent-description.json 新增 agent.plugins 默认插件（Agent 类型种子默认安装）；
Runtime 注册响应按引擎角色标签下发 desired_plugins（种子默认安装的协商回路）。

测试 seam（公开接口）：
- resolver.get_agent_plugins：Agent 类型默认插件读取（configs/agent-description.json）
- configs/agent-description.json：全部 agent 声明 plugins 字段（schema 扩展落地）
- POST /api/runtime/register：响应 per-runtime desired_plugins（按角色标签并集）
"""
import hashlib
import secrets

import pytest
from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.plugins.resolver import get_agent_plugins


def _make_token(name="test_1089_worker"):
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO runtime_credentials (token_hash, name, scope) VALUES (?,?,?)",
                  (hashlib.sha256(token.encode()).hexdigest(), name, "global"))
        db.commit()
    return token


@pytest.fixture
def client():
    return TestClient(__import__("src.api.main", fromlist=["app"]).app)


@pytest.fixture(autouse=True)
def _cleanup_runtimes():
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'plug-eng-%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'test_1089_%'")
        db.commit()


class TestAgentDescriptionPlugins:
    def test_all_agents_declare_plugins_field(self):
        import json
        from src.kernel.config import CONFIG_DIR
        with open(f"{CONFIG_DIR}/agent-description.json", encoding="utf-8") as f:
            desc = json.load(f)
        assert desc, "agent-description.json 不应为空"
        for agent_type, meta in desc.items():
            assert "plugins" in meta, f"agent {agent_type} 缺少 plugins 字段"
            assert isinstance(meta["plugins"], list)

    def test_get_agent_plugins_reads_config(self, monkeypatch):
        monkeypatch.setattr(
            "src.domains.mgmt.plugins.resolver._load_agent_description",
            lambda: {"sre": {"name": "SRE", "plugins": ["huawei-obs"]},
                     "coding": {"name": "Coding", "plugins": []}})
        assert get_agent_plugins("sre") == ["huawei-obs"]
        assert get_agent_plugins("coding") == []
        assert get_agent_plugins("ghost") == []

    def test_get_agent_plugins_real_config_is_list(self):
        # 真实配置读取（schema 扩展后每个 agent 都有 plugins）
        for at in ("vod", "issue", "coding", "qa", "sre"):
            assert isinstance(get_agent_plugins(at), list)


class TestAgentDefaultsApi:
    """挂点③前端选择器数据源：GET /api/plugins/agent-defaults（QA Spec-a3）。"""

    @pytest.fixture
    def client(self):
        return TestClient(__import__("src.api.main", fromlist=["app"]).app)

    def test_agent_defaults_lists_plugins_per_type(self, client, monkeypatch):
        from src.domains.mgmt.auth.grape_account_db import (
            create_grape_account, get_grape_account_by_username, get_token_version)
        from src.api.jwt_utils import create_access_token
        import uuid as _uuid
        from src.kernel.session_manager import db, db_lock
        _uname = f"test_1089_ad_{_uuid.uuid4().hex[:8]}"
        _uid = create_grape_account(_uname, "", "test123") or get_grape_account_by_username(_uname)
        assert _uid
        headers = {"Authorization": f"Bearer {create_access_token(_uid['id'], _uid['username'], get_token_version(_uid['id']))}"}
        try:
            monkeypatch.setattr(
                "src.domains.mgmt.plugins.resolver._load_agent_description",
                lambda: {"coding": {"name": "Coding", "plugins": ["git-pr"]},
                         "sre": {"name": "SRE", "plugins": []}})
            r = client.get("/api/plugins/agent-defaults", headers=headers)
            assert r.status_code == 200
            defaults = {d["agent_type"]: d["plugins"] for d in r.json()["defaults"]}
            assert defaults.get("coding") == ["git-pr"]
            assert defaults.get("sre") == []
        finally:
            with db_lock:
                db.cursor().execute("DELETE FROM grape_accounts WHERE username=?", (_uname,))
                db.commit()

    def test_agent_defaults_requires_auth(self, client):
        r = client.get("/api/plugins/agent-defaults")
        assert r.status_code in (401, 403)


class TestRegisterDesiredPlugins:
    def test_register_returns_desired_plugins_by_role_tags(self, client, monkeypatch):
        monkeypatch.setattr(
            "src.domains.mgmt.plugins.resolver._load_agent_description",
            lambda: {"coding": {"name": "Coding", "plugins": ["git-pr"]},
                     "qa": {"name": "QA", "plugins": ["obs-upload", "git-pr"]},
                     "vod": {"name": "VOD", "plugins": []}})
        token = _make_token("test_1089_dp")
        headers = {"Authorization": f"Bearer {token}"}
        # 无显式角色标签 → 默认 'all' 能力 → 不注入种子默认（种子期望仅由
        # 显式角色标签/控制台远程安装收敛，避免 device 级 installed 难以装齐全类型并集）
        r = client.post("/api/runtime/register", headers=headers, json={
            "device_name": "d1089", "cli_version": "1.0",
            "runtimes": [{"name": "plug-eng-all", "version": "v1", "healthy": True}]})
        assert r.status_code == 200
        entry = r.json()["runtimes"][0]
        assert entry["desired_plugins"] == []

        # 显式角色标签 → 对应 agent 默认插件并集去重排序
        r2 = client.post("/api/runtime/register", headers=headers, json={
            "device_name": "d1089", "cli_version": "1.0",
            "runtimes": [{"name": "plug-eng-coding", "version": "v1", "healthy": True,
                          "role_tags": ["coding"]}]})
        entry2 = r2.json()["runtimes"][0]
        assert entry2["desired_plugins"] == ["git-pr"]

        # 多显式标签 → 并集去重
        r3 = client.post("/api/runtime/register", headers=headers, json={
            "device_name": "d1089", "cli_version": "1.0",
            "runtimes": [{"name": "plug-eng-multi", "version": "v1", "healthy": True,
                          "role_tags": ["coding", "qa"]}]})
        entry3 = r3.json()["runtimes"][0]
        assert entry3["desired_plugins"] == ["git-pr", "obs-upload"]

    def test_register_without_agent_plugins_returns_empty_desired(self, client):
        token = _make_token("test_1089_dp2")
        headers = {"Authorization": f"Bearer {token}"}
        r = client.post("/api/runtime/register", headers=headers, json={
            "device_name": "d1089", "cli_version": "1.0",
            "runtimes": [{"name": "plug-eng-empty", "version": "v1", "healthy": True}]})
        assert r.status_code == 200
        assert r.json()["runtimes"][0]["desired_plugins"] == []
