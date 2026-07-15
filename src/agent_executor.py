"""向后兼容 shim — 从新的 src/agent/ 包 re-export 所有符号。"""
from src.agent import (
    AgentBackend, OpencodeBackend, CodeArtsBackend,
    stream_agent, get_backend, stream_opencode,
)
