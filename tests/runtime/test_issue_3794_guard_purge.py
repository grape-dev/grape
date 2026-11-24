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


# ── 附加守卫（本轮上收新增）：db_lock 内的行锁等待必须有上限且被还原 ────────────
class _RecordingCursor:
    """只记录语句的假游标：验证 SET SESSION innodb_lock_wait_timeout 的设置与还原。"""

    def __init__(self, log, prev="50", fail_delete=False):
        self.log = log
        self.prev = prev
        self.fail_delete = fail_delete
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        self.log.append(s)
        if s.startswith("SELECT @@SESSION.innodb_lock_wait_timeout"):
            self._row = (self.prev,)
        elif s.startswith("DELETE"):
            if self.fail_delete:
                raise RuntimeError(1205, "Lock wait timeout exceeded")
            self.rowcount = 1
        return self

    def fetchone(self):
        return getattr(self, "_row", None)


def _run_batch_with_fake(monkeypatch, fail_delete=False):
    log = []
    cur = _RecordingCursor(log, prev="50", fail_delete=fail_delete)

    class _DB:
        def cursor(self_inner):
            return cur

        def commit(self_inner):
            log.append("COMMIT")

        def rollback(self_inner):
            log.append("ROLLBACK")

    monkeypatch.setattr("src.kernel.session_manager._is_mysql_backend", lambda: True)
    monkeypatch.setattr(es, "db", _DB())
    monkeypatch.setattr(es, "db_lock", __import__("threading").RLock())
    try:
        es._delete_guard_batch("2026-01-01 00:00:00", [1, 2, 3])
    except Exception:
        pass
    return log


def test_lock_wait_bounded_then_restored(monkeypatch):
    log = _run_batch_with_fake(monkeypatch)
    set_stmt = [s for s in log if s.startswith("SET SESSION innodb_lock_wait_timeout")]
    assert set_stmt == ["SET SESSION innodb_lock_wait_timeout=3",
                        "SET SESSION innodb_lock_wait_timeout=50"], set_stmt
    # 设置必须包住 DELETE，且还原在其后
    assert log.index("SET SESSION innodb_lock_wait_timeout=3") < [
        i for i, s in enumerate(log) if s.startswith("DELETE")][0]
    assert log.index("SET SESSION innodb_lock_wait_timeout=3") < log.index(
        "SET SESSION innodb_lock_wait_timeout=50")


def test_lock_wait_restored_even_when_delete_fails(monkeypatch):
    log = _run_batch_with_fake(monkeypatch, fail_delete=True)
    assert "SET SESSION innodb_lock_wait_timeout=50" in log, log
    assert "ROLLBACK" in log, log


def test_failed_batch_log_is_not_flooded_with_seqs(monkeypatch, caplog):
    """撞锁告警只打数量与区间，不把 200 个 seq 原样写进日志（线上日志膨胀）。"""
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        raise RuntimeError(1205, "Lock wait timeout exceeded")

    monkeypatch.setattr(es, "_delete_guard_batch", boom)
    monkeypatch.setattr(es, "_purge_metric_inc", lambda *a, **kw: None)
    monkeypatch.setattr(es, "db_lock", __import__("threading").RLock())
    monkeypatch.setattr(es, "time", __import__("types").SimpleNamespace(
        sleep=lambda s: None))
    monkeypatch.setattr(es, "random", __import__("types").SimpleNamespace(
        uniform=lambda a, b: 0.0))

    seqs = [(i,) for i in range(1, 201)]

    class _Cursor:
        def execute(self, sql, params=None):
            self._r = seqs
            return self

        def fetchall(self):
            return self._r

    class _DB:
        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(es, "db", _DB())
    with caplog.at_level("WARNING"):
        es.purge_stale_event_guards()
    msg = " ".join(r.message for r in caplog.records if "purge_stale_event_guards failed" in r.message)
    assert msg, "expected a warning log"
    assert "seqs_n=200" in msg, msg

    assert len(msg) < 400, f"warning log too long ({len(msg)} chars)"


# ── 审查 O2/O3：SET SESSION 的降级与还原失败不能中断清理 ──


class _SetCursor:
    """记录 SET/SELECT 的假游标；fail_set / fail_restore 控制哪一步失败。"""

    def __init__(self, fail_set=0, fail_restore=0):
        self.statements = []
        self.fail_set = fail_set
        self.fail_restore = fail_restore
        self.restore_attempts = 0

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if sql.startswith("SELECT @@SESSION"):
            self._row = (50,)
        elif sql.startswith("SET SESSION"):
            if "=3" in sql and self.fail_set > 0:
                self.fail_set -= 1
                raise RuntimeError("SET SESSION not allowed (fake)")
            if "=50" in sql:
                self.restore_attempts += 1
                if self.fail_restore > 0:
                    self.fail_restore -= 1
                    raise RuntimeError("restore failed (fake)")
        return self

    def fetchone(self):
        return getattr(self, "_row", None)


def test_bounded_lock_wait_degrades_when_set_is_rejected(monkeypatch, caplog):
    """SET 被拒（权限/方言）→ 只告警、仍执行清理主体，不抛异常（O3）。"""
    import src.kernel.session_manager as sm

    monkeypatch.setattr(sm, "_is_mysql_backend", lambda: True)
    cur = _SetCursor(fail_set=1)
    ran = []
    with caplog.at_level("WARNING"):
        with es._bounded_lock_wait(cur, 3):
            ran.append(1)
    assert ran == [1], "body must still run when the bound cannot be applied"
    assert not any("=50" in s for s in cur.statements), "must not restore what was never set"
    assert any("degrade to unbounded" in r.message for r in caplog.records)


def test_bounded_lock_wait_retries_restore_then_warns(monkeypatch, caplog):
    """还原失败 → 重试一次后仍失败只告警，绝不把异常抛给清理循环（O2）。"""
    import src.kernel.session_manager as sm

    monkeypatch.setattr(sm, "_is_mysql_backend", lambda: True)
    cur = _SetCursor(fail_restore=2)
    monkeypatch.setattr(es, "db", type("D", (), {"rollback": staticmethod(lambda: None)})())
    with caplog.at_level("WARNING"):
        with es._bounded_lock_wait(cur, 3):
            pass
    assert cur.restore_attempts == 2, cur.restore_attempts
    assert any("keeps the bounded wait until reconnect" in r.message for r in caplog.records)


# ── QA 原子回归 D1：MySQL 上批次删除返回值恒为 0 ──
#
# MySQLCompatCursor 每条语句都重置 rowcount；`_bounded_lock_wait` 退出时会再执行一条
# `SET SESSION innodb_lock_wait_timeout=…` 还原语句，于是「在 with 之外读 c.rowcount」
# 恒为 0（真库实测：删 3 行→立即读 3→跑一条 SET→读 0）。旧用例的假游标只在 DELETE 时
# 置 rowcount、SET 不碰它，属测试盲区。下面这个游标按真 MySQL 语义重置 rowcount。


class _MySQLRowcountCursor:
    def __init__(self, deleted=3, candidates=None):
        self.deleted = deleted
        self.candidates = candidates
        self.rowcount = -1
        self.statements = []

    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        self.statements.append(s)
        self.rowcount = 0  # 每条语句都重置——真 MySQLCompatCursor 的行为
        if s.startswith("SELECT @@SESSION"):
            self._row = (50,)
        elif s.startswith("DELETE"):
            self.rowcount = self.deleted
        elif s.startswith("SELECT seq FROM event_active_guard"):
            self._rows = self.candidates or []
        return self

    def fetchone(self):
        return getattr(self, "_row", None)

    def fetchall(self):
        return getattr(self, "_rows", [])


def _patch_mysql_backend(monkeypatch, cur, metric_calls=None):
    class _DB:
        def cursor(self_inner):
            return cur

        def commit(self_inner):
            pass

        def rollback(self_inner):
            pass

    monkeypatch.setattr("src.kernel.session_manager._is_mysql_backend", lambda: True)
    monkeypatch.setattr(es, "db", _DB())
    monkeypatch.setattr(es, "db_lock", __import__("threading").RLock())
    if metric_calls is not None:
        monkeypatch.setattr(es, "_purge_metric_inc",
                            lambda res, n=1: metric_calls.append((res, n)))


def test_delete_batch_returns_rows_under_mysql_rowcount_semantics(monkeypatch):
    """D1 回归：SET 还原语句吃掉 rowcount 后，批次仍要返回真实删除行数。"""
    cur = _MySQLRowcountCursor(deleted=3)
    _patch_mysql_backend(monkeypatch, cur)
    assert es._delete_guard_batch("2026-01-01 00:00:00", [1, 2, 3]) == 3, \
        "在 _bounded_lock_wait 之外读 rowcount 会拿到 SET 的 0（QA D1）"
    assert any(s.startswith("SET SESSION innodb_lock_wait_timeout=50")
               for s in cur.statements), "还原语句仍必须执行"


def test_purge_metrics_and_log_work_under_mysql_rowcount_semantics(monkeypatch, caplog):
    """后果链回归：deleted>0 → ok 指标计数 + purged 日志打印（MySQL 后端）。"""
    cur = _MySQLRowcountCursor(deleted=3, candidates=[(1,), (2,), (3,)])
    calls = []
    _patch_mysql_backend(monkeypatch, cur, calls)
    import logging
    with caplog.at_level(logging.INFO):
        total = es.purge_stale_event_guards()
    assert total == 3
    assert ("ok", 3) in calls, f"ok 指标必须计入真实删除数，实际 {calls}"
    assert any("purged=3" in r.message for r in caplog.records)
