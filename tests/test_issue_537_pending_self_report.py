"""Issue #537: 停双主时 REMOTE 任务终态误报 failed——pending_self_report 状态机测试。

复现生产事故链路（exec incident case）：
  升级任务（worker-owned REMOTE）执行「停双主」→ master 退出触发
  terminate_active_agents → #404 只跳过留悬空 → 重启窗口 zombie recovery 误判
  failed → worker 侧跑完自报被终态幂等拒绝 → 永败误报。

本文件覆盖：
1. executor：停机 REMOTE → pending_self_report（pending_since 落时刻、busy 不释放、
   事件保持 running、exit_code 不动）；DIRECT 照旧 failed。
2. service：pending → completed/failed 自报放行；timeout_failed 幂等；validate_run_token 分流；
   活跃计数计入 pending；requeue/reap 不回炉 pending。
3. scheduler：pending 超时 → timeout_failed 全链路；zombie recovery 离线宽限；
   _poll_dispatched_completion 放行 pending。
4. deploy_evidence：收口版本指纹 + 健康检查证据校验（非部署 skip / 匹配 pass / 不匹配 fail）。
"""
import os
import sys
import json
import threading
import http.server
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock

RT_DIRECT = 9301
RT_REMOTE = 9302
CRED_REMOTE = 9402
RK = "gitcode:shutdown/test537"

# Issue #537 默认参数（与实现常量对齐；#1071 起实现默认放宽为 5400）
PENDING_TIMEOUT_DEFAULT = 5400
ZOMBIE_OFFLINE_GRACE_DEFAULT = 300


def _iso(dt):
    return dt.isoformat()


def _utcnow():
    return datetime.now(timezone.utc)


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("agent_tasks", "agent_runtimes", "events", "runtime_credentials"):
            try:
                c.execute(f"DELETE FROM {t} WHERE runtime_id IN ({RT_DIRECT},{RT_REMOTE}) "
                          "OR id LIKE 't_i537%' OR repo_key='shutdown/test537'")
            except Exception:
                pass
        try:
            c.execute("DELETE FROM autopilot_executions WHERE id >= 93500 AND id < 93600")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM autopilot_executions WHERE a2a_seq >= 9500 AND a2a_seq < 9600")
        except Exception:
            pass
        db.commit()


def _seed_runtime(rid, cid, busy=1, status="online", last_seen=None, worker_version="",
                  max_concurrent=4):
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
            "busy_slots, max_concurrent, last_seen_at, worker_version) VALUES (?,?,?,?,1,1,?,?,?,?)",
            (rid, f"rt-{rid}", cid, status, busy, max_concurrent,
             last_seen or _iso(_utcnow()), worker_version))
        db.commit()


def _seed_task(tid, rid, seq, status="running", pending_since=None, skill="", prompt="do work",
               env_extra=None, issue_id="#537", started_at=None, event_type="autopilot_event"):
    _env = json.dumps(env_extra or {}, ensure_ascii=False) if env_extra else "{}"
    _started = started_at or _iso(_utcnow())
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, issue_id, "
            "agent_type, event_type, status, prompt, cwd, skill_files, env_extra, event_seq, "
            "skill, started_at, pending_since) VALUES (?,?,?,?,?, 'vod', ?, ?, ?, '', '{}', ?, ?, ?, ?, ?)",
            (tid, "sess-" + tid, rid, RK, issue_id, event_type, status, prompt, _env, seq, skill,
             _started, pending_since))
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, agent_type, status, summary, result) "
            "VALUES (?,?,?,?,?, 'running', 'A2A: → coding', '')",
            (seq, RK, issue_id, "a2a_event", "vod"))
        db.commit()


def _seed_exec(exec_id, status="dispatched", task_id=77, started_at=None):
    # Issue #1546: fallback reverse lookup requires exec.started_at <= task.created_at
    # and the young-grace guard needs an aged execution, so seed a past start time.
    _started = started_at or _iso(_utcnow() - timedelta(hours=1))
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE id=?", (exec_id,))
        c.execute(
            "INSERT INTO autopilot_executions (id, task_id, status, a2a_seq, started_at, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (exec_id, task_id, status, exec_id, _started, _started))
        db.commit()


def _task_row(tid):
    with db_lock:
        r = db.cursor().execute(
            "SELECT status, exit_code, errors, completed_at, pending_since, runtime_id "
            "FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        return r


def _event_row(seq):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, result FROM events WHERE seq=?", (seq,)).fetchone()


def _exec_row(exec_id):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, error FROM autopilot_executions WHERE id=?", (exec_id,)).fetchone()


def _busy(rid):
    with db_lock:
        r = db.cursor().execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        return r[0] if r else None


import pytest


@pytest.fixture(autouse=True)
def _fixture():
    _clean()
    yield
    _clean()


class _Stub503(http.server.BaseHTTPRequestHandler):
    """本地健康检查桩：返回 503（health 可达但非 2xx）。"""
    def do_GET(self):
        self.send_response(503)
        self.end_headers()
        self.wfile.write(b"unavailable")

    def log_message(self, *args):
        pass


# =====================================================================
# S1 · 状态常量 + insert 契约（pending_since 列）
# ======================================================================

class TestS1StatusConstants:
    def test_terminal_statuses_include_timeout_failed(self):
        from src.runtime.service import TASK_TERMINAL_STATUSES
        assert "completed" in TASK_TERMINAL_STATUSES
        assert "failed" in TASK_TERMINAL_STATUSES
        assert "cancelled" in TASK_TERMINAL_STATUSES
        assert "timeout_failed" in TASK_TERMINAL_STATUSES, "timeout_failed 必须是终态"

    def test_active_statuses_include_pending_self_report(self):
        from src.runtime.service import TASK_ACTIVE_STATUSES
        for s in ("queued", "dispatched", "running", "pending_self_report"):
            assert s in TASK_ACTIVE_STATUSES, f"{s} 应为活动态"
        assert "timeout_failed" not in TASK_ACTIVE_STATUSES

    def test_task_columns_include_pending_since(self):
        from src.runtime.service import _TASK_COLUMNS_LIST
        assert "pending_since" in _TASK_COLUMNS_LIST, "agent_tasks 列清单须含 pending_since"

    def test_ddl_status_comment_documents_new_states(self):
        from src.api.db_setup import _AGENT_TASKS_DDL
        assert "pending_self_report" in _AGENT_TASKS_DDL
        assert "timeout_failed" in _AGENT_TASKS_DDL

    def test_migration_ensures_pending_since_column(self):
        """幂等迁移：老库缺列时补 pending_since（对已有表结构生效）。"""
        from src.api.db_setup import init_database
        init_database()
        with db_lock:
            cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(agent_tasks)").fetchall()]
        assert "pending_since" in cols


# =====================================================================
# S2 · executor：停机 REMOTE → pending_self_report
# ======================================================================

class TestS2ExecutorPending:
    def test_remote_task_marked_pending_self_report(self):
        """核心用例：停机时 REMOTE 任务置 pending_self_report + pending_since 落时刻。

        生产事故链路的根因修复点：不再「跳过留悬空」。
        """
        from src.agent.executor import terminate_active_agents
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_remote", RT_REMOTE, 9501)
        terminate_active_agents(grace_seconds=0)
        row = _task_row("t_i537_remote")
        assert row[0] == "pending_self_report", f"REMOTE 任务应置 pending_self_report，实际 {row[0]}"
        assert row[5], "pending_since 应记录等待起点"

    def test_remote_pending_does_not_release_busy_slot(self):
        from src.agent.executor import terminate_active_agents
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_busy", RT_REMOTE, 9502)
        terminate_active_agents(grace_seconds=0)
        assert _busy(RT_REMOTE) == 1, "任务仍占 worker 槽，busy 不得释放"

    def test_remote_event_stays_running(self):
        from src.agent.executor import terminate_active_agents
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_evt", RT_REMOTE, 9503)
        terminate_active_agents(grace_seconds=0)
        ev = _event_row(9503)
        assert ev[0] == "running", "事件不得写终态（等 worker 自报）"

    def test_remote_exit_code_untouched(self):
        from src.agent.executor import terminate_active_agents
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_ec", RT_REMOTE, 9504)
        terminate_active_agents(grace_seconds=0)
        row = _task_row("t_i537_ec")
        assert row[1] is None, "pending 不写 exit_code（终态由 worker 自报决定）"

    def test_direct_task_still_failed(self):
        """回归：DIRECT 任务照旧 force-fail（#404 行为保留）。"""
        from src.agent.executor import terminate_active_agents
        _seed_runtime(RT_DIRECT, 0, busy=1)
        _seed_task("t_i537_direct", RT_DIRECT, 9505)
        terminate_active_agents(grace_seconds=0)
        row = _task_row("t_i537_direct")
        assert row[0] == "failed" and row[1] == -1
        assert _busy(RT_DIRECT) == 0

    def test_no_remote_tasks_no_pending_written(self):
        from src.agent.executor import terminate_active_agents
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0)
        terminate_active_agents(grace_seconds=0)
        # 作用域限定本测试仓库（同库全量跑时其它测试文件可能残留 pending 行）
        with db_lock:
            n = db.cursor().execute(
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE status='pending_self_report' AND repo_key=?", (RK,)).fetchone()[0]
        assert n == 0


# =====================================================================
# S3 · service 状态机放行（pending → 终态；timeout_failed 幂等；活跃计数）
# ======================================================================

class TestS3ServiceStateMachine:
    def test_update_task_status_accepts_timeout_failed(self):
        """pending 超时回收写 timeout_failed：completed_at 落 + busy 释放。"""
        from src.runtime.service import update_task_status
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_tf1", RT_REMOTE, 9510, status="pending_self_report",
                   pending_since=_iso(_utcnow() - timedelta(seconds=PENDING_TIMEOUT_DEFAULT + 60)))
        update_task_status("t_i537_tf1", "timeout_failed",
                           exit_code=-1, errors=json.dumps(["超时"]), completed_at=_iso(_utcnow()))
        row = _task_row("t_i537_tf1")
        assert row[0] == "timeout_failed"
        assert row[3], "timeout_failed 应写 completed_at"
        assert _busy(RT_REMOTE) == 0, "timeout_failed 应释放 busy 槽"

    def test_update_task_status_terminal_guard_blocks_timeout_failed(self):
        """已 timeout_failed 再写 completed 被终态幂等拒绝（worker 迟到上报不得洗掉超时终态）。"""
        from src.runtime.service import update_task_status
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0)
        _seed_task("t_i537_tf2", RT_REMOTE, 9511, status="timeout_failed")
        update_task_status("t_i537_tf2", "completed", result_text="迟到完成")
        row = _task_row("t_i537_tf2")
        assert row[0] == "timeout_failed", "timeout_failed 不允许被 completed 覆盖"

    def test_validate_run_token_pending_ok(self):
        """worker 自报守卫：pending_self_report + started_at 匹配 → ok（不被 stale 拒收）。"""
        from src.runtime.service import validate_run_token, get_task
        _started = _iso(_utcnow())
        _seed_task("t_i537_vtok1", RT_REMOTE, 9512, status="pending_self_report",
                   pending_since=_iso(_utcnow()), started_at=_started)
        task = get_task("t_i537_vtok1")
        assert validate_run_token(task, _started) == "ok"

    def test_validate_run_token_timeout_failed_noop(self):
        """补报守卫：timeout_failed（终态）→ noop，不再 stale。"""
        from src.runtime.service import validate_run_token, get_task
        _seed_task("t_i537_vtok2", RT_REMOTE, 9513, status="timeout_failed")
        task = get_task("t_i537_vtok2")
        assert validate_run_token(task, "whatever") == "noop"

    def test_complete_task_from_pending_full_chain(self):
        """worker 自报完成：pending → completed，事件同步 completed，busy 释放（无 issue 时不下发评论）。"""
        from src.runtime.service import complete_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_comp1", RT_REMOTE, 9514, status="pending_self_report",
                   pending_since=_iso(_utcnow()), issue_id="")
        complete_task("t_i537_comp1", '{"actions":["x"],"conclusion":"done"}', exit_code=0)
        row = _task_row("t_i537_comp1")
        assert row[0] == "completed", f"pending 自报应转 completed，实际 {row[0]}"
        ev = _event_row(9514)
        assert ev[0] == "completed"
        assert _busy(RT_REMOTE) == 0

    def test_complete_task_does_not_override_timeout_failed(self):
        """complete_task 幂等：timeout_failed 不被普通迟到上报覆盖（结构化产出例外路径保留）。"""
        from src.runtime.service import complete_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0)
        _seed_task("t_i537_comp2", RT_REMOTE, 9515, status="timeout_failed", issue_id="")
        complete_task("t_i537_comp2", "late plain text", exit_code=0)
        assert _task_row("t_i537_comp2")[0] == "timeout_failed"

    def test_fail_task_skips_timeout_failed(self):
        """fail_task 幂等：已 timeout_failed 不再覆写为 plain failed。"""
        from src.runtime.service import fail_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0)
        _seed_task("t_i537_ft1", RT_REMOTE, 9516, status="timeout_failed")
        fail_task("t_i537_ft1", "late failure")
        assert _task_row("t_i537_ft1")[0] == "timeout_failed"

    def test_fail_task_from_pending_self_report(self):
        """worker 自报失败：pending → failed 正常转换。"""
        from src.runtime.service import fail_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_ft2", RT_REMOTE, 9517, status="pending_self_report",
                   pending_since=_iso(_utcnow()))
        fail_task("t_i537_ft2", "worker failed", exit_code=2)
        row = _task_row("t_i537_ft2")
        assert row[0] == "failed" and row[1] == 2
        assert _busy(RT_REMOTE) == 0

    def test_real_active_count_includes_pending(self):
        """调度决策真实活跃数：pending_self_report 计入（占槽不泄漏）。"""
        from src.runtime.service import _real_active_count
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0)
        _seed_task("t_i537_ac1", RT_REMOTE, 9518, status="running")
        _seed_task("t_i537_ac2", RT_REMOTE, 9519, status="pending_self_report",
                   pending_since=_iso(_utcnow()))
        with db_lock:
            n = _real_active_count(db.cursor(), RT_REMOTE)
        assert n == 2, f"running+pending 应计数 2，实际 {n}"

    def test_engine_busy_includes_pending(self):
        """local_claimer 满载判定：pending_self_report 计入（max_concurrent=1 时满载）。"""
        from src.runtime.local_claimer import _engine_busy
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, max_concurrent=1)
        _seed_task("t_i537_eb1", RT_REMOTE, 9520, status="pending_self_report",
                   pending_since=_iso(_utcnow()))
        assert _engine_busy(RT_REMOTE) is True, "pending 占槽应判满载"

    def test_requeue_does_not_touch_pending(self):
        """requeue_task 不回收 pending_self_report（等自报，不回炉）。"""
        from src.runtime.service import requeue_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_rq1", RT_REMOTE, 9521, status="pending_self_report",
                   pending_since=_iso(_utcnow()))
        ok = requeue_task("t_i537_rq1")
        row = _task_row("t_i537_rq1")
        assert ok is False and row[0] == "pending_self_report"


# =====================================================================
# S4 · scheduler：pending 超时回收 + zombie 离线宽限 + poll 放行
# ======================================================================

class TestS4PendingTimeoutSweep:
    def test_sweep_marks_timeout_failed_full_chain(self):
        """超时回收全链路：agent_tasks=timeout_failed + events=failed + autopilot=failed + busy 释放。"""
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_sw1", RT_REMOTE, 9522, status="pending_self_report",
                   pending_since=_iso(_utcnow() - timedelta(seconds=PENDING_TIMEOUT_DEFAULT + 60)))
        _seed_exec(9522, status="dispatched")
        n = _sweep_pending_self_report_timeouts()
        assert n >= 1, "应至少回收 1 个超时任务（同库残留达到超时的行为一并回收属正常）"
        row = _task_row("t_i537_sw1")
        assert row[0] == "timeout_failed"
        assert row[3], "timeout_failed 应写 completed_at"
        ev = _event_row(9522)
        assert ev[0] == "failed", "事件应同步 failed"
        assert "超时" in (ev[1] or ""), "事件 result 应带超时说明"
        ex = _exec_row(9522)
        assert ex[0] == "failed", "autopilot_executions 应同步 failed"
        assert _busy(RT_REMOTE) == 0

    def test_sweep_skips_within_timeout(self):
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_sw2", RT_REMOTE, 9523, status="pending_self_report",
                   pending_since=_iso(_utcnow() - timedelta(seconds=60)))
        _sweep_pending_self_report_timeouts()
        assert _task_row("t_i537_sw2")[0] == "pending_self_report"

    def test_sweep_prod_space_format_within_timeout_not_marked(self):
        """生产 executor 写入的 pending_since 为 '%Y-%m-%d %H:%M:%S'（空格分隔）；
        60s 前（窗口内）不得误判超时（字符串比较 bug 回归防护）。"""
        import time as _t
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _space_recent = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime(_t.time() - 60))
        _seed_task("t_i537_sp1", RT_REMOTE, 9538, status="pending_self_report",
                   pending_since=_space_recent)
        _sweep_pending_self_report_timeouts()
        assert _task_row("t_i537_sp1")[0] == "pending_self_report", "窗口内空格格式不得超时"

    def test_sweep_prod_space_format_expired_marked(self):
        """生产空格格式且确实超时（5500s 前，超过 #1071 放宽后的 5400s 窗口）→ 照常判 timeout_failed。"""
        import time as _t
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _space_old = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime(_t.time() - 5500))
        _seed_task("t_i537_sp2", RT_REMOTE, 9539, status="pending_self_report",
                   pending_since=_space_old)
        _sweep_pending_self_report_timeouts()
        assert _task_row("t_i537_sp2")[0] == "timeout_failed", "超时空格格式应判 timeout_failed"

    def test_sweep_skips_other_statuses(self):
        from src.runtime.scheduler import _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_sw3", RT_REMOTE, 9524, status="running")
        _seed_task("t_i537_sw4", RT_REMOTE, 9525, status="completed")
        _sweep_pending_self_report_timeouts()
        assert _task_row("t_i537_sw3")[0] == "running"
        assert _task_row("t_i537_sw4")[0] == "completed"


class TestS4ZombieOfflineGrace:
    def test_zombie_short_offline_not_killed(self):
        """重启窗口短暂离线（<5min）不判死（本轮修复核心之一）。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1, status="offline",
                      last_seen=_iso(_utcnow() - timedelta(seconds=60)))
        _seed_task("t_i537_z1", RT_REMOTE, 9526, status="running")
        _seed_exec(9526, status="running")
        _reap_zombie_executions()
        assert _exec_row(9526)[0] == "running", "短离线不得判 failed"
        assert _task_row("t_i537_z1")[0] == "running"

    def test_zombie_long_offline_failed(self):
        """引擎离线持续 >5min 才判死（照旧收口 failed）。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1, status="offline",
                      last_seen=_iso(_utcnow() - timedelta(seconds=3600)))
        _seed_task("t_i537_z2", RT_REMOTE, 9527, status="running")
        _seed_exec(9527, status="running")
        _reap_zombie_executions()
        ex = _exec_row(9527)
        assert ex[0] == "failed"
        assert _task_row("t_i537_z2")[0] == "failed"

    def test_zombie_no_last_seen_info_not_killed(self):
        """last_seen_at 缺失（信息不足）→ 不判死（宁可多等，不误杀重启窗口）。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1, status="offline", last_seen="")
        _seed_task("t_i537_z3", RT_REMOTE, 9528, status="running")
        _seed_exec(9528, status="running")
        _reap_zombie_executions()
        assert _exec_row(9528)[0] == "running"

    def test_zombie_pending_waits_not_failed(self):
        """pending_self_report：即使引擎离线也不直接判 failed（等自报或 pending 超时兜底）。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1, status="offline",
                      last_seen=_iso(_utcnow() - timedelta(seconds=3600)))
        _seed_task("t_i537_z4", RT_REMOTE, 9529, status="pending_self_report",
                   pending_since=_iso(_utcnow() - timedelta(seconds=120)))
        _seed_exec(9529, status="running")
        _reap_zombie_executions()
        assert _exec_row(9529)[0] == "running"
        assert _task_row("t_i537_z4")[0] == "pending_self_report"

    def test_zombie_completed_sync_success(self):
        """回归：agent_task 已完成 → 僵尸 exec 同步 success（无 evidence 时保持原行为）。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0)
        _seed_task("t_i537_z5", RT_REMOTE, 9530, status="completed", skill="",
                   prompt="do routine")
        _seed_exec(9530, status="running")
        _reap_zombie_executions()
        assert _exec_row(9530)[0] == "success"


class TestS4PollPending:
    def test_poll_marks_exec_running_for_pending(self):
        """poll 轮询：pending_self_report 视同 running（exec 不卡 dispatched 悬空展示）。"""
        import asyncio
        from src.runtime.scheduler import _poll_dispatched_completion
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_p1", RT_REMOTE, 9531, status="pending_self_report",
                   pending_since=_iso(_utcnow()))
        _seed_exec(9531, status="dispatched")

        async def _run():
            t = asyncio.ensure_future(_poll_dispatched_completion(9531, timeout=2, poll_interval=0.02))
            await asyncio.sleep(0.15)
            st = _exec_row(9531)[0]
            t.cancel()
            try:
                await t
            except BaseException:
                pass
            return st

        assert asyncio.run(_run()) == "running"

    def test_poll_flips_failed_on_timeout_failed(self):
        """pending 超时后 poll 应快速收口 failed（不必等自身 3600s 超时）。"""
        import asyncio
        from src.runtime.scheduler import _poll_dispatched_completion, _sweep_pending_self_report_timeouts
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=1)
        _seed_task("t_i537_p2", RT_REMOTE, 9532, status="pending_self_report",
                   pending_since=_iso(_utcnow() - timedelta(seconds=PENDING_TIMEOUT_DEFAULT + 60)))
        _seed_exec(9532, status="dispatched")

        async def _run():
            t = asyncio.ensure_future(_poll_dispatched_completion(9532, timeout=5, poll_interval=0.02))
            await asyncio.sleep(0.1)
            _sweep_pending_self_report_timeouts()
            return await asyncio.wait_for(t, timeout=3)

        result = asyncio.run(_run())
        assert result == "failed"
        assert _exec_row(9532)[0] == "failed"


# =====================================================================
# S5 · deploy_evidence 收口证据校验（版本指纹 + 健康检查）
# ======================================================================

class TestS5DeployEvidence:
    def test_evidence_non_deploy_skip(self):
        from src.runtime.deploy_evidence import check_deploy_evidence
        from src.runtime.service import get_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610301316")
        _seed_task("t_i537_ev1", RT_REMOTE, 9533, status="completed", skill="",
                   prompt="do routine scan")
        res = check_deploy_evidence(get_task("t_i537_ev1"))
        assert res["checked"] is False, "非部署类任务应跳过"
        assert res["passed"] is True

    def test_evidence_version_match_pass(self):
        from src.runtime.deploy_evidence import check_deploy_evidence
        from src.runtime.service import get_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610301316")
        _seed_task("t_i537_ev2", RT_REMOTE, 9534, status="completed", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316"},
                   prompt="升级到目标版本 202610301316")
        res = check_deploy_evidence(get_task("t_i537_ev2"))
        assert res["checked"] is True
        assert res["passed"] is True
        assert res.get("version_ok") is True

    def test_evidence_version_mismatch_fail(self):
        """版本指纹明确不匹配 → 校验不通过（收口转 failed）。"""
        from src.runtime.deploy_evidence import check_deploy_evidence
        from src.runtime.service import get_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610101200")
        _seed_task("t_i537_ev3", RT_REMOTE, 9535, status="completed", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316"},
                   prompt="升级到目标版本 202610301316")
        res = check_deploy_evidence(get_task("t_i537_ev3"))
        assert res["checked"] is True
        assert res["passed"] is False
        assert res.get("version_ok") is False

    def test_evidence_health_unreachable_skip(self):
        """健康检查不可达 → 该维度 skip（None），不阻断（不误杀）。"""
        from src.runtime.deploy_evidence import check_deploy_evidence
        from src.runtime.service import get_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610301316")
        _seed_task("t_i537_ev4", RT_REMOTE, 9536, status="completed", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316", "HEALTH_URL": "http://127.0.0.1:1/nope"},
                   prompt="升级到目标版本 202610301316")
        res = check_deploy_evidence(get_task("t_i537_ev4"))
        assert res["checked"] is True
        assert res["passed"] is True
        assert res.get("health_ok") is None

    def test_evidence_health_non_200_fail(self):
        """健康检查可达但非 2xx → 校验不通过（SSRF 护栏显式放行后仍按明确矛盾判失败）。"""
        from unittest.mock import patch
        from src.runtime.deploy_evidence import check_deploy_evidence
        from src.runtime.service import get_task
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Stub503)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610301316")
            _seed_task("t_i537_ev5", RT_REMOTE, 9537, status="completed", skill="upgrade-master",
                       env_extra={"EXPECTED_VERSION": "202610301316",
                                  "HEALTH_URL": f"http://127.0.0.1:{port}/health"},
                       prompt="升级到目标版本 202610301316")
            # 回环地址默认被 SSRF 护栏 skip；此处显式放行以验证"可达但非 2xx"分支
            with patch("src.runtime.deploy_evidence._is_blocked_target", return_value=False):
                res = check_deploy_evidence(get_task("t_i537_ev5"))
            assert res["checked"] is True
            assert res["passed"] is False
            assert res.get("health_ok") is False
        finally:
            srv.shutdown()

    def test_evidence_result_text_no_false_deploy(self):
        """QA Required：result_text 自由文本含"升级/version"不得误判部署类。"""
        from src.runtime.deploy_evidence import check_deploy_evidence
        from src.runtime.service import get_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610101200")
        _seed_task("t_i537_ev9", RT_REMOTE, 9541, status="completed", skill="",
                   prompt="do routine scan", env_extra={})
        task = dict(get_task("t_i537_ev9"))
        task["result_text"] = "升级依赖 version 2.31.0 已安装完成"
        res = check_deploy_evidence(task)
        assert res["checked"] is False, "result_text 自由文本不得触发部署类判定（QA Required）"
        assert res["passed"] is True

    def test_evidence_text_extracted_version_not_fail(self):
        """QA Required：无 EXPECTED_VERSION 显式预期 → 文本提取版本仅参考，不判版本失败。"""
        from src.runtime.deploy_evidence import check_deploy_evidence
        from src.runtime.service import get_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610101200")
        _seed_task("t_i537_ev10", RT_REMOTE, 9542, status="completed", skill="upgrade-master",
                   prompt="升级到目标版本 202610301316", env_extra={})
        res = check_deploy_evidence(get_task("t_i537_ev10"))
        assert res["checked"] is True
        assert res["version_ok"] is None, "无结构化预期 → 版本维度不得判失败"
        assert res["passed"] is True
        assert res.get("referenced_version") == "202610301316", "文本提取版本仅作诊断参考"

    def test_evidence_health_ssrf_loopback_skip(self):
        """QA Recommended：回环地址健康检查 → skip 且不发起出站请求（SSRF 防护）。"""
        from unittest.mock import patch
        from src.runtime.deploy_evidence import check_deploy_evidence
        from src.runtime.service import get_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610301316")
        _seed_task("t_i537_ev11", RT_REMOTE, 9543, status="completed", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316",
                              "HEALTH_URL": "http://127.0.0.1:8080/health"})
        with patch("src.runtime.deploy_evidence.urllib.request.urlopen") as m:
            res = check_deploy_evidence(get_task("t_i537_ev11"))
            m.assert_not_called()
        assert res["health_ok"] is None
        assert res["passed"] is True

    def test_evidence_health_ssrf_bad_scheme_skip(self):
        """QA Recommended：非 http/https scheme → skip，不发起请求。"""
        from unittest.mock import patch
        from src.runtime.deploy_evidence import check_deploy_evidence
        from src.runtime.service import get_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610301316")
        _seed_task("t_i537_ev15", RT_REMOTE, 9547, status="completed", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316",
                              "HEALTH_URL": "ftp://example.com/health"})
        with patch("src.runtime.deploy_evidence.urllib.request.urlopen") as m:
            res = check_deploy_evidence(get_task("t_i537_ev15"))
            m.assert_not_called()
        assert res["health_ok"] is None
        assert res["passed"] is True

    def test_poll_evidence_gate_mismatch(self):
        """QA Recommended：poll 接入 evidence 校验——版本不匹配 → exec failed 带详情。"""
        import asyncio
        from src.runtime.scheduler import _poll_dispatched_completion
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610101200")
        _seed_task("t_i537_ev12", RT_REMOTE, 9544, status="completed", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316"},
                   prompt="升级到目标版本 202610301316")
        _seed_exec(9544, status="dispatched")

        async def _run():
            t = asyncio.ensure_future(_poll_dispatched_completion(9544, timeout=3, poll_interval=0.02))
            await asyncio.sleep(0.3)
            if t.done():
                return t.result()
            t.cancel()
            try:
                await t
            except BaseException:
                pass
            return "not-done"

        assert asyncio.run(_run()) == "failed"
        ex = _exec_row(9544)
        assert ex[0] == "failed"
        assert "版本" in (ex[1] or ""), "error 应带 evidence 详情"

    def test_poll_evidence_gate_pass(self):
        """QA Recommended：poll 接入 evidence 校验——版本匹配 → exec success。"""
        import asyncio
        from src.runtime.scheduler import _poll_dispatched_completion
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610301316")
        _seed_task("t_i537_ev13", RT_REMOTE, 9545, status="completed", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316"},
                   prompt="升级到目标版本 202610301316")
        _seed_exec(9545, status="dispatched")

        async def _run():
            t = asyncio.ensure_future(_poll_dispatched_completion(9545, timeout=3, poll_interval=0.02))
            await asyncio.sleep(0.3)
            if t.done():
                return t.result()
            t.cancel()
            try:
                await t
            except BaseException:
                pass
            return "not-done"

        assert asyncio.run(_run()) == "success"
        assert _exec_row(9545)[0] == "success"

    def test_zombie_evidence_gate_mismatch(self):
        """QA Recommended：zombie recovery 接入 evidence 校验——版本不匹配 → exec failed。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610101200")
        _seed_task("t_i537_ev14", RT_REMOTE, 9546, status="completed", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316"},
                   prompt="升级到目标版本 202610301316")
        _seed_exec(9546, status="running")
        _reap_zombie_executions()
        ex = _exec_row(9546)
        assert ex[0] == "failed"
        assert "版本" in (ex[1] or ""), "error 应带 evidence 详情"

    def test_zombie_evidence_gate_pass(self):
        """QA Recommended：zombie recovery 接入 evidence 校验——版本匹配 → exec success。"""
        from src.runtime.scheduler import _reap_zombie_executions
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610301316")
        _seed_task("t_i537_ev16", RT_REMOTE, 9548, status="completed", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316"},
                   prompt="升级到目标版本 202610301316")
        _seed_exec(9548, status="running")
        _reap_zombie_executions()
        ex = _exec_row(9548)
        assert ex[0] == "success"

    def test_finalize_autopilot_evidence_gate_mismatch(self):
        """收口接入：成功分支前置 evidence 校验，版本不匹配 → exec failed 带详情。"""
        from src.runtime.scheduler import _finalize_autopilot_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610101200")
        _seed_task("t_i537_ev6", RT_REMOTE, 9538, status="running", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316"},
                   prompt="升级到目标版本 202610301316")
        _seed_exec(9538, status="dispatched")
        _finalize_autopilot_task("t_i537_ev6", 9538, 0,
                                 '{"summary":"done","findings":[]}')
        ex = _exec_row(9538)
        assert ex[0] == "failed", "evidence 不通过 → 收口 failed"
        assert "版本" in (ex[1] or ""), "error 应带 evidence 详情"

    def test_finalize_autopilot_evidence_gate_pass(self):
        from src.runtime.scheduler import _finalize_autopilot_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610301316")
        _seed_task("t_i537_ev7", RT_REMOTE, 9539, status="running", skill="upgrade-master",
                   env_extra={"EXPECTED_VERSION": "202610301316"},
                   prompt="升级到目标版本 202610301316")
        _seed_exec(9539, status="dispatched")
        _finalize_autopilot_task("t_i537_ev7", 9539, 0,
                                 '{"summary":"done","findings":[]}')
        assert _exec_row(9539)[0] == "success"

    def test_finalize_autopilot_non_deploy_unchanged(self):
        """非部署类任务不校验，成功收口保持原行为。"""
        from src.runtime.scheduler import _finalize_autopilot_task
        _seed_runtime(RT_REMOTE, CRED_REMOTE, busy=0, worker_version="202610301316")
        _seed_task("t_i537_ev8", RT_REMOTE, 9540, status="running", skill="",
                   prompt="do routine scan")
        _seed_exec(9540, status="dispatched")
        _finalize_autopilot_task("t_i537_ev8", 9540, 0,
                                 '{"summary":"done","findings":[]}')
        assert _exec_row(9540)[0] == "success"