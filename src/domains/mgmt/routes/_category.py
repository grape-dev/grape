"""场景/技能写路径共用的分类校验与目标值计算（Issue #2761）。

统一口径，避免 scene / skill 两份实现漂移：
- 校验一级/二级存在、层级正确、二级归属所给一级（只给二级时反推一级）；
- 0 是「不修改」哨兵值，显式清空由 `clear` 开关承担。
"""
from __future__ import annotations

from fastapi import HTTPException


def normalize_category_pair(cat_l1_id: int, cat_l2_id: int) -> tuple[int, int]:
    """校验并归一化 (一级, 二级) 分类 ID。

    一级/二级必须存在且层级正确（一级 level=1、二级 level=2）；二级必须属于
    所给一级（只给二级时由 parent_id 反推一级）。不合法时抛 HTTP 400。
    """
    from src.domains.mgmt.user.db import get_category_dict_entry

    l1_id = int(cat_l1_id or 0)
    l2_id = int(cat_l2_id or 0)
    l1 = get_category_dict_entry(l1_id) if l1_id else None
    l2 = get_category_dict_entry(l2_id) if l2_id else None
    if l1_id and (not l1 or int(l1.get("level") or 1) != 1):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"一级分类 ID {l1_id} 不存在"})
    if l2_id and (not l2 or int(l2.get("level") or 1) != 2):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"二级分类 ID {l2_id} 不存在"})
    if l2:
        parent_id = int(l2.get("parent_id") or 0)
        if l1 and parent_id != l1_id:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"二级分类 {l2_id} 不属于一级分类 {l1_id}"})
        if not l1:
            l1_id = parent_id
            l1 = get_category_dict_entry(l1_id)
            if not l1:
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"二级分类 {l2_id} 的父级不存在"})
    return l1_id, l2_id


def prepare_category_write(cat_l1_id: int, cat_l2_id: int,
                           cur_l1_id: int = 0, cur_l2_id: int = 0,
                           clear: bool = False) -> tuple[int, int]:
    """计算写库目标 (一级, 二级)。

    - `clear=True`：显式清空，返回 (0, 0)；
    - 一级/二级都未提供：沿用旧值 (cur_l1, cur_l2)；
    - 提供分类：先校验归一化；只给一级时二级归零（避免跨父级脏数据），
      只给二级时由 parent_id 反推一级。
    """
    if clear:
        return 0, 0
    l1 = int(cat_l1_id or 0)
    l2 = int(cat_l2_id or 0)
    if not l1 and not l2:
        return int(cur_l1_id or 0), int(cur_l2_id or 0)
    return normalize_category_pair(l1, l2)