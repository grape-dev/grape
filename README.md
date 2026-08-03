# DevOps Bot

**Agent 驱动的 DevOps 事件编排器**。Bot 监听 GitCode Webhook 和飞书 IM 消息，通过 opencode / codearts CLI 调用 Agent 实现 Issue 分诊、PR 审查、自动代码实现等 DevOps 全流程自动化。Bot 是壳，Agent 是脑。

## 支持的入口

| 入口 | 方式 | 状态 |
|------|------|------|
| GitCode | HTTP Webhook | ✅ 运行中 |
| 飞书 | WebSocket 长连接（lark-oapi SDK） | ✅ 运行中 |

## 多后端 Agent

| 后端 | CLI | 认证方式 | 适用场景 |
|------|-----|----------|----------|
| opencode | `opencode run --format json` | GitCode API Token | 开源场景、开发调试 |
| codearts | `codearts run --format json` | 华为云 AK/SK | 华为云 CodeArts 集成 |

两个后端通过 `configs/agent-routing.json` 按 Skill 路由，可混合使用。

## 多租户

全局 Bot 配置（`configs/global-bot-token.json`）：
- GitCode Bot Token / Webhook Token / OAuth 凭据
- `bot_usernames`（self-comment 检测用 bot 用户名）

用户账户（含各自的 GitCode Token、飞书凭据、华为云 AK/SK）通过 GitCode OAuth 登录后动态创建，存储在数据库 `user_accounts` 表。

## Skills 库

| Skill | 说明 |
|-------|------|
| `issue` | Issue 全生命周期（分类、标签、CODEOWNERS 分配） |
| `pr` | PR 审查（差异分析、安全扫描、合并检查、评论回复） |
| `qa` | PR 审查 + 工作流质量审查（pr 的增强版备选：Agent Work Loop 五维模型，按仓库启用） |
| `implementation` | 自主开发实现（spec → plan → TDD → 增量代码 → self-review → PR） |
| `build-debug` | 构建失败诊断与修复建议 |
| `deploy-guard` | 部署安全守卫与合规检查 |
| `incident-triage` | 事件分诊与定级 |
| `changelog-gen` | 变更日志生成与发布说明 |
| `im-notify` | 飞书通知 — 接收 GitCode 事件，按事件类型决策发送通知 |
| `chat` | 飞书 IM 对话、意见反馈、实施审批 |
| `agent` | 5 个 agent-skills 子技能（spec-driven-dev / planning / TDD / incremental-impl / code-review） |

## 核心能力

| 能力 | 说明 |
|------|------|
| **Issue 分诊** | CODEOWNERS 触发词 + 语义匹配 → 自动标签 + 分配责任人 |
| **飞书通知** | webhook 事件自动前置路由至 `im-notify` 技能，Agent 按事件类型决策通知 |
| **GitCode ↔ 飞书映射** | 绑定群时自动 name-matching，按需 API 查询 open_id |
| **自动实现开关** | `/autodev on/off` 控制；ON 时 Bug 自动修复、Feature 征求同意后开发 |
| **飞书通知开关** | `/notify on/off` 控制飞书推送通知 |
| **CODEOWNERS 兜底** | 名名匹配失败时通知仓库管理员 |
| **PR 关联 Issue** | `inner_issue_nums` 字符串格式 + `close_related_issue` + PATCH `body/labels/prune_branch` |
| **Session 历史文件** | 每次操作后写入 `data/workspaces/history/{account}/{repo}/session_history.json`，Agent 可自行 `read` 获取上下文 |

## 飞书命令

| 命令 | 说明 |
|------|------|
| `/bind owner/repo` | 绑定当前群到仓库 |
| `/unbind` | 解绑并清除用户映射 |
| `/status` | 查看当前绑定仓库 |
| `/refresh` | 刷新 GitCode ↔ 飞书用户映射 |
| `/autodev on/off` | 自动实现开关 |
| `/notify on/off` | 飞书通知开关 |
| `/clean` | 清除所有后端 Agent 会话缓存（下次触发全新会话） |
| `/help` | 帮助 |

## 快速开始

```bash
git clone https://gitcode.com/Jason7602/devops-bot.git
cd devops-bot
pip install lark-oapi websocket-client pytest paramiko
cp configs/global-bot-token.example.json configs/global-bot-token.json
# 编辑 global-bot-token.json 填入 GitCode Bot Token / OAuth 凭据
python -m src.main
```

## 事件处理流程

```
GitCode webhook 到达
  → event_processor.py（归一化）
    → 前置通知: im-notify 技能（异步，不受防循环限制）
    → 防循环检查（bot 自身事件跳过）
    → 按 event-routing.json 分配到对应 Skill（issue / pr / impl 等）
```

- `Issue Hook` 按 `action` 区分：`open` → `issue_opened`，`update`/`close` → `issue_updated`
- `im-notify` 技能独立处理通知逻辑，不阻塞主流程

## 测试

```bash
pip install pytest pytest-mock
python -m pytest tests/ -v
```

共 **136** 个测试用例，覆盖：

| 模块 | 用例数 | 说明 |
|------|--------|------|
| `test_session_manager` | 25 | Session CRUD、Feishu 映射表、autodev/notify 开关、并发安全、历史文件转储 |
| `test_config` | 5 | 事件路由、fallback、自定义路由 |
| `test_prompt_builder` | 20 | Issue/PR/评论 prompt 构建、Feishu context 注入、角色定义 |
| `test_event_processor` | 19 | normalize_event（GitCode/Issue Hook 动作/GBK/字节流/边界） |
| `test_webhook_handler` | 12 | Health/Sessions/UserMapping API、Token 校验 |
| `test_feishu_handler` | 21 | Card 构建、Token 缓存、消息发送、群成员分页、清理命令 |
| `test_chat_handler` | 6 | 命令路由、绑定检测、能力查询 |
| `test_agent_executor` | 20 | opencode/codearts 参数构建、NDJSON 解析、环境变量 |

## 配置

### 全局 Bot 配置 (`configs/global-bot-token.json`)

```json
{
  "gitcode_api_token": "",
  "gitcode_webhook_token": "",
  "gitcode_oauth_client_id": "",
  "gitcode_oauth_client_secret": "",
  "gitcode_oauth_redirect_uri": "",
  "bot_usernames": []
}
```

首次运行时会自动加载到 `meta` 表（OAuth 配置）并作为全局 Bot token fallback。用户账户通过 GitCode OAuth 登录动态创建，无需预置。

### Agent 路由 (`configs/agent-routing.json`)

按 Skill 指定使用 opencode 还是 codearts 后端。

### 事件路由 (`configs/event-routing.json`)

事件类型 → Skill 映射。

## 项目结构

```
devops-bot/
├── src/
│   ├── main.py                 # 启动入口（HTTP server + WS 线程）
│   ├── config.py               # 环境变量 + 事件/Agent 路由 + 多账户配置
│   ├── session_manager.py      # SQLite Session + Feishu 用户映射 + 历史文件转储
│   ├── prompt_builder.py       # Agent Prompt 构建（含 history 文件指引）
│   ├── event_processor.py      # 事件归一化 + 路由 + 执行
│   ├── webhook_handler.py      # HTTP Webhook 处理器
│   ├── agent/
│   │   ├── base.py             # AgentBackend 抽象基类
│   │   ├── executor.py         # 流式子进程调度（NDJSON 事件循环）
│   │   ├── opencode.py         # opencode 后端（--session, --dangerously-skip-permissions）
│   │   └── codearts.py         # codearts 后端（stdin 传 prompt, 华为云 AK/SK）
│   ├── im/
│   │   ├── handler.py          # 飞书 IM 消息处理
│   │   └── feishu.py           # 飞书 API（WS + REST）
│   ├── vcs/
│   │   └── gitcode.py          # GitCode VCS 适配器
│   └── admin/
│       └── server.py           # 管理控制台（Vue 3 SPA）
├── configs/
│   ├── global-bot-token.json    # 全局 Bot token + OAuth 凭据（gitignored）
│   ├── event-routing.json      # 事件 → Skill 路由
│   └── agent-routing.json      # Skill → Agent 后端路由
├── docs/
│   └── codearts-cli-guide.md   # CodeArts CLI 使用指南
├── skills/                     # 技能库
│   ├── issue/pr/implementation/build-debug/...
│   ├── im-notify/              # 飞书通知技能
│   └── agent/                  # 5 个 agent-skills 子技能
├── tests/                      # 136 个测试用例（pytest）
├── data/
│   ├── sessions.db             # SQLite 数据库
│   ├── workspaces/             # 工作区 + Agent session 历史
│   └── devops-bot.log          # 运行日志（按天轮转）
└── CODEOWNERS                  # 模块责任人配置
```

## Directory Structure

The workspace directory `data/workspaces/` contains:

| 路径 | 说明 |
|------|------|
| `workspaces/history/{account}/{repo}/session_history.json` | 最近 10 条事件（有分片时按序号递增） |
| `workspaces/{repo}/` | Agent 执行工作区（.codeartsdoer 配置等） |

## License

MIT
