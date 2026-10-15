"""task_messages 运行日志（实时日志功能）测试。

覆盖：结构化落库 / seq 单调与 since 增量 / 幂等防重放 / fanout 实时推送 / SSE 端点鉴权与增量接口。
见 src/api/static/grape-task-logs-analysis.md。
"""
import asyncio
from src.auth import grape_account_db
import json

import pytest
from fastapi.testclient import TestClient

from src.session_manager import db, db_lock
from src.runtime import service
from src.runtime.task_log_stream import subscribe, unsubscribe, fanout, _subscribers
# JWT access token helper（替代旧的 create_session_token）
def _jwt_access_token(user_id, username="test"):
    from src.api.jwt_utils import create_access_token
    from src.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM task_messages WHERE task_id LIKE 'test_%' OR task_id LIKE 't_test%' OR task_id IN (SELECT id FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%')")
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM task_messages WHERE task_id LIKE 'test_%' OR task_id LIKE 't_test%' OR task_id IN (SELECT id FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%')")
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.commit()


def _make_task(task_id="t1", status="running"):
    with db_lock:
        c = db.cursor()
        c.execute("INSERT OR IGNORE INTO agent_tasks (id, agent_session_id, runtime_id, prompt, status) "
                  "VALUES (?, 's1', 1, 'p', ?)", (task_id, status))
        db.commit()


def _mk_user_token():
    import uuid
    from src.user import db as user_db
    u = grape_account_db.create_grape_account(f"tasklog_{uuid.uuid4().hex[:8]}", email=None, password="pw123456")
    return _jwt_access_token(u["id"], u["username"])


# ---------- 结构化落库 ----------

class TestReportMessages:
    def test_normalize_and_persist(self):
        _make_task()
        service.report_messages("t1", [
            {"type": "step_start", "data": "Step 1 started (session: x)"},
            {"type": "text", "data": "hello"},
            {"type": "tool_use", "data": {"tool": "read", "input": {"path": "/a"}}},
            {"type": "reasoning", "data": "think..."},
            {"type": "error", "data": "boom"},
        ])
        msgs = service.list_task_messages("t1")
        assert len(msgs) == 5
        assert [m["seq"] for m in msgs] == [1, 2, 3, 4, 5]  # seq 单调
        assert msgs[0]["type"] == "step_start" and msgs[0]["content"] == "Step 1 started (session: x)"
        assert msgs[1]["type"] == "text" and msgs[1]["content"] == "hello"
        assert msgs[2]["type"] == "tool_use" and msgs[2]["tool"] == "read"
        assert msgs[2]["input"] == {"path": "/a"}
        assert msgs[4]["type"] == "error"

    def test_seq_continues_across_batches(self):
        _make_task()
        service.report_messages("t1", [{"type": "text", "data": "a"}])
        service.report_messages("t1", [{"type": "text", "data": "b"}])
        msgs = service.list_task_messages("t1")
        assert [m["seq"] for m in msgs] == [1, 2]

    def test_since_incremental(self):
        _make_task()
        service.report_messages("t1", [{"type": "text", "data": "a"}, {"type": "text", "data": "b"},
                                       {"type": "text", "data": "c"}])
        after = service.list_task_messages("t1", since=1)
        assert [m["seq"] for m in after] == [2, 3]

    def test_unknown_task_noop(self):
        # 任务不存在 → 静默跳过（不抛异常）
        service.report_messages("nope", [{"type": "text", "data": "x"}])
        assert service.list_task_messages("nope") == []

    def test_tool_use_input_json_string_form(self):
        # executor 端 tool_use input 是脱敏后的 JSON 字符串（见 executor.py L263）
        _make_task()
        service.report_messages("t1", [{"type": "tool_use",
                                        "data": {"tool": "bash", "input": '{"cmd":"ls","secret":"[REDACTED]"}'}}])
        msgs = service.list_task_messages("t1")
        assert msgs[0]["input"] == {"cmd": "ls", "secret": "[REDACTED]"}


# ---------- fanout 实时推送 ----------

class TestFanout:
    def test_fanout_delivers_and_filters_done(self):
        async def main():
            q = await subscribe("t1")
            fanout("t1", [
                {"type": "text", "data": "live"},
                {"type": "done", "data": {"text": "ok"}},  # 终态事件不推送
                {"type": "tool_use", "data": {"tool": "bash", "input": {"cmd": "ls"}}},
            ])
            first = await asyncio.wait_for(q.get(), 1)
            second = await asyncio.wait_for(q.get(), 1)
            assert first["type"] == "text" and first["content"] == "live"
            assert second["type"] == "tool_use" and second["tool"] == "bash"
            assert q.empty()
            await unsubscribe("t1", q)
            assert "t1" not in _subscribers
        asyncio.run(main())

    def test_fanout_after_unsubscribe_noop(self):
        async def main():
            q = await subscribe("t1")
            await unsubscribe("t1", q)
            fanout("t1", [{"type": "text", "data": "x"}])  # 不抛异常
        asyncio.run(main())


# ---------- API 端点 ----------

@pytest.fixture
def client():
    return TestClient(__import__("src.api.main", fromlist=["app"]).app)


class TestTaskLogsAPI:
    def test_messages_requires_auth(self, client):
        assert client.get("/api/task-logs/t1/messages").status_code == 401

    def test_messages_incremental(self, client):
        _make_task()
        service.report_messages("t1", [{"type": "text", "data": "a"}, {"type": "text", "data": "b"}])
        token = _mk_user_token()
        r = client.get("/api/task-logs/t1/messages?since=1",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert [m["seq"] for m in body] == [2]
        assert body[0]["content"] == "b"

    def test_stream_requires_auth(self, client):
        assert client.get("/api/task-logs/t1/stream").status_code == 401

    def test_unknown_task_404(self, client):
        token = _mk_user_token()
        r = client.get("/api/task-logs/nope/messages",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404
