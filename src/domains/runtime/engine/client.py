"""RuntimeAgent HTTP 客户端（仅外连，见 agent-runtime-design.md §7.4 / tech-impl.md §3.1）。

用 aiohttp.ClientSession 连接池（已在依赖，避免新增 requests）。
终态回调（complete/fail）带指数退避（4s→8s→…→64s），仅 5xx/408/429 重试。
runtime_gone（404）→ 清理本地 → 幂等重注册。
"""
import asyncio, json, logging, os, time

log = logging.getLogger("devops-bot")


class RetryableError(Exception):
    pass


class RuntimeGoneError(Exception):
    pass


class CredentialRevokedError(Exception):
    """401 凭据被吊销（revoke）——不可恢复，调用方应中止任务并退出 worker 进程。"""
    pass


class ReportBatchError(Exception):
    """4xx 批次级失败（413 payload too large / 400 bad request 等，Issue #975）。

    与网络错误（可重试，返回 {}）不同：同批次原样重试大概率仍失败，
    调用方应拆批重发或降级占位，绝不得当成功清空 batch。
    """

    def __init__(self, status: int, text: str = ""):
        super().__init__(f"http {status}: {text}")
        self.status = status
        self.text = text


# Issue #975：4xx 中「非批次级失败」的状态码——已有专属语义（401 凭据吊销 /
# 404 runtime gone / 408+429 可重试），不得进入 ReportBatchError 拆批降级分支
NON_BATCH_4XX_STATUSES = (401, 404, 408, 429)


class RuntimeClient:
    def __init__(self, server: str, token: str, max_connections: int | None = None,
                 timeout: float = 30.0):
        # 池子默认 10 太窄：长任务一次批量上报几百条消息就会占满，心跳、claim、tags
        # 全部排队到 sock_read 超时 → 引擎被判失联（2026-11-15 远端主机实测，
        # 同机的另一租户因为没有长任务所以正常）。默认放宽到 30，env 可调。
        if max_connections is None:
            try:
                max_connections = int(os.environ.get("RUNTIME_CLIENT_MAX_CONNECTIONS", "30"))
            except ValueError:
                max_connections = 30
        self.base = server.rstrip("/")
        self.token = token
        self._timeout_secs = timeout
        self._timeout = None  # aiohttp.ClientTimeout, built in _ensure_session
        self._connector = None
        self._session = None
        self._max_connections = max_connections
        # Issue #975：最近一次 _request 的 HTTP 错误（status=0 表示网络错误/重试耗尽）
        self.last_http_error = None
        # Issue #1011：worker v2-first（uid 寻址）状态
        self._uid_by_rid = {}    # runtime_id -> engine_uid（register 响应 / 本地持久化）
        self._v2_enabled = True  # 空白 404/405 判定对端为旧服务端后置 False（记忆，不再每轮撞 v2）
        self.last_register_detail = []
        self._load_engine_uids()

    # ------------------------------------------------------------------
    # Issue #1011：engine_uid 持久化（重启后本地 uid 不丢，claim 不依赖服务端 id 记忆）
    # ------------------------------------------------------------------

    @property
    def _uid_store_path(self) -> str:
        import os
        return os.environ.get("GRAPE_WORKER_UID_FILE") or "worker-engine-uids.json"

    def _load_engine_uids(self) -> None:
        """启动时从本地 JSON 恢复 uid 映射（文件缺失/损坏静默降级 v1）。"""
        try:
            import json, os
            if not os.path.exists(self._uid_store_path):
                return
            with open(self._uid_store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._uid_by_rid = {int(k): str(v) for k, v in (data or {}).items()
                               if str(v or "").strip()}
        except Exception as e:
            log.warning(f"worker uid store load failed (fallback v1): {e}")

    def persist_engine_uids(self) -> None:
        """register 成功后落盘 uid 映射（幂等，失败仅告警不影响主流程）。"""
        try:
            import json
            tmp = self._uid_store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in self._uid_by_rid.items()}, f)
            import os
            os.replace(tmp, self._uid_store_path)
        except Exception as e:
            log.warning(f"worker uid store persist failed: {e}")

    def uid_for(self, runtime_id: int) -> str:
        return self._uid_by_rid.get(int(runtime_id)) or ""

    def _remember_register_detail(self, detail: list) -> None:
        """register 响应明细 {id, name, engine_uid} → uid 映射 + 落盘。"""
        for r in detail or []:
            if isinstance(r, dict) and r.get("id") is not None and r.get("engine_uid"):
                self._uid_by_rid[int(r["id"])] = str(r["engine_uid"]).strip().lower()
        if detail:
            self.persist_engine_uids()

    # ------------------------------------------------------------------
    # Issue #1011：v2 调用原语（404 语义二分）
    # ------------------------------------------------------------------

    async def _request_raw(self, method: str, path: str, body=None, retries: int = 1):
        """一次性探测请求：返回 (status, json_or_None, text)。

        与 _request 的区别：不吞 404，供 v2-first 回退判定按「空白 404/405 =
        旧服务端」vs「body 带 UNKNOWN_ENGINE_UID = uid 失效」二分。
        """
        delay = 2
        attempt = 0
        while True:
            await self._ensure_session()
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                async with self._session.request(method, self.base + path, json=body, headers=headers) as resp:
                    text = ""
                    try:
                        text = await resp.text()
                    except Exception:
                        pass
                    data = None
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = None
                    if resp.status in (408, 429) or resp.status >= 500:
                        raise RetryableError(f"http {resp.status}")
                    return resp.status, data, text
            except RetryableError:
                if attempt >= retries:
                    return 0, None, ""
                await asyncio.sleep(delay)
                delay = min(delay * 2, 32)
                attempt += 1
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as e:
                log.warning(f"RuntimeClient raw {method} {path} error: {e}")
                if attempt >= retries:
                    return 0, None, ""
                await asyncio.sleep(delay)
                delay = min(delay * 2, 32)
                attempt += 1

    @staticmethod
    def _is_blank_not_found(status: int, data, text: str) -> bool:
        """空白 404/405（`{"detail":"Not Found"}`，无业务错误码）= 旧服务端，端点不存在。"""
        if status not in (404, 405):
            return False
        if isinstance(data, dict):
            detail = data.get("detail")
            if isinstance(detail, dict) and detail.get("code"):
                return False   # 业务错误码（如 UNKNOWN_ENGINE_UID）→ 非「端点不存在」
            if data.get("code"):
                return False
        if "UNKNOWN_ENGINE_UID" in (text or ""):
            return False
        return True

    @staticmethod
    def _is_uid_stale(data, text: str) -> bool:
        """body 带明确错误码 UNKNOWN_ENGINE_UID = 服务端支持 v2 但 uid 已漂移。"""
        if isinstance(data, dict):
            detail = data.get("detail")
            if isinstance(detail, dict) and detail.get("code") == "UNKNOWN_ENGINE_UID":
                return True
            if data.get("code") == "UNKNOWN_ENGINE_UID":
                return True
        return "UNKNOWN_ENGINE_UID" in (text or "")

    async def _v2_first(self, path_v2: str, payload: dict, v1_call, *args, **kwargs):
        """v2-first / v1-fallback 调用壳（claim 系与上报共用）。

        - uid 已知且 v2 许可开启 → 先调 v2 路径；
        - 空白 404/405 → 判定旧服务端：记忆性关闭 v2（不再每轮撞 v2），回退 v1；
        - UNKNOWN_ENGINE_UID → uid 失效（引擎删除/重派生窗口）：回退 v1 id 口径
          （v1 再失效由既有 RuntimeGoneError → 重注册链路兜底，register 幂等）；
        - v2 200 → 直接返回响应。
        """
        if self._v2_enabled:
            status, data, text = await self._request_raw("POST", path_v2, payload, retries=1)
            if status == 200:
                return data
            if self._is_blank_not_found(status, data, text):
                self._v2_enabled = False
                log.warning(f"v2 endpoint missing (old server?) -> switch to v1 and remember: {path_v2}")
            elif self._is_uid_stale(data, text):
                log.warning(f"v2 engine_uid stale (re-derive window?) -> fallback v1: {path_v2}")
            elif status:
                log.warning(f"v2 call failed (status={status}) -> fallback v1: {path_v2}")
        return await v1_call(*args, **kwargs)

    async def claim_v2_capabilities(self, engine_uid: str) -> dict | None:
        """capabilities.uid_claim 探测（A13）：旧服务端 404 返回 None（不误判、不切 v2）。"""
        status, data, text = await self._request_raw(
            "GET", f"/api/runtime/runtimes/uid/{engine_uid}/tasks/claim-capabilities", None, retries=1)
        if status == 200 and isinstance(data, dict):
            return data
        return None


    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            import aiohttp
            # force_close=False + keepalive: 复用 TCP 连接减少握手开销，
            # 配合 enable_cleanup_closed 清理半关闭连接，避免 CLOSE-WAIT 僵尸。
            # ssl=False: 内网 IP 访问时 SSL 证书 SAN 不包含 IP 地址，跳过验证
            self._connector = aiohttp.TCPConnector(
                limit=self._max_connections,
                force_close=False,
                enable_cleanup_closed=True,
                keepalive_timeout=30,
                ssl=False,
            )
            self._timeout = aiohttp.ClientTimeout(
                total=self._timeout_secs,
                connect=10,
                sock_read=30,
            )
            self._session = aiohttp.ClientSession(
                connector=self._connector,
                timeout=self._timeout,
            )

    async def reset_session(self):
        """强制关闭并重建连接池（处理 CLOSE-WAIT 死连接）。"""
        log.warning("resetting aiohttp session (clearing stale connections)")
        await self.close()
        self._connector = None
        self._session = None

    async def _request(self, method: str, path: str, body=None, retries: int = 4,
                       error_sink: dict | None = None, session=None) -> dict | None:
        """HTTP 请求 + 指数退避。404 且含 runtime → RuntimeGone。返回 JSON 或 None。

        401 鉴权失败（凭据被吊销/revoke）抛 CredentialRevokedError——调用方感知后
        应中止任务并退出进程，而不是当网络错误无限重试（2026-09-30 吊销场景复盘）。

        Issue #975：返回 None 时把最近一次 HTTP 错误记录到 self.last_http_error
        （{"status": int, "text": str}，status=0 表示网络/重试耗尽），供 report_messages
        区分 410 终态 / 4xx 批次级失败 / 网络错误。

        error_sink（QA 复审修复）：并发请求会互相覆盖实例级 last_http_error，
        需要可靠读取本次错误的调用方（report_messages 三态判定）传入本次调用
        私有的 dict，错误同时写入 sink 与 last_http_error（后者仅为兼容保留）。
        """
        self.last_http_error = None

        def _record(status: int, text: str):
            _err = {"status": status, "text": text}
            self.last_http_error = _err
            if error_sink is not None:
                error_sink.clear()
                error_sink.update(_err)

        delay = 4
        attempt = 0
        while True:
            if session is not None:
                _sess = session          # 心跳等专用会话，不参与共享池排队
            else:
                await self._ensure_session()
                _sess = self._session
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                async with _sess.request(method, self.base + path, json=body, headers=headers) as resp:
                    if resp.status in (408, 429) or resp.status >= 500:
                        raise RetryableError(f"http {resp.status}")
                    if resp.status == 404 and "runtime" in path:
                        raise RuntimeGoneError(path)
                    if resp.status == 401:
                        txt = (await resp.text())[:200]
                        log.warning(f"RuntimeClient {method} {path} -> 401 (credential revoked): {txt}")
                        raise CredentialRevokedError(path)
                    if resp.status >= 400:
                        txt = (await resp.text())[:200]
                        _record(resp.status, txt)
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
            except CredentialRevokedError:
                raise  # 凭据失效不重试，直接上抛——调用方决定退出
            except RuntimeGoneError:
                log.warning(f"runtime_gone: {path}, clearing and re-registering")
                _record(404, "runtime gone")
                return None  # 调用方处理重注册
            except RetryableError:
                if attempt >= retries:
                    log.warning(f"RuntimeClient {method} {path} retries exhausted")
                    _record(0, "retries exhausted")
                    return None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 64)
                attempt += 1
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as e:
                log.warning(f"RuntimeClient {method} {path} error: {e}")
                if attempt >= retries:
                    _record(0, str(e)[:200])
                    return None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 64)
                attempt += 1

    async def register(self, runtimes: list[dict], device_name: str = "",
                       cli_version: str = "", local_skills: list[dict] | None = None,
                       provider: str = "") -> tuple[list[int], str]:
        """注册引擎，返回 (runtime_ids, probe_url)。probe_url：v3.0 启动可达性探测地址。
        local_skills：per-engine 本地 skill 列表（可选，向后兼容）。"""
        payload = {"device_name": device_name, "cli_version": cli_version, "runtimes": runtimes}
        if local_skills is not None:
            payload["local_skills"] = local_skills
        # [沙箱迁移] provider: "devbox"/"cubesandbox"/""(空=本机/默认)——cloud_worker 传入
        if provider:
            payload["provider"] = provider
        data = await self._request("POST", "/api/runtime/register", payload)
        if not data:
            return [], ""
        _detail = [r for r in (data.get("runtimes") or []) if isinstance(r, dict)]
        # Issue #471 S2：暴露最近一次注册明细（{id, name}），供 _reregister 按 name
        # 配对 rid→引擎（个别引擎被跳过时位置对齐会错位）。测试 Fake 亦可提供该属性。
        self.last_register_detail = _detail
        ids = [r["id"] for r in _detail]
        # Issue #1011：register 响应双带 {id, engine_uid} → uid 映射 + 本地持久化
        # （重启后 claim 不依赖服务端 id 记忆；旧服务端无 engine_uid 字段静默忽略）
        try:
            self._remember_register_detail(_detail)
        except Exception as _e:
            log.warning(f"register uid map persist failed: {_e}")
        return ids, (data.get("probe_url") or "")

    async def latest_version(self) -> str:
        """查询服务端当前 Worker 安装包版本（worker-server-delivery-design.md §2.4）。"""
        data = await self._request("GET", "/api/runtime/install/latest", retries=1)
        return (data or {}).get("version", "")

    async def _heartbeat_session(self):
        """心跳专用会话：与任务上报的连接池隔离。

        心跳被 messages 上报挤掉是「引擎经常失联」的直接成因：一个跑了很久的任务
        持续批量上报，占满共享池 → 心跳排队 → 连续失败三次 → 服务端判 offline。
        心跳只需要一条连接，单独开会话最省事也最稳。
        """
        import aiohttp
        if getattr(self, "_hb_session", None) is None or self._hb_session.closed:
            self._hb_session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=2, force_close=False,
                                               enable_cleanup_closed=True,
                                               keepalive_timeout=30, ssl=False),
                timeout=aiohttp.ClientTimeout(total=self._timeout_secs, connect=10, sock_read=30),
            )
        return self._hb_session

    async def _poll_session(self):
        """轮询专用会话：短超时（5s）。

        claim / claim_by_tag / get_tags 每轮循环都要发一次，用默认 30s 超时的话，
        一次失败就吃掉两个心跳周期；多个 rid × 多个 tag 串行叠加，主循环会被拖到
        分钟级，心跳协程完全饿死（实测远端两个凭据的心跳长期 149-157s，
        而同一端点的直接压测只要 0.05s）。轮询失败要快速失败、下一轮再来。
        """
        import aiohttp
        if getattr(self, "_poll_session_obj", None) is None or self._poll_session_obj.closed:
            self._poll_session_obj = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=4, force_close=False,
                                               enable_cleanup_closed=True,
                                               keepalive_timeout=15, ssl=False),
                timeout=aiohttp.ClientTimeout(total=8, connect=4, sock_read=5),
            )
        return self._poll_session_obj

    async def get_task_env_full(self, task_id: str) -> dict:
        """获取任务环境变量（含 LLM key + AK/SK）。cloud_worker 用这个替代直接查 DB。

        [沙箱迁移] cloud_worker._cloud_execute_task 调用本方法（含 USER_GITCODE_TOKEN /
        _LLM_* 注入链路）。旧分支移植——原迁移漏掉导致 cloud_worker 执行任务时
        AttributeError（client 无此方法）。与 get_task_env 的差异：retries=2。
        """
        data = await self._request("GET", f"/api/runtime/tasks/{task_id}/env", retries=2)
        if data:
            # [repo-llm-credential spec A] 透传 env 之外的显式失败标记（llm_cred_missing/
            # hint）——worker 据此不建箱、任务 fail 带指引
            _env = data.get("env", {})
            if data.get("llm_cred_missing"):
                _env["llm_cred_missing"] = True
                _env["llm_cred_source"] = data.get("llm_cred_source", "")
                _env["llm_cred_hint"] = data.get("llm_cred_hint", "")
            return _env
        return {}

    # [沙箱迁移 i7 漏移植恢复 2026-11-11] cloud_worker 经 remote_services 走 HTTP 签发/
    # 撤销 proxy token（B 机器无 DB）。此前 client 缺这两个方法 → worker claim 后
    # "proxy token issue failed: 'RuntimeClient' object has no attribute
    # 'issue_proxy_token_remote'"（仅 WARNING，grape_t_task_proxy_tokens 链路空转）。i7 client.py:434。
    async def issue_proxy_token_remote(self, task_id: str, backend_name: str = "") -> str:
        """cloud_worker 签发 proxy token（B 机器无 DB，通过 HTTP API）。返回 token 或空字符串。"""
        body = {"backend_name": backend_name} if backend_name else {}
        data = await self._request("POST", f"/api/runtime/tasks/{task_id}/proxy-token", body, retries=2)
        if data:
            return data.get("token", "")
        return ""

    async def revoke_proxy_token_remote(self, task_id: str) -> None:
        """cloud_worker 撤销 proxy token（B 机器无 DB，通过 HTTP API）。"""
        await self._request("DELETE", f"/api/runtime/tasks/{task_id}/proxy-token", {}, retries=1)

    # ===== [沙箱迁移] cloud_worker 专用 API（旧分支 7857a3592 移植——原迁移漏掉，
    # cloud_worker 启动/心跳/流控链路全部依赖这 4 个方法）=====

    async def sandbox_provider_report(self, platform: str, endpoint: str = "",
                                      api_key: str = "", ssl_cert: str = "",
                                      device_name: str = "", templates: list | None = None,
                                      template_engines: list | None = None) -> dict:
        """[沙箱迁移] 沙箱 worker 上报平台信息（端点/SSL/模板）。

        「生成沙箱凭据」一键安装的 cloud_worker 启动时调用——服务端
        UPSERT Provider + 登记模板（/api/runtime/sandbox-provider-report）。
        [引擎探测 spec] template_engines: 每模板预装引擎探测结果
        （[{"template_id", engines: [{engine, version, source}]}]）→ binding
        UPSERT status='detected'。
        失败仅返回空 dict（上报是增强能力，不阻塞 worker 启动）。
        """
        payload = {"platform": platform, "endpoint": endpoint, "api_key": api_key,
                   "ssl_cert": ssl_cert, "device_name": device_name,
                   "templates": templates or [],
                   "template_engines": template_engines or []}
        # [return_body 移植错位修复 2026-11-10] i7 的 _request 有 return_body 形参，
        # arch 线的没有——原调用传了不存在的 kwarg → 每次 TypeError 被下方 except
        # 吞掉返回 {} → worker 打"reported: provider_id= templates=0"假成功日志，
        # 服务端从未收到上报（S2 provider 模板/引擎永缺的根因）。改为不传该参，
        # 并把真实失败打 WARNING（不再吞成静默假成功）。
        try:
            data = await self._request("POST", "/api/runtime/sandbox-provider-report",
                                       payload, retries=2)
            return data or {}
        except Exception as _e:
            log.warning(f"sandbox_provider_report request failed: {_e}")
            return {}

    async def get_templates(self) -> list[dict]:
        """查询沙箱模板列表。cloud_worker 启动 + 心跳刷新时用。"""
        data = await self._request("GET", "/api/settings/sandbox-templates", retries=2)
        if data:
            return data.get("templates", [])
        return []

    async def get_provider_configs(self) -> dict:
        """拉取所有 provider 配置。返回 {provider_id: config_dict}。

        v3：按 provider_id 索引（不再是 provider_name 字符串）。
        和 get_templates() 同时机调用——启动时 + 心跳刷新时。
        config_dict 中 api_key 已解密为明文，含 platform/name/max_sandboxes 字段。
        """
        data = await self._request("GET", "/api/runtime/provider-configs", retries=2)
        if not data:
            return {}
        # v3：服务端返回 {providers: [...]}，按 provider_id 索引
        from src.domains.runtime.agents.sandbox.backends import get_adapter
        providers = data.get("providers", [])
        configs = {}
        for p in providers:
            _pid = p.get("id", 0)
            if not _pid:
                continue
            _platform = p.get("platform", "")
            adapter = get_adapter(_platform)
            if not adapter:
                log.error(f"no adapter for platform '{_platform}', provider {_pid} skipped")
                continue
            configs[_pid] = adapter.normalize_db_config(p)
        return configs

    async def whoami(self) -> dict:
        """凭据自述（GET /api/runtime/whoami, Bearer rta_）——[同机多实例 spec §4.1]。

        返回 {credential_id, name, cred_type, platform, scope, credential_uid}。
        消费方：改名跟随（name 比对）、实例键（credential_uid，锚简化 2026-11-04）、
        升级形态恢复。响应永不包含凭证字段（token/api_key）——泄露面为零。
        401 = 凭据吊销/无效（调用方计入自杀阈值，见 _heartbeat_401 计数）。
        """
        return await self._request("GET", "/api/runtime/whoami", retries=1) or {}

    async def get_cloud_runtimes(self) -> list[dict]:
        """查当前 credential 下已有的 cloud 类型 runtime（用于 _refresh_templates 首次初始化）。

        v3 R16：进程重启后 _last_runtimes 为空，无法对比删除。
        首次调用时从 DB 查同一 credential_id 的现有 cloud runtime 初始化 _last_runtimes。
        """
        data = await self._request("GET", "/api/runtime/cloud-runtimes", retries=2)
        if not data:
            return []
        return data.get("runtimes", [])

    async def get_provider_active_sandboxes(self, provider_id: int) -> dict:
        """查指定 Provider 的活跃沙箱数（用于流控校验）。

        返回 {"active_count": N, "max_sandboxes": M}。
        """
        data = await self._request("GET", f"/api/runtime/provider-configs/{provider_id}/active-sandboxes", retries=1)
        if not data:
            return {"active_count": 0, "max_sandboxes": 10}
        return data

    async def report_sandbox_status(self, task_id: str, issue_id: str, repo_key: str,
                                    sandbox_id: str, state: str = "running",
                                    provider: str = "", agent_installed: int = 0,
                                    template_id: str = "", template_name: str = "",
                                    engine_type: str = "", provider_id: int = 0,
                                    error: str = ""):
        """cloud_worker 上报沙箱状态（B 机器无 DB，通过 HTTP API 写 grape_t_cloud_sandboxes 表）。

        [沙箱迁移] template_id/template_name/engine_type 为建箱时快照——grape_t_cloud_sandboxes
        行内固化，防 runtime_info 刷新（换模板重绑）改写历史沙箱的展示。
        """
        await self._request("POST", f"/api/runtime/tasks/{task_id}/sandbox-status",
                            {"issue_id": issue_id, "repo_key": repo_key,
                             "sandbox_id": sandbox_id, "state": state,
                             "provider": provider, "agent_installed": agent_installed,
                             "template_id": template_id, "template_name": template_name,
                             "engine_type": engine_type, "provider_id": provider_id,
                             "error": error}, retries=2)

    async def task_start(self, task_id: str):
        """标记任务 running。返回 _request 结果（None=失败），调用方据此判断是否成功。"""
        return await self._request("POST", f"/api/runtime/tasks/{task_id}/start", {})

    async def heartbeat(self, runtime_ids: list[int], log_lines: list = None, metrics: dict = None, probe_results: dict = None) -> dict | None:
        """发送心跳。返回完整响应 dict（含 cancelled_tasks）或 None（网络错误/服务端不可达）。

        worker 调用后检查 result.get("status") == "ok" 判断成功，
        检查 result.get("cancelled_tasks", []) 发现取消任务并杀进程。

        log_lines: worker 最近的 ERROR/WARNING 日志条目列表
        [{ts, level, message}]，主控存入 data/logs/workers/{id}.log
        metrics: worker 系统资源指标 {cpu_percent, memory_percent, disk_percent, tmp_percent, bandwidth_percent, load_avg, spec}
        probe_results: worker 引擎探测结果 {engine_name: {status, latency, last_probe, error}}
        """
        payload = {"runtime_ids": runtime_ids}
        if log_lines:
            payload["log_lines"] = log_lines
        if metrics:
            payload.update(metrics)
        if probe_results:
            payload["probe_results"] = probe_results
        # Issue #1011：heartbeat v2-first——engine_uids 优先（uid 优先解析、失效回退
        # runtime_ids、全失效不造行）；空白 404/405 = 旧服务端 → 记忆性回退 v1
        _hb = await self._heartbeat_session()
        _uids = [self.uid_for(rid) for rid in (runtime_ids or [])]
        _uids = [u for u in _uids if u]
        if _uids and self._v2_enabled:
            payload["engine_uids"] = _uids
            status, data, text = await self._request_raw(
                "POST", "/api/runtime/heartbeat-v2", payload, retries=1)
            if status == 200:
                return data
            if self._is_blank_not_found(status, data, text):
                self._v2_enabled = False
                log.warning("heartbeat-v2 endpoint missing (old server?) -> switch to v1 and remember")
            else:
                log.warning(f"heartbeat-v2 failed (status={status}) -> fallback v1")
            payload.pop("engine_uids", None)
        result = await self._request("POST", "/api/runtime/heartbeat", payload, retries=2,
                                     session=_hb)
        return result

    async def claim(self, runtime_id: int, supports_obs_url: bool = False,
                    prefer_fallback: bool = False) -> dict | None:
        """claim（v3.0：携带能力声明——supports_obs_url/prefer_fallback 动态声明零落库）。
        返回值可能含 task 和 scan_request 两个字段。

        Issue #1011：claim v2-first（uid 寻址；uid 失效回退 v1 id；空白 404 记忆切 v1）。
        """
        _payload = {"supports_obs_url": supports_obs_url, "prefer_fallback": prefer_fallback}
        _uid = self.uid_for(runtime_id)
        if _uid and self._v2_enabled:
            _payload["runtime_id"] = runtime_id   # uid 失效时的兜底锚
            return await self._v2_first(
                f"/api/runtime/runtimes/uid/{_uid}/tasks/claim", _payload,
                self._claim_v1, runtime_id, supports_obs_url, prefer_fallback)
        return await self._claim_v1(runtime_id, supports_obs_url, prefer_fallback)

    async def _claim_v1(self, runtime_id: int, supports_obs_url: bool = False,
                        prefer_fallback: bool = False) -> dict | None:
        """v1 claim（id 寻址，旧路径原样保留——并存期与旧服务端共用）。"""
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
        _payload = {"supports_obs_url": supports_obs_url, "prefer_fallback": prefer_fallback}
        # Issue #1011：claim-by-tag v2-first（同主 claim）
        _uid = self.uid_for(runtime_id)
        if _uid and self._v2_enabled:
            _payload["runtime_id"] = runtime_id
            return await self._v2_first(
                f"/api/runtime/runtimes/uid/{_uid}/tasks/claim-by-tag/{tag}", _payload,
                self._claim_by_tag_v1, runtime_id, tag, supports_obs_url, prefer_fallback)
        return await self._claim_by_tag_v1(runtime_id, tag, supports_obs_url, prefer_fallback)

    async def _claim_by_tag_v1(self, runtime_id: int, tag: str,
                               supports_obs_url: bool = False,
                               prefer_fallback: bool = False) -> dict | None:
        """v1 claim-by-tag（id 寻址，旧路径原样保留；复用轮询会话）。"""
        _poll = await self._poll_session()
        data = await self._request("POST",
                                   f"/api/runtime/runtimes/{runtime_id}/tasks/claim-by-tag/{tag}",
                                   {"supports_obs_url": supports_obs_url, "prefer_fallback": prefer_fallback},
                                   retries=1, session=_poll)
        if not data:
            return None
        return data

    async def get_tags(self, runtime_id: int) -> list[str]:
        """Fetch this engine's claimable tags from server DB (replaces ENGINE_TAGS env).

        Called periodically by the claim loop to get the current tag list.
        Enables real-time tag updates via console without worker restart.
        """
        _poll = await self._poll_session()
        data = await self._request("GET", f"/api/runtime/runtimes/{runtime_id}/tags",
                                   retries=1, session=_poll)
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

    async def report_progress(self, task_id: str, summary: str = "", step: int = 0,
                              total: int = 0, engine_uid: str = ""):
        """Issue #1011：engine_uid 非空时 v2-first（按 uid 校验归属），空白 404 回退 v1。"""
        _uid = (engine_uid or "").strip().lower()
        if _uid and self._v2_enabled:
            status, data, text = await self._request_raw(
                "POST", f"/api/runtime/tasks/{task_id}/progress-v2",
                {"summary": summary, "step": step, "total": total, "engine_uid": _uid}, retries=1)
            if status == 200:
                return
            if self._is_blank_not_found(status, data, text):
                self._v2_enabled = False
            # 其余失败（含 uid 失效）回退 v1 口径上报
        await self._request("POST", f"/api/runtime/tasks/{task_id}/progress",
                            {"summary": summary, "step": step, "total": total}, retries=1)

    async def report_messages(self, task_id: str, messages: list):
        """三态返回（Issue #975，不再用 {} 冒充成功）：

        - dict：成功（服务端回执，含 status=="ok" 与 seq 范围回执）
        - None：任务已终态（410 Gone / 服务端状态确认 failed/cancelled/completed），调用方应中断
        - {}：网络错误（可重试）——调用方保留 batch 下个窗口重试，**不得当成功清空**
        - raise ReportBatchError：4xx 批次级失败（413/400 等）——调用方拆批重发或降级占位

        410 Gone 表示 task 已被服务端标终态（用户终止/超时），worker 应中断执行。
        网络错误（服务重启等）返回空 dict 让 worker 继续执行，
        避免服务短暂中断导致正在执行的 task 被误标 failed。
        """
        try:
            # QA 复审修复：本次调用私有 error_sink，避免并发请求互相覆盖
            # last_http_error 导致 413 被误判为网络错误（吞错语义回归）
            _sink: dict = {}
            # 重试预算必须覆盖「主控/网关滚动重启」的窗口：原来 retries=3 约 28s，
            # 一次部署重启就把整批消息打空（task_messages 零行 → 前端「无执行日志」，
            # 任务停在 running 无会话）。6 次指数退避约 3 分钟，足以跨过重启。
            data = await self._request("POST", f"/api/runtime/tasks/{task_id}/messages",
                                       {"messages": messages}, retries=6, error_sink=_sink)
            if data is None:
                _err = _sink or getattr(self, "last_http_error", None) or {}
                _status = _err.get("status") or 0
                # 410（任务终态）→ None，调用方中断
                if _status == 410:
                    return None
                # 4xx 批次级失败（413 payload too large / 400 bad request 等）→ 显式抛错，
                # 不得返回 {} 冒充成功（原缺陷根因，Issue #975）
                if 400 <= _status < 500 and _status not in NON_BATCH_4XX_STATUSES:
                    raise ReportBatchError(_status, _err.get("text") or "")
                # 其余（网络错误/重试耗尽/404 runtime gone）：查一次任务状态区分终态
                try:
                    state = await self._request("GET", f"/api/runtime/tasks/{task_id}/state", retries=1)
                    if state and state.get("status") in ("failed", "cancelled", "completed"):
                        log.info(f"report_messages: task {task_id} is {state.get('status')} on server, returning None")
                        return None  # task truly terminated
                except CredentialRevokedError:
                    raise  # 401 二次确认也吊销——上抛让执行中止
                except Exception:
                    pass
                # Network error (server restart) — don't abort, let worker continue
                log.warning(f"report_messages: task {task_id} network error (status={_status}), returning empty dict to continue")
                return {}
            return data
        except CredentialRevokedError:
            raise  # 凭据被吊销——不让外层当网络错误吞掉，调用方中止任务
        except ReportBatchError:
            raise  # 批次级失败显式上抛，由调用方拆批/降级（Issue #975）
        except Exception as e:
            log.warning(f"report_messages: task {task_id} exception: {e}, returning empty dict to continue")
            return {}

    async def report_usage(self, task_id: str, usage: dict):
        await self._request("POST", f"/api/runtime/tasks/{task_id}/usage", usage, retries=1)

    async def complete(self, task_id: str, result_text: str, exit_code: int, session_id: str = "",
                       work_dir: str = "", engine: str = "", run_token: str = "",
                       engine_uid: str = ""):
        # 必须 return：补报队列以 "resp is not None" 作为 ack 判据（漏 return 会导致条目
        # 永不出队、每 10s 无限重放，2026-10-19 补报重放事故根因）。
        # Issue #1011：engine_uid 非空时 v2-first（按 uid 校验归属，不一致 403 → 回退 v1）。
        _uid = (engine_uid or "").strip().lower()
        if _uid and self._v2_enabled:
            status, data, text = await self._request_raw(
                "POST", f"/api/runtime/tasks/{task_id}/complete-v2",
                {"result_text": result_text, "exit_code": exit_code,
                 "session_id": session_id, "work_dir": work_dir,
                 "engine": engine, "run_token": run_token, "engine_uid": _uid}, retries=1)
            if status == 200:
                return data or {}
            if self._is_blank_not_found(status, data, text):
                self._v2_enabled = False
            # uid 失效（403 MISMATCH/404）→ 回退 v1 口径（按 runtime_id 归属，语义不丢）
        return await self._request("POST", f"/api/runtime/tasks/{task_id}/complete",
                                   {"result_text": result_text, "exit_code": exit_code,
                                    "session_id": session_id, "work_dir": work_dir,
                                    "engine": engine, "run_token": run_token}, retries=4)

    async def fail(self, task_id: str, error: str, exit_code: int = -1, run_token: str = "",
                   engine_uid: str = ""):
        # 同上：fail 是补报队列最常用分支，漏 return 必现「无限重放 + 旧失败原因污染重派后事件」。
        _uid = (engine_uid or "").strip().lower()
        if _uid and self._v2_enabled:
            status, data, text = await self._request_raw(
                "POST", f"/api/runtime/tasks/{task_id}/fail-v2",
                {"error": error, "exit_code": exit_code, "run_token": run_token,
                 "engine_uid": _uid}, retries=1)
            if status == 200:
                return data or {}
            if self._is_blank_not_found(status, data, text):
                self._v2_enabled = False
        return await self._request("POST", f"/api/runtime/tasks/{task_id}/fail",
                                   {"error": error, "exit_code": exit_code, "run_token": run_token}, retries=4)

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

    async def recover_orphans(self, runtime_id: int, active_task_ids: list[str] | None = None) -> dict:
        """清理该 runtime 遗留任务（Issue #1750：可携带 worker 自报活跃白名单）。

        active_task_ids 为本 worker 内存 active_tasks 的 task id 列表；服务端命中白名单
        即 skipped_active，优先于年龄闸门与活跃窗口。None 时发送空 body，保持旧行为
        （启动注册清理上次遗留）且兼容旧服务端（字段可选）。

        返回服务端响应 dict（含 recovered/skipped_active/skipped_active_reason），
        便于 worker 观测白名单是否生效；请求失败返回空 dict。
        """
        body = {} if active_task_ids is None else {"active_task_ids": list(active_task_ids)}
        resp = await self._request("POST", f"/api/runtime/runtimes/{runtime_id}/recover-orphans",
                                   body, retries=2)
        return resp if isinstance(resp, dict) else {}

    async def report_skills(self, runtime_id: int, skills: list[dict]):
        """回报 skill 扫描结果给服务端（scan_request 指令的响应）。"""
        await self._request("POST", f"/api/runtime/runtimes/{runtime_id}/skills-report",
                            {"skills": skills}, retries=2)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        if self._connector:
            await self._connector.close()
        _hb = getattr(self, "_hb_session", None)
        if _hb is not None and not _hb.closed:
            await _hb.close()
        _poll = getattr(self, "_poll_session_obj", None)
        if _poll is not None and not _poll.closed:
            await _poll.close()
