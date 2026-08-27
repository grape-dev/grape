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
            # force_close=True: 服务端用短连接，不复用避免 CLOSE-WAIT 僵尸连接
            self._connector = aiohttp.TCPConnector(limit=self._max_connections, force_close=True)
            self._session = aiohttp.ClientSession(connector=self._connector)

    async def reset_session(self):
        """强制关闭并重建连接池（处理 CLOSE-WAIT 死连接）。"""
        log.warning("resetting aiohttp session (clearing stale connections)")
        await self.close()
        self._connector = None
        self._session = None

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
                        if resp.status == 410:
                            log.info(f"RuntimeClient {method} {path} -> 410 (task terminated): {txt}")
                        else:
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

    async def register(self, runtimes: list[dict], device_name: str = "",
                       cli_version: str = "", local_skills: list[dict] | None = None) -> tuple[list[int], str]:
        """注册引擎，返回 (runtime_ids, probe_url)。probe_url：v3.0 启动可达性探测地址。
        local_skills：per-engine 本地 skill 列表（可选，向后兼容）。"""
        payload = {"device_name": device_name, "cli_version": cli_version, "runtimes": runtimes}
        if local_skills is not None:
            payload["local_skills"] = local_skills
        data = await self._request("POST", "/api/runtime/register", payload)
        if not data:
            return [], ""
        ids = [r["id"] for r in (data.get("runtimes") or [])]
        return ids, (data.get("probe_url") or "")

    async def latest_version(self) -> str:
        """查询服务端当前 Worker 安装包版本（worker-server-delivery-design.md §2.4）。"""
        data = await self._request("GET", "/api/runtime/install/latest", retries=1)
        return (data or {}).get("version", "")

    async def heartbeat(self, runtime_ids: list[int], log_lines: list = None, metrics: dict = None) -> dict | None:
        """发送心跳。返回完整响应 dict（含 cancelled_tasks）或 None（网络错误/服务端不可达）。

        worker 调用后检查 result.get("status") == "ok" 判断成功，
        检查 result.get("cancelled_tasks", []) 发现取消任务并杀进程。

        log_lines: worker 最近的 ERROR/WARNING 日志条目列表
        [{ts, level, message}]，主控存入 data/logs/workers/{id}.log
        metrics: worker 系统资源指标 {cpu_percent, memory_percent, disk_percent, load_avg}
        """
        payload = {"runtime_ids": runtime_ids}
        if log_lines:
            payload["log_lines"] = log_lines
        if metrics:
            payload.update(metrics)
        result = await self._request("POST", "/api/runtime/heartbeat", payload, retries=2)
        return result

    async def claim(self, runtime_id: int, supports_obs_url: bool = False,
                    prefer_fallback: bool = False) -> dict | None:
        """claim（v3.0：携带能力声明——supports_obs_url/prefer_fallback 动态声明零落库）。
        返回值可能含 task 和 scan_request 两个字段。"""
        data = await self._request("POST", f"/api/runtime/runtimes/{runtime_id}/tasks/claim",
                                   {"supports_obs_url": supports_obs_url, "prefer_fallback": prefer_fallback},
                                   retries=1)
        if not data:
            return None
        return data

    async def claim_by_tag(self, runtime_id: int, tag: str,
                           supports_obs_url: bool = False,
                           prefer_fallback: bool = False) -> dict | None:
        """Claim task by tag + scope isolation (plugin mode).

        返回完整响应 dict（含 task 和 scan_request 字段），与 claim() 一致。
        调用方通过 .get("task") 取任务、.get("scan_request") 取扫描指令。
        """
        data = await self._request("POST",
                                   f"/api/runtime/runtimes/{runtime_id}/tasks/claim-by-tag/{tag}",
                                   {"supports_obs_url": supports_obs_url, "prefer_fallback": prefer_fallback},
                                   retries=1)
        if not data:
            return None
        return data

    async def get_tags(self, runtime_id: int) -> list[str]:
        """Fetch this engine's claimable tags from server DB (replaces ENGINE_TAGS env).

        Called periodically by the claim loop to get the current tag list.
        Enables real-time tag updates via console without worker restart.
        """
        data = await self._request("GET", f"/api/runtime/runtimes/{runtime_id}/tags", retries=1)
        if not data:
            return []
        return data.get("tags") or []

    async def get_task_env(self, task_id: str) -> dict:
        """Fetch latest env (AK/SK + credentials) for a task from server DB.

        Called before executing a task to get fresh credentials,
        instead of relying on the env_extra snapshot from task creation time.
        """
        data = await self._request("GET", f"/api/runtime/tasks/{task_id}/env", retries=1)
        if not data:
            return {}
        return data.get("env") or {}

    async def task_start(self, task_id: str):
        """标记任务 running。返回 _request 结果（None=失败），调用方据此判断是否成功。"""
        return await self._request("POST", f"/api/runtime/tasks/{task_id}/start", {})

    async def report_progress(self, task_id: str, summary: str = "", step: int = 0, total: int = 0):
        await self._request("POST", f"/api/runtime/tasks/{task_id}/progress",
                            {"summary": summary, "step": step, "total": total}, retries=1)

    async def report_messages(self, task_id: str, messages: list):
        """返回 dict（正常/网络错误）或 None（410 task 已终态）。

        410 Gone 表示 task 已被服务端标终态（用户终止/超时），worker 应中断执行。
        网络错误（服务重启等）返回空 dict 让 worker 继续执行，
        避免服务短暂中断导致正在执行的 task 被误标 failed。
        """
        try:
            data = await self._request("POST", f"/api/runtime/tasks/{task_id}/messages",
                                       {"messages": messages}, retries=3)
            if data is None:
                # Distinguish 410 (task terminated) from network error:
                # _request returns None for both — do a lightweight GET to check task state
                try:
                    state = await self._request("GET", f"/api/runtime/tasks/{task_id}/state", retries=1)
                    if state and state.get("status") in ("failed", "cancelled", "completed"):
                        log.info(f"report_messages: task {task_id} is {state.get('status')} on server, returning None")
                        return None  # task truly terminated
                except Exception:
                    pass
                # Network error (server restart) — don't abort, let worker continue
                log.warning(f"report_messages: task {task_id} network error (not 410), returning empty dict to continue")
                return {}
            return data
        except Exception as e:
            log.warning(f"report_messages: task {task_id} exception: {e}, returning empty dict to continue")
            return {}

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
        """回滚 dispatched/running 任务为 queued（客户端 claim 冲突跳过时调用）。

        见 seq#622/#667 教训：worker 端因并发槽冲突 skip 任务后，若服务端任务仍停留
        dispatched 将永久悬空（不执行也不重新排队）；须调本接口回滚。
        """
        await self._request("POST", f"/api/runtime/tasks/{task_id}/requeue", {}, retries=4)

    async def get_task_state(self, task_id: str) -> str:
        """轻量查询任务状态（worker 端 sid 看门狗用，seq#1485 根因修复）。

        worker 的 active_sessions 是内存 set，_wrap 的 finally 在 opencode 进程
        hang 住时永远不执行 → sid 永久泄漏。主循环定期调本方法查 active_tasks
        里的 task 是否已在服务端终态（failed/cancelled/completed/not_found），
        是则主动清理 active_sessions。
        """
        data = await self._request("GET", f"/api/runtime/tasks/{task_id}/state", retries=1)
        if not data:
            return ""
        return data.get("status") or ""

    async def recover_orphans(self, runtime_id: int):
        await self._request("POST", f"/api/runtime/runtimes/{runtime_id}/recover-orphans", {}, retries=2)

    async def report_skills(self, runtime_id: int, skills: list[dict]):
        """回报 skill 扫描结果给服务端（scan_request 指令的响应）。"""
        await self._request("POST", f"/api/runtime/runtimes/{runtime_id}/skills-report",
                            {"skills": skills}, retries=2)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        if self._connector:
            await self._connector.close()
