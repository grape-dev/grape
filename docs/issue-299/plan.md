# Plan: Issue #299 — 优化工作台我的组织展示

## 现状分析
工作台顶部工具栏 `tbOrgBtn` 当前显示固定文本"我的组织"（`data-i18n="organizations.my_orgs"`），
`_updateTbOrgName()` 为空实现（注释："无需更新按钮文字 — 固定显示'我的组织'"）。

用户无法直观看到当前所选空间名称，需要改为显示当前已选空间名称。

## 改动方案

### 1. index.html — tbOrgBtn 按钮文本改为动态 span
- 当前：`🏢 <span data-i18n="organizations.my_orgs">我的组织</span>`
- 改为：`🏢 <span id="tbOrgName" data-i18n="organizations.my_orgs">我的组织</span> <svg chevron>`
- 添加下拉箭头图标（与 lobby 页面一致）
- 样式微调：0.5px border、rounded-lg

### 2. app.js — `_updateTbOrgName()` 实现填充逻辑
- 读取 `localStorage.getItem('current_org_id')`
- 调用 `API.get('/orgs')` 获取组织列表
- 找到当前 org，填充 name 到 `#tbOrgName`
- 找不到时回退显示 i18n "organizations.my_orgs"

### 3. app.js — `switchOrg()` 已有 `_updateTbOrgName()` 调用（line 464），无需改动
### 4. app.js — `loadProfile()` 已有 `_updateTbOrgName()` 调用（line 193），无需改动

### 5. tbOrgMenu 下拉面板样式微调
- border-radius: 10px → rounded-lg（12px，与 Apple theme 一致）
- border: 1px → 0.5px（Apple hairline）

## 风险
- 无后端改动，纯前端 DOM/CSS 改造
- `_updateTbOrgName` 新增 API 调用，但 `loadProfile` 已调 `/orgs`，可复用缓存
