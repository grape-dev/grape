"""Issue #4183: 任务会话 get-or-create 存在性判断脱离 LIMIT 50 窗口。

覆盖：
1. 老 Issue 的 task 会话滑出「最近 50 条」窗口后仍能按业务键命中（不撞 1062）。
2. `create_conversation` 的 1062 兜底使用业务键查询而非窗口重查。
3. 1062 后仍查不到 → 自定义异常 + 可观测指标（不再只是 WARNING）。
4. 并发 ensure 同一 Issue → 同一 conv_id、单行、无 ensure failed 告警。
5. general 会话不受唯一索引/get-or-create 影响。
"""
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from prometheus_client import CollectorRegistry

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api import issues_center as ic
from src.kernel import session_manager as sm
from src.kernel import task_conversation_metrics as tcm

REPO = "gitcode:owner/repo4183"
USER_ID = 84183


def _cleanup():
    with sm.db_lock:
        c = sm.db.cursor()
        c.execute("DELETE FROM chat_messages WHERE conversation_id IN "
                  "(SELECT id FROM chat_conversations WHERE user_id=?)", (USER_ID,))
        c.execute("DELETE FROM chat_conversations WHERE user_id=?", (USER_ID,))
        sm.db.commit()


def setup_function():
    _cleanup()


def teardown_function():
    _cleanup()
    tcm.reset()


def _task_rows(issue_number):
    with sm.db_lock:
        c = sm.db.cursor()
        c.execute("SELECT id, title FROM chat_conversations WHERE user_id=? AND repo_key=? "
                  "AND conv_type='task' AND issue_number=?",
                  (USER_ID, REPO, issue_number))
        return c.fetchall()


def _bump_window(old_id="", count=60):
    """造 >=50 条更新的 general 会话，把更早的 task 会话挤出 LIMIT 50 窗口。"""
    if old_id:
        with sm.db_lock:
            c = sm.db.cursor()
            c.execute("UPDATE chat_conversations SET updated_at='2000-01-01 00:00:00' "
                      "WHERE id=?", (old_id,))
            sm.db.commit()
    for i in range(count):
        sm.create_conversation(USER_ID, REPO, title=f"general-{i}", conv_type="general")


def test_old_issue_beyond_window_reuses_task_conversation():
    old_id = sm.create_conversation(USER_ID, REPO, title="Issue #100 · 老需求",
                                    conv_type="task", issue_number=100)
    _bump_window(old_id)
    # 窗口内确实看不到该 task 会话（复现根因前提）
    window = sm.get_user_conversations(USER_ID, REPO, limit=50)
    assert old_id not in [c["id"] for c in window]

    conv_id = ic._ensure_task_conversation(USER_ID, REPO, 100, title="老需求")
    assert conv_id == old_id
    assert len(_task_rows(100)) == 1


def test_duplicate_fallback_finds_old_conversation_by_business_key():
    old_id = sm.create_conversation(USER_ID, REPO, title="Issue #200 · 老需求",
                                    conv_type="task", issue_number=200)
    _bump_window(old_id)

    def _dup_insert(name, params=()):
        if name == "conversation_insert":
            raise RuntimeError(
                "(1062, \"Duplicate entry '84183-gitcode:owner/repo4183-200-task' "
                "for key 'chat_conversations.idx_chat_conv_repo_issue'\")")
        return real_exec(name, params)

    real_exec = sm._im.exec_any
    sm._im.exec_any = _dup_insert
    try:
        conv_id = sm.create_conversation(USER_ID, REPO, title="Issue #200 · 老需求",
                                         conv_type="task", issue_number=200)
        assert conv_id == old_id
    finally:
        sm._im.exec_any = real_exec
    assert len(_task_rows(200)) == 1


def test_duplicate_unresolved_raises_and_counts_metric(caplog):
    reg = CollectorRegistry()
    assert tcm.install(reg) is True
    assert tcm.install(reg) is False

    def _dup_insert(name, params=()):
        if name == "conversation_insert":
            raise RuntimeError("(1062, \"Duplicate entry 'x' for key "
                               "'chat_conversations.idx_chat_conv_repo_issue'\")")
        return real_exec(name, params)

    real_exec = sm._im.exec_any
    sm._im.exec_any = _dup_insert
    try:
        with caplog.at_level(logging.ERROR):
            conv_id = ic._ensure_task_conversation(USER_ID, REPO, 300, title="不一致")
    finally:
        sm._im.exec_any = real_exec

    assert conv_id == ""
    assert "Task conversation ensure unresolved after duplicate" in caplog.text
    assert "Task conversation ensure failed" not in caplog.text
    assert reg.get_sample_value(
        "grape_task_conversation_ensure_failed_total",
        {"reason": "duplicate_unresolved"}) == 1


def test_concurrent_ensure_same_issue_yields_single_conversation(caplog):
    barrier = threading.Barrier(2)

    def _worker():
        barrier.wait(timeout=5)
        return ic._ensure_task_conversation(USER_ID, REPO, 900, title="并发需求")

    with caplog.at_level(logging.WARNING):
        with ThreadPoolExecutor(max_workers=2) as ex:
            ids = list(ex.map(lambda _: _worker(), range(2)))

    assert all(ids), ids
    assert len(set(ids)) == 1, ids
    assert len(_task_rows(900)) == 1
    assert "Task conversation ensure failed" not in caplog.text


def test_general_conversations_unaffected():
    general_id = sm.create_conversation(USER_ID, REPO, title="一般对话")
    task_id = ic._ensure_task_conversation(USER_ID, REPO, 400, title="任务需求")
    assert task_id and task_id != general_id
    assert len(_task_rows(400)) == 1
    general = sm.get_conversation(general_id)
    assert general["conv_type"] == "general"
    assert general["issue_number"] == 0