"""Issue #3794：purge_stale_event_guards 分批清理 + §33 复核 + 1205 重试。

生产事故：4 台主控 100% 因 InnoDB 1205 锁等待超时失败（54 条/小时），
event_active_guard 30min TTL 自愈失效，残留守卫行永久阻塞重跑/续办/转交派发。

契约（见 docs/issue-3794/spec.md）：
- 两段短事务按 seq 分批（BATCH=200，最多 50 轮），DELETE 内重新 NOT EXISTS 复核。
- 有运行中 task（status IN ('dispatched','running')）的 guard 不删（#372 §33 不回归）。
- 1205/1213 短退避重试一次；重试仍失败 WARNING 带 seq 集合、返回累计已删数。
"""
from datetime import datetime, timezone

from src.api import event_store as es

RK = "gitcode:test/guard3794"

_CLEAN_SQL = [
    "DELETE FROM event_active_guard WHERE repo_key=?",
    "DELETE FROM agent_tasks WHERE repo_key=?",
    "DELETE FROM events WHERE repo_key=?",
]


def _clean():
    with es.db_lock:
        c = es.db.cursor()
        for sql in _CLEAN_SQL:
            c.execute(sql, (RK,))
        es.db.commit()


def _seed_guard(issue: str, agent: str, seq: int, age_seconds: int):
    """插入一条守卫行；seq=0 表示抢占后未回填，age 控制其是否过期。"""
    created = (datetime.now(timezone.utc).timestamp() - age_seconds)
    created = datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with es.db_lock:
        c = es.db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO event_active_guard (repo_key, issue_id, agent_type, seq, created_at) "
            "VALUES (?,?,?,?,?)", (RK, issue, agent, seq, created))
        es.db.commit()


def _seed_running_task(seq: int, status: str = "running", task_id: str = "t_3794"):
    with es.db_lock:
        c = es.db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, status, agent_type, skill, prompt, "
            "env_extra, cwd, event_seq, repo_key, issue_id, user_id, account, skill_files, created_at) "
            "VALUES (?,'ses_3794',0,?,'coding','implementation','p','{}','/tmp/g',?,?,'#3794',1,'','{}',?)",
            (task_id, status, seq, RK, datetime.now(timezone.utc).isoformat()))
        es.db.commit()


def _guard_count() -> int:
    with es.db_lock:
        row = es.db.cursor().execute(
            "SELECT COUNT(*) FROM event_active_guard WHERE repo_key=?", (RK,)).fetchone()
        return row[0] if row else 0


def test_expired_guard_without_running_task_is_purged():
    _clean()
    _seed_guard("#3794", "coding", 1001, age_seconds=3600)
    assert es.purge_stale_event_guards(max_age_seconds=1800) == 1
    assert _guard_count() == 0, "过期且无运行中 task 的 guard 行必须被清理"


def test_fresh_guard_is_not_purged():
    _clean()
    _seed_guard("#3794", "coding", 1002, age_seconds=10)
    assert es.purge_stale_event_guards(max_age_seconds=1800) == 0
    assert _guard_count() == 1, "未过期的 guard 行不得被清理"


def test_guard_with_running_task_is_not_purged():
    """#372 §33：有运行中 task 的 guard 不清理，防长任务被误清后重复派发。"""
    _clean()
    _seed_guard("#3794", "coding", 2001, age_seconds=3600)
    _seed_running_task(2001, status="running")
    try:
        assert es.purge_stale_event_guards(max_age_seconds=1800) == 0
        assert _guard_count() == 1, "有 running task 的 guard 行不得被清理"
    finally:
        with es.db_lock:
            es.db.cursor().execute("DELETE FROM agent_tasks WHERE id='t_3794'")
            es.db.commit()


def test_guard_with_dispatched_task_is_not_purged():
    _clean()
    _seed_guard("#3794", "coding", 2002, age_seconds=3600)
    _seed_running_task(2002, status="dispatched")
    try:
        assert es.purge_stale_event_guards(max_age_seconds=1800) == 0
        assert _guard_count() == 1, "有 dispatched task 的 guard 行不得被清理"
    finally:
        with es.db_lock:
            es.db.cursor().execute("DELETE FROM agent_tasks WHERE id='t_3794'")
            es.db.commit()


def test_guard_with_terminal_task_is_purged():
    """终态 task（非 dispatched/running）不算运行中，guard 可清理。"""
    _clean()
    _seed_guard("#3794", "coding", 2003, age_seconds=3600)
    _seed_running_task(2003, status="completed")
    try:
        assert es.purge_stale_event_guards(max_age_seconds=1800) == 1
        assert _guard_count() == 0
    finally:
        with es.db_lock:
            es.db.cursor().execute("DELETE FROM agent_tasks WHERE id='t_3794'")
            es.db.commit()


def test_more_than_batch_size_rows_are_all_purged():
    """>200 行过期 guard 分批删完（BATCH=200，多轮），返回总删除数。"""
    _clean()
    n = 450  # > 2 × 200，需 3 轮
    with es.db_lock:
        c = es.db.cursor()
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 3600, timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        for i in range(n):
            c.execute(
                "INSERT OR REPLACE INTO event_active_guard (repo_key, issue_id, agent_type, seq, created_at) "
                "VALUES (?,?,?,?,?)", (RK, f"#batch{i}", "coding", 300000 + i, cutoff))
        es.db.commit()
    try:
        assert es.purge_stale_event_guards(max_age_seconds=1800) == n
        assert _guard_count() == 0, f"{n} 行过期 guard 必须分批全部删完"
    finally:
        _clean()


def test_protected_batch_does_not_block_remaining_purgeable_guards():
    """候选批全为受保护 guard 时必须继续向后推进（QA #1981 Required）。

    200 条受保护（running）+ 5 条可删过期守卫：候选窗口被受保护行占满时，
    旧实现 `deleted == 0 → break` 整轮结束，两次调用都返回 0、205 行全残留，
    与「守卫自愈失效」同一症状。keyset 按 seq 推进后必须在同一次调用内删掉 5 条。
    """
    _clean()
    hold_seqs = [100000 + i for i in range(es._PURGE_BATCH)]
    drop_seqs = [200000 + i for i in range(5)]
    for i, seq in enumerate(hold_seqs):
        # 受保护行 seq 更小：首轮候选窗口被它们占满
        _seed_guard(f"#hold{i}", "coding", seq, age_seconds=3600)
        _seed_running_task(seq, status="running", task_id=f"t_hold{i}")
    for i, seq in enumerate(drop_seqs):
        _seed_guard(f"#drop{i}", "coding", seq, age_seconds=3600)
    try:
        assert es.purge_stale_event_guards(max_age_seconds=1800) == 5, (
            "受保护候选批不得阻塞剩余可删守卫（头阻塞回归）")
        assert _guard_count() == es._PURGE_BATCH, "5 条可删删掉、200 条受保护保留"
    finally:
        with es.db_lock:
            es.db.cursor().execute("DELETE FROM agent_tasks WHERE id LIKE 't_hold%'")
            es.db.commit()
        _clean()


def test_lock_wait_timeout_1205_retried_once():
    """DELETE 首次抛 1205 → 短退避重试一次成功，照常删除。"""
    _clean()
    _seed_guard("#3794", "coding", 5001, age_seconds=3600)
    real_delete = es._delete_guard_batch
    calls = []

    def flaky_delete(cutoff, seqs):
        calls.append(list(seqs))
        if len(calls) == 1:
            # InnoDB 1205：args[0]=1205（pymysql OperationalError 形态）
            raise RuntimeError(1205, "Lock wait timeout exceeded; try restarting transaction")
        return real_delete(cutoff, seqs)

    orig = es._delete_guard_batch
    es._delete_guard_batch = flaky_delete
    try:
        assert es.purge_stale_event_guards(max_age_seconds=1800) == 1
        assert len(calls) == 2, "1205 必须且只重试一次"
        assert _guard_count() == 0
    finally:
        es._delete_guard_batch = orig


def test_lock_wait_timeout_1205_retry_exhausted_returns_zero():
    """重试仍 1205：WARNING 口径返回 0（不抛出），失败日志带 seq 集合。"""
    _clean()
    _seed_guard("#3794", "coding", 5002, age_seconds=3600)

    def always_1205(cutoff, seqs):
        raise RuntimeError(1205, "Lock wait timeout exceeded; try restarting transaction")

    orig = es._delete_guard_batch
    es._delete_guard_batch = always_1205
    try:
        assert es.purge_stale_event_guards(max_age_seconds=1800) == 0
        assert _guard_count() == 1, "删除失败时 guard 行保留，下轮重试"
    finally:
        es._delete_guard_batch = orig


def test_deadlock_1213_also_retried_once():
    """InnoDB 1213（deadlock）与 1205 同属可重试锁冲突，同样短退避重试一次。"""
    _clean()
    _seed_guard("#3794", "coding", 5003, age_seconds=3600)
    real_delete = es._delete_guard_batch
    calls = []

    def flaky_deadlock(cutoff, seqs):
        calls.append(list(seqs))
        if len(calls) == 1:
            raise RuntimeError(1213, "Deadlock found when trying to get lock; try restarting transaction")
        return real_delete(cutoff, seqs)

    orig = es._delete_guard_batch
    es._delete_guard_batch = flaky_deadlock
    try:
        assert es.purge_stale_event_guards(max_age_seconds=1800) == 1
        assert len(calls) == 2, "1213 deadlock 必须且只重试一次"
        assert _guard_count() == 0
    finally:
        es._delete_guard_batch = orig


def test_batch_constant_within_contract():
    """分批契约：BATCH=200、最多 50 轮（防无界循环）。"""
    assert es._PURGE_BATCH == 200
    assert es._PURGE_MAX_ROUNDS == 50
