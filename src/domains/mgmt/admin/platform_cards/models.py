"""管理面平台卡用例的 DTO（无框架依赖，router 与 service 共用）。

分三组，对应三个用例：
  - `CardView`       列表/详情回读（带凭据存在性与管理面覆盖字段）
  - `CardSaveRequest` 保存请求（整卡 + 凭据通道 + 操作人）
  - `CardSaveResult`  保存结果（逐平台行落库动作、覆盖字段、非致命错误）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["CardView", "CardSaveRequest", "CardSaveResult"]


@dataclass(frozen=True)
class CardView:
    """一张平台卡的回读视图。

    `raw` 是卡定义（整卡回写形态，`event_map`/`fields` 用别名键）；
    `overrides` 是管理面相对基线卡不同的字段（仅展示用，A 方案后不再落库）；
    `managed_by` = "table"（已被管理面接管，文件卡不再自动覆盖）| "file"（文件卡定义）；
    凭据只回存在性，永不回值。
    """

    provider: str
    raw: dict[str, Any]
    overrides: dict[str, Any] = field(default_factory=dict)
    bot_token_configured: bool = False
    webhook_token_configured: bool = False
    webhook_token_supported: bool = False
    bot_token_health: dict[str, Any] = field(default_factory=dict)
    managed_by: str = "file"

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.raw)
        out["overrides"] = dict(self.overrides)
        out["bot_token_configured"] = self.bot_token_configured
        out["webhook_token_configured"] = self.webhook_token_configured
        out["webhook_token_supported"] = self.webhook_token_supported
        out["bot_token_health"] = dict(self.bot_token_health)
        out["managed_by"] = self.managed_by
        return out


@dataclass(frozen=True)
class CardSaveRequest:
    """保存请求（来自管理面 PUT）。

    cards:          {provider: 卡定义 raw}（整卡，未展示字段由调用方原样带回）
    bot_tokens:     {provider: 全局 bot token}（空 = 不改）
    webhook_tokens: {provider: webhook token}（空 = 不改）
    operator_id:    操作人（审计）
    """

    cards: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    bot_tokens: Mapping[str, str] = field(default_factory=dict)
    webhook_tokens: Mapping[str, str] = field(default_factory=dict)
    operator_id: int = 0

    @property
    def providers(self) -> list[str]:
        return sorted(p for p in self.cards if isinstance(p, str) and p)


@dataclass(frozen=True)
class CardSaveResult:
    """保存结果。

    cards:     本次校验通过的 provider 列表
    rows:      {provider: inserted|updated|config_owned|error}（表行落库动作）
    overrides: {provider: {field: value}}（本次生效的管理面覆盖）
    rows_error: 行落库整体失败摘要（非致命：卡定义已保存，须让管理面看见）
    """

    cards: list[str] = field(default_factory=list)
    rows: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, dict] = field(default_factory=dict)
    rows_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "message": "VCS platform cards saved (cache invalidated)",
            "cards": list(self.cards),
            "rows": dict(self.rows),
            "overrides": {p: dict(v) for p, v in self.overrides.items()},
        }
        if self.rows_error:
            out["rows_error"] = self.rows_error
        return out


class CardValidationError(ValueError):
    """整卡校验/唯一性校验失败（路由映射 400，逐条 errors 供前端展示）。"""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = list(errors)