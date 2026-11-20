"""远程 worker / cloud_worker：HTTP API 调主服务。不查本地 DB。"""


class RemoteWorkerServices:
    def __init__(self, client):
        self._client = client

    async def issue_proxy_token(self, task_id: str, backend_name: str) -> str:
        # 遗漏-3 修复：传 backend_name 给服务端 API
        return await self._client.issue_proxy_token_remote(task_id, backend_name)

    async def revoke_proxy_token(self, task_id: str) -> None:
        await self._client.revoke_proxy_token_remote(task_id)
