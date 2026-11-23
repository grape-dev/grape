"""技能分类切换（mgmt 侧）：统一字典补种新二级 + 老分类停用重申（迁移 202611230001）。

背景（grapedev.cn 2026-11-22 线上实测）：统一字典 grape_t_category_dict 上线后
挂靠侧缺失——二级「grape平台应用」不在 _CATEGORY_DICT_SEED 里（线上 tree 也没有），
grape 平台自身的技能无处可挂。本迁移补种该行（slug it-rd-grape-platform /
id 100109，id 由 category_dict_seed_id 规则复算，与种子单一真源一致：种子清单
2026-11-22 已在 mgmt db.py 增补同 slug 条目，本迁移对存量库重跑 seed 兜底）。

与上游 202611210002（category-collapse-three-domains-mgmt）的关系：那条已把
skill_category 全量停用；这里幂等重申一次（老 skill_category 表在部分环境可能
被重新激活，停用是老筛选行消失的唯一开关）。grape_t_category_dict /
skill_category 归 mgmt（configs/table_ownership.json），SQL 住本域 migrations 下
（跨域守卫 P0-2）；注册由 src/api/db_setup.py 引导器做（register_migration）。

domains=("mgmt",)：单库 / 测试不筛域、全量执行。幂等：INSERT OR IGNORE +
条件 UPDATE，可重复执行。失败 raise 不盖章（下次启动重试）。
"""
from __future__ import annotations

#: 新增二级的钉子（tests 断言用）：slug 与 parent slug。
NEW_L2_SLUG = "it-rd-grape-platform"
NEW_L2_PARENT_SLUG = "it-rd"


def seed_dict_and_retire_legacy(conn=None) -> None:
    """重跑分类字典种子（补 grape平台应用）+ 老分类停用重申（幂等）。"""
    from src.domains.mgmt.user.db import seed_category_dict
    from src.kernel.config import log
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    # seed_category_dict 内部走 mgmt 连接，幂等 INSERT OR IGNORE——已盖章
    # 202611171845/846 的存量库不会自动补新种子行，这里重跑补上。
    seed_category_dict()
    with db_lock:
        c = conn.cursor()
        cur = c.execute("UPDATE skill_category SET is_active=0 WHERE is_active=1")
        conn.commit()
        log.info(f"202611230001 skill_category 停用重申 "
                 f"{getattr(cur, 'rowcount', 0)} 行（is_active=0，可逆）")


def verify_dict_cutover(conn=None) -> None:
    """后置校验（Issue #3648）：种子行在位 + 老分类全停用，才盖章。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    c = conn.cursor()
    seed_hit = int(c.execute(
        "SELECT COUNT(*) FROM grape_t_category_dict "
        "WHERE slug=? AND parent_id=(SELECT id FROM grape_t_category_dict WHERE slug=?)",
        (NEW_L2_SLUG, NEW_L2_PARENT_SLUG)).fetchone()[0] or 0)
    active_legacy = int(c.execute(
        "SELECT COUNT(*) FROM skill_category WHERE is_active=1").fetchone()[0] or 0)
    problems: list = []
    if not seed_hit:
        problems.append("grape_t_category_dict 缺二级「grape平台应用」种子行"
                        f"（slug={NEW_L2_SLUG}）")
    if active_legacy:
        problems.append(f"老 skill_category 仍有 {active_legacy} 行 is_active=1")
    if problems:
        raise RuntimeError(
            "202611230001 post_check 未收敛: " + "; ".join(problems))
    log.info("202611230001 post_check 通过：种子行在位、老分类全停用")


MIGRATION = {
    "version": "202611230001",
    "name": "skill-category-cutover-dict-seed",
    "kind": "backfill",
    "fn": seed_dict_and_retire_legacy,
    "domains": ("mgmt",),
    "post_check": verify_dict_cutover,
}
