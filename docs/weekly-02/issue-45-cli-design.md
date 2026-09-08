# Issue #45 设计文档：grape CLI 独立包

> Issue：https://gitcode.com/grape-dev/grape/issues/45
> 类型：Feature | P1 | 状态：spec-ready
> 分支：`weekly-02`
> 日期：2026-09-07
> 范围：`grape` 独立发布包的形态、子命令、与 git/worker 集成。

## 关联文档

- API 认证/OpenAPI：`docs/weekly-02/issue-45-openapi-api-design.md`
- Git 仓库统一接口（方案③ token 不可感知）：`docs/weekly-02/issue-45-git-repos-api-design.md`
- 技能实际 API 使用面：`docs/weekly-02/issue-45-skills-api-scan.md`

## 一、定界

| 项 | 结论 |
|----|------|
| 形态 | **独立发布包**（`pip install grape`），独立代码目录，不并入 grape 主包 |
| 框架 | click/typer |
| 覆盖 | 优先 worker agent 核心操作；全量可迭代扩展 |
| 认证 | 复用 grape API 三通道（Bearer / PRIVATE-TOKEN / access_token） |
| **命令生成方式** | **方案 B（选定）：基于 OpenAPI 契约自动生成 CLI 命令**——CLI 读 `/openapi.json`，按 operationId 动态注册 typer 子命令；**新增接口进 OpenAPI 即自动获得 CLI 命令，无需改 CLI 源码** |

> **决策（2026-09-07）**：采用**契约驱动命令生成（方案 B）**，而非为每个接口手写 CLI 命令。Git 传输类（`git`，本地非 HTTP）无法由契约生成，仍手写（方案③）。

## 二、包结构

```
cli/                          # 独立发布包根（可放仓库根 cli/，单独 pyproject）
├── pyproject.toml            # [project] name="grape" version="0.1.0" deps=[typer, httpx]
├── grape_cli/
│   ├── __init__.py
│   ├── __main__.py           # python -m grape_cli
│   ├── cli.py                # typer 入口（组命令）：注册"契约生成命令" + 手写特殊命令（git）
│   ├── client.py             # HTTP 客户端（httpx，base_url + api_key header）
│   ├── config.py             # 配置：--base-url / --token / env GRAPE_BASE_URL/GRAPE_TOKEN / ~/.grape/config.json
│   ├── contracts.py          # ★ 契约加载：读 /openapi.json → 缓存 → 解析 operationId/参数映射（方案 B）
│   ├── registry.py           # ★ 契约→命令注册：把 OpenAPI operation 动态变成 typer 命令
│   └── commands/
│       └── git.py            # ★ 手写（非 REST）：grape git <任意 git 原生命令> 透传（方案③）
├── tests/
└── README.md
```

> 说明：`task/repo/agent/event/knowledge/gitrepo` 等 **REST 命令不再手写**，由 `contracts.py + registry.py` 按 `/openapi.json` 自动生成；仅 `git`（本地传输）手写。

## 三、命令设计（契约驱动，方案 B）

### 3.1 契约生成命令（自动获得全部 REST 接口）

CLI 启动时（或显式 `grape refresh`）读取 `{{base_url}}/openapi.json`，按 **`operationId`/`tags`** 动态注册 typer 子命令。**新增 OpenAPI 接口 → CLI 自动获得命令，零 CLI 代码改动**。

**命令形态（`<tag> <verb>` 空格分隔，非下划线）**：
```
grape <tag> <operation-id 的动词段> [--参数...]
# 例 /openapi.json：tags="tasks", operation_id="tasks_create", POST /api/tasks/create
grape tasks create [--json '<body>']
grape tasks mine [--status active] [--repo_key o/r]
```

- **下划线规则**：operationId 用 `snake_case`，CLI 拆分：**首段=组（tag），其余=动词链** → `tasks_mine` ⇒ `grape tasks mine`。
- **多段 group 映射连字符**：`git_repo_issues_list` ⇒ 组=`git-repo`，动词=`issues list` → `grape git-repo issues list`。
- **无动词自动补**（operation_id=`tags_list`）：确保总有 `<group> <verb>`。
- **内置保留字**：`git`（本地传输，手写）、`api`（通用兜底）——契约 command 组不与它们冲突；Git 仓库 REST 组固定 `git-repo`。

**参数映射规则**（openapi.py 契约自动推导）：

| OpenAPI 元素 | CLI 参数 |
|--------------|----------|
| `parameters`（path） | 位置参数或 `--<name>`（如 `--issue 5`） |
| `parameters`（query） | `--<name>`（如 `--status active`） |
| `requestBody`（application/json） | `--json '<body>'`（JSON 字符串）或按 schema 字段展开 `--<field>` |
| `security`（apiKey） | 自动带 `Authorization: Bearer <token>` |
| `X-Git-Provider`（git-repo 类） | `--provider`（契约里标记） |

**示例（由契约生成）**：
```
grape tasks mine --status active
grape tasks create --json '{"repo_key":"owner/repo","title":"x","description":"y"}'
grape git-repo issues list --provider github       # Git 仓库 REST（git-repo 组，避免与内置 git 冲突）
```

**契约缓存与刷新**：
- 首次：`GET /openapi.json` → 缓存 `~/.grape/openapi-<version>.json`（同 base_url 共存）。
- 版本变化（响应 `version` 字段不同）自动拉新。
- `grape refresh` 强制刷新；`--version` 显示契约版本。

### 3.2 手写命令（非契约）

**仅 `git`（本地传输，非 HTTP）** 手写——见 git-repos 文档方案③：
```
grape git setup --repo owner/repo --issue 7          # 就绪工作区
grape git checkout --repo owner/repo -b feat/issue-7 # 任意 git 原生命令透传
grape git commit --repo owner/repo -m "fix: xxx"
grape git push --repo owner/repo feat/issue-7
grape git status --repo owner/repo
```

### 3.3 通用兜底（可选保留）

`grape api <method> <path> [-d '{"...":...}]`：对契约未覆盖/特殊场景的直连调用；验证方法/路径来自契约白名单。

### 3.4 帮助系统（`--help` / `--list`，命令可发现性）

契约驱动意味着命令动态生成，**可发现性至关重要**——必须设计完善的帮助与查找能力。

| 命令 | 行为 |
|------|------|
| `grape --help`（或 `grape help`） | 顶层帮助：列出**全部命令组**（tasks/repos/agents/git-repo…）+ 内置（git/api/refresh/config）+ 用法示例，按字母序分组 |
| `grape <group> --help` | 组内命令列表：该 group 的**动作/动词**（如 `grape tasks --help` → create/mine/get/status…）+ 关键参数说明 |
| `grape <group> <verb> --help` | 具体命令帮助：该接口的**参数**（来自契约 schema）、示例 curl/用法、输出示例 |
| `grape --list [pattern]` | 快速搜索：列出匹配 `pattern` 的所有命令（如 `grape --list git` → git / git-repo 相关） |
| `grape commands` | 与 `--list` 等价，无 pattern 全量；`grape commands | grep issues` 可管道 |
| `grape --version` | 显示 grape-cli 版本 + 契约版本 |

**帮助内容来源**：契约里的 `summary`/`description`/`parameters` 自动生成——服务端接口声明即成为 CLI `--help` 文案（对齐 issue-45-openapi-api-design.md §3.5）。

**应用场景**（worker agent 可用性）：
- agent 不确定能否执行某操作 → `grape --list <关键字>` 快速确认命令存在性；
- agent 不确定参数 → `grape <group> <verb> --help` 查看参数；
- 无网络/契约未拉取时 `--help` 显示已缓存契约的组名（降级提示）。

- **配置优先级**：CLI 参数 > 环境变量（`GRAPE_BASE_URL`/`GRAPE_TOKEN`）> `~/.grape/config.json`。
- **认证**：默认 `Authorization: Bearer <token>`；兼容 `PRIVATE-TOKEN`/`access_token`。
- **输出**：human-readable；`--json` 机器可读（worker 解析）。
- **错误处理**：401 → 提示 token 无效；契约拉取失败 → 提示 base_url 或 refresh。

## 四、与 worker agent 集成

- worker 侧 agent（`src/agent/opencode.py` 等）`pip install grape`，用 `GRAPE_BASE_URL` + `GRAPE_TOKEN` 调用 task/repo/agent/git 操作。
- 替代当前部分直接 curl 的依赖，统一走 CLI/API。
- **CLI 不提供 create/revoke token 子命令**（避免自举越权）；API Key 由 Web UI 生成。

## 五、改动清单（CLI 部分）

| # | 模块 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | 新增 `cli/` 独立包 | typer 入口 + 配置/客户端 | P1 |
| 2 | `cli/grape_cli/contracts.py` | ★ 契约加载与缓存（读 `/openapi.json`、版本检测） | P0 |
| 3 | `cli/grape_cli/registry.py` | ★ operationId → 命令动态注册 + 参数映射 | P0 |
| 4 | `cli/grape_cli/commands/git.py` | `grape git <原生 git 命令>` 透传 + credential 注入（方案③，手写） | P1 |
| 5 | `api` 兜底命令（可选） | `grape api <method> <path> [-d JSON]` | P2 |
| 6 | `tests/` | CLI 测试用例（见 §五・测试设计） | P1 |

### 五・测试用例设计

#### A. 认证与配置（`tests/test_cli_config.py`）

| # | 用例 | 输入 | 断言 |
|---|------|------|------|
| A1 | 参数 > 环境变量 > 配置文件优先级 | 三处都给 `--base-url`/`--token` | 取参数值 |
| A2 | 仅环境变量 | `GRAPE_BASE_URL`+`GRAPE_TOKEN` | 正确加载 |
| A3 | 仅配置文件 | `~/.grape/config.json` | 正确加载 |
| A4 | 三者皆无 | — | 报错并给出 "需 --base-url/--token" 提示（exit≠0） |
| A5 | 认证头 | 给定 token | 请求头为 `Authorization: Bearer <token>`；兼容 PRIVATE-TOKEN / access_token |

#### A5・契约加载（`tests/test_cli_contracts.py`）

| # | 用例 | 断言 |
|---|------|------|
| H1 | 首次拉取缓存 | 调 `/openapi.json` → 写 `~/.grape/openapi-<version>.json` |
| H2 | 契约版本不变 | 复用缓存，不重复请求 |
| H3 | 契约版本变化 | 自动重新拉取 |
| H4 | `refresh` 强制刷新 | 忽略缓存重新拉取 |
| H5 | operationId 提取 | 契约 operation → `{tag}_{name}` 命令名映射正确 |
| H6 | 拉取失败 | 明确报错 + 提示 refresh/检查 base_url |

#### B. 契约生成命令（`tests/test_cli_registry.py`，mock httpx + mock 契约）

| # | 用例 | 断言 |
|---|------|------|
| R1 | `/openapi.json` 中 operation 动态注册命令 | `grape --help` 列出 `tasks mine`、`tasks create` 等组/动词 |
| R2 | query 参数映射 | `grape tasks mine --status active` → `GET /api/tasks/mine?status=active` |
| R3 | path 参数映射 | `grape git-repo issues get --issue 5` → `GET /api/repos/.../issues/5` |
| R4 | body → `--json '<body>'` | `grape tasks create --json '{"repo_key":...}'` → POST body 正确 |
| R5 | provider 参数 | operation 含 `X-Git-Provider` → `--provider github` 生效 |
| R6 | 安全自动带 token | 命令请求自动带 `Authorization: Bearer` |
| R7 | 新增契约自动获得命令 | mock 契约加一个 operation → 不重装 CLI 也能 `--help` 出对应 `<tag> <verb>` 命令 |
| R8 | `--json` 输出 | 所有生成命令支持 `--json` |
| R9 | operationId 拆分规则 | `tasks_mine` → 命令 `tasks mine`（tag+动词）；无动词自动补 `list` |

#### A5・帮助系统（`tests/test_cli_help.py`）

| # | 用例 | 断言 |
|---|------|------|
| H7 | `grape --help` | 列出全部命令组+内置（git/api/refresh）+用法，退出 0 |
| H8 | `grape <group> --help` | 列出该组动词（create/mine/…）+关键参数 |
| H9 | `grape <group> <verb> --help` | 列出参数（从契约 schema 映射）、示例用法与输出 |
| H10 | `grape --list git` | 只返回 git / git-repo 相关命令（模式过滤） |
| H11 | `grape commands` | 全量命令列表，可管道 |
| H12 | 契约摘要进帮助 | operation 的 summary/description 出现在对应命令 `--help` |
| H13 | 无契约缓存时 `--help` | 显示缓存契约组名（若有），否则提示需 refresh；不崩溃 |

#### B5・git 手写命令透传与 token 隔离（`tests/test_cli_git.py`）—— 方案③ 核心

| # | 用例 | 断言 |
|---|------|------|
| C1 | `--repo/--issue` 解析工作区 | 解析为 `{o}/{r}/issues/{n}/{agent}` worktree 路径（mock workspace API） |
| C2 | git 原生命令透传 | `grape git log --oneline -5` → spawn git 参数含 `log --oneline -5` |
| C3 | **token 不进 git 命令行** | mock 后断言 git argv 无 token；token 只经 `GIT_CONFIG` env / credential helper 传递 |
| C4 | **token 不落 agent 可见** | CLI 运行前后，工作区无 token 文件；stdout 不含 token |
| C5 | credential 获取 | 调 `GET /api/repos/{o}/{r}/git-credential` 取一次性 token；失败 → 明确报错 |
| C6 | 401 处理 | 无效 api_key → 提示 "token 无效" |
| C7 | 网络失败 | base_url 不可达 → 提示检查 base_url |

#### D. 安全/边界（`tests/test_cli_security.py`）

| # | 用例 | 断言 |
|---|------|------|
| D1 | CLI 无 create/revoke token 子命令 | `grape --help` 不含 token 管理 | 
| D2 | token 日志脱敏 | 任何日志/错误输出不含完整 token |
| D3 | `--json` 输出 | 所有命令支持 `--json` 输出合法 JSON |
| D4 | 契约命令白名单 | 契约外 operation 不暴露（只生成对外安全路由命令） |

#### E. 集成冒烟（`tests/test_cli_smoke.py`）

| # | 用例 | 断言 |
|---|------|------|
| E1 | `pip install` 后入口可用 | `grape --help` 退出 0 且列出契约命令 + git |
| E2 | 端到端 mock | mock grape 服务（含 /openapi.json），`grape tasks mine` 与 `grape git push` 全链路返回正确 |

> 测试覆盖目标：配置优先级、认证头、**契约加载（H1-H6）**、**contract 动态生成命令（R1-R9，R7 验证新增接口零 CLI 改动）**、**帮助系统（H7-H13）**、git 透传、**token 零暴露（C3/C4 为方案③关键验收）**、安全边界、安装冒烟。

## 六、验收标准

- [ ] `pip install grape` 后命令入口 `grape --help` 可用（列出契约生成命令 + git）
- [ ] **命令可发现**：`grape --help` / `grape <group> --help` / `grape --list <关键字>` / `grape commands` 可查找任意命令（H7-H13）
- [ ] **契约驱动**：读 `/openapi.json` 自动生成 `grape <tag> <verb>` REST 命令；**新增 OpenAPI 接口后，不重装/不改 CLI 源码，`--help` 即出现新命令**
- [ ] `grape git <任意 git 命令>` 本地执行，token 进程内外泄面为零（方案③）
- [ ] `--json` 输出可被 worker 解析
- [ ] 测试用例 A-E 全绿（含契约加载 H1-H6、契约生成 R1-R9、**帮助系统 H7-H13**、方案③ C3/C4 token 零暴露）

## 七、风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 契约与接口不同步（openapi.py 未及时渲染新接口） | 中 | 契约版本管理（2.1.0+）；CLI 版本检测自动拉新；`refresh` 强制 |
| 参数映射不完整（复杂 body / 流式 / 二进制） | 中 | query/path/`--json` 覆盖常见；流式/二进制/部分特殊接口标记手写或 `api` 兜底 |
| operationId 缺失/非唯一 | 中 | 要求 routes 接口声明 operationId；缺失时按 tag+method+path 推导 |
| **契约命令误暴露内部接口** | 中 | openapi.py 只渲染对外安全路由（D4 白名单）；内部接口不进契约 |
| CLI 覆盖不全 | 低 | 契约驱动天然全量；手写合并点 |
| git 命令透传参数复杂 | 中 | 透传 + `--repo/--issue` 解析；文档示例约束 |