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
        if self._first is not None:
            return self._first
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install(monkeypatch, cur):
    """把 db_registry.cursor 与进程主库游标都换成产出该 FakeCursor 的上下文管理器。

    runtime lib 的单库/测试拓扑回落路径（`_any_cursor` → session_manager.db）
    不经过 db_registry；只补 registry 会让 list_credential_rows 这类函数打到
    真实空库、静默返回 []（2026-11-16 修 s80b 同批红）。
    """
    @contextlib.contextmanager
    def _ctx(domain):
        yield cur

    class _FakeDB:
        def cursor(self):
            return cur

        def commit(self):
            pass

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    monkeypatch.setattr(db_registry, "domain_lock", lambda d: NullLock())
    import src.kernel.session_manager as _sm
    monkeypatch.setattr(_sm, "db", _FakeDB())
    monkeypatch.setattr(_sm, "db_lock", NullLock())
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

def test_get_skill_by_id_maps_fields(monkeypatch):
    cur = install(monkeypatch, FakeCursor(
        rows=[(753, "vod-repo-x", "VOD Repo X", "desc", "hermes", "vod/x", "/p/x", "repo", "u1", "abc")],
        desc=[("id",), ("skill_name",), ("display_name",), ("description",), ("agent_type",),
              ("skill_path",), ("file_path",), ("scope",), ("owner_id",), ("content_hash",)],
    ))
    out = rt.get_skill_by_id(753)
    assert out["id"] == 753 and out["skill_name"] == "vod-repo-x"
    assert out["display_name"] == "VOD Repo X" and out["scope"] == "repo"
    sql, args = cur.executed[0]
    assert "FROM skill WHERE id=%s AND is_active=1" in sql and args == (753,)


def test_get_skill_by_id_defaults_display_and_scope(monkeypatch):
    install(monkeypatch, FakeCursor(
        rows=[(1, "n1", None, None, None, None, None, None, None, None)],
        desc=[("id",)],
    ))
    out = rt.get_skill_by_id(1)
    assert out["display_name"] == "n1"      # 空 display_name 回落 skill_name
    assert out["skill_path"] == "n1"
    assert out["scope"] == "official"       # 空 scope 回落 official
    assert out["content_hash"] == ""


def test_get_skill_by_id_missing(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[], desc=[("id",)], first=None))
    assert rt.get_skill_by_id(99) is None
    assert rt.get_skill_by_id(0) is None     # 假值短路，不查库


def test_get_skill_content_hash_found_and_missing(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[], desc=[("id",)], first=(7, "h1", "/p")))
    assert rt.get_skill_content_hash("n", "repo") == (7, "h1", "/p")

    install(monkeypatch, FakeCursor(rows=[], desc=[("id",)], first=None))
    assert rt.get_skill_content_hash("n") == (0, "", "")


def test_count_skills_sql(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rows=[], desc=[("c",)], first=(473,)))
    assert rt.count_skills() == 473
    assert "WHERE is_active=1" in cur.executed[0][0]
    cur2 = install(monkeypatch, FakeCursor(rows=[], desc=[("c",)], first=(500,)))
    assert rt.count_skills(active_only=False) == 500
    assert "WHERE" not in cur2.executed[0][0]

def test_latest_task_for_event_returns_tuple(monkeypatch):
    row = (11, "completed", 3, 0, "t0", "t1", "ok", "vod/x")
    cur = install(monkeypatch, FakeCursor(rows=[], desc=[("id",)], first=row))
    assert rt.latest_task_for_event(99) == row
    sql, args = cur.executed[0]
    assert "FROM agent_tasks WHERE event_seq=%s" in sql and args == (99,)
    assert "ORDER BY created_at DESC LIMIT 1" in sql


def test_latest_task_for_event_missing(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[], desc=[("id",)], first=None))
    assert rt.latest_task_for_event(-1) is None


def test_task_brief_returns_tuple(monkeypatch):
    row = (42, "hermes", "skill-a", 7, "gitcode:o/r")
    cur = install(monkeypatch, FakeCursor(rows=[], desc=[("event_seq",)], first=row))
    assert rt.task_brief(5) == row
    sql, args = cur.executed[0]
    assert "SELECT event_seq, agent_type, skill, runtime_id, repo_key" in sql
    assert "FROM agent_tasks WHERE id=%s LIMIT 1" in sql and args == (5,)


def test_task_brief_missing(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[], desc=[("id",)], first=None))
    assert rt.task_brief(-1) is None

def test_list_credential_rows_columns_and_passthrough(monkeypatch):
    row = (1, "rt-cred", "dev-1", "global", "", "alice", "active", "t0", "1.2.3.4", "", 1, "")
    cur = install(monkeypatch, FakeCursor(rows=[row], desc=[("id",)]))
    out = rt.list_credential_rows()
    assert out == [row]
    sql, _args = cur.executed[0]
    for col in ("id", "name", "device_name", "scope", "owner_id", "owner_name",
                "status", "last_seen_at", "last_ip", "repo_key", "org_id", "spec"):
        assert col in sql
    assert "FROM runtime_credentials" in sql


def test_list_credential_rows_empty(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[], desc=[("id",)]))
    assert rt.list_credential_rows() == []

