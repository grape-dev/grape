"""login_whitelist 存量字面 'NULL' 占位清洗守卫（D105d 59 RDS 演练发现，挂账小批清偿）。

背景：存量 MySQL 库 github_username/gitcode_username 有历史导入留下的字面
'NULL' 字符串。_ensure_case_insensitive_indexes 的大小写不敏感唯一索引生成列
把它当真实用户名 → 多行 'null' 撞 1062，CREATE UNIQUE INDEX 被 except 吞成
warning，唯一索引静默缺失（59 演练实锤：d105d 文档"遇存量字面 'NULL' 值
1062 跳过"）。

根修：建索引前先 UPDATE 清成空串（UPDATE 基列触发 STORED 生成列重算，旧装
脏生成列同样被治愈）。本文件锁三件事：
1. 清洗把 'NULL'/'null'/' NULL ' 归零，真实用户名不动，NULL 不动；
2. 清洗后唯一索引（SQLite 表达式形态 + MySQL 生成列形态的等价模拟）能建成；
3. 幂等：二次执行零改动。
"""
import sqlite3

import pytest

from src.api.db_setup import _normalize_whitelist_null_literals


DDL = """
CREATE TABLE login_whitelist (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    github_username TEXT DEFAULT '',
    gitcode_username TEXT DEFAULT NULL
)
"""


def _make_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(DDL)
    conn.executemany(
        "INSERT INTO login_whitelist (id, username, github_username, gitcode_username) "
        "VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn


ROWS = [
    (1, "alice", "alice-gh", None),          # 真实用户名，不动
    (2, "bob", "NULL", "NULL"),              # 字面 NULL 占位（大写）
    (3, "carol", "null", None),              # 小写变体
    (4, "dave", " NULL ", "  NULL"),         # 带空白
    (5, "erin", "", ""),                     # 空串，不动
    (6, "frank", None, "real-gitcode"),      # 真 NULL，不动；gitcode 真实名不动
]


def test_null_literals_normalized():
    conn = _make_db(ROWS)
    _normalize_whitelist_null_literals(conn.cursor())
    got = conn.execute(
        "SELECT id, github_username, gitcode_username FROM login_whitelist ORDER BY id"
    ).fetchall()
    assert got == [
        (1, "alice-gh", None),
        (2, "", ""),          # 'NULL' → ''
        (3, "", None),        # 'null' → ''
        (4, "", ""),          # 空白包裹的 'NULL' → ''
        (5, "", ""),          # 空串保持
        (6, None, "real-gitcode"),
    ]


def test_unique_index_builds_after_cleanup_sqlite_form():
    conn = _make_db(ROWS)
    _normalize_whitelist_null_literals(conn.cursor())
    conn.commit()
    # db_setup.py 的 SQLite 表达式索引原句（uq_login_whitelist_github_lower）
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_login_whitelist_github_lower "
        "ON login_whitelist(LOWER(github_username)) WHERE github_username != ''"
    )
    # MySQL 生成列形态的等价模拟：LOWER(NULLIF(col,'')) —— 清洗后同列互不撞
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_login_whitelist_gitcode_lower "
        "ON login_whitelist(LOWER(NULLIF(gitcode_username, '')))"
    )


def test_unique_index_fails_without_cleanup_reproduces_1062():
    """不清洗时建索引确实撞唯一冲突——复现 59 演练告警，防守卫失真。"""
    conn = _make_db(ROWS)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "CREATE UNIQUE INDEX uq_login_whitelist_github_lower "
            "ON login_whitelist(LOWER(NULLIF(github_username, '')))"
        )


def test_cleanup_idempotent():
    conn = _make_db(ROWS)
    c = conn.cursor()
    _normalize_whitelist_null_literals(c)
    conn.commit()
    _normalize_whitelist_null_literals(c)
    conn.commit()
    got = conn.execute(
        "SELECT id, github_username, gitcode_username FROM login_whitelist ORDER BY id"
    ).fetchall()
    assert got[1] == (2, "", "")
    assert sum(1 for r in got if r[1] == "alice-gh") == 1
