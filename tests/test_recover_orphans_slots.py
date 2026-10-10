"""recover-orphans busy_slots 释放 + 回炉归零修复测试（主人提出的槽位泄漏问题）。

覆盖：
- UT-1 force-fail（attempts 封顶）→ busy_slots 释放（旧代码永久泄漏点）
- UT-2 回炉（requeue 分支）→ busy_slots 释放 + runtime_id 归零 + pool_agent_type 回填
- UT-3 活跃任务跳过 → busy_slots 不动
"""
import os, sys, threading

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import pytest


@pytest.fixture()
def setup_db(monkeypatch):
    import sqlite3
    from src.runtime import service as svc
    import src.api.routes.runtime as rt

    test_db = sqlite3.connect(":memory:", check_same_thread=False)
    test_db.executescript("""
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY, agent_session_id TEXT DEFAULT '', runtime_id INTEGER DEFAULT 0,
        event_seq INTEGER, issue_id TEXT DEFAULT '', repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT '', skill TEXT DEFAULT '', skill_id INTEGER DEFAULT 0,
        prompt TEXT DEFAULT '', cwd TEXT DEFAULT '', runtime_session_id TEXT DEFAULT '',
        event_type TEXT DEFAULT '', account TEXT DEFAULT '', user_id INTEGER DEFAULT 0,
        repo_gitcode_token TEXT DEFAULT '', env_extra TEXT DEFAULT '{}',
        skill_files TEXT DEFAULT '{}', server_url TEXT DEFAULT '',
        issue_session_id TEXT DEFAULT '',
        attempts INTEGER DEFAULT 0, pool_agent_type TEXT DEFAULT '',
        affinity_runtime_id INTEGER DEFAULT 0, pool_tag TEXT DEFAULT '',
        pool_engine_ids TEXT DEFAULT '',
        status TEXT DEFAULT 'queued', stream_ended INTEGER DEFAULT 0,
        exit_code INTEGER, result_text TEXT DEFAULT '', errors TEXT DEFAULT '',
        started_at TEXT, completed_at TEXT, created_at TEXT
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 0, busy_slots INTEGER DEFAULT 0, max_concurrent INTEGER DEFAULT 4
    );
    CREATE TABLE task_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, created_at TEXT, content TEXT
    );
    """)
    monkeypatch.setattr(svc, "db", test_db)
    monkeypatch.setattr(svc, "db_lock", threading.Lock())
    # runtime.py 路由模块从 session_manager 导入 db/db_lock —— 一并替换
    monkeypatch.setattr(rt, "db", test_db)
    monkeypatch.setattr(rt, "db_lock", svc.db_lock)
    import src.session_manager as _sm
    monkeypatch.setattr(_sm, "db", test_db)
    monkeypatch.setattr(_sm, "db_lock", svc.db_lock)
    yield test_db
    test_db.close()


def _seed(c, tid, status, runtime_id, attempts=0, agent_type="coding", messages_ago=None):
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat()
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, attempts, agent_type, "
              "pool_agent_type, agent_session_id, created_at, started_at) "
              "VALUES (?,?,?,?,?,'',?, ?, ?)",
              (tid, status, runtime_id, attempts, agent_type, "s-" + tid, created, created))
    if messages_ago is not None:
        ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=messages_ago)).isoformat()
        c.execute("INSERT INTO task_messages (task_id, created_at, content) VALUES (?,?,?)",
                  (tid, ts, "alive"))


def test_ut1_force_fail_releases_busy_slots(setup_db):
    """UT-1：attempts 封顶 force-fail → busy_slots 释放（旧代码永久泄漏）。"""
    from src.api.routes.runtime import runtime_recover_orphans
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
              "VALUES (501, 'codex', 'online', 1, 2, 4)")
    _seed(c, "t_orp1", "dispatched", 501, attempts=2)   # +1 = 3 → 封顶 force-fail
    setup_db.commit()

    runtime_recover_orphans(501, credential_id=0)

    row = c.execute("SELECT status, attempts FROM agent_tasks WHERE id='t_orp1'").fetchone()
    assert row[0] == "failed", "attempts 封顶应 force-fail"
    busy = c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=501").fetchone()[0]
    assert busy == 1, f"force-fail 应释放槽位 2→1，实际 {busy}"


def test_ut2_requeue_releases_and_zeroes_runtime(setup_db):
    """UT-2：回炉 → 槽位释放 + runtime_id 归零 + pool_agent_type 回填。"""
    from src.api.routes.runtime import runtime_recover_orphans
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
              "VALUES (502, 'codex', 'online', 1, 3, 4)")
    _seed(c, "t_orp2", "running", 502, attempts=0, agent_type="qa")
    setup_db.commit()

    runtime_recover_orphans(502, credential_id=0)

    row = c.execute("SELECT status, runtime_id, pool_agent_type FROM agent_tasks WHERE id='t_orp2'").fetchone()
    assert row[0] == "queued", "应回炉 queued"
    assert row[1] == 0, f"runtime_id 应归零（引擎删重建防死定向），实际 {row[1]}"
    assert row[2] == "qa", f"pool_agent_type 应回填 qa（广播可再领），实际 {row[2]}"
    busy = c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=502").fetchone()[0]
    assert busy == 2, f"回炉应释放槽位 3→2，实际 {busy}"


def test_ut3_active_task_keeps_busy_slots(setup_db):
    """UT-3：活跃任务（近期有消息）跳过 → 状态/槽位都不动。"""
    from src.api.routes.runtime import runtime_recover_orphans
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
              "VALUES (503, 'codex', 'online', 1, 2, 4)")
    _seed(c, "t_orp3", "running", 503, attempts=0, messages_ago=30)  # 30 秒前有消息 = 活跃
    setup_db.commit()

    runtime_recover_orphans(503, credential_id=0)

    row = c.execute("SELECT status, runtime_id FROM agent_tasks WHERE id='t_orp3'").fetchone()
    assert row[0] == "running" and row[1] == 503, "活跃任务应完全跳过"
    busy = c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=503").fetchone()[0]
    assert busy == 2, f"活跃任务槽位不动，实际 {busy}"
