"""Issue #297 回归测试：Chat 多图发送与持久化。

根因：多图 base64 请求体超 nginx client_max_body_size → 413 HTML → 请求从未到达后端 →
消息不落库（F5 丢失）。后端 submit 多图链路本身已支持（_save_chat_images JSON 数组解析、
[chat_img:] marker、agent_fn finally 落库），本测试锁定该行为不回归。

覆盖：
  1. _save_chat_images：2 张 data URL JSON 数组 → 2 个 URL + 文件落盘
  2. _build_image_markers：多 URL → 换行拼接 marker
  3. POST /chat/submit 多图：用户消息含 2 个 [chat_img:] marker + task_id/generate_status=1；
     agent 回复落库（generate_status=2）；GET /chat/messages/{cid} 可恢复（模拟 F5）
"""
import base64
import json
import os
import shutil
import threading
import time
import uuid

import pytest

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.session_manager import db, db_lock
from src.api.routes.chat import _save_chat_images, _build_image_markers

UID = 99097
USERNAME = "chat_multi_img_297"

# 1x1 红色 PNG
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _two_images_payload():
    return json.dumps([
        "data:image/png;base64," + base64.b64encode(_TINY_PNG).decode(),
        "data:image/png;base64," + base64.b64encode(_TINY_PNG).decode(),
    ])


@pytest.fixture(autouse=True)
def _client():
    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    from fastapi.testclient import TestClient
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    with db_lock:
        cur = db.cursor()
        cur.execute("DELETE FROM chat_messages WHERE conversation_id LIKE 'cimg297-%'")
        cur.execute("DELETE FROM chat_conversations WHERE user_id=?", (UID,))
        cur.execute("DELETE FROM agent_runtimes WHERE display_name='chat_297_local'")
        db.commit()
    shutil.rmtree(os.path.join("data", "chat_images"), ignore_errors=True)


def _local_engine_id():
    with db_lock:
        c = db.cursor()
        row = c.execute("SELECT id FROM agent_runtimes WHERE credential_id=0 AND lower(name)='opencode' AND healthy=1").fetchone()
        if row:
            return row[0]
        c.execute("INSERT INTO agent_runtimes (name, display_name, credential_id, installed, healthy, status, scope, owner_id) "
                  "VALUES ('opencode','chat_297_local',0,1,1,'online','global','')")
        db.commit()
        return c.lastrowid


def _fake_stream_agent(prompt, cwd, session_id="", **kw):
    yield ("text", "多图回复OK")
    yield ("done", {"exit_code": 0, "text": "多图回复OK", "oc_session_id": ""})


class TestSaveChatImages:
    def test_multi_image_saves_two_files(self):
        conv_id = "cimg297-" + uuid.uuid4().hex[:6]
        urls = _save_chat_images(_two_images_payload(), conv_id)
        assert len(urls) == 2, f"expect 2 urls, got {urls}"
        img_dir = os.path.join("data", "chat_images", conv_id)
        for u in urls:
            assert u.startswith(f"/api/chat/image/{conv_id}/")
            fname = u.rsplit("/", 1)[-1]
            assert os.path.isfile(os.path.join(img_dir, fname))

    def test_empty_and_invalid(self):
        assert _save_chat_images("", "x") == []
        assert _save_chat_images("not-base64!!", "cimg297-inv") == []

    def test_markers_join(self):
        s = _build_image_markers(["/a.png", "/b.png"])
        assert "[chat_img:/a.png]" in s and "[chat_img:/b.png]" in s


class TestSubmitMultiImage:
    def test_submit_persists_user_images_and_agent_reply(self, _client):
        """多图 submit：用户消息（2 个 marker）在 submit 阶段落库，agent 回复 finally 落库，
        /chat/messages 可恢复（模拟 F5 刷新恢复链路）。"""
        rid = _local_engine_id()
        cid = "cimg297-" + uuid.uuid4().hex[:6]
        with db_lock:
            db.cursor().execute("INSERT INTO chat_conversations (id, user_id, repo_key) VALUES (?,?,'')", (cid, UID))
            db.commit()

        with _patch_chat(rid):
            r = _client.post("/api/chat/submit", json={
                "message": "看下这两张图",
                "conversation_id": cid,
                "image_data": _two_images_payload(),
            })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] in ("running",)
        assert data["conversation_id"] == cid
        task_id = data["task_id"]
        assert task_id.startswith("chat_")

        # 等待后台 agent_fn finally 落库（ChatTaskManager daemon 线程）
        _wait_agent_reply(cid, timeout=10, task_id=task_id)

        # 模拟 F5：重新拉取消息
        r2 = _client.get(f"/api/chat/messages/{cid}")
        assert r2.status_code == 200
        msgs = r2.json()["messages"]
        user_msgs = [m for m in msgs if m["role"] == "user"]
        agent_msgs = [m for m in msgs if m["role"] == "agent"]
        assert user_msgs, f"no user message: {msgs}"
        assert user_msgs[-1]["content"].count("[chat_img:") == 2, \
            f"expect 2 image markers, got: {user_msgs[-1]['content']!r}"
        assert user_msgs[-1].get("task_id") == task_id
        assert any("多图回复OK" in (m["content"] or "") for m in agent_msgs), \
            f"agent reply not persisted: {msgs}"

    def test_submit_image_with_text_and_single_image_no_regression(self, _client):
        """图+文字 & 单图回归：marker 数量与消息文案正确。"""
        rid = _local_engine_id()
        cid = "cimg297-" + uuid.uuid4().hex[:6]
        with db_lock:
            db.cursor().execute("INSERT INTO chat_conversations (id, user_id, repo_key) VALUES (?,?,'')", (cid, UID))
            db.commit()

        with _patch_chat(rid):
            single = json.dumps(["data:image/png;base64," + base64.b64encode(_TINY_PNG).decode()])
            r = _client.post("/api/chat/submit", json={
                "message": "单图+文字",
                "conversation_id": cid,
                "image_data": single,
            })
        assert r.status_code == 200
        task_id = r.json()["task_id"]
        _wait_agent_reply(cid, timeout=10, task_id=task_id)
        with db_lock:
            rows = db.cursor().execute(
                "SELECT role, content FROM chat_messages WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()
        user_content = [c for role, c in rows if role == "user"][-1]
        assert "单图+文字" in user_content
        assert user_content.count("[chat_img:") == 1
        assert any("多图回复OK" in (c or "") for role, c in rows if role == "agent")


def _patch_chat(rid):
    from unittest.mock import patch
    return _multi_patch(rid)


class _multi_patch:
    def __init__(self, rid):
        self.rid = rid

    def __enter__(self):
        from unittest.mock import patch
        self._cms = [
            patch("src.api.routes.chat._get_chat_config",
                  return_value={"backend": "opencode", "runtime_id": self.rid}),
            patch("src.runtime.executor.stream_chat_agent", side_effect=_fake_stream_agent),
            patch("src.api.routes.chat._understand_image", return_value="[图片描述]"),
            patch("src.api.routes.chat._push_reply_to_feishu", new=self._no_push),
        ]
        for cm in self._cms:
            cm.start()
        return self

    async def _no_push(self, *a, **kw):
        return None

    def __exit__(self, *exc):
        for cm in self._cms:
            cm.stop()
        return False


def _wait_agent_reply(cid, timeout=10, task_id=None):
    """等待后台 agent_fn 线程完全收尾：agent 消息落库 + ChatTaskManager 任务终态。

    双条件避免 teardown 时线程仍持有 db_lock 写库，干扰后续测试（偶发 database is locked）。
    """
    from src.api.chat_task_manager import ChatTaskManager
    deadline = time.time() + timeout
    reply_ok = False
    task_ok = (task_id is None)
    while time.time() < deadline:
        if not reply_ok:
            with db_lock:
                row = db.cursor().execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE conversation_id=? AND role='agent'", (cid,)).fetchone()
            if row and row[0] >= 1:
                reply_ok = True
        if not task_ok:
            with ChatTaskManager._lock:
                t = ChatTaskManager._tasks.get(task_id)
                task_ok = bool(t and t.status != "running")
        if reply_ok and task_ok:
            return True
        time.sleep(0.1)
    return reply_ok and task_ok
