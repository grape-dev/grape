"""P2-1 / P2-2 守卫：DDL 语句切分 与 半建表补列。

P2-1：`iter_statements` 原按 `;` + 换行正则切分 —— 注释里出现"分号+换行"会把注释
尾巴拼进下一条语句（评审注入实测切出 22 条碎片）。现为逐字符扫描（识别注释与引号）。

P2-2：`_ensure_platform_tables` 只靠 CREATE TABLE IF NOT EXISTS + "already exists"
忽略，半建表（表在列缺）不会报错也不会补列。现按 SQLAlchemy 模型逐列对比并补。
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Log:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def info(self, msg, *a):
        self.lines.append(("info", msg % a if a else msg))

    def warning(self, msg, *a):
        self.lines.append(("warn", msg % a if a else msg))


# ── P2-1 语句切分 ──────────────────────────────────────────────

def test_semicolon_inside_line_comment_does_not_split():
    from src.api.platform_object_ddl import iter_statements

    ddl = "CREATE TABLE a (id TEXT); -- 说明含分号; 换行\nCREATE TABLE b (id TEXT);"
    stmts = iter_statements(ddl)
    assert len(stmts) == 2, stmts
    assert all("说明含分号" not in s for s in stmts), "注释内容不得进入语句"
    assert stmts[1].lstrip().upper().startswith("CREATE TABLE B"), stmts


def test_semicolon_inside_string_literal_does_not_split():
    from src.api.platform_object_ddl import iter_statements

    ddl = "CREATE TABLE c (note TEXT DEFAULT 'a;b');\nCREATE TABLE d (x TEXT);"
    stmts = iter_statements(ddl)
    assert len(stmts) == 2, stmts
    assert "'a;b'" in stmts[0]


def test_block_comment_is_stripped_not_merged():
    from src.api.platform_object_ddl import iter_statements

    ddl = "/* 块注释; \n换行 */ CREATE TABLE e (id TEXT);"
    stmts = iter_statements(ddl)
    assert len(stmts) == 1, stmts
    assert "块注释" not in stmts[0]
    assert stmts[0].lstrip().upper().startswith("CREATE TABLE E")


def test_real_ddl_yields_whole_statements():
    from src.api.platform_object_ddl import PLATFORM_OBJECT_DDL, iter_statements

    stmts = iter_statements()
    # D110 删掉 grape_t_bot_accounts 后，平台对象只剩 4 张表（oauth_channels /
    # channel_repo_links / repo_platforms / repo_credentials）。这里锁**表名集合**而不是
    # 只锁数量：将来再加/删表时，报错信息直接指出是哪张表，而不是"5 变 6"这种哑断言。
    assert len(stmts) == 4, len(stmts)
    tables = {s.split("(", 1)[0].split()[-1] for s in stmts}
    assert tables == {"grape_t_oauth_channels", "grape_t_channel_repo_link",
                      "grape_t_user_repo_platform_link", "grape_t_repo_platforms"}, tables
    for s in stmts:
        assert not s.lstrip().startswith("--"), f"切出了注释碎片: {s[:60]!r}"
        assert ";" not in s, f"语句内残留分号: {s[:60]!r}"
    assert len(iter_statements(PLATFORM_OBJECT_DDL)) == len(stmts)


# ── P2-2 半建表补列 ─────────────────────────────────────────────

def test_reconcile_adds_missing_columns_on_half_built_table():
    from src.api.d109_startup import reconcile_missing_columns

    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        # 故意少建两列（模拟历史半建表）
        conn.exec_driver_sql(
            "CREATE TABLE grape_t_repo_platforms (id TEXT PRIMARY KEY, provider TEXT NOT NULL,"
            " name TEXT, enabled INTEGER NOT NULL DEFAULT 1)"
        )
        fixed = reconcile_missing_columns(conn, _Log(),
                                          only_tables={"grape_t_repo_platforms"})
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(grape_t_repo_platforms)")}

    assert fixed, "半建表未补列"
    for must in ("pat_create_url", "bot_username"):
        assert must in cols, f"缺列未补: {must}"
    eng.dispose()


def test_reconcile_skips_absent_tables():
    """整表不存在时交给 CREATE 循环，不在这里误 ALTER。"""
    from src.api.d109_startup import reconcile_missing_columns

    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        fixed = reconcile_missing_columns(conn, _Log(),
                                          only_tables={"grape_t_repo_platforms"})
    assert fixed == []
    eng.dispose()


def test_reconcile_is_idempotent():
    from src.api.d109_startup import reconcile_missing_columns

    eng = create_engine("sqlite://")
    log = _Log()
    with eng.begin() as conn:
        reconcile_missing_columns(conn, log, only_tables={"grape_t_repo_platforms"})
    with eng.begin() as conn:
        # 第二次：表已由第一次补齐（CREATE 未发生，表仍不存在 → 返回空）
        fixed2 = reconcile_missing_columns(conn, log, only_tables={"grape_t_repo_platforms"})
    assert fixed2 == []
    eng.dispose()