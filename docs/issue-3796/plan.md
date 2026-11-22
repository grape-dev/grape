# Issue #3796 方案设计（plan）

> 分支 `feat/issue-3796`，基线 `arch-weekly-11-ljt`。测试 seam 见 task.md；TDD 红/绿分提交。

## 总体思路

别名落点选 **kernel 共用**（场景+技能同步共用单一解析口径，避免二次漂移）；
别名定位为**过渡**（带每进程每 slug 1 条 deprecation INFO，数据侧迁移完成后可整表删除）；
未命中语义保持**保留库内旧值**；校验强度选**分级**（启动默认聚合告警 + strict 开关、
CI 强校验），避免误伤线上启动。

## 改动 1：`src/kernel/category.py`

- 新增 `LEGACY_CATEGORY_ALIASES`（纯数据，独立可调）：
  `skill-dev→it-rd-grape-platform`（Spec 初值，产品口径确认后改一行）、
  `iac→it-rd-infra`、`general→other-misc`、`content→ops-content-goods`、
  `ai→it-rd-ai-app`、`swarm-dev→it-rd-engineering`。
- 新增 `canonical_category_slug(slug, logger=None)`：旧 slug → 收敛后 slug，无别名原样返回；
  别名生效每进程每 slug 仅记 1 条 INFO（模块级 `_alias_info_logged` 去重）。
- `lookup_slug`：先直查 `dict_map`，未命中再过别名查目标 slug；返回值语义不变（未命中 (0,0)）。
- `resolve_category_slugs(..., on_miss=None)`：经 `lookup_slug` 自动获得别名能力；
  新增 `on_miss(slug)` 回调——提供时未命中降为 debug + 回调（调用方按轮聚合），
  缺省保持原 WARNING 行为（skill_sync 等调用方零改动）。新增 debug/聚合文案
  避免出现 `not found in category_dict` 字面量，防 LogQL 误计。
- 新增 `validate_category_slugs(slugs, dict_map)`：返回「直查与别名均未命中」的 slug 列表
  （去重保序，无日志副作用）。
- 新增 `scene_category_slug_values(info)`：从 scene/skill JSON 提取全部 slug
  （category_l1/category_l2/category，path 形态按 `/` 拆段）。
- 新增 `category_seed_dict_map()`：种子级 slug → {id, level, parent_id}（CI/启动无 DB 时的纯数据口径）。
- 新增 `iter_scenes_category_slugs(scenes_dir)` + `validate_local_scenes_categories(scenes_dir)`：
  遍历 `scenes/*/scene.json`，返回 {scene: [未知 slug]}；目录缺失返回空 dict（降级跳过）。

## 改动 2：`src/domains/runtime/storage/scene_sync.py`

- `_resolve_scene_category(scene_data, miss_sink=None, scene_name="")`：
  miss_sink 提供时传 `on_miss` 给 kernel，未命中进 sink（循环内仅 debug）。
- `sync_official_scenes` 循环前建 `unresolved: dict[str, dict]`（slug → {count, scenes≤3}）；
  `_record_unresolved(sink, slug, scene)` 计数 + 样例收集。
- `category_id`（`_cat_cache`/scene_category）路径同口径先直查、未命中过
  `canonical_category_slug` 再查，两条路径共用同一 sink；未命中循环内仅 debug。
- 轮末（stale 检测前）有未命中才发 1 条聚合 WARNING：
  `Scene sync: unresolved category slugs this round (kept existing categories): 'slug'×N (scenes: ...)`
  —— 文案不含 `not found in category_dict` / `not found in scene_category` 字面量。

## 改动 3：校验接线

- 新增 `scripts/check_category_slugs.py`：`scenes/` 缺目录 → 打印说明退出 0（降级跳过）；
  存在未命中 → 打印明细退出 1。支持 `--dir` 指定 scenes 目录。
- 启动期（`src/api/main.py` lifespan，安全门禁之后）：调用
  `validate_local_scenes_categories`；默认 1 条聚合 WARNING；
  `CATEGORY_SLUG_STRICT=1` 时 log.error + SystemExit(2) fail-fast；
  校验异常不阻断启动（non-fatal WARNING）。
- CI：`.github/workflows/windows-smoke.yml` linux-backend job 新增步骤：
  `python3 scripts/check_category_slugs.py` + 新增用例 pytest
  （tests/kernel/test_issue_3796_category_aliases.py、
  tests/runtime/test_issue_3796_scene_sync_category.py、
  tests/scripts/test_check_category_slugs.py）。

## 测试 seam（公开接口）

1. `src.kernel.category`：`lookup_slug` / `resolve_category_slugs` / `validate_category_slugs`
   / `canonical_category_slug` / `iter_scenes_category_slugs` / `category_seed_dict_map` —— 纯函数 seam。
2. `scene_sync`：`_resolve_scene_category(miss_sink)`、`_record_unresolved`、
   `sync_official_scenes` 轮末聚合 WARNING（mock ZIP + mock DB，沿用 tests/mgmt/test_scene_sync.py 先例）。
3. `scripts/check_category_slugs.py`：`main(argv)` 退出码 seam（tests/scripts 子进程/函数双口径）。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 别名映射口径与产品最终口径不符（skill-dev 落向） | 纯数据表独立可调，改一行生效 |
| 部署根无 `scenes/`（grape-skills 下发） | 校验脚本/启动校验对缺目录降级跳过 |
| 聚合告警掩盖新增坏 slug | 聚合行输出完整 slug 列表 + CI/strict 校验前移拦截 |
| 现有 test_scene_sync 用例依赖 `_cat_cache={"general":1}` 直查 | category_id 路径保持「先直查后别名」，直查命中行为不变 |
