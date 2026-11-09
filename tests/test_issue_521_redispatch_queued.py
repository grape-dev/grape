"""Issue #521 回归测试：重派 queued 任务副本原子收敛 + 回执 task_id 一致性。

覆盖 spec 验收标准：
- UT-1 plugin 模式 queued→重派：复用原任务行（task_id 不变、runtime_id 重选）、
       同一事件仅一个可领取（queued）任务、enqueue_task 不被调用、回执=原 task_id
- UT-2 终态重建回执一致性：返回新任务 ID（旧实现返回旧终态 task_id）
- UT-3 local_only + queued：同事务复用原行，不新建副本，回执=原 task_id
- UT-4 dispatched/running 回归：仍走 requeue（不进入 queued 复用分支），回执=原 task_id
- UT-5 领取竞态守卫：UPDATE ... WHERE status='queued' rowcount=0 → 409，无半状态
"""
import os
import sys
import threading

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

import pytest

RK = "gitcode:test/i521r"


@pytest.fixture()
def setup_db(monkeypatch):
    """独立内存库：agent_tasks / agent_runtimes / runtime_credentials /
    engine_capabilities / events / devops_agent_session。"""
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
    CREATE TABLE devops_agent_session (
        repo_key TEXT, agent_type TEXT, runtime_id INTEGER DEFAULT 0
    );
    """)
    _lock = threading.RLock()
    # redispatch_event 内部均为函数级 import：patch 模块属性即可全部生效
    import src.session_manager as _sm
    import src.api.event_store as _es
    monkeypatch.setattr(_sm, "db", test_db, raising=False)
    monkeypatch.setattr(_sm, "db_lock", _lock, raising=False)
    monkeypatch.setattr(_es, "db", test_db, raising=False)
    monkeypatch.setattr(_es, "db_lock", _lock, raising=False)
    monkeypatch.setattr(svc, "db", test_db, raising=False)
    monkeypatch.setattr(svc, "db_lock", _lock, raising=False)
    yield test_db
    test_db.close()


def _mk_engine(c, rid, name, tag, cred=63):
    import datetime as _dt
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, installed, credential_id, "
              "last_seen_at, busy_slots, max_concurrent, probe_status) VALUES (?,?, 'online', 1, 1, ?, ?, 0, 4, 'ok')",
              (rid, name, cred, _dt.datetime.now(_dt.timezone.utc).isoformat()))
    c.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, enabled) VALUES (?,?,4,1)",
              (rid, tag))
    c.execute("INSERT INTO runtime_credentials (id, scope) VALUES (?, 'global')", (cred,))


def _mk_task(c, tid, status, runtime_id=0, pool_tag="", pool_eids="", seq=9401, prompt="do work"):
    c.execute(
        "INSERT INTO agent_tasks (id, status, runtime_id, pool_tag, pool_engine_ids, "
        "agent_session_id, issue_session_id, created_at, agent_type, event_seq, repo_key, "
        "issue_id, prompt, skill, event_type, account) "
        "VALUES (?,?,?,?,?,?, 'is', datetime('now'), 'coding', ?, ?, '5', ?, 'skillx', 'push', 'tester')",
        (tid, status, runtime_id, pool_tag, pool_eids, "s1", seq, RK, prompt))


_EVT = {"seq": 9401, "repo_key": RK, "issue_id": "5", "event_type": "push",
        "status": "running", "payload": "{}", "source": "gitcode", "actor": "tester",
        "agent_type": "coding", "scene_id": ""}


def _common_patches(monkeypatch, plugin):
    from unittest.mock import MagicMock
    from src.api.routes import events as ev
    from src.api import event_pipeline as epl
    from src.api import event_store as est
    from src.api import deps as _deps
    from src.user import db as udb
    from src.runtime import service as svc

    monkeypatch.setattr(ev, "_load_event", lambda s: dict(_EVT))
    monkeypatch.setattr(_deps, "check_repo_access", lambda *a, **k: None)
    monkeypatch.setattr(udb, "get_meta_value", lambda k: "")
    monkeypatch.setattr(udb, "set_meta_value", lambda k, v: None)
    monkeypatch.setattr(epl, "is_plugin_mode", lambda: plugin)
    monkeypatch.setattr(est, "reset_event_terminal", MagicMock())
    monkeypatch.setattr(epl, "build_task_env", lambda **k: {})
    monkeypatch.setattr(epl, "get_agent_workdir", lambda *a, **k: "/tmp/w")
    monkeypatch.setattr(svc, "enqueue_task", MagicMock(return_value="t_new_521"))
    monkeypatch.setattr(svc, "requeue_task", MagicMock())
    monkeypatch.setattr(svc, "_remote_runtime_ready", lambda rid: True)
    return ev, svc


# ===== UT-1：plugin 模式 queued→重派 → 复用原行，仅一个可领取任务，回执=原 task_id =====

def test_ut1_plugin_queued_redispatch_reuses_row(setup_db, monkeypatch):
    c = setup_db
    _mk_engine(c, 301, "codex-w", tag="coding")
    _mk_task(c, "t_q1", "queued", runtime_id=0, pool_tag="coding", pool_eids="[301]")
    setup_db.commit()
    ev, svc = _common_patches(monkeypatch, plugin=True)

    res = ev.redispatch_event(9401, user={"username": "admin"})

    assert res["status"] == "redispatched"
    assert res["task_id"] == "t_q1", f"回执应为原任务 ID，实际 {res['task_id']}"
    svc.enqueue_task.assert_not_called(), "queued 重派必须复用原行，不得 enqueue 新任务"
    rows = c.execute("SELECT id, status, runtime_id FROM agent_tasks WHERE event_seq=9401").fetchall()
    assert len(rows) == 1, f"同一事件应仅一个任务行，实际 {len(rows)}"
    assert rows[0][0] == "t_q1" and rows[0][1] == "queued"
    assert rows[0][2] == 301, "复用行应重选在线引擎 rid=301"
    n_claimable = c.execute(
        "SELECT COUNT(*) FROM agent_tasks WHERE event_seq=9401 AND status='queued'").fetchone()[0]
    assert n_claimable == 1, f"可领取（queued）任务应仅 1 个，实际 {n_claimable}"


# ===== UT-2：终态重建回执一致性 → 返回新任务 ID（非旧终态 task_id） =====

def test_ut2_terminal_rebuild_receipt_returns_new_task_id(setup_db, monkeypatch):
    from unittest.mock import MagicMock
    c = setup_db
    _mk_engine(c, 501, "codex-t", tag="coding")
    _mk_task(c, "t_old2", "failed", runtime_id=0, seq=9402)
    c.execute("INSERT INTO events (seq, repo_key, status) VALUES (9402, ?, 'failed')", (RK,))
    setup_db.commit()
    ev, svc = _common_patches(monkeypatch, plugin=False)
    monkeypatch.setattr(ev, "_load_event", lambda s: dict(_EVT, seq=9402))
    import src.api.session_store as sst
    from src.runtime import engine_select as esel
    monkeypatch.setattr(sst, "get_issue_scene", lambda *a, **k: ("", 0))
    monkeypatch.setattr(esel, "pick_engine", lambda *a, **k: 501)

    res = ev.redispatch_event(9402, user={"username": "admin"})

    assert res["task_id"] == "t_new_521", f"终态重建回执应为新任务 ID，实际 {res['task_id']}"
    svc.enqueue_task.assert_called_once()
    old_row = c.execute("SELECT status FROM agent_tasks WHERE id='t_old2'").fetchone()
    assert old_row[0] == "failed", "旧终态行不被复活为可领取副本"


# ===== UT-3：local_only + queued → 同事务复用原行，不新建副本 =====

def test_ut3_local_only_queued_reuses_row(setup_db, monkeypatch):
    c = setup_db
    _mk_engine(c, 77, "local-w", tag="coding")
    _mk_task(c, "t_q3", "queued", runtime_id=77)
    setup_db.commit()
    ev, svc = _common_patches(monkeypatch, plugin=True)

    res = ev.redispatch_event(9401, local_only=True, user={"username": "admin"})

    assert res["task_id"] == "t_q3", f"local_only queued 重试应复用原行，实际 {res['task_id']}"
    assert res.get("local_only") is True
    svc.enqueue_task.assert_not_called(), "local_only queued 不得 enqueue 新任务"
    rows = c.execute("SELECT id, status, runtime_id FROM agent_tasks WHERE event_seq=9401").fetchall()
    assert len(rows) == 1 and rows[0][0] == "t_q3" and rows[0][1] == "queued"
    assert rows[0][2] == 77


# ===== UT-4：dispatched/running 回归 → 仍走 requeue，回执=原 task_id =====

def test_ut4_running_redispatch_still_requeues(setup_db, monkeypatch):
    c = setup_db
    _mk_task(c, "t_run4", "running", runtime_id=301, seq=9403)
    setup_db.commit()
    ev, svc = _common_patches(monkeypatch, plugin=False)
    monkeypatch.setattr(ev, "_load_event", lambda s: dict(_EVT, seq=9403))

    res = ev.redispatch_event(9403, user={"username": "admin"})

    assert res["task_id"] == "t_run4"
    svc.requeue_task.assert_called_once_with("t_run4")
    svc.enqueue_task.assert_not_called()


# ===== UT-5：领取竞态守卫 → 快照读为 queued 但行已被领取 → 409 无半状态 =====

def test_ut5_race_claimed_during_redispatch_409(setup_db, monkeypatch):
    from fastapi import HTTPException
    from unittest.mock import MagicMock
    c = setup_db
    _mk_engine(c, 301, "codex-w", tag="coding")
    # DB 行初始为 queued（进入 queued 复用分支）；get_task 读取快照时模拟窗口期内被引擎领取
    # （领取 = UPDATE status='running'），随后原子收敛 UPDATE ... WHERE status='queued' 落空
    _mk_task(c, "t_q5", "queued", runtime_id=0, pool_tag="coding", pool_eids="[301]")
    setup_db.commit()
    ev, svc = _common_patches(monkeypatch, plugin=True)
    _snap = {"id": "t_q5", "status": "queued", "runtime_id": 0, "pool_tag": "coding",
             "pool_engine_ids": "[301]", "repo_key": RK, "issue_id": "5", "user_id": 0,
             "agent_type": "coding", "agent_session_id": "s1", "issue_session_id": "is",
             "event_seq": 9401, "prompt": "p", "skill": "skillx", "event_type": "push",
             "account": "tester", "cwd": "/tmp/w", "runtime_session_id": "",
             "repo_gitcode_token": "", "server_url": "", "skill_id": 0}

    def _claim_during_read(tid):
        with svc.db_lock:
            svc.db.execute("UPDATE agent_tasks SET status='running' WHERE id=?", (tid,))
            svc.db.commit()
        return dict(_snap)

    monkeypatch.setattr(svc, "get_task", MagicMock(side_effect=_claim_during_read))

    with pytest.raises(HTTPException) as ei:
        ev.redispatch_event(9401, user={"username": "admin"})
    assert ei.value.status_code == 409, "竞态窗口被领取 → 409 拒绝，无半状态"
    svc.enqueue_task.assert_not_called()
    # 行状态未被半改动（仍为领取后的 running）
    assert c.execute("SELECT status FROM agent_tasks WHERE id='t_q5'").fetchone()[0] == "running"
