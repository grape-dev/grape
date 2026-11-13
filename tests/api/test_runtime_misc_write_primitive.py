"""runtime.public 杂项写原语单测（P3-5h）：解绑审计 / 巡检执行 / 通知偏好 / 事件过滤规则。"""
import contextlib

import pytest

from src.domains.runtime import public as rt
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, rowcount=1, lastrowid=42):
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((" ".join(sql.split()), args))
        return self.rowcount

    def close(self):
        pass


def install(monkeypatch, cur):
    @contextlib.contextmanager
    def _ctx(domain):
        assert domain == "runtime"
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    return cur


def test_mark_detach_rebound(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=2))
    assert rt.mark_detach_rebound(31, "sre", "2026-11-13T23:00:00+00:00") == 2
    sql, args = cur.executed[0]
    assert sql == ("UPDATE scene_engine_detach_log SET rebound_at=%s "
                   "WHERE scene_row_id=%s AND node_tag=%s "
                   "AND (rebound_at='' OR rebound_at IS NULL)")
    assert args == ("2026-11-13T23:00:00+00:00", 31, "sre")


def test_delete_autopilot_executions(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=3))
    assert rt.delete_autopilot_executions(7) == 3
    assert cur.executed[0] == ("DELETE FROM autopilot_executions WHERE task_id=%s", (7,))


def test_set_autopilot_findings(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.set_autopilot_findings(9, '{"a":1}') == 1
    assert cur.executed[0] == ("UPDATE autopilot_executions SET findings_json=%s WHERE id=%s",
                               ('{"a":1}', 9))


def test_set_repo_im_notifications_has_no_where(monkeypatch):
    """原 SQL 无 WHERE（全局偏好），原语保持一致。"""
    cur = install(monkeypatch, FakeCursor(rowcount=4))
    assert rt.set_repo_im_notifications(1, 0) == 4
    sql, args = cur.executed[0]
    assert sql == "UPDATE repo_im_group_binding SET auto_impl_enabled=%s, notify_enabled=%s"
    assert args == (1, 0)


def test_add_event_action_filter(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1, lastrowid=88))
    rid = rt.add_event_action_filter(
        {"event_type": "issue_opened", "action": "allow", "allow": 1, "actor": "any"},
        "2026-11-13T23:00:00+00:00")
    assert rid == 88
    sql, args = cur.executed[0]
    assert sql.startswith("INSERT INTO event_action_filter")
    assert args[0] == "issue_opened" and args[1] == "allow" and args[2] == 1
    assert args[-2:] == ("2026-11-13T23:00:00+00:00", "2026-11-13T23:00:00+00:00")


def test_update_event_action_filter_whitelist(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    n = rt.update_event_action_filter(5, {"allow": 0, "priority": 10}, "T")
    assert n == 1
    sql, args = cur.executed[0]
    assert sql == ("UPDATE event_action_filter SET allow=%s, priority=%s, updated_at=%s "
                   "WHERE id=%s")
    assert args == (0, 10, "T", 5)


def test_update_event_action_filter_rejects_unknown_column(monkeypatch):
    install(monkeypatch, FakeCursor())
    with pytest.raises(ValueError):
        rt.update_event_action_filter(5, {"event_type": "x"}, "T")


def test_update_event_action_filter_empty_is_noop(monkeypatch):
    cur = install(monkeypatch, FakeCursor())
    assert rt.update_event_action_filter(5, {}, "T") == 0
    assert cur.executed == []


def test_delete_event_action_filter(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.delete_event_action_filter(5) == 1
    assert cur.executed[0] == ("DELETE FROM event_action_filter WHERE id=%s", (5,))


def test_delete_event_action_filter_missing_returns_zero(monkeypatch):
    install(monkeypatch, FakeCursor(rowcount=0))
    assert rt.delete_event_action_filter(999) == 0