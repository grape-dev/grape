"""mgmt.public 巡检任务写原语单测（P3-5d）：运行时间 / 引擎绑定。"""
import contextlib

from src.domains.mgmt import public as mg
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((" ".join(sql.split()), args))
        return self.rowcount

    def close(self):
        pass


def install(monkeypatch, cur):
    @contextlib.contextmanager
    def _ctx(domain):
        assert domain == "mgmt"
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    return cur


def test_set_run_info_writes_three_timestamps(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert mg.set_autopilot_run_info(7, "2026-11-13T23:00:00+08:00", "2026-11-14T00:00:00+08:00") == 1
    sql, args = cur.executed[0]
    assert sql == ("UPDATE autopilot_tasks SET last_run_at=%s, next_run_at=%s, updated_at=%s "
                   "WHERE id=%s")
    # updated_at 与原实现一致 = last_run_at
    assert args == ("2026-11-13T23:00:00+08:00", "2026-11-14T00:00:00+08:00",
                    "2026-11-13T23:00:00+08:00", 7)


def test_set_engine_ids_writes_json_as_is(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    payload = "[11, 13]"
    assert mg.set_autopilot_engine_ids(9, payload) == 1
    sql, args = cur.executed[0]
    assert sql == "UPDATE autopilot_tasks SET engine_runtime_ids=%s WHERE id=%s"
    assert args == (payload, 9)


def test_set_engine_ids_missing_row_returns_zero(monkeypatch):
    install(monkeypatch, FakeCursor(rowcount=0))
    assert mg.set_autopilot_engine_ids(999, "[]") == 0