"""平台卡管理面用例服务（Issue #3580 A2 重构）。

为什么是一个服务而不是路由里的一段代码：
- **用例边界**：一次保存 = 校验整卡 → 唯一性 → 落 meta（卡定义 + 凭据通道 + 覆盖层）
  → 落行（手建实例）→ 失效缓存 → 审计。这是 6 个步骤的编排，属于领域用例，
  不属于 HTTP 适配（router 只该做 body → 请求对象 → 结果 → JSON）。
- **跨域契约**（3 库 4 进程）：平台卡定义、覆盖层计算、缓存失效都在 runtime 域，
  本服务只经 `runtime.public` 契约调用，不 import 对方内部模块、不直连对方库表；
  平台行的表在 mgmt 域，走本域仓储；审计走 mgmt 认证域契约。
- **可测性**：契约方法可按需替换，路由不再带业务分支。
"""
from __future__ import annotations

import dataclasses as _dc
import json

from src.domains.mgmt.admin.platform_cards.models import (
    CardSaveRequest,
    CardSaveResult,
    CardValidationError,
    CardView,
)
from src.domains.mgmt.admin.platform_cards.repository import CardRowStore, PlatformRowRepository

__all__ = ["PlatformCardService"]


class PlatformCardService:
    """平台卡管理面用例（无状态，可复用）。"""

    def __init__(self, rows: CardRowStore | None = None) -> None:
        self._rows = rows or PlatformRowRepository()

    # ── 读 ────────────────────────────────────────────────────────
    def list_cards(self) -> tuple[dict[str, CardView], str]:
        """全部平台卡视图 + 探活检查时间。凭据只回存在性。"""
        from src.domains.mgmt.user.db import get_meta_value
        from src.domains.runtime.public import (
            get_platform_card,
            list_vcs_platforms,
            platform_card_overrides_meta_key,
        )

        health, checked_at = self._bot_health(get_meta_value)
        overrides = self._overrides(get_meta_value)

        views: dict[str, CardView] = {}
        for provider in list_vcs_platforms():
            card = get_platform_card(provider)
            if card is None:
                continue
            raw = _dc.asdict(card)
            raw.pop("provider", None)
            raw.pop("resources", None)
            raw.pop("capabilities", None)
            raw["event_map"] = raw.pop("event_map_overrides", {})
            raw["fields"] = raw.pop("fields_overrides", {})
            raw["token_env"] = card.token_env
            bot_key = card.global_bot_meta_key or f"global_bot_{provider}_token"
            views[provider] = CardView(
                provider=provider,
                raw=raw,
                overrides=overrides.get(provider, {}),
                bot_token_configured=bool(get_meta_value(bot_key)),
                webhook_token_configured=bool(get_meta_value(f"{provider}_webhook_token")),
                webhook_token_supported=bool(card.webhook_token_field),
                bot_token_health=health.get(provider, {}),
            )
        return views, checked_at

    # ── 写 ────────────────────────────────────────────────────────
    def save_cards(self, request: CardSaveRequest) -> CardSaveResult:
        """整卡保存：校验 → meta（定义/凭据/覆盖层）→ 行 → 缓存 → 审计。"""
        from src.domains.runtime import public as runtime
        from src.domains.runtime.public import (
            build_platform_card,
            compute_card_overrides,
            invalidate_platform_card_cache,
            platform_card_overrides_meta_key,
            PlatformCardError,
        )

        cleaned = self._validate_cards(request.cards, build_platform_card, PlatformCardError)
        self._assert_unique(cleaned)

        meta_pairs = self._meta_pairs(request, cleaned, build_platform_card, runtime)
        overrides = self._merge_overrides(cleaned, request, runtime, meta_pairs)
        runtime.set_meta_values_any(meta_pairs)
        invalidate_platform_card_cache()

        rows, rows_error = self._write_rows(cleaned, invalidate_platform_card_cache)
        self._audit(request, cleaned, rows)

        return CardSaveResult(
            cards=sorted(cleaned),
            rows=rows,
            overrides={p: overrides.get(p, {}) for p in cleaned},
            rows_error=rows_error,
        )

    # ── 内部步骤（每个步骤单一职责，便于单独测） ───────────────────
    @staticmethod
    def _bot_health(get_meta) -> tuple[dict, str]:
        try:
            raw = get_meta("bot_token_health")
            if not raw:
                return {}, ""
            data = json.loads(raw) or {}
            return data.get("platforms") or {}, data.get("checked_at") or ""
        except Exception:
            return {}, ""

    @staticmethod
    def _overrides(get_meta) -> dict[str, dict]:
        from src.domains.runtime.public import platform_card_overrides_meta_key

        try:
            return json.loads(get_meta(platform_card_overrides_meta_key()) or "{}") or {}
        except Exception:
            return {}

    @staticmethod
    def _validate_cards(cards, build_card, platform_card_error) -> dict[str, dict]:
        """整卡校验 + 归一成「整卡回写形态」（asdict + 别名键 + capabilities 归一）。

        白名单清洗是历史坑：手抄字段清单每漏一个能力位就是一次静默降级，
        所以以 Builder 校验后的卡为准全字段落库。
        """
        cleaned: dict[str, dict] = {}
        errors: list[str] = []
        for provider, raw in (cards or {}).items():
            try:
                card = build_card(provider, dict(raw or {}))
            except platform_card_error as exc:
                errors.append(f"{provider}: {exc}")
                continue
            payload = _dc.asdict(card)
            payload.pop("provider", None)
            payload["event_map"] = payload.pop("event_map_overrides", {})
            payload["fields"] = payload.pop("fields_overrides", {})
            caps = payload.get("capabilities")
            if caps:
                payload["capabilities"] = list(caps)
            else:
                payload.pop("capabilities", None)
            cleaned[provider] = payload
        if errors:
            raise CardValidationError(errors)
        return cleaned

    @staticmethod
    def _assert_unique(cleaned: dict[str, dict]) -> None:
        """D14 invariant：host / webhook_path 全局唯一（多实例卡不能撞）。"""
        errors: list[str] = []
        hosts: dict[str, str] = {}
        paths: dict[str, str] = {}
        for provider, raw in cleaned.items():
            host = raw.get("host", "")
            if host in hosts:
                errors.append(f"{provider}: host 与 {hosts[host]} 重复")
            hosts[host] = provider
            path = raw.get("webhook_path", "")
            if path in paths:
                errors.append(f"{provider}: webhook_path 与 {paths[path]} 重复")
            paths[path] = provider
        if errors:
            raise CardValidationError(errors)

    @staticmethod
    def _meta_pairs(request: CardSaveRequest, cleaned: dict[str, dict], build_card, runtime) -> list:
        pairs: list[tuple[str, str]] = [
            ("vcs_providers_config", json.dumps(cleaned, ensure_ascii=False))
        ]
        for provider, token in (request.bot_tokens or {}).items():
            token = str(token or "").strip()
            if not token:
                continue
            if provider not in cleaned:
                raise CardValidationError([f"bot_tokens 含不在册平台: {provider}"])
            card = build_card(provider, cleaned[provider])
            pairs.append((card.global_bot_meta_key or f"global_bot_{provider}_token", token))
        for provider, token in (request.webhook_tokens or {}).items():
            token = str(token or "").strip()
            if not token:
                continue
            if provider not in cleaned:
                raise CardValidationError([f"webhook_tokens 含不在册平台: {provider}"])
            pairs.append((f"{provider}_webhook_token", token))
        return pairs

    @staticmethod
    def _merge_overrides(cleaned, request, runtime, meta_pairs) -> dict[str, dict]:
        """覆盖层：配置文件派生行的管理面改动，最后叠在文件/meta/表行之上。"""
        from src.domains.mgmt.user.db import get_meta_value
        from src.domains.runtime.public import (
            compute_card_overrides,
            platform_card_overrides_meta_key,
        )

        key = platform_card_overrides_meta_key()
        try:
            current = get_meta_value(key) or ""
        except Exception:
            current = ""
        overrides = compute_card_overrides(cleaned, current)
        meta_pairs.append((key, json.dumps(overrides, ensure_ascii=False)))
        return overrides

    def _write_rows(self, cleaned: dict[str, dict], invalidate) -> tuple[dict[str, str], str]:
        """手建实例行。落行失败不阻断卡保存，但必须回给管理面（不静默）。"""
        from src.kernel.config import log

        try:
            actions = self._rows.upsert_cards(cleaned)
            invalidate()          # 行变了 → 卡按表重建
            return actions, ""
        except Exception as exc:  # noqa: BLE001
            log.warning(f"vcs-platforms: 平台行落库失败（卡已保存）: {exc}")
            return {}, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _audit(request: CardSaveRequest, cleaned: dict, rows: dict) -> None:
        from src.domains.mgmt.auth.audit import log_audit

        log_audit(
            "admin_vcs_platforms_saved",
            operator_id=request.operator_id,
            detail={"providers": sorted(cleaned), "rows": rows},
        )