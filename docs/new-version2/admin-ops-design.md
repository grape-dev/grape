# Grape 管理运营系统设计

> 版本：v1.0
> 日期：2026-08-08
> 范围：仅设计，不改代码
> 对齐：`agent-runtime-design.md` / `implementation-plan.md`（new-version2 既有升级主线）

---

## 一、背景与目标

### 1.1 背景

Grape 目前已具备完整的业务能力：

| 能力域 | 现状实现 |
|---|---|
| 用户 | GitCode OAuth 登录 + 密码登录，登录白名单（`configs/login-whitelist.json`） |
| 仓库 | `managed_repos` + `user_repos`，一键托管 / 加入 / 解除托管 |
| Agent | 5 类 Agent（vod/issue/coding/qa/sre）+ Chat，`devops_agent_session` + `agent_type` + `backend_agents` |
| 技能 | `skill` 表三态（official/user/repo）+ 上传校验 |
| 事件 | `events` / `event_skipped` / `event_pipeline`，Pipeline 可视化 |
| IM | 多飞书 Bot（`global_im_bot_config` + BotManager，100+ Bot 隔离） |
| 对话 | `chat_conversations` / `chat_messages` / `chat_agent_session` |
| 任务 | `tasks` 任务中心 |
| 配置 | `meta` 表 KV + 多份 configs/*.json |

但**管理运营面几乎没有**：

- `src/admin/` 为空目录，旧的 HTML 管理台（`docs/admin-web-console-design.md`）已与 FastAPI/SPA 架构脱节。
- **无 RBAC**：所有登录用户同等权限，只有 GitCode 仓库角色的隐式区分，无管理员/运营者/审计者角色。
- **无审计**：关键操作（白名单变更、用户禁用、配置修改）无留痕。
- **无运营视图**：无系统健康总览、无跨仓库事件统计、无 Agent/Runtime 运行态、无 IM Bot 状态看板。
- **无治理手段**：用户不能禁用、白名单只能手改 JSON、技能无审核位、无配额/限流。

### 1.2 目标

1. 建立 **RBAC 角色权限模型**，区分系统管理员 / 运营人员 / 仓库管理员 / 普通用户。
2. 提供 **用户生命周期管理**：创建、禁用、重置密码、配额、凭据审计。
3. 提供 **运营可观测**：系统健康、事件统计、Agent/Runtime、IM Bot、任务队列看板。
4. 提供 **治理能力**：白名单管理、技能审核位、公告、审计日志。
5. **不回归**：业务面（用户侧 SPA + API）行为不变，管理面增量叠加。

### 1.3 非目标（本期不做）

- 不做计费/订单系统。
- 不做独立部署的服务网格/网关（沿用 FastAPI 单进程 + 现有 DB）。
- 不重写现有用户/仓库/Agent/技能 API（只加管理面端点）。
- 不引入新的外部依赖（沿用 SQLite + FastAPI + 原生 SPA JS）。

---

## 二、设计原则（业界最佳实践）

| # | 原则 | 落地 |
|---|---|---|
| P1 | **职责分离** | 管理面（`/api/admin/*`）与业务面（`/api/*`）端点分离，前端独立 `Admin` 视图 |
| P2 | **最小权限** | RBAC 按「角色→权限点」建模，管理端点逐点鉴权，不做"一揽子的管理员" |
| P3 | **审计留痕** | 所有管理写操作写入 `admin_audit_log`，支持按人/按操作/按时间检索 |
| P4 | **治理闭环** | 白名单、技能、公告等从"改文件/直改库"收敛为"管理台可操作" |
| P5 | **可观测即运营** | 健康四态、事件统计、运行态指标以 API 提供，管理台只做聚合展示 |
| P6 | **增量不回归** | 全部新增，不改既有表结构/端点行为；默认角色 `user`，存量用户自动兼容 |
| P7 | **开关先行** | 管理面能力可独立发布；`meta` 存功能开关，灰度可控 |
| P8 | **防御纵深** | 密码哈希（SHA256）、token 独立、敏感字段脱敏、操作二次确认 |

---

## 三、总体架构

```
┌──────────────────────────── 管理运营层（Admin & Ops）───────────────────────────┐
│  Admin SPA（/admin 路由）                                                        │
│  ├─ 总览 Overview         ├─ 用户管理 Users        ├─ 白名单 Whitelist          │
│  ├─ 仓库运营 Repos        ├─ Agent/Runtime 运营    ├─ 技能治理 Skills           │
│  ├─ 事件监控 Events        ├─ IM Bot 运营           ├─ 公告 Notices              │
│  └─ 审计日志 Audit         └─ 系统设置 Settings                                   │
└────────────────────────────────┬─────────────────────────────────────────────────┘
                                 │ /api/admin/*（Bearer token，RBAC 鉴权）
┌────────────────────────────────▼─────────────────────────────────────────────────┐
│  FastAPI 主服务（端口 3000）                                                      │
│  ├─ 新增 src/api/routes/admin/*（admin_auth / users / whitelist / repos /      │
│  │     agents / skills / events / im / notices / audit / settings / overview）  │
│  ├─ 新增 src/api/deps_admin.py（RBAC 依赖）                                      │
│  └─ 新增 src/admin/ 服务层（用户/白名单/审计/指标 聚合）                          │
└────────────────────────────────┬─────────────────────────────────────────────────┘
                                 │ 复用现有：src/user/db.py / src/session_manager.py
                                 │ src/api/event_store.py / src/config.py / src/im/feishu_async.py
┌────────────────────────────────▼─────────────────────────────────────────────────┐
│  数据层 data/sessions.db                                                          │
│  ├─ 新增：admin_roles / admin_role_assignments / admin_audit_log / admin_notices │
│  │         + user_accounts 增列 role/status/quota_json 等                         │
│  └─ 复用：user_accounts / user_credentials / managed_repos / events / skill /    │
│           backend_agents / devops_agent_session / global_im_bot_config / tasks   │
└───────────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 与既有升级主线的关系

- **Runtime 主线**（`agent-runtime-design.md` M0–M4）：把 `backend_agents` 演进为 `agent_runtimes`，引入 runtime 健康四态 + 任务队列。本设计在 Runtime 之上补 **Agent/Runtime 运营视图**（设备列表、健康态、任务量），只读聚合现有数据，不与其冲突。
- **执行模式开关**（`implementation-plan.md` §三 `execution_mode`）：管理台"系统设置"页直接读写 `meta.execution_mode`，作为运维入口，不改其切换逻辑。

---

## 四、角色与权限模型（RBAC）

### 4.1 角色定义

| 角色 code | 名称 | 说明 | 内置授予 |
|---|---|---|---|
| `super_admin` | 超级管理员 | 系统所有者，全部权限 | 全部 |
| `ops_admin` | 运营管理员 | 日常运营：用户/仓库/Agent/技能/公告/事件 | 除「系统设置(敏感)」外的运营权限 |
| `repo_admin` | 仓库管理员 | 仅能管理被分配的仓库集合 | 该仓库集合的 Agent/技能/事件查看 |
| `user` | 普通用户 | 默认角色，业务面能力 | 无管理权限 |
| `auditor` | 审计员 | 只读审计（Audit 日志 + 只读看板） | audit.read + 各只读 |

> `user` 为**默认角色**，保证存量用户零改动。角色可叠加 `repo_scope`（仓库级授权，仅 `repo_admin` 使用）。

### 4.2 权限点（permission）清单

| 权限点 | 说明 | super | ops | repo | auditor |
|---|------|:--:|:--:|:--:|:--:|
| `overview.read` | 总览看板 | ✅ | ✅ | — | ✅ |
| `users.read` | 用户列表/详情 | ✅ | ✅ | — | ✅ |
| `users.manage` | 创建/禁用/重置密码/改角色 | ✅ | ✅ | — | — |
| `whitelist.manage` | 白名单增删改 | ✅ | ✅ | — | — |
| `repos.read` | 仓库运营列表 | ✅ | ✅ | 限 scope | ✅ |
| `repos.manage` | 解除托管/改 bot 配置 | ✅ | ✅ | 限 scope | — |
| `agents.read` | Agent/Runtime 状态 | ✅ | ✅ | 限 scope | ✅ |
| `agents.manage` | 绑定 runtime/清会话 | ✅ | ✅ | 限 scope | — |
| `skills.audit` | 技能审核位/禁用 | ✅ | ✅ | — | — |
| `events.read` | 全量事件检索/统计 | ✅ | ✅ | 限 scope | ✅ |
| `im.read` | IM Bot 状态 | ✅ | ✅ | — | ✅ |
| `im.manage` | 重启/停用 IM Bot | ✅ | ✅ | — | — |
| `notices.manage` | 公告增删改 | ✅ | ✅ | — | — |
| `audit.read` | 审计日志 | ✅ | ✅ | — | ✅ |
| `settings.read` | 系统设置查看 | ✅ | ✅ | — | — |
| `settings.manage` | 系统设置修改（敏感） | ✅ | — | — | — |

> 权限模型按"权限点集合"存储（`admin_roles.permissions_json`），新增权限点只改种子数据，不改代码判断。

### 4.3 鉴权链路

```
请求 /api/admin/xxx
  → Bearer token 校验（复用 user_db.validate_token）
  → 查 user_accounts.role + admin_role_assignments 汇总权限
  → 端点级权限点校验（depends=require_permission("xxx"))
  → repo_admin 额外校验 repo_scope 归属
```

---

## 五、数据模型设计

### 5.1 新增表

```sql
-- 角色定义（种子数据，可扩展）
CREATE TABLE IF NOT EXISTS admin_roles (
    role TEXT PRIMARY KEY,                -- super_admin / ops_admin / repo_admin / auditor
    name TEXT NOT NULL,                   -- 显示名
    permissions_json TEXT DEFAULT '[]',   -- 权限点列表
    is_builtin INTEGER DEFAULT 1,         -- 内置角色不可删除
    created_at TEXT DEFAULT (datetime('now'))
);

-- 用户角色分配（支持仓库级 scope，user 角色可不入此表）
CREATE TABLE IF NOT EXISTS admin_role_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,                   -- 引用 admin_roles.role
    repo_scope TEXT DEFAULT '',           -- 空=全局；非空=仅该 repo_key（repo_admin 用）
    granted_by INTEGER DEFAULT 0,
    granted_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, role, repo_scope)
);

-- 管理操作审计日志
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user TEXT DEFAULT '',           -- 操作者用户名
    actor_user_id INTEGER DEFAULT 0,
    action TEXT NOT NULL,                 -- create_user / disable_user / update_whitelist ...
    target_type TEXT DEFAULT '',          -- user / repo / skill / notice / config ...
    target_id TEXT DEFAULT '',
    detail TEXT DEFAULT '{}',             -- JSON，敏感字段脱敏
    ip TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_action ON admin_audit_log(action);
CREATE INDEX IF NOT EXISTS idx_admin_audit_actor ON admin_audit_log(actor_user_id);
CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log(created_at);

-- 系统公告
CREATE TABLE IF NOT EXISTS admin_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    level TEXT DEFAULT 'info',            -- info / warning / danger
    scope TEXT DEFAULT 'all',             -- all / repo / user
    target_id TEXT DEFAULT '',            -- scope=repo 时 repo_key；scope=user 时 user_id
    is_active INTEGER DEFAULT 1,
    created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT DEFAULT NULL
);
```

### 5.2 既有表扩展（`user_accounts` 幂等加列）

```sql
ALTER TABLE user_accounts ADD COLUMN role TEXT DEFAULT 'user';       -- 主角色（默认 user，兼容存量）
ALTER TABLE user_accounts ADD COLUMN status TEXT DEFAULT 'active';   -- active / disabled
ALTER TABLE user_accounts ADD COLUMN quota_json TEXT DEFAULT '{}';   -- {max_repos, max_skills, max_bots, ...}
ALTER TABLE user_accounts ADD COLUMN last_login_at TEXT DEFAULT '';
ALTER TABLE user_accounts ADD COLUMN remark TEXT DEFAULT '';
```

- `status=disabled` 的用户：`verify_user` / OAuth 回调返回 403"账号已被禁用"；已有 token 在 `validate_token` 中一并校验。
- `quota_json` 暂为预留字段，本期在创建/编辑用户页可填，强制校验列为二期（见 §9.3）。

### 5.3 登录白名单落库（替代手改 JSON）

```sql
CREATE TABLE IF NOT EXISTS login_whitelist (
    gitcode_login TEXT PRIMARY KEY,
    note TEXT DEFAULT '',
    added_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
```

- `configs/login-whitelist.json` 迁移为**种子导入**（首次启动导入，幂等），之后以 DB 为准。
- `config.py:is_gitcode_user_allowed` 改为「DB 优先，JSON 兜底（兼容未迁移环境）」。
- 白名单开关：`meta.whitelist_enabled`（默认 `1`；关闭 = 全部放行，回到旧默认行为）。

---

## 六、功能模块设计

### 6.1 总览 Overview

| 区块 | 数据来源 | 说明 |
|---|---|---|
| 系统健康卡 | `/health` + 数据库自检 | 主服务 / webhook / DB 可写性 / IM WS 状态 |
| 用户统计 | `user_accounts` | 总数、今日新增、禁用数、角色分布 |
| 仓库统计 | `managed_repos` + `user_repos` | 托管数、已加入数、活跃 webhook 数 |
| 事件统计 | `events` | 近 7 日事件量、成功率、失败 Top 事件类型 |
| Runtime 概览 | `backend_agents`（或演进后 `agent_runtimes`） | 引擎数、healthy 数 |
| IM Bot 概览 | `global_im_bot_config` + BotManager | 总数、在线数、异常数 |
| 待办告警 | 各表聚合 | 禁用用户数、失败事件、离线 Bot 等 |

### 6.2 用户管理 Users

| 操作 | 说明 |
|---|---|
| 列表/搜索 | 按用户名、角色、状态、创建时间分页检索 |
| 详情 | 基本信息 + 角色 + 配额 + 凭据存在性（脱敏）+ 关联仓库 + 关联事件数 |
| 创建用户 | 用户名 + 初始密码 + 角色（默认 user）；审计留痕 |
| 禁用/启用 | `status=disabled` 立即生效；禁用时提示影响（事件/对话不受影响） |
| 重置密码 | 生成新密码或指定，返回明文一次 |
| 修改角色 | 更新 `user_accounts.role` 或 `admin_role_assignments` |
| 仓库级授权 | 授予/撤销某用户的 repo_admin + repo_scope |
| 凭据审计 | 展示 gitcode/huawei/codearts/feishu 凭据是否配置（不展示明文），提示轮换 |

### 6.3 登录白名单 Whitelist

| 操作 | 说明 |
|---|---|
| 列表 | 分页 + 搜索 |
| 新增 | gitcode_login + 备注 |
| 删除 | 移除后该账号下次登录被拒 |
| 开关 | `whitelist_enabled` 全局开关 |
| 审计 | 增删均写 `admin_audit_log` |

### 6.4 仓库运营 Repos

| 操作 | 说明 |
|---|---|
| 全量仓库列表 | 跨用户视角：repo_key / 托管人 / webhook 状态 / bot_username / dev_scene |
| 仓库详情 | 关联 Agent 配置、事件数、技能、绑定 Bot |
| 解除托管 | 复用 `POST /api/repos/remove` 全链路，管理台入口 + 二次确认 |
| 修改仓库配置 | bot token 存在性、webhook token、dev_scene |

### 6.5 Agent / Runtime 运营

| 操作 | 说明 |
|---|---|
| Runtime 列表 | `backend_agents` 引擎：installed / healthy / version / cli_path，刷新触发 `discover_backends` |
| Runtime 健康态 | 演进后读 `agent_runtimes.status/last_seen_at`（对齐 `agent-runtime-design.md` 四态） |
| Agent 配置查看 | 按仓库 × agent_type 展示 backend / skill / session 状态 |
| 清会话 | 复用 `POST /api/agents/{name}/{owner}/{repo}/clean` |

### 6.6 技能治理 Skills

| 操作 | 说明 |
|---|---|
| 全量技能列表 | official / user / repo 三态 + 所属 agent_type |
| 官方技能同步 | 复用 `POST /api/skills/sync-official` |
| 技能审核位 | 为 `skill` 表新增 `audit_status`（pending/approved/rejected），用户上传默认 pending，管理台可审核（见 §9.2 表扩展） |
| 禁用技能 | `is_active=0` 立即不再被解析 |

### 6.7 事件监控 Events

| 操作 | 说明 |
|---|---|
| 全量事件流 | 跨仓库检索：seq / repo / event_type / agent_type / status / 耗时 / 结果 |
| 状态筛选 | pending / running / completed / failed |
| 失败归因 | `event_skipped` 展示（无路由/标签回声等），失败事件查看 `agent_logs` |
| 统计聚合 | 按天/按 Agent/按事件类型分布 |

### 6.8 IM Bot 运营

| 操作 | 说明 |
|---|---|
| Bot 列表 | `global_im_bot_config` + BotManager 实时状态（connected / reconnecting / stopped / failed） |
| 单 Bot 指标 | messages_received / sent / failed / reconnect_count / last_error |
| 操作 | 重启（remove_bot + add_bot）、停用（`is_active=0`） |

### 6.9 公告 Notices

| 操作 | 说明 |
|---|---|
| 发布 | 标题 + 内容 + 级别 + 范围（all / repo / user） |
| 下线 | `is_active=0` |
| 业务面展示 | 登录页 / SPA 顶部 banner 拉取 `GET /api/notices/active`（轻量，只读 active） |

### 6.10 审计日志 Audit

| 操作 | 说明 |
|---|---|
| 检索 | 按操作者 / action / 时间 / 目标类型过滤分页 |
| 详情 | 查看 detail JSON（脱敏后） |
| 导出 | CSV（管理员/审计员） |

### 6.11 系统设置 Settings

| 项 | 说明 |
|---|---|
| OAuth 配置 | 复用 `PUT /api/settings/oauth`（仅 super_admin） |
| Webhook URL | `meta.webhook_url` |
| 执行模式 | `meta.execution_mode`（auto/direct/queue_remote，`queue_local` 已废除，见 engine-default-selection-design.md §9） |
| 白名单开关 | `meta.whitelist_enabled` |
| 公告/banner | 与 Notices 打通 |

---

## 七、API 设计（`/api/admin/*`）

统一前缀 `/api/admin`，统一响应 `{code, data, message}`，统一 Bearer token + RBAC 依赖。

| 方法 | 路径 | 权限点 | 说明 |
|---|---|---|---|
| GET | `/api/admin/overview` | `overview.read` | 总览聚合数据 |
| GET | `/api/admin/users` | `users.read` | 用户列表（分页/搜索） |
| GET | `/api/admin/users/{id}` | `users.read` | 用户详情 |
| POST | `/api/admin/users` | `users.manage` | 创建用户 |
| PUT | `/api/admin/users/{id}/status` | `users.manage` | 禁用/启用 |
| PUT | `/api/admin/users/{id}/password` | `users.manage` | 重置密码 |
| PUT | `/api/admin/users/{id}/role` | `users.manage` | 改角色（含 repo_scope） |
| GET | `/api/admin/whitelist` | `whitelist.manage` | 白名单列表 |
| POST | `/api/admin/whitelist` | `whitelist.manage` | 新增 |
| DELETE | `/api/admin/whitelist/{login}` | `whitelist.manage` | 删除 |
| PUT | `/api/admin/whitelist/toggle` | `whitelist.manage` | 全局开关 |
| GET | `/api/admin/repos` | `repos.read` | 全量仓库列表 |
| GET | `/api/admin/repos/{repo_key}` | `repos.read` | 仓库详情 |
| POST | `/api/admin/repos/{repo_key}/remove` | `repos.manage` | 解除托管 |
| PUT | `/api/admin/repos/{repo_key}` | `repos.manage` | 改仓库配置 |
| GET | `/api/admin/runtimes` | `agents.read` | Runtime 列表 |
| POST | `/api/admin/runtimes/refresh` | `agents.manage` | 刷新引擎探测 |
| GET | `/api/admin/agents` | `agents.read` | 按仓库×Agent 配置 |
| POST | `/api/admin/agents/{agent_type}/{owner}/{repo}/clean` | `agents.manage` | 清会话 |
| GET | `/api/admin/skills` | `skills.audit` | 全量技能 |
| POST | `/api/admin/skills/sync-official` | `skills.audit` | 同步官方技能 |
| PUT | `/api/admin/skills/{id}/audit` | `skills.audit` | 审核/禁用 |
| GET | `/api/admin/events` | `events.read` | 全量事件（分页/筛选） |
| GET | `/api/admin/events/stats` | `events.read` | 事件统计聚合 |
| GET | `/api/admin/events/{seq}` | `events.read` | 事件详情（含日志） |
| GET | `/api/admin/im/bots` | `im.read` | IM Bot 列表（含实时状态） |
| POST | `/api/admin/im/bots/{app_id}/restart` | `im.manage` | 重启 Bot |
| PUT | `/api/admin/im/bots/{app_id}/active` | `im.manage` | 启用/停用 Bot |
| GET | `/api/admin/notices` | `notices.manage` | 公告列表 |
| POST | `/api/admin/notices` | `notices.manage` | 发布公告 |
| PUT | `/api/admin/notices/{id}` | `notices.manage` | 更新/下线 |
| GET | `/api/admin/audit` | `audit.read` | 审计日志（分页/筛选） |
| GET | `/api/admin/audit/export` | `audit.read` | 审计 CSV 导出 |
| GET | `/api/admin/settings` | `settings.read` | 系统设置 |
| PUT | `/api/admin/settings` | `settings.manage` | 修改系统设置 |
| GET | `/api/notices/active` | 业务面公开 | 公告 banner（登录页/SPA） |

---

## 八、前端页面设计（Admin SPA）

- 入口：`index.html` 增加侧栏 `Admin` 分组（仅拥有任意管理权限的用户可见）。
- 路由：`app.js` VIEWS 增加 `admin`，子视图通过 `admin?tab=xxx` 切换。
- 页面文件：`src/api/static/js/pages/admin/{overview,users,whitelist,repos,agents,skills,events,im,notices,audit,settings}.js`。
- 统一复用现有 `API` 客户端与 `renderMarkdown` / `fmtLocalTime` 工具。
- 高风险操作（解除托管、禁用用户、重置密码、删除白名单）统一「确认弹窗 + 二次输入确认」。

---

## 九、与现有体系的关系

### 9.1 复用的既有模块

| 模块 | 用途 |
|---|---|
| `src/user/db.py` | 用户 CRUD、token、meta、backend_agents |
| `src/session_manager.py` | chat / IM 绑定表 |
| `src/api/event_store.py` | 事件读写、统计 |
| `src/api/routes/repos.py: remove_managed_repo` | 解除托管全链路 |
| `src/api/routes/skills.py: sync_official` | 官方技能同步 |
| `src/im/feishu_async.py: BotManager` | IM Bot 实时状态与重启用 |
| `src/config.py: is_gitcode_user_allowed` | 白名单判定（改为 DB 优先） |
| `src/agent/executor.py: discover_backends` | Runtime 探测 |

### 9.2 需要的小改动（幂等，不破坏现有行为）

1. `user_accounts` 加 `role/status/quota_json/last_login_at/remark` 列（§5.2）。
2. `config.py:is_gitcode_user_allowed` 增加 DB 优先逻辑（§5.3），JSON 保留为兜底。
3. `skill` 表加 `audit_status TEXT DEFAULT 'approved'`（存量全部默认 approved，不引入审核阻塞）。
4. `verify_user` / OAuth 回调 / `validate_token` 增加 `status='disabled'` 拦截。
5. 前端 `app.js` / `index.html` 增加 Admin 导航（按权限可见）。

### 9.3 二期（本期不实现，仅在设计中预留）

- 配额强制校验（`quota_json` 生效于 repos/skills/bots 创建路径）。
- 管理台内置运维脚本（备份 DB、清理过期事件）。
- 事件/审计趋势图表（ECharts 引入待定）。

---

## 十、安全设计

| 项 | 方案 |
|---|---|
| 认证 | 复用 Bearer user token；管理端点不再新增独立 admin token 体系（减少管理面） |
| 鉴权 | RBAC 权限点 + repo_scope 双重校验；未授权 403 |
| 审计 | 所有写操作写 `admin_audit_log`；detail 中敏感字段（密码、token）一律 `[REDACTED]` |
| 密码 | SHA256 哈希存储（沿用现状），重置密码明文仅返回一次 |
| 禁用用户 | `validate_token` 层拦截，禁用即时生效，无需踢 token |
| 白名单 | 落库 + 开关 + 审计 |
| 公告 | 业务面只读 active，不暴露管理端点 |
| 防枚举 | 用户列表仅管理员可访问；用户详情接口校验权限点 |

---

## 十一、实施路线（里程碑）

| 里程碑 | 内容 | 门禁 |
|---|---|---|
| **A0** | RBAC 基建：表 + 种子角色 + `deps_admin` 鉴权依赖 + `user_accounts` 加列 | 单测绿；存量用户默认 role=user 不回归 |
| **A1** | 用户管理 + 白名单落库 + 审计日志框架 | 用户 CRUD/禁用/重置密码 + 白名单开关 E2E |
| **A2** | 运营看板：overview / repos / events / agents / im | 只读聚合，管理台可见 |
| **A3** | 治理能力：技能审核 / 公告 / 系统设置 / 审计检索导出 | 管理台全流程 E2E |
| **A4** | 前端 Admin SPA + 导航 + 确认弹窗 | Playwright UI + 手工验收 |

> 详细拆解见 `admin-ops-implementation-plan.md`，验收用例见 `admin-ops-acceptance-plan.md`。

---

## 十二、风险与对策

| 风险 | 对策 |
|---|---|
| 存量用户无角色 | 默认 `role='user'`，`admin_role_assignments` 空 = 无管理权限，零改动 |
| `validate_token` 加禁用拦截影响在线用户 | 仅 `status=disabled` 拦截，禁用前管理台提示；回归用例覆盖正常用户 |
| 白名单落库迁移 | 首次启动 JSON 种子导入幂等；`is_gitcode_user_allowed` JSON 兜底保兼容 |
| 管理面端点暴露面扩大 | 全部走 RBAC 权限点；审计留痕；前端按权限隐藏入口 |
| Runtime 演进冲突 | 只读聚合 `backend_agents`/`agent_runtimes`，不写运行时状态 |
| 审核位影响既有上传 | `audit_status` 默认 `approved`，上传流程不变，审核为可选治理 |
