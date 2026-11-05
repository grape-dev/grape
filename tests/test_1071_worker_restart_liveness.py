"""Issue #1071：重启后「活着却被判死」——活性判定 · 回炉守卫 · 迟到结果复活。

三个原生产故障（同一条链）：
1) 优雅停机把 REMOTE 任务置 pending_self_report，pending_since=停机时刻；判死扫描只看
   pending_since，worker 仍在产出消息也 30 分钟后一刀切 → 活着的任务被判 timeout_failed。
2) 自动回炉（reap/requeue）不看活性，回炉认领改写 started_at（= worker 的 run_token）
   → 老 worker 的 complete/fail 被 409 stale 永久拒收。
3) 判死走 update_task_status，对 timeout_failed 也 revoke 会话 token → 之后全 401。

4) 判死后才回来的结构化结果原本永远丢弃（finalize_by_stream_end 终态早退）。

本测试用真实函数 + 测试库（conftest 强制 SQLite，绝不连生产/远端）：
- scheduler._sweep_pending_self_report_timeouts / service.requeue_task /
  service.update_task_status / service.finalize_by_stream_end / service.revive_timeout_task /
  event_store.update_event_complete。
"""
import json
from datetime import datetime, timezone, timedelta

import pytest

RT = 9001

# sweep 的真实写法：errors 是纯字符串数组，来源标记内嵌在判死文案里（不改 errors 结构）
TIMEOUT_ERRORS = json.dumps(
    ["[PENDING_SELF_REPORT_TIMEOUT] worker 在 1800s 内未自报完成/失败"
     "（pending_self_report 超时），判 timeout_failed"], ensure_ascii=False)


def _iso(offset_seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _legacy(offset_seconds: float = 0) -> str:
    """存量口径：无时区 '%Y-%m-%d %H:%M:%S'（UTC 墙钟）。"""
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _seed_task(tid: str, status: str, *, pending_since=None, errors="", event_seq=None,
               started_at=None, completed_at=None, result_text="", revived_at=None):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute("DELETE FROM task_messages WHERE task_id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, repo_key, agent_type, "
            "event_type, status, prompt, cwd, skill_files, env_extra, issue_id, pending_since, "
            "errors, event_seq, started_at, completed_at, result_text, revived_at) "
            "VALUES (?,?,?,?, 'issue', 'a2a_event', ?, 'x', '', '{}', '{}', '#0', ?, ?, ?, ?, ?, ?, ?)",
            (tid, "sess-" + tid, RT, "busy/test", status, pending_since, errors, event_seq,
             started_at, completed_at, result_text, revived_at)
        )
        db.commit()


def _seed_message(tid: str, created_at: str, seq: int = 1):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO task_messages (task_id, seq, type, content, created_at) "
            "VALUES (?,?, 'text', 'progress', ?)",
            (tid, seq, created_at)
        )
        db.commit()


def _seed_event(seq: int, status: str):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE seq=?", (seq,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, agent_type, status, issue_id) "
            "VALUES (?, 'busy/test', 'a2a_event', 'issue', ?, '#0')",
            (seq, status)
        )
        db.commit()


def _status(tid: str):
    from src.session_manager import db, db_lock
    with db_lock:
        r = db.cursor().execute(
            "SELECT status, started_at, errors, revived_at, completed_at FROM agent_tasks WHERE id=?",
            (tid,)
        ).fetchone()
    return r


STRUCTURED = json.dumps({
    "actions": [{"type": "comment", "content": "done"}],
    "conclusion": "任务完成，改动已推送",
}, ensure_ascii=False)


# ---------- 1-1 判死活性判定 ----------

def test_sweep_renews_task_with_fresh_messages():
    """pending_since 已过 30 分钟，但 worker 最近还在产出 → 不判死。"""
    from src.runtime.scheduler import _sweep_pending_self_report_timeouts
    tid = "t_1071_alive"
    _seed_task(tid, "pending_self_report", pending_since=_iso(-2400), started_at=_iso(-3000))
    _seed_message(tid, _iso(-60))  # 1 分钟前还在写日志
    _sweep_pending_self_report_timeouts()
    row = _status(tid)
    assert row[0] == "pending_self_report", f"活着的任务被判死了: {row}"


def test_sweep_kills_task_without_activity():
    """真死兜底：pending_since 过 30 分钟且完全没有产出 → 照旧判死（真死任务仍回收）。"""
    from src.runtime.scheduler import _sweep_pending_self_report_timeouts
    tid = "t_1071_dead"
    _seed_task(tid, "pending_self_report", pending_since=_iso(-2400), started_at=_iso(-3000))
    _sweep_pending_self_report_timeouts()
    row = _status(tid)
    assert row[0] == "timeout_failed", f"真死任务没被判死: {row}"
    assert "PENDING_SELF_REPORT_TIMEOUT" in (row[2] or ""), f"缺来源标记: {row[2]}"


def test_sweep_handles_legacy_naive_timestamp_with_activity():
    """存量无时区 pending_since + 新口径带时区消息混合：不能被字符串比较误判。"""
    from src.runtime.scheduler import _sweep_pending_self_report_timeouts
    tid = "t_1071_legacy_alive"
    _seed_task(tid, "pending_self_report", pending_since=_legacy(-2400), started_at=_iso(-3000))
    _seed_message(tid, _iso(-120))
    _sweep_pending_self_report_timeouts()
    assert _status(tid)[0] == "pending_self_report"


# ---------- 1-3 判死不收钥匙 ----------

def test_timeout_failed_keeps_session_token(monkeypatch):
    from src.runtime import service
    calls = []
    import src.api.auth_token as auth_token
    monkeypatch.setattr(auth_token, "revoke_session_token", lambda tid: calls.append(tid))

    _seed_task("t_1071_token_to", "running", started_at=_iso(-60))
    service.update_task_status("t_1071_token_to", "timeout_failed", exit_code=-1)
    assert calls == [], f"timeout_failed 不该收钥匙，实际 revoke 了 {calls}"

    _seed_task("t_1071_token_fail", "running", started_at=_iso(-60))
    service.update_task_status("t_1071_token_fail", "failed", exit_code=1)
    assert calls == ["t_1071_token_fail"], f"真失败必须收钥匙，实际 {calls}"


# ---------- 1-2 回炉守卫 ----------

def test_requeue_skips_running_task_with_recent_activity():
    from src.runtime import service
    tid = "t_1071_requeue_alive"
    _seed_task(tid, "running", started_at=_iso(-600))
    _seed_message(tid, _iso(-30))
    assert service.requeue_task(tid) is False, "活着的任务不该被回炉"
    row = _status(tid)
    assert row[0] == "running"
    assert row[1] == _iso(-600) or row[1] is not None, "started_at（=run_token）不该被改写"


def test_requeue_still_works_when_no_recent_activity():
    from src.runtime import service
    tid = "t_1071_requeue_idle"
    _seed_task(tid, "running", started_at=_iso(-7200))
    _seed_message(tid, _iso(-7000))  # 窗口外（默认 600s）
    assert service.requeue_task(tid) is True, "超过窗口无产出必须照旧回炉（防悬空卡死）"
    row = _status(tid)
    assert row[0] == "queued"
    assert row[1] is None, "回炉后 started_at 应清空，等新认领写新 token"


def test_worker_completes_after_restart_without_409():
    """全链路：停机置 pending_self_report → worker 持续产出 → 不判死 → 正常 complete 收口。

    这条是主人那个问题的正面答案：重启后还在跑的 worker 不该被 1800s 判死，
    判死后也不该被 409/401 挡住交付。
    """
    from src.runtime import service
    from src.runtime.scheduler import _sweep_pending_self_report_timeouts
    tid = "t_1071_full_chain"
    evt = 91070
    _seed_event(evt, "running")
    # 停机瞬间：terminate_active_agents 写的就是这两列
    _seed_task(tid, "pending_self_report", pending_since=_iso(-2400), started_at=_iso(-3000),
               event_seq=evt)
    _seed_message(tid, _iso(-45))  # worker 还在跑（45 秒前刚写日志）
    _sweep_pending_self_report_timeouts()
    assert _status(tid)[0] == "pending_self_report", "重启 30 分钟后活着的任务被判死了"

    # worker 干完活自报（pending_self_report 属活动态，补报守卫放行）
    from src.session_manager import db, db_lock
    with db_lock:
        tok = db.cursor().execute("SELECT started_at FROM agent_tasks WHERE id=?", (tid,)).fetchone()[0]
    assert service.validate_run_token(service.get_task(tid), tok) == "ok"
    assert service.finalize_by_stream_end(tid, STRUCTURED, 0) is True
    assert _status(tid)[0] == "completed"
    with db_lock:
        assert db.cursor().execute("SELECT status FROM events WHERE seq=?", (evt,)).fetchone()[0] == "completed"


# ---------- 2-1 迟到结果复活 ----------

def test_revive_late_structured_result():
    """判死 10 分钟后 worker 交出完整结构化收尾 → 复活为 completed，事件侧同步改判。"""
    from src.runtime import service
    tid = "t_1071_revive"
    evt = 91071
    _seed_event(evt, "failed")
    _seed_task(tid, "timeout_failed", pending_since=_iso(-2400), started_at=_iso(-3000),
               completed_at=_iso(-600),
               errors=TIMEOUT_ERRORS,
               event_seq=evt)
    ok = service.finalize_by_stream_end(tid, STRUCTURED, 0)
    assert ok is True, "迟到结构化结果应被复活"
    row = _status(tid)
    assert row[0] == "completed", f"复活后应为 completed: {row}"
    assert row[3], "revived_at 必须落痕（只复活一次）"
    from src.session_manager import db, db_lock
    with db_lock:
        ev_status = db.cursor().execute("SELECT status FROM events WHERE seq=?", (evt,)).fetchone()[0]
    assert ev_status == "completed", f"事件侧应改判 completed，实际 {ev_status}"


def test_revive_rejects_business_failure():
    """业务失败（不是兜底超时）永远不复活——seq#2488 的「失败被成功覆盖」不重开。"""
    from src.runtime import service
    tid = "t_1071_revive_biz"
    evt = 91072
    _seed_event(evt, "failed")
    _seed_task(tid, "timeout_failed", completed_at=_iso(-600), event_seq=evt,
               errors=json.dumps([{"error_code": "BUILD_FAILED", "note": "编译失败"}],
                                 ensure_ascii=False))
    assert service.revive_timeout_task(tid, STRUCTURED, 0) is False
    assert _status(tid)[0] == "timeout_failed"


def test_revive_rejects_unstructured_result_and_stale_window():
    from src.runtime import service
    evt = 91073
    _seed_event(evt, "failed")
    marker = TIMEOUT_ERRORS

    tid_noout = "t_1071_revive_noout"
    _seed_task(tid_noout, "timeout_failed", completed_at=_iso(-600), event_seq=evt, errors=marker)
    assert service.revive_timeout_task(tid_noout, "只是普通文本，没有结构化收尾", 0) is False

    tid_old = "t_1071_revive_old"
    _seed_task(tid_old, "timeout_failed", completed_at=_iso(-7200), event_seq=evt, errors=marker)
    assert service.revive_timeout_task(tid_old, STRUCTURED, 0) is False, "超出复活窗口不该复活"


def test_revive_only_once():
    from src.runtime import service
    evt = 91074
    _seed_event(evt, "failed")
    tid = "t_1071_revive_once"
    _seed_task(tid, "timeout_failed", completed_at=_iso(-600), event_seq=evt,
               errors=TIMEOUT_ERRORS)
    assert service.revive_timeout_task(tid, STRUCTURED, 0) is True
    # 状态已 completed → 再调不复活
    assert service.revive_timeout_task(tid, STRUCTURED, 0) is False

    # 已落 revived_at 的行（重复提交/重放）也不复活
    tid2 = "t_1071_revive_marked"
    _seed_task(tid2, "timeout_failed", completed_at=_iso(-600), event_seq=evt,
               revived_at=_iso(-300),
               errors=TIMEOUT_ERRORS)
    assert service.revive_timeout_task(tid2, STRUCTURED, 0) is False


def test_event_terminal_guard_unaffected_without_flag():
    """不带窄口子参数时，failed → completed 仍被拒绝（回归 seq#2488 保护）。"""
    from src.api.event_store import update_event_complete
    evt = 91075
    _seed_event(evt, "failed")
    update_event_complete(evt, "completed", exit_code=0, completed_at=_iso(), result="attempt")
    from src.session_manager import db, db_lock
    with db_lock:
        st = db.cursor().execute("SELECT status FROM events WHERE seq=?", (evt,)).fetchone()[0]
    assert st == "failed", "正常 complete 路径不得覆盖 failed 事件"