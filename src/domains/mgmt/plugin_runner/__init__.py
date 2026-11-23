"""Grape 插件薄 Runner（Issue #1080，总纲 #1078 A2）。

公开 API：
- PluginRunner(manifest).run(command, params, credentials) -> CallResult
- CallResult / PluginRunnerError 及子类
- JsonlAuditWriter   JSONL 审计（凭据强制掩码）
- redact             掩码规则统一出口
- ensure_plugin_venv 每插件独立 venv（D8）
- try_call_with_fallback 通道调度层：多通道按优先级降序逐通道 try，
  超时/401/5xx 触发回落（Issue #1311，补录 #1083）
- ChannelHealth      通道健康管理：连续失败摘除 + 半开被动探测回接
- call_script        第五型 script adapter 执行器（签名脚本通道，补录 #1081）
"""
from .adapters import (
    FALLBACK_REMOVAL_THRESHOLD,
    HALF_OPEN_PROBE_SLOTS,
    ChannelHealth,
    call_script,
    try_call_with_fallback,
)
from .audit import JsonlAuditWriter
from .credentials import MASK, redact
from .errors import (
    CallTimeout,
    EngineNotSupported,
    ManifestError,
    MissingCredential,
    PluginRunnerError,
    TransportError,
    UnknownCommand,
    VenvError,
)
from .runner import CallResult, PluginRunner
from .venv import ensure_plugin_venv

__all__ = [
    "PluginRunner",
    "CallResult",
    "PluginRunnerError",
    "ManifestError",
    "UnknownCommand",
    "EngineNotSupported",
    "MissingCredential",
    "TransportError",
    "VenvError",
    "CallTimeout",
    "JsonlAuditWriter",
    "redact",
    "MASK",
    "ensure_plugin_venv",
    "try_call_with_fallback",
    "ChannelHealth",
    "FALLBACK_REMOVAL_THRESHOLD",
    "HALF_OPEN_PROBE_SLOTS",
    "call_script",
]
