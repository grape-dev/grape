"""Agent 后端库：支持 opencode / codearts 等多 Agent CLI。"""
from .base import AgentBackend
from .opencode import OpencodeBackend
from .codearts import CodeArtsBackend
from .executor import stream_agent, get_backend, stream_opencode
