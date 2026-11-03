"""Grape Service Bus — 统一服务路由基类（GsbService）.

设计依据：docs/weekly-09/grape-unified-implementation-design.md §7.8.5.

每个域进程继承 GsbService，自动获得：
- 服务注册（启动时 register_service）
- 心跳更新（30s asyncio 循环）
- /internal/* 认证中间件挂载
- 优雅注销（shutdown 时 deregister_service）

使用方式（在 apps/xxx/main.py 或 server 入口中）：
    from src.kernel.gsb.router import GsbService
    gsb = GsbService("mgmt", 38080, app)
    gsb.mount()
    # 在 lifespan 中调用:
    await gsb.startup()   # 启动心跳
    await gsb.shutdown()  # 注销服务
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import FastAPI
from fastapi import APIRouter

from src.kernel.gsb.registry import (
    register_service,
    heartbeat_service,
    deregister_service,
)
from src.kernel.auth_sdk.internal_middleware import InternalAuthMiddleware
from src.kernel.config import log

# 心跳间隔
HEARTBEAT_INTERVAL = 30


class GsbService:
    """服务基类——每个域进程继承此类，自动获得注册/心跳/认证能力。"""

    def __init__(self, name: str, port: int, app: FastAPI):
        self.name = name
        self.port = port
        self.app = app
        self.base_url = f"http://localhost:{port}"
        self.internal_router = APIRouter(prefix="/internal")
        self._mounted = False
        self._heartbeat_task: asyncio.Task | None = None

    def mount(self) -> None:
        """启动时调用：注册服务 + 挂载内部路由。

        幂等：重复调用不会重复注册。

        Note: startup/shutdown 钩子通过 ``startup()`` / ``shutdown()``
        方法暴露，由调用方在 lifespan 中调用（替代废弃的 ``@app.on_event``）。
        """
        if self._mounted:
            return
        self._mounted = True

        # 挂载 /internal/* 认证中间件
        self.app.add_middleware(InternalAuthMiddleware)

        # 挂载内部路由
        self.app.include_router(self.internal_router)

        # 注册服务到 DB
        try:
            register_service(
                name=self.name,
                base_url=self.base_url,
                health_url="/api/health",
                metadata=self._metadata(),
            )
        except Exception as e:
            log.warning(f"GSB register_service failed: {e}")

        log.info(f"GSB service '{self.name}' mounted at {self.base_url}")

    async def startup(self) -> None:
        """启动心跳循环——在 lifespan startup 阶段调用。"""
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def shutdown(self) -> None:
        """注销服务——在 lifespan shutdown 阶段调用。"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        try:
            deregister_service(self.name)
        except Exception as e:
            log.warning(f"GSB deregister failed: {e}")

    async def _heartbeat_loop(self) -> None:
        """30s 心跳循环。"""
        while True:
            try:
                heartbeat_service(self.name)
            except Exception as e:
                log.warning(f"GSB heartbeat failed: {e}")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    def _metadata(self) -> dict[str, Any]:
        """服务元数据（子类可覆盖）。"""
        return {"description": self.name, "version": "1", "started_at": time.time()}
