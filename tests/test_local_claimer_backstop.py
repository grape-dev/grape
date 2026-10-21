"""local_claimer 兜底守卫（backstop）单测。

设计：本机直连认领广播任务时，若任务年龄 < 窗口且仍有可达远端认领方 → 让位回滚；
远端全灭或任务超龄 → 立即由本机兜底执行。
"""
import os
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.runtime import local_claimer
from src.session_manager import db, db_lock


def _mk_task(age_s: float, pool_engine_ids: str = "", pool_tag: str = "qa"):
    tid = "bs-" + uuid.uuid4().hex[:12]
    created = datetime.fromtimestamp(time.time() - age_s, tz=timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, pool_tag, pool_engine_ids, status, "
            "repo_key, agent_type, user_id, prompt, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, tid, 0, pool_tag, pool_engine_ids, "queued", "org/test", "qa", 1, "test", created))
        db.commit()
    return {"id": tid, "pool_tag": pool_tag, "pool_engine_ids": pool_engine_ids,
            "repo_key": "org/test", "agent_type": "qa", "user_id": 1,
            "created_at": created, "event_seq": 0}


def test_yield_when_young_and_remote_alive(monkeypatch):
    """窗口内 + 有在线远端 → 让位"""
    t = _mk_task(age_s=5)
    monkeypatch.setattr(local_claimer, "_remote_claimants_reachable", lambda task: True)
    assert local_claimer._backstop_should_yield(t) is True


def test_no_yield_when_task_old(monkeypatch):
    """超龄 → 不让位，本机兜底"""
    t = _mk_task(age_s=local_claimer.CLAIM_BACKSTOP_DELAY + 10)
    monkeypatch.setattr(local_claimer, "_remote_claimants_reachable", lambda task: True)
    assert local_claimer._backstop_should_yield(t) is False


def test_no_yield_when_remote_dead(monkeypatch):
    """远端全灭 → 不空等，立即兜底（即使任务刚入队）"""
    t = _mk_task(age_s=1)
    monkeypatch.setattr(local_claimer, "_remote_claimants_reachable", lambda task: False)
    assert local_claimer._backstop_should_yield(t) is False


def test_no_yield_when_delay_disabled(monkeypatch):
    """GRAPE_LOCAL_CLAIMER_DELAY=0 → 守卫关闭（恢复旧行为）"""
    t = _mk_task(age_s=1)
    old = local_claimer.CLAIM_BACKSTOP_DELAY
    local_claimer.CLAIM_BACKSTOP_DELAY = 0
    try:
        monkeypatch.setattr(local_claimer, "_remote_claimants_reachable", lambda task: True)
        assert local_claimer._backstop_should_yield(t) is False
    finally:
        local_claimer.CLAIM_BACKSTOP_DELAY = old


def test_age_parse_failure_treated_as_old():
    """created_at 解析失败 → 视为超龄不阻塞兜底"""
    t = {"created_at": "garbage", "pool_tag": "qa", "pool_engine_ids": "",
         "repo_key": "org/test", "agent_type": "qa", "user_id": 1}
    assert local_claimer._task_age_seconds(t) > local_claimer.CLAIM_BACKSTOP_DELAY


def test_pool_only_local_engines_is_dead():
    """白名单只剩本机直连引擎 → _remote_claimants_reachable 返回 False（不空等）"""
    import json
    # 干净测试库：先插一行本机直连引擎（credential_id=0, scope=global, online）
    _rid = 990001
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (_rid,))
        c.execute(
            "INSERT INTO agent_runtimes (id, credential_id, name, status, healthy, scope, "
            "last_seen_at, host_name) VALUES (?,?,?,'online',1,'global','',?)",
            (_rid, 0, "bs-test-local", "test-host"))
        db.commit()
    try:
        monkey = None
        orig = local_claimer._local_direct_engine_ids
        local_claimer._local_direct_engine_ids = lambda: [_rid]
        try:
            t = _mk_task(age_s=1, pool_engine_ids=json.dumps([_rid]))
            assert local_claimer._remote_claimants_reachable(t) is False
        finally:
            local_claimer._local_direct_engine_ids = orig
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_runtimes WHERE id=?", (_rid,))
            db.commit()


def test_yield_rollback_restores_queued():
    """让位回滚 SQL：任务恢复 queued + runtime_id=0"""
    t = _mk_task(age_s=5)
    tid = t["id"]
    # 模拟 claim 后状态
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET status='running', runtime_id=? WHERE id=?", (999999, tid))
        db.commit()
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET status='queued', runtime_id=0, started_at=NULL WHERE id=? AND status='running'", (tid,))
        db.commit()
        row = c.execute("SELECT status, runtime_id FROM agent_tasks WHERE id=?", (tid,)).fetchone()
    assert row[0] == "queued" and row[1] in (0, None)
    with db_lock:
        db.cursor().execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        db.commit()
