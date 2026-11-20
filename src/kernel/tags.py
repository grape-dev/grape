"""标签（tags）解析与归一化工具（Issue #2761）。

场景市场 `scene_market.tags` 历史值是逗号串，技能侧 `skill.tags` 为新增列；
统一存储为 JSON 数组 `[{"name": str, "source": "manual"|"auto"}]`，读路径兼容旧逗号串。

规则：
- 最多 3 个标签（服务端强校验；读路径按上限截断）；
- 大小写不敏感去重；
- 同一标签同时存在 manual / auto 时 manual 覆盖 auto。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

MAX_TAGS = 3
#: 单个标签名的最大字符数（技能侧强校验；场景侧保持原契约不变）
MAX_TAG_LEN = 12
VALID_SOURCES = ("manual", "auto")


def _entry(item: Any) -> tuple[str, str] | None:
    """把单个标签项解析成 (name, source)。无效项返回 None。"""
    source = "manual"
    if isinstance(item, dict):
        name = str(item.get("name") or "").strip()
        src = str(item.get("source") or "").strip().lower()
        if src in VALID_SOURCES:
            source = src
    else:
        name = str(item or "").strip()
    if not name:
        return None
    return name, source


def _normalize(items: Iterable[Any], limit: int | None = None) -> list[dict[str, str]]:
    seen: dict[str, int] = {}
    out: list[dict[str, str]] = []
    for item in items or []:
        parsed = _entry(item)
        if parsed is None:
            continue
        name, source = parsed
        key = name.lower()
        if key in seen:
            idx = seen[key]
            if source == "manual" and out[idx]["source"] != "manual":
                out[idx]["source"] = "manual"
            continue
        seen[key] = len(out)
        out.append({"name": name, "source": source})
    if limit is not None and limit >= 0:
        return out[:limit]
    return out


def parse_tags(raw: Any, limit: int | None = None) -> list[dict[str, str]]:
    """把库内/入参的标签值解析成归一化列表。

    支持：JSON 数组字符串、Python list、单个 dict、逗号/中文逗号分隔串。
    `limit=None` 表示不截断（用于写侧超限检测）；读路径传 MAX_TAGS。
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        return _normalize([raw], limit)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                # 以 '[' 开头却非法 JSON：不再退化为逗号切分（会产出 "[foo" 这类脏标签）
                return []
            if isinstance(data, list):
                return _normalize(data, limit)
            return []
        text = text.replace("，", ",")
        parts = [p.strip() for p in text.split(",")]
        return _normalize(parts, limit)
    if isinstance(raw, (list, tuple, set)):
        return _normalize(raw, limit)
    return []


def prepare_tags(raw: Any, limit: int = MAX_TAGS) -> tuple[list[dict[str, str]], list[str]]:
    """写侧归一化：返回 (保留的标签, 超限标签名)。超限非空时调用方应返回 400。"""
    normalized = parse_tags(raw, limit=None)
    kept = normalized[:limit]
    overflow = [t["name"] for t in normalized[limit:]]
    return kept, overflow


def oversized_tags(items: Any, max_len: int = MAX_TAG_LEN) -> list[str]:
    """返回超过 max_len 字符的标签名（保持输入顺序，供调用方 400 点名）。"""
    out: list[str] = []
    for item in items or []:
        parsed = _entry(item)
        if parsed is None:
            continue
        name = parsed[0]
        if len(name) > max_len:
            out.append(name)
    return out


def encode_tags(items: Any, limit: int | None = None) -> str:
    """归一化后编码为 JSON 字符串（空列表返回空串，保持列默认语义）。"""
    normalized = parse_tags(items, limit=limit)
    if not normalized:
        return ""
    return json.dumps(normalized, ensure_ascii=False)


def merge_tags(existing: Any, incoming: Any, limit: int = MAX_TAGS) -> list[dict[str, str]]:
    """合并标签：保留 existing 中的 manual 标签，用 incoming 补齐（同名 manual 优先）。

    同步层（scene_sync / skill_sync）每次都会带一组 auto 标签；此处保证人工标签
    不被整体重写抹掉，且 incoming 中与 manual 同名的项不会降级 auto。
    """
    merged = [t for t in parse_tags(existing, limit=None) if t["source"] == "manual"]
    seen = {t["name"].lower() for t in merged}
    for item in parse_tags(incoming, limit=None):
        key = item["name"].lower()
        if key in seen:
            continue
        merged.append({"name": item["name"], "source": item["source"]})
        seen.add(key)
        if len(merged) >= limit:
            break
    return merged[:limit]


#: 标签推荐候选（场景/技能共用；无关键词命中时不补泛化标签）
COMMON_TAG_CANDIDATES = (
    "AI", "自动化", "测试", "部署", "代码评审", "文档", "运维", "开发",
    "安全", "监控", "数据", "协作",
)


def suggest_tags(name: str = "", description: str = "",
                 candidates: Iterable[str] | None = None,
                 limit: int = MAX_TAGS) -> list[str]:
    """规则型标签推荐：仅返回名称/描述中真实命中的候选（最多 limit 个）。"""
    base = list(candidates or COMMON_TAG_CANDIDATES)
    text = f"{name} {description}".lower()
    hits: list[str] = []
    for tag in base:
        if tag.lower() in text and tag not in hits:
            hits.append(tag)
            if len(hits) >= limit:
                break
    return hits
