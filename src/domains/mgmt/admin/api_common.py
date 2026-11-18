"""管理面平台对象 API 的公共依赖：鉴权、UoW 写、错误口径、请求体。"""
from __future__ import annotations

from src.kernel.config import log

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from src.api.deps import require_admin


class EnabledBody(BaseModel):
    enabled: bool


class ChannelCredentialsBody(BaseModel):
    client_id: str = ""
    client_secret: str = ""



def load_field_docs(config_file: str) -> dict:
    """读配置文件的 `_fields` 文档键（字段说明的**单一来源**）。

    前端每个配置项的 "?" 文案由此透传：新增平台/字段时文案跟着配置走，
    不会出现"页面写死说明、配置早已改"的漂移。读不到返回空（前端回落 i18n）。
    """
    import json
    import os

    try:
        from src.kernel.config import CONFIG_DIR

        path = config_file if os.path.isabs(config_file) else os.path.join(str(CONFIG_DIR), config_file)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        docs = data.get("_fields") or {}
        return {k: v for k, v in docs.items() if isinstance(v, dict)}
    except Exception:
        return {}


def not_found(resource: str, ident: str) -> HTTPException:
    """统一 404 口径：带 code/resource/id，绝不返回空对象。"""
    return HTTPException(status_code=404, detail={"code": "NOT_FOUND", "resource": resource, "id": ident})


def conflict(resource: str, ident: str, reason: str = "") -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "CONFLICT", "resource": resource,
                                                  "id": ident, "reason": reason})


def table_not_ready() -> HTTPException:
    return HTTPException(status_code=503, detail={"code": "TABLE_NOT_READY",
                                                  "detail": "平台对象表未就绪，写接口不静默降级"})


def uow_write(fn):
    """统一写入：uow + bump epoch + 失效缓存。"""
    from src.kernel.persistence.uow import uow
    from src.domains.platform.importer import bump_epoch

    with uow() as s:
        fn(s)
    bump_epoch()
    try:
        from src.domains.mgmt.auth.providers import invalidate_provider_cache
        from src.domains.runtime.public import invalidate_platform_card_cache

        invalidate_provider_cache()
        invalidate_platform_card_cache()
    except Exception as e:
        # 缓存失效失败不影响本次保存结果，只影响下一次读取的新鲜度
        log.debug(f"admin api_common: cache invalidate failed: {e}")


__all__ = ["load_field_docs", "require_admin", "Depends", "EnabledBody", "ChannelCredentialsBody",
           "not_found", "conflict", "table_not_ready", "uow_write"]
