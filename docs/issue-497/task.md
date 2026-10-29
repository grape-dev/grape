# Task: Issue #497 — 登录名变更同步

## 切片 1：同步函数（grape_account_db.sync_external_login_on_rename）
- [ ] 红：测试四表同步 + UNIQUE 冲突保留旧 username + 未改名不写 + audit 留痕
- [ ] 绿：实现 sync_external_login_on_rename + 迁移 _sync_um_platform_col + audit EVENT_TYPES.login_rename

## 切片 2：回调接线（grape_auth._handle_oauth_login）
- [ ] 红：已绑定分支改名登录 → 四表更新；rebind 路径改名 → 更新
- [ ] 绿：已绑定分支 + rebind 路径接入对比与 sync 调用

## 切片 3：issues_center assignee fallback 验证
- [ ] 测试：同步后 assignee fallback 取新名（不改取值逻辑）

## 收尾
- [ ] 全量回归（现有测试全过）
- [ ] 自审查（5 轴 + 开源前置增量检查）
- [ ] 建 PR + Issue 评论 + A2A QA