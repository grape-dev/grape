# DevOps Bot Skills 库

本目录是 DevOps Bot 的**项目级 Skills 库**，存放面向 DevOps 领域的专属技能定义。这些 Skills 作为接入 Agent（OpenCode、Claude、OpenClaw 等）的项目级技能目录，Agent 在处理 DevOps 事件时会自行发现和触发所需技能。

## 设计理念

```
DevOps Bot = 事件编排器，只负责把 Skills 路径告诉 Agent
Skills    = Agent 的项目级 DevOps 领域知识库
Agent     = 收到 Skills 路径后，自行发现、匹配、触发技能
```

- **Bot 不注入 Skills 内容** — Bot 不读取 SKILL.md，不拼接到 Prompt 中
- **Bot 只传递路径** — 通过环境变量 `DEVOPS_SKILLS_DIR` 将 Skills 目录的绝对路径传给 Agent
- **Agent 自主决策** — Agent 根据事件上下文，自行判断需要激活哪些 Skills
- **Skills 是 Agent 的知识，不是 Bot 的逻辑** — Bot 不理解 Skill 内容，也不干预 Agent 的技能选择

## 工作机制

```
1. Webhook 事件到达
2. Bot 路由匹配 → 创建 Session
3. Bot 调用 Agent CLI，传入:
   - DEVOPS_SKILLS_DIR=/path/to/skills   ← Skills 路径
   - DEVOPS_SESSION_ID=sess-xxx          ← Session 标识
   - Prompt（事件模板渲染结果）
4. Agent 启动后:
   - 读取 DEVOPS_SKILLS_DIR 下的技能目录
   - 根据事件类型和上下文，自行匹配并加载需要的 Skills
   - 执行技能中定义的步骤
```

## 目录结构

```
skills/
├── README.md                    # 本文件
├── SKILL-SPEC.md                # Skill 规范定义
├── build-debug/                 # 构建失败诊断技能
│   └── SKILL.md
├── changelog-gen/               # 变更日志生成技能
│   └── SKILL.md
├── code-review/                 # 代码审查技能
│   └── SKILL.md
├── comment/                     # 用户评论处理技能
│   └── SKILL.md
├── deploy-guard/                # 部署安全守卫技能
│   └── SKILL.md
├── devops-event-ops/            # 事件运营全链路技能
│   └── SKILL.md
├── gitcode-api-guidance/        # GitCode API 交互技能
│   └── SKILL.md
├── incident-triage/             # 事件分诊技能
│   └── SKILL.md
└── security-scan/               # 安全扫描技能
    └── SKILL.md
```

## 环境变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `DEVOPS_SKILLS_DIR` | Skills 目录的绝对路径 | `/app/skills` |
| `DEVOPS_SESSION_ID` | 当前 Session ID | `sess-1704067200000000000` |

Agent 启动时通过这两个环境变量获知上下文，`DEVOPS_SKILLS_DIR` 下每个子目录即是一个 Skill，Agent 读取其中的 `SKILL.md` 获取技能定义。

## 如何创建新 Skill

1. 在 `skills/` 下创建目录，如 `skills/my-skill/`
2. 在其中创建 `SKILL.md`，按 [SKILL-SPEC.md](./SKILL-SPEC.md) 规范编写
3. 无需修改 Bot 代码或配置 — Agent 会自动发现新 Skill

## 已有 Skills

| Skill | 说明 | 适用事件 |
|-------|------|---------|
| `code-review` | 代码审查最佳实践、检查清单 | push, pull_request |
| `build-debug` | 构建失败诊断方法论 | build_failure |
| `deploy-guard` | 部署前安全与合规检查 | deploy_start, build_success |
| `incident-triage` | 生产事件分诊与定级 | deploy_failure, build_failure |
| `security-scan` | 安全漏洞扫描与修复建议 | push, pull_request |
| `changelog-gen` | 自动生成变更日志 | push, release |
| `gitcode-api-guidance` | GitCode API 交互指南 | 任意 GitCode 事件 |
| `devops-event-ops` | 事件运营全链路编排 | 任意 DevOps 事件 |
| `comment` | 用户评论处理与能力边界管理 | issue_comment, chat |
