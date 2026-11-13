"""mgmt.public 用户映射写原语单测（P3-5c）：飞书三元组 / 清空作用域 / 昵称 / 接入任务 / 外部身份。"""
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


def test_upsert_feishu_creates_row_then_updates(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    out = mg.upsert_user_mapping_feishu("u1", "ou_x", "张三", "cli_a")
    assert out == {"action": "updated", "created": 1, "changed": 1}
    sql0, args0 = cur.executed[0]
    assert sql0 == "INSERT IGNORE INTO unified_user_mapping (user_id) VALUES (%s)"
    assert args0 == ("u1",)
    sql1, args1 = cur.executed[1]
    assert "SET feishu_open_id=%s, feishu_name=%s, app_id=%s, updated_at=NOW()" in sql1
    assert args1 == ("ou_x", "张三", "cli_a", "u1")


def test_clear_by_app_scope(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=3))
    assert mg.clear_user_mapping_feishu(app_id="cli_a") == 3
    sql, args = cur.executed[0]
    assert sql.endswith("WHERE app_id=%s")
    assert args == ("cli_a",)


def test_clear_by_user_scope(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert mg.clear_user_mapping_feishu(user_id="u1") == 1
    sql, args = cur.executed[0]
    assert sql.endswith("WHERE user_id=%s")
    assert args == ("u1",)


def test_clear_requires_scope(monkeypatch):
    install(monkeypatch, FakeCursor())
    with pytest.raises(ValueError):
        mg.clear_user_mapping_feishu()


def test_nickname_sets_row_and_value(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    out = mg.set_user_mapping_nickname("u1", "昵称")
    assert out["action"] == "updated" and out["created"] == 1
    assert cur.executed[0][0].startswith("INSERT IGNORE INTO unified_user_mapping")
    sql, args = cur.executed[1]
    assert "SET gitcode_nickname=%s, updated_at=NOW()" in sql
    assert args == ("昵称", "u1")


def test_delete_onboard_task(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=2))
    assert mg.delete_feishu_onboard_task("cli_a") == 2
    assert cur.executed[0] == ("DELETE FROM feishu_onboard_task WHERE app_id=%s", ("cli_a",))


def test_delete_onboard_task_empty_app_is_noop(monkeypatch):
    cur = install(monkeypatch, FakeCursor())
    assert mg.delete_feishu_onboard_task("") == 0
    assert cur.executed == []


def test_mark_external_identity_expired(monkeypatch):
    cur = install(monkeypatch, FakeCursor(rowcount=1))
    assert mg.mark_external_identity_expired("u1", "gitcode") == 1
    sql, args = cur.executed[0]
    assert sql == ("UPDATE external_identities SET status=4 "
                   "WHERE grape_user_id=%s AND provider=%s AND status=1")
    assert args == ("u1", "gitcode")