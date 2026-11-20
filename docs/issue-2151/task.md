# Issue #2151 Task Checklist

- [ ] Task 1: 写 RED 契约测试
  - Acceptance: `tests/js/test_org_workbench.js` 可在实现前失败并指出缺失函数/API。
  - Verify: `node tests/js/test_org_workbench.js`
  - Files: `tests/js/test_org_workbench.js`

- [ ] Task 2: API 封装与错误映射
  - Acceptance: `API.orgRepos`/`API.orgDispatches` 路径和 403/409 映射存在。
  - Verify: 契约测试 + `node --check src/api/static/js/api.js`
  - Files: `src/api/static/js/api.js`, `tests/js/test_org_workbench.js`

- [ ] Task 3: 路由深链
  - Acceptance: `organizations` 带 param 时进入对应组织详情。
  - Verify: 契约测试 + `node --check src/api/static/js/app.js`
  - Files: `src/api/static/js/app.js`

- [ ] Task 4: 组织详情四卡片和 Tab
  - Acceptance: `org_repo_key`、`org_scene_id`、provision retry、设置入口、角色显隐存在。
  - Verify: 契约测试
  - Files: `src/api/static/js/pages/organizations.js`

- [ ] Task 5: 关联仓库 Tab
  - Acceptance: list/bind/unbind/transfer、确认弹层、错误提示与权限分支存在。
  - Verify: 契约测试
  - Files: `src/api/static/js/pages/organizations.js`

- [ ] Task 6: 需求下派和状态看板
  - Acceptance: 新建组织需求、多目标 dispatch、目标子 Issue/任务/PR/错误、cancel/retry 存在。
  - Verify: 契约测试
  - Files: `src/api/static/js/pages/organizations.js`

- [ ] Task 7: i18n 与状态闭环
  - Acceptance: zh/en 键齐全，空态/加载/错误态、失败高亮、context 占位可用。
  - Verify: 契约测试 + `node --check` 三个改动 JS
  - Files: `src/api/static/locales/zh/organizations.json`, `src/api/static/locales/en/organizations.json`
