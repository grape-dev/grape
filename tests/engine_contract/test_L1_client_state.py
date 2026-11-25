"""B7 段③/⑤（L1 mock 段）：acp_client 协议状态机 + 假完成守卫断言逻辑。

以预录 NDJSON 流（回放 fixture）驱动单源客户端，覆盖「客户端逻辑正确性」：
- 段③ 会话生命周期状态机：initialize → session/new → session/prompt →
  turn 流式（text/thinking/tool）→ PromptResponse(stopReason) 收口；
  超时/畸形输入（非 JSON、非 session/update、params 畸形）不炸不断。
- 段⑤ 假完成守卫：PromptResponse stopReason 主判；无 stopReason 的响应
  视为 end_turn 兜底（不静默丢弃）；错误响应触发 error stop。

CI 恒跑（无引擎依赖）。真机全生命周期/杀进程链见 test_L2_real_engine.py。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import src.domains.runtime.engine.registry  # noqa: E402,F401

from src.domains.runtime.agents.acp_client import _ACPSimpleClient


class _FakeStdout:
    """回放式 stdout：预录行依次读出。"""

    def __init__(self, lines):
        self.lines = list(lines)

    def readline(self):
        return self.lines.pop(0) if self.lines else ""


class _FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass


def _mk_client(lines):
    stdin = _FakeStdin()
    events = []
    client = _ACPSimpleClient(
        stdin=stdin, stdout=_FakeStdout([]), label="[test] ",
        on_message=lambda t, c: events.append((t, c)),
        on_prompt_done=lambda r: events.append(("prompt_done", r)),
    )
    for line in lines:
        client.handle_line(line)
    return client, events, stdin


class TestSessionLifecycle:
    """段③：会话全生命周期的状态机（mock 流回放）。"""

    def test_initialize_response_resolves_pending(self):
        client, _, _ = _mk_client([
            '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentCapabilities":{},"agentInfo":{"name":"x","version":"1"}}}',
        ])
        # request() 已在 handle_line 之前不可行——直接验证 pending 机制：
        evt = client.register_pending(1)
        client.handle_line('{"jsonrpc":"2.0","id":1,"result":{"ok":true}}')
        assert evt.is_set()

    def test_session_new_yields_session_id_field(self):
        client, events, _ = _mk_client([])
        client.register_pending(2)
        client.handle_line('{"jsonrpc":"2.0","id":2,"result":{"sessionId":"acp_test_1"}}')
        assert client._pending_results.get(2) == {"sessionId": "acp_test_1"}

    def test_turn_streaming_events_in_order(self):
        """turn 流式：message/thought/tool 事件依序回传，stopReason 收口。"""
        client, events, _ = _mk_client([
            '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"text":"part1"}}}}',
            '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_thought_chunk","content":{"text":"thinking"}}}}',
            '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"tool_call","toolCallId":"t1","title":"terminal","rawInput":{"command":"ls"}}}}',
            '{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}',
        ])
        client.set_prompt_req_id(3)
        client.register_pending(3)
        for line in [
            '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"text":"part1"}}}}',
            '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_thought_chunk","content":{"text":"thinking"}}}}',
            '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"tool_call","toolCallId":"t1","title":"terminal","rawInput":{"command":"ls"}}}}',
            '{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}',
        ]:
            client.handle_line(line)
        types = [t for t, _ in events]
        assert types[:3] == ["text", "thinking", "tool_use"]
        assert events[-1] == ("prompt_done", {"stopReason": "end_turn", "result": {"stopReason": "end_turn"}})

    def test_malformed_input_never_crashes(self):
        """畸形输入：非 JSON / 非 session/update / params 畸形——不炸不断。"""
        client, events, _ = _mk_client([
            "not-json{{",
            '{"jsonrpc":"2.0","method":"session/update","params":"not-a-dict"}',
            '{"jsonrpc":"2.0","method":"other/method","params":{"update":{}}}',
            '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"usage_update"}}}',
            '{"jsonrpc":"2.0","id":99,"result":{"unmatched":true}}',
        ])
        # usage_update 等已知但非事件类型 → 静默（无 on_message）
        assert all(t != "text" for t, _ in events)


class TestPrematureEndGuard:
    """段⑤：假完成守卫断言逻辑（mock 流）。"""

    def test_stopreason_is_primary_signal(self):
        """PromptResponse 的 stopReason 是收口主判。"""
        client, events, _ = _mk_client([])
        client.set_prompt_req_id(3)
        client.register_pending(3)
        client.handle_line('{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}')
        assert events[-1][1]["stopReason"] == "end_turn"

    def test_missing_stopreason_falls_back_to_end_turn(self):
        """无 stopReason 的 PromptResponse → end_turn 兜底（不静默丢弃）。"""
        client, events, _ = _mk_client([])
        client.set_prompt_req_id(3)
        client.register_pending(3)
        client.handle_line('{"jsonrpc":"2.0","id":3,"result":{"_meta":{"mode":"auto"}}}')
        assert events[-1][1]["stopReason"] == "end_turn"

    def test_error_response_triggers_error_stop(self):
        """错误响应 → stopReason=error（任务侧可感知失败，不假成功）。"""
        client, events, _ = _mk_client([])
        client.set_prompt_req_id(3)
        client.register_pending(3)
        client.handle_line('{"jsonrpc":"2.0","id":3,"error":{"code":-32603,"message":"boom"}}')
        assert events[-1][1]["stopReason"] == "error"

    def test_non_prompt_response_does_not_close_turn(self):
        """非 prompt 的响应（如 session/new）不误触发收口。"""
        client, events, _ = _mk_client([])
        client.set_prompt_req_id(3)
        client.handle_line('{"jsonrpc":"2.0","id":2,"result":{"sessionId":"s1"}}')
        assert not any(t == "prompt_done" for t, _ in events)

    def test_turn_end_notification_carries_stop_reason(self):
        """turn_end 通知路径同样以 stopReason 收口（PREMATURE_END 兜底的服务端前提）。"""
        client, events, _ = _mk_client([
            '{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"turn_end","stopReason":"end_turn"}}}',
        ])
        assert events[-1][1]["stopReason"] == "end_turn"
