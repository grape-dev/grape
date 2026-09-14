"""任务状态 processing 触发点测试（task-status-processing-trigger-fix.md T01-T07）。

web /send、/send/stream 与飞书 handler 均调用同一 helper `mark_task_processing_by_conversation`，
此处以单元级覆盖其语义。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock, create_conversation, add_message, mark_task_processing_by_conversation


def _clean():
    with db_lock:
        c = db.cursor()
        for t in ("tasks", "chat_conversations", "chat_messages"):
            try:
                c.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        db.commit()


def _mk_task(issue_number, status="active", user_id=1, conv_id="", repo="gitcode:owner/repo"):
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, conversation_id) VALUES (?,?,?,?,?,?)",
                  (user_id, "alice", repo, issue_number, status, conv_id))
        db.commit()
        return c.lastrowid


def _task_status(tid):
    with db_lock:
        row = db.cursor().execute("SELECT status FROM issue WHERE id=?", (tid,)).fetchone()
        return row[0] if row else None


def test_task_conversation_message_flips_to_processing():
    """T01-T03：任务会话收到真实消息 → active→processing（web/飞书共用 helper）"""
    _clean()
    cid = create_conversation(1, "gitcode:owner/repo", title="Issue #1", conv_type="task", issue_number=1)
    tid = _mk_task(1, conv_id=cid)
    add_message(cid, "user", "开始处理")
    mark_task_processing_by_conversation(cid)
    assert _task_status(tid) == "processing"
    _clean()


def test_task_conversation_fallback_by_repo_issue():
    """会话未关联任务（conversation_id 空）→ 按 repo+issue 回退翻转"""
    _clean()
    cid = create_conversation(1, "gitcode:owner/repo", title="Issue #2", conv_type="task", issue_number=2)
    tid = _mk_task(2)   # conversation_id 空
    add_message(cid, "user", "hi")
    mark_task_processing_by_conversation(cid)
    assert _task_status(tid) == "processing"
    _clean()


def test_already_processing_unchanged():
    """T04：已 processing 再发消息不变"""
    _clean()
    cid = create_conversation(1, "gitcode:owner/repo", title="Issue #3", conv_type="task", issue_number=3)
    tid = _mk_task(3, status="processing", conv_id=cid)
    mark_task_processing_by_conversation(cid)
    assert _task_status(tid) == "processing"
    _clean()


def test_completed_not_reverted():
    """T05：终态不回翻"""
    _clean()
    cid = create_conversation(1, "gitcode:owner/repo", title="Issue #4", conv_type="task", issue_number=4)
    tid = _mk_task(4, status="completed", conv_id=cid)
    mark_task_processing_by_conversation(cid)
    assert _task_status(tid) == "completed"
    _clean()


def test_not_owner_task_not_flipped():
    """T06：非本人任务不翻转"""
    _clean()
    cid = create_conversation(1, "gitcode:owner/repo", title="Issue #5", conv_type="task", issue_number=5)
    tid = _mk_task(5, user_id=2, conv_id=cid)
    mark_task_processing_by_conversation(cid)
    assert _task_status(tid) == "active"
    _clean()


def test_general_conversation_no_effect():
    """T07：普通会话不影响任务"""
    _clean()
    cid = create_conversation(1, "gitcode:owner/repo", title="普通")
    tid = _mk_task(6)
    mark_task_processing_by_conversation(cid)
    assert _task_status(tid) == "active"
    _clean()
