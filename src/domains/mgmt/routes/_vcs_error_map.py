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
"""

import re

from fastapi import HTTPException

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