# Chrome 浏览器兼容性注意事项

## 背景

在 DevOps Bot 管理台开发过程中，多次遇到 Chrome 桌面版无法正常加载页面或执行 JavaScript 的问题，而移动端浏览器（Safari、Chrome Mobile）均正常工作。以下是已验证的问题和解决方案汇总。

## 已验证的问题

### 1. 自托管 JS 文件加载失败

**现象**：`<script src="/admin/vue.min.js">` 在 Chrome 中不加载，Vue 应用无法初始化。页面显示"Vue 未加载，使用静态表单"。

**原因**：
- Chrome 对自托管脚本的缓存策略非常激进
- 带查询参数（`?v=2`）的脚本路径曾导致服务端路径匹配失败
- chrome/Chromium 对 `application/javascript` 的 MIME 校验可能比其他浏览器更严格
- 首次加载失败后，Chrome 会缓存 404 结果，后续刷新也不重新请求

**解决方案**：
- **不使用外部 JS 依赖**，全部用纯 HTML + CSS + 内联 `<script>` 实现
- 如需框架，使用 CDN 而非自托管

### 2. `fetch()` Promise 永久 pend

**现象**：`fetch('/api/admin/login', {method:'POST', headers:{'Content-Type':'application/json'}, ...})` 发送请求后，Promise 既不 resolve 也不 reject，永远停留在 pending 状态。按钮显示"登录中…"且不会恢复。

**影响**：这是最严重的问题——没有错误提示，用户无法判断是否成功。

**排除过程**：
1. 服务端正常响应（curl 测试通过，移动端 AJAX 登录正常）
2. `Content-Type` 和 CORS 头均正确
3. 同源请求，不涉及跨域
4. 改用 JSON 之外的 `Content-Type` 无效
5. 改用 `XMLHttpRequest` 同样无效（`onload` 不触发）

**原因**：Chrome 对同源异步 POST 请求（Content-Type: application/json）存在间歇性不执行的问题。可能是浏览器扩展、安全策略、或特定内核版本有关。

**解决方案**：
- **登录与鉴权不依赖 `fetch()` 或 `XMLHttpRequest`**
- 使用纯 HTML `<form method="POST" action="/api/...">` + 服务端 302 跳转
- 浏览器原生的 form 提交不受 Chrome 该限制影响

### 3. `XMLHttpRequest` 同样不可靠

**现象**：改用 `XMLHttpRequest` 后，`onload`、`onreadystatechange` 回调均不触发。

**结论**：问题不在 API 调用方式，而在 Chrome 对该类请求本身存在限制。具体原因待查（可能与 `Content-Type: application/json` 相关，也可能与同策略异步请求相关）。

**解决方案**：同问题 2。

### 4. IIFE 作用域导致事件处理器无法访问

**现象**：函数定义在 `(function(){ ... })()` 闭包中，HTML 中的 `onclick="fn()"` 报错 `fn is not defined`。

**原因**：IIFE 创建独立作用域，函数只在闭包内可见。HTML 属性中的事件处理器（`onclick`、`onsubmit` 等）要求函数在全局作用域。

**解决方案**：
- 将所有事件处理函数定义为全局函数（不用 IIFE 包裹）
- 如需模块化，将函数挂在 `window.fn = function(){}`
- 更推荐使用 `addEventListener` 绑定事件（不在 HTML 属性中写 `onclick`）

## 最佳实践清单

### 登录与鉴权

| ❌ 不要 | ✅ 要 |
|--------|------|
| 用 `fetch()` 或 XHR 处理登录 | 用 `<form method="POST">` + 服务端 302 跳转 |
| 在前端通过 JS 切换显示/隐藏来"跳转" | 让浏览器自然跟随 302 跳转到目标页面 |

### 前端架构

| ❌ 不要 | ✅ 要 |
|--------|------|
| 依赖自托管大型 JS 框架（Vue, React） | 纯 HTML + CSS + 内联 JavaScript |
| 在 HTML 属性中 `onclick="fn()"` + IIFE 包裹 | 函数全部全局作用域 或 `addEventListener` |
| 多个外部 `<script src="...">` 文件 | 所有 JS 内联在单个 `<script>` 标签中 |

### 表单提交

| ❌ 不要 | ✅ 要 |
|--------|------|
| `onsubmit` 用 JS 拦截 + `preventDefault` + AJAX | `<form method="POST" action="/api/...">` 原生提交 |
| 登录成功后在当前页面 JS 切换状态 | 服务端 302 跳转到已登录页面 |

## 建议的开发流程

1. **先做 HTML 原型**：用 `<form>` + 302 跳转实现核心流程，在 Chrome 中验证
2. **再逐步加 JS**：仅用于非关键功能（列表交互、表单编辑、Toast 提示）
3. **每次改动后在 Chrome 中测试**：不要假设 JS 会正常执行
4. **关键路径保留纯 HTML 兜底**：登录、退出等必须在不依赖 JS 的情况下正常工作

## 服务端配合示例

```python
def do_POST(self):
    if self.path == "/api/admin/login":
        ctype = self.headers.get("Content-Type", "")
        # 支持 JSON（XHR）和 form-urlencoded（HTML form）两种提交
        if "application/json" in ctype:
            data = json.loads(body)
            username = data.get("username", "")
            password = data.get("password", "")
        else:
            params = urllib.parse.parse_qs(body.decode())
            username = params.get("username", [""])[0]
            password = params.get("password", [""])[0]

        if username == ADMIN_USER and password == ADMIN_PASS:
            token = generate_token()
            if "application/json" in ctype:
                self._json(200, {"token": token})   # AJAX 响应
            else:
                self.send_response(302)               # HTML form → 跳转
                self.send_header("Location", f"/admin/panel?token={token}")
        else:
            if "application/json" in ctype:
                self._json(401, {"error": "invalid"})
            else:
                self.send_response(302)               # 失败也跳转，带 error 参数
                self.send_header("Location", "/admin/?error=1")
```

## 相关文档

- `docs/admin-web-console-design.md` — 管理台设计文档
- `src/admin/page.html` — 当前实现（纯 HTML + 内联 JS）
- `src/admin/server.py` — 服务端（支持 JSON 和 form 两种登录方式）
