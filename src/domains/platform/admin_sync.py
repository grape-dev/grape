"""管理面保存 → 平台对象表同步（D109：表是运行时真源，旧扁平 API 保持兼容）。

接线点：`admin/config_store.save_config()` 末尾（旧 flow 写 meta/文件之后）。
原则：
- **fail-safe**：同步失败只告警，不影响管理面保存返回（旧路径仍可用）；
- 每次真实写入后 `epoch += 1` + 失效内存缓存（跨实例靠 epoch 比对）；
- 凭据值只用 `credential_crypto.encrypt` 落库，绝不回显。
"""
from __future__ import annotations

from src.kernel.config import log

# 扁平字段 → (provider, 凭据字段)（与 config_store._OAUTH_FIELD_MAP 保持一致）
OAUTH_FLAT_MAP = {
    "gitcode_oauth_client_id": ("gitcode", "client_id"),
    "gitcode_oauth_client_secret": ("gitcode", "client_secret"),
    "github_oauth_client_id": ("github", "client_id"),
    "github_oauth_client_secret": ("github", "client_secret"),
    "gitee_oauth_client_id": ("gitee", "client_id"),
    "gitee_oauth_client_secret": ("gitee", "client_secret"),
    "huawei_oauth_client_id": ("huawei", "client_id"),
    "huawei_oauth_client_secret": ("huawei", "client_secret"),
}


def _enc(value: str) -> str:
    from src.domains.mgmt.public import encrypt

    return encrypt(value) if value else ""


def sync_from_admin_payload(payload: dict) -> None:
    """把管理面扁平 payload 同步进对象表（幂等；失败不阻断）。"""
    try:
        from sqlalchemy import update
        from src.kernel.persistence.models_platform_object import OAuthChannelModel
        from src.kernel.persistence.repositories_platform_object import (
            OAuthChannelRepo,
            RepoPlatformRepo,
        )
        from src.kernel.persistence.uow import uow

        wrote = 0
        with uow() as s:
            # bot（D110）：一平台一个 bot，直接写平台行；不凭空建行，也不编造账号名
            gc_token = str(payload.get("gitcode_api_token") or "")
            if gc_token and RepoPlatformRepo.set_bot(s, "gitcode", token_enc=_enc(gc_token)):
                wrote += 1
            gh_token = str(payload.get("github_bot_token") or "")
            if gh_token:
                _gh_uname = str(payload.get("github_bot_username") or "")
                if RepoPlatformRepo.set_bot(
                    s, "github",
                    token_enc=_enc(gh_token),
                    username=(_gh_uname or None),
                ):
                    wrote += 1

            # oauth 客户端凭据：逐字段写上（不覆盖另一列）
            for flat, (provider, field) in OAUTH_FLAT_MAP.items():
                val = str(payload.get(flat) or "")
                if not val:
                    continue
                if OAuthChannelRepo.get(s, provider) is None:
                    log.warning("platform-object admin sync: channel %s 不存在，跳过 %s", provider, flat)
                    continue
                col = "client_id_enc" if field == "client_id" else "client_secret_enc"
                s.execute(
                    update(OAuthChannelModel)
                    .where(OAuthChannelModel.provider == provider)
                    .values({col: _enc(val)})
                )
                wrote += 1

        if wrote:
            from src.domains.platform.importer import bump_epoch

            bump_epoch()
            from src.domains.mgmt.public import invalidate_provider_cache
            from src.domains.runtime.public import invalidate_platform_card_cache

            invalidate_provider_cache()
            invalidate_platform_card_cache()
            log.info("platform-object admin sync: %d field(s) written, epoch bumped", wrote)
    except Exception as e:  # fail-safe：旧路径（meta/文件）已写成功
        log.warning("platform-object admin sync skipped (non-fatal): %s: %s", type(e).__name__, e)