"""runtime public lib 的单测：SQL 组装、行转 dict、聚合、幂等写（全部 fake cursor）。"""
import contextlib

from src.domains.runtime import public as rt
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, rows=None, desc=None, first=None, lastrowid=7):
        self._rows = rows or []
        self.description = desc
        self._first = first
        self.lastrowid = lastrowid
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((sql, args))
        return 0

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._first

    def close(self):
        pass


class NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install(monkeypatch, cur):
    """把 db_registry.cursor 换成产出该 FakeCursor 的上下文管理器。"""
    @contextlib.contextmanager
    def _ctx(domain):
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    monkeypatch.setattr(db_registry, "domain_lock", lambda d: NullLock())
    return cur


def test_list_runtimes_maps_rows(monkeypatch):
    cur = FakeCursor(
        rows=[(1, "rt-a", "hermes", "online", 1, 2, "o/r", 1, "t", None, "c", "u")],
        desc=[("id",), ("name",), ("runtime_type",), ("status",), ("org_id",), ("user_id",),
              ("repo_key",), ("healthy",), ("last_heartbeat",), ("spawn_fail_degraded_at",),
              ("created_at",), ("updated_at",)],
    )
    install(monkeypatch, cur)
    rows = rt.list_runtimes(org_id=1, limit=10)
    assert rows[0]["name"] == "rt-a"
    sql, args = cur.executed[0]
    assert "FROM agent_runtimes" in sql and "org_id=%s" in sql and args == (1, 10)


def test_list_runtimes_without_org(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rows=[], desc=[("id",)]))
    assert rt.list_runtimes() == []
    sql, args = cur.executed[0]
    assert "WHERE" not in sql and args == (200,)


def test_get_runtime_empty(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[], desc=[("id",)], first=None))
    assert rt.get_runtime(9) == {}


def test_task_stats_aggregates(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rows=[("done", 3), ("failed", 1)], desc=[("status",), ("n",)]))
    out = rt.task_stats("owner/repo", since="2026-11-01")
    assert out == {"done": 3, "failed": 1}
    sql, args = cur.executed[0]
    assert "GROUP BY status" in sql and args == ("owner/repo", "2026-11-01")


def test_append_task_message_dedupes(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rows=[], desc=None, first=(42,)))
    out = rt.append_task_message("t1", "user", "hi", idempotency_key="k1")
    assert out == {"ok": True, "deduped": True, "id": 42}
    assert len(cur.executed) == 1


def test_append_task_message_inserts_when_missing(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rows=[], desc=None, first=None))
    out = rt.append_task_message("t1", "user", "hi", idempotency_key="k2")
    assert out["ok"] is True and out["deduped"] is False
    assert any("INSERT INTO task_messages" in s for s, _ in cur.executed)


def test_available_false_without_dsn(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    monkeypatch.delenv("GRAPE_DSN_RUNTIME", raising=False)
    db_registry.reset(close=False)
    assert rt.available() is False