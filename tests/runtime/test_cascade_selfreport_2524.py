# -*- coding: utf-8 -*-
"""Issue #2524 现场回归（三）：自报成功的任务，级联不得盖“回报丢失补偿”。

8380/8381 实例：agent 已通过 `tasks complete` 交过结构化结论（self_reported=1，
exit=0，评论已发、A2A 已交办），但 worker 置终态时事件还是 running → 兜底级联
把事件写成「任务已完成：级联收敛（回报丢失补偿）」，随后事件侧真实结果被
终态保护拒写（`Event ... already completed, skip completed overwrite`）。
"""
from src.domains.runtime.engine.service import _cascade_event_terminal


class _Cursor:
    def __init__(self, event_status="running"):
        self._status = event_status
        self.updates = []
        self._pending = None

    def execute(self, sql, params=None):
        self._pending = (sql, params)
        self.updates.append((sql, params))
        return self

    def fetchone(self):
        sql, _params = self._pending or ("", None)
        if "SELECT status FROM events" in sql:
            return (self._status,)
        return None


def _event_update_row(c: _Cursor):
    for sql, params in c.updates:
        if sql.startswith("UPDATE events SET status="):
            return sql, params
    return None, None


def test_cascade_uses_self_reported_result():
    c = _Cursor("running")
    text = '{"actions": ["a"], "conclusion": "done"}'
    assert _cascade_event_terminal(c, 8380, "completed", result_text=text) is True
    sql, params = _event_update_row(c)
    assert sql and "result=?" in sql
    assert params[1] == text, "自报结果必须原样回写，不能被兜底文案覆盖"


def test_cascade_falls_back_when_no_self_report():
    from src.domains.runtime.engine.service import _EVENT_CASCADE_RESULTS
    c = _Cursor("running")
    assert _cascade_event_terminal(c, 8383, "completed") is True
    _sql, params = _event_update_row(c)
    assert params[1] == _EVENT_CASCADE_RESULTS["completed"]


def test_cascade_never_overwrites_terminal_event():
    c = _Cursor("completed")
    assert _cascade_event_terminal(c, 1, "completed", result_text="later") is False
    assert _event_update_row(c) == (None, None)