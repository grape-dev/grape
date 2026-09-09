# Git 仓库统一接口（/api/repos）

> 设计：技能/第三方访问 Git 仓库资源（Issue/PR/内容/分支等）时，统一走 grape 的 Git 仓库 API。
> **不是 GitCode 代理**：grape 侧定义中立的 Git 仓库资源语义，backend 由请求头/参数区分（gitcode / github / gitlab，未来扩展）。
> 认证：`get_current_user_or_api_key`（API Key 用于 worker/技能）。写操作需授权。
> Base：`{{SERVER_URL}}/api/repos`
> 出参结构为 **gitcode 平台实际结构**（与技能 `gitcode-api-reference.md` 一致）；github/gitlab 未来映射。

## 后端区分（Git Provider 判定）

通过 **`X-Git-Provider` 请求头**（推荐）或 **`provider` query 参数**指定后端，缺省默认 `gitcode`：

```
X-Git-Provider: gitcode | github | gitlab
 或
GET .../api/repos/...?provider=github
```

| provider | 内部实现 |
|----------|----------|
| `gitcode` | 现状：`repo_manager_vcs` → `api.gitcode.com/api/v5` |
| `github` | 未来接入 `api.github.com` |
| `gitlab` | 未来接入 `gitlab.com/api/v4` |

通用的 `Content-Type: application/json`（有 body 时）；分页 `?page=1&per_page=20`（max 100）。

---

## Git 传输层：clone / push 代码（不是 REST 接口）

**clone / push 不属于 `/api/repos` 的 REST 资源接口**——它们是 **git 协议传输（Smart HTTP）**，由 worker/agent 在本地执行 `git` 命令完成，grape 负责给出 **clone URL** 与 **传输凭据**。

### ⚠️ 两套独立认证（关键）

| 场景 | 认证方式 | token 类型 |
|------|----------|-----------|
| REST `/api/repos/*` | `Authorization: Bearer <grape api_key>`（三通道） | **grape API Key** |
| git 协议 clone/push | `git http.extraheader` / `GIT_ASKPASS` 注入 | **对应平台 token**（如 GitCode bot token） |

**git 协议不认 grape api_key**：`git clone` 走 smart HTTP，只接受 Basic / 自定义 header 中的**平台凭据**，不能带 grape 的 Bearer api_key。
→ **clone 只能用对应平台的 token**；REST 接口用 grape api_key，两者不互通、职权分离。

### 现状（grape 已实现，`src/runtime/repogit.py`）

- **clone URL**：`clone_url_for(repo_key)` → `https://{host}/{owner}/{repo}.git`（provider 决定 host，现仅 gitcode）。
- **凭据注入**：`Authorization: Basic base64("x-access-token:{bot_token}")` 经 `git -c http.extraheader=` 注入（不落盘、不进日志）；worker 用 `GIT_ASKPASS`（`repogit.askpass_path`）。
- **bot_token 来源**：`get_gitcode_token()`（当前 = GitCode bot token）。这意味着 **现有 clone 只能用 GitCode token**。
- **工作区机制**：
  - bare 基线 `{WORKSPACE_DIR}/{owner}/{repo}/codebase/`（每机懒建立，`fetch --prune` 刷新）；
  - Issue 级 worktree `{WORKSPACE_DIR}/{owner}/{repo}/issues/{n}/{agent_type}/`（`ensure_issue_worktree`）；
  - coding/qa/sre agent 的 cwd 即该 worktree，git commit/push 直接操作。

### 未来 GitHub / GitLab 的 clone 凭据

git 协议对每个平台用**各自的 token 传递方式**，`clone_url_for` 换 host、凭据格式随 provider 变：

| provider | clone 地址 | git 凭据传递 |
|----------|-----------|--------------|
| `gitcode`（现状） | `https://gitcode.com/{owner}/{repo}.git` | `x-access-token:<bot_token>` |
| `github`（未来） | `https://github.com/{owner}/{repo}.git` | `<username>:<PAT>` 或 `x-access-token:<PAT>` |
| `gitlab`（未来） | `https://gitlab.com/{owner}/{repo}.git` | `oauth2:<token>` 或 Basic `<用户名>:<PAT>` |

→ **clone 凭据永远按 provider 用对应平台的 token**（GitCode bot token / GitHub PAT / GitLab PAT），由 grape 内部注入，技能侧不感知平台 token；REST 层始终用 grape api_key。

### 🔒 token 不可感知设计（重点，防止 agent 泄露平台 token）

> **目标**：git 传输的**平台 token 只出现在 grape 进程内**，agent/技能**感知不到、读不到、拿不到**。agent 只负责跑 `git clone/push`，凭据由 grape 在进程外注入。

#### 现状风险点（确有泄露面，需改进）

| 注入点 | 现状 | 风险 |
|--------|------|------|
| **env 变量** `BOT_GITCODE_TOKEN` | 作为环境变量注入，SKILL 引用 `$BOT_GITCODE_TOKEN` | ⚠️ **agent `env` 可直接看到明文**；且技能被要求用它直连 GitCode（本设计要收敛的对象） |
| **askpass 脚本**（`_write_askpass`） | 在 issue_dir 写 `.git-askpass.sh`：`echo "$BOT_GITCODE_TOKEN"` | ⚠️ 脚本落 agent 工作区，`cat` 可读到（虽只读 env 引用，若 env 有值则泄露） |
| **git `-c http.extraheader`** | `Authorization: Basic base64("x-access-token:{token}")` 作命令行参数 | ⚠️ **`ps` 可看到进程参数中的 base64 token** |

#### 不可感知设计（目标态：token 仅 grape 进程内）

原则：**平台 token 不放环境变量、不落脚本文件、不进 git 命令行**。

| 方案 | 实现 | 是否达标 |
|------|------|---------|
| ① GIT_CONFIG_COUNT 注入 | 不写 token 到磁盘，启动 git 时用 env `GIT_CONFIG_COUNT/KEY/VALUE` 注入，仅对 git 子进程短暂传；不落 agent 环境 | 部分（token 仍可能被深挖） |
| ② credential helper 临时文件 | 一次性 `.git-credentials`（chmod 600，grape 私有目录）+ `GIT_ASKPASS` 指向 grape 侧锁文件 | 部分（脚本在 grape 侧，但仍需处理 agent 触达） |
| **③ grape CLI 本地执行 git（✅ 选定）** | **git 命令由 grape CLI 在本地执行**，平台 token 由 CLI 内部经 grape API 查询并在 CLI 进程内使用（不外泄、不进 agent 可读界面） | ✅ **根治**：agent 无需任何平台 token，服务端也不参与 git 执行 |

### ✅ 选定方案 ③：grape CLI 本地执行 git 命令

**原则**：git 的 clone/fetch/push 由 **grape CLI 在 agent 机器本地执行**——由于 CLI 持有 grape api_key，可通过 grape API 查询 `user_gitcode_token` / `bot_gitcode_token`，**在 CLI 进程内**注入 git 完成操作；token 不出 CLI 进程、不对外暴露、agent 无感知。

> **`grapecli git` 是 git 原生命令的透传**：`grapecli git <任意 git 子命令>` **等价于在已就绪工作区执行 git**——clone/fetch/checkout/commit/push/log/diff/status 等**全部 git 原生命令都支持**，CLI 只做三件事：
> 1. 用 `--repo/--issue` 解析 grape 工作区并执行到目录；
> 2. 注入 platform token（credential helper）；
> 3. 其余原样透传给 git。
> `git setup` 只是**便捷的就绪入口**（= 定位/复用 grape worktree → 必要则 `clone`/`fetch` → 切 `feat/issue-N` 分支），并非唯一可用命令。

**执行链**：

```
agent / 技能
   │  只调 grape CLI：grapecli git <原生git命令>（含 setup/clone/fetch/push/...）
   ▼
grape CLI（grape_cli，本地进程）
   │  内部：解析工作区 + 用 api_key 调 GET /api/repos/{o}/{r}/git-credential 取 token
   │  在 CLI 进程内以 credential helper 注入 → 执行 git
   ▼
Git 仓库（gitcode/github/gitlab）
```

- **token 获取**：CLI 通过 grape API 查询该用户/仓库当前可用的平台 git token（`GET /api/repos/{o}/{r}/git-credential`），服务端按权限返回一次性/短期凭据。
- **token 使用**：在 **grape CLI 进程内**（credential helper 返回给 git 内存使用），git 命令由 CLI 在本地 spawn；不落磁盘、不进 agent 可读文件/输出；进程结束即释放。
- **agent 隔离**：agent 只给 CLI 传仓库/分支/commit 消息；platform token 在 CLI 内部，不对外暴露。
- **服务端不跑 git**：grape 服务端只提供 token/工作区查询接口，不实际执行 git（git 传输在 agent 本地完成）。

**接口（grape 服务端，供 CLI 内部使用）**：

| 能力 | API（CLI 内部调用） | 说明 |
|------|---------------------|------|
| 查询可用 git token | `GET /api/repos/{o}/{r}/git-credential` | 按调用者权限返回 user/bot git token 的**一次性值**（或短期凭证），仅服务端→CLI 安全通道 |
| 获取工作区路径 | `GET /api/repos/{o}/{r}/workspace` | 返回工作区位置（如 worktree 路径），CLI 本地执行 |
| CLI 侧 git 透传 | `grapecli git <任何 git 原生命令>` | `--repo/--issue` 解析目录 + credential 注入，其余原样透传 git |

> ⚠️ token 经 API 返回给 CLI 有传输风险 → 建议：
> - 走已认证的 HTTPS + 一次性使用（响应读取后即失效）；
> - 或将 token 存 grape 服务端凭据库，CLI 通过 **`git credential helper` 接入** grape 端点（git 每次要凭据时才请求，token 不经 CLI 常驻内存）。这是更优路径（见下「credential helper 方案」）。

**更优实现（credential helper 接 grape）**：

```
git (本地，CLI 内) -- 缺凭据 → grape credential helper → POST/GET /api/repos/{o}/{r}/git-credential
                                                          → 返回 {username, password} → git 内存使用
```

- git 协议层在 CLI 本地执行；token 只在 git 请求凭据瞬间从 grape 服务端获取，用完即弃。
- 不落磁盘、不进 agent 可读文件。服务端可审计每次取用、限时过期。

### 技能/worker 如何使用（`grapecli git <原生 git 命令>`，不接触平台 token）

worker agent 安装 `grape-cli`，用 `GRAPE_API_TOKEN`（grape api_key）执行任意 git 命令；CLI 本地执行、token 进程内获取：

```bash
# 就绪工作区（clone/复用 + 切 Issue 分支）
grapecli git setup --repo Jason7602/hello --issue 7

# 任意 git 原生命令（全部支持）
grapecli git clone --repo Jason7602/hello .                 # 等价手写 clone（由 CLI 注入凭据）
grapecli git checkout --repo Jason7602/hello -b feat/issue-7
grapecli git add --repo Jason7602/hello src/a.py
grapecli git commit --repo Jason7602/hello -m "fix: xxx"
grapecli git push --repo Jason7602/hello feat/issue-7
grapecli git log --repo Jason7602/hello --oneline -5
grapecli git status --repo Jason7602/hello
grapecli git diff --repo Jason7602/hello
```

- `--repo` 解析工作区目录，`--issue`（可选）指定 Issue 分支；其余参数**原样透传 git**。
- agent 全程只传"仓库 + 分支 + commit 消息"，**看不到任何平台 token**。
- 文件在 grape 管理的工作区（REST `GET /api/repos/{o}/{r}/contents/*` 同源），git 动作统一走 `grapecli git`。

### 需要在 grape API 层暴露的能力（方案③）

| 资源/命令 | 说明 |
|------|------|
| `GET /api/repos/{o}/{r}/git-credential` | **CLI 内部**取一次性 git token（user/bot），服务端按权限返回；仅 HTTPS 安全通道、一次性失效、可审计 |
| `GET /api/repos/{owner}/{repo}` | 返回 `clone_url`/`http_url`/`default_branch`，供 CLI 确定 clone 地址（token 由 credential 端点提供） |
| `grapecli git <任意 git 原生命令>` | CLI 本地执行 git：`--repo/--issue` 解析目录 + credential 注入，其余原样透传（setup/clone/fetch/checkout/commit/push/log/diff/status 等全部支持） |
| `grapecli git usertoken --repo o/r` | （可选）辅助：内部打印/user 自查，非 agent 使用 |

> **边界**：方案③ 下 agent 通过 `grapecli git` 本地执行 git，platform token 只在 **CLI 进程内**（credential helper 用完即弃），不暴露给 agent；grape 服务端不实际跑 git（仅提供 token/工作区查询）。未来 GitHub/GitLab 增加时，CLI 的 credential 获取按 provider 切换即可。

---

## 1. 仓库信息

### GET /api/repos/{owner}/{repo}

**入参**

| 位置 | 参数 | 类型 | 说明 |
|------|------|------|------|
| Path | `owner` | string | 仓库属主 |
| Path | `repo` | string | 仓库名 |
| Header/Query | `X-Git-Provider` / `provider` | string | 缺省 gitcode |

**返回（200）**：仓库对象
```json
{
  "id": 10489208,
  "name": "hello",
  "full_name": "Jason7602/hello",
  "description": "...",
  "default_branch": "main",
  "html_url": "https://gitcode.com/Jason7602/hello",
  "clone_url": "...",
  "visibility_level": 20,
  "created_at": "..."
}
```
**错误**：401 / 403 / 404（仓库不存在或无权）。

---

## 2. 分支

### GET /api/repos/{owner}/{repo}/branches

**入参**：同仓库信息（Path owner/repo + provider）；Query `?per_page=50`（技能用）。

**返回（200）**：分支对象数组
```json
[
  {"name": "main", "protected": false, "commit": {"id": "sha", "message": "..."}},
  {"name": "feat/issue-7", "protected": false}
]
```
**错误**：401 / 403 / 404。

---

## 3. 文件内容

### GET /api/repos/{owner}/{repo}/contents/{path}?ref={branch}

**入参**

| 位置 | 参数 | 类型 | 说明 |
|------|------|------|------|
| Path | `owner`/`repo`/`path` | string | 文件路径（技能多用 `CODEOWNERS`） |
| Query | `ref` | string | 分支/引用（技能用 `ref=codeowner`） |

**返回（200）**：文件对象（base64 content）
```json
{"name": "CODEOWNERS", "path": "CODEOWNERS", "size": 123,
 "sha": "sha", "content": "<base64>", "encoding": "base64",
 "type": "file", "download_url": "..."}
```
**错误**：401 / 403 / 404（文件不存在）。

---

## 4. 提交历史

### GET /api/repos/{owner}/{repo}/commits

**入参**：Path owner/repo；Query `?per_page=`（可选）。

**返回（200）**：提交对象数组
```json
[{"sha": "...", "message": "...", "author": {"name": "x", "email": "x@x"},
  "committer": {"name": "x"}, "date": "2026-..."}]
```
**错误**：401 / 403 / 404。

---

## 5. 标签

### GET /api/repos/{owner}/{repo}/labels

**返回（200）**：标签数组 `[{"id":1,"name":"bug","color":"#ff0000","description":""}]`

### POST /api/repos/{owner}/{repo}/labels

**Body（form-encoded 或 JSON）**：`name`（必填）、`color`（如 `#d73a4a`）

**返回（201）**：创建后的标签对象。**错误**：400（缺 name）/ 409（已存在）/ 403。

---

## 6. 里程碑

### GET /api/repos/{owner}/{repo}/milestones?state=all

**Query**：`state`（open/closed/all，技能用 all）

**返回（200）**：里程碑数组 `[{"id":1,"title":"v1.0","state":"open","description":"","due_on":null,"open_issues":3,"closed_issues":1,"created_at":"..."}]`

**错误**：401 / 403 / 404。

---

## 7. Issues

### GET /api/repos/{owner}/{repo}/issues

**入参**：Query `?state=open&page=1&per_page=50`（state: open/closed/all）

**返回（200）**：Issue 对象数组
```json
[{"id": 4312271, "number": 5, "title": "bug: ...", "state": "opened",
  "body": "desc", "labels": [{"name":"bug"}],
  "assignee": {"username":"Jason7602"}, "assignees": [...],
  "closed_at": null, "created_at": "2026-...", "milestone": null}]
```

### POST /api/repos/{owner}/{repo}/issues

**入参**：
- GitCode（issue-advance 风格）：Body `{repo, title, body}`（repo = 仓库名，可省略）
- gitcode 标准：Body `{title, body, assignee?}`

**入参 Body**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `title` | string | ✅ | Issue 标题 |
| `body` | string | ✅ | Issue 正文 |
| `assignee` | string | 否 | 负责人用户名 |

**返回（201）**：创建后的 Issue 对象（同 GET 单个结构）。

### GET /api/repos/{owner}/{repo}/issues/{number}

**返回（200）**：单个 Issue 对象（结构同上）。**错误**：404。

### PATCH /api/repos/{owner}/{repo}/issues/{number}

**入参 Body（JSON）**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `labels` | string | 标签（逗号分隔字符串）或数组 |
| `assignee` | string | 责任人（单） |
| `assignee_ids` | array | 责任人 id 数组（implementation 风格） |
| `milestone_id` | int | 里程碑 id |
| `state` | string | `"close"`（**注意**：GitCode 关闭用 "close" 非 "closed"） |

**返回（200）**：更新后的 Issue 对象。**错误**：400/404/403。

### GET/POST /api/repos/{owner}/{repo}/issues/{number}/comments

- **GET**：返回评论数组 `[{"id":1,"body":"...","user":{"username":"x"},"created_at":"..."}]`
- **POST**：Body `{body}`（必填）→ 返回（201）创建后的评论对象。

### GET/DELETE /api/repos/{owner}/{repo}/issues/{number}/labels

- **GET**：返回标签数组。
- **DELETE**：清除全部标签 → 返回 204（空）。

---

## 8. Pull Requests

### GET /api/repos/{owner}/{repo}/pulls

**返回（200）**：PR 对象数组
```json
[{"id": 1, "number": 1, "title": "feat: #7 ...", "state": "opened",
  "head": {"ref": "feat/issue-7"}, "base": {"ref": "main"},
  "body": "...", "merged": false, "mergeable": true, "user": {"username":"x"},
  "created_at": "...", "html_url": "..."}]
```

### POST /api/repos/{owner}/{repo}/pulls

**入参 Body（JSON）**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `title` | string | ✅ | PR 标题（如 `feat: #7 标题`） |
| `head` | string | ✅ | 源分支（push 的分支，如 `feat/issue-7`） |
| `base` | string | ✅ | 目标分支（main/master） |
| `inner_issue_nums` | string | 否 | 关联 Issue（`owner/repo#7`） |
| `close_related_issue` | bool | 否 | true=合并后自动关闭关联 Issue |

**返回（201）**：创建的 PR 对象。**错误**：400（缺 head/base）/ 404 / 403。

### GET /api/repos/{owner}/{repo}/pulls/{number}

**返回（200）**：单个 PR 对象（结构同上）。**错误**：404。

### PATCH /api/repos/{owner}/{repo}/pulls/{number}

**入参 Body（JSON）**：`title` / `body` / `labels`（按需更新字段）。

**返回（200）**：更新后的 PR 对象。

### PUT /api/repos/{owner}/{repo}/pulls/{number}/merge

**返回（200）**：`{"merged": true, "sha": "..."}`。**错误**：409（不可合并）。

### GET/POST /api/repos/{owner}/{repo}/pulls/{number}/comments

- **GET**：返回 PR 行内评论数组 `[{"id":1,"body":"LGTM","path":"a.py","line":5,"user":{"username":"x"},"created_at":"..."}]`
- **POST**：Body `{body}`（必填）→ 返回（201）评论对象。
- **PUT /pulls/{number}/comments/{discussion_id}**：标记 resolved → 更新后的评论对象。

### GET /api/repos/{owner}/{repo}/pulls/{number}/files

**返回（200）**：变更文件数组
```json
[{"filename": "src/a.py", "status": "modified", "additions": 10, "deletions": 2,
  "patch": "@@ ...", "raw_url": "..."}]
```

---

## 通用错误（全接口一致）

| 码 | 含义 | body |
|----|------|------|
| 401 | 未认证（缺失/无效 API Key） | `{"detail":{"code":401,"message":"401 Unauthorized"}}` |
| 402/403 | 无权限（写操作/白名单外 path/未知 provider） | `{"detail":{"code":403,"message":"..."}}` |
| 404 | 资源不存在/无权 | `{"detail":{"code":404,"message":"..."}}` |
| 400 | 参数缺失/校验失败 | `{"detail":{"code":400,"message":"..."}}` |
| 502 | 平台 token 解析失败 | `{"detail":{"code":502,"message":"Git 仓库 token 解析失败"}}` |
| 429 | 平台限流（400/min，4000/hour） | 透传 |

> 技能迁移后：`gitcode-api-reference.md` 中 `api.gitcode.com/api/v5` → `{{SERVER_URL}}/api/repos/...`；认证 `$BOT_GITCODE_TOKEN` → `$GRAPE_API_TOKEN`。