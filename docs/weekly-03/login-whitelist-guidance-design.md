# 未加白名单用户登录引导方案

## 1. 问题

当前未加白名单用户登录时：
- GitCode：后端 `raise HTTPException(403)`，浏览器直接看到 JSON 错误页
- GitHub：后端重定向到 `/login?oauth_error=xxx`，前端只显示一个 toast 提示
- 两种方式都只说"联系统管理员"，没有引导到内测申请页面

## 2. 方案

### 2.1 后端统一：白名单不通过时重定向到 /login?whitelist=1

GitCode 和 GitHub 的白名单校验不通过时，统一用重定向到 `/login?whitelist=1&platform=gitcode|github&username=xxx`，不再 raise 403 JSON。

**auth.py 改动：**

GitCode 白名单校验（第 190 行）：
```python
# 之前：raise HTTPException(403, ...)
# 之后：
return _oauth_error_redirect(request, f"WHITELIST:{gitcode_login}")
```

GitHub 白名单校验（第 349 行）已经用 `_oauth_error_redirect`，只需改消息格式：
```python
# 之前：return _oauth_error_redirect(request, "您的 GitHub 账号不在白名单内...")
# 之后：
return _oauth_error_redirect(request, f"WHITELIST:{github_login}")
```

### 2.2 前端：检测 whitelist 参数显示弹窗

app.js 中 `init()` 函数检测 URL 参数：
- `oauth_error` 包含 `WHITELIST:` 前缀时 → 显示白名单引导弹窗（不是 toast）
- 其他 `oauth_error` → 保持原有 toast 行为

### 2.3 白名单引导弹窗设计

```
┌──────────────────────────────────────────────┐
│                                              │
│        🔒                                    │
│                                              │
│   账号尚未开通内测权限                        │
│                                              │
│   您的 GitCode/GitHub 账号尚未获得内测权限。  │
│   请先申请内测，审核通过后即可登录。          │
│                                              │
│           [申请内测]    [关闭]                │
│                                              │
└──────────────────────────────────────────────┘
```

- 居中模态弹窗，半透明遮罩
- 标题图标 🔒 + "账号尚未开通内测权限"
- 说明文字
- "申请内测"按钮 → 跳转到 `/static/beta-apply.html`
- "关闭"按钮 → 关闭弹窗

### 2.4 密码登录的白名单校验

密码登录（`/auth/login`）也可能返回 403 白名单错误。前端 `pwdLogin()` 函数 catch 403 时也显示同样的弹窗。

## 3. 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/auth.py | GitCode 白名单不通过改用 redirect（不再 raise 403）；GitHub 改消息格式为 WHITELIST: 前缀 |
| src/api/static/js/app.js | init() 检测 WHITELIST: 前缀显示弹窗；pwdLogin() catch 403 显示弹窗 |

## 4. 不改动的部分

- `_oauth_error_redirect` 函数本身不改（已经能把消息编码到 URL 参数）
- 前端 toast 逻辑保留（非白名单错误仍用 toast）
- beta-apply.html 不改（已经有完整的申请表单）
