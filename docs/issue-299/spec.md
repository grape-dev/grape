# Spec: Issue #299 — 优化工作台我的组织展示为当前已选空间名称+下拉切换

## Objective
- 将工作台"我的组织"区域从静态平铺展示改为显示当前已选空间名称 + 可下拉选择切换空间

## Scope
### In Scope
- 顶部工具栏（tbOrgBtn）"我的组织"按钮显示当前已选空间名称（而非固定文本"我的组织"）
- 下拉面板复用现有 `_loadTbOrgs()` 逻辑（列出全部可访问空间）
- 切换空间后更新按钮显示名称（`_updateTbOrgName` 实现填充逻辑）
- 下拉样式遵循统一设计规范（0.5px border、rounded-lg、SF Pro）

### Out of Scope
- 组织管理（创建/删除/编辑组织）
- 后端组织数据结构变更
- lobby 页面组织下拉（已实现 `_updateLobbyOrgName`，本次不改）

## Technical Approach
1. **index.html**：tbOrgBtn 按钮文本改为动态 span（id="tbOrgName"），移除固定 i18n 文本
2. **app.js `_updateTbOrgName()`**：实现为读取当前 org name 并填充到 `#tbOrgName`
3. **app.js `switchOrg()`**：切换后调用 `_updateTbOrgName()`（已有调用，只需实现函数）
4. 下拉面板样式微调：0.5px border、rounded-lg

## Affected Modules
| 模块 | 改动类型 | 说明 |
|------|---------|------|
| src/api/static/index.html | 修改 | tbOrgBtn 按钮文本改为动态 span |
| src/api/static/js/app.js | 修改 | `_updateTbOrgName()` 实现填充逻辑 |

## Acceptance Criteria
- [ ] 顶部工具栏组织区域默认显示当前已选空间名称
- [ ] 点击展开下拉列表，列出全部可访问空间
- [ ] 选择空间后正确切换上下文并更新显示名称
- [ ] 样式符合统一设计规范
