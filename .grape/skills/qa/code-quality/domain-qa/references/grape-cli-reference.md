# grape CLI 命令参考

## 1. 安装

```bash
pip install grape-cli
# 或从源码安装
pip install git+https://gitcode.com/grape-dev/grape-cli.git
```

验证安装：

```bash
grape --version
grape --help
```

## 2. 认证与配置

### Token 配置

`$GRAPE_SESSION_TOKEN` 由服务端注入到环境，grape **自动读取**——**不要手动传 token、不要 export 其他 token**：

| 方式 | 参数 | 说明 |
|------|------|------|
| 环境变量 | `$GRAPE_SESSION_TOKEN` | 服务端注入，自动读取，无需配置 |

```bash
# 无需任何 token 配置，直接使用
grape tasks current

# 禁止 `--token ...` / export 其他 token（如 BOT_GITCODE_TOKEN、GRAPE_API_TOKEN 等）
```

### Base URL

```bash
grape git-repo issues list --owner XOcto --repo grape-cli
```

## 3. 常用命令

### Issue 管理

| 命令 | 说明 |
|------|------|
| `grape git-repo issues list --owner <o> --repo <r>` | Issue 列表 |
| `grape git-repo issues get --owner <o> --repo <r> --number <n>` | Issue 详情 |
| `grape git-repo issues create --owner <o> --repo <r>` | 创建 Issue |
| `grape git-repo issues update --owner <o> --repo <r> --number <n>` | 更新 Issue |
| `grape git-repo issues comments list --owner <o> --repo <r> --number <n>` | Issue 评论列表 |
| `grape git-repo issues comments create --owner <o> --repo <r> --number <n>` | 创建 Issue 评论 |

### PR 管理

| 命令 | 说明 |
|------|------|
| `grape git-repo pulls list --owner <o> --repo <r>` | PR 列表 |
| `grape git-repo pulls create --owner <o> --repo <r>` | 创建 PR |
| `grape git-repo pulls get --owner <o> --repo <r> --number <n>` | PR 详情 |
| `grape git-repo pulls merge --owner <o> --repo <r> --number <n>` | 合并 PR |
| `grape git-repo pulls files --owner <o> --repo <r> --number <n>` | PR 变更文件 |

### 仓库操作

| 命令 | 说明 |
|------|------|
| `grape git-repo repo --owner <o> --repo <r>` | 仓库信息 |
| `grape git-repo branches --owner <o> --repo <r>` | 分支列表 |
| `grape git-repo commits --owner <o> --repo <r>` | 提交历史 |
| `grape git-repo contents --owner <o> --repo <r> --path <p>` | 文件内容 |

> ⚠️ `grape git-repo git credential` **不存在**（0.3.3 实测 `No such command`——v2.1.0 契约已移除该端点）——git 克隆/拉取认证统一由 **GIT_ASKPASS 注入**处理，不要引用。

### Git 透传

```bash
grape git clone https://gitcode.com/owner/repo.git
grape git checkout feat/issue-5
grape git add .
grape git commit -m "feat: deploy skill refactor"
grape git push origin feat/issue-5
```

`grape git` 透传 git 原生命令，进程内注入凭据，token 不落盘。

### 任务管理

| 命令 | 说明 |
|------|------|
| `grape tasks current --json-out` | 当前任务 |
| `grape tasks complete --json '{"result_text":"...","next_step":"done","actions":[...],"conclusion":"...","artifacts":[...],"issue_summary":"..."}'` | 任务完成自报（会话 token 自动定位，无需 --task_id）|
| `grape tasks notify` | 通知（等待人工确认等场景）|

> ⚠️ `tasks` 组命令面（0.3.3 实测 `grape tasks --help`）仅为 `complete` / `current` / `notify` / `wait`——`tasks mine` / `start` / `status` / `close` / `clarify` **不存在**（v2.1.0 契约已移除）；`tasks create` 亦不存在——建任务用 `issues create`；轮询终态用 `tasks wait --task_id <id>`（或 `issues pipeline` 查流水线）。不得使用上述不存在命令。

### A2A 交办

```bash
# ⚠️ 0.3.3 实测：a2a handoff 只接受 --json body（顶层字段 agent/payload）——无独立 flag 形式
grape a2a handoff --json '{
  "agent": "qa",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "dongzhiri",
    "issue_number": "'"$ISSUE_NUMBER"'",
    "pr_number": "'"$PR_NUMBER"'",
    "context": "部署完成，请验证"
  }
}'
```

### 无 grape api 逃生舱

v0.2.2 起移除 `grape api` 通用兜底命令（0.3.3 实测 `No such command`）——契约未覆盖的端点无法通过 CLI 调用，**不要 curl 直连**。项目知识基线由服务端 prompt 内联（`knowledge context/content` 亦已于 v2.1.0 移除）。

### 其他

| 命令 | 说明 |
|------|------|
| `grape refresh` | 刷新契约缓存 |
| `grape commands` | 列出所有命令 |
| `grape skills list` | 技能列表 |
| `grape knowledge list` | 知识清单 |
| `grape events detail --seq <n>` | 事件详情 |

## 4. 命令限制

| 限制 | 说明 |
|------|------|
| 超时 | 默认 30s，可 `--timeout 120` 调整（git 透传不受限） |
| 分页 | `?page=1&per_page=20`，最大 per_page=100 |
| Rate Limit | GitCode API: 400 req/min, 4000 req/hour |
| 契约依赖 | 契约命令从 `openapi.json` 动态生成，`grape refresh` 更新 |
| 凭据注入 | `grape git` 进程内注入，不写入 git config 或文件 |

## 5. 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 通用/配置错误（缺 token、网络超时等） |
| 2 | 用法错误（click 参数错误） |
| =HTTP(≥400) | API 错误透传（如 401 鉴权失败） |
| 130 | Ctrl+C 用户中断 |

## 6. 安全与边界

1. **只用系统注入的 token**：`$GRAPE_SESSION_TOKEN`，不得从数据库/配置文件/日志/历史中寻找其他 token。
2. **禁止访问 Grape 内部数据**：不得读取 data/、configs/、sessions.db、user_accounts 等。
3. **403 处理**：API 返回 403 时停止操作，回复用户「需要管理员/维护者权限」。
4. **token 不落盘**：`grape git` 进程内注入凭据，不写入 git config；其他命令通过 header 传递。
5. **超时必设**：所有 API 调用默认 30s 超时，超时判定失败不 hang。
