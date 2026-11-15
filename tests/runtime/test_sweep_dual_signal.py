"""sweeper「双信号齐」收敛方向单测（2026-11-15 seq=9762 回归）。

qa 任务跑完 742 条消息、结论齐全、agent 自报 exit_code=0，却因为主控没来得及写终态
（tasks/complete 连续 504），被 sweeper 一律判成「中断：双信号已齐…」。
这里钉住两个方向：自报成功 → completed；自报失败 → failed。
"""
import contextlib

from src.domains.runtime.engine import cleanup


class _Cur:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, args=None):
        self.executed.append((" ".join(sql.split()), args))
        self.rowcount = 1
        return self.rowcount

    def fetchall(self):
        return list(self._rows)


class _Db:
    def __init__(self, cur):
        self._cur = cur
        self.committed = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed += 1


def _install(monkeypatch, read_rows, write_cur):
    read_cur = _Cur(read_rows)

    @contextlib.contextmanager
    def _sweep_cursor():
        yield read_cur

    monkeypatch.setattr(cleanup, "_sweep_cursor", _sweep_cursor)
    monkeypatch.setattr(cleanup, "db", _Db(write_cur))
    monkeypatch.setattr(cleanup, "db_lock", contextlib.nullcontext())
    return read_cur


def test_dual_signal_success_converges_to_completed(monkeypatch):
    # (id, event_seq, runtime_id, created_at, started_at, status, stream_ended, self_reported, exit_code)
    _install(monkeypatch, [("test_a", 0, 900417, None, None, "running", 1, 1, 0)], _Cur([]))
    assert cleanup.sweep_stuck_agent_tasks_once() == 1


def test_dual_signal_failure_stays_failed(monkeypatch):
    _install(monkeypatch, [("test_b", 0, 900417, None, None, "running", 1, 1, 1)], _Cur([]))
    assert cleanup.sweep_stuck_agent_tasks_once() == 1


def test_dual_signal_writes_terminal_status_by_exit_code(monkeypatch):
    write_cur = _Cur([])
    _install(monkeypatch,
             [("test_ok", 0, 0, None, None, "running", 1, 1, 0),
              ("test_bad", 0, 0, None, None, "running", 1, 1, 1)],
             write_cur)
    cleanup.sweep_stuck_agent_tasks_once()
    updates = [(sql, args) for sql, args in write_cur.executed if "UPDATE agent_tasks" in sql]
    assert len(updates) == 2
    statuses = {args[0] for _, args in updates}
    assert statuses == {"completed", "failed"}
    # exit_code 也要跟着方向走，不能把成功任务写成 exit_code=1
    codes = {args[1] for _, args in updates}
    assert codes == {0, 1}