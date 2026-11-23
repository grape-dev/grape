# Spec — Issue #3846：202611180001 撞号致 ensure-cloud-sandboxes DDL 永久跳过

> 来源：Issue #3846 正文 + Issue Agent Spec 评论（评级 ready，目标分支 arch-weekly-11-ljt）。

## 目标

让 runtime 域 `grape_t_cloud_sandboxes` 的建表 + 三索引迁移在存量库上真正执行，
不再因版本号被其它分支的迁移（`issue2761-skill-classification-columns`）盖章而被判
「已执行」永久跳过；并让同类撞号不再只能靠启动期 ERROR 暴露。

## 现状（源码证据）

1. `src/domains/runtime/migrations/ensure_cloud_sandboxes_table.py` 注册
   `version="202611180001"`、`name="ensure-cloud-sandboxes-in-runtime"`，`MIGRATION`
   无 `post_check`。
2. `src/api/db_setup.py` 撞号守卫（#1412 / #3648）：同号不同名时，
   `post_check is None` 分支只 `log.error` + `continue`——该迁移永远不进 pending，
   建表 DDL 永不执行。
3. 注册表内 `202611180001` 现只剩本迁移一家；残留是存量库历史盖章行
   （`202611180001=issue2761-skill-classification-columns`）。renumber 到未占用号
   即可命中 pending；`post_check` 作为后续跨分支撞号的兜底。

## 期望行为（In Scope）

1. `MIGRATION["version"]` 由 `202611180001` 改为未占用号 `202611230010`
   （已核 `202611230001/002` 被技能分类切换占用、`003/004` 被 autopilot 失败状态列
   占用，`202611230010` 全仓未占用且与同段留间隔）；文件头 docstring、
   `[DDL_SKIP]`/索引跳过/就位日志里的旧号一并替换。
2. 新增 `_postcheck_cloud_sandboxes()`：校验 `grape_t_cloud_sandboxes` 表与
   `idx_cloud_sandboxes_repo/state/task` 三索引存在，以 `post_check=` 注册，使同号
   不同名时进入 db_setup 的 reconcile 队列（幂等重跑 fn + 再校验后盖章）。
3. 单测：带 `post_check` 的撞号迁移进 reconcile；无 `post_check` 仅 ERROR 不进。
4. CI 门禁：register_migration 注册表同一 version 重复登记即失败（接入
   `.github/workflows/windows-smoke.yml` linux-backend job）。

## Out of Scope

- 不改 db_setup 撞号守卫的 fail-safe 语义（无 `post_check` 仍只报错不修）。
- 不删除存量库历史盖章行 `202611180001=issue2761-skill-classification-columns`。
- 不处理 #3527 的 mgmt 空表遮蔽。
- 不刷新 `tests/arch/migration_versions_baseline.json`（按其 note 需发布评审单独提交；
  该基线测试在改动前已因 #3648 renumber 处于红态，PR 中如实说明）。

## 验收标准

- [ ] 存量库启动后 `grape_t_cloud_sandboxes` 与三索引存在
- [ ] 台账新增行 `name=ensure-cloud-sandboxes-in-runtime`（`202611230010`），旧行保留
- [ ] 启动日志「迁移版本号撞号」不再出现该迁移
- [ ] 单测：带 post_check 进 reconcile、无 post_check 仅 ERROR 不进
- [ ] CI：同 version 重复登记即失败
- [ ] 现有相关测试通过（基线测试的红态为存量问题，PR 说明）
