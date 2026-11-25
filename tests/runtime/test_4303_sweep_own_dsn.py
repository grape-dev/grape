"""Issue #4303：event sweeper 三库拓扑（own DSN）方言层回归。

生产实例（2026-11-24）：worker 重启后首轮巡检报
`sweeper: per-seq recovery failed, fallback bulk: not all arguments converted
during string formatting`，随后 9 个在途事件同秒被一刀切判 failed（8 个停留
failed，106798 需人工 redispatch）。根因：own 模式下 `_c2 =
_c.connection.cursor()` 经 `_CompatCursor.__getattr__` 透传拿到**裸 pymysql
游标**，`?→%s` 翻译层被绕过，首条含 `?` 的 UPDATE 必抛 TypeError。

这里用「裸 pymysql 游标语义」的假连接（带参执行 = `sql % args`，SQL 无 `%s`
即抛生产同款 TypeError）复刻三库拓扑，钉死：
- per-seq UPDATE / cascade 不抛 TypeError（?→%s 翻译恢复），不触发 bulk 回落；
- 任务已终态的事件按任务终态级联、回填真实 result_text，不被写成 failed；
- 重启恢复窗口内的在途事件不动，窗口外僵尸照常回收；
- per-seq 失败逐条降级（重读终态再收敛），最后手段 bulk 用占位符绑定文案，
  失败原因不静默。
"""
import contextlib
import time

import pytest

from src.kernel import db_registry
from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import cleanup

_REPO = "gitcode:org/i4303-sweep"
_OLD_STARTED = "2020-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _past_restart_window(monkeypatch):
    """默认把重启恢复窗口钉到已过期（本文件测的是回收/降级逻辑本身；
    TestRestartGraceWindow 用例自行覆盖 _PROCESS_STARTED_AT 验证护栏）。"""
    monkeypatch.setattr(cleanup, "_PROCESS_STARTED_AT", time.time() - 3600)


@pytest.fixture(autouse=True)
def _clean_rows():
    """只清本用例的行（repo_key 前缀隔离），前后各清一次。"""
    def _purge():
        with db_lock:
            for t in ("events", "agent_tasks", "event_active_guard"):
                try:
                    db.cursor().execute(
                        f"DELETE FROM {t} WHERE repo_key LIKE '{_REPO}%'")
                except Exception:
                    pass
            db.commit()
    _purge()
    yield
    _purge()


class _FakeRawCursor:
    """裸 pymysql 游标语义（#4303 根因复刻层）。

    - 带参执行 = `sql % args` 格式化：SQL 里没有 `%s`（即没经过 ?→%s 翻译）
      时直接抛 `TypeError: not all arguments converted during string
      formatting`——与生产报错逐字一致；
    - 翻译已发生的 SQL（`%s`）翻回 `?` 后落**真 sqlite** 执行，每语句后提交
      （autocommit 域连接语义），数据对用例可见；
    - `.connection` 落点由构造方注入：own 连接游标指向裸连接（复刻
      `_CompatCursor.__getattr__` 的透传链），裸连接再开的游标不带翻译层。
    """

    def __init__(self, log):
        self._log = log              # 全部裸游标共享的执行流水
        self._conn: "_FakeRawPyConn | None" = None   # .connection 落点（构造方注入）
        self._rows = []
        self.rowcount = 0
        self.lastrowid = 0

    def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))
        _sqlite_sql = sql
        if params is not None:
            if "?" in sql:
                # pymysql：query % escaped_args —— 未翻译的 ? SQL 在这里必挂
                raise TypeError(
                    "not all arguments converted during string formatting")
            _sqlite_sql = sql.replace("%s", "?")
        with db_lock:
            cur = db.cursor()
            if params is not None:
                cur.execute(_sqlite_sql, params)
            else:
                cur.execute(_sqlite_sql)
            try:
                self._rows = cur.fetchall()
            except Exception:
                self._rows = []
            self.rowcount = getattr(cur, "rowcount", 0)
            db.commit()
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    @property
    def connection(self):
        """_CompatCursor.__getattr__ 透传 .connection 的落点。"""
        return self._conn


class _FakeRawPyConn:
    """裸 pymysql 连接：cursor() 返回**不带翻译层**的裸游标。

    生产根因即在这里：`_c.connection.cursor()`（绕过 _CompatCursor）拿到的是
    这一层游标，? 绑定不做 ?→%s 翻译，首条含 ? 的语句必抛 TypeError。
    """

    def __init__(self, log):
        self._log = log

    def cursor(self, *a, **kw):
        cur = _FakeRawCursor(self._log)
        cur._conn = self
        return cur


class _FakeOwnConn:
    """`_CompatConn` 语义：cursor() 返回带 ?→%s 翻译层的兼容游标。"""

    def __init__(self, log, raw_conn):
        self._log = log
        self._raw_conn = raw_conn
        self.executed = log          # 断言侧直接读共享执行流水

    def cursor(self, *a, **kw):
        cur = _FakeRawCursor(self._log)
        # _CompatCursor.__getattr__('.connection') → 裸游标.connection → 裸连接
        cur._conn = self._raw_conn
        return db_registry._CompatCursor(cur)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def own_dsn_topology(monkeypatch):
    """mock 三库拓扑：uses_own_dsn("runtime")=True，new_connection 返回带兼容
    游标的假连接（与 tests/api/test_p2_lib_switch.py 的 patch 模式同源）。"""
    log: list = []
    conn = _FakeOwnConn(log, _FakeRawPyConn(log))
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda domain: domain == "runtime")
    monkeypatch.setattr(db_registry, "new_connection",
                        lambda domain, autocommit=True: conn)
    monkeypatch.setattr(cleanup, "_PROCESS_STARTED_AT", time.time() - 3600)
    return conn


def _mk_running_event(started_at=_OLD_STARTED, status="running"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, "
            "status, started_at) VALUES (?, '#1', 'issue_opened', 'vod', 't', ?, ?)",
            (_REPO, status, started_at))
        db.commit()
        return c.lastrowid


def _mk_task(tid, seq, status, exit_code=None, result_text=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, "
            "status, exit_code, result_text, prompt, repo_key, agent_type) "
            "VALUES (?, 'sess', 0, ?, ?, ?, ?, 'p', ?, 'vod')",
            (tid, seq, status, exit_code, result_text, _REPO))
        db.commit()


def _event_row(seq):
    with db_lock:
        return db.cursor().execute(
            "SELECT status, exit_code, result FROM events WHERE seq=?",
            (seq,)).fetchone()


def _all_executed(conn):
    return conn.executed


class TestOwnDsnPerSeqRecovery:
    """三库拓扑：第二游标必须带 ?→%s 方言翻译层（根因回归）。"""

    def test_per_seq_update_no_typeerror_no_bulk(self, own_dsn_topology):
        """per-seq 文案 UPDATE 经翻译层执行：不抛 TypeError、参数走绑定、
        不触发 bulk 回落。"""
        seq_no_task = _mk_running_event()
        seq_timeout = _mk_running_event()
        # pending_self_report：有任务行、非活跃、非终态 → _TIMEOUT_TXT 分支
        _mk_task("t-i4303-pend", seq_timeout, "pending_self_report")

        fixed = cleanup.sweep_stuck_events_once(no_start_minutes=10, stale_minutes=30)
        assert fixed == 2

        row_a, row_b = _event_row(seq_no_task), _event_row(seq_timeout)
        assert row_a[0] == "failed" and "创建任务失败" in row_a[2]
        assert row_b[0] == "failed" and "running 超时" in row_b[2]
        assert "创建任务失败" not in row_b[2]

        # 翻译层证据：所有带参 events 终态 UPDATE 都只含 %s（? 已全部翻译）
        updates = [(s, p) for s, p in _all_executed(own_dsn_topology)
                   if s.startswith("UPDATE events SET status='failed'")
                   and "WHERE seq=%s" in s]
        assert updates, "per-seq 文案 UPDATE 应该发生（而不是 bulk）"
        for sql, params in updates:
            assert "?" not in sql, f"未翻译的 ? 泄漏进裸游标 SQL: {sql}"
            assert "%s" in sql
            assert len(params) == 3
        # 不触发 bulk 回落：不应出现 seq IN 形态的 events UPDATE
        assert not [s for s, _ in _all_executed(own_dsn_topology)
                    if "UPDATE events" in s and "seq IN (" in s]

    def test_terminal_task_cascades_with_real_result(self, own_dsn_topology):
        """任务已终态（completed/exit=0/真实结论）→ 事件级联 completed 并回填
        真实 result_text，绝不被一刀切写成 failed。"""
        real_text = "PR #2119 head cacd602f 架构审查 APPROVED，已通知人工合并"
        seq_term = _mk_running_event()
        _mk_task("t-i4303-done", seq_term, "completed", exit_code=0,
                 result_text=real_text)
        seq_no_task = _mk_running_event()

        fixed = cleanup.sweep_stuck_events_once(no_start_minutes=10, stale_minutes=30)
        assert fixed == 2

        row_t = _event_row(seq_term)
        assert row_t[0] == "completed", "终态任务的事件必须级联 completed，不是 failed"
        assert row_t[1] == 0
        assert real_text in row_t[2], "必须回填任务真实结论"
        assert "sweeper 兜底回收" not in row_t[2]
        assert _event_row(seq_no_task)[0] == "failed"


class TestRestartGraceWindow:
    """重启恢复窗口护栏：以进程启动时刻为参照（#4303 Spec 第 3 点）。"""

    def test_fresh_start_skips_inflight_recovery(self, monkeypatch):
        """进程刚启动（窗口内）→ 在途僵尸事件不动。"""
        seq = _mk_running_event()
        monkeypatch.setattr(cleanup, "_PROCESS_STARTED_AT", time.time())
        assert cleanup.sweep_stuck_events_once(no_start_minutes=10,
                                               stale_minutes=30) == 0
        row = _event_row(seq)
        assert row[0] == "running", "重启恢复窗口内的在途事件不得被回收"
        assert not row[2]

    def test_window_expired_recovers_as_before(self, monkeypatch):
        """窗口外（启动超过 1h）→ 僵尸照常回收（护栏不引入漏收）。"""
        seq = _mk_running_event()
        monkeypatch.setattr(cleanup, "_PROCESS_STARTED_AT", time.time() - 3600)
        assert cleanup.sweep_stuck_events_once(no_start_minutes=10,
                                               stale_minutes=30) == 1
        assert _event_row(seq)[0] == "failed"

    def test_grace_disabled_by_env(self, monkeypatch):
        """SWEEPER_RESTART_GRACE_MINUTES=0 → 护栏关闭，立即照常回收。"""
        seq = _mk_running_event()
        monkeypatch.setenv("SWEEPER_RESTART_GRACE_MINUTES", "0")
        monkeypatch.setattr(cleanup, "_PROCESS_STARTED_AT", time.time())
        assert cleanup.sweep_stuck_events_once(no_start_minutes=10,
                                               stale_minutes=30) == 1
        assert _event_row(seq)[0] == "failed"

    def test_fresh_start_skips_in_own_dsn_topology(self, monkeypatch,
                                                   own_dsn_topology):
        """三库拓扑 + 刚重启：首轮巡检同样不回收在途事件。"""
        seq = _mk_running_event()
        monkeypatch.setattr(cleanup, "_PROCESS_STARTED_AT", time.time())
        assert cleanup.sweep_stuck_events_once(no_start_minutes=10,
                                               stale_minutes=30) == 0
        assert _event_row(seq)[0] == "running"


class _BlownSeqUpdateCursor:
    """per-seq 形态（`WHERE seq=?`）的 events 终态 UPDATE 一律抛错；
    其余语句透传真 sqlite 游标（bulk 形态 `seq IN (...)` 放行）。"""

    def __init__(self, inner):
        self._inner = inner
        self.executed = []

    def execute(self, sql, params=None):
        norm = " ".join(sql.split())
        self.executed.append((norm, params))
        if "UPDATE events" in norm and "WHERE seq=?" in norm:
            raise RuntimeError("simulated per-seq write failure")
        if params is not None:
            return self._inner.execute(sql, params)
        return self._inner.execute(sql)

    def __getattr__(self, item):
        return getattr(self._inner, item)


class TestPerSeqDegradation:
    """per-seq 失败不再整段回落 bulk：逐条降级 → 最后手段 bulk（占位符绑定）。
    """

    @pytest.fixture
    def blown_per_seq_writes(self, monkeypatch):
        """per-seq/degraded 的单 seq UPDATE 全部失败，逼出降级链与最后手段。"""
        blown = []

        @contextlib.contextmanager
        def _sweep_cursor():
            with db_lock:
                cur = _BlownSeqUpdateCursor(db.cursor())
                blown.append(cur)
                yield cur

        monkeypatch.setattr(cleanup, "_sweep_cursor", _sweep_cursor)
        monkeypatch.setattr(cleanup, "_second_sweep_cursor",
                            lambda c: _BlownSeqUpdateCursor(db.cursor()))
        return blown

    def test_degrade_then_last_resort_bulk_uses_bound_params(
            self, monkeypatch, blown_per_seq_writes, caplog):
        seq = _mk_running_event()
        _mk_task("t-i4303-pend2", seq, "pending_self_report")

        with caplog.at_level("WARNING", logger="devops-bot"):
            fixed = cleanup.sweep_stuck_events_once(no_start_minutes=10,
                                                   stale_minutes=30)
        assert fixed == 1
        row = _event_row(seq)
        assert row[0] == "failed"
        assert "running 超时" in row[2]

        # 失败原因不静默：per-seq 与降级失败都有日志
        assert any("per-seq recovery failed for seq=" in r.message
                   for r in caplog.records)
        assert any("degraded per-seq recovery failed" in r.message
                   for r in caplog.records)
        # 最后手段 bulk：占位符绑定文案（result 不再 f-string 直拼）
        bulk = [(s, p) for cur in blown_per_seq_writes for s, p in cur.executed
                if "UPDATE events" in s and "seq IN (" in s]
        assert bulk, "降级失败后应走最后手段 bulk"
        for sql, params in bulk:
            assert cleanup._TIMEOUT_TXT not in sql, "文案不得内联进 SQL"
            assert params[1] == cleanup._TIMEOUT_TXT, "文案必须经绑定参数传入"

    def test_degraded_pass_recovers_when_second_cursor_blown(
            self, monkeypatch, caplog):
        """仅 per-seq 第二游标写失败 → 降级换新游标重读后照常收敛，无需 bulk。"""
        monkeypatch.setattr(cleanup, "_second_sweep_cursor",
                            lambda c: _BlownSeqUpdateCursor(db.cursor()))
        seq = _mk_running_event()
        _mk_task("t-i4303-pend3", seq, "pending_self_report")

        with caplog.at_level("WARNING", logger="devops-bot"):
            fixed = cleanup.sweep_stuck_events_once(no_start_minutes=10,
                                                   stale_minutes=30)
        assert fixed == 1
        row = _event_row(seq)
        assert row[0] == "failed" and "running 超时" in row[2]
        assert any("per-seq recovery failed for seq=" in r.message
                   for r in caplog.records)
        assert not any("last-resort" in r.message for r in caplog.records)

    def test_degraded_pass_keeps_terminal_event_running_on_write_failure(
            self, monkeypatch, blown_per_seq_writes, caplog):
        """写路径故障时：任务已终态的事件只尝试级联，级联再失败宁可保留
        running 下一轮重试，也绝不被一刀切写成 failed（#4303 核心语义）。"""
        real_text = "降级重读后回填的真实结论"
        seq_term = _mk_running_event()
        _mk_task("t-i4303-deg", seq_term, "completed", exit_code=0,
                 result_text=real_text)

        with caplog.at_level("WARNING", logger="devops-bot"):
            fixed = cleanup.sweep_stuck_events_once(no_start_minutes=10,
                                                   stale_minutes=30)
        assert fixed == 0
        row = _event_row(seq_term)
        assert row[0] == "running", "级联写失败时不得把 completed 任务的事件标成 failed"
        # 失败原因不静默：per-seq 级联未收敛 + 级联重试失败均有日志
        assert any("per-seq" in r.message and f"seq={seq_term}" in r.message
                   for r in caplog.records)
        assert any("cascade retry failed" in r.message
                   for r in caplog.records)
        # 终态任务的事件绝不进最后手段 bulk
        assert not [s for cur in blown_per_seq_writes for s, _ in cur.executed
                    if "UPDATE events" in s and "seq IN (" in s]
