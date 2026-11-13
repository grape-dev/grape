"""runtime.public device 凭据原语单测（P3-5f-3）：幂等登记 + 15s 指标刷新。"""
import contextlib

from src.domains.runtime import public as rt
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, rowcount=1, lastrowid=31, rows=None, fail_on_insert=None):
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.rows = rows or []
        self.fail_on_insert = fail_on_insert
        self.executed = []

    def execute(self, sql, args=None):
        s = " ".join(sql.split())
        self.executed.append((s, args))
        if self.fail_on_insert and s.startswith("INSERT"):
            raise Exception(self.fail_on_insert)
        return self.rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass


def install(monkeypatch, cur):
    @contextlib.contextmanager
    def _ctx(domain):
        assert domain == "runtime"
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    return cur


def test_ensure_device_credential_existing_row(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rows=[(7,)]))
    out = rt.ensure_device_credential("host-a", "hash", "10.0.0.1")
    assert out == {"action": "exists", "id": 7}
    assert len(cur.executed) == 1          # 只 SELECT，不 INSERT


def test_ensure_device_credential_inserts_when_missing(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rows=[], lastrowid=42))
    out = rt.ensure_device_credential("host-a", "hash", "10.0.0.1")
    assert out == {"action": "inserted", "id": 42}
    sql, args = cur.executed[1]
    assert sql.startswith("INSERT INTO runtime_credentials")
    assert args == ("hash", "host-a", "host-a", "10.0.0.1")


def test_ensure_device_credential_duplicate_is_a_value(monkeypatch):
    install(monkeypatch, FakeCursor(rows=[], fail_on_insert="Duplicate entry 'host-a' for key 'name'"))
    assert rt.ensure_device_credential("host-a", "hash", "")["action"] == "duplicate"


def test_touch_credential_metrics_columns_and_order(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    n = rt.touch_credential_metrics("host-a", "2026-11-13T15:44:51+00:00", "10.0.0.1",
                                    1.5, 2.5, 3.5, 4.5, 5.5, 6.5, "0.10 0.20")
    assert n == 1
    sql, args = cur.executed[0]
    assert sql == ("UPDATE runtime_credentials SET last_seen_at=%s, last_ip=%s, cpu_percent=%s, "
                   "memory_percent=%s, disk_percent=%s, tmp_percent=%s, bandwidth_percent=%s, "
                   "bandwidth_mbps=%s, load_avg=%s WHERE device_name=%s AND scope='global'")
    assert args == ("2026-11-13T15:44:51+00:00", "10.0.0.1", 1.5, 2.5, 3.5, 4.5, 5.5, 6.5,
                    "0.10 0.20", "host-a")