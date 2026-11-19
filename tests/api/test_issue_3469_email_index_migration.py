"""Issue #3469 O/N 行为锁：uq_active_email 部分唯一化 + 存量邮箱语义归一脚本。

覆盖三块：
1. O 的双态用例（SQLite）：旧形态（全列唯一索引 + status=4 行占键 + email=''）
   → 跑迁移 fn → 旧索引消失 / 新部分索引存在 / 墓碑行不再占键 / 活跃行仍占键 /
   '' 已归一为 NULL / 二次执行零变化；
2. O 的反证用例：索引创建被静默吞掉时 fn 必须 raise（执行器据此不盖章，不假绿）；
3. N 的脚本用例：dry-run 不改数据；--apply 先备份再按规则归一；二次 apply 幂等。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sqlite3

import pytest

import src.api.db_setup as db_setup

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "migrations" / \
    "007_normalize_email_semantics.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("mig_007_email_semantics", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- helpers

def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(db_setup._GRAPE_ACCOUNTS_DDL)
    # 旧形态：无 WHERE 的全列唯一索引（正是 Issue #3469 O 要修掉的形态）
    conn.execute("CREATE UNIQUE INDEX uq_active_email ON grape_accounts(email)")
    conn.commit()
    return conn


def _insert(conn, rid, email, status=1, source=None, verified=0, placeholder=0):
    conn.execute(
        "INSERT INTO grape_accounts "
        "(id, username, email, email_source, status, email_verified, "
        " email_is_placeholder) VALUES (?,?,?,?,?,?,?)",
        (rid, f"u{rid}", email, source, status, verified, placeholder))
    conn.commit()


def _index_sql(conn, name="uq_active_email"):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
    return str(row[0]) if row else ""


def _drop_index_if_any(conn, name):
    conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.commit()


@pytest.fixture
def old_state_db(tmp_path):
    conn = _make_db(tmp_path / "issue3469_old.db")
    _insert(conn, "a1", "active@example.com", status=1, source="user", verified=1)
    _insert(conn, "t4", "gone@example.com", status=4)      # 注销墓碑：不该占键
    _insert(conn, "t2", "banned@example.com", status=2)    # 封禁：不该占键
    _insert(conn, "b1", "", status=1)                      # 空串占键 → 应归一为 NULL
    yield conn
    conn.close()


# ---------------------------------------------------------------- O: 双态

def test_migration_resculpts_index_and_frees_tombstone_rows(old_state_db):
    """旧形态 → 迁移后：旧全列索引消失、新部分索引生效、墓碑行让出 email 键。"""
    assert "WHERE" not in _index_sql(old_state_db).upper()

    stats = db_setup.migrate_active_email_uniqueness(conn=old_state_db)

    sql_now = _index_sql(old_state_db)
    assert sql_now, "uq_active_email 必须存在"
    assert " WHERE " in f" {sql_now} ".upper(), sql_now
    assert "STATUS IN" in sql_now.upper(), sql_now
    assert stats["dropped_old"] is True
    assert stats["created"] is True
    assert stats["blank_to_null"] == 1

    # 索引只有一份（DROP 旧的 + CREATE 新的，没有残留同名索引）
    rows = old_state_db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_active_email'"
    ).fetchall()
    assert len(rows) == 1

    # '' 已归一为 NULL（否则活跃行仍会占键）
    assert old_state_db.execute(
        "SELECT COUNT(*) FROM grape_accounts WHERE email = ''").fetchone()[0] == 0
    blank_row = old_state_db.execute(
        "SELECT email, email_is_placeholder FROM grape_accounts WHERE id='b1'").fetchone()
    assert blank_row[0] is None

    # 墓碑（status=2/4）行持有的邮箱不再占键 → 同 email 的 status=1 可以插入
    _insert(old_state_db, "n1", "gone@example.com", status=1)
    assert old_state_db.execute(
        "SELECT COUNT(*) FROM grape_accounts WHERE email='gone@example.com'").fetchone()[0] == 2
    _insert(old_state_db, "n2", "banned@example.com", status=1)

    # 活跃行仍占键：同 email 再插一条 status=1 必须撞唯一约束
    with pytest.raises(sqlite3.IntegrityError):
        old_state_db.execute(
            "INSERT INTO grape_accounts (id, username, email, status) "
            "VALUES ('dup', 'dup', 'active@example.com', 1)")
    # status=3（未验证的活号）同样参与唯一
    old_state_db.execute(
        "INSERT INTO grape_accounts (id, username, email, status) "
        "VALUES ('v3', 'v3', 'pending@example.com', 3)")
    old_state_db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        old_state_db.execute(
            "INSERT INTO grape_accounts (id, username, email, status) "
            "VALUES ('v3b', 'v3b', 'pending@example.com', 1)")


def test_migration_is_idempotent(old_state_db):
    db_setup.migrate_active_email_uniqueness(conn=old_state_db)
    sql_first = _index_sql(old_state_db)

    stats2 = db_setup.migrate_active_email_uniqueness(conn=old_state_db)
    assert stats2["created"] is False
    assert stats2["dropped_old"] is False
    assert stats2["blank_to_null"] == 0
    assert stats2["already_ok"] is True
    assert _index_sql(old_state_db) == sql_first


def test_fresh_install_index_matches_upgrade_state(tmp_path):
    """新装库口径（迁移模块 `ActiveEmailUniquenessIndex.ensure`）
    必须与升级库终态一致：都是「status IN (1,3) 且 email 非 NULL」的部分唯一索引。"""
    fresh = sqlite3.connect(str(tmp_path / "fresh.db"))
    fresh.execute(db_setup._GRAPE_ACCOUNTS_DDL)
    from src.domains.mgmt.migrations.active_email_uniqueness import ensure_active_email_index
    ensure_active_email_index(fresh, is_mysql=False)
    fresh.commit()
    fresh_sql = f" {_index_sql(fresh)} ".upper()
    assert " WHERE " in fresh_sql and "STATUS IN" in fresh_sql

    upgraded = _make_db(tmp_path / "upgraded.db")
    db_setup.migrate_active_email_uniqueness(conn=upgraded)
    upgraded_sql = f" {_index_sql(upgraded)} ".upper()
    assert " WHERE " in upgraded_sql and "STATUS IN" in upgraded_sql
    fresh.close()
    upgraded.close()


def test_blank_only_db_needs_no_index_rebuild(tmp_path):
    """纯 '' 归一也必须让索引真的落地：空串（哪怕在墓碑行）不残留、索引是部分索引。"""
    conn = _make_db(tmp_path / "blank.db")
    _insert(conn, "b1", "", status=4)      # 墓碑的空串
    _insert(conn, "b2", None, status=1)    # 无邮箱的活号
    db_setup.migrate_active_email_uniqueness(conn=conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM grape_accounts WHERE email = ''").fetchone()[0] == 0
    assert _row_email(conn, "b1") is None
    assert "STATUS IN" in _index_sql(conn).upper()
    conn.close()


def _row_email(conn, rid):
    return conn.execute(
        "SELECT email FROM grape_accounts WHERE id=?", (rid,)).fetchone()[0]


# ---------------------------------------------------------------- O: 反证

class _SilentCreateCursor:
    """把 CREATE UNIQUE INDEX 静默吞掉（模拟兼容层/异常被 log.warning 吞掉）。"""

    def __init__(self, real):
        self._real = real
        self.swallowed = 0

    def execute(self, sql, params=None):
        if sql.strip().upper().startswith("CREATE UNIQUE INDEX"):
            self.swallowed += 1
            return self
        if params is None:
            return self._real.execute(sql)
        return self._real.execute(sql, params)

    def fetchone(self):
        return self._real.fetchone()

    def fetchall(self):
        return self._real.fetchall()


class _SilentCreateConn:
    def __init__(self, real):
        self._real = real
        self.cur = _SilentCreateCursor(real.cursor())

    def cursor(self):
        return self.cur

    def commit(self):
        self._real.commit()


def test_silent_index_creation_failure_raises(tmp_path):
    """索引建不出来时 fn 必须抛（执行器不盖章），不能把「旧索引已删」当成功。"""
    real = _make_db(tmp_path / "silent.db")
    _insert(real, "a1", "active@example.com")
    proxy = _SilentCreateConn(real)

    with pytest.raises(RuntimeError, match="复查失败|创建"):
        db_setup.migrate_active_email_uniqueness(conn=proxy)
    assert proxy.cur.swallowed == 1
    assert _index_sql(real) == ""      # 索引确实缺失（旧索引已删、新索引没建）
    real.close()


def test_non_owner_db_raises_non_owner_object(tmp_path):
    """缺表的域库（非 owner）→ _NonOwnerObject，执行器跳过且不盖章。"""
    conn = sqlite3.connect(str(tmp_path / "no_table.db"))
    with pytest.raises(db_setup._NonOwnerObject):
        db_setup.migrate_active_email_uniqueness(conn=conn)
    conn.close()


# ---------------------------------------------------------------- N: 脚本

@pytest.fixture
def script_db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "issue3469_n.db"))
    conn.execute(db_setup._GRAPE_ACCOUNTS_DDL)
    conn.commit()
    # A: 没邮箱但没打占位标（现网 85 行同形）→ R2
    _insert(conn, "a", None, source=None, placeholder=0)
    # B: provider 私有中转邮箱（现网 27 行）→ R1
    _insert(conn, "b", "123+u@users.noreply.github.com", source="oauth", placeholder=0)
    # C: 非邮箱形态字面串（现网 1 行）→ R1
    _insert(conn, "c", "未公开邮箱", source="oauth", placeholder=0)
    # D: 空串 → R1（email→NULL + 占位）
    _insert(conn, "d", "", source=None, placeholder=0)
    # E: 用户自填的真实邮箱但未验证 → R3
    _insert(conn, "e", "user@example.com", source="user", verified=0, placeholder=0)
    # F: 真实邮箱、来源不是 user → 不动，计数上报
    _insert(conn, "f", "other@example.com", source="oauth", verified=0, placeholder=0)
    # G: 已经是标准占位行 → 不动
    _insert(conn, "g", None, source=None, verified=0, placeholder=1)
    yield conn
    conn.close()


def _row(conn, rid):
    return conn.execute(
        "SELECT email, email_source, email_verified, email_is_placeholder "
        "FROM grape_accounts WHERE id=?", (rid,)).fetchone()


def _bak_tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'grape_accounts_bak_%'").fetchall()]


def test_script_dry_run_changes_nothing(script_db, monkeypatch):
    mod = _load_script()
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", script_db)
    before = script_db.execute(
        "SELECT id, email, email_verified, email_is_placeholder "
        "FROM grape_accounts ORDER BY id").fetchall()

    assert mod.main([]) == 0

    after = script_db.execute(
        "SELECT id, email, email_verified, email_is_placeholder "
        "FROM grape_accounts ORDER BY id").fetchall()
    assert before == after
    assert _bak_tables(script_db) == []


def test_script_apply_normalizes_and_reports(script_db, monkeypatch):
    mod = _load_script()
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", script_db)

    assert mod.main(["--apply"]) == 0

    # 备份表存在（整表复制）
    baks = _bak_tables(script_db)
    assert len(baks) == 1, baks
    assert script_db.execute(f"SELECT COUNT(*) FROM {baks[0]}").fetchone()[0] == 7

    # R1：私有中转 / 非邮箱形态 / 空串 → email NULL，保留 email_source，占位=1
    for rid, kept_source in (("b", "oauth"), ("c", "oauth"), ("d", None)):
        email, source, _verified, placeholder = _row(script_db, rid)
        assert email is None, rid
        assert source == kept_source, rid
        assert placeholder == 1, rid
    # R2：原本 NULL + placeholder=0 → 1
    assert _row(script_db, "a")[3] == 1
    # R3：任何来源的真实邮箱（复审：62 行 verified=0 不能只认 user 来源）→ verified=1
    assert _row(script_db, "e")[2] == 1
    assert _row(script_db, "f") == ("other@example.com", "oauth", 1, 0)
    # 其余不动：已占位行
    assert _row(script_db, "g") == (None, None, 0, 1)
    assert _row(script_db, "g") == (None, None, 0, 1)

    # 幂等自检：再分类应 0 待改
    plan = mod.classify(mod._rows(script_db))
    assert plan["will_null"] == 0
    assert plan["placeholder_fix"] == 0
    assert plan["verify_fix"] == 0


def test_script_apply_is_idempotent(script_db, monkeypatch):
    mod = _load_script()
    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", script_db)

    assert mod.main(["--apply"]) == 0
    snapshot = script_db.execute(
        "SELECT id, email, email_source, email_verified, email_is_placeholder "
        "FROM grape_accounts ORDER BY id").fetchall()
    first_baks = _bak_tables(script_db)
    assert len(first_baks) == 1

    # 第二次：0 待改 → 不再建备份表、数据零变化
    assert mod.main(["--apply"]) == 0
    assert script_db.execute(
        "SELECT id, email, email_source, email_verified, email_is_placeholder "
        "FROM grape_accounts ORDER BY id").fetchall() == snapshot
    assert _bak_tables(script_db) == first_baks

def test_disabled_account_still_occupies_email(tmp_path):
    """复审 P1：status=0（管理员禁用）必须继续占键。

    原口径只认 (1,3) → 禁用后 email 不占键，别人拿同邮箱注册成功；读侧
    `WHERE email=? LIMIT 1` 命中两行时可能取到"已禁用"那一行（真 MySQL 实测）。
    """
    conn = _make_db(tmp_path / "disabled.db")
    db_setup.migrate_active_email_uniqueness(conn=conn)
    _insert(conn, "dis-0", "taken@example.com", status=0)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, "dis-1", "taken@example.com", status=1)
    # 2/4 仍是释放态：注销/合并行不占键
    _insert(conn, "rel-2", "taken@example.com", status=2)
    _insert(conn, "rel-4", "taken@example.com", status=4)
    conn.close()
