# Implementation Plan: Issue #2151 组织工作台前端

## Overview

在现有组织管理页中扩展详情工作台。请求统一走 `api.js`，页面按「详情摘要 + 关联仓库 +
需求下派 + 下发看板 + context 占位」组织；权限只做前端显隐，后端 403 仍为最终边界。

## Architecture Decisions

- 不引框架：复用现有 `API.get/post/del`、`I18N.t`、`app.nav`、`app.showConfirm`。
- 保留现有成员管理：新增工作台区块包住原成员列表/编辑/删除入口，避免功能回退。
- API 分组封装：`API.orgRepos` 与 `API.orgDispatches` 固化路径、编码和错误码映射。
- 深链最小化：`#organizations/<org_id>` 进入详情，Tab/批次详情采用页面内状态。
- 看板手动刷新：进入详情单次请求 + 刷新按钮，不引入长驻定时器。
- 安全渲染：新内容统一 `_orgEsc`；内联操作使用数组索引，避免动态字符串注入。

## Task List

### Phase 1: Contract and red tests

- [ ] Task 1: 新增 `tests/js/test_org_workbench.js`，覆盖 API 路径、错误映射、权限、i18n 和页面函数契约。
  - Acceptance: 当前代码下测试失败，失败原因是目标契约尚未实现。
  - Verify: `node tests/js/test_org_workbench.js`
  - Files: `tests/js/test_org_workbench.js`

### Checkpoint: Contract

- [ ] RED 测试已提交，能稳定指出缺失能力

### Phase 2: API and routing foundation

- [ ] Task 2: 扩展 `api.js` 的 org 仓库/下派封装与 409/403 错误映射。
- [ ] Task 3: 扩展 `app.js` 的组织详情深链接线。
  - Acceptance: 两个模块语法检查通过，API 测试转为通过。
  - Verify: `node --check ...` + `node tests/js/test_org_workbench.js`
  - Files: `src/api/static/js/api.js`, `src/api/static/js/app.js`

### Checkpoint: Foundation

- [ ] API 路径、X-Org-ID 继承和深链路由契约通过

### Phase 3: Workbench UI

- [ ] Task 4: 实现详情四卡片、provision 重试、角色显隐和 Tab。
- [ ] Task 5: 实现关联仓库列表/关联/解绑/迁移与二次确认。
- [ ] Task 6: 实现组织需求新建、多目标下派、dispatch 详情、cancel/retry、失败高亮。
- [ ] Task 7: 补齐中英文 i18n、空态/加载/错误态与 context 占位。
  - Acceptance: 新增契约测试全绿，页面函数具备真实 API 接线。
  - Verify: `node --check src/api/static/js/pages/organizations.js` + `node tests/js/test_org_workbench.js`
  - Files: `src/api/static/js/pages/organizations.js`, `src/api/static/locales/{zh,en}/organizations.json`

### Checkpoint: Complete

- [ ] 新增测试通过，组织相关现有测试不回归
- [ ] 自审查与开源增量检查通过
- [ ] PR 创建并通知 QA

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 后端字段与页面假设不一致 | Med | 按 #2090/#2112/#2118 实际响应字段实现并写契约测试 |
| 动态组织名/错误文本造成 XSS | High | 统一转义，内联事件只传索引 |
| 大页面难维护 | Med | 按加载/渲染/动作拆函数，API 走分组封装 |
| 旧成员管理回归 | Med | 保留原函数入口，用新详情 shell 内嵌原成员列表 |
| context 依赖 #2150 | Low | 只做静态占位 |

## Open Questions

- 无。
