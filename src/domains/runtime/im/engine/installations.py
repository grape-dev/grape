"""安装枚举（Issue #760 §5.3 第 2 步——P1 切换）。

Supervisor 的 installation_provider 从 load_eligible_bots 包装切换为
grape_t_channel_installation 全表枚举（不按平台过滤——multica MUL-3620 教训）。

A2 收尾（Phase B）：legacy 双轨（chat_agent_session / devops_agent_session 的 im_* 列）
已 DROP，原来与旧表对账的 reconcile_installations 一并删除——安装表是唯一真相。

表归属：`grape_t_channel_installation` 归 mgmt。本模块只做「行 → 安装字典」的投影，
SQL 在 `src.domains.mgmt.public.installation_provider_rows_any` 一处（按拓扑自选连接）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Optional

from src.kernel.config import log


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
    """在同一线程内获取连接（sqlite 线程亲和约束），查询走归属域 public。

    conn_factory 给的是本进程的 IM 库句柄（engine_wiring 里钉 runtime 库）；单库 / 测试
    拓扑下原语复用它——连接与游标都在本 worker 线程内创建、使用，关闭责任仍在本函数
    （与原实现逐字一致）。三库拓扑下原语忽略它改用 mgmt 连接：本模块不再持有任何本表 SQL。
    """
    from src.domains.mgmt import public as _mgmt_public

    conn = conn_factory()
    try:
        rows = _mgmt_public.installation_provider_rows_any(conn)
        return [_row_to_installation(r) for r in rows]
    finally:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不掩盖正常返回（共享连接 close 为空操作）
            log.debug("im installations: close skipped: %s", exc)


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