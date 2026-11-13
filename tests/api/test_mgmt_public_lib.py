"""mgmt.public 单测：允许列表、SQL 组装、封装函数、health。"""
import contextlib

import pytest

from src.domains.mgmt import public as mg
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, rows=None, first=None):
        self._rows = rows or []
        self._first = first
        self.description = [("c",)]
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


def install(monkeypatch, cur):
    @contextlib.contextmanager
    def _ctx(domain):
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    return cur


def test_unknown_table_rejected():
    with pytest.raises(ValueError):
        mg.fetch_rows("users", ["id"])


def test_unknown_column_rejected():
    with pytest.raises(ValueError):
        mg.fetch_rows("managed_repos", ["id"])       # 该表没有 id 列


def test_fetch_rows_sql_and_params(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rows=[("bot",)]))
    rows = mg.fetch_rows("managed_repos", ["bot_username"], "repo_key=%s", ("r1",), "repo_key", 5)
    assert rows == [("bot",)]
    sql, args = cur.executed[0]
    assert sql == "SELECT bot_username FROM managed_repos WHERE repo_key=%s ORDER BY repo_key LIMIT 5"
    assert args == ("r1",)


def test_get_repo_field(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[("o/r",)], first=("o/r",)))
    assert mg.get_repo_field("r1", "full_name") == "o/r"


def test_get_repo_field_missing(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[], first=None))
    assert mg.get_repo_field("r1", "dev_scene") is None


def test_get_repo_org_id(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rows=[(7,)], first=(7,)))
    assert mg.get_repo_org_id("r1") == 7
    assert "ORDER BY repo_key" in cur.executed[0][0]


def test_mapping_helpers(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[("u1",), ("u2",)]))
    assert mg.list_user_ids_by_open_id("oid") == ["u1", "u2"]

    install(monkeypatch, FakeCursor(rows=[("oid",)], first=("oid",)))
    assert mg.get_feishu_open_id("u1") == "oid"

    install(monkeypatch, FakeCursor(rows=[("u2",), ("u1",), ("u1",)]))
    assert mg.list_all_user_ids() == ["u1", "u2"]     # 去重 + 排序


def test_health_ok_and_fail(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[], first=(239,)))
    out = mg.health()
    assert out["ok"] is True and out["managed_repos"] == 239

    @contextlib.contextmanager
    def _boom(domain):
        raise RuntimeError("down")
        yield

    monkeypatch.setattr(db_registry, "cursor", _boom)
    out2 = mg.health()
    assert out2["ok"] is False and "down" in out2["error"]