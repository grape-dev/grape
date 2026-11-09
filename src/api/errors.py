"""Grape API 错误码统一约定（docs/weekly-03/error-code-standard-design.md）。

统一错误返回契约：
- HTTP 错误：`HTTPException(detail={"code": <status>, "message": "<English>"}）`
  —— 供 Agent 可读；前端展示语言由 i18n 映射。
- CLI/业务失败：`{"ok": False, "code": N, "message": "<English>"}`（HTTP 200 但业务失败）。
`detail.code` 默认 = HTTP 状态码；业务细分可用 4 位子码（如 40401）。
"""

from fastapi import HTTPException
from fastapi.responses import JSONResponse

UNPROCESSABLE_ENTITY = 422

# ===== 状态码常量 =====
OK = 200
CREATED = 201
NO_CONTENT = 204
BAD_REQUEST = 400
UNAUTHORIZED = 401
FORBIDDEN = 403
NOT_FOUND = 404
CONFLICT = 409
GONE = 410
TOO_MANY_REQUESTS = 429
INTERNAL_SERVER_ERROR = 500
BAD_GATEWAY = 502
SERVICE_UNAVAILABLE = 503


def api_error(status: int, message: str, internal: str = "") -> HTTPException:
    """构造统一 HTTP 错误响应（Agent 可读英文 message）。

    - status: HTTP 状态码（默认取常量大写命名）。
    - message: 英文，对外返回；Agent 据此解析与决策。
    - internal: 可选内部信息（错误码/追踪），不暴露敏感信息；仅记录日志用。
    """
    detail = {"code": status, "message": message}
    if internal:
        detail["internal"] = internal
    return HTTPException(status_code=status, detail=detail)


def business_ok_false(code: int, message: str) -> dict:
    """CLI/业务层失败返回体（HTTP 200 但业务失败，body 带 ok 标志）。

    用于 tasks/notify 等命令语义端点：调用方凭 `ok=False` 判失败。
    """
    return {"ok": False, "code": code, "message": message}


def extract_allowed_values(pattern: str) -> list:
    """从 pattern regex 解析合法值枚举（pattern 为合法值真相源，禁止硬编码漂移）。

    支持 `^(a|b|c)$` / `^(a|b|)$` 形式；解析失败返回空列表（调用方降级）。
    """
    if not pattern:
        return []
    body = pattern.strip()
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$"):
        body = body[:-1]
    body = body.strip()
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    values = [v for v in body.split("|")]
    # 去掉空串占位（`|)` 尾部空分支表示"可选空值"），保留用户可感知的非空合法值
    cleaned = [v for v in values if v]
    return cleaned[:64]


def validation_error_response(exc) -> JSONResponse:
    """将 FastAPI RequestValidationError 转为统一 {code, message} 信封（Issue #683）。

    message 携带非法字段名与合法值枚举（从 ctx.pattern 解析），Agent 可据此自纠命令；
    多个错误用 "; " 拼接。
    """
    parts = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", [])]
        field = ".".join(p for p in loc if p not in ("query", "body", "path"))
        if not field and loc:
            field = loc[-1]
        msg = str(err.get("msg", "invalid value"))
        ctx = err.get("ctx") or {}
        pattern = ctx.get("pattern") or ""
        allowed = extract_allowed_values(pattern)
        source = loc[0] if loc else "query"
        if allowed:
            parts.append(
                f"{source} param '{field}' invalid: allowed values are "
                f"[{', '.join(allowed)}]")
        else:
            parts.append(f"{source} param '{field}' invalid: {msg}")
    message = "; ".join(parts) if parts else "request validation failed"
    return JSONResponse(status_code=UNPROCESSABLE_ENTITY,
                        content={"code": UNPROCESSABLE_ENTITY, "message": message})