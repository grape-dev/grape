# 项目列表页/引导页语言切换局部刷新设计方案

## 问题

项目列表页（repos/lobby）和引导页（binding guide）在语言切换时表现如同全局刷新：
- loading 遮罩闪烁
- 发送 /repos/member API 请求
- 整个 DOM 重建

## 根因

`_rerenderCurrentPage()` 对 repos 视图调用 `app.nav('repos')` → `goToRepos()` → 完整流程（API 请求 + loading 遮罩 + DOM 重建）。

引导页/项目列表页 348 处文案使用 JS 动态拼接（I18N.t），仅 7 处 data-i18n 标记。`applyI18nToDOM()` 只能更新 7 个标记元素，其余必须重新渲染。

## 方案

缓存 /repos/member 数据，语言切换时用缓存数据重新调 renderRepoSelection，不发 API 请求、不闪烁 loading 遮罩。

### 改动点

1. app.js: 新增 `_cachedReposData` 变量，goToRepos 成功时缓存数据
2. app.js: 新增 `_rerenderReposView()` 轻量重渲染函数
3. i18n.js: `_rerenderCurrentPage` 对 repos 视图走 `_rerenderReposView` 而非 `app.nav`
4. index.html: bump i18n.js v=20261020-1, app.js v=178

### 验收标准

- 语言切换时不发 /repos/member API 请求
- 语言切换时不显示 loading 遮罩
- 引导页文案切换语言后即时更新
- 项目列表页文案切换语言后即时更新
- 无缓存数据时 fallback 到完整 goToRepos
