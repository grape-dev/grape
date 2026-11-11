<h1 align="center">Grape</h1>

<p align="center"><a href="README.md">English</a> | 中文</p>

<p align="center">
  <strong>AI Native DevOps —— 将任意 Git 项目一键变为 AI 自主驱动的开发工作流</strong>
  <br>
  无需 IDE，随处设计、开发、测试、发布——让团队每个人都是交付者
  <br>
  <em>Turn any Git project into an AI-native one with one click. Design, develop, test, and ship anywhere, anytime.</em>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#核心价值">核心价值</a> ·
  <a href="#核心能力">核心能力</a> ·
  <a href="#安装部署">安装部署</a> ·
  <a href="#开发测试">开发测试</a> ·
  <a href="#如何贡献">如何贡献</a> ·
  <a href="#反馈问题">反馈问题</a>
</p>

---

## 核心价值

Grape 重新定义了软件开发工作流——不再是"人驱动 AI 辅助"，而是"AI 驱动、人把关"。

| # | 核心价值 | 说明 |
|---|---------|------|
| **1** | **一键 AI 托管，项目自主演进** | 将任意 Git 项目接入 Grape 后，从需求到落地的全流程由 Multi-Agent 接力驱动。Issue 自动分诊、代码自动实现、PR 自动审查、构建自动部署——项目迭代速度**跃升**。 |
| **2** | **无需 IDE，随时随地的研发全流程** | 飞书 / WebUI 即开发入口。设计、开发、测试、发布——无需打开编辑器，团队每个人都能直接参与交付。 |
| **3** | **开源即模板，快速体验与二次开发** | 社区海量开源项目都是你的项目模板。一键创建、自动体验部署、Skill 驱动二次开发——从 0 到 1 的 AI 开发工作流即刻就绪。 |

---

## 这是什么

Grape 通过多个 AI Agent 自主协作，让开发工作流中的重复环节全部交由 AI 接力完成。一个传统 Git 项目接入 Grape 后，即获得完整的 AI 开发工作流——从需求澄清、代码实现、审查验证到构建部署，全流程 AI Native。

**一句话：把你的 Git 项目变成 AI 驱动的自主开发团队。**

## 解决的问题

传统 DevOps 中，从需求到发布的大量工作依赖人工：Issue 要手动分诊打标签、代码要开发自己写、PR 要人肉 Review、构建部署要手动触发。项目越多，人力成本越高。

Grape 的目标是将这些环节**全部 AI 化**，让开发者从执行者变为决策者——只需定义方向、Review 结果。

| 环节 | 传统工作流 | Grape AI Native 工作流 |
|------|-----------|----------------------|
| Issue 处理 | 人工分诊、标签、指派 | VOD / Issue Agent 自动分类、分派 |
| 代码实现 | 开发者手动编码 | Coding Agent 自动实现 + 提交 PR |
| 代码审查 | 人工 Review | QA Agent 自动审查 + 安全扫描 |
| 构建部署 | 手动 / CI 触发 | SRE Agent 自动构建、部署、运维 |
| 需求收集 | 人工汇总反馈 | VOD Agent 收集开发者声音、聚类提炼 |

## 核心能力

### Multi-Agent 工作流

项目接入后，每个 Issue / PR 事件由多个 Agent 按流水线接力处理：

```
VOD Agent → Issue Agent → Coding Agent → QA Agent → SRE Agent
 收集声音    分类需求      编码实现      审查验证    构建部署
```

- **VOD Agent**：收集开发者反馈、聚类共性、自动创建 Issue
- **Issue Agent**：分类、打标签、指派、触发 Coding
- **Coding Agent**：代码实现、PR 创建、自我审查
- **QA Agent**：PR 审查、自动化测试、安全检查
- **SRE Agent**：构建部署、发布管理、运维监控
- **Chat Agent**：个人对话助手，通过飞书或 WebUI 随时查询 / 操作

Agent 之间通过 A2A 协议链式协作，一个任务自动贯穿全流程。

### 一键转化为 AI Native 项目

```
选择模板 / 现有 Git 项目
  → 一键创建 & 托管
  → 自动应用开发场景（Agent × Skill 映射）
  → 异步首次引导（分析项目、创建体验部署与探索方向 Issue）
  → 从此该项目的每个 Issue / PR 由 Multi-Agent 流水线自主处理
```

### Skills 技能库

15+ 官方技能覆盖开发全流程：`vod`（声音收集）、`issue` / `issue-advance`（Issue 管理）、`implementation`（代码实现）、`pr` / `qa` / `skill-pr`（PR 审查）、`build-debug` / `cloud-deploy` / `deploy-guard` / `changelog-gen` / `incident-triage`（构建部署运维）、`chat` / `im-notify`（对话通知）、`repo-onboarding`（首次托管引导）。

支持上传自定义技能（官方 / 仓库级 / 个人级三态），并可按仓库配置。

### 支持的入口

| 入口 | 方式 | 说明 |
|------|------|------|
| GitCode | HTTP Webhook | Issue / PR / 评论 / 构建 / 部署 事件 |
| 飞书 | WebSocket 长连接 | 多 Bot 隔离运行，支持 100+ Bot |
| WebUI | SPA | Dashboard / Pipeline / Agent 详情 / Chat / Skills / Settings |

### 其他特性

- **多飞书 Bot**：单进程隔离运行 100+ 飞书机器人，热加载、自动重连
- **GitCode ↔ 飞书用户映射**：私聊身份确认绑定，防误绑
- **多租户**：GitCode OAuth 登录动态建号，登录白名单，凭据按用户隔离
- **事件审计**：每次 Agent 执行留痕（日志、耗时、结论），Pipeline 可视化追踪
- **多后端**：opencode / codearts 可插拔切换

## 快速开始

### 环境要求

| 组件 | 版本要求 | 说明 |
|------|---------|------|
| **Python** | >= 3.10（必须） | 主服务与 devbox sandbox 均要求 Python 3.10+（新式类型注解/语法依赖；低于 3.10 启动会显式提示并退出） |
| **Node.js** | >= 18（Agent CLI） | `npm install -g opencode` |
| **Git** | 任意较新版本 | |

### Windows (PowerShell)

```powershell
git clone https://github.com/grape-dev/grape.git
cd grape

# 一键启动：自动建 venv + 装依赖 + 复制 .env + 自检 + 启动
.\start.ps1                    # 本地开发（最小依赖，SQLite）
.\start.ps1 -Full              # 完整依赖（含 OBS/MySQL/飞书）
.\start.ps1 -SkipDoctor        # 跳过自检，直接启动

# 或手动操作：
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-local.txt
Copy-Item .env.example .env
New-Item -ItemType Directory -Force -Path data
python -m src.api.run --dev    # 开发模式，打开 http://localhost:3000
```

> 如遇 PowerShell 执行策略拦截：`Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

### macOS / Linux

```bash
git clone https://github.com/grape-dev/grape.git
cd grape

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-local.txt     # 最小依赖（不含 OBS/MySQL/飞书）
# pip install -r requirements.txt         # 完整依赖

cp .env.example .env
mkdir -p data
python -m src.api.run --dev                # 开发模式，打开 http://localhost:3000
```

### 国内镜像加速 / 网络建议

```bash
# PyPI 阿里云镜像
pip install -r requirements-local.txt -i https://mirrors.aliyun.com/pypi/simple/

# 永久配置：
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
```

> git 拉取偶发 HTTP/2 / 连接超时属网络抖动，拉取路径已内置有限次自动重试（3 次退避）。
> 仍失败可降级 HTTP/1.1：`git config --global http.version HTTP/1.1`；或按你的网络环境配置
> 镜像/代理（如 `git config --global url."https://你的代理/https://github.com/".insteadOf "https://github.com/"`）。

### 安装 Agent CLI（至少一个）

```bash
npm install -g opencode          # 基于 Node.js
# 或
pip install codearts-cli         # 基于 Python
```

### AtomCode 引擎（macOS / Linux）

按[官方安装说明](https://github.com/atomgit-atomcode/atomcode#readme)安装 AtomCode CLI，
将 `atomcode` 加入 Grape 服务端或 worker 的 `PATH`，并在实际执行机器上登录：

```bash
atomcode login
atomcode --version
```

Grape 使用 headless JSONL schema 1，已核对 CLI 5.2.3–5.2.6。发现引擎后，在引擎管理中
选择 `atomcode`，并在场景或 Chat 配置中绑定对应实例。版本检查成功只说明已安装；健康状态
需要有时限的真实模型探测成功。首版暂不支持 Windows 执行。

默认使用 CLI 配置中的 provider/model；也可以**同时**配置 `GRAPE_ATOMCODE_PROVIDER`
与 `GRAPE_ATOMCODE_MODEL`。任务环境优先于 worker 环境，参数按独立 argv 传入。
`ATOMCODE_HOME` 默认是 `~/.atomcode`，Grape 会发现其 `skills` 目录。

每项任务使用 `--ephemeral`，由 Grape 提供上下文，不续接或持久化 CLI 原生会话，提示词通过
stdin 传输。Grape 不添加 `-y`，不改写登录与权限文件；请参考[官方权限模型](https://github.com/atomgit-atomcode/atomcode/blob/main/docs/security/permission-model.md)
配置所需权限。CLI 策略仍可能允许无头 shell 执行；需要交互批准的工具被拒绝时，任务会明确失败。

排障时检查执行机器上的登录状态、provider/model 是否成对、CLI 是否支持必需的 JSONL 参数与
schema，以及工具权限。缺失完成事件、限流终止和权限拒绝均判为失败。失败或超时后 Grape
不会自动重跑整项 AtomCode 任务；人工重派前应检查部分操作是否已执行。健康探测关闭工具与
会话持久化，使用临时目录，最长 60 秒；沿用现有探活周期，会消耗少量模型请求额度。

若 CLI 返回 `ProviderError`、HTTP 403，并提示 `model is not enabled for codingplan`，
说明已登录，但当前模型未对该账号的套餐启用。请在 CLI 中选择可用模型，或者在实际执行任务的
Grape 服务端/worker 进程环境中同时设置以下变量，再重启该进程。下面这组配置已在 CLI 5.2.5
上通过 Grape 的真实编码任务验证；模型可用性取决于账号，此处仅为示例，不作为新默认值：

```bash
export GRAPE_ATOMCODE_PROVIDER=AtomGit-glm5.3-flash
export GRAPE_ATOMCODE_MODEL=glm5.3-flash
```

provider 是 CLI 配置中的别名，model 是上游模型 ID。Grape 不会在 provider 出错后自动切换模型。

### 环境自检（可选）

```bash
python scripts/doctor.py         # 运行安装前自检
```

> 完整「0→1 安装自检清单」（阶段化操作 + 期望结果 + 失败排查）见 [docs/0-to-1-checklist.md](docs/0-to-1-checklist.md)。

### 配置 OAuth 应用凭据（必做）

```bash
cp configs/oauth-providers.example.json configs/oauth-providers.json
# 编辑 configs/oauth-providers.json，填入 GitCode / GitHub 两个 provider 的 client_id / client_secret
```

`configs/oauth-providers.json` 是 OAuth 凭据的权威配置文件（重启时同步入库，凭据采用「首次写入」幂等模式）：
写错文件、留空字段会导致登录入口 `/api/auth/login/{provider}` 返回 400（not_configured）。

```jsonc
{
  "gitcode": {
    "client_id": "你的 GitCode OAuth App Client ID",
    "client_secret": "你的 GitCode OAuth App Client Secret",
    "redirect_uri": "https://你的域名/api/auth/callback/gitcode",
    "auth_url": "https://gitcode.com/oauth/authorize",
    "token_url": "https://gitcode.com/oauth/token",
    "user_url": "https://gitcode.com/api/v5/user",
    "scope": "all"
  },
  "github": {
    "client_id": "你的 GitHub OAuth App Client ID",
    "client_secret": "你的 GitHub OAuth App Client Secret",
    "redirect_uri": "https://你的域名/api/auth/callback/github",
    "auth_url": "https://github.com/login/oauth/authorize",
    "token_url": "https://github.com/login/oauth/access_token",
    "user_url": "https://api.github.com/user",
    "scope": "repo user admin:repo_hook offline_access"
  }
}
```

> 完整协议字段与示例见 `configs/oauth-providers.example.json`；OAuth App 的 Callback URL 必须与上表 `redirect_uri` 完全一致。
> 旧版 `configs/global-bot-token.json` 中的 OAuth 凭据字段仍兼容（向前兼容回退），新配置请统一使用 `oauth-providers.json`。

### 配置对外可访问基址（必做）

```bash
cp configs/server-config.example.json configs/server-config.json
# 编辑 configs/server-config.json，把 server_base_url / webhook_url 改为对外可访问地址
```

```jsonc
{
  "server_base_url": "https://你的域名",
  "webhook_url": "https://你的域名/webhook/gitcode"
}
```

> 未配置 `server_base_url` 时服务会回退到 `http://localhost:3000`：OAuth 回调地址指向本机，
> 第三方平台（GitCode/GitHub）无法回访，登录/托管不可用。启动日志与 `/api/health` 会输出明确告警
> （`server_base_url_configured=false`）。本地纯开发可忽略，但 OAuth 登录前必须配置。

首次登录用 GitCode OAuth 一键授权，即可开始托管仓库、把项目接入 AI 工作流。

## 安装部署

```bash
python -m src.api.run          # 生产：主服务 3000 + Webhook 8080 双进程
python -m src.api.run --dev    # 开发：单端口 3000，热重载
```

生产部署（nginx 反代 / systemd / Docker）示例见 `docs/DEPLOYMENT`。

### 配置

| 文件 | 说明 |
|------|------|
| `configs/global-bot-token.json` | 全局 Bot token（gitignored） |
| `configs/oauth-providers.json` | OAuth 应用凭据（GitCode/GitHub 的 client_id/secret 等，从 `oauth-providers.example.json` 复制，gitignored） |
| `configs/server-config.json` | 对外基址 server_base_url / webhook_url（从 `server-config.example.json` 复制；未配置时 OAuth 回调指向 localhost 不可用） |
| `configs/agent-description.json` | Agent 类型种子（订阅事件、默认后端） |
| `configs/skill-description.json` | 官方技能种子（由独立技能仓库 [grape-skills](https://github.com/grape-dev/grape-skills) 经 `scripts/sync_skills.sh` 同步） |
| `configs/agent-skill-mapping.json` | 开发场景 → Agent × Skill 映射 |
| `configs/project-templates.json` | 项目模板库 |
| `configs/login-whitelist.json` | GitCode 登录白名单 |

## 开发测试

```bash
# 安装依赖（本地开发最小集）
python3 -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements-local.txt
pip install -r requirements-dev.txt

# 运行测试
mkdir -p data                    # Windows: New-Item -ItemType Directory -Force data
python -m pytest tests/ -v
```

测试覆盖：事件归一化、Prompt 构建、Agent 执行器（NDJSON 解析 / 会话重建）、飞书 handler、p2p 绑定、OAuth 白名单、Pipeline、项目模板、仓库托管、任务中心、UI 场景测试（Playwright）。

## 如何贡献

欢迎参与 Grape 的开发。建议的工作流：

1. **提 Issue**：先到 [Issues](https://github.com/grape-dev/grape/issues) 描述你要做的事或发现的问题，确认方向
2. **Fork & 开发**：Fork 仓库，在特性分支上开发
3. **补充测试**：新增 / 修改功能请补充 pytest 测试
4. **提交 PR**：描述改动内容、测试情况、关联的 Issue 编号

### 分支命名

| 前缀 | 用途 |
|------|------|
| `feat/` | 新功能 |
| `fix/` | Bug 修复 |
| `refactor/` | 代码重构（不改变行为） |
| `docs/` | 文档 |
| `chore/` | 构建、工具链调整 |

### 不写代码也能贡献

- **文档**：改进 README、补充设计文档、修正错别字
- **Skills**：向独立技能仓库 [grape-skills](https://github.com/grape-dev/grape-skills) 贡献新的 Agent 技能（`skills/<agent_type>/<skill>/`，Markdown 即可，无需改后端代码）
- **项目模板**：为 `configs/project-templates.json` 补充可一键创建的模板
- **Bug 报告**：提供清晰的复现步骤、运行环境、相关日志
- **测试用例**：补充覆盖边界场景的用例
- **社区支持**：帮助解答问题、写教程

每一个贡献，代码或非代码，都在让 Grape 变得更好。

## 反馈问题

遇到问题或想提建议，请通过以下任一渠道：

- **GitHub Issues**：https://github.com/grape-dev/grape/issues
  - Bug 请附上运行环境、复现步骤、相关日志
  - 特性建议请描述使用场景和预期行为
- **飞书**：接入 Grape 后可在绑定的群中直接反馈

## License

MIT
