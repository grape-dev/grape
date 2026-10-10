"""事件详情引擎三级回退逻辑测试（Issue #285）

验证：
- 未派发（runtime_id=0 且 failed）→ engine_instance/device/ip 为空，不兜底到无关引擎
- 正常执行（runtime_id>0）→ 引擎实例/设备/IP 正确解析
- direct 模式（devops_agent_session 绑定）→ 引擎显示不受影响
- 引擎健康分仅 runtime_id>0 时展示
"""
import os, sys, json, sqlite3, tempfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import threading

import pytest


RK = "gitcode:org/engfallback"


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """创建临时 SQLite DB 并替换全局 db/db_lock。"""
    db_path = str(tmp_path / "test_engfb.db")
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row

    _conn.executescript("""
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY,
        repo_key TEXT, issue_id TEXT, event_type TEXT, source TEXT,
        agent_type TEXT, title TEXT, actor TEXT, summary TEXT,
        payload TEXT, status TEXT, result TEXT,
        runtime TEXT, skill TEXT, exit_code INTEGER,
        duration REAL, agent_logs TEXT,
        started_at TEXT, completed_at TEXT, created_at TEXT,
        structured_output TEXT
    );
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY,
        agent_session_id TEXT DEFAULT '',
        runtime_id INTEGER DEFAULT 0,
        event_seq INTEGER,
        issue_id TEXT DEFAULT '',
        repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT '',
        skill TEXT DEFAULT '',
        prompt TEXT DEFAULT '',
        cwd TEXT DEFAULT '',
        status TEXT DEFAULT 'queued',
        exit_code INTEGER,
        started_at TEXT,
        completed_at TEXT,
        created_at TEXT,
        runtime_session_id TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY,
        credential_id INTEGER DEFAULT 0,
        name TEXT,
        display_name TEXT DEFAULT '',
        status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global',
        owner_id TEXT DEFAULT '',
        host_name TEXT DEFAULT '',
        last_seen_at TEXT
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY,
        device_name TEXT DEFAULT '',
        last_ip TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        scope TEXT DEFAULT 'global',
        owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT ''
    );
    CREATE TABLE devops_agent_session (
        id TEXT PRIMARY KEY,
        repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT '',
        runtime_id INTEGER DEFAULT 0
    );
    """)

    import src.session_manager as _sm
    monkeypatch.setattr(_sm, "db", _conn)
    monkeypatch.setattr(_sm, "db_lock", threading.Lock())

    import src.api.routes.events as _ev
    monkeypatch.setattr(_ev, "db", _conn, raising=False)
    monkeypatch.setattr(_ev, "db_lock", threading.Lock(), raising=False)

    yield _conn
    _conn.close()


def _seed_event(conn, seq, runtime="opencode", status="failed"):
    conn.execute(
        "INSERT INTO events (seq, repo_key, issue_id, event_type, source, title, summary, "
        "actor, payload, agent_type, runtime, skill, status, exit_code, duration, "
        "agent_logs, result, created_at, started_at, completed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (seq, RK, "#285", "a2a_event", "webhook", "t", "s", "",
         "{}", "coding", runtime, "coding-cli", status, 1, 0.0,
         "[]", "", "2026-10-10T10:00:00Z", "2026-10-10T10:00:00Z", "2026-10-10T10:01:00Z"))
    conn.commit()


def _seed_runtime(conn, rid, name="opencode", device_name="master-002", ip="10.0.0.2", cred_id=1079):
    conn.execute(
        "INSERT INTO agent_runtimes (id, credential_id, name, status, healthy) VALUES (?,?,?,?,1)",
        (rid, cred_id, name, "online"))
    conn.execute(
        "INSERT INTO runtime_credentials (id, device_name, last_ip, status) VALUES (?,?,?,?)",
        (cred_id, device_name, ip, "active"))
    conn.commit()


def _seed_task(conn, event_seq, runtime_id=0, status="failed", task_id="task-001"):
    conn.execute(
        "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (task_id, "sess-1", runtime_id, event_seq, status, "2026-10-10T10:00:00Z"))
    conn.commit()


def _seed_session(conn, repo_key, agent_type, runtime_id):
    conn.execute(
        "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id) VALUES (?,?,?,?)",
        ("sess-direct-1", repo_key, agent_type, runtime_id))
    conn.commit()


def _detail(conn, monkeypatch, seq):
    """调用 get_event_detail 并返回结果 dict。"""
    from unittest.mock import patch
    from src.api.routes.events import get_event_detail
    user = {"username": "test", "managed_repos": [RK]}
    with patch("src.api.deps.check_repo_access", return_value=None):
        return get_event_detail(seq, user)


class TestUndispatchedNoFallback:
    """未派发（runtime_id=0 且 failed）→ 不兜底，引擎字段为空。"""

    def test_undispatched_failed_empty_engine_fields(self, test_db):
        """Issue #285 核心场景：runtime_id=0 且 failed → engine_instance/device/ip 为空。"""
        _seed_event(test_db, 5001, runtime="opencode", status="failed")
        _seed_runtime(test_db, 1079, name="opencode", device_name="master-002")
        _seed_task(test_db, 5001, runtime_id=0, status="failed")

        result = _detail(test_db, None, 5001)
        assert result["engine_instance"] == "", \
            f"未派发时 engine_instance 应为空，实际={result['engine_instance']!r}"
        assert result["engine_device"] == ""
        assert result["engine_ip"] == ""
        assert result["engine_health"]["score"] is None

    def test_undispatched_does_not_pick_unrelated_engine(self, test_db):
        """验证未派发不会兜底到无关引擎（如 master-002 id=1079）。"""
        _seed_event(test_db, 5002, runtime="opencode", status="failed")
        _seed_runtime(test_db, 1079, name="opencode", device_name="master-002", ip="10.0.0.2")
        _seed_task(test_db, 5002, runtime_id=0, status="failed")

        result = _detail(test_db, None, 5002)
        assert "master-002" not in (result["engine_instance"] or "")
        assert "master-002" not in (result["engine_device"] or "")
        assert "10.0.0.2" not in (result["engine_ip"] or "")


class TestNormalExecutionShowsEngine:
    """正常执行（runtime_id>0）→ 引擎实例/设备/IP 正确解析。"""

    def test_dispatched_runtime_resolves_engine(self, test_db):
        """runtime_id>0 → 解析到对应引擎实例。"""
        _seed_event(test_db, 5101, runtime="opencode", status="completed")
        _seed_runtime(test_db, 2001, name="opencode", device_name="worker-001", ip="10.0.0.10", cred_id=2001)
        _seed_task(test_db, 5101, runtime_id=2001, status="completed")

        result = _detail(test_db, None, 5101)
        assert result["engine_instance"] != "", "正常执行时 engine_instance 不应为空"
        assert result["engine_device"] == "worker-001"
        assert result["engine_ip"] == "10.0.0.10"

    def test_dispatched_engine_health_shown(self, test_db):
        """runtime_id>0 → 健康分有值（不一定是 None）。"""
        _seed_event(test_db, 5102, runtime="opencode", status="completed")
        _seed_runtime(test_db, 2002, name="opencode", device_name="worker-002", ip="10.0.0.11", cred_id=2002)
        _seed_task(test_db, 5102, runtime_id=2002, status="completed")

        result = _detail(test_db, None, 5102)
        # engine_health 字段存在；score 可能是 None（无健康分数据）但字段不应缺失
        assert "engine_health" in result
        assert "score" in result["engine_health"]


class TestDirectModeShowsEngine:
    """direct 模式（devops_agent_session 绑定）→ 引擎显示不受影响。"""

    def test_direct_session_resolves_engine(self, test_db):
        """无 agent_tasks.runtime_id>0，但有 devops_agent_session 绑定 → 解析到引擎。"""
        _seed_event(test_db, 5201, runtime="opencode", status="completed")
        _seed_runtime(test_db, 3001, name="opencode", device_name="local-host", ip="127.0.0.1", cred_id=3001)
        _seed_task(test_db, 5201, runtime_id=0, status="completed")
        _seed_session(test_db, RK, "coding", 3001)

        result = _detail(test_db, None, 5201)
        assert result["engine_instance"] != "", "direct 模式应解析到引擎"
        assert result["engine_device"] == "local-host"
        assert result["engine_ip"] == "127.0.0.1"


class TestTypeFallbackStillWorksForExecutedTasks:
    """类型兜底对「执行过但记录缺实例」的场景保留。"""

    def test_executed_non_failed_task_gets_fallback(self, test_db):
        """runtime_id=0 且非 failed（如 completed）→ 仍走类型兜底。"""
        _seed_event(test_db, 5301, runtime="opencode", status="completed")
        _seed_runtime(test_db, 4001, name="opencode", device_name="fallback-host", ip="10.0.0.99", cred_id=4001)
        _seed_task(test_db, 5301, runtime_id=0, status="completed")

        result = _detail(test_db, None, 5301)
        # runtime_id=0 且非 failed → 类型兜底仍生效
        assert result["engine_instance"] != "", "非 failed 的 runtime_id=0 应走类型兜底"
        assert "fallback-host" in (result["engine_device"] or "")


class TestEngineHealthGated:
    """引擎健康分门控：仅 runtime_id>0 时展示。"""

    def test_health_null_when_undispatched(self, test_db):
        """未派发 → 健康分 score 为 None。"""
        _seed_event(test_db, 5401, runtime="opencode", status="failed")
        _seed_runtime(test_db, 5001, name="opencode", device_name="master-x")
        _seed_task(test_db, 5401, runtime_id=0, status="failed")

        result = _detail(test_db, None, 5401)
        assert result["engine_health"]["score"] is None
        assert result["engine_health"]["total_tasks"] == 0
