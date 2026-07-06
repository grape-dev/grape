# DevOps Bot

DevOps Bot 是一个**无智能的事件编排器**，它本身不做任何智能决策，而是通过 CLI 调用外部 Agent（OpenCode、Claude、OpenClaw 等）来实现 DevOps 事件的智能化处理。

可以理解为：DevOps Bot 是 Agent 的**面向 DevOps 领域的外挂工具**，统一桥接 GitCode/GitHub/GitLab/Jenkins/Spanner 等 CI/CD 工具的消息事件，并支持通过飞书等 IM 工具交互。

## 核心设计

```
Bot = 壳（事件编排器），无智能
Agent = 脑（opencode），自主决策执行
```

```
Webhook 事件 → 归一化 → 规则路由 → 创建 Session → CLI 调用 Agent → 处理结果
                                                          │
                                                     失败 → 委托 Agent 自分析
```

- **Bot 是"壳"，Agent 是"脑"** — Bot 只负责把 DevOps 事件翻译成 Agent 能理解的 Prompt
- **一个仓库 = 一个 Session** — 同一 repo 的所有事件进入同一 Session，Agent 可看到项目完整上下文
- **失败自愈** — 失败后将失败上下文交给 Agent，让 Agent 理解失败并决策
- **规则驱动** — 事件路由基于事件类型和来源路由到不同的 Skills
- **SKIP 机制** — Agent 输出 `SKIP` 时 Bot 不发送任何内容，避免无关回复
- **能力边界** — Agent 受角色约束：只处理 DevOps 话题，不写代码、不探索、不自取数据

## 支持的入口

| 入口 | 方式 | 状态 |
|------|------|------|
| GitCode | HTTP Webhook（Cloudflare Tunnel） | ✅ 已验证 |
| GitHub | HTTP Webhook | ✅ 代码就绪 |
| GitLab | HTTP Webhook | ✅ 代码就绪 |
| Jenkins | HTTP Webhook | ✅ 代码就绪 |
| Spanner | HTTP Webhook | ✅ 代码就绪 |
| 飞书 | WebSocket 长连接（官方 `lark-oapi` SDK） | ✅ 已验证 |

## 支持的 Agent

| Agent | CLI 命令 | 说明 |
|-------|---------|------|
| OpenCode | `opencode` | 默认启用，NDJSON 流式解析 |
| Claude | `claude` | 需在配置中启用 |
| OpenClaw | `openclaw` | 需在配置中启用 |

## Skills 库（9 个）

| Skill | 说明 | 适用事件 |
|-------|------|---------|
| `code-review` | 代码审查最佳实践与检查清单 | push, pull_request |
| `build-debug` | 构建失败诊断方法论与常见模式 | build_failure |
| `deploy-guard` | 部署前安全与合规检查守卫 | deploy_start, build_success |
| `incident-triage` | 生产事件分诊与定级方法论 | deploy_failure, build_failure |
| `security-scan` | 安全漏洞扫描与修复建议 | push, pull_request |
| `changelog-gen` | 自动生成变更日志 | push, release |
| `gitcode-api-guidance` | GitCode API 完整交互参考 | GitCode 事件 |
| `devops-event-ops` | 事件编排全链路管理 | 所有事件 |
| `comment` | 用户评论处理与能力边界管理 | issue_comment, chat |

## 快速开始

### 前置条件

- Python 3.14+
- opencode CLI（或其他 Agent CLI）
- GitCode Token（或其他事件源 Token）

### 安装与运行

```bash
git clone https://gitcode.com/Jason7602/devops-bot.git
cd devops-bot

# 安装依赖
pip install lark-oapi websocket-client

# 配置
cp .env.example .env
# 编辑 .env 填入 Token

# 运行
python devops_bot.py
```

### 配置 Webhook

以 GitCode 为例，设置 Cloudflare Tunnel 暴露本地服务：

```powershell
# 启动 Tunnel
Start-Process -FilePath "cloudflared.exe" `
  -ArgumentList "tunnel --url http://localhost:8080 --logfile data\tunnel.log"

# 获取 Tunnel URL
Select-String "trycloudflare" data\tunnel.log

# 在 GitCode 仓库 Settings → Webhooks 中添加：
# URL: https://xxx.trycloudflare.com/webhook/gitcode
```

## 架构

```
                    ┌─────────────────────────────────────────┐
                    │           DevOps Bot (Python)            │
                    │                                         │
GitCode ──────→  │  ┌──────────┐    ┌────────────────────┐ │
Webhook          │  │ Webhook  │    │  Session Manager   │ │
                 │  │ Handler  │───→│  repo_key → Session │ │
                 │  └──────────┘    │  Session → history  │ │
                 │                  │  Session → oc_sid   │ │
飞书消息 ──────→  │  ┌──────────┐    └────────┬───────────┘ │
                 │  │ Feishu   │           │              │
                 │  │ WSClient │───────────┘              │
                 │  └──────────┘                          │
                 │         │                              │
                 │  ┌──────▼───────┐                      │
                 │  │  build_prompt │                     │
                 │  │  + role def  │                     │
                 │  └──────┬───────┘                      │
                 │         │                              │
                 │  ┌──────▼───────┐                      │
                 │  │  opencode    │                      │
                 │  │  NDJSON 流   │                      │
                 │  └──────┬───────┘                      │
                 │         │                              │
                 │  ┌──────▼───────┐                      │
                 │  │  输出路由    │                      │
                 │  │  Web→GitCode│                      │
                 │  │  Chat→飞书  │                      │
                 │  └──────────────┘                      │
                 └─────────────────────────────────────────┘
```

## 事件处理流程

```
1. Webhook/消息到达
2. normalize_event → 统一事件格式
3. get_or_create_session → 按 repo_key 复用
4. build_prompt → 包含事件 + Session 历史 + Skills + 角色定义
5. stream_opencode → NDJSON 流式解析
   ├─ step_start → "Step N started"
   ├─ tool_use → Agent 工具调用
   ├─ text → Agent 分析输出
   └─ done → 完成
6. 判断 Agent 输出：
   ├─ "SKIP" → 不发送
   └─ 其他 → post_comment / reply_text
7. 失败 → FailureHandler → Agent 自分析
```

## Session 管理

| 维度 | 机制 |
|------|------|
| 粒度 | 一个仓库一个 Session |
| 键值 | `repo_key = "{source}:{repository}"` |
| 存储 | SQLite（`data/sessions.db`） |
| Bot Session | 存 EventHistory，失败重置 |
| opencode Session | `--session` 原生上下文续传 |
| 飞书绑定 | 自然语言绑定，支持多仓库查询 |

## 飞书集成

Bot 通过官方 `lark-oapi` SDK 建立 WebSocket 长连接，无需公网回调地址。

### 命令

| 命令 | 说明 |
|------|------|
| `/bind owner/repo` | 绑定群到仓库 |
| `/unbind` | 解绑 |
| `/status` | 查看绑定状态 |
| `/help` | 帮助 |

### 自然语言绑定

```
绑定到 Jason7602/devops-bot-test
使用 Jason7602/devops-bot-test
```

### 交互流程

- **已绑定仓库** → 直接送 Agent 处理
- **未绑定 + 有 Session 记录** → 列出可用仓库 + 绑定建议
- **未绑定 + 无 Session 记录** → 提示先触发 GitCode 事件

## 配置说明

### .env

```ini
GITCODE_WEBHOOK_TOKEN=your_bot_token
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=your_app_secret
```

### 能力边界

Agent 受角色约束（`build_role_definition` + `comment` Skill）：

| 场景 | 行为 |
|------|------|
| DevOps 话题 | ✅ 分析并回复 |
| @提及 + 任意话题 | ✅ 礼貌回复，说明 DevOps 能力范围 |
| 无 @提及 + 无关话题 | ✅ 输出 `SKIP`，不回复 |
| 写代码、改文件 | ❌ 禁止 |

## 项目结构

```
devops-bot/
├── devops_bot.py              # 主程序
├── feishu_handler.py          # 飞书集成（lark-oapi SDK WS）
├── cloudflare/                # Cloudflare Tunnel
├── skills/                    # Skills 库（9 个）
│   ├── comment/               #   评论处理 + 能力边界
│   ├── code-review/           #   代码审查
│   ├── build-debug/           #   构建诊断
│   ├── deploy-guard/          #   部署守卫
│   ├── incident-triage/       #   事件分诊
│   ├── security-scan/         #   安全扫描
│   ├── changelog-gen/         #   变更日志
│   ├── gitcode-api-guidance/  #   GitCode API 参考
│   └── devops-event-ops/      #   事件编排参考
├── configs/                   # 配置文件
├── data/                      # 运行时数据（SQLite + 日志）
└── .env                       # 环境变量
```

## License

MIT
