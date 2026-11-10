"""Issue #1546: autopilot 执行记录直持 agent_task_id，消除 event_seq 撞号误判终态。

覆盖：
- E1-1 同号场景：历史 a2a_event（completed、空结果）+ 随后真 autopilot_event 任务 →
  回收器跑一轮后 exec 仍为 running/dispatched，不得写 success；
- E1-1 真复现：回收时只有历史同号 a2a_event 行、真任务尚未落库 → 旧代码会命中历史行
  误收口 success，新代码按 event_type + 时间边界过滤后不匹配；
- E1-2 直连场景：exec.agent_task_id 指向真任务 → 只按 id 取行，绝不触发 event_seq 查询；
- E1-3 年轻度：started_at=now 的 exec 即便有历史同号行也不收口；
- E1-4 兜底边界：历史 exec（agent_task_id 空）+ event_type=autopilot_event 但
  created_at 早于 started_at 的同号行 → 不匹配；
- E2 迁移：注册项正确；存量库首跑经执行器盖章列+索引、二次 0 pending；新装库
  （空版本表）首跑经 legacy-stamp 执行器路径后列+索引同样存在；
- E4 间接验证：agent_tasks 的 _TASK_COLUMNS 未因本改动被修改。

全部走 sqlite 测试库（tests/conftest.py 初始化）与独立临时库，不连生产库、不触发远端执行。
"""
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock

RT_ID = 15461
RK = "gitcode:autopilot/test1546"
# 高位执行 id：避开共享测试库其它用例的 id，确保本用例始终落在回收器
# `ORDER BY id DESC LIMIT 20` 窗口内，断言不会因被挤出而空跑。
EXEC_BASE = 1900000000


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _clean():
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM autopilot_executions WHERE id >= ? AND id < ?",
                      (EXEC_BASE, EXEC_BASE + 1000))
        except Exception:
            pass
        try:
            c.execute("DELETE FROM agent_tasks WHERE id LIKE 't_i1546%' "
                      "OR repo_key=?", (RK,))
        except Exception:
            pass
        db.commit()


def _seed_task(tid, seq, status, event_type, result_text="", created_at=None):
    _created = created_at or _iso(_utcnow())
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, "
            "agent_type, event_type, status, prompt, cwd, skill_files, env_extra, event_seq, "
            "skill, started_at, result_text, created_at) "
            "VALUES (?,?,?,?,?, 'vod', ?, ?, 'do work', '', '{}', '{}', ?, '', ?, ?, ?)",
            (tid, "sess-" + tid, RT_ID, RK, "#1546", event_type, status, seq,
             _iso(_utcnow()), result_text, _created))
        db.commit()


def _seed_exec(exec_id, status="running", task_id=1546, agent_task_id=None, started_at=None):
    _started = started_at or _iso(_utcnow() - timedelta(hours=1))
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id=?", (exec_id,))
        c.execute(
            "INSERT INTO autopilot_executions "
            "(id, task_id, status, a2a_seq, agent_task_id, started_at, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (exec_id, task_id, status, exec_id, agent_task_id, _started, _started))
        db.commit()


def _exec_status(exec_id):
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT status FROM autopilot_executions WHERE id=?",
                        (exec_id,)).fetchone()
        return row[0] if row else None


class TestReverseLookupUsesAgentTaskId:
    def setup_method(self):
        _clean()

    def teardown_method(self):
        _clean()

    def test_same_seq_history_not_used_when_real_task_later(self):
        """E1-1：历史同号 completed(a2a_event) 不得让 exec 误收口 success。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_task("t_i1546_hist", EXEC_BASE + 1, "completed", "a2a_event", result_text="",
                   created_at=_iso(_utcnow() - timedelta(hours=2)))
        _seed_task("t_i1546_real", EXEC_BASE + 1, "running", "autopilot_event",
                   created_at=_iso(_utcnow()))
        _seed_exec(EXEC_BASE + 1)
        _reap_zombie_executions()
        assert _exec_status(EXEC_BASE + 1) == "running", "历史同号 completed 被误判为终态"

    def test_only_history_row_no_real_task_not_reaped(self):
        """E1-1 真复现：只有历史同号 a2a_event 行、真任务尚未落库 → 不得收口 success。

        旧代码 `event_seq=? ORDER BY created_at DESC LIMIT 1` 无 event_type/时间过滤，
        会命中历史 a2a_event completed 行并写 success（本用例在 pre-fix 会失败）。
        """
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_task("t_i1546_only_hist", EXEC_BASE + 2, "completed", "a2a_event",
                   result_text="", created_at=_iso(_utcnow() - timedelta(hours=2)))
        _seed_exec(EXEC_BASE + 2, started_at=_iso(_utcnow() - timedelta(hours=1)))
        _reap_zombie_executions()
        assert _exec_status(EXEC_BASE + 2) == "running", \
            "无真 autopilot_event 任务时不得命中历史 a2a_event 行收口"

    def test_direct_agent_task_id_skips_event_seq_query(self):
        """E1-2：agent_task_id 非空只按 id 取行，绝不触发 event_seq 查询。"""
        from src.runtime import scheduler
        executed = []

        class _Cursor:
            def execute(self, sql, params=()):
                executed.append(sql)
                assert "event_seq" not in sql, f"直连路径不应查 event_seq：{sql}"
                return self

            def fetchone(self):
                return ("t_i1546_real", "running", "", 0, "", "{}", RT_ID)

        row = scheduler._get_exec_agent_task(
            _Cursor(), EXEC_BASE + 3, "t_i1546_real",
            _iso(_utcnow() - timedelta(hours=1)), _iso(_utcnow() - timedelta(hours=1)))
        assert row is not None
        assert row[0] == "t_i1546_real"
        assert not any("event_seq" in s for s in executed)

    def test_young_execution_not_reaped(self):
        """E1-3：started_at=now 的执行本轮跳过（回收器竞态保护）。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_task("t_i1546_hist2", EXEC_BASE + 4, "completed", "a2a_event", result_text="old",
                   created_at=_iso(_utcnow() - timedelta(hours=2)))
        _seed_exec(EXEC_BASE + 4, started_at=_iso(_utcnow()))
        _reap_zombie_executions()
        assert _exec_status(EXEC_BASE + 4) == "running", "年轻执行不应被回收"

    def test_fallback_boundary_created_before_started_not_matched(self):
        """E1-4：兜底要求 task.created_at >= exec.started_at，早于则视为不匹配。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_task("t_i1546_old", EXEC_BASE + 5, "completed", "autopilot_event", result_text="old",
                   created_at=_iso(_utcnow() - timedelta(hours=3)))
        _seed_exec(EXEC_BASE + 5, started_at=_iso(_utcnow() - timedelta(hours=2)))
        _reap_zombie_executions()
        assert _exec_status(EXEC_BASE + 5) == "running", \
            "created_at 早于 started_at 的同号行被误匹配"


class _DBFacade:
    """把迁移 fn 内的全局 db 调用重定向到独立的 sqlite 连接。"""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return self._conn.execute(sql, params)

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()


def _fresh_migration_conn(tmp_path, name, legacy_stamped):
    from src.api import db_setup
    conn = sqlite3.connect(str(tmp_path / name))
    conn.execute(db_setup._SCHEMA_MIGRATIONS_DDL)
    conn.execute("CREATE TABLE autopilot_executions (id INTEGER PRIMARY KEY)")
    if legacy_stamped:
        conn.execute(
            "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at) "
            "VALUES (?, ?, 'legacy-stamp', '2026-10-30 00:00:00')",
            (db_setup._LEGACY_STAMP_VERSION, db_setup._LEGACY_STAMP_NAME))
    conn.commit()
    return conn


def _counting_migration(db_setup, calls):
    def _fn():
        calls.append(1)
        db_setup._fn_add_ap_exec_agent_task_id()
    return db_setup.Migration("202611090001", "add-autopilot-exec-agent-task-id",
                              "safe", _fn)


def _assert_column_and_index(conn, check_stamp=True):
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(autopilot_executions)").fetchall()]
    idx = [r[1] for r in conn.execute(
        "PRAGMA index_list(autopilot_executions)").fetchall()]
    assert "agent_task_id" in cols
    assert "idx_ap_exec_agent_task" in idx
    if check_stamp:
        stamped = conn.execute(
            "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE version=?",
            ("202611090001",)).fetchone()[0]
        assert stamped == 1, "迁移未盖章"


class TestMigration:
    def test_registered_safe_migration(self):
        from src.api import db_setup
        by_version = {m.version: m for m in db_setup._MIGRATIONS}
        assert "202611090001" in by_version
        mig = by_version["202611090001"]
        assert mig.name == "add-autopilot-exec-agent-task-id"
        assert mig.kind == "safe"

    def test_fn_creates_column_and_index_idempotent(self, tmp_path, monkeypatch):
        from src.api import db_setup
        conn = sqlite3.connect(str(tmp_path / "ap1546.db"))
        conn.execute("CREATE TABLE autopilot_executions (id INTEGER PRIMARY KEY)")
        conn.commit()
        monkeypatch.setattr(db_setup, "db", _DBFacade(conn))
        db_setup._fn_add_ap_exec_agent_task_id()
        db_setup._fn_add_ap_exec_agent_task_id()   # 幂等：二次执行不报错
        _assert_column_and_index(conn, check_stamp=False)
        conn.close()

    def test_existing_db_first_run_stamps_then_zero_pending(self, tmp_path, monkeypatch):
        """存量库：经执行器首跑盖章列+索引一次，二次启动 0 pending（不再执行 fn）。"""
        from src.api import db_setup
        conn = _fresh_migration_conn(tmp_path, "ap1546_stamped.db", legacy_stamped=True)
        monkeypatch.setattr(db_setup, "db", _DBFacade(conn))
        calls = []
        mig = _counting_migration(db_setup, calls)
        try:
            db_setup._run_migrations_with_conn(conn, registry=[mig])
            assert calls == [1], "存量库首跑应经执行器执行本迁移一次"
            _assert_column_and_index(conn)
            # 二次启动：版本已盖章 → 0 pending，不重复执行 fn
            db_setup._run_migrations_with_conn(conn, registry=[mig])
            assert calls == [1], "二次启动仍执行（0 pending 破约）"
        finally:
            conn.close()

    def test_new_install_empty_version_table_first_run_creates_schema(
            self, tmp_path, monkeypatch):
        """新装库（空版本表）首跑经 legacy-stamp 执行器路径后列+索引同样存在。"""
        from src.api import db_setup
        conn = _fresh_migration_conn(tmp_path, "ap1546_fresh.db", legacy_stamped=False)
        monkeypatch.setattr(db_setup, "db", _DBFacade(conn))
        # 空版本表 → 执行器走 legacy-stamp 全量路径；本用例只验证增量迁移段，
        # 全量迁移（新装库建表）以 no-op 模拟，避免触达真实全局库。
        monkeypatch.setattr(db_setup, "_run_all_migrations", lambda: None, raising=True)
        calls = []
        mig = _counting_migration(db_setup, calls)
        try:
            db_setup._run_migrations_with_conn(conn, registry=[mig])
            assert calls == [1], "新装库首跑应执行本迁移一次"
            _assert_column_and_index(conn)
            db_setup._run_migrations_with_conn(conn, registry=[mig])
            assert calls == [1], "二次启动仍执行（0 pending 破约）"
        finally:
            conn.close()


class TestTaskColumnsUnchanged:
    def test_agent_task_columns_not_extended(self):
        """E4：agent_tasks 的 _TASK_COLUMNS 未被本改动扩展（常量直接对拍）。"""
        from src.runtime.service import _TASK_COLUMNS_LIST
        assert "agent_task_id" not in _TASK_COLUMNS_LIST
