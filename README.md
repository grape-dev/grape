# DevOps Bot

DevOps Bot 是一个**无智能的事件编排器**，它本身不做任何智能决策，而是通过 CLI 调用外部 Agent（OpenCode、Claude、OpenClaw 等）来实现 DevOps 事件的智能化处理。

可以理解为：DevOps Bot 是 Agent 的**面向 DevOps 领域的外挂工具**，统一桥接 GitCode/GitHub/GitLab/Jenkins/Spanner 等 CI/CD 工具的消息事件。

## 核心设计

```
Webhook 事件 → 归一化 → 规则路由 → 创建 Session → CLI 调用 Agent → 处理结果
                                                          │
                                                     失败 → 委托 Agent 自分析
```

- **Bot 是"壳"，Agent 是"脑"** — Bot 只负责把 DevOps 事件翻译成 Agent 能理解的 Prompt
- **一个事件 = 一个 Session** — 短期会话，处理完即归档
- **失败自愈** — 失败后将失败上下文交给 Agent，让 Agent 理解失败并决策
- **规则驱动** — 事件路由在 `rules.yaml` 中声明式配置，不硬编码

## 支持的事件源

| 事件源 | Webhook 端点 | 签名验证 |
|--------|-------------|---------|
| GitHub | `POST /webhook/github` | HMAC-SHA256 (`X-Hub-Signature-256`) |
| GitLab | `POST /webhook/gitlab` | Token (`X-GitLab-Token`) |
| GitCode | `POST /webhook/gitcode` | Signature / Token |
| Jenkins | `POST /webhook/jenkins` | Token 参数 |
| Spanner | `POST /webhook/spanner` | Token Header |

## 支持的 Agent

| Agent | CLI 命令 | 说明 |
|-------|---------|------|
| OpenCode | `opencode` | 默认启用 |
| Claude | `claude` | 需在配置中启用 |
| OpenClaw | `openclaw` | 需在配置中启用 |

## 快速开始

### 前置条件

- Go 1.22+
- 至少安装一个 Agent CLI（如 [OpenCode](https://opencode.ai)）

### 安装与运行

```bash
# 克隆项目
git clone <repo-url> && cd devops-bot

# 下载依赖
go mod tidy

# 编译运行
make run
```

或直接：

```bash
go run ./cmd/devops-bot --config ./configs/config.yaml --rules ./configs/rules.yaml
```

### 配置 Webhook

以 GitHub 为例，在仓库 Settings → Webhooks 中添加：

- **Payload URL**: `http://your-host:8080/webhook/github`
- **Content type**: `application/json`
- **Secret**: 与 `configs/config.yaml` 中 `sources.github.secret` 一致
- **Events**: 勾选需要监听的事件（Push、Pull request 等）

## 配置说明

### config.yaml — 主配置

```yaml
server:
  port: 8080
  base_url: "http://localhost:8080"

agents:
  opencode:
    cli: "opencode"
    args: ["--prompt", "{{.Prompt}}", "--workspace", "{{.Workspace}}"]
    timeout: 5m
    available: true

session:
  store: sqlite
  db_path: "./data/sessions.db"
  max_concurrent: 10
  default_timeout: 10m

workspace:
  base_dir: "./data/workspaces"
  auto_clone: true

sources:
  github:
    secret: "your-webhook-secret"
```

### rules.yaml — 事件路由规则

```yaml
rules:
  - name: "push事件-代码审查"
    match:
      source: [github, gitlab, gitcode]
      event: [push]
    agent: opencode
    prompt_template: push_event
    workspace:
      clone: true

  - name: "构建失败分析"
    match:
      source: [jenkins, spanner]
      event: [build_failure]
    agent: opencode
    prompt_template: build_failure
    workspace:
      clone: false
```

规则匹配逻辑：事件的 `source` 和 `type` 同时匹配时，路由到对应 Agent 并使用指定 Prompt 模板。

## 项目结构

```
devops-bot/
├── cmd/devops-bot/main.go              # 入口
├── internal/
│   ├── config/                         #   配置加载
│   ├── event/                          #   统一事件模型 + 路由器
│   ├── source/                         #   事件源接入
│   │   ├── github/webhook.go
│   │   ├── gitlab/webhook.go
│   │   ├── gitcode/webhook.go
│   │   ├── jenkins/webhook.go
│   │   └── spanner/webhook.go
│   ├── session/                        #   Session 生命周期 + SQLite 存储
│   ├── agent/                          #   Agent 适配器
│   │   ├── adapter.go                  #     接口定义
│   │   ├── registry.go                 #     注册表
│   │   ├── opencode/adapter.go
│   │   ├── claude/adapter.go
│   │   └── openclaw/adapter.go
│   ├── prompt/                         #   Prompt 构建器 + 模板
│   │   ├── builder.go
│   │   └── templates/                  #     (Go embed)
│   ├── failure/                        #   失败处理：委托 Agent 自分析
│   └── server/                         #   HTTP Server + 事件处理 Pipeline
├── pkg/cli/                            # 通用 CLI 执行器
├── skills/                             # Skills 库（Agent 的 DevOps 领域知识包）
│   ├── README.md
│   ├── SKILL-SPEC.md
│   ├── code-review/SKILL.md
│   ├── build-debug/SKILL.md
│   ├── deploy-guard/SKILL.md
│   ├── incident-triage/SKILL.md
│   ├── security-scan/SKILL.md
│   └── changelog-gen/SKILL.md
├── configs/                            # 配置文件
│   ├── config.yaml
│   └── rules.yaml
├── Makefile
└── go.mod
```

## 事件处理流程

```
1. Webhook 到达 → 签名验证
2. Source.Normalize() → 统一 NormalizedEvent
3. Router.Match(event) → 匹配规则，确定 Agent + Prompt 模板
4. SessionManager.Create() → 创建 Session (status=pending)
5. PromptBuilder.Build() → 渲染模板构造 Prompt
6. AgentAdapter.Execute() → CLI 调用 Agent，传入 DEVOPS_SKILLS_DIR (status=running)
7. Agent 自行发现和触发 Skills → 执行处理
8. 成功 → Session 归档 (status=completed)
   失败 → FailureHandler → 构造失败 Prompt → 再次调用 Agent 自分析
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查，返回可用 Agent 列表和规则数 |
| `/api/sessions` | GET | 最近 50 个 Session 列表 |
| `/webhook/github` | POST | GitHub Webhook 接收 |
| `/webhook/gitlab` | POST | GitLab Webhook 接收 |
| `/webhook/gitcode` | POST | GitCode Webhook 接收 |
| `/webhook/jenkins` | POST | Jenkins 通知接收 |
| `/webhook/spanner` | POST | Spanner 通知接收 |

## 扩展指南

### 新增 Agent

1. 在 `internal/agent/xxx/` 下实现 `Adapter` 接口：

```go
type Adapter interface {
    Name() string
    Execute(ctx context.Context, input *Input) (*Output, error)
    IsAvailable() bool
}
```

2. 在 `configs/config.yaml` 的 `agents` 中声明
3. 在 `cmd/devops-bot/main.go` 中注册

### 新增事件源

1. 在 `internal/source/xxx/` 下实现 `Source` 接口
2. 在 `internal/server/server.go` 的 `registerSources()` 中注册

### 新增 Prompt 模板

1. 在 `internal/prompt/templates/` 下添加 `.tmpl` 文件
2. 在 `configs/rules.yaml` 中引用模板名称

### 新增事件类型

1. 在 `internal/event/types.go` 中添加 `EventType` 枚举
2. 在对应 Source 的 `Normalize()` 中映射

### 新增 Skill

1. 在 `skills/` 下创建目录，如 `skills/my-skill/`
2. 在其中创建 `SKILL.md`，按 `skills/SKILL-SPEC.md` 规范编写
3. 无需修改 Bot 代码或配置 — Agent 会通过 `DEVOPS_SKILLS_DIR` 环境变量自动发现新 Skill

## Skills 库

Skills 是 Agent 的**项目级 DevOps 领域知识库**。Bot 不读取、不注入 Skills 内容，只通过环境变量 `DEVOPS_SKILLS_DIR` 将路径传给 Agent，Agent 自行发现和触发。

| Skill | 说明 | 适用事件 |
|-------|------|---------|
| `code-review` | 代码审查最佳实践与检查清单 | push, pull_request |
| `build-debug` | 构建失败诊断方法论与常见模式 | build_failure |
| `deploy-guard` | 部署前安全与合规检查守卫 | deploy_start, build_success |
| `incident-triage` | 生产事件分诊与定级 | deploy_failure, build_failure |
| `security-scan` | 安全漏洞扫描与修复建议 | push, pull_request |
| `changelog-gen` | 自动生成变更日志 | release |

详见 [`skills/README.md`](skills/README.md) 和 [`skills/SKILL-SPEC.md`](skills/SKILL-SPEC.md)。

## 开发

```bash
make build    # 编译
make run      # 编译并运行
make test     # 运行测试
make clean    # 清理构建产物
```

### Debug 模式

```bash
go run ./cmd/devops-bot --debug
```

## License

MIT
