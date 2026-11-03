"""Issue #588: pending_since 时区语义不一致——任务启动约 8 秒即被判定 1800 秒超时。

根因：写入端 terminate_active_agents() 用 gmtime() 写无时区 UTC（ISO8601 无偏移）；
解析端 health.parse_dt() 在 MySQL 后端把无时区时间按 UTC+08:00 解释（Issue #512 口径），
elapsed = 8 + 28800 = 28808s > 1800s，与「启动约 8 秒即判 1800 秒超时」现象精确吻合。

修复：写入端改为带时区 ISO-8601 UTC（+00:00），写入与解析同源；
parse_dt 对带时区值不做 naive 回填，双后端 elapsed 均按真实 UTC 计算。

回归覆盖（Spec 要求：MySQL 与 SQLite 双后端）：
1. executor 写入值带时区偏移；
2. 刚写入（8s 前）不超时——双后端（核心误判场景回归）；
3. 真实 1800s 超时照常回收——双后端；
4. 带时区值解析同源——parse_dt 单元级双后端；
5. 无时区历史值（含空格格式）不崩溃、远期值仍可回收（历史值兼容单独评估，不在本次范围）。

MySQL 分支通过 patch src.api.time_utils._db_backend 模拟（parse_dt 运行时导入该函数）。
"""
import os
import re
import sys
import time as _t
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock

RT_REMOTE = 9318
CRED_REMOTE = 9418
RK = "gitcode:tz/test588"
PENDING_TIMEOUT_DEFAULT = 1800

import pytest


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    yield
    _clean()


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "agent_runtimes", "events", "runtime_credentials"):
            try:
                c.execute(f"DELETE FROM {t} WHERE runtime_id IN ({RT_REMOTE}) "
                          "OR id LIKE 't_i588%' OR repo_key='tz/test588'")
            except Exception:
                pass
        try:
            c.execute("DELETE FROM autopilot_executions WHERE a2a_seq >= 9700 AND a2a_seq < 9800")
        except Exception:
            pass
        db.commit()


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _seed_runtime(rid, cid, busy=1):
    with db_lock:
        c = db.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        if "busy_slots" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN busy_slots INTEGER DEFAULT 0")
        if "max_concurrent" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN max_concurrent INTEGER DEFAULT 4")
        if "worker_version" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN worker_version TEXT DEFAULT ''")
        c.execute(
            "INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
            "busy_slots, max_concurrent, last_seen_at, worker_version) VALUES (?,?,?, 'online',1,1,?,4,?,'')",
            (rid, f"rt-{rid}", cid, busy, _iso(_utcnow())))
        db.commit()


def _seed_task(tid, seq, pending_since, status="pending_self_report"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, "
            "agent_type, event_type, status, prompt, cwd, skill_files, env_extra, event_seq, "
            "skill, started_at, pending_since) VALUES "
            "(?,?,?,?,'#588','vod','a2a_event',?,'do work','','{}',?,?, '',?,?)",
            (tid, "sess-" + tid, RT_REMOTE, RK, status, _env(), seq, pending_since, pending_since))
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, status, summary, result) "
            "VALUES (?,?,?,'a2a_event','vod','running','A2A: → coding','')",
            (seq, RK, "#588"))
        db.commit()


def _env():
    return "{}"


def _task_status(tid):
    with db_lock:
        r = db.cursor().execute("SELECT status FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        return r[0] if r else None


# =====================================================================
# T1 · executor 写入端：pending_since 带时区偏移
# ======================================================================

class TestT1ExecutorWritesTzAware:
    def test_terminate_writes_utc_offset_suffix(self):
        """terminate_active_agents 写入的 pending_since 必须带 +00:00 时区偏移。"""
        from src.agent.executor import terminate_active_agents
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        _seed_task("t_i588_exec", 9700, _iso(_utcnow()), status="running")
        terminate_active_agents(grace_seconds=0)
        with db_lock:
            ps = db.cursor().execute(
                "SELECT pending_since FROM agent_tasks WHERE id=?", ("t_i588_exec",)).fetchone()[0]
        assert ps is not None
        assert re.search(r"(Z|[+-]\d{2}:\d{2})$", ps), \
            f"pending_since 须带时区偏移（Issue #588），实际值：{ps!r}"


# =====================================================================
# T2 · 核心误判场景回归：刚写入（8s 前）不超时——双后端
# ======================================================================

class TestT2FreshWriteNotTimeout:
    def _fresh_tz_value(self, age=8):
        # 模拟修复后 executor 写入：gmtime + 显式 +00:00 偏移
        return _t.strftime("%Y-%m-%dT%H:%M:%S+00:00", _t.gmtime(_t.time() - age))

    def test_sqlite_fresh_8s_not_timeout(self):
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        _seed_task("t_i588_f_sq", 9701, self._fresh_tz_value())
        _sweep_pending_self_report_timeouts()
        assert _task_status("t_i588_f_sq") == "pending_self_report"

    def test_mysql_fresh_8s_not_timeout(self):
        """核心回归：MySQL 后端下刚写入 8 秒不得判 timeout_failed（原缺陷：elapsed=28808s）。"""
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        _seed_task("t_i588_f_my", 9702, self._fresh_tz_value())
        with patch("src.api.time_utils._db_backend", return_value="mysql"):
            _sweep_pending_self_report_timeouts()
        assert _task_status("t_i588_f_my") == "pending_self_report", \
            "MySQL 后端刚写入 8s 的带时区值不得被判 1800s 超时（Issue #588）"

    def test_mysql_fresh_8s_via_terminate_end_to_end(self):
        """端到端：terminate 写入 → MySQL 口径 sweep → 不超时。"""
        from src.agent.executor import terminate_active_agents
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        _seed_task("t_i588_e2e", 9703, _iso(_utcnow()), status="running")
        terminate_active_agents(grace_seconds=0)
        with patch("src.api.time_utils._db_backend", return_value="mysql"):
            _sweep_pending_self_report_timeouts()
        assert _task_status("t_i588_e2e") == "pending_self_report"


# ======================================================================
# T3 · 真实 1800s 超时照常回收——双后端
# ======================================================================

class TestT3RealTimeoutStillSwept:
    def _expired_tz_value(self):
        return _t.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                           _t.gmtime(_t.time() - (PENDING_TIMEOUT_DEFAULT + 60)))

    def test_sqlite_expired_marked(self):
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        _seed_task("t_i588_x_sq", 9704, self._expired_tz_value())
        _sweep_pending_self_report_timeouts()
        assert _task_status("t_i588_x_sq") == "timeout_failed"

    def test_mysql_expired_marked(self):
        """超时兜底不得被本次修复削弱：真实超时（1800+60s 前）仍判 timeout_failed。"""
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        _seed_task("t_i588_x_my", 9705, self._expired_tz_value())
        with patch("src.api.time_utils._db_backend", return_value="mysql"):
            _sweep_pending_self_report_timeouts()
        assert _task_status("t_i588_x_my") == "timeout_failed"


# ======================================================================
# T4 · parse_dt 同源解析：带时区值双后端 epoch 一致
# ======================================================================

class TestT4ParseDtSameEpoch:
    VAL = "2026-10-31T01:00:00+00:00"

    def test_tz_aware_epoch_identical_across_backends(self):
        from src.runtime.health import parse_dt
        with patch("src.api.time_utils._db_backend", return_value="sqlite"):
            ts_sql = parse_dt(self.VAL)
        with patch("src.api.time_utils._db_backend", return_value="mysql"):
            ts_my = parse_dt(self.VAL)
        assert ts_sql is not None and ts_my is not None
        assert abs(ts_sql - ts_my) < 1.0, \
            f"带时区值双后端 epoch 应一致：sqlite={ts_sql} mysql={ts_my}"

    def test_tz_aware_matches_expected_utc_epoch(self):
        from src.runtime.health import parse_dt
        expected = datetime(2026, 10, 31, 1, 0, 0, tzinfo=timezone.utc).timestamp()
        with patch("src.api.time_utils._db_backend", return_value="mysql"):
            assert abs(parse_dt(self.VAL) - expected) < 1.0

    def test_naive_values_keep_backend_semantics(self):
        """无时区历史值维持既有口径（sqlite=UTC / mysql=CST）——本次不改 parse_dt。"""
        from src.runtime.health import parse_dt
        naive = "2026-10-31T01:00:00"
        with patch("src.api.time_utils._db_backend", return_value="sqlite"):
            ts_sql = parse_dt(naive)
        with patch("src.api.time_utils._db_backend", return_value="mysql"):
            ts_my = parse_dt(naive)
        assert abs((ts_sql - ts_my) - 28800.0) < 1.0, \
            "无时区值语义应保留 #512 口径（差 8h），本次修复不动该行为"


# ======================================================================
# T5 · 无时区历史值：不崩溃 + 远期值仍回收（兼容策略单独评估）
# ======================================================================

class TestT5NaiveHistoricalValues:
    def test_naive_expired_still_swept_both_backends(self):
        """无时区历史远期值（超过任何口径的超时窗口）双后端仍应回收。"""
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        _seed_task("t_i588_h_sq", 9706, "2026-01-01 00:00:00")
        _seed_task("t_i588_h_my", 9707, "2026-01-01T00:00:00")
        _sweep_pending_self_report_timeouts()
        with patch("src.api.time_utils._db_backend", return_value="mysql"):
            _sweep_pending_self_report_timeouts()
        assert _task_status("t_i588_h_sq") == "timeout_failed"
        assert _task_status("t_i588_h_my") == "timeout_failed"

    def test_naive_recent_not_crash(self):
        """无时区近窗值：sweep 不抛异常、不写半态（兼容恢复单独改动处理）。"""
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE)
        _seed_task("t_i588_h_r", 9708, _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime()))
        with patch("src.api.time_utils._db_backend", return_value="mysql"):
            n = _sweep_pending_self_report_timeouts()
        assert isinstance(n, int)
        st = _task_status("t_i588_h_r")
        assert st in ("pending_self_report", "timeout_failed"), f"状态异常：{st}"
