"""方案一：标签只做路由，并发纯引擎级。

验证 claim_by_tag 的并发检查从 engine_capabilities.max_concurrent 改为
agent_runtimes.max_concurrent（busy_slots >= max_concurrent → 拒绝 claim）。

用项目真实 DB schema（data/sessions.db），测试前备份/恢复。
"""
import sqlite3, json, os, shutil
from datetime import datetime, timezone

import pytest

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "sessions.db")
BACKUP_PATH = DB_PATH + ".test_backup"


def _backup():
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, BACKUP_PATH)


def _restore():
    if os.path.exists(BACKUP_PATH):
        shutil.move(BACKUP_PATH, DB_PATH)


@pytest.fixture(scope="module", autouse=True)
def backup_restore():
    _backup()
    yield
    _restore()


@pytest.fixture
def test_env(monkeypatch):
    """用项目真实 DB，测试前清理 + 插入测试数据，测试后恢复。"""
    from src.session_manager import db, db_lock
    import src.runtime.service as svc
    monkeypatch.setattr(svc, "db", db)
    monkeypatch.setattr(svc, "db_lock", db_lock)

    c = db.cursor()
    with db_lock:
        # 清理旧测试数据
        c.execute("DELETE FROM agent_tasks WHERE id IN (9001, 9002, 9003, 9004)")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=999")
        c.execute("DELETE FROM agent_runtimes WHERE id=999")
        # 插入测试引擎
        c.execute(
            "INSERT INTO agent_runtimes (id, credential_id, name, status, healthy, busy_slots, max_concurrent) "
            "VALUES (999, 0, 'test-engine-claim', 'online', 1, 0, 4)"
        )
        # 引擎标签：coding tag，max_concurrent=1（旧值，方案一后应被忽略）
        c.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled) "
            "VALUES (999, 'coding', 1, 50, 1)"
        )
        # queued 任务
        c.execute(
            "INSERT INTO agent_tasks (id, runtime_id, status, pool_tag, agent_session_id, issue_session_id, prompt) "
            "VALUES (9001, 0, 'queued', 'coding', 'test-sess-1', 'test-issue-1', 'test')"
        )
        db.commit()

    yield db

    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id IN (9001, 9002, 9003, 9004)")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=999")
        c.execute("DELETE FROM agent_runtimes WHERE id=999")
        db.commit()


def test_claim_by_tag_uses_engine_level_max_concurrent(test_env):
    """busy_slots=0 < max_concurrent=4，应能 claim。"""
    from src.runtime.service import claim_by_tag
    result = claim_by_tag(tag='coding', runtime_id=999)
    assert result is not None, "busy_slots=0 < max_concurrent=4，应能 claim"


def test_claim_by_tag_rejected_when_busy_slots_full(test_env):
    """真实 running 数 >= max_concurrent 时拒绝 claim（方案 D：以真实 COUNT 为事实源，
    busy_slots 列仅为展示快照，不再参与决策）。
    """
    from src.session_manager import db, db_lock
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        # 真实占用 4 个槽（running），max_concurrent=4 → 满载
        for i in range(1, 5):
            c.execute(
                "INSERT OR REPLACE INTO agent_tasks (id, runtime_id, status, pool_tag, agent_session_id, issue_session_id, started_at, prompt) "
                "VALUES (?, 999, 'running', 'coding', ?, ?, ?, 'test')",
                (9000 + i, f"full-sess-{i}", f"full-issue-{i}", now))
        db.commit()

    from src.runtime.service import claim_by_tag
    result = claim_by_tag(tag='coding', runtime_id=999)
    assert result is None, "真实 running=4 >= max_concurrent=4，应拒绝 claim（即使 busy_slots 列残留任意值）"


def test_claim_by_tag_ignores_engine_capabilities_max_concurrent(test_env):
    """engine_capabilities.max_concurrent=1 但 agent_runtimes.max_concurrent=4 时，应能 claim。"""
    from src.session_manager import db, db_lock
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        # 已有 2 个 dispatched 任务（超过 engine_capabilities.max_concurrent=1）
        c.execute(
            "INSERT INTO agent_tasks (id, runtime_id, status, pool_tag, agent_session_id, issue_session_id, started_at, prompt) "
            "VALUES (9002, 999, 'dispatched', 'coding', 'test-sess-2', 'test-issue-2', ?, 'test')", (now,)
        )
        c.execute(
            "INSERT INTO agent_tasks (id, runtime_id, status, pool_tag, agent_session_id, issue_session_id, started_at, prompt) "
            "VALUES (9003, 999, 'dispatched', 'coding', 'test-sess-3', 'test-issue-3', ?, 'test')", (now,)
        )
        # busy_slots=2，max_concurrent=4，还有余量
        c.execute("UPDATE agent_runtimes SET busy_slots=2 WHERE id=999")
        # 新的 queued 任务供 claim
        c.execute(
            "INSERT INTO agent_tasks (id, runtime_id, status, pool_tag, agent_session_id, issue_session_id, prompt) "
            "VALUES (9004, 0, 'queued', 'coding', 'test-sess-4', 'test-issue-4', 'test')"
        )
        db.commit()

    from src.runtime.service import claim_by_tag
    result = claim_by_tag(tag='coding', runtime_id=999)
    assert result is not None, "engine_capabilities.max_concurrent=1 应被忽略，busy_slots=2 < max_concurrent=4 应能 claim"
