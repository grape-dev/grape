# Issue #80 技术方案：GitHub 平台接入——双 VCS provider 全链路集成（已实施）

> Issue: https://gitcode.com/grape-dev/grape/issues/80
> 需求：接入 GitHub 作为第二个 VCS provider，实现 OAuth 登录 + 仓库托管 + Webhook 事件 + 事件流水线全链路双平台并行
> 关联分支：`grape-dev/dev-github`（基础 commit e3e88c5a）+ 本地 `mlc-0903`（增量修复）
> 基线：`grape-dev/weekly-02`（commit c9746a67）
> 状态：**已实施并验证**（本地 49 文件未提交）

## 关联文档

- GitCode OAuth Token 刷新：`docs/weekly-02/issue-52-gitcode-token-refresh-design.md`
- API Token 认证机制：`docs/weekly-02/issue-45-api-token-design.md`
- 早期集成计划（已过时）：`docs/github-oauth-integration-plan.md`（dev-github 分支）

---

## 1. 现状分析

### 1.1 单 provider 架构

Grape 平台最初仅支持 GitCode 作为 VCS provider。所有 API 调用、OAuth 登录、Webhook 接收、Agent prompt 均硬编码为 GitCode：

```
OAuth:  gitcode.com/oauth/authorize → /api/auth/callback/gitcode
API:    api.gitcode.com/api/v5/user/repos, /repos/{owner}/{repo}/...
Webhook: /api/webhook/gitcode（明文 token 验证）
Prompt: "Use GitCode API + GITCODE_TOKEN"
DB:     user_accounts.gitcode_id, managed_repos.repo_key LIKE 'gitcode:%'
```

### 1.2 需要接入 GitHub 的原因

用户在 GitHub 上也有仓库，需要同一平台同时管理 GitCode 和 GitHub 仓库的 DevOps 流程。不能要求用户在两个平台之间切换。

### 1.3 GitCode API 端点迁移（实施中发现）

实施过程中发现 GitCode 把仓库相关 API 从 `api.gitcode.com` 迁移到了 `gitcode.com`：

| 端点 | api.gitcode.com | gitcode.com |
|------|----------------|-------------|
| `/api/v5/user` | 200（自签名证书） | 200（证书正常） |
| `/api/v5/user/repos` | **404** | 200 |
| `/api/v5/repos/{owner}/{repo}` | **404** | 200 |
| `/api/v5/repos/.../hooks` | **404** | 200 |

全部仓库相关 API 从 `api.gitcode.com` 迁移到 `gitcode.com`，同时去掉 `verify=False`（gitcode.com 证书正常）。

---

## 2. 设计目标

1. **双 provider 并行**——GitCode 和 GitHub 仓库在同一 UI 中共存，用户可同时管理两个平台的仓库
2. **provider 感知**——所有 VCS API 调用按 repo_key 前缀自动选择正确的平台 URL 和 token
3. **零破坏性**——不修改现有 GitCode 功能逻辑，GitHub 功能以增量方式叠加
4. **用户隔离**——用户只能访问自己有权的仓库，不能跨用户/跨仓库查看事件和数据
5. **统一流水线**——GitHub webhook 事件复用现有 Agent 流水线（VOD → Issue → Coding → QA → SRE）

---

## 3. 架构设计

### 3.1 repo_key 前缀分流

所有 provider 感知的核心机制——repo_key 带平台前缀：

```
gitcode:owner/repo   → GitCode 仓库
github:owner/repo    → GitHub 仓库
```

前缀决定了：
- API URL（`gitcode.com/api/v5` vs `api.github.com`）
- Token 类型（`GITCODE_TOKEN` vs `GITHUB_TOKEN`）
- Webhook 验证方式（明文 token vs HMAC-SHA256）
- 前端链接（`gitcode.com/owner/repo` vs `github.com/owner/repo`）

### 3.2 系统架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                         前端 (app.js)                            │
│  ┌──────────┐  ┌──────────────┐  ┌─────────────────────────┐   │
│  │GitCode   │  │GitHub 登录    │  │ currentRepo (localStorage)│  │
│  │登录按钮   │  │按钮          │  │ repo_key 带前缀           │  │
│  └────┬─────┘  └──────┬───────┘  └───────────┬─────────────┘   │
│       │               │                      │                  │
│       ▼               ▼                      ▼                  │
│  /api/auth/login  /api/auth/login    /api/dashboard/{o}/{r}     │
│  /gitcode         /github            /api/events/{o}/{r}        │
│       │               │               /api/pipeline/{o}/{r}      │
└───────┼───────────────┼───────────────┼──────────────────────────┘
        │               │               │
┌───────▼───────────────▼───────────────▼──────────────────────────┐
│                         后端 API 层                                │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  auth.py — OAuth 双回调                                    │   │
│  │  /callback/gitcode → GitCode token → user_accounts         │   │
│  │  /callback/github  → GitHub token  → user_accounts         │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  deps.py — Token 分流                                     │   │
│  │  get_gitcode_token(repo_key) → 按前缀分流                  │   │
│  │  get_github_token(repo_key) → GitHub 回退链               │   │
│  │  check_repo_access(user, repo_key) → 用户仓库权限校验      │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  repo_manager.py — 仓库管理                                │   │
│  │  fetch_repos_with_role()     → GitCode 仓库列表            │   │
│  │  fetch_github_repos_with_role() → GitHub 仓库列表          │   │
│  │  setup_webhook()             → GitCode webhook            │   │
│  │  setup_github_webhook()      → GitHub webhook (HMAC)      │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  webhook.py — 事件接收                                     │   │
│  │  /webhook/gitcode → 明文 token 验证                       │   │
│  │  /webhook/github  → HMAC-SHA256 签名验证                  │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  event_pipeline.py — provider 感知                         │   │
│  │  _is_github_repo() → 前缀判断                             │   │
│  │  _get_repo_api_token() → 按 provider 取 token             │   │
│  │  _github_fetch_issue() → GitHub issue 详情               │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  prompt_builder.py — Agent prompt 适配                    │   │
│  │  _vcs_api_base()  → API URL 按 provider                  │   │
│  │  _vcs_token_env() → token 环境变量名                      │   │
│  │  _vcs_name()      → "GitHub" / "GitCode"                 │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
        │
┌───────▼──────────────────────────────────────────────────────────┐
│                         数据层 (SQLite)                           │
│  user_accounts: gitcode_id, github_id, github_api_token, ...     │
│  managed_repos: repo_key (带前缀), bot_gitcode_token,            │
│                 bot_github_token, github_webhook_token            │
│  user_repos:    user_id + repo_key (带前缀)                      │
│  events:        repo_key (带前缀)                                 │
│  login_whitelist: gitcode_user, github_username                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 4. 详细设计

### 4.1 GitHub OAuth 登录

#### 4.1.1 流程

```
用户点击 "GitHub 登录" 按钮
  │
  ▼
GET /api/auth/login/github
  → 生成 state（CSRF 防护，存 oauth_states 表）
  → 重定向到 github.com/login/oauth/authorize
     scope=repo user admin:repo_hook offline_access
  │
  ▼
GitHub 授权页 → 用户授权 → 回调
  │
  ▼
GET /api/auth/callback/github?code=xxx&state=xxx
  → validate_oauth_state(state, "github")
  → POST github.com/login/oauth/access_token (code → token)
  → GET api.github.com/user (token → 用户信息)
  → get_user_by_github_id(github_id) 或 get_user_by_username(github_login)
  → is_github_user_allowed(github_login) 白名单校验
  → 新用户 → create_user() + link_github_user()
  → 已有用户 → link_github_user() 刷新 token
  → 签发 JWT (access + refresh)
  → 302 重定向到前端 /?token=xxx&first_login=0/1
```

#### 4.1.2 OAuth 失败处理

GitHub 网络超时（服务器在国内，连 github.com 可能超时）：

```python
def _oauth_error_redirect(request: Request, message: str):
    """OAuth 回调失败 → 重定向到前端 /login?oauth_error=xxx"""
    return RedirectResponse(url=f"{base}/login?oauth_error={encoded}", status_code=302)
```

前端 `init()` 检测 URL 中的 `oauth_error` 参数，显示 toast 提示并清理 URL。

#### 4.1.3 GitHub App Installation 检测

OAuth 回调后检测 GitHub App installation 状态：

```python
# 检测是否为 GitHub App token（X-OAuth-Scopes 为空且 X-OAuth-Client-Id 存在）
_perm_req = urllib.request.Request("https://api.github.com/user/repos?per_page=1", ...)
_scopes = _perm_resp.headers.get("X-OAuth-Scopes", "")
_client_id = _perm_resp.headers.get("X-OAuth-Client-Id", "")

if _client_id and not _scopes.strip():
    # GitHub App token：检查 installation
    _inst_data = json.loads(urllib.request.urlopen(
        "https://api.github.com/user/installations?per_page=1", ...
    ))
    if _inst_count == 0:
        _gh_perm_warning = "GitHub App 未安装到您的账户..."
    else:
        _gh_perm_warning = "GitHub App 已安装但权限可能不足..."
```

警告通过 URL 参数 `gh_perm_warning=xxx` 传给前端 toast。

#### 4.1.4 白名单

```python
def is_github_user_allowed(github_login: str) -> bool:
    """白名单判定（GitHub 用户名）。
    优先查 DB login_whitelist 表 github_username 列，降级读 login-whitelist.json。
    DB 无数据 / 文件缺失 → 全部放行（默认开放）。
    """
```

### 4.2 Provider-aware Token 分流

#### 4.2.1 Token 体系

| Token 类型 | 存储位置 | 用途 | 获取方式 |
|-----------|---------|------|---------|
| 用户 GitCode token | user_accounts.gitcode_api_token | 用户身份调 GitCode API | OAuth 登录 |
| 用户 GitHub token | user_accounts.github_api_token | 用户身份调 GitHub API | OAuth 登录 |
| 仓库 bot GitCode token | managed_repos.bot_gitcode_token | Bot 身份调 GitCode API | 仓库托管时配置 |
| 仓库 bot GitHub token | managed_repos.bot_github_token | Bot 身份调 GitHub API | 仓库托管时配置 |
| 全局 bot token | configs/global-bot-token.json | 兜底 bot token | 配置文件 |

#### 4.2.2 分流逻辑

`get_gitcode_token()` 按 repo_key 前缀分流（event_pipeline.py 4 处调用）：

```python
# event_pipeline.py
if repo_key.startswith("github:"):
    from src.user.db import get_github_token
    repo_gitcode_token = get_github_token(repo_key=repo_key) or ""
else:
    from src.user.db import get_gitcode_token
    repo_gitcode_token = get_gitcode_token(repo_key=repo_key) or ""
```

`get_github_token()` 回退链：

```python
# deps.py / user/db.py
def get_github_token(repo_key: str = "") -> str:
    # 1. managed_repos.bot_github_token（仓库级 bot token）
    # 2. user_accounts.github_api_token（owner 级用户 token）
    # 3. global-bot-token.json（全局兜底）
```

`_get_user_provider_and_token()` 自动选择 provider（repos.py）：

```python
def _get_user_provider_and_token(username: str) -> tuple[str, str]:
    github_token = get_user_github_token(username)
    if github_token:
        return "github", github_token
    gitcode_token = get_user_token(username)
    if gitcode_token:
        return "gitcode", gitcode_token
    return "", ""
```

#### 4.2.3 Token 刷新

GitHub OAuth token 的刷新复用 Issue #52 的 GitCode token 刷新机制（`refresh_gitcode_oauth_token`），按 provider 分别刷新。

### 4.3 GitHub 仓库托管

#### 4.3.1 仓库列表

```python
# repos.py — list_member_repos
provider, api_token = _get_user_provider_and_token(user["username"])
if provider == "github":
    repos = fetch_github_repos_with_role(api_token)  # GitHub API
    for r in repos:
        r["repo_key"] = f"github:{r['full_name']}"   # 补前缀
else:
    repos = fetch_repos_with_role(api_token)          # GitCode API
```

#### 4.3.2 仓库托管流程

```
用户选择仓库 → 点击"加入并托管"
  │
  ├─ GitHub 仓库 (provider == "github")
  │   ├─ create_github_repo()（如果是新建）
  │   ├─ find_existing_github_webhook() → 检查已有 webhook
  │   ├─ setup_github_webhook() → 创建 webhook (HMAC-SHA256 secret)
  │   └─ managed_repos.repo_key = "github:owner/repo"
  │
  └─ GitCode 仓库 (provider == "gitcode")
      ├─ create_repo()
      ├─ find_existing_webhook()
      ├─ setup_webhook() → 创建 webhook (明文 token)
      └─ managed_repos.repo_key = "gitcode:owner/repo"
```

#### 4.3.3 失效检测

`_probe_stale_repos` 对 GitHub 仓库也做失效检测（仓库被删 / webhook 丢失）：

```python
if provider == "github":
    from src.user.repo_manager import _github_req
    _github_req("GET", f"https://api.github.com/repos/{login}/{name}", api_token)
```

### 4.4 GitHub Webhook 事件接收

#### 4.4.1 端点

```python
@router.post("/webhook/github")
async def github_webhook(request: Request):
    event_header = request.headers.get("X-GitHub-Event", "unknown")
    body = await request.body()

    # 1. 提取 repo full_name
    repo = payload.get("repository", {}).get("full_name", "")

    # 2. 获取 webhook secret（managed_repos.github_webhook_token）
    expected = get_webhook_token_by_repo(f"github:{repo}")

    # 3. HMAC-SHA256 签名验证
    signature = request.headers.get("X-Hub-Signature-256", "")
    computed = hmac.new(expected.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(f"sha256={computed}", signature):
        raise HTTPException(status_code=403)
```

#### 4.4.2 事件解析

GitHub issue 事件映射到 Grape 内部事件类型：

| GitHub Event | Action | Grape event_type |
|-------------|--------|-----------------|
| issues | opened | issue_opened |
| issues | closed | issue_closed |
| issues | reopened | issue_reopened |
| issue_comment | created | issue_comment |
| issue_comment | edited | issue_comment_edited |
| issue_comment | deleted | (ignored) |

事件写入 events 表时 repo_key 带前缀：`github:owner/repo`。

### 4.5 事件流水线 Provider 感知

#### 4.5.1 VCS API URL 分流

```python
# event_pipeline.py
def _is_github_repo(repo_key: str) -> bool:
    return bool(repo_key) and repo_key.startswith("github:")

def _get_repo_api_token(repo_key: str) -> str:
    if _is_github_repo(repo_key):
        return get_github_token(repo_key=repo_key) or ""
    return get_gitcode_token(repo_key=repo_key) or ""
```

#### 4.5.2 Issue 详情获取

```python
def _github_fetch_issue(owner, repo, issue_num, token):
    """通过 GitHub API 获取 Issue 详情。"""
    return _github_req("GET", f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_num}", token)
```

#### 4.5.3 Issue 操作（tasks.py）

```python
# 创建 issue
if repo_key.startswith("github:"):
    _post_github_issue(api_token, owner, repo, title, body)
else:
    _post_gitcode_issue(api_token, owner, repo, title, body)

# 添加 comment
if repo_key.startswith("github:"):
    _github_req("POST", f"https://api.github.com/repos/{owner}/{repo}/issues/{num}/comments", api_token, {"body": msg})
else:
    # GitCode: ?access_token=xxx query param
```

### 4.6 Agent Prompt 适配

#### 4.6.1 Provider 感知函数

```python
# prompt_builder.py
def _vcs_api_base(repo_key: str) -> str:
    return "https://api.github.com" if _is_github_repo(repo_key) else "https://gitcode.com/api/v5"

def _vcs_token_env(repo_key: str) -> str:
    return "USER_GITHUB_TOKEN" if _is_github_repo(repo_key) else "USER_GITCODE_TOKEN"

def _vcs_bot_token_env(repo_key: str) -> str:
    return "BOT_GITHUB_TOKEN" if _is_github_repo(repo_key) else "BOT_GITCODE_TOKEN"

def _vcs_name(repo_key: str) -> str:
    return "GitHub" if _is_github_repo(repo_key) else "GitCode"
```

#### 4.6.2 Prompt 生成

```python
# build_role_definition()
_vcs = _vcs_name(repo_key)          # "GitHub" or "GitCode"
_token_env = _vcs_token_env(repo_key)  # "BOT_GITHUB_TOKEN" or "BOT_GITCODE_TOKEN"

f"1. Use ONLY the {_vcs} token provided in this prompt ({_bot_token_env})."
f"3. If the {_vcs} API returns 403: tell the user to configure it manually in {_vcs}."
```

GitHub 仓库的 Agent prompt 生成 `GitHub API + GITHUB_TOKEN`，GitCode 仓库保持 `GitCode API + GITCODE_TOKEN`。

### 4.7 前端适配

#### 4.7.1 登录页

```html
<!-- index.html -->
<button onclick="app.oauthLoginGithub()">GitHub 登录</button>
<button onclick="app.oauthLogin()">GitCode 登录</button>
```

#### 4.7.2 VCS 链接生成

```javascript
// app.js — _vcsUrl()
function _vcsUrl(repoKey, path) {
    var host = repoKey.indexOf('github:') === 0 ? 'https://github.com' : 'https://gitcode.com';
    var repoPath = parts.length > 1 ? parts[1] : repoKey;
    return host + '/' + repoPath + (path || '');
}
```

#### 4.7.3 用户缓存隔离

切换账号时清理 localStorage 中所有用户相关缓存：

```javascript
// logout()
localStorage.removeItem('token');
localStorage.removeItem('user');
localStorage.removeItem('currentRepo');      // 新增
localStorage.removeItem(REPO_CACHE_KEY);      // 新增（repo_member_cache）

// OAuth 回调 init()
localStorage.removeItem(REPO_CACHE_KEY);      // 每次登录都清

// 密码登录 pwdLogin()
localStorage.removeItem('currentRepo');       // 新增
localStorage.removeItem(REPO_CACHE_KEY);      // 新增

// cache version mismatch
localStorage.clear();  // 不再保留 oldRepo
```

### 4.8 用户隔离（安全加固）

#### 4.8.1 后端权限校验

```python
# deps.py
def check_repo_access(user: dict, repo_key: str) -> None:
    """校验用户是否有权访问指定仓库。
    session_token / api_key 跳过（已有作用域限定）。
    同时检查 gitcode: 和 github: 前缀（前端 URL 不含前缀）。
    """
    candidates = {f"gitcode:{base}", f"github:{base}"}
    repos = user_db.get_managed_repos(uid)
    for r in repos:
        if r["repo_key"] in candidates:
            return
    raise HTTPException(status_code=403, detail={"message": "无权访问该仓库"})
```

校验接入点：

| 接口 | 文件 | 校验位置 |
|------|------|---------|
| `GET /api/dashboard/{owner}/{repo}` | dashboard.py | `check_repo_access(user, repo_key)` |
| `GET /api/events/{owner}/{repo}` | events.py | `check_repo_access(user, repo_key)` |
| `GET /api/events/detail/{seq}` | events.py | `check_repo_access(user, row.repo_key)` |
| `GET /api/pipeline/{owner}/{repo}` | pipeline.py | `check_repo_access(user, repo_key)` |
| `GET /api/agents/{name}/{owner}/{repo}` | agents.py | `check_repo_access(user, repo_key)` |
| `GET /api/search` | search.py | 事件结果按用户仓库集合过滤 |

#### 4.8.2 验证结果

```
grape-bot → mm6667/hello-blog dashboard → 403 ✓
grape-bot → mm6667/hello-blog events    → 403 ✓
grape-bot → ancha-meng/test dashboard   → 403 ✓
mm6667   → mm6667/hello-blog dashboard  → 200 ✓
mm6667   → ancha-meng/test dashboard    → 403 ✓
```

### 4.9 GitCode API 迁移

#### 4.9.1 变更范围

`api.gitcode.com` → `gitcode.com`，涉及 12 个 Python 文件：

| 文件 | 改动 |
|------|------|
| `src/user/repo_manager.py` | `API_BASE` 改为 `gitcode.com`，去掉 `verify=False` |
| `src/api/routes/auth.py` | OAuth 用户信息获取改为 `gitcode.com`，去掉 `verify=False` |
| `src/api/routes/repos.py` | 仓库探活 / webhook 查询 URL |
| `src/api/routes/tasks.py` | Issue 创建 / comment URL |
| `src/api/routes/scenes.py` | 分支查询 URL |
| `src/api/event_pipeline.py` | Issue 详情 / comment URL |
| `src/api/git_repo_api.py` | `_GITCODE_API_BASE` |
| `src/config.py` | 全局 SSL 配置（保留，影响 urllib） |
| `src/prompt_builder.py` | Prompt 中的 API URL |
| `src/project_info/builder.py` | Commit 查询 URL |
| `src/im/feishu_async.py` | Collaborator 查询 URL |
| `src/vcs/gitcode.py` | Collaborator URL |

#### 4.9.2 SSL 验证

- `gitcode.com`：证书正常，`verify=True`（默认）
- `api.gitcode.com`：自签名证书（GFW SNI 拦截），仅 `/api/v5/user` 还能用，auth.py 中保留 `verify=False`
- `api.github.com`：证书正常

---

## 5. 数据库变更

### 5.1 user_accounts 表

```sql
ALTER TABLE user_accounts ADD COLUMN github_id TEXT DEFAULT '';
ALTER TABLE user_accounts ADD COLUMN github_api_token TEXT DEFAULT '';
ALTER TABLE user_accounts ADD COLUMN github_login TEXT DEFAULT '';
```

### 5.2 managed_repos 表

```sql
-- 新增列（幂等迁移）
ALTER TABLE managed_repos ADD COLUMN bot_github_token TEXT DEFAULT '';
ALTER TABLE managed_repos ADD COLUMN github_webhook_token TEXT DEFAULT '';
```

repo_key 支持 `github:` 前缀，与 `gitcode:` 共存。

### 5.3 user_repos 表

repo_key 支持 `github:` 前缀。`_fetch_and_cache()` 对 GitHub 仓库补前缀：

```python
if provider == "github":
    r["repo_key"] = f"github:{r['full_name']}"
```

### 5.4 login_whitelist 表

```sql
ALTER TABLE login_whitelist ADD COLUMN github_username TEXT DEFAULT '';
```

### 5.5 user_credentials 表

GitHub OAuth token 存储：

```python
# provider = "github_oauth"
{"github_id": "...", "access_token": "...", "refresh_token": "...", "fetched_at": ...}
```

### 5.6 global-bot-token.json

```json
{
  "gitcode_oauth_client_id": "...",
  "gitcode_oauth_client_secret": "...",
  "github_oauth_client_id": "...",
  "github_oauth_client_secret": "...",
  "github_api_token": "ghp_..."
}
```

---

## 6. 实施清单

### 6.1 后端（Python）

| 文件 | 改动内容 | 行数 |
|------|---------|------|
| `src/api/routes/auth.py` | GitHub OAuth 路由 + installation 检测 + 错误重定向 | +186 |
| `src/api/deps.py` | `get_user_github_token()` + `check_repo_access()` | +34 |
| `src/user/db.py` | `get_user_by_github_id()` + `link_github_user()` + 列迁移 | +161 |
| `src/user/repo_manager.py` | `fetch_github_repos_with_role()` + GitHub webhook + `_github_req()` + API_BASE 迁移 | +193 |
| `src/api/routes/repos.py` | `_get_user_provider_and_token()` + GitHub 仓库托管 | +203 |
| `src/api/routes/webhook.py` | `/webhook/github` + HMAC-SHA256 验证 | +132 |
| `src/api/event_pipeline.py` | `_is_github_repo()` + `_get_repo_api_token()` + `_github_fetch_issue()` | +264 |
| `src/api/routes/tasks.py` | GitHub issue 创建/comment + provider 分流 | +284 |
| `src/prompt_builder.py` | `_vcs_api_base()` + `_vcs_token_env()` + `_vcs_name()` | +90 |
| `src/api/git_repo_api.py` | `_SUPPORTED_PROVIDERS` 加 github + `_GITHUB_API_BASE` | +130 |
| `src/config.py` | `is_github_user_allowed()` + GitHub 配置读取 | +52 |
| `src/api/routes/dashboard.py` | `check_repo_access()` 校验 | +4 |
| `src/api/routes/events.py` | `check_repo_access()` 校验（列表 + 详情） | +11 |
| `src/api/routes/pipeline.py` | `check_repo_access()` 校验 | +44 |
| `src/api/routes/agents.py` | `check_repo_access()` 校验 | +27 |
| `src/api/routes/search.py` | 事件搜索按用户仓库过滤 | +10 |
| `src/api/routes/scenes.py` | API URL 迁移 | +4 |
| `src/project_info/builder.py` | API URL 迁移 | +4 |
| `src/im/feishu_async.py` | API URL 迁移 | +2 |
| `src/vcs/gitcode.py` | API URL 迁移 | +2 |

### 6.2 前端（JavaScript）

| 文件 | 改动内容 |
|------|---------|
| `src/api/static/js/app.js` | GitHub 登录按钮 + `_vcsUrl()` + 缓存隔离（logout/OAuth/密码登录） |
| `src/api/static/js/pages/runtimes.js` | Codex logo 改用 `<img>` 标签 |
| `src/api/static/js/pages/repos.js` | GitHub 仓库列表展示 |
| `src/api/static/js/pages/tasks.js` | VCS 链接适配 |
| `src/api/static/js/pages/dashboard.js` | VCS 链接适配 |
| `src/api/static/index.html` | GitHub 登录按钮 + 脚本版本号 |
| `src/api/static/locales/zh/app.json` | i18n |
| `src/api/static/locales/en/app.json` | i18n |

### 6.3 测试

| 文件 | 改动内容 |
|------|---------|
| `tests/test_auth_routes.py` | mock 从 `urllib.request.urlopen` 改为 `requests.get`（匹配 auth.py 改动） |
| `tests/test_git_repo_api.py` | github provider 不再 403 |

---

## 7. 验证结果

### 7.1 功能验证

| 验证项 | 结果 |
|--------|------|
| GitHub OAuth 登录全链路 | ✓（ancha-meng, github_id=143864942） |
| GitHub 仓库托管 | ✓（github:ancha-meng/test, webhook_id=676560159） |
| GitHub webhook 事件接收 | ✓（issue_opened 事件正常触发 pipeline） |
| GitCode API 迁移 | ✓（38 个仓库正常拉取） |
| 用户隔离 | ✓（grape-bot → mm6667 仓库 → 403） |
| Codex 引擎 logo | ✓（codex.png 从 gitcode.com/mm6667/logo 下载） |

### 7.2 测试结果

| 测试集 | 结果 |
|--------|------|
| `test_auth_routes.py` | 32 passed |
| `test_git_repo_api.py` | passed（github provider 不再 403） |
| `test_login_whitelist.py` | passed |
| Python 编译检查 | 12 files all OK |
| JS 语法检查 | `node --check` OK |

---

## 8. 待修复

### 8.1 `update_unified_user_info()` 不支持 `github_login` 参数

**问题**：auth.py GitHub OAuth 回调调用：
```python
update_unified_user_info(github_login, open_id="", github_login=github_login)
```
但函数签名只有 `gitcode_user` / `gitcode_nickname`，没有 `github_login`。

**日志证据**：
```
WARNING: GitHub OAuth login: write user mapping failed: 
  update_unified_user_info() got an unexpected keyword argument 'github_login'
```

**修复方案**：给 `update_unified_user_info()` 加 `github_login=""` 参数，在 `unified_user_mapping` 表中增加 `github_login` 列。

### 8.2 本地改动未提交

49 个文件未提交未推送。需要决定提交策略：
- 方案 A：将 dev-github 的 commit e3e88c5a 合入 weekly-02，再叠加本地修复提交
- 方案 B：直接在 mlc-0903 分支上提交全部改动

---

## 9. 配置指南

### 9.1 GitHub OAuth App 创建

1. 前往 https://github.com/settings/applications/new
2. Application name: `Grape DevOps`
3. Homepage URL: `http://<server-ip>`
4. Authorization callback URL: `http://<server-ip>/api/auth/callback/github`
5. 创建后获取 Client ID + Client Secret

### 9.2 GitHub App Installation（可选，用于仓库创建权限）

1. 前往 https://github.com/settings/apps
2. 创建 GitHub App，Repository permissions → Administration: Read and write
3. Install App 到个人账户

### 9.3 Grape 平台配置

在 Settings → OAuth 配置页面设置：
- `oauth_github_client_id`
- `oauth_github_client_secret`
- `oauth_github_redirect_uri`（如 `http://120.46.2.221/api/auth/callback/github`）

或在 `configs/global-bot-token.json` 中配置。

### 9.4 白名单配置

在 `login_whitelist` 表中添加 GitHub 用户名：
```sql
INSERT INTO login_whitelist (gitcode_user, github_username) VALUES ('', 'ancha-meng');
```

或 `configs/login-whitelist.json`：
```json
{
  "whitelist": ["mm6667"],
  "github_whitelist": ["ancha-meng"]
}
```

---

## 10. 安全设计

### 10.1 Webhook 签名验证

| Provider | 验证方式 | Secret |
|----------|---------|--------|
| GitCode | 明文 token 比对 | `managed_repos.gitcode_webhook_token` |
| GitHub | HMAC-SHA256 | `managed_repos.github_webhook_token` |

GitHub 使用 `hmac.compare_digest()` 防止时序攻击。

### 10.2 用户隔离

- 前端：切换账号时清 `currentRepo` + `repo_member_cache`
- 后端：`check_repo_access()` 校验用户管辖仓库列表
- 事件搜索：结果按用户仓库集合过滤
- session_token / api_key：已有作用域限定，跳过校验

### 10.3 Token 安全

- JWT access_token：15 分钟过期，前端 401 自动刷新
- JWT refresh_token：HttpOnly cookie，7 天滚动 / 30 天绝对上限
- VCS OAuth token：存 user_credentials 表，不暴露给前端
- Agent prompt 中只传 token 环境变量名（`BOT_GITHUB_TOKEN`），不传值

### 10.4 CSRF 防护

OAuth state 参数：随机 256 位 SHA-256，存 `oauth_states` 表（10 分钟过期，DB 共享），回调时验证并消耗。
