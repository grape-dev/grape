# Issue #3796 任务拆解（task）

> TDD：每个切片先提交红测试（`test(#3796)`），再提交最小实现（`feat(#3796)`）。

## 切片 1：kernel 别名 + 校验函数

- [x] RED：`tests/kernel/test_issue_3796_category_aliases.py`
  - 别名表 6 条映射齐备（skill-dev/iac/general/content/ai/swarm-dev）
  - `lookup_slug` 直查命中不变；直查未命中→别名命中（1xxx 段）；直查+别名均未命中 (0,0)
  - `resolve_category_slugs`：`category="skill-dev"` 解析到 it-rd-grape-platform；on_miss 聚合回调生效（不再出 WARNING）
  - `canonical_category_slug`：别名生效每进程每 slug 仅 1 条 INFO（caplog 断言）
  - `validate_category_slugs`：无别名未知 slug 被拦截；别名覆盖的 slug 放行；去重
  - `iter_scenes_category_slugs` / `validate_local_scenes_categories`：扫描 + 缺目录降级空 dict
- [x] GREEN：`src/kernel/category.py`（LEGACY_CATEGORY_ALIASES / canonical_category_slug /
  lookup_slug 别名 / resolve_category_slugs on_miss / validate_category_slugs /
  scene_category_slug_values / category_seed_dict_map / iter_scenes_category_slugs /
  validate_local_scenes_categories）

## 切片 2：scene_sync 告警聚合 + category_id 别名

- [x] RED：`tests/runtime/test_issue_3796_scene_sync_category.py`
  - `_resolve_scene_category(miss_sink)` 未命中进 sink、不刷 WARNING（caplog 0 条 WARNING）
  - `_record_unresolved` 计数 + 样例 ≤3 去重
  - `sync_official_scenes` 全流程（mock ZIP + mock DB）：旧 slug 场景经别名落 1xxx；
    轮末恰好 1 条聚合 WARNING（带 slug 计数）；未命中不写 0 覆盖（`sync_category_target` 语义回归）
  - `category_id` 路径：`_cat_cache` 直查未命中过别名再查；两条路径同 sink
- [x] GREEN：`src/domains/runtime/storage/scene_sync.py`

## 切片 3：校验接线（CI 脚本 + 启动期 + 门禁）

- [x] RED：`tests/scripts/test_check_category_slugs.py`
  - `scenes/` 缺目录 → 退出 0（降级跳过）
  - 含未知 slug scene.json → 退出 1 且明细含 slug
  - 仅别名可解析的旧 slug → 退出 0
- [x] GREEN：`scripts/check_category_slugs.py` + `src/api/main.py` 启动期校验
  （默认聚合 WARNING，`CATEGORY_SLUG_STRICT=1` fail-fast）+
  `.github/workflows/windows-smoke.yml` linux-backend 步骤接入

## 切片 4：回归与收尾

- [x] 新用例 + 既有 `tests/mgmt/test_scene_sync.py`（category=general 直查路径不回归）全绿
- [x] 自审查（5 轴 + pre-pr 增量检查）
- [x] 提交推送 `feat/issue-3796`、建 PR（base=arch-weekly-11-ljt）、Issue 评论告知、A2A QA
