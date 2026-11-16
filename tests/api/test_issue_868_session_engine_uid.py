"""Issue #868 原子契约测试：会话表 engine_uid 双读（T10）。

覆盖切片（Spec 验收标准的可测子集，全部走真实 SQLite 临时库，不连 MySQL）：
- S1 迁移 202611030005/6：加列 + 索引幂等、列未建成不盖章、回填 + 孤儿 uid 置 NULL 入报告
- S2 session_store 双写双读：新建/读取会话带 engine_uid；upsert 改绑镜像 uid；旧库缺列降级
- S3 chat 配置行双写双读：_write_chat_config_row 双写、get_chat_config uid 优先 id 兜底、缺列降级
- S4 对账：_rebind_runtime_references 会话 uid 轨道（改绑→保留行 uid；删除/孤儿→置 NULL 入报告）
"""
import importlib
import sqlite3
import threading

import pytest

from src.api import db_setup
from src.api.engine_uid import derive_engine_uid, is_uid_str


UID_HERMES = derive_engine_uid("", "hermes", "global", "", "")
UID_OPENCODE = derive_engine_uid("", "opencode", "global", "", "")

_LEGACY_RUNTIMES = """
CREATE TABLE agent_runtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER DEFAULT 0,
    name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    scope TEXT DEFAULT 'global',
    owner_id TEXT DEFAULT '',
    host_name VARCHAR(191) DEFAULT '',
    status TEXT DEFAULT 'offline',
    engine_uid VARCHAR(36) DEFAULT NULL
)
"""

_LEGACY_DEVOPS_SESSION = """
CREATE TABLE devops_agent_session (
    id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    runtime_id INTEGER DEFAULT 0,
    runtime_session_id TEXT DEFAULT '',
    skill TEXT DEFAULT '',
    skill_id INTEGER DEFAULT 0,
    repo_gitcode_token TEXT DEFAULT '',
    subscribe_events TEXT DEFAULT '[]',
    im_app_id TEXT DEFAULT '',
    im_app_secret TEXT DEFAULT '',
    im_provider TEXT DEFAULT '',
    status TEXT DEFAULT 'idle',
    enable INTEGER DEFAULT 1,
    last_active_at TEXT DEFAULT ''
)
"""

_LEGACY_CHAT_SESSION = """
CREATE TABLE chat_agent_session (
    id TEXT PRIMARY KEY,
    user_id INTEGER DEFAULT 0,
    repo_key TEXT DEFAULT '',
    runtime_id INTEGER DEFAULT 0,
    skill TEXT DEFAULT '',
    skill_id INTEGER DEFAULT 0,
    im_app_id TEXT DEFAULT '',
    im_app_secret TEXT DEFAULT '',
    im_provider TEXT DEFAULT '',
    status TEXT DEFAULT 'idle',
    created_at TEXT DEFAULT '',
    last_active_at TEXT DEFAULT ''
)
"""

_LEGACY_TASKS = """
CREATE TABLE agent_tasks (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT NOT NULL,
    runtime_id INTEGER NOT NULL,
    status TEXT DEFAULT 'queued'
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
    enabled INTEGER DEFAULT 1
)
"""


def _legacy_db(tmp_path, with_sessions=True, session_uid_col=False):
    conn = sqlite3.connect(str(tmp_path / "issue868.db"))
    ddls = [_LEGACY_RUNTIMES, _LEGACY_TASKS, _LEGACY_POOL, _LEGACY_CAPABILITIES]
    if with_sessions:
        if session_uid_col:
            ddls += [
                _LEGACY_DEVOPS_SESSION.replace(
                    "runtime_id INTEGER DEFAULT 0,",
                    "runtime_id INTEGER DEFAULT 0, engine_uid VARCHAR(36) DEFAULT NULL,"),
                _LEGACY_CHAT_SESSION.replace(
                    "runtime_id INTEGER DEFAULT 0,",
                    "runtime_id INTEGER DEFAULT 0, engine_uid VARCHAR(36) DEFAULT NULL,"),
            ]
        else:
            ddls += [_LEGACY_DEVOPS_SESSION, _LEGACY_CHAT_SESSION]
    for ddl in ddls:
        conn.execute(ddl)
    conn.commit()
    return conn


def _seed_engines(conn):
    c = conn.cursor()
    c.execute("INSERT INTO agent_runtimes (id, name, engine_uid) VALUES (1, 'opencode', ?)", (UID_OPENCODE,))
    c.execute("INSERT INTO agent_runtimes (id, name, engine_uid) VALUES (3, 'hermes', ?)", (UID_HERMES,))
    conn.commit()


# ---------------------------------------------------------------------------
# S1 · 迁移：加列（幂等 / 列核验不盖章）+ 回填（孤儿 uid 置 NULL 入报告）
# ---------------------------------------------------------------------------

class TestMigration868:
    def test_columns_added_and_idempotent(self, tmp_path):
        conn = _legacy_db(tmp_path)
        try:
            db_setup._migration_868_add_session_uid_columns(conn)
            for tbl in ("devops_agent_session", "chat_agent_session"):
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
                assert "engine_uid" in cols, f"{tbl}.engine_uid 未加列"
            db_setup._migration_868_add_session_uid_columns(conn)  # 幂等重跑不报错
            for tbl in ("devops_agent_session", "chat_agent_session"):
                idx = [r[1] for r in conn.execute(f"PRAGMA index_list({tbl})").fetchall()]
                assert f"idx_{tbl}_engine_uid" in idx, f"{tbl} engine_uid 索引未建"
        finally:
            conn.close()

    def test_migrations_registered(self):
        versions = {m.version: m for m in db_setup._MIGRATIONS}
        assert "202611030005" in versions and versions["202611030005"].kind == "safe"
        assert "202611030006" in versions and versions["202611030006"].kind == "backfill"
        # 与 #646 已用版本号区分（防两线漂移）：005/006 在注册表内全局唯一
        _versions = [m.version for m in db_setup._MIGRATIONS]
        assert _versions.count("202611030005") == 1
        assert _versions.count("202611030006") == 1

    def test_missing_column_raises_not_stamped(self, tmp_path, monkeypatch):
        conn = _legacy_db(tmp_path)
        try:
            monkeypatch.setattr(db_setup, "_ensure_column", lambda c, t, col, ddl: None)
            with pytest.raises(RuntimeError):
                db_setup._migration_868_add_session_uid_columns(conn)
        finally:
            conn.close()

    def test_backfill_missing_columns_raises_not_stamped(self, tmp_path):
        """QA Required：005 加列失败不盖章时，006 须预检缺列 raise（不盖章可重跑）。"""
        conn = _legacy_db(tmp_path, session_uid_col=False)  # 模拟 005 未建成列
        _seed_engines(conn)
        try:
            with pytest.raises(RuntimeError):
                db_setup._migration_868_backfill_session_uids(conn)
            # 不盖章语义：函数抛错即返回，未写入任何回填数据（无中间态污染）
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(devops_agent_session)").fetchall()]
            assert "engine_uid" not in cols
        finally:
            conn.close()

    def test_backfill_agent_runtimes_unreadable_raises_not_stamped(self, tmp_path):
        """数据源 agent_runtimes 不可读同样不盖章（避免空回填被盖章成永久中间态）。"""
        conn = _legacy_db(tmp_path, session_uid_col=True)  # 列已建成，仅数据源缺失
        c = conn.cursor()
        c.execute("DROP TABLE agent_runtimes")
        conn.commit()
        try:
            with pytest.raises(RuntimeError):
                db_setup._migration_868_backfill_session_uids(conn)
        finally:
            conn.close()

    def _migrated(self, tmp_path):
        conn = _legacy_db(tmp_path)
        _seed_engines(conn)
        c = conn.cursor()
        c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id) "
                  "VALUES ('s1', 'gitcode:o/r', 'coding', 3)")
        c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id) "
                  "VALUES ('s2', 'gitcode:o/r', 'qa', 999)")      # 孤儿 runtime
        c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id) "
                  "VALUES ('s3', 'gitcode:o/r', 'sre', 0)")       # 未绑定
        c.execute("INSERT INTO chat_agent_session (id, user_id, runtime_id) VALUES ('sess-cfg-7', 7, 1)")
        c.execute("INSERT INTO chat_agent_session (id, user_id, runtime_id) VALUES ('c2', 7, 999)")
        conn.commit()
        db_setup._migration_868_add_session_uid_columns(conn)
        return conn

    def test_backfill_and_orphan_report(self, tmp_path):
        conn = self._migrated(tmp_path)
        try:
            report = db_setup._migration_868_backfill_session_uids(conn)
            assert conn.execute(
                "SELECT engine_uid FROM devops_agent_session WHERE id='s1'").fetchone()[0] == UID_HERMES
            assert conn.execute(
                "SELECT engine_uid FROM devops_agent_session WHERE id='s2'").fetchone()[0] is None
            assert conn.execute(
                "SELECT engine_uid FROM devops_agent_session WHERE id='s3'").fetchone()[0] is None
            assert conn.execute(
                "SELECT engine_uid FROM chat_agent_session WHERE id='sess-cfg-7'").fetchone()[0] == UID_OPENCODE
            assert conn.execute(
                "SELECT engine_uid FROM chat_agent_session WHERE id='c2'").fetchone()[0] is None
            # 孤儿入报告（不静默丢弃）
            assert any(d["session_id"] == "s2" for d in report["orphan_devops_sessions"])
            assert any(d["session_id"] == "c2" for d in report["orphan_chat_sessions"])
            assert report["devops_sessions_backfilled"] == 1
            assert report["chat_sessions_backfilled"] == 1
        finally:
            conn.close()

    def test_backfill_idempotent(self, tmp_path):
        conn = self._migrated(tmp_path)
        try:
            db_setup._migration_868_backfill_session_uids(conn)
            u = conn.execute("SELECT engine_uid FROM devops_agent_session WHERE id='s1'").fetchone()[0]
            report2 = db_setup._migration_868_backfill_session_uids(conn)
            u2 = conn.execute("SELECT engine_uid FROM devops_agent_session WHERE id='s1'").fetchone()[0]
            assert u == u2, "二次回填会话 engine_uid 漂移"
            assert report2["devops_sessions_backfilled"] == 0
            assert report2["chat_sessions_backfilled"] == 0
        finally:
            conn.close()

    def test_backfill_unmatched_update_not_counted(self, tmp_path):
        """E2E 复核问题1：NULL 主键行 UPDATE rowcount=0 不命中时不得计数 backfilled（防虚报）。"""
        conn = self._migrated(tmp_path)
        try:
            c = conn.cursor()
            # SQLite TEXT PRIMARY KEY 宽松允许 NULL id：UPDATE ... WHERE id=NULL 不命中
            c.execute("INSERT INTO chat_agent_session (id, user_id, runtime_id) VALUES (NULL, 9, 1)")
            conn.commit()
            report = db_setup._migration_868_backfill_session_uids(conn)
            # 真实主键行照常计数
            assert report["chat_sessions_backfilled"] == 1
            # 未命中 UPDATE 不虚报 backfilled，且入异常报告（不静默丢弃）
            assert any(d.get("session_id") is None
                       for d in report["update_unmatched_sessions"]), \
                "UPDATE 未命中（rowcount!=1）应入 update_unmatched_sessions 异常报告"
        finally:
            conn.close()

    def test_backfill_report_persisted_to_disk(self, tmp_path):
        """E2E 复核问题2：孤儿对账报告落盘 JSON 可复核（对齐 #646 迁移报告机制）。"""
        import json
        import os
        conn = self._migrated(tmp_path)
        try:
            report = db_setup._migration_868_backfill_session_uids(conn)
            path = report.get("report_file", "")
            assert path and os.path.isfile(path), "迁移报告应落盘（report_file 返回且文件存在）"
            with open(path, encoding="utf-8") as f:
                persisted = json.load(f)
            assert persisted["issue"] == "868"
            assert any(d["session_id"] == "s2"
                       for d in persisted["report"]["orphan_devops_sessions"])
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# S2 · session_store 双写双读（uid 优先 / id 兜底 / 旧库缺列降级）
# ---------------------------------------------------------------------------

class TestSessionStoreDualWrite:
    def _patch(self, monkeypatch, conn):
        sm = importlib.import_module("src.kernel.session_manager")
        ss = importlib.import_module("src.api.session_store")
        udb = importlib.import_module("src.domains.mgmt.user.db")
        lock = threading.RLock()
        for mod in (sm, ss, udb):
            monkeypatch.setattr(mod, "db", conn, raising=True)
            monkeypatch.setattr(mod, "db_lock", lock, raising=True)
        return ss

    def test_get_or_create_writes_and_reads_engine_uid(self, tmp_path, monkeypatch):
        conn = _legacy_db(tmp_path, session_uid_col=True)
        _seed_engines(conn)
        ss = self._patch(monkeypatch, conn)
        try:
            sess = ss.get_or_create_agent_session("gitcode:o/r", "coding", runtime_id=3)
            assert sess["new"] is True
            assert sess["engine_uid"] == UID_HERMES
            row = conn.execute(
                "SELECT engine_uid FROM devops_agent_session WHERE id=?", (sess["id"],)).fetchone()
            assert row[0] == UID_HERMES
            # 二次读取（已有行）：返回带 engine_uid
            again = ss.get_or_create_agent_session("gitcode:o/r", "coding", runtime_id=3)
            assert again["new"] is False
            assert again["engine_uid"] == UID_HERMES
        finally:
            conn.close()

    def test_upsert_rebind_mirrors_engine_uid(self, tmp_path, monkeypatch):
        conn = _legacy_db(tmp_path, session_uid_col=True)
        _seed_engines(conn)
        ss = self._patch(monkeypatch, conn)
        try:
            sid = ss.upsert_agent_session(repo_key="gitcode:o/r", agent_type="coding",
                                          runtime_id=3, skill="coding")
            uid = conn.execute(
                "SELECT engine_uid FROM devops_agent_session WHERE id=?", (sid,)).fetchone()[0]
            assert uid == UID_HERMES
            # 改绑到引擎 1：uid 镜像跟随
            ss.upsert_agent_session(repo_key="gitcode:o/r", agent_type="coding",
                                    runtime_id=1, skill="coding")
            uid2 = conn.execute(
                "SELECT engine_uid FROM devops_agent_session WHERE id=?", (sid,)).fetchone()[0]
            assert uid2 == UID_OPENCODE
        finally:
            conn.close()

    def test_legacy_db_missing_column_degrades(self, tmp_path, monkeypatch):
        """旧库缺 engine_uid 列：读写降级回 legacy SQL，不崩溃。"""
        conn = _legacy_db(tmp_path)   # 无 engine_uid 列
        _seed_engines(conn)
        ss = self._patch(monkeypatch, conn)
        try:
            sess = ss.get_or_create_agent_session("gitcode:o/r", "coding", runtime_id=3)
            assert sess["new"] is True
            assert sess.get("engine_uid", "") == ""
            again = ss.get_or_create_agent_session("gitcode:o/r", "coding", runtime_id=3)
            assert again["new"] is False
            sid = ss.upsert_agent_session(repo_key="gitcode:o/r2", agent_type="coding",
                                          runtime_id=3, skill="coding")
            assert sid
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# S3 · chat 配置行双写双读（uid 优先 / 失效回退 id / 缺列降级）
# ---------------------------------------------------------------------------

class TestChatConfigDualWrite:
    def _patch(self, monkeypatch, conn):
        sm = importlib.import_module("src.kernel.session_manager")
        udb = importlib.import_module("src.domains.mgmt.user.db")
        lock = threading.RLock()
        for mod in (sm, udb):
            monkeypatch.setattr(mod, "db", conn, raising=True)
            monkeypatch.setattr(mod, "db_lock", lock, raising=True)
        return sm

    def test_write_chat_config_row_dual_writes_uid(self, tmp_path, monkeypatch):
        conn = _legacy_db(tmp_path, session_uid_col=True)
        _seed_engines(conn)
        sm = self._patch(monkeypatch, conn)
        try:
            sm._write_chat_config_row(7, runtime_id=3)
            row = conn.execute(
                "SELECT runtime_id, engine_uid FROM chat_agent_session WHERE id='sess-cfg-7'").fetchone()
            assert row[0] == 3 and row[1] == UID_HERMES
        finally:
            conn.close()

    def test_get_chat_config_uid_preferred_over_id(self, tmp_path, monkeypatch):
        """uid 有效 → 按 uid 解析引擎（优先于 runtime_id 指向的引擎）。"""
        conn = _legacy_db(tmp_path, session_uid_col=True)
        _seed_engines(conn)
        sm = self._patch(monkeypatch, conn)
        c = conn.cursor()
        # runtime_id 指向 opencode(1)，engine_uid 指向 hermes(3)：uid 优先 → hermes
        c.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid, "
                  "skill, skill_id, im_app_id, im_app_secret, im_provider, created_at, last_active_at) "
                  "VALUES ('sess-cfg-8', 8, '', 1, ?, 'chat', 0, '', '', '', '', '')", (UID_HERMES,))
        conn.commit()
        try:
            cfg = sm.get_chat_config(8)
            assert cfg["engine_uid"] == UID_HERMES
            assert (cfg["backend"] or "").lower() == "hermes"
        finally:
            conn.close()

    def test_get_chat_config_stale_uid_falls_back_to_id(self, tmp_path, monkeypatch):
        conn = _legacy_db(tmp_path, session_uid_col=True)
        _seed_engines(conn)
        sm = self._patch(monkeypatch, conn)
        c = conn.cursor()
        c.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, runtime_id, engine_uid, "
                  "skill, skill_id, im_app_id, im_app_secret, im_provider, created_at, last_active_at) "
                  "VALUES ('sess-cfg-9', 9, '', 3, ?, 'chat', 0, '', '', '', '', '')",
                  ("11111111-2222-3333-4444-555555555555",))
        conn.commit()
        try:
            cfg = sm.get_chat_config(9)
            assert cfg["backend"].lower() == "hermes"
        finally:
            conn.close()

    def test_legacy_chat_table_missing_column_degrades(self, tmp_path, monkeypatch):
        conn = _legacy_db(tmp_path)   # 无 engine_uid 列
        _seed_engines(conn)
        sm = self._patch(monkeypatch, conn)
        try:
            sm._write_chat_config_row(10, runtime_id=3)
            row = conn.execute(
                "SELECT runtime_id FROM chat_agent_session WHERE id='sess-cfg-10'").fetchone()
            assert row and row[0] == 3
            cfg = sm.get_chat_config(10)
            assert cfg["backend"].lower() == "hermes"
            assert cfg.get("engine_uid", "") == ""
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# S4 · 对账：_rebind_runtime_references 会话 uid 轨道
# ---------------------------------------------------------------------------

class TestRebindSessionUidTrack:
    def _seed_sessions(self, conn):
        c = conn.cursor()
        c.execute("INSERT INTO agent_runtimes (id, name, engine_uid) VALUES (1, 'opencode', ?)", (UID_OPENCODE,))
        c.execute("INSERT INTO agent_runtimes (id, name, engine_uid) VALUES (3, 'hermes', ?)", (UID_HERMES,))
        c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, engine_uid) "
                  "VALUES ('s1', 'gitcode:o/r', 'coding', 1, ?)", (UID_OPENCODE,))
        c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id) "
                  "VALUES ('s0', 'gitcode:o/r', 'sre', 0)")   # 未绑定但残留 stale uid
        c.execute("UPDATE devops_agent_session SET engine_uid=? WHERE id='s0'",
                  ("99999999-8888-7777-6666-555555555555",))
        c.execute("INSERT INTO chat_agent_session (id, user_id, runtime_id, engine_uid) "
                  "VALUES ('c1', 7, 1, ?)", (UID_OPENCODE,))
        conn.commit()

    def test_rebind_session_uid_mirror_sql_cross_dialect(self):
        # QA 复审 Required（MySQL 1064）：会话 uid 镜像 UPDATE 原用 SQLite 专用
        # `engine_uid IS NOT (SELECT ...)` 方言语法，MySQL 8.0 无该运算符报 1064，
        # 且 mysql_compat.translate_sql 无对应翻译规则 → 异常被 except 吞掉，
        # uid 轨道静默失同步（runtime_id 已改绑成功 → 双读 uid 优先路由错引擎）。
        # 修复契约：镜像 SQL 必须跨方言（不含 IS NOT 运算），并保留 NULL-safe 语义。
        udb = importlib.import_module("src.domains.mgmt.user.db")
        mc = importlib.import_module("src.mysql_compat")
        for tbl in ("devops_agent_session", "chat_agent_session"):
            sql = udb._session_uid_mirror_sql(tbl)
            assert "IS NOT" not in sql.upper()
            # NULL-safe 对账语义保留：NULL/''/stale 均命中（NULLIF+COALESCE 归一比较）
            assert "COALESCE" in sql.upper() and "NULLIF" in sql.upper()
            translated = mc.translate_sql(sql)
            assert "IS NOT" not in translated.upper()   # MySQL 1064 防线
            assert "%s" not in translated               # 无待翻译占位符残留

    def test_rebind_backfills_null_uid_and_skips_consistent_rows(self, tmp_path, monkeypatch):
        # 「去重先回填 uid 置 NULL 再改绑」顺序下 NULL 会话 uid 被镜像补齐为保留行 uid；
        # 已一致行不重写不计数（sessions_uid_rebound = 实际矫正行数）。
        conn = _legacy_db(tmp_path, session_uid_col=True)
        c = conn.cursor()
        c.execute("INSERT INTO agent_runtimes (id, name, engine_uid) VALUES (1, 'opencode', ?)", (UID_OPENCODE,))
        c.execute("INSERT INTO agent_runtimes (id, name, engine_uid) VALUES (3, 'hermes', ?)", (UID_HERMES,))
        # s-null：runtime 指向 1（待删行）、uid 为 NULL（回填置 NULL 顺序）→ 需补齐为保留行 uid
        c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, engine_uid) "
                  "VALUES ('s-null', 'gitcode:o/r', 'coding', 1, NULL)")
        # s-ok：runtime/uid 均已指向保留行 → 不重写、不计数
        c.execute("INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id, engine_uid) "
                  "VALUES ('s-ok', 'gitcode:o/r', 'sre', 3, ?)", (UID_HERMES,))
        conn.commit()
        udb = importlib.import_module("src.domains.mgmt.user.db")
        monkeypatch.setattr(udb, "db", conn, raising=True)
        monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=True)
        try:
            report = udb._rebind_runtime_references(conn.cursor(), [1], 3, reason="test-dedup")
            assert conn.execute(
                "SELECT runtime_id, engine_uid FROM devops_agent_session WHERE id='s-null'").fetchone() \
                == (3, UID_HERMES)
            assert conn.execute(
                "SELECT runtime_id, engine_uid FROM devops_agent_session WHERE id='s-ok'").fetchone() \
                == (3, UID_HERMES)
            assert report["sessions_uid_rebound"] == 1
        finally:
            conn.close()

    def test_rebind_updates_session_uids(self, tmp_path, monkeypatch):
        conn = _legacy_db(tmp_path, session_uid_col=True)
        self._seed_sessions(conn)
        udb = importlib.import_module("src.domains.mgmt.user.db")
        monkeypatch.setattr(udb, "db", conn, raising=True)
        monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=True)
        try:
            report = udb._rebind_runtime_references(conn.cursor(), [1], 3, reason="test-dedup")
            assert conn.execute(
                "SELECT runtime_id, engine_uid FROM devops_agent_session WHERE id='s1'").fetchone() == (3, UID_HERMES)
            assert conn.execute(
                "SELECT runtime_id, engine_uid FROM chat_agent_session WHERE id='c1'").fetchone() == (3, UID_HERMES)
            assert report["sessions_rebound"] == 1
            assert report["sessions_uid_rebound"] == 1
            assert report["chat_sessions_uid_rebound"] == 1
        finally:
            conn.close()

    def test_explicit_cleanup_nulls_orphan_uids(self, tmp_path, monkeypatch):
        conn = _legacy_db(tmp_path, session_uid_col=True)
        self._seed_sessions(conn)
        udb = importlib.import_module("src.domains.mgmt.user.db")
        monkeypatch.setattr(udb, "db", conn, raising=True)
        monkeypatch.setattr(udb, "db_lock", threading.RLock(), raising=True)
        try:
            es = importlib.import_module("src.domains.runtime.engine.engine_select")
            monkeypatch.setattr(es, "cleanup_scene_engine_bindings", lambda rids: 0)
            report = udb._rebind_runtime_references(conn.cursor(), [1], 0, reason="test-delete")
            # runtime_id 置 0 + uid 置 NULL
            assert conn.execute(
                "SELECT runtime_id, engine_uid FROM devops_agent_session WHERE id='s1'").fetchone() == (0, None)
            # 孤儿 stale uid（runtime_id=0 但残留 uid）也置 NULL 入报告
            assert conn.execute(
                "SELECT engine_uid FROM devops_agent_session WHERE id='s0'").fetchone()[0] is None
            assert conn.execute(
                "SELECT runtime_id, engine_uid FROM chat_agent_session WHERE id='c1'").fetchone() == (0, None)
            assert report["sessions_reset"] == 1
            assert report["sessions_uid_nulled"] >= 2
            assert report["chat_sessions_uid_nulled"] >= 1
        finally:
            conn.close()
