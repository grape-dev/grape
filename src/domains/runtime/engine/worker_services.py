"""Worker 数据获取端口——统一本机和远程的 proxy token 获取方式。

基线已解耦的数据（work_branch / user_gitcode_token / 用户凭据 / AK/SK）：
  通过 env_extra 注入 或 client.get_task_env HTTP API，不需要此端口。

本端口只统一 proxy token——基线技术债（只有 DB 写入，远程 worker 写 SQLite 主服务查不到）。
"""
from __future__ import annotations
from typing import Protocol
import logging

log = logging.getLogger("devops-bot")


class WorkerServices(Protocol):
    """Worker 数据获取端口。"""

    async def issue_proxy_token(self, task_id: str, backend_name: str) -> str:
        """签发 proxy token。backend_name 用于审计/白名单（本机写入 DB，远程传给 API）。"""
        ...

    async def revoke_proxy_token(self, task_id: str) -> None:
        """撤销 proxy token。"""
        ...


def create_worker_services(client=None, is_local: bool = False) -> WorkerServices:
    """工厂函数——显式传入部署位置。

    is_local 由调用方在进程启动时确定一次（不是每次 _execute_task 从 URL 猜）：
    - agent.py main(): _IS_LOCAL_WORKER 模块级常量，启动时从 RUNTIME_SERVER_URL 判断一次
      （已知权宜之计，TODO 改为专门 WORKER_MODE=local|remote 配置项）
    - cloud_worker.py main(): is_local=False（明确是远程/云沙箱）
    """
    if is_local:
        from .local_services import LocalWorkerServices
        return LocalWorkerServices()
    else:
        if client is None:
            raise ValueError("remote worker services requires RuntimeClient")
        from .remote_services import RemoteWorkerServices
        return RemoteWorkerServices(client)
