# Spec: Issue #2151 组织工作台前端

## Objective

在现有 vanilla JS 单页应用内补齐组织维度工作台，让组织 owner/admin
能够集中查看 provisioning/组织仓库/组织流水线、关联仓库、新建组织需求并跨仓下发，
并在下发看板中跟踪每个目标仓库的子 Issue、任务、PR、失败及部分失败状态。
maintainer/member 保持只读。成功标准是五个前端模块可用、现有前端测试不回归、新增静态契约测试通过，
且不新增后端接口。

## Tech Stack

- 前端：原生 JavaScript、HTML、CSS，现有 `API`/`I18N`/`app` 模块
- 后端：FastAPI 已合入接口，仅消费以下既有契约：
  - `GET/POST /api/orgs/{id}/repos`
  - `POST /api/orgs/{id}/repos/{repo_key}/transfer`
  - `DELETE /api/orgs/{id}/repos/{repo_key}`
  - `GET/POST /api/orgs/{id}/dispatches`
  - `GET /api/orgs/{id}/dispatches/{dispatch_id}`
  - `POST /api/orgs/{id}/dispatches/{dispatch_id}/cancel`
  - `POST /api/orgs/{id}/dispatches/{dispatch_id}/targets/{repo_key}/retry`
  - `POST /api/orgs/{id}/provision/retry`
  - `GET/POST /api/issues/{owner}/{repo}`
- 测试：Node 内置 `assert`/`fs`/`path`/`vm`，沿用 `tests/js` 静态契约测试模式

## Commands

- 新增契约测试：`node tests/js/test_org_workbench.js`
- 受影响 JS 语法检查：`node --check src/api/static/js/api.js`
- 受影响 JS 语法检查：`node --check src/api/static/js/pages/organizations.js`
- 受影响 JS 语法检查：`node --check src/api/static/js/app.js`
- 现有页面测试抽样：`node tests/js/test_issue_299_org_name_display.js`

## Project Structure

- `src/api/static/js/api.js`：通用请求封装；新增 `orgRepos`/`orgDispatches` 薄封装和错误码映射
- `src/api/static/js/pages/organizations.js`：组织列表、详情四卡片、工作台 Tab、向导、看板
- `src/api/static/js/app.js`：`#organizations/<org_id>` 深链路由接线
- `src/api/static/locales/zh/organizations.json`、`locales/en/organizations.json`：中英文键
- `tests/js/test_org_workbench.js`：页面函数、API 路径、权限分支、错误映射、i18n 契约
- `docs/issue-2151/`：本 Issue 的 Spec、Plan、Task

## Code Style

沿用现有 ES5 风格页面代码，动态文本先做 HTML 转义，内联事件只传整数索引，
不把组织名、仓库 key、错误文本直接拼进可执行字符串。

```js
function _orgCanWrite(role) {
  return ['owner', 'admin'].indexOf(String(role || '')) >= 0;
}
```

## Testing Strategy

- 静态契约测试加载真实 `api.js`/`organizations.js`/`app.js` 文本，断言关键 API 路径、
  函数、角色显隐分支与 i18n 键，不依赖浏览器和网络。
- 失败/部分失败、409/403、空态/加载/错误态在页面渲染辅助函数上做小范围行为断言。
- 现有前端测试作为回归门，至少运行组织相关用例和新增用例。

## Boundaries

- Always：复用既有 API；动态内容转义；member 只读；测试先行；只改 Issue 范围内文件。
- Ask first：新增后端接口、引入前端框架或第三方运行时依赖。
- Never：修改后端契约、提交密钥/令牌、破坏现有成员管理、用裸 DOM 字符串执行未转义数据。

## Success Criteria

- 组织详情四卡片展示 `org_repo_key`、`org_scene_id`、provision 状态与设置入口。
- provision 失败可重试；owner/admin 可见写操作，maintainer/member 只读。
- 关联仓库 Tab 支持列表、关联、解绑/迁移二次确认，409/403 展示本地化可读提示。
- 组织需求可新建且使用 `scene_id=org_scene_id`，可分派到多个目标仓库并返回 `dispatch_id`。
- 下发列表/详情手动刷新，目标行展示子 Issue、任务、PR、状态、错误；失败与部分失败高亮。
- context tab 显示「即将上线」占位。
- 中英文 i18n 键齐全，现有测试与新增 `tests/js/test_org_workbench.js` 通过。

## Open Questions

- 无。
