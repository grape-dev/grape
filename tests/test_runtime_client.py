"""RuntimeAgent HTTP 客户端单元测试（src/runtime/client.py，agent-runtime-design.md §7.4）。

用 mock aiohttp session/response 覆盖：请求状态码分支、指数退避重试、
runtime_gone(404)、RetryableError、异常兜底、各业务方法、连接池生命周期。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.runtime.client import RuntimeClient, RetryableError, RuntimeGoneError


class _FakeResp:
    def __init__(self, status, body=None, text=""):
        self.status = status
        self._body = body
        self._text = text

    async def json(self):
        return self._body

    async def text(self):
        return self._text


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _RaiseCtx:
    """在 __aenter__ 时抛出指定异常，模拟请求阶段被取消/连接中断。"""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *a):
        return False


def _client(responses):
    """构造 client，session.request 按序返回给定响应（或 raise）。"""
    c = RuntimeClient("https://srv", "tok", timeout=1)
    c._session = AsyncMock()
    # 关键：AsyncMock 的 .closed 自动属性是 truthy，会让 _ensure_session 重建真实会话；
    # request 必须是"同步返回 async 上下文管理器"的 MagicMock（AsyncMock 返回协程，async with 会挂起）。
    c._session.closed = False
    req = MagicMock()
    if isinstance(responses, list):
        req.side_effect = responses
    else:
        req.return_value = responses
    c._session.request = req
    return c


def _run(coro):
    return asyncio.run(coro)


class TestRequestStatusPaths:
    def test_ok_returns_json(self):
        c = _client(_Ctx(_FakeResp(200, {"a": 1})))
        assert _run(c._request("GET", "/api/x")) == {"a": 1}

    def test_204_returns_empty_dict(self):
        c = _client(_Ctx(_FakeResp(204)))
        assert _run(c._request("GET", "/api/x")) == {}

    def test_400_returns_none(self):
        c = _client(_Ctx(_FakeResp(400, text="bad")))
        assert _run(c._request("GET", "/api/x")) is None

    def test_non_json_body_returns_empty_dict(self):
        resp = _FakeResp(200, body="not json")
        resp.json = AsyncMock(side_effect=ValueError("no json"))
        c = _client(_Ctx(resp))
        assert _run(c._request("GET", "/api/x")) == {}

    def test_404_runtime_path_raises_runtime_gone_internally(self):
        """404 且 path 含 runtime → RuntimeGoneError，_request 返回 None（调用方重注册）。"""
        c = _client(_Ctx(_FakeResp(404)))
        assert _run(c._request("GET", "/api/runtime/runtimes/1/tasks/claim")) is None

    def test_404_non_runtime_path_returns_none(self):
        c = _client(_Ctx(_FakeResp(404)))
        assert _run(c._request("GET", "/api/other")) is None


class TestRetry:
    def test_500_retries_then_succeeds(self):
        c = _client([_Ctx(_FakeResp(500)), _Ctx(_FakeResp(200, {"ok": True}))])
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("GET", "/api/x", retries=4)) == {"ok": True}

    def test_429_retries_then_succeeds(self):
        c = _client([_Ctx(_FakeResp(429)), _Ctx(_FakeResp(200, {"ok": True}))])
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("GET", "/api/x", retries=4)) == {"ok": True}

    def test_408_retries_then_succeeds(self):
        c = _client([_Ctx(_FakeResp(408)), _Ctx(_FakeResp(200, {"ok": True}))])
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("GET", "/api/x", retries=4)) == {"ok": True}

    def test_retries_exhausted_returns_none(self):
        c = _client([_Ctx(_FakeResp(500))] * 5)
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("GET", "/api/x", retries=4)) is None

    def test_exception_retries_then_succeeds(self):
        c = _client([RuntimeError("conn reset"), _Ctx(_FakeResp(200, {"ok": True}))])
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("GET", "/api/x", retries=4)) == {"ok": True}

    def test_exception_exhausted_returns_none(self):
        c = _client([RuntimeError("conn reset")] * 5)
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("GET", "/api/x", retries=4)) is None

    def test_cancelled_error_reraises(self):
        c = _client(_RaiseCtx(asyncio.CancelledError()))
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                _run(c._request("GET", "/api/x"))

    def test_runtime_gone_error_returns_none(self):
        """__aenter__ 阶段抛 RuntimeGoneError（模拟 404 runtime）→ _request 捕获并返回 None（调用方重注册）。"""
        c = _client(_RaiseCtx(RuntimeGoneError("gone")))
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            assert _run(c._request("GET", "/api/runtime/x")) is None


class TestBusinessMethods:
    def test_register_returns_ids(self):
        # v3.0 契约：register 返回 (runtime_ids, probe_url)
        c = _client(_Ctx(_FakeResp(200, {"runtimes": [{"id": 1}, {"id": 2}], "probe_url": "https://p/probe"})))
        ids, probe = _run(c.register([{"name": "opencode"}], "dev1", "1.0"))
        assert ids == [1, 2]
        assert probe == "https://p/probe"

    def test_register_empty_returns_empty_list(self):
        c = _client(_Ctx(_FakeResp(200, {})))
        assert _run(c.register([])) == ([], "")

    def test_latest_version(self):
        c = _client(_Ctx(_FakeResp(200, {"version": "1.2.3"})))
        assert _run(c.latest_version()) == "1.2.3"

    def test_latest_version_empty(self):
        c = _client(_Ctx(_FakeResp(400, text="x")))
        assert _run(c.latest_version()) == ""

    def test_heartbeat_true(self):
        c = _client(_Ctx(_FakeResp(200, {})))
        assert _run(c.heartbeat([1])) is True

    def test_heartbeat_false(self):
        c = _client(_Ctx(_FakeResp(400, text="x")))
        assert _run(c.heartbeat([1])) is False

    def test_claim_returns_task(self):
        c = _client(_Ctx(_FakeResp(200, {"task": {"id": "t1"}})))
        assert _run(c.claim(7)) == {"id": "t1"}

    def test_claim_no_task(self):
        c = _client(_Ctx(_FakeResp(200, {})))
        assert _run(c.claim(7)) is None

    @pytest.mark.parametrize("method,args,kwargs,path", [
        ("task_start", ("t1",), {}, "/api/runtime/tasks/t1/start"),
        ("report_progress", ("t1",), {"summary": "s", "step": 1, "total": 3}, "/api/runtime/tasks/t1/progress"),
        ("report_messages", ("t1",), {"messages": [{"role": "user"}]}, "/api/runtime/tasks/t1/messages"),
        ("report_usage", ("t1",), {"usage": {"n": 1}}, "/api/runtime/tasks/t1/usage"),
        ("requeue", ("t1",), {}, "/api/runtime/tasks/t1/requeue"),
        ("recover_orphans", (5,), {}, "/api/runtime/runtimes/5/recover-orphans"),
    ])
    def test_simple_post_methods(self, method, args, kwargs, path):
        c = _client(_Ctx(_FakeResp(200, {})))
        assert _run(getattr(c, method)(*args, **kwargs)) is None
        req = c._session.request.call_args
        assert req.args[1].endswith(path)

    def test_complete_posts_payload(self):
        c = _client(_Ctx(_FakeResp(200, {})))
        _run(c.complete("t1", "done text", 0, session_id="s1", work_dir="/w"))
        req = c._session.request.call_args
        body = req.kwargs["json"]
        assert body["exit_code"] == 0 and body["result_text"] == "done text"
        assert body["session_id"] == "s1" and body["work_dir"] == "/w"

    def test_fail_posts_error(self):
        c = _client(_Ctx(_FakeResp(200, {})))
        _run(c.fail("t1", "boom"))
        body = c._session.request.call_args.kwargs["json"]
        assert body["error"] == "boom"


class TestSessionLifecycle:
    def test_ensure_session_creates(self):
        c = RuntimeClient("https://srv", "tok")
        c._session = None
        with patch("aiohttp.ClientSession") as m_sess, patch("aiohttp.TCPConnector") as m_conn:
            _run(c._ensure_session())
        m_conn.assert_called_once_with(limit=10, force_close=True)
        m_sess.assert_called_once_with(connector=c._connector)
        assert c._session is not None

    def test_ensure_session_reuses_existing(self):
        c = RuntimeClient("https://srv", "tok")
        c._session = AsyncMock()
        c._session.closed = False
        with patch("aiohttp.ClientSession") as m_sess:
            _run(c._ensure_session())
        m_sess.assert_not_called()

    def test_reset_session_closes_and_rebuilds(self):
        c = RuntimeClient("https://srv", "tok")
        sess = AsyncMock()
        sess.closed = False
        c._session = sess
        conn = AsyncMock()
        c._connector = conn
        _run(c.reset_session())
        sess.close.assert_awaited_once()
        conn.close.assert_awaited_once()
        assert c._session is None and c._connector is None

    def test_close(self):
        c = RuntimeClient("https://srv", "tok")
        sess = AsyncMock()
        sess.closed = False
        c._session = sess
        conn = AsyncMock()
        c._connector = conn
        _run(c.close())
        sess.close.assert_awaited_once()
        conn.close.assert_awaited_once()

    def test_close_no_session(self):
        c = RuntimeClient("https://srv", "tok")
        c._session = None
        _run(c.close())  # 不应抛错
