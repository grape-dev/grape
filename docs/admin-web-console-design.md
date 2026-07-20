# DevOps Bot Web 管理台 — 设计文档

## 概述

DevOps Bot 管理台让运营人员通过浏览器安全地查看和编辑多账户配置（GitCode 凭据、飞书应用凭证、华为云 AK/SK、CodeArts CLI 密钥等）。

## 架构

- **后端**：Python `http.server`（端口 80），内嵌在 `src/main.py` 线程中
- **前端**：服务端渲染 HTML + 内联 CSS + 少量内联 JavaScript
- **数据存储**：`configs/accounts.json`，通过 `save_accounts()` 写回并重载
- **会话**：类级 `_tokens` 字典，SHA256 token，24h 过期

## 页面流

```
浏览器 → /admin/ → 登录页面（HTML form）
  ↓ POST /admin/login（form-urlencoded）
  ↓ 服务端验证
  ↓ 返回管理面板页面（200 OK，直接渲染，无跳转）

管理面板
  ├─ 账户列表（左侧，链接导航）
  ├─ 点击账户 → /admin?token=xxx&select=name（服务端渲染表单）
  ├─ 编辑字段 → POST /admin/save?token=xxx → 保存并返回面板
  ├─ 测试连接 → /admin/test?token=xxx&name=xxx → 返回面板+结果
  ├─ 删除账户 → /admin/delete?token=xxx&name=xxx → 删除并返回面板
  └─ 退出 → /admin/ → 返回登录页
```

## 登录认证

- 固定管理员账号：`developer` / `YOUR_SSH_PASSWORD`
- POST `/admin/login`（form-urlencoded）
- 验证通过后直接返回管理面板 HTML（非 302 跳转）
- 所有面板链接和表单中嵌入 `?token=xxx`

## 功能模块

### 账户列表（左侧边栏）

- 链接列表，每行显示账户名称 + 配置状态指示灯（🟢/⚫）
- 指示器依次为：GitCode Token、飞书 App、华为云 AK
- 支持新增（通过 `?select=__new__` 参数）

### 账户编辑（右侧表单，按组分栏）

| 分组 | 字段 | 敏感 |
|------|------|------|
| 基础信息 | 账户名称、Bot 用户名 | 否 |
| GitCode | API Token、Webhook Token | 是 |
| 飞书 | App ID、App Secret | 是 |
| 华为云 | Access Key、Secret Key | 是 |
| CodeArts | CLI Access Key、CLI Secret Key | 是 |

- 敏感字段默认 `type=password`，有 👁 切换按钮
- 保存时 `bot_usernames` 自动从逗号分隔字符串转为数组

### 操作

| 操作 | 说明 |
|------|------|
| 保存 | form POST 提交，服务端写 `accounts.json` + 重载 |
| 测试连接 | 用 GitCode Token 调用 `/user` API 验证有效性 |
| 删除 | `default` 账户不可删除 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/login` | HTML 表单登录，返回面板页面 |
| GET | `/admin/` | 登录页或面板页（取决于是否有 token） |
| GET | `/admin?token=xxx&select=name` | 面板页（选中账户） |
| POST | `/admin/save?token=xxx` | 保存账户 |
| GET | `/admin/delete?token=xxx&name=xxx` | 删除账户 |
| GET | `/admin/test?token=xxx&name=xxx` | 测试连接 |
| GET | `/api/admin/accounts` | 账户列表（JSON，Bearer 认证） |
| GET | `/api/admin/accounts/<name>` | 账户详情（JSON，Bearer 认证） |

## 技术要点

- 零外部依赖：无 Vue.js、无 CDN、无外部 JS 文件
- 无 302 跳转：登录 POST 直接返回新页面
- 纯表单提交：所有写操作通过 `<form method="POST">` 完成
- 所有链接和表单带 `token` 参数，服务端验证
- 安全字段服务端不输出明文到列表 API
- Admin server 启动失败自动重试（5s 间隔）
