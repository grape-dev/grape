"""runtime.public 命名查询白名单单测（P4 读路径）。"""
import contextlib
import pytest

from src.domains.runtime import public as rt
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, one=("x",), rows=None):
        self._one = one
        self._rows = rows or []
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((" ".join(sql.split()), args))
        return 1

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows

    def close(self):
        pass


def install(monkeypatch, cur):
    @contextlib.contextmanager
    def _ctx(domain):
        assert domain == "runtime"
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    return cur


def test_read_one_uses_whitelisted_sql(monkeypatch):
    cur = install(monkeypatch, FakeCursor(one=(5, "opencode")))
    assert rt.read_one("runtime_host_name", (5,)) == (5, "opencode")
    sql, args = cur.executed[0]
    assert sql == "SELECT host_name FROM agent_runtimes WHERE id=%s"
    assert args == (5,)


def test_read_all_returns_list(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rows=[(1, "all"), (2, "sre")]))
    assert rt.read_all("engine_tags_enabled", (7,)) == [(1, "all"), (2, "sre")]
    assert "ORDER BY tag" in cur.executed[0][0]


def test_unknown_query_raises(monkeypatch):
    install(monkeypatch, FakeCursor())
    with pytest.raises(KeyError):
        rt.read_one("drop table agent_runtimes", ())


def test_capability_query_keeps_all_wildcard():
    sql = rt._READ_QUERIES["capability_tag_hit"]
    assert "(tag=%s OR tag='all')" in sql and "enabled=1" in sql


def test_account_queries_match_the_real_call_sites():
    # 这两条必须与 engine_select._repo_user_id 里的 SQL 同形（曾因自己编列名 external_login 报 1054）
    login_sql = rt._READ_QUERIES["account_id_by_platform_login"]
    assert "NOT IN ('', '0')" in login_sql and "EXISTS (SELECT 1 FROM external_identities" in login_sql
    assert "LOWER(ei.external_login)=LOWER(%s)" in login_sql
    ext_sql = rt._READ_QUERIES["grape_user_id_by_external_id"]
    assert "FROM external_identities" in ext_sql and "external_id=%s" in ext_sql

# ---------- mgmt 域命名查询（runtime 代码读 mgmt 表） ----------
def test_mgmt_registry_exists_and_is_whitelisted(monkeypatch):
    from src.domains.mgmt import public as mp

    @contextlib.contextmanager
    def _ctx(domain):
        assert domain == "mgmt"
        yield FakeCursor(one=("uid-1",))

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    assert mp.read_one("grape_user_id_by_external_id", ("gitcode", "123")) == ("uid-1",)
    with pytest.raises(KeyError):
        mp.read_one("select * from user_accounts", ())


def test_mgmt_knowledge_queries_match_call_sites():
    from src.domains.mgmt import public as mp
    b = mp._READ_QUERIES["knowledge_id_baseline"]
    assert "scope='baseline'" in b and "archived=0" in b
    s = mp._READ_QUERIES["knowledge_ids_scoped"]
    assert "agent_type=%s" in s and "issue_num=%s" in s
