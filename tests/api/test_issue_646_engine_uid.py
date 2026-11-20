"""Issue #646 原子契约测试：engine_uid / credential_uid 稳定键改造。

覆盖切片（Spec v3 验收标准的可测子集，全部走真实 SQLite 临时库，不连 MySQL）：
- S1 uid 派生纯函数：UUIDv5 确定性 / 字段敏感性 / is_uid_str 校验
- S2 迁移 202611080011（加列+索引）幂等可重跑
- S3 迁移 202611080012（回填）：uid 回填、自然键重复行处理、孤儿引用置 NULL 入报告、
  二次执行幂等（uid 不变）、credential 改名不影响 engine_uid
- S4 save_agent_runtimes：uid 优先匹配、重复注册 uid/id 不漂移、表重建后 uid 不变
- S5 双读 helper：resolve_engine_refs / engine_ref_snapshot / get_runtime_id_by_engine_uid
"""
import importlib
import sqlite3
import sys
import types

import pytest

from src.kernel.engine_uid import (
    GRAPE_ENGINE_UID_NAMESPACE,
    derive_engine_uid,
    is_uid_str,
    new_credential_uid,
)
from src.api import db_setup


# ---------------------------------------------------------------------------
# 测试库 schema：最小 legacy 形态（无 uid 列）——模拟未迁移的存量库
# ---------------------------------------------------------------------------

_LEGACY_AGENT_RUNTIMES = """
CREATE TABLE agent_runtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER DEFAULT 0,
    name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    scope TEXT DEFAULT 'global',
    owner_id TEXT DEFAULT '',
    host_name VARCHAR(191) DEFAULT '',
    status TEXT DEFAULT 'offline',
    tags_user_set INTEGER DEFAULT 0,
    org_id INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT ''
)
"""

_LEGACY_CREDENTIALS = """
CREATE TABLE runtime_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL,
    name TEXT NOT NULL UNIQUE,
    scope TEXT DEFAULT 'user',
    owner_id TEXT DEFAULT '',
    owner_name TEXT DEFAULT '',
    status TEXT DEFAULT 'active'
)
"""

_LEGACY_POOL = """
CREATE TABLE agent_type_runtime_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_type TEXT NOT NULL,
    runtime_id INTEGER NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 5,
    enabled INTEGER NOT NULL DEFAULT 1
)
"""

_LEGACY_CAPABILITIES = """
CREATE TABLE engine_capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    max_concurrent INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 50,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
)
"""

_LEGACY_TASKS = """
CREATE TABLE agent_tasks (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT NOT NULL,
    runtime_id INTEGER NOT NULL,
    pool_engine_ids TEXT DEFAULT '',
    status TEXT DEFAULT 'queued'
)
"""

_LEGACY_MESSAGES = """
CREATE TABLE task_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    content TEXT DEFAULT '',
    UNIQUE(task_id, seq)
)
"""


def _legacy_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "issue646.db"))
    for ddl in (_LEGACY_AGENT_RUNTIMES, _LEGACY_CREDENTIALS, _LEGACY_POOL,
                _LEGACY_CAPABILITIES, _LEGACY_TASKS, _LEGACY_MESSAGES):
        conn.execute(ddl)
    conn.commit()
    return conn


def _seed(conn):
    """预置存量数据：1 凭据 / 3 引擎（含 1 自然键重复行）/ 池 / 能力 / 任务 / 消息。

    - 引擎 id=1,2: 同自然键重复（历史去重遗留）→ 1 保留 uid，2 置 NULL 入报告
    - 引擎 id=3: 正常行（credential_id=10）
    - 池 id=1 → runtime 3（有效）；池 id=2 → runtime 999（孤儿）
    - 能力 id=1 → runtime 3；任务 t2 → runtime 999（孤儿）
    """
    c = conn.cursor()
    c.execute("INSERT INTO runtime_credentials (id, token_hash, name) VALUES (10, 'h1', 'worker-a')")
    c.execute("INSERT INTO agent_runtimes (id, credential_id, name, scope, owner_id, host_name) "
              "VALUES (1, 10, 'opencode', 'global', '', '')")
    c.execute("INSERT INTO agent_runtimes (id, credential_id, name, scope, owner_id, host_name) "
              "VALUES (2, 10, 'opencode', 'global', '', '')")
    c.execute("INSERT INTO agent_runtimes (id, credential_id, name, scope, owner_id, host_name) "
              "VALUES (3, 10, 'hermes', 'global', '', '')")
    c.execute("INSERT INTO agent_type_runtime_pool (agent_type, runtime_id) VALUES ('coding', 3)")
    c.execute("INSERT INTO agent_type_runtime_pool (agent_type, runtime_id) VALUES ('qa', 999)")
    c.execute("INSERT INTO engine_capabilities (runtime_id, tag) VALUES (3, 'all')")
    c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, pool_engine_ids) "
              "VALUES ('t1', 's1', 3, '[3]')")
    c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, pool_engine_ids) "
              "VALUES ('t2', 's1', 999, '[999]')")
    c.execute("INSERT INTO task_messages (task_id, seq, type) VALUES ('t1', 1, 'text')")
    conn.commit()


# ---------------------------------------------------------------------------
# S1 · uid 派生纯函数
# ---------------------------------------------------------------------------

class TestEngineUidDerivation:
    def test_v5_deterministic(self):
        a = derive_engine_uid("cu-1", "opencode", "global", "", "host1")
        b = derive_engine_uid("cu-1", "opencode", "global", "", "host1")
        assert a == b and is_uid_str(a)

    def test_v5_case_and_space_insensitive(self):
        a = derive_engine_uid("CU-1", " OpenCode ", "GLOBAL", "", "Host1")
        b = derive_engine_uid("cu-1", "opencode", "global", "", "host1")
        assert a == b

    def test_field_sensitivity(self):
        base = derive_engine_uid("cu-1", "opencode", "global", "", "host1")
        assert derive_engine_uid("cu-2", "opencode", "global", "", "host1") != base
        assert derive_engine_uid("cu-1", "hermes", "global", "", "host1") != base
        assert derive_engine_uid("cu-1", "opencode", "repo", "o/r", "host1") != base
        assert derive_engine_uid("cu-1", "opencode", "global", "", "host2") != base

    def test_credential_uid_v4_random_and_valid(self):
        u = new_credential_uid()
        v = new_credential_uid()
        assert u != v and is_uid_str(u) and is_uid_str(v)

    def test_is_uid_str_rejects_invalid(self):
        assert not is_uid_str("")
        assert not is_uid_str("not-a-uuid")
        assert not is_uid_str(123)
        assert not is_uid_str("Z1F4A9E2B6D4E839F0A5D8C3B21E64700")  # 错误长度/内容
        assert is_uid_str(derive_engine_uid("c", "n", "s", "o", "h"))

    def test_namespace_is_uuid(self):
        import uuid as _uuid
        assert isinstance(GRAPE_ENGINE_UID_NAMESPACE, _uuid.UUID)


# ---------------------------------------------------------------------------
# S2 · 迁移1：加列 + 索引（幂等）
# ---------------------------------------------------------------------------

class TestMigrationAddColumns:
    def test_columns_added(self, tmp_path):
        conn = _legacy_db(tmp_path)
        _seed(conn)
        try:
            db_setup._migration_646_add_uid_columns(conn)
            for tbl, col in (("agent_runtimes", "engine_uid"),
                             ("agent_runtimes", "credential_uid"),
                             ("runtime_credentials", "credential_uid"),
                             ("agent_type_runtime_pool", "engine_uid"),
                             ("engine_capabilities", "engine_uid"),
                             ("agent_tasks", "engine_uid"),
                             ("agent_tasks", "engine_name_snapshot"),
                             ("agent_tasks", "pool_engine_uids"),
                             ("task_messages", "engine_uid")):
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
                assert col in cols, f"{tbl}.{col} 未加列"
        finally:
            conn.close()

    def test_idempotent_rerun(self, tmp_path):
        conn = _legacy_db(tmp_path)
        try:
            db_setup._migration_646_add_uid_columns(conn)
            db_setup._migration_646_add_uid_columns(conn)  # 不报错
        finally:
            conn.close()

    def test_unique_index_created(self, tmp_path):
        conn = _legacy_db(tmp_path)
        try:
            db_setup._migration_646_add_uid_columns(conn)
            idx = [r[1] for r in conn.execute("PRAGMA index_list(agent_runtimes)").fetchall()]
            assert "idx_agent_runtimes_engine_uid" in idx
            idx2 = [r[1] for r in conn.execute("PRAGMA index_list(runtime_credentials)").fetchall()]
            assert "idx_runtime_credentials_credential_uid" in idx2
        finally:
            conn.close()

    def test_migrations_registered(self):
        versions = {m.version: m for m in db_setup._MIGRATIONS}
        for v in ("202611080011", "202611080012", "202611080013"):
            assert v in versions, f"迁移 {v} 未注册"
        assert versions["202611080012"].kind == "backfill"

    def test_mysql_text_columns_drop_default(self, monkeypatch):
        """QA R1：MySQL 后端 TEXT 列加列 DDL 去默认字面量（ALTER 路径 TEXT DEFAULT '' 不合法）。"""
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
        assert db_setup._issue_646_column_ddl("TEXT DEFAULT ''") == "TEXT NULL"
        assert db_setup._issue_646_column_ddl("MEDIUMTEXT DEFAULT '{}'") == "MEDIUMTEXT NULL"
        # 非 TEXT 列（VARCHAR）不受影响
        assert db_setup._issue_646_column_ddl("VARCHAR(36) DEFAULT NULL") == "VARCHAR(36) DEFAULT NULL"
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: False)
        assert db_setup._issue_646_column_ddl("TEXT DEFAULT ''") == "TEXT DEFAULT ''"

    def test_add_columns_migration_raises_when_column_missing(self, tmp_path, monkeypatch):
        """QA R1：列未建成 → 抛错不盖章（下次启动幂等重试），不留「已盖章+回填挂起」中间态。"""
        conn = _legacy_db(tmp_path)
        try:
            monkeypatch.setattr(db_setup, "_ensure_column", lambda c, t, col, ddl: None)
            with pytest.raises(RuntimeError):
                db_setup._migration_646_add_uid_columns(conn)
            assert db_setup._issue_646_columns_missing(conn.cursor())  # 核验清单非空
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# S3 · 迁移2：回填（幂等 / 重复行 / 孤儿 / 报告 / 改名不漂移）
# ---------------------------------------------------------------------------

class TestMigrationBackfill:
    def _migrated(self, tmp_path):
        conn = _legacy_db(tmp_path)
        _seed(conn)
        db_setup._migration_646_add_uid_columns(conn)
        return conn

    def test_backfill_assigns_uids(self, tmp_path):
        conn = self._migrated(tmp_path)
        try:
            report = db_setup._migration_646_backfill_uids(conn)
            cred_uid = conn.execute("SELECT credential_uid FROM runtime_credentials WHERE id=10").fetchone()[0]
            assert is_uid_str(cred_uid)
            # 引擎 1/3 得 uid；引擎 2 为自然键重复行 → NULL
            u1 = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=1").fetchone()[0]
            u3 = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=3").fetchone()[0]
            u2 = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=2").fetchone()[0]
            assert is_uid_str(u1) and is_uid_str(u3) and u1 != u3
            assert u2 is None
            # 重复行入报告
            assert any(d["id"] == 2 for d in report["duplicated_engine_rows"])
            # 池/能力：有效引用得 uid，孤儿置 NULL 入报告
            assert conn.execute("SELECT engine_uid FROM agent_type_runtime_pool WHERE id=1").fetchone()[0] == u3
            assert conn.execute("SELECT engine_uid FROM agent_type_runtime_pool WHERE id=2").fetchone()[0] is None
            assert any(d["runtime_id"] == 999 for d in report["orphan_pool_rows"])
            assert conn.execute("SELECT engine_uid FROM engine_capabilities WHERE id=1").fetchone()[0] == u3
            # 任务：t1 得 uid+快照+pool 镜像；t2 孤儿置 NULL 入报告
            t1 = conn.execute("SELECT engine_uid, engine_name_snapshot, pool_engine_uids "
                              "FROM agent_tasks WHERE id='t1'").fetchone()
            assert t1[0] == u3 and t1[1] == "hermes"
            import json as _j
            assert _j.loads(t1[2]) == [u3]
            t2 = conn.execute("SELECT engine_uid FROM agent_tasks WHERE id='t2'").fetchone()[0]
            assert t2 is None
            assert any(d["task_id"] == "t2" for d in report["orphan_task_runtime"])
            # t2.pool_engine_ids=[999] 孤儿：镜像置空且逐条入 orphan_task_pool_ids 报告（不静默丢弃）
            t2_pool = conn.execute("SELECT pool_engine_uids FROM agent_tasks WHERE id='t2'").fetchone()[0]
            assert t2_pool == "[]"
            t2_orphan = [d for d in report["orphan_task_pool_ids"] if d["task_id"] == "t2"]
            assert t2_orphan and t2_orphan[0]["orphan_engine_ids"] == [999]
            # 消息经任务快照带入
            m = conn.execute("SELECT engine_uid FROM task_messages WHERE id=1").fetchone()[0]
            assert m == u3
        finally:
            conn.close()

    def test_backfill_idempotent_uids_stable(self, tmp_path):
        conn = self._migrated(tmp_path)
        try:
            db_setup._migration_646_backfill_uids(conn)
            u1 = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=1").fetchone()[0]
            cu = conn.execute("SELECT credential_uid FROM runtime_credentials WHERE id=10").fetchone()[0]
            report2 = db_setup._migration_646_backfill_uids(conn)
            u1b = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=1").fetchone()[0]
            cub = conn.execute("SELECT credential_uid FROM runtime_credentials WHERE id=10").fetchone()[0]
            assert u1 == u1b, "二次回填 engine_uid 漂移"
            assert cu == cub, "二次回填 credential_uid 漂移"
            assert report2["engines_backfilled"] == 0 and report2["credentials_backfilled"] == 0
        finally:
            conn.close()

    def test_backfill_report_persisted_to_disk(self, tmp_path):
        """Issue #868 E2E 复核问题2（对齐落盘）：#646 迁移报告同样落盘 JSON 可复核。"""
        import json
        import os
        conn = self._migrated(tmp_path)
        try:
            report = db_setup._migration_646_backfill_uids(conn)
            path = report.get("report_file", "")
            assert path and os.path.isfile(path), "迁移报告应落盘（report_file 返回且文件存在）"
            with open(path, encoding="utf-8") as f:
                persisted = json.load(f)
            assert persisted["issue"] == "646"
            assert any(d["runtime_id"] == 999 for d in persisted["report"]["orphan_pool_rows"])
        finally:
            conn.close()

    def test_credential_rename_keeps_engine_uid(self, tmp_path):
        conn = self._migrated(tmp_path)
        try:
            db_setup._migration_646_backfill_uids(conn)
            u3 = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=3").fetchone()[0]
            conn.execute("UPDATE runtime_credentials SET name='worker-a-renamed' WHERE id=10")
            conn.commit()
            db_setup._migration_646_backfill_uids(conn)
            u3b = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=3").fetchone()[0]
            assert u3 == u3b, "credential 改名导致 engine_uid 漂移"
        finally:
            conn.close()

    def test_report_accessible(self, tmp_path):
        conn = self._migrated(tmp_path)
        try:
            db_setup._migration_646_backfill_uids(conn)
            report = db_setup.get_issue_646_uid_migration_report()
            assert "orphan_pool_rows" in report and isinstance(report, dict)
        finally:
            conn.close()

    def test_backfill_snapshot_display_name_first(self, tmp_path):
        """QA R3：回填快照名与新写路径统一 display_name 优先（COALESCE(display_name, name)）。"""
        conn = self._migrated(tmp_path)
        try:
            conn.execute("UPDATE agent_runtimes SET display_name='Hermes Panel' WHERE id=3")
            conn.commit()
            db_setup._migration_646_backfill_uids(conn)
            t1 = conn.execute("SELECT engine_name_snapshot FROM agent_tasks WHERE id='t1'").fetchone()[0]
            assert t1 == "Hermes Panel", "回填快照应 display_name 优先"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# S4 · save_agent_runtimes：uid 优先 upsert / 重复注册不漂移 / 表重建不漂移
# ---------------------------------------------------------------------------

class TestSaveAgentRuntimesUid:
    @pytest.fixture()
    def patched_db(self, tmp_path, monkeypatch):
        conn = sqlite3.connect(str(tmp_path / "save.db"))
        for ddl in (_LEGACY_AGENT_RUNTIMES, _LEGACY_CREDENTIALS, _LEGACY_POOL,
                    _LEGACY_CAPABILITIES, _LEGACY_TASKS, _LEGACY_MESSAGES):
            conn.execute(ddl)
        # agent_runtimes 需要本路径引用的其余列
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN display_name2 TEXT DEFAULT ''") if False else None
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN installed INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN cli_path TEXT DEFAULT ''")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN version TEXT DEFAULT ''")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN healthy INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN pip_package TEXT DEFAULT ''")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN local_skills TEXT DEFAULT '[]'")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN skill_scan_result TEXT DEFAULT ''")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN last_seen_at TEXT DEFAULT NULL")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN agent_tags TEXT DEFAULT '[]'")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN max_concurrent INTEGER DEFAULT 20")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN scene_tags TEXT DEFAULT '[]'")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN alias TEXT DEFAULT ''")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN worker_version TEXT DEFAULT ''")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN engine_uid VARCHAR(36) DEFAULT NULL")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN credential_uid VARCHAR(36) DEFAULT NULL")
        # 基线演进列（upsert_local_runtimes 原语现含 probe 探活/模型列回写）
        for _col, _ddl in (("probe_status", "TEXT DEFAULT 'pending'"), ("probe_latency", "INTEGER DEFAULT 0"),
                           ("probe_error", "TEXT DEFAULT ''"), ("probe_last", "TEXT DEFAULT NULL"),
                           ("probe_ok_streak", "INTEGER DEFAULT 0"), ("health", "TEXT DEFAULT ''"),
                           ("detached_from_scenes_at", "TEXT DEFAULT ''"), ("role_tags", "TEXT DEFAULT ''"),
                           ("model_name", "TEXT DEFAULT ''"), ("model_source", "TEXT DEFAULT ''"),
                           ("model_reported_at", "TEXT DEFAULT ''"), ("runtime_type", "VARCHAR(32) DEFAULT 'local'"),
                           ("runtime_info", "TEXT DEFAULT '{}'"), ("repo_key", "TEXT DEFAULT NULL"),
                           ("pending_skill_scan", "INTEGER DEFAULT 0"), ("spec", "TEXT DEFAULT ''")):
            try:
                conn.execute(f"ALTER TABLE agent_runtimes ADD COLUMN {_col} {_ddl}")
            except Exception:
                pass
        conn.commit()
        # 迁移补 engine_capabilities.engine_uid
        db_setup._migration_646_add_uid_columns(conn)
        # monkeypatch user.db 的 db/db_lock
        import src.domains.mgmt.user.db as udb
        import threading
        lock = threading.RLock()
        monkeypatch.setattr(udb, "db", conn, raising=True)
        monkeypatch.setattr(udb, "db_lock", lock, raising=True)
        # 三库化重构后 save_agent_runtimes 走 runtime public 原语(_any_tx 回落 session_manager.db),
        # 测试单库桩需同步 patch 主库连接
        import src.kernel.session_manager as _sm_test
        monkeypatch.setattr(_sm_test, "db", conn, raising=True)
        monkeypatch.setattr(_sm_test, "db_lock", lock, raising=True)
        # _sync_agent_tags 打桩（避免拖入 settings 重依赖）
        fake = types.ModuleType("src.domains.mgmt.routes.settings")
        fake._sync_agent_tags = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "src.domains.mgmt.routes.settings", fake)
        return udb, conn

    def test_repeat_register_no_drift(self, patched_db):
        udb, conn = patched_db
        agents = [{"name": "opencode", "display_name": "OpenCode", "installed": True,
                   "healthy": True, "cli_path": "/usr/bin/opencode", "version": "1.0",
                   "pip_package": "", "local_skills": []}]
        udb.save_agent_runtimes(agents, scope="global", owner_id="", host_name="ctrl-1")
        row1 = conn.execute("SELECT id, engine_uid FROM agent_runtimes").fetchall()
        assert len(row1) == 1 and is_uid_str(row1[0][1])
        udb.save_agent_runtimes(agents, scope="global", owner_id="", host_name="ctrl-1")
        row2 = conn.execute("SELECT id, engine_uid FROM agent_runtimes").fetchall()
        assert len(row2) == 1, "重复注册产生重复行"
        assert row2[0][0] == row1[0][0] and row2[0][1] == row1[0][1], "重复注册 id/uid 漂移"

    def test_rebuild_keeps_uid(self, patched_db):
        udb, conn = patched_db
        agents = [{"name": "opencode", "display_name": "OpenCode", "installed": True,
                   "healthy": True, "cli_path": "/x", "version": "1.0",
                   "pip_package": "", "local_skills": []}]
        udb.save_agent_runtimes(agents, scope="global", owner_id="", host_name="ctrl-1")
        uid = conn.execute("SELECT engine_uid FROM agent_runtimes").fetchone()[0]
        # 模拟表重建（_rebuild_agent_runtimes_inst_key 类场景）：清表后重新注册
        conn.execute("DELETE FROM agent_runtimes")
        conn.commit()
        udb.save_agent_runtimes(agents, scope="global", owner_id="", host_name="ctrl-1")
        uid2 = conn.execute("SELECT engine_uid FROM agent_runtimes").fetchone()[0]
        assert uid == uid2, "表重建后 engine_uid 漂移（uid 派生非确定性）"

    def test_dedup_rebinds_references(self, patched_db):
        udb, conn = patched_db
        # 预置自然键重复两行（历史遗留）：id=1 保留、id=2 待删
        uid = derive_engine_uid("", "opencode", "global", "", "ctrl-1")
        conn.execute("INSERT INTO agent_runtimes (id, credential_id, name, scope, owner_id, host_name, engine_uid, installed) "
                     "VALUES (1, 0, 'opencode', 'global', '', 'ctrl-1', ?, 1)", (uid,))
        conn.execute("INSERT INTO agent_runtimes (id, credential_id, name, scope, owner_id, host_name, installed) "
                     "VALUES (2, 0, 'opencode', 'global', '', 'ctrl-1', 1)")
        conn.execute("INSERT INTO agent_type_runtime_pool (agent_type, runtime_id) VALUES ('coding', 2)")
        conn.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, pool_engine_ids) VALUES ('t9', 's9', 2, '')")
        conn.commit()
        udb.save_agent_runtimes([{"name": "opencode", "installed": True, "healthy": True,
                                  "cli_path": "/x", "version": "1", "pip_package": "", "local_skills": []}],
                                scope="global", owner_id="", host_name="ctrl-1")
        # 重复行被去重删除，但引用已改绑到保留行（不再孤儿）
        assert conn.execute("SELECT COUNT(*) FROM agent_runtimes").fetchone()[0] == 1
        pool = conn.execute("SELECT runtime_id FROM agent_type_runtime_pool").fetchall()
        assert pool == [(1,)], "池绑定未改绑到保留行"
        task = conn.execute("SELECT runtime_id FROM agent_tasks WHERE id='t9'").fetchone()
        assert task == (1,), "任务引用未改绑到保留行"


# ---------------------------------------------------------------------------
# S5 · 双读 helper
# ---------------------------------------------------------------------------

class TestDualReadHelpers:
    @pytest.fixture()
    def patched_db(self, tmp_path, monkeypatch):
        conn = sqlite3.connect(str(tmp_path / "dual.db"))
        conn.execute(_LEGACY_AGENT_RUNTIMES)
        conn.execute(_LEGACY_TASKS)
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN installed INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN engine_uid VARCHAR(36) DEFAULT NULL")
        uid = derive_engine_uid("", "opencode", "global", "", "h")
        conn.execute("INSERT INTO agent_runtimes (id, name, credential_id, engine_uid, installed) "
                     "VALUES (7, 'opencode', 0, ?, 1)", (uid,))
        conn.commit()
        import src.domains.mgmt.user.db as udb
        import threading
        monkeypatch.setattr(udb, "db", conn, raising=True)
        monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=True)
        # 三库化重构后 save_agent_runtimes 走 runtime public 原语(_any_tx 回落 session_manager.db),
        # 测试单库桩需同步 patch 主库连接
        import src.kernel.session_manager as _sm_test
        monkeypatch.setattr(_sm_test, "db", conn, raising=True)
        monkeypatch.setattr(_sm_test, "db_lock", threading.RLock(), raising=True)
        return udb, conn, uid

    def test_resolve_engine_refs(self, patched_db):
        udb, conn, uid = patched_db
        ids, uids = udb.resolve_engine_refs([7, uid, "not-a-uid"])
        assert ids == [7, 7, None]
        assert uids[0] == uid and uids[1] == uid and uids[2] is None

    def test_resolve_invalid_uid(self, patched_db):
        udb, conn, uid = patched_db
        ids, uids = udb.resolve_engine_refs(["11111111-2222-3333-4444-555555555555"])
        assert ids == [None] and uids == ["11111111-2222-3333-4444-555555555555"]

    def test_get_helpers(self, patched_db):
        udb, conn, uid = patched_db
        assert udb.get_engine_uid_by_runtime_id(7) == uid
        assert udb.get_runtime_id_by_engine_uid(uid) == 7
        assert udb.get_engine_uid_by_runtime_id(0) is None
        assert udb.get_runtime_id_by_engine_uid("") is None

    def test_engine_ref_snapshot(self, patched_db):
        udb, conn, uid = patched_db
        e_uid, name, pool_json = udb.engine_ref_snapshot(7, "[7]")
        assert e_uid == uid and name == "opencode"
        import json as _j
        assert _j.loads(pool_json) == [uid]
        # 孤儿/不存在 → 空快照不抛错
        assert udb.engine_ref_snapshot(999) == (None, "", "")


class TestEngineUidHelpersDomainRouted:
    """三库独立拓扑回归：mgmt 进程主库**没有** agent_runtimes（归运行域）。

    旧实现直查 `udb.db` → 1146（Table '<mgmt 库>.agent_runtimes' doesn't exist）被
    except 吞掉 → 一律 None → 场景保存按 uid 绑定必报「引擎 uid 无效」。
    本组用例把进程主库换成空库并给运行域公开读原语打桩：命中桩即证明不再走进程主库，
    覆盖四个 helper（uid/id 互转、批量解析、任务快照）与失败告警去重。
    """

    UID = "00000000-0000-4000-8000-000000000000"
    RID = 42

    @pytest.fixture()
    def patched(self, monkeypatch):
        import sqlite3
        import threading
        import src.domains.mgmt.user.db as udb
        from src.domains.runtime import public as _rt

        empty = sqlite3.connect(":memory:")  # 无 agent_runtimes —— 等价 mgmt 库形态
        monkeypatch.setattr(udb, "db", empty, raising=True)
        monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=True)
        uid, rid = self.UID, self.RID

        def fake_ids(uids):
            return [rid if u == uid else 0 for u in uids]

        def fake_read(name, params=()):
            if name == "agent_runtime_engine_uid_by_id" and params and params[0] == rid:
                return (uid,)
            if name == "agent_runtime_snapshot_by_id" and params and params[0] == rid:
                return (uid, "example-engine", "example-engine")
            return None

        monkeypatch.setattr(_rt, "runtime_ids_by_engine_uids", fake_ids, raising=True)
        monkeypatch.setattr(_rt, "read_one_any", fake_read, raising=True)
        return udb, uid, rid

    def test_helpers_use_runtime_public_surface(self, patched):
        udb, uid, rid = patched
        assert udb.get_runtime_id_by_engine_uid(uid) == rid
        assert udb.get_runtime_id_by_engine_uid(uid.upper()) == rid  # 大小写归一
        assert udb.get_engine_uid_by_runtime_id(rid) == uid
        assert udb.get_runtime_id_by_engine_uid("deadbeef-0000-4000-8000-000000000000") is None

    def test_resolve_engine_refs_domain_routed(self, patched):
        udb, uid, rid = patched
        ids, uids = udb.resolve_engine_refs([uid, rid, "not-a-uid"])
        assert ids == [rid, rid, None]
        assert uids == [uid, uid, None]

    def test_engine_ref_snapshot_domain_routed(self, patched):
        import json as _j
        udb, uid, rid = patched
        e_uid, name, pool_json = udb.engine_ref_snapshot(rid, "[%d]" % rid)
        assert e_uid == uid and name == "example-engine"
        assert _j.loads(pool_json) == [uid]

    def test_lookup_failure_warns_once(self, patched, monkeypatch, caplog):
        """取数失败不能全静默：同一 (调用点, 错误) 只告警一次，保留可诊断线索。"""
        udb, uid, rid = patched
        from src.domains.runtime import public as _rt
        udb._UID_LOOKUP_WARNED.clear()

        def boom(*_a, **_kw):
            raise RuntimeError("runtime db unreachable")

        monkeypatch.setattr(_rt, "runtime_ids_by_engine_uids", boom, raising=True)
        with caplog.at_level("WARNING"):
            assert udb.get_runtime_id_by_engine_uid(uid) is None
            assert udb.get_runtime_id_by_engine_uid(uid) is None
        hits = [r for r in caplog.records if "engine uid lookup failed" in r.getMessage()]
        assert len(hits) == 1, "同一失败只应告警一次（不刷屏）"


# ---------------------------------------------------------------------------
# S6 · 复审整改：FK RESTRICT + 删除路径统一显式清理例程（对账报告）
# ---------------------------------------------------------------------------

class TestFksRestrict:
    def test_fk_ddls_are_restrict_not_cascade(self):
        """迁移 202611080013 三条 FK 一律 ON DELETE RESTRICT（复审裁定落地）。"""
        ddls = db_setup._ISSUE_646_FK_DDLS
        assert len(ddls) == 3
        joined = "\n".join(ddls)
        assert "RESTRICT" in joined
        assert "CASCADE" not in joined, "复审裁定后 FK 不得再使用 CASCADE"
        # 涵盖三条约束：池/能力/凭据
        assert "agent_type_runtime_pool" in joined and "fk_pool_engine_uid" in joined
        assert "engine_capabilities" in joined and "fk_cap_engine_uid" in joined
        assert "runtime_credentials" in joined and "fk_runtime_credential_uid" in joined

    def test_fk_drop_list_matches_names(self):
        drops = "\n".join(db_setup._ISSUE_646_FK_DROPS)
        for name in ("fk_pool_engine_uid", "fk_cap_engine_uid", "fk_runtime_credential_uid"):
            assert name in drops


class TestExplicitCleanupChain:
    """删除路径统一显式清理例程：池/能力显式删除、会话置 0、任务快照保留、对账报告。"""

    @pytest.fixture()
    def patched_db(self, tmp_path, monkeypatch):
        conn = sqlite3.connect(str(tmp_path / "cleanup.db"))
        for ddl in (_LEGACY_AGENT_RUNTIMES, _LEGACY_CREDENTIALS, _LEGACY_POOL,
                    _LEGACY_CAPABILITIES, _LEGACY_TASKS, _LEGACY_MESSAGES):
            conn.execute(ddl)
        # 会话表（删除路径清理对象）
        conn.execute(
            "CREATE TABLE devops_agent_session ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, runtime_id INTEGER DEFAULT 0)")
        # 基线演进列（upsert_local_runtimes 原语现含 probe 探活/模型列回写）
        for _col, _ddl in (("probe_status", "TEXT DEFAULT 'pending'"), ("probe_latency", "INTEGER DEFAULT 0"),
                           ("probe_error", "TEXT DEFAULT ''"), ("probe_last", "TEXT DEFAULT NULL"),
                           ("probe_ok_streak", "INTEGER DEFAULT 0"), ("health", "TEXT DEFAULT ''"),
                           ("detached_from_scenes_at", "TEXT DEFAULT ''"), ("role_tags", "TEXT DEFAULT ''"),
                           ("model_name", "TEXT DEFAULT ''"), ("model_source", "TEXT DEFAULT ''"),
                           ("model_reported_at", "TEXT DEFAULT ''"), ("runtime_type", "VARCHAR(32) DEFAULT 'local'"),
                           ("runtime_info", "TEXT DEFAULT '{}'"), ("repo_key", "TEXT DEFAULT NULL"),
                           ("pending_skill_scan", "INTEGER DEFAULT 0"), ("spec", "TEXT DEFAULT ''"),
                           ("agent_tags", "TEXT DEFAULT '[]'"), ("max_concurrent", "INTEGER DEFAULT 20"),
                           ("scene_tags", "TEXT DEFAULT '[]'"), ("alias", "TEXT DEFAULT ''"),
                           ("worker_version", "TEXT DEFAULT ''"), ("tags_user_set", "INTEGER DEFAULT 0")):
            try:
                conn.execute(f"ALTER TABLE agent_runtimes ADD COLUMN {_col} {_ddl}")
            except Exception:
                pass
        conn.commit()
        db_setup._migration_646_add_uid_columns(conn)
        _seed(conn)
        db_setup._migration_646_backfill_uids(conn)
        # 会话绑定引擎 3
        conn.execute("INSERT INTO devops_agent_session (runtime_id) VALUES (3)")
        conn.commit()
        # 场景清理打桩（例程内 lazy import，打模块属性即可生效；避免触达共享测试库）
        import src.domains.runtime.engine.engine_select as _es
        monkeypatch.setattr(_es, "cleanup_scene_engine_bindings", lambda rids: 0)
        import src.domains.mgmt.user.db as udb
        import threading
        monkeypatch.setattr(udb, "db", conn, raising=True)
        monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=True)
        # 三库化重构后 save_agent_runtimes 走 runtime public 原语(_any_tx 回落 session_manager.db),
        # 测试单库桩需同步 patch 主库连接
        import src.kernel.session_manager as _sm_test
        monkeypatch.setattr(_sm_test, "db", conn, raising=True)
        monkeypatch.setattr(_sm_test, "db_lock", threading.RLock(), raising=True)
        return udb, conn

    def test_explicit_cleanup_chain_and_report(self, patched_db):
        udb, conn = patched_db
        uid3 = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=3").fetchone()[0]
        report = udb.explicit_cleanup_runtime_references(conn.cursor(), [3], reason="revoke test")
        assert report["mode"] == "explicit_cleanup"
        assert report["reason"] == "revoke test"
        assert report["runtime_ids"] == [3]
        # 池/能力引用行显式删除（RESTRICT 兜底前清理）
        assert report["pool_rows_deleted"] == 1
        assert report["capability_rows_deleted"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_type_runtime_pool WHERE runtime_id=3 OR engine_uid=?",
            (uid3,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM engine_capabilities WHERE runtime_id=3").fetchone()[0] == 0
        # 孤儿池行（runtime 999，uid NULL）不受影响
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_type_runtime_pool WHERE runtime_id=999").fetchone()[0] == 1
        # 会话引用置 0（dashboard 红字「无 engine」如实反映）
        assert report["sessions_reset"] == 1
        assert conn.execute("SELECT runtime_id FROM devops_agent_session").fetchall() == [(0,)]
        # 任务历史按快照语义保留（uid+名称快照+runtime_id 原样，恢复走 redispatch）
        assert report["tasks_snapshot_kept"] == 1
        t1 = conn.execute(
            "SELECT runtime_id, engine_uid, engine_name_snapshot FROM agent_tasks WHERE id='t1'").fetchone()
        assert t1 == (3, uid3, "hermes")
        # 引擎行由 caller 随后 DELETE（对账报告不替代删除）
        assert conn.execute("SELECT COUNT(*) FROM agent_runtimes WHERE id=3").fetchone()[0] == 1

    def test_explicit_cleanup_covers_uid_only_references(self, patched_db):
        """池行 engine_uid 指向引擎而 runtime_id 指向别处（双轨残留）→ uid 侧同样清理。"""
        udb, conn = patched_db
        uid3 = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=3").fetchone()[0]
        conn.execute(
            "INSERT INTO agent_type_runtime_pool (agent_type, runtime_id, engine_uid) "
            "VALUES ('sre', 777, ?)", (uid3,))
        conn.commit()
        report = udb.explicit_cleanup_runtime_references(conn.cursor(), [3])
        assert report["pool_rows_deleted"] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_type_runtime_pool WHERE engine_uid=?", (uid3,)).fetchone()[0] == 0

    def test_explicit_cleanup_empty_ids_noop(self, patched_db):
        udb, conn = patched_db
        report = udb.explicit_cleanup_runtime_references(conn.cursor(), [])
        assert report["runtime_ids"] == []
        assert report["pool_rows_deleted"] == 0
        assert report["tasks_snapshot_kept"] == 0
        # 未触发任何删除
        assert conn.execute("SELECT COUNT(*) FROM agent_type_runtime_pool").fetchone()[0] == 2

    def test_rebind_repoints_uid_mirror_to_keep_row(self, patched_db):
        """去重改绑：uid 镜像统一指向保留行 uid（RESTRICT 兜底防误伤）。"""
        udb, conn = patched_db
        uid3 = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=3").fetchone()[0]
        u5 = derive_engine_uid("", "other", "global", "", "")
        conn.execute(
            "INSERT INTO agent_runtimes (id, credential_id, name, scope, owner_id, host_name, engine_uid) "
            "VALUES (5, 10, 'other', 'global', '', '', ?)", (u5,))
        conn.execute(
            "INSERT INTO agent_type_runtime_pool (agent_type, runtime_id, engine_uid) "
            "VALUES ('qa', 5, ?)", (u5,))
        # 同键绑定已在保留行存在 → 删重复；否则改绑 + uid 镜像指向保留行
        conn.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, engine_uid) VALUES (5, 'all', ?)", (u5,))
        conn.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id) VALUES ('t5', 's5', 5)")
        conn.execute("INSERT INTO devops_agent_session (runtime_id) VALUES (5)")
        conn.commit()
        report = udb.rebind_runtime_references(conn.cursor(), [5], 3, reason="dedup test")
        assert report["mode"] == "rebind"
        assert report["tasks_rebound"] == 1
        assert report["sessions_rebound"] == 1
        # 池行改绑到保留行且 uid 镜像指向保留行 uid（孤儿行 qa/999 不受影响）
        assert conn.execute(
            "SELECT runtime_id, engine_uid FROM agent_type_runtime_pool "
            "WHERE agent_type='qa' AND runtime_id=3").fetchone() == (3, uid3)
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_type_runtime_pool WHERE engine_uid=?", (u5,)).fetchone()[0] == 0
        # 能力同键 (3,'all') 已在保留行 → 删重复绑定，保留行原样
        assert conn.execute(
            "SELECT runtime_id, engine_uid FROM engine_capabilities WHERE runtime_id=3 AND tag='all'").fetchone() \
            == (3, uid3)
        assert conn.execute(
            "SELECT COUNT(*) FROM engine_capabilities WHERE runtime_id=5").fetchone()[0] == 0
        # 任务/会话引用改绑（fixture 预置 1 条绑定 runtime 3 的会话 + 本例改绑 1 条 → 共 2 条）
        assert conn.execute(
            "SELECT runtime_id FROM agent_tasks WHERE id='t5'").fetchone() == (3,)
        assert conn.execute(
            "SELECT COUNT(*) FROM devops_agent_session WHERE runtime_id=3").fetchone()[0] == 2

    def test_rebind_fills_null_uid_track_after_backfill_dedup(self, patched_db):
        """QA R2：「先回填去重（重复行 uid 置 NULL）再改绑」顺序下 uid 轨道按保留行补齐。"""
        udb, conn = patched_db
        uid1 = conn.execute("SELECT engine_uid FROM agent_runtimes WHERE id=1").fetchone()[0]
        # 引擎 2 是回填去重置 NULL 的重复行；引用行 runtime_id=2 且 engine_uid 为 NULL
        conn.execute(
            "INSERT INTO agent_type_runtime_pool (agent_type, runtime_id) VALUES ('sre', 2)")
        conn.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag) VALUES (2, 'cli')")
        conn.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id) VALUES ('t9', 's9', 2)")
        conn.commit()
        report = udb.rebind_runtime_references(conn.cursor(), [2], 1)
        assert report["mode"] == "rebind" and report["tasks_rebound"] == 1
        # runtime_id 轨道与 uid 轨道均指向保留行（不留 NULL 尾巴）
        assert conn.execute(
            "SELECT runtime_id, engine_uid FROM agent_type_runtime_pool WHERE agent_type='sre'"
        ).fetchone() == (1, uid1)
        assert conn.execute(
            "SELECT runtime_id, engine_uid FROM engine_capabilities WHERE tag='cli'"
        ).fetchone() == (1, uid1)
        t9 = conn.execute(
            "SELECT runtime_id, engine_uid, engine_name_snapshot FROM agent_tasks WHERE id='t9'"
        ).fetchone()
        assert t9[0] == 1 and t9[1] == uid1 and t9[2] == "opencode"

    def test_save_stale_engines_run_cleanup_chain(self, tmp_path, monkeypatch):
        """刷新过期行清理：save_agent_runtimes 删除 stale 行前走显式清理链（池/能力引用不悬挂）。"""
        conn = sqlite3.connect(str(tmp_path / "stale.db"))
        for ddl in (_LEGACY_AGENT_RUNTIMES, _LEGACY_CREDENTIALS, _LEGACY_POOL,
                    _LEGACY_CAPABILITIES, _LEGACY_TASKS, _LEGACY_MESSAGES):
            conn.execute(ddl)
        for extra in ("installed INTEGER DEFAULT 0", "healthy INTEGER DEFAULT 0",
                      "max_concurrent INTEGER DEFAULT 20"):
            conn.execute(f"ALTER TABLE agent_runtimes ADD COLUMN {extra}")
        for extra in ("cli_path", "version", "pip_package", "local_skills",
                      "skill_scan_result", "last_seen_at", "agent_tags", "scene_tags",
                      "alias", "worker_version"):
            conn.execute(f"ALTER TABLE agent_runtimes ADD COLUMN {extra} TEXT DEFAULT ''")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN engine_uid VARCHAR(36) DEFAULT NULL")
        conn.execute("ALTER TABLE agent_runtimes ADD COLUMN credential_uid VARCHAR(36) DEFAULT NULL")
        # 基线演进列（upsert_local_runtimes 原语现含 probe 探活列回写）
        for _col, _ddl in (("probe_status", "TEXT DEFAULT 'pending'"), ("probe_latency", "INTEGER DEFAULT 0"),
                           ("probe_error", "TEXT DEFAULT ''"), ("probe_last", "TEXT DEFAULT NULL"),
                           ("probe_ok_streak", "INTEGER DEFAULT 0"), ("health", "TEXT DEFAULT ''"),
                           ("detached_from_scenes_at", "TEXT DEFAULT ''"), ("role_tags", "TEXT DEFAULT ''"),
                           ("model_name", "TEXT DEFAULT ''"), ("model_source", "TEXT DEFAULT ''"),
                           ("model_reported_at", "TEXT DEFAULT ''"), ("runtime_type", "VARCHAR(32) DEFAULT 'local'"),
                           ("runtime_info", "TEXT DEFAULT '{}'"), ("repo_key", "TEXT DEFAULT NULL"),
                           ("pending_skill_scan", "INTEGER DEFAULT 0"), ("spec", "TEXT DEFAULT ''"),
                           ("updated_at", "TEXT DEFAULT ''"), ("tags_user_set", "INTEGER DEFAULT 0")):
            try:
                conn.execute(f"ALTER TABLE agent_runtimes ADD COLUMN {_col} {_ddl}")
            except Exception:
                pass
        conn.execute(
            "CREATE TABLE devops_agent_session ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, runtime_id INTEGER DEFAULT 0)")
        conn.commit()
        db_setup._migration_646_add_uid_columns(conn)
        import src.domains.runtime.engine.engine_select as _es
        monkeypatch.setattr(_es, "cleanup_scene_engine_bindings", lambda rids: 0)
        import src.domains.mgmt.user.db as udb
        import threading
        monkeypatch.setattr(udb, "db", conn, raising=True)
        monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=True)
        # 三库化重构后 save_agent_runtimes 走 runtime public 原语(_any_tx 回落 session_manager.db),
        # 测试单库桩需同步 patch 主库连接
        import src.kernel.session_manager as _sm_test
        monkeypatch.setattr(_sm_test, "db", conn, raising=True)
        monkeypatch.setattr(_sm_test, "db_lock", threading.RLock(), raising=True)
        fake = types.ModuleType("src.domains.mgmt.routes.settings")
        fake._sync_agent_tags = lambda *a, **k: None
        monkeypatch.setitem(sys.modules, "src.domains.mgmt.routes.settings", fake)

        def _agent(name):
            return {"name": name, "display_name": name, "installed": True, "healthy": True,
                    "cli_path": "/x", "version": "1", "pip_package": "", "local_skills": []}

        udb.save_agent_runtimes([_agent("opencode")], scope="global", owner_id="", host_name="ctrl-1")
        rid_old = conn.execute(
            "SELECT id FROM agent_runtimes WHERE name='opencode'").fetchone()[0]
        conn.execute(
            "INSERT INTO agent_type_runtime_pool (agent_type, runtime_id) VALUES ('coding', ?)", (rid_old,))
        conn.execute("INSERT INTO devops_agent_session (runtime_id) VALUES (?)", (rid_old,))
        conn.commit()
        # 下一次刷新不再上报 opencode → stale 行删除走显式清理链（池引用删除、会话置 0）
        udb.save_agent_runtimes([_agent("hermes")], scope="global", owner_id="", host_name="ctrl-1")
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_runtimes WHERE name='opencode'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_runtimes WHERE name='hermes'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_type_runtime_pool WHERE runtime_id=?",
            (rid_old,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM engine_capabilities WHERE runtime_id=? AND tag='all'",
            (rid_old,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT runtime_id FROM devops_agent_session").fetchall() == [(0,)]


# ---------------------------------------------------------------------------
# S6 · merge_duplicate_engine_rows：capabilities 迁移的 uid 镜像守卫
# （Issue #826 外部审查：dup 行 capabilities 迁到存活行时 engine_uid 必须同步
#   指向存活行 uid，否则 uid 轨道悬挂指向被删 dup 行；MySQL RESTRICT FK 会拦截
#   DELETE agent_runtimes，且删除段 except 仅 warning → 注册静默不完整。）
# ---------------------------------------------------------------------------

class TestMergeDuplicateEngineRowsUidMirror:
    @pytest.fixture()
    def patched_engine_db(self, tmp_path, monkeypatch):
        conn = sqlite3.connect(str(tmp_path / "merge.db"))
        for ddl in (_LEGACY_AGENT_RUNTIMES, _LEGACY_CREDENTIALS, _LEGACY_POOL,
                    _LEGACY_CAPABILITIES, _LEGACY_TASKS, _LEGACY_MESSAGES):
            conn.execute(ddl)
        conn.commit()
        db_setup._migration_646_add_uid_columns(conn)
        # merge_duplicate_engine_rows 内部 `from src.session_manager import db, db_lock`
        import src.kernel.session_manager as sm
        import threading
        monkeypatch.setattr(sm, "db", conn, raising=True)
        monkeypatch.setattr(sm, "db_lock", threading.RLock(), raising=True)
        return conn

    def test_merge_migrates_capability_uid_to_surviving_row(self, patched_engine_db):
        """守卫：dup 行 capabilities 带 uid → merge → cap 行 uid 必须指向存活行。"""
        conn = patched_engine_db
        uid_dup = derive_engine_uid("", "dup", "global", "", "")
        uid_keep = derive_engine_uid("", "keep", "global", "", "")
        # 同 (credential_id, lower(name)) 两行，MAX(id)=2 存活
        conn.execute(
            "INSERT INTO agent_runtimes (id, credential_id, name, engine_uid) "
            "VALUES (1, 10, 'cli', ?)", (uid_dup,))
        conn.execute(
            "INSERT INTO agent_runtimes (id, credential_id, name, engine_uid) "
            "VALUES (2, 10, 'cli', ?)", (uid_keep,))
        # 存活行无 capabilities → dup 行首条 cap 改指向存活行；uid 轨道须同步
        conn.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, engine_uid) "
            "VALUES (1, 'all', ?)", (uid_dup,))
        conn.commit()
        from src.domains.runtime.engine.engine_select import merge_duplicate_engine_rows
        rid_map = merge_duplicate_engine_rows(10)
        assert rid_map == {1: 2}
        cap = conn.execute(
            "SELECT runtime_id, engine_uid FROM engine_capabilities").fetchone()
        assert cap == (2, uid_keep), (
            f"capabilities 迁移后 uid 须指向存活行（期望 {uid_keep}，实际 {cap and cap[1]}）")
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_runtimes WHERE id=1").fetchone()[0] == 0

    def test_merge_capability_uid_null_track_backfilled(self, patched_engine_db):
        """守卫：cap 行 uid 为 NULL（回填去重置 NULL 顺序）→ 迁移后按存活行补齐。"""
        conn = patched_engine_db
        uid_keep = derive_engine_uid("", "keep2", "global", "", "")
        conn.execute(
            "INSERT INTO agent_runtimes (id, credential_id, name) VALUES (1, 10, 'cli')")
        conn.execute(
            "INSERT INTO agent_runtimes (id, credential_id, name, engine_uid) "
            "VALUES (2, 10, 'cli', ?)", (uid_keep,))
        conn.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, engine_uid) "
            "VALUES (1, 'all', NULL)")
        conn.commit()
        from src.domains.runtime.engine.engine_select import merge_duplicate_engine_rows
        assert merge_duplicate_engine_rows(10) == {1: 2}
        cap = conn.execute(
            "SELECT runtime_id, engine_uid FROM engine_capabilities").fetchone()
        assert cap == (2, uid_keep), "uid 轨道 NULL 须按存活行 uid 补齐（不留 NULL 尾巴）"
