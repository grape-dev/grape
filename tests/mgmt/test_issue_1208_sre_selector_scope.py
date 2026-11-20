"""SRE 节点的 selector 必须带 scope:user|scope:repo（保存端收紧，Issue #1208 补漏）。

背景：SRE 的 user/repo 边界在 claim 侧没有独立一层——`_DEFAULT_ENGINE_ROLE_TAGS` 是空表、
`claim_by_tag` 无 sre/scope 层、`_selector_guard_blocks` 只做标签 AND 匹配。实际边界完全落在
selector 的 scope 标签上：带 `scope:user`/`scope:repo` 的 selector 只能命中个人/仓库档引擎，
不带 scope 的 selector 会被 global 档引擎认领（SRE 执行部署/运维命令，责任边界不清）。

前端 SRE 的引擎筛选弹窗本来就只提供「个人/仓库」两档，这里把服务端口径补齐，避免纯 API
绕过 UI 造出 `sre + selector(engine:xxx)` 的越界配置。
"""
import sqlite3
import threading

import pytest


@pytest.fixture()
def save_db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE devops_pipeline_scene (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scene_id TEXT, name TEXT DEFAULT '',
        description TEXT DEFAULT '', nodes_json TEXT DEFAULT '[]', onboarding TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1, repo_key TEXT DEFAULT '', updated_at TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', credential_id INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (runtime_id INTEGER, tag TEXT, enabled INTEGER DEFAULT 1);
    CREATE TABLE managed_repos (repo_key TEXT PRIMARY KEY, dev_scene TEXT DEFAULT '');
    """)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", c)
    monkeypatch.setattr(sm, "db_lock", threading.RLock())
    import src.domains.mgmt.user.db as udb
    monkeypatch.setattr(udb, "db", c)
    monkeypatch.setattr(udb, "db_lock", threading.RLock())
    from src.api import pipeline_config as pc
    monkeypatch.setattr(pc, "_SCENE_CACHE", {})
    yield c
    c.close()


def _validate(nodes):
    from src.domains.mgmt.routes.scenes import _validate_scene_engine_ids
    return _validate_scene_engine_ids(nodes, "gitcode:o/r", "u")


def test_sre_selector_without_scope_rejected(save_db):
    """SRE + selector(engine:...) 无 scope → 400（否则 global 档引擎可认领）。"""
    nodes = [{"tag": "sre", "enabled": True,
              "engine_selector": {"tags": ["engine:opencode"]}}]
    with pytest.raises(Exception) as ei:
        _validate(nodes)
    assert getattr(ei.value, "status_code", 0) == 400
    assert "scope:user" in str(getattr(ei.value, "detail", ""))
    assert "scope:repo" in str(getattr(ei.value, "detail", ""))


def test_sre_selector_with_global_scope_rejected(save_db):
    """SRE + scope:global → 400（SRE 禁止全局限定）。"""
    nodes = [{"tag": "sre", "enabled": True,
              "engine_selector": {"tags": ["engine:opencode", "scope:global"]}}]
    with pytest.raises(Exception) as ei:
        _validate(nodes)
    assert getattr(ei.value, "status_code", 0) == 400


@pytest.mark.parametrize("scope_tag", ["scope:user", "scope:repo"])
def test_sre_selector_with_user_or_repo_scope_passes(save_db, scope_tag):
    """带个人/仓库档 scope 的 SRE selector 合法（与前端弹窗两档一致）。"""
    nodes = [{"tag": "sre", "enabled": True,
              "engine_selector": {"tags": ["engine:opencode", scope_tag]}}]
    _validate(nodes)  # 不抛


def test_sre_disabled_node_not_blocked(save_db):
    """enabled=False 的 SRE 节点不拦（与「空绑定」规则同口径，允许保留编辑中配置）。"""
    nodes = [{"tag": "sre", "enabled": False,
              "engine_selector": {"tags": ["engine:opencode"]}}]
    _validate(nodes)


def test_non_sre_selector_without_scope_still_allowed(save_db):
    """非 SRE 节点不受该约束（coding 等可只按引擎类型筛选）。"""
    nodes = [{"tag": "coding", "enabled": True,
              "engine_selector": {"tags": ["engine:opencode"]}}]
    _validate(nodes)
