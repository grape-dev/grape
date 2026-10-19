"""Issue #369 测试：任务判死路径完整回写关联事件诊断字段。

覆盖（对应 Spec 验收标准）：
- UT-A 认领判死（_fail_task_pool_unreachable）→ 事件 result/exit_code/completed_at 非空
- UT-B 健康分不足超时判死（_fail_task_pool_unhealthy）→ 同上
- UT-C 回收超限判死（attempts>=50 dispatched 回收）→ 事件完整回写
- UT-D 幂等/终态保护：事件已 completed 时不被 failed 覆盖（双主 scp 同步两侧重复回写安全）
- UT-E 事件已 failed 时重复回写不报错、字段保持（幂等重放）
"""
import os, sys, json, threading

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import pytest


@pytest.fixture()
def setup_db(monkeypatch, tmp_path):
    import sqlite3
    from src.runtime import service as svc

    test_db = sqlite3.connect(":memory:", check_same_thread=False)
    test_db.execute("PRAGMA foreign_keys=OFF")
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
        project_info_files TEXT DEFAULT '{}', project_info_meta TEXT DEFAULT '{}',
        knowledge_files TEXT DEFAULT '{}', project_info_file_urls TEXT DEFAULT '{}',
        knowledge_file_urls TEXT DEFAULT '{}', attempts INTEGER DEFAULT 0,
        pool_agent_type TEXT DEFAULT '', affinity_runtime_id INTEGER DEFAULT 0,
        pool_tag TEXT DEFAULT '', pool_engine_ids TEXT DEFAULT '',
        status TEXT DEFAULT 'queued', stream_ended INTEGER DEFAULT 0, self_reported INTEGER DEFAULT 0,
        exit_code INTEGER, result_text TEXT DEFAULT '', errors TEXT DEFAULT '', stderr TEXT DEFAULT '',
        usage_json TEXT DEFAULT '{}', started_at TEXT, completed_at TEXT, created_at TEXT,
        user_gitcode_token TEXT DEFAULT ''
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 0, installed INTEGER DEFAULT 0, credential_id INTEGER DEFAULT 0,
        last_seen_at TEXT, scope TEXT DEFAULT '', busy_slots INTEGER DEFAULT 0,
        max_concurrent INTEGER DEFAULT 4, probe_status TEXT DEFAULT ''
    );
    CREATE TABLE runtime_credentials (
        id INTEGER PRIMARY KEY, scope TEXT DEFAULT 'global', owner_id TEXT DEFAULT '',
        owner_name TEXT DEFAULT '', device_name TEXT DEFAULT '', status TEXT DEFAULT 'active'
    );
    CREATE TABLE engine_capabilities (
        runtime_id INTEGER, tag TEXT, max_concurrent INTEGER DEFAULT 4, enabled INTEGER DEFAULT 1
    );
    CREATE TABLE events (
        seq INTEGER PRIMARY KEY, repo_key TEXT, event_type TEXT, source TEXT, issue_id TEXT,
        title TEXT, summary TEXT, actor TEXT, payload TEXT DEFAULT '', agent_type TEXT,
        runtime TEXT, skill TEXT, status TEXT, exit_code INTEGER, duration REAL,
        agent_logs TEXT, result TEXT, structured_output TEXT,
        created_at TEXT, started_at TEXT, completed_at TEXT, entity_type TEXT DEFAULT ''
    );
    CREATE TABLE task_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, created_at TEXT, content TEXT
    );
    """)
    monkeypatch.setattr(svc, "db", test_db)
    monkeypatch.setattr(svc, "db_lock", threading.Lock())
    import src.session_manager as _sm
    import src.api.event_store as _es
    monkeypatch.setattr(_sm, "db", test_db)
    monkeypatch.setattr(_sm, "db_lock", threading.Lock())
    monkeypatch.setattr(_es, "db", test_db)
    monkeypatch.setattr(_es, "db_lock", threading.Lock())
    yield test_db
    test_db.close()


def _mk_engine(c, rid, name, status="online", healthy=1, cred=63, tag="coding", probe="ok"):
    import datetime as _dt
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, installed, credential_id, "
              "last_seen_at, busy_slots, max_concurrent, probe_status) VALUES (?,?,?,?,1,?,?,0,4,?)",
              (rid, name, status, healthy, cred,
               _dt.datetime.now(_dt.timezone.utc).isoformat(), probe))
    if tag is not None:
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, enabled) "
                  "VALUES (?,?,4,1)", (rid, tag))


def _mk_task(c, tid, seq, status="queued", attempts=0):
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
              "agent_session_id, issue_session_id, created_at, agent_type, event_seq, attempts) "
              "VALUES (?, ?, 0, 'coding', '[10907]', 's', 'is', datetime('now'), 'coding', ?, ?)",
              (tid, status, seq, attempts))


def _mk_event(c, seq, status="running"):
    c.execute("INSERT INTO events (seq, repo_key, status, result, exit_code, completed_at) "
              "VALUES (?, 'gitcode:x/r', ?, '', NULL, '')", (seq, status))


# ===== UT-A：认领判死 → 事件诊断字段完整 =====

def test_ut_a_claim_pool_unreachable_writes_event_diagnostics(setup_db):
    from src.runtime import service as svc
    c = setup_db
    _mk_engine(c, 10907, "codex", status="offline", healthy=0)
    _mk_task(c, "t_369a", 9401)
    _mk_event(c, 9401)
    setup_db.commit()

    with svc.db_lock:
        _c = svc.db.cursor()
        svc._fail_task_pool_unreachable(_c, "t_369a", [10907], "coding")

    ev = c.execute("SELECT status, result, exit_code, completed_at FROM events WHERE seq=9401").fetchone()
    assert ev[0] == "failed"
    assert ev[1] and "codex" in ev[1] and "不可达" in ev[1], f"result 含人话原因: {ev[1]}"
    assert ev[2] is not None and ev[2] != 0, f"exit_code 非空非 0: {ev[2]}"
    assert ev[3], "completed_at 非空"


# ===== UT-B：健康分不足超时判死 → 事件诊断字段完整 =====

def test_ut_b_pool_unhealthy_writes_event_diagnostics(setup_db):
    from src.runtime import service as svc
    c = setup_db
    _mk_engine(c, 10907, "codex", healthy=0, probe="fail")
    _mk_task(c, "t_369b", 9402)
    _mk_event(c, 9402)
    setup_db.commit()

    with svc.db_lock:
        _c = svc.db.cursor()
        svc._fail_task_pool_unhealthy(_c, "t_369b", [10907], 120)

    ev = c.execute("SELECT status, result, exit_code, completed_at FROM events WHERE seq=9402").fetchone()
    assert ev[0] == "failed"
    assert ev[1] and "健康分不足" in ev[1] and "codex" in ev[1], f"result 含人话原因: {ev[1]}"
    assert ev[2] is not None and ev[2] != 0
    assert ev[3], "completed_at 非空"


# ===== UT-C：_sync_event_failed 直接覆盖回收超限等其余判死路径 =====

def test_ut_c_sync_event_failed_full_fields(setup_db):
    from src.runtime import service as svc
    c = setup_db
    _mk_event(c, 9403)
    setup_db.commit()

    svc._sync_event_failed(9403, "回收超限：dispatched 超时回收 50 次仍无有效认领，任务判死")

    ev = c.execute("SELECT status, result, exit_code, completed_at FROM events WHERE seq=9403").fetchone()
    assert ev[0] == "failed"
    assert ev[1] and "回收超限" in ev[1]
    assert ev[2] == 1
    assert ev[3]


# ===== UT-D：终态保护——已 completed 的事件不被 failed 覆盖（幂等/双主安全）=====

def test_ut_d_terminal_completed_not_overwritten(setup_db):
    from src.runtime import service as svc
    c = setup_db
    _mk_event(c, 9404, status="completed")
    c.execute("UPDATE events SET result='done ok', exit_code=0, completed_at='2026-10-19T00:00:00+00:00' "
              "WHERE seq=9404")
    setup_db.commit()

    svc._sync_event_failed(9404, "迟到的判死信号（双主对侧重放）")

    ev = c.execute("SELECT status, result, exit_code FROM events WHERE seq=9404").fetchone()
    assert ev[0] == "completed", "已 completed 不被 failed 覆盖"
    assert ev[1] == "done ok"
    assert ev[2] == 0


# ===== UT-E：已 failed 的事件重复回写幂等、不报错 =====

def test_ut_e_repeated_failed_writeback_idempotent(setup_db):
    from src.runtime import service as svc
    c = setup_db
    _mk_event(c, 9405)
    setup_db.commit()

    svc._sync_event_failed(9405, "第一次判死原因")
    first = c.execute("SELECT status, result, exit_code, completed_at FROM events WHERE seq=9405").fetchone()
    assert first[0] == "failed" and first[1] == "第一次判死原因"

    # 双主 scp 同步场景：对侧再次回写同终态 → 不异常、不破坏字段
    svc._sync_event_failed(9405, "对侧重放判死原因")
    second = c.execute("SELECT status, result, exit_code, completed_at FROM events WHERE seq=9405").fetchone()
    assert second[0] == "failed"
    assert second[1] == "第一次判死原因", "终态保护下不重复覆盖"


# ===== UT-F：空 seq / 异常安全 =====

def test_ut_f_zero_seq_noop(setup_db):
    from src.runtime import service as svc
    svc._sync_event_failed(0, "no event")  # 不抛异常即可
    svc._sync_event_failed(None, "no event")


# ===== UT-G：停机清理判死（executor.terminate_active_agents DIRECT 任务）→ 事件字段完整 =====

def test_ut_g_terminate_active_agents_writes_event_diagnostics(setup_db):
    from src.runtime import service as svc
    from src.agent import executor
    c = setup_db
    _mk_engine(c, 10907, "codex", cred=0)  # credential_id=0 → DIRECT，停机判死
    c.execute(
        "INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, agent_session_id, "
        "issue_session_id, created_at, agent_type, event_seq, attempts) "
        "VALUES ('t_369g', 'running', 10907, 'coding', 's', 'is', datetime('now'), 'coding', 9406, 0)")
    _mk_event(c, 9406)
    setup_db.commit()

    executor.terminate_active_agents()

    ev = c.execute("SELECT status, result, exit_code, completed_at FROM events WHERE seq=9406").fetchone()
    assert ev[0] == "failed"
    assert ev[1] and "shutdown" in ev[1].lower(), f"result 含停机原因: {ev[1]}"
    assert ev[2] is not None and ev[2] != 0
    assert ev[3], "completed_at 非空"


# ===== UT-H：recover-orphans attempts 封顶判死 → 事件字段完整 =====

def test_ut_h_recover_orphans_cap_writes_event_diagnostics(setup_db, monkeypatch):
    from src.api.routes import runtime as rr
    c = setup_db
    monkeypatch.setattr(rr, "db", setup_db)
    monkeypatch.setattr(rr, "db_lock", __import__("threading").Lock())
    _mk_engine(c, 10907, "codex", cred=63)
    c.execute(
        "INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, agent_session_id, "
        "issue_session_id, created_at, agent_type, event_seq, attempts) "
        "VALUES ('t_369h', 'dispatched', 10907, 'coding', 's', 'is', datetime('now'), 'coding', 9407, 2)")
    _mk_event(c, 9407)
    setup_db.commit()

    rr.runtime_recover_orphans(runtime_id=10907, credential_id=63)

    task = c.execute("SELECT status FROM agent_tasks WHERE id='t_369h'").fetchone()
    assert task[0] == "failed", "attempts 2+1>=3 → 封顶判死"
    ev = c.execute("SELECT status, result, exit_code, completed_at FROM events WHERE seq=9407").fetchone()
    assert ev[0] == "failed"
    assert ev[1] and "attempts cap" in ev[1], f"result 含判死原因: {ev[1]}"
    assert ev[2] is not None and ev[2] != 0
    assert ev[3], "completed_at 非空"
