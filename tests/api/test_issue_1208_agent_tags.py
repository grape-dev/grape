"""Issue #1208 原子契约测试：agent_tags 三维度派生（engine:/scope:/scene:）。

覆盖（Issue 正文 §2.1 agent_tags 表 / §4 实施顺序 3 / D4）：
- derive_agent_tags：engine:=agent_runtimes.name、scope:=凭据 scope（global/user/repo）、
  scene:=scene_tags（空回退 default）；platform:/worker: 本期派生不消费（数据先行）；
- sync_derived_agent_tags：capabilities 行式标签（D4），幂等两跑稳定、不覆盖用户手设行、
  agent_tags JSON 快照刷新、engine_uid 引用同步；
- runtime_get_tags：过滤冒号行（NOT LIKE '%:%'，worker 能力面不暴露派生维度）；
- 注册/心跳链挂点：heartbeat 到达即刷新派生（引擎换绑/凭据 scope 切换随心跳收敛）。
"""
import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture()
def rt_db(monkeypatch):
    """独立测试库：agent_runtimes / runtime_credentials / engine_capabilities。"""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT DEFAULT '', credential_id INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '', status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 0, installed INTEGER DEFAULT 0, last_seen_at TEXT,
        scene_tags TEXT DEFAULT '["default"]', agent_tags TEXT DEFAULT '[]',
        role_tags TEXT DEFAULT '', engine_uid VARCHAR(36) DEFAULT NULL,
        busy_slots INTEGER DEFAULT 0, max_concurrent INTEGER DEFAULT 4,
        probe_status TEXT DEFAULT '', probe_last TEXT DEFAULT '', probe_fail_count INTEGER DEFAULT 0,
        runtime_info TEXT DEFAULT ''
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', status TEXT DEFAULT 'active',
        last_seen_at TEXT DEFAULT '', last_ip TEXT DEFAULT '', device_name TEXT DEFAULT '',
        cpu_percent REAL DEFAULT 0, memory_percent REAL DEFAULT 0, disk_percent REAL DEFAULT 0,
        tmp_percent REAL DEFAULT 0, bandwidth_percent REAL DEFAULT 0, bandwidth_mbps REAL DEFAULT 0,
        load_avg TEXT DEFAULT '', spec TEXT DEFAULT ''
    );
    CREATE TABLE engine_capabilities (
        runtime_id INTEGER, tag TEXT, max_concurrent INTEGER DEFAULT 4,
        priority INTEGER DEFAULT 50, enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '',
        engine_uid VARCHAR(36) DEFAULT NULL
    );
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY, runtime_id INTEGER DEFAULT 0, status TEXT DEFAULT 'queued'
    );
    """)
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", c)
    monkeypatch.setattr(sm, "db_lock", threading.RLock())
    # runtime.py 模块级绑定了 db/db_lock（from src.kernel.session_manager import db）——
    # 必须连路由模块命名空间一起替换（test_issue_253 setup_db 同款口径）
    from src.domains.runtime.routes import runtime as rt_mod
    monkeypatch.setattr(rt_mod, "db", c)
    monkeypatch.setattr(rt_mod, "db_lock", threading.RLock())
    yield c
    c.close()


def _mk_runtime(c, rid=1, name="opencode", cred=7, scope="repo",
                scene_tags='["default"]', caps=("all",)):
    c.execute("INSERT INTO runtime_credentials (id, scope, status) VALUES (?,?, 'active')",
              (cred, scope))
    c.execute("INSERT INTO agent_runtimes (id, name, credential_id, scope, scene_tags) "
              "VALUES (?,?,?,?,?)", (rid, name, cred, scope, scene_tags))
    for t in caps:
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)",
                  (rid, t))


# ===== 派生纯函数 =====

class TestDeriveAgentTags:
    def test_three_dimensions(self):
        from src.domains.runtime.engine.agent_tags import derive_agent_tags
        tags = derive_agent_tags("opencode", "repo", ["default", "iac"])
        assert "engine:opencode" in tags
        assert "scope:repo" in tags
        assert "scene:default" in tags and "scene:iac" in tags

    def test_empty_scene_tags_fallback_default(self):
        from src.domains.runtime.engine.agent_tags import derive_agent_tags
        tags = derive_agent_tags("hermes", "global", [])
        assert "scene:default" in tags
        assert "scope:global" in tags
        assert "engine:hermes" in tags

    def test_scope_normalized(self):
        from src.domains.runtime.engine.agent_tags import derive_agent_tags
        # 未知 scope 归一 global（值域三档）；大小写归一
        assert "scope:global" in derive_agent_tags("x", "weird", ["default"])
        assert "scope:user" in derive_agent_tags("x", "USER", ["default"])

    def test_empty_engine_name_no_engine_tag(self):
        from src.domains.runtime.engine.agent_tags import derive_agent_tags
        tags = derive_agent_tags("", "repo", ["default"])
        assert not [t for t in tags if t.startswith("engine:")]


# ===== capabilities 行式落库（D4）=====

class TestSyncDerivedAgentTags:
    def test_rows_written_and_idempotent(self, rt_db):
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _mk_runtime(rt_db)
        t1 = sync_derived_agent_tags(rt_db.cursor(), 1, "opencode", "repo", ["default"])
        t2 = sync_derived_agent_tags(rt_db.cursor(), 1, "opencode", "repo", ["default"])
        assert t1 == t2  # 幂等两跑稳定
        rows = [r[0] for r in rt_db.execute(
            "SELECT tag FROM engine_capabilities WHERE runtime_id=1 ORDER BY tag")]
        assert "all" in rows                      # 用户/默认平铺行保留
        assert "engine:opencode" in rows
        assert "scope:repo" in rows
        assert "scene:default" in rows
        colon = [r for r in rows if ":" in r]
        assert len(colon) == len(set(colon))      # 无重复冒号行

    def test_scope_change_refreshes(self, rt_db):
        # 凭据 scope 切换（repo→user）后心跳再派生 → 旧冒号行被替换（不残留双 scope 行）
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _mk_runtime(rt_db, scope="repo")
        sync_derived_agent_tags(rt_db.cursor(), 1, "opencode", "repo", ["default"])
        sync_derived_agent_tags(rt_db.cursor(), 1, "opencode", "user", ["default"])
        rows = [r[0] for r in rt_db.execute(
            "SELECT tag FROM engine_capabilities WHERE runtime_id=1")]
        assert "scope:user" in rows and "scope:repo" not in rows

    def test_user_set_rows_untouched(self, rt_db):
        # 用户手设能力行（非冒号）永不删除（D4：派生行只管冒号命名空间）
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _mk_runtime(rt_db, caps=("all", "coding"))
        sync_derived_agent_tags(rt_db.cursor(), 1, "opencode", "repo", ["default"])
        rows = [r[0] for r in rt_db.execute(
            "SELECT tag FROM engine_capabilities WHERE runtime_id=1")]
        assert "coding" in rows

    def test_snapshot_json_refreshed(self, rt_db):
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _mk_runtime(rt_db)
        sync_derived_agent_tags(rt_db.cursor(), 1, "opencode", "repo", ["default"])
        import json
        snap = json.loads(rt_db.execute(
            "SELECT agent_tags FROM agent_runtimes WHERE id=1").fetchone()[0])
        assert "scope:repo" in snap and "all" in snap

    def test_engine_uid_synced_on_rows(self, rt_db):
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _mk_runtime(rt_db)
        sync_derived_agent_tags(rt_db.cursor(), 1, "opencode", "repo", ["default"],
                                engine_uid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        orphan = rt_db.execute(
            "SELECT COUNT(*) FROM engine_capabilities WHERE runtime_id=1 "
            "AND tag LIKE '%:%' AND (engine_uid IS NULL OR engine_uid='')").fetchone()[0]
        assert orphan == 0


# ===== get_tags 过滤冒号行（D4：worker 能力面不暴露派生维度）=====

class TestGetTagsFiltersColon:
    def test_runtime_get_tags_excludes_derived(self, rt_db, monkeypatch):
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _mk_runtime(rt_db, caps=("all", "coding"))
        sync_derived_agent_tags(rt_db.cursor(), 1, "opencode", "repo", ["default"])
        # 路由函数内部 from src.kernel.session_manager import db, db_lock → monkeypatch 生效
        from src.domains.runtime.routes import runtime as rt
        resp = asyncio.run(rt.runtime_get_tags(1, credential_id=7))
        tags = resp["tags"]
        assert "coding" in tags and "all" in tags
        assert not [t for t in tags if ":" in t]


# ===== 心跳链挂点（注册链共用同一派生函数）=====

class TestHeartbeatHook:
    def test_heartbeat_refreshes_derived_tags(self, rt_db, monkeypatch):
        # 场景：引擎 scene_tags 被用户改过（引擎详情弹窗）→ 心跳到达即派生刷新
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags
        _mk_runtime(rt_db, scene_tags='["iac"]')
        from src.domains.runtime.routes import runtime as rt

        class _Req:
            class client:
                host = "127.0.0.1"
        monkeypatch.setattr(rt, "_client_ip", lambda r: "127.0.0.1")
        monkeypatch.setattr("src.worker_package.worker_code_version", lambda: "abc")
        body = rt.HeartbeatBody(runtime_ids=[1])
        resp = asyncio.run(rt.runtime_heartbeat(body, _Req(), credential_id=7))
        assert resp["status"] == "ok"
        rows = [r[0] for r in rt_db.execute(
            "SELECT tag FROM engine_capabilities WHERE runtime_id=1")]
        assert "engine:opencode" in rows and "scene:iac" in rows
