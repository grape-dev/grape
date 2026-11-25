"""Issue #3909：mgmt 进程读 runtime 域 ``scene_engine_detach_log`` 必须走域连接 + 降级可见。

三库拓扑下 mgmt 进程主库没有该表（`grape_prod_mgmt.scene_engine_detach_log` → 1146），
原实现 ``with db_lock: db.cursor()`` 直连进程主库，异常被 ``log.warning + return []``
吞掉 → 场景详情 ``detached_engines`` 与巡检任务 ``engine_detached`` 恒空，生产 40 条
WARNING/小时，「查询失败」与「无解绑」不可区分。同族的 ``recovery.recover_engine``
（mgmt 一键加回）同样错库，且失败返回 None（调用方 ``out.get`` 直接 AttributeError）。

本用例锁六件事：

1. 读路径走 runtime 域游标（三库），命中审计行且 ``degraded=False``；
2. 查询异常 → ``degraded=True reason="query"``，日志带 ``route``/``target_db``/``pid``；
3. ``DomainUnavailable``（三库缺 DSN）→ ``degraded=True reason="domain"``，**不回落主库**；
4. 单库/测试拓扑零回归：``route=local`` 仍读进程主库；
5. 契约同步：巡检任务在降级为真时带 ``engine_detached_degraded``（不与「无解绑」同形）；
6. ``recover_engine`` 走 runtime 事务；域不可用返回显式失败原因，不再抛/消失。
"""
import inspect
import sqlite3
import threading

import pytest

from src.kernel import db_registry

RUNTIME_DSN = "mysql://u:***@127.0.0.1:3306/grape_runtime_db"
_ERR_1146 = '(1146, "Table \'grape_prod_mgmt.scene_engine_detach_log\' doesn\'t exist")'


class _FakeCursor:
    """最小域游标桩：记录 SQL、可注入异常、可断言是否关闭。"""

    def __init__(self, rows=None, exc=None):
        self.rows = list(rows or [])
        self.exc = exc
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.exc is not None:
            raise self.exc
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def _install_runtime_domain(monkeypatch, cur):
    """模拟三库拓扑：runtime 域有自有 DSN（非本域进程），cursor_for 拿到注入的假连接。"""
    monkeypatch.setenv("GRAPE_DSN_RUNTIME", RUNTIME_DSN)
    monkeypatch.delenv("GRAPE_DOMAIN_LIB", raising=False)
    monkeypatch.delenv("GRAPE_PROCESS_ROLE", raising=False)
    monkeypatch.delenv("DEVOPS_DB_NAME", raising=False)
    monkeypatch.setattr(db_registry, "domain_db",
                        lambda d: _FakeConn(cur) if d == "runtime" else None)
    return cur


def _sqlite_conn(ddl: str = "") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    if ddl:
        conn.executescript(ddl)
    return conn


_DETACH_LOG_DDL = """
CREATE TABLE scene_engine_detach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_row_id INTEGER DEFAULT 0,
    repo_key TEXT DEFAULT '',
    scene_id TEXT DEFAULT '',
    node_tag TEXT DEFAULT '',
    runtime_id INTEGER DEFAULT 0,
    runtime_name TEXT DEFAULT '',
    detached_at TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    affected_autopilot_tasks TEXT DEFAULT '',
    rebound_at TEXT DEFAULT ''
);
CREATE TABLE agent_runtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    engine_uid VARCHAR(36) DEFAULT ''
);
"""

_AUTOPILOT_TASKS_DDL = """
CREATE TABLE autopilot_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT DEFAULT '',
    repo_key TEXT DEFAULT '',
    agent_type TEXT DEFAULT '',
    instruction TEXT DEFAULT '',
    skill_name TEXT DEFAULT '',
    cron TEXT DEFAULT '',
    max_retry INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT DEFAULT '',
    next_run_at TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    engine_runtime_ids TEXT DEFAULT '',
    custom_fields TEXT DEFAULT '',
    work_branch TEXT DEFAULT '',
    engine_runtime_uids TEXT DEFAULT '',
    dispatch_state TEXT DEFAULT '',
    dispatch_state_reason TEXT DEFAULT '',
    dispatch_state_at TEXT DEFAULT ''
);
"""


def _es():
    from src.domains.runtime.engine import engine_select
    return engine_select


# ── A. 三库拓扑：走 runtime 域游标，读出真实审计行 ────────────────────────────


def test_reads_detach_log_through_runtime_domain(monkeypatch):
    cur = _install_runtime_domain(monkeypatch, _FakeCursor(rows=[
        (11, "scene-a", "coding", 7, "opencode", "2026-11-23T00:00:00+00:00", "[]"),
    ]))

    res = _es().get_detached_engine_hints(scene_row_id=11)

    assert res["degraded"] is False, "三库下读到数据不得标降级"
    assert res["reason"] == ""
    assert len(res["hints"]) == 1
    hint = res["hints"][0]
    assert hint["runtime_id"] == 7 and hint["runtime_name"] == "opencode"
    assert hint["affected_autopilot_tasks"] == []
    assert cur.executed and "FROM scene_engine_detach_log" in cur.executed[0][0]
    assert cur.closed, "域游标必须关闭（泄漏连接）"


# ── B. 查询异常：显式降级 + 可诊断日志 ────────────────────────────────────────


def test_query_failure_marks_degraded_with_conn_facts(monkeypatch, caplog):
    _install_runtime_domain(monkeypatch, _FakeCursor(exc=RuntimeError(_ERR_1146)))

    with caplog.at_level("WARNING"):
        res = _es().get_detached_engine_hints(scene_row_id=11)

    assert res["degraded"] is True, "查询失败必须显式降级（否则与「无解绑」不可区分）"
    assert res["reason"] == "query"
    assert res["hints"] == []
    logs = caplog.text
    assert "get_detached_engine_hints: degraded" in logs
    assert "route=domain" in logs and "target_db=grape_runtime_db" in logs
    assert "pid=" in logs and "host=" in logs


# ── C. 域不可用：显式降级，绝不回落 mgmt 主库 ─────────────────────────────────


def test_domain_unavailable_never_falls_back_to_process_db(monkeypatch, caplog):
    def _no_dsn(_domain):
        raise db_registry.DomainUnavailable("runtime", "GRAPE_DSN_RUNTIME missing")

    monkeypatch.setattr(db_registry, "route_of", _no_dsn)
    from src.kernel import session_manager as sm
    fallback_used = []
    monkeypatch.setattr(sm, "db", type("_Boom", (), {
        "cursor": lambda self, *a, **k: fallback_used.append(1)})(), raising=False)

    with caplog.at_level("WARNING"):
        res = _es().get_detached_engine_hints(scene_row_id=11)

    assert res["degraded"] is True
    assert res["reason"] == "domain"
    assert res["hints"] == []
    assert not fallback_used, "三库缺 DSN 时不得回落进程主库（会读到别的库）"
    assert "route=unavailable" in caplog.text


# ── D. 单库/测试拓扑零回归：仍走进程主库 ──────────────────────────────────────


def test_single_db_topology_falls_back_to_process_db(monkeypatch):
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "0")
    monkeypatch.delenv("GRAPE_DSN_RUNTIME", raising=False)
    assert db_registry.route_of("runtime") == "local"

    conn = _sqlite_conn(_DETACH_LOG_DDL)
    conn.execute(
        "INSERT INTO scene_engine_detach_log "
        "(scene_row_id, repo_key, scene_id, node_tag, runtime_id, runtime_name, "
        " detached_at, reason, affected_autopilot_tasks) "
        "VALUES (4242, 'gitcode:org/r', 'sc-x', 'coding', 99, 'engine-x', ?, 'offline_timeout', '[]')",
        ("2026-11-23T00:00:00+00:00",))
    conn.commit()
    from src.kernel import session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=True)
    monkeypatch.setattr(sm, "db_lock", threading.RLock(), raising=True)

    res = _es().get_detached_engine_hints(scene_row_id=4242)

    assert res["degraded"] is False, "单库拓扑空/命中都不得标降级"
    assert [h["runtime_id"] for h in res["hints"]] == [99]


# ── E. 静态守门：读点不得再裸用进程库游标 ────────────────────────────────────


def test_read_path_no_longer_touches_session_manager():
    src = inspect.getsource(_es().get_detached_engine_hints)
    assert "from src.kernel.session_manager import" not in src, \
        "解绑提示读点仍导入进程库直连（同病未收口）"
    assert "db.cursor()" not in src, "解绑提示读点仍裸用进程库游标"
    assert 'cursor_for("runtime")' in src, "runtime 域表读必须走内核唯一路由"


# ── F. 契约同步：巡检任务降级可见 ────────────────────────────────────────────


def test_autopilot_task_carries_degraded_flag(monkeypatch):
    from src.domains.mgmt.routes import autopilot

    conn = _sqlite_conn(_AUTOPILOT_TASKS_DDL)
    conn.execute(
        "INSERT INTO autopilot_tasks (name, repo_key, agent_type, instruction, cron, "
        " engine_runtime_ids, enabled) VALUES ('t-3909', '', 'coding', 'x', '0 * * * *', '[7]', 1)")
    conn.commit()
    monkeypatch.setattr(autopilot, "db", conn, raising=True)
    monkeypatch.setattr(autopilot, "db_lock", threading.RLock(), raising=True)

    import src.domains.runtime.public as runtime_public
    monkeypatch.setattr(runtime_public, "get_detached_engine_hints",
                        lambda **kw: {"hints": [], "degraded": True, "reason": "query"})

    task = autopilot.get_task(1, user={"id": 1, "username": "alice"})

    assert task.get("engine_detached_degraded") is True, \
        "降级必须透传到任务契约（否则前端把「查询失败」渲染成「无解绑」）"
    assert "engine_detached" not in task


# ── G. recovery：一键加回走 runtime 事务，域不可用返回显式失败 ────────────────


def test_recover_engine_uses_runtime_transaction():
    from src.domains.runtime.engine import recovery

    src = inspect.getsource(recovery.recover_engine)
    assert 'transaction_for("runtime")' in src
    assert "from src.kernel.session_manager import" not in src


def test_recover_engine_domain_unavailable_returns_reason(monkeypatch, caplog):
    from src.domains.runtime.engine import recovery

    def _no_dsn(_domain):
        raise db_registry.DomainUnavailable("runtime", "GRAPE_DSN_RUNTIME missing")

    monkeypatch.setattr(db_registry, "route_of", _no_dsn)
    with caplog.at_level("WARNING"):
        out = recovery.recover_engine(7, trigger="manual", force=True)

    assert out["recovered"] is False
    assert out["reason"] == "runtime_domain_unavailable"
    assert out["degraded"] is True
    assert "route=unavailable" in caplog.text


@pytest.mark.parametrize("step", ["_readd_scene_bindings", "_recover_in_tx"])
def test_recovery_helpers_stay_on_runtime_owned_tables(step):
    """同族收口的静态守门：helper 不新增进程库直连（游标由调用方按域提供）。"""
    from src.domains.runtime.engine import recovery

    src = inspect.getsource(getattr(recovery, step))
    assert "session_manager" not in src

# ── H. 路由判据只取配置口径：连接退避期不得误交 runtime 游标 ────────────────────


MGMT_DSN = "mysql://u:***@127.0.0.1:3306/grape_mgmt_db"


def test_readd_scene_bindings_ignores_mgmt_connection_availability(monkeypatch):
    """mgmt DSN 已配置但连接处于退避期（domain_db 返回 None）时，mgmt 基表仍必须交

    mgmt 自取连接，绝不复用 runtime 域游标 —— 否则 mgmt 进程内 ``_same_domain_*``
    按进程域判同域复用，会用 runtime 库连接读写 ``devops_pipeline_scene``（跨库误写）。

    回归 Required：判据从 ``uses_own_dsn``（内含 domain_db 探活）改为 ``route_of``。
    """
    from src.domains.runtime.engine import recovery
    from src.domains.mgmt import public as mgmt_public

    monkeypatch.setenv("GRAPE_DSN_RUNTIME", RUNTIME_DSN)
    monkeypatch.setenv("GRAPE_DSN_MGMT", MGMT_DSN)
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "1")
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "mgmt")
    monkeypatch.delenv("DEVOPS_DB_NAME", raising=False)

    # 配置口径：runtime 走自有域连接、mgmt 走进程主库；但 mgmt 连接当前不可用
    monkeypatch.setattr(db_registry, "domain_db", lambda d: None)
    assert db_registry.route_of("runtime") == "domain"
    assert db_registry.route_of("mgmt") == "local"
    assert db_registry.uses_own_dsn("mgmt") is False, "退避期探活口令必须为 False（旧判据缺陷来源）"

    runtime_cur = _FakeCursor(rows=[(1, 11, "coding")])
    seen_cursors = []

    def _scene_row(_sid, cursor=None):
        seen_cursors.append(cursor)
        return None  # 行不存在 → 只标 rebound，不进写路径

    monkeypatch.setattr(mgmt_public, "scene_nodes_row_by_id_any", _scene_row)

    recovery._readd_scene_bindings(runtime_cur, runtime_id=7,
                                   now_iso="2026-11-23T00:00:00+00:00")

    assert seen_cursors, "必须调用 mgmt public 读取场景节点"
    assert seen_cursors[0] is None, \
        "mgmt 表读必须交 mgmt 自取连接，不得复用 runtime 域游标（跨库误写）"
    assert runtime_cur not in seen_cursors


def test_readd_scene_bindings_single_db_reuses_caller_cursor(monkeypatch):
    """单库/测试拓扑（runtime/mgmt 都 local）仍复用调用方游标，保住事务快照与提交责任。"""
    from src.domains.runtime.engine import recovery
    from src.domains.mgmt import public as mgmt_public

    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "0")
    monkeypatch.delenv("GRAPE_DSN_RUNTIME", raising=False)
    monkeypatch.delenv("GRAPE_DSN_MGMT", raising=False)
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "mgmt")
    assert db_registry.route_of("runtime") == "local"
    assert db_registry.route_of("mgmt") == "local"

    runtime_cur = _FakeCursor(rows=[(1, 11, "coding")])
    seen_cursors = []

    def _scene_row(_sid, cursor=None):
        seen_cursors.append(cursor)
        return None

    monkeypatch.setattr(mgmt_public, "scene_nodes_row_by_id_any", _scene_row)

    recovery._readd_scene_bindings(runtime_cur, runtime_id=7,
                                   now_iso="2026-11-23T00:00:00+00:00")

    assert seen_cursors and seen_cursors[0] is runtime_cur, \
        "单库拓扑必须复用调用方游标（否则事务快照/提交责任被悄悄改掉）"
