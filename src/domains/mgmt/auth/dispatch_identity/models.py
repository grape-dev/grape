"""派发身份的领域模型（值对象 + 枚举）—— 不依赖 DB / 框架，可单测可序列化。"""
from __future__ import annotations

import enum
from dataclasses import dataclass


_ON_WORDS = ("on", "1", "true", "yes", "enable", "enabled")
_OFF_WORDS = ("off", "0", "false", "no", "disable", "disabled")


class DispatchMode(str, enum.Enum):
    """生效开关（GRAPE_DISPATCH_USER_ID）。默认 SHADOW：只记录不生效。"""

    OFF = "off"
    SHADOW = "shadow"
    ON = "on"

    @classmethod
    def parse(cls, raw) -> "DispatchMode":
        """解析开关值。

        默认（未设置/空）= **ON**：按 id 认人是修复 Issue #21 的必要条件，
        默认演练等于没修（hello-blog 仍取不到令牌、推不了分支）。
        显式 shadow 仍可观察；off 可一键回滚；无法识别的值回落 SHADOW（故障安全：
        拼错时宁可不生效，也不会悄悄把口径改掉）。
        """
        value = str(raw or "").strip().lower()
        if not value:
            return cls.ON
        if value in _ON_WORDS:
            return cls.ON
        if value in _OFF_WORDS:
            return cls.OFF
        return cls.SHADOW


class IdentitySource(str, enum.Enum):
    """身份来源（按可信度排序）。NONE = 没解析到。"""

    TASK = "task"
    ACCOUNT = "account"
    ACTOR = "actor"
    NONE = ""


@dataclass(frozen=True)
class DispatchIdentity:
    """一次派发的身份判定结果（纯值对象）。

    `user_id` 是**候选**：是否真正生效看 `applies`（mode=ON 且 id 非空）。
    shadow 模式下 user_id 照样解析出来，交给日志观察，但不写库、不取令牌。
    """

    user_id: str = ""
    source: IdentitySource = IdentitySource.NONE
    account_name: str = ""
    mode: DispatchMode = DispatchMode.SHADOW

    @property
    def applies(self) -> bool:
        return self.mode is DispatchMode.ON and bool(self.user_id)

    @property
    def has_candidate(self) -> bool:
        return bool(self.user_id)
