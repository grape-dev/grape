"""Issue #495：调度侧 healthy 门控 + 连续失败熔断 单元测试。

验收标准（Spec 评论 196643584 / 196675863）：
- healthy=0 的 runtime 不被派发新任务
- 同一 runtime 连续失败达阈值（默认 3）自动置 offline 并记录日志
- 任务成功后连续失败计数重置
- 阈值可配置（ENGINE_CIRCUIT_BREAKER_THRESHOLD）

夹具复用 test_local_claimer 的内存 sqlite 模式。
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def cb_env(tmp_path, monkeypatch):
    """内存 sqlite + 基础表数据（一台健康 / 一台不健康引擎）。"""
    import sqlite3

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")

    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'online',
        healthy INTEGER DEFAULT 1, credential_id INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global', busy_slots INTEGER DEFAULT 0,
        max_concurrent INTEGER DEFAULT 20, last_seen_at TEXT DEFAULT '',
        probe_status TEXT DEFAULT 'ok', display_name TEXT DEFAULT ''
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (
        runtime_id INTEGER, tag TEXT, enabled INTEGER DEFAULT 1
    );
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY, agent_session_id TEXT, runtime_id INTEGER DEFAULT 0,
        event_seq INTEGER, issue_id TEXT DEFAULT '', repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT '', skill TEXT DEFAULT '', prompt TEXT DEFAULT '',
        cwd TEXT DEFAULT '', runtime_session_id TEXT DEFAULT '',
        event_type TEXT DEFAULT '', account TEXT DEFAULT '', user_id INTEGER DEFAULT 0,
        repo_gitcode_token TEXT DEFAULT '', user_gitcode_token TEXT DEFAULT '',
        env_extra TEXT DEFAULT '{}', skill_files TEXT DEFAULT '{}', server_url TEXT DEFAULT '',
        issue_session_id TEXT DEFAULT '', project_info_files TEXT DEFAULT '{}',
        project_info_meta TEXT DEFAULT '{}', knowledge_files TEXT DEFAULT '{}',
        project_info_file_urls TEXT DEFAULT '[]', knowledge_file_urls TEXT DEFAULT '[]',
        attempts INTEGER DEFAULT 0, pool_agent_type TEXT DEFAULT '',
        affinity_runtime_id INTEGER DEFAULT 0, pool_tag TEXT DEFAULT '',
        pool_engine_ids TEXT DEFAULT '', pool_engine_source TEXT DEFAULT '',
        status TEXT DEFAULT 'queued', stream_ended INTEGER DEFAULT 0,
        self_reported INTEGER DEFAULT 0, exit_code INTEGER, result_text TEXT DEFAULT '',
        errors TEXT DEFAULT '', stderr TEXT DEFAULT '', usage_json TEXT DEFAULT '{}',
        started_at TEXT, completed_at TEXT, created_at TEXT,
        skill_id INTEGER DEFAULT 0
    );
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY, repo_key TEXT, issue_id TEXT, event_type TEXT,
        source TEXT, actor TEXT, payload TEXT DEFAULT '{}', title TEXT DEFAULT '',
        agent_type TEXT, runtime TEXT, skill TEXT, status TEXT DEFAULT 'completed',
        created_at TEXT
    );
    CREATE TABLE devops_agent_session (id TEXT PRIMARY KEY, repo_key TEXT, agent_type TEXT, runtime_id INTEGER DEFAULT 0);
    CREATE TABLE issue_agent_session (id TEXT PRIMARY KEY, session_engine TEXT DEFAULT '');
    CREATE TABLE runtime_health_scores (
        runtime_id INTEGER PRIMARY KEY, health_score INTEGER NOT NULL DEFAULT 100,
        total_tasks INTEGER NOT NULL DEFAULT 0, failed_tasks INTEGER NOT NULL DEFAULT 0,
        completed_tasks INTEGER NOT NULL DEFAULT 0, consecutive_failures INTEGER NOT NULL DEFAULT 0,
        failure_rate REAL NOT NULL DEFAULT 0.0, last_status TEXT DEFAULT '',
        window_days INTEGER NOT NULL DEFAULT 7, updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE unified_user_mapping (user_id INTEGER, gitcode_user TEXT, feishu_open_id TEXT DEFAULT '');
    """)
    # 6280 = 事故中的 codearts worker（healthy=0）；1079 = 健康 opencode worker
    for rid, name, healthy in [(6280, "codearts", 0), (1079, "opencode", 1)]:
        cur.execute("INSERT INTO agent_runtimes (id, name, credential_id, scope, status, healthy, last_seen_at) "
                    "VALUES (?,?,0,'global','online',?,?)",
                    (rid, name, healthy, datetime.now(timezone.utc).isoformat()))
        cur.execute("INSERT INTO engine_capabilities (runtime_id, tag) VALUES (?, 'all')", (rid,))
        cur.execute("INSERT INTO runtime_health_scores (runtime_id) VALUES (?)", (rid,))
    conn.commit()

    import src.session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=False)
    import src.runtime.service as svc
    monkeypatch.setattr(svc, "db", conn, raising=False)
    import src.runtime.circuit_breaker as cb
    cb.reset_all()
    yield conn
    cb.reset_all()
    conn.close()


def _enqueue(cur, tid, tag="vod", repo="gitcode:o/r", created_off=0):
    created = (datetime.now(timezone.utc) - timedelta(seconds=created_off)).isoformat()
    cur.execute(
        "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, repo_key, agent_type, "
        "pool_tag, status, created_at, prompt, cwd) "
        "VALUES (?,?,0,1,?,?,?, 'queued', ?, 'p', 'w')",
        (tid, "sess_cb", repo, tag, tag, created))
    cur.connection.commit()


class TestHealthyGate:
    def test_unhealthy_engine_not_dispatched(self, cb_env):
        """验收 1：healthy=0 的 runtime（6280 事故场景）claim_by_tag 返回 None。"""
        from src.runtime.service import claim_by_tag
        cur = cb_env.cursor()
        _enqueue(cur, "t_cb1")
        assert claim_by_tag("all", 6280) is None

    def test_healthy_engine_claims(self, cb_env):
        """验收 1 对照：healthy=1 的 runtime 正常认领同一任务。"""
        from src.runtime.service import claim_by_tag
        cur = cb_env.cursor()
        _enqueue(cur, "t_cb2")
        task = claim_by_tag("all", 1079)
        assert task and task["id"] == "t_cb2"
        cur.execute("SELECT status, runtime_id FROM agent_tasks WHERE id='t_cb2'")
        assert cur.fetchone() == ("running", 1079)

    def test_null_healthy_fail_open(self, cb_env):
        """healthy 为 NULL 的存量行 fail-open，不阻塞认领。"""
        from src.runtime.service import claim_by_tag
        cur = cb_env.cursor()
        cur.execute("UPDATE agent_runtimes SET healthy=NULL WHERE id=1079")
        cb_env.commit()
        _enqueue(cur, "t_cb3")
        assert claim_by_tag("all", 1079) is not None

    def test_legacy_claim_gate(self, cb_env):
        """验收 1（legacy 路径）：claim() 对 healthy=0 引擎同样拒绝。"""
        from src.runtime.service import claim
        cur = cb_env.cursor()
        _enqueue(cur, "t_cb4")
        cur.execute("UPDATE agent_tasks SET runtime_id=6280 WHERE id='t_cb4'")
        cb_env.commit()
        assert claim(6280) is None
        # 对照：健康引擎定向认领成功
        cur.execute("UPDATE agent_tasks SET runtime_id=1079, status='queued' WHERE id='t_cb4'")
        cb_env.commit()
        task = claim(1079)
        assert task and task["id"] == "t_cb4"


class TestCircuitBreaker:
    def test_consecutive_failures_trip_offline(self, cb_env):
        """验收 2：同一 runtime 连续失败 3 次（默认阈值）自动置 offline + healthy=0。"""
        from src.runtime import service as svc
        from src.runtime import circuit_breaker as cb
        for i in range(1, 4):
            cur = cb_env.cursor()
            _enqueue(cur, f"t_fail{i}")
            cur.execute("UPDATE agent_tasks SET runtime_id=1079, status='running' WHERE id=?", (f"t_fail{i}",))
            cb_env.commit()
            svc.fail_task(f"t_fail{i}", "engine exited rc=127", exit_code=127)
            assert cb.get_failure_count(1079) == i
        cur = cb_env.cursor()
        cur.execute("SELECT status, healthy FROM agent_runtimes WHERE id=1079")
        assert cur.fetchone() == ("offline", 0)
        # 熔断后（healthy=0）不再被派发新任务
        _enqueue(cur, "t_after_trip")
        from src.runtime.service import claim_by_tag
        assert claim_by_tag("all", 1079) is None

    def test_below_threshold_no_trip(self, cb_env):
        """失败 2 次（< 阈值 3）不熔断，仍 online。"""
        from src.runtime import service as svc
        from src.runtime import circuit_breaker as cb
        for i in range(1, 3):
            cur = cb_env.cursor()
            _enqueue(cur, f"t_nf{i}")
            cur.execute("UPDATE agent_tasks SET runtime_id=1079, status='running' WHERE id=?", (f"t_nf{i}",))
            cb_env.commit()
            svc.fail_task(f"t_nf{i}", "boom", exit_code=127)
        cur = cb_env.cursor()
        cur.execute("SELECT status, healthy FROM agent_runtimes WHERE id=1079")
        assert cur.fetchone() == ("online", 1)

    def test_success_resets_counter(self, cb_env):
        """验收 3：任务成功（exit=0）后连续失败计数清零——2 失败 + 1 成功 + 2 失败 < 阈值不熔断。"""
        from src.runtime import service as svc
        from src.runtime import circuit_breaker as cb

        def _run_task(tid, terminal):
            cur = cb_env.cursor()
            _enqueue(cur, tid)
            cur.execute("UPDATE agent_tasks SET runtime_id=1079, status='running' WHERE id=?", (tid,))
            cb_env.commit()
            if terminal == "fail":
                svc.fail_task(tid, "boom", exit_code=127)
            else:
                svc.complete_task(tid, "ok done", exit_code=0)

        _run_task("t_r1", "fail")
        _run_task("t_r2", "fail")
        assert cb.get_failure_count(1079) == 2
        _run_task("t_r3", "success")
        assert cb.get_failure_count(1079) == 0
        _run_task("t_r4", "fail")
        _run_task("t_r5", "fail")
        assert cb.get_failure_count(1079) == 2
        cur = cb_env.cursor()
        cur.execute("SELECT status, healthy FROM agent_runtimes WHERE id=1079")
        assert cur.fetchone() == ("online", 1), "成功清零后未达阈值，不应熔断"

    def test_threshold_configurable(self, cb_env, monkeypatch):
        """验收 4：阈值可配置（ENGINE_CIRCUIT_BREAKER_THRESHOLD=2 → 2 次即熔断）。"""
        import src.runtime.circuit_breaker as cb
        monkeypatch.setattr(cb, "FAILURE_THRESHOLD", 2)
        from src.runtime import service as svc
        for i in range(1, 3):
            cur = cb_env.cursor()
            _enqueue(cur, f"t_cfg{i}")
            cur.execute("UPDATE agent_tasks SET runtime_id=1079, status='running' WHERE id=?", (f"t_cfg{i}",))
            cb_env.commit()
            svc.fail_task(f"t_cfg{i}", "boom", exit_code=127)
        cur = cb_env.cursor()
        cur.execute("SELECT status, healthy FROM agent_runtimes WHERE id=1079")
        assert cur.fetchone() == ("offline", 0)

    def test_trip_is_idempotent(self, cb_env, monkeypatch):
        """已 offline 后继续失败不重复写/不报错，计数继续累积。"""
        import src.runtime.circuit_breaker as cb
        cb.record_task_failure(1079, "t_x1", 127)
        cb.record_task_failure(1079, "t_x2", 127)
        n = cb.record_task_failure(1079, "t_x3", 127)
        assert n == 3
        n2 = cb.record_task_failure(1079, "t_x4", 127)
        assert n2 == 4
        cur = cb_env.cursor()
        cur.execute("SELECT status, healthy FROM agent_runtimes WHERE id=1079")
        assert cur.fetchone() == ("offline", 0)
