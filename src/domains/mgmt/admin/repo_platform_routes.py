"""RepoPlatform（代码托管平台）：GET 集合/单条；写只走 enabled 子资源。"""
from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import BaseModel

from src.domains.mgmt.admin.api_common import Depends, load_field_docs, EnabledBody, not_found, require_admin, uow_write
from src.kernel.config import log

router = APIRouter(prefix="/repo-platforms", tags=["admin"])


def _item(r, bots=None, health=None) -> dict:
    from src.domains.platform.public import platform_capabilities
    from src.domains.platform.public import is_platform_visible

    cap = platform_capabilities(r.provider)
    return {
        "provider": r.provider, "name": r.name, "dialect": r.dialect,
        "host": r.host, "api_base": r.api_base, "api_auth": r.api_auth,
        "webhook_path": r.webhook_path, "event_header": r.event_header,
        "webhook_verify": r.webhook_verify, "webhook_dialect": r.webhook_dialect,
        "clone_auth": r.clone_auth, "issue_write_form": r.issue_write_form,
        "branch_create_form": r.branch_create_form, "pr_head_filter": r.pr_head_filter,
        "issue_search": r.issue_search, "git_tree_family": r.git_tree_family,
        "pr_comment_endpoint": r.pr_comment_endpoint,
        "pr_labels_via_issues": bool(r.pr_labels_via_issues),
        "workflows_write": getattr(r, "workflows_write", ""),
        "brand_color": r.brand_color,
        "enabled": bool(r.enabled), "source": r.source, "config_hash": r.config_hash,
        "pat_create_url": getattr(r, "pat_create_url", ""),
        "bot_username": getattr(r, "bot_username", ""),
        # D110：bot 归平台行（一平台一个）。列表形态保留（前端/老调用方兼容），
        # 值一律来自平台行本身（D110：bot 目录表已删除）。
        # 用户面可见性（平台行 enabled）：管理面照常展示，但标记出来，
        # 避免管理员误判"平台配好了 = 用户就能用"（disabled 平台无 UI 入口）。
        "disabled": (not is_platform_visible(r.provider)),
        "bot_accounts": ([{
            "username": (getattr(r, "bot_username", "") or ""),
            "is_default": True,
            "status": ("active" if (getattr(r, "bot_token_enc", "") or "") else "disabled"),
            "last_used_at": (getattr(r, "bot_checked_at", "") or ""),
        }] if ((getattr(r, "bot_username", "") or "") or
               (getattr(r, "bot_token_enc", "") or "")) else []),
        "bot_token_health": health or {},
        "capability": {"login": cap["login"], "configured": cap["configured"],
                        "enabled": cap["enabled"], "hosting": cap["hosting"]},
    }


def _health(provider: str) -> dict:
    try:
        from src.domains.mgmt.user.db import get_meta_value

        raw = get_meta_value("bot_token_health")
        if raw:
            return (json.loads(raw) or {}).get("platforms", {}).get(provider, {})
    except Exception as e:
        log.debug(f"repo_platform_routes._health({provider}): meta read failed: {e}")
    return {}


@router.get("", summary="代码托管平台集合（定义字段 + enabled + 凭据能力 + bot 摘要）")
def list_platforms(admin: dict = Depends(require_admin)):
    try:
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

        with Session(get_engine()) as s:
            rows = list(RepoPlatformRepo.list_all(s))
            if rows:
                return {"platforms": [_item(p, None, _health(p.provider)) for p in rows],
                        "source": "table", "docs": {}}
    except Exception as e:
        from src.kernel.config import log

        log.warning("repo-platforms: table read failed, fallback: %s", e)

    from src.domains.runtime.public import get_platform_card, list_vcs_platforms
    from src.domains.platform.public import platform_capabilities

    out = []
    for provider in list_vcs_platforms():
        card = get_platform_card(provider)
        if card is None:
            continue
        out.append({
            "provider": provider, "name": card.name, "dialect": card.dialect,
            "host": card.host, "api_base": card.api_base, "api_auth": card.api_auth,
            "webhook_path": card.webhook_path, "event_header": card.event_header,
            "webhook_verify": card.webhook_verify, "webhook_dialect": card.webhook_dialect,
            "clone_auth": card.clone_auth, "issue_write_form": card.issue_write_form,
            "branch_create_form": card.branch_create_form, "pr_head_filter": card.pr_head_filter,
            "issue_search": card.issue_search, "git_tree_family": card.git_tree_family,
            "pr_comment_endpoint": card.pr_comment_endpoint,
            "pr_labels_via_issues": bool(card.pr_labels_via_issues),
            "workflows_write": getattr(card, "workflows_write", ""),
            "brand_color": card.brand_color,
            "enabled": True, "source": "config", "config_hash": "",
            "pat_create_url": getattr(card, "pat_create_url", ""),
            "bot_username": getattr(card, "bot_username", ""),
            "bot_accounts": [], "bot_token_health": _health(provider),
            "capability": (lambda cp: {"login": cp["login"], "configured": cp["configured"],
                                       "enabled": cp["enabled"], "hosting": cp["hosting"]})(
                platform_capabilities(provider)),
        })
    return {"platforms": out, "source": "fallback", "docs": {}}


@router.get("/{provider}", summary="单个代码托管平台")
def get_platform(provider: str, admin: dict = Depends(require_admin)):
    from sqlalchemy.orm import Session

    from src.kernel.persistence.engine import get_engine
    from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

    with Session(get_engine()) as s:
        row = RepoPlatformRepo.get(s, provider)
        if row is None:
            raise not_found("repo-platforms", provider)
        return _item(row, None, _health(provider))


def _provider_impact(provider: str) -> dict:
    """关闭某平台会影响多少用户/仓库（下线预检用）。

    口径：用户面可见性=平台行 enabled，关掉后该平台的仓库在用户面**一律不展示**
    （严格语义，见 d110 §16）；托管行不删、任务照旧跑，所以这里只报"用户会看不到"的量。
    前缀判定用 substr 相等（provider 允许下划线，LIKE 会误伤同形平台）。
    """
    prefix = f"{provider}:"
    out = {"provider": provider, "repos": 0, "users": 0, "managed_repos": 0, "pat_users": 0}
    try:
        from src.kernel.session_manager import db
        c = db.cursor()
        n = len(prefix)
        row = c.execute(
            "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM user_repos WHERE substr(repo_key,1,?)=?",
            (n, prefix)).fetchone()
        out["repos"], out["users"] = int(row[0] or 0), int(row[1] or 0)
        row = c.execute(
            "SELECT COUNT(*) FROM managed_repos WHERE substr(repo_key,1,?)=?", (n, prefix)).fetchone()
        out["managed_repos"] = int(row[0] or 0)
        row = c.execute(
            "SELECT COUNT(DISTINCT user_id) FROM user_credentials WHERE provider=? AND kind='pat'",
            (provider,)).fetchone()
        out["pat_users"] = int(row[0] or 0)
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


@router.put("/{provider}/enabled",
            summary="平台开关（DB 权威，配置文件不覆盖）：关=用户面不提供该平台")
def set_enabled(provider: str, body: EnabledBody, confirm: int = 0,
                admin: dict = Depends(require_admin)):
    """关闭平台前必须过影响面预检（`confirm=1` 才真关）。

    为什么要有这一步：关闭后该平台对用户面**完全不可见**（目录不显示、仓库列表不返回、
    显式请求 403），静默关掉会让存量用户的项目凭空消失且没有任何提示——
    先回统计让人确认，是把"会疼"这件事摆到操作者面前。
    """
    from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo
    from fastapi import HTTPException

    impact = _provider_impact(provider) if not body.enabled else {}
    if not body.enabled and not confirm:
        # 关闭**一律**要确认，影响面为 0 也不例外：它仍然是"该平台在用户面不再提供"的
        # 状态变更。（2026-11-15 实锤：早先按"影响面 > 0 才拦"，导致 gitlab 这类没仓库的
        # 平台开关可以随便点，与其他平台行为不一致，操作者无法预判。）
        raise HTTPException(status_code=409, detail={
            "code": 409, "error_code": "PLATFORM_DISABLE_NEEDS_CONFIRM",
            "message": ("关闭后该平台在用户面完全不可见（仓库列表不再返回、显式请求 403）；"
                        "确认影响面后带 ?confirm=1 重试"),
            "impact": impact})

    hit = {}

    def _do(s):
        hit["ok"] = RepoPlatformRepo.set_enabled(s, provider, body.enabled)

    uow_write(_do)
    if not hit.get("ok"):
        raise not_found("repo-platforms", provider)
    return {"ok": True, "provider": provider, "enabled": body.enabled,
            "impact": impact if impact else None}


class BotBody(BaseModel):
    """平台 bot 写入体（D110）：username 空=不改；token 空=不改；token 留空无法清除，清除走 DELETE。"""

    username: str = ""
    token: str = ""
    status: str = ""


@router.put("/{provider}/bot", summary="平台 bot（一平台一个）：写账号名/轮换 token（加密入库）")
def set_platform_bot(provider: str, body: BotBody, admin: dict = Depends(require_admin)):
    """写平台行上的 bot（D110）。

    语义（与前端契约一一对应）：
    - `username` 为空串 = 不改名字；非空 = 覆盖（平台行是事实来源，平台卡只做首次播种）；
    - `token` 为空串 = 不改/保留现 token（轮换时填），清除请走 DELETE；
    - 落库前 `encrypt`（AES-256-GCM），响应只回 `token_present`，永不回显 token；
    - token 真正变化时触发 `global_bot_token_swap`：path A 托管把平台 bot token 同时写成了
      仓库 webhook 验签 token，不刷这批仓库会让老仓库 webhook 立刻 403。
    """
    from src.domains.mgmt.auth.credential_crypto import encrypt
    from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

    from src.domains.mgmt.admin.config_store import platform_bot_old_token, trigger_platform_bot_swap

    old_plain = platform_bot_old_token(provider) if body.token else ""
    token_enc = encrypt(body.token) if body.token else None
    username = body.username if body.username != "" else None
    verified_login = ""
    if token_enc is not None:
        # 令牌即事实：存令牌时用平台 /user 回答「我是谁」，自动回填账号名。
        # basic_password 平台（gitee）的 clone 用户名必须是**令牌所属账号 login**，
        # 靠人手填必然出错（实测填组织名/grape 账号名 → git clone 恒 403）。
        # 这里把约束变成派生：坏令牌当场 400（不静默入库），好令牌自动带回 login。
        try:
            from src.domains.mgmt.admin.config_store import verify_token as _verify_token

            verified_login = str((_verify_token(provider, body.token) or {}).get("login") or "").strip()
        except Exception as e:  # noqa: BLE001 - 上游拒绝/网络失败都算令牌不可用
            raise HTTPException(status_code=400, detail={
                "code": 400, "error_code": "TOKEN_INVALID",
                "message": f"{provider} 令牌校验失败（无法确认所属账号）: {e}",
            }) from e
        if not username and verified_login:
            username = verified_login
        elif username and verified_login and username != verified_login:
            # 人工值与令牌真实账号不一致：保留人工值（可能是刻意的别名），但必须留痕——
            # basic_password 平台用这个值做 HTTP Basic 用户名，不一致就会 403。
            log.warning(f"repo_platform_routes: {provider} bot_username={username!r} "
                        f"与令牌所属账号 {verified_login!r} 不一致（basic_password 平台会 403）")
    if token_enc is None and username is None:
        return {"ok": True, "provider": provider, "changed": False}
    hit = {}

    def _do(s):
        hit["ok"] = RepoPlatformRepo.set_bot(s, provider, username=username, token_enc=token_enc)

    uow_write(_do)
    if not hit.get("ok"):
        raise not_found("repo-platforms", provider)
    if verified_login:
        # 验证结果落位：bot_token_status/checked_at 由"猜"变成"验过"，管理面直接可读。
        try:
            from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

            uow_write(lambda s: RepoPlatformRepo.mark_bot_status(s, provider, "ok"))
        except Exception as e:  # noqa: BLE001 - 状态位写失败不影响令牌写入结果
            log.debug(f"repo_platform_routes: mark bot status failed: {e}")
        # 令牌换过就把 clone 用户名缓存清掉（指纹变了本就会重解析，这里再兜一层旧明文键）
        try:
            from src.domains.runtime import public as _runtime_public

            _runtime_public.invalidate_clone_identity(provider, body.token)
            if old_plain:
                _runtime_public.invalidate_clone_identity(provider, old_plain)
        except Exception as e:  # noqa: BLE001
            log.debug(f"repo_platform_routes: clone_identity invalidate failed: {e}")
    # 轮换传播：path A 托管把平台 bot token 同时写成仓库 webhook 验签 token，
    # 换 token 必须刷已托管仓库，否则老仓库 webhook 立刻 403（D110 §7 R1）。
    if token_enc is not None:
        try:
            from src.domains.mgmt.user.db import _invalidate_global_bot_cache

            _invalidate_global_bot_cache()
        except Exception as e:
            # 缓存失效失败只影响下次读取的新鲜度，不影响写入结果
            log.debug(f"repo_platform_routes: invalidate bot cache failed: {e}")
        # D110 R1：轮换传播到 path A 仓库的 webhook 验签 token
        trigger_platform_bot_swap(provider, old_plain, body.token)
    from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo as _R

    return {"ok": True, "provider": provider, "changed": True, "bot": _R.get_bot(provider)}


@router.delete("/{provider}/bot", summary="清除平台 bot token（账号名保留）")
def clear_platform_bot(provider: str, admin: dict = Depends(require_admin)):
    """只清 `bot_token_enc`（平台回落到"无全局 bot"），`bot_username` 保留用于身份展示/名单。"""
    from src.kernel.persistence.repositories_platform_object import RepoPlatformRepo

    hit = {}

    def _do(s):
        hit["ok"] = RepoPlatformRepo.set_bot(s, provider, token_enc="")

    uow_write(_do)
    if not hit.get("ok"):
        raise not_found("repo-platforms", provider)
    return {"ok": True, "provider": provider, "token_present": False}
