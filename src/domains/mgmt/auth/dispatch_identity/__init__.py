"""派发身份（Issue #21 第 2 步）——领域包：模型 + 解析服务。"""
from src.domains.mgmt.auth.dispatch_identity.models import (
    DispatchIdentity, DispatchMode, IdentitySource)
from src.domains.mgmt.auth.dispatch_identity.service import (
    current_mode, dispatch_identity, resolve_identity, user_id_is_valid)

__all__ = ["DispatchIdentity", "DispatchMode", "IdentitySource", "current_mode",
           "dispatch_identity", "resolve_identity", "user_id_is_valid"]
