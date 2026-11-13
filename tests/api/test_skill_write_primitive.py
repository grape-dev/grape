"""runtime.public 技能写原语单测（P3-5f-1）：hash / OBS 元数据 / 建行 / 删行 / 点赞计数。"""
import contextlib

from src.domains.runtime import public as rt
from src.kernel import db_registry


class FakeCursor:
    def __init__(self, rowcount=1, lastrowid=77):
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((" ".join(sql.split()), args))
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


def test_update_skill_content_with_file_path(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.update_skill_content(5, "abc", "obs/5.zip") == 1
    assert cur.executed[0] == ("UPDATE skill SET content_hash=%s, file_path=%s WHERE id=%s",
                               ("abc", "obs/5.zip", 5))


def test_update_skill_content_hash_only(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=0))
    assert rt.update_skill_content(5, "abc") == 0
    assert cur.executed[0] == ("UPDATE skill SET content_hash=%s WHERE id=%s", ("abc", 5))


def test_update_skill_obs_writes_nine_columns(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    n = rt.update_skill_obs(3, "obs/3", "S", "issue", "d", "cat", "u1", "h1")
    assert n == 1
    sql, args = cur.executed[0]
    assert sql == ("UPDATE skill SET file_path=%s, display_name=%s, agent_type=%s, "
                   "description=%s, category_id=%s, updated_by=%s, is_active=1, "
                   "content_hash=%s, updated_at=NOW() WHERE id=%s")
    assert args == ("obs/3", "S", "issue", "d", "cat", "u1", "h1", 3)


def test_insert_skill_returns_lastrowid(monkeypatch):
    cur = install(monkeypatch, FakeCursor(lastrowid=91))
    rid = rt.insert_skill("S", "S", "d", "qa", "obs/91", "user", "u1", "u1", "u1", "cat", "h")
    assert rid == 91
    sql, args = cur.executed[0]
    assert sql.startswith("INSERT INTO skill (skill_name, display_name")
    assert args[-2:] == ("cat", "h")


def test_create_skill_row_is_idempotent(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.create_skill_row("S", "d", "qa", "repo", "gitcode:o/r", "u1") == 1
    sql, args = cur.executed[0]
    assert sql.startswith("INSERT IGNORE INTO skill")
    assert args == ("S", "S", "d", "qa", "S", "repo", "gitcode:o/r", "u1", "u1")


def test_delete_skill_row_scoped(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.delete_skill_row("S", "user", "u1") == 1
    assert cur.executed[0] == (
        "DELETE FROM skill WHERE skill_name=%s AND scope=%s AND owner_id=%s", ("S", "user", "u1"))


def test_delete_skill_by_id(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.delete_skill_by_id(5) == 1
    assert cur.executed[0] == ("DELETE FROM skill WHERE id=%s", (5,))


def test_adjust_skill_like_count_up(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.adjust_skill_like_count(5, 1) == 1
    assert cur.executed[0] == ("UPDATE skill SET like_count=like_count+1 WHERE id=%s", (5,))


def test_adjust_skill_like_count_down(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert rt.adjust_skill_like_count(5, -1) == 1
    assert cur.executed[0] == ("UPDATE skill SET like_count=like_count-1 WHERE id=%s", (5,))