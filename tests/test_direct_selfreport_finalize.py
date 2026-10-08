"""direct 单信号收敛测试（weekly-05 stream-end-finalize-single-signal-design.md）。

覆盖：
- UT-01 流结束(finalize_by_stream_end) 优先采用已显式 self_report 的结构化结论收敛 completed
- UT-02 流结束晚到 self_report 宽限窗内幂等补挂（attached）
- UT-03 failed 占位（exit_code in (0,NULL)）宽限窗内受控提升 completed + 补挂（promoted）
- UT-04 真实 crash（exit_code!=0）的 failed 不提升
"""
import os, sys, json

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock

RK = "gitcode:org/direct_self"
_SEQ = 9201


def _seed_task(task_id="t_direct1", status="running", self_reported=0, stream_ended=0,
               result_text="", exit_code=None):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (task_id,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, "
            "agent_type, skill, status, prompt, attempts, result_text, stream_ended, self_reported, exit_code) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, "", 24, _SEQ, "#7", RK, "vod", "vod-cli", status,
             "prompt", 0, result_text, stream_ended, self_reported, exit_code))
        db.commit()


def _seed_event(status="running"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=? AND seq=?", (RK, _SEQ))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, source, issue_id, title, summary, actor, "
            "payload, agent_type, runtime, skill, status, created_at, started_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (_SEQ, RK, "issue_opened", "webhook", "#7", "t", "s", "", "{}",
             "vod", "opencode", "vod-cli", status))
        db.commit()


def _get_task(task_id="t_direct1"):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, self_reported, stream_ended, result_text, exit_code FROM agent_tasks WHERE id=?", (task_id,))
        return c.fetchone()


def _get_event():
    with db_lock:
        return db.cursor().execute(
            "SELECT status, structured_output, result FROM events WHERE seq=?", (_SEQ,)).fetchone()


def test_stream_end_uses_self_report_structured():
    from src.runtime.service import finalize_by_stream_end
    structured = json.dumps({
        "actions": [{"cmd": "x"}], "conclusion": "结构化自报结论", "artifacts": ["a1"],
        "next_step": "issue", "issue_summary": "s7",
    }, ensure_ascii=False)
    _seed_event(status="running")
    _seed_task(self_reported=1, result_text=structured)  # agent 已显式自报

    ok = finalize_by_stream_end("t_direct1", "stream plain text", 0)
    st, sr, se, rt, _ = _get_task()
    assert ok is True
    assert st == "completed"
    assert json.loads(rt)["conclusion"] == "结构化自报结论"  # 以自报为权威


def test_stream_end_fallback_stream_text():
    from src.runtime.service import finalize_by_stream_end
    _seed_event(status="running")
    _seed_task(self_reported=0, result_text="")
    ok = finalize_by_stream_end("t_direct1", "普通流文本结论", 0)
    st, *_ = _get_task()
    assert ok is True
    assert st == "completed"


def test_late_self_report_attaches_to_completed():
    from src.runtime.service import set_task_signal, enrich_task_on_late_report
    structured = json.dumps({"actions": [{"cmd": "y"}], "conclusion": "晚到自报", "artifacts": []}, ensure_ascii=False)
    # 流结束已定案：completed 但无结构化产出
    _seed_event(status="completed")
    _seed_task(status="completed", self_reported=0, result_text="")

    set_task_signal("t_direct1", "self_reported", structured)
    r = enrich_task_on_late_report("t_direct1", structured)
    assert r == "attached"
    st, sr, se, rt, _ = _get_task()
    assert st == "completed"
    assert json.loads(rt)["conclusion"] == "晚到自报"
    _ev_status, _ev_sd, _ev_res = _get_event()
    assert _ev_status == "completed" and json.loads(_ev_sd)["conclusion"] == "晚到自报"


def test_late_self_report_promotes_placeholder_failed():
    from src.runtime.service import set_task_signal, enrich_task_on_late_report
    structured = json.dumps({"actions": [], "conclusion": "占位失败的补报", "artifacts": []}, ensure_ascii=False)
    _seed_event(status="failed")
    _seed_task(status="failed", self_reported=0, result_text="", exit_code=0)

    set_task_signal("t_direct1", "self_reported", structured)
    r = enrich_task_on_late_report("t_direct1", structured)
    assert r == "promoted"
    st, *_ = _get_task()
    assert st == "completed"
    assert _get_event()[0] == "completed"


def test_no_promote_on_real_crash():
    from src.runtime.service import set_task_signal, enrich_task_on_late_report
    structured = json.dumps({"actions": [], "conclusion": "crash 后补报", "artifacts": []}, ensure_ascii=False)
    _seed_event(status="failed")
    _seed_task(status="failed", self_reported=0, result_text="", exit_code=1)

    set_task_signal("t_direct1", "self_reported", structured)
    r = enrich_task_on_late_report("t_direct1", structured)
    assert r == "already"
    st, *_ = _get_task()
    assert st == "failed"  # 真实 crash 不提升