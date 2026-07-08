# DevOps Bot

**无智能的事件编排器**，通过 CLI 调用外部 Agent（opencode）实现 DevOps 事件的智能化处理。Bot 是壳，Agent 是脑。

## 支持的入口

| 入口 | 方式 | 状态 |
|------|------|------|
| GitCode | HTTP Webhook | ✅ 运行中 |
| GitHub/GitLab/Jenkins/Spanner | HTTP Webhook | ✅ 代码就绪 |
| 飞书 | WebSocket 长连接（lark-oapi SDK） | ✅ 运行中 |

## Skills 库（8 个）

| Skill | 说明 |
|-------|------|
| `devops-event-ops` | 事件路由入口 + 公共规范 |
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
python devops_bot.py
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
├── devops_bot.py          # 主程序
├── feishu_handler.py      # 飞书集成
├── skills/                # 技能库（8 个）
│   ├── devops-event-ops/
│   ├── issue/
│   ├── pr/
│   ├── build-debug/
│   ├── deploy-guard/
│   ├── incident-triage/
│   ├── changelog-gen/
│   └── chat/
├── CODEOWNERS             # 模块责任人
├── data/                  # 运行时数据
└── .env                   # 环境变量
```

## License

MIT
