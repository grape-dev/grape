"""VCS 上游错误 → HTTPException 统一映射（Issue #4169）。

背景：#383/#395 只在 `repos.py` 内联做了「上游 404/401/403 不折叠成 502」的分流，
`scenes.py` 的 /branches 端点各自 `except Exception → 502` 并把上游原文拼进 detail，
导致「仓库不存在/无权访问」与「代码托管平台网关故障」不可区分且泄露上游报文。
本模块把分流逻辑抽出共享，scenes / repos 复用同一口径。

上游错误有两种报文形态（都要支持，Issue #4169 实测）：

- `repos.py` 侧 RuntimeError（`repo_manager._req`）：
  ``GitCode API GET {url} failed (404): {body}`` → 正则 ``failed (\\d{3})``
- `VcsApiClient` 错误 dict：
  ``{"error": "HTTP 404", "status_code": 404, "detail": <上游全文>}`` → ``status_code`` 字段
  （超时/网络等非 HTTP 异常为 ``{"error": "timeout ..."}``，无 status_code → 502）

防泄露约定（#383/#395）：上游原文只落服务端 `log.warning`，一律不进响应体。

Issue #3922 补充：`map_vcs_error` 的读路径语义（无 `conflict_on` 的 409 → 502）被既有
契约用例锁定，不能直接放开 4xx 透传；因此**新增** `map_upstream_error(status_code, detail)`
作为写路径的单点映射——上游 4xx 逐档透传（400 带可读原因）、仅 5xx / 传输异常 → 502。
"""

import json
import re

from fastapi import HTTPException

from src.api.oauth_token import sanitize_upstream_error
from src.kernel.config import log

# 见模块 docstring：两种上游报文形态
_UPSTREAM_FAILED_CODE_RE = re.compile(r"failed \((\d{3})\)")
_UPSTREAM_HTTP_CODE_RE = re.compile(r"HTTP (\d{3})")

# 通用文案（不含上游原文/内部 URL）
_MESSAGE_REPO_NOT_FOUND = "仓库不存在或无权访问"
_MESSAGE_PERMISSION = "Token 权限不足或已过期，请更新后重试或改用 OAuth"
_MESSAGE_UPSTREAM_502 = "托管服务暂时不可用，请稍后重试"

# 托管链路 502 重试提示（repos.py 语义；其它端点用 retry_hint 覆盖）
_RETRY_HINT_HOST = ("可稍后重试；若 webhook 已创建但托管未完成，可使用「修复托管」自愈"
                    "（POST /repos/{owner}/{repo}/repair-host）")

# Issue #3922：4xx（客户端错误）透传的口径——文案兜底 + 重试提示（盲重试无效）
_MESSAGE_CLIENT_ERROR = "请求参数有误，请检查输入后重试"
_RETRY_HINT_CLIENT = "请按提示修正输入后重试；此类错误重试无效"

# 上游可读 message 的取值键（顺序即优先级）；`VcsApiClient` 把说明放在 detail 里
_MESSAGE_KEYS = ("error_message", "message", "msg", "detail",
                 "error_description", "reason", "error")

# 上游报文里的凭据字段：透传前必须脱敏（URL 由 sanitize_upstream_error 剥离）
# 值长 ≥8 才脱敏：避免把 "token: required" 这类可读原因一起吃掉
_SECRET_RE = re.compile(
    r"(?i)\b(access_token|private_token|refresh_token|token|authorization)\b"
    r"(\s*[=:]\s*)(\"?)([^\s&\"',;}]{8,})")
_SECRET_MASK = "***"

# 纯状态码形态（"HTTP 400"）不含语义，不能当作可读原因透传
_BARE_STATUS_RE = re.compile(r"(?i)HTTP\s+\d{3}")

# 上游 detail 里嵌套报文的最大解析层数（防畸形深嵌套）
_MAX_MESSAGE_DEPTH = 3


def _pick_message(value, depth: int = 0) -> str:
    """从上游 detail（dict / JSON 字符串 / 异常）里抽出面向用户的可读 message。"""
    if value is None or depth > _MAX_MESSAGE_DEPTH:
        return ""
    if isinstance(value, dict):
        for key in _MESSAGE_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for key in _MESSAGE_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, (dict, list)):
                nested = _pick_message(candidate, depth + 1)
                if nested:
                    return nested
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text[0] in "{[":
        try:
            return _pick_message(json.loads(text), depth + 1)
        except (ValueError, TypeError):
            return text
    # 异常形态 ``... failed (400): {json}`` → 只取报文尾部再解析，避免前缀噪声进用户文案
    match = _UPSTREAM_FAILED_CODE_RE.search(text)
    if match:
        tail = text[match.end():].lstrip(": ").strip()
        if tail and tail != text:
            return _pick_message(tail, depth + 1)
    return text


def upstream_error_message(detail) -> str:
    """上游 detail → 可透传的短消息（剥离内部 URL、脱敏凭据、截断）。

    Args:
        detail: 上游错误 dict / 原始报文 / 异常对象。
    """
    message = sanitize_upstream_error(_pick_message(detail))
    if _BARE_STATUS_RE.fullmatch(message):
        # 只有 "HTTP 400" 这类状态码时不算「可读原因」，交给调用方兜底文案
        return ""
    message = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{_SECRET_MASK}",
                             message)
    return message.strip()[:300]


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _error_text(err_or_data) -> str:
    """错误对象 -> 用于匹配的文本（dict 拼接 error/detail）。"""
    if isinstance(err_or_data, dict):
        return " ".join(
            str(err_or_data.get(k) or "") for k in ("error", "detail"))
    return str(err_or_data or "")


def upstream_status_code(err_or_data) -> int:
    """提取上游 HTTP 状态码；非 HTTP 错误（超时/网络/无码）返回 0。

    状态码字段优先（VcsApiClient 错误 dict 自带 `status_code`），再按
    ``failed (404)``（repos 形态）/ ``HTTP 404``（scenes 形态）双正则兜底。

    Args:
        err_or_data: 上游异常（Exception）或 `VcsApiClient` 错误 dict。
    """
    if isinstance(err_or_data, dict):
        code = _as_int(err_or_data.get("status_code"))
        if code:
            return code
        # dict 只用 error 文本兜底：detail 是上游全文，其中数字可能被误判成状态码
        text = str(err_or_data.get("error") or "")
    else:
        text = str(err_or_data or "")
    for pattern in (_UPSTREAM_FAILED_CODE_RE, _UPSTREAM_HTTP_CODE_RE):
        match = pattern.search(text)
        if match:
            return _as_int(match.group(1))
    return 0


def host_502_detail(stage: str, err_or_data, message: str = "",
                    host_status: str = "partial", missing: list | None = None,
                    step: str = "", retry_hint: str = "") -> dict:
    """构造带上下文的 502 detail（Issue #455 / #2126 / #4169）。

    - 不把上游内部 URL/报文透出给客户端（Issue #383/#395 防泄露约束），原文仅落日志；
    - 401/403/404 为权限/资源类错误，不标记 retryable（盲重试无意义）；
    - 网络类/5xx 标记 retryable，并给出重试路径提示；
    - 统一携带 host_status / missing / step，前端可直接渲染缺失项。

    Args:
        retry_hint: 面向用户的重试提示；缺省为托管链路文案（场景/分支等其它端点自传）。
    """
    code = upstream_status_code(err_or_data)
    retryable = code not in (401, 403, 404)
    return {
        "code": 502,
        "error_code": f"UPSTREAM_{stage.upper()}" + (f"_{code}" if code else ""),
        "message": message or _MESSAGE_UPSTREAM_502,
        "host_status": host_status,
        "missing": list(missing or []),
        "step": step or stage,
        "upstream": {"stage": stage, "status": code or None, "retryable": retryable},
        "retryable": retryable,
        "retry_hint": retry_hint or _RETRY_HINT_HOST,
    }


def map_vcs_error(err_or_data, *, conflict_on: tuple = (), conflict_markers: tuple = (),
                  stage: str = "vcs", message: str = "",
                  conflict_message: str = "分支已存在",
                  permission_message: str = "",
                  log_prefix: str = "", host_status: str = "partial",
                  missing: list | None = None, step: str = "",
                  retry_hint: str = "") -> HTTPException:
    """把上游错误映射为语义化 HTTPException（调用方 `raise` 即可）。

    - 上游 404 → 404「仓库不存在或无权访问」（业务语义，而非网关故障）；
    - 上游 401/403 → 403 `TOKEN_PERMISSION_INSUFFICIENT`（供前端统一映射）；
    - `conflict_on`/`conflict_markers` 命中 → 409 `conflict_message`（仅创建类端点传）；
    - 其余（网络/超时/上游 5xx/无码）→ 502 `host_502_detail`（不含上游原文）。

    副作用仅一条 `log.warning`（上游原文只落服务端日志）。

    Args:
        err_or_data: 上游异常或 `VcsApiClient` 错误 dict。
        conflict_on: 视为「已存在」冲突的上游状态码（GET 不传，POST 传 (409, 422)）。
        conflict_markers: 报文文本标记（上游忽略状态码只给文案时兜底）。
    """
    code = upstream_status_code(err_or_data)
    log.warning(f"{log_prefix or 'vcs upstream'} failed (status={code or 'unknown'}): {err_or_data}")
    if code == 404:
        return HTTPException(status_code=404, detail={"code": 404, "message": _MESSAGE_REPO_NOT_FOUND})
    if code in (401, 403):
        return HTTPException(status_code=403, detail={
            "code": 403,
            "error_code": "TOKEN_PERMISSION_INSUFFICIENT",
            "message": permission_message or _MESSAGE_PERMISSION,
        })
    if code in tuple(conflict_on):
        return HTTPException(status_code=409, detail={"code": 409, "message": conflict_message})
    if conflict_markers:
        text = _error_text(err_or_data)
        if any(marker and marker in text for marker in conflict_markers):
            return HTTPException(status_code=409, detail={"code": 409, "message": conflict_message})
    return HTTPException(status_code=502, detail=host_502_detail(
        stage, err_or_data, message=message, host_status=host_status,
        missing=missing, step=step, retry_hint=retry_hint))


def _detail_with_status(code: int, detail):
    """给 502 映射补上已知状态码（detail 自身取不到时以调用方传入的 `code` 为准）。"""
    if not code or upstream_status_code(detail) == code:
        return detail
    return {"error": f"HTTP {code}", "status_code": code, "detail": detail}


def map_upstream_error(status_code, detail="", *, stage: str = "vcs", message: str = "",
                       permission_message: str = "",
                       conflict_on: tuple = (), conflict_markers: tuple = (),
                       conflict_message: str = "分支已存在",
                       retry_hint: str = "", log_prefix: str = "",
                       host_status: str = "partial", missing: list | None = None,
                       step: str = "") -> HTTPException:
    """上游错误 → 语义化 HTTPException（Issue #3922 写路径单点映射）。

    与 `map_vcs_error` 的差别只有一条：**4xx 逐档透传**而不是折叠成 502——
    「用户输入非法 / 参数错误」属于客户端错误，映射成网关故障既误导用户又污染 5xx 告警口径。

    判定顺序（先冲突后透传：上游常以 400 + "already exists" 表达冲突）：
    1. `conflict_markers` 文本命中 / `conflict_on` 命中 → 409 `conflict_message`；
    2. 404 → 404「仓库不存在或无权访问」；
    3. 401/403 → 403 `TOKEN_PERMISSION_INSUFFICIENT`；
    4. 其余 4xx（400/405/406/410/413/415/422/429 ...）→ **同码透传**，message 取上游可读
       原因并强制经 `upstream_error_message()` 脱敏（无可用原因时用 `message` 兜底）；
    5. 5xx / 传输异常（无状态码）→ 502 `host_502_detail`（不含上游原文）。

    副作用仅一条 `log.warning`（上游原文只落服务端日志，不进响应体）。

    Args:
        status_code: 上游 HTTP 状态码（0/None = 超时/网络等非 HTTP 异常）。
        detail: 上游错误 dict / 原始报文 / 异常对象（其可读 message 会脱敏后透传）。
        stage: 链路阶段（用于 `error_code` 前缀与日志）。
        message: 4xx 无可用上游原因时的兜底文案 / 502 的面向用户文案。
        permission_message: 401/403 的面向用户文案（缺省用通用 Token 提示）。
        conflict_on: 视为「已存在」冲突的上游状态码（仅创建类端点传）。
        conflict_markers: 报文文本标记（上游忽略状态码只给文案时兜底）。
        conflict_message: 409 的面向用户文案。
        retry_hint: 面向用户的重试提示（4xx 默认「此类错误重试无效」）。
        log_prefix: 日志前缀。
        host_status / missing / step: 透传给 `host_502_detail` 的结构化字段。
    """
    code = _as_int(status_code)
    log.warning(f"{log_prefix or 'vcs upstream'} failed (status={code or 'unknown'}): {detail}")
    if conflict_markers:
        text = _error_text(detail)
        if any(marker and marker in text for marker in conflict_markers):
            return HTTPException(status_code=409,
                                 detail={"code": 409, "message": conflict_message})
    if code in tuple(conflict_on):
        return HTTPException(status_code=409,
                             detail={"code": 409, "message": conflict_message})
    if code == 404:
        return HTTPException(status_code=404,
                             detail={"code": 404, "message": _MESSAGE_REPO_NOT_FOUND})
    if code in (401, 403):
        return HTTPException(status_code=403, detail={
            "code": 403,
            "error_code": "TOKEN_PERMISSION_INSUFFICIENT",
            "message": permission_message or _MESSAGE_PERMISSION,
        })
    if 400 <= code < 500:
        return HTTPException(status_code=code, detail={
            "code": code,
            "error_code": f"UPSTREAM_{stage.upper()}_{code}",
            "message": upstream_error_message(detail) or message or _MESSAGE_CLIENT_ERROR,
            "upstream": {"stage": stage, "status": code, "retryable": False},
            "retryable": False,
            "retry_hint": retry_hint or _RETRY_HINT_CLIENT,
        })
    return HTTPException(status_code=502, detail=host_502_detail(
        stage, _detail_with_status(code, detail), message=message,
        host_status=host_status, missing=missing, step=step, retry_hint=retry_hint))
