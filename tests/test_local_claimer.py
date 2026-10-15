"""Local Claimer 单元测试（local-claimer-first-class-broadcast-design.md §5 UT-01~07）。

设计要点：本机 direct 引擎通过与远端 worker 同一条 claim_by_tag 检查链参与广播
——白名单/健康分/满载/scope 全部同约束。测试用内存 sqlite 隔离，monkeypatch
health_scorer / engine_select 控制健康状态。
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def claimer_env(tmp_path, monkeypatch):
    """内存 sqlite + 基础表数据（两台本机引擎 + 一台远端 worker 引擎）。"""
    import sqlite3
    from src.session_manager import db as _db_mod

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
    # 健康分种子：默认全 100（UT-04/UT-05 用 monkeypatch 单独拉低）
    for rid in (246, 247, 15249):
        cur.execute("INSERT INTO runtime_health_scores (runtime_id) VALUES (?)", (rid,))
    # 引擎：246/247 本机 direct（cred=0/global）；15249 远端 worker（cred=330/repo-scope）
    cur.execute("INSERT INTO runtime_credentials (id, scope, owner_id, owner_name) VALUES (330, 'repo', 'gitcode:o/r', 'owner')")
    for rid, name, cred in [(246, "opencode", 0), (247, "codearts", 0), (15249, "hwcloud", 330)]:
        cur.execute("INSERT INTO agent_runtimes (id, name, credential_id, scope, status, healthy, last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (rid, name, cred, "repo" if cred else "global", "online", 1,
                     datetime.now(timezone.utc).isoformat()))
        cur.execute("INSERT INTO engine_capabilities (runtime_id, tag) VALUES (?, 'all')", (rid,))
    conn.commit()

    # monkeypatch session_manager 的 db 为内存库
    import src.session_manager as sm
    monkeypatch.setattr(sm, "db", conn, raising=False)
    import src.runtime.service as svc
    monkeypatch.setattr(svc, "db", conn, raising=False)
    # service 的 db_lock 直接沿用（RLock 可重入）
    yield conn
    conn.close()


def _enqueue(cur, tid, pool_engine_ids="", source="", tag="vod", repo="gitcode:o/r", created_off=0):
    created = (datetime.now(timezone.utc) - timedelta(seconds=created_off)).isoformat()
    cur.execute(
        "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, repo_key, agent_type, "
        "pool_tag, pool_engine_ids, pool_engine_source, status, created_at, prompt, cwd) "
        "VALUES (?,?,0,1,?,?,?,?,?, 'queued', ?, 'p', 'w')",
        (tid, "sess1", repo, tag, tag, pool_engine_ids, source, created))
    cur.connection.commit()


class TestLocalClaimer:
    def test_ut01_whitelist_isolation(self, claimer_env, monkeypatch):
        """UT-01 白名单隔离（事故场景回归）：白名单 [15249]（远端健康）时本机 246 抢不到。"""
        from src.runtime import local_claimer as lc
        from src.runtime.service import claim_by_tag
        cur = claimer_env.cursor()
        _enqueue(cur, "t_ut01", pool_engine_ids=json.dumps([15249]), source="scene")
        # 本机引擎发现应包含 246/247
        ids = lc._local_direct_engine_ids()
        assert 246 in ids and 247 in ids and 15249 not in ids
        # 本机 246 尝试认领 → 白名单拦下
        task = claim_by_tag("all", 246)
        assert task is None
        # 远端 15249 能领到
        task = claim_by_tag("all", 15249)
        assert task and task["id"] == "t_ut01"

    def test_ut02_local_in_whitelist(self, claimer_env):
        """UT-02 本机在白名单：正常认领，状态流转 + busy_slots 正确。"""
        from src.runtime.service import claim_by_tag
        cur = claimer_env.cursor()
        _enqueue(cur, "t_ut02", pool_engine_ids=json.dumps([246]), source="scene")
        task = claim_by_tag("all", 246)
        assert task and task["id"] == "t_ut02"
        cur.execute("SELECT status, runtime_id FROM agent_tasks WHERE id='t_ut02'")
        assert cur.fetchone() == ("running", 246)
        cur.execute("SELECT busy_slots FROM agent_runtimes WHERE id=246")
        assert cur.fetchone()[0] == 1

    def test_ut03_busy_skip(self, claimer_env):
        """UT-03 满载不抢：busy_slots >= max_concurrent 时 _engine_busy 返回 True。"""
        from src.runtime import local_claimer as lc
        cur = claimer_env.cursor()
        cur.execute("UPDATE agent_runtimes SET busy_slots=20, max_concurrent=20 WHERE id=246")
        claimer_env.commit()
        assert lc._engine_busy(246) is True
        cur.execute("UPDATE agent_runtimes SET busy_slots=1, max_concurrent=20 WHERE id=246")
        claimer_env.commit()
        assert lc._engine_busy(246) is False

    def test_ut04_unhealthy_blocked(self, claimer_env, monkeypatch):
        """UT-04 低分不抢：health_scorer 判不健康时本机引擎 claim 返回 None（与远端同约束）。"""
        from src.runtime.service import claim_by_tag
        cur = claimer_env.cursor()
        _enqueue(cur, "t_ut04", pool_engine_ids=json.dumps([246]), source="scene")
        import src.runtime.health_scorer as hs
        monkeypatch.setattr(hs, "is_healthy", lambda rid: False)
        task = claim_by_tag("all", 246)
        assert task is None

    def test_ut05_degrade_by_source(self, claimer_env, monkeypatch):
        """UT-05 降级-source 区分：auto 全灭 → 清白名单重入队（degraded）；scene → failed。"""
        from src.runtime import service as svc
        cur = claimer_env.cursor()
        # 健康全灭：把引擎拉离线 + 健康分 0
        cur.execute("UPDATE agent_runtimes SET status='offline', healthy=0 WHERE id=15249")
        claimer_env.commit()
        import src.runtime.health_scorer as hs
        monkeypatch.setattr(hs, "is_healthy", lambda rid: False)

        # auto 任务：超时后降级放行
        _enqueue(cur, "t_ut05a", pool_engine_ids=json.dumps([15249]), source="auto", created_off=300)
        # scene 任务：超时后 failed
        _enqueue(cur, "t_ut05b", pool_engine_ids=json.dumps([15249]), source="scene", created_off=300)
        svc._check_stale_queued_tasks()
        cur.execute("SELECT pool_engine_ids, pool_engine_source, status FROM agent_tasks WHERE id='t_ut05a'")
        pe, psrc, st = cur.fetchone()
        assert pe == "" and psrc == "degraded" and st == "queued", "auto 应降级重入队"
        cur.execute("SELECT status FROM agent_tasks WHERE id='t_ut05b'")
        assert cur.fetchone()[0] == "failed", "scene 应保持 #253 失败"

    def test_ut05b_degrade_only_once(self, claimer_env, monkeypatch):
        """UT-05b 降级只做一次：degraded 任务不再被重复降级/失败（pool_engine_ids 已空不进扫描）。"""
        from src.runtime import service as svc
        cur = claimer_env.cursor()
        _enqueue(cur, "t_ut05c", pool_engine_ids="", source="degraded", created_off=600)
        svc._check_stale_queued_tasks()
        cur.execute("SELECT status, pool_engine_source FROM agent_tasks WHERE id='t_ut05c'")
        st, psrc = cur.fetchone()
        assert st == "queued" and psrc == "degraded"

    def test_ut06_dual_master_race(self, claimer_env):
        """UT-06 双主竞争：同一 task 只能被认领一次（UPDATE 原子性）。

        注意 claim_by_tag 的 skip-loop 语义：不匹配白名单时会继续尝试下一个任务，
        所以"第二认领者"可能拿到的是队列里的其他任务——原子性验证点在于
        同一个 task_id 不会被认领两次。
        """
        from src.runtime.service import claim_by_tag
        cur = claimer_env.cursor()
        # 只放一个任务（白名单含双机）→ 双机先后认领，同一 task 只成功一次
        _enqueue(cur, "t_ut06", pool_engine_ids=json.dumps([246, 247]), source="scene")
        t1 = claim_by_tag("all", 246)
        t2 = claim_by_tag("all", 247)
        claimed = [t for t in (t1, t2) if t]
        assert len(claimed) == 1, f"同一 task 只能被认领一次, got {[t['id'] for t in claimed]}"
        assert claimed[0]["id"] == "t_ut06"
        cur.execute("SELECT status, runtime_id FROM agent_tasks WHERE id='t_ut06'")
        assert cur.fetchone() == ("running", claimed[0].get("runtime_id") or 0)
        # 已认领的 task 再来第三者也领不到（queued 不存在了）
        t3 = claim_by_tag("all", 246)
        assert t3 is None

    def test_ut07_evt_rebuild(self, claimer_env):
        """UT-07 evt 重建：events 表按 seq 还原 payload/type/actor（供 direct 执行 prompt 重建）。"""
        from src.runtime import local_claimer as lc
        cur = claimer_env.cursor()
        cur.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, source, actor, payload, title) "
            "VALUES (5, 'gitcode:o/r', '#7', 'issue_opened', 'gitcode', 'alice', "
            "'{\"object_kind\":\"issue\",\"issue_number\":7}', 'title')")
        claimer_env.commit()
        task = {"repo_key": "gitcode:o/r", "issue_id": "#7", "event_seq": 5,
                "event_type": "issue_opened", "account": "o/r"}
        evt = lc._rebuild_evt_from_task(task)
        assert evt["type"] == "issue_opened"
        assert evt["actor"] == "alice"
        assert evt["payload"]["issue_number"] == 7
        assert evt["repository"] == "o/r"

    def test_switch_disabled(self, claimer_env, monkeypatch):
        """开关：GRAPE_LOCAL_CLAIMER=0 时 start_local_claimer 不启动。"""
        monkeypatch.setenv("GRAPE_LOCAL_CLAIMER", "0")
        import importlib
        from src.runtime import local_claimer as lc
        importlib.reload(lc)
        assert lc.CLAIMER_ENABLED is False
        async def _run():
            return lc.start_local_claimer()
        assert asyncio.run(_run()) is None
