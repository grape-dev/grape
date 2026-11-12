"""安装枚举（Issue #760 §5.3 第 2 步——P1 切换）。

Supervisor 的 installation_provider 从 load_eligible_bots 包装切换为
grape_t_channel_installation 全表枚举（不按平台过滤——multica MUL-3620 教训）。

A2 收尾（Phase B）：legacy 双轨（chat_agent_session / devops_agent_session 的 im_* 列）
已 DROP，原来与旧表对账的 reconcile_installations 一并删除——安装表是唯一真相。
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Optional

_PROVIDER_SELECT = (
    "SELECT id, channel_type, scope, owner_user_id, repo_key, config, "
    "config_fingerprint, status FROM grape_t_channel_installation WHERE status='active'"
)


def _row_to_installation(row) -> dict:
    return {
        "installation_id": row[0],
        "id": row[0],
        "channel_type": row[1],
        "scope": row[2],
        "owner_user_id": row[3],
        "repo_key": row[4],
        "config": json.loads(row[5] or "{}"),
        "config_fingerprint": row[6],
        "status": row[7],
    }


def _list_installations_with_factory(conn_factory: Callable) -> list:
    """在同一线程内获取并使用连接（sqlite 线程亲和约束）。"""
    conn = conn_factory()
    try:
        return [_row_to_installation(r) for r in conn.execute(_PROVIDER_SELECT).fetchall()]
    finally:
        try:
            conn.close()
        except Exception:
            pass


class SqliteInstallationProvider:
    """P1 安装来源：grape_t_channel_installation 活跃行。

    conn_factory 返回可用 sqlite 连接（在 worker 线程内获取并使用，
    满足 sqlite 线程亲和约束）；阻塞读经 asyncio.to_thread 下放，
    不卡事件循环（multica「慢 DB 卡事件循环」陷阱）。
    """

    def __init__(self, conn_factory: Callable) -> None:
        self._conn_factory = conn_factory

    async def __call__(self) -> list:
        return await asyncio.to_thread(_list_installations_with_factory, self._conn_factory)


__all__ = ["SqliteInstallationProvider"]
