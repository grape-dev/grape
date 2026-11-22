"""Issue #3811：release guard 按域读取 runtime.agent_tasks。

三库拓扑下 `agent_tasks` 属 runtime 域；mgmt/worker 进程主库是 mgmt，裸 SQL 恒 1146。
本用例锁三件事：
1. runtime 有自有 DSN 时，`_read_task_issue_id` 命中 runtime 库（不是进程库）；
2. 无自有 DSN 回落进程库时，打跨域降级告警；
3. 查询异常时 fail-open 可观测（计数 + 告警）。
"""
import logging
import sqlite3
import tempfile
import pytest

from src.domains.runtime.vcs import release_guard
from src.kernel import db_registry, session_manager as sm


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def dual_sqlite_dbs(monkeypatch):
    """双库 fixture：mgmt 库存错值、runtime 库存正确值。"""
    tmp = tempfile.mkdtemp(prefix="grape-3811-db-")
    mgmt_path = f"{tmp}/mgmt.db"
    runtime_path = f"{tmp}/runtime.db"
    for path, issue in ((mgmt_path, "#2085"), (runtime_path, "#3811")):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE agent_tasks (id TEXT PRIMARY KEY, issue_id TEXT)")
        conn.execute("INSERT INTO agent_tasks (id, issue_id) VALUES ('t_3811', ?)", (issue,))
        conn.commit()
        conn.close()

    def _connect(path):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(sm, "_db_path", "")  # 跳过 SQLite 只读快路径，进入域路由分支
    yield _connect(mgmt_path), _connect(runtime_path), mgmt_path, runtime_path


def test_reads_runtime_domain_when_own_dsn(dual_sqlite_dbs, monkeypatch):
    _mgmt_conn, runtime_conn, _mgmt_path, _runtime_path = dual_sqlite_dbs
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: d == "runtime")
    monkeypatch.setattr(db_registry, "domain_db",
                        lambda d: runtime_conn if d == "runtime" else None)
    monkeypatch.setattr(db_registry, "domain_lock", lambda d: _NullLock())

    assert release_guard._read_task_issue_id("t_3811") == "#3811"


def test_fallback_to_process_db_warns_cross_domain(dual_sqlite_dbs, monkeypatch, caplog):
    mgmt_conn, _runtime_conn, _mgmt_path, _runtime_path = dual_sqlite_dbs
    monkeypatch.setattr(sm, "db", mgmt_conn)
    monkeypatch.setattr(sm, "db_lock", _NullLock())
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: False)
    monkeypatch.setattr(db_registry, "domain_db", lambda d: None)
    monkeypatch.setattr(db_registry._local, "_warned_cross_runtime", False, raising=False)

    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_prod_mgmt")
    monkeypatch.setenv("GRAPE_RUNTIME_DB_NAME", "grape_prod_runtime")
    monkeypatch.delenv("GRAPE_DSN_RUNTIME", raising=False)

    with caplog.at_level(logging.ERROR):
        assert release_guard._read_task_issue_id("t_3811") == "#2085"

    guard_logs = [r.getMessage() for r in caplog.records if "[db-guard]" in r.getMessage()]
    assert guard_logs, "回落进程库必须打跨域降级告警"
    assert "runtime" in guard_logs[0] and "GRAPE_DSN_RUNTIME" in guard_logs[0]


def test_fail_open_is_counted_and_logged(monkeypatch, caplog):
    class _Boom:
        def cursor(self):
            raise RuntimeError("Table 'grape_prod_mgmt.agent_tasks' doesn't exist")

    monkeypatch.setattr(sm, "_db_path", "")
    monkeypatch.setattr(db_registry, "uses_own_dsn", lambda d: False)
    monkeypatch.setattr(db_registry, "domain_db", lambda d: _Boom())
    monkeypatch.setattr(db_registry, "warn_if_cross_domain_fallback", lambda d: None)

    before = release_guard._FAIL_OPEN_STATS["count"]
    with caplog.at_level(logging.WARNING):
        assert release_guard._read_task_issue_id("t_3811") is None
    assert release_guard._FAIL_OPEN_STATS["count"] == before + 1
    msgs = [r.getMessage() for r in caplog.records]
    assert any("fail-open" in m and "t_3811" in m for m in msgs)


def test_guard_still_blocks_mismatched_issue(monkeypatch):
    """回归：域路由修好后，任务 Issue 可解析，跨 Issue 正文写入仍返回 409。"""
    monkeypatch.setattr(release_guard, "_read_task_issue_id", lambda key: "#3811")
    user = {"auth_type": "session_token", "agent_type": "coding", "session_key": "t_3811"}
    assert release_guard.resolve_session_task_issue(user) == 3811
    ok, msg = release_guard.check_issue_consistency(
        task_issue=3811, target_issue=2085, body="普通正文", operation="issues update")
    assert not ok and "#2085" in msg and "#3811" in msg