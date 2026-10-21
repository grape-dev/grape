# Webhook 验证 + A2A 鉴权 + 建单用户 Token

> 来源：`repo-token-config-design.md` R20.12–R20.14（634–712）、R20.16–R20.20（713–798）、R20.21–R20.23（799–840）。
> 文档拆分 issue-372。

## 需求项

### R20.12 webhook 验证 token 获取（P0 补充，不属于 token 体系）

- webhook 收到 VCS 推送时需验证 `X-Bot-Token` header，验证用的 token 是"webhook 创建时用的 password"——路径 A 是 bot token，路径 B 是随机密钥（`webhook_secret`）。TokenResolver 的策略链不包含 `webhook_secret`，不能用于 webhook 验证。
- 当前代码 `webhook.py` 用 `get_repo_bot_token_no_global` 验证 → 查 `managed_repos.bot_gitcode_token` + `devops_agent_session` + 用户 token（跳过全局），不查 `webhook_secret`。
- **R20.12.1**：webhook 验证不走 TokenResolver，走独立的 `get_webhook_verify_token()`。
- **R20.12.2**：验证优先级 `webhook_secret → webhook_token_field → get_repo_bot_token_no_global`（兼容旧数据）。
- **R20.12.3**：短路机制不影响 webhook 验证——即使 bot token 过期短路，webhook 验证仍用 `webhook_secret` / `webhook_token` 验证。
- **R20.12.4**：路径 B 仓库 webhook 验证用 `webhook_secret`（随机密钥），与 token 过期状态无关。
- **R20.12.5 不合并原因**：验证 token（`get_webhook_verify_token`）与鉴权 token（`read_stored_token`）本质都是"原始值比对"，但不合并为同一个方法。原因：数据源不同——验证 token 查 `webhook_secret` / `webhook_token_field`（webhook 创建时的 password，不在 token 策略链中），鉴权 token 查策略链（`bot_gitcode_token` / `devops_agent_session` / 用户 token）。合并需要传 `source` 参数区分数据源，又是参数地狱。两个方法各自语义清晰，不合并。

### R20.13 A2A 鉴权 token 保留独立逻辑（P0 补充）

- A2A 鉴权（`webhook.py` / `events.py`）用 `get_repo_bot_token_no_global` 比对请求的 `Authorization` header。token 过期短路返回空串会导致鉴权失败 → agent 之间无法通信。
- 当前代码：`webhook.py` line 302-318 用 `get_repo_bot_token_no_global` 做 bot token 比对；`events.py` line 969, 1281 同理。还有关联仓库信任域比对（两仓 bot token 是否相同）。
- **R20.13.1**：`get_repo_bot_token_no_global` 保留为薄包装，内部调 `TokenResolver.read_stored_token()`（不是 `get_token()`）。
- **R20.13.2**：`read_stored_token()` 是独立方法——只调策略链的 `_fetch()`（纯读存储值），不调 `_is_expired()`（不检查过期），不触发 OAuth refresh。与 `get_token()` 共享底层策略链但走不同检查路径。
- **R20.13.3**：A2A 鉴权即使 token 过期也返回原始值用于比对（鉴权不依赖 token 有效性，只依赖 token 一致性）。
- **R20.13.4**：关联仓库信任域比对（`webhook.py` line 318）同样用 `read_stored_token()`——比对两仓 token 是否相同，不依赖 token 有效性。
- **R20.13.5**：session token 鉴权（`verify_session_token_for_repo`）不受影响，走独立链路。
- **R20.13.6 不用 flag**：不使用 `skip_expired_check` 参数——布尔 flag 会掩盖语义差异，用独立方法 `read_stored_token()` 让方法名表达意图，调用方不可能传错。

### R20.14 建单场景保留用户 token（P0 补充）

- GitCode 建单（`issues_center.py` `create_issue`）必须用用户自己的 token——assign 需用户身份，bot token 无 assign 权限会 403。TokenResolver 策略链优先返回 bot token（路径 A），会导致建单 403。
- 当前代码 `issues_center.py` line 1296-1304：GitCode 走 `get_user_token_with_refresh`（用户 token），GitHub 走 `get_github_token`（repo token）。
- **R20.14.1**：`issues_center create_issue` GitCode 路径保留 `get_user_token_with_refresh`（用户身份建单）。
- **R20.14.2**：`issues_center create_issue` GitHub 路径可用 `Repo.bot_token`（GitHub assign 无用户身份限制）。
- **R20.14.3**：建单时 token 缺失/失效 → 明确引导用户重新绑定，不 fallback 到 bot token。
- **R20.14.4**：此场景不受 TokenResolver 改造影响，保持现有逻辑。

### R20.16 setup_webhook password 缺省行为修正（P1 补充）

- `setup_webhook`（`repo_manager.py` line 560-563）的 `webhook_password` 参数为空时，fallback 到 `get_repo_token_with_refresh` 做 password。路径 B 仓库应传随机密钥（`webhook_secret`），不应 fallback 到 token。
- **R20.16.1**：`setup_webhook` 必须显式传 `webhook_password` 参数（路径 A 传 bot token，路径 B 传随机密钥）。
- **R20.16.2**：`webhook_password` 为空时不再 fallback 到 `get_repo_token_with_refresh`，改为报错或用空串。
- **R20.16.3**：托管逻辑（`repos.py register_repo`）负责生成随机密钥并传入 `setup_webhook`。

### R20.17 VOD handler restart 重新获取 token（P1 补充）

- VOD handler restart（`vod_handler_async.py` line 488/515/528）从 `devops_agent_session` 表读 `repo_gitcode_token` 直接注入 agent。如果 token 已过期但 session 中仍存储旧 token，restart 后 agent 用过期 token → 401。
- **R20.17.1**：VOD handler restart 不从 session 读旧 token，改为调 `TokenResolver.resolve()` 获取最新 token。
- **R20.17.2**：session 中的 `repo_gitcode_token` 仍保留（兼容 A2A 鉴权），但 VOD handler 不直接使用。
- **R20.17.3**：token 过期短路时 VOD handler 获取空 token → 不启动 agent → 飞书通知用户（R20.9.2）。

### R20.18 session token 写入不走短路（P1 补充）

- `agents start/restart` 和 `session_store` 启动时调 `get_repo_token_with_refresh` 获取 token 写入 `devops_agent_session.repo_gitcode_token`。如果写入时走短路返回空串，session 中 token 被清空，VOD handler fallback 到 `Repo.bot_token` 时也会被短路。
- **R20.18.1**：agents start/restart 写入 `session.repo_gitcode_token` 时调 `TokenResolver.read_stored_token()`，写入原始 token 值（不过期检查、不刷新）。
- **R20.18.2**：短路机制仅在运行时 token 获取（`TokenResolver.get_token`）时生效——write 用 `read_stored_token`，read 用 `get_token`。
- **R20.18.3**：session 中的 token 即使已过期也保留原值，供 A2A 鉴权（R20.13）和 VOD restart fallback 使用。
- **R20.18.4**：健康检查标记过期后，session 中的 token 不清除（只标记 `managed_repos.bot_token_status` / `meta.global_bot_token_status`）。

### R20.19 token 短路后 task fail 的 error_code 检测（P1 补充）

- token 短路后返回空串 → agent git clone/push 失败 → error 可能不含 "401 unauthorized" 关键词（可能是 "Authentication failed" 或 "could not read Username"）→ `fail_task` 的 401 检测不触发 → 不写 `NO_TOKEN_AVAILABLE`。
- **R20.19.1**：`fail_task` 检测到 task 获取 token 为空时（`get_task_token` 返回空串），直接写入 `NO_TOKEN_AVAILABLE` error_code。
- **R20.19.2**：空 token 检测在 401 关键词检测之前执行（短路优先）。
- **R20.19.3**：空 token 检测不调 VCS API 验证（token 已为空，无需验证）。
- **R20.19.4**：git clone/push 失败的 error 中含扩展关键词时也触发 token 过期检测（扩展 401 关键词列表）。

### R20.20 其他遗漏场景补充（P1/P2）

- **R20.20.1**：`submit_feedback` 检查 `bot_token_available` 改为调 `Repo.bot_token`（R20.11 已覆盖，自动继承短路）。
- **R20.20.2**：templates verify 保留 `get_user_token_with_refresh`（用户主动操作，独立链路，不走 TokenResolver）——修正 R20.7 表格中"templates read"描述为"templates read 用全局 bot token（独立只读）；templates verify 用用户 token（独立链路）"。
- **R20.20.3**：events terminate → `fail_task` 路径：terminate 是用户手动终止，error="用户手动终止"不含 401 关键词，不触发 token 过期检测。如果 terminate 恰好发生在 agent 401 失败后，`fail_task` 的 401 检测会触发（幂等，已标记不重复）。
- **R20.20.4**：`list_repo_branches` → `get_github_token` 改为 `Repo.bot_token`（R20.11 已覆盖）。token 过期时短路返回空 → 前端显示"GitHub Bot Token 未配置或已失效"。
- **R20.20.5**：`_post_subtask_completion_to_parent` → 用 bot_token 发评论，改为通过 `handler.post_issue_comment()`（R18.6.10 已覆盖）。
- **R20.20.6**：Chat `_resolve_gitcode_token` 保留 user_token 优先逻辑——Chat agent 需用户身份执行（assign 等 VCS 操作需用户 token）。注意：Chat 的 token 语义与执行 token 相同（都需要有效 token），但策略优先级不同——标准 TokenResolver 是 bot-first（session → repo_bot → global → user），Chat 需要 user-first。不合并的原因是避免 `TokenResolver.get_token()` 增加 `strategy_mode` 参数导致参数膨胀，属于"策略差异为避免参数地狱而独立"，不是"语义不同导致必须独立"。`_resolve_gitcode_token` 内部：user_token 非空 → 返回 user_token；user_token 为空 → fallback `Repo.bot_token`（TokenResolver）。

### R20.21 pipeline 步骤顺序明确（设计逻辑补充）

设计文档 5.6 调整后完整步骤顺序：

```
2.0b 开发者指令检测（移到最前，R20.6.2）
  → 含 /done /cancel /resume → 处理指令 → skip
2.0a 标记检测（grape-auto-comment）
  → 系统回帖含标记 → skip
2.0 自循环检测（通过 handler.is_self_loop_actor 调用，R18.6.8）
  → 路径 A：actor in bot_usernames → skip
  → 路径 B：四条件组合判断 → skip
2.0a2 issue_updated description 标记检测
  → description 含 grape-auto-comment 标记 → skip
2.1 L1 过滤
2.2 路由
```

注意：2.0 在 2.0a 之前执行——自循环检测先于标记检测。2.0b 在最前——开发者指令优先于一切。（R24.5 修正：与代码实现和 05-path-b-self-loop.md 一致）

### R20.22 路径 B 判断方式修正（设计逻辑补充）

设计文档 R20.1.1 条件 1 原写"仓库是路径 B（`host_user_id > 0`）"，但代码中路径 B 判断用 `_is_user_token_repo` / `_is_github_user_token_repo`，不只是 `host_user_id > 0`——还检查 `bot_gitcode_token` / `bot_github_token` 是否为空。

修正：条件 1 改为"仓库是路径 B（`_is_user_token_repo(repo_key) == True` 或 `_is_github_user_token_repo(repo_key) == True`）"。

`_is_user_token_repo` 的判断逻辑：`host_user_id > 0` AND `bot_gitcode_token` 为空。如果仓库同时有 `host_user_id > 0` 和 `bot_gitcode_token`（异常状态），`_is_user_token_repo` 返回 False（有 bot token 不是路径 B）。

### R20.23 fail_task 去重逻辑保留在 handler 外部（设计逻辑补充）

当前 `fail_task` 的 `comment_token` 选择逻辑包含 `_fetch_issue_comments` 去重（查 issue 是否已有"Token 已过期"评论）。设计 R18.6.9 说改为 `handler.get_comment_token()`。

修正：`handler.get_comment_token()` 只负责 token 选择（用户 token → 全局 bot fallback → 空），`_fetch_issue_comments` 去重逻辑保留在 `fail_task` 调用方层面，不进入 handler。原因：去重逻辑依赖 issue 已有评论内容（VCS API 调用），不属于 token 操作职责。

## 设计方案

### webhook 验证 3 级优先级

webhook 验证保留独立逻辑，不走 TokenResolver。验证链：

1. 查 `managed_repos.webhook_secret`（路径 B 随机密钥，优先）
2. `webhook_secret` 为空 → 查 `gitcode_webhook_token` / `github_webhook_token`（路径 A bot token 写入的 webhook password）
3. 以上都为空 → fallback 到 `get_repo_bot_token_no_global`（兼容旧数据）

```python
def get_webhook_verify_token(repo_key: str) -> str:
    """获取 webhook 验证用的 token/password。不走 TokenResolver，独立查询。"""
    # 1. 查 webhook_secret（路径 B）
    secret = get_managed_repo_field(repo_key, "webhook_secret")
    if secret:
        return secret
    # 2. 查 webhook_token_field（路径 A）
    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
    wt_field = provider.webhook_token_field()
    wt = get_managed_repo_field(repo_key, wt_field)
    if wt:
        return wt
    # 3. fallback：兼容旧数据（未迁移的仓库）
    return get_repo_bot_token_no_global(repo_key) or ""
```

### A2A 鉴权 read_stored_token 代码示例

`get_repo_bot_token_no_global` 保留为薄包装，内部调 `TokenResolver.read_stored_token()`。`read_stored_token()` 只调策略链 `_fetch()`（纯读存储值），不调 `_is_expired()`，不触发 OAuth refresh。

```python
def get_repo_bot_token_no_global(repo_key: str) -> str:
    """A2A 鉴权用：不含全局兜底，读取存储原始值（不走短路）。"""
    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
    resolver = TokenResolver(provider)
    resolver.strategies = [resolver.strategies[1], resolver.strategies[0], resolver.strategies[3]]  # 跳过全局
    return resolver.read_stored_token(repo_key)
```

### 建单场景：GitCode 用户 token / GitHub repo token

- GitCode 建单保留 `get_user_token_with_refresh`（用户身份，assign 需用户 token，bot token 无 assign 权限会 403）。
- GitHub 建单可用 `Repo.bot_token`（GitHub assign 无用户身份限制）。
- 建单 token 缺失/失效 → 引导用户重新绑定，不 fallback 到 bot token。

### setup_webhook 必传 password

- `setup_webhook` 必须显式传 `webhook_password`（路径 A 传 bot token，路径 B 传随机密钥）。
- `webhook_password` 为空时不再 fallback 到 `get_repo_token_with_refresh`，改为报错或用空串。
- 托管逻辑（`repos.py register_repo`）负责生成随机密钥并传入。

### VOD restart 调 resolve

- VOD handler restart 不从 session 读旧 token，改为调 `TokenResolver.resolve()` 获取最新 token。
- session 中 `repo_gitcode_token` 仍保留（兼容 A2A 鉴权），VOD handler 不直接使用。
- token 过期短路时 VOD handler 获取空 token → 不启动 agent → 飞书通知用户。

### session 写入 read_stored_token

- agents start/restart 写入 `session.repo_gitcode_token` 时调 `TokenResolver.read_stored_token()`，写入原始 token 值（不过期检查、不刷新）。
- 短路机制仅在运行时 token 获取（`get_token`）时生效——write 用 `read_stored_token`，read 用 `get_token`。
- session 中 token 即使已过期也保留原值，供 A2A 鉴权和 VOD restart fallback 使用。
- 健康检查标记过期后 session 中 token 不清除（只标记 `managed_repos.bot_token_status` / `meta.global_bot_token_status`）。

### fail_task 空 token 检测 + 扩展关键词列表

- `fail_task` 检测到 `get_task_token` 返回空串时直接写入 `NO_TOKEN_AVAILABLE` error_code。
- 空 token 检测在 401 关键词检测之前执行（短路优先），不调 VCS API 验证。
- git clone/push 失败 error 含以下扩展关键词时也触发 token 过期检测：
  - `"Authentication failed"`
  - `"could not read Username"`
  - `"fatal: Authentication"`
  - （原有 `"401 unauthorized"` 等）

### pipeline 步骤顺序

```
2.0b 开发者指令检测（/done /cancel /resume）
  → 2.0a 标记检测（grape-auto-comment 系统回帖）
    → 2.0 自循环检测（handler.is_self_loop_actor，路径 A actor in bot_usernames / 路径 B 四条件组合）
      → 2.0a2 issue_updated description 标记检测
        → 2.1 L1 过滤 → 2.2 路由
```

2.0 在 2.0a 之前执行——自循环检测先于标记检测。2.0b 在最前——开发者指令优先于一切。（R24.5 修正）

### _is_user_token_repo 判断

路径 B 判断不只看 `host_user_id > 0`，还检查 `bot_gitcode_token` 是否为空：

- `_is_user_token_repo`：`host_user_id > 0` AND `bot_gitcode_token` 为空。
- 同时有 `host_user_id > 0` 和 `bot_gitcode_token`（异常状态）→ 返回 False（有 bot token 不是路径 B）。
- 条件 1 修正为：`_is_user_token_repo(repo_key) == True` 或 `_is_github_user_token_repo(repo_key) == True`。

### fail_task 去重逻辑

- `handler.get_comment_token()` 只负责 token 选择（用户 token → 全局 bot fallback → 空）。
- `_fetch_issue_comments` 去重逻辑保留在 `fail_task` 调用方层面，不进入 handler。
- 原因：去重依赖 issue 已有评论内容（VCS API 调用），不属于 token 操作职责。

## 涉及代码文件

| 文件 | 改动点 |
|------|--------|
| `src/api/routes/webhook.py` | webhook 验证改用 `get_webhook_verify_token`；A2A 鉴权改用 `get_repo_bot_token_no_global`（内部 `read_stored_token`）；关联仓库信任域比对（line 318）用 `read_stored_token` |
| `src/api/routes/events.py` | A2A 鉴权（line 969, 1281）改用 `get_repo_bot_token_no_global`（`read_stored_token`） |
| `src/api/issues_center.py` | `create_issue`（line 1296-1304）：GitCode 保留 `get_user_token_with_refresh`，GitHub 可用 `Repo.bot_token` |
| `src/user/repo_manager.py` `setup_webhook` | line 560-563：必传 `webhook_password`，去掉 `get_repo_token_with_refresh` fallback |
| `src/im/vod_handler_async.py` | line 488/515/528：restart 不从 session 读旧 token，改调 `TokenResolver.resolve()` |
| `src/api/routes/agents.py` | start/restart 写入 `session.repo_gitcode_token` 时调 `read_stored_token` |
| `src/api/session_store.py` | 启动时写入 session token 调 `read_stored_token` |
| `src/runtime/service.py` `fail_task` | 空 token 检测（`get_task_token` 返回空串 → `NO_TOKEN_AVAILABLE`）；扩展 401 关键词列表；去重逻辑保留在调用方层面 |

## 验收标准

- [ ] webhook 验证走独立 `get_webhook_verify_token`，3 级优先级 `webhook_secret → webhook_token_field → get_repo_bot_token_no_global`，bot token 过期短路不影响 webhook 验证。
- [ ] A2A 鉴权（`webhook.py` / `events.py`）用 `read_stored_token` 获取原始值，token 过期不导致鉴权失败；关联仓库信任域比对同样用 `read_stored_token`。
- [ ] `get_repo_bot_token_no_global` 不使用 `skip_expired_check` flag，改为独立方法 `read_stored_token`。
- [ ] GitCode 建单保留 `get_user_token_with_refresh`，token 缺失时引导用户重新绑定，不 fallback 到 bot token。
- [ ] `setup_webhook` 必传 `webhook_password`，为空时报错/空串，不再 fallback 到 `get_repo_token_with_refresh`。
- [ ] VOD handler restart 调 `TokenResolver.resolve()` 获取最新 token，不从 session 读旧 token；token 短路时不启动 agent 并通知用户。
- [ ] session token 写入用 `read_stored_token`（原始值），短路仅在运行时 `get_token` 生效；session 中过期 token 不清除。
- [ ] `fail_task` 空 token 检测在 401 关键词检测之前执行，空 token 直接写 `NO_TOKEN_AVAILABLE`，不调 VCS API 验证。
- [ ] `fail_task` 401 关键词列表扩展：`"Authentication failed"` / `"could not read Username"` / `"fatal: Authentication"`。
- [ ] R20.20 六项场景覆盖：submit_feedback、templates verify、events terminate、list_repo_branches、_post_subtask_completion_to_parent、Chat _resolve_gitcode_token。
- [ ] pipeline 步骤顺序：2.0b → 2.0 → 2.0a → 2.0a2 → 2.1 → 2.2。（R24.5 修正：2.0 在 2.0a 之前，与代码实现一致）
- [ ] 路径 B 判断用 `_is_user_token_repo` / `_is_github_user_token_repo`，不只看 `host_user_id > 0`。
- [ ] `fail_task` 去重逻辑（`_fetch_issue_comments`）保留在 handler 外部，`handler.get_comment_token()` 只负责 token 选择。

## 依赖场景

- 依赖 **04-token-resolver** 的 `read_stored_token()`——A2A 鉴权（R20.13）、session token 写入（R20.18）均调用该方法读取存储原始值（不走短路、不检查过期、不触发 OAuth refresh）。
- 依赖 **04-token-resolver** 的 `resolve()` / `get_token()`——VOD handler restart（R20.17）调用 `resolve()` 获取最新 token + Handler。
- webhook 验证（R20.12）不依赖 TokenResolver，走独立查询；但 fallback 第 3 级调用 `get_repo_bot_token_no_global`，后者内部依赖 `read_stored_token`。
