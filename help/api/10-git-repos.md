# Git 仓库统一接口（/api/git-repo）

> 设计：技能/第三方访问 Git 仓库资源（Issue/PR/内容/分支等）时，统一走 grape 的 Git 仓库 API。
> ⚠️ **grape `/api/repos` 是平台业务接口**（托管/同步/成员/部署 URL 等，见 02-repos.md）；**本接口 `/api/git-repo` 是 Git 仓库透传**（Issue/PR/内容/分支等由 gitcode/github/gitlab 提供）。
> **不是 GitCode 代理**：grape 侧定义中立的 Git 仓库资源语义，backend 由请求头/参数区分（gitcode / github / gitlab，未来扩展）。
> 认证：`get_current_user_or_api_key`（契约收录）+ 体内 `_repo_token` **强制 session**。
> scope（x-token-scope）：**全部 = `session_only`**——仅 `tk_` 会话 token（agent 专用）可用；
> `grape_` API Key / Web JWT → **403**。写操作需授权。
> Base：`{{SERVER_URL}}/api/git-repo`
> 出参结构为 **gitcode 平台实际结构**（与技能 `gitcode-api-reference.md` 一致）；github/gitlab 未来映射。
> CLI：`grape git-repo <子资源>`（如 `grape git-repo branches` 对应 `GET /api/git-repo/{owner}/{repo}/branches`；
> `owner`/`repo` 由 CLI 参数传入，**命令名 ≠ 路径**，见 README「CLI 命令映射」）。

## 后端区分（Git Provider 判定）

Git provider（gitcode / github）**由认证 token 的 `repo_key` 前缀派生**（`gitcode:` / `github:`），
不接受 `X-Git-Provider` / `provider` 参数（单一信息源，避免错配，见 `docs/weekly-02/git-repo-provider-from-token-design.md`）：

```
token（tk_）repo_key = gitcode:owner/repo  → GitCode 后端
token（tk_）repo_key = github:owner/repo   → GitHub 后端
```

| provider | 内部实现 |
|----------|----------|
| `gitcode` | 现状：`repo_manager_vcs` → `api.gitcode.com/api/v5` |
| `github` | 未来接入 `api.github.com` |
| `gitlab` | 未来接入 `gitlab.com/api/v4` |

通用的 `Content-Type: application/json`（有 body 时）；分页 `?page=1&per_page=20`（max 100）。

---
## 1. 仓库信息

### GET /api/git-repo/{owner}/{repo}
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**入参**

| 位置 | 参数 | 类型 | 说明 |
|------|------|------|------|
| Path | `owner` | string | 仓库属主 |
| Path | `repo` | string | 仓库名 |

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

### GET /api/git-repo/{owner}/{repo}/branches
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**入参**：Path `{owner}`/`{repo}`（provider 由 token 派生，不传）；Query `?per_page=50`（技能用）。

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

### GET /api/git-repo/{owner}/{repo}/tree?ref={branch}&depth={single|recursive}
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**入参**

| 参数 | 类型 | 说明 |
|------|------|------|
| `ref` | string(Query) | 分支/引用；空 → 自动解析默认分支 |
| `depth` | string(Query) | `single`（单层，默认） / `recursive`（递归，**仅 GitHub**） |

**返回（200）**：底层 `git/trees/{ref}` 结构
```json
{"tree": [
  {"type": "tree", "name": "configs", "path": "configs", "mode": "040000", "sha": "..."},
  {"type": "blob", "name": "README.md", "path": "README.md", "mode": "100644", "sha": "..."}
]}
```

> **平台差异**：GitCode 单层可用（tree+blob）；`depth=recursive` 在 **GitCode 明确不支持**（返回破碎树，
> 丢失 blob），会返回 **400 提示**；GitHub 原生支持递归（`git/trees/{sha}?recursive=1`）。

**错误**：401 / 403 / 400（GitCode 递归）/ 404。

---

### GET /api/git-repo/{owner}/{repo}/contents — 根目录列表
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

> 无尾斜杠的根路由（`gitrepo_contents_root`），等价 `contents/{path}` 的空 path：返回根目录条目数组。

**返回（200）**：条目数组（同目录列表结构）：
```json
[{"type":"dir","name":"configs","path":"configs","sha":"..."},
 {"type":"file","name":"README.md","path":"README.md","sha":"..."}]
```

---

### GET /api/git-repo/{owner}/{repo}/contents/{path}?ref={branch}
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**入参**

| 位置 | 参数 | 类型 | 说明 |
|------|------|------|------|
| Path | `owner`/`repo`/`path` | string | 路径。**空/省略 → 根目录列表**；目录 → 该目录条目列表；文件 → 单文件 base64 内容（技能多用 `CODEOWNERS`） |
| Query | `ref` | string | 分支/引用（技能用 `ref=codeowner`） |

**返回（200）**：
- **目录/根** → 条目数组（一接口枚举仓库结构，QA/开发 agent 一步拿目录树）：
  ```json
  [{"type":"dir","name":"configs","path":"configs","sha":"..."},
   {"type":"file","name":"README.md","path":"README.md","sha":"..."}]
  ```
- **文件** → 文件对象（base64 content）：
  ```json
  {"name": "CODEOWNERS", "path": "CODEOWNERS", "size": 123,
   "sha": "sha", "content": "<base64>", "encoding": "base64",
   "type": "file", "download_url": "..."}
  ```

> **根目录列表**：`GET /api/git-repo/{owner}/{repo}/contents`（无尾斜杠的根路由
> `gitrepo_contents_root`）或 `/contents?ref=` 请求根目录，平台返回条目数组。

**错误**：401 / 403 / 404（路径不存在）。
> 提示：路径不存在时（尤其根/目录探测），可先取根目录列表确认仓库结构，避免盲目探测吃 404。

---

## 4. 提交历史

### GET /api/git-repo/{owner}/{repo}/commits
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**入参**：Path owner/repo；Query `?per_page=`（可选）。

**返回（200）**：提交对象数组
```json
[{"sha": "...", "message": "...", "author": {"name": "x", "email": "x@x"},
  "committer": {"name": "x"}, "date": "2026-..."}]
```
**错误**：401 / 403 / 404。

---

## 5. 标签

### GET /api/git-repo/{owner}/{repo}/labels
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**返回（200）**：标签数组 `[{"id":1,"name":"bug","color":"#ff0000","description":""}]`

### POST /api/git-repo/{owner}/{repo}/labels
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**Body（form-encoded 或 JSON）**：`name`（必填）、`color`（如 `#d73a4a`）

**返回（201）**：创建后的标签对象。**错误**：400（缺 name）/ 409（已存在）/ 403。

---

## 6. 里程碑

### GET /api/git-repo/{owner}/{repo}/milestones?state=all
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**Query**：`state`（open/closed/all，技能用 all）

**返回（200）**：里程碑数组 `[{"id":1,"title":"v1.0","state":"open","description":"","due_on":null,"open_issues":3,"closed_issues":1,"created_at":"..."}]`

**错误**：401 / 403 / 404。

---

## 7. Issues

### GET /api/git-repo/{owner}/{repo}/issues
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**入参**：Query `?state=open&page=1&per_page=50`（state: open/closed/all）

**返回（200）**：Issue 对象数组
```json
[{"id": 4312271, "number": 5, "title": "bug: ...", "state": "opened",
  "body": "desc", "labels": [{"name":"bug"}],
  "assignee": {"username":"Jason7602"}, "assignees": [...],
  "closed_at": null, "created_at": "2026-...", "milestone": null}]
```

### POST /api/git-repo/{owner}/{repo}/issues
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

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

### GET /api/git-repo/{owner}/{repo}/issues/{number}
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**返回（200）**：单个 Issue 对象（结构同上）。**错误**：404。

### PATCH /api/git-repo/{owner}/{repo}/issues/{number}
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**入参 Body（JSON）**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `labels` | string | 标签（逗号分隔字符串）或数组 |
| `assignee` | string | 责任人（单） |
| `assignee_ids` | array | 责任人 id 数组（implementation 风格） |
| `milestone_id` | int | 里程碑 id |
| `state` | string | `"close"`（**注意**：GitCode 关闭用 "close" 非 "closed"） |
| `body` | string | Issue 正文（同 POST create / PR update 的 body；空值忽略，不影响其他字段） |

**返回（200）**：更新后的 Issue 对象。**错误**：400/404/403。

### POST /api/git-repo/{owner}/{repo}/issues/{number}/close
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

关闭 Issue（对外关闭入口，供 grape-cli / agent 使用；无 Body）。

**平台差异（自动适配，复用 `VcsApiClient.close_issue`）：**

| provider | 平台调用 |
|----------|----------|
| `github` | `PATCH /repos/{owner}/{repo}/issues/{number}` body `{"state":"closed"}` |
| `gitcode` | 先 `GET` 单个 Issue 取当前 `title`（PATCH 必填），再 `PATCH` body `{"state":"close","repo":"owner/repo","title":"当前标题"}`（GitCode 的值是 `close` 不是 `closed`） |

**返回（200）**：关闭后的 Issue 对象（含 `"state":"closed"`）。
**错误**：401 / 403 / 404（Issue 不存在）/ **502（平台关闭未生效或 token 解析失败）**。

### GET/POST /api/git-repo/{owner}/{repo}/issues/{number}/comments
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

- **GET**：返回评论数组 `[{"id":1,"body":"...","user":{"username":"x"},"created_at":"..."}]`
- **POST**：Body `{body}`（必填）→ 返回（201）创建后的评论对象。

---

## 8. Pull Requests

### GET /api/git-repo/{owner}/{repo}/pulls
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**返回（200）**：PR 对象数组
```json
[{"id": 1, "number": 1, "title": "feat: #7 ...", "state": "opened",
  "head": {"ref": "feat/issue-7"}, "base": {"ref": "main"},
  "body": "...", "merged": false, "mergeable": true, "user": {"username":"x"},
  "created_at": "...", "html_url": "..."}]
```

### POST /api/git-repo/{owner}/{repo}/pulls
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**入参 Body（JSON）**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `title` | string | ✅ | PR 标题（如 `feat: #7 标题`） |
| `head` | string | ✅ | 源分支（push 的分支，如 `feat/issue-7`） |
| `base` | string | ✅ | 目标分支（main/master） |
| `inner_issue_nums` | string | 否 | 关联 Issue（`owner/repo#7`） |
| `close_related_issue` | bool | 否 | true=合并后自动关闭关联 Issue |

**返回（201）**：创建的 PR 对象。**错误**：400（缺 head/base）/ 404 / 403。

### GET /api/git-repo/{owner}/{repo}/pulls/{number}
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**返回（200）**：单个 PR 对象（结构同上）。**错误**：404。

### PATCH /api/git-repo/{owner}/{repo}/pulls/{number}
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**入参 Body（JSON）**：`title` / `body` / `labels`（按需更新字段）。

**返回（200）**：更新后的 PR 对象。

### PUT /api/git-repo/{owner}/{repo}/pulls/{number}/merge
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**返回（200）**：`{"merged": true, "sha": "..."}`。**错误**：409（不可合并）。

### GET/POST /api/git-repo/{owner}/{repo}/pulls/{number}/comments
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

- **GET**：返回 PR 行内评论数组 `[{"id":1,"body":"LGTM","path":"a.py","line":5,"user":{"username":"x"},"created_at":"..."}]`
- **POST**：Body `{body}`（必填）→ 返回（201）评论对象。
- **PUT /pulls/{number}/comments/{discussion_id}**：标记 resolved → 更新后的评论对象。

### GET /api/git-repo/{owner}/{repo}/pulls/{number}/files
> scope（x-token-scope）：**session_only**（仅 `tk_` 会话 token；API Key / JWT → 403）

**返回（200）**：变更文件数组
```json
[{"filename": "src/a.py", "status": "modified", "additions": 10, "deletions": 2,
  "patch": "@@ ...", "raw_url": "..."}]
```

---

## 通用错误（全接口一致）

> `detail.message` 英文；格式见 `README.md`「错误响应统一格式」。

| 码 | 含义（message 英文） | body |
|----|------|------|
| 401 | Unauthorized（缺失/无效 API Key） | `{"detail":{"code":401,"message":"Not logged in"}}` |
| 403 | Forbidden（写操作/白名单外 path/未知 provider） | `{"detail":{"code":403,"message":"Forbidden"}}` |
| 404 | Not Found（资源不存在/无权） | `{"detail":{"code":404,"message":"Not Found"}}` |
| 400 | Bad Request（参数缺失/校验失败） | `{"detail":{"code":400,"message":"Invalid parameter"}}` |
| 502 | Bad Gateway（平台 token 解析失败） | `{"detail":{"code":502,"message":"Git repository token resolve failed"}}` |
| 429 | Too Many Requests（平台限流 400/min，4000/hour） | 透传 |

> 技能迁移后：`gitcode-api-reference.md` 中 `api.gitcode.com/api/v5` → `{{SERVER_URL}}/api/git-repo/...`；认证 `$BOT_GITCODE_TOKEN` → `$GRAPE_API_TOKEN`。
