# DevOps Bot

**Agent 驱动的 DevOps 事件编排器**。Bot 监听 GitHub/GitCode Webhook 和飞书 IM 消息，通过 opencode CLI 调用 Agent 实现 Issue 分诊、PR 审查、自动代码实现等 DevOps 全流程自动化。Bot 是壳，Agent 是脑。

事件 → 配置文件路由 → domain skill（自包含原则 + API 参考）→ Agent 按流程执行。

## 支持的入口

| 入口 | 方式 | 状态 |
|------|------|------|
| GitCode | HTTP Webhook | ✅ 运行中 |
| 飞书 | WebSocket 长连接（lark-oapi SDK） | ✅ 运行中 |

## Skills 库（10 个）

| Skill | 说明 |
|-------|------|
| `issue` | Issue 全生命周期（分类、标签、CODEOWNERS 分配、通知、飞书映射） |
| `pr` | PR 审查（差异分析、安全扫描、合并检查、评论回复） |
| `implementation` | 自主开发实现（spec → plan → TDD → 增量代码 → self-review → PR） |
| `build-debug` | 构建失败诊断与修复建议 |
| `deploy-guard` | 部署安全守卫与合规检查 |
| `incident-triage` | 事件分诊与定级 |
| `changelog-gen` | 变更日志生成与发布说明 |
| `chat` | 飞书 IM 对话、意见反馈、实施审批 |
| `agent` | 5 个 agent-skills 子技能（spec-driven-dev / planning / TDD / incremental-impl / code-review） |

## 核心能力

| 能力 | 说明 |
|------|------|
| **Issue 分诊** | CODEOWNERS 触发词 + 语义匹配 → 自动标签 + 分配责任人 |
| **飞书通知** | 分诊后主动通知责任人（Bug/Feature 区别文案）；PR 创建后通知检视人 |
| **GitCode ↔ 飞书映射** | 绑定群时自动 name-matching，按需 API 查询 open_id |
| **自动实现开关** | `/autodev on/off` 控制；ON 时 Bug 自动修复、Feature 征求同意后开发 |
| **飞书通知开关** | `/notify on/off` 控制飞书推送通知 |
| **CODEOWNERS 兜底** | 名名匹配失败时通知仓库管理员 |
| **PR 关联 Issue** | `inner_issue_nums` 字符串格式 + `close_related_issue` + PATCH `body/labels/prune_branch` |

## 飞书命令

| 命令 | 说明 |
|------|------|
| `/bind owner/repo` | 绑定当前群到仓库 |
| `/unbind` | 解绑并清除用户映射 |
| `/status` | 查看当前绑定仓库 |
| `/refresh` | 刷新 GitCode ↔ 飞书用户映射 |
| `/autodev on/off` | 自动实现开关 |
| `/notify on/off` | 飞书通知开关 |
| `/clean` | 清除 Agent 会话缓存 |
| `/help` | 帮助 |

## 快速开始

```bash
git clone https://gitcode.com/Jason7602/devops-bot.git
cd devops-bot
pip install lark-oapi websocket-client pytest
cp .env.example .env
# 编辑 .env 填入 Token
python -m src.main
```

## 测试

```bash
pip install pytest
python -m pytest tests/ -v
```

共 **87** 个测试用例，覆盖：

| 模块 | 用例数 | 说明 |
|------|--------|------|
| `test_session_manager` | 17 | Session CRUD、Feishu 映射表、autodev/notify 开关、并发安全 |
| `test_config` | 5 | 事件路由、fallback、自定义路由 |
| `test_prompt_builder` | 20 | Issue/PR/评论 prompt 构建、Feishu context 注入、角色定义 |
| `test_event_processor` | 14 | normalize_event（GitCode/GitLab/GBK/字节流/边界） |
| `test_webhook_handler` | 12 | Health/Sessions/UserMapping API、Token 校验 |
| `test_feishu_handler` | 15 | Card 构建、Token 缓存、消息发送、群成员分页 |
| `test_chat_handler` | 6 | 命令路由、绑定检测、能力查询 |

## 项目结构

```
devops-bot/
├── src/                   # Python 源码
│   ├── main.py            # 启动入口（HTTP server + WS 线程）
│   ├── config.py          # 环境变量 + 事件路由配置
│   ├── session_manager.py # SQLite Session + Feishu 用户映射
│   ├── prompt_builder.py  # Agent Prompt 构建
│   ├── event_processor.py # 事件归一化 + 路由 + 执行
│   ├── agent_executor.py  # opencode CLI 子进程调度
│   ├── chat_handler.py    # 飞书 IM 消息处理
│   ├── webhook_handler.py # HTTP Webhook 处理器
│   └── feishu_handler.py  # 飞书 API（WS + REST）
├── configs/
│   └── event-routing.json # 事件 → Skill 路由配置
├── skills/                # 技能库（10 个 domain skills）
│   ├── issue/             # Issue 运营
│   ├── pr/                # PR 审查
│   ├── implementation/    # 自主开发实现
│   ├── build-debug/       # 构建诊断
│   ├── deploy-guard/      # 部署守卫
│   ├── incident-triage/   # 事件分诊
│   ├── changelog-gen/     # 变更日志
│   ├── chat/              # 飞书对话
│   └── agent/             # 5 个 agent-skills 子技能
├── tests/                 # 87 个测试用例（pytest）
├── CODEOWNERS             # 模块责任人配置
├── data/                  # 运行时数据（DB / workspace / logs）
└── .env                   # 环境变量
```

## License

MIT
