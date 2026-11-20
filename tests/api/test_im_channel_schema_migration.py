"""IM 渠道表族：存量库补建迁移 + SQLite 库路径回归锁（2026-11-18）。

背景（本机实测）：库存量库已盖 legacy-stamp，启动不跑老轨 `_create_tables()`，
而 8 张 `grape_t_channel_*` 表只挂在那里 → 表永不创建；同时
`engine_wiring.default_db_path()` 少一层 dirname，SQLite 部署下拼出
`src/domains/data/sessions.db`（目录不存在）→ IM 仓储读写全部
`unable to open database file`。两处都会让「保存 IM 渠道」静默失败
（页面显示成功、徽标一直未连接、通知发不出去）。

本测试锁：
  T1. 注册式迁移 202611182001 / ensure-im-channel-tables 存在且 kind=safe；
  T2. 该迁移的 DDL 幂等（临时库连跑两次不报错，8 张表齐全）；
  T3. default_db_path 与 session_manager._db_path 同源（SQLite 默认路径落在 <repo>/data）。
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_channel_tables_migration_registered():
    from src.api import db_setup

    # 版本号按名定位：202611182001 在本仓与 create-org-dispatch-tables 撞号，
    # db_setup 已避让改号为 202611184000（同号不同名会被判「已执行」永久跳过）。
    mig = next((m for m in db_setup._MIGRATIONS
                if m.name == "ensure-im-channel-tables"), None)
    assert mig is not None, "存量库补建 IM 渠道表的迁移必须注册（否则新表永不创建）"
    assert mig.version in {"202611182001", "202611184000"}, mig.version
    assert mig.kind == "safe"
    assert callable(mig.fn)


def test_ensure_channel_schema_is_idempotent(tmp_path):
    from src.domains.runtime.im.engine_wiring import (ensure_channel_schema,
                                                      ensure_notify_outbox_schema)

    db_file = tmp_path / "im_schema.db"
    conn = sqlite3.connect(str(db_file))
    try:
        ensure_channel_schema(conn)
        ensure_notify_outbox_schema(conn)
        first = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'grape_t_channel%'")}
        # 第二次：幂等，不抛
        ensure_channel_schema(conn)
        ensure_notify_outbox_schema(conn)
        second = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'grape_t_channel%'")}
    finally:
        conn.close()
    expected = {
        "grape_t_channel_installation",
        "grape_t_channel_chat_session_binding",
        "grape_t_channel_inbound_dedup",
        "grape_t_channel_sender_identity",
        "grape_t_channel_outbound_card",
        "grape_t_channel_lease",
        "grape_t_channel_inbound_message",
    }
    assert expected <= first, f"缺表: {expected - first}"
    assert first == second, "第二次执行不应新建/丢失表（必须幂等）"


def test_default_db_path_matches_session_manager(monkeypatch):
    monkeypatch.delenv("DEVOPS_DB_PATH", raising=False)
    from src.domains.runtime.im.engine_wiring import default_db_path
    from src.kernel import session_manager

    got = os.path.abspath(default_db_path())
    want = os.path.abspath(session_manager._db_path)
    assert got == want, f"IM 仓储库路径必须与 session_manager 同源: {got} != {want}"
    assert got.endswith(".db"), got
    assert "/src/domains/data/" not in got, "历史 bug：三层 dirname 拼到 src/domains/data"
