"""
统一 cookie 常量和辅助函数（D13 修复）。

auth.py 和 grape_auth.py 原各自定义了 _COOKIE_NAME / _COOKIE_PATH /
_COOKIE_MAX_AGE / _set_refresh_cookie / _clear_refresh_cookie / _app_base，
此处提取为单一来源，消除重复。
"""

import os

from fastapi import Response

# HttpOnly cookie 配置
COOKIE_NAME = "refresh_token"
COOKIE_PATH = "/api/auth"
COOKIE_MAX_AGE = 7 * 24 * 3600  # 7 天


def _cookie_secure() -> bool:
    """从环境变量判断是否启用 Secure 标志（默认 False，生产环境设 COOKIE_SECURE=true）。"""
    return os.environ.get("COOKIE_SECURE", "false").lower() in ("true", "1", "yes")


def set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """设置 refresh_token HttpOnly cookie——JS 不可读，防 XSS。"""
    response.set_cookie(
        key=COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        path=COOKIE_PATH,
        max_age=COOKIE_MAX_AGE,
    )


def clear_refresh_cookie(response: Response) -> None:
    """清除 refresh_token cookie——logout 时调用。

    BUG-14 修复：delete_cookie 的 secure/samesite 属性必须与 set 时一致，
    否则浏览器不会执行删除（同站点策略要求删除请求的属性匹配设置时）。
    """
    response.delete_cookie(
        key=COOKIE_NAME,
        path=COOKIE_PATH,
        secure=_cookie_secure(),
        samesite="strict",
    )


def get_refresh_token(request) -> str:
    """从请求中读取 refresh_token cookie。"""
    return request.cookies.get(COOKIE_NAME, "")


def app_base() -> str:
    """平台部署在子路径时的 URL 前缀。"""
    return (os.environ.get("APP_BASE_PATH", "") or "").rstrip("/")
