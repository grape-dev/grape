"""Issue #442: ChatAgent 消息撤回（二次确认删除 + 终止执行 + 重新编辑）。

覆盖：
- 撤回端点：本人 user 消息软删除 / 他人消息 404 / agent 消息 400 / 不存在 404 / 幂等
- 终止联动（direct 模式）：ChatTaskManager.cancel → 状态 cancelled、后续事件丢弃、
  agent_fn finally 撤回感知跳过落库
- 同轮 agent 回复一并软删除；get_messages 返回 deleted 字段（存量兼容默认 0）
- remote 模式映射：_CHAT_ENQUEUE_MAP → service.cancel_task（#136 复用）
- chat_submit 响应包含 message_id（前端 live 气泡绑定撤回入口）
"""
import time
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.session_manager import (
    db, db_lock, add_message, get_message, get_messages,
)
from src.user import db as user_db

UID = 99001
USERNAME = "recall_tester"


@pytest.fixture(autouse=True)
def _client():
    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    from src.api.chat_task_manager import ChatTaskManager
    with ChatTaskManager._lock:
        for tid in [t for t, tk in ChatTaskManager._tasks.items()
                    if tk.conv_id and tk.conv_id.startswith("rc442-")]:
            del ChatTaskManager._tasks[tid]
    from src.api.routes.chat import _CHAT_ENQUEUE_MAP
    _CHAT_ENQUEUE_MAP.clear()
    with db_lock:
        cur = db.cursor()
        cur.execute("DELETE FROM chat_messages WHERE conversation_id LIKE 'rc442-%'")
        cur.execute("DELETE FROM chat_conversations WHERE user_id IN (?, ?)", (UID, 99002))
        cur.execute("DELETE FROM agent_runtimes WHERE display_name='rc442_local'")
        db.commit()


def _local_engine_id():
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id FROM agent_runtimes WHERE credential_id=0 AND lower(name)='opencode' AND healthy=1").fetchone()
        if row:
            return row[0]
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                  "VALUES ('opencode','rc442_local',0,1,1,'online','global','')")
        db.commit()
        return c.lastrowid


def _new_conv(prefix="rc442"):
    cid = f"{prefix}-" + uuid.uuid4().hex[:8]
    with db_lock:
        db.cursor().execute("INSERT INTO chat_conversations (id, user_id, repo_key) VALUES (?,?,'')", (cid, UID))
        db.commit()
    return cid


class _no_push:
    async def __call__(self, *a, **kw):
        return None


def _slow_stream_agent(prompt, cwd, session_id="", **kw):
    """慢速事件流：撤回前任务必须处于 running。"""
    for i in range(200):
        time.sleep(0.02)
        yield ("text", f"chunk{i}")
    yield ("done", {"exit_code": 0, "text": "最终回复", "oc_session_id": ""})


def _wait(cond, timeout=10, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


# ====== 端点：权限与状态流转 ======


class TestRecallEndpoint:
    def test_recall_own_user_message(self, _client):
        cid = _new_conv()
        mid = add_message(cid, "user", "发错的消息", task_id="chat_test1", generate_status=1)
        r = _client.post(f"/api/chat/messages/{mid}/recall")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["recalled"] is True and d["message_id"] == mid and d["task_id"] == "chat_test1"
        # 软删除：deleted=1 且原文保留（供重新编辑）
        m = get_message(mid)
        assert m["deleted"] == 1 and m["content"] == "发错的消息"
        # 列表接口带 deleted 字段
        msgs = get_messages(cid)
        assert msgs[-1]["id"] == mid and msgs[-1]["deleted"] == 1

    def test_recall_other_users_message_404(self, _client):
        cid = _new_conv()
        # 他人（user_id=99002）会话里的一条消息
        with db_lock:
            cur = db.cursor()
            other_cid = "rc442-other-" + uuid.uuid4().hex[:6]
            cur.execute("INSERT INTO chat_conversations (id, user_id, repo_key) VALUES (?,?,'')",
                        (other_cid, 99002))
            cur.execute("INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)",
                        (other_cid, "user", "他人的消息"))
            mid = cur.lastrowid
            db.commit()
        r = _client.post(f"/api/chat/messages/{mid}/recall")
        assert r.status_code == 404
        assert get_message(mid)["deleted"] == 0

    def test_recall_agent_message_400(self, _client):
        cid = _new_conv()
        mid = add_message(cid, "agent", "agent 回复", task_id="chat_test2", generate_status=2)
        r = _client.post(f"/api/chat/messages/{mid}/recall")
        assert r.status_code == 400
        assert get_message(mid)["deleted"] == 0

    def test_recall_nonexistent_404(self, _client):
        r = _client.post("/api/chat/messages/900001/recall")
        assert r.status_code == 404

    def test_recall_idempotent(self, _client):
        cid = _new_conv()
        mid = add_message(cid, "user", "撤回两次", task_id="chat_test3")
        assert _client.post(f"/api/chat/messages/{mid}/recall").status_code == 200
        r = _client.post(f"/api/chat/messages/{mid}/recall")
        assert r.status_code == 200
        assert r.json()["recalled"] is True and r.json()["terminated"] is False

    def test_recall_soft_deletes_same_turn_agent_reply(self, _client):
        cid = _new_conv()
        tid = "chat_test4"
        umid = add_message(cid, "user", "提问", task_id=tid)
        amid = add_message(cid, "agent", "部分回复", task_id=tid, generate_status=2)
        r = _client.post(f"/api/chat/messages/{umid}/recall")
        assert r.status_code == 200
        assert get_message(umid)["deleted"] == 1
        assert get_message(amid)["deleted"] == 1  # 同轮 agent 回复一并软删除

    def test_deleted_default_zero_for_legacy_rows(self, _client):
        """存量兼容：无 deleted 值插入的旧行默认 0。"""
        cid = _new_conv()
        with db_lock:
            cur = db.cursor()
            cur.execute("INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)",
                        (cid, "user", "存量消息"))
            mid = cur.lastrowid
            db.commit()
        assert get_message(mid)["deleted"] == 0


# ====== 终止联动（direct 模式）======


class TestRecallTermination:
    def test_submit_returns_message_id(self, _client):
        rid = _local_engine_id()
        cid = _new_conv()
        with patch("src.api.routes.chat._get_chat_config",
                   return_value={"backend": "opencode", "runtime_id": rid}), \
             patch("src.runtime.executor.stream_chat_agent", side_effect=_slow_stream_agent), \
             patch("src.api.routes.chat._push_reply_to_feishu", new=_no_push()):
            r = _client.post("/api/chat/submit", json={"message": "hi", "conversation_id": cid})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["message_id"] and get_message(d["message_id"])["role"] == "user"
        # 收尾：终止该任务避免拖慢后续测试
        from src.api.chat_task_manager import ChatTaskManager
        ChatTaskManager.cancel(d["task_id"], reason="test teardown")

    def test_recall_terminates_running_task_and_skips_agent_db_write(self, _client):
        """撤回运行中任务：状态 cancelled、无 agent 回复落库（撤回感知）、消息软删除。"""
        from src.api.chat_task_manager import ChatTaskManager
        rid = _local_engine_id()
        cid = _new_conv()
        with patch("src.api.routes.chat._get_chat_config",
                   return_value={"backend": "opencode", "runtime_id": rid}), \
             patch("src.runtime.executor.stream_chat_agent", side_effect=_slow_stream_agent), \
             patch("src.api.routes.chat._push_reply_to_feishu", new=_no_push()):
            r = _client.post("/api/chat/submit", json={"message": "会撤回的消息", "conversation_id": cid})
        assert r.status_code == 200
        d = r.json()
        task_id, mid = d["task_id"], d["message_id"]

        # 任务处于 running
        assert _wait(lambda: (ChatTaskManager.get_status(task_id) or {}).get("status") == "running", timeout=5)

        # 撤回
        rr = _client.post(f"/api/chat/messages/{mid}/recall")
        assert rr.status_code == 200, rr.text
        assert rr.json()["terminated"] is True

        # 状态 → cancelled
        assert _wait(lambda: (ChatTaskManager.get_status(task_id) or {}).get("status") == "cancelled", timeout=5)

        # 后台线程收尾完成（finished_at 置位）
        def _finished():
            with ChatTaskManager._lock:
                t = ChatTaskManager._tasks.get(task_id)
                return bool(t and t.finished_at > 0)
        assert _wait(_finished, timeout=10)

        # 撤回后 publish/push 事件被丢弃（不再输出状态更新）
        before = len(ChatTaskManager._tasks[task_id].events)
        ChatTaskManager.publish(task_id, "text", "late event")
        assert len(ChatTaskManager._tasks[task_id].events) == before

        # agent 回复不落库（撤回感知 finally）
        def _no_agent_row():
            with db_lock:
                n = db.cursor().execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE conversation_id=? AND role='agent' AND deleted=0",
                    (cid,)).fetchone()[0]
            return n == 0
        assert _wait(_no_agent_row, timeout=5), "agent reply should not be persisted after recall"

        # 用户消息软删除且原文保留
        m = get_message(mid)
        assert m["deleted"] == 1 and m["content"] == "会撤回的消息"

    def test_recall_remote_mode_cancels_enqueued_task(self, _client):
        """远端模式：撤回经 _CHAT_ENQUEUE_MAP → service.cancel_task（复用 #136）。"""
        from src.api.routes.chat import _CHAT_ENQUEUE_MAP
        cid = _new_conv()
        tid = "chat_rc442r"
        mid = add_message(cid, "user", "远端消息", task_id=tid)
        _CHAT_ENQUEUE_MAP[tid] = "999888"
        with patch("src.runtime.service.cancel_task", return_value=True) as mock_cancel:
            r = _client.post(f"/api/chat/messages/{mid}/recall")
        assert r.status_code == 200
        assert r.json()["terminated"] is True
        mock_cancel.assert_called_once_with("999888")


# ====== ChatTaskManager.cancel 单元 ======


class TestChatTaskManagerCancel:
    def test_cancel_external_task_drops_events(self):
        from src.api.chat_task_manager import ChatTaskManager
        tid = "chat_rc442ext"
        cid = _new_conv()
        task = ChatTaskManager.register_idle(tid, cid, UID, USERNAME)
        ChatTaskManager.publish(tid, "text", "before")
        assert ChatTaskManager.cancel(tid, reason="撤回") is True
        assert ChatTaskManager.get_status(tid)["status"] == "cancelled"
        n = len(task.events)
        ChatTaskManager.publish(tid, "text", "after")  # 丢弃
        assert len(task.events) == n
        assert ChatTaskManager.cancel("nonexistent") is False
