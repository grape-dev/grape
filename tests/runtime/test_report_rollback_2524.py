# -*- coding: utf-8 -*-
"""Issue #2524 现场回归：上报落库失败时必须回滚，不能把事务/行锁挂在线程级连接上。

三库独立后 worker/event 进程的 `_rt_db` 是 autocommit=False 的线程级连接；
`report_messages` 一批最多 50 行、只在末尾 commit。半批失败若不回滚，
已插入行的锁会一直被同线程复用，其它写要等满 innodb_lock_wait_timeout(50s)
才 1205，完成回报随之丢失，事件被兜底收敛成 completed 且不再派发下一阶段。
"""


class _BoomCursor:
    """命中 task_messages INSERT 即抛 1205；其它语句返回占位行。"""

    def __init__(self, boom: bool):
        self._boom = boom

    def execute(self, sql, params=None):
        if self._boom and "INSERT INTO task_messages" in sql:
            raise Exception("(1205, 'Lock wait timeout exceeded; try restarting transaction')")
        return self

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []


class _FakeDb:
    def __init__(self, boom: bool):
        self._boom = boom
        self.rolled_back = 0
        self.committed = 0

    def cursor(self, *a, **kw):
        return _BoomCursor(self._boom)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _fake_task():
    # event_seq=0 → 跳过 events.agent_logs 落档，聚焦 task_messages 落库路径
    return {"repo_key": "gitcode:o/r", "event_seq": 0, "agent_type": "vod", "issue_id": ""}


def test_report_messages_rolls_back_when_insert_fails(monkeypatch):
    from src.domains.runtime.engine import service

    db = _FakeDb(boom=True)
    monkeypatch.setattr(service, "_rt_db", db)
    monkeypatch.setattr(service, "get_task", lambda _tid: _fake_task())

    receipt = service.report_messages("t_rollback", [{"type": "text", "data": "x"}])

    assert receipt == {}, "半批失败不得返回成功回执"
    assert db.rolled_back >= 1, "异常路径必须 rollback，否则行锁挂在线程级连接上"


def test_report_messages_commits_on_success(monkeypatch):
    from src.domains.runtime.engine import service

    db = _FakeDb(boom=False)
    monkeypatch.setattr(service, "_rt_db", db)
    monkeypatch.setattr(service, "get_task", lambda _tid: _fake_task())

    receipt = service.report_messages("t_commit", [{"type": "text", "data": "x"}])

    assert receipt and receipt.get("count") == 1
    assert db.committed >= 1
    assert db.rolled_back == 0