# 3. CLI 命令行（grape）

> Grape 官方 CLI 读取服务端 OpenAPI 契约，自动生成 `grape <group> <verb>` 命令；
> 同时在本地跑原生 `git` 并把凭据注入子进程 —— 平台凭据不进命令行、不写 `.git/config`。
> 它主要面向 **Agent** 设计，也适合人和脚本调用：输出可机器解析，退出码可判定。

当前版本 **0.3.4**（PyPI `grape-cli`，要求 Python >= 3.10，distribution 名是 `grape-cli`，
命令始终是 `grape`）。

## 安装

```bash
pip install grape-cli          # 推荐：PyPI
grape --version                # 验证安装
pip install -U grape-cli       # 升级
```

源码或内网环境可以用 git 直装：

```bash
pip install "git+https://gitcode.com/grape-dev/grape-cli.git@main"
```

开发调试用 editable 安装：

```bash
git clone https://gitcode.com/grape-dev/grape-cli.git
cd grape-cli && pip install -e .
```

> 💡 **提示**
>
> 命令结束后若发现新版本，`grape` 会向 stderr 打一行升级提示。检查在后台线程进行
> （连接 0.5s / 总 1.5s 超时），结果缓存 24 小时于 `~/.grape/version-check.json`；
> 非 TTY 与 `CI=1` 场景自动关闭。可用 `--no-version-check` 或 `GRAPE_NO_VERSION_CHECK=1` 显式关闭。

## 连接服务端

所有请求都要知道**服务端地址**和**token**。优先级从高到低：

| 来源 | 示例 | 说明 |
|------|------|------|
| 一次性参数 | `grape --base-url https://grape.example.com tasks current` | 仅本次进程生效，不落盘；`--token` 同理，但会留痕于 shell history 与进程列表 |
| 环境变量 | `export GRAPE_BASE_URL=...`（回退 `GRAPE_SERVER_URL`）、`export GRAPE_SESSION_TOKEN=tk_xxx`、`export GRAPE_API_TOKEN=grape_xxx` | 推荐给 Agent 与 CI 用 |
| 配置文件 | `~/.grape/config.json`：`{"base_url": "...", "token": ""}` | 持久默认值 |

- `tk_` 是会话 token（任务/会话结束时自动失效）；`grape_` 是长期用户 key（可在 Web UI 生成、可吊销）。
- 地址优先级：`--base-url` > `GRAPE_BASE_URL`（回退 `GRAPE_SERVER_URL`）> `~/.grape/config.json` > 默认。
  Agent 运行时注入的通常只有 `GRAPE_SERVER_URL`，排障时别漏这一级。
- 不带任何配置时，`base_url` 默认 `http://localhost:8000`（自托管本机场景）。
- 自签证书或代理环境可加 `--no-proxy`（等价 `GRAPE_TRUST_ENV=0`）忽略 `*_proxy` / `NO_PROXY`。

```bash
# 一次性调用：只临时覆盖地址，token 走环境变量（避免写进 shell history）
export GRAPE_SESSION_TOKEN=tk_xxx
grape --base-url https://grape.example.com skills list --json-out

# 配好环境变量后，命令就是 grape <group> <verb>
export GRAPE_BASE_URL=https://grape.example.com
export GRAPE_SESSION_TOKEN=tk_xxx
grape doctor                    # 自检：token 通道、base_url、契约缓存
grape commands                  # 列出当前服务端契约对应的全部命令
```

## 三条命令上手

```bash
# 1. 看看平台上有哪些技能
grape skills list --json-out

# 2. 取当前 Agent 任务（在 Agent 运行时里最常用）
grape tasks current --json-out

# 3. 用平台凭据跑原生 git（凭据由 GIT_CONFIG 注入子进程）
grape git status
grape git log --oneline -5
```

`--json-out` 输出单行 JSON，直接喂给脚本或 Agent；不加时是人类可读的多行文本。

## 命令组一览

命令集合由**所连服务端的契约**决定，因此不同部署可能略有差异。`grape commands` 列出全部
（当前 0.3.4 对默认契约是 **26 个组、311 条命令**），`grape <group> --help` 看组内动词；
被 `GRAPE_CLI_MODE` 作用域隐藏的命令用 `grape commands --all` 查看（带 `[scope]` 标注，只列不执行）：

| 组 | 条数 | 用途 |
|----|------|------|
| `settings` | 38 | 平台与 Agent 设置、凭据、引擎、事件配置 |
| `scenes` | 31 | 场景模板与套用（含 advanced） |
| `git-repo` | 30 | 平台侧仓库 API：分支、提交、文件、Issue/PR、标签、里程碑 |
| `issues` | 27 | Issue 卡片、状态流转、待输入/待评审 |
| `obs` | 22 | 可观测性：告警、日志源、探活 |
| `runtime` | 21 | Worker runtime：机器、引擎、凭据、批量操作 |
| `repos` | 19 | 仓库注册、检查、机器人配置 |
| `chat` | 17 | 对话、会话、消息、微信登录 |
| `skills` | 16 | 技能上传、分类、转换、Agent 绑定 |
| `events` | 14 | 事件列表、详情、重派、终止、thread |
| `dashboard` | 12 | 效率看板、Ops 总览、仓库看板 |
| `orgs` | 11 | 组织创建、成员、详情 |
| `knowledge` | 11 | 知识库条目增删查 |
| `devstage` | 8 | 开发阶段环境与规格 |
| `feedback` | 5 | 反馈提交、上传、上下文 |
| `grape` | 5 | CLI 自身管理（auth 等） |
| `project` | 4 | 项目模板 |
| `tasks` / `task` | 3 / 3 | 任务自报完成、详情、通知、`wait` 轮询、日志 |
| `feishu` | 3 | 飞书 onboard |
| `agents` | 3 | Agent 配置、会话清理 |
| `pipeline` | 2 | 流水线路线图与交接 |
| `auth` | 2 | 登录态、`profile` |
| `a2a` | 2 | Agent 间交接（handoff / status） |
| `search` / `patrol` | 1 / 1 | 全局搜索 / 巡检扫描 |
| `git` | 透传 | 原生 git（任意子命令） |
| `commands` / `doctor` / `refresh` | 内置 | 列命令 / 自检 / 重拉契约 |

## 契约驱动：接口一变，命令就到位

```bash
# 服务端新增/修改接口后，重拉契约并刷新缓存
grape refresh

# 新命令随即出现，不需要升级 CLI
grape commands | grep <新关键词>
grape <group> <verb> --help
```

命令形态由 OpenAPI 的 `operationId` 推导：`tasks_current` → `grape tasks current`；
query/path 参数变成 `--<name>`，`requestBody` 变成 `--json '<JSON>'`。
这也是为什么 CLI 能覆盖平台的全部 API，而不需要为每个接口手写客户端。

## 给 Agent 用

- **机器可读**：`--json-out` 单行 JSON；失败时退出码非 0，不用解析报错文本。
- **轮询任务**：`grape tasks wait --task-id 56 --timeout 120 --interval 3`，直到
  `completed` / `failed` / `cancelled` 才返回。
- **按 token 作用域裁剪命令**：`GRAPE_CLI_MODE=WORKER` 只暴露会话 token 能打的命令
  （`USER` / `ALL` 可选）；持 `tk_` 的 Agent 不会看到会 403 的用户级命令。
- **凭据零暴露**：`grape git` 用进程内 `GIT_CONFIG` 注入，token 不出现在命令行、
  不写进 `.git/config`、不进日志。
- 平台侧还有配套技能 `grape-cli-guidance` 与 `issue-cli`，装进 Agent 技能库后可直接调用。

## 退出码与安全约定

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| `1` | 通用/配置错误（地址、token、网络） |
| `2` | 用法错误（参数缺漏、未知子命令） |
| `>=400` | HTTP 状态透传（如 `401` token 失效、`403` 越权） |
| `130` | Ctrl+C 中断 |

- CLI **不提供** create/revoke token 的子命令（防止自举越权）；用户级 key 在 Web UI 生成。
- token 不出现在输出与错误信息里；`grape git` 注入的 git 凭据同样不外泄、不落盘。
- 换券失败时 `grape git` 降级为本机原生 git，并给出告警。
- `--token` 一次性参数会留在 shell history 与进程列表（`ps`）中，仅限临时调试；自动化场景改用环境变量或 `~/.grape/config.json`。

## 常见问题

**`401` / token 失效**
`tk_` 会话 token 随任务结束失效。重新取会话 token，或改用 `grape_` 用户 key。

**连不上、超时**
先 `grape doctor` 看 base_url 与 token 通道；代理干扰时加 `--no-proxy`；
自签证书环境确认服务端地址用的是实际可达的 scheme + host。

**命令列表和文档对不上**
先 `grape refresh`；仍不一致说明服务端契约版本不同 —— `grape commands` 才是当前部署的权威清单。

**想限定在某次调用上**
一次性参数优先于环境变量与配置文件，适合在脚本里临时覆盖：

```bash
grape --base-url https://other.example.com --timeout 300 tasks current --json-out
```
