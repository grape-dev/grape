"""Runtime 注册表：注册 / 查询 / 发现本机可用的 Agent 执行引擎。

实现复用 src/agent/executor.py 的注册表（M0 迁移期间统一为一份 dict，
避免调用方/测试 mock 路径分裂；后续可整体搬移）。
"""
from src.agent.executor import (
    _BACKENDS as _RUNTIMES,
    register_backend as register_runtime,
    get_backend as get_runtime,
    discover_backends as discover_runtimes,
)
from .base import AgentRuntime
