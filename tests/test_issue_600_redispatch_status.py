"""Issue #600：重派成功后流水线卡片状态不残留失败。

覆盖：
- UT-1 _mark_event_redispatched：原 failed 事件 → cancelled + 接管说明（不再残留失败态）
- UT-2 重派接管后同 agent 新事件 completed → 节点状态随最新事件翻绿
- UT-3 旧失败事件改标 cancelled 后不再参与失败聚合（statuses 无 3）
"""
import os, sys, json

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock

RK = "gitcode:org/issue600"


def _seed_event(seq, agent="qa", status="running", result=""):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=? AND seq=?", (RK, seq))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, actor, "
            "payload, agent_type, runtime, skill, status, exit_code, duration, agent_logs, result, "
            "created_at, started_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'),"
            "CASE WHEN ?='completed' OR ?='failed' THEN datetime('now') ELSE '' END)",
            (seq, RK, "a2a_event", "webhook", "#600", "t", "s", "", "{}",
             agent, "opencode", "qa-cli", status, None, None, "[]", result,
             status, status))
        db.commit()


def _get_event(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, result FROM events WHERE seq=?", (seq,))
        return c.fetchone()


# UT-1：原 failed 事件经 _mark_event_redispatched → cancelled + 接管说明

def test_mark_redispatched_converts_failed_to_cancelled():
    from src.api.routes.events import _mark_event_redispatched
    _seed_event(9201, status="failed", result="旧失败")
    _mark_event_redispatched(9201, 9202, "A2A")
    st, result = _get_event(9201)
    assert st == "cancelled"
    assert "9202" in (result or "")
    assert "接管" in (result or "")


# UT-2：重派接管后（旧事件 cancelled + 新事件 completed），节点状态取最新事件 → completed

def test_node_status_follows_latest_event_after_redispatch():
    from src.api.pipeline_status import compute_node_statuses
    _seed_event(9203, status="failed")
    from src.api.routes.events import _mark_event_redispatched
    _mark_event_redispatched(9203, 9204, "A2A")
    _seed_event(9204, status="completed")
    nodes = [{"id": "qa"}]
    events = [
        {"seq": 9203, "agent_type": "qa", "status": "cancelled"},
        {"seq": 9204, "agent_type": "qa", "status": "completed"},
    ]
    _, statuses = compute_node_statuses(nodes, events, current_stage="qa")
    assert statuses == [2]  # completed，失败态不残留


# UT-3：旧失败事件改标 cancelled 后不参与 any_failed 聚合

def test_cancelled_superseded_event_not_counted_as_failed():
    from src.api.pipeline_status import compute_node_statuses
    events = [
        {"seq": 9205, "agent_type": "qa", "status": "failed"},
    ]
    _, statuses = compute_node_statuses([{"id": "qa"}], events, current_stage="qa")
    assert statuses == [3]
    # 重派接管后：同 agent 仅剩 cancelled（无更新成功事件）→ 非失败
    events2 = [{"seq": 9205, "agent_type": "qa", "status": "cancelled"}]
    _, statuses2 = compute_node_statuses([{"id": "qa"}], events2, current_stage="qa")
    assert statuses2 == [4]
    assert 3 not in statuses2
