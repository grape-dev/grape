"""OAuthChannel（登录渠道）：GET 集合/单条；写只走子资源 credentials / enabled。"""
from __future__ import annotations

import json

from fastapi import APIRouter

from src.domains.mgmt.admin.api_common import ChannelCredentialsBody, load_field_docs, Depends, EnabledBody, not_found, require_admin, table_not_ready, uow_write

router = APIRouter(prefix="/oauth-channels", tags=["admin"])


def _links_of(provider: str) -> list[dict]:
    """该渠道的授权行（读 grape_t_channel_repo_link，单源）。读失败 → 空列表。"""
    try:
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import ChannelRepoLinkRepo

        with Session(get_engine()) as s:
            rows = list(ChannelRepoLinkRepo.list_by_oauth(s, provider))
        return [{"repo_provider": x.repo_provider, "source": x.source} for x in rows]
    except Exception:
        return []


def _item(r) -> dict:
    from src.domains.platform.capabilities import platform_capabilities

    cap = platform_capabilities(r.provider)
    return {
        "provider": r.provider, "name": r.name, "type": r.type,
        "auth_url": r.auth_url, "token_url": r.token_url, "user_url": r.user_url,
        "emails_endpoint": r.emails_endpoint, "scope": r.scope,
        "pkce_enabled": bool(r.pkce_enabled),
        "id_field": r.id_field, "login_field": r.login_field,
        "name_field": r.name_field, "avatar_field": r.avatar_field,
        "redirect_uri": r.redirect_uri, "brand_color": r.brand_color,
        "accept_header": r.accept_header,
        "enabled": bool(r.enabled), "source": r.source, "config_hash": r.config_hash,
        "credential_present": {"client_id": bool(r.client_id_enc), "client_secret": bool(r.client_secret_enc)},
        # 授权单一真源 = grape_t_channel_repo_link（channel-repo-links.json 导入 / 管理面 manual 写）；
        # 渠道行上的 repo_links_json 是历史副本，不再作为读源。
        "repo_links": _links_of(r.provider),
        "capability": {"login": cap["login"], "login_supported": cap["login_supported"],
                        "configured": cap["configured"], "hosting": cap["hosting"]},
    }


@router.get("", summary="登录渠道集合（定义字段 + 凭据存在态 + enabled + 关联）")
def list_channels(admin: dict = Depends(require_admin)):
    try:
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine
        from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

        with Session(get_engine()) as s:
            rows = list(OAuthChannelRepo.list_all(s))
        if rows:
            docs = load_field_docs("oauth-providers.json")
            # 授权字段的说明来自 channel-repo-links.json（渠道卡里已无 repo_links）
            link_docs = load_field_docs("channel-repo-links.json")
            if link_docs:
                docs["repo_links"] = {
                    "what": (link_docs.get("repo_provider") or {}).get("what", ""),
                    "values": (link_docs.get("relation") or {}).get("values", ""),
                    "note": (link_docs.get("relation") or {}).get("note", ""),
                }
            return {"channels": [_item(r) for r in rows], "source": "table", "docs": docs}
    except Exception as e:
        from src.kernel.config import log

        log.warning("oauth-channels: table read failed, fallback: %s", e)

    from src.domains.mgmt.auth.providers import get_provider, list_providers
    from src.domains.platform.capabilities import platform_capabilities

    out = []
    for provider in list_providers():
        card = get_provider(provider) or {}
        cap = platform_capabilities(provider)
        out.append({
            "provider": provider, "name": card.get("name", provider), "type": card.get("type", "oauth2"),
            "auth_url": card.get("auth_url", ""), "token_url": card.get("token_url", ""),
            "user_url": card.get("user_url", ""), "emails_endpoint": card.get("emails_endpoint", ""),
            "scope": card.get("scope", ""), "pkce_enabled": bool(card.get("pkce_enabled", False)),
            "id_field": card.get("id_field", ""), "login_field": card.get("login_field", ""),
            "name_field": card.get("name_field", ""), "avatar_field": card.get("avatar_field", ""),
            "redirect_uri": card.get("redirect_uri", ""), "brand_color": card.get("brand_color", ""),
            "accept_header": card.get("accept_header", ""),
            "enabled": True, "source": "config", "config_hash": "",
            "credential_present": {"client_id": bool(card.get("client_id")), "client_secret": False},
            "repo_links": list(card.get("repo_links") or []),
            "capability": {"login": cap["login"], "login_supported": cap["login_supported"],
                            "configured": cap["configured"], "hosting": cap["hosting"]},
        })
    return {"channels": out, "source": "fallback",
            "docs": load_field_docs("oauth-providers.json")}


@router.get("/{channel}", summary="单个登录渠道")
def get_channel(channel: str, admin: dict = Depends(require_admin)):
    from sqlalchemy.orm import Session

    from src.kernel.persistence.engine import get_engine
    from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

    with Session(get_engine()) as s:
        row = OAuthChannelRepo.get(s, channel)
        if row is None:
            raise not_found("oauth-channels", channel)
        return _item(row)


@router.put("/{channel}/credentials", summary="写 OAuth 客户端凭据（加密列，未知键拒绝）")
def set_credentials(channel: str, body: ChannelCredentialsBody, admin: dict = Depends(require_admin)):
    from sqlalchemy import update

    from src.domains.mgmt.auth.credential_crypto import encrypt
    from src.kernel.persistence.models_platform_object import OAuthChannelModel
    from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

    if not (body.client_id or body.client_secret):
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail={"code": "VALIDATION", "field": "client_id|client_secret"})

    def _do(s):
        if OAuthChannelRepo.get(s, channel) is None:
            raise not_found("oauth-channels", channel)
        values = {}
        if body.client_id:
            values["client_id_enc"] = encrypt(body.client_id)
        if body.client_secret:
            values["client_secret_enc"] = encrypt(body.client_secret)
        s.execute(update(OAuthChannelModel).where(OAuthChannelModel.provider == channel).values(**values))

    uow_write(_do)
    return {"ok": True, "channel": channel}


@router.put("/{channel}/enabled", summary="登录渠道开关（DB 权威）")
def set_enabled(channel: str, body: EnabledBody, admin: dict = Depends(require_admin)):
    from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

    hit = {}

    def _do(s):
        hit["ok"] = OAuthChannelRepo.set_enabled(s, channel, body.enabled)

    uow_write(_do)
    if not hit.get("ok"):
        raise not_found("oauth-channels", channel)
    return {"ok": True, "channel": channel, "enabled": body.enabled}
