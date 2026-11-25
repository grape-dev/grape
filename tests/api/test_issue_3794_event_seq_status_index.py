"""Issue #3794（QA #1981 C1）：(event_seq, status) 组合索引必须走注册式迁移。

背景
----
该索引曾写进老轨 `_create_indexes()`，而老轨只在「版本表为空首跑」或「执行器
异常兜底」时执行；存量库已盖 legacy-stamp（202610300001），启动时完全不跑，
三库模式下 runtime 域进程更是只跑登记迁移 → 1205 根因（NOT EXISTS 逐行扫热表）
在生产未消除。本用例钉住三条契约：

1. 迁移已登记（version=202611240009 / kind=safe / domains=runtime）；
2. fn 幂等：建组合索引 + 回收左前缀冗余 idx_agent_tasks_event，重跑不报错；
3. 老轨 `_create_indexes()` 内不得再出现该索引（否则存量库仍不会建）。
"""
import inspect
import sqlite3

import pytest

from src.api import db_setup

VERSION = "202611240009"
NAME = "add-agent-tasks-event-seq-status-index"
COMPOSITE = "idx_agent_tasks_event_seq_status"
REDUNDANT = "idx_agent_tasks_event"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """独立 sqlite 库：agent_tasks + 旧的 event_seq 单列索引（模拟存量库）。"""
    c = sqlite3.connect(str(tmp_path / "mig3794.db"))
    c.executescript(
        """
        CREATE TABLE agent_tasks (
            id TEXT PRIMARY KEY,
            event_seq INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_agent_tasks_event ON agent_tasks(event_seq);
        """
    )
    c.commit()
    from src.kernel import session_manager
    monkeypatch.setattr(session_manager, "db", c)
    # 三库拓扑（配了 runtime DSN）下 _migration_conn 会切到真 runtime 库，本用例只验 DDL
    # 语义，强制回落到 fixture 的 sqlite 连接，保证有 DSN 的开发机/CI 同样可跑。
    monkeypatch.setattr(db_setup, "_migration_conn", lambda domain, default=None: c)
    return c


def _index_names(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA index_list('agent_tasks')").fetchall()}


def test_migration_registered_for_runtime_domain():
    migs = [m for m in db_setup._MIGRATIONS if m.version == VERSION]
    assert len(migs) == 1, f"{VERSION} 必须且只能登记一次"
    m = migs[0]
    assert m.name == NAME
    assert m.kind == "safe", "纯补索引迁移，kind=safe（存量库启动即补）"
    assert tuple(m.domains) == ("runtime",), "agent_tasks 属 runtime 域，三库模式下必须由 runtime 进程执行"
    assert m.fn is db_setup._fn_add_agent_tasks_event_seq_status_index


def test_fn_creates_composite_index_and_reclaims_left_prefix(conn):
    db_setup._fn_add_agent_tasks_event_seq_status_index()
    names = _index_names(conn)
    assert COMPOSITE in names, "组合索引必须建成（1205 根因：NOT EXISTS 逐行扫热表）"
    assert REDUNDANT not in names, "组合索引已覆盖 event_seq 左前缀，旧单列索引应回收"


def test_fn_is_idempotent(conn):
    db_setup._fn_add_agent_tasks_event_seq_status_index()
    db_setup._fn_add_agent_tasks_event_seq_status_index()  # 存量库每次启动都可能重跑
    names = _index_names(conn)
    assert COMPOSITE in names
    assert REDUNDANT not in names


def test_mysql_ddl_uses_text_prefix_and_avoids_if_not_exists(monkeypatch):
    """MySQL 契约：`IF NOT EXISTS` 会 1064，`status` 为 TEXT 必须带前缀长度（1170）。

    三库拓扑下迁移走 db_registry._CompatCursor，mysql_compat 的「自动补 (191)」特判
    不生效；若仍下发 IF NOT EXISTS 或裸列名，迁移每次启动失败不盖章，组合索引永远不建，
    1205 根因在生产依旧存在（本地实测 1064=真实证据）。
    """
    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(sql)

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

    fake = _Conn()
    monkeypatch.setattr(db_setup, "_migration_conn", lambda domain, default=None: fake)
    monkeypatch.setattr(db_setup, "_mysql_backend", lambda: True)
    monkeypatch.setattr(db_setup, "_index_present", lambda c, table, name: False)

    db_setup._fn_add_agent_tasks_event_seq_status_index()

    creates = [s for s in executed if "CREATE INDEX" in s]
    assert creates, "MySQL 路径必须下发 CREATE INDEX"
    assert "IF NOT EXISTS" not in creates[0], "MySQL 不支持 CREATE INDEX IF NOT EXISTS（1064）"
    assert "status(191)" in creates[0], "TEXT 列建索引必须带前缀长度（1170）"


def test_index_ddl_not_in_frozen_legacy_track():
    src = inspect.getsource(db_setup._create_indexes)
    assert COMPOSITE not in src, (
        "补索引必须走 register_migration：写进冻结老轨 _create_indexes() 时存量库永不执行，"
        "只在新库首跑/执行器异常兜底时才跑得到")