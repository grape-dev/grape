"""表归属副本审计 + 安全清理（S1–S10）。

背景：契约层单点（跨域读只走 owner 域 public），但 DDL 层带历史副本 ——
非 owner 库里留有同名 BASE TABLE（不是视图），裸 SQL 查它成功返回 0 行、不报错，
于是「库里 0 行」被当成「没配置」（本仓已误判两次）。

两个原语的契约：
  audit_foreign_copies  只发现不修改，区分 empty_copy / divergent_copy
  drop_empty_copy       只删 0 行副本，非 0 行/非法表名一律拒绝

场景表（对应 docs/weekly-11/table-ownership-copy-audit-scenarios.md）：
  S1 空副本 → empty_copy        S6  drop 0 行副本成功
  S2 非空副本 → divergent_copy  S7  drop 拒绝非 0 行
  S3 视图形态 → 不出现           S8  drop 拒绝非法表名（注入）
  S4 owner 自己的表 → 不出现      S9  迁移默认只告警、不动数据
  S5 库名判不出域（单库/临时库） → []，不猜
"""
from __future__ import annotations

import pytest

from src.kernel import table_ownership as own


class _FakeConn:
    """最小假连接：information_schema 行 + COUNT + DROP 记录。"""

    def __init__(self, tables: dict[str, tuple[str, int]], dbname: str = "grape_lfr_mgmt"):
        self.tables = tables          # name -> (table_type, row_count)
        self.dbname = dbname
        self.dropped: list[str] = []

    def execute(self, sql: str, params=None):
        s = sql.strip()
        if "DATABASE()" in s:
            return _Cur([(self.dbname,)])
        if "information_schema.TABLES" in s:
            rows = [(n,) for n, (tt, _n) in self.tables.items() if tt == "BASE TABLE"]
            return _Cur(rows)
        if s.upper().startswith("SELECT COUNT(*)"):
            name = s.split("`")[1]
            return _Cur([(self.tables[name][1],)])
        if s.upper().startswith("DROP TABLE"):
            name = s.split("`")[1]
            self.dropped.append(name)
            self.tables.pop(name, None)
            return _Cur([])
        raise AssertionError(f"unexpected sql: {sql}")


class _Cur:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


TABLE = "grape_t_repo_credentials"      # owner=insight（configs/table_ownership.json:75）
OWNER_TABLE = "managed_repos"           # owner=mgmt
OTHER = "agent_tasks"                   # owner=runtime


def test_s1_empty_copy_is_classified_empty(monkeypatch):
    """S1：本库是 mgmt，insight 的表是 BASE TABLE 且 0 行 → empty_copy（可清理）。"""
    conn = _FakeConn({TABLE: ("BASE TABLE", 0)})
    items = own.audit_foreign_copies(conn, "grape_lfr_mgmt")

    assert len(items) == 1
    assert items[0]["table"] == TABLE
    assert items[0]["owner"] == "insight"
    assert items[0]["rows"] == 0
    assert items[0]["kind"] == "empty_copy"


def test_s2_divergent_copy_is_not_deletable(monkeypatch):
    """S2：非 0 行副本（227 实测形态：rc 基表在 runtime）→ divergent_copy。"""
    conn = _FakeConn({TABLE: ("BASE TABLE", 20)})
    items = own.audit_foreign_copies(conn, "grape_dev_runtime")

    assert items and items[0]["kind"] == "divergent_copy"
    assert items[0]["rows"] == 20
    # 非空副本绝不能被清理
    with pytest.raises(RuntimeError):
        own.drop_empty_copy(conn, TABLE)


def test_s3_view_is_not_reported(monkeypatch):
    """S3：视图形态（227 的 mgmt/insight 里 rc 是 VIEW）→ 不出现，属正确形态。"""
    conn = _FakeConn({TABLE: ("VIEW", 0)})
    assert own.audit_foreign_copies(conn, "grape_dev_mgmt") == []


def test_s4_owner_table_is_not_reported(monkeypatch):
    """S4：本域 owner 的表不报（managed_repos owner=mgmt，在 mgmt 库属正常）。"""
    conn = _FakeConn({OWNER_TABLE: ("BASE TABLE", 262)})
    assert own.audit_foreign_copies(conn, "grape_lfr_mgmt") == []


def test_s5_unknown_db_name_returns_empty(monkeypatch):
    """S5：库名判不出域（单库/临时库）→ 不猜，返回空。"""
    conn = _FakeConn({TABLE: ("BASE TABLE", 0)})
    assert own.audit_foreign_copies(conn, "some_random_db") == []
    assert own.audit_foreign_copies(conn, "") == []


def test_s6_drop_empty_copy_succeeds(monkeypatch):
    """S6：0 行副本可删（开关打开时的清理动作）。"""
    conn = _FakeConn({TABLE: ("BASE TABLE", 0)})
    assert own.drop_empty_copy(conn, TABLE) is True
    assert conn.dropped == [TABLE]


def test_s7_drop_refuses_non_empty(monkeypatch):
    """S7：非 0 行一律拒绝（数据可能只在这份里）。"""
    conn = _FakeConn({TABLE: ("BASE TABLE", 3)})
    with pytest.raises(RuntimeError) as ei:
        own.drop_empty_copy(conn, TABLE)
    assert "refuse to drop non-empty" in str(ei.value)
    assert conn.dropped == []


def test_s8_drop_refuses_unsafe_table_name(monkeypatch):
    """S8：表名白名单校验（防注入；表名虽来自 information_schema 仍校验）。"""
    conn = _FakeConn({})
    for bad in ("grape_t`;(DROP TABLE x)", "a b", "", "x" * 65):
        with pytest.raises(ValueError):
            own.drop_empty_copy(conn, bad)
    assert conn.dropped == []


def test_s9_migration_warns_without_touching_data(monkeypatch):
    """S9：迁移默认只告警 —— 不 DROP 任何表（含 0 行副本）。"""
    from src.api import db_setup as D

    conn = _FakeConn({TABLE: ("BASE TABLE", 0), OTHER: ("BASE TABLE", 5)}, "grape_lfr_mgmt")
    monkeypatch.setattr(D, "db", conn, raising=False)
    monkeypatch.setenv("GRAPE_TABLE_COPY_CLEANUP", "")
    monkeypatch.setattr(D.log, "warning", lambda *a, **k: None)

    D._fn_table_ownership_copy_audit()

    assert conn.dropped == [], "未开开关时不得删任何副本"


def test_s10_cleanup_switch_only_drops_empty_copies(monkeypatch):
    """S10：开 GRAPE_TABLE_COPY_CLEANUP=1 → 只删 empty_copy，divergent 保留并告警。"""
    from src.api import db_setup as D

    conn = _FakeConn({TABLE: ("BASE TABLE", 0), OTHER: ("BASE TABLE", 5)}, "grape_lfr_mgmt")
    monkeypatch.setattr(D, "db", conn, raising=False)
    monkeypatch.setenv("GRAPE_TABLE_COPY_CLEANUP", "1")
    monkeypatch.setattr(D.log, "warning", lambda *a, **k: None)

    D._fn_table_ownership_copy_audit()

    assert conn.dropped == [TABLE], "只允许删 0 行副本"
    assert OTHER in conn.tables, "非空副本必须保留（可能是该部署真源）"
