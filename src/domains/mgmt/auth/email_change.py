"""邮箱变更领域服务（Issue #3469 G / P / Q）。

把"改绑邮箱"的编排从路由巨型文件里拆出来，按领域建模形态落位：

- **值对象**：`EmailFactor`（二次认证因子）、`EmailChangeRequest` / `EmailChangeCommand`
   （入参）、`EmailChangeResult`（出参）、`LimitPolicy`（频率策略常量）。
- **端口（契约）**：`AccountPort`（账号读写）、`VerificationPort`（验证码服务）、
  `NotificationPort`（通知渠道）——领域服务只依赖协议，不 import 具体实现，
  换实现（站内信/IM 替代邮件）不改领域逻辑；跨域能力走端口，不直接摸外域表。
- **领域服务**：`EmailChangeService.request_code()` / `commit()`，只编排、只判策略；
  SQL/HTTP 一律不在本层。
- **异常**：`EmailChangeError(status, error_code, message, require_factor)`——路由层
  仅做 "捕获 → HTTPException" 一行映射，失败原因由领域层给出（P）。

设计口径（见 docs/weekly-11/email-verification-design.md D4/D5/D6/D7）：

- 验证码存储键统一 `user:<id>`，`new_email` 入哈希盐（Q）：发往 A 的码不能确认 B。
- 二次因子"有则要求、无则放行"（G）：有密码要当前密码；无密码但原邮箱可投递要原邮箱码；
  两者都没有（占位/私有中转邮箱）不要求——否则这批账号永远换不了邮箱（Q 验收第 11 条）。
- 限流两把锁：新地址维度（由验证码服务按 new_email 计）+ 用户维度（本服务的每小时上限）。
- 通知只在库写成功后发；失败也通知原邮箱（若可投递），不再 `except: pass` 静默。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from src.domains.mgmt.auth import verification as verification_service
from src.domains.mgmt.auth import grape_account_db
from src.domains.mgmt.auth.identity_email import ProviderEmailPolicy

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------

class EmailFactor(str, Enum):
    """改绑邮箱可用的二次认证因子。"""

    PASSWORD = "password"
    OLD_EMAIL_CODE = "old_email_code"
    NONE = "none"


@dataclass(frozen=True)
class LimitPolicy:
    """改绑频率策略（D7）。数值来源：Issue #3469 E 的"每用户每小时上限"要求。"""

    scene: str = "change_email"
    confirm_scene: str = "change_email_confirm"
    hourly_cap: int = 5
    hourly_window_seconds: int = 3600

    def key(self, user_id: str) -> str:
        return f"user:{user_id}"


@dataclass(frozen=True)
class EmailChangeRequest:
    user_id: str
    new_email: str
    ip: str = ""


@dataclass(frozen=True)
class EmailChangeCommand:
    user_id: str
    new_email: str
    code: str
    ip: str = ""
    password: str = ""
    old_email_code: str = ""


@dataclass(frozen=True)
class EmailChangeResult:
    message: str
    require_factor: EmailFactor = EmailFactor.NONE
    old_email: str = ""


class EmailChangeError(Exception):
    """领域错误 → 路由层映射为 HTTP 状态 + error_code（P）。"""

    def __init__(self, status: int, error_code: str, message: str,
                 require_factor: EmailFactor | None = None, retry_after: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.message = message
        self.require_factor = require_factor
        self.retry_after = int(retry_after or 0)


# ---------------------------------------------------------------------------
# 端口（跨层/跨域契约）
# ---------------------------------------------------------------------------

class AccountPort(Protocol):
    """账号域能力（当前实现同为 mgmt 域，抽协议是为了可替换/可测）。"""

    def get(self, user_id: str) -> dict | None: ...

    def update_email(self, user_id: str, new_email: str) -> bool | str: ...

    def is_email_registered(self, email: str) -> bool: ...


class VerificationPort(Protocol):
    """验证码域能力（发码 / 校验 / 计频）。"""

    def send_code(self, email: str, scene: str, ip: str = "",
                  rate_limit_key: str = "", target: str = "") -> dict: ...

    def verify_code(self, key: str, scene: str, code: str, target: str = "",
                    consume: bool = True) -> dict: ...

    def count_sends(self, key: str, scene: str, window_seconds: int = 600) -> int: ...

    def check_rate_limit(self, rl_key: str, scene: str, ip: str = "") -> dict: ...


class NotificationPort(Protocol):
    """通知渠道（邮件 + 站内信/IM）。

    Issue #3469 F：旧邮箱不可投递时不能"干脆不发"——必须改走站内信/IM，
    否则真实被改绑（含盗号）时原机主完全无感。
    """

    def send(self, to: str, subject: str, body: str) -> bool: ...

    def notify_user(self, user_id: str, subject: str, body: str) -> bool: ...


class _GrapeAccountAdapter:
    """AccountPort 的 mgmt 域实现（薄委托，不加判定）。"""

    def get(self, user_id: str) -> dict | None:
        return grape_account_db.get_grape_account_by_id(user_id)

    def update_email(self, user_id: str, new_email: str) -> bool | str:
        return grape_account_db.update_email(user_id, new_email)

    def is_email_registered(self, email: str) -> bool:
        return grape_account_db.is_email_registered(email)


class _VerificationAdapter:
    """VerificationPort 的 mgmt 域实现（薄委托）。"""

    def send_code(self, email: str, scene: str, ip: str = "",
                  rate_limit_key: str = "", target: str = "") -> dict:
        return verification_service.send_code(email, scene, ip,
                                              rate_limit_key=rate_limit_key,
                                              target=target)

    def verify_code(self, key: str, scene: str, code: str, target: str = "",
                    consume: bool = True) -> dict:
        return verification_service.verify_code(key, scene, code, target=target,
                                                consume=consume)

    def count_sends(self, key: str, scene: str, window_seconds: int = 600) -> int:
        return verification_service.count_sends(key, scene, window_seconds)

    def check_rate_limit(self, rl_key: str, scene: str, ip: str = "") -> dict:
        return verification_service.check_rate_limit(rl_key, scene, ip)


class _EmailNotificationAdapter:
    """NotificationPort 实现：邮件 + 站内信/IM（失败只记日志，不阻断业务）。"""

    def send(self, to: str, subject: str, body: str) -> bool:
        try:
            from src.domains.mgmt.auth.email_service import get_email_service

            return bool(get_email_service().send_notification(to, subject, body))
        except Exception as exc:  # noqa: BLE001 - 通知失败不能回滚账号变更
            log.warning(f"email_change notification failed (to={_mask(to)}): {exc}")
            return False

    def notify_user(self, user_id: str, subject: str, body: str) -> bool:
        """站内信/IM 通道——经 runtime 域 public 契约（跨域不碰外域表）。

        复审 P1：① 不再 `int(user_id)`（D105 后账号 id 是 UUID，int() 必 ValueError）；
        ② 不再无条件 return True——runtime 侧返回"是否真的入队"，这里如实回传，
        失败只记日志（不能回滚账号变更）。
        """
        try:
            from src.domains.runtime import public as runtime_public

            ok = runtime_public.schedule_notify(str(user_id or ""), "email_change",
                                                f"{subject}：{body}")
            if not ok:
                log.warning(f"email_change in-app notify enqueued nothing (user={user_id})")
            return bool(ok)
        except Exception as exc:  # noqa: BLE001 - 通知降级失败不影响账号变更
            log.warning(f"email_change in-app notify failed (user={user_id}): {exc}")
            return False


# ---------------------------------------------------------------------------
# 策略（纯判定，无 IO）
# ---------------------------------------------------------------------------

class EmailChangePolicy:
    """改绑相关判定的单一来源（G / D）。"""

    @staticmethod
    def is_deliverable(account: dict | None) -> bool:
        return ProviderEmailPolicy.is_deliverable((account or {}).get("email"))

    @classmethod
    def required_factor(cls, account: dict | None) -> EmailFactor:
        """有则要求、无则放行：占位账号不能因加固被锁死（Q 验收 11）。"""
        if (account or {}).get("password_hash"):
            return EmailFactor.PASSWORD
        if cls.is_deliverable(account):
            return EmailFactor.OLD_EMAIL_CODE
        return EmailFactor.NONE


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------

class EmailChangeService:
    """改绑邮箱用例编排。"""

    def __init__(self, accounts: AccountPort | None = None,
                 verification: VerificationPort | None = None,
                 notifier: NotificationPort | None = None,
                 policy: LimitPolicy | None = None) -> None:
        self._accounts = accounts or _GrapeAccountAdapter()
        self._verification = verification or _VerificationAdapter()
        self._notifier = notifier or _EmailNotificationAdapter()
        self._policy = policy or LimitPolicy()

    # -- 发码 ------------------------------------------------------------
    def request_code(self, req: EmailChangeRequest) -> EmailChangeResult:
        new_email = (req.new_email or "").strip().lower()
        if not ProviderEmailPolicy.is_valid(new_email):
            raise EmailChangeError(400, "EMAIL_FORMAT", "邮箱格式不正确")
        # 复审 P2：只有格式校验不够——把 noreply.*/私有中转绑成账号邮箱，会重新造出
        # 「有 email 但收不到信」形态，还永久占住该中转地址的唯一键。
        if not ProviderEmailPolicy.is_deliverable(new_email):
            raise EmailChangeError(400, "EMAIL_UNDELIVERABLE", "该邮箱不可接收邮件，请使用常用邮箱")

        account = self._accounts.get(req.user_id)
        old_email = ((account or {}).get("email") or "").strip().lower()
        if old_email and new_email == old_email:
            raise EmailChangeError(400, "EMAIL_SAME", "新邮箱与当前邮箱相同")
        if self._accounts.is_email_registered(new_email):
            raise EmailChangeError(400, "EMAIL_TAKEN", "该邮箱已被占用")

        key = self._policy.key(req.user_id)
        if self._verification.count_sends(key, self._policy.scene,
                                          self._policy.hourly_window_seconds) >= self._policy.hourly_cap:
            raise EmailChangeError(429, "CHANGE_EMAIL_TOO_OFTEN",
                                   f"换绑过于频繁，请 {self._policy.hourly_window_seconds // 60} 分钟后再试")

        # Issue #3469 E（复审补齐）：新地址维度也要限流——只按 user:<id> 计时的话，
        # 一个账号 1 小时可对 5 个任意地址发码，多账号可对同一地址反复发。
        # check_rate_limit 以新地址为 key（命中即 60s 间隔 / 3 次 10 分钟），并落影子记录。
        target_rl = self._verification.check_rate_limit(new_email, self._policy.scene, req.ip)
        if not target_rl.get("ok"):
            # 透传限流器给的结构化码（60s 间隔/窗口 → RATE_LIMITED，IP → IP_LIMITED），
            # 只有裸拒绝才回落到目标地址维度码 —— 复审指出这里曾把"请 59 秒后再试"
            # 报成 CODE_SEND_FAILED，与真正的发信失败混淆。
            raise EmailChangeError(429, target_rl.get("error_code") or "CHANGE_EMAIL_TARGET_LIMITED",
                                   target_rl.get("message") or "该邮箱请求过于频繁，请稍后再试",
                                   retry_after=target_rl.get("retry_after") or 0)

        factor = EmailChangePolicy.required_factor(account)

        sent = self._verification.send_code(new_email, self._policy.scene, req.ip,
                                            rate_limit_key=key, target=new_email)
        if not sent.get("ok"):
            raise EmailChangeError(429, sent.get("error_code") or "CODE_SEND_FAILED",
                                   sent.get("message") or "验证码发送失败",
                                   retry_after=sent.get("retry_after") or 0)

        if factor is EmailFactor.OLD_EMAIL_CODE:
            confirm = self._verification.send_code(old_email, self._policy.confirm_scene, req.ip,
                                                   rate_limit_key=key, target=old_email)
            if not confirm.get("ok"):
                # 原邮箱确认码发不出去 → 后续提交必然失败，直接如实反馈（不静默降级）
                raise EmailChangeError(429, confirm.get("error_code") or "CODE_SEND_FAILED",
                                       "原邮箱确认码发送失败，请稍后重试",
                                       retry_after=confirm.get("retry_after") or 0)
        return EmailChangeResult("验证码已发送", factor)

    # -- 提交 ------------------------------------------------------------
    def commit(self, cmd: EmailChangeCommand) -> EmailChangeResult:
        new_email = (cmd.new_email or "").strip().lower()
        if not ProviderEmailPolicy.is_valid(new_email):
            raise EmailChangeError(400, "EMAIL_FORMAT", "邮箱格式不正确")
        if not ProviderEmailPolicy.is_deliverable(new_email):
            raise EmailChangeError(400, "EMAIL_UNDELIVERABLE", "该邮箱不可接收邮件，请使用常用邮箱")

        account = self._accounts.get(cmd.user_id)
        if not account:
            raise EmailChangeError(404, "ACCOUNT_NOT_FOUND", "账号不存在")
        old_email = (account.get("email") or "").strip().lower()
        if old_email and new_email == old_email:
            raise EmailChangeError(400, "EMAIL_SAME", "新邮箱与当前邮箱相同")

        key = self._policy.key(cmd.user_id)

        # 1) 先只判定新邮箱验证码、不消费：密码/原邮箱码输错一次不能把码烧掉
        #    （复审实测：原顺序 verify→factor 时，密码错一次后同一个码再提交即 CODE_USED）
        probe = self._verification.verify_code(key, self._policy.scene, cmd.code,
                                               target=new_email, consume=False)
        if not probe.get("ok"):
            raise EmailChangeError(400, probe.get("error_code") or "CODE_INVALID",
                                   probe.get("message") or "验证码无效或已过期")

        # 2) 二次因子校验（失败也只消耗因子，不消耗新邮箱码）
        factor = EmailChangePolicy.required_factor(account)
        self._require_second_factor(cmd, account, factor, key, old_email)

        # 3) 所有前置条件都过了才正式消费新邮箱验证码
        verified = self._verification.verify_code(key, self._policy.scene, cmd.code,
                                                  target=new_email, consume=True)
        if not verified.get("ok"):
            raise EmailChangeError(400, verified.get("error_code") or "CODE_INVALID",
                                   verified.get("message") or "验证码无效或已过期")

        result = self._accounts.update_email(cmd.user_id, new_email)
        if result == "conflict":
            self._notify_failure(cmd.user_id, old_email, new_email)
            raise EmailChangeError(409, "EMAIL_TAKEN", "该邮箱已被占用")
        if not result:
            self._notify_failure(cmd.user_id, old_email, new_email)
            raise EmailChangeError(500, "EMAIL_UPDATE_FAILED", "邮箱更新失败")

        self._notify_success(cmd.user_id, old_email, new_email, factor)
        return EmailChangeResult("邮箱变更成功", factor, old_email=old_email)

    def _require_second_factor(self, cmd: EmailChangeCommand, account: dict,
                               factor: EmailFactor, key: str, old_email: str) -> None:
        if factor is EmailFactor.PASSWORD:
            if not cmd.password:
                raise EmailChangeError(400, "NEED_SECOND_FACTOR", "请输入当前密码",
                                       require_factor=factor)
            from src.domains.mgmt.auth import password as auth_password
            from src.domains.mgmt.auth import login_throttle

            # 复审 P2：本 PR 新开的密码判定面必须接节流，否则拿到会话即可无限制试密码
            throttle_key = f"change_email_pwd:{cmd.user_id}"
            gate = login_throttle.check_throttle(throttle_key, cmd.ip or "")
            if not gate.get("allowed", True):
                raise EmailChangeError(429, gate.get("error_code") or "TOO_MANY_ATTEMPTS",
                                       gate.get("message") or "尝试过于频繁，请稍后再试")
            if not auth_password.verify_password(cmd.password, str(account.get("password_hash") or "")):
                login_throttle.record_failure(throttle_key, cmd.ip or "")
                raise EmailChangeError(400, "PASSWORD_INVALID", "当前密码不正确")
            login_throttle.clear_failures(throttle_key, cmd.ip or "")
        elif factor is EmailFactor.OLD_EMAIL_CODE:
            if not cmd.old_email_code:
                raise EmailChangeError(400, "NEED_SECOND_FACTOR", "请输入原邮箱验证码",
                                       require_factor=factor)
            confirm = self._verification.verify_code(key, self._policy.confirm_scene,
                                                     cmd.old_email_code, target=old_email)
            if not confirm.get("ok"):
                raise EmailChangeError(400, confirm.get("error_code") or "CODE_INVALID",
                                       "原邮箱验证码无效或已过期")
        else:
            # NONE：无任何可验证因子（占位/私密中转邮箱 + 无密码），按 Q 放行。
            # 设计 D6 要求留审计：这类改绑没有第二因子，只能靠事后追溯。
            try:
                from src.domains.mgmt.auth import audit as _audit

                _audit.log_audit("email_change_no_secondary_factor", user_id=cmd.user_id,
                                 ip=cmd.ip or "",
                                 detail={"account_id": cmd.user_id,
                                         "old_email": _audit.mask_email(old_email or ""),
                                         "new_email": _audit.mask_email(str(cmd.new_email or ""))})
            except Exception as _e:  # noqa: BLE001 - 审计失败不影响主流程
                log.warning(f"email_change_no_secondary_factor audit failed: {_e}")

    # -- 通知（F）--------------------------------------------------------
    def _notify_success(self, user_id: str, old_email: str, new_email: str,
                        factor: EmailFactor) -> None:
        if old_email and ProviderEmailPolicy.is_deliverable(old_email):
            self._notifier.send(old_email, "邮箱已变更",
                                "您的 Grape 账号邮箱已被更换。若非本人操作，请联系管理员。")
        else:
            # Issue #3469 F：旧邮箱不可投递（或为空）时改走站内信/IM，
            # 不能让原机主在真实改绑（含盗号）时完全无感。
            self._notifier.notify_user(user_id, "邮箱已变更",
                                       "您的账号邮箱已被更换。若非本人操作，请立即联系管理员。")
        self._notifier.send(new_email, "邮箱绑定成功",
                            "该地址已绑定为您的 Grape 账号邮箱，后续验证码将发送到此地址。")

    def _notify_failure(self, user_id: str, old_email: str, new_email: str) -> None:
        if old_email and ProviderEmailPolicy.is_deliverable(old_email):
            self._notifier.send(old_email, "邮箱变更失败",
                                "有人尝试变更您的 Grape 账号邮箱但未成功。若非本人操作，建议立即修改密码。")
        else:
            self._notifier.notify_user(user_id, "邮箱变更失败",
                                       "有人尝试变更您的账号邮箱但未成功。若非本人操作，请立即修改密码。")


def _mask(email: str) -> str:
    """日志脱敏（只留首字符与域名）。"""
    try:
        local, _, domain = (email or "").partition("@")
        return f"{local[:1]}***@{domain}"
    except Exception:  # noqa: BLE001
        return "***"