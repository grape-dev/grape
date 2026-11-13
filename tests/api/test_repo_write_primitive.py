"""mgmt.public 写原语单测（P3-5a）：managed_repos 列白名单 + SQL 组装 + rowcount 语义。"""
import contextlib

import pytest

from src.domains.mgmt import public as mg
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((" ".join(sql.split()), args))
        return self.rowcount

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def close(self):
        pass


def install(monkeypatch, cur):
    @contextlib.contextmanager
    def _ctx(domain):
        assert domain == "mgmt"
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    return cur


def test_set_repo_fields_updates_whitelisted_column(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    n = mg.set_repo_fields("gitcode:o/r", {"deploy_url": "http://x"})
    assert n == 1
    sql, args = cur.executed[0]
    assert sql == "UPDATE managed_repos SET deploy_url=%s WHERE repo_key=%s"
    assert args == ("http://x", "gitcode:o/r")


def test_set_repo_fields_multi_column_is_ordered(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=2))
    n = mg.set_repo_fields("gitcode:o/r", {"bot_token_status": "expired", "deploy_url": "u"})
    assert n == 2
    sql, args = cur.executed[0]
    assert "bot_token_status=%s" in sql and "deploy_url=%s" in sql
    assert args == ("expired", "u", "gitcode:o/r")


def test_set_repo_fields_rejects_unlisted_column(monkeypatch):
    install(monkeypatch, FakeCursor())
    with pytest.raises(ValueError):
        mg.set_repo_fields("gitcode:o/r", {"bot_gitcode_token": "secret"})
    with pytest.raises(ValueError):
        mg.set_repo_fields("gitcode:o/r", {"deploy_url": "u", "is_admin": 1})


def test_set_repo_fields_empty_is_noop(monkeypatch):
    cur = install(monkeypatch, FakeCursor())
    assert mg.set_repo_fields("gitcode:o/r", {}) == 0
    assert cur.executed == []


def test_set_repo_fields_missing_repo_returns_zero(monkeypatch):
    install(monkeypatch, FakeCursor(rowcount=0))
    assert mg.set_repo_fields("gitcode:missing/repo", {"deploy_url": "u"}) == 0


def test_ensure_repo_row_is_insert_ignore(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert mg.ensure_repo_row("gitcode:o/r", "o", "r") == 1
    sql, args = cur.executed[0]
    assert sql.startswith("INSERT IGNORE INTO managed_repos")
    assert args == ("gitcode:o/r", "o", "r", "o/r")


def test_ensure_repo_row_full_name_override(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=0))
    assert mg.ensure_repo_row("gitcode:o/r", "o", "r", "o/r.git") == 0
    assert cur.executed[0][1][3] == "o/r.git"


def test_ensure_repo_row_existing_returns_zero(monkeypatch):
    install(monkeypatch, FakeCursor(rowcount=0))
    assert mg.ensure_repo_row("gitcode:o/r", "o", "r") == 0