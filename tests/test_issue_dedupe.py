"""issue 卡片去重测试（2026-10-06）

ensure_issue_task / _ensure_assignee_task_card 改为按 (repo_key, issue_number) 复用任何状态卡，
避免 awaiting_input/completed/cancelled 时后续事件另建新卡 → 同一平台 issue 多卡。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.db_setup import init_database
from src.session_manager import db, db_lock
from src.api.issues_center import ensure_issue_task, _ensure_assignee_task_card

init_database()

REPO = "gitcode:org/dup"
NUM = 77


def _count():
    with db_lock:
        return db.cursor().execute(
            "SELECT COUNT(*) FROM issue WHERE repo_key=? AND issue_number=?", (REPO, NUM)).fetchone()[0]


def _seed(status, assignee="", user_id=0, issue_number=None):
    num = issue_number or NUM
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO, num))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, task_description, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, assignee, REPO, num, "t", "d", status))
        id_ = c.lastrowid
        db.commit()
        return id_


def _row():
    with db_lock:
        return db.cursor().execute(
            "SELECT id, status, assignee, user_id FROM issue WHERE repo_key=? AND issue_number=?",
            (REPO, NUM)).fetchone()


def test_ensure_issue_task_reuses_awaiting_card():
    cid = _seed("awaiting_input")
    task_id = ensure_issue_task(REPO, NUM, title="新标题", assignee="alice")
    assert task_id == cid
    assert _count() == 1
    assert _row()[1] == "active"  # 复用终态/待澄清卡 → 重新武装为 active


def test_ensure_issue_task_reuses_completed_card():
    cid = _seed("completed")
    task_id = ensure_issue_task(REPO, NUM, title="再激活", assignee="alice")
    assert task_id == cid
    assert _count() == 1
    assert _row()[1] == "active"


def test_ensure_issue_task_reuses_processing_card_without_flip():
    cid = _seed("processing")
    task_id = ensure_issue_task(REPO, NUM, title="同名", assignee="alice")
    assert task_id == cid
    assert _count() == 1
    assert _row()[1] == "processing"  # 进行中卡不动


def test_ensure_issue_task_creates_when_absent():
    _seed("active")  # 清掉后重新从零（测试库已建唯一索引，无法插重复）
    with db_lock:
        db.cursor().execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO, NUM))
        db.commit()
    task_id = ensure_issue_task(REPO, NUM, title="新建", assignee="alice")
    assert task_id is not None
    assert _count() == 1


def test_notify_card_reuses_any_status_and_syncs_assignee():
    cid = _seed("processing", assignee="bob", user_id=3)
    got = _ensure_assignee_task_card(9, "alice", REPO, NUM, "t", "d")
    assert got == cid
    assert _count() == 1
    st = _row()
    assert st[2] == "alice"
    assert st[3] == 9