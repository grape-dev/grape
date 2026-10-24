# Plan: Issue #407 — 页签闲置返回整页重载导致编辑内容丢失

## 根因
1. `app.js` visibilitychange 处理器：页签隐藏 >60s 后切回前台时，无条件执行 `app.nav(_baseView)` 重新渲染整个视图，导致编辑中的表单内容丢失。
2. `dashboard.js` 编辑模式（添加场景/场景编辑）仅用内存标志（`window._dashboardEditing`/`_isCreateMode`）跟踪，无持久化，刷新即丢。

## 修复方案
1. **visibilitychange 守卫**：编辑态下（`_dashboardEditing=true`）跳过 `app.nav()` 重渲染，仅保留 `scheduleProactiveRefresh()` 静默续期。
2. **编辑态持久化**：进入编辑模式时写入 sessionStorage（`dashEditMode`），dashboard/场景详情渲染后检测并自动恢复。
3. **草稿持久化**：表单字段（场景名称/描述/图标/节点配置/PR订阅）变更时自动写入 sessionStorage 草稿，恢复时回填；保存/取消/返回时清除。

## 改动文件
| 文件 | 改动 |
|------|------|
| `src/api/static/js/app.js` | visibilitychange 增加 `!_dashboardEditing` 守卫 |
| `src/api/static/js/pages/dashboard.js` | 草稿/编辑态 helper、创建模式草稿、编辑模式草稿、自动恢复、退出清除 |

## 验收对照
- [x] 闲置返回不重渲染（visibilitychange 守卫）
- [x] F5 刷新后恢复编辑界面并回填草稿（sessionStorage 自动恢复）
- [x] 保存/返回后草稿清除（exit 点 clearDraft）
- [x] 编辑态下 30s 轮询不覆盖 UI（既有 `_dashboardEditing` 守卫覆盖）
