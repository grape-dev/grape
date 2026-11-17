"""渠道凭据源抽象（Issue #3054 P1）。

**唯一取凭据入口**：登录换 code、token 刷新、configured 判定都从这里拿
``client_id + client_secret``，各调用点不再自写「表优先还是 meta 优先」的优先级逻辑。

口径（P1-1 / P1-2 / P1-3）：

1. 真源 = ``grape_t_oauth_channels`` 的加密列（``client_id_enc`` / ``client_secret_enc``），
   属 **mgmt 库自己的表**（D109 对象层，mgmt 域所有，直连不跨域）；
2. ``client_id`` 与 ``client_secret`` **同源成对**取——一半表一半 meta 就是「登录用新凭据、
   刷新用旧/空凭据」的根因，所以两列只要有一列有值，另一列空就是空，绝不从 meta 补；
3. meta ``oauth_<p>_client_id/_client_secret`` **只作历史读取窗口**（迁移未完 / 表行两列皆空），
   且只在「整对」时生效；
4. 密文不可解 = 显式错误 ``CredentialDecryptError``（错误码 ``credential_decrypt_failed``）
   + 结构化告警事件，**绝不回落 meta**——回落会把密钥事故伪装成「登录成功 / 未配置」。

DB 连接抖动（读表本身失败）不算密钥事故，按「表未就绪」处理（历史窗口回落），
因为那是基础设施故障，回落 meta 才是当时的正确降级。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.kernel.config import log

#: 结构化告警 / 错误码（与 audit_logs.event_type 同名，Issue #3054 P1-3）
ERROR_DECRYPT_FAILED = "credential_decrypt_failed"

SOURCE_TABLE = "table"
SOURCE_META = "meta"

#: 字段 → 渠道表加密列
_TABLE_COLUMNS = {
    "client_id": "client_id_enc",
    "client_secret": "client_secret_enc",
}


class CredentialDecryptError(RuntimeError):
    """渠道表凭据密文不可解密（同 kid 换了密钥字节 / 密钥池缺该 kid）。"""

    code = ERROR_DECRYPT_FAILED

    def __init__(self, provider: str, field: str = "", reason: str = ""):
        self.provider = provider
        self.field = field
        self.column = _TABLE_COLUMNS.get(field, "")
        self.reason = reason
        super().__init__(
            f"{self.code}: provider={provider} field={field} "
            f"column={self.column or '-'} reason={reason or 'unknown'}")


@dataclass(frozen=True)
class Credential:
    """解析结果：值 + 来源 + 错误码（三者组合表达「有没有、从哪来、为什么没有」）。"""

    value: str = ""
    source: str = ""
    error: str = ""
    field: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def present(self) -> bool:
        return bool(self.value)


# 结构化告警去抖：同一 (provider, field, kid) 窗口内只落一次，
# 免得登录目录 / 配置页反复读坏密文时刷爆 audit_logs。
_ALERT_WINDOW_S = 300.0
_alert_seen: "dict[tuple[str, str, str], float]" = {}


def _kid_of(blob: str) -> str:
    """密文的 key_id（``{kid}:{nonce}:{ct}`` 第一段；非密文返回空）。"""
    parts = str(blob or "").split(":", 2)
    return parts[0] if len(parts) == 3 else ""


def alert_decrypt_failed(provider: str, field: str, kid: str, err: Exception) -> None:
    """解密失败 = ERROR 日志 + 结构化审计事件（既有 audit_logs 机制，不新造）。"""
    key = (str(provider), str(field), str(kid))
    now = time.time()
    if now - _alert_seen.get(key, 0.0) < _ALERT_WINDOW_S:
        return
    _alert_seen[key] = now
    log.error("credential_decrypt_failed provider=%s field=%s kid=%s err=%s: %s",
              provider, field, kid, type(err).__name__, err)
    try:
        from src.domains.mgmt.auth.audit import log_audit

        log_audit(
            "credential_decrypt_failed",
            detail={
                "provider": str(provider),
                "field": str(field),
                "column": _TABLE_COLUMNS.get(field, ""),
                "kid": str(kid),
                "error": f"{type(err).__name__}: {err}",
                # 只记来源位置，永不落密文/明文
                "source": "grape_t_oauth_channels",
            },
        )
    except Exception as e:  # 审计写失败不阻断登录链路（已有 ERROR 日志兜底）
        log.warning("credential_decrypt_failed audit event failed: %s", e)


class CredentialSource(ABC):
    """凭据源：一个 provider 的一个字段 → :class:`Credential`。"""

    name = "abstract"

    @abstractmethod
    def resolve(self, provider: str, field: str) -> Credential:
        ...

    def identity(self, provider: str) -> Credential:
        return self.resolve(provider, "client_id")

    def secret(self, provider: str) -> Credential:
        return self.resolve(provider, "client_secret")


class TableCredentialSource(CredentialSource):
    """渠道表加密列（D109 对象层，mgmt 库所有）。"""

    name = SOURCE_TABLE

    def _blob(self, provider: str, field: str) -> "str | None":
        column = _TABLE_COLUMNS.get(field)
        if not column:
            return None
        try:
            from src.domains.platform.importer import is_table_backed

            if not is_table_backed():
                return None
            from sqlalchemy.orm import Session

            from src.kernel.persistence.engine import get_engine
            from src.kernel.persistence.repositories_platform_object import OAuthChannelRepo

            with Session(get_engine()) as s:
                row = OAuthChannelRepo.get(s, str(provider))
        except Exception as e:
            # 基础设施故障（非密钥事故）：按"表未接管"处理，交给历史窗口
            log.warning("credential table read failed (%s.%s), treat as not-migrated: %s",
                        provider, column, e)
            return None
        if row is None:
            return None
        return getattr(row, column, "") or ""

    def resolve(self, provider: str, field: str) -> Credential:
        blob = self._blob(provider, field)
        if not blob:
            return Credential(field=field)  # 表"弃权"：无该行 / 凭据列为空
        from src.domains.mgmt.auth.credential_crypto import decrypt

        try:
            return Credential(value=decrypt(blob) or "", source=SOURCE_TABLE, field=field)
        except Exception as e:
            alert_decrypt_failed(provider, field, _kid_of(blob), e)
            return Credential(error=ERROR_DECRYPT_FAILED, field=field)


class MetaCredentialSource(CredentialSource):
    """meta 历史副本——**只作迁移窗口的整对回落**，不是真源（Issue #3054 P1-1）。"""

    name = SOURCE_META

    def resolve(self, provider: str, field: str) -> Credential:
        if field not in _TABLE_COLUMNS:
            return Credential(field=field)
        from src.domains.mgmt.user.db import get_meta_value

        val = (get_meta_value(f"oauth_{provider}_{field}") or "").strip()
        return Credential(value=val, source=SOURCE_META if val else "", field=field)


class ProviderCredentialSource(CredentialSource):
    """表优先 + 同源成对（登录/刷新/configured 的唯一入口）。"""

    name = "provider"

    def __init__(self, table: "CredentialSource | None" = None,
                 legacy: "CredentialSource | None" = None):
        self.table = table or TableCredentialSource()
        self.legacy = legacy if legacy is not None else MetaCredentialSource()

    def pair(self, provider: str) -> "tuple[Credential, Credential]":
        """成对取 client_id / client_secret（本类的主入口）。

        表任一列有值 → 表为准（另一列空就报半配置，绝不从 meta 补）；
        两列都空 → 整对回落 meta（历史窗口）；
        任一列解密失败 → 两个 Credential 都带错误码，调用方必须显式失败。
        """
        cid = self.table.identity(provider)
        sec = self.table.secret(provider)
        if cid.error or sec.error:
            err = cid.error or sec.error
            return (Credential(error=err, field="client_id"),
                    Credential(error=err, field="client_secret"))
        if cid.present or sec.present:
            if not (cid.present and sec.present):
                log.warning("oauth channel credential 半配置 provider=%s "
                            "(client_id=%s, client_secret=%s): 不从 meta 补齐（混源=用错凭据）",
                            provider, cid.present, sec.present)
            return cid, sec
        return self.legacy.identity(provider), self.legacy.secret(provider)

    def resolve(self, provider: str, field: str) -> Credential:
        cid, sec = self.pair(provider)
        return cid if field == "client_id" else sec

    def configured(self, provider: str) -> bool:
        """``configured`` 判定：与运行时同一入口（不可用即未配置，不看 meta 副本）。"""
        cid, _sec = self.pair(provider)
        return cid.present


#: 进程级单例（无状态；测试可注入替身）
provider_credentials = ProviderCredentialSource()