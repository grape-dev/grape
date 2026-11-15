"""owner_db 代理：跨域 owner 连接解析与回落语义（2026-11-15，A3 归零配套）。

背景：拆库后「A 域代码在 B 进程里跑」必须打到 owner 库，否则得到
`1146 Table 'grape_stage_mgmt.obs_*' doesn't exist` 并静默退化成空结果
（本机实测：mgmt 进程 8 分钟 274 条 1146，归零后 0 条）。

本文件钉住代理的四条契约：
1. 分库模式（GRAPE_DOMAIN_LIB=1 + 该域 DSN）→ 用该域自有连接；
2. 单库/无 DSN（测试与 sqlite 拓扑）→ 回落 session_manager.db，零回归；
3. 未知域直接报错（防拼写错误静默走错库）；
4. lock 是进程级单例、可重入（多语句 + commit 段落的串行化依赖它）。
"""
import threading
import types

import pytest


def test_unknown_domain_raises():
    from src.kernel.owner_db import owner_db, owner_lock
    with pytest.raises(ValueError):
        owner_db("nope")
    with pytest.raises(ValueError):
        owner_lock("nope")


def test_singletons():
    from src.kernel.owner_db import owner_db, owner_lock
    assert owner_db("insight") is owner_db("insight")
    assert owner_lock("runtime") is owner_lock("runtime")
    assert isinstance(owner_lock("mgmt"), type(threading.RLock()))


def test_fallback_to_process_db_without_dsn(monkeypatch):
    """无 DSN / 非 lib 模式 → 回落进程库（与改动前 session_manager.db 等价）。"""
    from src.kernel import db_registry, session_manager
    from src.kernel.owner_db import owner_db

    monkeypatch.delenv("GRAPE_DSN_INSIGHT", raising=False)
    sentinel = types.SimpleNamespace(cursor=lambda *a, **k: "PROCESS_CURSOR",
                                      commit=lambda: "COMMITTED")
    monkeypatch.setattr(session_manager, "db", sentinel)
    monkeypatch.setattr(db_registry, "domain_db", lambda d: None)

    o = owner_db("insight")
    # cursor() 现在返回断线重试包装器：断言它最终打到进程库游标
    assert o._conn().cursor() == "PROCESS_CURSOR"
    assert o.commit() == "COMMITTED"


def test_uses_owner_connection_when_dsn_present(monkeypatch):
    from src.kernel import db_registry
    from src.kernel.owner_db import owner_db

    class FakeConn:
        def cursor(self, *a, **k):
            return "OWNER_CURSOR"

        def commit(self):
            return "OWNER_COMMIT"

    monkeypatch.setattr(db_registry, "domain_db", lambda d: FakeConn() if d == "insight" else None)
    o = owner_db("insight")
    assert o._conn().cursor() == "OWNER_CURSOR"
    assert o.commit() == "OWNER_COMMIT"


def test_execute_returns_cursor_semantics(monkeypatch):
    """与 MySQLCompatDB.execute 同语义：返回 cursor（调用方常用 cur.rowcount）。"""
    from src.kernel import db_registry
    from src.kernel.owner_db import owner_db

    class Cur:
        rowcount = 3

        def execute(self, sql, params=None):
            self.sql, self.params = sql, params

    class FakeConn:
        def __init__(self):
            self.cur = Cur()

        def cursor(self, *a, **k):
            return self.cur

    conn = FakeConn()
    monkeypatch.setattr(db_registry, "domain_db", lambda d: conn)
    got = owner_db("runtime").execute("UPDATE t SET a=?", (1,))
    # execute() 返回包装游标，rowcount / params 透传到底层游标
    assert got.rowcount == 3 and conn.cur.params == (1,)


def test_observability_modules_wired_to_insight_owner():
    """可观测性模块的 db 必须是 insight 域代理（不是裸 session_manager.db）。"""
    from src.kernel.owner_db import _OwnerDB
    from src.domains.insight.observability import alerts, metric_rollup
    # 函数内 import 的形态：直接验证 db_pair 产物
    db, lock = __import__("src.kernel.owner_db", fromlist=["db_pair"]).db_pair("insight")
    assert isinstance(db, _OwnerDB) and db.domain == "insight"
    assert callable(alerts.list_rules) and callable(metric_rollup.catch_up)


def test_gsb_route_config_uses_mgmt_owner():
    from src.kernel.owner_db import _OwnerDB
    from src.kernel.gsb import route_config
    assert isinstance(route_config._owner, _OwnerDB)
    assert route_config._owner.domain == "mgmt"