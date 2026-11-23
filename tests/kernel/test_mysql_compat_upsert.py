"""方言翻译：meta upsert 的 ON DUPLICATE KEY 不得被「key 列加反引号」误伤（S1–S4）。

背景（2026-11-21 真库实测）：把 `set_meta_value_any` 改成单语句
`INSERT INTO meta (...) VALUES (...) ON CONFLICT(key) DO UPDATE SET value=excluded.value`
之后，SQLite 全绿，但 MySQL 上报：

    (1064, "You have an error in your SQL syntax ... near '`key` UPDATE value=VALUES(value)'")

原因：`_backtick_key()` 把 meta 表 SQL 里所有 `key` 都加反引号以避开 MySQL 保留字，
它保护了 `PRIMARY/UNIQUE/FOREIGN/INDEX KEY`，却漏了翻译产物 `ON DUPLICATE KEY`
—— 于是 `ON DUPLICATE KEY UPDATE` 变成 ``ON DUPLICATE `key` UPDATE``，语法错误。

这条正是「SQLite 通过≠生产通过」的又一例：唯一能在单测里挡住它的是把翻译结果
本身断言下来（SQLite 侧根本走不到这段翻译）。
"""
from __future__ import annotations

from src.kernel.mysql_compat import translate_sql

META_UPSERT = ("INSERT INTO meta (`key`, value) VALUES (?, ?) "
               "ON CONFLICT(`key`) DO UPDATE SET value=excluded.value")


def test_s1_meta_upsert_translates_to_duplicate_key_update():
    """S1：ON CONFLICT → ON DUPLICATE KEY UPDATE，且 KEY 不被反引号化。"""
    out = translate_sql(META_UPSERT)
    assert "ON DUPLICATE KEY UPDATE" in out, out
    assert "ON DUPLICATE `key`" not in out, "ON DUPLICATE KEY 被当成列名加了反引号"
    assert "VALUES(value)" in out
    assert "excluded." not in out


def test_s2_plain_meta_update_still_backticks_key_column():
    """S2：普通 meta 语句里的 key **列**仍必须加反引号（MySQL 保留字）。"""
    out = translate_sql("UPDATE meta SET value=? WHERE key=?")
    assert "WHERE `key`=%s" in out, out


def test_s3_session_token_upsert_regression():
    """S3：session_api_tokens 的 upsert 翻译不受影响（它不含 meta，走另一条路径）。"""
    sql = ("INSERT INTO session_api_tokens (session_key, repo_key) VALUES (?, ?) "
           "ON CONFLICT(session_key) DO UPDATE SET repo_key=excluded.repo_key")
    out = translate_sql(sql)
    assert "ON DUPLICATE KEY UPDATE repo_key=VALUES(repo_key)" in out, out


def test_s4_primary_key_phrase_untouched():
    """S4：`PRIMARY KEY` / `UNIQUE KEY` 这类短语原本就被保护，别被新规则破坏。"""
    out = translate_sql("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    assert "PRIMARY KEY" in out
    assert "PRIMARY `key`" not in out