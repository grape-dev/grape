"""Hwcloud Runtime 转发层（M0 兼容，对齐 src/domains/runtime/agents/hermes.py → src/domains/runtime/engine/hermes.py 模式）。

实际实现在 src/domains/runtime/agents/hwcloud_acp.py（ACP JSON-RPC 协议，不走 build_args/parse_output）。
本文件提供 runtime 侧的名字锚点 + env 组装入口，供 executor/_BACKENDS 注册与
discover_backends 发现。
"""

import os

from .base import AgentRuntime

class HwcloudRuntime(AgentRuntime):
    """hwcloud 引擎的 runtime 描述。

    build_args/parse_output 不被使用（hwcloud 走 ACP 专用路径，
    见 src/domains/runtime/agents/executor.py stream_agent 的 backend_name == "hwcloud" 分支）。
    """

    name = "hwcloud"
    binary = "hwcloud"
    prompt_via_stdin = False

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        # ACP 模式不用 CLI 参数传 prompt；保留接口以兼容 AgentRuntime 契约。
        args = ["serve", "--acp"]
        return args

    def get_env(self, account: str = "", user_id: str = "") -> dict:
        from src.domains.runtime.agents.hwcloud_acp import HwcloudACPBackend
        return HwcloudACPBackend().get_env(account=account, user_id=user_id)

    # [P0a A3] 本机路径 ACP 能力钩子：executor.stream_agent 按 callable(acp_backend)
    # 分派统一 _stream_acp（不再按引擎名）。hwcloud 本机执行恒走 ACP。
    def acp_backend(self):
        from src.domains.runtime.agents.hwcloud_acp import HwcloudACPBackend
        return HwcloudACPBackend()

"""Hwcloud Runtime 转发层（M0 兼容，对齐 src/agent/hermes.py → src/runtime/hermes.py 模式）。

实际实现在 src/domains/runtime/agents/hwcloud_acp.py（ACP JSON-RPC 协议，不走 build_args/parse_output）。
本文件提供 runtime 侧的名字锚点 + env 组装入口，供 executor/_BACKENDS 注册与
discover_backends 发现。
"""
