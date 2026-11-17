"""第三方邮箱归一（OAuth 建号 / 登录决策的唯一判据来源）。

**它解决什么**（Issue #3054 遗留：v1 只修了 status=4 死锁，邮箱这层没动）：

provider 下发 `email` 时有两种性质完全不同的值：

1. **真实邮箱** —— 可收信、可找回密码、可参与「邮箱已注册 → 关联/二选一」判定；
2. **provider 私有/中转邮箱** —— gitee `xxx@noreply.gitee.com`、GitCode/GitHub
   `noreply.gitcode.com` / `users.noreply.github.com` 以及用户勾选「不公开邮箱」时的
   `noreply.<provider>.*` 形态。这类邮箱**不可收信**，写进 grape_accounts.email 会：
   - 占用 email 唯一键（别人注册同名邮箱时 500 而不是「已注册」业务提示）；
   - 让 is_email_registered 命中，把登录推进「注册新账号 / 关联已有账号」二选一页（死路）。

历史实现把判定散在 `is_placeholder_email()` 一处（只有两个硬编码域名），
而建号路径 `create_grape_account_oauth()` 完全没调它（`is_placeholder = 1 if not email`），
于是私有邮箱仍按真实邮箱入库 —— 这就是「用 gitee 私密邮箱再登一次仍可能落到绑定页」
的直接原因。本模块把「归一（决定入库值）+ 判定」收成唯一来源，供建号、登录决策、
解绑清理三处共用。

**口径**：
- 私有邮箱 → 不落库（email=NULL）、`email_is_placeholder=1`（占位账号语义），
  并把原值交给调用方记日志/审计（不入 email 列）；
- 真实邮箱 → 原样小写入库、`email_is_placeholder=0`；
- 空邮箱 → NULL + 占位。
"""
from __future__ import annotations

import re

# 邮箱形态：本地部@域名含点。gitee 在「不公开邮箱」时下发的不是邮箱而是字面串
# （实锤：'未公开邮箱'），这类值既不能收信也不该占 email 唯一键。
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# provider 私有邮箱的域名特征（小写匹配）。
# 说明：只放**域名后缀**，不猜本地部分；后缀命中即视为不可收信。
PRIVATE_EMAIL_DOMAINS: tuple[str, ...] = (
    "noreply.gitcode.com",
    "no-reply.gitcode.com",
    "noreply.github.com",          # users.noreply.github.com 的宽匹配（endswith）
    "noreply.gitee.com",
    "noreply.gitlab.com",
    "noreply.gitee.io",
    "privaterelay.appleid.com",
)

# 用户勾选「不公开邮箱」时常见的本地部分（形如 12345+user@…）
_PRIVATE_LOCAL_HINTS: tuple[str, ...] = ("noreply", "no-reply", "users.noreply")


class ProviderEmailPolicy:
    """第三方邮箱归一与判定（无状态，类方法即接口）。"""

    PRIVATE_DOMAINS = PRIVATE_EMAIL_DOMAINS

    @classmethod
    def is_private(cls, email: str | None) -> bool:
        """是否不可收信（私有/中转邮箱，或 provider 下发的非邮箱形态字面串）。

        gitee「不公开邮箱」返回的是字面串（'未公开邮箱'），没有 @；历史实现
        在这里 `return False`，于是它被当真实邮箱入库、占 email 唯一键，下一次
        同账号登录撞 email_conflict 并被推进二选一页 —— 必须先过形态校验。
        """
        e = (email or "").strip().lower()
        if not e:
            return False
        if not _EMAIL_SHAPE.match(e):
            return True
        domain = e.rsplit("@", 1)[1]
        if any(domain == d or domain.endswith("." + d) or domain.endswith(d) for d in cls.PRIVATE_DOMAINS):
            return True
        return False

    @classmethod
    def for_storage(cls, email: str | None) -> tuple[str | None, int]:
        """返回 (入库邮箱, email_is_placeholder)。

        空邮箱、非邮箱形态、私有邮箱都返回 (None, 1)：不写 email 列，账号按占位
        账号语义处理（email_is_placeholder=1）。
        """
        e = (email or "").strip().lower()
        if not e:
            return None, 1
        if not _EMAIL_SHAPE.match(e):
            return None, 1
        if cls.is_private(e):
            return None, 1
        return e, 0

    @classmethod
    def is_conflict_eligible(cls, email: str) -> bool:
        """是否参与 email 唯一/冲突判定（私有与空邮箱都不参与）。"""
        return cls.for_storage(email)[1] == 0