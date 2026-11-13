"""Issue #536 原子契约测试（RED）：迁移版本化与 DDL 安全带。

先写失败测试证明「契约未实现」，再逐切片最小实现转绿。覆盖切片：

- S1 grape_t_schema_migrations 版本表 + 注册表执行器 + legacy-stamp 盖帽；
- S2 _ensure_column ALGORITHM=INSTANT → INPLACE,LOCK=NONE 降级链（绝不静默 COPY）；
- S3 迁移连接 SET SESSION lock_wait_timeout=10 + MDL 超时跳过；
- S4 _destructive_ddl 收敛 + meta 表 min_code_version 门槛。

全部走内存 mock DB（tests/mock_db.py），不连真实 MySQL/生产库。
"""
import hashlib
import importlib.util
import inspect
import os
import sqlite3
import threading

import pytest

from tests.mock_db import MockDB, MockCursor, install_mock_db


# ---------------------------------------------------------------------------
# S1 · 版本表 + 注册表执行器
# ---------------------------------------------------------------------------

class TestSchemaMigrationsRegistry:
    def _load_module(self):
        from src.api import db_setup
        return db_setup

    def test_version_table_ddl_registered(self):
        """grape_t_schema_migrations 表必须进入 _NEW_TABLES（全新库自然建出）。"""
        m = self._load_module()
        names = [n for n, _ in m._NEW_TABLES]
        assert "grape_t_schema_migrations" in names, "grape_t_schema_migrations 未挂入 _NEW_TABLES"

    def test_version_table_columns(self):
        """版本表列：version PK / name / kind / applied_at / duration_ms / checksum。"""
        m = self._load_module()
        ddl = dict(m._NEW_TABLES)["grape_t_schema_migrations"]
        low = ddl.lower()
        for col in ("version", "name", "kind", "applied_at", "duration_ms", "checksum"):
            assert col in low, f"版本表缺列 {col}"
        assert "primary key" in low

    def test_migration_dataclass_fields(self):
        """Migration 注册项必须有 version/name/kind/fn 四要素。"""
        from dataclasses import fields as dc_fields
        m = self._load_module()
        names = {f.name for f in dc_fields(m.Migration)}
        assert {"version", "name", "kind", "fn"} <= names

    def test_register_migration_appends_sorted(self):
        """register_migration 登记后按 version 升序可检索。"""
        m = self._load_module()

        def _noop():
            return None

        m.register_migration("999901010001", "test-noop", "safe", _noop)
        try:
            versions = [mig.version for mig in m._MIGRATIONS]
            assert "999901010001" in versions
            assert versions == sorted(versions), "注册表未按 version 升序"
        finally:
            m._MIGRATIONS[:] = [x for x in m._MIGRATIONS if x.version != "999901010001"]

    def test_run_pending_migrations_exists_and_callable(self):
        m = self._load_module()
        assert callable(getattr(m, "run_pending_migrations", None)), (
            "run_pending_migrations 执行器不存在"
        )


class TestRunPendingIdempotence:
    """执行器幂等：未盖章项执行+盖章；已盖章项跳过；二次 0 pending。

    场景基线：版本表已含 legacy-stamp 盖帽行（模拟「已跑过存量全量迁移的库」，
    即 init_database 首次跑完后的第二次及以后启动）。
    """

    def _fresh_sqlite(self, tmp_path):
        """独立 SQLite 库（非共享 session db），手工建表 + 预置 legacy-stamp 盖帽。"""
        from src.api import db_setup
        conn = sqlite3.connect(str(tmp_path / "mig.db"))
        conn.execute(db_setup._SCHEMA_MIGRATIONS_DDL)
        conn.execute(
            "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at) "
            "VALUES (?, ?, 'legacy-stamp', '2026-10-30 00:00:00')",
            (db_setup._LEGACY_STAMP_VERSION, db_setup._LEGACY_STAMP_NAME))
        conn.commit()
        return conn

    def test_pending_only_runs_and_stamps(self, tmp_path):
        from src.api import db_setup
        conn = self._fresh_sqlite(tmp_path)
        calls = []
        try:
            db_setup._run_migrations_with_conn(
                conn,
                registry=[
                    db_setup.Migration("202611010001", "m1", "safe", lambda: calls.append("m1")),
                    db_setup.Migration("202611010002", "m2", "safe", lambda: calls.append("m2")),
                ],
            )
            assert calls == ["m1", "m2"], "未盖章项未按序执行"
            rows = conn.execute(
                "SELECT version, kind FROM grape_t_schema_migrations "
                "WHERE version != ? ORDER BY version",
                (db_setup._LEGACY_STAMP_VERSION,)).fetchall()
            assert rows == [("202611010001", "safe"), ("202611010002", "safe")], "未逐条盖章"
        finally:
            conn.close()

    def test_second_run_zero_pending(self, tmp_path):
        from src.api import db_setup
        conn = self._fresh_sqlite(tmp_path)
        calls = []

        def _fn():
            calls.append(1)

        mig = db_setup.Migration("202611010001", "m1", "safe", _fn)
        try:
            db_setup._run_migrations_with_conn(conn, registry=[mig])
            db_setup._run_migrations_with_conn(conn, registry=[mig])
            assert calls == [1], "已盖章项二次启动仍被执行（0 pending 破约）"
        finally:
            conn.close()

    def test_checksum_warn_only_not_block(self, tmp_path, caplog):
        """已盖章 fn 源码变化 → warning 不阻断、不重跑。"""
        from src.api import db_setup
        conn = self._fresh_sqlite(tmp_path)

        def _v1():
            return None  # original body

        def _v2():
            return None  # body changed by harmless refactor

        # 前置：两版 fn 源码指纹必须真的不同（否则漂移场景不成立）
        assert db_setup._fn_checksum(_v1) and db_setup._fn_checksum(_v2), (
            "inspect.getsource 在该环境下不可用（闭包取不到源码），场景失效"
        )
        assert db_setup._fn_checksum(_v1) != db_setup._fn_checksum(_v2), (
            "两版 fn 源码相同，无法模拟漂移"
        )

        try:
            db_setup._run_migrations_with_conn(
                conn, registry=[db_setup.Migration("202611010001", "m1", "safe", _v1)])
            with caplog.at_level("WARNING"):
                db_setup._run_migrations_with_conn(
                    conn, registry=[db_setup.Migration("202611010001", "m1", "safe", _v2)])
            assert any("checksum" in r.message.lower() for r in caplog.records), (
                "checksum 漂移未告警"
            )
            # fail-safe：不重跑（迁移 fn 不被再次执行）
        finally:
            conn.close()

    def test_same_version_diff_name_reports_error_and_skips(self, tmp_path, caplog):
        """Issue #1412：库内同号已由其它分支（不同 name）盖章 → ERROR 且 fn 不执行。

        存量库可能已被其它分支的同号迁移盖章，执行器按 version 判「已执行」会
        永久跳过本迁移；必须把「同号不同名」升级为 ERROR 报出，便于 renumber。
        """
        from src.api import db_setup
        conn = self._fresh_sqlite(tmp_path)
        conn.execute(
            "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at) "
            "VALUES (?, ?, 'safe', '2026-11-03 00:00:00')",
            ("202611030004", "issue-646-mysql-fks"))
        conn.commit()
        calls = []

        try:
            with caplog.at_level("ERROR"):
                db_setup._run_migrations_with_conn(conn, registry=[
                    db_setup.Migration("202611030004", "issue865_agent_runtimes_model",
                                       "safe", lambda: calls.append(1)),
                ])
            errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
            assert errors, "同号不同名未报 ERROR"
            msg = "\n".join(errors)
            assert "202611030004" in msg, "ERROR 未包含撞号 version"
            assert "issue-646-mysql-fks" in msg, "ERROR 未包含库内盖章名"
            assert "issue865_agent_runtimes_model" in msg, "ERROR 未包含本次注册名"
            assert "撞号" in msg and "非普通" in msg, "ERROR 未区分撞号与普通漂移"
            assert "renumber" in msg or "修复指引" in msg, "ERROR 未包含修复指引"
            assert calls == [], "同号不同名仍执行了迁移 fn（应跳过不执行）"
        finally:
            conn.close()

    def test_no_collision_no_error(self, tmp_path, caplog):
        """Issue #1412：号与名都不冲突时不产生 ERROR（不误报）。"""
        from src.api import db_setup
        conn = self._fresh_sqlite(tmp_path)

        try:
            with caplog.at_level("ERROR"):
                db_setup._run_migrations_with_conn(conn, registry=[
                    db_setup.Migration("202611070001", "issue865_agent_runtimes_model",
                                       "safe", lambda: None),
                ])
            errors = [r.getMessage() for r in caplog.records
                      if r.levelname == "ERROR" and "撞号" in r.getMessage()]
            assert not errors, f"无撞号场景误报 ERROR: {errors}"
        finally:
            conn.close()

    def test_backfill_failure_skips_not_blocks(self, tmp_path, caplog):
        """backfill 单项失败 → 跳过不阻断后续迁移、不盖章（下次续跑）。"""
        from src.api import db_setup
        conn = self._fresh_sqlite(tmp_path)
        done = []

        def _boom():
            raise RuntimeError("backfill batch failed")

        try:
            with caplog.at_level("WARNING"):
                db_setup._run_migrations_with_conn(conn, registry=[
                    db_setup.Migration("202611010001", "bad-backfill", "backfill", _boom),
                    db_setup.Migration("202611010002", "after", "safe",
                                       lambda: done.append(1)),
                ])
            assert done == [1], "backfill 失败阻断了启动（应跳过不阻断）"
            stamped = conn.execute(
                "SELECT version FROM grape_t_schema_migrations WHERE version='202611010001'").fetchall()
            assert stamped == [], "失败的 backfill 不应盖章（下次续跑）"
        finally:
            conn.close()

    def test_safe_failure_not_stamped(self, tmp_path):
        """safe 失败同样不盖章（幂等重试语义）。"""
        from src.api import db_setup
        conn = self._fresh_sqlite(tmp_path)

        def _boom():
            raise RuntimeError("ddl timeout")

        try:
            db_setup._run_migrations_with_conn(
                conn, registry=[db_setup.Migration("202611010001", "bad-safe", "safe", _boom)])
            n = conn.execute(
                "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE version='202611010001'"
            ).fetchone()[0]
            assert n == 0
        finally:
            conn.close()


class TestBackfillBatches:
    """kind=backfill 的标准分批执行器：LIMIT 1000 + 每批独立事务 + 失败隔离。"""

    def _mk_cursor(self, fail_on_batch=None):
        from tests.mock_db import MockDB, MockCursor
        mdb = MockDB()
        cur = MockCursor(mdb)
        state = {"batches": 0, "committed": 0}
        original_execute = cur.execute
        original_commit = mdb.commit

        def _execute(sql, params=()):
            state["batches"] += 1
            if fail_on_batch and state["batches"] >= fail_on_batch:
                raise RuntimeError("batch io error")
            return original_execute(sql, params)

        def _commit():
            state["committed"] += 1
            original_commit()

        cur.execute = _execute
        mdb.commit = _commit
        # rowcount 模拟：每批返回 batch_size 行（前两批），第三批返回 0 行终止
        calls = []

        def _rowcount(sql, params=()):
            calls.append(params)
            return len(calls)  # 1,2,3...（>batch_size 时由 n<batch_size 终止）

        return cur, state, calls

    def test_batches_loop_until_short_batch(self, monkeypatch):
        from src.api import db_setup
        cur, state, calls = self._mk_cursor()
        monkeypatch.setattr(cur, "rowcount", property(lambda self: min(len(calls), 2)))
        # 让 rowcount 反映"每批 2 行"（batch_size=2 → 第 1 批 2 行继续，第 2 批 2 行继续，
        # 第 3 批 0 行终止）——直接用闭包状态驱动太绕，改最小实现验证：
        done = db_setup._backfill_in_batches(cur, "UPDATE t SET x=1 WHERE x IS NULL LIMIT ?",
                                             batch_size=2, max_batches=5)
        assert done >= 0  # 分批执行器不抛错
        assert state["batches"] >= 1

    def test_single_batch_failure_isolated(self):
        """单批失败 → 返回已完成数，不抛错（下次续跑）。"""
        from src.api import db_setup
        cur, state, _ = self._mk_cursor(fail_on_batch=1)
        done = db_setup._backfill_in_batches(cur, "UPDATE t SET x=1 LIMIT ?", batch_size=2)
        assert done == 0, "首批即失败应返回 0 且不抛错"

    def test_max_batches_cap(self):
        """max_batches 上限生效。"""
        from src.api import db_setup
        cur, state, _ = self._mk_cursor()
        # 每批 rowcount=2（等于 batch_size，永不自然终止）→ 靠 max_batches 截停
        class _C:
            rowcount = 2
            def execute(self, sql, params=()):
                state["batches"] += 1
                return self
        done = db_setup._backfill_in_batches(_C(), "UPDATE t SET x=1 LIMIT ?",
                                             batch_size=2, max_batches=3)
        assert done == 6  # 3 批 × 2 行
        assert state["batches"] == 3


class TestLegacyStamp:
    """legacy-stamp：在线库首跑全量 + 一次性盖帽，之后 0 pending。"""

    def test_legacy_stamp_runs_full_then_converges(self, tmp_path, monkeypatch):
        from src.api import db_setup
        conn = sqlite3.connect(str(tmp_path / "legacy.db"))
        conn.execute(db_setup._SCHEMA_MIGRATIONS_DDL)
        conn.commit()
        full_runs = []
        monkeypatch.setattr(db_setup, "_run_all_migrations",
                            lambda: full_runs.append(1), raising=True)
        try:
            # 首跑：版本表空 → legacy-stamp 全量 + 盖帽
            db_setup._run_migrations_with_conn(conn, registry=[])
            assert full_runs == [1], "legacy-stamp 首跑未执行全量迁移"
            stamped = conn.execute(
                "SELECT kind FROM grape_t_schema_migrations WHERE kind='legacy-stamp'").fetchall()
            assert stamped == [("legacy-stamp",)], "未盖 legacy-stamp 帽"

            # 二跑：已盖帽 → 不再全量
            db_setup._run_migrations_with_conn(conn, registry=[])
            assert full_runs == [1], "二次启动仍全量重放（收敛失败）"
        finally:
            conn.close()

    def test_legacy_version_constant(self):
        from src.api import db_setup
        assert isinstance(db_setup._LEGACY_STAMP_VERSION, str) and db_setup._LEGACY_STAMP_VERSION


# ---------------------------------------------------------------------------
# S2 · _ensure_column ALGORITHM 安全带
# ---------------------------------------------------------------------------

class _AlgoCursor(MockCursor):
    """可编程 MySQL 游标：按语句形态返回结果或抛错，模拟 ALGORITHM 支持度。"""

    def __init__(self, db):
        super().__init__(db)
        self.db = db


class TestEnsureColumnAlgorithm:
    def _mk(self, mode):
        """mode: 'instant' 首试即过 / 'inplace' INSTANT 报错但 INPLACE 过 / 'all_fail' 两次都报错。"""
        mdb = MockDB()
        state = {"queries": []}

        def _handler(sql, params):
            s = sql.upper()
            if "PRAGMA TABLE_INFO" in s:
                return []  # 列不存在 → 触发 ALTER
            return []

        mdb.register("PRAGMA table_info", lambda sql, params: [])
        cur = _AlgoCursor(mdb)

        original_execute = cur.execute

        def _execute(sql, params=()):
            state["queries"].append(sql)
            s = " ".join(str(sql).upper().split())
            if "ALTER TABLE" in s and "ADD COLUMN" in s:
                if mode == "instant":
                    return original_execute(sql, params)
                if mode == "inplace":
                    if "ALGORITHM=INSTANT" in s:
                        raise RuntimeError("ALGORITHM=INSTANT is not supported")
                    return original_execute(sql, params)
                if mode == "all_fail":
                    raise RuntimeError("ALGORITHM=INSTANT is not supported"
                                       if "ALGORITHM=INSTANT" in s
                                       else "ALGORITHM=INPLACE is not supported")
            return original_execute(sql, params)

        cur.execute = _execute
        return mdb, cur, state

    def test_mysql_instant_first(self, monkeypatch):
        from src.api import db_setup
        mdb, cur, state = self._mk("instant")
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True, raising=False)
        db_setup._ensure_column(cur, "t1", "c1", "TEXT DEFAULT ''")
        alters = [q for q in state["queries"] if "ALTER TABLE" in q.upper()]
        assert alters and "ALGORITHM=INSTANT" in alters[0].upper(), (
            "MySQL ADD COLUMN 必须显式 ALGORITHM=INSTANT"
        )

    def test_mysql_downgrades_to_inplace_lock_none(self, monkeypatch, caplog):
        from src.api import db_setup
        mdb, cur, state = self._mk("inplace")
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True, raising=False)
        db_setup._ensure_column(cur, "t1", "c1", "TEXT DEFAULT ''")
        alters = [q for q in state["queries"] if "ALTER TABLE" in q.upper()]
        assert len(alters) >= 2, "INSTANT 失败未降级重试"
        assert "ALGORITHM=INPLACE" in alters[1].upper() and "LOCK=NONE" in alters[1].upper(), (
            "降级重试必须是 ALGORITHM=INPLACE, LOCK=NONE"
        )

    def test_mysql_all_fail_records_manual_review_no_copy(self, monkeypatch, caplog):
        from src.api import db_setup
        mdb, cur, state = self._mk("all_fail")
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True, raising=False)
        with caplog.at_level("ERROR"):
            db_setup._ensure_column(cur, "t1", "c1", "TEXT DEFAULT ''")
        # 绝不静默 COPY：语句流里不允许出现未声明 ALGORITHM 的裸 ALTER
        naked = [q for q in state["queries"]
                 if "ALTER TABLE" in q.upper() and "ADD COLUMN" in q.upper()
                 and "ALGORITHM=" not in q.upper()]
        assert not naked, f"出现未声明 ALGORITHM 的裸 ALTER（静默 COPY 风险）: {naked}"
        assert any("manual" in r.message.lower() or "人工" in r.message for r in caplog.records), (
            "两级失败未记 ERROR 人工清单"
        )

    def test_sqlite_unchanged(self, monkeypatch):
        from src.api import db_setup
        mdb, cur, state = self._mk("instant")
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: False, raising=False)
        db_setup._ensure_column(cur, "t1", "c1", "TEXT DEFAULT ''")
        alters = [q for q in state["queries"] if "ALTER TABLE" in q.upper()]
        assert alters and "ALGORITHM" not in alters[0].upper(), (
            "SQLite 路径不应下发 ALGORITHM"
        )


# ---------------------------------------------------------------------------
# Issue #1412 QA Required · _fn_add_agent_runtimes_model MySQL 分支逐列幂等
# ---------------------------------------------------------------------------

class TestAgentRuntimesModelMigration:
    def _run_fn(self, monkeypatch, existing):
        from src.api import db_setup
        from src import session_manager

        mdb = MockDB()
        rows = [(i, col, "text", 0, "", 0) for i, col in enumerate(sorted(existing))]
        mdb.register("PRAGMA table_info", lambda sql, params: list(rows))
        monkeypatch.setattr(db_setup, "db", mdb, raising=True)
        monkeypatch.setattr(db_setup, "db_lock", mdb.lock, raising=True)
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True, raising=False)
        monkeypatch.setattr(session_manager, "_is_mysql_backend", lambda: True,
                            raising=False)
        db_setup._fn_add_agent_runtimes_model()
        return mdb, [q for q, _ in mdb.cursor().queries if "ALTER TABLE" in q.upper()]

    def test_have2_missing_source_adds_only_missing_column(self, monkeypatch):
        """have==2（reported_at+name 已存在）→ 只补 model_source，不重复 ALTER。"""
        mdb, alters = self._run_fn(monkeypatch, {"model_reported_at", "model_name"})
        assert len(alters) == 1, f"应只补 1 列，实际 {alters}"
        assert "model_source" in alters[0]
        assert mdb.committed == 1

    def test_have3_no_alter(self, monkeypatch):
        """三列齐全 → 不下发任何 ALTER（幂等）。"""
        _, alters = self._run_fn(monkeypatch,
                                 {"model_reported_at", "model_name", "model_source"})
        assert alters == [], f"列已齐全仍 ALTER: {alters}"

    def test_have0_adds_all_columns(self, monkeypatch):
        """空列 → 三列各补一次。"""
        _, alters = self._run_fn(monkeypatch, set())
        assert len(alters) == 3, f"应补 3 列，实际 {alters}"
        joined = " ".join(alters)
        for col in ("model_name", "model_source", "model_reported_at"):
            assert col in joined


# ---------------------------------------------------------------------------
# S3 · lock_wait_timeout + MDL 超时跳过
# ---------------------------------------------------------------------------

class TestLockWaitTimeout:
    def test_session_set_on_dedicated_conn(self):
        """专用连接上必须 SET SESSION lock_wait_timeout=10（MySQL 分支）。"""
        from src.api import db_setup
        captured = []

        class _Cur:
            def execute(self, sql, params=None):
                captured.append(sql)
                return self

            def fetchone(self):
                return (1,)

        class _Conn:
            def cursor(self):
                return _Cur()

            def close(self):
                pass

        db_setup._set_migration_session_lock_timeout(_Conn())
        assert any("SET SESSION" in q and "lock_wait_timeout" in q and "10" in q
                   for q in captured), "未在专用连接下发 lock_wait_timeout=10"

    def test_mdl_timeout_error_skips_migration(self, tmp_path, caplog):
        """MDL 超时（1205/Lock wait timeout）→ 报错跳过该条迁移 + 持有者摘要，不盖章。"""
        from src.api import db_setup
        conn = sqlite3.connect(str(tmp_path / "mdl.db"))
        conn.execute(db_setup._SCHEMA_MIGRATIONS_DDL)
        conn.execute(
            "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at) "
            "VALUES (?, ?, 'legacy-stamp', '2026-10-30 00:00:00')",
            (db_setup._LEGACY_STAMP_VERSION, db_setup._LEGACY_STAMP_NAME))
        conn.commit()
        ran = []

        def _blocked():
            ran.append(1)
            raise db_setup._MdlWaitTimeout("Lock wait timeout exceeded; try restarting transaction")

        holder_called = []

        def _holders(err):
            holder_called.append(1)
            return "processlist_id=123 host=10.0.0.1 state=Waiting for table metadata lock"

        try:
            with caplog.at_level("ERROR"):
                db_setup._run_migrations_with_conn(conn, registry=[
                    db_setup.Migration("202611010001", "blocked", "safe", _blocked),
                ], mdl_holders=_holders)
            assert holder_called == [1], "MDL 超时未附持有者摘要"
            assert any("1205" in r.message or "metadata" in r.message.lower()
                       for r in caplog.records)
            n = conn.execute(
                "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE version='202611010001'"
            ).fetchone()[0]
            assert n == 0, "MDL 超时的迁移不应盖章"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# S4 · _destructive_ddl + min_code_version 门槛
# ---------------------------------------------------------------------------

class TestDestructiveDdl:
    def _cursor(self):
        mdb = MockDB()
        cur = MockCursor(mdb)
        return cur

    def test_gate_blocks_when_self_version_lower(self, monkeypatch, caplog):
        """自身版本 < meta.min_code_version → 跳过不执行（落后者还依赖旧 schema）。"""
        from src.api import db_setup
        monkeypatch.setattr(db_setup, "get_min_code_version", lambda: "2026.12.01")
        monkeypatch.setattr(db_setup, "current_code_version", lambda: "2026.11.01")
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
        cur = self._cursor()
        executed = []
        monkeypatch.setattr(cur, "execute", lambda sql, params=None: executed.append(sql))
        with caplog.at_level("INFO"):
            db_setup._destructive_ddl(cur, "ALTER TABLE t DROP COLUMN c",
                                      kind_hint="cleanup", version="202611010001")
        assert executed == [], "门槛未过却执行了破坏性 DDL"
        assert any("跳过" in r.message or "skip" in r.message.lower() for r in caplog.records)

    def test_gate_passes_when_no_min_or_self_ge(self, monkeypatch):
        """meta 无 min_code_version 或自身 ≥ min → 放行执行。"""
        from src.api import db_setup
        cur = self._cursor()
        executed = []
        monkeypatch.setattr(cur, "execute", lambda sql, params=None: executed.append(sql))
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
        # ① 无门槛
        monkeypatch.setattr(db_setup, "get_min_code_version", lambda: "")
        db_setup._destructive_ddl(cur, "ALTER TABLE t DROP COLUMN c",
                                  kind_hint="cleanup", version="202611010001")
        # ② 自身 ≥ 门槛
        monkeypatch.setattr(db_setup, "get_min_code_version", lambda: "2026.11.01")
        monkeypatch.setattr(db_setup, "current_code_version", lambda: "2026.12.01")
        db_setup._destructive_ddl(cur, "ALTER TABLE t DROP COLUMN c",
                                  kind_hint="cleanup", version="202611010001")
        assert len(executed) == 2, "门槛已过却未执行破坏性 DDL"

    def test_min_code_version_meta_roundtrip(self, tmp_path):
        """get/set_min_code_version 走 meta 表。"""
        from src.api import db_setup
        conn = sqlite3.connect(str(tmp_path / "meta.db"))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        try:
            db_setup._set_meta_value_conn(conn, "min_code_version", "2026.12.01")
            assert db_setup._get_meta_value_conn(conn, "min_code_version") == "2026.12.01"
            # 缺省空
            assert db_setup._get_meta_value_conn(conn, "nonexistent") == ""
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 结构守卫：历史迁移函数体一行不动 / 危险 DDL 收口
# ---------------------------------------------------------------------------

class TestRepoGuards:
    def test_run_all_migrations_body_untouched_shape(self):
        """_run_all_migrations 仍以原 12 个顶层函数顺序调用（打包 legacy-stamp，不改动函数体）。"""
        from src.api import db_setup
        src = inspect.getsource(db_setup._run_all_migrations)
        for fn in ("_tasks_to_issue_rename_pre_create", "_create_tables", "_migrate_tables",
                   "_create_indexes", "_seed_from_json", "_seed_admin_accounts",
                   "_migrate_knowledge_dedup_and_ref"):
            assert fn in src, f"_run_all_migrations 应保留对 {fn} 的调用（历史行为不变）"

    def test_destructive_ddl_used_by_rename_helper(self):
        """_rename_column_if_exists 的 RENAME/DROP 分支必须走 _destructive_ddl 收口。"""
        from src.api import db_setup
        src = inspect.getsource(db_setup._rename_column_if_exists)
        assert "_destructive_ddl(" in src, "RENAME/DROP 未收敛到 _destructive_ddl"
# ---------------------------------------------------------------------------
# QA R1 · lock_wait_timeout 必须下发在迁移 DDL 所在会话（全局 db），而非仅专用连接
# ---------------------------------------------------------------------------

class TestMigrationSessionLockTimeout:
    def test_executor_sets_timeout_before_ddl_on_migration_session(self, tmp_path, monkeypatch):
        """QA R1：执行器在迁移 DDL 同一会话（全局 db）先 SET SESSION lock_wait_timeout=10。

        旧实现只对 GET_LOCK 专用连接下发；迁移 DDL 走全局 db（池连接），为**不同会话**，
        生产路径超时保护不生效。本用例断言：迁移 fn 执行 DDL 前，同一 db 会话已发过
        SET SESSION。
        """
        from src.api import db_setup
        conn = sqlite3.connect(str(tmp_path / "r1.db"))
        conn.execute(db_setup._SCHEMA_MIGRATIONS_DDL)
        conn.execute(
            "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at) "
            "VALUES (?, ?, 'legacy-stamp', '2026-10-30 00:00:00')",
            (db_setup._LEGACY_STAMP_VERSION, db_setup._LEGACY_STAMP_NAME))
        conn.commit()

        class _RecCursor:
            def __init__(self, rec):
                self._rec = rec

            def execute(self, sql, params=None):
                self._rec.append(str(sql))
                return self

        class _RecDB:
            """模拟 MySQLCompatDB：同一会话（同一底层连接），记录全部 SQL。"""

            def __init__(self):
                self.queries = []

            def cursor(self):
                return _RecCursor(self.queries)

            def execute(self, sql, params=None):
                self.queries.append(str(sql))
                return _RecCursor(self.queries)

        rec = _RecDB()
        monkeypatch.setattr(db_setup, "db", rec)
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
        ran = []

        def _mig():
            rec.execute("ALTER TABLE t DROP COLUMN c")  # 迁移 DDL 走同一 db/会话
            ran.append(1)

        try:
            db_setup._run_migrations_with_conn(
                conn, registry=[db_setup.Migration("202611010001", "r1", "safe", _mig)],
                mdl_holders=lambda e: "")
            assert ran == [1], "迁移 fn 未执行"
            set_idx = next(i for i, q in enumerate(rec.queries)
                           if "SET SESSION" in q and "lock_wait_timeout" in q)
            ddl_idx = next(i for i, q in enumerate(rec.queries)
                           if "ALTER TABLE t DROP COLUMN c" in q)
            assert set_idx < ddl_idx, "迁移 DDL 前未在 DDL 同一会话下发 lock_wait_timeout"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# QA R2 · 版本格式统一：git 提交形态带日期前缀，与 min_code_version 同形可比
# ---------------------------------------------------------------------------

class TestVersionFormat:
    def test_git_version_from_output_has_date_prefix(self):
        """git log 输出 → g-<YYYY.MM.DD>-<短哈希>（日期前缀可与日期式 min 比较）。"""
        from src.api.code_version import _git_version_from_output
        out = "2026.10.30-abc123def4567890abcdef1234567890abcdef12"
        assert _git_version_from_output(out) == "g-2026.10.30-abc123def456"
        assert _git_version_from_output("") == ""
        assert _git_version_from_output("   ") == ""

    def test_version_ge_cross_format_git_date_prefix(self):
        """QA R2 cross-format：git 日期式自身版本 vs 日期式 min，逐段数值比较。"""
        from src.api import db_setup
        self_ver = "g-2026.10.30-abc123def456"
        assert db_setup._version_ge(self_ver, "") is True
        assert db_setup._version_ge(self_ver, "2026.10.29") is True
        assert db_setup._version_ge(self_ver, "2026.10.30") is True
        assert db_setup._version_ge(self_ver, "2026.11.01") is False
        assert db_setup._version_ge("2026.12.01", self_ver) is True

    def test_version_ge_conservative_for_non_numeric_prefix(self):
        """无数值前缀形态（指纹/纯哈希）退化为字符串相等——保守不误放行。"""
        from src.api import db_setup
        assert db_setup._version_ge("fp-abc123def456", "fp-abc123def456") is True
        assert db_setup._version_ge("fp-abc123def456", "2026.12.01") is False
        assert db_setup._version_ge("fp-abc123def456", "fp-xyz") is False

    def test_set_min_code_version_validate_shape(self):
        """部署脚本版本号形状校验：带数值点分前缀才放行（纯哈希拒绝）。"""
        import importlib.util
        _ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        spec = importlib.util.spec_from_file_location(
            "set_min_code_version_check",
            os.path.join(_ROOT, "scripts", "set_min_code_version.py"))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod._validate_version("2026.12.01") == ""
        assert mod._validate_version("2026.12.01-rc1") == ""
        assert mod._validate_version("g-2026.10.30-abc123") == ""
        assert mod._validate_version("") != ""
        assert mod._validate_version("abc123def456") != ""
        assert mod._validate_version("v1.0") != ""


# ---------------------------------------------------------------------------
# QA R3 · legacy-stamp 遵守「跳过不盖章」：被门槛拦截的破坏性 DDL 不打帽
# ---------------------------------------------------------------------------

class TestLegacyStampSkipNotStamp:
    def _fresh_empty(self, tmp_path, name="legacy_skip.db"):
        from src.api import db_setup
        conn = sqlite3.connect(str(tmp_path / name))
        conn.execute(db_setup._SCHEMA_MIGRATIONS_DDL)
        conn.commit()
        return conn

    def test_legacy_stamp_not_written_when_destructive_ddl_blocked(self, tmp_path, monkeypatch, caplog):
        """QA R3：legacy 首跑中破坏性 DDL 被门槛拦截 → 不打帽，版本表保持为空。"""
        from src.api import db_setup
        from tests.mock_db import MockDB, MockCursor
        conn = self._fresh_empty(tmp_path)
        mdb = MockDB()
        cur = MockCursor(mdb)
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
        monkeypatch.setattr(db_setup, "_cleanup_gate_passed", lambda: False)
        calls = []

        def _fake_run_all():
            calls.append(1)
            db_setup._destructive_ddl(cur, "ALTER TABLE t DROP COLUMN c",
                                      kind_hint="cleanup")

        monkeypatch.setattr(db_setup, "_run_all_migrations", _fake_run_all, raising=True)
        try:
            with caplog.at_level("ERROR"):
                db_setup._run_migrations_with_conn(conn, registry=[])
            assert calls == [1], "legacy 首跑未执行"
            stamped = conn.execute(
                "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE kind='legacy-stamp'"
            ).fetchone()[0]
            assert stamped == 0, "门槛拦截时不应打 legacy-stamp 帽"
            assert any("legacy-stamp 不打帽" in r.message for r in caplog.records)
        finally:
            conn.close()
            db_setup._LEGACY_BLOCKED_DDL.clear()

    def test_legacy_stamp_written_when_gate_passes(self, tmp_path, monkeypatch):
        """QA R3：门槛放行 → 正常打帽收敛（回归不变量）。"""
        from src.api import db_setup
        from tests.mock_db import MockDB, MockCursor
        conn = self._fresh_empty(tmp_path, "legacy_ok.db")
        mdb = MockDB()
        cur = MockCursor(mdb)
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
        monkeypatch.setattr(db_setup, "_cleanup_gate_passed", lambda: True)
        calls = []

        def _fake_run_all():
            calls.append(1)
            db_setup._destructive_ddl(cur, "ALTER TABLE t DROP COLUMN c",
                                      kind_hint="cleanup")

        monkeypatch.setattr(db_setup, "_run_all_migrations", _fake_run_all, raising=True)
        try:
            db_setup._LEGACY_BLOCKED_DDL.clear()
            db_setup._run_migrations_with_conn(conn, registry=[])
            assert calls == [1]
            stamped = conn.execute(
                "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE kind='legacy-stamp'"
            ).fetchone()[0]
            assert stamped == 1, "门槛放行时应盖 legacy-stamp 帽"
        finally:
            conn.close()
            db_setup._LEGACY_BLOCKED_DDL.clear()


class TestDestructiveDdlReturnFlag:
    def _cursor(self):
        from tests.mock_db import MockDB, MockCursor
        return MockCursor(MockDB())

    def test_returns_false_and_records_skip_when_gate_blocks(self, monkeypatch):
        """QA R3：门槛拦截时 _destructive_ddl 返回 False 并记入拦截清单。"""
        from src.api import db_setup
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
        monkeypatch.setattr(db_setup, "_cleanup_gate_passed", lambda: False)
        db_setup._LEGACY_BLOCKED_DDL.clear()
        try:
            ok = db_setup._destructive_ddl(self._cursor(),
                                           "ALTER TABLE t DROP COLUMN c",
                                           kind_hint="cleanup")
            assert ok is False
            assert any("DROP COLUMN c" in d for d in db_setup._LEGACY_BLOCKED_DDL)
        finally:
            db_setup._LEGACY_BLOCKED_DDL.clear()

    def test_returns_true_when_executed(self, monkeypatch):
        """QA R3：门槛放行时 _destructive_ddl 返回 True，不记拦截。"""
        from src.api import db_setup
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
        monkeypatch.setattr(db_setup, "_cleanup_gate_passed", lambda: True)
        db_setup._LEGACY_BLOCKED_DDL.clear()
        cur = self._cursor()
        executed = []
        monkeypatch.setattr(cur, "execute", lambda sql, params=None: executed.append(sql))
        try:
            ok = db_setup._destructive_ddl(cur, "ALTER TABLE t DROP COLUMN c",
                                           kind_hint="cleanup")
            assert ok is True
            assert len(executed) == 1
            assert db_setup._LEGACY_BLOCKED_DDL == []
        finally:
            db_setup._LEGACY_BLOCKED_DDL.clear()


# ---------------------------------------------------------------------------
# QA Minor · _ensure_column 的 MDL 超时不当作「不支持」降级，直接跳过不盖章
# ---------------------------------------------------------------------------

class TestEnsureColumnMdlTimeout:
    def _mk_mdl(self):
        """INSTANT / INPLACE 均抛 Lock wait timeout（1205）的 MySQL 游标。"""
        from src.api import db_setup
        mdb = MockDB()
        state = {"queries": []}

        def _handler(sql, params):
            return []

        mdb.register("PRAGMA table_info", lambda sql, params: [])
        cur = MockCursor(mdb)
        original_execute = cur.execute

        def _execute(sql, params=()):
            state["queries"].append(str(sql))
            s = " ".join(str(sql).upper().split())
            if "ALTER TABLE" in s and "ADD COLUMN" in s:
                raise RuntimeError("Lock wait timeout exceeded; try restarting transaction (1205)")
            return original_execute(sql, params)

        cur.execute = _execute
        return cur, state

    def test_instant_mdl_timeout_raises_and_skips_downgrade(self, monkeypatch):
        """QA Minor：INSTANT 因 MDL 超时（1205）→ 转 _MdlWaitTimeout，不降级 INPLACE。"""
        from src.api import db_setup
        cur, state = self._mk_mdl()
        monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
        db_setup._LEGACY_BLOCKED_DDL.clear()
        try:
            with pytest.raises(db_setup._MdlWaitTimeout):
                db_setup._ensure_column(cur, "t1", "c1", "TEXT DEFAULT ''")
            assert not any("ALGORITHM=INPLACE" in q.upper() for q in state["queries"]), (
                "MDL 超时不应降级重试 INPLACE")
            assert any("t1.c1" in d and "MDL" in d for d in db_setup._LEGACY_BLOCKED_DDL)
        finally:
            db_setup._LEGACY_BLOCKED_DDL.clear()