# R21 修订章——竞态/PR维度/Session/鉴权/GitHub升级/测试

> 源文档：`repo-token-config-design.md` §五补（R21.4–R21.11），提取自 issue-372 token 配置设计。
> R21.1–R21.3（全局 bot token 更换 / per-provider 过期标记 / 健康检查超时误标记修复）属于独立修订项，见 `10-r21-core-fixes.md`（如已拆分）或源文档 §五补 R21.1–R21.3。

## 需求项

### R21.4 OAuth refresh 竞态补充（B1 遗漏）

**问题**：R20.27 分布式锁框架已有，但 R20.27.2「先读 token_expires_at」不能完全解决竞态——两节点同时读到旧值 → 同时获取锁（一个等待）→ 第一个刷新成功 → 第二个拿到锁后用旧 refresh_token 刷新 → 如果平台做 refresh_token rotation，旧 refresh_token 已失效 → 刷新失败 → token 彻底失效。

- R21.4.1：分布式锁内 refresh 前先读 `token_expires_at`，如果已更新（其他实例刷新成功）→ 直接读新 access_token 返回，不重复 refresh
- R21.4.2：如果 `token_expires_at` 未更新（确实是当前实例需要刷新）→ 执行 refresh → 如果 refresh 失败（refresh_token rotation 导致旧 refresh_token 失效）→ 读 `external_identities` 获取最新 refresh_token（可能已被其他实例更新）→ 用新 refresh_token 重试一次
- R21.4.3：重试仍失败 → 标记 `status=4` → 用户重新绑定
- R21.4.4：UserToken 策略（R18.2.4）的 `_fetch` 内部 `get_user_token_with_refresh` 已有进程内锁（`deps.py _gitcode_refresh_locks`），R20.27 分布式锁在其外层——双层锁保护

### R21.5 PR 维度自循环检测（B2 遗漏）

**问题**：R20.1.8 `check_running_task` 只定义 `WHERE repo_key=? AND issue_id=?`，PR 评论自循环检测缺 `pr_id` 字段定义。

- R21.5.1：`agent_tasks` 表利用现有 `issue_id` 字段存储 PR 关联标识——PR 类事件的 `issue_id` 已存为 PR 号或 PR 关联 Issue 号（`resolve_pr_issue_id` 解析结果）
- R21.5.2：`check_running_task` 对 `note_hook_pr` 事件：先尝试 `issue_id` 查询（PR 关联的 Issue），如果查不到 → 按 `repo_key + event_type='note_hook_pr'` 查询（PR 维度）
- R21.5.3：补充查询 SQL：`SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('running','dispatched') LIMIT 1`——`issue_id` 字段对 PR 事件存储 PR 号或关联 Issue 号，查询逻辑不变
- R21.5.4：如果 PR 无关联 Issue（`issue_id`=空或 PR 号格式如 `#pr_123`）→ 条件 4 降级到 R20.10.5 session 兜底查询

### R21.6 非场景 task 行 issue_id 语义（B3 遗漏）

**问题**：飞书 VOD / Chat direct / autopilot / event dialogue 创建 task 行时无 issue 上下文，`issue_id` 填空则条件 4 永远匹配不到。

- R21.6.1：飞书 VOD agent 执行时 `issue_id` 填飞书消息关联的 Issue 号（VOD handler 从飞书消息解析 `repo_key + issue_id`）。如果无关联 Issue → `issue_id` 填空 → 条件 4 降级到 session 兜底（R20.10.5）
- R21.6.2：Chat direct 模式 `issue_id` 填会话关联的 Issue 号（`conv.repo_key + conv.issue_number`）。如果无关联 Issue → `issue_id` 填空 → 条件 4 降级到 session 兜底
- R21.6.3：autopilot `issue_id` 填巡检关联的 Issue 号（如有）。无关联 → `issue_id` 填空 → session 兜底
- R21.6.4：event dialogue `issue_id` 填事件关联的 Issue 号（`evt.issue_id`）。无关联 → `issue_id` 填空 → session 兜底
- R21.6.5：`issue_id` 为空时，条件 4 的 `agent_tasks` 查询不会命中（`WHERE issue_id=?` 用空串匹配不到有值行）→ 降级到 R20.10.5 session 兜底查询（`devops_agent_session.last_active_at > now-300s`）
- R21.6.6：session 兜底查询是 repo 级（无 issue_id），会覆盖该仓库所有 issue/PR 的评论——存在误拦风险（见 R21.7）

### R21.7 check_running_task session 兜底误拦分析（B4 遗漏）

**问题**：session 兜底查询（`devops_agent_session.last_active_at > now-300s AND status=active`）是 repo 级，agent 完成但 session 未置 inactive 时，host 用户对该仓库任何 issue 的评论都会被误拦最多 5 分钟。

- R21.7.1：agent 执行完成后立即更新 `devops_agent_session.last_active_at = NULL` 或 `status = 'idle'`（如果当前设计是保持 active），缩小误拦窗口
- R21.7.2：session 兜底查询窗口从 300s 缩短到 60s——agent 完成后 60s 内的评论才可能被误拦（大部分 agent 完成后用户不会立即评论）
- R21.7.3：误拦场景用例补充：
  1. agent 完成 → session `last_active_at` 更新 → task `status=completed`
  2. 用户 30s 后对该仓库另一个 issue 评论
  3. 条件 4 `agent_tasks` 查询：`issue_id` 不同 → 不命中
  4. 条件 4 session 兜底：`last_active_at > now-60s` → 命中 → 误拦
  5. 60s 后用户再次评论 → session 兜底不命中 → 放行
  6. 可接受——最多 60s 误拦窗口，用户重试即可
- R21.7.4：如果 agent 执行完后立即更新 `session status='idle'`（R21.7.1），则 session 兜底查询 `status='active'` 不命中 → 无误拦

### R21.8 「有仓库权限的用户」鉴权规则（B5 遗漏）

**问题**：全文反复出现「有仓库权限的用户」但未定义判定规则。

- R21.8.1：「有仓库权限的用户」定义：grape 侧托管人（`managed_repos.host_user_id` 指向的用户）+ grape 侧仓库协作者（`devops_agent_session` 中该仓库有 active session 的用户）+ grape admin
- R21.8.2：`PUT /repos/bot-token` 鉴权：只有托管人或 admin 可配置仓库级 bot token。普通 grape 用户不能给别人托管的仓库配 token（防水平越权）
- R21.8.3：settings 页 token 状态可见性：托管人 + admin 可见全局/仓库级 bot token 状态
- R21.8.4：VCS 侧 repo 成员不等于 grape 侧有权限——grape 侧权限独立于 VCS 侧。VCS 侧 repo 成员可以托管仓库（成为托管人），但托管后 grape 侧权限由托管关系决定
- R21.8.5：组织仓库（`org_id > 0`）：组织管理员视为有仓库权限

### R21.9 GitHub 路径 B auto_upgrade（B6 遗漏）

**问题**：auto_upgrade 全部场景只描述 GitCode，GitHub 路径 B 仓库是否参与升级未定义。

- R21.9.1：`auto_upgrade_path_b_repos` 按 provider 分流——GitCode 全局 bot token 配置时遍历 GitCode 路径 B 仓库，GitHub 全局 bot token 配置时遍历 GitHub 路径 B 仓库
- R21.9.2：GitHub 路径 B 仓库判断：`_is_github_user_token_repo(repo_key) == True`（`host_user_id > 0 AND bot_github_token` 为空）
- R21.9.3：GitHub auto_upgrade 流程与 GitCode 对称：获取 host 用户 GitHub token → 邀 GitHub bot 为协作者 → accept invite → 重建 GitHub webhook → 写 DB（`bot_github_token + 清 host_user_id + 清 webhook_secret`）
- R21.9.4：GitHub 协作者邀请用 `add_github_collaborator`（`permission="push"`），accept 用 `accept_github_invite`
- R21.9.5：GitHub webhook 重建用 `setup_github_webhook / remove_github_webhook`
- R21.9.6：`save_config` 同时配置 GitCode + GitHub 全局 bot token 时，两个 provider 的 auto_upgrade 并行执行（后台线程）

### R21.10 token 存储安全与测试需求（B7 遗漏）

**问题**：token 存储安全需求缺失，测试/验收需求缺失，`webhook_secret` / A2A 比对日志未纳入脱敏范围。

- R21.10.1：token 存储安全——现有 `credential_crypto` 加密覆盖 `external_identities`（用户 token）。`bot_gitcode_token / bot_github_token / repo_gitcode_token / repo_github_token` 在 `managed_repos / devops_agent_session` 表中明文存储（与现有设计一致，这些是 bot token 不是用户凭据）。新增列（`bot_token_status / host_vcs_username / token_status`）不涉及加密——它们是状态/元数据列，不含 token 原文
- R21.10.2：`webhook_secret` 脱敏——R19.1 日志脱敏范围扩展到 `webhook_secret` 值。webhook 验证日志（`webhook.py`）不输出 `webhook_secret` 原文或片段
- R21.10.3：A2A 比对日志脱敏——`webhook.py` A2A 鉴权日志不输出 token 原文（当前日志只输出 `bot=True/False`，已安全）
- R21.10.4：测试/验收需求——R18.4.3「现有测试通过」扩展为验收清单：
  - 单元测试：`TokenResolver.get_token / read_stored_token / resolve` 各策略覆盖
  - 单元测试：`TokenActionHandler.is_self_loop_actor / get_comment_token / mark_expired` 各 Handler 覆盖
  - 集成测试：路径 A/B token 获取 + 短路 + 恢复全流程
  - 集成测试：`fail_task` 401 检测 + `error_code` 写入 + 评论 fallback
  - 集成测试：健康检查标记过期 + 短路 + 恢复
  - E2E：路径 B agent 评论自循环检测（四条件组合）
  - E2E：全局 bot token 更换（R21.1 `global_bot_token_swap`）
  - E2E：`GIT_ASKPASS` fallback（R20.24 路径 B git clone）

### R21.11 其他轻量修正（C 级）

- R21.11.1：场景 13.2 评论权限模型——给出结论性需求：`fail_task` 评论 best-effort，403 时不重试不报错（与 R20.2.3 一致）。不预探测权限——复杂度高收益低，403 时静默降级到 dashboard 通知
- R21.11.2：自循环检测性能——`host_vcs_username` 查询走 `managed_repos.host_vcs_username` 列（R16.5.5 缓存），避免每次 webhook 查 `external_identities`。`check_running_task` 查 `agent_tasks` 索引（`repo_key + issue_id + status`），无性能瓶颈
- R21.11.3：R20.19.4「could not read Username」误报声明——该关键词对私有仓库不存在等场景也会出现，触发 verify 多一次 API 调用（5s 超时保护），可接受。verify 通过（200）→ 不标记过期，只记录 warning
- R21.11.4：DB 迁移降级——5 处幂等 `ALTER try-except` 保护，部分迁移失败时对应功能降级（R16.4.4 已覆盖 session `token_status` 列缺失降级）。整体降级原则：列不存在 → 对应短路检查跳过（返回 False），不影响 token 获取主链路

## 设计方案

### 竞态保护（R21.4）

OAuth refresh 竞态采用**双层锁 + 锁内重读 + 失败重取**策略：

1. 外层分布式锁（R20.27 `run_with_distributed_lock`）+ 内层进程锁（`deps.py _gitcode_refresh_locks`）——双层保护
2. 获取分布式锁后**先读 `token_expires_at`**：已更新 → 直接返回新 access_token，不重复 refresh
3. 未更新 → 执行 refresh；失败时**重读 `external_identities` 获取最新 refresh_token** → 重试一次
4. 重试仍失败 → `status=4` 标记，用户重新绑定

### PR 维度自循环（R21.5 + R21.6）

- `agent_tasks.issue_id` 字段复用：PR 事件存 PR 号或关联 Issue 号（`resolve_pr_issue_id` 解析结果）
- 非场景 task（飞书 VOD / Chat direct / autopilot / event dialogue）：有 Issue 号则填，无则填空
- `issue_id` 为空时降级到 session 兜底查询

### Session 兜底误拦（R21.7）

| 措施 | 效果 |
|------|------|
| agent 完成后 `session status='idle'` | session 兜底查询 `status='active'` 不命中 → 无误拦 |
| 兜底窗口 300s → 60s | 即使 status 未及时更新，误拦窗口缩小到 60s |
| 用户重试即可放行 | 60s 后 session 兜底不命中 → 正常处理 |

### 鉴权规则（R21.8）

```
有仓库权限的用户 = 托管人（managed_repos.host_user_id）
                 + 仓库协作者（devops_agent_session active session）
                 + grape admin
                 + 组织管理员（org_id > 0）
```

- `PUT /repos/bot-token`：只有托管人或 admin 可配置（防水平越权）
- settings 页 token 状态：托管人 + admin 可见
- grape 侧权限独立于 VCS 侧 repo 成员

### GitHub auto_upgrade（R21.9）

GitHub 路径 B auto_upgrade 与 GitCode 对称：

```
auto_upgrade_path_b_repos(provider):
  1. 遍历 _is_github_user_token_repo(repo_key)==True 的仓库
  2. 获取 host 用户 GitHub token
  3. add_github_collaborator(permission="push") → accept_github_invite
  4. remove_github_webhook + setup_github_webhook（新 bot token 做 password）
  5. 写 DB: bot_github_token + 清 host_user_id + 清 webhook_secret
```

- `save_config` 同时配置两个 provider 时，auto_upgrade 并行执行（后台线程）

### 安全与测试（R21.10）

- token 存储：`external_identities` 加密（`credential_crypto`），bot token 明文（非用户凭据）
- 日志脱敏扩展：`webhook_secret` + A2A 比对日志（当前已安全，只输出 `bot=True/False`）
- 测试验收清单覆盖：单元（Resolver + Handler）/ 集成（路径 + fail_task + 健康检查）/ E2E（自循环 + bot 更换 + GIT_ASKPASS）

### 轻量修正（R21.11）

| 项 | 结论 |
|----|------|
| 评论权限模型 | best-effort，403 静默降级，不预探测 |
| 自循环性能 | `host_vcs_username` 走缓存列，`agent_tasks` 走索引 |
| "could not read Username" 误报 | verify 通过则不标记，只 warning |
| DB 迁移降级 | 幂等 `ALTER try-except`，列缺失→短路跳过 |

## 涉及代码文件

| 文件 | 改动点 | 关联需求 |
|------|--------|----------|
| `src/api/deps.py` | `_gitcode_refresh_locks` 进程内锁 + 分布式锁双层保护；refresh 失败重读 `external_identities` | R21.4 |
| `src/db.py` | `get_user_token_with_refresh` / `get_user_github_token_with_refresh` 竞态保护逻辑 | R21.4 |
| `src/webhook.py` | `check_running_task` PR 维度查询 + session 兜底 60s 窗口；`note_hook_pr` 事件处理 | R21.5, R21.7 |
| `src/pipeline.py`（或 task 创建处） | 飞书 VOD / Chat direct / autopilot / event dialogue task 行 `issue_id` 填充 | R21.6 |
| `src/agents/`（session 管理） | agent 完成后更新 `devops_agent_session.status='idle'` | R21.7 |
| `src/api/repos.py`（`PUT /repos/bot-token`） | 鉴权规则：托管人/admin 校验 | R21.8 |
| `src/api/settings.py` | token 状态可见性：托管人 + admin | R21.8 |
| `src/user/repo_manager.py` | `add_github_collaborator` / `accept_github_invite` / `setup_github_webhook` / `remove_github_webhook` | R21.9 |
| `src/api/settings.py`（`save_config`） | GitHub auto_upgrade 触发 + 并行执行 | R21.9 |
| `src/webhook.py` | `webhook_secret` 脱敏 + A2A 比对日志确认 | R21.10 |
| `src/credential_crypto.py` | 确认加密覆盖范围（不改） | R21.10 |
| 测试文件（`tests/`） | 单元 + 集成 + E2E 验收清单 | R21.10 |
| `src/user/repo_manager.py` | `fail_task` 评论 best-effort + 403 静默降级 | R21.11 |
| DB 迁移（`ALTER TABLE` 语句） | 幂等 `try-except` 保护 | R21.11 |

## 验收标准

### R21.4 OAuth 竞态
- [ ] 两实例同时触发 refresh：第一个成功，第二个锁内重读 `token_expires_at` → 直接返回新 token，不重复 refresh
- [ ] refresh_token rotation 场景：第二个实例 refresh 失败 → 重读 `external_identities` → 用新 refresh_token 重试成功
- [ ] 重试仍失败 → `status=4` 标记，account 页提示重新绑定

### R21.5 PR 维度自循环
- [ ] PR 评论事件：`issue_id` 查询命中 running task → 自循环拦截
- [ ] PR 无关联 Issue：降级到 `repo_key + event_type='note_hook_pr'` 查询 → 命中
- [ ] PR 无关联 Issue 且无 running task → 降级到 session 兜底

### R21.6 非场景 task issue_id
- [ ] 飞书 VOD / Chat direct / autopilot / event dialogue：有 Issue 号 → 填入 `agent_tasks.issue_id`
- [ ] 无 Issue 号 → `issue_id` 为空 → 条件 4 不命中 → 降级到 session 兜底

### R21.7 session 兜底误拦
- [ ] agent 完成后 `session status='idle'` → session 兜底查询不命中 → 无误拦
- [ ] 即使 status 未及时更新，兜底窗口 60s → 最多 60s 误拦，用户重试放行
- [ ] 误拦用例验证：agent 完成 → 30s 后另一 issue 评论 → 误拦 → 60s 后重试 → 放行

### R21.8 鉴权规则
- [ ] `PUT /repos/bot-token`：托管人可配置，admin 可配置，普通用户不可配置（403）
- [ ] settings 页：托管人 + admin 可见 token 状态，普通用户不可见
- [ ] 组织仓库：组织管理员视为有权限

### R21.9 GitHub auto_upgrade
- [ ] GitHub 全局 bot token 配置 → `auto_upgrade_path_b_repos` 遍历 GitHub 路径 B 仓库
- [ ] 邀 GitHub bot 为协作者（`permission="push"`）→ accept invite → 重建 webhook → 写 DB
- [ ] 同时配置 GitCode + GitHub → 两个 provider 并行执行

### R21.10 安全与测试
- [ ] 健康检查日志 / `fail_task` 日志 / `error_code` 不含 token 原文或片段
- [ ] `webhook.py` 日志不输出 `webhook_secret` 原文或片段
- [ ] A2A 比对日志只输出 `bot=True/False`（已安全，确认）
- [ ] 单元测试：`TokenResolver` 各策略 + `TokenActionHandler` 各 Handler 覆盖
- [ ] 集成测试：路径 A/B + `fail_task` + 健康检查全流程
- [ ] E2E：自循环检测 + bot 更换 + `GIT_ASKPASS` fallback

### R21.11 轻量修正
- [ ] `fail_task` 评论 403 → 不重试不报错，静默降级到 dashboard 通知
- [ ] `host_vcs_username` 查询走 `managed_repos` 缓存列，不每次查 `external_identities`
- [ ] "could not read Username" verify 通过 → 不标记过期，只 warning
- [ ] DB 迁移：列不存在 → 短路检查跳过（返回 False），不影响 token 获取主链路

## 依赖场景

| 依赖项 | 关系 | 说明 |
|--------|------|------|
| R20.27 分布式锁 | R21.4 前置 | R21.4 在分布式锁框架上补充竞态保护 |
| R20.1.8 `check_running_task` | R21.5 修正 | PR 维度查询补充 |
| R20.10.5 session 兜底 | R21.6/R21.7 依赖 | 非场景 task 降级到 session 兜底；R21.7 缩小兜底窗口 |
| R16.5.5 `host_vcs_username` 列 | R21.11.2 依赖 | 自循环检测性能优化走缓存列 |
| R16.4.4 session `token_status` 降级 | R21.11.4 复用 | DB 迁移降级原则 |
| R20.2.3 评论 best-effort | R21.11.1 一致 | `fail_task` 评论 403 静默降级 |
| R20.19.4 "could not read Username" | R21.11.3 声明 | verify 误报处理 |
| R19.1 日志脱敏 | R21.10.2 扩展 | `webhook_secret` 纳入脱敏范围 |
| R21.1 `global_bot_token_swap` | R21.10.4 E2E 依赖 | 全局 bot token 更换 E2E 测试 |
| R20.24 `GIT_ASKPASS` fallback | R21.10.4 E2E 依赖 | 路径 B git clone E2E 测试 |
| R18.2.4 UserToken 策略 | R21.4.4 依赖 | `_fetch` 内部进程锁 + 外层分布式锁 |
| R15 `auto_upgrade` | R21.9 扩展 | GitHub 路径 B auto_upgrade 对称设计 |

### R21.12 task 排队期间 token 过期窗口（P1 补充）

**问题**：路径 A 仓库 task 创建时写入 bot token 到 task 行，排队期间 token 过期，claim 时不检查过期标记直接用旧 token → 401。

**时序**：
```
T0: task 创建 → bot token 写入 task 行（非空）
T1: task 排队（status=queued）
T2: 健康检查发现 token 过期 → 标记 meta/managed_repos expired
T3: worker claim → task 行 token 非空 → 不检查 → 用旧 token → 401
```

- R21.12.1：claim 时对路径 A 仓库 task 行 token 做新鲜度检查——查 DB 过期标记（meta / managed_repos）
- R21.12.2：已过期 → 重新调 TokenResolver.get_token → 短路返回空则注入空串 → fail_task NO_TOKEN_AVAILABLE
- R21.12.3：重新获取返回非空（其他策略有可用 token）→ 注入新 token
- R21.12.4：路径 B 不受影响（task 行本来就为空，走现有实时获取）
- R21.12.5：只对路径 A 生效
- R21.12.6：不调 VCS API，只查 DB 过期标记，无延迟
- R21.12.7：在现有 `if not task.get("repo_gitcode_token")` 逻辑之前执行

**涉及代码文件**：
| 文件 | 改动 | 说明 |
|------|------|------|
| `src/runtime/service.py` claim 函数 | 新增新鲜度检查 | task 行 token 非空时检查 _is_expired |

**验收标准**：
- [ ] 路径 A task 排队期间 token 过期 → claim 时检测到 → 重新获取或注入空串
- [ ] 路径 A task 排队期间 token 未过期 → claim 时 _is_expired=False → 不重新获取（无延迟）
- [ ] 路径 B task 不受影响（task 行为空，走现有逻辑）
- [ ] 新鲜度检查不调 VCS API（只查 DB 过期标记）
