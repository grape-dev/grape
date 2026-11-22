"""技能分类切换（runtime 侧）：官方技能挂靠统一字典 l1/l2（迁移 202611230002）。

背景（grapedev.cn 2026-11-22 线上实测）：全量技能 cat_l1_id=0——官方技能到
统一字典（grape_t_category_dict）的挂靠从未落库，技能市场点任何新分类必然
空列表。上游 202611210003 只做了「被删一级重挂 1010 + 老 category_id 清零」，
没有把官方技能挂到保留一级的各二级上——本迁移补齐这一步。

先例（QA A8/A11，2026-11-22 两轴审查）：迁移 SQL 必须住属主域 migrations 下
（skill 归 runtime，configs/table_ownership.json）；src/api/db_setup.py 直写
runtime/mgmt 表 = 新增跨域引用，tests/arch/test_foreign_table_refs.py 必红。
注册仍由 src/api/db_setup.py 引导器做（register_migration(**MIGRATION)）。

失败语义（两轴审查 Spec#2/#3）：
  1. 探测前置到任何写之前：官方技能数为 0（全新库首启、skill sync 未跑）
     时一字节不写、raise 不盖章，等 sync 拉到技能后下次启动整体收敛；
  2. 挂靠未全命中（skill 名单与映射表漂移）→ raise 不盖章，逼着补
     _OFFICIAL_SKILL_L2_MAP 再收敛——不允许「部分挂靠却盖章」让漏网技能
     永久 cat_l1_id=0；
  3. post_check 断言收敛结果（官方技能全挂靠 + category_id 清零）才盖章。

domains=("runtime",)：三库模式由 runtime 进程执行（进程库即 runtime 库）；
单库 / 测试不筛域、全量执行。幂等：纯 UPDATE，可重复执行。
"""
from __future__ import annotations

#: 官方技能 → 二级分类 slug 映射（74 条全量）。
#: 归属口径 = 用户逐条拍板的《官方技能分类清单》（2026-11-22）：组织仓库 AI 分析
#: → 测试与质量；分布：工程29 / AI应用14 / 基础设施9 / 内容商品7 / grape平台6 /
#: 运维观测4 / 个人生活3 / 安全1 / 测试质量1。
#: 单一真源在本模块（tests/api/test_skill_category_cutover.py 直接 import 断言，
#: 不再从 db_setup.py 正则提取）。
OFFICIAL_SKILL_L2_MAP = [
    ("_template", "it-rd-ai-app"),
    ("ai-blog-generation", "ops-content-goods"),
    ("auto-review-pr", "it-rd-engineering"),
    ("blog-issue", "ops-content-goods"),
    ("blog-manager", "ops-content-goods"),
    ("blog-publish", "ops-content-goods"),
    ("blog-vod", "ops-content-goods"),
    ("build-debug", "it-rd-engineering"),
    ("changelog-gen", "it-rd-ops-observability"),
    ("chat", "it-rd-grape-platform"),
    ("chat-cli", "it-rd-engineering"),
    ("cloud-deploy", "it-rd-infra"),
    ("coding-cli", "it-rd-engineering"),
    ("coding-mattpocock", "it-rd-engineering"),
    ("common-skill-creator", "it-rd-ai-app"),
    ("common-skill-tester", "it-rd-ai-app"),
    ("deploy-cli", "it-rd-infra"),
    ("deploy-guard", "it-rd-security-compliance"),
    ("devbridge-deploy", "it-rd-infra"),
    ("domain-coding", "it-rd-engineering"),
    ("domain-deploy", "it-rd-infra"),
    ("domain-issue", "it-rd-ai-app"),
    ("domain-qa", "it-rd-ai-app"),
    ("domain-vod", "it-rd-ai-app"),
    ("event-dialogue", "it-rd-engineering"),
    ("grape-skill-generator", "it-rd-grape-platform"),
    ("grape-skill-issue", "it-rd-grape-platform"),
    ("grape-skill-uploader", "it-rd-grape-platform"),
    ("grape-skill-validator", "it-rd-grape-platform"),
    ("grape-vod-cli", "it-rd-engineering"),
    ("huawei-cloud", "it-rd-ai-app"),
    ("huawei-cloud-devbridge-deploy", "it-rd-infra"),
    ("iac-coding", "it-rd-infra"),
    ("iac-issue", "it-rd-engineering"),
    ("iac-qa", "it-rd-infra"),
    ("iac-sre", "it-rd-infra"),
    ("iac-vod", "it-rd-infra"),
    ("im-notify", "it-rd-grape-platform"),
    ("impact-analysis", "it-rd-engineering"),
    ("implementation", "it-rd-engineering"),
    ("incident-triage", "it-rd-ops-observability"),
    ("issue", "it-rd-engineering"),
    ("issue-advance", "it-rd-engineering"),
    ("issue-cli", "it-rd-engineering"),
    ("issue-mattpocock", "it-rd-engineering"),
    ("issue-multitask", "it-rd-engineering"),
    ("narrated-explainer-video", "other-personal-life"),
    ("nine-grid-storyboard", "other-personal-life"),
    ("novel-to-manga", "other-personal-life"),
    ("org-repo-analyzer", "it-rd-test-quality"),
    ("os-disk-list", "it-rd-ops-observability"),
    ("plugin-creator", "it-rd-engineering"),
    ("plugin-publish", "it-rd-engineering"),
    ("plugin-tester", "it-rd-engineering"),
    ("pr", "it-rd-engineering"),
    ("qa", "it-rd-engineering"),
    ("qa-cli", "it-rd-engineering"),
    ("qa-mattpocock", "it-rd-engineering"),
    ("qwen-image-generation", "ops-content-goods"),
    ("repo-onboarding", "it-rd-engineering"),
    ("rest-api-skill-creator", "it-rd-ai-app"),
    ("rest-api-skill-tester", "it-rd-ai-app"),
    ("runtime-install", "it-rd-ops-observability"),
    ("skill-creator", "it-rd-ai-app"),
    ("skill-issue", "it-rd-ai-app"),
    ("skill-pr", "it-rd-ai-app"),
    ("skill-publish", "it-rd-ai-app"),
    ("skill-tester", "it-rd-ai-app"),
    ("vod", "it-rd-engineering"),
    ("vod-advance", "it-rd-engineering"),
    ("vod-cli", "it-rd-engineering"),
    ("vod-mattpocock", "it-rd-engineering"),
    ("vod-strict", "it-rd-engineering"),
    ("wan-video-generation", "ops-content-goods"),
]

#: 挂靠分布钉子（用户拍板口径；tests 断言用，勿随手改）。
EXPECTED_DISTRIBUTION = {
    "it-rd-engineering": 29,
    "it-rd-ai-app": 14,
    "it-rd-infra": 9,
    "ops-content-goods": 7,
    "it-rd-grape-platform": 6,
    "it-rd-ops-observability": 4,
    "other-personal-life": 3,
    "it-rd-security-compliance": 1,
    "it-rd-test-quality": 1,
}


def _resolve_ids() -> tuple[dict, dict]:
    """l2 slug → (显式 id, 一级 id)：与种子同一套规则离线复算（不查库）。

    种子数据/复算函数的单一真源在 kernel（src/kernel/category.py 的
    CATEGORY_DICT_SEED / category_dict_seed_id）——import 的是**纯数据与
    纯函数**，不触碰 mgmt 的表（跨域 SQL 才违反 P0-2 棘轮）；跨域共享走
    kernel，不直接 import mgmt 模块（Issue #3168 QA Standards#2，先例
    KEEP_L1_IDS）。mgmt / runtime 进程各自独立复算出同一结果。
    """
    from src.kernel.category import (
        CATEGORY_DICT_SEED,
        category_dict_seed_id,
    )

    id_by_slug: dict = {}
    l1_of_l2: dict = {}
    for idx, row in enumerate(CATEGORY_DICT_SEED):
        id_by_slug[row[3]] = category_dict_seed_id(CATEGORY_DICT_SEED, idx)
        if int(row[1]) == 2:
            l1_of_l2[row[3]] = id_by_slug.get(row[0], 0)
    missing = sorted({slug for _, slug in OFFICIAL_SKILL_L2_MAP} - set(id_by_slug))
    if missing:
        raise RuntimeError(
            f"202611230002 挂靠失败：种子字典缺二级 slug {missing}"
            "——先核对 kernel CATEGORY_DICT_SEED 再重试")
    return id_by_slug, l1_of_l2


def cutover_official_skills(conn=None) -> None:
    """官方技能挂 l1/l2 + 老 category_id 清零（幂等；失败 raise 不盖章）。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    # ── 前置探测（先于一切写操作）：官方技能必须已入库 ──
    # 全新库首启时本迁移先于首轮 skill sync 执行；此刻一字节不写（Spec#3 半完成态）。
    with db_lock:
        c = conn.cursor()
        official_n = int(c.execute(
            "SELECT COUNT(*) FROM skill WHERE scope='official'").fetchone()[0])
        if official_n == 0:
            raise RuntimeError(
                "202611230002 官方技能尚未入库（skill sync 未跑或远程为空）："
                "不做任何写操作、不盖章，等 sync 拉到技能后下次启动重试")

    id_by_slug, l1_of_l2 = _resolve_ids()
    mapped = 0
    missed: list = []
    with db_lock:
        c = conn.cursor()
        for skill_name, l2_slug in OFFICIAL_SKILL_L2_MAP:
            cur = c.execute(
                "UPDATE skill SET cat_l1_id=?, cat_l2_id=? "
                "WHERE skill_name=? AND scope='official'",
                (int(l1_of_l2.get(l2_slug, 0)), int(id_by_slug[l2_slug]), skill_name))
            n = int(getattr(cur, "rowcount", 0) or 0)
            mapped += n
            if not n:
                missed.append(skill_name)
        # 老 category_id 清零与上游 202611210003 幂等重申（存量库可能已清）。
        c.execute("UPDATE skill SET category_id=0 WHERE category_id<>0")
        conn.commit()
    if missed:
        raise RuntimeError(
            f"202611230002 官方技能挂靠未全命中（{len(OFFICIAL_SKILL_L2_MAP)} 条"
            f"命中 {mapped}，未命中：{missed}）——skill 名单与映射表已漂移，"
            "请把缺失技能补进 OFFICIAL_SKILL_L2_MAP 后重试（幂等，已挂靠行保持不变）")
    log.info(f"202611230002 官方技能挂靠完成：{mapped}/{len(OFFICIAL_SKILL_L2_MAP)}")


def verify_official_skill_cutover(conn=None) -> None:
    """后置校验（Issue #3648 / 两轴审查 Standards#3）：收敛可判定才盖章。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    c = conn.cursor()
    unmapped = c.execute(
        "SELECT skill_name FROM skill WHERE scope='official' AND cat_l1_id=0"
    ).fetchall()
    stale = int(c.execute(
        "SELECT COUNT(*) FROM skill WHERE category_id<>0").fetchone()[0] or 0)
    problems: list = []
    if unmapped:
        problems.append(f"官方技能仍有 {len(unmapped)} 个 cat_l1_id=0: "
                        f"{[r[0] for r in unmapped][:5]}…")
    if stale:
        problems.append(f"仍有 {stale} 行 skill.category_id<>0（老分类未清零）")
    if problems:
        raise RuntimeError(
            "202611230002 post_check 未收敛: " + "; ".join(problems))
    log.info("202611230002 post_check 通过：官方技能全挂靠、老 category_id 清零")


MIGRATION = {
    "version": "202611230002",
    "name": "skill-category-cutover-official-map",
    "kind": "backfill",
    "fn": cutover_official_skills,
    "domains": ("runtime",),
    "post_check": verify_official_skill_cutover,
}
