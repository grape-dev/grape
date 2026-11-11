"""Issue #699 Phase 3 验收测试（TC-3.1 ~ TC-3.5）。

设计依据：docs/weekly-09/grape-unified-implementation-design.md §6 + §Phase 3 验证用例。
- TC-3.1 direct_executor.py 包含 _execute_agent 系列（源码 + 可导入 + 池提交行为）
- TC-3.2 event_pipeline.py 无 start_direct_agent 调用、分派统一 _enqueue_pipeline_task
- TC-3.3 local_claimer import 路径指向 direct_executor
- TC-3.4 enqueue(queued) → local_claimer 确定性认领 → _execute_claimed_locally 直连执行通道
- TC-3.5 _DIRECT_FUTURES 取消（cancel_direct_task 消费 direct_executor 的 Future 表）
"""
import asyncio
import os
import sys
from concurrent.futures import Future
from datetime import datetime, timezone

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "src")
DE_PATH = os.path.join(SRC, "domains", "runtime", "engine", "direct_executor.py")
EP_PATH = os.path.join(SRC, "domains", "runtime", "pipeline", "event_pipeline.py")
LC_PATH = os.path.join(SRC, "domains", "runtime", "engine", "local_claimer.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# TC-3.1：direct_executor.py 创建验证
# ===========================================================================

class TestTC31DirectExecutor:
    def test_file_contains_migrated_symbols(self):
        """TC-3.1: _DIRECT_POOL/_DIRECT_FUTURES/_execute_agent/_execute_agent_inner/start_direct_agent 均已迁移。"""
        src = _read(DE_PATH)
        for sym in ("_DIRECT_POOL", "_DIRECT_FUTURES", "_DIRECT_FUTURES_LOCK",
                    "def start_direct_agent", "def _execute_agent", "def _execute_agent_inner"):
            assert sym in src, f"{sym} missing in direct_executor.py"

    def test_importable_and_shared_state(self):
        """TC-3.1: 模块可导入；event_pipeline 与 direct_executor 共享同一份 _DIRECT_FUTURES。"""
        import src.domains.runtime.engine.direct_executor as de
        import src.domains.runtime.pipeline.event_pipeline as ep
        assert callable(de.start_direct_agent)
        assert callable(de._execute_agent)
        assert callable(de._execute_agent_inner)
        assert ep._DIRECT_FUTURES is de._DIRECT_FUTURES
        assert ep._DIRECT_FUTURES_LOCK is de._DIRECT_FUTURES_LOCK

    def test_start_direct_agent_registers_future(self):
        """TC-3.1: start_direct_agent 提交有界池并按 seq 登记 Future（供 sweeper 取消）。"""
        import src.domains.runtime.engine.direct_executor as de

        ran = []

        def _fn(repo_key, seq, *args):
            ran.append((repo_key, seq))

        fut = de.start_direct_agent(_fn, ("gitcode:o/r", 4321), name="tc31")
        assert de._DIRECT_FUTURES.get(4321) is fut
        fut.result(timeout=10)
        assert ran == [("gitcode:o/r", 4321)]
        # _execute_agent finally 会清理；此处同步清掉避免污染共享表
        de._DIRECT_FUTURES.pop(4321, None)


# ===========================================================================
# TC-3.2：event_pipeline.py direct 调用移除验证
# ===========================================================================

class TestTC32NoDirectCalls:
    def test_no_start_direct_agent_references(self):
        """TC-3.2: event_pipeline.py 中不再出现 start_direct_agent（调用/导入均无）。"""
        src = _read(EP_PATH)
        assert "start_direct_agent" not in src, "event_pipeline.py still references start_direct_agent"

    def test_dispatch_sites_use_enqueue(self):
        """TC-3.2: 5 个原 direct 分派函数均收敛到 _enqueue_pipeline_task（入队 enqueue_task）。"""
        src = _read(EP_PATH)
        for fn in ("def _dispatch_pr_subscription_extra",
                   "def _dispatch_pr_subscription_redispatch",
                   "def _dispatch_subscription_agent",
                   "def _redispatch_vod_after_recommend",
                   "def run_pipeline"):
            i = src.find(fn)
            assert i > 0, f"{fn} not found"
            # 函数体内（到下一个顶层 def 为止）应包含 _enqueue_pipeline_task 调用
            nxt = src.find("\ndef ", i + 10)
            body = src[i:nxt if nxt > 0 else len(src)]
            assert "_enqueue_pipeline_task(" in body, f"{fn} does not enqueue"
        # 入队最终走 service.enqueue_task（5 处分派共用同一入口）
        assert src.count("_enqueue_pipeline_task(") >= 6  # 5 个分派点 + 定义引用

    def test_run_pipeline_dispatch_not_create_direct_task(self):
        """TC-3.2: run_pipeline 主分派段不再 _create_direct_task（queued 任务由 claim 创建语义取代）。"""
        src = _read(EP_PATH)
        i = src.find("Issue #372 §12.7")
        assert i > 0
        j = src.find("dispatch exception", i)
        assert j > i
        seg = src[i:j]
        assert "_create_direct_task" not in seg


# ===========================================================================
# TC-3.3：local_claimer import 路径验证
# ===========================================================================

class TestTC33LocalClaimerImport:
    def test_imports_from_direct_executor(self):
        """TC-3.3: local_claimer 从 direct_executor 导入 _execute_agent/start_direct_agent。"""
        src = _read(LC_PATH)
        assert "from src.domains.runtime.engine.direct_executor import _execute_agent, start_direct_agent" in src
        assert "from src.domains.runtime.pipeline.event_pipeline import _execute_agent" not in src


# ===========================================================================
# TC-3.4：webhook → enqueue → claim → execute 链路验证（内存 sqlite）
# ===========================================================================

@pytest.fixture()
def phase3_env(monkeypatch):
    """内存 sqlite：一台本机 direct 引擎（246）+ events/agent_tasks 基础表。"""
    import sqlite3
    from src.kernel.session_manager import db as _db_mod  # noqa: F401

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'online',
        healthy INTEGER DEFAULT 1, credential_id INTEGER DEFAULT 0,
        scope TEXT DEFAULT 'global', busy_slots INTEGER DEFAULT 0,
        max_concurrent INTEGER DEFAULT 20, last_seen_at TEXT DEFAULT '',
        probe_status TEXT DEFAULT 'ok', display_name TEXT DEFAULT '', org_id INTEGER DEFAULT 0
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
        issue_session_id TEXT DEFAULT '',
        event_type TEXT DEFAULT '', account TEXT DEFAULT '', user_id INTEGER DEFAULT 0,
        repo_gitcode_token TEXT DEFAULT '', user_gitcode_token TEXT DEFAULT '',
        env_extra TEXT DEFAULT '{}', skill_files TEXT DEFAULT '{}', server_url TEXT DEFAULT '',
        project_info_files TEXT DEFAULT '{}', project_info_meta TEXT DEFAULT '{}',
        knowledge_files TEXT DEFAULT '{}', project_info_file_urls TEXT DEFAULT '[]',
        knowledge_file_urls TEXT DEFAULT '[]', attempts INTEGER DEFAULT 0,
        pool_agent_type TEXT DEFAULT '', affinity_runtime_id INTEGER DEFAULT 0,
        pool_tag TEXT DEFAULT '', pool_engine_ids TEXT DEFAULT '',
        pool_engine_source TEXT DEFAULT '', status TEXT DEFAULT 'queued',
        stream_ended INTEGER DEFAULT 0, self_reported INTEGER DEFAULT 0,
        exit_code INTEGER, result_text TEXT DEFAULT '', errors TEXT DEFAULT '',
        stderr TEXT DEFAULT '', usage_json TEXT DEFAULT '{}',
        started_at TEXT, completed_at TEXT, created_at TEXT,
        skill_id INTEGER DEFAULT 0
    );
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY, repo_key TEXT, issue_id TEXT, event_type TEXT,
        source TEXT, actor TEXT, payload TEXT DEFAULT '{}', title TEXT DEFAULT '',
        agent_type TEXT, runtime TEXT, skill TEXT, status TEXT DEFAULT 'pending',
        result TEXT DEFAULT '', created_at TEXT, completed_at TEXT DEFAULT ''
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
    CREATE TABLE task_messages (task_id TEXT, created_at TEXT);
    CREATE TABLE unified_user_mapping (user_id INTEGER, gitcode_user TEXT, feishu_open_id TEXT DEFAULT '');
    """)
    cur.execute("INSERT INTO runtime_health_scores (runtime_id) VALUES (246)")
    cur.execute("INSERT INTO agent_runtimes (id, name, credential_id, scope, status, healthy, last_seen_at) "
                "VALUES (246, 'opencode', 0, 'global', 'online', 1, ?)",
                (datetime.now(timezone.utc).isoformat(),))
    cur.execute("INSERT INTO engine_capabilities (runtime_id, tag) VALUES (246, 'all')")
    conn.commit()

    import src.kernel.session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=False)
    import src.domains.runtime.engine.service as svc
    monkeypatch.setattr(svc, "db", conn, raising=False)
    yield conn
    conn.close()


def _insert_task(cur, tid, runtime_id, seq, repo="gitcode:o/r", pool_agent_type="", pool_tag=""):
    cur.execute(
        "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, repo_key, "
        "agent_type, skill, prompt, cwd, status, created_at, pool_agent_type, pool_tag) "
        "VALUES (?, 'sess1', ?, ?, ?, 'qa', 'qa-skill', 'p', 'w', 'queued', ?, ?, ?)",
        (tid, runtime_id, seq, repo, datetime.now(timezone.utc).isoformat(),
         pool_agent_type, pool_tag))
    cur.connection.commit()


class TestTC34EnqueueClaimExecute:
    def test_deterministic_claim_binds_local_engine(self, phase3_env):
        """TC-3.4: 入队任务（queued, runtime_id=本机引擎）→ claim(rid, deterministic_only=True) 认领。"""
        from src.domains.runtime.engine.service import claim
        cur = phase3_env.cursor()
        _insert_task(cur, "t_tc34a", 246, 9001)
        cur.execute("INSERT INTO events (seq, repo_key, status, agent_type) VALUES (9001, 'gitcode:o/r', 'pending', 'qa')")
        phase3_env.commit()
        task = claim(246, deterministic_only=True)
        assert task and task["id"] == "t_tc34a"
        cur.execute("SELECT status, runtime_id FROM agent_tasks WHERE id='t_tc34a'")
        assert cur.fetchone() == ("running", 246)
        # 事件随认领置 running（方案 E）
        cur.execute("SELECT status FROM events WHERE seq=9001")
        assert cur.fetchone()[0] == "running"

    def test_claim_by_tag_does_not_steal_deterministic(self, phase3_env):
        """TC-3.4: 确定性任务（runtime_id=246≠0）不属于广播池——claim_by_tag 不认领。"""
        from src.domains.runtime.engine.service import claim_by_tag
        cur = phase3_env.cursor()
        _insert_task(cur, "t_tc34b", 246, 9002)
        assert claim_by_tag("all", 246) is None

    def test_deterministic_only_skips_legacy_pool(self, phase3_env):
        """TC-3.4: deterministic_only=True 只吃定向任务，不捞 legacy 池任务（池走 claim_by_tag）。"""
        from src.domains.runtime.engine.service import claim
        cur = phase3_env.cursor()
        _insert_task(cur, "t_tc34c", 0, 9003, pool_agent_type="qa")
        assert claim(246, deterministic_only=True) is None
        cur.execute("SELECT status FROM agent_tasks WHERE id='t_tc34c'")
        assert cur.fetchone()[0] == "queued"

    def test_claim_default_behavior_unchanged(self, phase3_env):
        """TC-3.4 回归：默认 claim（deterministic_only=False）仍可捞 legacy 池任务。"""
        from src.domains.runtime.engine.service import claim
        cur = phase3_env.cursor()
        _insert_task(cur, "t_tc34d", 0, 9004, pool_agent_type="qa")
        task = claim(246)
        assert task and task["id"] == "t_tc34d"

    def test_claimer_loop_deterministic_step(self, phase3_env, monkeypatch):
        """TC-3.4: local_claimer 的确定性认领步骤（_claim_deterministic_locally）返回本机绑定任务。"""
        from src.domains.runtime.engine import local_claimer as lc
        cur = phase3_env.cursor()
        _insert_task(cur, "t_tc34e", 246, 9005)
        task = asyncio.run(lc._claim_deterministic_locally(246))
        assert task and task["id"] == "t_tc34e"

    def test_execute_claimed_locally_uses_direct_executor(self, phase3_env, monkeypatch):
        """TC-3.4: 认领后 _execute_claimed_locally 走 direct_executor 的 start_direct_agent 通道。"""
        import src.domains.runtime.engine.direct_executor as de
        from src.domains.runtime.engine import local_claimer as lc
        cur = phase3_env.cursor()
        _insert_task(cur, "t_tc34f", 246, 9006)
        task = {"id": "t_tc34f", "repo_key": "gitcode:o/r", "event_seq": 9006,
                "agent_type": "qa", "skill": "qa-skill"}
        captured = {}

        def _fake_start(fn, args, name=""):
            captured["fn"] = fn
            captured["args"] = args
            return Future()

        monkeypatch.setattr(de, "start_direct_agent", _fake_start)
        asyncio.run(lc._execute_claimed_locally(task, 246))
        assert captured["fn"] is de._execute_agent
        args = captured["args"]
        assert args[0] == "gitcode:o/r" and args[1] == 9006 and args[2] == "qa"
        assert args[6] == 246 and args[7] == "t_tc34f"


# ===========================================================================
# TC-3.5：_DIRECT_FUTURES 取消验证
# ===========================================================================

class TestTC35FutureCancel:
    def test_cancel_direct_task_cancels_queued_future(self):
        """TC-3.5: cancel_direct_task 取消 direct_executor._DIRECT_FUTURES 中排队的 Future。"""
        import src.domains.runtime.engine.direct_executor as de
        import src.domains.runtime.pipeline.event_pipeline as ep

        seq = 987654
        fut = Future()
        de._DIRECT_FUTURES[seq] = fut
        try:
            assert ep.cancel_direct_task(seq) is True
            assert fut.cancelled() is True
            assert seq not in de._DIRECT_FUTURES
        finally:
            de._DIRECT_FUTURES.pop(seq, None)

    def test_sweeper_cancel_chain_points_to_direct_executor(self):
        """TC-3.5: sweeper 取消链（cleanup → cancel_direct_task → direct_executor._DIRECT_FUTURES）。"""
        cleanup_src = _read(os.path.join(SRC, "domains", "runtime", "engine", "cleanup.py"))
        assert "cancel_direct_task" in cleanup_src
        ep_src = _read(EP_PATH)
        assert "from src.domains.runtime.engine.direct_executor import" in ep_src
        assert "_DIRECT_FUTURES" in ep_src
