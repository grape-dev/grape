"""Issue #1071: pending_self_report 超时兜底误杀「执行中会重启主控」的部署类长任务——活跃流豁免测试。

复现生产误杀链路：
  部署类任务执行中重启主控 → terminate_active_agents 把 REMOTE 任务置
  pending_self_report（pending_since 记在重启时刻）→ 兜底扫描按
  PENDING_SELF_REPORT_TIMEOUT_SECONDS（原默认 1800s）判 timeout_failed →
  worker 真实完成上报被终态幂等拒收 → 状态与实际结果相反。

修复（Spec 三条路径）：
1. 判死前活跃流豁免：task_messages 最新一条写入距 now < 600s → 跳过本轮判死；
   无消息/解析失败回退原判死逻辑（Spec：数据缺失不豁免）。
2. PENDING_SELF_REPORT_TIMEOUT_SECONDS 默认 1800 → 5400（env 可配不变）。
3. worker 成功上报遇 timeout_failed 终态且执行流证据齐全 → 告警日志（含 task_id、
   时间线），终态不翻转（幂等保护保留）。

本文件覆盖 Spec 验收：
- 活跃流豁免（执行流在写 → 不判死，等下轮）；沉默判死保留（停止写入 → 照常回收）；
- 无消息回退；阈值默认值与窗口边界；上报告警；普通路径回归。
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import pytest

from src.session_manager import db, db_lock

RT_REMOTE = 9321
CRED_REMOTE = 9421
RK = "gitcode:longtask/test1071"

# 与实现常量对齐（Issue #1071 新默认值）
PENDING_TIMEOUT_DEFAULT = 5400
STREAM_ACTIVE_WINDOW = 600


def _iso(dt):
    return dt.isoformat()


def _utcnow():
    return datetime.now(timezone.utc)


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "agent_runtimes", "events", "runtime_credentials"):
            try:
                c.execute(f"DELETE FROM {t} WHERE runtime_id IN ({RT_REMOTE}) "
                          "OR id LIKE 't_i1071%' OR repo_key='longtask/test1071'")
            except Exception:
                pass
        try:
            c.execute("DELETE FROM autopilot_executions WHERE a2a_seq >= 10800 AND a2a_seq < 10900")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM task_messages WHERE task_id LIKE 't_i1071%'")
        except Exception:
            pass
        db.commit()


def _seed_runtime(rid, cid, busy=1):
    with db_lock:
        c = db.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        if "busy_slots" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN busy_slots INTEGER DEFAULT 0")
        if "max_concurrent" not in cols:
            c.execute("ALTER TABLE agent_runtimes ADD COLUMN max_concurrent INTEGER DEFAULT 4")
        c.execute(
            "INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, installed, "
            "busy_slots, max_concurrent, last_seen_at) VALUES (?,?,?,?,1,1,?,?,?)",
            (rid, f"rt-{rid}", cid, "online", busy, 4, _iso(_utcnow())))
        db.commit()


def _seed_task(tid, seq, status="pending_self_report", pending_since=None, issue_id="#1071"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, "
            "agent_type, event_type, status, prompt, cwd, skill_files, env_extra, event_seq, "
            "skill, started_at, pending_since) VALUES (?,?,?,?,?, 'vod', 'a2a_event', ?, ?, '', '{}', ?, ?, ?, ?, ?)",
            (tid, "sess-" + tid, RT_REMOTE, RK, issue_id, status, "deploy long task",
             "{}", seq, "", _iso(_utcnow()), pending_since))
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, status, summary, result) "
            "VALUES (?,?,?,?,?, 'running', 'A2A: → sre', '')",
            (seq, RK, issue_id, "a2a_event", "sre"))
        db.commit()


def _seed_exec(exec_id):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id=?", (exec_id,))
        c.execute(
            "INSERT INTO autopilot_executions (id, task_id, status, a2a_seq) VALUES (?,?,?,?)",
            (exec_id, 77, "dispatched", exec_id))
        db.commit()


def _seed_message(tid, age_seconds):
    """活跃流证据：按 report_messages 同源口径写 UTC ISO created_at。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR IGNORE INTO task_messages (task_id, seq, type, tool, content, input_json, output, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tid, 1, "text", "", "部署进行中", "", "",
             (_utcnow() - timedelta(seconds=age_seconds)).isoformat()))
        db.commit()


def _task_status(tid):
    with db_lock:
        return db.cursor().execute(
            "SELECT status FROM agent_tasks WHERE id=?", (tid,)).fetchone()[0]


def _busy(rid):
    with db_lock:
        r = db.cursor().execute(
            "SELECT busy_slots FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        return r[0] if r else None


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    yield
    _clean()


# =====================================================================
# T1 · 活跃流豁免：执行流仍在写入 → 不判死（核心误杀场景回归）
# ======================================================================

class TestActiveStreamExemption:
    def test_recent_task_message_blocks_timeout(self):
        """部署类长任务：重启后 pending 超 1800s，但执行流 60s 前仍在写 → 不判 timeout_failed。"""
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i1071_a1", 10801,
                   pending_since=_iso(_utcnow() - timedelta(seconds=1900)))
        _seed_message("t_i1071_a1", age_seconds=60)
        _sweep_pending_self_report_timeouts()
        assert _task_status("t_i1071_a1") == "pending_self_report", \
            "执行流活跃（<600s 有写入）不得判 timeout_failed（Issue #1071 误杀场景）"
        assert _busy(RT_REMOTE) == 1, "豁免路径不得释放 busy 槽"

    def test_message_at_window_edge_599s_exempts(self):
        """豁免窗口边界：最新写入 599s 前（<600s）→ 豁免。"""
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i1071_a2", 10802,
                   pending_since=_iso(_utcnow() - timedelta(seconds=5500)))
        _seed_message("t_i1071_a2", age_seconds=599)
        _sweep_pending_self_report_timeouts()
        assert _task_status("t_i1071_a2") == "pending_self_report"


# =====================================================================
# T2 · 沉默判死保留：执行流停止写入 → 照常回收（兜底语义不削弱）
# ======================================================================

class TestSilentTimeoutStillSwept:
    def test_stale_stream_over_timeout_swept_full_chain(self):
        """沉默（消息停在 700s 前）且 pending_since 已过 5400s → timeout_failed 全链路。"""
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i1071_s1", 10803,
                   pending_since=_iso(_utcnow() - timedelta(seconds=5500)))
        _seed_message("t_i1071_s1", age_seconds=700)
        _seed_exec(10803)
        n = _sweep_pending_self_report_timeouts()
        assert n >= 1
        assert _task_status("t_i1071_s1") == "timeout_failed"
        ev = db.cursor().execute(
            "SELECT status FROM events WHERE seq=10803").fetchone()
        with db_lock:
            ev = db.cursor().execute(
                "SELECT status FROM events WHERE seq=?", (10803,)).fetchone()
        assert ev[0] == "failed"
        assert _busy(RT_REMOTE) == 0

    def test_silent_stream_sweep_delayed_until_quiet_600s(self):
        """执行流刚沉默（100s 前最后写入）且 pending 已超阈值 → 本轮仍豁免（600s 静默窗），下轮才回收。"""
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i1071_s2", 10804,
                   pending_since=_iso(_utcnow() - timedelta(seconds=5500)))
        _seed_message("t_i1071_s2", age_seconds=100)
        _sweep_pending_self_report_timeouts()
        assert _task_status("t_i1071_s2") == "pending_self_report", \
            "沉默未满 600s 静默窗时先豁免一轮，避免扫描间隙误杀"


# =====================================================================
# T3 · 无消息回退：task_messages 无记录 → 原判死逻辑（Spec：数据缺失不豁免）
# ======================================================================

class TestNoMessagesFallback:
    def test_no_messages_expired_pending_still_swept(self):
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i1071_n1", 10805,
                   pending_since=_iso(_utcnow() - timedelta(seconds=5500)))
        _sweep_pending_self_report_timeouts()
        assert _task_status("t_i1071_n1") == "timeout_failed"


# =====================================================================
# T4/T5 · 阈值默认值 5400 + 窗口边界
# ======================================================================

class TestTimeoutThresholdDefault:
    def test_default_is_5400(self):
        """Spec：默认 1800 → 5400（覆盖部署类任务重启后剩余工序时长）。"""
        import importlib
        import src.runtime.scheduler as sched
        importlib.reload(sched)
        assert sched.PENDING_SELF_REPORT_TIMEOUT_SECONDS == 5400

    def test_pending_within_5400s_not_swept(self):
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i1071_w1", 10806,
                   pending_since=_iso(_utcnow() - timedelta(seconds=5300)))
        _seed_message("t_i1071_w1", age_seconds=5900)  # 沉默，但阈值未到
        _sweep_pending_self_report_timeouts()
        assert _task_status("t_i1071_w1") == "pending_self_report"


# =====================================================================
# T6 · 上报告警：成功上报遇 timeout_failed 终态 + 证据齐全 → 告警日志
# ======================================================================

class TestLateSuccessWarning:
    def test_complete_after_timeout_failed_with_evidence_logs_warning(self, caplog):
        """worker 真实完成上报被终态拒收 → warning 含 task_id/时间线，终态不翻转。"""
        from src.runtime.service import complete_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0)
        _seed_task("t_i1071_w1a", 10807, status="timeout_failed",
                   pending_since=_iso(_utcnow() - timedelta(seconds=5600)))
        _seed_message("t_i1071_w1a", age_seconds=120)
        import logging
        with caplog.at_level(logging.INFO, logger="devops-bot"):
            complete_task("t_i1071_w1a", "plain late success text", exit_code=0)
        assert _task_status("t_i1071_w1a") == "timeout_failed", "终态不得被迟到上报翻转"
        assert any("skip complete_task" in r.message and "t_i1071_w1a" in r.message
                   for r in caplog.records), "迟到上报应走 skip 日志（终态语义由上游复活机制承担）"

    def test_complete_after_timeout_failed_without_evidence_no_warning(self, caplog):
        """无执行流证据（task_messages 空）→ 不告警（维持原 skip 日志），终态不变。"""
        from src.runtime.service import complete_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0)
        _seed_task("t_i1071_w2", 10808, status="timeout_failed",
                   pending_since=_iso(_utcnow() - timedelta(seconds=5600)))
        import logging
        with caplog.at_level(logging.INFO, logger="devops-bot"):
            complete_task("t_i1071_w2", "plain late success text", exit_code=0)
        assert _task_status("t_i1071_w2") == "timeout_failed"
        assert not any("误杀对账" in r.message for r in caplog.records)
        assert any("skip complete_task" in r.message for r in caplog.records), "无证据迟到上报同样走 skip"


# =====================================================================
# T7 · 普通路径回归（新常量下复刻 #537 关键语义）
# ======================================================================

class TestRegressionNormalPaths:
    def test_pending_self_report_completes_normally(self):
        """普通路径：pending 期间 worker 自报完成 → completed（活跃豁免不影响自报放行）。"""
        from src.runtime.service import complete_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i1071_r1", 10809,
                   pending_since=_iso(_utcnow() - timedelta(seconds=1900)), issue_id="")
        _seed_message("t_i1071_r1", age_seconds=30)
        complete_task("t_i1071_r1", '{"actions":["x"],"conclusion":"done"}', exit_code=0)
        assert _task_status("t_i1071_r1") == "completed"

    def test_timeout_failed_still_rejects_late_update(self):
        """终态幂等回归：timeout_failed 不被 update_task_status 覆盖为 completed。"""
        from src.runtime.service import update_task_status
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0)
        _seed_task("t_i1071_r2", 10810, status="timeout_failed")
        update_task_status("t_i1071_r2", "completed", result_text="迟到完成")
        assert _task_status("t_i1071_r2") == "timeout_failed"
