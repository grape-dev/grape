"""平台卡管理面用例包（Issue #3580）。

分层：
  models.py       DTO / 领域错误（无框架依赖）
  repository.py   平台行仓储（mgmt 库自持，封装 ORM）
  service.py      用例编排（跨域只经 runtime.public 契约）

路由（`src.domains.mgmt.admin.routes`）只做 HTTP 适配：body → CardSaveRequest →
service → DTO.as_dict()；业务规则、跨域调用与落库顺序全部收在本包内。
"""
from src.domains.mgmt.admin.platform_cards.models import (  # noqa: F401
    CardSaveRequest,
    CardSaveResult,
    CardValidationError,
    CardView,
)
from src.domains.mgmt.admin.platform_cards.repository import PlatformRowRepository  # noqa: F401
from src.domains.mgmt.admin.platform_cards.service import PlatformCardService  # noqa: F401

__all__ = [
    "CardSaveRequest",
    "CardSaveResult",
    "CardValidationError",
    "CardView",
    "PlatformRowRepository",
    "PlatformCardService",
]