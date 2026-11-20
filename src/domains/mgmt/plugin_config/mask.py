"""凭据掩码中间件 —— 审计/日志视图强制掩码（Issue #1082）。

规则（纵深防御）：
- 密钥命名键（全库统一命名约定，见 vault.is_credential_name）的值一律掩码 ——
  无论其值是统一凭据库引用句柄还是意外混入的明文；
- 引用句柄值（vault://cred/...）即使位于非密钥命名键下也掩码；
- 其余结构原样保留；返回新对象，不改入参（原始视图仍可供 A2 契约使用）。

使用约束：凡把配置视图写入审计/日志/IM 卡片等展示面的调用方，
必须先经 mask_config 掩码；A2 #1080 审计 JSONL 写入点接入时同此约定。
"""
from __future__ import annotations

from typing import Any

from src.domains.mgmt.auth.vault import is_credential_name, is_handle

MASK = "***"


def mask_config(obj: Any) -> Any:
    """递归掩码凭据材料，返回脱敏新对象（审计/日志专用视图）。"""
    if isinstance(obj, dict):
        return {
            key: (MASK if _is_sensitive(key, value) else mask_config(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [mask_config(item) for item in obj]
    if isinstance(obj, str) and is_handle(obj):
        return MASK
    return obj


def _is_sensitive(key: Any, value: Any) -> bool:
    """键名命中密钥命名约定，或值本身是凭据库引用句柄 → 需掩码。"""
    if is_credential_name(str(key)):
        return True
    return isinstance(value, str) and is_handle(value)
