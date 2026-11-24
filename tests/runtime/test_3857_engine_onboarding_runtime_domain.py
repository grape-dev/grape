"""Issue #3857：engine-onboarding 读 runtime 域 agent_runtimes 必须走域连接 + 降级可见。

三库拓扑下 mgmt 进程主库没有 ``agent_runtimes`` / ``runtime_credentials``（1146）：
- 原实现 ``with db_lock: db.cursor()`` 查 runtime 域表，异常被 ``except`` 吞成
  WARNING，``_user_visible`` 保持 ``[]``；
- ``remote_engines`` 恒空、``needs_worker`` 恒 true（4 台主控 216 条 WARNING/小时）。

本用例锁四件事：

1. 查询走 runtime 域游标（三库）；单库拓扑仍回落进程库（零回归）；
2. 域读失败可见：响应 ``degraded=true`` + ``engines_unavailable_reason``，仍 200；
3. 「确实无引擎」与「查询失败」可区分（``needs_worker`` 判定不变，靠 ``degraded`` 分流）；
4. 读点不再裸用 ``session_manager`` 进程库游标（静态守门）。
"""
import inspect
from contextlib import contextmanager

from src.kernel import db_registry

REPO_KEY = "gitcode:owner/myrepo"
RUNTIME_DSN = "mysql://u:***@127.0.0.1:3306/grape_runtime_db"
_ERR_1146 = "(1146, \"Table 'grape_prod_mgmt.agent_runtimes' doesn't exist\")"


class _FakeCursor:
    """最小域游标桩：记录 SQL、可注入异常、可断言是否关闭。"""

    def __init__(self, rows=None, exc=None):
        self.rows = list(rows or [])
        self.exc = exc
        self.executed = []
        self.closed = False
        self.description = [
            ("id",), ("name",), ("status",), ("device_name",), ("scope",), ("repo_key",),
        ]

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.exc is not None:
            raise self.exc
        return self

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur


def _install_runtime_domain(monkeypatch, cur):
    """模拟三库拓扑：runtime 域有自有 DSN，cursor_for 拿到注入的假连接。"""
    monkeypatch.setenv("GRAPE_DSN_RUNTIME", RUNTIME_DSN)
    monkeypatch.delenv("GRAPE_PROCESS_ROLE", raising=False)
    monkeypatch.delenv("DEVOPS_DB_NAME", raising=False)
    monkeypatch.setattr(db_registry, "domain_db",
                        lambda d: _FakeConn(cur) if d == "runtime" else None)
    return cur


def _patch_io(monkeypatch):
    """隔离本机引擎扫描与 base url（与本 Issue 无关的外部 IO）。"""
    import src.domains.mgmt.user.db as db_mod
    import src.domains.runtime.agents.executor as executor_mod

    monkeypatch.setattr(db_mod, "get_server_base_url", lambda: "https://test-base")
    monkeypatch.setattr(
        executor_mod, "discover_backends",
        lambda: [{"name": n, "installed": False, "cli_path": "", "pip_package": ""}
                 for n in ("opencode", "hermes")])
    # 生产端点在函数体内 `from src.domains.runtime.engine.executor import discover_backends`，
    # 延迟导入取到的是模块属性，故上面对模块打桩即生效。


def _call(repo="", user=None):
    from src.domains.runtime.routes.runtime import runtime_engine_onboarding
    u = user or {"username": "dave", "id": 999, "org_id": 0}
    return runtime_engine_onboarding(repo=repo, user=u)


# ── A. 三库下走 runtime 域游标，在线引擎可见 → 不误报 needs_worker ────────────


def test_reads_agent_runtimes_through_runtime_domain(monkeypatch):
    _patch_io(monkeypatch)
    cur = _install_runtime_domain(monkeypatch, _FakeCursor(rows=[
        (1, "opencode", "online", "opencode-host", "global", ""),
    ]))

    r = _call()

    assert cur.executed, "必须经 runtime 域连接执行查询"
    sql, params = cur.executed[0]
    assert "FROM agent_runtimes" in sql
    assert params == (0, "999", "dave")
    assert len(r["remote_engines"]) == 1, "三库下 domain 读必须命中有在线引擎"
    assert r["remote_engines"][0]["device_name"] == "opencode-host"
    assert r["needs_worker"] is False
    assert r["degraded"] is False
    assert r["engines_unavailable_reason"] == ""
    assert cur.closed, "域游标必须关闭（泄漏连接）"


# ── B. 域读失败：degraded 可见，不再静默等价于「无引擎」 ──────────────────────


def test_domain_read_failure_marks_degraded(monkeypatch, caplog):
    _patch_io(monkeypatch)
    _install_runtime_domain(monkeypatch, _FakeCursor(exc=RuntimeError(_ERR_1146)))

    with caplog.at_level("WARNING"):
        r = _call()

    assert r["degraded"] is True, "查询失败必须显式降级（否则与「确实无引擎」不可区分）"
    assert r["engines_unavailable_reason"] == "query_failed"
    assert r["remote_engines"] == []
    assert r["needs_worker"] is True
    logs = caplog.text
    assert "engine-onboarding" in logs and "degraded" in logs
    assert "route=" in logs and "target_db=runtime" in logs


def test_domain_unavailable_never_falls_back_to_process_db(monkeypatch, caplog):
    """三库拓扑缺 GRAPE_DSN_RUNTIME：route_of 抛 DomainUnavailable，不得回落 mgmt。"""
    _patch_io(monkeypatch)

    def _no_dsn(_domain):
        raise db_registry.DomainUnavailable("runtime", "GRAPE_DSN_RUNTIME missing")

    monkeypatch.setattr(db_registry, "route_of", _no_dsn)

    with caplog.at_level("WARNING"):
        r = _call()

    assert r["degraded"] is True
    assert r["engines_unavailable_reason"] == "runtime_domain_unavailable"
    assert r["needs_worker"] is True
    assert "route=unavailable" in caplog.text


# ── C. 确实无引擎：degraded=false（与查询失败可区分） ────────────────────────


def test_genuinely_no_engine_is_not_degraded(monkeypatch):
    _patch_io(monkeypatch)
    _install_runtime_domain(monkeypatch, _FakeCursor(rows=[]))

    r = _call()

    assert r["degraded"] is False, "空结果是事实而非故障，不得标降级"
    assert r["needs_worker"] is True, "无可用引擎仍需引导安装 worker"


# ── D. 静态守门：读点不得再裸用进程库游标 ────────────────────────────────────


def test_read_path_no_longer_touches_session_manager():
    from src.domains.runtime.routes.runtime import runtime_engine_onboarding

    src = inspect.getsource(runtime_engine_onboarding)
    assert "from src.kernel.session_manager import" not in src, \
        "engine-onboarding 仍导入进程库直连（同病未收口）"
    assert "db.cursor()" not in src, "engine-onboarding 仍裸用进程库游标"
    assert 'cursor_for("runtime")' in src, "runtime 域表读必须走内核唯一路由"


# ── E. 单库拓扑零回归：无自有 DSN → 仍回落进程库 ─────────────────────────────


def test_single_db_topology_falls_back_to_process_db(monkeypatch):
    """单库/测试拓扑（无任何 DSN）→ route_of=local，行为与原实现一致。"""
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "0")
    monkeypatch.delenv("GRAPE_DSN_RUNTIME", raising=False)
    _patch_io(monkeypatch)

    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, host_name, scope, owner_id, "
            "credential_id, repo_key, org_id, status, installed) "
            "VALUES ('opencode','opencode','h','global','',0,'',0,'online',0)")
        db.commit()
    try:
        r = _call()
        assert r["degraded"] is False
        assert len(r["remote_engines"]) == 1
        assert r["needs_worker"] is False
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM agent_runtimes")
            db.commit()