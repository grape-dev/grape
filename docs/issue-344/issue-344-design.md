# Issue #344 设计文档：Token 缓存安全 + 幂等查重 + 测试修复

> Issue #344 | 分支 feat/issue-344-token-cache-and-idempotency
> 基线：grape-dev/weekly-07-clean（891244bf5）
> 状态：待评审

## 1. 问题概述

Issue #344 包含 7 个子问题，分 3 个主题：

| # | 子问题 | 严重度 | 主题 |
|---|--------|--------|------|
| P1 | chat会话明文缓存 OAuth token | 高（安全） | Token 安全 |
| P2 | 幂等检查分页上限 500 条 | 中（功能缺陷） | 幂等查重 |
| P3 | 幂等查重失败静默放行（create_standard_task） | 中（健壮性） | 幂等查重 |
| P4 | GitHub 读路径没接预刷新 | 低（长期风险） | Token 安全 |
| P5 | agent token 被拿去建单（权限边界） | 低（已部分修复） | Token 安全 |
| P6 | 测试代码全面使用 deprecated user_accounts 函数 | 高（441 失败根因） | 测试修复 |
| P7 | test_agent_internal_auth fixture 列名漂移 | 中（测试失败） | 测试修复 |

## 2. 子问题分析与方案

### P1: chat会话明文缓存 OAuth token

**现状**：
- `chat.py` 4 处（558, 723, 1462, 1826）调用 `get_user_token_with_refresh` 获取用户 token，然后 `update_conversation(conv_id, user_gitcode_token=user_token)` 明文写入 `chat_conversations.user_gitcode_token`
- `im/handler_async.py` 也写入 `user_gitcode_token`
- `agent.py:1821` 执行时从 `task.get("user_gitcode_token")` 读取（enqueue_task 时注入）
- `agent.py:1824` 为空时回退 `get_user_gitcode_token(user_id)` 从 external_identities 实时读

**问题**：
1. 明文 token 躺在 DB 里，external_identities 那边是加密的，不一致
2. 缓存 token 2 小时后失效，agent 拿死 token 调 GitCode 失败

**方案**：agent 执行时实时从 external_identities 读 token（带预刷新），不依赖会话缓存

**具体改动**：

1. **`chat.py`** — 4 处 send/stream/async 函数
   - 删除 `get_user_token_with_refresh` 调用和 `update_conversation(conv_id, user_gitcode_token=user_token)` 回写
   - 改为在 enqueue_task 时传 `user_gitcode_token=""`（空字符串，不缓存）
   - `_resolve_gitcode_token` 函数保留兜底逻辑（repo bot token），但不再优先用缓存的 user_token

2. **`chat.py:859`** — `enqueue_task(..., user_gitcode_token=gitcode_token)`
   - 改为 `user_gitcode_token=""`，让 agent 端实时获取

3. **`agent.py:1817-1825`** — 已有回退逻辑
   - `task.get("user_gitcode_token")` 为空时，调用 `get_user_gitcode_token(user_id)` 从 external_identities 读
   - **增强**：改为调用 `get_user_token_with_refresh(username, user_id)` 带预刷新
   - 需要从 task 中获取 username（task payload 有 `account` 字段）

4. **`im/handler_async.py`** — IM 路径
   - 同样不缓存，`create_conversation` 时 `user_gitcode_token=""` 
   - agent 端实时获取

5. **`event_pipeline.py`** — 已传 `user_gitcode_token=""`，不改

**不改的部分**：
- `chat_conversations.user_gitcode_token` 字段保留（向后兼容，存量数据迁移后清空）
- `agent_tasks.user_gitcode_token` 字段保留（enqueue_task 参数保留，只是不传值）

**数据清理**：
- 升级脚本：`UPDATE chat_conversations SET user_gitcode_token = '' WHERE user_gitcode_token != ''`
- 升级脚本：`UPDATE agent_tasks SET user_gitcode_token = '' WHERE user_gitcode_token != ''`

### P2: 幂等检查分页上限 500 条

**现状**：
- `_find_open_issue_by_title` 分页循环最多 10 页 × 50 条 = 500 条
- 超过 500 条 open issue 的仓库会漏检

**API 能力调查**：
- GitCode `GET /repos/{owner}/{repo}/issues?state=open&q=xxx` — **q 参数不生效**（实测返回所有 open issues，不做 title 过滤）
- GitCode `GET /search/issues?q=xxx` — 全局搜索，不限定仓库，且结果不精确（搜索"bot token 单点故障"返回不相关结果）
- GitHub `GET /search/issues?q=xxx+in:title+repo:owner/repo+state:open` — 支持精确搜索

**方案**：
- **GitCode**：保持分页循环但取消 10 页上限（改为 while True，直到某页返回不足 50 条才终止）
- **GitHub**：改用 Search API `GET /search/issues?q={title}+in:title+repo:{owner}/{repo}+state:open`，单次请求精确匹配

**具体改动**：
- `src/api/issues_center.py:_find_open_issue_by_title`
  - GitHub 分支：改用 search API
  - GitCode 分支：`while page <= 10` → `while True`（靠 `len(items) < 50` 终止）

### P3: 幂等查重失败静默放行（create_standard_task）

**现状**：
- `create_task_via_issue`（用户手动建单）：已有 try/except RuntimeError → 502（#343 修复）
- `create_standard_task`（onboarding 自动建单）：**没有 try/except**，token 失效时 RuntimeError 直接冒泡到 onboarding 流程

**方案**：
- `create_standard_task` 包裹 try/except RuntimeError，返回 `{"ok": False, "code": 502, "message": "查重失败，Bot Token 可能失效: ..."}`

### P4: GitHub 读路径没接预刷新

**现状**：
- `with_oauth_token` 的 GitHub 分支：**已在 #343 修复**，改为 `get_user_github_token_with_refresh`
- `deps.py:get_user_github_token(username)`（无刷新版）：仍被 repos.py、templates.py、settings.py 等 10+ 处裸用

**方案**：
- 所有裸用 `get_user_github_token(username)` 的读路径改为 `get_user_github_token_with_refresh(username, user_id)`
- 涉及文件：`repos.py`（10 处）、`templates.py`（1 处）、`settings.py`（1 处）、`vcs/providers/github.py`（1 处）

**注意**：部分调用方没有 user_id（如 `repos.py:169` 的循环中只有 username），这些保持原样——GitHub token 寿命 1 年，无预刷新短期无碍。

### P5: agent token 被拿去建单（权限边界）

**现状**：
- `create_task_via_issue`：**已在 #343 修复**，用 `get_repo_bot_token(repo_key)` 跳过 agent session
- `create_standard_task`：用调用方传入的 `bot_token`（onboarding.py 从 `Repo(repo_key).bot_token` 获取，不走 `get_gitcode_token`）

**结论**：已修复，无需改动

### P6: 测试代码全面使用 deprecated user_accounts 函数

**现状**：
- 38 个测试文件调用 `user_db.create_user()` / `get_user_by_username()` / `get_token_version()` 等 deprecated 函数
- `create_user` 写 `user_accounts` 表，`get_current_user` 查 `grape_accounts` 表 → 401

**API 签名差异**：

| deprecated 函数 | 签名 | 替换函数 | 签名 |
|----------------|------|---------|------|
| `create_user(username, password, created_by=0)` | 返回 `{"id", "username"}` | `create_grape_account(username, email, password, ...)` | 返回 `{"id", "username", "email"}` |
| `get_user_by_username(username)` | 返回 `{"id", "username", "password_hash", ...}` | `get_grape_account_by_username(username)` | 返回 `{"id", "username", "email", ...}` |
| `get_user(user_id)` | 返回 `{"id", "username", ...}` | `get_grape_account_by_id(user_id)` | 返回 `{"id", "username", "email", ...}` |
| `from src.user.db import get_token_version` | — | `from src.auth.grape_account_db import get_token_version` | — |

**关键差异**：
- `create_user` 自动创建个人组织，`create_grape_account` 不创建
- `create_grape_account` 必须传 `email` 参数（可传 None）
- `create_user` 的 `created_by` 参数在 `create_grape_account` 中无对应

**方案**：

1. **批量替换**（脚本自动 + 人工验证）：
   - `user_db.create_user(username, password)` → `grape_account_db.create_grape_account(username, email=None, password=password)`
   - `user_db.get_user_by_username(username)` → `grape_account_db.get_grape_account_by_username(username)`
   - `user_db.get_user(user_id)` → `grape_account_db.get_grape_account_by_id(user_id)`
   - `from src.user.db import get_token_version` → `from src.auth.grape_account_db import get_token_version`

2. **特殊文件处理**：
   - `test_user_db.py`：直接测 deprecated 函数行为（如 `test_create_and_get`、`test_create_duplicate_returns_none`），**不改**——这些测试验证 deprecated 函数本身的行为
   - `test_engine_select.py`：直接操作 `user_accounts` 表测 issue_creator 回退逻辑，**不改**
   - `test_im_async_remediation.py`：注释说"预置 user_accounts 用户"，但 `set_feishu_open_id` 实际需要 user_id 存在于 grape_accounts → **改**

3. **个人组织创建**：
   - `create_grape_account` 不自动创建个人组织
   - 检查测试是否依赖个人组织（grep 结果显示仅 `test_issue_338_worker_visibility.py` 直接插入 organization 表，不依赖 create_user 的 org 创建）
   - 如有依赖，在 `create_grape_account` 中补个人组织创建逻辑，或在测试中手动创建

4. **清理 fixture 中的 `token LIKE`** → 见 P7

### P7: test_agent_internal_auth fixture 列名漂移

**现状**：
- `api_tokens` 表列名是 `token_sha256`，不是 `token`
- 5 个测试文件的 fixture 中 `DELETE FROM api_tokens WHERE token LIKE 'test_%'...` 会报错（列不存在）

**涉及文件**（5 个）：
- `tests/test_agent_internal_auth.py:23`
- `tests/test_task_complete.py:37,46`
- `tests/test_api_key_lifecycle.py:28`
- `tests/test_openapi.py:409`

**方案**：
- `token LIKE` → `token_sha256 LIKE`
- 同时将 `user_id IN (SELECT id FROM user_accounts WHERE ...)` 改为 `user_id IN (SELECT id FROM grape_accounts WHERE ...)`

## 3. 改动文件清单

### 生产代码

| 文件 | 改动点 | 子问题 |
|------|--------|--------|
| `src/api/routes/chat.py` | 4 处删除 token 缓存回写 + enqueue_task 传空 token + _resolve_gitcode_token 简化 | P1 |
| `src/runtime/agent.py` | token 获取改用 get_user_token_with_refresh（带预刷新） | P1 |
| `src/im/handler_async.py` | 不缓存 user_gitcode_token | P1 |
| `src/api/issues_center.py` | _find_open_issue_by_title 取消 10 页上限 + create_standard_task 加 try/except | P2, P3 |
| `src/api/routes/repos.py` | get_user_github_token → get_user_github_token_with_refresh | P4 |
| `src/api/routes/templates.py` | 同上 | P4 |
| `src/api/routes/settings.py` | 同上 | P4 |
| `src/vcs/providers/github.py` | 同上 | P4 |

### 测试代码

| 文件 | 改动点 | 子问题 |
|------|--------|--------|
| 38 个测试文件 | create_user → create_grape_account 等批量替换 | P6 |
| 5 个测试文件 fixture | token LIKE → token_sha256 LIKE + user_accounts → grape_accounts | P7 |

### 升级脚本

| 文件 | 改动点 | 子问题 |
|------|--------|--------|
| `src/api/db_setup.py` | 清空 user_gitcode_token 存量数据 | P1 |

## 4. 风险分析

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| agent.py 实时获取 token 增加延迟 | 每次 chat 消息多一次 DB 查询 | external_identities 查询 < 1ms，可忽略 |
| create_grape_account 不创建个人组织 | 测试可能依赖 org | 先跑测试验证，如有依赖再补 |
| GitCode 分页取消上限可能慢 | open issue > 500 的仓库 | 实际很少 > 100，分页自动终止 |
| 批量替换测试代码可能遗漏 | 部分测试仍 401 | 替换后跑全量 pytest 验证 |

## 5. 验证标准

1. `python3 -m pytest tests/ -q --tb=no` — 441 failed 降到接近 0（排除已知基线问题）
2. test_agent_internal_auth / test_task_complete / test_api_key_lifecycle / test_openapi 不再报 `column "token" does not exist`
3. chat 流程 E2E：发消息 → agent 执行 → 不依赖缓存 token
4. 幂等查重：_find_open_issue_by_title 超过 500 条 open issue 仍能查到

## 6. 实施顺序

1. P7（fixture 列名修复）— 最简单，立即减少测试失败
2. P6（测试 deprecated 函数替换）— 最大批量，减少 441 failed
3. P1（chat token 缓存移除）— 核心安全问题
4. P2 + P3（幂等查重）— 功能修复
5. P4（GitHub 预刷新）— 长期风险
6. 全量验证
