"""域连接存活校验 + 断线一次重试（2026-11-15 线上 500 回归）。

事故：`POST /api/chat/submit` 返回 500，日志先刷
`get_meta_value failed: server_base_url: (0, '')` / `set_meta_value failed: …`。
根因不是 SQL，而是**域连接被 MySQL 按 wait_timeout 断开后仍留在线程本地缓存里**：
- 旧实现 `domain_db()` 命中缓存直接返回，从不 ping；
- 连接一死，本线程后续所有跨域调用都抛 `InterfaceError(0, '')`，只能重启进程恢复。

本文件钉住三条契约：
1. 空闲超过阈值的缓存连接会 ping(reconnect=True)，ping 失败则重建；
2. 刚用过（阈值内）的连接不重复 ping（不额外增加往返）；
3. 执行期才发现的断线（ping 之后断）由 `_RetryCursor` 重建连接并重试一次，业务侧不 500。
"""
import types

import pytest


@pytest.fixture
def domain_env(monkeypatch):
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
    monkeypatch.setenv("GRAPE_DSN_RUNTIME", "mysql://u:p@127.0.0.1:3306/grape_runtime_db")
    from src.kernel import db_registry
    db_registry.reset(close=False)
    yield db_registry
    db_registry.reset(close=False)


class _FakeConn:
    def __init__(self, dead=False, fail_connect=False):
        self.dead = dead
        self.pings = 0
        self.closed = False
        self.fail_connect = fail_connect
        self.cursors = 0

    def ping(self, reconnect=True):
        self.pings += 1
        if self.dead:
            raise RuntimeError("broken pipe")

    def close(self):
        self.closed = True

    def cursor(self, *a, **k):
        self.cursors += 1
        return types.SimpleNamespace(execute=lambda *a, **k: None,
                                     fetchall=lambda: [], description=None, rowcount=0)


def test_dead_cached_conn_is_replaced(domain_env, monkeypatch):
    dead = _FakeConn(dead=True)
    fresh = _FakeConn()
    domain_env._thread_conns()["runtime"] = dead
    monkeypatch.setattr(domain_env, "_new_conn", lambda d, autocommit=True: fresh)
    # 让存活校验必然触发：清掉 alive_at
    monkeypatch.setattr(domain_env, "_needs_liveness_check", lambda d: True)

    conn = domain_env.domain_db("runtime")
    assert conn is fresh
    assert dead.pings == 1 and dead.closed is True


def test_healthy_recent_conn_not_pinged_again(domain_env, monkeypatch):
    alive = _FakeConn()
    domain_env._thread_conns()["runtime"] = alive
    domain_env._mark_alive("runtime")
    monkeypatch.setattr(domain_env, "_new_conn", lambda d, autocommit=True: pytest.fail("不应重建"))

    assert domain_env.domain_db("runtime") is alive
    assert alive.pings == 0


def test_forbidden_expires_and_retries(domain_env, monkeypatch):
    import time as _t
    domain_env._forbidden["runtime"] = _t.time()  # 刚刚失败 → 60s 内返回 None
    assert domain_env.domain_db("runtime") is None

    domain_env._forbidden["runtime"] = _t.time() - 3600  # 过期 → 允许重试
    ok = _FakeConn()
    monkeypatch.setattr(domain_env, "_new_conn", lambda d, autocommit=True: ok)
    assert domain_env.domain_db("runtime") is ok
    assert "runtime" not in domain_env._forbidden


def test_retry_cursor_reconnects_once_on_interface_error(domain_env, monkeypatch):
    from src.kernel.owner_db import owner_db, _is_disconnect

    calls = {"n": 0}

    class _Cur:
        def __init__(self):
            self.description = None
            self.rowcount = 1

        def execute(self, sql, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                import pymysql
                raise pymysql.err.InterfaceError(0, "")
            return self

        def fetchall(self):
            return [(1,)]

    class _Conn:
        def cursor(self, *a, **k):
            return _Cur()

        def commit(self):
            return None

    monkeypatch.setattr(domain_env, "domain_db", lambda d: _Conn())
    cur = owner_db("runtime").cursor()
    got = cur.execute("SELECT 1")
    assert calls["n"] == 2, "断线应重试一次"
    assert got.fetchall() == [(1,)]
    assert _is_disconnect(Exception("x")) is False