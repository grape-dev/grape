"""凭据有效期值对象（PAT 到期语义的唯一判定处）。

为什么独立成域对象：到期状态（ok / soon / expired / unknown）**同时**被三处消费——
凭据绑定（落库 expires_at）、设置页状态投影（红字提示）、以及将来任务前置检查
（过期就别派发）。三处各写一份 `(date - today).days` 必然漂移（阈值不一致、时区不一致、
解析失败处理不一致），所以在这里定义一次。

不变式：
- 空 expires_at → state="none"（平台没给信息，不猜测、不显示）；
- 解析失败 → state="unknown"（有值但读不懂，界面不显示天数）；
- days_left < 0 → expired；0..SOON_DAYS → soon；其余 ok。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

SOON_DAYS = 14

STATE_NONE = "none"
STATE_OK = "ok"
STATE_SOON = "soon"
STATE_EXPIRED = "expired"
STATE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class CredentialExpiry:
    """一个凭据的到期快照。构造请走 `parse()`，保证三个字段自洽。"""

    at: str = ""
    days_left: int | None = None
    state: str = STATE_NONE

    @classmethod
    def parse(cls, at: str, today: date | None = None) -> "CredentialExpiry":
        raw = str(at or "").strip()
        if not raw:
            return cls()
        try:
            due = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001 - 平台可能给 ISO 时间戳/异常值
            return cls(at=raw, state=STATE_UNKNOWN)
        today = today or date.today()
        left = (due - today).days
        if left < 0:
            state = STATE_EXPIRED
        elif left <= SOON_DAYS:
            state = STATE_SOON
        else:
            state = STATE_OK
        return cls(at=raw, days_left=left, state=state)

    @property
    def is_alert(self) -> bool:
        """界面是否需要用醒目样式（过期 / 即将过期）。"""
        return self.state in (STATE_SOON, STATE_EXPIRED)

    def as_payload(self) -> dict:
        """设置页投影（前端直接吃这三个键，不再自己算天数）。"""
        out: dict = {"expires_at": self.at, "expires_state": self.state}
        if self.days_left is not None:
            out["expires_in_days"] = self.days_left
        return out