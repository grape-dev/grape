# Issue #3648 Spec — 技能仓库分类异常（renumber 撞号迁移静默跳过）

> 开发分支：`feat/issue-3648` | 基线：`arch-weekly-11`（本 Issue 的模块只存在于 arch 线；场景未注入 `GRAPE_WORK_BRANCH`）

## 目标

让被 renumber 过的分类收敛 / 分类列迁移，在「版本台账已被旧同名号盖章」的库上仍能真正执行：

- mgmt：`grape_t_category_dict` 仅 1001/1002/1010 三个一级活跃，老分类全部停用；
- runtime：`skill` 具备 `cat_l1_id` / `cat_l2_id`，技能列表非空；
- 读路径不再吞 schema 异常；
- 启动期后置校验拦住「台账 applied 但目标缺失」。

## 现状与根因

dev 三库台账现值（详单 #3630）：

```
202611180003  skill-intro-override-schema      （旧构建）
202611180004  category-collapse-three-domains  （旧合并迁移）
```

本分支注册的是同号新名：

- `202611180003 issue2761-skill-classification-columns`（runtime 列）
- `202611180004 category-collapse-three-domains-mgmt`（mgmt 收敛）
- `202611181304 category-collapse-three-domains-runtime`（runtime 收敛）

执行器 `_run_migrations_with_conn` 只按 version 判 pending，撞号守卫只打 ERROR 后仍跳过 →
旧号库上的新迁移永不执行。mgmt 老分类残留、runtime 缺列；`list_skills` 的裸 `except` 把
`skill_search` 的 `no such column` 吞成 200 空列表。

## 方案

1. **renumber（主）**：三条迁移改到 `202611210001/2/3`，避开旧台账号；顺序保证
   runtime 先补列（1001）再收敛（1003）。
2. **执行器后置校验（兜底）**：`Migration` 增加 `post_check`；同号不同名时先校验目标：
   - 已到位 → 视为已执行；
   - 未到位 → 重跑幂等 fn + 再校验，通过才改盖新名；
   - 校验失败 → ERROR 且不盖章；
   - 无 `post_check` 的撞号保持 fail-safe 跳过但必须 ERROR（不静默）。
3. **两个收敛迁移带 post_check**：mgmt 校验活跃一级集合；runtime 校验列在 + 无残留旧
   `cat_l1_id`。
4. **启动期总校验**：`verify_migration_targets()` 在 mgmt / runtime 启动收口后核对
   「3 个活跃一级 / skill 两列」，失败落 ERROR。
5. **读路径**：`list_skills` 的 `skill_search` 异常改 `log.exception` + HTTP 503。
6. **前端**：`SkillsView.vue` 去掉 `loadCategories()` 会话级缓存，进入视图即刷新。

## 验收

- [x] 旧号 202611180003/04/1304 不再注册，新号 202611210001/2/3 注册且带 post_check；
- [x] 台账 applied + 目标缺失：旧逻辑跳过（红），新逻辑重跑并改盖新名（绿）；
- [x] 目标已到位不重跑；post_check 失败不盖章；
- [x] mgmt 后置校验要求活跃一级恰为 1001/1002/1010；
- [x] runtime 后置校验要求 `cat_l1_id/cat_l2_id` 存在；
- [x] `list_skills` schema 异常返回 503 + ERROR 日志；
- [x] SkillsView 进入视图重新加载分类。

## 范围外

- dev 环境已手工补齐的列 / 数据不回滚；
- 前端分类树整体 UI 重构；
- 与本 Issue 无关的历史迁移业务改造。