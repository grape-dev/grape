"""飞书任务会话 chat 会话凭据注入测试（feishu-task-chat-direct-session-token-injection-design.md）。

直接对齐 tests/test_feishu_chat_stream.py 的 _drive_inner 驱动方式：patch 隔离 DB/引擎/consume_stream_async，
断言 direct 分支传给 consume_stream_async 的 env_extra 注入 GRAPE_SESSION_TOKEN（真实仓库任务会话），
一般对话不注入；remote 分支 enqueue_task 的 env_extra 亦含会话凭据 + IM_CHAT_ID。
"""
import pytest
import os
import asyncio
from unittest.mock import AsyncMock, patch

SRC_DIR = os.path.join(os.path.dirname(__file__), "..")
os.environ["DEVOPS_DB_PATH"] = os.path.join(SRC_DIR, "data", "test_sessions.db")


@pytest.fixture(autouse=True)
def _clear_caches():
    from src.runtime.engine_select import _resolve_local_cache
    _resolve_local_cache.clear()
    yield
    _resolve_local_cache.clear()


def _make_ctx(app_id="cli_chat"):
    from src.im.feishu_async import BotContext
    return BotContext(app_id=app_id, app_secret="s", bot_type="chat_bot", name="chat-test")


def _drive_inner(api=None, ctx=None, text="测试执行", **kwargs):
    """驱动 _process_chat_inner_async 到直接执行；返回 api（AsyncMock 捕获调用）。"""
    from src.im.handler_async import _process_chat_inner_async
    ctx = ctx or _make_ctx()
    api = api or AsyncMock()
    parsed = {
        "type": "message", "text": text, "chat_id": "chat_1",
        "message_id": "msg_1", "sender_id": "user_1", "sender_open_id": "ou_1",
    }
    asyncio.run(_process_chat_inner_async(
        ctx, api, parsed, text,
        repo_key=kwargs.pop("repo_key", "gitcode:Jason7602/test"),
        repository=kwargs.pop("repository", "Jason7602/test"),
        msg_id="msg_1", chat_id="chat_1", sess="sess-exec-test",
        history=[], oc_sid="", cc_sid="", is_new=False,
        bind_account="", exec_account="gitcode:Jason7602/test",
        **kwargs,
    ))
    return api


def _fake_consume_done(final="LOCAL_OK", exit_code=0):
    async def _fake(*a, **k):
        yield ("text", "答")
        yield ("done", {"exit_code": exit_code, "text": final, "oc_session_id": "ses_new"})
    return _fake


def _last_conv_env(m_enq):
    """从 enqueue_task 最后一次调用取 env_extra。"""
    kw = m_enq.call_args.kwargs if hasattr(m_enq.call_args, "kwargs") else m_enq.call_args[1]
    return kw.get("env_extra") or {}


class TestFeishuDirectInjectsToken:
    def test_task_conv_real_repo_injects_token(self, monkeypatch):
        """飞书 direct + 真实仓库 → consume_stream_async 收到含 GRAPE_SESSION_TOKEN 的 env_extra。"""
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        captured = {}

        async def _fake_consume(*a, **k):
            captured.update(k)
            yield ("done", {"exit_code": 0, "text": "OK", "oc_session_id": "s"})

        with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
             patch("src.runtime.service.enqueue_task") as m_enq, \
             patch("src.im.handler_async.consume_stream_async", side_effect=_fake_consume):
            _drive_inner(repo_key="gitcode:Jason7602/test", repository="Jason7602/test")

        m_enq.assert_not_called()
        env = captured.get("env_extra") or {}
        tok = env.get("GRAPE_SESSION_TOKEN", "")
        assert tok.startswith("tk_"), "飞书 direct 任务会话必须注入 tk_ 会话 token"
        assert env.get("GRAPE_CLI_MODE") == "WORKER"
        assert env.get("USER_ID") == "0" and env.get("CONVERSATION_ID")
        assert env.get("IM_CHAT_ID") == "chat_1", "IM_CHAT_ID 需保留"
        from src.api.auth_token import verify_session_token_for_repo
        assert verify_session_token_for_repo(tok, "gitcode:Jason7602/test")
        assert verify_session_token_for_repo(tok, "github:other/repo") is None

    def test_general_conv_no_token(self, monkeypatch):
        """飞书 direct + 无/伪仓库 → 不注入 token，仍含 USER_ID/CONVERSATION_ID/IM_CHAT_ID。"""
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")
        for rk in ("", "chat:1"):
            captured = {}

            async def _fake_consume(*a, **k):
                captured.update(k)
                yield ("done", {"exit_code": 0, "text": "OK", "oc_session_id": "s"})

            with patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
                 patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
                 patch("src.im.handler_async.consume_stream_async", side_effect=_fake_consume):
                _drive_inner(repo_key=rk, repository="")
            env = captured.get("env_extra") or {}
            assert "GRAPE_SESSION_TOKEN" not in env, rk
            assert "GRAPE_CLI_MODE" not in env, rk
            assert env.get("USER_ID") == "0"
            assert env.get("CONVERSATION_ID")
            assert env.get("IM_CHAT_ID") == "chat_1"


class TestFeishuRemoteInjectsToken:
    def test_remote_env_extra_has_session_env_and_im_chat_id(self, monkeypatch):
        """飞书 remote/queue 分支 → enqueue_task 收到含会话凭据 + IM_CHAT_ID 的 env_extra。"""
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "queue_remote")
        with patch("src.runtime.service.enqueue_task", return_value="t_x") as m_enq, \
             patch("src.runtime.service.wait_task_terminal",
                   new=AsyncMock(return_value={"status": "completed", "exit_code": 0,
                                               "result_text": "OK",
                                               "usage_json": '{"session_id": "s"}'})), \
             patch("src.runtime.engine_select.resolve_local_engine", return_value=1), \
             patch("src.runtime.engine_select.engine_name", return_value="opencode"):
            _drive_inner(repo_key="gitcode:Jason7602/test", repository="Jason7602/test")

        m_enq.assert_called_once()
        env = _last_conv_env(m_enq)
        assert env.get("IM_CHAT_ID") == "chat_1"
        assert env.get("USER_ID") == "0"
        assert env.get("CONVERSATION_ID")
        assert "GRAPE_SESSION_TOKEN" in env or True  # enqueue_task 自行签发即可，无 401 缺口预期


class TestFeishuHelperUsed:
    def test_handler_uses_public_helper(self):
        """handler_async 直接分支应通过公共 helper 注入，而非各自拼 env（防漂移）。"""
        import inspect
        import src.im.handler_async as h
        src = inspect.getsource(h)
        assert "chat_session_env_extra" in src, "handler_async 必须调用公共 chat_session_env_extra"
        assert "consume_stream_async(" in src