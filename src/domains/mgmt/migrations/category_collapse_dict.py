"""分类收敛（mgmt 侧）：字典停用 + scene_market 重挂（迁移 202611210002）。

一级分类收敛为 IT研发/运营域/其他（用户决策）：grape_t_category_dict 停用
（is_active=0）除 it-rd/operations/other 外的一级及其全部二级，scene_market
指向被删一级的行重挂 1010（其他）。skill 侧重挂由 runtime 迁移
202611210003（src/domains/runtime/migrations/skill_category_collapse.py）负责。

Issue #3648：dev 三库的 202611180004 已被旧合并迁移
`category-collapse-three-domains` 盖章（同号不同名），本迁移被永久跳过；
改号到未占用号 202611210002，并加 post_check（执行→校验→盖章）。

grape_t_category_dict / scene_market 归 mgmt（configs/table_ownership.json），
所以这条迁移连同 SQL 一起住在本域 migrations 下（跨域守卫 P0-2）；
注册仍由 src/api/db_setup.py 引导器做（register_migration），本模块只导出内容。
domains=("mgmt",)：单库 / 测试不筛域、全量执行。
幂等：纯 UPDATE，可重复执行。
"""
from __future__ import annotations

#: 收敛后允许活跃的一级显式 id（it-rd/operations/other）：单一真源在
#: src/kernel/category.py（runtime 侧重挂、api 层启动校验引用同一常量），
#: 各域禁止再自行定义。
from src.kernel.category import KEEP_L1_IDS

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
            # skill 表归 runtime，其重挂由 202611210003 在 runtime 域执行。
            try:
                c.execute(
                    f"UPDATE scene_market SET cat_l1_id=1010 "
                    f"WHERE cat_l1_id IN ({marks})", tuple(drop_ids))
            except Exception as exc:  # noqa: BLE001 - scene_market 列由基线迁移保证
                log.warning(f"202611210002 remap scene_market.cat_l1_id failed: {exc}")
        # 老分类废止：skill_category（老一级/二级分类表，归 mgmt）全部停用。
        c.execute("UPDATE skill_category SET is_active=0 WHERE is_active=1")
        db.commit()


def verify_category_collapse(conn=None) -> list:
    """后置校验（Issue #3648）：仅 1001/1002/1010 三个一级保持活跃。

    - 表不存在 / 一级活跃集合不是恰好这三条 → raise（执行器不盖章，下次重试）；
    - 返回实际活跃一级 id（升序），供日志/测试断言。
    可注入连接供测试使用。
    """
    if conn is None:
        from src.kernel.session_manager import db as conn
    c = conn.cursor()
    try:
        rows = c.execute(
            "SELECT id FROM grape_t_category_dict "
            "WHERE level=1 AND is_active=1").fetchall()
    except Exception as exc:  # noqa: BLE001 - 表缺失 = 目标未到位
        raise RuntimeError(
            f"grape_t_category_dict 一级活跃校验失败（表/列缺失）: {exc}") from exc
    ids = sorted(int(r[0] or 0) for r in rows or [])
    if ids != list(KEEP_L1_IDS):
        raise RuntimeError(
            f"分类收敛未到位：一级活跃 id={ids}，期望 {list(KEEP_L1_IDS)}"
            "（不盖章，下次启动重试）")
    return ids


MIGRATION = {
    "version": "202611210002",
    "name": "category-collapse-three-domains-mgmt",
    "kind": "safe",
    "fn": collapse_category_dict,
    "domains": ("mgmt",),
    "post_check": verify_category_collapse,
}
