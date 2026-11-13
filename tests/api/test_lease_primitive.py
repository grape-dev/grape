"""renew_channel_lease 的 SQL 形状与 CAS 条件单测（不连库）。"""
import contextlib

from src.domains.runtime import public as rt
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.executed = []
        self.description = [("c",)]

    def execute(self, sql, args=None):
        self.executed.append((sql, args))
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
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    return cur


def test_renew_without_expected_token_requires_null_lease(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.renew_channel_lease("feishu:1", "tok-A", "owner-1", 60) is True
    sql, args = cur.executed[0]
    assert "UPDATE grape_t_channel_lease" in sql
    assert "lease_token IS NULL" in sql
    assert "lease_expires_at=%s" in sql
    assert args[0] == "tok-A" and args[1] == "owner-1" and args[-1] == "feishu:1"


def test_renew_with_expected_token_is_cas(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.renew_channel_lease("feishu:1", "tok-A", "owner-1", 120,
                                  expected_token="tok-A") is True
    sql, args = cur.executed[0]
    assert "lease_token IS NULL OR lease_token=%s" in sql
    assert args[-1] == "tok-A"          # expected_token 在参数末尾


def test_renew_reports_failure_when_no_row_changed(monkeypatch):
    install(monkeypatch, FakeCursor(rowcount=0))
    assert rt.renew_channel_lease("feishu:1", "tok-B", "owner-2", 60) is False


def test_expiry_is_offset_by_ttl(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    rt.renew_channel_lease("k", "t", "o", ttl_seconds=300)
    sql, args = cur.executed[0]
    expires, acquired = args[2], args[3]
    assert expires > acquired          # ISO 字符串可直接比较
    assert "T" in expires              # isoformat 带 T