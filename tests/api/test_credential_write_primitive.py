"""runtime.public 凭据写原语单测（P3-5f-3）：rc 插入/改名/scope/删除、仓库凭据 upsert/清空。"""
import contextlib

from src.domains.runtime import public as rt
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, rowcount=1, lastrowid=55, fail_on_insert=None):
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.fail_on_insert = fail_on_insert
        self.executed = []

    def execute(self, sql, args=None):
        s = " ".join(sql.split())
        self.executed.append((s, args))
        if self.fail_on_insert and s.startswith("INSERT"):
            raise Exception(self.fail_on_insert)
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


def test_insert_runtime_credential_returns_id(monkeypatch):
    cur = install(monkeypatch, FakeCursor(lastrowid=55))
    out = rt.insert_runtime_credential("h", "cred-a", "repo", "gitcode:o/r", "liufei",
                                       "gitcode:o/r", "org1")
    assert out == {"action": "inserted", "id": 55}
    sql, args = cur.executed[0]
    assert sql.startswith("INSERT INTO runtime_credentials")
    assert args == ("h", "cred-a", "repo", "gitcode:o/r", "liufei", "gitcode:o/r", "org1")


def test_insert_runtime_credential_duplicate_is_a_value_not_exception(monkeypatch):
    """重名必须返回 duplicate（否则意图会被无限重放）。"""
    install(monkeypatch, FakeCursor(fail_on_insert="Duplicate entry 'cred-a' for key 'runtime_credentials.name'"))
    out = rt.insert_runtime_credential("h", "cred-a", "user", "", "u", "", "")
    assert out == {"action": "duplicate", "id": 0}


def test_rename_runtime_credential_active_only(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.rename_runtime_credential("cid-1", "new-name") == 1
    assert cur.executed[0] == (
        "UPDATE runtime_credentials SET name=%s WHERE id=%s AND status='active'",
        ("new-name", "cid-1"))


def test_set_runtime_credential_scope(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.set_runtime_credential_scope("cid-1", "repo", "gitcode:o/r", "gitcode:o/r", "org") == 1
    sql, args = cur.executed[0]
    assert sql == ("UPDATE runtime_credentials SET scope=%s, repo_key=%s, owner_id=%s, org_id=%s "
                   "WHERE id=%s")
    assert args == ("repo", "gitcode:o/r", "gitcode:o/r", "org", "cid-1")


def test_delete_runtime_credential(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.delete_runtime_credential("cid-1") == 1
    assert cur.executed[0] == ("DELETE FROM runtime_credentials WHERE id=%s", ("cid-1",))


def test_upsert_repo_credential_updates_existing(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    out = rt.upsert_repo_credential("gitcode:o/r", "bot", "enc", "2026-11-13 23:00:00")
    assert out["action"] == "updated"
    assert len(cur.executed) == 1
    assert cur.executed[0][0].startswith("UPDATE grape_t_repo_credentials SET value=%s")


def test_upsert_repo_credential_inserts_when_missing(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=0))
    out = rt.upsert_repo_credential("gitcode:o/r", "bot", "enc", "T")
    assert out["action"] == "inserted"
    assert out["id"]
    assert cur.executed[1][0].startswith("INSERT INTO grape_t_repo_credentials")


def test_clear_repo_credential_keeps_row(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.clear_repo_credential("gitcode:o/r", "bot", "T") == 1
    sql, args = cur.executed[0]
    assert sql == ("UPDATE grape_t_repo_credentials SET value='', updated_at=%s "
                   "WHERE repo_key=%s AND cred_type=%s")
    assert args == ("T", "gitcode:o/r", "bot")