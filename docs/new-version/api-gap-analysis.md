# API 接口缺口分析

> 基于 `system-workflow-overview.md`（系统设计）和 `prototype.html`（原型），对照 `api-endpoints.md`（现有接口），分析需要新增和改造的 API。

---

## 一、匹配分析

### 1. 登录页

| 功能 | 原型位置 | 需要接口 | 现有接口 | 状态 |
|------|---------|---------|---------|------|
| GitCode OAuth 登录 | 主按钮 | OAuth 跳转 + 回调 | `GET /auth/gitcode/login` + `/callback` | ✅ 已有 |
| 用户名密码登录 | 展开表单 | 密码验证 + Token | `POST /login` | ✅ 已有 |

### 2. 仓库选择页（首屏）

| 功能 | 原型位置 | 需要接口 | 现有接口 | 状态 |
|------|---------|---------|---------|------|
| 获取 GitCode 成员仓库列表 | 卡片网格 | 用用户 token 拉取所有有权限的仓库，带角色信息 | `POST /user/repo-sync`（现有，但设计为加载+自动托管） | ⚠️ 可复用 |
| 按名称搜索仓库 | 搜索框 | 仓库搜索过滤 | 无 | 🔴 **新增** |
| 按角色过滤仓库 | 下拉过滤 | 前端过滤即可 | — | ✅ 无需 |
| 加入仓库（成员角色） | `+ Join` 按钮 | 加入仓库，创建 Webhook（非管理员只能 join 不能 host） | `POST /user/repo-add`（现有触发 webhook 创建） | ⚠️ 逻辑不同 |
| 托管仓库（管理员角色） | `Host Now` 按钮 | 创建 webhook + 托管 | `POST /user/repo-add` | ✅ 已有 |

**建议新增/改造：**

```
GET  /api/user/repos?token=xxx&search=&role=
      返回: {repos:[{full_name,owner,description,role,is_joined,is_hosted}]}
      说明: 获取用户有权限的 GitCode 仓库列表，标注角色和加入/托管状态

POST /api/user/repos/join?token=xxx
      Body: {full_name: "owner/repo"}
      说明: 加入仓库（非管理员角色，不做 webhook 托管）
```

### 3. Dashboard（按仓库维度）

| 功能 | 原型位置 | 需要接口 | 现有接口 | 状态 |
|------|---------|---------|---------|------|
| Repo 基本信息 | Repo Header | 仓库名称、分支、Webhook 状态、角色 | 无直接接口 | 🔴 **新增** |
| 仓库指标 | 3 个 stat-card | Auto-Resolution %、Events/Week、Avg Response Time | 无 | 🔴 **新增** |
| Agent 工作流状态 | 5 个 Agent 节点 | 每个 Agent 的运行状态、Backend、当前任务 | 无 | 🔴 **新增** |
| Chat Agent 条 | Chat lane | Chat Agent 连接状态 | 无 | 🔴 **新增** |
| 近期活动消息 | Activity 列表 | 最近 24h 的 Agent 处理事件时间线 | 无 | 🔴 **新增** |
| 切换已托管仓库 | 侧边栏下拉 | 前端路由 + session | 无（Cookie/localStorage） | ✅ 无需后端 |

**建议新增：**

```
GET  /api/dashboard/{owner}/{repo}?token=xxx
     返回: {
       repo: {name, branch, webhook_ok, role, ...},
       metrics: {auto_resolution_pct, events_per_week, avg_response_sec},
       agents: [{name, status, backend, task, signals, ...}],
       chat_agent: {status, backend_type},
       activity: [{time, agent, action, detail, tags}]
     }
     说明: Dashboard 全量数据入口，前端可将其拆分展示到不同区域
```

### 4. Pipeline 页面

| 功能 | 原型位置 | 需要接口 | 现有接口 | 状态 |
|------|---------|---------|---------|------|
| 活跃流水线状态 | 5 阶段节点 | 当前活跃 Issue 在每个 Agent 阶段的状态 | 无 | 🔴 **新增** |
| 流水线历史表格 | 历史表 | 已完成的流水线列表（Issue→Duration→Agents→Result） | 无 | 🔴 **新增** |
| 点击 Issue 切换流水线 | 表格行点击 | 前端切换 state 即可 | — | ✅ 无需 |

**建议新增：**

```
GET  /api/pipeline/{owner}/{repo}?token=xxx
     返回: {
       active: {issue_id, title, stages:[{agent,done_at,status}]},
       history: [{issue_id, title, duration, agents, result, release}]
     }
     说明: 获取指定仓库的流水线状态，包含活跃流水线 + 历史
```

### 5. Agent 详情页

| 功能 | 原型位置 | 需要接口 | 现有接口 | 状态 |
|------|---------|---------|---------|------|
| Agent 摘要 | Agent Header | Agent 名称、状态、描述 | 无 | 🔴 **新增** |
| Agent 指标 | 3 个 stat-card | 该 Agent 的统计数据 | 无 | 🔴 **新增** |
| Agent 事件历史 | Event History | 该 Agent 的最近操作记录 | 无 | 🔴 **新增** |
| Agent Skill 配置 | Skill 区域 | 默认 skill + 自定义 skill 列表 | 部分（`/user/skill*`） | ⚠️ **需整合** |
| Agent 配置 | Configure 按钮 | 打开该 Agent 的配置面板 | 无 | 🔴 **新增** |

**建议新增：**

```
GET  /api/agents/{agent_name}/{owner}/{repo}?token=xxx
     返回: {
       agent: {name, description, status, backend},
       metrics: [{label, value}],
       events: [{time, text}],
       skill: {default, customs:[]}
     }

PUT  /api/agents/{agent_name}/{owner}/{repo}/config?token=xxx
     Body: {backend, skill_override, ...}
     说明: 配置 Agent 的 backend 和自定义 skill
```

### 6. Chat Agent 页

| 功能 | 原型位置 | 需要接口 | 现有接口 | 状态 |
|------|---------|---------|---------|------|
| 对话交互 | 聊天窗口 | 发送消息 → Agent 回复 | 无 | 🔴 **新增** |
| Agents 连接状态 | 右侧面板 | 5 个 DevOps Agent 的当前状态 | 可复用 `/api/dashboard/` | ⚠️ **可复用** |
| Backend 配置 | ★ Backend | Backend Type、URL、API Key 的读写 | 无 | 🔴 **新增** |
| IM 配置 | ★ IM | Feishu App ID/Secret、WeCom/DingTalk | 部分（`/user/cred` 存 feishu 字段） | ⚠️ **需扩展** |

**建议新增：**

```
POST /api/chat/send?token=xxx
     Body: {message: "..."}
     Response: SSE stream 或 {reply: "...", conversation_id}
     说明: 发送消息给 Chat Agent，支持流式返回

GET  /api/chat/config?token=xxx
     返回: {backend: {type, url, api_key_exist}, im: {feishu:{app_id, app_secret_exist}, wecom:{...}, dingtalk:{...}}}

PUT  /api/chat/config?token=xxx
     Body: {backend_type, backend_url, backend_api_key, ...}
     说明: 更新 Chat Agent 的 Backend 和 IM 配置
```

### 7. Repositories 页面

| 功能 | 原型位置 | 需要接口 | 现有接口 | 状态 |
|------|---------|---------|---------|------|
| 已托管仓库列表 | Hosted 区 | 列出已托管仓库 + 状态 | 无直接接口 | 🔴 **新增** |
| 同步仓库 | ↻ 按钮 | 从 GitCode 重新加载 | `POST /user/repo-sync` | ✅ 已有 |
| 可加入仓库列表 | Available 区 | 未加入但有权限的仓库 | 无 | 🔴 **新增** |
| 加入 / 托管操作 | Join / Host Now | 见 2.仓库选择页 | 见上面 | — |

**建议新增：**

```
GET  /api/user/repos/managed?token=xxx
     返回: [{full_name, role, webhook_ok, branch, status}]
     说明: 获取当前用户已托管/已加入的仓库列表

GET  /api/user/repos/available?token=xxx
     返回: [{full_name, description, role}]
     说明: 用户有权限但尚未托管/加入的仓库（不含已加入的）

POST /api/user/repos/host?token=xxx
     Body: {full_name: "owner/repo"}
     说明: 托管仓库（创建 webhook + 标记 managed）
     与现有 /user/repo-add 类似，但语义更清晰
```

### 8. General Settings 页

| 功能 | 原型位置 | 需要接口 | 现有接口 | 状态 |
|------|---------|---------|---------|------|
| GitCode API Token | Credentials tab | 读取/更新个人 GitCode Token（自动填充） | `POST /user/cred` | ✅ 已有 |
| 华为云 AK/SK | Credentials tab | 读取/更新华为云凭据 | `POST /user/cred` | ✅ 已有 |
| 通知偏好 | Notifications tab | Issue/PR/Build/Release/VOD 开关 | 无 | 🔴 **新增** |
| 自定义 Skill | Skills tab | 上传、列表、删除 | `POST /user/skill` 等 | ✅ 已有 |
| Repo-Skill 映射 | Skills 编辑 | 仓库→事件→skill 绑定 | `POST /user/skill-config` | ✅ 已有 |

**建议新增：**

```
GET  /api/user/notifications?token=xxx
     返回: {issues:bool, pr:bool, build:bool, release:bool, vod:bool}

PUT  /api/user/notifications?token=xxx
     Body: {issues:true, pr:true, build:false, release:true, vod:false}
     说明: 更新通知偏好
```

### 9. GitCode Repo 页

| 功能 | 原型位置 | 需要接口 | 现有接口 | 状态 |
|------|---------|---------|---------|------|
| DevOps Bot Token | Bot Token | 仓库级别的 bot token | 无 | 🔴 **新增** |
| Webhook Token | Webhook Bot | Webhook Token 读写 | `POST /user/cred` 中的 `gitcode_webhook_token` | ⚠️ **需分离** |
| Webhook URL | Webhook Bot | Webhook URL 显示 | 无（自动检测，不暴露） | ✅ 无需 |
| Bot Username | Webhook Bot | 机器人用户名 | 无（在 `bot_usernames` 中） | ⚠️ **需分离** |

**建议新增：**

```
GET  /api/repo/{owner}/{repo}/gitcode?token=xxx
     返回: {bot_token, webhook_token, webhook_url, bot_usernames}

PUT  /api/repo/{owner}/{repo}/gitcode?token=xxx
     Body: {bot_token, webhook_token, bot_usernames}
     说明: 更新仓库级别的 GitCode 配置（仅管理员可操作）
```

### 10. 侧边栏

| 功能 | 原型位置 | 需要接口 | 现有接口 | 状态 |
|------|---------|---------|---------|------|
| 已托管仓库下拉 | Repo selector | 获取用户已托管的仓库名称列表 | 可复用 `/api/dashboard/` 的 repo 信息 | ⚠️ **可复用** |
| 用户信息 | 侧边栏底部 | 用户名、在线状态 | 无 | 🔴 **新增** |

**建议新增：**

```
GET  /api/user/profile?token=xxx
     返回: {username, status, managed_repos:[{key, full_name}]}
     说明: 获取用户基本信息和已托管仓库列表（侧边栏使用）
```

---

## 二、新增接口汇总

| # | 方法 | 路径 | 说明 | 优先级 |
|---|------|------|------|--------|
| N1 | GET | `/api/user/repos` | 获取用户有权限的 GitCode 仓库列表（含角色、状态） | P0 |
| N2 | POST | `/api/user/repos/join` | 加入仓库（非管理员） | P0 |
| N3 | POST | `/api/user/repos/host` | 托管仓库（含 webhook 创建） | P0 |
| N4 | GET | `/api/user/repos/managed` | 获取已托管/已加入仓库列表 | P0 |
| N5 | GET | `/api/user/repos/available` | 获取可加入仓库列表 | P1 |
| N6 | GET | `/api/dashboard/{owner}/{repo}` | Dashboard 全量数据 | P0 |
| N7 | GET | `/api/pipeline/{owner}/{repo}` | 流水线状态 + 历史 | P0 |
| N8 | GET | `/api/agents/{name}/{owner}/{repo}` | Agent 详情（指标+事件+Skill） | P0 |
| N9 | PUT | `/api/agents/{name}/{owner}/{repo}/config` | Agent 配置更新 | P1 |
| N10 | POST | `/api/chat/send` | 发送消息给 Chat Agent | P0 |
| N11 | GET | `/api/chat/config` | 获取 Chat Agent 配置 | P0 |
| N12 | PUT | `/api/chat/config` | 更新 Chat Agent 配置（Backend、IM） | P0 |
| N13 | GET | `/api/user/notifications` | 获取通知偏好 | P1 |
| N14 | PUT | `/api/user/notifications` | 更新通知偏好 | P1 |
| N15 | GET | `/api/repo/{owner}/{repo}/gitcode` | 获取仓库级 GitCode 配置 | P1 |
| N16 | PUT | `/api/repo/{owner}/{repo}/gitcode` | 更新仓库级 GitCode 配置 | P1 |
| N17 | GET | `/api/user/profile` | 用户基本信息和仓库列表 | P0 |
| N18 | GET | `/api/settings/backend-agents` | 获取 5 个 Agent 的 Backend 配置 | P1 |
| N19 | PUT | `/api/settings/backend-agents` | 更新 Agent 的 Backend 引擎（OpenCode/CodeArts/Hermes/Cursor） | P1 |

---

## 三、需要改造的现有接口

| 现 # | 路径 | 当前问题 | 改造建议 |
|------|------|---------|---------|
| 13 | `POST /user/repo-sync` | 从 GitCode 同步仓库并自动托管逻辑耦合 | 拆分为"获取列表"和"托管"两个独立操作（见 N1+N3） |
| 5 | `POST /user/cred` | 将所有凭据混在一个接口里 | 按场景拆分为：个人 GitCode Token、华为云 AK/SK、GitCode Repo Bot 配置 |
| 10-12 | `/user/skill*` | Skill 未关联到具体的 Agent 详情页 | 在 Agent 详情 API（N8）中暴露 Skill 配置字段 |

---

## 四、可移除或下沉的现有接口

| 现 # | 路径 | 说明 |
|------|------|------|
| 6-9 | `/user/vm*` | 云主机管理在新 UI 原型中未展示，可后续按需补充 |
| 16-24 | `/admin/*` | 管理员功能保留但可能需要独立的后台页面（非新设计的 SPA 的一部分） |
| 1-3 | `/`, `/panel`, `/logout` | 前后端分离后改为 JSON API + SPA 路由 + JWT |

---

## 五、通用设计规范建议

基于原型设计推导的 API 设计约定：

| 规范 | 说明 |
|------|------|
| **认证方式** | 统一使用 `Authorization: Bearer <token>` header（替代 `?token=` 查询参数） |
| **响应格式** | 统一 JSON，包含 `{code, data, message}` 三层结构 |
| **路径风格** | RESTful：资源名 + 标识符，如 `/api/agents/{name}/{owner}/{repo}` |
| **仓库标识** | `{owner}/{repo}` 格式（与 GitCode 的 full_name 一致） |
| **分页** | 列表类接口支持 `?page=&per_page=` |
| **错误码** | 统一错误码体系（401 未认证，403 无权限，404 不存在，422 参数错误） |
