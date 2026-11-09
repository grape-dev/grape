"""Issue #1546: autopilot 执行记录直持 agent_task_id，消除 event_seq 撞号误判终态。

覆盖：
- E1-1 同号场景：历史 a2a_event（completed、空结果）+ 随后真 autopilot_event 任务 →
  回收器跑一轮后 exec 仍为 running/dispatched，不得写 success；
- E1-2 直连场景：exec.agent_task_id 指向真任务 → 只按 id 取行，绝不触发 event_seq 查询；
- E1-3 年轻度：started_at=now 的 exec 即便有历史同号行也不收口；
- E1-4 兜底边界：历史 exec（agent_task_id 空）+ event_type=autopilot_event 但
  created_at 早于 started_at 的同号行 → 不匹配；
- E2 迁移：注册项正确、fn 在新库建列+索引且幂等、现有库已盖章且二次 0 pending；
- E4 间接验证：agent_tasks 的 _TASK_COLUMNS 未因本改动被修改。

全部走 sqlite 测试库（tests/conftest.py 初始化），不连生产库、不触发远端执行。
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


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _clean():
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM autopilot_executions WHERE id >= 154600 AND id < 154700")
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
        _seed_task("t_i1546_hist", 154601, "completed", "a2a_event", result_text="",
                   created_at=_iso(_utcnow() - timedelta(hours=2)))
        _seed_task("t_i1546_real", 154601, "running", "autopilot_event",
                   created_at=_iso(_utcnow()))
        _seed_exec(154601)
        _reap_zombie_executions()
        assert _exec_status(154601) == "running", "历史同号 completed 被误判为终态"

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
            _Cursor(), (154602, "t_i1546_real", _iso(_utcnow() - timedelta(hours=1)),
                        _iso(_utcnow() - timedelta(hours=1))))
        assert row is not None
        assert row[0] == "t_i1546_real"
        assert not any("event_seq" in s for s in executed)

    def test_young_execution_not_reaped(self):
        """E1-3：started_at=now 的执行本轮跳过（回收器竞态保护）。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_task("t_i1546_hist2", 154603, "completed", "a2a_event", result_text="old",
                   created_at=_iso(_utcnow() - timedelta(hours=2)))
        _seed_exec(154603, started_at=_iso(_utcnow()))
        _reap_zombie_executions()
        assert _exec_status(154603) == "running", "年轻执行不应被回收"

    def test_fallback_boundary_created_before_started_not_matched(self):
        """E1-4：兜底要求 task.created_at >= exec.started_at，早于则视为不匹配。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_task("t_i1546_old", 154604, "completed", "autopilot_event", result_text="old",
                   created_at=_iso(_utcnow() - timedelta(hours=3)))
        _seed_exec(154604, started_at=_iso(_utcnow() - timedelta(hours=2)))
        _reap_zombie_executions()
        assert _exec_status(154604) == "running", "created_at 早于 started_at 的同号行被误匹配"

    def test_direct_agent_task_id_resolves_real_task(self):
        """直连：agent_task_id 指向真任务即按主键命中（回收收口 success）。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_task("t_i1546_direct", 154605, "completed", "autopilot_event",
                   result_text="real result")
        _seed_exec(154605, agent_task_id="t_i1546_direct")
        _reap_zombie_executions()
        assert _exec_status(154605) == "success"


class TestMigration:
    def test_registered_safe_migration(self):
        from src.api import db_setup
        by_version = {m.version: m for m in db_setup._MIGRATIONS}
        assert "202611060003" in by_version
        mig = by_version["202611060003"]
        assert mig.name == "add-autopilot-exec-agent-task-id"
        assert mig.kind == "safe"

    def test_fn_creates_column_and_index_idempotent(self, tmp_path, monkeypatch):
        from src.api import db_setup
        conn = sqlite3.connect(str(tmp_path / "ap1546.db"))
        conn.execute("CREATE TABLE autopilot_executions (id INTEGER PRIMARY KEY)")
        conn.commit()

        class _DBFacade:
            def execute(self, sql, params=()):
                return conn.execute(sql, params)

            def cursor(self):
                return conn.cursor()

            def commit(self):
                conn.commit()

        monkeypatch.setattr(db_setup, "db", _DBFacade())
        db_setup._fn_add_ap_exec_agent_task_id()
        db_setup._fn_add_ap_exec_agent_task_id()   # 幂等：二次执行不报错
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(autopilot_executions)").fetchall()]
        idx = [r[1] for r in conn.execute(
            "PRAGMA index_list(autopilot_executions)").fetchall()]
        assert "agent_task_id" in cols
        assert "idx_ap_exec_agent_task" in idx
        conn.close()

    def test_existing_db_stamped_and_zero_pending(self):
        """存量库：迁移已盖章，二次启动 0 pending（不重复执行）。"""
        from src.api import db_setup
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT COUNT(*) FROM grape_t_schema_migrations WHERE version=?",
                ("202611060003",)).fetchone()
            cols = [r[1] for r in c.execute(
                "PRAGMA table_info(autopilot_executions)").fetchall()]
        assert rows[0] == 1, "迁移未盖章"
        assert "agent_task_id" in cols
        # 二次执行器：全部已盖章 → 0 pending，不抛错
        db_setup._run_migrations_with_conn(db)


class TestTaskColumnsUnchanged:
    def test_agent_task_columns_not_extended(self):
        """E4：agent_tasks 的 _TASK_COLUMNS 未被本改动扩展。"""
        from src.runtime.service import _TASK_COLUMNS_LIST
        assert "agent_task_id" not in _TASK_COLUMNS_LIST