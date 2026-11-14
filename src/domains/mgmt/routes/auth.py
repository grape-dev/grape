"""认证路由：token 续期、用户信息、登出

D1 修复后仅保留 3 个路由（POST /refresh、GET /profile、POST /logout）。
密码登录和 OAuth 路由统一由 grape_auth.py 提供。

JWT 双 token 机制：
  - access_token（15分钟）——存 localStorage，JS 读取加到 header
  - refresh_token（7天滚动续期）——存 HttpOnly cookie，JS 不可读
  - token_version——改密码/logout 时 +1，旧 token 失效
"""
import logging

from fastapi import APIRouter, HTTPException, Depends, Request, Response
from src.domains.mgmt.user import db as user_db
from src.api.deps import require_no_session_token

log = logging.getLogger(__name__)
from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_username, increment_token_version as grape_inc
from src.kernel.auth_sdk.cookie_utils import (
    set_refresh_cookie as _set_refresh_cookie,
    clear_refresh_cookie as _clear_refresh_cookie,
    get_refresh_token,
    app_base as _app_base,
)
import os

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/refresh")
def refresh_token(request: Request, response: Response):
    """用 refresh_token 换新的 access_token + refresh_token（滚动续期）。

    D3 修复：复用 deps.py 的 get_user_status_cached 校验 token_version + status。
    """
    # CSRF 保护
    xrw = request.headers.get("X-Requested-With", "")
    if xrw != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "缺少 CSRF header"})
    # refresh_token 从 HttpOnly cookie 读
    refresh = get_refresh_token(request)
    if not refresh:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "MISSING_REFRESH_TOKEN", "message": "缺少 refresh_token"})
    from src.kernel.auth_sdk.jwt_utils import verify_refresh_token, create_access_token, create_refresh_token
    payload = verify_refresh_token(refresh)
    if not payload:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "REFRESH_TOKEN_INVALID", "message": "refresh_token 过期或无效"})
    uid = str(payload.get("sub") or "").strip()   # D105 第二批：sub 字符串化（老 int 兼容）
    username = payload.get("username", "")
    token_ver = int(payload.get("ver", 0))
    if not uid:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "USER_NOT_FOUND", "message": "用户不存在"})

    # D3 修复：复用 deps.py 缓存校验 token_version + status
    from src.api.deps import get_user_status_cached, invalidate_user_cache
    db_ver, status, _ = get_user_status_cached(uid)
    if token_ver != db_ver:
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "SESSION_REVOKED", "message": "会话已失效"})
    if status != 1:
        invalidate_user_cache(uid)
        # BUG-7 修复：区分冷静期(status=3)和禁用(status=0)，需求20要求冷静期返回不同提示
        if status == 3:
            raise HTTPException(status_code=401, detail={"code": 401, "error_code": "ACCOUNT_DEACTIVATING", "message": "该账号正在注销中，暂时无法登录"})
        raise HTTPException(status_code=401, detail={"code": 401, "error_code": "ACCOUNT_DEACTIVATED", "message": "该账号已被停用，请联系管理员"})

    # 滚动续期
    old_max_exp = int(payload.get("max_exp", 0))
    old_login_provider = payload.get("login_provider", "")
    new_access_token = create_access_token(uid, username, db_ver, login_provider=old_login_provider)
    new_refresh_token = create_refresh_token(uid, username, db_ver, max_exp=old_max_exp,
                                             login_provider=old_login_provider)
    _set_refresh_cookie(response, new_refresh_token)
    return {"token": new_access_token}


def _pat_platforms(user_id) -> "list[str]":
    """系统支持的平台仓库中，该用户已配 PAT 的那些（D109 关系推导第二来源）。

    用户口径（2026-11-14）：「查询系统支持哪些平台仓库，再基于这些平台仓库查是否
    管理了对应的 PAT 凭据配置」。凭据域 `<p>_pat` 有 token 即视为已接入该平台——
    不依赖保存动作是否走到 hooks（存量数据、脚本写入、旧实例都覆盖到）。
    """
    try:
        from src.domains.mgmt.user.db import get_credential
        from src.domains.platform.registry import supported_platforms

        out = []
        for p in supported_platforms():
            try:
                cfg = get_credential(str(user_id), str(p), kind="pat")
            except Exception:
                cfg = None
            if cfg and (cfg.get("token") or cfg.get("api_token")):
                out.append(str(p))
        return out
    except Exception as e:  # 平台卡/凭据域不可用不影响主判据
        log.warning(f"pat platform scan skipped: {e}")
        return []


def _merge_pat_platforms(active, pats) -> "list[str]":
    """关系行 ∪ PAT 凭据平台（纯函数，便于单测）。"""
    return sorted({str(p) for p in (active or [])} | {str(p) for p in (pats or [])})


def _backfill_pat_links(user_id, active, pats) -> None:
    """PAT 有凭据但关系行缺失 → 幂等补写关系行（自愈，不掩盖真实关系）。"""
    missing = [p for p in (pats or []) if str(p) not in {str(a) for a in (active or [])}]
    if not missing:
        return
    try:
        from src.domains.platform import hooks

        for p in missing:
            hooks.on_pat_saved(str(user_id), str(p))
    except Exception as e:
        log.warning(f"pat link backfill skipped: {e}")


def _identity_providers(user_id: str) -> set:
    """该用户的身份行平台集合（OAuth 登录/绑定只有这一处真源）。"""
    try:
        from src.domains.mgmt.auth.grape_account_db import get_user_identities

        return {str(i.get("provider") or "") for i in get_user_identities(str(user_id))}
    except Exception as e:  # 身份域不可用 → 空集（只影响剪枝，不影响主判据）
        log.warning(f"_identity_providers({user_id}) failed: {e}")
        return set()


def _prune_dangling_links(user_id: str, active: "list[str]", pats: "list[str]") -> "list[str]":
    """关系行还在、但来源（身份行 / PAT 凭据）全无 → 撤销并从句柄里去掉。

    2026-11-16 实锤：清 PAT 的那次删除没有触发撤销钩子（或只删了凭据行），
    `grape_t_user_repo_platform_link` 里留下一条永远 active 的 gitcode 关系 →
    前端拿 `/auth/platform-links` 判「已接入」→ 进工作区拉仓 → 服务端无凭据，
    用户看到的是一个自相矛盾的状态（一个页面说已绑定、另一个报授权过期）。
    关系是**派生结论**：没有来源就必须撤销，不能靠"删除时必须记得调钩子"。
    """
    sources = _identity_providers(user_id) | {str(p) for p in (pats or [])}
    # 直接看凭据行（不依赖平台卡目录）：关系刚由「保存 PAT」写入时，哪怕目录/凭据解析
    # 暂时不可用，也不该把它剪掉 —— 只有「身份行与 PAT 行都没有」才算来源全无。
    try:
        from src.domains.mgmt.auth.grape_account_db import list_platform_links

        sources |= {str(i.get("provider") or "") for i in list_platform_links(str(user_id))
                    if str(i.get("source") or "") == "pat"}
    except Exception as e:
        log.warning(f"_prune_dangling_links: source scan skipped: {e}")
    dangling = [str(p) for p in (active or []) if str(p) not in sources]
    if not dangling:
        return list(active or [])
    try:
        from src.domains.platform import hooks

        for p in dangling:
            # 复用撤销钩子：它内部会再查一次「是否还有其它来源」，没有才真正写 revoked
            hooks.on_pat_deleted(str(user_id), p)
        log.info(f"repo platform links pruned (no source left): user={user_id} providers={dangling}")
    except Exception as e:
        log.warning(f"repo platform links prune failed: user={user_id}: {e}")
    return [str(p) for p in (active or []) if str(p) in sources]


def _repo_platform_links(user_id: str) -> "dict | None":
    """当前用户 ↔ 仓库平台关系（D109 对象模型读口）。

    登录后是否进引导页的判据从「identities 里有几个绑定」换成「关系表里有没有
    未撤销的平台关系」：grape_t_user_repo_platform_link 是纯对应关系（登录渠道
    绑定不等价于仓库平台接入，例如 huawei 只是登录渠道）。

    表未就绪 / 读失败 → 返回 None，调用方回落 identities（fail-open，与其余
    D109 读路径同口径）。
    """
    try:
        from src.domains.platform.importer import is_table_backed

        if not is_table_backed():
            return None
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import UserRepoPlatformLinkRepo

        with Session(get_engine()) as s:
            rows = list(UserRepoPlatformLinkRepo.list_by_user(s, str(user_id)))
        active = sorted({str(r.repo_provider) for r in rows if not r.revoked_at})
        # 可用性（2026-11-16 yangaiwu 现场）：关系行在、身份行也在，但身份行的 token 解不出来
        # （历史 derived 派生密钥）+ refresh_token 为空 → 事实是"这个平台现在没法用"。
        # 入口判据必须和 /repos/member 用同一口径：不可用不算已接入，并给出 needs_reauth
        # 让前端提示重新授权；否则一边说已绑定、一边说授权过期。
        _usable: set = set()
        try:
            from src.domains.mgmt.auth.grape_account_db import list_platform_links

            for _i in list_platform_links(str(user_id)):
                if str(_i.get("provider") or "") and _i.get("token_usable", True):
                    _usable.add(str(_i["provider"]))
        except Exception as e:
            log.warning(f"_repo_platform_links: usability scan skipped: {e}")
            _usable = set(active)   # 扫描失败时保守：不额外收紧（保持原行为）
        # 第二来源：凭据域里已配 PAT 的系统平台也算「已接入」（关系行缺失时自愈补写）
        pats = _pat_platforms(user_id)
        # 反向自愈（2026-11-16）：关系行在、来源全无 → 撤销；否则前端会拿一条
        # 「幽灵关系」判已接入，进工作区后再被 401/引导页打脸。
        active = _prune_dangling_links(user_id, active, pats)
        merged = _merge_pat_platforms(active, pats)
        if len(merged) > len(active):
            _backfill_pat_links(user_id, active, pats)
        _needs_reauth = sorted(p for p in merged if p not in _usable)
        usable = sorted(p for p in merged if p in _usable)
        out = {"providers": usable, "has_any": bool(usable), "source": "table"}
        if _needs_reauth:
            out["needs_reauth"] = _needs_reauth
            out["hint"] = (f"{_needs_reauth[0]} 登录授权已过期，请重新授权")
        return out
    except Exception as e:
        log.warning(f"repo platform link read failed, fallback to identities: {e}")
        return None


def _links_payload(user_id: str) -> dict:
    """关系载荷（profile 字段与 /auth/platform-links 共用同一实现，单一真源）。"""
    from src.domains.mgmt.auth.grape_account_db import get_user_identities

    bindings = [i["provider"] for i in get_user_identities(user_id)]
    payload = _repo_platform_links(str(user_id)) or {
        "providers": [], "has_any": bool(bindings), "source": "identities",
    }
    payload["user_id"] = str(user_id)
    return payload


@router.get("/platform-links", operation_id="auth_platform_links",
            summary="Current user's repo-platform relations (object model)",
            description="Returns the caller's non-revoked user↔repo-platform relations read from grape_t_user_repo_platform_link, with the identities fallback marked as source=identities. Same payload the /auth/profile field carries.")
def get_platform_links(user: dict = Depends(require_no_session_token)):
    """当前用户 ↔ 仓库平台关系（引导页判据的独立查询口）。"""
    return _links_payload(user["id"])


@router.get("/profile", operation_id="auth_profile", summary="Get current user profile", description="Returns the current user profile. Supports JWT session, user API Key (grape_) or session token (tk_).")
def get_profile(user: dict = Depends(require_no_session_token)):
    """用户信息——支持 JWT 会话 / grape_ API Key / tk_ 会话 token（Issue #69）。

    D9 修复：返回 bindings 列表（已绑定的 provider 数组），兼容旧 gitcode_bound/github_bound 字段。
    """
    acc = get_grape_account_by_username(user["username"]) or {}
    managed_repos_raw = user_db.get_managed_repos(user["id"])
    managed = [{"key": r["full_name"], "full_name": r["full_name"], "is_admin": r["is_admin"]}
               for r in managed_repos_raw[:10]]

    # D9: 构建 bindings 列表（动态，不硬编码平台）
    from src.domains.mgmt.auth.grape_account_db import get_user_identities, has_password
    identities = get_user_identities(user["id"])
    bindings = [ident["provider"] for ident in identities]
    # 兼容旧字段——D100（2026-11-08）R3：<p>_oauth 快照回落判定已删
    # （快照清退，绑定权威=get_user_identities；快照行 P1=a 冻结不再作为判定源）
    gitcode_bound = "gitcode" in bindings
    github_bound = "github" in bindings
    # D109：仓库平台关系（对象模型权威）。前端「登录后是否进引导页」读这个字段；
    # 表未就绪时回落 identities，源标记 source='identities' 供前端区分。
    repo_platform_links = _links_payload(str(user["id"]))
    repo_platform_links.pop("user_id", None)  # profile 本身已经代表当前用户

    return {
        "username": user["username"],
        "nickname": acc.get("nickname", "") or user["username"],
        "email": acc.get("email", ""),
        "has_password": has_password(user["id"]),
        "status": "online",
        "managed_repos": managed,
        "bindings": bindings,
        "repo_platform_links": repo_platform_links,
        "gitcode_bound": gitcode_bound,
        "github_bound": github_bound,
        "email_verified": acc.get("email_verified", 0),
    }


@router.post("/logout", operation_id="auth_logout", summary="Logout (revoke all tokens)", description="Logs out: increments token_version to revoke all tokens and clears the refresh cookie.")
def logout(user: dict = Depends(require_no_session_token), response: Response = None):
    """登出——token_version+1 撤销所有 token + 清 cookie + 失效缓存。"""
    # D32: 只走 grape_accounts
    grape_inc(user["id"])
    # 失效内存缓存
    from src.api.deps import invalidate_user_cache
    invalidate_user_cache(user["id"])
    if response:
        _clear_refresh_cookie(response)
    return {"message": "已登出"}
