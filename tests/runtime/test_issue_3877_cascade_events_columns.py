"""Issue #3877 回归：级联终态收敛在「缺 cascade 五列」时不得整条失败。

背景
----
三库拓扑下存量 runtime 库的 ``events`` 缺 ``cascade_reason`` / ``report_missing`` /
``triggered_by`` / ``cascade_task_status`` / ``cascade_prev_event_status``：
``_cascade_event_terminal_impl`` 原来的 UPDATE 把五列写在 SET 里 → 每条 1054 →
事件终态不收敛、``event_active_guard`` 释放（写在 UPDATE 之后同一 try）被跳过 → 只能等
TTL 30min；异常被外层吞成 WARNING（72 小时 54 次）。

覆盖
----
1. 正常路径（三库拓扑的 runtime 库形态）：五列写入 + 守卫即时删除；
2. 真缺列（库内事件表物理上没有五列）：降级为「只写存在列」，终态照常收敛、无 1054、守卫仍删；
3. 列缓存漂移（UPDATE 报 1054）：失效重探列集后重写一次，不把整次收敛判失败；
4. UPDATE 失败（非缺列）：守卫仍在 ``finally`` 释放，且失败留结构化日志 + 指标；
5. 指标通道：failed/degraded 计数器可按 reason/stage/missing 聚合；
6. 存量补偿入口 ``reconverge_terminal_gaps``：任务已终态 + 事件仍活跃 → 幂等重跑收敛。
"""
from datetime import datetime, timezone

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import cascade, service

REPO = "gitcode:test3877/repo"
CASCADE_COLUMNS = (
    "cascade_reason", "report_missing", "triggered_by",
    "cascade_task_status", "cascade_prev_event_status",
)


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _purge():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'gitcode:test3877/%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:test3877/%'")
        db.commit()


def _insert_event(seq, status="running", issue_id="#3877"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, status, agent_type, issue_id, "
            "scene_id, created_at) VALUES (?, ?, 'a2a_event', ?, 'coding', ?, 'scene-3877', ?)",
            (seq, REPO, status, issue_id, datetime.now(timezone.utc).isoformat()))
        db.commit()


def _insert_task(task_id, event_seq, status="running", exit_code=None, result_text=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, "
            "repo_key, agent_type, prompt, status, created_at, exit_code, result_text) "
            "VALUES (?, 'sess-3877', 0, ?, '#3877', ?, 'coding', 'p', ?, ?, ?, ?)",
            (task_id, event_seq, REPO, status,
             datetime.now(timezone.utc).isoformat(), exit_code, result_text))
        db.commit()


def _event_row(seq, columns):
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT {', '.join(columns)} FROM events WHERE seq=?", (seq,))
        return c.fetchone()


def _guard_count(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM event_active_guard WHERE seq=?", (seq,))
        return c.fetchone()[0]


def _claim_guard(seq, issue_id="#3877"):
    from src.api.event_store import claim_event_guard, update_event_guard_seq
    assert claim_event_guard(REPO, issue_id, "coding")
    update_event_guard_seq(REPO, issue_id, "coding", seq)


class _ShimDb:
    """把 service 的 runtime 域句柄指向测试库（模拟 runtime 库）。"""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()


@pytest.fixture
def bare_events_db(monkeypatch):
    """runtime 库形态的 events 表：**物理上没有** cascade 五列（存量库事故现场）。"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE events ("
        "seq INTEGER PRIMARY KEY, repo_key TEXT, event_type TEXT, issue_id TEXT, "
        "agent_type TEXT, scene_id TEXT, status TEXT, exit_code INTEGER, "
        "completed_at TEXT, result TEXT, structured_output TEXT)")
    conn.execute(
        "CREATE TABLE event_active_guard ("
        "repo_key TEXT, issue_id TEXT, agent_type TEXT, seq INTEGER)")
    conn.execute(
        "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, scene_id, status) "
        "VALUES (38770, 'gitcode:test3877/repo', 'a2a_event', '#3877', 'coding', "
        "'scene-3877', 'running')")
    conn.execute(
        "INSERT INTO event_active_guard (repo_key, issue_id, agent_type, seq) "
        "VALUES ('gitcode:test3877/repo', '#3877', 'coding', 38770)")
    conn.commit()

    monkeypatch.setattr(service, "_rt_db", _ShimDb(conn), raising=False)
    monkeypatch.setattr(service, "db", conn, raising=False)
    monkeypatch.setattr(service, "_TABLE_COLS_CACHE", {}, raising=False)
    monkeypatch.setattr(service, "_TABLE_COLS_CACHE_DB_ID", object(), raising=False)
    yield conn
    conn.close()


def test_converge_writes_cascade_columns_and_releases_guard():
    """① 正常路径：五列写入成功 + 守卫即时删除（三库拓扑 runtime 库验收口径）。"""
    _insert_event(38771)
    _insert_task("task-3877-a", 38771, status="running")
    _claim_guard(38771)
    assert _guard_count(38771) == 1

    service.update_task_status("task-3877-a", "completed", exit_code=0)

    row = _event_row(38771, ("status", "exit_code") + CASCADE_COLUMNS)
    assert row[0] == "completed"
    assert row[2] == cascade.REASON_SYNTHESIZED
    assert int(row[3]) == 1, "report_missing 恒为 1（补偿发生即原始回报丢失）"
    assert row[4] == cascade.TRIGGER_UPDATE_TASK_STATUS
    assert row[5] == "completed" and row[6] == "running"
    assert _guard_count(38771) == 0, "收敛后守卫必须即时释放（不等 TTL 30min）"


def test_missing_cascade_columns_degrade_to_terminal_columns(bare_events_db, caplog):
    """② 真缺列：只写存在列 → 终态收敛、无 1054、守卫仍删除。"""
    with caplog.at_level("WARNING"):
        ok = service._cascade_event_terminal_impl(
            bare_events_db.cursor(), 38770, "failed", exit_code=1,
            task_status="failed", task_result_text="")

    assert ok is True, "缺归因列不得让整次收敛失败"
    row = bare_events_db.execute(
        "SELECT status, exit_code, completed_at, result FROM events WHERE seq=38770").fetchone()
    assert row[0] == "failed", "终态字段必须照常收敛"
    assert int(row[1]) == 1 and row[2], "completed_at 也要落库"
    assert row[3].startswith(cascade.CASCADE_NOTICE)
    assert bare_events_db.execute(
        "SELECT COUNT(*) FROM event_active_guard WHERE seq=38770").fetchone()[0] == 0, \
        "UPDATE 降级后守卫仍须删除"
    text = "\n".join(r.message for r in caplog.records)
    assert "no such column" not in text.lower() and "1054" not in text, "不得再产生缺列错误"
    assert "_cascade_event_terminal: degraded for event 38770" in text
    assert "cascade_reason" in text, "降级日志必须报出缺失的归因列"


def test_missing_column_1054_invalidates_cache_and_retries(monkeypatch):
    """③ 列缓存漂移（UPDATE 报 1054）→ 失效重探列集后重写一次，不判整次失败。"""
    _insert_event(38772)
    _insert_task("task-3877-b", 38772, status="running")
    _claim_guard(38772)

    real_cols = {r[1] for r in db.cursor().execute("PRAGMA table_info(events)").fetchall()}
    probe = {"n": 0}

    def _fake_table_columns(table):
        probe["n"] += 1
        if table != "events":
            return real_cols
        return set(real_cols) if probe["n"] == 1 else set(real_cols) - set(CASCADE_COLUMNS)

    monkeypatch.setattr(service, "_table_columns", _fake_table_columns)

    class _FlakyCursor:
        """首次写 cascade_reason 时抛 1054（模拟「代码认为列在、库里没有」）。"""

        def __init__(self, real):
            self._real = real
            self.failed = False

        def execute(self, sql, params=()):
            if not self.failed and "cascade_reason" in sql:
                self.failed = True
                raise Exception('(1054, "Unknown column \'cascade_reason\' in \'field list\'")')
            return self._real.execute(sql, params)

        def fetchone(self):
            return self._real.fetchone()

        def fetchall(self):
            return self._real.fetchall()

    with db_lock:
        cursor = _FlakyCursor(db.cursor())
        ok = service._cascade_event_terminal_impl(
            cursor, 38772, "completed", exit_code=0, task_status="completed",
            task_result_text="真实结论")
        db.commit()

    assert ok is True, "1054 只是缓存漂移，重探列集后应成功收敛"
    assert probe["n"] >= 2, "报 1054 后必须失效缓存重探列集"
    row = _event_row(38772, ("status", "result"))
    assert row[0] == "completed"
    assert row[1] == "真实结论", "真实结论回填不受缺列降级影响"
    assert _guard_count(38772) == 0


def test_guard_released_when_update_fails(monkeypatch, caplog):
    """④ 非缺列失败：守卫仍在 finally 释放，失败留结构化日志 + 指标。"""
    _insert_event(38773)
    _insert_task("task-3877-c", 38773, status="running")
    _claim_guard(38773)

    def _boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service, "_build_cascade_terminal_update", _boom)

    from src.domains.insight import observability as obs
    before = obs.registry.get_sample_value(
        "grape_event_cascade_converge_failed_total",
        labels={"reason": "db_error", "stage": "update"}) or 0.0

    with caplog.at_level("ERROR"):
        assert service.update_task_status("task-3877-c", "failed", exit_code=1) is None

    assert _guard_count(38773) == 0, "UPDATE 失败时守卫也必须释放（finally）"
    after = obs.registry.get_sample_value(
        "grape_event_cascade_converge_failed_total",
        labels={"reason": "db_error", "stage": "update"}) or 0.0
    assert after == before + 1.0, "收敛失败必须进计数指标（可告警）"
    text = "\n".join(r.message for r in caplog.records)
    assert "_cascade_event_terminal: failed for event 38773" in text
    assert "stage=update" in text and "reason=db_error" in text


def test_degraded_counter_groups_by_missing_columns():
    """⑤ 降级指标按 missing 列名聚合（基数 = 五列子集）。"""
    from src.domains.insight import observability as obs
    from src.kernel import cascade_converge_metrics as cm

    labels = {"missing": cm.missing_label(["triggered_by", "cascade_reason"])}
    before = obs.registry.get_sample_value(
        "grape_event_cascade_converge_degraded_total", labels=labels) or 0.0
    cm.observe_degraded(["triggered_by", "cascade_reason"])
    after = obs.registry.get_sample_value(
        "grape_event_cascade_converge_degraded_total", labels=labels) or 0.0
    assert after == before + 1.0
    assert cm.missing_label([]) == "-"
    assert cm.missing_label(None) == "-"


def test_reconverge_terminal_gaps_is_idempotent():
    """⑥ 存量补偿入口：任务已终态 + 事件仍活跃 → 收敛一次；再跑为 noop（可重复执行）。"""
    _insert_event(38774)
    _insert_task("task-3877-d", 38774, status="failed", exit_code=1,
                 result_text="真实结论：迁移已上线并回归通过")
    _claim_guard(38774)

    first = service.reconverge_terminal_gaps(seqs=[38774])
    assert first["scanned"] == 1 and first["converged"] == 1
    assert first["results"][0]["status"] == "converged"
    row = _event_row(38774, ("status",) + CASCADE_COLUMNS)
    assert row[0] == "failed"
    assert row[1] == cascade.REASON_BACKFILLED, "存量补偿走 _real 回填路径"
    assert row[2] and row[3], "triggered_by / 原始任务终态必须落库"
    assert row[1:]  # 归因字段非空
    assert _guard_count(38774) == 0

    second = service.reconverge_terminal_gaps(seqs=[38774])
    assert second["scanned"] == 0, "已收敛事件不再进入补偿集合（幂等）"
    assert _event_row(38774, ("status",) + CASCADE_COLUMNS) == row, "重复执行不改写已落库结论"