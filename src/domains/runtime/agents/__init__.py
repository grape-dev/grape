"""Agent 后端库（兼容转发层，M0 后已迁移至 src.domains.runtime.engine）。

调用方应迁移到 `from src.domains.runtime.engine import ...`；本模块保留旧导出，避免调用方破坏。
"""
from .base import AgentBackend
from .opencode import OpencodeBackend
from .codearts import CodeArtsBackend
from .atomcode import AtomCodeBackend
from .executor import stream_agent, get_backend, stream_opencode
