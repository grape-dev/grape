"""分类收敛（runtime 侧）：skill 挂载重挂 + 老分类废止（迁移 202611181304）。

一级分类收敛为 IT研发/运营域/其他（用户决策，Issue #3202 系）。收敛拆成两条
域内迁移，各自只写属主表（QA A11：原实现把 runtime 表的 UPDATE 混在无
domains= 声明的 mgmt 迁移里，三库拓扑下 runtime 库上的收敛被静默跳过）：

- mgmt 侧（202611180004，src/domains/mgmt/migrations/category_collapse_dict.py）：
  停用 grape_t_category_dict 的被删一级及其二级 + scene_market.cat_l1_id 重挂 1010；
- runtime 侧（本模块）：skill.cat_l1_id 重挂 1010 + 老 skill.category_id 清零
  （skill_category 表归 mgmt，其停用在 mgmt 侧 202611180004 一并执行）。

skill 归 runtime、skill_category 归 mgmt（table_ownership），SQL 各住属主域
migrations 下（跨域守卫 P0-2：api 层直写他域表 = 新增跨域引用）。
domains=("runtime",)：三库模式由 runtime 进程执行；单库 / 测试不筛域、全量执行。
幂等：纯 UPDATE，可重复执行。失败即 raise（不盖章，下次启动重试）。
"""
from __future__ import annotations

#: 种子显式 id（一级 = 1000+order）：it-rd=1001 / operations=1002 / other=1010。
#: 与 mgmt 侧 KEEP_L1_SLUGS 一一对应（那边按 slug 停用，这边按 id 重挂）。
KEEP_L1_IDS = (1001, 1002, 1010)


def collapse_skill_categories() -> None:
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        marks = ",".join(str(i) for i in KEEP_L1_IDS)
        # 指向被删一级的 skill 行归入 1010（其他）；指向保留一级 / 0 的不动。
        # 列由基线迁移 202611180003（runtime 域）保证存在。
        c.execute(
            f"UPDATE skill SET cat_l1_id=1010 "
            f"WHERE cat_l1_id<>0 AND cat_l1_id NOT IN ({marks})")
        # 老分类废止：skill.category_id 清零。
        # （skill_category 表归 mgmt，其停用由 mgmt 侧 202611180004 执行。）
        c.execute("UPDATE skill SET category_id=0 WHERE category_id<>0")
        db.commit()


MIGRATION = {
    "version": "202611181304",
    "name": "category-collapse-three-domains-runtime",
    "kind": "safe",
    "fn": collapse_skill_categories,
    "domains": ("runtime",),
}
