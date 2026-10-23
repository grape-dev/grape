# Plan: Issue #456 — 组织管理页面增加返回工作台按钮

## 方案概述
纯前端静态资源改动（3 个文件），复用 `app.js` 既有导出 `app._accountGoBack()`，无新增模块间调用、无后端改动。

## 改动清单

| # | 文件 | 改动 | 依赖 |
|---|------|------|------|
| 1 | `src/api/static/locales/zh/organizations.json` | 顶层新增 `back_to_workspace`: "← 返回工作台" | 无（先行，避免 i18n 缺 key 显示裸 key） |
| 2 | `src/api/static/locales/en/organizations.json` | 顶层新增 `back_to_workspace`: "← Back to Workspace" | 无 |
| 3 | `src/api/static/js/pages/organizations.js` | `renderOrganizations()` 头部按钮容器（`display:flex;gap:12px`）内、「创建组织」按钮之前插入返回按钮：`class="bt bt-o"`，`onclick="app._accountGoBack()"`，文案 `I18N.t("organizations.back_to_workspace")` | 1、2 的 key 已存在；`app._accountGoBack` 已在 app.js return 导出（app.js:2797） |

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 返回逻辑 | 直接调用 `app._accountGoBack()` | 该函数已处理侧边栏恢复 + 未绑定守卫 + dash/repos 分流（app.js:560-575），组织管理页与用户设置页同为隐藏侧边栏的非仓库视图，行为语义一致；复制守卫逻辑会造成重复 |
| 文案 key | `organizations.json` 顶层新增 `back_to_workspace` | 组织页文案归属 organizations 命名空间便于维护；`app.account.back_to_workspace_btn` 英文现值为 "← Back" 语义偏弱，organizations 侧补全称（Issue Agent Spec 决策） |
| 按钮位置 | 头部按钮容器内、「创建组织」左侧 | 与 Spec「与创建组织同排」一致；视觉上次要操作（返回）置于主操作（创建）之前 |
| 按钮样式 | `class="bt bt-o"` + 内联尺寸对齐现有按钮 | `bt/bt-o` 类在 app.css 全局定义（透明背景+边框），account.js:31 用户设置页同款；字号/内边距对齐本页「创建组织」按钮（13px/8px 16px）保证同排视觉一致 |

## 影响面分析
- `renderOrganizations()` 仅被 `app.nav('organizations')` 分支调用（app.js:1535），改动不影响详情页 `_showOrgDetail()`（其自有 `detail.back` 返回链接不变）；
- 新增按钮不绑定任何新事件/接口，`_accountGoBack` 为既有导出；
- locale 新增 key 不影响既有 key（JSON 顶层追加）。

## 风险与缓解
| 风险 | 缓解 |
|------|------|
| 按钮误加到详情页 | 仅改 `renderOrganizations()`，`_showOrgDetail` 不动（自审查核对） |
| 未绑定用户点击后 403 | 复用 `_accountGoBack` 守卫（未绑定 → 引导页） |

## 测试计划
1. 自动化：`node --check` JS 语法；`python3 -m json.tool` 校验两个 locale JSON；`python3 -m pytest tests/ -x -q` 后端全量回归（确认无意外破坏）；
2. 手动回归清单（task.md 记录）：列表页按钮可见/点击分流正确（有仓库/未绑定两种场景）、中英文切换文案、组织 CRUD/切换不回归。
