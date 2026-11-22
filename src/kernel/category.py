"""统一分类字典（category_dict）slug 解析工具（Issue #2761）。

场景同步与技能同步共用同一套解析口径，避免两份实现漂移：
- 支持显式 `category_l1` / `category_l2`，或单个 `category`（一级/二级/"一级/二级"）；
- 未命中的显式 slug 会告警（便于发现仓库配置错误），但保留已命中侧，返回 (0, 0) 时
  由调用方决定保留库内旧值，绝不写 0 覆盖既有分类。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

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


#: Issue #3796 legacy slug 过渡别名：分类收敛（#3202/#3648）后历史 scenes/*/scene.json
#: （grape-skills 仓库下发）仍携带旧 slug，直查恒未命中 → 同步每轮刷 696 条/小时
#: WARNING 且这批场景分类永久停更。别名让解析链过渡命中收敛后 slug；数据侧迁移
#: 完成后可整表删除。skill-dev 落 it-rd-grape-platform 为 Spec 初值，产品口径
#: 确认后改这一行即生效（纯数据，独立可调）。
LEGACY_CATEGORY_ALIASES = {
    "skill-dev": "it-rd-grape-platform",
    "iac": "it-rd-infra",
    "general": "other-misc",
    "content": "ops-content-goods",
    "ai": "it-rd-ai-app",
    "swarm-dev": "it-rd-engineering",
}

#: 别名生效去重集合：每进程每 slug 仅记 1 条 INFO（防同步每轮刷屏）。
_alias_warned: set[str] = set()


def canonical_category_slug(slug: str,
                            logger: logging.Logger | None = None) -> str:
    """旧 slug → 收敛后 slug；无别名原样返回（Issue #3796）。

    别名生效每进程每 slug 记 1 条 deprecation INFO（非 WARNING，避免污染
    告警计数），提示数据侧迁移 scenes/*/scene.json 后可移除过渡。
    """
    key = (slug or "").strip()
    if not key:
        return slug or ""
    target = LEGACY_CATEGORY_ALIASES.get(key)
    if not target:
        return key
    log = logger or logging.getLogger(__name__)
    if key not in _alias_warned:
        _alias_warned.add(key)
        log.info(f"category: legacy slug '{key}' aliased to '{target}' "
                 f"(Issue #3796 transition; migrate scenes/*/scene.json to retire)")
    return target


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
    """把 slug 解析成 (l1_id, l2_id)；未命中返回 (0, 0)。

    Issue #3796：先直查，未命中再过 legacy 别名查目标 slug（直查命中优先，
    管理台自定义同名 slug 不被别名抢占）。
    """
    item = (dict_map or {}).get((slug or "").strip())
    if not item:
        aliased = canonical_category_slug(slug)
        if aliased and aliased != (slug or "").strip():
            item = (dict_map or {}).get(aliased)
    if not item:
        return 0, 0
    if int(item.get("level") or 1) == 1:
        return int(item["id"]), 0
    return int(item.get("parent_id") or 0), int(item["id"])


def _slug_miss(log: logging.Logger, on_miss: Callable[[str], None] | None,
               slug: str, prefix: str, tail: str) -> None:
    """未命中上报：有聚合回调走 debug（调用方轮末聚合，Issue #3796），否则保持原 WARNING。

    debug/聚合文案不使用「not found in category_dict」字面量，防 LogQL 告警
    计数把 debug 行也算进去。
    """
    if on_miss is not None:
        log.debug(f"{prefix} slug '{slug}' unresolved in category_dict, {tail} (aggregated per round)")
        on_miss(slug)
    else:
        log.warning(f"{prefix} slug '{slug}' not found in category_dict, {tail}")


def resolve_category_slugs(info: dict, dict_map: dict[str, dict[str, Any]],
                           logger: logging.Logger | None = None,
                           label: str = "category",
                           on_miss: Callable[[str], None] | None = None) -> tuple[int, int]:
    """按 info 中的分类 slug 解析 (cat_l1_id, cat_l2_id)。

    Issue #3796：lookup_slug 内置 legacy 别名过渡；on_miss 提供时未命中降为
    debug + 回调（调用方按同步轮聚合告警），缺省保持每处 1 条 WARNING。
    """
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
            _slug_miss(log, on_miss, l1, f"{label}: category_l1", "keep existing")
    if l2:
        a, b = lookup_slug(dict_map, l2)
        if a:
            l1_id, l2_id = a, b
        else:
            _slug_miss(log, on_miss, l2, f"{label}: category_l2", "keep existing")
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
    _slug_miss(log, on_miss, raw, f"{label}: category", "keep existing category")
    return 0, 0


def validate_category_slugs(slugs, dict_map: dict[str, dict[str, Any]]) -> list[str]:
    """返回「直查与别名均未命中」的 slug 列表（去重保序；Issue #3796 启动/CI 校验用）。

    纯函数、无日志副作用：校验高频调用不应每轮刷 deprecation INFO。
    """
    unknown: list[str] = []
    for raw in slugs or []:
        slug = (str(raw or "")).strip()
        if not slug or slug in unknown:
            continue
        if (dict_map or {}).get(slug):
            continue
        target = LEGACY_CATEGORY_ALIASES.get(slug, "")
        if target and (dict_map or {}).get(target):
            continue
        unknown.append(slug)
    return unknown


def scene_category_slug_values(info: dict) -> list[str]:
    """从场景/技能 JSON 提取全部分类 slug（category_l1/l2/category，path 按 `/` 拆段）。"""
    values: list[str] = []
    for key in ("category_l1", "category_l2"):
        v = str((info or {}).get(key) or "").strip()
        if v:
            values.append(v)
    raw = str((info or {}).get("category") or "").strip()
    if raw:
        values.extend(p.strip() for p in raw.replace("\\", "/").split("/") if p.strip())
    return values


def category_seed_dict_map() -> dict[str, dict[str, Any]]:
    """种子级 slug → {id, level, parent_id} 映射（CI/启动校验无 DB 时的纯数据口径）。"""
    ids = {row[3]: category_dict_seed_id(CATEGORY_DICT_SEED, i)
           for i, row in enumerate(CATEGORY_DICT_SEED)}
    m: dict[str, dict[str, Any]] = {}
    for row in CATEGORY_DICT_SEED:
        parent_slug, level, _name, slug, _icon, _order = row
        m[slug] = {"id": ids[slug], "level": int(level),
                   "parent_id": int(ids.get(parent_slug, 0)) if parent_slug else 0}
    return m


def iter_scenes_category_slugs(scenes_dir: str) -> dict[str, list[str]]:
    """扫描 scenes/*/scene.json，返回 {scene_name: [分类 slug]}（Issue #3796）。

    scenes_dir 不存在返回空 dict（部署根 scenes/ 由 grape-skills 随 ZIP 下发，
    缺目录属正常，由调用方决定是否提示跳过）。
    """
    result: dict[str, list[str]] = {}
    if not scenes_dir or not os.path.isdir(scenes_dir):
        return result
    for entry in sorted(os.listdir(scenes_dir)):
        if entry in ("_template", "third-party", "__MACOSX", ".DS_Store", ".git"):
            continue
        json_path = os.path.join(scenes_dir, entry, "scene.json")
        if not os.path.isfile(json_path):
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                info = json.load(f)
        except Exception:
            continue
        slugs = scene_category_slug_values(info if isinstance(info, dict) else {})
        if slugs:
            result[entry] = slugs
    return result


def validate_local_scenes_categories(scenes_dir: str) -> dict[str, list[str]]:
    """校验 scenes/*/scene.json 的分类 slug，返回 {scene: [未知 slug]}（Issue #3796）。

    供启动期（默认聚合 WARNING / CATEGORY_SLUG_STRICT=1 fail-fast）与 CI 门禁
    共用；dict_map 用种子映射（无 DB 纯数据口径）。scenes_dir 缺失返回空 dict。
    """
    if not scenes_dir or not os.path.isdir(scenes_dir):
        return {}
    dict_map = category_seed_dict_map()
    unknown: dict[str, list[str]] = {}
    for scene_name, slugs in iter_scenes_category_slugs(scenes_dir).items():
        bad = validate_category_slugs(slugs, dict_map)
        if bad:
            unknown[scene_name] = bad
    return unknown
