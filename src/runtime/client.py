"""RuntimeAgent HTTP 客户端（仅外连，见 agent-runtime-design.md §7.4 / tech-impl.md §3.1）。

用 aiohttp.ClientSession 连接池（已在依赖，避免新增 requests）。
终态回调（complete/fail）带指数退避（4s→8s→…→64s），仅 5xx/408/429 重试。
runtime_gone（404）→ 清理本地 → 幂等重注册。
"""
import asyncio, json, logging, time

log = logging.getLogger("devops-bot")


class RetryableError(Exception):
    pass


class RuntimeGoneError(Exception):
    pass


class RuntimeClient:
    def __init__(self, server: str, token: str, max_connections: int = 10, timeout: float = 30.0):
        self.base = server.rstrip("/")
        self.token = token
        self._timeout = timeout
        self._connector = None
        self._session = None
        self._max_connections = max_connections

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            import aiohttp
            self._connector = aiohttp.TCPConnector(limit=self._max_connections)
            self._session = aiohttp.ClientSession(connector=self._connector)

    async def _request(self, method: str, path: str, body=None, retries: int = 4) -> dict | None:
        """HTTP 请求 + 指数退避。404 且含 runtime → RuntimeGone。返回 JSON 或 None。"""
        delay = 4
        attempt = 0
        while True:
            await self._ensure_session()
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                async with self._session.request(method, self.base + path, json=body, headers=headers,
                                                 timeout=self._timeout) as resp:
                    if resp.status in (408, 429) or resp.status >= 500:
                        raise RetryableError(f"http {resp.status}")
                    if resp.status == 404 and "runtime" in path:
                        raise RuntimeGoneError(path)
                    if resp.status >= 400:
                        txt = (await resp.text())[:200]
                        log.warning(f"RuntimeClient {method} {path} -> {resp.status}: {txt}")
                        return None
                    if resp.status == 204:
                        return {}
                    try:
                        return await resp.json()
                    except Exception:
                        return {}
            except RuntimeGoneError:
                log.warning(f"runtime_gone: {path}, clearing and re-registering")
                return None  # 调用方处理重注册
            except RetryableError:
                if attempt >= retries:
                    log.warning(f"RuntimeClient {method} {path} retries exhausted")
                    return None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 64)
                attempt += 1
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as e:
                log.warning(f"RuntimeClient {method} {path} error: {e}")
                if attempt >= retries:
                    return None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 64)
                attempt += 1

    async def register(self, runtimes: list[dict], device_name: str = "", cli_version: str = "") -> list[int]:
        data = await self._request("POST", "/api/runtime/register",
                                   {"device_name": device_name, "cli_version": cli_version, "runtimes": runtimes})
        if not data:
            return []
        return [r["id"] for r in (data.get("runtimes") or [])]

    async def latest_version(self) -> str:
        """查询服务端当前 Worker 安装包版本（worker-server-delivery-design.md §2.4）。"""
        data = await self._request("GET", "/api/runtime/install/latest", retries=1)
        return (data or {}).get("version", "")

    async def heartbeat(self, runtime_ids: list[int]):
        await self._request("POST", "/api/runtime/heartbeat", {"runtime_ids": runtime_ids}, retries=2)

    async def claim(self, runtime_id: int) -> dict | None:
        data = await self._request("POST", f"/api/runtime/runtimes/{runtime_id}/tasks/claim", retries=1)
        if not data:
            return None
        return data.get("task")

    async def task_start(self, task_id: str):
        await self._request("POST", f"/api/runtime/tasks/{task_id}/start", {})

    async def report_progress(self, task_id: str, summary: str = "", step: int = 0, total: int = 0):
        await self._request("POST", f"/api/runtime/tasks/{task_id}/progress",
                            {"summary": summary, "step": step, "total": total}, retries=1)

    async def report_messages(self, task_id: str, messages: list):
        await self._request("POST", f"/api/runtime/tasks/{task_id}/messages", {"messages": messages}, retries=1)

    async def report_usage(self, task_id: str, usage: dict):
        await self._request("POST", f"/api/runtime/tasks/{task_id}/usage", usage, retries=1)

    async def complete(self, task_id: str, result_text: str, exit_code: int, session_id: str = "", work_dir: str = ""):
        await self._request("POST", f"/api/runtime/tasks/{task_id}/complete",
                            {"result_text": result_text, "exit_code": exit_code,
                             "session_id": session_id, "work_dir": work_dir}, retries=4)

    async def fail(self, task_id: str, error: str):
        await self._request("POST", f"/api/runtime/tasks/{task_id}/fail",
                            {"error": error}, retries=4)

    async def requeue(self, task_id: str):
        """并发冲突跳过时回滚任务为 queued（避免 dispatched 悬空，见 seq#622 教训）。"""
        await self._request("POST", f"/api/runtime/tasks/{task_id}/requeue", {}, retries=2)

    async def recover_orphans(self, runtime_id: int):
        await self._request("POST", f"/api/runtime/runtimes/{runtime_id}/recover-orphans", {}, retries=2)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        if self._connector:
            await self._connector.close()
