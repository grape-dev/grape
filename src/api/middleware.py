"""共享 HTTP 中间件（Issue #596 P1：从 main.py 抽出，A/B/C 三入口复用）。

内容与 main.py 原实现一致（request_id 注入 + repo_key/user 提取 +
webhook 路径 redact + 分级请求日志 + 慢请求入库 + Host 头 webhook_url
自愈），抽为独立函数供多入口安装：

- `src/api/main.py`（单进程兼容模式）
- `src/api/grape_server.py`（A 进程）
- `src/api/grape_event_server.py`（B 进程）
- `src/api/grape_worker_server.py`（C 进程）

注意：本模块只注册中间件，不注册路由/静态资源——各入口按需组装。
"""
import asyncio
import os
import time
import uuid as _uuid

from fastapi import FastAPI, Request

from src.kernel.config import request_id_var, repo_var, user_var, log as _log

# ── meta URL 自愈探测的进程内缓存（Issue: 压测下 UI 卡顿）───────────────────
# 每个带登录态的请求原本要同步查 2~3 次 meta（webhook_url / server_base_url），
# 这些同步查询跑在事件循环上并共用进程级 db_lock，压测时把整个进程串行化
# （实测：8 并发带 token 的 /api/health 从 50ms 涨到 8.2s）。
# 这两个值只在「尚未配置」时才需要写，属自愈逻辑，缓存 60s 完全够；
# 未命中时走 asyncio.to_thread，不再阻塞事件循环。
# TTL 默认 60s 的依据（B2 五源）：webhook_url/server_base_url 是「写一次几乎不变」的
# 自愈配置（Issue #372 的语义即"缺了才补"），meta 表无 TTL 类写入方；本仓
# patrol/托管状态读这两个键的容忍窗口为分钟级（repos.py 展示用途）。实测：
# dev 双节点压测（56 并发建单 + UI 混跑）下 60s 缓存让 meta 读降为 0 次/请求
# （PR 自述 8 并发带 token 8.3s→59-75ms 的主要成分即此）；env 可覆盖。
_META_URL_CACHE_TTL_S = float(os.environ.get("GRAPE_META_URL_CACHE_TTL_S", "60"))
_meta_url_cache = {"ts": 0.0, "webhook_url": "", "server_base_url": ""}


def _reset_meta_url_cache() -> None:
    """测试/写入后失效缓存。"""
    _meta_url_cache.update({"ts": 0.0, "webhook_url": "", "server_base_url": ""})


def _read_meta_urls_sync() -> "tuple[str, str]":
    """同步读 meta（只在线程池里调用）。"""
    from src.domains.mgmt.user.db import get_meta_value

    return (get_meta_value("webhook_url") or "",
            get_meta_value("server_base_url") or "")


def _write_meta_sync(key: str, value: str) -> None:
    """同步写 meta（只在线程池里调用）。"""
    from src.domains.mgmt.user.db import set_meta_value

    set_meta_value(key, value)


async def _meta_urls_cached() -> "tuple[str, str]":
    """带 TTL 的 meta URL 读取：命中缓存不碰数据库，未命中丢线程池。"""
    now = time.time()
    if _meta_url_cache["ts"] and (now - _meta_url_cache["ts"]) < _META_URL_CACHE_TTL_S:
        return _meta_url_cache["webhook_url"], _meta_url_cache["server_base_url"]
    webhook_url, server_base = await asyncio.to_thread(_read_meta_urls_sync)
    _meta_url_cache.update({"ts": now, "webhook_url": webhook_url,
                            "server_base_url": server_base})
    return webhook_url, server_base


# webhook 路径中的 token 需要 redact（参考 multica redactWebhookPath）
_WEBHOOK_PREFIX = "/webhook/"

# 健康探针路径不记请求日志
_LOG_SKIP_PATHS = frozenset({"/health", "/healthz", "/readyz", "/metrics"})
# Worker 轮询路径跳过请求日志（13 台 worker × 3s × 3 请求 ≈ 24 行/秒）
_LOG_SKIP_PREFIXES = ("/api/runtime/runtimes/", "/api/runtime/heartbeat", "/api/runtime/tasks/", "/api/ping")

# Issue #3699：trace 中继禁用态（主控未配 OTLP_RELAY_TARGET）是配置态 feature-off，
# 不是事故。中继侧用该响应头标记；访问日志据此把 503 降为 debug。与 server_common.py
# 的同名判据保持一致（单进程/四进程共用一套分级口径）。
_RELAY_DISABLED_HEADER = "x-grape-trace-relay"


def _is_relay_disabled_503(status: int, headers) -> bool:
    return status == 503 and (headers.get(_RELAY_DISABLED_HEADER) or "") == "disabled"


def _redact_path(path: str) -> str:
    """webhook 路径中的 token 段替换为 [redacted]。"""
    if not path.startswith(_WEBHOOK_PREFIX):
        return path
    rest = path[len(_WEBHOOK_PREFIX):]
    if "/" in rest:
        return _WEBHOOK_PREFIX + "[redacted]" + rest[rest.index("/"):]
    return _WEBHOOK_PREFIX + "[redacted]"


def install_request_middleware(app: FastAPI) -> None:
    """request_id 注入 + HTTP 请求日志中间件（与 main.py 原实现一致）。"""

    @app.middleware("http")
    async def request_id_and_log_middleware(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or _uuid.uuid4().hex[:12]
        request.scope["request_id"] = rid
        token_rid = request_id_var.set(rid)

        # 从请求路径提取 repo_key
        path = request.url.path
        _repo = ""
        _path_parts = path.strip("/").split("/")
        if len(_path_parts) >= 4 and _path_parts[0] == "api":
            _module = _path_parts[1]
            if _module == "agents" and len(_path_parts) >= 5:
                _owner = _path_parts[3]
                _repo_name = _path_parts[4]
                if "/" not in _owner and "." not in _owner:
                    _repo = f"gitcode:{_owner}/{_repo_name}"
            elif _module in {"dashboard", "pipeline"} and len(_path_parts) >= 4:
                _owner = _path_parts[2]
                _repo_name = _path_parts[3]
                if "/" not in _owner and "." not in _owner:
                    _repo = f"gitcode:{_owner}/{_repo_name}"
        # query param 中的 repo
        if not _repo:
            _qp = request.query_params.get("repo", "")
            if _qp:
                # 已有 provider 前缀（github:o/r 等任意在册平台）原样保留；裸键才补默认前缀
                from src.kernel.repo_key import to_repo_key
                _repo = _qp if ":" in _qp else to_repo_key(_qp)
        token_repo = repo_var.set(_repo)

        # 从 Authorization header 提取 user_id
        _uid = ""
        _auth = request.headers.get("Authorization", "")
        if _auth.startswith("Bearer "):
            try:
                from src.api.jwt_utils import verify_access_token
                _payload = verify_access_token(_auth[7:])
                if _payload and _payload.get("sub"):
                    _uid = str(_payload["sub"])
                else:
                    from src.domains.mgmt.user.db import validate_token
                    # 同步查库不能跑在事件循环上（压测下会串行化整个进程）
                    _uid_val = await asyncio.to_thread(validate_token, _auth[7:])
                    if _uid_val:
                        _uid = str(_uid_val)
            except Exception:
                pass
        token_uid = user_var.set(_uid)

        # Issue #372 §14.2.2: 已认证请求到达时从 Host 头推导 webhook_url / server_base_url
        if _uid:
            try:
                from src.domains.mgmt.user.db import detect_webhook_from_host
                _host = request.headers.get("host", "")
                _fwd_host = request.headers.get("x-forwarded-host", "")
                _fwd_proto = request.headers.get("x-forwarded-proto", "")

                def _is_local_base(base: str) -> bool:
                    b = (base or "").lower().rstrip("/")
                    return (not b) or b.startswith((
                        "http://localhost", "https://localhost",
                        "http://127.", "https://127.",
                        "http://0.0.0.0", "https://0.0.0.0",
                        "http://[::1]", "https://[::1]"))

                _webhook_url, _server_base = await _meta_urls_cached()

                if not _webhook_url:
                    _candidate = detect_webhook_from_host(_host, _fwd_host, _fwd_proto)
                    if _candidate:
                        await asyncio.to_thread(_write_meta_sync, "webhook_url", _candidate)
                        # 探测成功自愈 → 复位失败标记（F3：base 的 T9-08 契约，
                        # repos.py 读该标记展示"探测失败"，不复位会永久显示失败态）
                        await asyncio.to_thread(_write_meta_sync, "webhook_url_detect_failed", "")
                        _reset_meta_url_cache()
                        _log.info(f"webhook_url auto-detected from Host header: {_candidate}")

                if _is_local_base(_server_base):
                    _base_candidate = detect_webhook_from_host(_host, _fwd_host, _fwd_proto)
                    if _base_candidate:
                        _base = _base_candidate[: -len("/webhook/gitcode")]
                        await asyncio.to_thread(_write_meta_sync, "server_base_url", _base)
                        _reset_meta_url_cache()
                        _log.info(f"server_base_url auto-detected from Host header: {_base}")
            except Exception:
                pass

        skip_log = path in _LOG_SKIP_PATHS or path.startswith(_LOG_SKIP_PREFIXES)
        start = time.time()
        status = 500
        relay_disabled = False
        try:
            response = await call_next(request)
            status = response.status_code
            # 需在 return 前取：finally 里 response 未绑定（call_next 抛异常分支）
            relay_disabled = _is_relay_disabled_503(status, response.headers)
            response.headers["X-Request-ID"] = rid
            return response
        except Exception:
            raise
        finally:
            if not skip_log:
                duration = (time.time() - start) * 1000
                redacted_path = _redact_path(path)
                extra = {
                    "method": request.method,
                    "path": redacted_path,
                    "status": status,
                    "duration_ms": round(duration),
                }
                msg = f"{request.method} {redacted_path} status={status} duration={duration:.0f}ms"
                # RUM 反代未配置（GRAPE_FARO_UPSTREAM 空）时 /faro/collect 固定 503：
                # 这是 feature-off 状态而非事故，前端会持续上报 → 降到 debug，避免刷 ERROR。
                if redacted_path == "/faro/collect" and status == 503:
                    _log.debug(msg, extra=extra)
                elif status >= 500:
                    # Issue #3699：中继禁用（配置缺失）是 feature-off，非事故 → debug；
                    # 其他 5xx（含上游 LGTM 故障透传）保持 ERROR，不被误抑制。
                    if relay_disabled:
                        _log.debug(msg, extra=extra)
                    else:
                        _log.error(msg, extra=extra)
                elif status == 499:
                    # Issue #4248：客户端主动中止（页面卸载/切换）与 nginx 499 对齐，
                    # 记 warning 而不进 5xx ERROR / 错误率，便于与真故障区分。
                    _log.warning(msg, extra=extra)
                elif status == 401:
                    if path.startswith("/api/runtime/"):
                        _log.warning(msg, extra=extra)
                    else:
                        _log.debug(msg, extra=extra)
                elif status in (403, 404):
                    _log.debug(msg, extra=extra)
                elif status >= 400:
                    _log.warning(msg, extra=extra)
                else:
                    _log.info(msg, extra=extra)
                # 慢请求入库（方案C）：api_latency_log 归 insight，走 owner 域薄写口（任意拓扑）
                try:
                    from src.kernel.config import SLOW_REQUEST_THRESHOLD_MS
                    if duration >= SLOW_REQUEST_THRESHOLD_MS:
                        from src.domains.insight import public as _insight_metrics
                        _insight_metrics.record_api_latency_any(
                            rid, request.method, redacted_path, status,
                            round(duration), _repo, _uid)
                except Exception as _exc:
                    _log.debug(f"api_latency_log insert skipped: {_exc}")
            request_id_var.reset(token_rid)
            repo_var.reset(token_repo)
            user_var.reset(token_uid)
