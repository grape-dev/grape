"""Issue #1208 回归用例：引擎场景标签保存后，selector 的 scene: 维度必须立即可命中。

背景（线上实测）：引擎详情里把场景改成「通用开发 + 博客开发」后，scene_tags 列已更新，
但 selector 读的是派生行 engine_capabilities（只在注册/心跳时写），勾「博客开发」仍 0 命中。
该用例看护：PUT /runtime/runtimes/{id}/scene-tags 必须在同一次保存里重算派生标签。

覆盖：
1. 保存新增场景 → engine_capabilities 出现 scene:<新场景>，agent_tags 快照同步，预览立即命中；
2. 保存移除场景 → 派生行被删，预览回到不命中（幂等替换，无残留双值）；
3. 远程引擎用凭据 scope 派生（credential scope=repo，不落 scope:global）；
4. 脏值容错：空列表回退 default（与 query_engines 读侧兜底一致）。
"""
import datetime as _dt
import json
import sqlite3
import threading

import pytest


@pytest.fixture()
def scene_db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', credential_id INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '', owner_name TEXT DEFAULT '', repo_key TEXT DEFAULT '',
        status TEXT DEFAULT 'online', healthy INTEGER DEFAULT 1, installed INTEGER DEFAULT 1,
        last_seen_at TEXT, scene_tags TEXT DEFAULT '["default"]', role_tags TEXT DEFAULT '',
        agent_tags TEXT DEFAULT '', engine_uid VARCHAR(36) DEFAULT NULL, org_id INTEGER DEFAULT 0,
        host_name TEXT DEFAULT '', last_ip TEXT DEFAULT '', device_name TEXT DEFAULT '',
        runtime_info TEXT DEFAULT ''
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', device_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (
        runtime_id INTEGER, tag TEXT, max_concurrent INTEGER DEFAULT 4,
        priority INTEGER DEFAULT 50, enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    """)
    import src.kernel.session_manager as sm
    lock = threading.RLock()
    monkeypatch.setattr(sm, "db", c)
    monkeypatch.setattr(sm, "db_lock", lock)
    # 路由模块是 `from src.kernel.session_manager import db` 的按值绑定，
    # 必须同样指到测库，否则会写到真实 sessions.db（曾导致跨用例 closed database）。
    import src.domains.runtime.routes.runtime as _rt
    import src.domains.mgmt.routes.scenes as _sc
    for _m in (_rt, _sc):
        if hasattr(_m, "db"):
            monkeypatch.setattr(_m, "db", c)
        if hasattr(_m, "db_lock"):
            monkeypatch.setattr(_m, "db_lock", lock)
    yield c
    c.close()


def _mk_engine(c, rid, name, cred, scope, owner="", status="online",
               scene_tags='["default"]', caps=("all",)):
    seen = _dt.datetime.now(_dt.timezone.utc).isoformat()
    if cred:
        c.execute("INSERT INTO runtime_credentials (id, scope, owner_id, status) VALUES (?,?,?,'active')",
                  (cred, scope, owner))
    c.execute("INSERT INTO agent_runtimes (id, name, credential_id, scope, owner_id, status, healthy, "
              "installed, last_seen_at, scene_tags, agent_tags) "
              "VALUES (?,?,?,?,?,?,1,1,?,?,?)",
              (rid, name, cred, scope, owner, status, seen, scene_tags, '["all"]'))
    for t in caps:
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, t))
    # 种子派生行：模拟注册/心跳已写过的旧状态
    from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
    sync_derived_agent_tags(c.cursor(), rid, name, scope, scene_tags)


def _save(c, rid, scenes):
    """走真实路由处理器（与 UI 保存同一条路径）。"""
    from src.domains.runtime.routes.runtime import SceneTagsBody, set_runtime_scene_tags
    # [rebase 适配 2026-11-12] 基线为该路由新加引擎归属守卫（scope=repo 走
    # check_repo_access）：直调测试用 session_token 通道（作用域已限定的既有豁免），
    # 语义与 UI 持 session token 保存一致。
    return set_runtime_scene_tags(rid, SceneTagsBody(scene_tags=scenes),
                                  user={"id": 0, "auth_type": "session_token"})


def _scene_caps(c, rid):
    return sorted(t for (t,) in c.execute(
        "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 AND tag LIKE 'scene:%'", (rid,)))


def _preview_hit(rid, scene, scope="repo", repo="o/r"):
    from src.domains.runtime.engine.engine_select import selector_candidate_engines
    selector = {"tags": ["scene:" + scene]}
    matched, _dropped = selector_candidate_engines(selector, "coding", repo_key="gitcode:" + repo,
                                                  user_id=0)
    return rid in {m["runtime_id"] for m in matched}


def test_scene_tag_save_immediately_syncs_derived_tags(scene_db):
    """核心看护：改成博客开发后，scene:blog-publish 立即可命中（不是等心跳）。"""
    _mk_engine(scene_db, 1, "opencode", 7, "repo", "o/r")
    assert _scene_caps(scene_db, 1) == ["scene:default"]

    _save(scene_db, 1, ["default", "blog-publish"])

    assert _scene_caps(scene_db, 1) == ["scene:blog-publish", "scene:default"]
    snap = json.loads(scene_db.execute(
        "SELECT agent_tags FROM agent_runtimes WHERE id=1").fetchone()[0])
    assert "scene:blog-publish" in snap and "scene:default" in snap
    assert _preview_hit(1, "blog-publish") is True


def test_scene_tag_removal_drops_derived_row(scene_db):
    """移除场景后派生行必须消失（幂等替换，不留旧值）。"""
    _mk_engine(scene_db, 1, "opencode", 7, "repo", "o/r", scene_tags='["default","blog-publish"]')
    assert _preview_hit(1, "blog-publish") is True

    _save(scene_db, 1, ["default"])

    assert _scene_caps(scene_db, 1) == ["scene:default"]
    assert _preview_hit(1, "blog-publish") is False


def test_remote_engine_derives_credential_scope(scene_db):
    """远程引擎的 scope 维度跟凭据走（cred scope=repo），不误落 global。"""
    _mk_engine(scene_db, 2, "hermes", 9, "repo", "o/r", status="online")

    _save(scene_db, 2, ["default", "blog-publish"])

    tags = sorted(t for (t,) in scene_db.execute(
        "SELECT tag FROM engine_capabilities WHERE runtime_id=2 AND tag LIKE '%:%'"))
    assert "scope:repo" in tags and "scope:global" not in tags
    assert "scene:blog-publish" in tags


def test_empty_scene_list_falls_back_to_default(scene_db):
    """脏值容错：清空场景回退 default，与读侧兜底一致。"""
    _mk_engine(scene_db, 3, "opencode", 7, "repo", "o/r")

    _save(scene_db, 3, [])

    assert _scene_caps(scene_db, 3) == ["scene:default"]
    assert json.loads(scene_db.execute(
        "SELECT scene_tags FROM agent_runtimes WHERE id=3").fetchone()[0]) == ["default"]