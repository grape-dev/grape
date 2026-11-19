"""分类收敛（mgmt 侧）：字典停用 + scene_market 重挂（迁移 202611180004）。

一级分类收敛为 IT研发/运营域/其他（用户决策）：grape_t_category_dict 停用
（is_active=0）除 it-rd/operations/other 外的一级及其全部二级，scene_market
指向被删一级的行重挂 1010（其他）。skill 侧重挂由 runtime 迁移
202611181304（src/domains/runtime/migrations/skill_category_collapse.py）负责。

grape_t_category_dict / scene_market 归 mgmt（configs/table_ownership.json），
所以这条迁移连同 SQL 一起住在本域 migrations 下（跨域守卫 P0-2）；
注册仍由 src/api/db_setup.py 引导器做（register_migration），本模块只导出内容。
domains=("mgmt",)：单库 / 测试不筛域、全量执行。
幂等：纯 UPDATE，可重复执行。
"""
from __future__ import annotations

KEEP_L1_SLUGS = ("it-rd", "operations", "other")


def collapse_category_dict() -> None:
    from src.kernel.session_manager import db, db_lock
    from src.kernel.config import log

    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT id, slug FROM grape_t_category_dict WHERE level=1").fetchall()
        drop_ids = []
        for rid, slug in rows or []:
            rid = int(rid or 0)
            if (slug or "") not in KEEP_L1_SLUGS:
                drop_ids.append(rid)
        if drop_ids:
            marks = ",".join("?" for _ in drop_ids)
            # 停用被删一级及其二级
            c.execute(
                f"UPDATE grape_t_category_dict SET is_active=0 "
                f"WHERE parent_id IN ({marks})", tuple(drop_ids))
            c.execute(
                f"UPDATE grape_t_category_dict SET is_active=0 "
                f"WHERE id IN ({marks})", tuple(drop_ids))
            # 引用重映射：挂在被删一级下的 scene_market 归入 1010（其他）。
            # skill 表归 runtime，其重挂由 202611181304 在 runtime 域执行。
            try:
                c.execute(
                    f"UPDATE scene_market SET cat_l1_id=1010 "
                    f"WHERE cat_l1_id IN ({marks})", tuple(drop_ids))
            except Exception as exc:  # noqa: BLE001 - scene_market 列由基线迁移保证
                log.warning(f"202611180004 remap scene_market.cat_l1_id failed: {exc}")
        # 老分类废止：skill_category（老一级/二级分类表，归 mgmt）全部停用。
        c.execute("UPDATE skill_category SET is_active=0 WHERE is_active=1")
        db.commit()


MIGRATION = {
    "version": "202611180004",
    "name": "category-collapse-three-domains-mgmt",
    "kind": "safe",
    "fn": collapse_category_dict,
    "domains": ("mgmt",),
}
