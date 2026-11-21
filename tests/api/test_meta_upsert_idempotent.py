"""meta 写入幂等 + OAuth 渠道不再写 meta（2026-11-21 两条缺陷的回归锁）。

缺陷一｜`set_meta_value_any` 用 UPDATE 的 rowcount 判存在性
    `UPDATE meta SET value=? WHERE key=?` 命中但值没变 → MySQL 返回 0（计「实际改变的行」）
    → 判定「这行不存在」→ INSERT → 1062 `meta.PRIMARY`。
    实测：每次 mgmt 启动刷一条
      `migrate_oauth_providers: sync from oauth-providers.json failed: (1062, "Duplicate entry
      'oauth_providers_config' for key 'meta.PRIMARY'")`
    同一个值每次启动都一样 → 稳定复现；SQLite 计「匹配行」→ 单测全绿也发现不了。

缺陷二｜`_sync_providers_from_config` 把渠道定义再抄进 meta
    `grape_t_oauth_channels` 已承载全套字段（auth_url/token_url/scope/pkce/client_*_enc/…），
    且 `providers.py::_load_providers` 的读取顺序是「文件卡 → meta 逐键覆盖 → 表整卡覆盖」，
    表是运行时真源。所以往 meta 写 `oauth_providers_config` 与 `oauth_<p>_client_id/secret`
    只是空转（并且正是缺陷一的触发者）。

本用例锁四件事：
  S1 同值重写两次不抛异常，行数恒 1，值不变
  S2 值变化时原地更新（仍是 1 行）
  S3 源码锁：`set_meta_value_any` 不含 rowcount，且 upsert SQL 走 ON CONFLICT
  S4 源码锁：`_sync_providers_from_config` 不再调用任何 meta 写入
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from src.domains.runtime import public as rt
from src.domains.runtime.public import set_meta_value_any
from src.kernel.session_manager import db, db_lock

KEY = "probe:meta_upsert_idem"


def _ensure_meta_table() -> None:
    with db_lock:
        c = db.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        db.commit()


def _body_source(fn) -> str:
    """函数体源码（剥掉 docstring）—— 注释里可以解释历史，断言只看真实语句。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]
    return ast.unparse(node)


def _rows(key: str) -> list:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT key, value FROM meta WHERE key=?", (key,))
        return c.fetchall()


@pytest.fixture()
def clean_key():
    _ensure_meta_table()
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key=?", (KEY,))
        db.commit()
    yield KEY
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key=?", (KEY,))
        db.commit()


def test_s1_same_value_rewrite_twice_keeps_single_row(clean_key):
    """S1：MySQL 上必炸的那条路径 —— 同值重写两次不得抛错、不得多行。"""
    payload = '{"gitee": {"auth_url": "https://gitee.com/oauth/authorize"}}'
    set_meta_value_any(clean_key, payload)
    set_meta_value_any(clean_key, payload)      # 同值重写（旧实现的 1062 就在这里）

    rows = _rows(clean_key)
    assert len(rows) == 1
    assert rows[0][1] == payload


def test_s2_value_change_updates_in_place(clean_key):
    """S2：值变化时原地更新，行数仍是 1。"""
    set_meta_value_any(clean_key, "v1")
    set_meta_value_any(clean_key, "v2")

    rows = _rows(clean_key)
    assert len(rows) == 1 and rows[0][1] == "v2"


def test_s3_source_never_uses_rowcount():
    """S3：源码锁 —— 存在性只能由主键回答，不许再用 rowcount。

    SQLite 的 rowcount 是「匹配行数」，两段式实现在单测里永远是绿的；
    只有源码锁挡得住复发（与 session_token 那两条同口径）。
    """
    src = _body_source(set_meta_value_any)
    assert "rowcount" not in src, "set_meta_value_any 又用 rowcount 判存在性了"
    sql = rt._ANY_QUERIES.get("meta_upsert") or ""
    # 列名可带反引号（`key` 是 MySQL 保留字），只断言 upsert 语义
    assert "ON CONFLICT(" in sql and "DO UPDATE" in sql, "meta upsert 必须走 ON CONFLICT"
    assert "`key`" in sql, "key 是 MySQL 保留字，SQL 里应显式加反引号"
    assert "excluded.value" in sql


def test_s4_oauth_providers_no_longer_writes_meta():
    """S4：渠道同步不再写 meta（表已是真源，meta 写入是空转 + 1062 触发者）。"""
    from src.domains.mgmt.user import db as udb

    src = _body_source(udb._sync_providers_from_config)
    for banned in ("set_meta_value_any", "set_meta_if_absent", "oauth_providers_config"):
        assert banned not in src, f"_sync_providers_from_config 仍在写 meta（{banned}）"
    # 仍必须说明真源，避免下一个人又把它写回去（docstring 里必须点名）
    assert "grape_t_oauth_channels" in inspect.getsource(udb._sync_providers_from_config)