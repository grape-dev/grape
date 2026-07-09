# DevOps Bot

**无智能的事件编排器**，通过 CLI 调用外部 Agent（opencode）实现 DevOps 事件的智能化处理。Bot 是壳，Agent 是脑。

事件 → 配置文件路由 → domain skill（自包含原则 + API 参考）→ Agent 按流程执行。

## 支持的入口

| 入口 | 方式 | 状态 |
|------|------|------|
| GitCode | HTTP Webhook | ✅ 运行中 |
| 飞书 | WebSocket 长连接（lark-oapi SDK） | ✅ 运行中 |

## Skills 库（8 个）

| Skill | 说明 |
|-------|------|
| `devops-event-ops` | 非 Issue/PR 事件路由入口 |
| `issue` | Issue 全生命周期（分类、标签、分配、评论） |
| `pr` | PR 全生命周期（代码审查、安全扫描、合并检查） |
| `build-debug` | 构建失败诊断 |
| `deploy-guard` | 部署安全守卫 |
| `incident-triage` | 事件分诊与定级 |
| `changelog-gen` | 变更日志生成 |
| `chat` | 飞书 IM 对话 |

## 快速开始

```bash
git clone https://gitcode.com/Jason7602/devops-bot.git
cd devops-bot
pip install lark-oapi websocket-client
cp .env.example .env
# 编辑 .env 填入 Token
python -m src.main
```

## 飞书命令

| 命令 | 说明 |
|------|------|
| `/bind owner/repo` | 绑定群到仓库 |
| `/unbind` | 解绑 |
| `/status` | 查看绑定状态 |
| `/clean` | 清除 Agent 会话缓存 |
| `/help` | 帮助 |

## 项目结构

```
devops-bot/
├── src/                   # Python 源码
│   ├── main.py            # 启动入口
│   ├── config.py          # 环境变量 + 配置
│   ├── session_manager.py # Session + DB 管理
│   ├── prompt_builder.py  # Prompt 构建
│   ├── event_processor.py # 事件路由 + 处理
│   ├── agent_executor.py  # opencode 调度
│   ├── chat_handler.py    # 飞书对话处理
│   ├── webhook_handler.py # HTTP webhook 处理
│   └── feishu_handler.py  # 飞书 WS 集成
├── configs/               # 配置文件
│   └── event-routing.json # 事件路由配置
├── skills/                # 技能库（8 个）
│   ├── devops-event-ops/
│   ├── issue/
│   ├── pr/
│   ├── build-debug/
│   ├── deploy-guard/
│   ├── incident-triage/
│   ├── changelog-gen/
│   └── chat/
├── tests/                 # 测试文件
├── CODEOWNERS             # 模块责任人
├── data/                  # 运行时数据
└── .env                   # 环境变量
```

## License

MIT
