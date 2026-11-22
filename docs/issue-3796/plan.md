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

## 审查修复（PR #1982 · QA Required/Optional/Nit）

### Required：hash 幂等层挡住分类回填（Spec 验收第 2 条的真正落地路径）

原方案只改了「解析」侧（别名 + 轮聚合），但写库侧 `upsert_scene_content*` 在
`content_hash` 与库内一致时直接 `skipped`：线上存量旧 slug 场景的 `scene.json` 未改、
hash 长期不变 → 每轮 skipped → `cat_l1_id/cat_l2_id` 永远停在收敛前旧值（告警会归零，
但「场景分类停更」未修复）。

修复分两层，缺一不可：

1. **mgmt 写原语（`src/domains/mgmt/public/__init__.py`）**
   - 抽出 `_scene_category_backfill_cols(payload, old_cat, old_l1, old_l2)`：只挑
     「目标明确（非 0）且与库内不同」的分类列；未命中语义不变（列缺席/0 = 保留旧值）；
     `cat_l1_id` 沿用 `sync_category_target`（一级变更且未给二级 → 重置旧二级）。
   - `upsert_scene_content` / `upsert_scene_content_any` 的 hash 一致分支先比对分类目标：
     不同 → **一次窄 UPDATE（只动分类列，内容列/`is_active` 不动）** 并返回
     `action="backfilled"` + `backfilled=[列名]`；相同 → 仍 `skipped`（幂等语义不变）。
2. **runtime 同步层（`scene_sync._persist_scene_row`）**
   - 三库拓扑下写走 `cross_write`，键原为 `scene-upsert:{name}:{hash}` = **永久去重**：
     存量场景每轮 `duplicate`，mgmt 侧 upsert 根本不执行 → 回填在线上跑不到。
   - 新增 `_scene_upsert_key`：键含分类目标（`category_id`/`cat_l1_id`/`cat_l2_id`）→
     目标变化换键、真实执行一次；同内容同目标仍去重（跨域写日志行不随轮次增长）。
   - `backfilled` 单独计 `stats["backfilled"]` 并跳过 icon/ref 重建（内容未变）。

验收口径：`sync_official_scenes(repo_root=...)` 全流程用例（**不 mock 持久化层**）断言
「hash 未变 + 分类目标变更 → 库内 `cat_l1/cat_l2` 落 1xxx，第二轮幂等不再写」。

### Optional

- `category_id`（scene_category）路径对 path 形态分类**逐段解析**（原实现整串查恒未命中）。
- 启动校验优先用运行时字典快照 `category_dict_active_map("scene")`，取不到回落种子口径，
  日志/告警显式标注 `dict=` 来源（消除「校验用种子、运行时用 DB」的口径错位）。
- `scripts/check_category_slugs.py`：缺 `scenes/` 输出 WARNING（不再静默通过）+
  显式 `--allow-missing` 开关。

### Nit

- 聚合文案 `'slug'×N hits`（次数含两条路径，避免误读为场景数）。
- `src/api/main.py` 去掉函数内 `import os as _os`，仓库根探测复用 `scene_sync._repo_root`。
- 别名解析抽 `alias_target()` 纯函数（跟随别名链），`lookup_slug`/`canonical_category_slug`/
  `validate_category_slugs` 共用，避免多处漂移。

### 追加测试 seam

- `test_issue_3796_scene_category_backfill`（mgmt，真 sqlite + lib 假游标）：回填/幂等/未命中/二级重置。
- `test_issue_3796_scene_sync_category`：`sync_official_scenes` 真持久化回填 + 幂等键含分类目标 +
  `_resolve_scene_category_id` path 拆段。
- `test_issue_3796_category_aliases`：别名链、`dict_map` 快照。
- `test_issue_3796_category_slug_startup` / `test_check_category_slugs`：DB 口径优先、缺目录 WARNING、`--allow-missing`。
