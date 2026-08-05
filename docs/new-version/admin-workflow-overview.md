# Agentic DevOps — 管理员业务流程设计

> 系统管理员视角的完整业务流程，涵盖系统部署、账户管理、全局配置、监控运维。

---

## 一、管理员角色概述

Agentic DevOps 中管理员负责：

| 职责领域 | 说明 |
|---------|------|
| **系统部署与维护** | 安装 Python 环境、Agent CLI、配置 systemd 服务 |
| **全局平台配置** | OAuth 应用注册、Webhook 全局 Token、飞书/IM 接入 |
| **账户生命周期管理** | 创建/禁用/删除用户账户，凭据审核 |
| **Agent 引擎管理** | 安装/升级/检测 opencode, codearts, hermes 等引擎 |
| **事件路由管理** | 全局事件→Skill 映射、仓库级覆盖规则 |
| **技能库管理** | 官方技能发布、自定义技能审核 |
| **监控与故障排查** | 系统健康检查、Agent 执行日志、告警处理 |

---

## 二、首次部署流程

### 2.1 服务器环境准备

```
管理员登录服务器
  │
  ├── 1. 安装 Python 3.11+
  │     apt install python3.11 python3.11-venv
  │
  ├── 2. 安装 Agent CLI
  │     which opencode → 引导安装
  │     which codearts → 引导安装
  │     which hermes   → 可选
  │
  ├── 3. 安装依赖
  │     pip install fastapi uvicorn paramiko lark-oapi pyyaml
  │
  ├── 4. 配置 systemd 服务
  │     cp devops-bot.service /etc/systemd/system/
  │     systemctl enable devops-bot.service
  │     systemctl start devops-bot.service
  │
  └── 5. 验证服务
        curl http://localhost:3000/health
        curl http://localhost:8080/health
```

### 2.2 GitCode OAuth 应用注册

```
管理员登录 GitCode 开发者设置页面
  │
  ├── 1. 创建 OAuth App
  │     设置 → 应用 → 创建 OAuth 应用
  │     填写: 应用名称, 回调 URL: http://{服务器IP}:3000/api/auth/callback/gitcode
  │
  ├── 2. 获取凭据
  │     App ID: cli_xxxxxxxxxxxx
  │     App Secret: xxxxxxxxxxxxxxxxxxxxxxxx
  │
  └── 3. 配置到系统
        管理员登录 Agentic DevOps → Settings → GitCode Repo
        填入 OAuth Client ID + Client Secret + Redirect URI
```

### 2.3 飞书 Bot 接入

```
管理员登录飞书开放平台
  │
  ├── 1. 创建企业自建应用 → 获取 App ID + App Secret
  │
  ├── 2. 配置机器人能力
  │     添加应用能力 → 机器人 → 配置
  │
  ├── 3. 配置权限
  │     im:message, im:message:send_as_bot, im:chat
  │     contact:user.employee_id:readonly
  │
  ├── 4. 配置事件订阅
  │     （如使用长连接模式）请求网址: http://{服务器IP}:8080/webhook/feishu
  │
  ├── 5. 发布应用 → 审核通过
  │
  └── 6. 配置到系统
        管理员登录 Agentic DevOps → Account → Chat Agent → IM 配置
        填入 App ID + App Secret
        或使用 Scan QR to auto-configure（见 feishu-agent-auto-config.md）
```

---

## 三、账户管理

### 3.1 创建用户账户

```
管理员通过 API 创建用户:
  │
  ├── POST /api/auth/register {username, password, role}
  │     创建本地用户 + 同步到 user_accounts 表
  │
  ├── 用户首次登录（通过 GitCode OAuth 自动创建）
  │     无需管理员手动创建，OAuth 回调自动注册
  │
  └── 管理员审核新注册用户
        查看 user_accounts 表 → 设置角色/权限
```

### 3.2 账户生命周期

| 状态 | 操作 | 说明 |
|------|------|------|
| Active | — | 正常状态 |
| Suspended | PUT `/api/admin/users/{id}/suspend` | 临时禁用 |
| Deleted | DELETE `/api/admin/users/{id}` | 永久删除（保留事件历史） |

### 3.3 凭据审计

```
管理员定期审查:
  ├── GitCode API Token 有效期
  ├── 华为云 AK/SK 权限范围
  ├── 飞书 App Secret 是否泄露
  └── Bot Token 是否需要轮换
```

---

## 四、全局配置管理

### 4.1 事件路由配置

```
管理员登录 → Settings → Event Routes
  │
  ├── 查看当前路由规则:
  │     事件类型 → 技能映射
  │     issue_opened → issue
  │     pull_request_opened → pr
  │     pull_request_merged → cloud-deploy
  │
  ├── 修改路由:
  │     PUT /api/settings/event-routes
  │     {event_type: "build_failure", skill: "sre"}
  │
  ├── 添加仓库级覆盖:
  │     {event_type: "pull_request_opened", repo_key: "owner/skill-repo", skill: "skill-pr"}
  │
  └── 即时生效 (无需重启服务)
```

### 4.2 Agent Backend 绑定

```
管理员配置每种 Agent 类型的默认 Backend:
  │
  │  GET /api/settings/agent-backends
  │  [
  │    {agent_type: "vod",      default_backend: "hermes"},
  │    {agent_type: "issue",    default_backend: "opencode"},
  │    {agent_type: "coding",   default_backend: "codearts"},
  │    {agent_type: "qa",       default_backend: "opencode"},
  │    {agent_type: "sre",      default_backend: "opencode"},
  │  ]
  │
  └── PUT /api/settings/agent-backends/vod {default_backend: "hermes"}
```

### 4.3 Agent 注册表管理

```
管理员管理 A2A 可用的 Agent 类型:
  │
  │  GET /api/settings/agent-registry
  │  [
  │    {name: "issue", description: "Issue 全生命周期管理",
  │     triggers: ["issue_opened","issue_comment"], tools: [...]},
  │    ...
  │  ]
  │
  ├── 新增 Agent 类型:
  │     INSERT INTO agent_registry ...
  │
  └── 删除 Agent 类型:
        DELETE FROM agent_registry WHERE name = ?
```

---

## 五、Backend Agent 引擎管理

### 5.1 引擎安装检测

```
管理员登录 → Settings → Backend Agents
  │
  ├── 系统自动检测已安装引擎:
  │     shutil.which("opencode")  → /usr/local/bin/opencode ✅
  │     shutil.which("codearts")  → /usr/local/bin/codearts ✅
  │     shutil.which("hermes")    → NOT FOUND ❌
  │
  ├── [⟳ Refresh All] → 重新检测
  │
  └── [Test Connection] → 验证引擎可用性
        opencode run --format json "Reply 'OK'" → ✅
```

### 5.2 引擎升级

```
管理员升级引擎:
  │
  ├── 1. 在服务器上安装新版本 CLI
  │     brew upgrade opencode / npm update -g codearts
  │
  ├── 2. 刷新 Backend Agents 页面
  │     → 版本号自动更新
  │
  └── 3. 测试连接验证新版本兼容性
```

---

## 六、技能库管理

### 6.1 官方技能发布

```
skills/
├── issue/SKILL.md           # Issue 管理官方技能
├── implementation/SKILL.md  # 代码实现官方技能
├── pr/SKILL.md              # PR 审查官方技能
├── changelog-gen/SKILL.md   # 变更日志官方技能
├── cloud-deploy/SKILL.md    # 部署官方技能
└── ...
```

管理员可编辑官方技能（`SKILL.md` 文件），修改后即时生效。

### 6.2 用户自定义技能审核

```
用户上传自定义 Skill:
  │
  ├── 1. 系统自动校验:
  │     ✓ 包含 SKILL.md
  │     ✓ YAML Front Matter 必填（name/version/description）
  │     ✓ name 符合 kebab-case 且与目录名一致
  │     ✓ version 符合语义化
  │     ✓ description ≤ 200 字
  │     ✓ 不含 .py/.sh/.jar 等可执行文件
  │     ✓ 总大小 ≤ 10MB
  │     ✓ 必须选择技能类型（vod/issue/coding/qa/sre/chat）
  │     （不强制特定正文章节，与官方技能格式一致）
  │
  ├── 2. 管理员可选审核:
  │     查看 skills/custom/{user_id}/{skill_name}/SKILL.md
  │
  └── 3. 用户通过 repo_skill_mapping 绑定到仓库
```

用户可从 Skills 页「＋新增技能」弹窗下载技能模板 ZIP（`GET /api/skills/template`，内含 `my-skill/` 示例），按模板格式制作自定义技能。

---

## 七、监控与运维

### 7.1 系统健康检查

```
管理员日常巡检:
  │
  ├── GET /health → {"status":"ok", "agents":[...], "skills":[...]}
  │
  ├── Backend Agents 页面 → 引擎运行状态
  │
  ├── systemd 日志:
  │     journalctl -u devops-bot.service --since "1 hour ago"
  │
  └── 数据库检查:
        sqlite3 data/sessions.db "SELECT COUNT(*) FROM events"
```

### 7.2 常见运维任务

| 任务 | 操作 |
|------|------|
| 服务重启 | `systemctl restart devops-bot` |
| 查看日志 | `journalctl -u devops-bot -f` |
| 清理过期事件 | `DELETE FROM events WHERE created_at < datetime('now','-30 days')` |
| 备份数据库 | `cp data/sessions.db data/backup/sessions_$(date +%Y%m%d).db` |
| Webhook 重连 | 在 GitCode Repo 页面重新保存 Webhook Token |
| 清除 Agent Session | `/clean` 命令 或 `UPDATE agent_sessions SET status='idle'` |

### 7.3 告警规则

| 告警项 | 阈值 | 通知方式 |
|--------|------|---------|
| Agent 引擎不可用 | 实时 | IM 通知管理员 |
| Webhook 响应 4xx/5xx | 连续 5 次 | IM 通知管理员 |
| Agent 执行超时 | 单次 > 120s | 记录日志 |
| 事件队列积压 | > 20 个排队 | 记录日志 + IM 通知 |
| 数据库文件增长 | > 100MB | 建议手动清理 |

---

## 八、开放兼容扩展

基于 `tech-design.md §20` 的插件模型，管理员负责将新的第三方组件接入系统。

### 8.1 接入第三方 Agent 引擎

```
管理员接入新引擎 (如 Hermes):
  │
  ├── 1. 在服务器安装 hermes CLI
  │
  ├── 2. 创建 src/agent/hermes.py → class HermesBackend
  │
  ├── 3. 注册到 _BACKENDS
  │
  └── 4. 重启服务 → Backend Agents 页面自动检测
```

### 8.2 接入新 IM 通道

```
管理员接入企业微信:
  │
  ├── 1. 在企业微信管理后台创建应用 → 获取 Webhook URL + Token
  │
  ├── 2. ALTER TABLE user_accounts ADD COLUMN wecom_webhook_url
  │
  ├── 3. Chat Agent Config 页新增 WeCom 配置表单
  │
  └── 4. 用户可自行填入企业微信凭据
```

### 8.3 接入新 VCS 平台

```
管理员接入 GitHub:
  │
  ├── 1. 在 GitHub 创建 OAuth App → 获取 Client ID + Secret
  │
  ├── 2. 创建 src/vcs/github.py → class GitHubBackend
  │
  ├── 3. 新增路由: POST /webhook/github
  │
  └── 4. 重启服务 → 支持 GitHub 事件处理
```

---

## 九、安全审计

### 9.1 定期安全检查

| 检查项 | 频率 | 操作 |
|--------|------|------|
| Token 过期检查 | 每周 | 检查 user_accounts 中的 API Token |
| Webhook Token 轮换 | 每月 | 更新 GitCode Repo 中的 Webhook Token |
| OAuth App 审核 | 每季度 | 检查 GitCode OAuth App 权限范围 |
| 备份验证 | 每周 | 验证 SQLite 备份可恢复 |
| 依赖漏洞扫描 | 每月 | `pip audit` 或 `safety check` |

### 9.2 访问日志

```
关键操作日志:
  ├── 用户登录/注销
  ├── Webhook Token 修改
  ├── 事件路由修改
  ├── Agent 引擎配置变更
  └── 技能上传/删除
```
