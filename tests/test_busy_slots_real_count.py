"""方案 D：调度决策点以真实 COUNT(running) 为唯一事实源（2026-10-18 定稿）。

背景（local-engine-busy-slots-zombie-fix-design.md §十）：
- #256 → #2340(L1/L2) → #10005，busy_slots 增量 counter 反复因漏递减路径泄漏，
  每次「补一处漏」下次又冒新漏。方案 D 改为：满载判定读 agent_tasks 真实 running COUNT，
  busy_slots 列降级为展示快照，不再参与决策。
- 核心不变量：**状态机字段正确 ⇒ 计数自动正确**——即使 busy_slots 列残留任意值，
  只要真实 running 数 < max_concurrent，引擎必须能认领。

UT-D1: _engine_busy 看真实 COUNT（busy_slots 列残留不影响）
UT-D2: claim_by_tag 满载看真实 COUNT（busy_slots=0 但实际满 → 拒绝；busy_slots=999 但实际空 → 放行）
UT-D3: 泄漏路径（漏 release_busy_slot）不再卡队列：真实 running 归零 → 立即可认领
UT-D4: 展示快照重算函数：busy_slots 列 = 真实 COUNT
UT-D5: covering 索引存在（runtime_id, status）→ EXPLAIN 显示 COVERING INDEX
"""
import os, sqlite3

import pytest

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "sessions.db")


@pytest.fixture
def env(monkeypatch):
    """复用项目真实 DB，插入独立测试数据（id 高段避免与其它测试撞车）。"""
    import src.runtime.service as svc
    from src.session_manager import db, db_lock
    monkeypatch.setattr(svc, "db", db)
    monkeypatch.setattr(svc, "db_lock", db_lock)

    RID = 970001
    TASKS = [970001, 970002, 970003, 970004, 970005]
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id IN (%s)" % ",".join("?" * len(TASKS)), TASKS)
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (RID,))
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (RID,))
        c.execute(
            "INSERT INTO agent_runtimes (id, credential_id, name, status, healthy, busy_slots, max_concurrent) "
            "VALUES (?, 0, 'dcount-eng', 'online', 1, 0, 4)", (RID,)
        )
        c.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled) "
            "VALUES (?, 'all', 1, 50, 1)", (RID,)
        )
        db.commit()
    yield db, RID, TASKS
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id IN (%s)" % ",".join("?" * len(TASKS)), TASKS)
        c.execute("DELETE FROM agent_runtimes WHERE id=?", (RID,))
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (RID,))
        db.commit()


def _seed_task(db, db_lock, tid, status, rid, pool_tag="all", sess=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO agent_tasks "
            "(id, runtime_id, status, pool_tag, agent_session_id, issue_session_id, started_at, prompt) "
            "VALUES (?,?,?,?,?,?, datetime('now'), 'test')",
            (tid, rid if status in ("running", "dispatched") else 0, status, pool_tag,
             sess or f"d-sess-{tid}", f"d-issue-{tid}"))
        db.commit()


def _count_real(env, statuses=("running", "dispatched")):
    db, rid, _ = env
    ph = ",".join("?" * len(statuses))
    return db.cursor().execute(
        f"SELECT COUNT(*) FROM agent_tasks WHERE runtime_id=? AND status IN ({ph})",
        (rid, *statuses)).fetchone()[0]


# ---------- UT-D1 ----------

def test_engine_busy_reads_real_count_not_column(env):
    """busy_slots 列故意置 999（僵尸残留），但真实 running=0 → 引擎不忙，可认领。"""
    import src.runtime.local_claimer as lc
    db, rid, _ = env
    with db.session_manager_db_lock if hasattr(db, "session_manager_db_lock") else __import__("src.session_manager", fromlist=["db_lock"]).db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET busy_slots=999 WHERE id=?", (rid,))
        db.commit()
    # 真实 running=0 → 不忙
    assert lc._engine_busy(rid) is False, "busy_slots=999 但真实 running=0，_engine_busy 应 False（决策看真实 COUNT）"


def test_engine_busy_real_count_full(env):
    """真实 running=max_concurrent=4，busy_slots 列残留 0 → 引擎忙。"""
    import src.runtime.local_claimer as lc
    db, rid, tasks = env
    for i, tid in enumerate(tasks[:4]):
        _seed_task(db, db.session_manager_db_lock if hasattr(db, "session_manager_db_lock") else __import__("src.session_manager", fromlist=["db_lock"]).db_lock,
                   tid, "running", rid, sess=f"d-sess-{i}")
    _seed_task(db, db.session_manager_db_lock if hasattr(db, "session_manager_db_lock") else __import__("src.session_manager", fromlist=["db_lock"]).db_lock,
               tasks[4], "queued", rid, sess="d-sess-q")
    assert _count_real(env) == 4
    assert lc._engine_busy(rid) is True, "真实 running=4=max_concurrent，_engine_busy 应 True"


# ---------- UT-D2 ----------

def test_claim_rejected_when_real_count_full(env):
    """busy_slots 列残留 0，但真实 running=4 → claim_by_tag 应拒绝（决策看真实 COUNT）。"""
    from src.runtime.service import claim_by_tag
    import src.session_manager as sm
    db, rid, tasks = env
    for i, tid in enumerate(tasks[:4]):
        _seed_task(db, sm.db_lock, tid, "running", rid, sess=f"d-sess-{i}")
    with sm.db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET busy_slots=0 WHERE id=?", (rid,))  # 残留 0
        db.commit()
    # 单测真实 running 已满，再加一个 queued 待认领
    _seed_task(db, sm.db_lock, tasks[4], "queued", rid, sess="d-sess-q")
    assert _count_real(env) == 4
    res = claim_by_tag(tag="all", runtime_id=rid)
    assert res is None, "真实 running=4 满载，busy_slots=0 也应由真实 COUNT 拒绝 claim"


def test_claim_allowed_when_real_count_spare(env):
    """busy_slots 列残留 999（僵尸），但真实 running=1 < 4 → claim_by_tag 放行。"""
    from src.runtime.service import claim_by_tag
    import src.session_manager as sm
    db, rid, tasks = env
    _seed_task(db, sm.db_lock, tasks[0], "running", rid, sess="d-sess-0")
    _seed_task(db, sm.db_lock, tasks[1], "queued", rid, sess="d-sess-q")
    with sm.db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET busy_slots=999 WHERE id=?", (rid,))  # 僵尸残留
        db.commit()
    assert _count_real(env) == 1
    res = claim_by_tag(tag="all", runtime_id=rid)
    assert res is not None, "真实 running=1<4，busy_slots=999 不应阻止 claim（决策看真实 COUNT）"


# ---------- UT-D3 ----------

def test_leaked_busy_no_longer_blocks_claim(env):
    """模拟「完成但漏 release_busy_slot」：任务置终态（真实 running 归零）后立即能再认领。"""
    from src.runtime.service import claim_by_tag
    import src.session_manager as sm
    db, rid, tasks = env
    # 先占用 1 槽（running）
    _seed_task(db, sm.db_lock, tasks[0], "running", rid, sess="d-sess-0")
    # busy_slots 列人为残留为 4（僵尸：漏递减）
    with sm.db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET busy_slots=4 WHERE id=?", (rid,))
        db.commit()
    # 任务终态（直接 UPDATE，模拟绕过 release_busy_slot 的泄漏路径）
    with sm.db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET status='completed', completed_at=datetime('now') WHERE id=?", (tasks[0],))
        db.commit()
    # 新 queued 任务
    _seed_task(db, sm.db_lock, tasks[1], "queued", rid, sess="d-sess-q")
    assert _count_real(env) == 0, "真实 running 已归零（终态）"
    res = claim_by_tag(tag="all", runtime_id=rid)
    assert res is not None, "泄漏路径后真实 running=0，应能立即认领（busy_slots=4 僵尸不阻塞）"


# ---------- UT-D4 ----------

def test_snapshot_reconcile_busy_slots(env):
    """展示快照重算：busy_slots 列 = 真实 running 数。"""
    import src.session_manager as sm
    db, rid, tasks = env
    # 快照函数（方案 D 新增）：stub 先验证行为，生产实现放到 src.runtime.service.refresh_busy_snapshot
    from src.runtime.service import refresh_busy_snapshot as fn
    _seed_task(db, sm.db_lock, tasks[0], "running", rid, sess="d-sess-0")
    _seed_task(db, sm.db_lock, tasks[1], "running", rid, sess="d-sess-1")
    with sm.db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_runtimes SET busy_slots=997 WHERE id=?", (rid,))
        db.commit()
    fn(rid)
    v = db.cursor().execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
    assert v == 2, f"快照重算后 busy_slots 应=真实 running 2，got {v}"
    # 终态后重算归零
    with sm.db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET status='completed' WHERE id IN (?,?)", (tasks[0], tasks[1]))
        db.commit()
    fn(rid)
    v = db.cursor().execute("SELECT busy_slots FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
    assert v == 0, f"终态重算后 busy_slots=0，got {v}"


# ---------- UT-D5 ----------

def test_covering_index_runtime_status_exists(env):
    """covering 索引 (runtime_id, status) 存在，且 EXPLAIN 为 COVERING INDEX。"""
    import src.session_manager as sm
    c = sm.db.cursor()
    row = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND tbl_name='agent_tasks' AND name='idx_agent_tasks_runtime_status'"
    ).fetchone()
    assert row, "covering 索引 idx_agent_tasks_runtime_status 应存在"
    plan = c.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM agent_tasks WHERE runtime_id=970001 AND status IN ('running','dispatched')"
    ).fetchall()
    detail = " ".join(str(r[3]) for r in plan)
    assert "COVERING" in detail.upper(), f"COUNT 应走 COVERING INDEX（纯索引扫描），got: {detail}"