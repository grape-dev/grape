"""租约（Issue #742 §4.2）——继承 bot_lease.py 全部语义，去平台化。

A2 M3：租约已落在独立表 `grape_t_channel_lease`，键即 `<channel_type>:<installation_id>`，
不再需要"installation 键 → bot_app_id"的翻译层。LeaseStore 协议保持不变
（§5.3：将来换 PG advisory / Redis 时接口仍不变，陷阱 13）。
"""
from __future__ import annotations

from typing import Optional, Protocol

from ..channel.types import Type


class LeaseStore(Protocol):
    """平台无关租约接口（§4.2）。实现：src/im/bot_lease.py BotLeaseStore。"""

    def try_acquire(self, key: str, steal_after: int = 60,
                    instance_timeout: float = 45.0) -> Optional[str]: ...

    def renew(self, key: str, token: str) -> bool: ...

    def release(self, key: str, token: str) -> None: ...

    def batch_renew(self, held: dict) -> dict: ...

    def list_expired_or_unheld(self, cursor: str = "", limit: int = 100) -> list: ...


def lease_key(installation_id: str, channel_type: Type | str) -> str:
    """安装级租约键：跨平台唯一（app_id 或 installation UUID）。"""
    return f"{channel_type}:{installation_id}"


__all__ = ["LeaseStore", "lease_key"]
