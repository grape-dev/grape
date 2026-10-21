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
        c.execute("DELETE FROM task_messages WHERE task_id LIKE 'test_%' OR task_id LIKE 't_test%' OR task_id LIKE 't1' OR task_id LIKE 't2' OR task_id IN (SELECT id FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%')")
        c.execute("DELETE FROM agent_tasks WHERE id LIKE 't1' OR id LIKE 't2' OR id LIKE 'test_%' OR repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM task_messages WHERE task_id LIKE 'test_%' OR task_id LIKE 't_test%' OR task_id LIKE 't1' OR task_id LIKE 't2' OR task_id IN (SELECT id FROM agent_tasks WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%')")
        c.execute("DELETE FROM agent_tasks WHERE id LIKE 't1' OR id LIKE 't2' OR id LIKE 'test_%' OR repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/%' OR repo_key LIKE 'org/%' OR repo_key LIKE 'gitcode:owner/%' OR repo_key LIKE 'owner/%' OR repo_key LIKE 'gitcode:tc_%' OR repo_key LIKE 'gitcode:o/r' OR repo_key LIKE 'o/r' OR repo_key LIKE 'gitcode:a/%' OR repo_key LIKE 'a/b' OR repo_key LIKE 'gitcode:b/%' OR repo_key LIKE 'gitcode:x/%' OR repo_key LIKE 'gitcode:new/%' OR repo_key LIKE 'gitcode:empty/%' OR repo_key LIKE 'gitcode:navuser/%' OR repo_key LIKE 'gitcode:nonexistent/%' OR repo_key LIKE 'gitcode:repair/%' OR repo_key LIKE 'gitcode:ghost/%' OR repo_key LIKE 'gitcode:iso/%' OR repo_key LIKE 'gitcode:iac/%' OR repo_key LIKE 'gitcode:Jason7602/%' OR repo_key LIKE 'gitcode:evapi/%' OR repo_key LIKE 'gitcode:sweep/%' OR repo_key LIKE 'gitcode:no-%' OR repo_key LIKE 'other/%' OR repo_key LIKE 'github:org/%' OR repo_key LIKE 'github:other/%' OR repo_key LIKE 'gitcode:chat:%'")
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

    def test_fanout_payload_carries_seq(self):
        """fanout 载荷透传 seq（report_messages 落库后传入，前端按 seq 去重）。"""
        from src.runtime.task_log_stream import _to_payload
        # 带 seq 的消息（report_messages 落库后构造）
        p1 = _to_payload({"type": "text", "data": "hi", "seq": 42})
        assert p1 is not None and p1.get("seq") == 42
        # tool_use 带 seq
        p2 = _to_payload({"type": "tool_use", "data": {"tool": "bash", "input": {"cmd": "ls"}}, "seq": 43})
        assert p2 is not None and p2.get("seq") == 43 and p2["tool"] == "bash"
        # 无 seq 的旧式调用（向后兼容）
        p3 = _to_payload({"type": "text", "data": "legacy"})
        assert p3 is not None and "seq" not in p3
        # done 不推送
        assert _to_payload({"type": "done", "data": {}, "seq": 99}) is None

    def test_fanout_delivers_seq_via_queue(self):
        """端到端：fanout 收到带 seq 消息 → 队列 payload 含 seq。"""
        async def main():
            q = await subscribe("t2")
            fanout("t2", [{"type": "text", "data": "msg", "seq": 7}])
            payload = await asyncio.wait_for(q.get(), 1)
            assert payload["type"] == "text" and payload["content"] == "msg"
            assert payload.get("seq") == 7
            await unsubscribe("t2", q)
        asyncio.run(main())


# ---------- seq 贯通 report_messages → fanout ----------

class TestReportMessagesSeq:
    def test_report_messages_fanout_carries_seq(self):
        """report_messages 落库后传给 fanout 的消息含 seq（通过 _seq_messages 构造）。

        验证：调用 report_messages 后，若本进程有订阅者，收到的 payload 含 seq。
        """
        _make_task("t2")
        async def main():
            from src.runtime.task_log_stream import subscribe, unsubscribe
            q = await subscribe("t2")
            # 在另一个线程/同步调用 report_messages（模拟同主快路径）
            service.report_messages("t2", [{"type": "text", "data": "hello"}])
            payload = await asyncio.wait_for(q.get(), 2)
            assert payload["type"] == "text" and payload["content"] == "hello"
            assert isinstance(payload.get("seq"), int) and payload["seq"] >= 1
            await unsubscribe("t2", q)
        asyncio.run(main())


# ---------- 跨主模拟：DB 尾随补发 ----------

class TestCrossMasterTail:
    """模拟双主场景：消息直接落 DB（另一台 master 的 fanout 不触达本进程），
    验证 SSE stream 的 DB 增量尾随在 ≤2×尾随周期内补发。"""

    def test_tail_picks_up_db_insert_without_fanout(self):
        """绕过本进程 fanout 直插 task_messages → SSE gen 尾随补发。"""
        import os as _os
        _make_task("t2")
        # 先通过 report_messages 落一条（建立 last_seq 基线）
        service.report_messages("t2", [{"type": "text", "data": "baseline"}])
        baseline = service.list_task_messages("t2")
        assert len(baseline) == 1
        base_seq = baseline[-1]["seq"]

        # 模拟另一台 master 直接 INSERT task_messages（不触发本进程 fanout）
        from datetime import datetime, timezone
        from src.session_manager import db as _db, db_lock as _dbl
        with _dbl:
            c = _db.cursor()
            c.execute(
                "INSERT OR IGNORE INTO task_messages (task_id, seq, type, tool, content, input_json, output, created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("t2", base_seq + 1, "text", "", "cross-master-msg", "{}", "",
                 datetime.now(timezone.utc).isoformat()))
            _db.commit()

        # 驱动 SSE gen：用尾随周期补发
        from src.api.routes.task_logs import _TAIL_POLL_S
        async def main():
            from src.runtime.task_log_stream import subscribe, unsubscribe, sse_frame
            import json as _json
            from src.runtime.service import list_task_messages
            # 模拟 gen() 的尾随逻辑（不经过 HTTP，直接测补发逻辑）
            last_seq = base_seq
            tailed = list_task_messages("t2", since=last_seq)
            assert len(tailed) == 1
            assert tailed[0]["seq"] == base_seq + 1
            assert tailed[0]["content"] == "cross-master-msg"
            # 验证 sse_frame 可序列化含 seq 的 payload
            frame = sse_frame(tailed[0])
            assert "cross-master-msg" in frame
            assert str(base_seq + 1) in frame
        asyncio.run(main())

    def test_tail_query_incremental_since(self):
        """尾随查询 since=last_seq 正确返回增量（多消息场景）。"""
        _make_task("t2")
        service.report_messages("t2", [
            {"type": "text", "data": "a"},
            {"type": "text", "data": "b"},
            {"type": "text", "data": "c"},
        ])
        msgs = service.list_task_messages("t2")
        assert [m["seq"] for m in msgs] == [1, 2, 3]
        # 模拟另一台 master 再落 2 条
        from datetime import datetime, timezone
        from src.session_manager import db as _db, db_lock as _dbl
        with _dbl:
            c = _db.cursor()
            for i, txt in enumerate(["d", "e"], start=4):
                c.execute(
                    "INSERT OR IGNORE INTO task_messages (task_id, seq, type, tool, content, input_json, output, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    ("t2", i, "text", "", txt, "{}", "",
                     datetime.now(timezone.utc).isoformat()))
            _db.commit()
        # 尾随查询 since=3 应返回 seq 4,5
        tailed = service.list_task_messages("t2", since=3)
        assert [m["seq"] for m in tailed] == [4, 5]
        assert [m["content"] for m in tailed] == ["d", "e"]

    def test_gen_tail_picks_up_cross_master_insert(self):
        """端到端：驱动 stream gen() 的 live 循环，验证 DB 尾随补发跨主 INSERT。

        流程：gen() catch-up → 进入 live 循环 → 队列超时 → 尾随查询发现另一台 master
        落库的消息 → yield 补发。用 mock request 控制断开时机。
        """
        from unittest.mock import AsyncMock, MagicMock
        _make_task("t2")
        # 落基线消息
        service.report_messages("t2", [{"type": "text", "data": "baseline"}])
        base_seq = service.list_task_messages("t2")[-1]["seq"]

        async def main():
            # 构建 gen() 所需的 request mock（is_disconnected 控制循环退出）
            mock_request = MagicMock()
            # 前 N 次 is_disconnected 返回 False（允许尾随执行），之后返回 True（退出）
            mock_request.is_disconnected = AsyncMock(side_effect=[False] * 20 + [True])
            import src.api.routes.task_logs as _tl
            _orig_poll = _tl._TAIL_POLL_S
            _tl._TAIL_POLL_S = 0.1
            try:
                resp = await _tl.stream_task_messages("t2", mock_request, user={"id": 1}, since=0)
                gen = resp.body_iterator
                # 消费 catch-up 帧 + 首个 live 超时帧
                seen = []
                async for frame in gen:
                    _f = frame.decode() if isinstance(frame, (bytes, bytearray)) else frame
                    seen.append(_f)
                    if "baseline" in _f:
                        break
                # 模拟另一台 master 落库（不触发本进程 fanout）
                from datetime import datetime, timezone
                from src.session_manager import db as _db, db_lock as _dbl
                with _dbl:
                    c = _db.cursor()
                    c.execute(
                        "INSERT OR IGNORE INTO task_messages (task_id, seq, type, tool, content, input_json, output, created_at) VALUES (?,?,?,?,?,?,?,?)",
                        ("t2", base_seq + 1, "text", "", "cross-master-gen", "{}", "",
                         datetime.now(timezone.utc).isoformat()))
                    _db.commit()
                # 继续消费：尾随应在 ≤2×尾随周期内补发 cross-master-gen
                found = False
                async for frame in gen:
                    _f = frame.decode() if isinstance(frame, (bytes, bytearray)) else frame
                    if "cross-master-gen" in _f:
                        found = True
                        break
                assert found, f"tail did not pick up cross-master insert: {seen}"
            finally:
                _tl._TAIL_POLL_S = _orig_poll
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
