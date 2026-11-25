"""技能分类补种「内容生成」（mgmt 侧）：Issue #3798（迁移 202611240012）。

背景（grapedev.cn 2026-11-22 线上实测）：官方技能同步（`runtime/storage/skill_sync`，
worker 每 30 分钟一轮）按 `skill-description.json` 的 `sub_scene` 解析
`skill.category_id`，映射源是 mgmt 域的 `skill_category`（`WHERE is_active=1`）。
种子只有 15 条（`src/api/db_setup.py::_init_skill_category_data`），**没有
`content-generation`**，于是 `nine-grid-storyboard` / `ai-blog-generation` 一类内容
生成技能每轮都判未命中、`category_id` 恒 0，且原实现逐技能刷一条 WARNING。

本迁移幂等补这一行（`内容生成` / `content-generation` / order_num 16），并显式重申
`is_active=1`——同步侧只认启用行（`skill_category_active_map()`），行在而停用等于
没种（Spec 决策：**补种子**而非「归 other」，保留分类语义、可逆：停用该行即回退）。

与既有分类迁移的关系（同一张表，口径不同，勿混）：

- 202611210002（`category_collapse_dict`）/ 202611230001（`skill_category_cutover_dict`）
  把**老分类体系整体停用**（统一字典 `grape_t_category_dict` 收口），runtime 侧
  202611230002 把老列 `skill.category_id` 清零——本迁移**不重开**那批老分类行，
  只新增并启用 `content-generation` 这一条新 slug；
- 分类的正式口径仍是 `cat_l1_id` / `cat_l2_id`（`/api/categories/tree`）；本行只服务
  `sub_scene=content-generation` 这类技能的老口径 `category_id` 解析。

表归属 mgmt（`configs/table_ownership.json`），SQL 住本域 migrations 下（跨域守卫
P0-2）；注册由 `src/api/db_setup.py` 引导器做（`register_migration`，字面量登记
version/name/kind，供 `tests/api/test_migration_track_guard.py` 解析）。
domains=("mgmt",)：三库模式由 mgmt 进程执行；单库 / 测试不筛域、全量执行。
幂等：INSERT OR IGNORE + 条件 UPDATE，可重复执行；失败 raise 不盖章（下次启动重试）。
"""
from __future__ import annotations

#: 补种的分类行（tests 直接 import 断言，避免字面量多源漂移）。
SEED_NAME = "内容生成"
SEED_SLUG = "content-generation"
SEED_ICON = "✍️"
SEED_ORDER_NUM = 16


def seed_content_generation_category(conn=None) -> None:
    """幂等补种 `content-generation` 分类行并重申启用（可重复执行）。"""
    from src.domains.mgmt.migrations_ddl import INSERT_IGNORE_SKILL_CATEGORY
    from src.kernel.config import log
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    with db_lock:
        c = conn.cursor()
        # 行级幂等：已有 slug 不重插（slug 唯一）；已在则不冲突
        c.execute(INSERT_IGNORE_SKILL_CATEGORY,
                  (SEED_NAME, SEED_SLUG, SEED_ICON, SEED_ORDER_NUM))
        # 启用重申：老分类被 202611210002 / 202611230001 整表停用过，补种行必须启用
        # 才进得了同步侧的映射（行在而 is_active=0 = 未命中）。
        cur = c.execute(
            "UPDATE skill_category SET is_active=1 WHERE slug=? AND is_active<>1",
            (SEED_SLUG,))
        conn.commit()
        log.info(f"202611240012 skill_category 补种 {SEED_SLUG} 完成"
                 f"（本次启用 {getattr(cur, 'rowcount', 0)} 行）")


def verify_content_generation_category(conn=None) -> None:
    """后置校验（Issue #3798）：行在位且启用，才盖章。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    c = conn.cursor()
    row = c.execute("SELECT id, is_active FROM skill_category WHERE slug=?",
                    (SEED_SLUG,)).fetchone()
    problems: list = []
    if row is None:
        problems.append(f"skill_category 缺 slug={SEED_SLUG} 的种子行")
    elif int(row[1] or 0) != 1:
        problems.append(f"slug={SEED_SLUG} 行未启用（is_active={row[1]}），"
                        "同步侧仍会判未命中")
    if problems:
        raise RuntimeError(
            "202611240012 post_check 未收敛: " + "; ".join(problems))
    log.info(f"202611240012 post_check 通过：{SEED_SLUG} 行在位且启用")


MIGRATION = {
    "version": "202611240012",
    "name": "skill-category-seed-content-generation",
    "kind": "backfill",
    "fn": seed_content_generation_category,
    "domains": ("mgmt",),
    "post_check": verify_content_generation_category,
}