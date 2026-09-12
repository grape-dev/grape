# Plan: Issue #125

## Tasks
1. [x] app.js: 新增 `showConfirm(message, onConfirm, opts)` + `closeConfirm()`
2. [x] app.css: 新增 `.confirm-modal` 样式（400px 缩小变体）
3. [x] settings.js: `unhostFromSettings` 改用 `app.showConfirm`
4. [x] repos.js: `unhostRepo` fallback 改用 `app.showConfirm`
5. [x] index.html: bump cache 版本号
6. [x] 自审查 + 提交 PR
