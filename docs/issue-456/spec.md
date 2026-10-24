# Spec: Issue #456 — 组织管理页面增加返回工作台按钮

## Objective
组织管理列表页头部操作区增加「返回工作台」按钮，点击复用 `app._accountGoBack()` 守卫逻辑返回工作台视图，补齐导航闭环（同类交互先例：#302 工作台「返回仓库大厅」、用户设置页「返回工作台」）。

## 背景
用户从工作台右上角组织菜单（`tbOrgMenu` → `app.nav('organizations')`）进入组织管理页后，`nav('organizations')` 分支会隐藏侧边栏（app.js:1534），页面头部仅有「创建组织」按钮，无返回工作台入口，只能靠浏览器返回或手动改路由，导航路径断裂。

## Scope (In)
- `src/api/static/js/pages/organizations.js`：`renderOrganizations()` 头部按钮容器（与「创建组织」同排）新增「返回工作台」按钮，`class="bt bt-o"`（参考用户设置页 account.js:31），点击调用 `app._accountGoBack()`；
- `src/api/static/locales/zh/organizations.json`：新增 `back_to_workspace` = "← 返回工作台"；
- `src/api/static/locales/en/organizations.json`：新增 `back_to_workspace` = "← Back to Workspace"。

## Scope (Out)
- 不改工作台、仓库大厅现有返回类按钮；
- 不改 `/orgs/*` 后端接口；
- 不做面包屑、快捷键等扩展导航。

## 行为语义（复用 `app._accountGoBack()`，app.js:560）
1. 恢复侧边栏 `display:flex`；
2. 未绑定平台（`user.bindings` 为空）→ `goToRepos()` 回仓库引导页（避免 403）；
3. 有当前仓库（`currentRepo()` 非空）→ `nav('dash')` 回工作台；
4. 无当前仓库 → `goToRepos()` 回仓库列表。

## Acceptance Criteria
- [ ] 组织管理列表页顶部可见「返回工作台」按钮，样式与现有返回类按钮一致（`bt bt-o`）；
- [ ] 点击后正确返回工作台（有当前仓库→工作台；未绑定平台→仓库引导页）；
- [ ] 中英文 i18n 文案正确显示；
- [ ] 组织列表/详情/创建/编辑/删除/切换组织等现有功能不回归；
- [ ] 所有现有测试通过；前端静态页改动以手动回归清单记录验证结果（task.md）。

## Commands
- 测试：`python3 -m pytest tests/ -x -q`（后端回归，前端静态资源无自动化测试）
- 语法校验：`node --check src/api/static/js/pages/organizations.js`；`python3 -m json.tool` 校验两个 locale JSON
