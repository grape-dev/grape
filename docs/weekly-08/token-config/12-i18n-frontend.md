# 前端 i18n + 日志脱敏 + 解绑清理 + PAT 提示

> 源文档：`repo-token-config-design.md` §九 其他需求（R19.1–R19.4），提取自 issue-372 token 配置设计。
> R19 属于「其他需求」章节，覆盖前端国际化、日志安全、解绑/删除清理、PAT 过期提醒四个维度。

## 需求项

### R19.1 日志脱敏（含 webhook_secret 扩展）

**基础三条**（R19.1 原文）：

- R19.1.1：健康检查日志不包含 token 原文或片段
- R19.1.2：`fail_task` 日志不包含 token 原文或片段
- R19.1.3：`error_code` 中不包含 token 片段

**webhook_secret 扩展**（R21.10.2 补充，归入 R19.1 脱敏范围）：

- R21.10.2：`webhook_secret` 脱敏——R19.1 日志脱敏范围扩展到 `webhook_secret` 值。webhook 验证日志（`webhook.py`）不输出 `webhook_secret` 原文或片段
- R21.10.3：A2A 比对日志脱敏——`webhook.py` A2A 鉴权日志不输出 token 原文（当前日志只输出 `bot=True/False`，已安全）

### R19.2 前端 i18n error_code 映射

- R19.2.1：zh/en `app.json` 新增 `error_code` 映射 key：
  - `TOKEN_EXPIRED` — 用户 token 过期
  - `GLOBAL_TOKEN_EXPIRED` — 全局 bot token 过期
  - `REPO_TOKEN_EXPIRED` — 仓库级 bot token 过期
  - `NO_TOKEN_AVAILABLE` — 无可用 token
- R19.2.2：zh/en `app.json` 新增「PAT 无自动续期」提示 key
- R19.2.3：settings 页 token 状态提示 zh/en 双语

### R19.3 解绑/删除仓库时的清理

- R19.3.1：用户解绑时检查是否有路径 B 仓库依赖该用户 token
- R19.3.2：有依赖时返回仓库列表 + 警告提示
- R19.3.3：用户确认解绑后，路径 B 仓库的 `host_user_id` 仍保留（指向已删除的 user），`get_repo_token_with_refresh` 第 4 级查不到用户 token → 返回空 → task fail（`NO_TOKEN_AVAILABLE`）。仓库保持不可用状态，直到管理员配置全局 bot token 后 `auto_upgrade_path_b_repos` 将其切换到路径 A
- R19.3.4：删除仓库时清理 `bot_token_status` 过期标记 + 清理 `devops_agent_session` 中的 `repo_gitcode_token` + 清理 `devops_agent_session.token_status`

### R19.4 PAT 过期提醒（含创建页链接）

- R19.4.1：GitHub token 过期提示中包含 GitHub PAT 创建页链接（通过 `provider.pat_create_url()` 获取，`GitHubProvider` 返回 `https://github.com/settings/tokens`）
- R19.4.2：GitCode token 过期提示中包含 GitCode PAT 创建页链接（通过 `provider.pat_create_url()` 获取，`GitCodeProvider` 返回 `https://gitcode.com/profile/personal_access_tokens`）

## 设计方案

### 日志脱敏（R19.1 + R21.10.2 扩展）

| 日志来源 | 脱敏要求 | 当前状态 |
|----------|----------|----------|
| 健康检查日志 | 不含 token 原文/片段 | 需新增检查 |
| `fail_task` 日志 | 不含 token 原文/片段 | 需新增检查 |
| `error_code` | 不含 token 片段 | error_code 为枚举值（`TOKEN_EXPIRED` 等），天然不含 token |
| webhook 验证日志（`webhook.py`） | 不含 `webhook_secret` 原文/片段 | 需新增检查 |
| A2A 鉴权日志（`webhook.py`） | 不含 token 原文 | 当前只输出 `bot=True/False`，**已安全** |

**脱敏原则**：
- 日志记录 token 相关信息时只输出「状态/级别/error_code」，不输出 token 值
- `webhook_secret` 与 token 同等对待——验证日志只输出「验证通过/失败」，不输出 secret 值
- `error_code` 为枚举字符串（`TOKEN_EXPIRED / GLOBAL_TOKEN_EXPIRED / REPO_TOKEN_EXPIRED / NO_TOKEN_AVAILABLE`），天然不含 token 片段

### 前端 i18n（R19.2）

**error_code → i18n key 映射**：

| error_code | zh 文案 | en 文案 |
|------------|---------|---------|
| `TOKEN_EXPIRED` | 你的 Token 已过期，请在设置页重新绑定 | Your token has expired, please rebind in settings |
| `GLOBAL_TOKEN_EXPIRED` | 全局 Bot Token 已过期，请联系管理员重新配置 | Global bot token expired, please contact admin to reconfigure |
| `REPO_TOKEN_EXPIRED` | 仓库 Bot Token 已过期，请联系有权限的成员重新配置 | Repo bot token expired, please contact authorized member to reconfigure |
| `NO_TOKEN_AVAILABLE` | 无可用 Token，请检查 Token 配置 | No token available, please check token configuration |

**PAT 无自动续期提示 key**：
- zh：「此 Token 为 PAT 类型，不会自动续期，过期后需手动重新配置」
- en：「This token is a PAT type, will not auto-renew, manual reconfiguration required after expiration」

**settings 页 token 状态双语**：
- 全局 bot token 状态：healthy / degraded / unhealthy / expired
- 仓库级 bot token 状态：正常 / 已过期
- 用户 token 状态：正常 / 已过期（account 页，已有）

### 解绑/删除清理（R19.3）

**用户解绑流程**（R19.3.1–R19.3.3）：

```
用户发起解绑
  ↓
检查路径 B 仓库依赖（managed_repos.host_user_id == 该用户）
  ↓
有依赖 → 返回仓库列表 + 警告「解绑后以下仓库将无法执行任务」
  ↓
用户确认解绑
  ↓
删除 external_identities 记录
  ↓
路径 B 仓库 host_user_id 保留（指向已删除的 user）
  ↓
get_repo_token_with_refresh 第 4 级查不到用户 token → 返回空
  ↓
task fail（NO_TOKEN_AVAILABLE）
  ↓
仓库保持不可用，直到管理员配置全局 bot token
  ↓
auto_upgrade_path_b_repos 将其切换到路径 A
```

**删除仓库清理**（R19.3.4）：

| 清理项 | 表 | 字段 |
|--------|-----|------|
| bot token 过期标记 | `managed_repos` | `bot_token_status` |
| session GitCode token | `devops_agent_session` | `repo_gitcode_token` |
| session GitHub token | `devops_agent_session` | `repo_github_token` |
| session token 状态 | `devops_agent_session` | `token_status` |

### PAT 过期提醒（R19.4）

**provider.pat_create_url() 接口**：

```python
# VCSProvider 抽象接口
@abstractmethod
def pat_create_url(self) -> str:
    """PAT 创建页链接。"""

# GitCodeProvider
def pat_create_url(self) -> str:
    return "https://gitcode.com/profile/personal_access_tokens"

# GitHubProvider
def pat_create_url(self) -> str:
    return "https://github.com/settings/tokens"
```

**过期提示文案**（i18n）：
- GitHub：「你的 GitHub Token 已过期，请前往 [GitHub Token 设置页](https://github.com/settings/tokens) 重新创建并重新绑定」
- GitCode：「你的 GitCode Token 已过期，请前往 [GitCode Token 设置页](https://gitcode.com/profile/personal_access_tokens) 重新创建并重新绑定」

**提示展示位置**：
- account 页（用户 token 过期）
- settings 页（全局/仓库级 bot token 过期，管理员可见）

## 涉及代码文件

| 文件 | 改动点 | 关联需求 |
|------|--------|----------|
| `src/webhook.py` | webhook 验证日志 `webhook_secret` 脱敏 + A2A 比对日志确认 | R19.1, R21.10.2 |
| `src/health_check.py`（或健康检查模块） | 健康检查日志不输出 token 原文/片段 | R19.1.1 |
| `src/pipeline.py`（`fail_task` 处理） | `fail_task` 日志不输出 token 原文/片段 | R19.1.2 |
| `src/api/account.py`（或用户解绑接口） | 解绑前检查路径 B 依赖 + 返回仓库列表 + 警告 | R19.3.1, R19.3.2 |
| `src/api/repos.py`（删除仓库接口） | 删除仓库时清理 `bot_token_status` + `devops_agent_session` token 字段 | R19.3.4 |
| `src/db.py` | 解绑后路径 B 仓库 `host_user_id` 保留逻辑；删除仓库清理 SQL | R19.3.3, R19.3.4 |
| `src/vcs/providers.py`（`GitCodeProvider / GitHubProvider`） | `pat_create_url()` 方法实现 | R19.4.1, R19.4.2 |
| 前端 `app.json`（zh） | 新增 error_code 映射 key + PAT 无续期提示 key + token 状态双语 | R19.2.1, R19.2.2, R19.2.3 |
| 前端 `app.json`（en） | 同上英文 | R19.2.1, R19.2.2, R19.2.3 |
| 前端 settings 页组件 | token 状态展示 + PAT 提示 + error_code i18n 映射 | R19.2.3, R19.4 |
| 前端 account 页组件 | PAT 过期提示 + 创建页链接 | R19.4 |

## 验收标准

### R19.1 日志脱敏
- [ ] 健康检查日志：失败时记录 `"global bot token expired for repo gitcode:owner/repo"`，不含 token 原文/片段
- [ ] `fail_task` 日志：记录 error_code + 仓库信息，不含 token 原文/片段
- [ ] `error_code`：为枚举字符串（`TOKEN_EXPIRED` 等），不含 token 片段
- [ ] `webhook.py` 验证日志：不输出 `webhook_secret` 原文或片段（R21.10.2 扩展）
- [ ] `webhook.py` A2A 鉴权日志：只输出 `bot=True/False`，不含 token 原文（已安全，确认）

### R19.2 前端 i18n
- [ ] zh `app.json` 新增 `TOKEN_EXPIRED / GLOBAL_TOKEN_EXPIRED / REPO_TOKEN_EXPIRED / NO_TOKEN_AVAILABLE` 映射 key
- [ ] en `app.json` 新增同上 key
- [ ] zh/en `app.json` 新增「PAT 无自动续期」提示 key
- [ ] settings 页 token 状态提示 zh/en 双语展示
- [ ] 前端收到 error_code → 正确映射到 i18n 文案展示

### R19.3 解绑/删除清理
- [ ] 用户解绑前：检查路径 B 仓库依赖 → 返回仓库列表 + 警告提示
- [ ] 用户确认解绑：`external_identities` 删除 → 路径 B 仓库 `host_user_id` 保留 → task fail（`NO_TOKEN_AVAILABLE`）
- [ ] 解绑后路径 B 仓库不可用 → 管理员配置全局 bot token → `auto_upgrade_path_b_repos` 切换到路径 A → 恢复
- [ ] 删除仓库：清理 `managed_repos.bot_token_status` + `devops_agent_session.repo_gitcode_token` + `devops_agent_session.repo_github_token` + `devops_agent_session.token_status`
- [ ] 删除仓库不影响其他仓库

### R19.4 PAT 过期提醒
- [ ] GitHub token 过期提示包含 `https://github.com/settings/tokens` 链接
- [ ] GitCode token 过期提示包含 `https://gitcode.com/profile/personal_access_tokens` 链接
- [ ] `provider.pat_create_url()` 接口在 `GitCodeProvider` 和 `GitHubProvider` 中正确实现
- [ ] account 页 token 过期提示展示 PAT 创建页链接
- [ ] settings 页 bot token 过期提示展示 PAT 创建页链接（管理员可见）

## 依赖场景

| 依赖项 | 关系 | 说明 |
|--------|------|------|
| R16.1.5 `error_code` 写入 | R19.2 前置 | `fail_task` 写入 `TOKEN_EXPIRED / GLOBAL_TOKEN_EXPIRED / REPO_TOKEN_EXPIRED`，前端 i18n 映射这些 code |
| R16.2 通知机制 | R19.2 依赖 | dashboard / settings 页 token 状态展示复用通知机制 |
| R17.1 PAT 无续期提示 | R19.2.2 对应 | settings 页配置 token 时提示「PAT 无自动续期」 |
| R17.3 健康检查通知 | R19.1.1 关联 | 健康检查日志脱敏 + 通知展示 |
| R20.12 webhook 验证 | R19.1 扩展关联 | `webhook_secret` 脱敏涉及 webhook 验证日志 |
| R20.13 A2A 鉴权 | R21.10.3 关联 | A2A 比对日志确认不输出 token 原文 |
| R15 `auto_upgrade` | R19.3.3 依赖 | 解绑后路径 B 仓库恢复依赖 `auto_upgrade_path_b_repos` |
| R18.5 `VCSProvider` 接口 | R19.4 依赖 | `pat_create_url()` 是 `VCSProvider` 抽象接口方法 |
| R21.10.2 `webhook_secret` 脱敏 | R19.1 扩展 | R21.10.2 将 `webhook_secret` 纳入 R19.1 脱敏范围 |
| R21.8 鉴权规则 | R19.3.2 关联 | 解绑警告提示的仓库列表依赖 `host_user_id` 关系 |
