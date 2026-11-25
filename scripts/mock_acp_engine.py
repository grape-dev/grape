#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mock ACP 引擎（C05/A1 终极验收被测体，P0b）。

用途: 「descriptor + 1 JSON 零改动接入」的第四个 ACP 引擎——不依赖厂商凭据、
CI 可复跑。协议与生产侧 `_ACPSimpleClient` / `hermes_acp.py` 实测口径同构:

  stdin/stdout NDJSON JSON-RPC:
  - initialize            → result{protocolVersion:1, agentInfo}
  - session/new           → result{sessionId}
  - session/prompt        → 3 × notification session/update(agent_message_chunk)
                          → result{stopReason:"end_turn"}
  - session/cancel        → result{}（会话标记取消，后续 prompt 立即 end_turn）
  - session/notification  忽略

CLI:
  --version   探活口径（descriptor probe.command），打印 "mock-acp <ver>" 退出 0
  --chaos     I2 失败注入钩子: 在首条 chunk 前注入一行非法 NDJSON，进程不崩、
              PromptResponse 照常返回（客户端侧不崩由 acp_client.parse 守卫测试覆盖）

事件文本固定 3 段（"mock-acp chunk 1/3" 等），供 C05-A4 断言「结果文本确为 mock
内容、未静默 fallback 到 opencode」。
"""
import json
import sys
import uuid

VERSION = "mock-acp 0.1.0"
_CHUNKS = ("mock-acp chunk 1/3", "mock-acp chunk 2/3", "mock-acp chunk 3/3")
_cancelled: set[str] = set()


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _reply(req_id, result) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle(line: str, chaos: bool, chaos_sent: list) -> None:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        # 请求行本身非法: 不崩、不回应（无 id），静默跳过（I2 服务侧口径）
        return
    if not isinstance(msg, dict):
        return
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        _reply(req_id, {
            "protocolVersion": 1,
            "agentInfo": {"name": "mock-acp", "version": "0.1.0"},
        })
    elif method == "session/new":
        _reply(req_id, {"sessionId": f"mock-{uuid.uuid4().hex[:16]}"})
    elif method == "session/prompt":
        session_id = params.get("sessionId", "")
        if session_id in _cancelled:
            _reply(req_id, {"stopReason": "cancelled"})
            return
        for i, text in enumerate(_CHUNKS):
            if chaos and not chaos_sent:
                # I2 失败注入: 一行非法 NDJSON（非 JSON 文本）
                sys.stdout.write("{{{ NOT-JSON chaos line for I2\n")
                sys.stdout.flush()
                chaos_sent.append(True)
            _send({"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": text}},
            }})
        _reply(req_id, {"stopReason": "end_turn"})
    elif method == "session/cancel":
        sid = params.get("sessionId", "")
        if sid:
            _cancelled.add(sid)
        _reply(req_id, {})
    elif method:
        pass  # 未知 method 的 notification（无 id）忽略；带 id 的回 -32601
        if req_id is not None:
            _error(req_id, -32601, f"method not found: {method}")
    # 无 method 的纯 response（对端回包）忽略


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print(VERSION)
        return 0
    chaos = "--chaos" in args
    chaos_sent: list = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        _handle(line, chaos, chaos_sent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
