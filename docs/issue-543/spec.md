# Spec — Issue #543：迁移到其他组织优化

> 完整 Spec 见 Issue #543 评论（id=197105409，issue agent 发布）。
> 目标分支：`weekly-09-ljt`。

## Objective
优化 #540 已实现的「迁移仓库到其他组织」交互：
1. 迁移时仓库名只读不可修改，并展示在目标组织选择区上方；
2. 目标组织改为下拉框选择，选项 = 当前登录用户拥有的 GitCode 组织列表。

## Scope (In)
1. `settings.js` 迁移弹窗：仓库名只读展示（置于目标组织选择区上方）；
2. 目标组织输入框 → 下拉框，数据源 = 当前登录用户拥有的 GitCode 组织（新增 `GET /api/repos/migrate-orgs` 端点 + `repo_manager.fetch_user_orgs` GitCode V5 API 封装）；
3. 提交沿用 `POST /api/repos/migrate`，`target_repo` 固定当前仓库名、`target_owner` 取下拉选中值，既有前置校验不变；
4. 单测覆盖（组织列表端点鉴权/返回结构等）。

## Out of Scope
迁移核心逻辑（repo_key 原子改写/webhook/工作区目录迁移）不改动；#540 QA Required 遗留项另行处理。

## Acceptance Criteria
- 迁移界面仓库名只读且在目标组织选择区上方；
- 目标组织下拉选择、无手工输入入口；
- 下拉仅列当前用户拥有的组织；
- 迁移后仓库名不变；
- 组织列表拉取失败有提示；
- 现有测试通过、新增测试覆盖新功能。
