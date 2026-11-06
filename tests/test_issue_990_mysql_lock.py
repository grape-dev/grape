"""Issue #990：MySQL 命名锁复用/串行化/失败语义（假连接，不依赖真实 MySQL）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

import src.runtime.service as svc


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def execute(self, sql, args=None):
        self.conn.calls.append((sql, args))
        if "GET_LOCK" in sql:
            if self.conn.raise_on_lock:
                raise RuntimeError("lock query failed")
            self._row = (self.conn.lock_result,)
        elif "RELEASE_LOCK" in sql:
            self._row = (1,)
        elif sql.strip().upper().startswith("SELECT 1"):
            if self.conn.broken:
                raise RuntimeError("connection lost")
            self._row = (1,)
        else:
            self._row = (1,)

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, lock_result=1, broken=False, raise_on_lock=False):
        self.lock_result = lock_result
        self.broken = broken
        self.raise_on_lock = raise_on_lock
        self.calls = []
        self.closed = False

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_lock_state():
    svc._lock_conn_state["conn"] = None
    svc._lock_conn_state["guard"] = __import__("threading").Lock()
    yield
    svc._lock_conn_state["conn"] = None
    svc._lock_conn_state["guard"] = __import__("threading").Lock()


class _DbProxy:
    """真实 db 的代理，只补 dedicated_connection（sqlite 连接对象不允许挂属性）。"""

    def __init__(self, real, factory):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_factory", factory)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def dedicated_connection(self):
        return self._factory()


def _install(monkeypatch, conn):
    created = []

    def factory():
        created.append(1)
        return conn

    monkeypatch.setattr(svc, "db", _DbProxy(svc.db, factory))
    return created


def test_acquire_release_pairs_and_guard_released(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    handle, name = svc._acquire_task_token_lock("t1")
    assert handle is conn and name.startswith("grape_sess_tk_t1")
    # 持锁期间进程内 guard 必须被占用（同一连接重入会漏释放）
    assert svc._lock_conn_state["guard"].locked()
    svc._release_task_token_lock(handle, name)
    assert any("RELEASE_LOCK" in sql for sql, _ in conn.calls)
    assert not svc._lock_conn_state["guard"].locked()
    # 释放后可再次获取
    handle2, _ = svc._acquire_task_token_lock("t1")
    assert handle2 is conn
    svc._release_task_token_lock(handle2, name)


def test_lock_connection_reused_across_calls(monkeypatch):
    conn = _Conn()
    created = _install(monkeypatch, conn)
    for _ in range(3):
        handle, name = svc._acquire_task_token_lock("t2")
        svc._release_task_token_lock(handle, name)
    assert len(created) == 1, "GET_LOCK 连接应跨次复用（每次新建 p50 8.4ms → 0.22ms）"
    assert conn.closed is False


def test_lock_not_acquired_fails_closed_without_leaking_guard(monkeypatch):
    conn = _Conn(lock_result=0)
    _install(monkeypatch, conn)
    handle, name = svc._acquire_task_token_lock("t3")
    assert handle is None and name == ""
    assert not svc._lock_conn_state["guard"].locked()
    # 下一轮仍可获取（guard 未被上一轮卡住）
    conn.lock_result = 1
    handle2, name2 = svc._acquire_task_token_lock("t3")
    assert handle2 is conn
    svc._release_task_token_lock(handle2, name2)


def test_lock_query_error_drops_connection_and_releases_guard(monkeypatch):
    conn = _Conn(raise_on_lock=True)
    created = _install(monkeypatch, conn)
    handle, name = svc._acquire_task_token_lock("t4")
    assert handle is None and name == ""
    assert svc._lock_conn_state["conn"] is None
    assert conn.closed is True
    assert not svc._lock_conn_state["guard"].locked()
    conn2 = _Conn()
    _install(monkeypatch, conn2)
    handle2, name2 = svc._acquire_task_token_lock("t4")
    assert handle2 is conn2
    svc._release_task_token_lock(handle2, name2)


def test_broken_connection_reconnects(monkeypatch):
    first = _Conn(broken=True)
    created = _install(monkeypatch, first)
    svc._lock_conn_state["conn"] = first          # 预置一条已断的连接
    second = _Conn()
    monkeypatch.setattr(svc, "db", _DbProxy(svc.db, lambda: (created.append(1), second)[1]))
    handle, name = svc._acquire_task_token_lock("t5")
    assert handle is second and first.closed is True
    svc._release_task_token_lock(handle, name)


def test_in_process_serialization_when_guard_busy(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    monkeypatch.setattr(svc, "_SESSION_TOKEN_LOCK_TIMEOUT", 0.2)
    handle, name = svc._acquire_task_token_lock("t6")
    assert handle is conn
    # 另一线程在临界区内拿不到（guard 阻塞到超时 → fail-closed，不会重复 GET_LOCK）
    blocked, blocked_name = svc._acquire_task_token_lock("t6")
    assert blocked is None and blocked_name == ""
    svc._release_task_token_lock(handle, name)
    assert not svc._lock_conn_state["guard"].locked()


def test_sync_task_session_token_uses_named_lock_and_rotates(monkeypatch):
    import json
    from src.session_manager import db, db_lock
    import src.api.auth_token as at

    conn = _Conn()
    _install(monkeypatch, conn)
    monkeypatch.setattr("src.session_manager._is_mysql_backend", lambda: True)
    monkeypatch.setattr(svc, "_session_token_lock_name", lambda tid: f"grape_sess_tk_{tid}")

    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id='t7'")
        c.execute("INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key, "
                  "agent_type, user_id, env_extra) VALUES ('t7','t7',1,'p','gitcode:o/r','coding',1,'{}')")
        db.commit()

    tok = svc.sync_task_session_token("t7")
    assert tok and at.verify_session_token(tok)
    with db_lock:
        env = json.loads(db.cursor().execute(
            "SELECT env_extra FROM agent_tasks WHERE id='t7'").fetchone()[0])
    assert env["GRAPE_SESSION_TOKEN"] == tok
    assert any("GET_LOCK" in sql for sql, _ in conn.calls)
    assert any("RELEASE_LOCK" in sql for sql, _ in conn.calls)
    assert not svc._lock_conn_state["guard"].locked()
    assert conn.closed is False, "连接应保留复用"