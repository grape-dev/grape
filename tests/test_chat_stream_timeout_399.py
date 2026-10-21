"""Issue #399：chat agent SSE 流式超时可配置 + 等待期心跳反馈。

覆盖：
- get_chat_stream_timeout 配置读取（env 常量默认 / server-config.json 动态覆盖 / 非法值回退）
- chat_task_stream DB 轮询分支：等待期间周期性「思考中」心跳 + 超时文案动态显示配置时长
- chat_task_stream SSE 分支：无输出期间心跳 + 总超时动态文案
"""
import asyncio
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import require_no_session_token
from src.session_manager import db, db_lock
import src.config as config_mod

UID = 99099
USERNAME = "chat_timeout_tester"

BASE = "/api/chat"


@pytest.fixture(autouse=True)
def _client():
    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[require_no_session_token] = _fake_user
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


class TestChatStreamTimeoutConfig:
    def test_default_300(self, monkeypatch):
        """环境变量未设置时默认 300s"""
        monkeypatch.delenv("CHAT_STREAM_TIMEOUT", raising=False)
        monkeypatch.setattr(config_mod, "CHAT_STREAM_TIMEOUT", 300)
        monkeypatch.setattr(config_mod, "read_server_config", lambda: {})
        assert config_mod.get_chat_stream_timeout() == 300

    def test_server_config_override(self, monkeypatch):
        """server-config.json 的 chat_stream_timeout 动态覆盖（无需重启）"""
        monkeypatch.setattr(config_mod, "CHAT_STREAM_TIMEOUT", 300)
        monkeypatch.setattr(config_mod, "read_server_config",
                            lambda: {"chat_stream_timeout": 600})
        assert config_mod.get_chat_stream_timeout() == 600

    def test_invalid_server_config_fallback(self, monkeypatch):
        """非法/非正值回退 env 常量"""
        monkeypatch.setattr(config_mod, "CHAT_STREAM_TIMEOUT", 432)
        for bad in ("abc", -5, 0, ""):
            monkeypatch.setattr(config_mod, "read_server_config",
                                lambda b=bad: {"chat_stream_timeout": b})
            assert config_mod.get_chat_stream_timeout() == 432

    def test_env_constant_default(self):
        """CHAT_STREAM_TIMEOUT 常量默认 300（未注入 env 时）"""
        if "CHAT_STREAM_TIMEOUT" not in os.environ:
            assert config_mod.CHAT_STREAM_TIMEOUT == 300
        assert config_mod.CHAT_STREAM_HEARTBEAT_INTERVAL == 15


class TestPollDbStreamHeartbeat:
    def test_heartbeat_and_dynamic_timeout_text(self, monkeypatch):
        """DB 轮询分支：等待期推送「思考中」心跳，超时文案含配置时长"""
        monkeypatch.setattr(config_mod, "read_server_config",
                            lambda: {"chat_stream_timeout": 3})
        monkeypatch.setattr(config_mod, "CHAT_STREAM_HEARTBEAT_INTERVAL", 1)

        task_id = "chat_" + uuid.uuid4().hex[:12]
        conv_id = "conv_" + uuid.uuid4().hex[:12]
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO chat_messages (conversation_id, role, content, task_id, generate_status) VALUES (?,?,?,?,1)",
                (conv_id, "user", "hello", task_id))
            db.commit()

        try:
            events = []
            with TestClient(app).stream("GET", f"{BASE}/task/{task_id}/stream") as r:
                assert r.status_code == 200
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
            types = [e.get("type") for e in events]
            # 初始占位 + 至少一次心跳（timeout=3s，心跳间隔 1s）
            assert types[0] == "思考中"
            assert types.count("思考中") >= 2
            # 超时文案动态显示配置时长
            err = [e for e in events if e.get("type") == "error"][-1]
            assert "查询超时（3s）" in err["text"]
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM chat_messages WHERE task_id=?", (task_id,))
                db.commit()

    def test_agent_reply_before_timeout(self, monkeypatch):
        """agent 已有终态回复时直接返回 done，不受超时配置影响"""
        monkeypatch.setattr(config_mod, "read_server_config",
                            lambda: {"chat_stream_timeout": 2})
        monkeypatch.setattr(config_mod, "CHAT_STREAM_HEARTBEAT_INTERVAL", 15)

        task_id = "chat_" + uuid.uuid4().hex[:12]
        conv_id = "conv_" + uuid.uuid4().hex[:12]
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO chat_messages (conversation_id, role, content, task_id, generate_status) VALUES (?,?,?,?,1)",
                (conv_id, "user", "hi", task_id))
            c.execute(
                "INSERT INTO chat_messages (conversation_id, role, content, task_id, generate_status) VALUES (?,?,?,?,2)",
                (conv_id, "agent", "final answer", task_id))
            db.commit()

        try:
            events = []
            with TestClient(app).stream("GET", f"{BASE}/task/{task_id}/stream") as r:
                assert r.status_code == 200
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
            assert events[-1]["type"] == "done"
            assert events[-1]["text"] == "final answer"
        finally:
            with db_lock:
                c = db.cursor()
                c.execute("DELETE FROM chat_messages WHERE task_id=?", (task_id,))
                db.commit()


class TestSSEStreamHeartbeat:
    def test_heartbeat_and_total_timeout(self, monkeypatch):
        """SSE 分支：无输出期间心跳 + 总超时动态文案"""
        monkeypatch.setattr(config_mod, "read_server_config",
                            lambda: {"chat_stream_timeout": 2})
        monkeypatch.setattr(config_mod, "CHAT_STREAM_HEARTBEAT_INTERVAL", 1)

        from src.api.chat_task_manager import ChatTaskManager

        task_id = "chat_" + uuid.uuid4().hex[:12]

        class _FakeTask:
            events = []
            final_text = ""

        def _fake_subscribe(cls, tid, loop):
            return asyncio.Queue(), _FakeTask()

        monkeypatch.setattr(ChatTaskManager, "subscribe",
                            classmethod(_fake_subscribe))
        unsub_called = []
        monkeypatch.setattr(ChatTaskManager, "unsubscribe",
                            classmethod(lambda cls, tid, q: unsub_called.append(tid)))

        events = []
        with TestClient(app).stream("GET", f"{BASE}/task/{task_id}/stream") as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        types = [e.get("type") for e in events]
        assert types[0] == "思考中"  # 缓冲为空的占位
        assert types.count("思考中") >= 2  # 等待期心跳（2s 总超时、1s 心跳间隔）
        err = [e for e in events if e.get("type") == "error"][-1]
        assert "超时（2s 无输出）" in err["text"]
        assert unsub_called == [task_id]
