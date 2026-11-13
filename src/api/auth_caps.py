"""Token 能力矩阵（ADR-2；接口约定见仓库内 takeover 文档）。

背景
----
历史上"这个 token 能做什么"是靠散落在 20 余处的 `auth_type == "session_token"` 字符串判定。
每新增一种 token（例如接管用的 `tko_`）都要把这些点逐个找出来改一遍，漏一处就是越权或误拒。
本模块把"能力"收敛成一张表：新增 token 类型 = 加一行，判定点只调用 `has_capability()`。

能力位
------
- repo_session     : 可调用 session-only 端点（git-repo 代理 / workspace / git-credential），
                     并应接受 repo 域校验。worker 会话与接管会话都有。
- agent_self       : 可调用"agent 自证"端点（tasks/current、self-complete、awaiting-input、
                     pending-review/review-pass、session-bound scene）。仅 worker 会话有。
- user_scope       : 可调用"人"才能做的账户/平台管理端点（user_only）。API Key / JWT / internal 有。
- interactive_only : 必须是交互登录（JWT）。改密码、换邮箱等。

未知类型默认**拒绝**（fail-closed）：新增 token 类型必须显式登记，不存在"忘了加判定"。
"""

from __future__ import annotations

from typing import Final

from fastapi import Depends, HTTPException

# ---- token 类型（= user dict 里的 auth_type）----
SESSION_TOKEN: Final[str] = "session_token"   # tk_   worker/agent 执行期
TAKEOVER: Final[str] = "takeover"            # tko_  用户接管会话
API_KEY: Final[str] = "api_key"              # grape_ 用户长期 key
JWT: Final[str] = "jwt"                      # Web 登录
INTERNAL: Final[str] = "internal"            # 平台内部调用（autopilot 派发等）

# ---- 能力位 ----
REPO_SESSION: Final[str] = "repo_session"
AGENT_SELF: Final[str] = "agent_self"
USER_SCOPE: Final[str] = "user_scope"
INTERACTIVE_ONLY: Final[str] = "interactive_only"

_DENY: Final[dict[str, bool]] = {
    REPO_SESSION: False,
    AGENT_SELF: False,
    USER_SCOPE: False,
    INTERACTIVE_ONLY: False,
}

#: 能力矩阵（唯一事实源）。改这里必须同步 tests/test_auth_caps.py。
CAPABILITIES: Final[dict[str, dict[str, bool]]] = {
    SESSION_TOKEN: {REPO_SESSION: True,  AGENT_SELF: True,  USER_SCOPE: False, INTERACTIVE_ONLY: False},
    TAKEOVER:      {REPO_SESSION: True,  AGENT_SELF: False, USER_SCOPE: False, INTERACTIVE_ONLY: False},
    API_KEY:       {REPO_SESSION: False, AGENT_SELF: False, USER_SCOPE: True,  INTERACTIVE_ONLY: False},
    JWT:           {REPO_SESSION: False, AGENT_SELF: False, USER_SCOPE: True,  INTERACTIVE_ONLY: True},
    INTERNAL:      {REPO_SESSION: False, AGENT_SELF: False, USER_SCOPE: True,  INTERACTIVE_ONLY: False},
}

_warned: set[str] = set()


def auth_type_of(user: dict | None) -> str:
    """取 user 的 auth_type；缺省视为 jwt（与 get_current_user 的默认语义一致）。"""
    return str((user or {}).get("auth_type") or JWT)


def capabilities_of(auth_type: str | None) -> dict[str, bool]:
    """某类型的完整能力位；未知类型返回全 False（fail-closed）。"""
    row = CAPABILITIES.get(str(auth_type or JWT))
    if row is None:
        _warn_unknown(str(auth_type or ""))
        return dict(_DENY)
    return dict(row)


def has_capability(user: dict | None, cap: str) -> bool:
    """当前 user 是否具备某能力位。"""
    return bool(capabilities_of(auth_type_of(user)).get(cap))


def require_capability(cap: str):
    """构造一个 FastAPI 依赖：要求当前调用者具备 `cap`。

    用法：
        @router.get("/x", dependencies=[Depends(require_capability(REPO_SESSION))])
        def x(user: dict = Depends(get_current_user_or_api_key)): ...
    """

    def _dep(user: dict = Depends(_resolve_user())) -> dict:  # type: ignore[valid-type]
        if not has_capability(user, cap):
            raise _forbidden(cap, auth_type_of(user))
        return user

    return _dep


def require_user_scope():
    """`user_scope` 的语义化别名：账户/平台管理端点用。"""
    return require_capability(USER_SCOPE)


def _resolve_user():
    """延迟导入，避免 deps ↔ auth_caps 循环依赖。"""
    from src.api.deps import get_current_user_or_api_key
    return get_current_user_or_api_key


_MESSAGES: Final[dict[str, str]] = {
    USER_SCOPE: "Session token not allowed on this endpoint (account/platform management)",
    AGENT_SELF: "This endpoint is for the executing agent session only",
    REPO_SESSION: "This endpoint requires a repo-scoped session credential",
    INTERACTIVE_ONLY: ("This endpoint requires interactive login (JWT). "
                       "API keys and session tokens are not allowed."),
}


def _forbidden(cap: str, auth_type: str) -> HTTPException:
    return HTTPException(status_code=403, detail={
        "code": 403,
        "message": _MESSAGES.get(cap, f"Token type '{auth_type}' lacks capability '{cap}'"),
        "capability": cap,
        "auth_type": auth_type,
    })


def _warn_unknown(auth_type: str) -> None:
    """未知 token 类型只告警一次，便于发现漏登记。"""
    if auth_type in _warned:
        return
    _warned.add(auth_type)
    try:
        from src.kernel.config import log
        log.warning(f"auth_caps: unknown auth_type '{auth_type}' → all capabilities denied "
                    f"(add it to src/api/auth_caps.CAPABILITIES if this is a new token type)")
    except Exception as _e:
        log.debug(f"auth_caps best-effort skipped: {_e}")
