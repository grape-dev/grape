"""Issue #3800: 绑定引擎永久离线 → ENGINE_OFFLINE 退避 + 同因熔断 + 一次性结构化告警。

覆盖验收（Spec/AC）：
- 环境性失败分类：引擎健康过滤为空 → `error_code=ENGINE_OFFLINE`（uid 池 / legacy id 池同构）；
  绑定 uid 在 `agent_runtimes` 查无此行 → `error_code=BINDING_STALE`（重绑指引）；
- 退避：`_compute_backoff_next_run` 走 60/120/240/480/960 → 封顶 1800s，`backoff_until` 落库；
- 熔断：连续 3 次同因 + 离线 ≥ 5min → `circuit_paused=1`，同因告警恰好 1 次；
- 假时钟 1 小时：60s cron + 引擎持续离线 → 新建 exec ≤ 4 条（修复前 60 条）；
- 自愈：引擎恢复健康 → 自动解除暂停、`consecutive_failures` 归零并续跑。

全部走 sqlite 测试库（tests/conftest.py 初始化），远端执行全部打桩，不连生产库、不起 agent。
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock
from src.domains.mgmt import public as mgmt_public
from src.domains.runtime.engine import scheduler as S

RK = "gitcode:autopilot/test3800"
TID_BASE = 3800001
TID_OFFLINE = TID_BASE          # 1h 退避 + 熔断 + 自愈
TID_STALE = TID_BASE + 1        # 绑定 uid 悬空（BINDING_STALE）
TID_IDS = TID_BASE + 2          # legacy id 池
CLOCK_START = datetime(2026, 11, 23, 3, 5, 0, tzinfo=timezone(timedelta(hours=8)))


class FakeClock:
    """假时钟（只驱动调度器时钟 seam，不碰系统时间）。"""

    def __init__(self, start=CLOCK_START):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


# ---------------------------------------------------------------- 夹具与播种


def _clean():
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM autopilot_executions WHERE task_id BETWEEN ? AND ?",
                      (TID_BASE, TID_BASE + 99))
        except Exception:
            pass
        try:
            c.execute("DELETE FROM autopilot_tasks WHERE id BETWEEN ? AND ?",
                      (TID_BASE, TID_BASE + 99))
        except Exception:
            pass
        try:
            c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'i3800%'")
        except Exception:
            pass
        db.commit()


def _mk_engine(name, engine_uid, healthy):
    """本机直连引擎行（credential_id=0 → 健康判定只取 installed+healthy，不依赖真实时钟）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, "
            "installed, last_seen_at, engine_uid, scope, owner_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, name, 0, "online", 1 if healthy else 0, 1, "", engine_uid, "global", ""))
        rid = c.lastrowid
        db.commit()
        return rid


def _set_engine_healthy(rid, healthy):
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET healthy=? WHERE id=?", (1 if healthy else 0, rid))
        db.commit()


def _mk_task(tid, uids=None, ids=None, cron="*/1 * * * *", max_retry=0):
    now_str = CLOCK_START.isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO autopilot_tasks (id, name, repo_key, agent_type, instruction, skill_name, "
            "cron, max_retry, enabled, last_run_at, next_run_at, created_at, updated_at, created_by, "
            "engine_runtime_ids, custom_fields, work_branch, engine_runtime_uids) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(tid), f"i3800-task-{tid}", RK, "issue", "巡检仓库并输出报告", "", cron,
             int(max_retry), 1, "", "", now_str, now_str, 0,
             json.dumps(ids) if ids else "", "", "",
             json.dumps(uids) if uids else ""))
        db.commit()


def _task_row(tid):
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT consecutive_failures, last_error_code, offline_since, backoff_until, "
            "circuit_paused, next_run_at FROM autopilot_tasks WHERE id=?", (int(tid),)).fetchone()


def _exec_count(tid):
    with db_lock:
        c = db.cursor()
        return int(c.execute("SELECT COUNT(*) FROM autopilot_executions WHERE task_id=?",
                             (int(tid),)).fetchone()[0] or 0)


def _exec_rows(tid):
    with db_lock:
        c = db.cursor()
        return list(c.execute(
            "SELECT id, status, error, error_code FROM autopilot_executions "
            "WHERE task_id=? ORDER BY id", (int(tid),)).fetchall())


def _fake_agent_offline(calls):
    """模拟 REMOTE 派发遇到「绑定引擎全灭」：写 failed + error_code 后返回。"""

    def _fake(repo_key, task, exec_id):
        calls.append(exec_id)
        probe = S._probe_task_binding(task, repo_key)
        code, msg = S._binding_failure_code(probe)
        assert code == S.ERROR_ENGINE_OFFLINE, (code, msg, probe)
        S._update_execution_result(exec_id, "failed", error=msg, error_code=code)

    return _fake


def _fake_agent_stale(calls):
    def _fake(repo_key, task, exec_id):
        calls.append(exec_id)
        probe = S._probe_task_binding(task, repo_key)
        code, msg = S._binding_failure_code(probe)
        assert code == S.ERROR_BINDING_STALE, (code, msg, probe)
        S._update_execution_result(exec_id, "failed", error=msg, error_code=code)

    return _fake


# ---------------------------------------------------------------- 纯函数 seam


def test_compute_backoff_sequence_caps_at_30min():
    """退避序列 60/120/240/480/960 → 封顶 1800s（60s cron 周期为基数）。"""
    clock = FakeClock()
    delays = []
    for n in range(1, 8):
        nxt = S._compute_backoff_next_run("*/1 * * * *", n, clock())
        delays.append(int(round((datetime.fromisoformat(nxt) - clock()).total_seconds())))
    assert delays[:5] == [60, 120, 240, 480, 960], delays
    assert delays[5] == 1800 and delays[6] == 1800, delays
    assert S.ERROR_ENGINE_OFFLINE == "ENGINE_OFFLINE"
    assert S.ERROR_BINDING_STALE == "BINDING_STALE"


def test_probe_task_binding_three_states():
    """绑定探测三态：无绑定 / 悬空（查无此行）/ 存在但全离线。"""
    _clean()
    rid = _mk_engine("i3800_probe", "i3800-probe-uid", healthy=False)
    _mk_task(TID_STALE, uids=["i3800-missing-uid"])
    _mk_task(TID_IDS, ids=[rid])

    no_bind = S._probe_task_binding({"id": 0, "repo_key": RK})
    assert no_bind["mode"] == "" and S._binding_failure_code(no_bind) == ("", "")

    stale = S._probe_task_binding({"id": TID_STALE, "repo_key": RK,
                                   "engine_runtime_uids": json.dumps(["i3800-missing-uid"])})
    assert stale["mode"] == "uid" and stale["members"] == []
    code, msg = S._binding_failure_code(stale)
    assert code == S.ERROR_BINDING_STALE, (code, msg)
    assert "not found" in msg.lower() or "rebind" in msg.lower(), msg

    offline = S._probe_task_binding({"id": TID_IDS, "repo_key": RK,
                                     "engine_runtime_ids": json.dumps([rid])})
    assert offline["mode"] == "ids" and [m["runtime_id"] for m in offline["members"]] == [rid]
    code, msg = S._binding_failure_code(offline)
    assert code == S.ERROR_ENGINE_OFFLINE, (code, msg)
    assert "all offline (Issue #259)" in msg, msg
    _clean()


# ---------------------------------------------------------------- 派发侧写入 error_code


def test_remote_dispatch_marks_engine_offline(monkeypatch):
    """uid 池健康过滤为空 → exec 落 error_code=ENGINE_OFFLINE（不再只写一句中文）。"""
    _clean()
    rid = _mk_engine("i3800_dispatch", "i3800-dispatch-uid", healthy=False)
    _mk_task(TID_IDS, uids=["i3800-dispatch-uid"])
    import src.api.session_store as store
    monkeypatch.setattr(store, "get_or_create_agent_session",
                        lambda *a, **k: {"id": "sess-i3800"})
    monkeypatch.setattr(store, "get_or_create_issue_session",
                        lambda *a, **k: {"id": ""})
    task = next(t for t in S._get_enabled_tasks() if t["id"] == TID_IDS)
    exec_id = S._create_execution(TID_IDS, "issue")
    S._execute_autopilot_remote(
        RK, task, exec_id, "issue", "", 0, "", "", "/tmp/i3800-ws", "prompt", "", "", "",
        "autopilot/test3800", {"repo_key": RK, "issue_id": "", "payload": {}})
    rows = _exec_rows(TID_IDS)
    target = [r for r in rows if r[0] == exec_id][0]
    assert target[1] == "failed", target
    assert target[3] == S.ERROR_ENGINE_OFFLINE, target
    assert "all offline (Issue #259)" in (target[2] or ""), target
    _set_engine_healthy(rid, True)
    _clean()


# ---------------------------------------------------------------- 假时钟 1h 回归


def test_engine_offline_hour_backoff_and_circuit(monkeypatch):
    """60s cron + 引擎持续离线 1 小时：exec ≤ 4 条、退避序列可见、告警恰好 1 次。"""
    _clean()
    rid = _mk_engine("i3800_offline", "i3800-offline-uid", healthy=False)
    _mk_task(TID_OFFLINE, uids=["i3800-offline-uid"])
    clock = FakeClock()
    monkeypatch.setattr(S, "_scheduler_now", clock)
    calls = []
    monkeypatch.setattr(S, "_execute_autopilot_agent", _fake_agent_offline(calls))
    alerts = []
    monkeypatch.setattr(S, "_notify_subscribers",
                        lambda task_id, exec_id, status, summary, findings:
                        alerts.append((task_id, exec_id, status, summary)))

    decisions = []

    def _tick():
        tasks = {t["id"]: t for t in S._get_enabled_tasks()}
        task = tasks.get(TID_OFFLINE)
        assert task is not None
        row = mgmt_public.autopilot_task_last_run_any(TID_OFFLINE)
        decision = S._scan_task_once(task, row[0] if row else "")
        decisions.append(decision)
        if decision == "dispatch":
            asyncio.run(S._execute_task(task))
        return decision

    _tick()                                  # t0：首次派发
    delays = []
    for _ in range(60):                      # 跑满 1 小时（60 个 60s tick）
        clock.advance(60)
        before = _exec_count(TID_OFFLINE)
        if _tick() == "dispatch":
            # 本次派发对应的退避时长（next_run_at - now）
            nxt = _task_row(TID_OFFLINE)[5]
            delays.append(int(round((datetime.fromisoformat(nxt) - clock()).total_seconds())))

    assert _exec_count(TID_OFFLINE) <= 4, (
        f"1 小时新建 exec {_exec_count(TID_OFFLINE)} 条（修复前 60 条），退避序列={delays}")
    assert [len(calls)] and _exec_count(TID_OFFLINE) == len(calls), (calls, _exec_count(TID_OFFLINE))
    assert delays == [120, 240] or delays == [120, 240, 480] or delays == [], delays
    state = _task_row(TID_OFFLINE)
    consecutive, code, offline_since, backoff_until, paused, next_run = state
    assert code == S.ERROR_ENGINE_OFFLINE, state
    assert consecutive >= 3, state
    assert int(paused) == 1, state
    assert offline_since and backoff_until, state
    assert len(alerts) == 1, f"同因告警应恰好 1 次，实际 {len(alerts)} 次: {alerts}"
    task_id, exec_id, status, summary = alerts[0]
    assert task_id == TID_OFFLINE and status == "failed"
    assert S.ERROR_ENGINE_OFFLINE in summary, summary
    assert "engine" in summary.lower(), summary
    # 熔断后不再派发（剩余 tick 全被暂停闸门拦住）
    assert decisions.count("dispatch") == len(calls), (decisions, calls)
    assert "paused" in decisions, decisions[-5:]

    # 引擎恢复健康 → 自动解除暂停并续跑，失败计数归零
    _set_engine_healthy(rid, True)

    def _fake_agent_success(repo_key, task, exec_id):
        calls.append(exec_id)
        S._update_execution_result(exec_id, "success", result="ok")

    monkeypatch.setattr(S, "_execute_autopilot_agent", _fake_agent_success)
    clock.advance(60)
    assert _tick() == "dispatch", "引擎恢复后应在下一 tick 自动续跑"
    assert int(_task_row(TID_OFFLINE)[4]) == 0, _task_row(TID_OFFLINE)
    assert int(_task_row(TID_OFFLINE)[0]) == 0, _task_row(TID_OFFLINE)
    _clean()


def test_binding_stale_alerts_on_first_dispatch(monkeypatch):
    """绑定 uid 在 agent_runtimes 中不存在：首派即告警（重绑指引）+ 进退避通道。"""
    _clean()
    _mk_task(TID_STALE, uids=["i3800-ghost-uid"])
    clock = FakeClock()
    monkeypatch.setattr(S, "_scheduler_now", clock)
    calls = []
    monkeypatch.setattr(S, "_execute_autopilot_agent", _fake_agent_stale(calls))
    alerts = []
    monkeypatch.setattr(S, "_notify_subscribers",
                        lambda task_id, exec_id, status, summary, findings:
                        alerts.append(summary))

    tasks = {t["id"]: t for t in S._get_enabled_tasks()}
    row = mgmt_public.autopilot_task_last_run_any(TID_STALE)
    assert S._scan_task_once(tasks[TID_STALE], row[0] if row else "") == "dispatch"
    asyncio.run(S._execute_task(tasks[TID_STALE]))

    assert _exec_count(TID_STALE) == 1, "首派只允许建 1 条 exec"
    assert len(alerts) == 1, alerts
    assert S.ERROR_BINDING_STALE in alerts[0], alerts
    assert "重新绑定" in alerts[0] or "rebind" in alerts[0].lower(), alerts
    consecutive, code, offline_since, backoff_until, paused, next_run = _task_row(TID_STALE)
    assert code == S.ERROR_BINDING_STALE and consecutive == 1, (code, consecutive)
    assert backoff_until, "BINDING_STALE 也要进退避通道"

    # 退避窗口内不再建 exec
    clock.advance(30)
    tasks = {t["id"]: t for t in S._get_enabled_tasks()}
    row = mgmt_public.autopilot_task_last_run_any(TID_STALE)
    assert S._scan_task_once(tasks[TID_STALE], row[0] if row else "") == "backoff"
    assert _exec_count(TID_STALE) == 1
    _clean()


# ---------------------------------------------------------------- DDL / 迁移


def test_failure_state_columns_and_migrations_registered():
    """两表新列在位 + 两条登记式迁移可按版本号查到（存量库会自动补列）。"""
    with db_lock:
        c = db.cursor()
        task_cols = [r[1] for r in c.execute("PRAGMA table_info(autopilot_tasks)").fetchall()]
        exec_cols = [r[1] for r in c.execute("PRAGMA table_info(autopilot_executions)").fetchall()]
    for col in ("consecutive_failures", "last_error_code", "offline_since",
                "backoff_until", "circuit_paused"):
        assert col in task_cols, (col, task_cols)
    assert "error_code" in exec_cols, exec_cols

    from src.api.db_setup import _MIGRATIONS
    versions = {m.version for m in _MIGRATIONS}
    assert "202611230003" in versions, "autopilot_tasks 失败状态列迁移未登记"
    assert "202611230004" in versions, "autopilot_executions.error_code 迁移未登记"
    by_version = {m.version: m for m in _MIGRATIONS}
    assert "mgmt" in tuple(by_version["202611230003"].domains or ()), by_version["202611230003"].domains
    assert "runtime" in tuple(by_version["202611230004"].domains or ()), by_version["202611230004"].domains