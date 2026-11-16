"""D109 凭据代理门面：用户驱动仓库时**现算**该用哪种凭据（2026-11-14 裁定）。

为什么需要它（模型约束）：
- 关系表 `grape_t_user_repo_platform_link` **只存关系与时间**，不存"用哪种凭据"；
- 凭据不进 managed_repos、不在绑定时指定 —— 一律在驱动那一刻按
  `(user_id, repo_provider, need_scope)` 解析。

解析序（d109 §6）：

```
① PAT（用户显式配置 = 意图最明确，优先）
   user_credentials(kind=pat, provider=repo_provider).config_json.token 有效 → source="pat"
② 渠道令牌（PAT 缺席时的正常回落）
   用户有活跃渠道 c（external_identities status ∈ {1,4}）→ source="oauth:<channel>"

**OAuth 与 PAT 是并存的两个模式**（2026-11-14 用户口径）：取 token 恒定 ①PAT ②OAuth，
不看 relation 档位、drivers、scope、audience，**也不看 channel-repo-links 配置**
（那份配置只决定"登录成功后要不要写用户↔平台仓库关系"）。
能力是否真够由上游 API 在调用时回答（403/401）。
③ 都不满足 → token="" + reason（7 个原因码之一）
```

**能力保留**：同域 + scope 足够时渠道令牌照常可用，本模块不是"禁用 OAuth"，只是把
"无声明静默借用 / scope 不足照用"收敛成明确原因码。

**bot 不在此层**：托管动作的三层链（RepoBotToken → GlobalBotToken → UserToken）在
`token_resolver` 里，本模块只服务"用户驱动"路径；没有 bot 时链会回落到用户凭据（既有能力保留）。
"""
from __future__ import annotations

from dataclasses import dataclass

from src.kernel.config import log

# 原因码（对外契约：不允许静默空串，UI/审计必须能显示原因）
NO_PAT = "NO_PAT"                        # 该平台没有配置 PAT
NO_CHANNEL = "NO_CHANNEL"                 # 用户没有任何可用登录渠道
TOKEN_EXPIRED = "TOKEN_EXPIRED"              # 渠道令牌已过期


@dataclass(frozen=True)
class Credential:
    """解析结果：token 为空时 reason 必非空；source 说明"用了谁的令牌"。"""

    token: str = ""
    source: str = ""      # "pat" | "oauth:<channel>"
    reason: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.token)


def _pat_token(user_id: str, repo_provider: str) -> str:
    try:
        from src.domains.mgmt.user import db as user_db

        cfg = user_db.get_credential(str(user_id), repo_provider, kind="pat") or {}
        return str(cfg.get("token") or "")
    except Exception as e:  # 凭据域读失败不影响后面走渠道
        log.warning("credential_resolver: PAT read failed for %s/%s: %s", user_id, repo_provider, e)
        return ""


def _active_channels(user_id: str) -> list[dict]:
    """活跃绑定（status ∈ {1,4}）；4=过期，仍列出以便给出 TOKEN_EXPIRED 而不是 NO_CHANNEL。"""
    try:
        from src.domains.mgmt.auth.grape_account_db import get_user_identities

        return list(get_user_identities(str(user_id)) or [])
    except Exception as e:
        log.warning("credential_resolver: identities read failed for %s: %s", user_id, e)
        return []


def _channel_token(session, user_id: str, channel: str) -> str:
    """取该用户在该渠道的 OAuth 令牌（解密；不触发刷新 —— 刷新仍归 token_resolver）。"""
    try:
        from sqlalchemy import text

        from src.domains.mgmt.auth.credential_crypto import decrypt

        row = session.execute(
            text(
                "SELECT access_token FROM external_identities "
                "WHERE grape_user_id=:u AND provider=:p AND status IN (1,4) LIMIT 1"
            ),
            {"u": str(user_id), "p": channel},
        ).fetchone()
        if row and row[0]:
            return decrypt(row[0])
    except Exception as e:
        log.warning("credential_resolver: channel token read failed %s/%s: %s", user_id, channel, e)
    return ""


def resolve_user_credential(user_id: str, repo_provider: str, need_scope: str = "", *,
                            session=None) -> Credential:
    """按 (user, repo_provider) 现算凭据（见模块 docstring 的解析序）。

    判据只有"关系是否配置"一条；need_scope 保留为入参但**不参与判定**。
    """
    own_session = session is None
    if own_session:
        from sqlalchemy.orm import Session

        from src.kernel.persistence.engine import get_engine

        session = Session(get_engine())
    try:
        # ① PAT
        pat = _pat_token(user_id, repo_provider)
        if pat:
            return Credential(token=pat, source="pat")

        # ② 渠道令牌
        identities = _active_channels(user_id)
        if not identities:
            return Credential(reason=NO_CHANNEL, detail="用户没有绑定任何登录渠道")
        # 渠道令牌：PAT 缺席时的正常回落（不看关系配置，两种模式并存）
        saw_expired = False
        for ident in identities:
            channel = str(ident.get("provider") or "")
            if not channel:
                continue
            if str(ident.get("status")) == "4":
                saw_expired = True
                continue
            token = _channel_token(session, user_id, channel)
            if token:
                return Credential(token=token, source=f"oauth:{channel}")
            saw_expired = True

        if saw_expired:
            return Credential(reason=TOKEN_EXPIRED, detail="渠道令牌已过期，需重新授权")
        return Credential(reason=NO_CHANNEL, detail="没有可用于该平台的渠道")
    finally:
        if own_session:
            session.close()


def describe(user_id: str, repo_provider: str, need_scope: str = "") -> dict:
    """诊断视图（管理台/排查用）：不含 token 值，只回 source/reason。"""
    cred = resolve_user_credential(user_id, repo_provider, need_scope)
    return {
        "provider": repo_provider,
        "available": cred.ok,
        "source": cred.source,
        "reason": cred.reason,
        "detail": cred.detail,
    }