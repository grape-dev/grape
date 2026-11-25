"""runtime 侧派发身份适配器（Issue #21 第 2 步）——唯一消费 mgmt 契约的地方。

为什么不把解析写进 event_pipeline：那是巨石文件（5000+ 行），派发身份是一次小判定，
放在这里可以单测，也让 event_pipeline 只留一行调用；跨域只走 `mgmt.public` 契约，
不 import mgmt 内部实现（3库4进程约束）。
"""
from __future__ import annotations

from src.kernel.config import log


def identity_for_actor(account_name: str, actor_name: str,
                       task_user_id: str = "") -> "tuple[str, str]":
    """返回 `(可写 user_id, source)`。

    ``task_user_id`` 优先（Issue 创建人的 Grape 用户 id），其次按账号名解析；
    on（默认）/off/shadow 由契约判定：`applies` 为假时 user_id 为空、行值保持 0；
    on：返回解析出的 id，由调用方写进 agent_tasks.user_id。
    异常不外抛：认人失败不得阻断派发。
    """
    try:
        from src.domains.mgmt.public import dispatch_identity

        ident = dispatch_identity(
            task_user_id=task_user_id,
            account_name=account_name, actor_name=actor_name)
        uid = ident.user_id if getattr(ident, "applies", False) else ""
        source = ident.source.value if getattr(ident, "has_candidate", False) else ""
        return uid, source
    except Exception as e:  # noqa: BLE001 - 契约不可用时保持旧行为
        log.debug(f"[dispatch-identity] contract call failed: {e}")
        return "", ""
