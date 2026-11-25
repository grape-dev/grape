"""Issue #1208 原子契约测试：场景引擎状态徽标（L2 可见性，§2.6）——selector 节点红/黄分层。

- selector 节点 0 命中（全库无引擎带该标签）→ selector_dead_nodes（红点语义）；
- selector 节点命中引擎全离线 → selector_offline_nodes（黄点语义）；
- all_offline 兼容聚合（既有消费方 issues.js 警告条不回归）；
- id/uid 绑定节点行为零变化（offline_nodes 原语义）。
"""
import json
import sqlite3
import threading

import pytest


@pytest.fixture()
def st_db(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE devops_pipeline_scene (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scene_id TEXT, name TEXT DEFAULT '',
        description TEXT DEFAULT '', nodes_json TEXT DEFAULT '[]', onboarding TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1, repo_key TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', display_name TEXT DEFAULT '',
        credential_id INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '', status TEXT DEFAULT 'online',
        healthy INTEGER DEFAULT 1, installed INTEGER DEFAULT 1, last_seen_at TEXT,
        engine_uid VARCHAR(36) DEFAULT NULL, role_tags TEXT DEFAULT ''
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', device_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (
        runtime_id INTEGER, tag TEXT, max_concurrent INTEGER DEFAULT 4,
        priority INTEGER DEFAULT 50, enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '', engine_uid VARCHAR(36) DEFAULT NULL
    );
    """)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", c)
    monkeypatch.setattr(sm, "db_lock", threading.RLock())
    from src.api import pipeline_config as pc
    monkeypatch.setattr(pc, "_SCENE_CACHE", {})
    yield c
    c.close()


def _mk_engine(c, rid, name="opencode", cred=7, scope="repo", owner="o/r", status="online",
               caps=("all",), derived=("scope:repo",)):
    import datetime as _dt
    c.execute("INSERT INTO runtime_credentials (id, scope, owner_id, status) VALUES (?,?,?,'active')",
              (cred, scope, owner))
    c.execute("INSERT INTO agent_runtimes (id, name, credential_id, scope, owner_id, status, "
              "healthy, installed, last_seen_at) VALUES (?,?,?,?,?,?,1,1,?)",
              (rid, name, cred, scope, owner, status, _dt.datetime.now(_dt.timezone.utc).isoformat()))
    for t in caps:
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, t))
    for t in derived:
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, t))


def _mk_scene(c, scene_id, nodes):
    c.execute("INSERT INTO devops_pipeline_scene (scene_id, name, nodes_json, is_active, repo_key) "
              "VALUES (?,?,?,1,'')", (scene_id, "s", json.dumps(nodes)))
    c.commit()


class TestSceneEngineStatusSelector:
    def test_selector_zero_hit_dead(self, st_db):
        # 红点：selector 全库无引擎携带（配置死）
        from src.domains.mgmt.routes.scenes import _scene_engine_status
        _mk_scene(st_db, "sc-d", [{"id": "coding", "tag": "coding", "enabled": True,
                                   "engine_selector": {"tags": ["engine:codearts"]}}])
        st = _scene_engine_status("sc-d", [{"id": "coding", "tag": "coding", "enabled": True,
                                            "engine_selector": {"tags": ["engine:codearts"]}}])
        assert "coding" in (st.get("selector_dead_nodes") or [])
        assert "coding" not in (st.get("selector_offline_nodes") or [])
        assert st.get("all_offline") is True

    def test_selector_all_offline_yellow(self, st_db):
        # 黄点：标签有引擎携带但全离线（瞬时死）
        from src.domains.mgmt.routes.scenes import _scene_engine_status
        _mk_engine(st_db, 1, status="offline")
        st_db.commit()
        nodes = [{"id": "coding", "tag": "coding", "enabled": True,
                  "engine_selector": {"tags": ["scope:repo"]}}]
        _mk_scene(st_db, "sc-y", nodes)
        st = _scene_engine_status("sc-y", nodes)
        assert "coding" in (st.get("selector_offline_nodes") or [])
        assert "coding" not in (st.get("selector_dead_nodes") or [])
        assert st.get("all_offline") is True

    def test_selector_healthy_no_warning(self, st_db):
        from src.domains.mgmt.routes.scenes import _scene_engine_status
        _mk_engine(st_db, 1)
        st_db.commit()
        nodes = [{"id": "coding", "tag": "coding", "enabled": True,
                  "engine_selector": {"tags": ["scope:repo"]}}]
        _mk_scene(st_db, "sc-h", nodes)
        st = _scene_engine_status("sc-h", nodes)
        assert st.get("all_offline") is False
        assert not (st.get("selector_dead_nodes") or [])
        assert not (st.get("selector_offline_nodes") or [])

    def test_id_node_zero_change(self, st_db):
        # id 绑定节点原语义零变化（offline_nodes）
        from src.domains.mgmt.routes.scenes import _scene_engine_status
        _mk_engine(st_db, 1, status="offline")
        st_db.commit()
        nodes = [{"id": "coding", "tag": "coding", "enabled": True, "engine_runtime_ids": [1]}]
        _mk_scene(st_db, "sc-i", nodes)
        st = _scene_engine_status("sc-i", nodes)
        assert "coding" in (st.get("offline_nodes") or [])
