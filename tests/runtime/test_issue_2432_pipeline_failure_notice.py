"""Issue #2432：流水线节点失败终态、路线图失败态与飞书群通知去重。

覆盖：
- 失败终态统一：执行失败 / 超时（timeout_failed）/ 重试耗尽均收敛为 failed；
- 失败节点名落库（events.failed_node）；
- 路线图失败态展示（node_status=3 + failed_node / failed_node_name）；
- 通知去重：按「需求+节点」绑定节点状态周期，恢复后再次失败可再次通知。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.kernel.session_manager import db, db_lock

RK = "gitcode:org/issue2432"


def _seed_event(seq, issue="#2432", agent="coding", status="running", result=""):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, actor, "
            "payload, agent_type, runtime, skill, status, exit_code, duration, agent_logs, result, "
            "created_at, started_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'),"
            "CASE WHEN ? IN ('completed','failed','cancelled') THEN datetime('now') ELSE '' END)",
            (seq, RK, "a2a_event", "webhook", issue, "t", "s", "", "{}",
             agent, "opencode", "coding-cli", status, None, None, "[]", result,
             status))
        db.commit()


def _get_event(seq):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, failed_node FROM events WHERE seq=?", (seq,),)
        return c.fetchone()


# ---------------------------------------------------------------- 1. 失败终态统一

def test_timeout_and_retry_exhausted_map_to_failed_terminal():
    """超时 / 重试耗尽与执行失败统一收敛为事件 failed（失败判定口径一致）。"""
    from src.domains.runtime.engine.cascade import cascade_event_terminal_state
    assert cascade_event_terminal_state("failed")[0] == "failed"
    assert cascade_event_terminal_state("timeout_failed")[0] == "failed"
    # 重试耗尽最终写任务 failed，事件终态同为 failed
    assert cascade_event_terminal_state("failed", exit_code=0)[0] == "failed"


def test_failed_terminal_persists_failed_node():
    """失败终态落库失败节点名（执行失败 / 超时 / 重试耗尽均走同一终态收口）。"""
    from src.api.event_store import update_event_complete
    _seed_event(2432001, status="running")
    update_event_complete(2432001, "failed", exit_code=1, result="执行失败")
    st, failed_node = _get_event(2432001)
    assert st == "failed"
    assert failed_node == "coding"


def test_timeout_failed_event_persists_failed_node():
    from src.api.event_store import update_event_complete
    _seed_event(2432002, agent="qa", status="running")
    update_event_complete(2432002, "failed", exit_code=-1, result="超时失败")
    st, failed_node = _get_event(2432002)
    assert st == "failed"
    assert failed_node == "qa"


# ---------------------------------------------------------------- 2. 路线图失败态

def test_roadmap_node_shows_failed_status_and_node_name():
    from src.api.pipeline_status import compute_node_statuses
    events = [
        {"seq": 1, "agent_type": "vod", "status": "completed"},
        {"seq": 2, "agent_type": "coding", "status": "failed", "failed_node": "coding"},
    ]
    nodes, statuses = compute_node_statuses(
        [{"id": "vod", "name": "VOD"}, {"id": "coding", "name": "Coding"}],
        events, current_stage="coding")
    assert statuses == [2, 3]
    assert nodes[1]["failed_node"] == "coding"
    assert nodes[1]["failed_node_name"] == "Coding"
    # 失败原因不外露（节点仅输出失败节点名）
    assert "result" not in nodes[1]
    assert "reason" not in nodes[1]


def test_roadmap_node_not_failed_has_no_failed_node():
    from src.api.pipeline_status import compute_node_statuses
    nodes, statuses = compute_node_statuses(
        [{"id": "coding", "name": "Coding"}],
        [{"seq": 1, "agent_type": "coding", "status": "completed"}],
        current_stage="coding")
    assert statuses == [2]
    assert nodes[0]["failed_node"] == ""
    assert nodes[0]["failed_node_name"] == ""


# ---------------------------------------------------------------- 3. 通知去重

def test_dedupe_by_node_state_cycle():
    from src.api.pipeline_failure_notify import should_notify_for_cycle
    assert should_notify_for_cycle([]) is True
    assert should_notify_for_cycle(["failed"]) is False
    assert should_notify_for_cycle(["completed", "failed"]) is False
    assert should_notify_for_cycle(["failed", "completed"]) is True
    assert should_notify_for_cycle(["failed", "cancelled"]) is True


def test_notify_skips_duplicate_failure_cycle(monkeypatch):
    from src.api.pipeline_failure_notify import notify_node_failure
    import src.domains.runtime.im.notify as N
    calls = []

    def _fake(repo_key, kind, text, **kw):
        calls.append((repo_key, kind, text, kw))
        return {"queued": 1, "reason": ""}

    monkeypatch.setattr(N, "notify_repo_group", _fake)
    # 上一终态 failed → 同一失败周期，不重复通知
    _seed_event(2432010, status="failed")
    res = notify_node_failure(RK, "#2432", "coding", 2432011)
    assert res["notified"] is False
    assert res["reason"] == "duplicate_cycle"
    assert calls == []


def test_notify_after_node_recovered(monkeypatch):
    from src.api.pipeline_failure_notify import notify_node_failure
    import src.domains.runtime.im.notify as N
    calls = []

    def _fake(repo_key, kind, text, **kw):
        calls.append((repo_key, kind, text, kw))
        return {"queued": 1, "reason": ""}

    monkeypatch.setattr(N, "notify_repo_group", _fake)
    # 失败 → 恢复（completed）→ 再次失败：新周期需再次通知
    _seed_event(2432020, status="failed")
    _seed_event(2432021, status="completed")
    res = notify_node_failure(RK, "#2432", "coding", 2432022)
    assert res["notified"] is True
    assert len(calls) == 1
    text = calls[0][2]
    assert "#2432" in text
    assert "Coding" in text or "coding" in text
    # 不含失败原因
    assert "失败原因" not in text


def test_failure_text_contains_issue_and_node_only():
    from src.api.pipeline_failure_notify import build_failure_text
    text = build_failure_text(2432, "流水线失败提示", "Coding Agent")
    assert "Issue：#2432" in text
    assert "标题：流水线失败提示" in text
    assert "失败节点：Coding Agent" in text
    assert "原因" not in text

# ---------------------------------------------------------------- 4. 公共提取 helper

def test_extract_failed_node_prefers_node_level_name():
    from src.api.pipeline_status import extract_failed_node
    nodes = [
        {"id": "vod", "name": "VOD", "node_status": 2},
        {"id": "coding", "name": "Coding", "node_status": 3,
         "failed_node": "coding", "failed_node_name": "Coding Agent"},
        {"id": "qa", "name": "QA", "node_status": 3,
         "failed_node": "qa", "failed_node_name": "QA Agent"},
    ]
    assert extract_failed_node(nodes) == ("coding", "Coding Agent")
    # 兼容 statuses 列表调用（菜单流水线页）
    assert extract_failed_node(nodes, [2, 3, 3]) == ("coding", "Coding Agent")
    assert extract_failed_node([{"id": "vod", "node_status": 2}]) == ("", "")


# ---------------------------------------------------------------- 5. 群通知范围/开关/机器人

def test_notify_repo_group_only_feishu_enabled_and_matching_app(monkeypatch):
    from src.domains.runtime.im import notify as N
    import src.domains.runtime.im.engine_wiring as EW
    import src.domains.runtime.im.outbox as OB

    rk = "gitcode:org/issue2432-notify"
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM repo_im_group_binding WHERE repo_key=?", (rk,))
        c.execute(
            "INSERT INTO repo_im_group_binding "
            "(im_provider, im_chat_id, repo_key, im_app_id, notify_enabled) "
            "VALUES ('feishu','chat-feishu',?,'app-feishu',1),"
            "('feishu','chat-off',?,'app-off',0),"
            "('dingtalk','chat-ding',?,'app-ding',1)", (rk, rk, rk))
        db.commit()

    inst_calls = []
    enqueued = []

    def _fake_inst(scope, **kw):
        inst_calls.append(kw)
        return {"installation_id": "iid-" + str(kw.get("app_id") or "")}

    def _fake_enqueue(**kw):
        enqueued.append(kw)
        return True

    monkeypatch.setattr(EW, "get_active_installation", _fake_inst)
    monkeypatch.setattr(OB, "enqueue", _fake_enqueue)
    try:
        res = N.notify_repo_group(rk, "pipeline_failed", "x", issue_number=1,
                                  discriminator="coding:1")
        assert res == {"queued": 1, "reason": ""}
        # 仅飞书且通知开关开启的绑定；按绑定 im_app_id 选安装
        assert [c.get("app_id") for c in inst_calls] == ["app-feishu"]
        assert [c.get("channel_chat_id") for c in enqueued] == ["chat-feishu"]
    finally:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM repo_im_group_binding WHERE repo_key=?", (rk,))
            db.commit()
