"""Issue #3350：max-concurrent / tags 端点必须按 runtime 域取连接。

背景：`agent_runtimes` / `engine_capabilities` 归运行域，注册与心跳写 runtime 库；
三库拓扑下 mgmt 进程用自己的库查这两个表 → runtime 库里的引擎恒 `404 runtime not found`，
并发上限只能手改库。

本用例锁两件事：
1. 配了 runtime 自有 DSN 时，归属校验/写入走 runtime 域连接（不是进程库）；
2. 单库/测试拓扑（无自有 DSN）仍回落进程库，且写入会提交。
"""
import pytest
from fastapi import HTTPException

from src.domains.runtime.routes import runtime as rt


class _FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((sql, args))
        return self

    def fetchone(self):
        return self.row


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


@pytest.fixture
def own_runtime_dsn(monkeypatch):
    """模拟三库拓扑：runtime 域有自有 DSN，返回可注入的假连接。"""
    cur = _FakeCursor()
    conn = _FakeConn(cur)
    monkeypatch.setattr(rt._dbreg, "uses_own_dsn", lambda d: d == "runtime")
    monkeypatch.setattr(rt._dbreg, "new_connection",
                        lambda d, autocommit=True: conn)
    return cur, conn


def test_admin_check_reads_runtime_domain(own_runtime_dsn):
    cur, _conn = own_runtime_dsn
    cur.row = ("global", "", "")
    # 引擎 24303 只在 runtime 库里存在；按域取连接后必须命中而不是 404
    rt._require_runtime_admin(24303, {"auth_type": "jwt", "id": "1"})
    assert cur.executed and "FROM agent_runtimes" in cur.executed[0][0]
    assert cur.executed[0][1] == (24303,)


def test_admin_check_user_scope_403(own_runtime_dsn):
    cur, _ = own_runtime_dsn
    cur.row = ("user", "42", "")
    with pytest.raises(HTTPException) as ei:
        rt._require_runtime_admin(24303, {"auth_type": "jwt", "id": "7"})
    assert ei.value.status_code == 403


def test_max_concurrent_writes_runtime_domain(own_runtime_dsn):
    cur, conn = own_runtime_dsn
    cur.row = ("global", "", "")
    out = rt.set_runtime_max_concurrent(
        24303, rt.MaxConcurrentBody(max_concurrent=20),
        user={"auth_type": "jwt", "id": "1"})
    assert out == {"max_concurrent": 20}
    sqls = [s for s, _a in cur.executed]
    assert any(s.startswith("UPDATE agent_runtimes SET max_concurrent") for s in sqls)
    assert conn.closed, "独立连接用完必须关闭"


def test_single_db_topology_falls_back_to_process_db():
    """无自有 DSN（单库/测试拓扑）：沿用进程库读写，写入要能落盘。"""
    from src.kernel.session_manager import db, db_lock

    rid = 9301
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        c.execute("INSERT INTO agent_runtimes (id, name, credential_id, status, healthy, "
                  "installed, max_concurrent) VALUES (?,?,0,'online',1,1,4)",
                  (rid, "issue3350-engine"))
        db.commit()

    out = rt.set_runtime_max_concurrent(
        rid, rt.MaxConcurrentBody(max_concurrent=17),
        user={"auth_type": "api_key", "id": "1"})
    assert out == {"max_concurrent": 17}

    with db_lock:
        c = db.cursor()
        c.execute("SELECT max_concurrent FROM agent_runtimes WHERE id=?", (rid,))
        assert c.fetchone()[0] == 17, "单库拓扑下写入必须提交"
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
        db.commit()