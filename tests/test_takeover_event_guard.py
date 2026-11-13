"""接管态的事件保护：worker 迟到上报不得改写 takeover。

真机事故（2026-11-10）：点接管 → cancel_task + mark_task_takeover（事件 status=takeover）
→ 4 秒后 worker 对已取消任务上报 fail → 事件被改写成 failed，流水线显示"失败"，
且 finish 会因为「failed 不能被 completed 覆盖」而写不进去。

修法两层：
  ① service.fail_task：task 已 cancelled → 直接跳过（cancel 后的失败上报是必然噪音）
  ② event_store.update_event_complete：事件为 takeover 时，只有接管服务
     （allow_from_takeover=True）能落终态
"""
import time
from datetime import datetime, timezone

import pytest

from src.api import event_store as es
from src.runtime import service as svc

REPO = "gitcode:takeoverguard/repo"
ISSUE = "#9"
SEQ = 995001


def _seed(task_status="cancelled", event_status="takeover"):
    with svc.db_lock:
        c = svc.db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id='t_guard'")
        c.execute("DELETE FROM events WHERE seq=?", (SEQ,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute(
            "INSERT INTO issue (user_id, repo_key, issue_number, title, status, stage, owner_agent, "
            "scene_id, assignee) VALUES (1,?,9,'守卫测试','processing','coding','coding','default','')",
            (REPO,))
        c.execute(
            "INSERT INTO events (seq, repo_key, event_type, issue_id, agent_type, status, source, title, payload) "
            "VALUES (?,?,'a2a_event',?,'coding',?,'a2a','t','{}')",
            (SEQ, REPO, ISSUE, event_status))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, status, agent_type, skill, prompt, "
            "env_extra, cwd, event_seq, repo_key, issue_id, user_id, account, skill_files, created_at) "
            "VALUES ('t_guard','ses_g',0,?,'coding','implementation','p','{}','/tmp/g',?,?,'#9',1,'','{}',?)",
            (task_status, SEQ, REPO, datetime.now(timezone.utc).isoformat()))
        svc.db.commit()


def _status() -> str:
    with svc.db_lock:
        c = svc.db.cursor()
        c.execute("SELECT status FROM events WHERE seq=?", (SEQ,))
        return c.fetchone()[0]


@pytest.fixture(autouse=True)
def _clean():
    _seed()
    yield
    with svc.db_lock:
        c = svc.db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id='t_guard'")
        c.execute("DELETE FROM events WHERE seq=?", (SEQ,))
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        svc.db.commit()


def test_fail_task_on_cancelled_does_not_clobber_takeover():
    """worker 对已取消任务的 fail 上报必须被丢弃，事件保持 takeover。"""
    svc.fail_task("t_guard", "process killed (cancelled by user)")
    assert _status() == "takeover"


def test_update_event_complete_blocks_worker_overwrite_of_takeover():
    """非接管调用方（worker complete/fail）不能改写 takeover。"""
    es.update_event_complete(SEQ, "failed", result="worker late report")
    assert _status() == "takeover"
    es.update_event_complete(SEQ, "completed", result="worker late report")
    assert _status() == "takeover"


def test_takeover_service_can_finalize():
    """接管服务自己（finish/abort）可以落终态。"""
    es.update_event_complete(SEQ, "completed", result="human done", allow_from_takeover=True)
    assert _status() == "completed"

    _seed(event_status="takeover", task_status="cancelled")
    es.update_event_complete(SEQ, "cancelled", result="human gave up", allow_from_takeover=True)
    assert _status() == "cancelled"


def test_list_expires_stale_active_sessions():
    """token 过期的 active 会话要自动转 expired，否则面板永远显示"接管中"。"""
    from src.runtime import takeover as tk
    with svc.db_lock:
        c = svc.db.cursor()
        c.execute("DELETE FROM takeover_sessions WHERE id='tkos_guard_stale'")
        c.execute(
            "INSERT INTO takeover_sessions (id, active_key, task_id, event_seq, repo_key, issue_id, "
            "agent_type, user_id, username, status, token_sha256, token_expires_at, token_issued_at) "
            "VALUES ('tkos_guard_stale','t_guard', 't_guard', ?, ?, '#9', 'coding', 1, 'tester', "
            "'active', 'x', 1, 1)",
            (SEQ, REPO))
        svc.db.commit()
    try:
        assert tk.list_takeovers(REPO, "#9", status="active") == []
        with svc.db_lock:
            c = svc.db.cursor()
            c.execute("SELECT status, active_key FROM takeover_sessions WHERE id='tkos_guard_stale'")
            assert c.fetchone() == ("expired", None)
    finally:
        with svc.db_lock:
            c = svc.db.cursor()
            c.execute("DELETE FROM takeover_sessions WHERE id='tkos_guard_stale'")
            svc.db.commit()
