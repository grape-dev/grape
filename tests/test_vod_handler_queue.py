"""飞书 VOD bot：非 direct 模式应入队由 RuntimeAgent 执行，direct 保持本地流式。

见 multi-runtime-instance-design 配套 / src/im/vod_handler_async.py._execute_vod_agent。
"""
import asyncio
import os
from unittest.mock import AsyncMock, patch

from src.im.vod_handler_async import _execute_vod_agent


def _args():
    repo_key = "gitcode:Jason7602/test"
    agent_session = {"backend": "opencode", "skill": "vod", "skill_id": 0, "repo_gitcode_token": ""}
    conv = {"id": 0, "runtime_session_id": ""}
    evt = {"type": "vod_signal", "issue_id": "", "payload": {"im_chat_id": "chat_v1"}}
    return repo_key, agent_session, conv, evt


class TestVodHandlerQueueMode:
    def test_queue_mode_enqueues(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "queue_remote")
        with patch("src.runtime.service.enqueue_task", return_value="t_v") as m_enq, \
             patch("src.runtime.service.wait_task_terminal",
                   new=AsyncMock(return_value={"status": "completed", "exit_code": 0,
                                               "result_text": "VOD_QUEUE_OK",
                                               "usage_json": "{}"})), \
             patch("src.user.db.get_runtime_id_by_name", return_value=1), \
             patch("src.im.vod_handler_async.consume_stream_async") as m_consume:
            reply = asyncio.run(_execute_vod_agent(*_args()))

        assert reply == "VOD_QUEUE_OK"
        m_enq.assert_called_once()
        assert m_enq.call_args.kwargs["runtime_id"] == 1, "入队应带解析出的 runtime_id"
        assert m_enq.call_args.kwargs["agent_type"] == "vod"
        assert m_enq.call_args.kwargs["skill"] == "vod"
        m_consume.assert_not_called(), "queue 模式不应本地流式执行"

    def test_direct_mode_streams(self, monkeypatch):
        monkeypatch.setenv("DEVOPS_EXECUTION_MODE", "direct")

        async def _fake_consume(*a, **k):
            yield ("done", {"exit_code": 0, "text": "VOD_LOCAL_OK", "oc_session_id": "ses_v"})

        with patch("src.runtime.service.enqueue_task") as m_enq, \
             patch("src.im.vod_handler_async.consume_stream_async", side_effect=_fake_consume):
            reply = asyncio.run(_execute_vod_agent(*_args()))

        assert reply == "VOD_LOCAL_OK"
        m_enq.assert_not_called(), "direct 模式不应入队"
