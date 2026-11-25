# Spec：Issue #3798 — 技能分类链路降噪与 content-generation 补种

> 来源：Issue #3798 正文（grapedev.cn 2026-11-22 线上实测）+ Issue Agent 深度可开发性分析 Spec
> （评论 203621462）。本文件是 coding 侧的落地版，范围以 Spec 的 Scope(In) 为准。

## 目标

1. 分库拓扑下技能分类字典**不可用时不再静默**：每轮 1 条 ERROR + 启动期降级打点；
2. 未命中 `sub_scene` 的告警由「每技能每轮一条」改为「每轮 ≤1 条聚合」，保留 slug、
   次数与 ≤3 样例（对齐 #3796 在 `scene_sync` 落地的口径）；
3. `skill_category` 补种缺失 slug `content-generation`（mgmt 域幂等迁移），使该
   `sub_scene` 的官方技能 `category_id` 可解析；
4. 回归用例覆盖上述三条。

## 现状（基线 `arch-weekly-11-ljt` 核查）

| 事实 | 位置 | 结论 |
|---|---|---|
| `skill` 表经 `db_pair("runtime")` 落 runtime 域 | `runtime/storage/skill_sync.py` | 不回退该路径 |
| `skill_category` 经 `mgmt/public.skill_category_active_map()` 读 mgmt 域（owner DSN，`WHERE is_active=1`） | `mgmt/public/__init__.py` | 跨域读取已具备 |
| 字典读取失败被 `logger.debug` 吞掉 | `skill_sync._sync_skills_to_db` | 缺表 / DSN 错时无任何可判定信号 |
| 未命中逐技能发 WARNING | 同上 | ≈72 技能 × 4 主控 × 2 轮/小时 = 576 条/小时 |
| 种子 15 条无 `content-generation` | `api/db_setup._init_skill_category_data` | 内容生成类技能恒判未命中 |

## 范围

### In
- `src/domains/runtime/storage/skill_sync.py`：字典不可用口径（ERROR + 打点）、
  未命中轮级聚合。
- `src/domains/mgmt/migrations/skill_category_content_generation_seed.py`（新增）+
  `src/api/db_setup.py` 注册引导（`202611240012`，字面量登记）。
- `tests/runtime/test_issue_3798_skill_sync_category.py`（新增）；
  `tests/runtime/test_issue179_skills.py` 种子行数断言 15 → 16（种子集合法增长）。

### Out
- 不在 runtime 库新建 `skill_category` 副本（字典归 mgmt，单一真源）。
- 不改 `category_dict`（`cat_l1_id`/`cat_l2_id`）解析逻辑（#3796 已覆盖）。
- 不改前端分类树渲染；**不重开**被 202611210002 / 202611230001 停用的老分类体系。

## 验收

| 项 | 判定 |
|---|---|
| 字典不可用 | 每轮恰好 1 条 ERROR + `record_startup_degraded("skill_category", read_failed\|empty)` |
| N 技能同一缺失 slug | 本轮恰好 1 条 WARNING（含次数 + ≤3 样例），逐技能只落 DEBUG |
| 聚合文案 | 不含字面量 `has no matching skill_category slug`（LogQL 计数归零） |
| `content-generation` | 迁移幂等补种并启用；该 `sub_scene` 技能 `category_id` 非 0 |
| 门禁 | 新增用例 + 现有测试 + silent-except 棘轮 + `check-arch` + `make lint` 通过 |
| 部署后 | Loki 同名告警计数 0；`skill_category` 字典非空时不再出现集中降级 |

## 风险

| 风险 | 处理 |
|---|---|
| 迁移号撞号被静默跳过（#3630 教训） | 号段核对到 `202611240012`（当前最大 `202611240011`），注册用字面量供守卫解析 |
| 补种行若 `is_active=0` 等于没种 | 迁移内显式重申 `is_active=1`，post_check 校验「行在位且启用」才盖章 |
| 老分类体系复活的误解 | 迁移 docstring 写明：只新增该 slug，不重开 202611210002/202611230001 停用的 15 条 |
| 聚合告警弱化真实缺失信号 | 聚合行保留 slug + 命中次数 + ≤3 样例，逐技能明细降 DEBUG 不丢证据 |