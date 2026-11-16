"""runtime.public 凭据域原语单测（P3-5f-2 收尾）：切 scope 合并 / 仓库迁移改 org_id。"""
import contextlib

from src.domains.runtime import public as rt
from src.kernel import db_registry


class ScriptedCursor:
    def __init__(self, ones=None, alls=None, rcs=None):
        self.ones = list(ones or [])
        self.alls = list(alls or [])
        self.rcs = list(rcs or [])
        self.rowcount = 0
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((" ".join(sql.split()), args))
        self.rowcount = self.rcs.pop(0) if self.rcs else 1
        return self.rowcount

    def fetchone(self):
        return self.ones.pop(0) if self.ones else None

    def fetchall(self):
        return self.alls.pop(0) if self.alls else []

    def close(self):
        pass


class NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install_tx(monkeypatch, cur):
    """把 registry 事务与进程主库游标都换成交付的脚本化游标。

    `_any_tx()` 在单库/测试拓扑下**不走 registry**，直接取
    session_manager.db —— 只补 registry 会让组合写原语打到真实库
    （2026-11-16 修：此前 2 条用例因此拿到真实 rowcount/行）。
    """
    @contextlib.contextmanager
    def _tx(domain):
        assert domain == "runtime"
        yield cur

    class _FakeDB:
        def cursor(self):
            return cur

        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(db_registry, "transaction", _tx)
    import src.kernel.session_manager as _sm
    monkeypatch.setattr(_sm, "db", _FakeDB())
    monkeypatch.setattr(_sm, "db_lock", NullLock())
    return cur


def test_change_credential_scope_merges_duplicate_engines(monkeypatch):
    # 同凭据下两条同名引擎行（id 11 / 12）→ 保留 12，删 11，并把能力迁到 12
    rows = [(11, "opencode", 0), (12, "opencode", 1)]
    cur = install_tx(monkeypatch, ScriptedCursor(
        alls=[rows], ones=[None, (1,)], rcs=[1, 1, 1, 1, 0, 0, 1]))
    out = rt.change_credential_scope("7", "repo", "gitcode:o/r", "gitcode:o/r", "org1")
    assert out["credential"] == 1
    assert out["merged"] == {"11": 12}
    assert out["engines"] == 1
    sqls = [e[0] for e in cur.executed]
    assert sqls[0].startswith("UPDATE runtime_credentials SET scope=%s")
    assert any("UPDATE engine_capabilities SET runtime_id=%s" in s for s in sqls)
    assert any(s.startswith("DELETE FROM engine_capabilities WHERE runtime_id IN") for s in sqls)
    assert any(s.startswith("DELETE FROM agent_runtimes WHERE id IN") for s in sqls)
    assert sqls[-1].startswith("UPDATE agent_runtimes SET scope=%s")


def test_change_credential_scope_single_engine_no_merge(monkeypatch):
    cur = install_tx(monkeypatch, ScriptedCursor(
        alls=[[(5, "qa", 1)]], rcs=[1, 1]))
    out = rt.change_credential_scope("9", "user", "", "", "")
    assert out["merged"] == {} and out["engines"] == 1
    assert not any("DELETE FROM agent_runtimes" in s for s, _ in cur.executed)


def test_reassign_repo_org_id_updates_fixed_tables(monkeypatch):
    cur = install_tx(monkeypatch, ScriptedCursor(rcs=[2, 1, 0, 3, 4, 0]))
    out = rt.reassign_repo_org_id("gitcode:o/r", "org-target")
    assert out == {"agent_runtimes": 2, "runtime_credentials": 1, "chat_conversations": 0,
                   "devops_agent_session": 3, "issue": 4, "agent_tasks": 0}
    assert [e[0].split(" SET")[0] for e in cur.executed] == [
        "UPDATE agent_runtimes", "UPDATE runtime_credentials", "UPDATE chat_conversations",
        "UPDATE devops_agent_session", "UPDATE issue", "UPDATE agent_tasks"]
    assert all(e[1] == ("org-target", "gitcode:o/r") for e in cur.executed)