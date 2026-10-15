"""Bot Token 401 识别公共函数（Issue #345 方向 3）。

写操作（建单/评论/关单）走 bot token，token 失效时需要返回明确的
"Bot Token 可能失效"提示，而非通用 502 错误。

两条异常传播路径：
- 建单/评论：调 _req / _post_comment → 抛 RuntimeError，消息格式 "failed (401): ..."
- 关单：调 VcsApiClient.close_issue → 返回 dict {"error": "HTTP 401", "detail": ...}

本模块提供统一的 401 识别 + 提示文案常量，三处写操作路径共用。
"""
import re

# 统一提示文案——建单/评论/关单三处引用同一常量
BOT_TOKEN_INVALID_MSG = "Bot Token 可能失效，请检查仓库 Bot Token 配置"


def extract_http_status_from_exc(exc: Exception) -> int | None:
    """从 RuntimeError 消息提取 HTTP status code。

    异常消息格式: "GitCode API ... failed (401): ..." / "GitCode comment failed (403): ..."
    """
    msg = str(exc)
    codes = re.findall(r'\((\d{3})\)', msg)
    return int(codes[0]) if codes else None


def extract_http_status_from_result(result) -> int | None:
    """从 VcsApiClient 返回的 dict 提取 HTTP status code。

    格式: {"error": "HTTP 401", "detail": "..."}
    """
    err = str(result.get("error", "")) if isinstance(result, dict) else ""
    m = re.match(r'HTTP (\d{3})', err)
    return int(m.group(1)) if m else None


def is_auth_error_exc(exc: Exception) -> bool:
    """判断异常是否为 401 认证失败（bot token 失效）。"""
    return extract_http_status_from_exc(exc) == 401


def is_auth_error_result(result) -> bool:
    """判断 VcsApiClient 返回值是否为 401 认证失败（bot token 失效）。

    VcsApiClient._do_request 不抛异常，HTTP 错误返回 {"error": "HTTP 401", ...}。
    """
    if not isinstance(result, dict) or "error" not in result:
        return False
    return extract_http_status_from_result(result) == 401
