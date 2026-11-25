# Issue #3796 Spec — 场景分类旧 slug 别名过渡 + category_dict 未命中告警治理

> 开发分支：`feat/issue-3796` | 基线：`arch-weekly-11-ljt`（服务端注入 `GRAPE_WORK_BRANCH`）
> Spec 权威源：Issue #3796「[issue agent] 评级 ready | 📋 Spec Document」评论（coding 侧按其执行，本文件为落仓镜像）。

## 目标

1. 消除 grape-mgmt 每轮场景同步对已收敛旧 slug 的 696 条/小时 WARNING
   （`Scene sync: category slug '<slug>' not found in category_dict, keep existing category`）。
2. 让仍携带旧 slug 的场景分类恢复同步，`cat_l1_id/cat_l2_id` 落到收敛后 1xxx 段
   （如 `skill-dev`→`it-rd-grape-platform` = 1001/100109），不再保留收敛前旧值。
3. 未命中告警按同步轮聚合：每轮 ≤1 条 WARNING（slug→次数 + 样例场景），不再按场景刷屏。
4. 校验前移：CI 门禁 + 启动期校验可拦截「字典无此 slug 且无别名」的 scene.json。

## 现状与根因

- 分类字典已收敛（Issue #3202/#3648）：`CATEGORY_DICT_SEED` 只剩 `it-rd-* / operations /
  ops-* / customer-support / cs-* / hr / finance / supply-chain / manufacturing / sales /
  collaboration / other-*`；`lookup_slug` 未命中即 (0,0) + WARNING。
- 场景定义（`scenes/*/scene.json`，由 grape-skills 仓库随 ZIP 下发）仍携带旧 slug：
  `skill-dev`(×4)、`iac`(×3)、`general`、`content`、`ai`、`swarm-dev`（线上）。
- `resolve_category_slugs` 未命中 → WARNING + (0,0)，调用方「保留库内旧值」→
  这批场景分类同步永久空转 + 每轮刷 WARNING（696 条/小时，4 主控均分）。
- 同源第二路径：`scene_sync` 的 `category_id = _cat_cache.get(slug, 0)`（scene_category 表）
  在生产收敛后同样未命中，另发 `not found in scene_category`。

## 约束

- **未命中语义不变**：保留库内旧值，绝不写 0 覆盖（与 `sync_category_target` 契约一致）。
- **不重编号、不改 `CATEGORY_DICT_SEED` 结构**；不改 `scene_category` 表结构。
- 目标基线根目录**无 `scenes/`**：`scene.json` 由 grape-skills 仓库下发；校验脚本与启动期
  校验对缺目录降级跳过；grape-skills 的 `scene.json` 迁移单列跨仓跟进，不在本仓阻断。

## 验收

- LogQL `sum(count_over_time({service_name="grape-mgmt"} |= "not found in category_dict" [1h]))` = 0
  （旧 slug 全部经别名命中，不再产生该文案告警）。
- 同步一轮后旧 slug 场景 `cat_l1_id/cat_l2_id` 落 1xxx（`it-rd-*`/`other-misc`/`ops-content-goods`）。
- 未命中告警每轮 ≤1 条且带 slug 计数。
- 校验可拦截「无别名未知 slug」：CI 非 0 退出 / `CATEGORY_SLUG_STRICT=1` 启动失败。
- 现有测试全绿 + 新增用例（tests/kernel、tests/runtime、tests/scripts）。
