"""统一分类字典（category_dict）slug 解析工具（Issue #2761）。

场景同步与技能同步共用同一套解析口径，避免两份实现漂移：
- 支持显式 `category_l1` / `category_l2`，或单个 `category`（一级/二级/"一级/二级"）；
- 未命中的显式 slug 会告警（便于发现仓库配置错误），但保留已命中侧，返回 (0, 0) 时
  由调用方决定保留库内旧值，绝不写 0 覆盖既有分类。
"""
from __future__ import annotations

import logging
from typing import Any


def sync_category_target(new_l1: int, new_l2: int,
                          old_l1: int, old_l2: int) -> tuple[int, int]:
    """同步写入口径：一级未变且来源未给出二级时，保留库内旧二级。

    同步来源可能只配置一级（`new_l2=0`）；若该一级与库内相同，直接写 0 会清空
    既有合法二级，与「未命中不覆盖旧值」冲突。仅在一级实际变更时重置二级。
    """
    n1, n2 = int(new_l1 or 0), int(new_l2 or 0)
    o1, o2 = int(old_l1 or 0), int(old_l2 or 0)
    if not n1:
        return o1, o2
    if n1 == o1 and not n2:
        return n1, o2
    return n1, n2


def lookup_slug(dict_map: dict[str, dict[str, Any]], slug: str) -> tuple[int, int]:
    """把 slug 解析成 (l1_id, l2_id)；未命中返回 (0, 0)。"""
    item = (dict_map or {}).get((slug or "").strip())
    if not item:
        return 0, 0
    if int(item.get("level") or 1) == 1:
        return int(item["id"]), 0
    return int(item.get("parent_id") or 0), int(item["id"])


def resolve_category_slugs(info: dict, dict_map: dict[str, dict[str, Any]],
                           logger: logging.Logger | None = None,
                           label: str = "category") -> tuple[int, int]:
    """按 info 中的分类 slug 解析 (cat_l1_id, cat_l2_id)。"""
    log = logger or logging.getLogger(__name__)
    if not dict_map:
        return 0, 0

    l1 = str(info.get("category_l1") or "").strip()
    l2 = str(info.get("category_l2") or "").strip()
    l1_id = l2_id = 0
    if l1:
        a, b = lookup_slug(dict_map, l1)
        if a:
            l1_id, l2_id = a, b
        else:
            log.warning(f"{label}: category_l1 slug '{l1}' not found in category_dict, keep existing")
    if l2:
        a, b = lookup_slug(dict_map, l2)
        if a:
            l1_id, l2_id = a, b
        else:
            log.warning(f"{label}: category_l2 slug '{l2}' not found in category_dict, keep existing")
    if l1_id or l2_id:
        return l1_id, l2_id

    raw = str(info.get("category") or "").strip()
    if not raw:
        return 0, 0
    # path 形态（"l1-slug/l2-slug"）：逐段取，一级段给 cat_l1、二级段补 cat_l2。
    # 原实现命中第一段就返回 → 二级整段丢失（只剩 cat_l1_id）。
    l1_id = l2_id = 0
    for part in [p.strip() for p in raw.replace("\\", "/").split("/") if p.strip()]:
        a, b = lookup_slug(dict_map, part)
        if not a:
            continue
        if b:
            l1_id, l2_id = a, b
        elif not l1_id:
            l1_id = a
    if l1_id or l2_id:
        return l1_id, l2_id
    log.warning(f"{label}: category slug '{raw}' not found in category_dict, keep existing category")
    return 0, 0
