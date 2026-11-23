"""Issue #3963：同仓 enqueue 撞 repo-dispatch 租约 → 排队等待，busy 归为瞬态不判死。

覆盖验收（Spec/AC）：
- `acquire_lock_with_retry` 支持总等待预算 `wait_seconds`，默认（None）路径 `attempts×interval` 语义不变；
- enqueue 调用点的有效等待预算 ≥ 租约 TTL；预算耗尽抛结构化 `REPO_DISPATCH_BUSY`
  （文案保留 `task enqueue busy for repo` 供既有 Loki 查询）；
- 同仓 5 并发 enqueue（其一持锁 2s）→ 其余 4 个 ≤TTL 全部入队，0 次 busy 失败；
- busy 不消耗 attempt：`max_retry=0` 的任务遇 busy 仍成功派发；永久错误仍按原语义落 failed；
- busy 预算耗尽 → requeue 下一 tick（不置 failed、不推进失败计数/退避熔断、日志无 `failed after 0 retries`）；
- 指标/告警：`grape_repo_dispatch_busy_total{repo}`、`grape_repo_dispatch_lease_wait_seconds`、
  `deploy/alerts.yml` 连续 busy 告警。

全部走 sqlite 测试库（tests/conftest.py 初始化），持锁/释放用真实 `distributed_locks`，
调度侧执行全部打桩，不连生产库、不起 agent。
"""
import asyncio
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock
from src.kernel import lock as L
from src.domains.runtime.engine import service
from src.domains.runtime.engine import scheduler as S

RK = "gitcode:issue3963/lease"
LEASE_KEY = L.repo_dispatch_lease_key(RK)
BUSY = "REPO_DISPATCH_BUSY"


# ---------------------------------------------------------------- 夹具


def _clean_tasks():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", ("issue3963/repo",))
        c.execute("DELETE FROM distributed_locks WHERE lock_key=?", (LEASE_KEY,))
        db.commit()


def _enqueue(seq=1, repo_key=RK):
    return service.enqueue_task(
        agent_session_id="sess-i3963", runtime_id=1, prompt="p", event_seq=seq,
        issue_id="#3963", repo_key=repo_key, agent_type="issue", skill="issue",
    )


def _task_count(repo_key=RK):
    with db_lock:
        return int(db.cursor().execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE repo_key=?", (repo_key,)).fetchone()[0] or 0)


def _busy_counter_value(repo_key=RK):
    from src.domains.insight.observability import REPO_DISPATCH_BUSY
    return REPO_DISPATCH_BUSY.labels(repo=repo_key)._value.get()


def _count_histogram_samples(repo_key=RK):
    from src.domains.insight.observability import REPO_DISPATCH_LEASE_WAIT
    return REPO_DISPATCH_LEASE_WAIT.labels(repo=repo_key)._sum.get()


@pytest.fixture(autouse=True)
def _clean():
    _clean_tasks()
    yield
    _clean_tasks()


# ---------------------------------------------------------------- 切片 1：抢锁等待预算


def test_wait_seconds_budget_waits_for_holder_and_succeeds():
    """给定总预算时按需等待持锁者，抢到立刻返回（不是空转耗尽预算）。"""
    assert L.acquire_lock(LEASE_KEY, ttl=30) is True
    holder = threading.Thread(target=lambda: (time.sleep(0.6), L.release_lock(LEASE_KEY)))
    holder.start()
    t0 = time.monotonic()
    ok = L.acquire_lock_with_retry(LEASE_KEY, ttl=30, wait_seconds=5.0)
    elapsed = time.monotonic() - t0
    holder.join(5)
    try:
        assert ok is True
        assert elapsed >= 0.5, f"应在预算内等待持锁者释放，实际仅 {elapsed:.3f}s"
        assert elapsed < 4.0, f"拿到锁应立刻返回，实际 {elapsed:.3f}s"
    finally:
        L.release_lock(LEASE_KEY)


def test_wait_seconds_budget_exhausted_returns_false():
    """预算内抢不到 → 返回 False（调用方据此判 busy），耗时应贴近预算。"""
    assert L.acquire_lock(LEASE_KEY, ttl=30) is True
    t0 = time.monotonic()
    ok = L.acquire_lock_with_retry(LEASE_KEY, ttl=30, wait_seconds=0.4)
    elapsed = time.monotonic() - t0
    L.release_lock(LEASE_KEY)
    assert ok is False
    assert elapsed >= 0.35, f"应按预算等待后放弃，实际 {elapsed:.3f}s"
    assert elapsed < 3.0, elapsed


def test_default_path_keeps_legacy_attempts_interval_semantics(monkeypatch):
    """未传 wait_seconds 的调用点行为不变：attempts 次尝试、attempts-1 次间隔睡眠。"""
    calls = []

    def _never(key, ttl=60, holder=""):
        calls.append(key)
        return False

    monkeypatch.setattr(L, "acquire_lock", _never)
    t0 = time.monotonic()
    assert L.acquire_lock_with_retry("k", ttl=1, attempts=3, interval=0.02) is False
    elapsed = time.monotonic() - t0
    assert len(calls) == 3, f"默认路径应恰好尝试 attempts 次，实际 {len(calls)}"
    assert 0.03 <= elapsed < 1.0, f"默认路径按 interval 间隔睡眠，实际 {elapsed:.3f}s"


def test_budget_path_ends_with_final_attempt(monkeypatch):
    """给定预算时末尾补一次尝试（预算耗尽的边界重试），attempts 参数不再约束次数。"""
    calls = []

    def _never(key, ttl=60, holder=""):
        calls.append(key)
        return False

    monkeypatch.setattr(L, "acquire_lock", _never)
    assert L.acquire_lock_with_retry("k2", ttl=1, attempts=1, interval=0.05,
                                     wait_seconds=0.12) is False
    assert len(calls) >= 3, f"预算期内应多次重试 + 末尾补一次，实际 {len(calls)}"


# ---------------------------------------------------------------- 切片 2：enqueue 排队等待 + 结构化 busy


def test_enqueue_lease_budget_covers_ttl():
    """AC：enqueue 调用点的有效等待预算 ≥ 租约 TTL。"""
    assert service._REPO_DISPATCH_LEASE_TTL >= 30
    assert service._REPO_DISPATCH_LEASE_WAIT_SECONDS >= service._REPO_DISPATCH_LEASE_TTL


def test_enqueue_passes_budget_to_lock_primitive(monkeypatch):
    """调用点把预算/抖动传给原语（不是只改常量不接线）。"""
    seen = {}

    def _spy(key, ttl=60, attempts=20, interval=0.05, wait_seconds=None, jitter_ratio=0.0):
        seen.update(key=key, ttl=ttl, wait_seconds=wait_seconds, jitter_ratio=jitter_ratio)
        return True

    monkeypatch.setattr(service, "acquire_lock_with_retry", _spy)
    _enqueue(seq=11)
    assert seen["key"] == LEASE_KEY
    assert seen["wait_seconds"] >= seen["ttl"], seen
    assert seen["ttl"] == service._REPO_DISPATCH_LEASE_TTL


def test_enqueue_waits_for_two_second_holder_then_succeeds():
    """同仓 enqueue 遇到持锁 2s → 等锁后正常入队（修复前 0.95s 即抛 busy）。"""
    assert L.acquire_lock(LEASE_KEY, ttl=30) is True
    holder = threading.Thread(target=lambda: (time.sleep(2.0), L.release_lock(LEASE_KEY)))
    holder.start()
    t0 = time.monotonic()
    tid = _enqueue(seq=12)
    elapsed = time.monotonic() - t0
    holder.join(5)
    assert tid
    assert elapsed >= 1.5, f"应等待持锁者，实际 {elapsed:.3f}s"
    assert _task_count() == 1


def test_enqueue_busy_raises_structured_error_and_counts_metric(monkeypatch):
    """预算耗尽 → 结构化 REPO_DISPATCH_BUSY 异常，文案兼容旧 Loki 查询，busy_total 自增。"""
    monkeypatch.setattr(service, "_REPO_DISPATCH_LEASE_WAIT_SECONDS", 0.2)
    before = _busy_counter_value()
    assert L.acquire_lock(LEASE_KEY, ttl=30) is True
    try:
        with pytest.raises(service.RepoDispatchBusyError) as ei:
            _enqueue(seq=13)
    finally:
        L.release_lock(LEASE_KEY)
    err = ei.value
    assert err.error_code == BUSY, err.error_code
    assert f"task enqueue busy for repo {RK}" in str(err), str(err)
    assert isinstance(err, RuntimeError), "既有调用方按 RuntimeError 捕获，必须保持兼容"
    assert _busy_counter_value() == before + 1
    assert _task_count() == 0


def test_lease_wait_histogram_observed():
    """抢锁耗时进直方图（repo 维度）。"""
    before = _count_histogram_samples()
    _enqueue(seq=14)
    assert _count_histogram_samples() > before, "repo_dispatch_lease_wait_seconds 未观测到样本"


def test_five_concurrent_enqueues_one_holds_lease_two_seconds():
    """AC：同仓 5 并发（其一持锁 2s）→ 其余 4 个 ≤TTL 全部入队，0 次 busy 失败。"""
    assert L.acquire_lock(LEASE_KEY, ttl=30) is True
    results, errors = [], []

    def _worker(seq):
        try:
            results.append(_enqueue(seq=seq))
        except Exception as exc:  # noqa: BLE001 - 测试要看到任何 busy 失败
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(100 + i,)) for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(2.0)
    L.release_lock(LEASE_KEY)
    for t in threads:
        t.join(60)

    assert errors == [], f"并发入队不应出现 busy 失败：{errors}"
    assert len(results) == 4 and all(results), results
    assert _task_count() == 4


# ---------------------------------------------------------------- 切片 3：调度侧瞬态通道

TID_BASE = 3963001
CLOCK_START = datetime(2026, 11, 23, 3, 5, 30, tzinfo=timezone(timedelta(hours=8)))


def _mk_task(tid, cron="*/1 * * * *", max_retry=0):
    now_str = CLOCK_START.isoformat()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE task_id=?", (int(tid),))
        c.execute("DELETE FROM autopilot_tasks WHERE id=?", (int(tid),))
        c.execute(
            "INSERT INTO autopilot_tasks (id, name, repo_key, agent_type, instruction, skill_name, "
            "cron, max_retry, enabled, last_run_at, next_run_at, created_at, updated_at, created_by, "
            "engine_runtime_ids, custom_fields, work_branch, engine_runtime_uids) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(tid), f"i3963-task-{tid}", RK, "issue", "巡检仓库并输出报告", "", cron,
             int(max_retry), 1, "", "", now_str, now_str, 0, "", "", "", ""))
        db.commit()


def _drop_task(tid):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM autopilot_executions WHERE task_id=?", (int(tid),))
        c.execute("DELETE FROM autopilot_tasks WHERE id=?", (int(tid),))
        db.commit()


def _task_row(tid):
    with db_lock:
        return db.cursor().execute(
            "SELECT consecutive_failures, last_error_code, backoff_until, circuit_paused, next_run_at "
            "FROM autopilot_tasks WHERE id=?", (int(tid),)).fetchone()


def _exec_rows(tid):
    with db_lock:
        return list(db.cursor().execute(
            "SELECT id, status, error, error_code FROM autopilot_executions "
            "WHERE task_id=? ORDER BY id", (int(tid),)).fetchall())


def _task_dict(tid):
    return next(t for t in S._get_enabled_tasks() if t["id"] == tid)


def _fake_agent_busy_then(calls, busy_times, then="success", fail_error=""):
    """模拟远端派发：前 busy_times 次入队撞锁（skipped + busy 分类），之后成功/失败。"""

    def _fake(repo_key, task, exec_id):
        calls.append(exec_id)
        if len(calls) <= busy_times:
            S._update_execution_result(
                exec_id, "skipped",
                error=f"入队异常: task enqueue busy for repo {repo_key}",
                error_code=S.ERROR_REPO_DISPATCH_BUSY)
        elif then == "success":
            S._update_execution_result(exec_id, "success", result="ok")
        else:
            S._update_execution_result(exec_id, "failed", error=fail_error)

    return _fake


def test_busy_is_not_env_failure_code():
    """busy 不得复用环境性失败通道（60s 退避 + 连续 3 次熔断会把秒级争用升级成任务停用）。"""
    assert S.ERROR_REPO_DISPATCH_BUSY == BUSY
    assert BUSY not in S._ENV_FAILURE_CODES
    assert S._BUSY_RETRY_BUDGET >= 1


def test_classify_enqueue_error_from_structured_exception():
    """入队异常分类：结构化 busy → REPO_DISPATCH_BUSY；其它异常不误分类。"""
    assert S._enqueue_failure_code(service.RepoDispatchBusyError(RK)) == BUSY
    assert S._enqueue_failure_code(RuntimeError(f"task enqueue busy for repo {RK}")) == BUSY
    assert S._enqueue_failure_code(RuntimeError("boom")) == ""


def test_classify_env_error_recovers_busy_from_fingerprint():
    """存量库缺 error_code 列时按文案指纹回推 busy（与 #3827 同机制）。"""
    assert S._classify_env_error("", f"入队异常: task enqueue busy for repo {RK}") == BUSY
    assert S._classify_env_error("", "入队异常: other") == ""


def test_remote_dispatch_writes_busy_error_code(monkeypatch):
    """AC：入队撞锁时执行记录落 error_code=REPO_DISPATCH_BUSY，且状态是 skipped（非 failed）。"""
    _mk_task(TID_BASE + 3, max_retry=0)
    import src.api.session_store as store
    monkeypatch.setattr(store, "get_or_create_agent_session", lambda *a, **k: {"id": "sess-i3963"})
    monkeypatch.setattr(store, "get_or_create_issue_session", lambda *a, **k: {"id": ""})

    def _raise_busy(**kwargs):
        raise service.RepoDispatchBusyError(RK, waited_seconds=35.0)

    monkeypatch.setattr(service, "enqueue_task", _raise_busy)
    task = _task_dict(TID_BASE + 3)
    exec_id = S._create_execution(TID_BASE + 3, "issue")
    S._execute_autopilot_remote(
        RK, task, exec_id, "issue", "", 0, "", "", "/tmp/i3963-ws", "prompt", "", "", "",
        "issue3963/repo", {"repo_key": RK, "issue_id": "", "payload": {}})
    row = [r for r in _exec_rows(TID_BASE + 3) if r[0] == exec_id][0]
    assert row[1] == "skipped", row
    assert row[3] == BUSY, row
    assert "task enqueue busy" in (row[2] or ""), row
    _drop_task(TID_BASE + 3)


def test_busy_does_not_consume_attempt_with_max_retry_zero(monkeypatch):
    """AC：max_retry=0 的任务遇 busy 仍成功派发（busy 走独立预算，不吃掉唯一 attempt）。"""
    _mk_task(TID_BASE, max_retry=0)
    monkeypatch.setattr(S, "_BUSY_RETRY_DELAYS", (0.01, 0.01, 0.01))
    calls = []
    monkeypatch.setattr(S, "_execute_autopilot_agent", _fake_agent_busy_then(calls, 2, "success"))
    task = _task_dict(TID_BASE)
    asyncio.run(S._execute_task(task))

    rows = _exec_rows(TID_BASE)
    assert len(rows) == 3, f"2 次 busy 重试 + 1 次成功派发，实际 {rows}"
    assert rows[-1][1] == "success", rows
    assert all(r[3] == BUSY for r in rows[:-1]), rows
    assert not any(r[1] == "failed" for r in rows), f"busy 不应落 failed：{rows}"
    assert int(_task_row(TID_BASE)[0] or 0) == 0, _task_row(TID_BASE)
    _drop_task(TID_BASE)


def test_busy_budget_exhausted_defers_to_next_tick(monkeypatch, caplog):
    """busy 预算耗尽 → requeue 下一 tick：不置 failed、不推进失败计数/退避、无 `failed after 0 retries`。"""
    import logging
    _mk_task(TID_BASE + 1, max_retry=0)
    monkeypatch.setattr(S, "_BUSY_RETRY_DELAYS", (0.01, 0.01, 0.01))
    calls = []
    monkeypatch.setattr(S, "_execute_autopilot_agent", _fake_agent_busy_then(calls, 99, "success"))
    notified = []
    monkeypatch.setattr(S, "_notify_subscribers",
                        lambda *a, **k: notified.append((a, k)))
    task = _task_dict(TID_BASE + 1)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(S._execute_task(task))

    rows = _exec_rows(TID_BASE + 1)
    assert len(rows) == S._BUSY_RETRY_BUDGET + 1, f"预算={S._BUSY_RETRY_BUDGET}，实际 {len(rows)} 条 exec"
    assert all(r[1] == "skipped" for r in rows), f"超预算不得落 failed：{rows}"
    assert all(r[3] == BUSY for r in rows), rows
    assert "requeue" in (rows[-1][2] or "").lower(), rows[-1][2]

    consecutive, code, backoff, paused, next_run = _task_row(TID_BASE + 1)
    assert int(consecutive or 0) == 0 and not code and not backoff and int(paused or 0) == 0, _task_row(TID_BASE + 1)
    assert next_run, "next_run_at 必须推进到下一 tick（requeue）"
    assert notified == [], f"busy 不是任务结果，不应发订阅通知：{notified}"

    text = caplog.text
    assert "failed after 0 retries" not in text, "busy 路径不得再产出 failed after 0 retries"
    assert "task enqueue busy" in text, "日志须保留既有指纹，便于 Loki 查询"
    _drop_task(TID_BASE + 1)


def test_permanent_failure_still_fails_with_max_retry_zero(monkeypatch, caplog):
    """回归：非 busy 的普通失败仍按原语义落 failed（瞬态通道不得吞掉真失败）。

    `error` 为空的失败正是线上 `failed after 0 retries` 的来源（#3963 现象行），
    这里锁定它会照旧出现——证明修复只吞掉 busy，不是把失败面整片消音。
    """
    import logging
    _mk_task(TID_BASE + 2, max_retry=0)
    calls = []
    monkeypatch.setattr(S, "_execute_autopilot_agent",
                        _fake_agent_busy_then(calls, 0, "failed", fail_error=""))
    task = _task_dict(TID_BASE + 2)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(S._execute_task(task))
    rows = _exec_rows(TID_BASE + 2)
    assert len(rows) == 1 and rows[0][1] == "failed", rows
    assert "failed after 0 retries" in caplog.text
    _drop_task(TID_BASE + 2)


# ---------------------------------------------------------------- 告警契约


def test_alerts_rule_for_repeated_busy():
    """deploy/alerts.yml 有「连续 busy」告警，且引用本单指标名。"""
    path = os.path.join(ROOT, "deploy", "alerts.yml")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    assert "grape_repo_dispatch_busy_total" in text, "告警须引用 busy_total 指标"
    m = re.search(r"- alert:\s*(\S*RepoDispatchBusy\S*)", text)
    assert m, "缺少 repo dispatch busy 告警规则"
    block = text[m.start():]
    assert "increase(grape_repo_dispatch_busy_total" in block[:800], block[:800]