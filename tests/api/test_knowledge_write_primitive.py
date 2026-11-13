"""mgmt.public 知识库写原语单测（P3-5b）：upsert / ref / delete / replace 的 SQL 与计数语义。"""
import contextlib

from src.domains.mgmt import public as mg
from src.kernel import db_registry


class SeqCursor:
    """按顺序返回预设 rowcount；SELECT id 用 ids 供给 fetchall/fetchone。"""

    def __init__(self, ids=None, rowcounts=None, lastrowid=1234):
        self.executed = []
        self.ids = list(ids or [])
        self.rowcounts = list(rowcounts or [])
        self.lastrowid = lastrowid
        self.rowcount = 0

    def execute(self, sql, args=None):
        s = " ".join(sql.split())
        self.executed.append((s, args))
        if s.startswith("SELECT id FROM agent_shared_knowledge"):
            self.rowcount = len(self.ids)
            return self.rowcount
        self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 1
        return self.rowcount

    def fetchall(self):
        return [(i,) for i in self.ids]

    def fetchone(self):
        return (self.ids[0],) if self.ids else None

    def close(self):
        pass


def install(monkeypatch, cur):
    @contextlib.contextmanager
    def _ctx(domain):
        assert domain == "mgmt"
        yield cur

    monkeypatch.setattr(db_registry, "cursor", _ctx)
    return cur


def test_baseline_upsert_updates_existing_row(monkeypatch):
    cur = install(monkeypatch, SeqCursor(ids=[77], rowcounts=[1]))
    out = mg.upsert_baseline_knowledge("gitcode:o/r", "项目说明", "desc",
                                       "baseline/project.md", "obs/key", "abc123")
    assert out["action"] == "updated" and out["id"] == 77 and out["changed"] == 1
    sql, args = cur.executed[1]
    assert sql.startswith("UPDATE agent_shared_knowledge SET status='accepted'")
    assert args == ("obs/key", "abc123", "项目说明", "desc", 77)


def test_baseline_upsert_inserts_when_missing(monkeypatch):
    cur = install(monkeypatch, SeqCursor(ids=[], rowcounts=[1], lastrowid=555))
    out = mg.upsert_baseline_knowledge("gitcode:o/r", "项目说明", "desc",
                                       "baseline/project.md", "", "abc123")
    assert out["action"] == "inserted" and out["id"] == 555
    sql, args = cur.executed[1]
    assert "INSERT INTO agent_shared_knowledge" in sql
    assert args == ("gitcode:o/r", "baseline/project.md", "", "abc123", "项目说明", "desc")
    assert "VALUES ('baseline'" in sql and "'accepted'" in sql


def test_ensure_ref_is_insert_ignore(monkeypatch):
    cur = install(monkeypatch, SeqCursor(rowcounts=[1]))
    assert mg.ensure_scene_knowledge_ref(9) == 1
    sql, args = cur.executed[0]
    assert sql.startswith("INSERT IGNORE INTO scene_knowledge_ref")
    assert args == (9, "", "", 0, "system")
    # 已存在 → 0
    install(monkeypatch, SeqCursor(rowcounts=[0]))
    assert mg.ensure_scene_knowledge_ref(9) == 0


def test_delete_event_knowledge_deletes_refs_then_rows(monkeypatch):
    cur = install(monkeypatch, SeqCursor(ids=[3, 5], rowcounts=[2, 2]))
    out = mg.delete_event_knowledge("gitcode:o/r", "issue", "", 42)
    assert out == {"knowledge": 2, "refs_deleted": 2, "rows_deleted": 2}
    assert len(cur.executed) == 3
    assert cur.executed[1][0].startswith("DELETE FROM scene_knowledge_ref WHERE knowledge_id IN")
    assert cur.executed[1][1] == (3, 5)
    assert cur.executed[2][0].startswith("DELETE FROM agent_shared_knowledge")
    assert cur.executed[2][1] == ("gitcode:o/r", "issue", "", 42)


def test_delete_event_knowledge_without_rows_skips_ref_delete(monkeypatch):
    cur = install(monkeypatch, SeqCursor(ids=[], rowcounts=[0]))
    out = mg.delete_event_knowledge("gitcode:o/r", "project", "", 0)
    assert out == {"knowledge": 0, "refs_deleted": 0, "rows_deleted": 0}
    assert len(cur.executed) == 2          # 只有 SELECT + 主表 DELETE


def test_replace_scene_skill_ref_deletes_then_inserts(monkeypatch):
    cur = install(monkeypatch, SeqCursor(rowcounts=[4, 1, 0, 1]))
    out = mg.replace_scene_skill_ref(31, [{"skill_id": 7, "skill_name": "a"},
                                          {"skill_id": 0, "skill_name": "b"},
                                          {"skill_id": 8, "skill_name": "c"}])
    assert out == {"deleted": 4, "inserted": 2}
    assert cur.executed[0][0] == "DELETE FROM scene_skill_ref WHERE scene_id=%s"
    assert cur.executed[0][1] == (31,)
    assert [e[1] for e in cur.executed[1:]] == [(31, 7, "a"), (31, 0, "b"), (31, 8, "c")]


def test_replace_scene_skill_ref_empty_list(monkeypatch):
    cur = install(monkeypatch, SeqCursor(rowcounts=[0]))
    out = mg.replace_scene_skill_ref(32, [])
    assert out == {"deleted": 0, "inserted": 0}
    assert len(cur.executed) == 1