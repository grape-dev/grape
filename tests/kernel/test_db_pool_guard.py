"""连接池僵死防护回归用例（2026-11-23 mgmt 进程全站僵死事故）。

事故形状：池上限 16 < anyio 线程上限 40；慢语句占满连接后 dbutils 的
blocking=True 让所有后到线程（含事件循环主线程）无限期阻塞，进程活着但
连静态文件都不再响应。

本用例固定三条不变量：
1. 只读判定必须认注释开头 / WITH(CTE) / EXPLAIN —— 漏判会让池化连接粘在
   线程上不归还；
2. 池满时借连接必须**快速失败**（超时抛错），不允许无限期挂死；
3. anyio 线程上限必须被收敛到不超过池上限。
"""
import threading

import pytest

from src.kernel import db_pool_guard, mysql_compat


@pytest.mark.parametrize("sql,expected", [
    ("SELECT 1", True),
    ("  select * from t", True),
    ("SHOW COLUMNS FROM devops_pipeline_scene", True),
    ("/* hint */ SELECT 1", True),
    ("-- 注释开头\nSELECT 1", True),
    ("WITH x AS (SELECT 1) SELECT * FROM x", True),
    ("EXPLAIN SELECT 1", True),
    ("DESCRIBE events", True),
    ("INSERT INTO t VALUES (1)", False),
    ("UPDATE t SET a=1", False),
    ("DELETE FROM t", False),
    ("", False),
])
def test_read_only_statement_detection(sql, expected):
    assert mysql_compat._is_read_only_statement(sql) is expected


class TooManyConnectionsError(Exception):
    """与 dbutils.pooled_db.TooManyConnectionsError 同名——借连接逻辑按类型名判定。"""


class _ExhaustedPool:
    """永远报池满的桩池：模拟 dbutils blocking=False 下的 TooManyConnectionsError。"""

    def __init__(self):
        self._idle_cache = []
        self._connections = 16
        self._maxconnections = 16

    def connection(self):
        raise TooManyConnectionsError("too many")


def test_borrow_fails_fast_when_pool_exhausted(monkeypatch):
    """池满时必须在 DB_POOL_WAIT_TIMEOUT 内抛错，而不是无限阻塞。"""
    monkeypatch.setattr(mysql_compat, "_DB_POOL_WAIT_TIMEOUT", 0.2)
    monkeypatch.setattr(mysql_compat, "_DB_POOL_WARN_WAIT", 0.05)

    inst = mysql_compat.MySQLCompatDB(database="grape_prod_mgmt")
    inst._pool = _ExhaustedPool()

    with pytest.raises(TimeoutError):
        inst._borrow_pooled_conn()
    assert inst.pool_stats()["max"] == 16


def test_pool_stats_reports_in_use():
    inst = mysql_compat.MySQLCompatDB(database="grape_prod_runtime")
    pool = type("P", (), {"_idle_cache": [1, 2], "_connections": 10, "_maxconnections": 16})()
    inst._pool = pool
    st = inst.pool_stats()
    assert st["max"] == 16 and st["in_use"] == 8 and st["idle"] == 2


def test_release_thread_connection_returns_borrowed_conn():
    inst = mysql_compat.MySQLCompatDB(database="t")
    closed = {"n": 0}

    class _Conn:
        def close(self):
            closed["n"] += 1

    inst._pool = object()  # 只要求非 None
    inst._local.conn = _Conn()
    assert inst.release_thread_connection() is True
    assert closed["n"] == 1
    assert inst.release_thread_connection() is False  # 幂等


def test_release_all_thread_connections_uses_registry():
    inst = mysql_compat.MySQLCompatDB(database="t")
    inst._pool = object()
    inst._local.conn = type("C", (), {"close": lambda self: None})()
    try:
        assert mysql_compat.release_all_thread_connections() >= 1
    finally:
        inst._local.conn = None


def test_align_clamps_threads_to_pool(monkeypatch):
    monkeypatch.setenv("DB_POOL_MAX_CONNECTIONS", "8")
    monkeypatch.setenv("DB_THREAD_LIMIT", "40")
    res = db_pool_guard.align_threads_and_pool()
    assert res["thread_limit"] == 8
    assert res["pool_max"] == 8


def test_align_keeps_smaller_thread_limit(monkeypatch):
    monkeypatch.setenv("DB_POOL_MAX_CONNECTIONS", "32")
    monkeypatch.setenv("DB_THREAD_LIMIT", "16")
    res = db_pool_guard.align_threads_and_pool()
    assert res["thread_limit"] == 16


def test_watchdog_thread_is_daemon(monkeypatch):
    monkeypatch.setenv("DB_POOL_WATCHDOG", "1")
    monkeypatch.setattr(db_pool_guard, "_WATCHDOG_STARTED", False)
    monkeypatch.setenv("DB_POOL_WATCH_INTERVAL", "3600")
    assert db_pool_guard.start_watchdog() is True
    names = [t.name for t in threading.enumerate()]
    assert "db-pool-watchdog" in names
    assert db_pool_guard.start_watchdog() is False  # 幂等


def test_guard_applies_thread_limit_inside_event_loop(monkeypatch):
    """anyio 4 只能在运行中的事件循环里设上限 —— lifespan 启动时必须生效。"""
    import anyio.to_thread
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    monkeypatch.setenv("DB_THREAD_LIMIT", "8")
    monkeypatch.setenv("DB_POOL_MAX_CONNECTIONS", "32")
    monkeypatch.setenv("DB_POOL_WATCHDOG", "0")

    app = FastAPI()

    @app.get("/ping")
    async def ping():
        lim = anyio.to_thread.current_default_thread_limiter()
        return {"limit": lim.total_tokens}

    db_pool_guard.install_guard(app)
    with TestClient(app) as client:
        assert client.get("/ping").json()["limit"] == 8


def test_request_middleware_releases_stuck_connections(monkeypatch):
    """请求收尾必须兜底归还本线程被粘住的池连接（正常路径下应为 0）。"""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    monkeypatch.setenv("DB_POOL_WATCHDOG", "0")
    calls = {"n": 0}

    def _fake_release():
        calls["n"] += 1
        return 0

    monkeypatch.setattr(mysql_compat, "release_all_thread_connections", _fake_release)

    app = FastAPI()

    @app.get("/x")
    def x():
        return {"ok": True}

    db_pool_guard.install_guard(app)
    with TestClient(app) as client:
        assert client.get("/x").status_code == 200
    assert calls["n"] >= 1
