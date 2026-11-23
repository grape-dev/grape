"""平台卡「管理面覆盖层」领域对象（Issue #3580 A2）。

问题形状：卡定义的真源分层是「配置文件 → meta 整卡 → DB 表行」。配置文件派生的行
（source=config）按设计不允许管理面回写（下次导入会冲回），于是管理台保存
`ca_bundle` 之类字段时返回 200 却永远不生效——运维看不到任何边界。

抽象：把「管理面相对配置文件真正改过的字段」建模成值对象 `CardOverrides`
（provider → {field: value}），作为读卡链的**最后一层**叠加。它有三条显式契约：

1. 只记**变化过**的键——改回配置文件的值即等于撤销覆盖（不留永久空覆盖）；
2. 显式置空记为 ""（键存在即尊重），与「键缺席 = 不覆盖」区分；
3. 存储走 meta 契约（`vcs_platform_overrides`），不新增表列、不跨域直写。

存储与读卡分别归 runtime 域；管理面（mgmt）只经 `runtime.public` 拿「该写哪些覆盖」，
自己用 meta 契约落库——跨域仍走契约，不直连对方库表。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "CardOverrides",
    "CardOverridesError",
    "META_KEY",
    "diff_card_fields",
]

#: meta 契约键：{provider: {field: value}}，只含相对配置文件/表行变化过的字段
META_KEY = "vcs_platform_overrides"

#: 卡字段回写形态里，这两个键在值对象上叫 *_overrides（PUT/GET 做别名转换）
_FIELD_ALIAS = {"event_map": "event_map_overrides", "fields": "fields_overrides"}

#: 非「定义字段」：代码派生或凭据域所有，不参与覆盖比较
_SKIP_FIELDS = {"provider", "resources", "capabilities", "token_env"}


class CardOverridesError(ValueError):
    """覆盖层内容非法（非对象/非字段映射）。"""


def _json_norm(value: Any) -> str:
    """字段比较用归一：None/""、tuple/list、dict 键序都不算差异。"""
    if value is None or value == "":
        return '""'
    if isinstance(value, (tuple, list)):
        return json.dumps(list(value), sort_keys=True, ensure_ascii=False, default=str)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


@dataclass
class CardOverrides:
    """管理面覆盖层（不可变语义：所有变更方法返回新实例）。"""

    by_provider: dict[str, dict] = field(default_factory=dict)

    # ── 构造 / 序列化 ────────────────────────────────────────────────
    @classmethod
    def empty(cls) -> "CardOverrides":
        return cls({})

    @classmethod
    def from_mapping(cls, raw: Mapping | None) -> "CardOverrides":
        if not raw:
            return cls({})
        if not isinstance(raw, Mapping):
            raise CardOverridesError(f"覆盖层必须是对象，收到 {type(raw).__name__}")
        clean: dict[str, dict] = {}
        for provider, fields in raw.items():
            if not isinstance(provider, str) or not provider:
                continue
            if not isinstance(fields, Mapping) or not fields:
                continue
            clean[provider] = {str(k): v for k, v in fields.items() if isinstance(k, str)}
        return cls(clean)

    @classmethod
    def from_json(cls, text: str | None) -> "CardOverrides":
        """坏 JSON/非对象一律视为空覆盖（读卡不能被脏 meta 拖垮）。"""
        if not text:
            return cls({})
        try:
            return cls.from_mapping(json.loads(text))
        except (ValueError, CardOverridesError):
            return cls({})

    def to_json(self) -> str:
        return json.dumps(self.by_provider, ensure_ascii=False, sort_keys=True)

    # ── 查询 / 变更 ────────────────────────────────────────────────
    def for_provider(self, provider: str) -> dict:
        return dict(self.by_provider.get(provider) or {})

    def providers(self) -> list[str]:
        return sorted(self.by_provider)

    def with_provider(self, provider: str, fields: Mapping | None) -> "CardOverrides":
        """设置某平台的覆盖；fields 为空 → 撤销该平台覆盖。返回新实例。"""
        next_map = {p: dict(f) for p, f in self.by_provider.items()}
        if fields:
            next_map[provider] = {str(k): v for k, v in fields.items()}
        else:
            next_map.pop(provider, None)
        return CardOverrides(next_map)

    def recompute(self, submitted: Mapping[str, Mapping], base_lookup) -> "CardOverrides":
        """管理面一次保存后的新覆盖层。

        submitted: {provider: 提交的整卡 raw}；base_lookup: provider → 不含覆盖层的卡。
        与基线一致的 provider 覆盖整条移除（= 改回配置文件值），不同的只留变化键。
        """
        next_map = {p: dict(f) for p, f in self.by_provider.items()}
        for provider, fields in (submitted or {}).items():
            changed = diff_card_fields(fields, base_lookup(provider))
            if changed:
                next_map[provider] = changed
            else:
                next_map.pop(provider, None)
        return CardOverrides(next_map)

    def prune_stale(self, base_lookup) -> "CardOverrides":
        """清掉与基线卡等价的覆盖项（老版本升级后遗留的"改了不生效"来源）。

        覆盖层只应保留"与基线确实不同"的键。文件卡随版本升级后，旧覆盖往往与新基线
        取值相同却仍留在 meta 里：运行时它照样叠加（值为空串时还会盖掉新默认值），
        表现为管理面改不动。返回值是新实例；基线条不出来的 provider 原样保留（人工处理）。
        """
        next_map: dict[str, dict] = {}
        for provider, fields in self.by_provider.items():
            try:
                base = base_lookup(provider)
            except Exception:  # noqa: BLE001 - 基线条不出来不该让读卡失败
                base = None
            if base is None:
                next_map[provider] = dict(fields)
                continue
            kept = diff_card_fields(fields, base)
            if kept:
                next_map[provider] = kept
        return CardOverrides(next_map)

    def stale_providers(self, base_lookup) -> list[str]:
        """与基线等价（升级残留）的 provider 列表，供一次性告警/运维清理。"""
        out: list[str] = []
        for provider, fields in self.by_provider.items():
            try:
                base = base_lookup(provider)
            except Exception as lookup_err:  # noqa: BLE001 - 基线不可得=无法判「残留」，留痕后跳过
                from src.kernel.config import log as _log
                _log.debug(f"stale_providers: baseline lookup failed for {provider}: {lookup_err}")
                continue
            if base is not None and not diff_card_fields(fields, base):
                out.append(provider)
        return sorted(out)

    # ── 应用 ────────────────────────────────────────────────────
    def apply_to(self, base_raws: dict) -> None:
        """把覆盖层叠加到卡 raw 字典（原地）。必须在文件/meta/表行之后叠。"""
        for provider, fields in self.by_provider.items():
            if provider not in base_raws or not fields:
                continue
            base_raws[provider].update(fields)


def diff_card_fields(submitted: Mapping | None, card) -> dict:
    """算出「管理面提交值 vs 基线卡」真正不同的字段。

    基线卡必须是**不含覆盖层**的卡（`base_platform_card`）：拿生效卡当基线会把上一轮
    覆盖当成基线，清空后仍留一条 `field: ""` 的永久覆盖。
    """
    import dataclasses as _dc

    if card is None or not isinstance(submitted, Mapping):
        return {}
    current = _dc.asdict(card)
    out: dict[str, Any] = {}
    for key, value in submitted.items():
        if not isinstance(key, str) or key in _SKIP_FIELDS:
            continue
        current_value = current.get(_FIELD_ALIAS.get(key, key))
        if _json_norm(value) != _json_norm(current_value):
            out[key] = value
    return out