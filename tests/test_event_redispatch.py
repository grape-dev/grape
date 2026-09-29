"""redispatch 事件终态重置测试（weekly-05 event-redispatch-terminal-reset-design）。

覆盖：
- UT-01 reset_event_terminal：failed 事件 → running + structured_output/result 清空
- UT-02 redispatch 重置后 complete 能写 structured_output（failed→reset running→completed）
- UT-03 普通（非 reset）complete 仍受终态保护（failed 不被 completed 覆盖）
"""
import os, sys, json

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock

RK = "gitcode:org/redis_event"


def _seed_event(seq, status="running", structured=None, result=""):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=? AND seq=?", (RK, seq))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, actor, "
            "payload, agent_type, runtime, skill, status, exit_code, duration, agent_logs, result, "
            "created_at, started_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'),"
            "CASE WHEN ?='completed' OR ?='failed' THEN datetime('now') ELSE '' END)",
            (seq, RK, "issue_opened", "webhook", "#7", "t", "s", "", "{}",
             "vod", "opencode", "vod-cli", status, None, None, "[]", result,
             status, status))
        if structured is not None:
            c.execute("UPDATE events SET structured_output=? WHERE seq=?", (json.dumps(structured), seq))
        db.commit()


def _get_event(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, structured_output, result FROM events WHERE seq=?", (seq,))
        return c.fetchone()


# UT-01：reset_event_terminal 重置 failed 事件并清结构化产出

def test_reset_terminal_clears_failed_event():
    from src.api.event_store import reset_event_terminal
    _seed_event(9101, status="failed", structured={"actions": [], "conclusion": "旧结论", "artifacts": []}, result="旧")
    reset_event_terminal(9101)
    st, so, result = _get_event(9101)
    assert st == "running"
    assert so is None
    assert result == ""


# UT-02：reset 后 complete 能写 structured_output 并转 completed

def test_after_reset_complete_writes_structured():
    from src.api.event_store import reset_event_terminal, update_event_complete
    _seed_event(9102, status="failed", result="旧")
    reset_event_terminal(9102)
    structured = {
        "actions": [{"cmd": "x"}], "conclusion": "重跑成功", "artifacts": ["a1"],
        "next_step": "issue", "issue_summary": "s7",
    }
    update_event_complete(9102, "completed", exit_code=0, result="重跑成功", structured_output=structured)
    st, so, result = _get_event(9102)
    assert st == "completed"
    assert result == "重跑成功"
    assert json.loads(so)["conclusion"] == "重跑成功"


# UT-03：普通（非 reset）终态保护仍生效——failed 不被 completed 覆盖

def test_terminal_protection_preserved():
    from src.api.event_store import update_event_complete
    _seed_event(9103, status="failed", result="已失败")
    update_event_complete(9103, "completed", exit_code=0, result="不应生效",
                          structured_output={"actions": [], "conclusion": "x", "artifacts": []})
    st, so, result = _get_event(9103)
    assert st == "failed"          # 保持
    assert result == "已失败"      # 未被覆盖
    assert so is None              # 不写 structured_output