"""统一分类字典（category_dict）slug 解析工具（Issue #2761）。

场景同步与技能同步共用同一套解析口径，避免两份实现漂移：
- 支持显式 `category_l1` / `category_l2`，或单个 `category`（一级/二级/"一级/二级"）；
- 未命中的显式 slug 会告警（便于发现仓库配置错误），但保留已命中侧，返回 (0, 0) 时
  由调用方决定保留库内旧值，绝不写 0 覆盖既有分类。
"""
from __future__ import annotations

import logging
from typing import Any

#: 分类收敛契约（Issue #3202 / #3648）单一真源：收敛后仅保留的一级分类显式 id
#: （it-rd=1001 / operations=1002 / other=1010）。mgmt 域 grape_t_category_dict
#: 停用判定、runtime 域 skill 重挂与 api 层启动校验共用，禁止各处再裸写字面量。
KEEP_L1_IDS = (1001, 1002, 1010)

#: 统一分类字典种子（Issue #2761）：(parent_slug, level, name, slug, icon, order_num)，
#: parent_slug 为空表示一级域。种子数据与显式 id 复算规则属**纯数据/纯函数**，
#: 单一真源在本模块（kernel）——mgmt 域 seed_category_dict 落库、runtime 域
#: skill 挂靠迁移（skill_category_cutover）离线复算、api 层 db_setup 重映射
#: 共用同一套口径；跨域共享走 kernel，不直接 import 他域模块（先例 KEEP_L1_IDS）。
CATEGORY_DICT_SEED = [
    ("", 1, "IT研发", "it-rd", "💻", 1),
    ("it-rd", 2, "研发工程", "it-rd-engineering", "🧱", 1),
    ("it-rd", 2, "AI应用", "it-rd-ai-app", "🤖", 2),
    ("it-rd", 2, "基础设施", "it-rd-infra", "☁", 3),
    ("it-rd", 2, "测试与质量", "it-rd-test-quality", "🧪", 4),
    ("it-rd", 2, "移动开发", "it-rd-mobile-dev", "📱", 5),
    ("it-rd", 2, "运维与观测", "it-rd-ops-observability", "📊", 6),
    ("it-rd", 2, "安全与合规", "it-rd-security-compliance", "🛡", 7),
    ("it-rd", 2, "成本治理", "it-rd-cost-governance", "💰", 8),
    ("it-rd", 2, "grape平台应用", "it-rd-grape-platform", "🍇", 9),
    ("", 1, "运营域", "operations", "📈", 2),
    ("operations", 2, "用户与增长", "ops-user-growth", "🚀", 1),
    ("operations", 2, "内容与商品", "ops-content-goods", "🛒", 2),
    ("operations", 2, "渠道与营销", "ops-channel-marketing", "📣", 3),
    ("operations", 2, "客户体验", "ops-customer-experience", "😊", 4),
    ("operations", 2, "文档与知识管理", "ops-doc-knowledge", "📚", 5),
    ("operations", 2, "经营研究", "ops-business-research", "🔎", 6),
    ("operations", 2, "资源与成本", "ops-resource-cost", "🧮", 7),
    ("operations", 2, "流程运营", "ops-process-ops", "🔁", 8),
    ("", 1, "客户支持", "customer-support", "🎧", 3),
    ("customer-support", 2, "客户服务", "cs-customer-service", "💬", 1),
    ("customer-support", 2, "售前支持", "cs-pre-sales", "🤝", 2),
    ("customer-support", 2, "售后与技术支持", "cs-after-sales-tech", "🛠", 3),
    ("", 1, "人力资源", "hr", "👥", 4),
    ("hr", 2, "招聘与录用", "hr-recruiting", "🧑‍💼", 1),
    ("hr", 2, "培训与发展", "hr-training", "🎓", 2),
    ("hr", 2, "绩效与组织", "hr-performance-org", "🏢", 3),
    ("", 1, "财务与会计", "finance", "💰", 5),
    ("finance", 2, "费用与报销", "fin-expense-reimburse", "🧾", 1),
    ("finance", 2, "应收应付", "fin-receivable-payable", "📑", 2),
    ("finance", 2, "财务分析与合规", "fin-analysis-compliance", "📉", 3),
    ("", 1, "供应链与采购", "supply-chain", "🚚", 6),
    ("supply-chain", 2, "采购管理", "scm-procurement", "📦", 1),
    ("supply-chain", 2, "仓储物流", "scm-warehousing-logistics", "🏬", 2),
    ("supply-chain", 2, "供应商协同", "scm-supplier-collab", "🤝", 3),
    ("", 1, "生产制造", "manufacturing", "🏭", 7),
    ("manufacturing", 2, "生产计划", "mfg-production-planning", "🗓", 1),
    ("manufacturing", 2, "质量控制", "mfg-quality-control", "🔬", 2),
    ("manufacturing", 2, "生产制造", "mfg-production", "⚙", 3),
    ("", 1, "销售域", "sales", "🤝", 8),
    ("sales", 2, "营销与增长", "sales-marketing-growth", "📣", 1),
    ("sales", 2, "市场调研", "sales-market-research", "🔎", 2),
    ("sales", 2, "商机跟进", "sales-opportunity-followup", "🎯", 3),
    ("", 1, "办公协作", "collaboration", "🗂", 9),
    ("collaboration", 2, "行政办公", "collab-admin-office", "🏢", 1),
    ("collaboration", 2, "数据分析", "collab-data-analysis", "📊", 2),
    ("", 1, "其他", "other", "📌", 10),
    ("other", 2, "个人生活", "other-personal-life", "🌱", 1),
    ("other", 2, "其他", "other-misc", "📌", 2),
]


def category_dict_seed_id(level_tuples: list, index: int) -> int:
    """种子行的显式 id（Issue #2761/A12）：一级 = 1000+order，二级 = 一级 id*100+order。

    与建表 DDL 的「id INTEGER PRIMARY KEY（非自增）」配套：id 由种子规则直接算出，
    不依赖 AUTOINCREMENT / lastrowid，迁移与同步两侧可独立复算。
    """
    parent_slug, level, _name, _slug, _icon, order_num = level_tuples[index]
    if int(level) == 1:
        return 1000 + int(order_num or 0)
    parent_id = 0
    for j in range(index):
        if level_tuples[j][0] == "" and level_tuples[j][3] == parent_slug:
            parent_id = category_dict_seed_id(level_tuples, j)
            break
    return parent_id * 100 + int(order_num or 0)


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
