"""recover-orphans busy_slots 释放 + 回炉归零修复测试（主人提出的槽位泄漏问题）。

覆盖：
- UT-1 force-fail（attempts 封顶）→ busy_slots 释放（旧代码永久泄漏点）
- UT-2 回炉（requeue 分支）→ busy_slots 释放 + runtime_id 归零 + pool_agent_type 回填
- UT-3 活跃任务跳过 → busy_slots 不动
- UT-4 新任务（< 宽限期）→ skipped_young，attempts/槽位不动（Issue #370）
- UT-5 started_at NULL 兜底 created_at → 仍 skipped_young（Issue #370）
- UT-6 RECOVER_GRACE_SECONDS 环境变量覆盖默认 300s（Issue #370）
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


def _seed(c, tid, status, runtime_id, attempts=0, agent_type="coding",
          messages_ago=None, started_seconds_ago=600, created_seconds_ago=600,
          started_at=None):
    """插入一条 agent_tasks。

    started_seconds_ago/created_seconds_ago：started_at/created_at 距 now 的秒数（默认 600s，
    大于默认宽限期 300s，使现有 UT-1/UT-2/UT-3 不受年龄闸门影响）。
    started_at：显式覆盖（None 表示按 started_seconds_ago 生成；传字符串则直接写入，
    传 "NULL" 字符串则写 NULL 模拟存量数据）。
    """
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    created = (now - dt.timedelta(seconds=created_seconds_ago)).isoformat()
    if started_at == "NULL":
        started_val = None
    elif isinstance(started_at, str):
        started_val = started_at
    else:
        started_val = (now - dt.timedelta(seconds=started_seconds_ago)).isoformat()
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, attempts, agent_type, "
              "pool_agent_type, agent_session_id, created_at, started_at) "
              "VALUES (?,?,?,?,?,'',?, ?, ?)",
              (tid, status, runtime_id, attempts, agent_type, "s-" + tid, created, started_val))
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


def test_ut4_young_task_skipped(setup_db):
    """UT-4（Issue #370）：新任务（< 宽限期）→ skipped_young，attempts/状态/槽位不动。"""
    from src.api.routes.runtime import runtime_recover_orphans
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
              "VALUES (504, 'codex', 'online', 1, 1, 4)")
    # started_at = 30s ago，created_at = 40s ago（均 < 300s 默认宽限期）
    _seed(c, "t_young1", "dispatched", 504, attempts=0,
          started_seconds_ago=30, created_seconds_ago=40)
    setup_db.commit()

    resp = runtime_recover_orphans(504, credential_id=0)

    assert resp["skipped_young"] == ["t_young1"], \
        f"新任务应进 skipped_young，实际 {resp.get('skipped_young')}"
    assert resp["recovered"] == [] and resp["force_failed"] == []
    row = c.execute("SELECT status, attempts FROM agent_tasks WHERE id='t_young1'").fetchone()
    assert row[0] == "dispatched", "新任务应保持原状态（不回炉）"
    assert row[1] == 0, "attempts 不变"
    busy = c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=504").fetchone()[0]
    assert busy == 1, "新任务 skipped，槽位不动"


def test_ut5_started_at_null_falls_back_to_created_at(setup_db):
    """UT-5（Issue #370）：started_at 为 NULL → 兜底 created_at；created_at < grace → skipped_young。"""
    from src.api.routes.runtime import runtime_recover_orphans
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
              "VALUES (505, 'codex', 'online', 1, 1, 4)")
    # started_at = NULL，created_at = 30s ago（< 300s 宽限期 → 兜底后仍判为新任务）
    _seed(c, "t_young2", "running", 505, attempts=0,
          started_at="NULL", created_seconds_ago=30)
    setup_db.commit()

    resp = runtime_recover_orphans(505, credential_id=0)

    assert resp["skipped_young"] == ["t_young2"], \
        f"started_at NULL 应兜底 created_at 判为新任务，实际 {resp.get('skipped_young')}"
    row = c.execute("SELECT status, attempts FROM agent_tasks WHERE id='t_young2'").fetchone()
    assert row[0] == "running" and row[1] == 0, "兜底 skipped 后状态/attempts 不变"
    busy = c.execute("SELECT busy_slots FROM agent_runtimes WHERE id=505").fetchone()[0]
    assert busy == 1, "槽位不动"


def test_ut6_recover_grace_seconds_env_override(setup_db, monkeypatch):
    """UT-6（Issue #370）：RECOVER_GRACE_SECONDS 环境变量覆盖默认 300s。

    场景 A：grace=60s，started_at=30s ago（< 60s）→ 仍 skipped_young。
    场景 B：grace=60s，started_at=90s ago（> 60s）→ 超龄，无消息 → 回炉。
    """
    from src.api.routes.runtime import runtime_recover_orphans
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots, max_concurrent) "
              "VALUES (506, 'codex', 'online', 1, 2, 4)")
    # 场景 A：30s ago < 60s grace → skipped_young
    _seed(c, "t_grace_a", "dispatched", 506, attempts=0, started_seconds_ago=30)
    # 场景 B：90s ago > 60s grace → 超龄 → 无消息 → 回炉
    _seed(c, "t_grace_b", "dispatched", 506, attempts=0, started_seconds_ago=90)
    setup_db.commit()

    monkeypatch.setenv("RECOVER_GRACE_SECONDS", "60")
    resp = runtime_recover_orphans(506, credential_id=0)

    assert "t_grace_a" in resp["skipped_young"], \
        f"grace=60s 时 30s 前任务应 skipped_young，实际 {resp.get('skipped_young')}"
    assert "t_grace_b" in resp["recovered"], \
        f"grace=60s 时 90s 前任务应超龄回炉，实际 recovered={resp.get('recovered')}"
    # 场景 A 状态/attempts 不变
    row_a = c.execute("SELECT status, attempts FROM agent_tasks WHERE id='t_grace_a'").fetchone()
    assert row_a[0] == "dispatched" and row_a[1] == 0
    # 场景 B 回炉 attempts+1=1
    row_b = c.execute("SELECT status, attempts FROM agent_tasks WHERE id='t_grace_b'").fetchone()
    assert row_b[0] == "queued" and row_b[1] == 1
