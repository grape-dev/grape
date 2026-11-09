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
import os
import time
import uuid as _uuid

from fastapi import FastAPI, Request

from src.config import request_id_var, repo_var, user_var, log as _log

# webhook 路径中的 token 需要 redact（参考 multica redactWebhookPath）
_WEBHOOK_PREFIX = "/webhook/"

# 健康探针路径不记请求日志
_LOG_SKIP_PATHS = frozenset({"/health", "/healthz", "/readyz", "/metrics"})
# Worker 轮询路径跳过请求日志（13 台 worker × 3s × 3 请求 ≈ 24 行/秒）
_LOG_SKIP_PREFIXES = ("/api/runtime/runtimes/", "/api/runtime/heartbeat", "/api/runtime/tasks/", "/api/ping")


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
                _repo = f"gitcode:{_qp}" if not _qp.startswith("gitcode:") else _qp
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
                    from src.user.db import validate_token
                    _uid_val = validate_token(_auth[7:])
                    if _uid_val:
                        _uid = str(_uid_val)
            except Exception:
                pass
        token_uid = user_var.set(_uid)

        # Issue #372 §14.2.2: 已认证请求到达时从 Host 头推导 webhook_url / server_base_url
        if _uid:
            try:
                from src.user.db import get_meta_value, set_meta_value, detect_webhook_from_host
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

                if not get_meta_value("webhook_url"):
                    _candidate = detect_webhook_from_host(_host, _fwd_host, _fwd_proto)
                    if _candidate:
                        set_meta_value("webhook_url", _candidate)
                        if get_meta_value("webhook_url_detect_failed"):
                            set_meta_value("webhook_url_detect_failed", "")
                        _log.info(f"webhook_url auto-detected from Host header: {_candidate}")

                if _is_local_base(get_meta_value("server_base_url")):
                    _base_candidate = detect_webhook_from_host(_host, _fwd_host, _fwd_proto)
                    if _base_candidate:
                        _base = _base_candidate[: -len("/webhook/gitcode")]
                        set_meta_value("server_base_url", _base)
                        _log.info(f"server_base_url auto-detected from Host header: {_base}")
            except Exception:
                pass

        skip_log = path in _LOG_SKIP_PATHS or path.startswith(_LOG_SKIP_PREFIXES)
        start = time.time()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
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
                if status >= 500:
                    _log.error(msg, extra=extra)
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
                # 慢请求入库（方案C）
                try:
                    from src.config import SLOW_REQUEST_THRESHOLD_MS
                    if duration >= SLOW_REQUEST_THRESHOLD_MS:
                        try:
                            from src.session_manager import db as _sdb, db_lock as _sdbl
                            with _sdbl:
                                _sdb.execute(
                                    "INSERT INTO api_latency_log (request_id, method, path, status, duration_ms, repo_key, user_id, source) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'backend')",
                                    (rid, request.method, redacted_path, status, round(duration), _repo, _uid)
                                )
                                _sdb.commit()
                        except Exception:
                            pass  # 入库失败不影响请求
                except Exception:
                    pass
            request_id_var.reset(token_rid)
            repo_var.reset(token_repo)
            user_var.reset(token_uid)
