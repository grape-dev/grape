# GitCode 硬编码审计报告

**审计范围**: `src/` 目录下 51 个包含 `gitcode` 的 Python 文件  
**审计日期**: 2026-09-16  
**项目**: Grape DevOps（双 VCS 平台：GitCode + GitHub）

---

## 汇总

| 分类 | 文件数 |
|------|--------|
| 安全（无需修复） | 33 |
| 需修复 | 18 |
| **严重** | 8 |
| **中等** | 6 |
| **低** | 4 |

---

## 一、安全文件（33 个，无需修复）

### 1. 基础设施 / 工具函数（已有 provider 分流或定义层）

| # | 文件 | 理由 |
|---|------|------|
| 1 | `src/repo_key.py` | 工具函数定义层：`to_repo_key` / `parse_repo_key` / `_is_github_repo` 等，`gitcode` 是默认 provider 值 |
| 2 | `src/prompt_builder.py` | 已有分流：`_vcs_api_base` 按 `_is_github_repo` 返回不同 URL（L24） |
| 3 | `src/agent/executor.py` | `_assemble_env` 按 repo_key 注入 BOT_TOKEN；L226-229 `get_user_gitcode_token` 是 fallback 分支 |
| 4 | `src/config.py` | webhook 路径 `/webhook/gitcode` 是路由注册名；`is_gitcode_user_allowed` 是 GitCode 专用白名单 |
| 5 | `src/vcs/gitcode.py` | GitCode backend 定义文件，硬编码 `gitcode.com/api/v5` 是正确的 |
| 6 | `src/vcs/adapter.py` | 适配器注册层：`{"gitcode": GitCodeBackend()}`，webhook 路径映射 |
| 7 | `src/vcs/api_client.py` | `if self.provider == "gitcode"` 分流逻辑（L34-99）；`get_gitcode_token` 在 provider 分流 else 分支（L178-179） |
| 8 | `src/runtime/repogit.py` | `_HOSTS = {"gitcode": "gitcode.com", "github": "github.com"}`，按 provider 选择 host（L15/L54） |
| 9 | `src/api/db_setup.py` | DB DDL：字段名 `repo_gitcode_token`/`bot_gitcode_token`/`user_gitcode_token` 等（改字段成本太高） |
| 10 | `src/api/event_store.py` | `source: str = "gitcode"` 是默认值参数 |
| 11 | `src/api/event_rule.py` | 注释中的 `gitcode/github/gitlab` |
| 12 | `src/api/session_store.py` | DB 字段名 `repo_gitcode_token` 读写（字段名兼容） |
| 13 | `src/api/event_archive.py` | 注释描述历史归档路径格式 |
| 14 | `src/api/pipeline_config.py` | 注释说明 `to_repo_key` 默认前缀 |
| 15 | `src/api/deps.py` | 已有 provider 分流：`_gitcode_refresh_locks` 是锁变量名；L243 `candidates = {"gitcode:...", "github:..."}` 跨平台查询 |
| 16 | `src/mysql_compat.py` | SQL 方言转换：`'gitcode:' || col` → `CONCAT('gitcode:', col)` |
| 17 | `src/storage/__init__.py` | 注释说明 repo_key 前缀兼容 |
| 18 | `src/user/db.py` | DB 层核心：`get_gitcode_token` / `get_github_token` 函数定义；DB 字段名；OAuth refresh 逻辑 |
| 19 | `src/api/routes/auth.py` | GitCode OAuth 专用路由 `/login/gitcode` `/callback/gitcode`，硬编码 GitCode OAuth URL 是正确的 |
| 20 | `src/api/routes/settings.py` | GitCode OAuth 配置管理端点；DB 字段名 |
| 21 | `src/api/routes/agents.py` | DB 字段名 `repo_gitcode_token` 读写；跨平台查询（L121-126 查 `gitcode:` 和 `github:` 前缀） |
| 22 | `src/api/routes/dashboard.py` | 跨平台查询：`_repo_keys = [f"gitcode:{_base}", f"github:{_base}"]`（多处） |
| 23 | `src/api/routes/knowledge.py` | 跨平台查询：`candidates = [f"gitcode:{base}", f"github:{base}"]`（L50） |
| 24 | `src/api/routes/pipeline.py` | 跨平台查询：同时查 `gitcode:` 和 `github:` 前缀（L280-285）；DB 字段名 `gitcode_user` |
| 25 | `src/api/routes/runtime.py` | 跨平台查询：`_rk_list = [f"gitcode:{_base}", f"github:{_base}"]`（L719-720/L752） |
| 26 | `src/api/routes/skills.py` | 跨平台查询 `_resolve_repo_key`（L36-46）；注释 |
| 27 | `src/api/routes/feishu_onboard.py` | DB 字段名 `gitcode_nickname`；注释 |
| 28 | `src/runtime/service.py` | DB 字段名 `repo_gitcode_token` / `user_gitcode_token` 读写；SQL 查询 `gitcode_user` |
| 29 | `src/runtime/agent.py` | 读取 task payload 字段 `repo_gitcode_token` / `user_gitcode_token`（字段名） |
| 30 | `src/runtime/dependency_check.py` | 读取 task payload 字段 `repo_gitcode_token`（字段名） |
| 31 | `src/runtime/engine_select.py` | SQL 查询 `gitcode_user`（DB 字段名）；跨平台 LIKE 匹配 |
| 32 | `src/im/handler_async.py` | DB 字段名 `gitcode_user` / `gitcode_nickname`；`get_user_gitcode_token` + `get_gitcode_token` fallback 是通用 token 解析（GitHub 仓库也会走 `get_gitcode_token` 返回全局 bot token，但设计上 `get_gitcode_token` 内部已处理跨平台） |
| 33 | `src/session_manager.py` | DB 字段名 `gitcode_user` / `gitcode_nickname` / `user_gitcode_token` 读写；`get_feishu_open_id(gitcode_user)` 是参数名 |

---

## 二、需修复文件（18 个）

### 严重（8 个）— API 调用硬编码无 provider 分流

---

#### ❌ 1. `src/api/routes/scenes.py` — L819-829, L916-926

**问题**: `list_repo_branches` 和 `create_repo_branch` 的 GitCode 分支中，硬编码 `gitcode.com/api/v5` URL + 硬编码 `gitcode_api_token` 全局 token 获取，没有判断 GitHub 仓库走 GitHub API。

**代码片段**:
```python
# L819-829 (list_repo_branches GitCode 分支)
token = _gbt.get("gitcode_api_token", "") or ""
url = f"https://gitcode.com/api/v5/repos/{owner}/{repo}/branches?per_page=100&page={page}"
data = _req("GET", url, token)

# L916-926 (create_repo_branch GitCode 分支)
token = _gbt.get("gitcode_api_token", "") or ""
url = f"https://gitcode.com/api/v5/repos/{owner}/{repo}/branches"
```

**说明**: `create_repo_branch`（L830起）已有 `if provider == "github"` 分流，但 `list_repo_branches`（L800起）的 GitCode 分支是唯一路径——GitHub 仓库会错误地调 GitCode API。需要为 `list_repo_branches` 添加 GitHub 分支。

**修复建议**: 
- `list_repo_branches`: 添加 `_detect_repo_provider` + GitHub API 分支（用 `get_github_token` + `api.github.com/repos/.../branches`）
- `create_repo_branch` GitCode 分支的 token 获取：GitHub 分支已正确分流，GitCode 分支保留

---

#### ❌ 2. `src/api/routes/templates.py` — L49, L102-116

**问题**: 
1. `_load_global_bot_config` 硬编码读 `gitcode_api_token`，GitHub 仓库创建模板时也会用 GitCode token
2. `_fetch_template_readme` GitCode 分支硬编码 `gitcode.com/api/v5` URL

**代码片段**:
```python
# L49
bot_token = gbt.get("gitcode_api_token", "") or ""

# L102-116
url = f"https://gitcode.com/api/v5/repos/{source_repo}/contents/README.md"
gitcode_pat = os.environ.get("GITCODE_PAT", "")
```

**说明**: `_fetch_template_readme`（L85起）已有 `source_platform` 判断，`source_platform == "github"` 走 GitHub API。但 `_load_global_bot_config` 返回的 bot_token 始终是 GitCode token，`create_repo_from_template` 中 GitHub 仓库 push 时用的是这个 token。

**修复建议**: `_load_global_bot_config` 增加 provider 参数，GitHub 仓库返回 `github_bot_token`。

---

#### ❌ 3. `src/api/onboarding.py` — L265-271, L324-326

**问题**: `create_onboarding_tasks` 和 `retry_onboarding_task` 硬编码 `get_gitcode_token`，GitHub 仓库的 onboarding 任务创建会调 GitCode API。

**代码片段**:
```python
# L265-271
from src.user.db import get_gitcode_token
bot_token = get_gitcode_token(repo_key=repo_key) or ""

# L324-326
from src.user.db import get_gitcode_token
bot_token = get_gitcode_token(repo_key=repo_key) or ""
```

**说明**: 这两个函数接收 `repo_key` 参数，但未按 provider 分流。GitHub 仓库的 onboarding 会用 GitCode token 调 `create_standard_task` → `_post_gitcode_issue`，对 GitHub 仓库无效。

**修复建议**: 按 `repo_key.startswith("github:")` 分流，GitHub 仓库用 `get_github_token`。

---

#### ❌ 4. `src/api/routes/events.py` — L1110-1111, L1449-1470

**问题**: `_redispatch_event_to_agent` 和 `manual_trigger_agent` 硬编码 `get_gitcode_token`，GitHub 仓库事件触发 agent 时用 GitCode token。

**代码片段**:
```python
# L1110-1111
from src.user.db import get_gitcode_token
bot_token = get_gitcode_token(repo_key=repo_key) or ""

# L1449-1470
from src.user.db import get_gitcode_token
bot_token = get_gitcode_token(repo_key=repo_key) or ""
```

**说明**: 两个函数都有 `repo_key` 参数但未做 provider 分流。GitHub 仓库事件触发 agent 执行时，bot_token 会是 GitCode 全局 token，agent 无法用它操作 GitHub 仓库。

**修复建议**: 按 `_is_github_repo(repo_key)` 分流，GitHub 仓库用 `get_github_token`。

---

#### ❌ 5. `src/api/event_pipeline.py` — L1998-1999

**问题**: `_execute_agent` 的 worktree 清理路径中硬编码 `get_gitcode_token`，GitHub 仓库清理 worktree 时 token 不匹配。

**代码片段**:
```python
# L1998-1999
from src.user.db import get_gitcode_token
_token = get_gitcode_token(repo_key=repo_key) or ""
```

**说明**: 在 `_redispatch_vod_after_recommend` 之前的 worktree 清理中，`repogit.remove_issue_worktree_if_pushed(repo_key, agent_type, _num, token=_token)` 对 GitHub 仓库传入了 GitCode token。

**修复建议**: 按 `_is_github_repo(repo_key)` 分流，或复用已有的 `_get_repo_api_token(repo_key)` 函数（L1380 已定义）。

---

#### ❌ 6. `src/project_info/builder.py` — L301-305, L387-393

**问题**: `build_project_info` 和 `_needs_rebuild` 硬编码 `get_gitcode_token` + `gitcode.com/api/v5` URL，GitHub 仓库的项目信息构建会调 GitCode API。

**代码片段**:
```python
# L301-305
from src.user.db import get_gitcode_token
tok = token or get_gitcode_token(repo_key=repo_key) or ""
req = urllib.request.Request(
    f"https://gitcode.com/api/v5/repos/{owner}/{rname}/commits?per_page=1", ...)

# L387-393
from src.user.db import get_gitcode_token
tok = get_gitcode_token(repo_key=repo_key) or ""
req = urllib.request.Request(
    f"https://gitcode.com/api/v5/repos/{owner}/{rname}/commits?per_page=1", ...)
```

**说明**: 两个函数都接收 `repo_key` 参数但未做 provider 分流。GitHub 仓库查 commit SHA 会调 GitCode API（404 或错误 token）。

**修复建议**: 按 `_is_github_repo(repo_key)` 分流，GitHub 仓库用 `get_github_token` + `api.github.com/repos/.../commits`。

---

#### ❌ 7. `src/api/routes/repos.py` — L587, L816

**问题**: `host_repo` 和 `verify_bot_token` 的 GitCode 分支中硬编码 `gitcode.com/api/v5/user` API，GitHub 仓库会用 GitCode API 验证用户身份。

**代码片段**:
```python
# L587 (host_repo GitCode 分支)
user_data = _req("GET", f"https://gitcode.com/api/v5/user", api_token)

# L816 (verify_bot_token GitCode 分支)
user_data = _req("GET", f"https://gitcode.com/api/v5/user", body.bot_token)
```

**说明**: `host_repo`（L500起）已有 `if provider == "github"` 分流，GitHub 分支用 GitHub API。`verify_bot_token`（L810起）也已有 `parse_repo_key` 分流。**这两处是各自 GitCode 分支内的硬编码，属于正确的 provider 分流后 GitCode 专属逻辑**。

**修正**: 经复查，这两处位于 `if provider == "github": ... else: # GitCode` 的 else 分支内，**实际是安全的**。重新分类为安全。

---

#### ❌ 8. `src/im/feishu_async.py` — L155-166, L725-736

**问题**: `_fetch_gitcode_collaborators` 硬编码 `gitcode.com/api/v5` URL；`sync_member_mapping` 的 token 获取硬编码 `gitcode_api_token`，GitHub 仓库的飞书用户映射同步会调 GitCode API。

**代码片段**:
```python
# L155-166 (_fetch_gitcode_collaborators)
f"https://gitcode.com/api/v5/repos/{owner}/{repo}/collaborators?per_page=100&page={page}"

# L725-736 (sync_member_mapping)
api_token = _gbt.get("gitcode_api_token", "") or ""
if provider == "github":
    collabs = _fetch_github_collaborators(owner, repo, api_token)
else:
    collabs = _fetch_gitcode_collaborators(owner, repo, api_token)
```

**说明**: `sync_member_mapping` 已有 `if provider == "github"` 分流调 `_fetch_github_collaborators`，但 token 获取（L725）硬编码 `gitcode_api_token`，GitHub 仓库传入的是 GitCode 全局 token 而非 GitHub bot token。

**修复建议**: token 获取按 provider 分流，GitHub 仓库从 `global-bot-token.json` 读 `github_bot_token` 或调 `get_github_token`。

---

### 中等（6 个）— Token 获取硬编码无 provider 分流

---

#### ⚠️ 9. `src/api/routes/chat.py` — L212-215, L235-239, L328-356

**问题**: `_resolve_gitcode_token` 和多处 `get_gitcode_token` 调用没有 GitHub token 分流，GitHub 仓库的 Chat Agent 会注入 GitCode token。

**代码片段**:
```python
# L212-215 (create_task_conversation)
from src.user.db import get_gitcode_token
_token = get_gitcode_token(repo_key=rk) or ""

# L235-239 (list_messages 补拉评论)
from src.user.db import get_gitcode_token
_token = get_gitcode_token(repo_key=_rk) or ""

# L328-356 (_resolve_gitcode_token 函数)
def _resolve_gitcode_token(conv, conv_id, user_token, repo_key="") -> str:
    ...
    from src.user.db import get_gitcode_token
    repo_tok = get_gitcode_token(repo_key=repo_key) or ""
```

**说明**: `get_gitcode_token` 内部会查 `devops_agent_session.repo_gitcode_token` → `managed_repos.bot_gitcode_token` → 全局 bot token。对 GitHub 仓库，这些字段可能为空（GitHub 用 `bot_github_token`），最终回退到 GitCode 全局 token，导致 Chat Agent 调 GitHub API 401。

**修复建议**: `_resolve_gitcode_token` 按 `_is_github_repo(repo_key)` 分流；补拉评论处也需分流（或复用 `event_pipeline._sync_issue_comments_to_conversation` 已有的分流逻辑，但需传入正确的 token）。

---

#### ⚠️ 10. `src/api/event_pipeline.py` — L1110-1111 (conversation 路径)

**问题**: `_handle_conversation_message` 中硬编码 `get_gitcode_token`，GitHub 仓库的对话消息处理会注入 GitCode token。

**代码片段**:
```python
# 在 _handle_conversation_message 中
from src.user.db import get_gitcode_token
bot_token = get_gitcode_token(repo_key=repo_key) or ""
```

**说明**: 该函数处理 Issue 对话消息（飞书/Web 触发），`repo_key` 参数已有但未做 provider 分流。

**修复建议**: 按 `_is_github_repo(repo_key)` 分流，GitHub 仓库用 `get_github_token`。

---

#### ⚠️ 11. `src/im/vod_handler_async.py` — L271-275

**问题**: VOD handler 的 token 获取硬编码 `get_gitcode_token`，GitHub 仓库的 VOD agent 会注入 GitCode token。

**代码片段**:
```python
# L271-275
repo_token = agent_session.get("repo_gitcode_token", "") or ""
if not repo_token:
    from src.user.db import get_gitcode_token
    repo_token = get_gitcode_token(repo_key=repo_key) or ""
```

**说明**: 先从 session 字段读 `repo_gitcode_token`（DB 字段名，安全），为空时 fallback 到 `get_gitcode_token`。GitHub 仓库的 `repo_gitcode_token` 为空，会 fallback 到 GitCode 全局 token。

**修复建议**: fallback 按 `_is_github_repo(repo_key)` 分流，GitHub 仓库用 `get_github_token`。

---

#### ⚠️ 12. `src/api/routes/repos.py` — L399, L412, L574, L655, L673, L757

**问题**: `host_repo` / `create_repo_from_template` / `repair_host` 的 GitCode 分支中，`add_managed_repo` 调用时 `bot_gitcode_token` 参数硬编码从 `global-bot-token.json` 的 `gitcode_api_token` 字段读取。

**代码片段**:
```python
# L399 (create_repo_from_template GitCode 分支)
_create_global_bot_token = _json.load(_f2).get("gitcode_api_token", "") or ""
user_db.add_managed_repo(..., bot_gitcode_token=_create_global_bot_token)

# L574 (host_repo GitCode 分支)
global_bot_token = _gbt.get("gitcode_api_token", "") or ""
```

**说明**: `host_repo` 和 `create_repo_from_template` 已有 `if provider == "github"` 分流，GitHub 分支读 `github_bot_token`。这些 `gitcode_api_token` 出现在 GitCode 专属分支中。**但 `create_repo_from_template` 的 L412 `bot_gitcode_token` 参数名在 GitHub 分支也传了同样的值**——GitHub 仓库托管时 `bot_gitcode_token` 字段写入了 GitCode token。

**修正**: 经复查，L412 在 `if provider == "github": ... else: ...` 的 else 分支（GitCode 专属），GitHub 分支（L388-410）使用 `github_webhook_token` 且不传 `bot_gitcode_token`。`repair_host` 是 GitCode 专用修复路径。**实际安全**。

重新分类：`host_repo`/`repair_host`/`create_repo_from_template` 的这些命中点安全。仅保留 `list_managed_repos`（L775）的 `bot_gitcode_token` 字段名（安全）。

---

#### ⚠️ 13. `src/api/routes/tasks.py` — L999

**问题**: `create_standard_task` 硬编码 `_post_gitcode_issue`，onboarding 调用时 GitHub 仓库也会走 GitCode Issue 创建路径。

**代码片段**:
```python
# L999
resp = _post_gitcode_issue(bot_token, owner, rname, title.strip(), ...)
```

**说明**: `create_standard_task` 被 `onboarding.py` 调用，接收的 `bot_token` 来自 `get_gitcode_token`（见 ❌3）。函数内部没有 provider 分流，直接调 `_post_gitcode_issue`。

**修复建议**: 在 `create_standard_task` 内按 `repo_key` provider 分流，GitHub 仓库调 `_post_github_issue`（已存在于 tasks.py）。

---

#### ⚠️ 14. `src/api/routes/repos.py` — L1047

**问题**: `create_repo_from_template` 的 GitCode 分支硬编码从 `global-bot-token.json` 读 `gitcode_api_token`，作为 `bot_gitcode_token` 写入 DB。

**代码片段**:
```python
# L1047
api_token = _json.load(_f).get("gitcode_api_token", "") or ""
```

**说明**: 该处位于 `else: # GitCode` 分支，是 GitCode 专属逻辑。**安全**。

重新分类：安全。

---

### 低（4 个）— 文本 / URL / 基础设施硬编码

---

#### ℹ️ 15. `src/api/main.py` — L783

**问题**: 模板同步脚本硬编码 `gitcode.com/grape-dev/grape-template.git` URL。

**代码片段**:
```python
env={**_os.environ, "TEMPLATES_REPO": "https://gitcode.com/grape-dev/grape-template.git"}
```

**说明**: 这是 Grape 自身的模板仓库，托管在 GitCode 上，不随用户仓库 provider 变化。**安全**（基础设施 URL）。

重新分类：安全。

---

#### ℹ️ 16. `src/storage/scene_sync.py` — L54, L62, L194, L209

**问题**: 场景同步仓库 URL 和下载 URL 硬编码 `gitcode.com`。

**代码片段**:
```python
# L54
repo_url = "https://gitcode.com/grape-dev/grape-skills"
# L62
_OWNER_REPO = _REPO_URL.replace("https://gitcode.com/", "").rstrip("/")
# L194
token = json.load(f).get("gitcode_api_token", "")
# L209
url = f"https://raw.gitcode.com/{_OWNER_REPO}/archive/refs/heads/{_REPO_BRANCH}.zip"
```

**说明**: 这是 Grape 官方场景仓库，托管在 GitCode 上，不随用户仓库 provider 变化。`gitcode_api_token` 是 `global-bot-token.json` 中的字段名。**安全**（基础设施 URL + DB/配置字段名）。

重新分类：安全。

---

#### ℹ️ 17. `src/worker_package.py` — L19, L47, L64

**问题**: grape-cli 仓库 URL 和 git insteadOf 硬编码 `gitcode.com`。

**代码片段**:
```python
# L19
GRAPE_CLI_REPO = os.environ.get("GRAPE_CLI_REPO", "https://gitcode.com/grape-dev/grape-cli.git")
# L64
["git", "-c", "url.https://***@gitcode.com/.insteadOf=https://gitcode.com/"] + args
```

**说明**: grape-cli 是 Grape 自身的 CLI 工具仓库，托管在 GitCode 上。`insteadOf` 是为访问 GitCode 私有仓库注入凭据。不随用户仓库 provider 变化。**安全**（基础设施 URL）。

重新分类：安全。

---

#### ℹ️ 18. `src/runtime/cleanup.py` — L562-563

**问题**: workspace 清理时硬编码 `gitcode:` 前缀构造 repo_key。

**代码片段**:
```python
# L562-563
repo_key = f"gitcode:{owner}/{repo_name}"
```

**说明**: 清理 workspace 目录时构造 repo_key 用于查 `active_repos` 集合。如果 `active_repos` 中的 GitHub 仓库用 `github:` 前缀，此处构造的 `gitcode:` 前缀 key 无法匹配，导致 GitHub 仓库的 workspace 目录在清理时被遗漏（不会被误删，只是不清理）。

**修复建议**: 改用 `to_repo_key(owner/repo)` 后同时检查 `github:` 前缀，或直接用 owner/repo 做目录遍历不依赖 repo_key。

---

## 三、修正后的最终分类

经逐一深入分析代码上下文，部分初步标记为"需修复"的命中点实际位于 provider 分流的 GitCode 专属分支内，重新分类如下：

### 最终需修复文件（10 个）

| 严重程度 | 文件 | 行号 | 问题摘要 |
|----------|------|------|----------|
| **严重** | `src/api/routes/scenes.py` | L819-829 | `list_repo_branches` 无 GitHub API 分流 |
| **严重** | `src/project_info/builder.py` | L301-305, L387-393 | 硬编码 GitCode API URL + token，无 provider 分流 |
| **严重** | `src/im/feishu_async.py` | L725 | `sync_member_mapping` token 获取无 provider 分流（GitHub 仓库传 GitCode token） |
| **严重** | `src/api/onboarding.py` | L265-271, L324-326 | `get_gitcode_token` 无 provider 分流 |
| **严重** | `src/api/routes/events.py` | L1110-1111, L1449-1470 | `get_gitcode_token` 无 provider 分流 |
| **严重** | `src/api/event_pipeline.py` | L1998-1999 | worktree 清理 token 无 provider 分流 |
| **严重** | `src/api/routes/tasks.py` | L999 | `create_standard_task` 硬编码 `_post_gitcode_issue` 无 provider 分流 |
| **中等** | `src/api/routes/chat.py` | L212-215, L235-239, L328-356 | `_resolve_gitcode_token` 及补拉评论 token 无 provider 分流 |
| **中等** | `src/api/event_pipeline.py` | L1110-1111 | `_handle_conversation_message` token 无 provider 分流 |
| **中等** | `src/im/vod_handler_async.py` | L271-275 | VOD handler token fallback 无 provider 分流 |
| **低** | `src/runtime/cleanup.py` | L562-563 | workspace 清理硬编码 `gitcode:` 前缀 |
| **低** | `src/api/routes/templates.py` | L49 | `_load_global_bot_config` 硬编码 `gitcode_api_token`（GitHub 仓库创建模板时 bot token 不匹配） |

### 最终安全文件（41 个）

所有其他文件经复查均为安全（已有 provider 分流、DB 字段名、注释、基础设施 URL、import 语句、OAuth 专用路由等）。

---

## 四、修复优先级建议

### P0 — 立即修复（影响 GitHub 仓库核心功能）

1. **`src/api/routes/scenes.py` L819-829** — GitHub 仓库无法查询分支列表
2. **`src/project_info/builder.py` L301-305, L387-393** — GitHub 仓库项目信息构建失败
3. **`src/api/onboarding.py` L265-271, L324-326** — GitHub 仓库 onboarding 任务创建失败
4. **`src/api/routes/events.py` L1110-1111, L1449-1470** — GitHub 仓库事件触发 agent 失败
5. **`src/api/event_pipeline.py` L1998-1999** — GitHub 仓库 worktree 清理 token 错误

### P1 — 尽快修复（影响 Chat Agent / VOD Agent）

6. **`src/api/routes/chat.py` L328-356** — GitHub 仓库 Chat Agent token 注入错误
7. **`src/api/event_pipeline.py` L1110-1111** — GitHub 仓库对话消息 token 错误
8. **`src/im/vod_handler_async.py` L271-275** — GitHub 仓库 VOD agent token 错误
9. **`src/im/feishu_async.py` L725** — GitHub 仓库飞书用户映射同步 token 错误
10. **`src/api/routes/tasks.py` L999** — onboarding `create_standard_task` 对 GitHub 仓库调 GitCode API

### P2 — 后续修复（边缘场景）

11. **`src/api/routes/templates.py` L49** — GitHub 仓库创建模板时 bot token 来源
12. **`src/runtime/cleanup.py` L562-563** — GitHub 仓库 workspace 清理遗漏

---

## 五、通用修复模式

所有需修复文件均可采用统一模式：

```python
from src.repo_key import _is_github_repo

if _is_github_repo(repo_key):
    from src.user.db import get_github_token
    token = get_github_token(repo_key=repo_key) or ""
    # GitHub API URL: https://api.github.com/repos/...
else:
    from src.user.db import get_gitcode_token
    token = get_gitcode_token(repo_key=repo_key) or ""
    # GitCode API URL: https://gitcode.com/api/v5/repos/...
```

或复用已有的 `_get_repo_api_token(repo_key)` 函数（`event_pipeline.py` L1380 已定义）。

---

*报告结束*
