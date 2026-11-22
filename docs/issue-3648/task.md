# Issue #3648 任务拆解

- [x] T1 守护测试先行：新增 `tests/api/test_issue_3648_migration_reconcile.py`（先红）
- [x] T2 执行器：`Migration.post_check` + 撞号 reconcile + 盖章前校验
- [x] T3 启动期后置校验 `verify_migration_targets()` 接入两个迁移入口
- [x] T4 mgmt 分类收敛：改号 202611210002 + `verify_category_collapse`
- [x] T5 runtime 分类列：改号 202611210001 + `verify_skill_classification_columns`
- [x] T6 runtime 分类收敛：改号 202611210003 + `verify_skill_category_collapse`
- [x] T7 `list_skills` 去裸 except：`log.exception` + 503
- [x] T8 `SkillsView.loadCategories()` 缓存失效 + 前端缓存版本 bump
- [x] T9 回归：迁移守护 / 域归属 / 分类标签 / 新增守护测试
- [x] T10 自审查 + 开源前置增量检查
- [x] T11 提交 / push / 创建 PR #1938 / Issue 评论 / A2A QA（seq=91449）