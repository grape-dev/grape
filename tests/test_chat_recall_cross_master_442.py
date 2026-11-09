# -*- coding: utf-8 -*-
"""Issue #442 跨主控撤回兜底（双主场景）回归测试。

背景：ChatTaskManager 与 _CHAT_ENQUEUE_MAP 都是**进程内**状态；nginx 轮询下撤回请求
可能打到另一台主控 → 本地 cancel 不会触发 → 在跑的引擎照跑（只丢输出、白烧算力）。

修复后由**任务归属主控**自己感知共享库里的撤回标记并终止：
- direct 模式：生成循环内逐事件复查撤回标记 → 立即 break（终止生成）
- remote 模式：轮询循环内复查 → service.cancel_task(enqueue_tid) → worker 心跳杀引擎
"""
import time
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.session_manager import db, db_lock, add_message, get_message

UID = 99011
USERNAME = "recall_xmaster_tester"


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
                    if tk.conv_id and tk.conv_id.startswith("rcxm-")]:
            del ChatTaskManager._tasks[tid]
    from src.api.routes.chat import _CHAT_ENQUEUE_MAP
    _CHAT_ENQUEUE_MAP.clear()
    with db_lock:
        cur = db.cursor()
        cur.execute("DELETE FROM chat_messages WHERE conversation_id LIKE 'rcxm-%'")
        cur.execute("DELETE FROM chat_conversations WHERE user_id=?", (UID,))
        cur.execute("DELETE FROM agent_runtimes WHERE display_name='rcxm_local'")
        db.commit()


def _local_engine_id():
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id FROM agent_runtimes WHERE credential_id=0 AND lower(name)='opencode' AND healthy=1").fetchone()
        if row:
            return row[0]
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                  "VALUES ('opencode','rcxm_local',0,1,1,'online','global','')")
        db.commit()
        return c.lastrowid


def _new_conv():
    cid = "rcxm-" + uuid.uuid4().hex[:8]
    with db_lock:
        db.cursor().execute("INSERT INTO chat_conversations (id, user_id, repo_key) VALUES (?,?,'')", (cid, UID))
        db.commit()
    return cid


class _no_push:
    async def __call__(self, *a, **kw):
        return None


def _long_stream_agent(prompt, cwd, session_id="", **kw):
    """长事件流（200×0.05s ≈ 10s）：用来证明"感知撤回后立即中断"（而非跑完）。"""
    for i in range(200):
        time.sleep(0.05)
        yield ("text", f"chunk{i}")
    yield ("done", {"exit_code": 0, "text": "最终回复", "oc_session_id": ""})


def _wait(cond, timeout=10, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


class TestCrossMasterRecall:
    def test_direct_mode_aborts_generation_when_recalled(self, _client):
        """direct 模式：撤回标记写在共享库 → 生成循环感知后立即中断（不再跑完全部事件）。"""
        from src.api.chat_task_manager import ChatTaskManager
        rid = _local_engine_id()
        cid = _new_conv()
        with patch("src.api.routes.chat._get_chat_config",
                   return_value={"backend": "opencode", "runtime_id": rid}), \
             patch("src.runtime.executor.stream_chat_agent", side_effect=_long_stream_agent), \
             patch("src.api.routes.chat._push_reply_to_feishu", new=_no_push()):
            r = _client.post("/api/chat/submit", json={"message": "长回答", "conversation_id": cid})
            assert r.status_code == 200, r.text
            d = r.json()
            task_id, mid = d["task_id"], d["message_id"]

            assert _wait(lambda: (ChatTaskManager.get_status(task_id) or {}).get("status") == "running", timeout=5)

            # 模拟“另一台主控”的撤回：直接写共享库的撤回标记（本进程内存状态完全不变，
            # 即本进程 ChatTaskManager 根本不知道撤回过）
            t0 = time.time()
            from src.session_manager import recall_message
            recall_message(mid)

            def _finished():
                with ChatTaskManager._lock:
                    t = ChatTaskManager._tasks.get(task_id)
                    return bool(t and t.finished_at > 0)
            assert _wait(_finished, timeout=5), "生成循环应感知撤回并结束"
            elapsed = time.time() - t0
            # 事件流总长约 10s：若没有逐事件复查，至少要跑完剩余事件（>5s）
            assert elapsed < 3.0, f"应在感知撤回后立即中断，实际耗时 {elapsed:.1f}s"

        # 撤回消息本身（模拟端）已软删除；且不会写入 agent 回复
        assert get_message(mid)["deleted"] == 1
        with db_lock:
            n = db.cursor().execute(
                "SELECT COUNT(*) FROM chat_messages WHERE conversation_id=? AND role='agent'",
                (cid,)).fetchone()[0]
        assert n == 0, "撤回后不得落库 agent 回复"

    def test_remote_mode_owner_loop_cancels_task_when_recalled(self, _client):
        """remote 模式（跨主控）：归属主控的轮询循环感知撤回 → service.cancel_task(enqueue_tid)。"""
        cid = _new_conv()
        with patch("src.api.routes.chat._get_chat_config",
                   return_value={"backend": "opencode", "runtime_id": 4321}), \
             patch("src.api.routes.chat._push_reply_to_feishu", new=_no_push()), \
             patch("src.runtime.service.runtime_execution_kind", return_value="remote"), \
             patch("src.runtime.service.runtime_credential_id", return_value=777), \
             patch("src.runtime.service._remote_runtime_ready", return_value=True), \
             patch("src.runtime.service.enqueue_task", return_value="t_rcxm12345"), \
             patch("src.runtime.service.get_task", return_value=None), \
             patch("src.runtime.service.cancel_task", return_value=True) as mock_cancel, \
             patch("src.api.routes.chat.is_task_recalled", return_value=True):
            r = _client.post("/api/chat/submit", json={"message": "远端长任务", "conversation_id": cid})
            assert r.status_code == 200, r.text

            # 归属主控的轮询循环应在 0.5s 周期内感知并终止远端任务
            assert _wait(lambda: mock_cancel.called, timeout=5), \
                "跨主控撤回必须由任务归属主控的轮询循环终止远端任务"
            mock_cancel.assert_any_call("t_rcxm12345")

        # 已撤回 → 不落库 agent 回复
        with db_lock:
            n = db.cursor().execute(
                "SELECT COUNT(*) FROM chat_messages WHERE conversation_id=? AND role='agent'",
                (cid,)).fetchone()[0]
        assert n == 0

    def test_cross_master_map_miss_is_logged(self, _client):
        """跨主控撤回（本机无映射）必须留下可排障的日志锚点。

        真跑发现：该日志原被写在 `if enqueue_tid:` 块内，而跨主控恰恰是 enqueue_tid 为空
        的分支 → 永远不会打印（死代码）。本用例锁死该行为。
        """
        from unittest.mock import MagicMock
        from src.api.routes import chat as chat_mod

        cid = _new_conv()
        tid = "chat_" + uuid.uuid4().hex[:8]
        mid = add_message(cid, "user", "跨主控撤回", task_id=tid)
        fake_log = MagicMock()
        with patch.object(chat_mod, "log", fake_log):
            r = _client.post(f"/api/chat/messages/{mid}/recall")
        assert r.status_code == 200
        # 本机无映射 → 无法本地终止（terminated=False），但必须打印跨主控锚点
        assert r.json()["terminated"] is False
        msgs = [str(c.args[0]) for c in fake_log.info.call_args_list if c.args]
        assert any("cross-master recall" in m for m in msgs), f"缺少跨主控日志锚点: {msgs}"