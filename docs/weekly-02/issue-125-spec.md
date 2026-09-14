# Spec: Issue #125 — 仓库设置"解除托管"确认对话框优化

> Branch: feat/issue-125 | 2026-09-12

## 目标
将仓库设置中"解除托管"操作使用的浏览器原生 `confirm()` 替换为自定义居中模态框，复用项目已有 `.create-repo-overlay` / `.create-repo-modal` CSS 模式。

## Scope
### In
- 替换 `settings.js:265` `unhostFromSettings()` 中的 `confirm()` 调用
- 替换 `repos.js:455` `unhostRepo()` fallback 中的 `confirm()` 调用
- 新增通用确认对话框函数 `app.showConfirm(message, onConfirm, opts)`
- 新增 `app.closeConfirm()` 清理函数
- 新增 `.confirm-modal` CSS 变体（缩小尺寸 ~400px）
- 复用 `common.confirm` / `common.cancel` i18n keys（已存在于 inline pack）

### Out
- 后端 API 改动
- 其他非"解除托管"场景的 confirm 替换
- 模态框动画效果的新增（复用已有 `crFadeIn` / `crPopUp`）

## Technical Approach
1. **`app.showConfirm(message, onConfirm, opts)`** — 动态创建 `create-repo-overlay` + 内部 `.confirm-modal`（缩小变体 400px）
   - 内容：消息文本 + 取消/确认按钮
   - 点击确认 → 执行 onConfirm 回调 → 关闭弹窗
   - 点击取消/遮罩层 → 关闭弹窗
   - `document.body.style.overflow = 'hidden'` 防滚动，关闭时恢复
   - 先调用 closeConfirm 清理已有弹窗（防重复）
2. **`.confirm-modal` CSS** — `width:400px` 缩小变体，复用 overlay + 动画
3. **`unhostFromSettings()`** — `confirm()` 替换为 `app.showConfirm(msg, callback)`
4. **`unhostRepo()`** — fallback `confirm()` 替换为 `app.showConfirm(msg, callback)`

## Affected Modules
| 模块 | 改动 |
|------|------|
| `src/api/static/js/app.js` | 新增 `showConfirm` / `closeConfirm` |
| `src/api/static/js/pages/settings.js` | 修改 `unhostFromSettings` |
| `src/api/static/js/pages/repos.js` | 修改 `unhostRepo` fallback |
| `src/api/static/css/app.css` | 新增 `.confirm-modal` 样式 |
| `src/api/static/index.html` | bump v: app.js→71, settings.js→21, repos.js→39, app.css→50 |

## Acceptance Criteria
- [x] 点击"解除托管"后弹出自定义居中确认对话框（非原生 confirm）
- [x] 对话框水平+垂直居中
- [x] 尺寸适当缩小（~400px）
- [x] 含取消/确认按钮，功能与原 confirm 一致
- [x] settings.js 和 repos.js 两处均替换
- [x] 弹窗打开时禁止页面滚动，关闭时恢复
