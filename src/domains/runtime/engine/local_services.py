"""本机 worker：直接查 MySQL（共享 DB）。保留基线行为。"""

import asyncio


class LocalWorkerServices:
    """本机 worker：同步 DB 调用用 asyncio.to_thread 包装，不阻塞事件循环。"""

    async def issue_proxy_token(self, task_id: str, backend_name: str) -> str:
        # token.py issue_proxy_token 是同步 DB 写入，用 to_thread 避免阻塞事件循环
        from src.domains.runtime.agents.sandbox.token import issue_proxy_token
        return await asyncio.to_thread(issue_proxy_token, task_id, backend_name)

    async def revoke_proxy_token(self, task_id: str) -> None:
        from src.domains.runtime.agents.sandbox.token import revoke_proxy_token
        await asyncio.to_thread(revoke_proxy_token, task_id)
