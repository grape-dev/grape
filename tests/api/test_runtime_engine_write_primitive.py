"""runtime.public 引擎行写原语单测（P3-5f-2）：批量 upsert / 默认引擎行 / 标签回写。"""
import contextlib

from src.domains.runtime import public as rt
from src.kernel import db_registry


class ScriptedCursor:
    """按脚本回放结果：one=依次给 fetchone 的值，all=依次给 fetchall，"rc"=依次给 rowcount。"""

    def __init__(self, ones=None, alls=None, rcs=None, lastrowid=99):
        self.ones = list(ones or [])
        self.alls = list(alls or [])
        self.rcs = list(rcs or [])
        self.lastrowid = lastrowid
        self.rowcount = 0
        self.executed = []

    def execute(self, sql, args=None):
        s = " ".join(sql.split())
        self.executed.append((s, args))
        self.rowcount = self.rcs.pop(0) if self.rcs else 1
        return self.rowcount

    def fetchone(self):
        return self.ones.pop(0) if self.ones else None

    def fetchall(self):
        return self.alls.pop(0) if self.alls else []

    def close(self):
        pass


def install_tx(monkeypatch, cur):
    @contextlib.contextmanager
    def _tx(domain):
        assert domain == "runtime"
        yield cur

    monkeypatch.setattr(db_registry, "transaction", _tx)
    return cur


def install_cur(monkeypatch, cur):
    @contextlib.contextmanager
    def _ctx(domain):
        assert domain == "runtime"
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    return cur


def test_upsert_local_runtimes_updates_existing(monkeypatch):
    # SET SESSION(0) → SELECT(1) → UPDATE status(1) → UPDATE display(1) → prune(0) → dedupe(0)
    cur = install_tx(monkeypatch, ScriptedCursor(ones=[(11,)], rcs=[0, 1, 1, 1, 0, 0]))
    out = rt.upsert_local_runtimes(
        [{"name": "OpenCode", "installed": True, "healthy": True, "display_name": "OpenCode"}],
        "global", "", "host-a", "2026-11-13T16:00:00+00:00")
    assert out["upserted"] == 1 and out["inserted"] == 0
    assert out["ids"] == {"opencode": 11}
    sqls = [e[0] for e in cur.executed]
    assert sqls[1].startswith("SELECT id FROM agent_runtimes WHERE name=%s")   # sqls[0] 是 SET SESSION
    assert any(s.startswith("UPDATE agent_runtimes SET status='online'") for s in sqls)
    assert any(s.startswith("UPDATE agent_runtimes SET display_name=%s") for s in sqls)
    assert sqls[-1].startswith("DELETE FROM agent_runtimes WHERE credential_id=0 AND id NOT IN")


def test_upsert_local_runtimes_inserts_and_syncs_tags(monkeypatch):
    # SELECT 未命中 → INSERT → 能力行不存在 → 插 'all' → 读回 tags → 回写 agent_tags
    # SET(0) → SELECT(None) → INSERT → SELECT cap(None) → INSERT cap → SELECT tag → UPDATE tags → prune(0) → dedupe(0)
    cur = install_tx(monkeypatch, ScriptedCursor(
        ones=[None, None], alls=[[("all",)]], rcs=[0, 0, 1, 0, 1, 1, 1, 0, 0]))
    out = rt.upsert_local_runtimes(
        [{"name": "qa-bot", "installed": True, "healthy": True}],
        "global", "", "host-a", "T")
    assert out["inserted"] == 1
    sqls = [e[0] for e in cur.executed]
    assert any(s.startswith("INSERT INTO agent_runtimes") for s in sqls)
    assert any("INSERT INTO engine_capabilities" in s for s in sqls)
    tag_update = [e for e in cur.executed if e[0].startswith("UPDATE agent_runtimes SET agent_tags")]
    assert tag_update and tag_update[0][1][0] == '["all"]'


def test_upsert_local_runtimes_skips_uninstalled_new_agent(monkeypatch):
    cur = install_tx(monkeypatch, ScriptedCursor(ones=[None], rcs=[0, 0, 0, 0]))
    out = rt.upsert_local_runtimes([{"name": "nope", "installed": False}], "global", "", "host-a", "T")
    assert out == {"upserted": 0, "inserted": 0, "pruned": 0, "deduped": 0, "ids": {}}
    assert not any(s.startswith("INSERT INTO agent_runtimes") for s, _ in cur.executed)


def test_upsert_local_runtimes_prune_counts(monkeypatch):
    # SELECT(None, rc=0) → prune(rc=2) → dedupe(rc=1)
    cur = install_tx(monkeypatch, ScriptedCursor(ones=[None], rcs=[0, 0, 2, 1]))
    out = rt.upsert_local_runtimes([{"name": "gone", "installed": False}], "global", "", "host-a", "T")
    assert out["pruned"] == 2 and out["deduped"] == 1


def test_ensure_default_runtime_returns_existing(monkeypatch):
    install_tx(monkeypatch, ScriptedCursor(ones=[(5,)]))
    out = rt.ensure_default_runtime("opencode", "Opencode", 1, "/usr/bin/oc", "1.0", 1, "opencode", "global")
    assert out == {"id": 5, "created": False}


def test_ensure_default_runtime_inserts(monkeypatch):
    cur = install_tx(monkeypatch, ScriptedCursor(ones=[None], lastrowid=77))
    out = rt.ensure_default_runtime("opencode", "Opencode", 1, "/usr/bin/oc", "1.0", 1, "opencode", "global")
    assert out == {"id": 77, "created": True}
    sql, args = cur.executed[-1]
    assert sql.startswith("INSERT INTO agent_runtimes")
    assert args[-1] == "global"


def test_ensure_default_runtime_select_prefers_online_and_joins_credential(monkeypatch):
    cur = install_tx(monkeypatch, ScriptedCursor(ones=[(3,)]))
    rt.ensure_default_runtime("x", "X", 0, "", "", 0, "x", "global")
    sql = cur.executed[0][0]
    assert "LEFT JOIN runtime_credentials" in sql and "ar.status='online'" in sql


def test_set_agent_tags_serializes_json(monkeypatch):
    cur = install_cur(monkeypatch, ScriptedCursor(rcs=[1]))
    assert rt.set_agent_tags(9, ["sre", "qa"]) == 1
    assert cur.executed[0] == ("UPDATE agent_runtimes SET agent_tags=%s WHERE id=%s",
                               ('["sre", "qa"]', 9))