"""A2A 去重上下文键（Issue #1667）。

抽出 webhook / event_store 共用，避免 webhook ↔ event_store 反向依赖
（排队幂等的原子写入需要在 event_store 内复用同一套键推导）。
"""
import hashlib
import json


def a2a_context_key(idempotency_key: str = "", context: str = "") -> str:
    """A2A 去重上下文键（幂等键优先，其次 context 摘要）。

    - 显式 idempotency_key：`idem:<key>`（跨请求稳定，由调用方派生）；
    - 无幂等键但携带 context：`ctx:<sha256(context)[:16]>`，区分不同任务的
      交办上下文（如 T3 补测 vs PR 交付），避免被同 issue+agent 去重吞掉；
    - 两者皆空：返回 ""（保持 #587 的 issue+agent 旧去重语义）。
    """
    _k = (idempotency_key or "").strip()
    if _k:
        return f"idem:{_k}"
    _c = (context or "").strip()[:500]
    if _c:
        return "ctx:" + hashlib.sha256(_c.encode("utf-8")).hexdigest()[:16]
    return ""


def a2a_event_context_key(idempotency_key: str = "", payload_raw: str = "") -> str:
    """从已落库事件的行内 payload 还原上下文键（历史行兼容）。"""
    _ctx = ""
    try:
        _p = json.loads(payload_raw or "{}")
        if isinstance(_p, dict):
            _ctx = str(_p.get("context") or "")
            if not idempotency_key:
                idempotency_key = str(_p.get("idempotency_key") or "")
    except Exception:
        pass
    return a2a_context_key(idempotency_key, _ctx)
