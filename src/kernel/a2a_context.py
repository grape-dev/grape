"""A2A 去重上下文键（Issue #1667）。

抽出 webhook / event_store 共用，避免 webhook ↔ event_store 反向依赖
（排队幂等的原子写入需要在 event_store 内复用同一套键推导）。
"""
import hashlib
import json
from typing import Any


def _ctx_text(value: Any) -> str:
    """把调用方给的 context / 幂等键归一成可哈希的文本。

    为什么需要：agent 端（PowerShell 拼 ``--json`` / 直连 curl）可能把 ``context``
    传成 JSON 对象或数组，旧实现直接 ``.strip()`` → ``AttributeError: 'dict' object
    has no attribute 'strip'`` → ``POST /api/a2a`` 500，交接请求整条丢失、下一节点
    永不派发。字典/列表按 JSON 序列化（``sort_keys`` 保证键序无关的稳定性），
    与落库 payload 里的形态一致——「创建时算键」和「从行内 payload 复算键」
    因此得到同一个值。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:  # noqa: BLE001 - 不可序列化对象退化为 repr，别再抛
            return str(value).strip()
    return str(value).strip()


def a2a_context_key(idempotency_key: str = "", context: Any = "") -> str:
    """A2A 去重上下文键（幂等键优先，其次 context 摘要）。

    - 显式 idempotency_key：`idem:<key>`（跨请求稳定，由调用方派生）；
    - 无幂等键但携带 context：`ctx:<sha256(context)[:16]>`，区分不同任务的
      交办上下文（如 T3 补测 vs PR 交付），避免被同 issue+agent 去重吞掉；
    - 两者皆空：返回 ""（保持 #587 的 issue+agent 旧去重语义）。

    context 允许对象/数组（见 `_ctx_text`）；纯字符串走原路径，键值不变。
    """
    _k = _ctx_text(idempotency_key)
    if _k:
        return f"idem:{_k}"
    _c = _ctx_text(context)[:500]
    if _c:
        return "ctx:" + hashlib.sha256(_c.encode("utf-8")).hexdigest()[:16]
    return ""


def a2a_event_context_key(idempotency_key: str = "", payload_raw: str = "") -> str:
    """从已落库事件的行内 payload 还原上下文键（历史行兼容）。"""
    _ctx: Any = ""
    try:
        _p = json.loads(payload_raw or "{}")
        if isinstance(_p, dict):
            _ctx = _p.get("context")
            if not idempotency_key:
                idempotency_key = _ctx_text(_p.get("idempotency_key"))
    except Exception:
        pass
    return a2a_context_key(idempotency_key, _ctx)
