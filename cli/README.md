# grape CLI

Agentic DevOps 命令行工具：**契约驱动命令生成** + `git` 本地传输（平台 token 零暴露）。

- 读服务端 `/openapi.json`（OpenAPI 3.x）自动生成 `grape <group> <verb>` 命令；
- **新增服务端接口 → 重新拉契约 → CLI 自动获得新命令，无需改 CLI 源码**（方案 B）；
- 认证复用 grape 三通道（`Authorization: Bearer` / `PRIVATE-TOKEN` / `?access_token=`），
  默认 Bearer，token 从配置/环境变量读取，自动携带。

---

## 安装

```bash
# 方式 A：从源码（仓库内 cli/）——未发布前的标准安装
cd cli && pip install -e .          # editable：改代码即生效（服务器建议此方式）

# 方式 B：发布到包索引后
pip install grape                   # 需先 publish 到 PyPI/内部镜像；请求从索引下载，非仓库源码
# ⚠️ 公有 PyPI 上已有同名第三方包，发布请用内部索引或专用命名，勿误装公有包

# 方式 C：生产服务器独立 venv（与系统环境隔离）
python3 -m venv /opt/grape-cli
/opt/grape-cli/bin/pip install -e /opt/grape/cli
ln -sf /opt/grape-cli/bin/grape /usr/local/bin/grape
```

装好验证：`grape --version`。

---

## 配置与 token

配置优先级（较高的优先）：

```
CLI 一次性参数（--token / --base-url）> 环境变量 GRAPE_SESSION_TOKEN > GRAPE_API_TOKEN / GRAPE_BASE_URL > ~/.grape/config.json
```

| 方式 | 示例 | 说明 |
|------|------|------|
| 一次性参数（**推荐单次调用**） | `grape --token tk_xxx tasks mine`<br>`grape --base-url https://grape.example.com --grape-api-token grape_xxx skills list --json-out` | 仅本次进程生效，不落盘、不污染环境；`--token` / `--grape-api-token` / `--grape_api_token` 同义 |
| 环境变量 | `export GRAPE_SESSION_TOKEN=tk_xxx` | 任务级优先 |
| 配置文件 | `~/.grape/config.json` 的 `base_url`/`token` | 持久默认值 |

| 环境变量 | 含义 | 何时用 |
|----------|------|--------|
| `GRAPE_SESSION_TOKEN` | 任务级**会话临时 token**（前缀 `tk_`） | worker/chat agent 内：随任务自动注入，零配置，任务结束即失效 |
| `GRAPE_API_TOKEN` | 用户级**长期 API Key**（前缀 `grape_`） | 独立/三方集成场景，Web UI「API Tokens」生成 |
| `GRAPE_BASE_URL` | 服务端地址（缺省 `http://localhost:8000`） | 指向生产如 `https://grape.example.com` |

`~/.grape/config.json` 示例：

```json
{
  "base_url": "https://grape.example.com",
  "token": ""          // 建议用环境变量注入 token，勿写进文件
}
```

> 无 token 时命令会提示缺 token；用 `export GRAPE_SESSION_TOKEN=...` 或
> `export GRAPE_API_TOKEN=...` 后重试。

---

## 命令

### 全局

```bash
grape --help                  # 顶层帮助：命令组 + 内置（git/api/refresh/commands）
grape commands [pattern]      # 列出全部契约命令，可管道过滤：grape commands | grep issue
grape refresh                 # 强制拉取最新 OpenAPI 契约并缓存
grape --version               # CLI 版本
```

### 契约生成命令（自动）

命令形态：`grape <group> <verb> [--参数...]`，由 `operationId` 推导。

```bash
grape tasks mine --status active --json-out        # GET  /api/tasks/mine?status=active
grape tasks create --json '{"repo_key":"o/r","title":"x","description":"y"}'
grape skills list --json-out
grape scenes list
grape git-repo issues list --owner o --repo r --state open --json-out
grape git-repo pulls merge --owner o --repo r --number 7
```

参数映射：

- `query` / `path` 参数 → `--<name>`
- `requestBody` → `--json '<JSON 字符串>'`
- `--json-out`：单行机器可读 JSON（worker 解析用，不加则缩进）

命令可用性：`grape <group> --help` 查看某组动词；`grape <group> <verb> --help` 查看参数。

### git（本地执行，方案③ token 零暴露）

`grape git` 是 **git 原生命令透传**，token 只在 CLI 进程内经 `/git-credential` 获取，
经 git 的 `GIT_CONFIG` env 注入——**不进命令行、不落磁盘、agent 感知不到**。

```bash
grape git --repo owner/repo status
grape git --repo owner/repo log --oneline -5
grape git --repo owner/repo checkout -b feat/issue-7
grape git --repo owner/repo commit -m "fix: ..."
grape git --repo owner/repo push feat/issue-7
# 不指定 --repo：在当前目录直接执行 git
grape git status
```

### api（通用兜底）

契约未覆盖/特殊场景：

```bash
grape api GET /api/scenes
grape api POST /api/tasks/create -d '{"repo_key":"o/r","title":"x"}'
```

---

## 快速上手指引

```bash
# 1) 一次性调用（不设任何环境变量也行）
grape --base-url https://grape.example.com --token tk_xxx skills list --json-out
grape --base-url https://grape.example.com --grape-api-token grape_xxx tasks mine --status active

# 2) 常用（一次设置，多次使用）：指向生产 + 给一个可用 token
export GRAPE_BASE_URL=https://grape.example.com
export GRAPE_SESSION_TOKEN=tk_xxx    # 或 export GRAPE_API_TOKEN=grape_xxx

# 3) 看有哪些命令
grape commands | head -40
grape git-repo issues list --help

# 4) 跑一个只读命令
grape skills list --json-out
grape tasks mine --status active
```

---

## 安全约定

- CLI **不提供** create/revoke token 子命令（防自举越权）；用户级 API Key 由 Web UI 生成。
- token 不出现在命令输出/错误信息（`GIT_CONFIG` 注入的 git 凭据同样不外泄）。
- `tk_` 会话 token 随任务/会话自动失效；`grape_` 用户 key 可吊销。