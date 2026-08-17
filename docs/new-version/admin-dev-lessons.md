# Admin 管理台开发教训总结

## 概述

本文档记录了 DevOps Bot 管理台（Web 管理面板）开发过程中遇到的所有问题、诊断过程和最终解决方案。供后续开发参考，避免同类问题。

## 问题总览

| # | 问题 | 根因 | 解决 |
|---|------|------|------|
| 1 | 自托管 Vue.js 加载失败 | 路径匹配 `?v=2` 查询参数 | 删除外部 JS 依赖 |
| 2 | `fetch()` Promise 永久 pend | Chrome 对同源 POST 限制 | 放弃 fetch，用纯 form |
| 3 | `XMLHttpRequest` 回调不触发 | 同上 | 放弃 XHR |
| 4 | 登录后 302 跳转不工作 | 前端 JS 无法正确跟随 | 登录 POST 直接返回新页面 |
| 5 | API 返回 401 | token 类变量跨线程丢失 | 不要用 `_load_tokens()` 覆盖类变量 |
| 6 | 页面渲染卡死 | 重复的 `class AdminHandler` | 检查 git 差异，避免重复定义 |
| 7 | meta refresh 不工作 | Chrome 不信任 HTTP 页面 | 直接返回页面，零跳转 |
| 8 | `renderForm()` 不渲染 | `esc()` 对数组类型报错 | 用 `String()` 包装 |

## 关键教训

### 1. 登录鉴权：post-重定向-get 模式有隐患

**现象**：HTML form POST → 服务端 302 跳转 → 面板页面。在 Chrome 中 302 不触发。

**根因**：Chrome 对同源 HTTP 的 302 跳转有时不处理（安全策略相关）。

**解决方案**：
```
POST /admin/login  →  直接 return 200 + HTML
```
登录 POST 的响应就是面板页面本身。不做跳转，不做 meta refresh。浏览器渲染返回的 HTML 即可。

### 2. 自托管 JS 库不可靠

**现象**：`<script src="/admin/vue.min.js">` 在 Chrome 中加载失败。

**根因**：服务端路径匹配未处理查询参数（`?v=2`）。自托管文件受 Chrome MIME 严格检查影响。

**解决方案**：不用外部 JS 库，所有 CSS 和 JS 内联在 Python 服务端生成的 HTML 中。

### 3. `fetch()` 和 `XMLHttpRequest` 在 Chrome 中不可靠

**现象**：`fetch()` 的 Promise 永不 resolve，`XHR` 的 `onload` 回调永不触发。

**触发条件**：使用 `Content-Type: application/json` 的 POST 请求。

**状态**：原因待查明（可能与 Chrome 安全策略、HTTP 页面限制、或企业策略有关）。

**解决方案**：
- 关键操作（登录、保存、删除）用纯 `<form method="POST" action="...">` 提交
- 导航用 `<a>` 链接
- 不依赖异步 JS 做数据交互
- 服务端在同一个 HTTP 响应中渲染完整页面

### 4. 类变量 vs 模块变量

**现象**：`_admin_tokens` 在登录时存入，在后续请求中读取为 `{}` / 401。

**根因**：
- `_load_tokens()` 用 `json.load(f)` 返回的新 `dict` 覆盖了类变量，导致已存入的 key 丢失
- 重复的 `class AdminHandler` 定义导致 `_tokens = {}` 被覆盖

**解决方案**：
- 只用类级别 `_tokens = {}`，不做文件 I/O
- `_auth()` 直接查 `self._tokens`（实际上是 `type(self)._tokens`），不经过文件加载
- `login()` 直接写 `type(self)._tokens[key] = value`
- 删除 `_load_tokens()`、`_save_tokens()` 方法

### 5. HTML 表单提交是唯一可靠的方式

**经验**：在所有尝试过的前端交互方式中，唯一 100% 可靠的是：
```
<form method="POST" action="/path">
  <input name="...">
  <button type="submit">
</form>
```

适用于：登录、保存、删除。浏览器原生处理，不受 JavaScript 错误、安全策略、浏览器版本影响。

### 6. 服务端渲染 > 客户端渲染

**结论**：对于管理面板这类工具型页面，服务端渲染比 SPA/客户端渲染更适合：

| 维度 | 服务端渲染 | 客户端渲染（SPA） |
|------|-----------|----------------|
| JavaScript 依赖 | 无 | 必须 |
| SEO | 无关 | 无关 |
| 首次渲染速度 | 快 | 需要加载 JS 框架 |
| 浏览器兼容性 | 全部 | 受 JS 加载影响 |
| 开发复杂度 | 中等（Python 生成 HTML） | 高（前后端分离） |

### 7. 不要过度工程化

**问题**：从一个简单的登录表单开始，慢慢叠加了 Vue.js、fetch API、文件持久化、meta refresh 等方案，每个都引入了新的问题。

**教训**：管理面板的核心需求就是 CRUD，用 90 年代的方式（HTML form + 服务端渲染）就能解决。不要预先引入框架。

## 推荐的开发模式

```
1. 先写纯 HTML form → 验证核心流程（登录 + 保存 + 删除）
2. 加 CSS → 视觉优化
3. 加内联 JavaScript → 非关键增强（密码显示切换）
4. 如需复杂交互 → 优先考虑页面导航（链接）而非 AJAX
5. 坚持：关键路径零外部依赖
```

## 当前技术栈

| 组件 | 选型 |
|------|------|
| Web 框架 | Python `http.server`（内嵌） |
| 前端 | 服务端渲染 HTML + 内联 CSS |
| JavaScript | 内联 `<script>`，仅用于 👁 切换 |
| 外部依赖 | 无（0 个外部 JS/CSS 文件） |
| 认证 | 内存 token（类变量 dict） |
| 页面切换 | 链接 + 表单提交（无 AJAX 导航） |
