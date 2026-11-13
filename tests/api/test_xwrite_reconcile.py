"""P3-6 对账/归档脚本的单测：口径计算与 SQL 形状（不连库）。"""
import time

from scripts import xwrite_reconcile as xr


class FakeCursor:
    def __init__(self, seq):
        self.seq = list(seq)          # 每个 execute 依次消费 (rows, rowcount)
        self.rowcount = 0
        self.executed = []
        self._rows = []

    def execute(self, sql, args=None):
        self.executed.append((" ".join(sql.split()), args))
        rows, rc = self.seq.pop(0)
        self._rows = rows or []
        self.rowcount = rc
        return rc

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


def test_archive_dry_run_only_counts(monkeypatch):
    cur = FakeCursor([([(12,)], 0)])
    monkeypatch.setattr(xr, "_connect", lambda d: FakeConn(cur))
    out = xr.archive("runtime", 7, apply=False, now=1_700_000_000)
    assert out["would_delete"] == 12 and out["deleted"] == 0
    sql, args = cur.executed[0]
    assert "status='applied'" in sql and "updated_at < %s" in sql
    assert args == (1_700_000_000 - 7 * 86400,)


def test_archive_apply_loops_until_short_batch(monkeypatch):
    cur = FakeCursor([([], 500), ([], 500), ([], 37)])
    monkeypatch.setattr(xr, "_connect", lambda d: FakeConn(cur))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    out = xr.archive("mgmt", 7, apply=True, batch=500, now=1_700_000_000)
    assert out["deleted"] == 1037 and out["batches"] == 3
    assert all("LIMIT 500" in s for s, _ in cur.executed)


def test_collect_reports_counts_and_dead_sample(monkeypatch):
    cur = FakeCursor([
        ([("applied", 45), ("failed", 2)], 0),
        ([(2, 1_699_999_000)], 0),
        ([("repo_field_update", 16)], 0),
        ([(7, "scene_nodes_update", 5, "k", "boom")], 0),
    ])
    monkeypatch.setattr(xr, "_connect", lambda d: FakeConn(cur))
    rep = xr.collect("runtime", now=1_700_000_000)
    assert rep["counts"] == {"pending": 0, "applied": 45, "failed": 2, "dead": 0}
    assert rep["total"] == 47 and rep["open"] == 2
    assert rep["oldest_open_age_s"] == 1000
    assert rep["dead_sample"][0]["attempts"] == 5


def test_collect_missing_dsn_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(xr, "_dsn", lambda d: "")
    rep = xr.collect("insight")
    assert rep["error"] == "no DSN"


def test_requeue_only_touches_dead(monkeypatch):
    cur = FakeCursor([([], 1)])
    monkeypatch.setattr(xr, "_connect", lambda d: FakeConn(cur))
    out = xr.requeue("runtime", 99)
    assert out["requeued"] == 1
    sql, args = cur.executed[0]
    assert "status='failed'" in sql and "status='dead'" in sql
    assert args[1] == 99