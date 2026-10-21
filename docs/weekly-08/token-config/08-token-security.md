# 关联仓库 Token + OAuth 锁 + 飞书 User Token + Redispatch

> 提取自设计文档 `repo-token-config-design.md` R20.26–R20.29（第 905–960 行）及 R21.4 OAuth 竞态补充（第 1030–1039 行）。

---

## 需求项

### R20.26 关联仓库 token 独立获取

关联仓库（linked repos, Issue #242）用主仓库 token 做所有 git 操作（`ensure_repo_base_multi token=token`, `_submit_direct_linked_prs repo_gitcode_token=token`）。但关联仓库可能在不同 VCS 平台（主仓库 gitcode，关联仓库 github）或不同 owner → 主仓库 token 不匹配 → **clone/push 403/401**。

- **R20.26.1**：关联仓库 token 独立获取——每个关联仓库用各自的 `Repo(repo_key).bot_token` 获取 token，不复用主仓库 token。
- **R20.26.2**：`ensure_repo_base_multi` 改为接收 `token_map: dict[repo_key, str]`（每个仓库独立 token），而非单一 token 参数。
- **R20.26.3**：`_submit_direct_linked_prs` 遍历关联仓库时，对每个仓库调 `Repo(repo_key).bot_token` 获取该仓库的 token。
- **R20.26.4**：关联仓库 token 短路时（过期）→ 该仓库 clone/push 跳过 + 记录 warning，不影响主仓库 PR。
- **R20.26.5**：关联仓库可能在不同 VCS 平台——`Repo.bot_token` 内部按 repo_key 前缀自动分流 provider，已有逻辑覆盖。

### R20.27 OAuth refresh 分布式锁

双主集群（master-001 + master-002）下两实例同时刷新同一用户 OAuth token → **竞态** → 旧 refresh_token 被覆盖 → 后刷新的请求拿到新 token 但旧 token 已失效。

当前代码 `deps.py` line 17-18 用进程内 `threading.Lock`（`_gitcode_refresh_locks`），只防单实例内并发，**不防跨实例**。

- **R20.27.1**：OAuth refresh 加分布式锁（`run_with_distributed_lock`），锁名 `oauth_refresh:{user_id}:{provider}`，多实例只有一个执行 refresh。
- **R20.27.2**：refresh 前先读 `external_identities` 获取最新 `token_expires_at`——如果其他实例已刷新成功（`token_expires_at` 更新），当前实例跳过刷新。
- **R20.27.3**：刷新失败时不清除旧 token（保留最后已知有效值）。
- **R20.27.4**：GitHub OAuth refresh（`get_user_github_token_with_refresh`）同样加分布式锁。
- **R20.27.5**：分布式锁超时 30s——OAuth refresh 通常 < 5s，30s 超时防止死锁。

### R20.28 飞书 direct 模式 user_token 获取

飞书 direct 模式 `consume_stream_async(user_token="")` → `user_token` **始终为空**。agent 如果需要用户身份操作（assign issue 等）会 403。

当前代码 `handler_async.py` line 1650: `user_token=""`。飞书 direct 模式不调 `get_user_token_with_refresh`。

- **R20.28.1**：飞书 direct 模式启动前获取用户 token——调 `get_user_token_with_refresh(username, user_id)` 获取飞书用户的 VCS token。**当前状态**：
  - 飞书 VOD handler direct 模式：**已实现**（vod_handler_async.py:320-323，路径 B 时从 `_token_result.token` 获取 user_token）
  - 飞书 Chat handler_async direct 模式：**遗漏**（handler_async.py:1650 `user_token=""` 始终为空，未调 `get_user_token_with_refresh`）
- **R20.28.2**：用户 token 注入 `consume_stream_async` 的 `user_token` 参数 → env 注入 `USER_GITCODE_TOKEN` / `USER_GITHUB_TOKEN`
  - VOD handler direct 模式：**已实现**（consume_stream_async 传 `user_token=_user_token`）
  - Chat handler_async direct 模式：**遗漏**（consume_stream_async 传 `user_token=""`）
- **R20.28.3**：token 过期时（`get_user_token_with_refresh` 返回空）→ agent 无用户身份 → agent 调 VCS API 403 时返回友好提示
- **R20.28.4**：飞书 queue 模式同理——`enqueue_task` 前获取用户 token。**当前状态**：
  - VOD handler queue 模式：**遗漏**（enqueue_task 调用未传 `user_gitcode_token` 参数，路径 B 仓库的 token 被注入为 `repo_gitcode_token` 而非 `user_gitcode_token`——语义混乱，agent 侧 GIT_ASKPASS 把 user token 当 bot token 用）
  - Chat handler_async queue 模式：**遗漏**（同上，enqueue_task 未传 `user_gitcode_token`）

### R20.29 redispatch 移除冗余 token 参数

redispatch 创建新 task 时，`_get_task_repo_token` 重新获取 token（正确）。但 `_redispatch_vod_after_recommend` 传递的 `repo_gitcode_token` 参数是**旧 task 的 token**（可能已过期），虽然 pipeline 内部会重新获取覆盖，但参数传递冗余且可能误导。

当前代码 `event_pipeline.py` line 3371: `_redispatch_vod_after_recommend(..., repo_gitcode_token)` 传旧 token。line 3905: 函数签名接收 `repo_gitcode_token` 但内部通过 `_enqueue_pipeline_task` 重新获取。

- **R20.29.1**：redispatch 创建新 task 时通过 `_get_task_repo_token` 重新获取 token（已有逻辑，正确）。
- **R20.29.2**：`_redispatch_vod_after_recommend` 移除 `repo_gitcode_token` 参数（冗余，pipeline 内部重新获取）。
- **R20.29.3**：events redispatch 调 `Repo.bot_token` 获取新 token（R20.7.5 已覆盖）。
- **R20.29.4**：redispatch 时如果 token 短路返回空 → 新 task 行 `repo_gitcode_token=空` → agent 运行时实时获取（同路径 B 逻辑）→ 如果也短路 → fail_task `NO_TOKEN_AVAILABLE`。

---

## 设计方案

### 关联仓库跨平台 token 问题 + token_map 改造

**问题**：关联仓库可能跨平台（主仓库 gitcode，关联仓库 github），主仓库 token 对关联仓库无效。

**当前代码**：
- `repogit.py` line 155-170: `ensure_repo_base_multi(linked_repos, token=token)` 用主仓库 token
- `event_pipeline.py` line 3503: `_submit_direct_linked_prs(env_extra, repo_key, issue_id, repo_gitcode_token)` 用主仓库 token
- `repogit.py` line 177: `ensure_linked_repo_worktrees(token=token)` 用主仓库 token

**改造方案**：
1. `ensure_repo_base_multi` 签名从 `token: str` 改为 `token_map: dict[repo_key, str]`，遍历时按 repo_key 取对应 token
2. `_submit_direct_linked_prs` 遍历关联仓库时，对每个仓库调 `Repo(repo_key).bot_token` 独立获取
3. `Repo.bot_token` 内部按 repo_key 前缀自动分流 provider（gitcode/github），已有逻辑覆盖跨平台场景
4. 关联仓库 token 短路时跳过该仓库 + warning，不影响主仓库 PR 流程

### OAuth refresh rotation 竞态 + 双层锁

**R20.27 分布式锁的局限**（R21.4 补充）：R20.27.2"先读 token_expires_at"不能完全解决竞态——两节点同时读到旧值 → 同时获取锁（一个等待）→ 第一个刷新成功 → 第二个拿到锁后用旧 refresh_token 刷新 → 如果平台做 **refresh_token rotation**，旧 refresh_token 已失效 → 刷新失败 → token 彻底失效。

**双层锁设计**：
1. **外层分布式锁**（`run_with_distributed_lock`）：锁名 `oauth_refresh:{user_id}:{provider}`，超时 30s，多实例只有一个执行 refresh
2. **内层进程内锁**（`deps.py` `_gitcode_refresh_locks`）：UserToken 策略 `_fetch` 内部 `get_user_token_with_refresh` 已有进程内锁

**锁内逻辑**：
- 进入分布式锁后先读 `token_expires_at`，如果已更新（其他实例刷新成功）→ 直接读新 access_token 返回，不重复 refresh（R21.4.1）
- 如果 `token_expires_at` 未更新 → 执行 refresh → 如果 refresh 失败（refresh_token rotation 导致旧 refresh_token 失效）→ 读 `external_identities` 获取最新 refresh_token（可能已被其他实例更新）→ 用新 refresh_token 重试一次（R21.4.2）
- 重试仍失败 → 标记 `status=4` → 用户重新绑定（R21.4.3）
- 刷新失败不清除旧 token，保留最后已知有效值（R20.27.3）

### 飞书 direct user_token=空 bug + 获取逻辑

**Bug**：`handler_async.py` line 1650 飞书 direct 模式 `consume_stream_async(user_token="")` → user_token 始终为空。

**修正逻辑**：
1. 飞书 direct 模式启动前调 `get_user_token_with_refresh(username, user_id)` 获取飞书用户的 VCS token
2. 用户 token 注入 `consume_stream_async` 的 `user_token` 参数
3. env 注入 `USER_GITCODE_TOKEN` / `USER_GITHUB_TOKEN`（与 R20.24 注入链路衔接）
4. token 过期时（`get_user_token_with_refresh` 返回空）→ agent 无用户身份 → VCS API 403 时返回友好提示
5. 飞书 queue 模式同理——`enqueue_task` 前获取用户 token

### redispatch _redispatch_vod_after_recommend 冗余参数

**问题**：`_redispatch_vod_after_recommend` 传递的 `repo_gitcode_token` 是旧 task 的 token（可能已过期），虽然 pipeline 内部 `_enqueue_pipeline_task` 会重新获取覆盖，但参数传递冗余且可能误导。

**修正**：移除 `repo_gitcode_token` 参数，pipeline 内部统一通过 `_get_task_repo_token` 重新获取。redispatch 时 token 短路返回空 → 新 task 行 `repo_gitcode_token=空` → agent 运行时实时获取（同路径 B 逻辑）→ 也短路则 fail_task `NO_TOKEN_AVAILABLE`。

---

## 涉及代码文件

| 文件 | 位置/函数 | 职责 |
|------|-----------|------|
| `src/runtime/repogit.py` | `ensure_repo_base_multi` (line 155-170) | 关联仓库 clone，改为 `token_map: dict[repo_key, str]` |
| `src/runtime/repogit.py` | `ensure_linked_repo_worktrees` (line 177) | 关联仓库 worktree 创建，token 独立获取 |
| `src/api/event_pipeline.py` | `_submit_direct_linked_prs` (line 3503) | 关联仓库 PR 提交，遍历时独立获取每个仓库 token |
| `src/api/deps.py` | `_gitcode_refresh_locks` (line 17-18) | 进程内 threading.Lock，内层锁；外层加分布式锁 |
| `src/im/handler_async.py` | line 1650 `consume_stream_async` | 飞书 direct 模式 user_token="" bug，需调 `get_user_token_with_refresh` |
| `src/api/event_pipeline.py` | `_redispatch_vod_after_recommend` (line 3371/3905) | 移除冗余 `repo_gitcode_token` 参数 |

---

## 验收标准

### R20.26 关联仓库 token
- [ ] `ensure_repo_base_multi` 接收 `token_map: dict[repo_key, str]`，每个关联仓库用独立 token
- [ ] `_submit_direct_linked_prs` 遍历关联仓库时调 `Repo(repo_key).bot_token` 独立获取
- [ ] 关联仓库 token 短路时跳过该仓库 + warning，不影响主仓库 PR
- [ ] 跨平台关联仓库（主 gitcode + 关联 github）token 正确分流

### R20.27 OAuth refresh 分布式锁
- [ ] OAuth refresh 加分布式锁 `oauth_refresh:{user_id}:{provider}`，超时 30s
- [ ] refresh 前先读 `token_expires_at`，已刷新则跳过
- [ ] refresh 失败不清除旧 token
- [ ] GitHub OAuth refresh 同样加分布式锁
- [ ] refresh_token rotation 竞态场景：分布式锁内重试一次新 refresh_token（R21.4.2）

### R20.28 飞书 direct user_token
- [ ] 飞书 VOD handler direct 模式启动前调 `get_user_token_with_refresh` 获取用户 token（**已实现**）
- [ ] 飞书 Chat handler_async direct 模式启动前调 `get_user_token_with_refresh` 获取用户 token（**遗漏，需补充**）
- [ ] 用户 token 注入 `consume_stream_async` 的 `user_token` 参数（VOD 已实现，Chat 遗漏）
- [ ] env 注入 `USER_GITCODE_TOKEN` / `USER_GITHUB_TOKEN`
- [ ] token 过期时返回友好提示
- [ ] 飞书 VOD handler queue 模式传 `user_gitcode_token`（**遗漏，需补充**）
- [ ] 飞书 Chat handler_async queue 模式传 `user_gitcode_token`（**遗漏，需补充**）

### R20.29 redispatch 冗余参数
- [ ] `_redispatch_vod_after_recommend` 移除 `repo_gitcode_token` 参数
- [ ] redispatch 创建新 task 时 `_get_task_repo_token` 重新获取 token
- [ ] redispatch 时 token 短路 → task 行空 → agent 运行时获取 → 也短路则 `NO_TOKEN_AVAILABLE`

---

## 依赖场景

- **关联仓库跨平台**（Issue #242）：主仓库 gitcode + 关联仓库 github，需独立获取各自 token
- **双主集群 OAuth 竞态**：master-001 + master-002 同时刷新同一用户 token，需分布式锁 + rotation 重试
- **飞书 direct 模式用户身份操作**：agent 需要 user_token 做 assign issue 等 VCS 操作，当前 user_token 始终为空
- **VOD redispatch**：推荐后 redispatch 创建新 task，旧 token 参数冗余需移除
- **路径 B 仓库 token 短路**（R20.24）：redispatch 时 token 短路 → task 行空 → agent 运行时实时获取，与路径 B 逻辑一致
- **TokenResolver 改造**（R20.7）：`Repo.bot_token` 获取逻辑受 TokenResolver 影响，关联仓库独立获取时同样适配
