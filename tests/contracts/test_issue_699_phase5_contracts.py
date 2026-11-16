"""Phase 5 acceptance tests — 契约接口填充（Issue #699 Phase 5）.

TC-5.1: MgmtService Local provider 契约接口验证
TC-5.2: RuntimeService Local provider 契约接口验证
TC-5.3: InsightService Local provider 契约接口验证
TC-5.4: 裸跨 schema SQL 消除验证（静态检查）
+ GSB 注册表/客户端/令牌/Provider 工厂基本功能测试
"""
import os
import sys
import json
import time
import tempfile
import sqlite3
import hashlib
import importlib
from unittest.mock import patch, MagicMock

import pytest

# Ensure project root on sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ===== Test fixtures =====

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Create a temporary SQLite DB and patch session_manager to use it."""
    db_path = str(tmp_path / "test_phase5.db")
    # Set env before importing session_manager
    monkeypatch.setenv("DEVOPS_DB_PATH", db_path)
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    # Ensure schema mode is off (bare table names, zero regression)
    monkeypatch.delenv("GRAPE_SCHEMA_MODE", raising=False)
    monkeypatch.delenv("GRAPE_MODE", raising=False)
    monkeypatch.delenv("GRAPE_PROVIDER_MODE", raising=False)
    monkeypatch.delenv("GRAPE_SERVICE_SECRET", raising=False)

    # Create DB and tables
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Create core tables for testing
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS managed_repos (
        repo_key TEXT PRIMARY KEY,
        repo_owner TEXT NOT NULL,
        repo_name TEXT NOT NULL,
        full_name TEXT NOT NULL,
        bot_gitcode_token TEXT DEFAULT '',
        gitcode_webhook_token TEXT DEFAULT '',
        bot_username TEXT DEFAULT '',
        org_id INTEGER DEFAULT 0,
        host_user_id INTEGER DEFAULT 0,
        webhook_secret TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS devops_agent_session (
        id TEXT PRIMARY KEY,
        repo_key TEXT NOT NULL,
        agent_type TEXT NOT NULL,
        skill TEXT DEFAULT '',
        status TEXT DEFAULT 'idle',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS devops_pipeline_scene (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id TEXT NOT NULL,
        name TEXT NOT NULL,
        repo_key TEXT NOT NULL DEFAULT '',
        nodes_json TEXT DEFAULT '[]',
        is_active INTEGER DEFAULT 1,
        UNIQUE (scene_id, repo_key)
    );
    CREATE TABLE IF NOT EXISTS agent_type_runtime_pool (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_type TEXT NOT NULL,
        runtime_id INTEGER NOT NULL,
        enabled INTEGER DEFAULT 1,
        priority INTEGER DEFAULT 5,
        weight INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS session_api_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key TEXT NOT NULL,
        repo_key TEXT NOT NULL DEFAULT '',
        agent_type TEXT NOT NULL DEFAULT '',
        user_id INTEGER NOT NULL DEFAULT 0,
        token_sha256 TEXT NOT NULL,
        expires_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS skill (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        skill_name TEXT NOT NULL,
        display_name TEXT DEFAULT '',
        description TEXT DEFAULT '',
        agent_type TEXT DEFAULT '',
        skill_path TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_key TEXT NOT NULL,
        event_type TEXT NOT NULL,
        source TEXT DEFAULT 'gitcode',
        issue_id TEXT DEFAULT '',
        title TEXT DEFAULT '',
        summary TEXT DEFAULT '',
        actor TEXT DEFAULT '',
        payload TEXT DEFAULT '{}',
        agent_type TEXT DEFAULT '',
        backend TEXT DEFAULT '',
        skill TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        exit_code INTEGER DEFAULT NULL,
        duration REAL DEFAULT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        started_at TEXT DEFAULT '',
        completed_at TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS agent_tasks (
        id TEXT PRIMARY KEY,
        agent_session_id TEXT NOT NULL,
        runtime_id INTEGER NOT NULL DEFAULT 0,
        event_seq INTEGER,
        issue_id TEXT DEFAULT '',
        repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT '',
        skill TEXT DEFAULT '',
        prompt TEXT DEFAULT '',
        cwd TEXT DEFAULT '',
        status TEXT DEFAULT 'queued',
        exit_code INTEGER DEFAULT NULL,
        result_text TEXT DEFAULT '',
        started_at TEXT DEFAULT NULL,
        completed_at TEXT DEFAULT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS task_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        tool TEXT DEFAULT '',
        content TEXT DEFAULT '',
        input_json TEXT DEFAULT '',
        output TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(task_id, seq)
    );
    CREATE TABLE IF NOT EXISTS issue_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_key TEXT NOT NULL,
        issue_id TEXT NOT NULL,
        stage TEXT DEFAULT 'vod',
        owner_agent TEXT DEFAULT 'vod',
        assignee TEXT DEFAULT '',
        a2a_seq INTEGER,
        scene_id TEXT DEFAULT '',
        updated_at TEXT DEFAULT (datetime('now')),
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE (repo_key, issue_id)
    );
    CREATE TABLE IF NOT EXISTS agent_runtimes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        credential_id INTEGER DEFAULT 0,
        name TEXT NOT NULL,
        display_name TEXT DEFAULT '',
        provider TEXT DEFAULT '',
        installed INTEGER DEFAULT 0,
        cli_path TEXT DEFAULT '',
        version TEXT DEFAULT '',
        healthy INTEGER DEFAULT 0,
        status TEXT DEFAULT 'offline',
        last_seen_at TEXT DEFAULT NULL,
        scope TEXT DEFAULT 'global',
        owner_id TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS obs_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedup_key TEXT NOT NULL UNIQUE,
        ts TEXT NOT NULL,
        level TEXT DEFAULT 'WARNING',
        component TEXT DEFAULT '',
        source TEXT DEFAULT '',
        trace_id TEXT DEFAULT '',
        task_id TEXT DEFAULT '',
        repo_key TEXT DEFAULT '',
        runtime_id INTEGER DEFAULT 0,
        msg TEXT DEFAULT '',
        extra_json TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS obs_llm_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedup_key TEXT NOT NULL UNIQUE,
        task_id TEXT NOT NULL,
        seq INTEGER DEFAULT 0,
        turn INTEGER DEFAULT 0,
        source TEXT DEFAULT '',
        repo_key TEXT DEFAULT '',
        scene_id TEXT DEFAULT '',
        agent_type TEXT DEFAULT '',
        engine_id INTEGER DEFAULT 0,
        user_id INTEGER DEFAULT 0,
        provider_raw TEXT DEFAULT '',
        model_raw TEXT DEFAULT '',
        model_key TEXT DEFAULT '',
        run_id TEXT DEFAULT '',
        run_kind TEXT DEFAULT 'task',
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_write_tokens INTEGER DEFAULT 0,
        reasoning_tokens INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS api_latency_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id VARCHAR(32) DEFAULT '',
        method VARCHAR(8) DEFAULT '',
        path VARCHAR(256) DEFAULT '',
        status INTEGER DEFAULT 0,
        duration_ms INTEGER DEFAULT 0,
        repo_key VARCHAR(128) DEFAULT '',
        user_id VARCHAR(32) DEFAULT '',
        source VARCHAR(16) DEFAULT 'backend',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS gsb_service_registry (
        service_name TEXT PRIMARY KEY,
        base_url TEXT NOT NULL,
        health_url TEXT,
        version TEXT DEFAULT '1',
        status TEXT DEFAULT 'online',
        last_heartbeat REAL DEFAULT 0,
        metadata TEXT DEFAULT '{}'
    );
    """)
    conn.commit()

    # Insert test data
    conn.execute(
        "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
        "bot_gitcode_token, gitcode_webhook_token, bot_username, org_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("test/repo", "test", "repo", "test/repo", "bot_tok_123", "wh_tok_456", "bot_user", 1),
    )
    conn.execute(
        "INSERT INTO devops_agent_session (id, repo_key, agent_type, skill, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("sess_1", "test/repo", "coding", "coding-cli-1", "idle"),
    )
    conn.execute(
        "INSERT INTO agent_type_runtime_pool (agent_type, runtime_id, enabled, priority, weight) "
        "VALUES (?, ?, ?, ?, ?)",
        ("coding", 100, 1, 1, 10),
    )
    conn.execute(
        "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active) "
        "VALUES (?, ?, ?, ?, ?)",
        ("scene_1", "Test Scene", "test/repo", json.dumps([{"tag": "coding"}, {"tag": "qa"}]), 1),
    )
    conn.execute(
        "INSERT INTO session_api_tokens (session_key, repo_key, agent_type, user_id, token_sha256, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("sess_key_1", "test/repo", "coding", 42, hashlib.sha256(b"test_token").hexdigest(), int(time.time()) + 3600),
    )
    conn.execute(
        "INSERT INTO skill (id, skill_name, display_name, agent_type, skill_path, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (1, "coding-cli-1", "Coding CLI", "coding", "skills/coding", 1),
    )
    conn.execute(
        "INSERT INTO events (seq, repo_key, event_type, status, title, actor) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (1, "test/repo", "issue_opened", "completed", "Test Event", "user1"),
    )
    conn.execute(
        "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, skill, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("task_1", "sess_1", 100, "test/repo", "coding", "coding-cli-1", "completed"),
    )
    conn.execute(
        "INSERT INTO task_messages (id, task_id, seq, type, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, "task_1", 1, "text", "Hello world"),
    )
    conn.execute(
        "INSERT INTO issue_state (repo_key, issue_id, stage, owner_agent) "
        "VALUES (?, ?, ?, ?)",
        ("test/repo", "#99", "coding", "coding"),
    )
    conn.execute(
        "INSERT INTO agent_runtimes (id, name, status, healthy) "
        "VALUES (?, ?, ?, ?)",
        (100, "engine_1", "online", 1),
    )
    conn.commit()
    conn.close()

    # Patch session_manager db/db_lock to use our temp DB
    import src.kernel.session_manager as sm
    # Reload to pick up new DEVOPS_DB_PATH
    importlib.reload(sm)

    yield sm

    # Cleanup：先撤掉本 fixture 设的 env 再 reload —— 否则第二次 reload 仍读临时库路径，
    # session_manager 会被永久绑在那个只有 5 张表的临时库上；同会话后续用例里凡是
    # 懒解析 db 的 owner 域原语（_any_cursor 回落进程库）都会看到「no such table」。
    monkeypatch.undo()
    importlib.reload(sm)


# ===== TC-5.1: MgmtService Local provider =====

class TestTC51MgmtServiceLocal:
    """TC-5.1: MgmtService Local provider 契约接口验证。"""

    def test_get_repo_webhook_config(self, temp_db):
        from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
        svc = MgmtServiceLocal()
        cfg = svc.get_repo_webhook_config("test/repo")
        assert cfg["repo_key"] == "test/repo"
        assert cfg["webhook_token"] == "wh_tok_456"
        assert cfg["bot_username"] == "bot_user"

    def test_get_agent_session(self, temp_db):
        from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
        svc = MgmtServiceLocal()
        sess = svc.get_agent_session("test/repo", "coding")
        assert sess["id"] == "sess_1"
        assert sess["skill"] == "coding-cli-1"
        assert sess["runtime_id"] == 100

    def test_get_scene_config(self, temp_db):
        from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
        svc = MgmtServiceLocal()
        scenes = svc.get_scene_config("test/repo")
        assert len(scenes) == 1
        assert scenes[0]["scene_id"] == "scene_1"

    def test_get_scene_pool_engine_ids(self, temp_db):
        from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
        svc = MgmtServiceLocal()
        ids = svc.get_scene_pool_engine_ids("scene_1")
        assert 100 in ids

    def test_verify_session_token(self, temp_db):
        from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
        svc = MgmtServiceLocal()
        token_hash = hashlib.sha256(b"test_token").hexdigest()
        result = svc.verify_session_token(token_hash)
        assert result["credential_id"] is not None
        assert result["owner_id"] == 42

    def test_get_bot_config(self, temp_db):
        from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
        svc = MgmtServiceLocal()
        cfg = svc.get_bot_config("test/repo")
        assert cfg["bot_token"] == "bot_tok_123"
        assert cfg["bot_username"] == "bot_user"

    def test_get_skill(self, temp_db):
        from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
        svc = MgmtServiceLocal()
        skill = svc.get_skill("1")
        assert skill["skill_name"] == "coding-cli-1"


# ===== TC-5.2: RuntimeService Local provider =====

class TestTC52RuntimeServiceLocal:
    """TC-5.2: RuntimeService Local provider 契约接口验证。"""

    def test_get_events(self, temp_db):
        from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
        svc = RuntimeServiceLocal()
        events = svc.get_events("test/repo")
        assert len(events) == 1
        assert events[0]["seq"] == 1

    def test_get_event_detail(self, temp_db):
        from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
        svc = RuntimeServiceLocal()
        evt = svc.get_event_detail(1)
        assert evt["repo_key"] == "test/repo"

    def test_get_tasks(self, temp_db):
        from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
        svc = RuntimeServiceLocal()
        tasks = svc.get_tasks("test/repo")
        assert len(tasks) == 1
        assert tasks[0]["id"] == "task_1"

    def test_get_task_messages(self, temp_db):
        from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
        svc = RuntimeServiceLocal()
        msgs = svc.get_task_messages("task_1")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello world"

    def test_get_runtimes(self, temp_db):
        from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
        svc = RuntimeServiceLocal()
        runtimes = svc.get_runtimes()
        assert len(runtimes) == 1
        assert runtimes[0]["name"] == "engine_1"

    def test_get_issue_state(self, temp_db):
        from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
        svc = RuntimeServiceLocal()
        state = svc.get_issue_state("test/repo", 99)
        assert state["stage"] == "coding"
        assert state["owner_agent"] == "coding"

    def test_update_issue_state(self, temp_db):
        from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
        svc = RuntimeServiceLocal()
        result = svc.update_issue_state("test/repo", 99, {"stage": "qa", "owner_agent": "qa"})
        assert result["ok"] is True
        state = svc.get_issue_state("test/repo", 99)
        assert state["stage"] == "qa"


# ===== TC-5.3: InsightService Local provider =====

class TestTC53InsightServiceLocal:
    """TC-5.3: InsightService Local provider 契约接口验证。"""

    def test_log_obs(self, temp_db):
        from src.domains.contracts.local.insight_local import InsightServiceLocal
        svc = InsightServiceLocal()
        result = svc.log_obs({
            "source": "runtime",
            "level": "INFO",
            "msg": "test message",
            "repo_key": "test/repo",
            "task_id": "task_1",
        })
        assert result["ok"] is True
        # Verify it was written
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM obs_log WHERE repo_key=?", ("test/repo",))
            count = c.fetchone()[0]
        assert count == 1

    def test_log_llm_usage(self, temp_db):
        from src.domains.contracts.local.insight_local import InsightServiceLocal
        svc = InsightServiceLocal()
        result = svc.log_llm_usage({
            "task_id": "task_1",
            "seq": 1,
            "repo_key": "test/repo",
            "input_tokens": 100,
            "output_tokens": 50,
        })
        assert result["ok"] is True

    def test_log_api_latency(self, temp_db):
        from src.domains.contracts.local.insight_local import InsightServiceLocal
        svc = InsightServiceLocal()
        result = svc.log_api_latency({
            "method": "GET",
            "path": "/api/health",
            "status": 200,
            "duration_ms": 15,
            "repo_key": "test/repo",
        })
        assert result["ok"] is True

    def test_aggregate_runtime_stats(self, temp_db):
        from src.domains.contracts.local.insight_local import InsightServiceLocal
        svc = InsightServiceLocal()
        stats = svc.aggregate_runtime_stats()
        assert "events_by_status" in stats
        assert "tasks_by_status" in stats
        assert stats["events_by_status"].get("completed", 0) == 1
        assert stats["tasks_by_status"].get("completed", 0) == 1


# ===== TC-5.4: 裸跨 schema SQL 消除验证 =====

class TestTC54NoRawCrossSchemaSQL:
    """TC-5.4: contracts/ 外无裸跨 schema SQL。"""

    def test_no_raw_grape_runtime_in_mgmt(self):
        """mgmt 域（排除 contracts/）不应有裸 grape_runtime. SQL。"""
        import subprocess
        mgmt_path = os.path.join(_REPO_ROOT, "src", "domains", "mgmt")
        result = subprocess.run(
            ["grep", "-rn", "grape_runtime\\.", mgmt_path,
             "--include=*.py", "--exclude-dir=contracts"],
            capture_output=True, text=True,
        )
        # grep returns 1=not found (desired), 0=found (violation), 2=error
        assert result.returncode == 1, (
            f"mgmt 域发现裸 grape_runtime. SQL 引用（应走 contracts 层）：\n{result.stdout}"
        )

    def test_no_raw_grape_mgmt_in_runtime(self):
        """runtime 域（排除 contracts/）不应有裸 grape_mgmt. SQL。"""
        import subprocess
        runtime_path = os.path.join(_REPO_ROOT, "src", "domains", "runtime")
        result = subprocess.run(
            ["grep", "-rn", "grape_mgmt\\.", runtime_path,
             "--include=*.py", "--exclude-dir=contracts"],
            capture_output=True, text=True,
        )
        assert result.returncode == 1, (
            f"runtime 域发现裸 grape_mgmt. SQL 引用（应走 contracts 层）：\n{result.stdout}"
        )

    def test_contracts_use_t_mgmt_t_runtime_t_insight(self):
        """contracts/local/ 实现类应使用 t_mgmt()/t_runtime()/t_insight() 映射。"""
        import subprocess
        for f in ["mgmt_local.py", "runtime_local.py", "insight_local.py"]:
            path = os.path.join(_REPO_ROOT, "src", "domains", "contracts", "local", f)
            result = subprocess.run(
                ["grep", "-c", "t_mgmt\\|t_runtime\\|t_insight", path],
                capture_output=True, text=True,
            )
            count = int(result.stdout.strip()) if result.stdout.strip() else 0
            assert count > 0, f"{f} should use t_mgmt()/t_runtime()/t_insight()"


# ===== GSB framework tests =====

class TestGSBRegistry:
    """GSB 服务注册表功能测试。"""

    def test_register_and_discover(self, temp_db):
        from src.kernel.gsb import registry
        registry.register_service("test_svc", "http://localhost:9999")
        svc = registry.discover_service("test_svc")
        assert svc["service_name"] == "test_svc"
        assert svc["base_url"] == "http://localhost:9999"

    def test_heartbeat_updates(self, temp_db):
        from src.kernel.gsb import registry
        registry.register_service("test_svc2", "http://localhost:8888")
        time.sleep(0.01)
        registry.heartbeat_service("test_svc2")
        svc = registry.discover_service("test_svc2")
        assert svc["status"] == "online"

    def test_deregister(self, temp_db):
        from src.kernel.gsb import registry
        registry.register_service("test_svc3", "http://localhost:7777")
        registry.deregister_service("test_svc3")
        svc = registry.discover_service("test_svc3")
        assert svc == {}

    def test_list_services(self, temp_db):
        from src.kernel.gsb import registry
        registry.register_service("svc_a", "http://localhost:1")
        registry.register_service("svc_b", "http://localhost:2")
        services = registry.list_services()
        names = [s["service_name"] for s in services]
        assert "svc_a" in names
        assert "svc_b" in names


class TestServiceToken:
    """服务令牌生成/验证测试。"""

    def test_generate_and_verify(self, monkeypatch):
        monkeypatch.setenv("GRAPE_SERVICE_SECRET", "test_secret_key_123")
        import importlib
        import src.kernel.auth_sdk.service_token as st
        importlib.reload(st)
        token = st.generate_service_token("runtime", "mgmt")
        assert token != ""
        assert st.verify_service_token(token) is True

    def test_verify_with_target(self, monkeypatch):
        monkeypatch.setenv("GRAPE_SERVICE_SECRET", "test_secret_key_456")
        import importlib
        import src.kernel.auth_sdk.service_token as st
        importlib.reload(st)
        token = st.generate_service_token("runtime", "mgmt")
        assert st.verify_service_token(token, expected_target="mgmt") is True
        assert st.verify_service_token(token, expected_target="insight") is False

    def test_empty_secret_skips_auth(self, monkeypatch):
        monkeypatch.delenv("GRAPE_SERVICE_SECRET", raising=False)
        import importlib
        import src.kernel.auth_sdk.service_token as st
        importlib.reload(st)
        token = st.generate_service_token("runtime")
        assert token == ""
        assert st.verify_service_token("") is True

    def test_invalid_token_rejected(self, monkeypatch):
        monkeypatch.setenv("GRAPE_SERVICE_SECRET", "test_secret_key_789")
        import importlib
        import src.kernel.auth_sdk.service_token as st
        importlib.reload(st)
        assert st.verify_service_token("invalid.token.here") is False
        assert st.verify_service_token("") is False


class TestProviderFactory:
    """Provider 工厂切换测试。"""

    def test_single_mode_returns_local(self, monkeypatch, temp_db):
        monkeypatch.delenv("GRAPE_MODE", raising=False)
        monkeypatch.delenv("GRAPE_PROVIDER_MODE", raising=False)
        # Import contracts to register providers
        import src.domains.contracts  # noqa: F401
        from src.kernel.gsb.provider import get_mgmt_service, is_remote_mode
        assert is_remote_mode() is False
        svc = get_mgmt_service()
        assert svc.__class__.__name__ == "MgmtServiceLocal"

    def test_cluster_remote_mode_returns_remote(self, monkeypatch, temp_db):
        """Issue #2479：remote 制式改为按方法路由代理（默认偏好 remote + 热路径 lib）。"""
        monkeypatch.setenv("GRAPE_MODE", "cluster")
        monkeypatch.setenv("GRAPE_PROVIDER_MODE", "remote")
        import src.domains.contracts  # noqa: F401
        from src.kernel.gsb.provider import decide_mode, get_mgmt_service, is_remote_mode
        assert is_remote_mode() is True
        # mgmt 作为被调方恒 lib（matrix 守卫）→ 拿到的是 Local 实例，不是路由代理
        mgmt_svc = get_mgmt_service()
        assert mgmt_svc.__class__.__name__ == "MgmtServiceLocal"
        # 代理仍存在于允许 remote 的方向
        from src.kernel.gsb.provider import get_runtime_service
        svc = get_runtime_service()
        assert svc.__class__.__name__ == "_RoutedService"
        # 内置基线（QA 审查修复 Required#1）：压测超阈值项（get_bot_config）
        # 基线锁 lib；采样错误项默认偏好 remote；热路径方法锁 lib
        assert decide_mode("mgmt", "get_bot_config") == "lib"
        assert decide_mode("insight", "query_metrics") == "remote"
        assert decide_mode("mgmt", "verify_session_token") == "lib"

    def test_cluster_without_remote_mode_returns_local(self, monkeypatch, temp_db):
        monkeypatch.setenv("GRAPE_MODE", "cluster")
        monkeypatch.delenv("GRAPE_PROVIDER_MODE", raising=False)
        import src.domains.contracts  # noqa: F401
        from src.kernel.gsb.provider import get_runtime_service
        svc = get_runtime_service()
        assert svc.__class__.__name__ == "RuntimeServiceLocal"

    def test_unknown_service_raises(self, temp_db):
        from src.kernel.gsb.provider import get_service
        with pytest.raises(ValueError, match="Unknown service"):
            get_service("nonexistent")


class TestInternalEndpoints:
    """/internal/* 端点基本结构测试。"""

    def test_mgmt_internal_router_exists(self):
        # P2/D5 修订：mgmt 由"完全不开放"改为只读白名单 + xwrite/apply
        from src.domains.mgmt.routes.internal import readonly_router
        assert readonly_router.prefix == "/internal"
        routes = [r.path for r in readonly_router.routes]
        assert any("/repos/" in r for r in routes)
        assert any("/tokens/verify" in r for r in routes)

    def test_runtime_internal_router_exists(self):
        from src.domains.runtime.routes.internal import router
        assert router.prefix == "/internal"
        routes = [r.path for r in router.routes]
        assert any("/tasks" in r for r in routes)
        assert any("/events" in r for r in routes)

    def test_insight_internal_router_exists(self):
        from src.domains.insight.routes.internal import router
        assert router.prefix == "/internal"
        routes = [r.path for r in router.routes]
        assert any("/logs" in r for r in routes)
        assert any("/latency" in r for r in routes)

    def test_internal_routes_mounted_on_servers(self):
        """四进程入口应挂载对应域的 /internal/* 路由（通过 TestClient 验证可达）。"""
        from starlette.testclient import TestClient
        from src.api.grape_worker_server import app as worker_app
        from src.api.grape_insight_server import app as insight_app

        # Worker: /internal/tasks 路由应存在（非 404）
        worker_client = TestClient(worker_app)
        resp = worker_client.get("/internal/tasks?repo_key=test")
        assert resp.status_code != 404, "Worker should mount /internal/tasks"

        # Insight: /internal/logs 路由应存在（非 404）
        insight_client = TestClient(insight_app)
        resp = insight_client.post("/internal/logs", json={})
        assert resp.status_code != 404, "Insight should mount /internal/logs"

    def test_cluster_empty_secret_rejects_internal(self, temp_db, monkeypatch):
        """集群模式 + 空密钥 → /internal/* 请求被 fail-closed 拒绝（503）。"""
        monkeypatch.setenv("GRAPE_MODE", "cluster")
        monkeypatch.delenv("GRAPE_SERVICE_SECRET", raising=False)
        # 重置 middleware 告警标志
        import src.kernel.auth_sdk.internal_middleware as mw_mod
        mw_mod._WARNED_NO_SECRET = False
        # 重置 SERVICE_SECRET 模块级变量
        import src.kernel.auth_sdk.service_token as st_mod
        st_mod.SERVICE_SECRET = ""

        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from src.kernel.auth_sdk.internal_middleware import InternalAuthMiddleware

        test_app = FastAPI()
        test_app.add_middleware(InternalAuthMiddleware)

        @test_app.post("/internal/test")
        async def _test():
            return {"ok": True}

        client = TestClient(test_app)
        resp = client.post("/internal/test")
        assert resp.status_code == 503, f"cluster+empty secret should return 503, got {resp.status_code}"
        assert resp.json()["code"] == "SERVICE_SECRET_NOT_CONFIGURED"


class TestContractsRegistration:
    """契约注册测试（惰性注册——首次调用 get_service 时触发）。"""

    def test_providers_registered_on_import(self, temp_db):
        """惰性注册：调用 ensure_providers_registered() 后 Provider 入表。"""
        from src.domains.contracts import ensure_providers_registered
        ensure_providers_registered()
        from src.kernel.gsb.provider import _PROVIDERS
        assert "mgmt" in _PROVIDERS
        assert "runtime" in _PROVIDERS
        assert "insight" in _PROVIDERS
        assert _PROVIDERS["mgmt"]["local"] is not None
        assert _PROVIDERS["mgmt"]["remote"] is not None
