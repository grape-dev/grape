# Task Breakdown — Issue #543

- [x] T1: `repo_manager.fetch_user_orgs` GitCode V5 API 封装
- [x] T2: `GET /api/repos/migrate-orgs` 路由（鉴权 + with_oauth_token + 返回结构）
- [x] T3: `settings.js` 迁移弹窗改造（仓库名只读 + 组织下拉 + 异步拉取 + 失败提示）
- [x] T4: `migrateRepoFromSettings` 提交逻辑（target_repo 固定 / target_owner 取下拉值）
- [x] T5: i18n 新增 key（zh/en）
- [x] T6: 单测（fetch_user_orgs + migrate-orgs 端点鉴权/返回结构/失败）
- [x] T7: 自审查 + 增量敏感扫描 + 建 PR + 评论 + A2A QA
