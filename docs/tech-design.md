# Agentic DevOps 技术设计文档

> 基于 `system-workflow-overview.md`（使用场景）和 `prototype.html`（高保真原型）的技术实现方案。

---

## 一、技术栈

| 层次 | 技术选型 | 说明 |
|------|---------|------|
| **前端** | Vanilla JS SPA | 纯 HTML/CSS/JS，无框架依赖，基于原型 HTML 演进 |
| **后端 API** | Python FastAPI | 端口 3000，JSON REST API，替换原有 HTML 渲染 |
| **事件处理** | Python http.server + 线程池 | 端口 8080 Webhook + 端口 80 管理面板（保留） |
| **Agent 执行** | opencode / codearts CLI | NDJSON 流式输出，子进程管理 |
| **数据库** | SQLite via `sqlite3` | `data/sessions.db`，单文件部署 |
| **IM 集成** | lark-oapi WebSocket | 飞书长连接 |
| **容器化** | systemd service | `/usr/bin/python3 -m src.main` |

---

## 二、系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                        Nginx / Direct                        │
│                  :80 (Admin)  :3000 (API v2)                 │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─ Frontend SPA ──────────────────────────────────────────┐ │
│  │  docs/ui-demo/prototype.html → src/v2/web/               │ │
│  │  ┌─────────┐ ┌──────────┐ ┌───────┐ ┌────────┐          │ │
│  │  │ Login   │ │ Dashboard│ │Pipeline│ │ Agent  │  ...     │ │
│  │  │ Page    │ │ (per-repo│ │ Page   │ │ Detail │          │ │
│  │  └─────────┘ └──────────┘ └───────┘ └────────┘          │ │
│  └──────────────────────────────────────────────────────────┘ │
│                              │ fetch()                        │
│                              ▼                                │
│  ┌─ v2 API Server (FastAPI :3000) ──────────────────────────┐ │
│  │  /api/v2/auth/*     /api/v2/repos/*                       │ │
│  │  /api/v2/dashboard/*  /api/v2/agents/*                    │ │
│  │  /api/v2/chat/*       /api/v2/settings/*                  │ │
│  └──────────────────────────────────────────────────────────┘ │
│                              │                                │
│  ┌─ Legacy Services ────────────────────────────────────────┐ │
│  │  :80  admin/server.py   (账户管理, OAuth)                 │ │
│  │  :8080 webhook_handler  (Webhook, A2A, Health)            │ │
│  │  WS    feishu.py        (飞书 IM)                         │ │
│  └──────────────────────────────────────────────────────────┘ │
│                              │                                │
│  ┌─ Core Engine ────────────────────────────────────────────┐ │
│  │  event_processor.py  →  prompt_builder.py  →  executor.py │ │
│  │  session_manager.py  →  config.py  →  vcs/adapter.py     │ │
│  └──────────────────────────────────────────────────────────┘ │
│                              │                                │
│  ┌─ Data Layer ─────────────────────────────────────────────┐ │
│  │  data/sessions.db (SQLite)                                │ │
│  │  skills/ (文件系统)  ·  configs/ (JSON)                   │ │
│  └──────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

---

## 三、数据库设计

### 3.1 user_accounts（核心用户表）

```sql
CREATE TABLE user_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    gitcode_id TEXT DEFAULT '',
    gitcode_api_token TEXT DEFAULT '',
    gitcode_webhook_token TEXT DEFAULT '',
    gitcode_oauth_client_id TEXT DEFAULT '',
    gitcode_oauth_client_secret TEXT DEFAULT '',
    gitcode_oauth_redirect_uri TEXT DEFAULT '',
    feishu_app_id TEXT DEFAULT '',
    feishu_app_secret TEXT DEFAULT '',
    huawei_cloud_ak TEXT DEFAULT '',
    huawei_cloud_sk TEXT DEFAULT '',
    CODEARTS_CLI_AK TEXT DEFAULT '',
    CODEARTS_CLI_SK TEXT DEFAULT '',
    password TEXT DEFAULT '',
    bot_usernames TEXT DEFAULT '',
    webhook_url TEXT DEFAULT '',
    created_by INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
```

### 3.2 关联表

| 表 | 用途 | 外键 |
|----|------|------|
| `managed_repos` | 托管仓库记录 + webhook_id | `user_id` → `user_accounts.id` |
| `user_vms` | 云主机配置 | `user_id` → `user_accounts.id` |
| `user_vm_repos` | 云主机-仓库映射 | `vm_id` → `user_vms.id` |
| `user_skills` | 用户自定义技能索引 | `user_id` → `user_accounts.id` |
| `repo_skill_mapping` | 仓库-事件-技能映射 | `user_id` → `user_accounts.id` |
| `user_credentials` | OAuth 凭据存储 | `user_id` → `user_accounts.id` |

### 3.3 前端状态（内存）

前端 SPA 不持久化状态到 DB 的配置项：

| 数据 | 存储位置 | 生命周期 |
|------|---------|---------|
| `auth_token` | `localStorage` | OAuth 登录后设置 |
| `current_repo` | URL hash / `sessionStorage` | 切换仓库时更新 |
| 通知偏好 | API 读写 DB | 持久化 |
| Agent Backend 选择 | API 读写 DB | 持久化 |
| 侧边栏展开状态 | `localStorage` | 浏览器会话 |

---

## 四、API 设计

### 4.1 认证模型

```
Authorization: Bearer <token>

token 来源:
  - OAuth: POST /api/v2/auth/callback/gitcode → {token}
  - 密码: POST /api/v2/auth/login → {token}
  - 有效期: 24h（内存存储）
  - 验证: src/v2/api/deps.py → get_current_user()
```

### 4.2 统一响应格式

```json
// 成功
{"code": 200, "data": {...}, "message": "ok"}

// 错误
{"code": 401, "data": null, "message": "会话过期"}
{"code": 404, "data": null, "message": "仓库未找到"}
{"code": 502, "data": null, "message": "GitCode API 调用失败"}
```

### 4.3 路由表（端口 3000）

#### 4.3.1 认证 `/api/v2/auth/`

| 方法 | 路径 | 说明 | 分页 |
|------|------|------|------|
| POST | `/login` | 密码登录 | — |
| GET | `/login/gitcode` | OAuth 跳转 URL | — |
| GET | `/callback/gitcode` | OAuth 回调 | — |
| GET | `/profile` | 用户信息 + 仓库列表 | — |

#### 4.3.2 仓库 `/api/v2/repos/`

| 方法 | 路径 | 说明 | 分页 |
|------|------|------|------|
| GET | `/member` | GitCode 成员仓库（含角色/状态） | ✅ |
| GET | `/managed` | 已托管仓库 | — |
| GET | `/available` | 可加入仓库 | — |
| POST | `/join` | 加入仓库 `{full_name}` | — |
| POST | `/host` | 托管仓库（webhook）`{full_name}` | — |
| POST | `/remove` | 移除仓库 `{full_name}` | — |

#### 4.3.3 Dashboard `/api/v2/dashboard/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/{owner}/{repo}` | 全量数据（指标/Agent/活动） |

#### 4.3.4 Agent `/api/v2/agents/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/{name}/{owner}/{repo}` | Agent 详情（指标/事件/Skill） |
| PUT | `/{name}/{owner}/{repo}/config` | Agent 配置（Engine + Skill） |

#### 4.3.5 Chat `/api/v2/chat/`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/send` | 发送消息 → Agent 回复（支持 SSE 流） |
| GET | `/config` | Chat Agent 配置（Backend + IM） |
| PUT | `/config` | 更新 Chat Agent 配置 |

#### 4.3.6 Settings `/api/v2/settings/`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/credentials` | 凭据状态（GitCode/Huawei Cloud） |
| PUT | `/credentials` | 更新凭据 |
| GET | `/notifications` | 通知偏好 |
| PUT | `/notifications` | 更新通知偏好 |
| GET | `/gitcode-repo/{owner}/{repo}` | 仓库级 GitCode 配置 |
| PUT | `/gitcode-repo/{owner}/{repo}` | 更新仓库级配置 |
| GET | `/backend-agents` | Backend Agent 引擎健康状态 |
| PUT | `/backend-agents` | 更新引擎配置 |

---

## 五、前端架构

### 5.1 页面树

```
loginOverlay
  ├─ GitCode OAuth 按钮
  ├─ 密码登录（toggle 展开）
  └─ 管理员登录（跳转 /admin/）

app (sidebar + topbar + content)
  ├─ v-rsel      仓库选择（全屏，无 sidebar）
  ├─ v-dash       Dashboard（按仓库）
  ├─ v-pipe       Pipeline
  ├─ v-agt        Agent 详情（5 子页）
  ├─ v-event      Event 详情
  ├─ v-chat       Chat Agent
  ├─ v-repos      Repositories
  ├─ v-settings   General Settings
  ├─ v-gitcode    GitCode Repo
  ├─ v-skills     DevOps Skills
  ├─ v-backend    Backend Agents
  └─ v-profile    Profile
```

### 5.2 路由机制

```
function nav(page, param, extra)
  → 隐藏所有 .vw
  → 执行 renderXxx(param)
  → 显示目标 .vw
  → 高亮侧边栏链接
  → 更新面包屑

特殊情况:
  - rsel: 隐藏 sidebar + topbar（全屏独立页面）
  - event: 使用 param=agent, extra=index
  - agt: 使用 param=agent_name
```

### 5.3 组件对应关系

| 设计文档章节 | 原型视图 | 渲染函数 | 核心数据 |
|-------------|----------|---------|---------|
| 2.1 登录 | `loginOverlay` | `login()` | 无（静态） |
| 2.1.1 仓库选择 | `v-rsel` | `renderRsel()` | `repos[]` |
| 2.2 Dashboard | `v-dash` | `renderDash(repo)` | `R[repo]` + `A[]` |
| 2.3 Pipeline | `v-pipe` | `renderPipe(repo)` | `pipedata[]` |
| 2.4 Agent 详情 | `v-agt` | `renderAgent(k)` | `A[k]` |
| 2.5 Event 详情 | `v-event` | `renderEventDetail(agent,idx)` | `A[agent].ev[idx]` |
| 2.6 Chat Agent | `v-chat` | `renderChat()` | 静态 |
| 2.7 Repositories | `v-repos` | `renderRepos()` | 静态 |
| 2.8 General Settings | `v-settings` | `renderSettings()` | 静态 |
| 2.9 GitCode Repo | `v-gitcode` | `renderGitcode()` | 静态 |
| 2.10 Backend Agents | `v-backend` | `renderBackend()` | `engines[]` |
| 2.12 DevOps Skills | `v-skills` | `renderSkills()` | `categories[]` |

---

## 六、数据流

### 6.1 登录流程

```
用户点击 GitCode 登录
  → nav('rsel')
  → 隐藏 sidebar/topbar
  → renderRsel() 展示仓库卡片
  → 点击仓库 → nav('dash')
  → renderDash(repo) 加载 Dashboard

后续访问：
  → 检查 localStorage token
  → GET /api/v2/auth/profile → 获取用户信息和已加入仓库
  → 有仓库 → 直接进入 Dashboard
  → 无仓库 → 展示仓库选择页
```

### 6.2 Dashboard 数据加载

```
renderDash(repo)
  → 静态假数据（原型阶段）
  → 生产阶段：
    GET /api/v2/dashboard/{owner}/{repo}
    → {repo, metrics, agents, chat_agent, activity}
    → 注入到模板渲染
```

### 6.3 事件交互链路

```
Dashboard Activity list
  ├─ 点击事件项 → nav('event', agent, idx)
  │     → renderEventDetail(agent, idx)
  │     → 展示上下文 + 日志 + 元信息 + 反馈
  │
Pipeline 节点
  ├─ 点击 VOD 节点 → nav('event', 'vod', 0)
  │     → 同上
  │
Agent Detail Event History
  └─ 点击事件行 → nav('event', k, i)
        → 同上
```

---

## 七、CSS 设计系统

### 7.1 设计令牌

```css
:root {
  /* 侧边栏 */
  --s: #0c1222;      /* 深度黑蓝 */
  --s2: #161e30;     /* hover */
  --s3: #1e2940;     /* active */

  /* 主内容 */
  --bg: #f5f6f8;     /* 背景灰 */
  --c: #ffffff;      /* 卡片白 */
  --b: #e3e6ee;      /* 边框 */

  /* 文字 */
  --t: #101724;      /* 主文字 */
  --t2: #5b667b;     /* 次要 */
  --t3: #8d97a8;     /* 辅助 */

  /* 语义色 */
  --a: #3168e0;      /* 主蓝色 */
  --g: #1ca44c;      /* 成功绿 */
  --y: #d49c0f;      /* 警告黄 */
  --r: #dc2828;      /* 错误红 */

  /* Agent 色 */
  --pk: #d83076;     /* VOD 粉 */
  --pu: #7b3cf2;     /* QA 紫 */
  --or: #ea5a0c;     /* SRE 橙 */
  --sk: #0c92d4;     /* Chat 天蓝 */
}
```

### 7.2 布局系统

```
body (flex)
├── .sb (232px, sticky, height:100vh)
│   ├── .sb-h (header)
│   ├── .sb-sel (repo selector)
│   ├── .sb-nav (flex:1, overflow-y:auto)
│   └── .sb-ft (footer)
│
└── .mn (flex:1, flex-direction:column)
    ├── .tb (52px, flex-shrink:0)
    └── .ct (flex:1, overflow-y:auto, padding:22px 28px)
        └── .vw (display:none) / .vw.ac (display:block)
```

### 7.3 常用组件

| 组件 | CSS 类 | 用途 |
|------|--------|------|
| 卡片 | `.cd` | 白色圆角带边框阴影 |
| 统计 | `.st` | 纵向排列的数字+标签 |
| 标签 | `.tg` | 小圆角色标 |
| 按钮 | `.bt` + `.bt-p`/`.bt-o`/`.bt-s` | 填充/描边/小型 |
| 输入行 | `.srow` | 标签+输入框横排布局 |
| Tab | `.stabs a` | 下划线激活样式 |
| Agent流 | `.af .nd` | 箭头连接的工作流卡片 |

---

## 八、部署架构

### 8.1 服务器

```
OS: Ubuntu / CentOS
Python: 3.11+
进程管理: systemd

依赖:
  pip install fastapi uvicorn paramiko lark-oapi pyyaml
```

### 8.2 systemd 服务

```ini
[Unit]
Description=Agentic DevOps
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/devops-bot
ExecStart=/usr/bin/python3 -m src.v2.run
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### 8.3 端口规划

| 端口 | 服务 | 对外 |
|------|------|------|
| **3000** | v2 REST API (FastAPI) | 可选（前端调用） |
| **80** | Admin 面板 + User 面板 (legacy) | 需要 |
| **8080** | Webhook + A2A + Health | 需要 |

### 8.4 目录结构

```
/root/devops-bot/
├── src/
│   ├── main.py              # 原有入口
│   ├── v2/
│   │   ├── run.py            # v2 入口（启动所有服务）
│   │   ├── api/
│   │   │   ├── main.py       # FastAPI app
│   │   │   ├── deps.py       # 依赖注入
│   │   │   └── routes/       # API 路由
│   │   └── web/              # 前端 SPA（待迁移）
│   ├── admin/                # 原有 Admin 面板
│   ├── agent/                # Agent 执行器
│   └── ...
├── docs/
│   ├── system-workflow-overview.md
│   ├── api-endpoints.md
│   ├── api-gap-analysis.md
│   └── ui-demo/
│       └── prototype.html
├── skills/                   # Agent 技能
├── configs/                  # 路由配置
└── data/                     # DB + workspaces
```

---

## 九、实施路线

### Phase 1: API 基础（当前）

- [x] FastAPI 框架搭建 + 路由注册
- [x] 认证路由（OAuth + 密码）
- [x] 仓库路由（列表/加入/托管）
- [x] Dashboard 路由
- [x] Agent 详情路由
- [x] Chat 路由
- [x] Settings 路由

### Phase 2: 前端迁移

- [ ] 从原型 HTML 提取为独立 SPA
- [ ] 替换假数据为 API 调用
- [ ] 状态管理（token, current_repo）
- [ ] 错误处理 + 加载态
- [ ] 路由守卫（未登录跳转）

### Phase 3: 后端补全

- [ ] Dashboard 数据对接真实统计
- [ ] Pipeline 数据对接 Session 历史
- [ ] Agent 详情对接 Event 日志
- [ ] Chat Agent 对接 opencode CLI
- [ ] Backend Agents 对接引擎检测
- [ ] 通知系统

### Phase 4: 优化

- [ ] SSE 流式 Chat 回复
- [ ] WebSocket IM 实时推送
- [ ] Dashboard 轮询/WebSocket 自动刷新
- [ ] 前端响应式适配
- [ ] 性能优化（缓存、分页）
