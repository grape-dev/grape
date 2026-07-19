# DevOps Bot Web 管理台 — 设计文档

## 概述

DevOps Bot 需要一个可视化的管理界面，用于配置和管理 Bot 依赖的多账户信息。当前账户配置存储在 `configs/accounts.json` 文件中，包含 GitCode 凭据、飞书应用凭证、华为云 AK/SK、CodeArts CLI 密钥等。管理台让运营人员通过浏览器安全地查看和编辑这些配置。

## 设计目标

1. **配置可视化** — 通过 Web 界面管理多账户配置，替代手动编辑 JSON 文件
2. **账户隔离** — 支持多账户配置，每个飞书 App 独立 WebSocket 连接
3. **操作便捷** — 增删改查账户配置，测试 GitCode 连接有效性，分组展示字段
4. **安全可审计** — 密码登录 + OAuth 扫码登录，敏感字段点击显示/隐藏

## 用户角色

| 角色 | 权限 | 说明 |
|------|------|------|
| 管理员 | 全部 | 登录后可查看和编辑所有账户配置，添加/删除账户 |

## 登录认证

### 用户名密码登录

- 固定管理员账号：`developer` / `YOUR_SSH_PASSWORD`
- POST `/api/admin/login` 提交认证
- 登录成功后返回 Bearer token，有效期 24 小时
- 支持 JSON 和 Form-URL-Encoded 两种请求格式

### OAuth 扫码登录

- **GitCode OAuth** — 在 GitCode 注册 OAuth App，配置 `gitcode_oauth_client_id` 和 `gitcode_oauth_client_secret`
- 登录页显示 GitCode 登录按钮（仅当 OAuth 配置完成后显示）
- 流程：`/api/admin/oauth/gitcode` → 跳转 GitCode 授权 → 回调 `/api/admin/oauth/callback` → 交换 token → 验证用户 → 创建 admin session

## 功能模块

### 1. 账户管理

#### 账户列表（左侧边栏）

- 以列表形式展示所有已配置的账户（默认账户除外）
- 每行显示账户名称 + 配置状态指示灯：
  - 🟢 绿色：已配置
  - ⚫ 灰色：未配置
- 指示器依次为：GitCode Token、飞书 App、华为云 AK
- 支持点击切换查看账户详情
- 支持新增账户

#### 账户编辑（右侧表单，按组分栏）

**基础信息**
| 字段 | 说明 |
|------|------|
| 账户名称 | 账户唯一标识，新增可编辑，已有不可改 |
| Bot 用户名 | 逗号分隔的 Bot 账号列表 |

**GitCode**
| 字段 | 说明 | 敏感 |
|------|------|------|
| API Token | 调用 GitCode API 的 Personal Access Token | 是 |
| Webhook Token | 验证 Webhook 签名的 token | 是 |

**飞书**
| 字段 | 说明 | 敏感 |
|------|------|------|
| App ID | 飞书应用的 App ID | 是 |
| App Secret | 飞书应用的 App Secret | 是 |

**华为云**
| 字段 | 说明 | 敏感 |
|------|------|------|
| Access Key | 华为云 AK/SK 的 Access Key | 是 |
| Secret Key | 华为云 AK/SK 的 Secret Key | 是 |

**CodeArts**
| 字段 | 说明 | 敏感 |
|------|------|------|
| CLI Access Key | CodeArts CLI 的 AK | 是 |
| CLI Secret Key | CodeArts CLI 的 SK | 是 |

- 所有敏感字段旁有 👁 按钮，点击切换明文/密文显示
- 空字段与已填字段在视觉上区分
- 保存时 `bot_usernames` 自动从逗号分隔字符串转为数组

#### 操作

| 操作 | 说明 | 限制 |
|------|------|------|
| 保存 | 新增或更新账户配置到 `accounts.json` | 名称不能为空 |
| 测试连接 | 用账户的 GitCode Token 调用 `/user` API 验证有效性 | Token 为空时提示 |
| 删除 | 删除账户 | `default` 账户不可删除 |

### 2. 页面布局

```
┌──────────────────────────────────────────────────┐
│  DevOps Bot 管理台                     [退出]    │
├─────────────┬────────────────────────────────────┤
│ 账户列表    │  编辑 account-name                 │
│             │  ── 基础信息 ──                    │
│ account-a 🔵│  账户名称: [account-a]             │
│ ⚫⚫⚫      │  Bot 用户名: [hwc-dev-bot]         │
│ account-b 🔵│  ── GitCode ──                     │
│ 🟢⚫⚫      │  API Token:  [****] 👁             │
│ [+ 新增]    │  Webhook:     [****] 👁             │
│             │  ── 飞书 ──                        │
│             │  App ID:      [****] 👁             │
│             │  App Secret:  [****] 👁             │
│             │  ── 华为云 ──                      │
│             │  Access Key:  [****] 👁             │
│             │  Secret Key:  [****] 👁             │
│             │  ── CodeArts ──                    │
│             │  CLI AK:      [****] 👁             │
│             │  CLI SK:      [****] 👁             │
│             │                                    │
│             │  [保存] [测试连接] [删除]          │
└─────────────┴────────────────────────────────────┘
```

- **左侧边栏**：账户列表 + 状态指示灯，可点击切换
- **右侧主区域**：选中账户的编辑表单，按组分栏
- **顶部导航栏**：标题 + 退出按钮

### 3. 登录页布局

```
┌──────────────────────────────┐
│                              │
│       DevOps Bot             │
│       管理台登录             │
│                              │
│   [______________] 用户名    │
│   [______________] 密码      │
│                              │
│   [         登录        ]    │
│                              │
│   ───────── 或 ─────────     │
│                              │
│   [ ✓ GitCode 登录 ]         │
│                              │
└──────────────────────────────┘
```

- GitCode 登录按钮仅在 OAuth 配置就绪时显示
- 用户名密码登录始终可用

### 4. 响应式适配

- 桌面端（≥768px）：左侧列表 + 右侧编辑，两栏并排
- 移动端（<768px）：单栏布局，列表和编辑上下排列

## 页面流

```
浏览器 → /admin/ → 登录页面（两种方式）
  ├─ 方式 1：用户名密码登录
  │   POST /api/admin/login → 返回 token → 302 → 管理面板
  └─ 方式 2：GitCode OAuth 登录
      /api/admin/oauth/gitcode → GitCode 授权 → /api/admin/oauth/callback
        → 交换 token → 创建 admin session → 302 → 管理面板

管理面板
  ├─ 加载账户列表（左侧）
  ├─ 点击账户 → 加载详情（右侧表单，分组件）
  ├─ 修改字段 → 保存 → 更新 accounts.json + 重载
  ├─ 测试连接 → 显示结果
  ├─ 删除账户 → 确认 → 删除
  └─ 退出 → 返回登录页
```

## 数据模型

### 账户（Account）

```json
{
  "name": "default",
  "gitcode_api_token": "",
  "gitcode_webhook_token": "",
  "gitcode_oauth_client_id": "",
  "gitcode_oauth_client_secret": "",
  "feishu_app_id": "",
  "feishu_app_secret": "",
  "huawei_cloud_ak": "",
  "huawei_cloud_sk": "",
  "CODEARTS_CLI_AK": "",
  "CODEARTS_CLI_SK": "",
  "bot_usernames": []
}
```

| 字段 | 说明 |
|------|------|
| `name` | 账户唯一标识，`default` 为保留账户不可删除 |
| `gitcode_api_token` | GitCode Personal Access Token |
| `gitcode_webhook_token` | Webhook 验证 token |
| `gitcode_oauth_client_id` | GitCode OAuth App Client ID（可选，用于扫码登录） |
| `gitcode_oauth_client_secret` | GitCode OAuth App Client Secret（可选） |
| `feishu_app_id` / `feishu_app_secret` | 飞书应用凭证 |
| `huawei_cloud_ak` / `huawei_cloud_sk` | 华为云 AK/SK |
| `CODEARTS_CLI_AK` / `CODEARTS_CLI_SK` | CodeArts CLI 认证密钥 |
| `bot_usernames` | Bot 账号名数组（用于自循环检测） |

## API 端点

| 方法 | 路径 | 说明 | 认证 |
|------|------|------|------|
| POST | `/api/admin/login` | 用户名密码登录 | 否 |
| GET | `/api/admin/oauth/gitcode` | 跳转 GitCode OAuth 授权 | 否 |
| GET | `/api/admin/oauth/callback` | OAuth 回调，交换 token | 否 |
| GET | `/api/admin/accounts` | 获取账户列表（脱敏） | Bearer |
| GET | `/api/admin/accounts/<name>` | 获取账户详情 | Bearer |
| PUT | `/api/admin/accounts/<name>` | 创建/更新账户 | Bearer |
| DELETE | `/api/admin/accounts/<name>` | 删除账户 | Bearer |
| POST | `/api/admin/accounts/<name>/test` | 测试 GitCode 连接 | Bearer |

所有 `/api/admin/accounts/*` 端点需要 `Authorization: Bearer <token>` 请求头。

## 技术实现

| 组件 | 技术 |
|------|------|
| 后端 | Python `http.server`（端口 80），内嵌在 `src/main.py` 线程中 |
| 前端 | Vue.js 3 CDN 自托管（`/admin/vue.min.js`） |
| 数据存储 | `configs/accounts.json`，通过 `save_accounts()` 写回并重载 |
| 会话 | 内存中的 `_admin_tokens` 字典，SHA256 token，24h 过期 |

## 安全措施

- 登录令牌 24h 过期
- 敏感字段前端 `type=password`，后端不输出明文到列表 API
- OAuth 回调使用 HTTPS（生产环境）或 HTTP（内网）
- 所有 POST/PUT 请求有 JSON 解析校验
- Admin server 启动失败自动重试（5s 间隔）
