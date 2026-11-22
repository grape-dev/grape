"""平台行仓储（mgmt 域自持：`grape_t_repo_platforms` 是 mgmt 库的表）。

职责边界：
- **只**负责「手建实例行」的 upsert/读取与启用开关；卡定义的真源不在这里
  （文件 → meta 整卡 → 表行，读卡归 runtime 卡的领域服务）。
- 封装 ORM/引擎细节，service 层不出现 SQLAlchemy 依赖（3 库 4 进程下引擎按进程
  自己的域库绑定，本仓储不做跨库写）。
- 行跟 `source` 字段走策略：`manual` 行可写，配置文件派生行（`config`）返回
  `config_owned` 且不写——防双真源，由管理面覆盖层（runtime 契约）承接改动。
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = ["CardRowStore", "PlatformRowRepository"]


@runtime_checkable
class CardRowStore(Protocol):
    """平台行写入端口（service 依赖它，而不是具体仓储实现）。"""

    def upsert_cards(self, cards: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
        ...


class PlatformRowRepository:
    """`grape_t_repo_platforms`（mgmt 库）的仓储适配器。"""

    @staticmethod
    def card_columns() -> set[str]:
        """表内真实列名（卡里还有非表列字段，写行前必须过滤，否则整条被拒）。"""
        from src.kernel.persistence.models_platform_object import RepoPlatformModel

        return {c.name for c in RepoPlatformModel.__table__.columns}

    def upsert_cards(self, cards: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
        """逐平台落行，返回 {provider: inserted|updated|promoted}（A：表为真源）。

        行落库失败整体上抛（service 决定是否降级成非致命 rows_error）。
        """
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

        columns = self.card_columns()
        actions: dict[str, str] = {}
        with Session(get_engine()) as session:
            for provider, fields in cards.items():
                payload = {k: v for k, v in fields.items() if k in columns}
                # A 方案：管理面是平台卡唯一写入口，改动必须落表（config 行会被
                # 提升为 manual，此后导入器不再回灌——消灭「文件/meta/表」三真源）。
                actions[provider] = RepoPlatformRepo.upsert_admin(session, provider, payload)
            session.commit()
        return actions