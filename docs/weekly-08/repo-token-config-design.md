# 仓库级 Token 配置处理逻辑 + Token 过期统一框架设计

> **实现状态声明（A4）**：本文档（R15-R21）为设计提案，当前零实现。
> TokenResolver / TokenActionHandler / read_stored_token / bot_token_status /
> host_vcs_username / global_bot_token_status 在 src/ 下全部零命中。
> 主线（issue-372-user-token-only-requirements.md，R1-R14）已实现并代码验证。
> 本文档与主线的关系：
> - 自循环检测：主线 R4 标记优先（已实现）是第一道防线，本文档 R20.1 四条件是标记失效时的兜底
> - token 获取：主线 get_repo_token_with_refresh（已实现）是当前入口，本文档 R18 TokenResolver 是统一化重构
> - 过期检测：主线 R10 只覆盖用户 token，本文档 R16 扩展到全局/仓库级 bot token
> - R20.7 矩阵中标注的"遗漏：不享受短路"的场景，部分在主线 R12.12/R14 已统一走 get_repo_token_with_refresh
>
> 实现时应先对齐主线已实现内容，避免重复或矛盾。

## 一、背景

系统有三个 token 层级：

| 层级 | 存储位置 | 类型 | 当前过期检测 |
|------|---------|------|------------|
| 用户 token | external_identities | OAuth / PAT | R10 有 |
| 全局 bot token | meta 表 | PAT | 无 |
| 仓库级 bot token | managed_repos | PAT | 无 |

当前问题：
1. R10 过期检测只针对用户 token，全局/仓库级 bot token 过期后静默失败
2. 配置仓库级 token 时不校验有效性、顺序错误（先建 webhook 后邀 bot）、部分失败导致全断
3. 三层 token 过期处理逻辑分散，是打补丁式而非统一框架

## 二、统一 Token 过期框架

### 2.1 设计原则

一个检测入口，按 token 来源分流，标记对应存储，统一 error_code 格式。

### 2.2 检测入口（fail_task 统一）

```
fail_task 检测到 401/auth 错误
  → _detect_token_expiry(task, repo_key, error)
    → 判断 token 来源（等价于现有 _is_user_token_repo + host_user_id 判断）：
      1. host_user_id > 0（现有 _is_user_token_repo=True）→ 用户 token（路径 B）→ _mark_user_token_expired
         （现有代码 if _hid > 0 分支保持不变，新增以下 else 分支）
      2. host_user_id == 0 + 有仓库级 bot_token → 仓库级 bot token → _mark_repo_bot_token_expired
      3. host_user_id == 0 + 无仓库级 bot_token → 全局 bot token → _mark_global_bot_token_expired
      4. devops_agent_session token → _mark_session_token_expired（R16.1.7）
    → 统一写 error_code（append 模式）
    → 统一通知逻辑

注意：改造方式是在现有 `if _hid > 0 and _expired_provider:` 分支后新增 else 分支，
      不修改原有路径 B 逻辑（R10 检测保持不变）。
```

### 2.3 统一 error_code

| token 来源 | error_code | message | 通知角色 |
|-----------|-----------|---------|---------|
| 用户 token | TOKEN_EXPIRED | user token expired or revoked | 用户（account 页提示重新绑定） |
| 全局 bot token | GLOBAL_TOKEN_EXPIRED | global bot token expired or revoked | 所有受影响用户 + 管理员 |
| 仓库级 bot token | REPO_TOKEN_EXPIRED | repo bot token expired or revoked | 该仓库用户 + 管理员 |
| 无可用 token | NO_TOKEN_AVAILABLE | no token available for this repo | 该仓库用户 + 管理员（dashboard 显示"无可用 Token，请联系管理员配置"） |

所有 error_code 走 append 模式写入 task errors（R14.18），保留原始错误详情。

**通知角色说明**：
- 用户 token 过期：只有该用户受影响，account 页提示重新绑定即可恢复
- 全局 bot token 过期：所有路径 A 仓库的用户都受影响（task 全部 401 失败）。用户在 dashboard/task 详情看到"全局 Bot Token 已过期，请联系管理员重新配置"提示；管理员在 settings 页看到全局 bot token 状态为"已过期"
- 仓库级 bot token 过期：只有该仓库的用户受影响。用户在 dashboard/task 详情看到"仓库 Bot Token 已过期，请联系有权限的成员重新配置"提示；有仓库权限的用户在 settings 页看到该仓库 bot token 状态为"已过期"

### 2.4 统一标记位置

| token 来源 | 标记方式 | 清除方式 |
|-----------|---------|---------|
| 用户 token | external_identities.status=4 | 用户重新绑定（create/update_external_identity 恢复 status=1） |
| 全局 bot token | meta.global_bot_token_status=expired | save_config 更新 bot token 时清除 |
| 仓库级 bot token | managed_repos.bot_token_status=expired（新列） | PUT /repos/bot-token 写入新 token 时清除 |

### 2.5 统一通知

#### 后端 API 返回

- `/auth/me`：返回 `token_expired_providers`（用户 token 过期，已有）
- `/settings/credentials` 或 `/settings/bot-status`：返回全局/仓库级 bot token 状态（管理员和有仓库权限的用户可见）
- task errors：统一 error_code 格式，所有用户在 task 详情中可见
- dashboard API：返回仓库 token 状态标记（全局过期时所有路径 A 仓库标记受影响）

#### 前端展示

- account 页：用户 token 过期提示（已有）
- settings 页：全局/仓库级 bot token 过期提示（管理员/有仓库权限的用户可见）
- dashboard：task failed 时显示 error_code 映射的本地化文案
  - TOKEN_EXPIRED → "你的 Token 已过期，请在设置页重新绑定"
  - GLOBAL_TOKEN_EXPIRED → "全局 Bot Token 已过期，请联系管理员重新配置"
  - REPO_TOKEN_EXPIRED → "仓库 Bot Token 已过期，请联系有权限的成员重新配置"

#### issue 回帖（best-effort）

- 路径 B 仓库：用全局 bot token 发评论（可能 403）
- 路径 A 仓库全局 token 过期 + 无仓库级 token：无可用 token 发评论 → 只靠 dashboard/settings 通知
- 路径 A 仓库全局 token 过期 + 有仓库级 token（未过期）：用仓库级 token 发评论
- 路径 A 仓库级 token 过期 + 全局 token 未过期：用全局 bot token 发评论
- 路径 A 仓库级 token 过期 + 全局 token 也过期：无可用 token 发评论 → 只靠 dashboard/settings 通知

### 2.6 短路机制

token 被标记过期后，`get_repo_token_with_refresh` 等函数检查过期标记，短路返回空串，避免每次都 401 重试：

- 用户 token status=4 → `_get_oauth_token_from_identity` 已返回 token（R10 设计意图，发评论 best-effort）
- 全局 bot token meta 标记过期 → `_get_global_bot_token` 检查标记，过期返回空串
- 仓库级 bot token managed_repos 标记过期 → `get_bot_token_by_repo` 检查标记，过期返回空串

### 2.7 健康检查 → 短路 → 无 token → fail_task 协调流程

当健康检查主动发现 token 过期时，完整的端到端链路：

```
健康检查发现 token 过期（R17.2）
  → 标记过期（meta / managed_repos）
  → 短路机制生效（R16.4）：_get_global_bot_token / get_bot_token_by_repo 返回空串
  → task 执行时 TokenResolver.get_token 所有策略返回空 → 返回空串
  → task fail（不是 401，是"无 token"）
  → fail_task 的 401 检测不触发（因为不是 401 错误）
  → fail_task 检测无 token → 写入 NO_TOKEN_AVAILABLE error_code（R16.6）
  → dashboard 显示"无可用 Token，请联系管理员配置"
```

当健康检查发现 token 恢复有效时：

```
健康检查发现 token 恢复有效（200）（R17.2.3 / R17.2.6）
  → 自动清除过期标记
  → 短路自动解除（R16.4 感知标记清除后重新返回 token）
  → task 正常执行
```

当管理员/用户手动重新配置 token 时：

```
save_config 更新全局 bot token（R16.3.2）
  → 清除 meta.global_bot_token_status 过期标记
  → 下次健康检查验证新 token → 200 → 确认标记已清除
  → 短路解除 → task 恢复

PUT /repos/bot-token 更新仓库级 token（R16.3.3）
  → 清除 managed_repos.bot_token_status 过期标记（R15 步骤 6）
  → 短路解除 → task 恢复
```

## 三、仓库级 Token 配置处理逻辑

### 3.1 问题

当前 `update_managed_repo_bot_token` 的执行顺序：

```
1. 检查仓库存在
2. _rebuild_webhook_with_bot_token（用传入 bot token 删旧 webhook + 建新 webhook）
3. _update_repo_bot_token（写 DB：bot_token + 清 host_user_id + 清 webhook_secret）
4. set_repo_bot_token 路由层：邀 bot 为协作者（用用户 token）
```

三个问题：
1. 顺序错误——bot 还没被邀请就去建 webhook，403
2. 不校验 token 有效性——错误 token 直接写 DB 导致全断
3. 部分失败——删旧成功建新失败，webhook 丢失 + host_user_id 被清 + 用户 token 降级被清

### 3.2 正确执行顺序

```
1. 检查仓库存在（不存在 → 404）
2. 验证 token 有效性（通过 provider.verify_token() 调 VCS API GET /user，无效 → 400 TOKEN_INVALID）
3. 获取 bot username（通过 provider.get_user_login()）
4. 邀 bot 为协作者（通过 provider.add_collaborator()，用用户 token / host 用户 token）
   - 新 bot 账号与旧 bot_username 不同 → 邀请新 bot
   - 相同 → 跳过邀请（已是协作者）
   - host 用户 token 过期 → 400 HOST_TOKEN_EXPIRED
   - 邀请失败 → 记日志继续（邀请幂等，bot 可能已是协作者）
5. 重建 webhook（通过 provider.remove_webhook() 删旧 + provider.setup_webhook() 建新）
   - 删旧失败 → 不建新，不写 DB，返回错误（保留原状态）
   - 删旧成功建新失败 → 不写 DB，返回 500 WEBHOOK_REBUILD_FAILED
   - 全部成功 → 继续
6. 写 DB（按 provider.token_storage_field() 分列写入：GitCode→bot_gitcode_token / GitHub→bot_github_token + 清 host_user_id + 清 webhook_secret + 写 webhook_id + 清 bot_token_status=空，即 R16.3.3 恢复逻辑）
7. 返回 200 + bot_username
```

### 3.3 部分失败保护原则

**任何步骤失败都不破坏现有状态**：
- token 无效 → 不写 DB、不重建 webhook、不清 host_user_id
- 邀 bot 失败 → 不重建 webhook、不写 DB
- webhook 重建失败 → 不写 DB（用户 token 降级仍可用）
- 写 DB 失败 → webhook 已重建但 DB 未更新，用户可 repair_host

### 3.4 与 auto_upgrade_path_b_repos 的关系

update_managed_repo_bot_token 有两个调用方：
1. set_repo_bot_token（手动配置入口）— R15 改造后走完整 7 步流程
2. auto_upgrade_path_b_repos（自动升级入口）— 自己已完成邀 bot + accept invite，然后调 update_managed_repo_bot_token 写 DB

R15 改造原则：**update_managed_repo_bot_token 内部不重复邀 bot 逻辑**。邀 bot 逻辑在调用方（set_repo_bot_token 路由层 / auto_upgrade）完成，update_managed_repo_bot_token 只负责"重建 webhook + 写 DB"。

这样 auto_upgrade 调 update_managed_repo_bot_token 时不会重复邀请——auto_upgrade 自己邀了 bot，update_managed_repo_bot_token 只做 webhook + DB。

set_repo_bot_token 路由层在调 update_managed_repo_bot_token 之前先完成邀 bot（R15 步骤 4），update_managed_repo_bot_token 只做步骤 5-6（webhook + DB）。

**向前兼容**：update_managed_repo_bot_token 函数签名不变（repo_key + bot_token → bool），内部逻辑保持"重建 webhook + 写 DB"不变。R15 改造的是 set_repo_bot_token 路由层的调用顺序，不是 update_managed_repo_bot_token 内部逻辑。

**R16.3.3 恢复逻辑落地**：_update_repo_bot_token 的 UPDATE SQL 需新增 `bot_token_status=''` 清除——当 token 重新配置时必须清除过期标记，否则短路仍生效导致 token 被写入但读取时返回空串。

### 3.5 场景覆盖

#### 全局 token 场景（路径 A → 路径 A，换 bot 账号）

仓库已通过全局 bot token 托管，有仓库权限的用户配不同的仓库级 bot token：
- 验证新 token → 获取新 bot username
- 新 bot 与旧 bot_username 不同 → 邀请新 bot 为协作者
- 新 bot 已是协作者 → 跳过邀请
- 旧 bot 可选移除协作者身份（best-effort）
- 用新 bot token 重建 webhook
- 写 DB：新 bot_token + 更新 bot_username

#### 用户 token 场景（路径 B → 路径 A，首次配 bot token）

仓库通过用户 token 托管，有仓库权限的用户配 bot token：
- 验证新 token → 获取 bot username
- 用 host 用户 token 邀 bot 为协作者
- host 用户 token 过期 → 400 HOST_TOKEN_EXPIRED
- 用 bot token 重建 webhook（bot 已是协作者，不会 403）
- 写 DB：bot_token + 清 host_user_id=0 + 清 webhook_secret + 写 webhook_id

## 四、需求项

### R15.1 配置仓库级 token 前先验证 token 有效性

- R15.1.1：调 VCS API（GitCode: GET /api/v5/user，GitHub: GET /user）验证 token 有效性
- R15.1.2：token 无效时返回 400 + error_code=TOKEN_INVALID，不写 DB、不重建 webhook、不清 host_user_id
- R15.1.3：token 有效时获取 bot username，继续后续流程

### R15.2 先邀 bot 为协作者，再用 bot token 重建 webhook

执行顺序：验证 token → 邀 bot → 重建 webhook → 写 DB

- R15.2.1：路径 B 仓库用 host 用户 token 邀请
- R15.2.2：路径 A 仓库如果新 bot 账号与旧 bot_username 不同，也需邀请新 bot
- R15.2.3：邀请失败时记日志但继续（邀请幂等，bot 可能已是协作者）
- R15.2.4：webhook 重建在邀请之后

### R15.3 webhook 重建部分失败时保护现有状态

- R15.3.1：删旧失败 → 不建新、不写 DB、返回错误（保留原状态）
- R15.3.2：删旧成功建新失败 → 不写 DB、返回 500 + WEBHOOK_REBUILD_FAILED
- R15.3.3：webhook 重建全部失败时不写 bot_token 到 DB，原状态不变

### R15.4 返回值语义

- R15.4.1：token 无效 → 400 + TOKEN_INVALID
- R15.4.2：仓库不存在 → 404
- R15.4.3：host 用户 token 过期 → 400 + HOST_TOKEN_EXPIRED
- R15.4.4：webhook 重建失败 → 500 + WEBHOOK_REBUILD_FAILED
- R15.4.5：全部成功 → 200 + bot_username

### R15.5 仓库级 token 清空/移除

- R15.5.1：PUT /repos/bot-token 传空串时清除仓库级 bot token
- R15.5.2：清除后不重建 webhook（用户需 repair_host）
- R15.5.3：清除后 get_repo_token_with_refresh 第 3 级命中全局 bot token
- R15.5.4：清除时同时清除 bot_token_status 过期标记（避免 settings 页误导用户以为 token 过期而非已清除）
- R15.5.5：清除时 best-effort 移除旧 bot 协作者身份（R15.6.3 best-effort 规则适用），不移除其他仓库的旧 bot

### R15.6 旧 bot 协作者移除

- R15.6.1：更换仓库级 bot token 时只移除该仓库的旧 bot 协作者身份
- R15.6.2：不移除其他仓库的旧 bot 协作者身份
- R15.6.3：移除失败 best-effort，不影响新 token 写入

### R16.1 统一 token 过期检测（fail_task 统一入口）

**需求描述**：fail_task 的 401 检测不再只针对路径 B，按 token 来源统一分流处理。

- R16.1.1：fail_task 401 检测判断 token 来源（用户/全局/仓库级/devops_agent_session 四级）
- R16.1.2：用户 token 过期 → 现有 R10 逻辑（标记 external_identities.status=4）
- R16.1.3：全局 bot token 过期 → 按 provider 验证全局 token 有效性（GitCode: GET /api/v5/user，GitHub: GET /user，复用 R15.1.1 逻辑），无效则标记 meta.global_bot_token_status=expired
- R16.1.4：仓库级 bot token 过期 → 按 provider 验证仓库级 token 有效性，无效则标记 managed_repos.bot_token_status=expired（新增列）
- R16.1.5：task errors 统一 append 模式写入 error_code（TOKEN_EXPIRED / GLOBAL_TOKEN_EXPIRED / REPO_TOKEN_EXPIRED）
- R16.1.6：幂等规则——已标记过期的 token 不重复标记、不重复写 error_code（避免每次 401 重试都 append）
- R16.1.7：第 1 级 devops_agent_session token 过期 → 标记 devops_agent_session 表 token_status=expired，短路返回空串。devops_agent_session 是 agent 专属配置（非用户直接管理），过期后由 agent 管理员重新配置；如未实现独立过期标记，则降级为不标记但记录日志，task 获取 token 时第 1 级查到过期 token 直接返回（由调用方 401 兜底）

### R16.2 统一过期通知

- R16.2.1：task errors 统一 error_code 格式，所有用户在 task 详情中可见
- R16.2.2：dashboard/task 详情显示 error_code 映射的本地化文案
- R16.2.3：settings 页返回全局/仓库级 bot token 状态
- R16.2.4：issue 回帖 best-effort（有可用 token 时发评论）

### R16.3 统一过期恢复

- R16.3.1：用户重新绑定 token → 恢复 external_identities.status=1（已有）
- R16.3.2：管理员 save_config 更新全局 bot token → 清除 meta 过期标记
- R16.3.3：有仓库权限的用户 PUT /repos/bot-token 更新仓库级 token → 清除 managed_repos 过期标记
- R16.3.4：新 token 写入后验证有效性（R15.1 逻辑）

### R16.4 短路机制

- R16.4.1：全局 bot token 标记过期后，_get_global_bot_token 短路返回空串（短路检查在 60s _cached_get_meta 缓存之后执行：先走缓存取 token，再查 meta.global_bot_token_status，如果 expired 则返回空串。集群下其他实例最多 60s 延迟感知过期，由 fail_task 401 兜底覆盖延迟窗口，用例 6.1）
- R16.4.2：仓库级 bot token 标记过期后，get_bot_token_by_repo 短路返回空串
- R16.4.3：用户 token status=4 保持现有行为（返回 token 供 best-effort 发评论）
- R16.4.4：devops_agent_session token 标记过期后（R16.1.7），第 1 级策略短路返回空串，降级到第 2 级仓库级 token。短路检查需 try-except 保护：如果 devops_agent_session 表无 token_status 列（未 ALTER），降级为不检查（返回 token，由调用方 401 兜底）
- R16.4.5：短路机制向前兼容——所有短路检查新增在现有逻辑之后，不修改原有 token 获取路径。未标记过期的 token 走原有逻辑完全不变

### R16.5 DB schema 变更

- R16.5.1：managed_repos 新增 bot_token_status 列（TEXT DEFAULT ''，空=正常，expired=过期），幂等 ALTER
- R16.5.2：meta 表新增 global_bot_token_status 键（空=正常/未检测，expired=过期）
- R16.5.3：devops_agent_session 新增 token_status 列（TEXT DEFAULT ''，空=正常，expired=过期），幂等 ALTER TABLE devops_agent_session ADD COLUMN token_status TEXT DEFAULT ''（try-except 保护，列已存在时忽略）。同时新增 repo_github_token 列（TEXT DEFAULT ''），幂等 ALTER TABLE devops_agent_session ADD COLUMN repo_github_token TEXT DEFAULT ''——GitHubProvider.session_token_field() 返回此列名，现有表只有 repo_gitcode_token
- R16.5.4：健康检查同时写两个 meta key——现有 bot_token_health（admin 页展示用，保留 healthy/degraded/unhealthy 三级状态）+ 新增 global_bot_token_status（短路机制用，expired=过期）。两个 key 独立，现有 admin 页读 bot_token_health 不受影响
- R16.5.5：managed_repos 新增 host_vcs_username 列（TEXT DEFAULT ''，幂等 ALTER）——托管时从 external_identities 同步写入 host 用户的 VCS 平台用户名（login 字段），供路径 B 自循环检测使用（R20.1.4）。每次 verify_token 时如果返回的 login 与 DB 不一致则更新（R20.1.5）

### R16.6 无 token 检测

- R16.6.1：task 获取不到 token 时 fail_task 写入 NO_TOKEN_AVAILABLE error_code
- R16.6.2：dashboard 显示"无可用 Token，请联系管理员配置"

## 五、路径 B Webhook 过滤修复

### 5.1 问题本质

路径 B（用户 token 驱动）下，agent 用用户 token 调 VCS API，所有 API 操作的 actor 是用户而非 bot。现有 webhook 过滤链路的自循环检测依赖 actor 是否在 bot_usernames 列表中——路径 B 的 host 用户不在该列表中，导致 agent 自身动作的 webhook 回声不被拦截，触发重复执行。

核心矛盾：路径 B 下 agent 和用户用同一个 token（用户 token），actor 完全相同（host_user VCS username）。任何仅基于 actor 的检测都无法区分"agent 回声"和"用户主动操作"。简单判断 `actor == host_vcs_username → 跳过` 会导致用户自己发评论也被拦截，无法触发流水线。

设计约束：
- 不能将 host_user 加入 bot_usernames（会拦截用户的正常操作，见场景 17.2）
- 不能依赖评论标记（agent 直接调 API 发的评论无标记，见场景 12.1）
- 不能仅基于 actor 判断（actor 相同无法区分，见场景 18）

### 5.2 解决方案：四条件组合判断

引入 issue 状态 + task 执行状态 + 开发者指令豁免的三条件组合判断。仅当四条全部满足时，才视为 agent 回声跳过：

1. 仓库是路径 B（host_user_id > 0）
2. actor == host_vcs_username（host 用户的 VCS 平台用户名）
3. event_type in (issue_comment, note_hook_pr, issue_updated)（agent 可能发的操作类型）
4. 该 issue/PR 有正在执行的 task（agent_tasks.status in (running, dispatched) 或 devops_agent_session.last_active_at > now-300s）

条件 4 不满足 → 说明没有 agent 在执行 → actor 的操作是用户主动操作 → 放行。

开发者指令豁免：评论内容含 /done /cancel /resume 等指令时，即使四条件满足也不拦截（用户指令优先，先于自循环检测处理）。

### 5.3 host_vcs_username 获取与同步

#### 5.3.1 获取方式

优先从 managed_repos.host_vcs_username 列读取（R16.5.5 新增列），为空时 fallback 到 external_identities 查询 host_user_id 对应的 provider username（VCS 平台 login 字段，不是 grape account username）。

```python
def get_host_vcs_username(repo_key, provider):
    """获取路径 B 仓库的 host 用户 VCS 平台用户名。
    优先从 managed_repos.host_vcs_username 读取（R16.5.5），
    为空时 fallback 到 external_identities 查询。"""
    # 1. 查 managed_repos.host_vcs_username
    username = get_managed_repo_field(repo_key, "host_vcs_username")
    if username:
        return username
    # 2. fallback: 从 external_identities 查 host_user_id 对应的 provider username
    host_user_id = get_host_user_id(repo_key)
    if host_user_id > 0:
        identity = get_external_identity(host_user_id, provider.provider_name)
        return identity.get("username", "") if identity else ""
    return ""
```

#### 5.3.2 同步时机

每次验证 token 有效性（provider.verify_token）时，如果返回的 login 与 DB 中的 host_vcs_username 不一致 → 更新 DB（用户改了 VCS 平台用户名的场景）。覆盖路径：
- 托管时从 external_identities 同步写入 host_vcs_username
- token 验证（provider.verify_token）返回 login 变更时更新
- 路径 B → 路径 A 升级时清空 host_vcs_username（host_user_id=0 时无意义）

#### 5.3.3 查询失败降级

如果 host_vcs_username 查询失败（external_identities 已删除等）→ 降级为不检测（不阻断 pipeline），记录 warning。

### 5.4 task 执行状态查询

```python
def check_running_task(repo_key, issue_id):
    """检查该 issue 是否有正在执行的 task（R20.1.8 + R20.10.5）。
    
    两级查询：
    1. agent_tasks.status=running/dispatched（webhook pipeline 触发的 task）
    2. devops_agent_session.last_active_at > now-300s AND status=active
       （飞书 VOD / Chat direct 等非 webhook 执行场景）
    """
    with db_lock:
        c = db.cursor()
        # 1. 查 agent_tasks
        c.execute(
            "SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? "
            "AND status IN ('running','dispatched') LIMIT 1",
            (repo_key, issue_id)
        )
        if c.fetchone():
            return True
        # 2. 降级：查 devops_agent_session 最近活跃
        c.execute(
            "SELECT 1 FROM devops_agent_session WHERE repo_key=? "
            "AND status='active' AND last_active_at > datetime('now', '-300 seconds') LIMIT 1",
            (repo_key,)
        )
        return c.fetchone() is not None
```

### 5.5 开发者指令检测

开发者指令检测移到自循环检测之前（pipeline 步骤顺序调整：2.0b 开发者指令 → 2.0 自循环检测），确保用户指令不被自循环检测误拦截。

```python
def check_developer_command(note_content):
    """检查评论内容是否含开发者指令（/done /cancel /resume 等）。"""
    note_lower = (note_content or "").lower().strip()
    return any(kw in note_lower for kw in [
        "/done", "完成任务", "任务完成", "确认完成",
        "/cancel", "取消任务",
        "/resume", "恢复任务"
    ])
```

### 5.6 pipeline 步骤调整

现有 pipeline 步骤顺序：
```
2.0 自循环检测（actor in bot_usernames）
2.0a 标记检测（grape-auto-comment）
2.0b 开发者指令检测
2.1 L1 过滤
2.2 路由
```

改为：
```
2.0b 开发者指令检测（移到最前，R20.6.2）
  → 含 /done /cancel /resume → 处理指令 → skip
2.0 自循环检测（通过 handler.is_self_loop_actor 调用，R18.6.8）
  → 路径 A：actor in bot_usernames → skip
  → 路径 B：四条件组合判断 → skip
2.0a 标记检测（grape-auto-comment）
  → 系统回帖含标记 → skip
2.1 L1 过滤
2.2 路由
```

### 5.7 需求项

### R20.1 路径 B agent 评论自循环修复

- R20.1.1：路径 B 自循环检测四条件——全部满足才跳过：
  1. 仓库是路径 B（host_user_id > 0）
  2. actor == host_vcs_username
  3. event_type in (issue_comment, note_hook_pr, issue_updated)
  4. 该 issue/PR 有正在执行的 task（agent_tasks.status in (running, dispatched) 或 devops_agent_session.last_active_at > now-300s AND status=active）
  条件 4 不满足 → 用户主动操作 → 放行
- R20.1.2：开发者指令豁免——评论含 /done /cancel /resume /完成任务 /取消任务 /恢复任务 时，即使四条件满足也不拦截（通过 2.0b 步骤优先处理）
- R20.1.3：host_vcs_username 从 external_identities 查 host_user_id 对应的 provider username（VCS 平台 login 字段）
- R20.1.4：host_vcs_username 缓存——managed_repos 新增 host_vcs_username 列（R16.5.5），托管时同步写入
- R20.1.5：host_vcs_username 同步——每次 provider.verify_token 返回 login 与 DB 不一致时更新 DB
- R20.1.6：向前兼容——路径 A 仓库（host_user_id == 0）不受影响，原有 bot_usernames 检测不变
- R20.1.7：查询失败降级——host_vcs_username 查询失败 → 降级为不检测，记录 warning
- R20.1.8：task 执行状态查询——查 agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('running','dispatched') LIMIT 1，有结果说明 agent 正在执行

### R20.2 路径 B fail_task 回帖降级处理

**问题**：路径 B 下 fail_task 无法发 Issue 评论（无全局 bot token + 用户 token 已过期）。

- R20.2.1：fail_task 路径 B 回帖失败时不静默——记录 warning 日志 + task errors 额外 append COMMENT_POST_FAILED
- R20.2.2：fail_task 路径 B 回帖失败时 dashboard 显示增强提示——"Token 已过期且无法发 Issue 评论，请重新绑定 Token 后在 dashboard 查看任务结果"
- R20.2.3：fail_task 路径 B 回帖失败时不重试（避免用过期 token 反复 401）
- R20.2.4：如果路径 B 仓库有全局 bot token（尚未 auto_upgrade 但已配置）→ fail_task 优先用全局 bot token 发评论（best-effort，可能 403）

**设计**：fail_task 评论 token 选择通过 handler.get_comment_token() 调用（R18.6.9），UserTokenHandler.get_comment_token 内部逻辑：
1. 用户 token 有效 → 用用户 token
2. 用户 token 过期 → 尝试全局 bot token（best-effort，可能 403）
3. 无可用 token → 返回空 → fail_task 记录 COMMENT_POST_FAILED

### R20.3 路径 B agent 执行期间 token 过期保护

**问题**：agent 执行耗时长，执行期间用户 token 可能过期，导致结果回帖失败或 git push 失败。

- R20.3.1：agent 执行前预检 token 有效性——如果 token 剩余有效期 < 预估执行时间，记录 warning 但不阻断（token 可能续期或任务提前完成）。PAT 无 expires_at，预检仅对 OAuth token 有效（检查 token_expires_at）
- R20.3.2：_post_result_to_issue 失败时（401）→ task errors append RESULT_POST_FAILED + dashboard 显示"结果已执行但无法回帖到 Issue，请在 dashboard 查看任务详情"
- R20.3.3：_post_result_to_issue 失败时不影响 task 状态——task 仍标记 completed（agent 确实完成了工作，只是回帖失败）
- R20.3.4：agent git push 失败时（401）→ fail_task 正常处理（已有 R10/R16.1 逻辑）

**设计**：_post_result_to_issue 改为通过 handler.post_issue_comment() 调用（R18.6.10），UserTokenHandler.post_issue_comment 内部 try-except 保护，失败时返回 False，调用方据此 append RESULT_POST_FAILED。

### R20.4 路径 B auto_upgrade webhook 重建保护

**问题**：auto_upgrade 删旧 webhook 可能因 bot 无 admin 权限或 host token 过期而失败。

- R20.4.1：auto_upgrade 遍历路径 B 仓库时，host 用户 token 过期 → 跳过并记录日志，仓库保持路径 B（已有行为，明确为设计意图）
- R20.4.2：auto_upgrade 删旧 webhook 失败时（bot 无 admin 权限）→ 不写 DB（不写 bot_token + 不清 host_user_id），仓库保持路径 B
- R20.4.3：auto_upgrade webhook 重建失败时 → 仓库保持路径 B，记录日志，管理员可手动 repair_host
- R20.4.4：auto_upgrade 成功标准：邀 bot + accept invite + webhook 重建 + DB 写入 全部成功才算升级，任一步失败则回滚到路径 B

**设计**：auto_upgrade_path_b_repos 内部对每一步的结果做检查，任一步失败则 continue 跳过该仓库，不写 DB。具体：
1. 获取 host 用户 token → 空 → skipped, continue（R20.4.1）
2. 邀 bot → 失败 → 记日志 continue（邀请幂等，可能已是协作者，继续尝试 webhook）
3. accept invite → 失败 → 记日志 continue
4. _rebuild_webhook_with_bot_token → 返回空（删旧/建新失败）→ 不写 DB, continue（R20.4.2/R20.4.3）
5. _update_repo_bot_token → 写 DB（只有前面全成功才执行）

### R20.5 路径 B webhook 创建权限提示

**问题**：非 admin 用户托管仓库时创建 webhook 403。

- R20.5.1：托管时如果 setup_webhook 返回 403 → 返回明确错误"需要仓库 admin 权限创建 webhook，请联系管理员托管或获取 admin 权限"
- R20.5.2：前端展示托管失败原因 + 指引文案

**设计**：托管逻辑（onboarding/repo register）中 setup_webhook 返回 403 时，返回 HTTP 403 + error_code=WEBHOOK_PERMISSION_DENIED + 指引文案。前端 i18n 映射该 error_code。

### R20.6 路径 B 自循环检测与路径 A 的一致性保证

- R20.6.1：pipeline 步骤 2.0 自循环检测改为通过 TokenActionHandler.is_self_loop_actor() 调用（R18.6.8），消除现有 if actor in bot_usernames + if _is_user_token_repo 散落分支
- R20.6.2：开发者指令检测（/done /cancel /resume）移到自循环检测之前（步骤 2.0b → 2.0，见 5.6 pipeline 步骤调整）
- R20.6.3：路径 B 自循环检测不影响用户正常操作——用户在 issue 上评论时，如果没有正在执行的 task（条件 4 不满足）→ 放行
- R20.6.4：路径 B 自循环检测的时序竞态——agent 完成执行到 task 状态更新为 completed 之间有窗口，期间 agent 发的评论可能被放行。可接受——最多多一次 pipeline 触发，由下次自循环检测兜底
- R20.6.5：如果 host_vcs_username 查询失败 → 降级为不检测，记录 warning

### R20.7 非 webhook 场景的 token 使用覆盖

**问题**：Chat 会话、飞书 IM、events redispatch、onboarding、project_info 等场景也使用 token，不经过 TokenResolver/TokenActionHandler，不享受短路/过期检测。

#### 29 个 token 使用场景全览

| 场景 | token 获取方式 | 改造方案 | 状态 |
|------|--------------|---------|------|
| webhook pipeline exec_agent | get_repo_token_with_refresh | → TokenResolver | 已覆盖 |
| webhook _post_result_to_issue | repo_gitcode_token | → Handler | 已覆盖 |
| webhook _post_result_to_pr | repo_gitcode_token | → Handler | 已覆盖 |
| webhook create_pr_for_repo | repo_gitcode_token | → TokenResolver | 已覆盖 |
| webhook SRE resolve_pr_issue_id | get_repo_token_with_refresh | → Repo.bot_token（R20.7.13） | 已覆盖 |
| webhook PR resolve_pr_issue_id | get_repo_token_with_refresh | → Repo.bot_token（R20.7.13） | 已覆盖 |
| webhook project_info builder | Repo.bot_token | → TokenResolver（R20.11） | 已覆盖 |
| webhook fail_task | comment_token | → Handler | 已覆盖 |
| Chat send_message | _resolve_gitcode_token | → resolve()+Handler（R20.7.2） | 已覆盖 |
| Chat ensure_issue_conversation | Repo(rk).bot_token | → TokenResolver（R20.11） | 已覆盖 |
| Chat list_messages | Repo(_rk).bot_token | → TokenResolver（R20.11） | 已覆盖 |
| Chat _enqueue_chat | gitcode_token | → resolve() | 已覆盖 |
| 飞书 sync_repo_feishu_mapping | get_repo_token_with_refresh | → TokenResolver（R20.7.3） | 已覆盖 |
| 飞书 VOD handler | agent_session.repo_gitcode_token → Repo.bot_token | → resolve()（R20.7.4） | 已覆盖 |
| Issues confirm_done close | get_repo_bot_token / get_github_token → with_oauth_token | → Repo.bot_token（R20.7.8） | 已覆盖 |
| Issues comment | get_github_token / get_repo_bot_token | → Repo.bot_token（R20.7.8） | 已覆盖 |
| agents start/restart | get_repo_token_with_refresh | → Repo.bot_token（R20.7.15） | 已覆盖 |
| settings repair_host | get_repo_token_with_refresh | → Repo.bot_token（R20.7.14） | 已覆盖 |
| settings verify token | _req / _github_req → GET /user | → provider.verify_token | 已覆盖 |
| settings update_credentials | 用户 token 绑定/验证 | 独立链路 | 无问题 |
| events redispatch | Repo.bot_token | → TokenResolver（R20.11） | 已覆盖 |
| events complete | Repo.bot_token | → TokenResolver（R20.11） | 已覆盖 |
| scheduler autopilot | get_github_token / get_gitcode_token | → Repo.bot_token（R20.7.9） | 已覆盖 |
| onboarding | Repo.bot_token | → TokenResolver（R20.11） | 已覆盖 |
| project_info builder | Repo.bot_token | → TokenResolver（R20.11） | 已覆盖 |
| templates read | get_global_*_bot_token | 独立（只读全局） | 无问题 |
| vcs/api_client create_client | get_gitcode_token | → Repo.bot_token（R20.7.10） | 已覆盖 |
| vcs/providers bot_token | get_gitcode_token / get_github_token | → Repo.bot_token（R20.7.11） | 已覆盖 |
| vcs/providers user_token | get_user_token_with_refresh | 不替代（R18.4.4） | 无问题 |
| deps.py get_bot_token | get_repo_token_with_refresh | → Repo.bot_token（R20.7.12） | 已覆盖 |
| scenes experience | with_oauth_token | 独立链路 | 无问题 |

#### token 场景分类模型（二维矩阵）

上表 29 个场景按"来源 × 消费方式"归入二维矩阵。同一格的场景可统一，跨格的不合并：

| 来源 \ 消费 | obtain（保证有效） | peek（原始值比对） |
|------------|------------------|-------------------|
| bot | 执行 token：pipeline exec_agent / project_info / onboarding / events redispatch / autopilot / Issues close&comment / list_repo_branches / vcs/api_client / vcs/providers / deps / Repo.bot_token | A2A 鉴权：get_repo_bot_token_no_global / 关联仓库信任域比对 |
| user | 身份 token：建单（issues_center create_issue）/ Chat（_resolve_gitcode_token）/ scenes experience / templates verify | — |
| config | — | webhook 验证（get_webhook_verify_token）→ **移出 token 体系** |

矩阵使用规则：
- 同一格的场景共享相同的来源和消费方式，可以统一到同一个方法
- 跨格的场景不能合并——消费方式不同（obtain 需要有效性+刷新，peek 只需要原始值）
- bot × obtain 是最大的一格（约 15 个场景），全部统一到 TokenResolver.get_token()
- bot × peek 只有 A2A 鉴权，用 TokenResolver.read_stored_token()
- user × obtain 是用户身份场景，用 get_user_token_with_refresh()（独立链路）
- config × peek 是 webhook 验证，不属于 token 体系

**不属于 token 体系的场景**（移出 token 体系边界讨论）：

- webhook 验证（get_webhook_verify_token）：语义是"secret 相等吗"的配置项比对，不是"获取可用 token"。查的是 webhook_secret / webhook_token_field（webhook 创建时的 password），不在 token 策略链中。放在 config/secret 模块更合适。保留在 R20.12 文档中是因为实现上与 token 存储有交集（webhook_token_field 列在 managed_repos 表中），但概念上不属于 token 体系。

- settings verify token（_req / _github_req → GET /user）：语义是"调 VCS API 做验证动作"，是动作不是 token 获取。验证结果决定是否写入 token，但验证本身不是 token 消费。

- scenes experience（with_oauth_token）：语义是"用户主动操作时用用户自己的 token"，走 with_oauth_token 独立链路（含 OAuth 预刷新 + 401 重试）。与 user × obtain 的其他场景（建单/Chat）语义相同，但实现链路不同（with_oauth_token 是封装层，不走 TokenResolver）。

- R20.7.1：Repo.bot_token 改为调 service 层方法 get_repo_bot_token()（R20.11），内部调 TokenResolver.get_token()——所有调 Repo.bot_token 的场景自动继承短路/过期检测
- R20.7.2：Chat _resolve_gitcode_token 改为调 TokenResolver.resolve() + Handler.get_comment_token()——Chat agent 注入的 token 经过过期检测和级别感知
- R20.7.3：飞书 sync_repo_feishu_mapping 改为调 TokenResolver.get_token()——sync mapping 享受短路（token 过期时跳过 sync 而非 401 报错）
- R20.7.4：飞书 VOD handler 改为调 TokenResolver.resolve()——VOD agent 注入的 token 经过级别感知
- R20.7.5：events redispatch / complete 改为调 Repo.bot_token（内部已委托 TokenResolver）
- R20.7.6：onboarding / project_info 改为调 Repo.bot_token（内部已委托 TokenResolver）——git clone/push 在 token 过期时短路返回空 → clone 失败有明确日志
- R20.7.7：scenes experience 保持 with_oauth_token（用户主动操作，不走 repo token 链）——但 token 过期时返回友好提示
- R20.7.8：Issues confirm_done close / comment 改为调 Repo.bot_token（内部已委托 TokenResolver）——close/comment 享受短路，token 过期时返回 bot_token_failed 而非 401
- R20.7.9：scheduler autopilot 改为调 Repo.bot_token（内部已委托 TokenResolver）——autopilot agent 注入的 token 经过短路检测
- R20.7.10：vcs/api_client create_client 改为调 Repo.bot_token（内部已委托 TokenResolver）——VcsApiClient 享受短路
- R20.7.11：vcs/providers bot_token 改为调 Repo.bot_token（内部已委托 TokenResolver）——vcs 层 token 获取统一入口
- R20.7.12：deps.py get_bot_token 改为调 Repo.bot_token（内部已委托 TokenResolver）——FastAPI Depends 链路统一
- R20.7.13：webhook SRE/PR resolve_pr_issue_id 改为调 Repo.bot_token——token 过期时短路返回空 → resolve_pr_issue_id 失败 → 降级为 resolve_issue_id（不调 API）或 evt.issue_id
- R20.7.14：settings repair_host 改为调 Repo.bot_token——repair 享受短路，token 过期时返回明确错误
- R20.7.15：agents start/restart 改为调 Repo.bot_token——写入 session.repo_gitcode_token 的 token 经过短路检测（过期 token 不写入 session）

### R20.8 Chat 场景 token 过期处理

**问题**：Chat agent 用用户 token 执行，token 过期时 agent 401 失败，Chat 没有 fail_task 逻辑，用户只看到 agent 报错。

- R20.8.1：Chat agent 执行返回 401/auth 错误时 → 检测是否 token 过期（复用 R16.1 检测逻辑，调 TokenResolver.detect_and_mark_expired）→ 返回友好提示"Token 已过期，请在设置页重新绑定 Token 后重试"
- R20.8.2：Chat agent 执行返回 401 但 token 验证有效 → 返回"权限不足或 Token 权限范围不够"
- R20.8.3：Chat agent token 过期提示在 Web Chat 界面显示（不是 Issue 评论）
- R20.8.4：Chat agent token 过期时不重试（避免用过期 token 反复 401）

**设计**：Chat send_message 内部，agent 执行结果包含 401/auth 错误时，调 TokenResolver.detect_and_mark_expired(repo_key) 获取 error_code。如果 error_code 非空 → 返回友好提示给前端。如果 error_code 为空（token 验证有效）→ 返回权限不足提示。

### R20.9 飞书场景 token 过期处理

**问题**：飞书 sync mapping / VOD agent 用用户 token，token 过期时操作静默失败。

- R20.9.1：飞书 sync_repo_feishu_mapping token 过期时（get_repo_token_with_refresh 返回空或 401）→ 跳过 sync + 飞书群发通知卡片"仓库 Token 已过期，协作者映射同步已跳过，请重新绑定 Token"
- R20.9.2：飞书 VOD agent token 过期时 → VOD agent 返回提示"Token 已过期，请在设置页重新绑定"→ 飞书发卡片通知用户
- R20.9.3：飞书场景 token 过期通知通过飞书消息卡片（不是 Issue 评论），因为飞书是独立通知渠道
- R20.9.4：飞书 VOD agent token 过期不重试

**设计**：
- sync_repo_feishu_mapping 调 TokenResolver.get_token() 返回空时，跳过 sync 并调飞书 send_card 通知群成员
- VOD handler 调 TokenResolver.resolve() 返回空 token 时，VOD agent 不启动，调飞书 send_card 通知用户

### R20.10 非 webhook agent 执行的自循环检测覆盖

**问题**：飞书 VOD agent、Chat direct 模式、scheduler autopilot 不走 agent_tasks 表 → 自循环检测条件 4（check_running_task）查不到 → agent 评论触发重复执行。

- R20.10.1：飞书 VOD agent 执行时在 agent_tasks 表创建 task 行（status=running）→ 自循环检测条件 4 可查到
- R20.10.2：Chat direct 模式执行时在 agent_tasks 表创建 task 行（status=running）
- R20.10.3：scheduler autopilot 执行时在 agent_tasks 表创建 task 行（status=running）（P0 修复）
- R20.10.4：非 webhook agent 执行完成后更新 task status=completed → 条件 4 不满足 → 用户后续评论可正常触发
- R20.10.5：条件 4 补充查询——除 agent_tasks.status=running/dispatched 外，也查 devops_agent_session.last_active_at > now-300s AND status=active（覆盖非 webhook agent 执行场景）
- R20.10.6：Chat worker 模式（_enqueue_chat）低风险——Chat agent 通常不调 VCS API，如 skill 有 VCS 操作则走 R20.10.2 创建 task 行

**设计**：
- 飞书 VOD handler 启动 agent 前 INSERT agent_tasks (repo_key, issue_id, status='running')，agent 完成后 UPDATE status='completed'
- Chat direct 模式同理
- autopilot 同理
- check_running_task 两级查询（见 5.4）覆盖 agent_tasks 和 devops_agent_session

### R20.11 Repo.bot_token 统一入口

**问题**：Repo.bot_token 是所有非 pipeline 场景的 token 入口，但未纳入 TokenResolver 体系。

- R20.11.1：Repo.bot_token 改为调 service 层方法 get_repo_bot_token(repo_key)（返回 str，签名不变）
- R20.11.2：所有调 Repo.bot_token 的场景（chat/飞书/events/onboarding/project_info）自动继承短路/过期检测，无需改动调用方
- R20.11.3：Repo.bot_token 返回空串时（token 过期短路）→ 调用方应处理空 token 场景（git clone 失败有明确日志，agent 注入空 token 有 warning）
- R20.11.4：需要级别信息的调用方（Chat _resolve_gitcode_token / 飞书 VOD handler）改为调 TokenResolver.resolve() 获取 TokenResult + Handler
- R20.11.5：Repo 是数据模型（domain object），不在属性内部直接 import TokenResolver——避免数据模型反向依赖 resolver（TokenResolver 内部策略类可能反向引用 Repo 查 host_user_id，形成循环依赖）。改为 service 层方法注入

**设计**：

Repo 保持数据模型纯净，不持有 resolver 引用。token 获取逻辑放 service 层：

```python
# src/repo.py — 数据模型，不 import TokenResolver
class Repo:
    """仓库领域对象。只封装仓库元数据（owner/name/provider/api_base/clone_url）。
    不持有 token resolver 引用——避免数据模型 → resolver → 策略类 → Repo 循环依赖。"""
    @property
    def bot_token(self) -> str:
        """委托 service 层获取 token。签名不变（str → str），向后兼容。"""
        from src.token_service import get_repo_bot_token  # service 层方法
        return get_repo_bot_token(self.repo_key)

# src/token_service.py — service 层，桥接 Repo 和 TokenResolver
def get_repo_bot_token(repo_key: str) -> str:
    """获取仓库可用执行 token。供 Repo.bot_token 和其他调用方使用。"""
    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
    return TokenResolver(provider).get_token(repo_key)
```

调用方无需改动——Repo.bot_token 签名不变（str → str），内部从直接查 DB 改为委托 service 层 → TokenResolver。

### R20.12 webhook 验证 token 获取（P0 补充，不属于 token 体系）

**问题**：webhook 收到 VCS 推送时，需要验证 X-Bot-Token header。验证用的 token 是"webhook 创建时用的 password"——路径 A 是 bot token，路径 B 是随机密钥（webhook_secret）。TokenResolver 的策略链不包含 webhook_secret，不能用于 webhook 验证。

**归属说明**：webhook 验证的语义是"secret 相等吗"的配置项比对，不是"获取可用 token"。概念上不属于 token 体系，应放在 config/secret 模块。本文档保留 R20.12 是因为实现上与 token 存储有交集（webhook_token_field 列在 managed_repos 表中），需明确边界。

**当前代码**：webhook.py 用 get_repo_bot_token_no_global 验证 → 查 managed_repos.bot_gitcode_token + devops_agent_session + 用户 token（跳过全局），不查 webhook_secret。

**设计**：webhook 验证保留独立逻辑，不走 TokenResolver。验证链：
1. 查 managed_repos.webhook_secret（路径 B 随机密钥，优先）
2. webhook_secret 为空 → 查 gitcode_webhook_token / github_webhook_token（路径 A bot token 写入的 webhook password）
3. 以上都为空 → fallback 到 get_repo_bot_token_no_global（兼容旧数据）

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

- R20.12.1：webhook 验证不走 TokenResolver，走独立的 get_webhook_verify_token()
- R20.12.2：验证优先级：webhook_secret → webhook_token_field → get_repo_bot_token_no_global（兼容旧数据）
- R20.12.3：短路机制不影响 webhook 验证——即使 bot token 过期短路，webhook 验证仍用 webhook_secret / webhook_token 验证
- R20.12.4：路径 B 仓库 webhook 验证用 webhook_secret（随机密钥），与 token 过期状态无关
- R20.12.5：验证 token（get_webhook_verify_token）与鉴权 token（read_stored_token）本质都是"原始值比对"，但不合并为同一个方法。原因：数据源不同——验证 token 查 webhook_secret / webhook_token_field（webhook 创建时的 password，不在 token 策略链中），鉴权 token 查策略链（bot_gitcode_token / devops_agent_session / 用户 token）。合并需要传 source 参数区分数据源，又是参数地狱。两个方法各自语义清晰，不合并

### R20.13 A2A 鉴权 token 保留独立逻辑（P0 补充）

**问题**：A2A 鉴权（webhook.py / events.py）用 get_repo_bot_token_no_global 比对请求的 Authorization header。token 过期短路返回空串会导致鉴权失败 → agent 之间无法通信。

**当前代码**：webhook.py line 302-318 用 get_repo_bot_token_no_global 做 bot token 比对；events.py line 969, 1281 同理。还有关联仓库信任域比对（两仓 bot token 是否相同）。

**设计**：A2A 鉴权用 TokenResolver.read_stored_token() 获取存储原始值，不走短路。

- R20.13.1：get_repo_bot_token_no_global 保留为薄包装，内部调 TokenResolver.read_stored_token()（不是 get_token()）
- R20.13.2：read_stored_token() 是独立方法——只调策略链的 _fetch()（纯读存储值），不调 _is_expired()（不检查过期），不触发 OAuth refresh。与 get_token() 共享底层策略链但走不同检查路径
- R20.13.3：A2A 鉴权即使 token 过期也返回原始值用于比对（鉴权不依赖 token 有效性，只依赖 token 一致性）
- R20.13.4：关联仓库信任域比对（webhook.py line 318）同样用 read_stored_token()——比对两仓 token 是否相同，不依赖 token 有效性
- R20.13.5：session token 鉴权（verify_session_token_for_repo）不受影响，走独立链路
- R20.13.6：不使用 skip_expired_check 参数——布尔 flag 会掩盖语义差异，用独立方法 read_stored_token() 让方法名表达意图，调用方不可能传错

```python
def get_repo_bot_token_no_global(repo_key: str) -> str:
    """A2A 鉴权用：不含全局兜底，读取存储原始值（不走短路）。"""
    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
    resolver = TokenResolver(provider)
    resolver.strategies = [resolver.strategies[1], resolver.strategies[0], resolver.strategies[3]]  # 跳过全局
    return resolver.read_stored_token(repo_key)
```

### R20.14 建单场景保留用户 token（P0 补充）

**问题**：GitCode 建单（issues_center.py create_issue）必须用用户自己的 token——assign 需用户身份，bot token 无 assign 权限会 403。TokenResolver 策略链优先返回 bot token（路径 A），会导致建单 403。

**当前代码**：issues_center.py line 1296-1304，GitCode 走 get_user_token_with_refresh（用户 token），GitHub 走 get_github_token（repo token）。

**设计**：建单场景不走 TokenResolver，保留 get_user_token_with_refresh。

- R20.14.1：issues_center create_issue GitCode 路径保留 get_user_token_with_refresh（用户身份建单）
- R20.14.2：issues_center create_issue GitHub 路径可用 Repo.bot_token（GitHub assign 无用户身份限制）
- R20.14.3：建单时 token 缺失/失效 → 明确引导用户重新绑定，不 fallback 到 bot token
- R20.14.4：此场景不受 TokenResolver 改造影响，保持现有逻辑

### R20.15 event dialogue 自循环检测覆盖（P0 补充）

**问题**：event dialogue（事件对话，events.py line 1398/1774）用 Repo(repo_key).bot_token 获取 token 注入 agent。event dialogue 不走 agent_tasks 表 → 自循环检测条件 4 查不到 → agent 评论触发重复执行。

**设计**：同 R20.10（飞书 VOD / Chat direct / autopilot），event dialogue 也需创建 agent_tasks 行。

- R20.15.1：event dialogue 执行时在 agent_tasks 表创建 task 行（status=running）→ 自循环检测条件 4 可查到。**R23.2 修正**：event dialogue 是内存执行架构（_conv_tasks 字典 + run_in_executor），不创建 DB task 行。改为 R20.15.4 session heartbeat 方案，效果等价
- R20.15.2：event dialogue 完成后更新 task status=completed → 条件 4 不满足 → 用户后续评论可正常触发。**R23.2 修正**：改为更新 devops_agent_session.status='idle'
- R20.15.3：event dialogue token 获取改为 Repo.bot_token（内部已委托 TokenResolver，R20.11）
- R20.15.4（R23.2 补充）：event dialogue 启动时更新 devops_agent_session.last_active_at + status='active' → 让 check_running_task session 兜底查询生效
- R20.15.5（R23.2 补充）：event dialogue 完成时更新 devops_agent_session.status='idle' → 兜底不再命中 → 用户后续评论可正常触发流水线（R21.7.1）

### R20.16 setup_webhook password 缺省行为修正（P1 补充）

**问题**：setup_webhook（repo_manager.py line 560-563）的 webhook_password 参数为空时，fallback 到 get_repo_token_with_refresh 做 password。路径 B 仓库应传随机密钥（webhook_secret），不应 fallback 到 token。

**设计**：取消 setup_webhook 的 token fallback 缺省行为。

- R20.16.1：setup_webhook 必须显式传 webhook_password 参数（路径 A 传 bot token，路径 B 传随机密钥）
- R20.16.2：webhook_password 为空时不再 fallback 到 get_repo_token_with_refresh，改为报错或用空串
- R20.16.3：托管逻辑（repos.py register_repo）负责生成随机密钥并传入 setup_webhook

### R20.17 VOD handler restart 重新获取 token（P1 补充）

**问题**：VOD handler restart（vod_handler_async.py line 488/515/528）从 devops_agent_session 表读 repo_gitcode_token 直接注入 agent。如果 token 已过期但 session 中仍存储旧 token，restart 后 agent 用过期 token → 401。

**设计**：VOD handler restart 时重新调 TokenResolver 获取最新 token。

- R20.17.1：VOD handler restart 不从 session 读旧 token，改为调 TokenResolver.resolve() 获取最新 token
- R20.17.2：session 中的 repo_gitcode_token 仍保留（兼容 A2A 鉴权），但 VOD handler 不直接使用
- R20.17.3：token 过期短路时 VOD handler 获取空 token → 不启动 agent → 飞书通知用户（R20.9.2）

### R20.18 session token 写入不走短路（P1 补充）

**问题**：agents start/restart 和 session_store 启动时调 get_repo_token_with_refresh 获取 token 写入 devops_agent_session.repo_gitcode_token。如果写入时走短路返回空串，session 中 token 被清空，VOD handler fallback 到 Repo.bot_token 时也会被短路。

**设计**：session token 写入时用 read_stored_token() 读取原始值，不走短路；短路仅在运行时读取（get_token）时生效。

- R20.18.1：agents start/restart 写入 session.repo_gitcode_token 时调 TokenResolver.read_stored_token()，写入原始 token 值（不过期检查、不刷新）
- R20.18.2：短路机制仅在运行时 token 获取（TokenResolver.get_token）时生效——write 用 read_stored_token，read 用 get_token
- R20.18.3：session 中的 token 即使已过期也保留原值，供 A2A 鉴权（R20.13）和 VOD restart fallback 使用
- R20.18.4：健康检查标记过期后，session 中的 token 不清除（只标记 managed_repos.bot_token_status / meta.global_bot_token_status）

### R20.19 token 短路后 task fail 的 error_code 检测（P1 补充）

**问题**：token 短路后返回空串 → agent git clone/push 失败 → error 可能不含 "401 unauthorized" 关键词（可能是 "Authentication failed" 或 "could not read Username"）→ fail_task 的 401 检测不触发 → 不写 NO_TOKEN_AVAILABLE。

**设计**：fail_task 新增空 token 检测，不依赖 401 关键词匹配。

- R20.19.1：fail_task 检测到 task 获取 token 为空时（get_task_token 返回空串），直接写入 NO_TOKEN_AVAILABLE error_code
- R20.19.2：空 token 检测在 401 关键词检测之前执行（短路优先）
- R20.19.3：空 token 检测不调 VCS API 验证（token 已为空，无需验证）
- R20.19.4：git clone/push 失败的 error 中含 "Authentication failed" / "could not read Username" / "fatal: Authentication" 时，也触发 token 过期检测（扩展 401 关键词列表）

### R20.20 其他遗漏场景补充（P1/P2）

- R20.20.1：submit_feedback 检查 bot_token_available 改为调 Repo.bot_token（R20.11 已覆盖，自动继承短路）
- R20.20.2：templates verify 保留 get_user_token_with_refresh（用户主动操作，独立链路，不走 TokenResolver）——修正 R20.7 表格中"templates read"描述为"templates read 用全局 bot token（独立只读）；templates verify 用用户 token（独立链路）"
- R20.20.3：events terminate → fail_task 路径：terminate 是用户手动终止，error="用户手动终止"不含 401 关键词，不触发 token 过期检测。如果 terminate 恰好发生在 agent 401 失败后，fail_task 的 401 检测会触发（幂等，已标记不重复）
- R20.20.4：list_repo_branches → get_github_token 改为 Repo.bot_token（R20.11 已覆盖）。token 过期时短路返回空 → 前端显示"GitHub Bot Token 未配置或已失效"
- R20.20.5：_post_subtask_completion_to_parent → 用 bot_token 发评论，改为通过 handler.post_issue_comment()（R18.6.10 已覆盖）
- R20.20.6：Chat _resolve_gitcode_token 保留 user_token 优先逻辑——Chat agent 需用户身份执行（assign 等 VCS 操作需用户 token）。注意：Chat 的 token 语义与执行 token 相同（都需要有效 token），但策略优先级不同——标准 TokenResolver 是 bot-first（session → repo_bot → global → user），Chat 需要 user-first。不合并的原因是避免 TokenResolver.get_token() 增加 strategy_mode 参数导致参数膨胀，属于"策略差异为避免参数地狱而独立"，不是"语义不同导致必须独立"。_resolve_gitcode_token 内部：user_token 非空 → 返回 user_token；user_token 为空 → fallback Repo.bot_token（TokenResolver）

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

注意：2.0a 在 2.0 之前执行——系统回帖标记优先拦截，确保 agent 回帖不会进入自循环检测。2.0b 在最前——开发者指令优先于一切。

### R20.22 路径 B 判断方式修正（设计逻辑补充）

设计文档 R20.1.1 条件 1 原写"仓库是路径 B（host_user_id > 0）"，但代码中路径 B 判断用 _is_user_token_repo / _is_github_user_token_repo，不只是 host_user_id > 0——还检查 bot_gitcode_token / bot_github_token 是否为空。

修正：条件 1 改为"仓库是路径 B（_is_user_token_repo(repo_key) == True 或 _is_github_user_token_repo(repo_key) == True）"。

_is_user_token_repo 的判断逻辑：host_user_id > 0 AND bot_gitcode_token 为空。如果仓库同时有 host_user_id > 0 和 bot_gitcode_token（异常状态），_is_user_token_repo 返回 False（有 bot token 不是路径 B）。

### R20.23 fail_task 去重逻辑保留在 handler 外部（设计逻辑补充）

当前 fail_task 的 comment_token 选择逻辑包含 _fetch_issue_comments 去重（查 issue 是否已有"Token 已过期"评论）。设计 R18.6.9 说改为 handler.get_comment_token()。

修正：handler.get_comment_token() 只负责 token 选择（用户 token → 全局 bot fallback → 空），_fetch_issue_comments 去重逻辑保留在 fail_task 调用方层面，不进入 handler。原因：去重逻辑依赖 issue 已有评论内容（VCS API 调用），不属于 token 操作职责。

### R20.24 token 注入链路（P0 补充）

**问题**：TokenResolver 改造只覆盖了"从 DB 获取 token"，未覆盖"把 token 注入 agent 进程"的链路。agent 进程通过 3 层链路获取 token：

1. task 行写入 repo_gitcode_token（pipeline 创建 task 时，_get_task_repo_token）
2. env_extra 注入 BOT_GITCODE_TOKEN / BOT_GITHUB_TOKEN 环境变量（runtime/agent.py line 1878: bot_token=_get_task_token(task)）
3. GIT_ASKPASS 脚本从环境变量读 token 做 git 认证（repogit.py line 270-289: _write_askpass）

**路径 B 仓库的 bug**：

路径 B 仓库 task 行 repo_gitcode_token=空（防过期 token 落库）→ agent 运行时 BOT_GITCODE_TOKEN=空 → GIT_ASKPASS 脚本读 BOT_GITCODE_TOKEN=空 → git clone/push 失败。

当前 GIT_ASKPASS fallback 链（repogit.py line 283）：
```
github:  ${BOT_GITHUB_TOKEN:-${BOT_TOKEN:-$BOT_GITCODE_TOKEN}}
gitcode:  ${BOT_GITCODE_TOKEN:-${BOT_TOKEN:-$BOT_GITHUB_TOKEN}}
```
只读 BOT_*_TOKEN 和 BOT_TOKEN，不 fallback 到 USER_*_TOKEN。路径 B 仓库 USER_GITCODE_TOKEN 有值（用户 token）但 GIT_ASKPASS 不读它。

**设计**：

- R20.24.1：token 注入链路 3 层全覆盖：task 行 → env_extra → GIT_ASKPASS。TokenResolver 改造后每层都要适配
- R20.24.2：env_extra 注入 BOT_GITCODE_TOKEN / BOT_GITHUB_TOKEN 时，如果 TokenResolver.get_token() 返回空串（短路）→ env 变量为空 → GIT_ASKPASS 读空 → 需 fallback 到 USER_*_TOKEN
- R20.24.3：GIT_ASKPASS fallback 链修正——路径 B 仓库 BOT_*_TOKEN 为空时 fallback 到 USER_*_TOKEN：
  ```
  github:  ${BOT_GITHUB_TOKEN:-${USER_GITHUB_TOKEN:-${BOT_TOKEN:-$BOT_GITCODE_TOKEN}}}
  gitcode:  ${BOT_GITCODE_TOKEN:-${USER_GITCODE_TOKEN:-${BOT_TOKEN:-$BOT_GITHUB_TOKEN}}}
  ```
- R20.24.4：agent 同时收到 bot_token 和 user_token 两个参数（runtime/agent.py line 1878-1879）——bot_token 注入 BOT_*_TOKEN，user_token 注入 USER_*_TOKEN。路径 B 仓库 bot_token=空但 user_token 非空 → GIT_ASKPASS fallback 到 USER_*_TOKEN → git clone 成功
- R20.24.5：env_extra 注入逻辑不经过 TokenResolver——env 变量值来自 task 行 repo_gitcode_token（bot_token）和 runtime 实时获取（user_token）。TokenResolver 改造影响 task 行写入的 repo_gitcode_token 值，不影响 env 注入逻辑本身
- R20.24.6：路径 B 仓库 BOT_*_TOKEN=空 时不报错——agent 通过 grape CLI（GRAPE_SESSION_TOKEN）调 API 不依赖 BOT_*_TOKEN，只有 git clone/push 依赖 GIT_ASKPASS。GIT_ASKPASS fallback 到 USER_*_TOKEN 保证 git 操作可用

### R20.25 token 脱敏全覆盖（P0 补充）

**问题**：当前 result_text 脱敏只 replace repo_gitcode_token（event_pipeline.py line 3310, 3418），不脱 user_token。agent 可能在输出中复述 USER_GITCODE_TOKEN 的值 → 写入 agent_tasks.result_text → dashboard 可见 → token 泄露。

**当前代码**：
```python
# event_pipeline.py line 3418
if repo_gitcode_token and result_text:
    result_text = result_text.replace(repo_gitcode_token, "[REDACTED]")
# 只脱 repo_gitcode_token，user_token 未脱
```

**设计**：

- R20.25.1：result_text 脱敏覆盖所有注入的 token 值——repo_gitcode_token + user_token
- R20.25.2：agent_logs 脱敏同步覆盖（line 3310 的 _msg.replace 也脱 user_token）
- R20.25.3：fail_task 的 comment_token 脱敏——fail_task 发 Issue 评论时评论内容不含 token 原文（已有 grape-auto-comment 标记，但评论内容可能含 token 复述）
- R20.25.4：脱敏列表从 task 行动态构建——repo_gitcode_token（task 行值）+ user_token（runtime/agent.py 实时获取值），两者都可能被 agent 复述
- R20.25.5：R19.1.3 "error_code 中不包含 token 片段"已有，但脱敏范围需扩展到 result_text / agent_logs 的所有 token 值

```python
# 修正后脱敏逻辑
_redact_tokens = []
if repo_gitcode_token:
    _redact_tokens.append(repo_gitcode_token)
if user_token:
    _redact_tokens.append(user_token)
for _tok in _redact_tokens:
    result_text = result_text.replace(_tok, "[REDACTED]")
    _msg = _msg.replace(_tok, "[REDACTED]")
```

### R20.26 关联仓库 token 独立获取（P1 补充）

**问题**：关联仓库（linked repos, Issue #242）用主仓库 token 做所有 git 操作（ensure_repo_base_multi token=token, _submit_direct_linked_prs repo_gitcode_token=token）。但关联仓库可能在不同 VCS 平台（主仓库 gitcode，关联仓库 github）或不同 owner → 主仓库 token 不匹配 → clone/push 403/401。

**当前代码**：
- repogit.py line 155-170: ensure_repo_base_multi(linked_repos, token=token) 用主仓库 token
- event_pipeline.py line 3503: _submit_direct_linked_prs(env_extra, repo_key, issue_id, repo_gitcode_token) 用主仓库 token
- repogit.py line 177: ensure_linked_repo_worktrees(token=token) 用主仓库 token

**设计**：

- R20.26.1：关联仓库 token 独立获取——每个关联仓库用各自的 Repo(repo_key).bot_token 获取 token，不复用主仓库 token
- R20.26.2：ensure_repo_base_multi 改为接收 token_map: dict[repo_key, str]（每个仓库独立 token），而非单一 token 参数
- R20.26.3：_submit_direct_linked_prs 遍历关联仓库时，对每个仓库调 Repo(repo_key).bot_token 获取该仓库的 token
- R20.26.4：关联仓库 token 短路时（过期）→ 该仓库 clone/push 跳过 + 记录 warning，不影响主仓库 PR
- R20.26.5：关联仓库可能在不同 VCS 平台——Repo.bot_token 内部按 repo_key 前缀自动分流 provider，已有逻辑覆盖

### R20.27 OAuth refresh 分布式锁（P1 补充）

**问题**：双主集群（master-001 + master-002）下两实例同时刷新同一用户 OAuth token → 竞态 → 旧 refresh_token 被覆盖 → 后刷新的请求拿到新 token 但旧 token 已失效。

**当前代码**：deps.py line 17-18 用进程内 threading.Lock（_gitcode_refresh_locks），只防单实例内并发，不防跨实例。

**设计**：

- R20.27.1：OAuth refresh 加分布式锁（run_with_distributed_lock），锁名 `oauth_refresh:{user_id}:{provider}`，多实例只有一个执行 refresh
- R20.27.2：refresh 前先读 external_identities 获取最新 token_expires_at——如果其他实例已刷新成功（token_expires_at 更新），当前实例跳过刷新
- R20.27.3：刷新失败时不清除旧 token（保留最后已知有效值）
- R20.27.4：GitHub OAuth refresh（get_user_github_token_with_refresh）同样加分布式锁
- R20.27.5：分布式锁超时 30s——OAuth refresh 通常 < 5s，30s 超时防止死锁

### R20.28 飞书 direct 模式 user_token 获取（P1 补充）

**问题**：飞书 direct 模式 consume_stream_async(user_token="") → user_token 始终为空。agent 如果需要用户身份操作（assign issue 等）会 403。

**当前代码**：handler_async.py line 1650: user_token=""。飞书 direct 模式不调 get_user_token_with_refresh。

**设计**：

- R20.28.1：飞书 direct 模式启动前获取用户 token——调 get_user_token_with_refresh(username, user_id) 获取飞书用户的 VCS token
- R20.28.2：用户 token 注入 consume_stream_async 的 user_token 参数 → env 注入 USER_GITCODE_TOKEN / USER_GITHUB_TOKEN
- R20.28.3：token 过期时（get_user_token_with_refresh 返回空）→ agent 无用户身份 → agent 调 VCS API 403 时返回友好提示
- R20.28.4：飞书 queue 模式同理——enqueue_task 前获取用户 token

### R20.29 redispatch 时 token 刷新（P1 补充）

**问题**：redispatch 创建新 task 时，_get_task_repo_token 重新获取 token（正确）。但 _redispatch_vod_after_recommend 传递的 repo_gitcode_token 参数是旧 task 的 token（可能已过期），虽然 pipeline 内部会重新获取覆盖，但参数传递冗余且可能误导。

**当前代码**：event_pipeline.py line 3371: _redispatch_vod_after_recommend(..., repo_gitcode_token) 传旧 token。line 3905: 函数签名接收 repo_gitcode_token 但内部通过 _enqueue_pipeline_task 重新获取。

**设计**：

- R20.29.1：redispatch 创建新 task 时通过 _get_task_repo_token 重新获取 token（已有逻辑，正确）
- R20.29.2：_redispatch_vod_after_recommend 移除 repo_gitcode_token 参数（冗余，pipeline 内部重新获取）
- R20.29.3：events redispatch 调 Repo.bot_token 获取新 token（R20.7.5 已覆盖）
- R20.29.4：redispatch 时如果 token 短路返回空 → 新 task 行 repo_gitcode_token=空 → agent 运行时实时获取（同路径 B 逻辑）→ 如果也短路 → fail_task NO_TOKEN_AVAILABLE

## 五补、R21 修订章（矛盾修复与遗漏补充）

### R21.1 全局 bot token 更换场景（A1 级矛盾修复）

**问题**：管理员把全局 bot token 从 bot_old 换成 bot_new（有→有）时：
1. auto_upgrade 不触发（save_config 只检测"从无到有"）→ 新 bot 未被邀请为任何路径 A 仓库的协作者 → 所有仓库 403
2. 所有路径 A 仓库 webhook 的 password 仍是旧 bot token → webhook 验证不匹配 → 事件断链
3. 旧全局 bot 在所有仓库的协作者身份未清理

**设计**：save_config 检测全局 bot token 值变更（不只是"从无到有"），触发全局 bot 更换流程。

- R21.1.1：save_config 检测全局 bot token 值变更（old_token != new_token 且 old_token 非空）→ 触发 global_bot_token_swap 流程
- R21.1.2：global_bot_token_swap 流程：
  1. 用新 bot token 调 VCS API GET /user 验证有效性 → 无效则拒绝配置（R15.1 逻辑）
  2. 获取新 bot username
  3. 遍历所有路径 A 仓库（host_user_id == 0 且 bot_gitcode_token==旧全局 token 或 bot_github_token==旧全局 token）
  4. 对每个仓库：用新 bot token 邀请新 bot 为协作者（如果新 bot 与旧 bot_username 不同）→ accept invite
  5. 用新 bot token 重建 webhook（删旧 webhook + 建新 webhook，password=新 bot token）
  6. 写 DB：更新 gitcode_webhook_token / github_webhook_token = 新 bot token + 更新 bot_username = 新 bot username
  7. best-effort 移除旧 bot 协作者身份（R15.6.3 best-effort 规则）
- R21.1.3：global_bot_token_swap 在后台线程执行，不阻塞 save_config 返回
- R21.1.4：global_bot_token_swap 失败时仓库保持旧状态（旧 bot token 仍有效直到被撤销），记录 warning
- R21.1.5：清除 meta.global_*_bot_token_status 过期标记（R16.3.2 已有，确认覆盖更换场景）
- R21.1.6：GitHub 全局 bot token 更换同理（provider 分流）
- R21.1.7：global_bot_token_swap 用分布式锁 `global_bot_swap`，防止多实例同时执行

**与 auto_upgrade 的关系**：
- auto_upgrade_path_b_repos：全局 bot token"从无到有"→ 遍历路径 B 仓库 → 邀 bot + 重建 webhook + 写 DB（切换到路径 A）
- global_bot_token_swap：全局 bot token"有→有换账号"→ 遍历路径 A 仓库 → 邀新 bot + 重建 webhook + 更新 webhook_token
- 两者互斥（不会同时触发）：从无到有走 auto_upgrade，有→有走 swap

### R21.2 per-provider 过期标记 key（A2 级矛盾修复）

**问题**：R16.5.2/R16.4.1 的过期标记只有一个 global_bot_token_status，GitCode 全局 token 过期会把 GitHub 全局 token 一起短路。需要 per-provider key。

**设计**：过期标记改为 per-provider。

- R21.2.1：meta 表过期标记改为 per-provider key：
  - global_gitcode_bot_token_status（GitCode 全局 bot token 过期标记）
  - global_github_bot_token_status（GitHub 全局 bot token 过期标记）
- R21.2.2：R16.4.1 短路机制修正：_get_global_bot_token 按 provider 查对应 key——GitCode 查 global_gitcode_bot_token_status，GitHub 查 global_github_bot_token_status
- R21.2.3：R16.1.3 fail_task 检测修正：按 provider 标记对应 key——GitHub 仓库 401 标记 global_github_bot_token_status，GitCode 仓库 401 标记 global_gitcode_bot_token_status
- R21.2.4：R17.2.2 健康检查修正：按 provider 验证 + 标记对应 key
- R21.2.5：R16.5.2 修正：meta 表新增 global_gitcode_bot_token_status 和 global_github_bot_token_status 两个 key（替代单个 global_bot_token_status）
- R21.2.6：R16.5.4 修正：健康检查同时写 bot_token_health（admin 页展示用）+ 对应 provider 的 global_*_bot_token_status（短路用）
- R21.2.7：GlobalBotToken 策略的 _is_expired / mark_expired / clear_expired 按 provider 查对应 key
- R21.2.8：GlobalBotTokenHandler 同理按 provider 操作对应 key
- R21.2.9：向前兼容——旧数据如果已有 global_bot_token_status key，迁移时拆分为两个 per-provider key

**对 R20.13 A2A 鉴权的影响**：read_stored_token 不检查过期标记，per-provider key 不影响鉴权逻辑。

### R21.3 健康检查/verify 超时误标记修复（A3 级矛盾修复）

**问题**：R18.5.10"超时视为验证失败（标记过期）"+ R17.2.1 FAILURE_THRESHOLD=1 + R16.1.3"必须先验证"三者矛盾。一次网络抖动/5xx → 全局 token 标记 expired → 短路 → 所有 task NO_TOKEN_AVAILABLE → 恢复要等下一小时健康检查。

**设计**：区分 401（真过期）和超时/5xx（可能网络问题），只对 401 标记过期。

- R21.3.1：verify_token 返回 401 → 标记过期（token 确实无效）
- R21.3.2：verify_token 超时或 5xx → 不标记过期，记录 warning，等下次健康检查重试
- R21.3.3：R18.5.10 修正：超时不视为验证失败（不标记过期），只记 warning。原"超时视为失败"设计取消
- R21.3.4：R17.2.1 修正：FAILURE_THRESHOLD=1 只对 401 生效。超时/5xx 不计入 FAILURE_THRESHOLD
- R21.3.5：fail_task detect_and_mark_expired 中 provider.verify_token(token) 区分：
  - HTTP 401 → 标记过期 + 返回 error_code
  - 超时 / 5xx → 不标记，返回空（401 可能是其他原因）
  - 其他异常 → 不标记，返回空
- R21.3.6：最坏情况分析：网络抖动导致 verify 超时 → 不标记 → task 仍用原 token 执行 → 如果 token 真过期 → task 401 → fail_task 再次 verify → 如果这次 verify 也超时 → 不标记 → task 失败但 error_code 为原始错误（不是 TOKEN_EXPIRED）。可接受——网络恢复后下次 verify 会正确判断
- R21.3.7：健康检查同理——401 才标记 expired，超时/5xx 跳过不标记

### R21.4 OAuth refresh 竞态补充（B1 遗漏）

**问题**：R20.27 分布式锁框架已有，但 R20.27.2"先读 token_expires_at"不能完全解决竞态——两节点同时读到旧值 → 同时获取锁（一个等待）→ 第一个刷新成功 → 第二个拿到锁后用旧 refresh_token 刷新 → 如果平台做 refresh_token rotation，旧 refresh_token 已失效 → 刷新失败 → token 彻底失效。

**设计**：

- R21.4.1：分布式锁内 refresh 前先读 token_expires_at，如果已更新（其他实例刷新成功）→ 直接读新 access_token 返回，不重复 refresh
- R21.4.2：如果 token_expires_at 未更新（确实是当前实例需要刷新）→ 执行 refresh → 如果 refresh 失败（refresh_token rotation 导致旧 refresh_token 失效）→ 读 external_identities 获取最新 refresh_token（可能已被其他实例更新）→ 用新 refresh_token 重试一次
- R21.4.3：重试仍失败 → 标记 status=4 → 用户重新绑定
- R21.4.4：UserToken 策略（R18.2.4）的 _fetch 内部 get_user_token_with_refresh 已有进程内锁（deps.py _gitcode_refresh_locks），R20.27 分布式锁在其外层——双层锁保护

### R21.5 PR 维度自循环检测（B2 遗漏）

**问题**：R20.1.8 check_running_task 只定义 WHERE repo_key=? AND issue_id=?，PR 评论自循环检测缺 pr_id 字段定义。

**设计**：

- R21.5.1：agent_tasks 表利用现有 issue_id 字段存储 PR 关联标识——PR 类事件的 issue_id 已存为 PR 号或 PR 关联 Issue 号（resolve_pr_issue_id 解析结果）
- R21.5.2：check_running_task 对 note_hook_pr 事件：先尝试 issue_id 查询（PR 关联的 Issue），如果查不到 → 按 repo_key + event_type='note_hook_pr' 查询（PR 维度）
- R21.5.3：补充查询 SQL：`SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? AND status IN ('running','dispatched') LIMIT 1`——issue_id 字段对 PR 事件存储 PR 号或关联 Issue 号，查询逻辑不变
- R21.5.4：如果 PR 无关联 Issue（issue_id=空或 PR 号格式如 #pr_123）→ 条件 4 降级到 R20.10.5 session 兜底查询

### R21.6 非场景 task 行 issue_id 语义（B3 遗漏）

**问题**：飞书 VOD / Chat direct / autopilot / event dialogue 创建 task 行时无 issue 上下文，issue_id 填空则条件 4 永远匹配不到。

**设计**：

- R21.6.1：飞书 VOD agent 执行时 issue_id 填飞书消息关联的 Issue 号（VOD handler 从飞书消息解析 repo_key + issue_id）。如果无关联 Issue → issue_id 填空 → 条件 4 降级到 session 兜底（R20.10.5）
- R21.6.2：Chat direct 模式 issue_id 填会话关联的 Issue 号（conv.repo_key + conv.issue_number）。如果无关联 Issue → issue_id 填空 → 条件 4 降级到 session 兜底
- R21.6.3：autopilot issue_id 填巡检关联的 Issue 号（如有）。无关联 → issue_id 填空 → session 兜底
- R21.6.4：event dialogue issue_id 填事件关联的 Issue 号（evt.issue_id）。无关联 → issue_id 填空 → session 兜底
- R21.6.5：issue_id 为空时，条件 4 的 agent_tasks 查询不会命中（WHERE issue_id=? 用空串匹配不到有值行）→ 降级到 R20.10.5 session 兜底查询（devops_agent_session.last_active_at > now-300s）
- R21.6.6：session 兜底查询是 repo 级（无 issue_id），会覆盖该仓库所有 issue/PR 的评论——存在误拦风险（见 R21.7）

### R21.7 check_running_task session 兜底误拦分析（B4 遗漏）

**问题**：session 兜底查询（devops_agent_session.last_active_at > now-300s AND status=active）是 repo 级，agent 完成但 session 未置 inactive 时，host 用户对该仓库任何 issue 的评论都会被误拦最多 5 分钟。

**设计**：

- R21.7.1：agent 执行完成后立即更新 devops_agent_session.last_active_at = NULL 或 status = 'idle'（如果当前设计是保持 active），缩小误拦窗口
- R21.7.2：session 兜底查询窗口从 300s 缩短到 60s——agent 完成后 60s 内的评论才可能被误拦（大部分 agent 完成后用户不会立即评论）
- R21.7.3：误拦场景用例补充：
  1. agent 完成 → session last_active_at 更新 → task status=completed
  2. 用户 30s 后对该仓库另一个 issue 评论
  3. 条件 4 agent_tasks 查询：issue_id 不同 → 不命中
  4. 条件 4 session 兜底：last_active_at > now-60s → 命中 → 误拦
  5. 60s 后用户再次评论 → session 兜底不命中 → 放行
  6. 可接受——最多 60s 误拦窗口，用户重试即可
- R21.7.4：如果 agent 执行完后立即更新 session status='idle'（R21.7.1），则 session 兜底查询 status='active' 不命中 → 无误拦

### R21.8 「有仓库权限的用户」鉴权规则（B5 遗漏）

**问题**：全文反复出现"有仓库权限的用户"但未定义判定规则。

**设计**：

- R21.8.1：「有仓库权限的用户」定义：grape 侧托管人（managed_repos.host_user_id 指向的用户）+ grape 侧仓库协作者（devops_agent_session 中该仓库有 active session 的用户）+ grape admin
- R21.8.2：PUT /repos/bot-token 鉴权：只有托管人或 admin 可配置仓库级 bot token。普通 grape 用户不能给别人托管的仓库配 token（防水平越权）
- R21.8.3：settings 页 token 状态可见性：托管人 + admin 可见全局/仓库级 bot token 状态
- R21.8.4：VCS 侧 repo 成员不等于 grape 侧有权限——grape 侧权限独立于 VCS 侧。VCS 侧 repo 成员可以托管仓库（成为托管人），但托管后 grape 侧权限由托管关系决定
- R21.8.5：组织仓库（org_id > 0）：组织管理员视为有仓库权限

### R21.9 GitHub 路径 B auto_upgrade（B6 遗漏）

**问题**：auto_upgrade 全部场景只描述 GitCode，GitHub 路径 B 仓库是否参与升级未定义。

**设计**：

- R21.9.1：auto_upgrade_path_b_repos 按 provider 分流——GitCode 全局 bot token 配置时遍历 GitCode 路径 B 仓库，GitHub 全局 bot token 配置时遍历 GitHub 路径 B 仓库
- R21.9.2：GitHub 路径 B 仓库判断：_is_github_user_token_repo(repo_key) == True（host_user_id > 0 AND bot_github_token 为空）
- R21.9.3：GitHub auto_upgrade 流程与 GitCode 对称：获取 host 用户 GitHub token → 邀 GitHub bot 为协作者 → accept invite → 重建 GitHub webhook → 写 DB（bot_github_token + 清 host_user_id + 清 webhook_secret）
- R21.9.4：GitHub 协作者邀请用 add_github_collaborator（permission="push"），accept 用 accept_github_invite
- R21.9.5：GitHub webhook 重建用 setup_github_webhook / remove_github_webhook
- R21.9.6：save_config 同时配置 GitCode + GitHub 全局 bot token 时，两个 provider 的 auto_upgrade 并行执行（后台线程）

### R21.10 token 存储安全与测试需求（B7 遗漏）

**问题**：token 存储安全需求缺失，测试/验收需求缺失，webhook_secret / A2A 比对日志未纳入脱敏范围。

**设计**：

- R21.10.1：token 存储安全——现有 credential_crypto 加密覆盖 external_identities（用户 token）。bot_gitcode_token / bot_github_token / repo_gitcode_token / repo_github_token 在 managed_repos / devops_agent_session 表中明文存储（与现有设计一致，这些是 bot token 不是用户凭据）。新增列（bot_token_status / host_vcs_username / token_status）不涉及加密——它们是状态/元数据列，不含 token 原文
- R21.10.2：webhook_secret 脱敏——R19.1 日志脱敏范围扩展到 webhook_secret 值。webhook 验证日志（webhook.py）不输出 webhook_secret 原文或片段
- R21.10.3：A2A 比对日志脱敏——webhook.py A2A 鉴权日志不输出 token 原文（当前日志只输出 bot=True/False，已安全）
- R21.10.4：测试/验收需求——R18.4.3"现有测试通过"扩展为验收清单：
  - 单元测试：TokenResolver.get_token / read_stored_token / resolve 各策略覆盖
  - 单元测试：TokenActionHandler.is_self_loop_actor / get_comment_token / mark_expired 各 Handler 覆盖
  - 集成测试：路径 A/B token 获取 + 短路 + 恢复全流程
  - 集成测试：fail_task 401 检测 + error_code 写入 + 评论 fallback
  - 集成测试：健康检查标记过期 + 短路 + 恢复
  - E2E：路径 B agent 评论自循环检测（四条件组合）
  - E2E：全局 bot token 更换（R21.1 global_bot_token_swap）
  - E2E：GIT_ASKPASS fallback（R20.24 路径 B git clone）

### R21.11 其他轻量修正（C 级）

- R21.11.1：场景 13.2 评论权限模型——给出结论性需求：fail_task 评论 best-effort，403 时不重试不报错（与 R20.2.3 一致）。不预探测权限——复杂度高收益低，403 时静默降级到 dashboard 通知
- R21.11.2：自循环检测性能——host_vcs_username 查询走 managed_repos.host_vcs_username 列（R16.5.5 缓存），避免每次 webhook 查 external_identities。check_running_task 查 agent_tasks 索引（repo_key + issue_id + status），无性能瓶颈
- R21.11.3：R20.19.4 "could not read Username" 误报声明——该关键词对私有仓库不存在等场景也会出现，触发 verify 多一次 API 调用（5s 超时保护），可接受。verify 通过（200）→ 不标记过期，只记录 warning
- R21.11.4：DB 迁移降级——5 处幂等 ALTER try-except 保护，部分迁移失败时对应功能降级（R16.4.4 已覆盖 session token_status 列缺失降级）。整体降级原则：列不存在 → 对应短路检查跳过（返回 False），不影响 token 获取主链路

### R21.12 task 排队期间 token 过期窗口（P1 补充）

**问题**：路径 A 仓库 task 创建时 _get_task_repo_token 获取 bot token 写入 task 行 repo_gitcode_token（非空）。task 排队期间全局 bot token 过期（健康检查标记 meta.global_*_bot_token_status=expired）。claim 时 task 行已有 token（非空）→ 不重新获取 → agent 用过期 token → 401。

**当前代码**：
- event_pipeline.py _get_task_repo_token：task 创建时获取 bot token 写入 task 行
- runtime/service.py line 1278：claim 时 `if task and not (task.get("repo_gitcode_token") or "")` → 只在 task 行 token 为空时重新获取
- 路径 A 仓库 task 行 token 非空 → claim 时不重新获取 → 用旧 token

**时序**：
```
T0: task 创建 → _get_task_repo_token → bot token 写入 task 行（非空）
T1: task 排队（status=queued）
T2: 健康检查发现全局 bot token 过期 → 标记 meta.global_*_bot_token_status=expired
T3: worker claim task → task 行 token 非空 → 不重新获取 → 注入旧 token
T4: agent 用旧 token → git clone/API 401 → fail_task
T5: fail_task detect_and_mark_expired → 标记过期（但已标记）→ 幂等跳过
T6: task errors 写入 TOKEN_EXPIRED
```

**设计**：claim 时检查 task 行 token 的过期标记，过期则重新获取或走短路。

- R21.12.1：claim 时（service.py claim 函数内）对路径 A 仓库的 task 行 token 做新鲜度检查——检查 token 对应的过期标记（meta.global_*_bot_token_status / managed_repos.bot_token_status），如果已标记 expired 则重新获取
- R21.12.2：重新获取逻辑：调 TokenResolver.get_token(repo_key) → 如果短路返回空（已标记 expired 且无其他可用 token）→ task 行 repo_gitcode_token 注入空串 → agent 运行时获取空 token → fail_task NO_TOKEN_AVAILABLE（R20.19.1）
- R21.12.3：如果重新获取返回非空 token（其他策略有可用 token，如仓库级 bot token 未过期）→ 注入新 token
- R21.12.4：路径 B 仓库 task 行 token 为空 → claim 时已实时获取（现有逻辑，正确）→ 不受影响
- R21.12.5：新鲜度检查只对路径 A 仓库生效——路径 A 仓库 task 行写入的是 bot token（可能过期），路径 B 仓库 task 行写空（claim 时实时获取）
- R21.12.6：新鲜度检查不调 VCS API（不增加 claim 延迟）——只查 DB 过期标记（meta / managed_repos），与短路机制一致
- R21.12.7：claim 时新鲜度检查在现有 `if task and not (task.get("repo_gitcode_token") or "")` 逻辑之前执行——即使 task 行有 token，也先检查过期标记

```python
# runtime/service.py claim 函数内，现有逻辑之前新增：
if task and task.get("repo_gitcode_token"):
    # 路径 A：task 行有 token，检查是否已过期
    _rk = task.get("repo_key") or ""
    if _rk:
        provider = create_provider("github" if _rk.startswith("github:") else "gitcode")
        resolver = TokenResolver(provider)
        # 检查当前生效的策略是否标记过期
        for strategy in resolver.strategies:
            token = strategy._fetch(_rk)  # 纯读
            if token and token == task["repo_gitcode_token"]:
                # 找到 task 行 token 对应的策略，检查过期标记
                if strategy._is_expired(_rk):
                    # 已过期 → 重新获取（可能短路返回空或返回其他策略的 token）
                    task["repo_gitcode_token"] = resolver.get_token(_rk)
                break
# 现有逻辑：task 行 token 为空时实时获取
if task and not (task.get("repo_gitcode_token") or ""):
    task["repo_gitcode_token"] = _get_task_token(task)
```

## 六、PAT 无自动续期问题

### 6.1 问题本质

没有配置 OAuth client/secret 时，所有层级的 token 都是 PAT（Personal Access Token）。PAT 没有 refresh_token，无法自动续期。三个层级都受影响：

| 层级 | token 来源 | 有 OAuth client/secret | 无 OAuth client/secret |
|------|-----------|----------------------|----------------------|
| 用户 token | OAuth 绑定 | access_token + refresh_token，get_user_token_with_refresh 内部自动刷新 ✓ | 无法 OAuth 绑定，只能 PAT 手动输入，_has_refresh_token=False，不刷新 ✗ |
| 用户 token | PAT 手动输入 | _has_refresh_token=False，不刷新 ✗ | 同左 ✗ |
| 全局 bot token | 手动输入 | PAT，无 refresh_token，不刷新 ✗ | 同左 ✗ |
| 仓库级 bot token | 手动输入 | PAT，无 refresh_token，不刷新 ✗ | 同左 ✗ |

**关键结论**：
- 用户 token 是唯一可能支持自动刷新的层级，前提是配置了 OAuth client/secret 并通过 OAuth 绑定
- 全局 bot token 和仓库级 bot token 无论是否有 OAuth 配置，都是手动输入的 PAT，不会有自动续期能力
- 无 OAuth client/secret 时，三层 token 全是 PAT，全部不刷新

当前系统对 PAT 过期的处理是**被动检测**——等 task 401 失败后才知道过期（R10/R16.1）。没有主动续期能力，没有过期前预警。

### 6.2 PAT 的固有限制

1. **无法获取过期时间**：GitCode/GitHub 的 PAT 不返回 expires_at 字段，系统无法知道 token 何时过期
2. **无法自动续期**：PAT 没有 refresh_token，不能像 OAuth token 那样自动刷新
3. **只能被动感知**：唯一发现 PAT 过期的方式是 API 调用返回 401

### 6.3 设计方案

#### 6.3.1 配置时提示无自动续期

**用户 token**：通过 OAuth 绑定的 token 有 refresh_token，可自动刷新；通过手动输入的 PAT 无 refresh_token，不会自动刷新。配置时通过 `_has_refresh_token` 判断是否有 refresh_token，无则提示。

**全局 bot token / 仓库级 bot token**：手动输入的 token 天然是 PAT，一定没有 refresh_token，无需检测，直接提示。

**用户 token（PAT 手动绑定）**：
- 已有 R13.3：account.js `_accountManualBindForm` 在未配 OAuth App 时显示 "PAT 不会自动续期" 提示
- 已有 R2：`_has_refresh_token` 判断 PAT 跳过刷新

**全局 bot token**：
- settings 页配置全局 bot token 时直接提示"此 token 不会自动续期，过期后需手动重新配置"
- 无需检测 refresh_token（手动输入天然是 PAT）

**仓库级 bot token**：
- PUT /repos/bot-token 时，验证 token 有效性后直接提示"此 token 不会自动续期，过期后需手动重新配置"
- 无需检测 refresh_token（手动输入天然是 PAT）

#### 6.3.2 过期前主动健康检查

由于 PAT 无法获取过期时间，采用**定期健康检查**方式主动发现过期：

- 定期（如每小时）用全局 bot token / 仓库级 bot token 调 VCS API GET /user
- 401 → 标记过期（meta / managed_repos）
- 200 → 清除过期标记（如果之前标记过）
- 健康检查在后台线程执行，不影响主流程
- 用户 token 不做主动健康检查（已有 _has_refresh_token + get_user_token_with_refresh 的刷新机制，OAuth token 自动刷新；PAT 在每次使用时被动检测）

#### 6.3.3 过期后恢复指引

token 过期后，系统提供明确的恢复指引：

| 层级 | 过期后表现 | 恢复方式 | 指引文案 |
|------|-----------|---------|---------|
| 用户 token | task 401 + account 页提示 | 用户重新绑定 token | "你的 Token 已过期，请在设置页重新绑定" |
| 全局 bot token | 所有路径 A 仓库 task 401 + dashboard 提示 | 管理员重新配置全局 bot token | "全局 Bot Token 已过期，请联系管理员重新配置" |
| 仓库级 bot token | 该仓库 task 401 + dashboard 提示 | 有仓库权限的用户重新配置 | "仓库 Bot Token 已过期，请联系有权限的成员重新配置" |

### 6.4 需求

#### R17.1 配置 token 时提示无自动续期

**需求描述**：配置 PAT 类型 token 时明确提示不会自动续期。

- R17.1.1：用户 token PAT 绑定时提示（已有 R13.3）
- R17.1.2：全局 bot token 配置时提示（settings 页）
- R17.1.3：仓库级 bot token 配置时提示（PUT /repos/bot-token 返回提示或 settings 页）
- R17.1.4：提示文案："此 Token 为 PAT 类型，不会自动续期，过期后需手动重新配置"

#### R17.2 全局/仓库级 bot token 定期健康检查

**需求描述**：后台定期验证全局/仓库级 bot token 有效性，主动发现过期。

- R17.2.1：每小时后台线程用全局 bot token 调 VCS API GET /user。现有代码 CHECK_INTERVAL=300s=5min + FAILURE_THRESHOLD=3（最坏 15min 才标记），改为 CHECK_INTERVAL=3600s + FAILURE_THRESHOLD=1（1 次失败即标记 expired，因为有 fail_task 401 兜底，不需要连续 3 次防误判——健康检查标记后短路，但 fail_task 验证 token 有效时会自动清除标记）
- R17.2.2：401 → 标记 meta.global_bot_token_status=expired
- R17.2.3：200 → 清除过期标记（如果之前标记过）→ 短路自动解除（R16.4 短路机制感知标记清除后重新返回 token）
- R17.2.4：有仓库级 bot token 的仓库（含 GitCode bot_gitcode_token 和 GitHub bot_github_token），每小时验证仓库级 token
- R17.2.5：401 → 标记 managed_repos.bot_token_status=expired
- R17.2.6：200 → 清除过期标记 → 短路自动解除
- R17.2.7：健康检查在后台线程执行，失败不影响主流程
- R17.2.8：用户 token 不做主动健康检查（OAuth token 自动刷新，PAT 在使用时被动检测）
- R17.2.9：健康检查用分布式锁（run_with_distributed_lock），多实例只有一个执行
- R17.2.10：仓库级 token 健康检查分批执行（每 10 个一批，间隔 1 秒），避免 rate limit
- R17.2.11：仓库级 token 健康检查遍历方式：查询 managed_repos 中 bot_gitcode_token!='' 或 bot_github_token!='' 的仓库，按 provider 调对应 VCS API GET /user 验证

#### R17.3 健康检查结果通知

- R17.3.1：全局 bot token 健康检查发现过期 → dashboard API 返回标记 → 所有路径 A 仓库用户看到提示
- R17.3.2：仓库级 bot token 健康检查发现过期 → 该仓库用户看到提示
- R17.3.3：通知复用 R16.2 的统一通知机制（dashboard/task 详情 + settings 页）

#### R17.4 短路机制复用

健康检查标记的过期状态复用 R16.4 的短路机制：

- R17.4.1：健康检查标记的过期状态复用 R16.4 短路机制
- R17.4.2：全局 bot token 标记过期 → _get_global_bot_token 短路返回空串
- R17.4.3：仓库级 bot token 标记过期 → get_bot_token_by_repo 短路返回空串
- R17.4.4：管理员/用户重新配置 token → 清除标记 → 短路解除

## 七、全局/仓库级 Bot Token 自动刷新可行性分析

### 7.1 问题

用户 token 通过 OAuth 绑定可以自动刷新（有 refresh_token）。全局 bot token 和仓库级 bot token 是手动输入的 PAT，没有 refresh_token，无法自动刷新。是否有办法让它们也像用户 token 一样自动刷新？

### 7.2 方案分析

#### 方案 A：bot 账号走 OAuth 授权流程

**原理**：bot 账号也通过 OAuth 授权获取 access_token + refresh_token，存入 DB。之后像用户 token 一样定期检查过期 + 调 refresh_oauth_token 自动刷新。

**可行性**：技术可行，但实际操作困难。

**问题**：
1. bot 账号需要完成 OAuth 授权流程——管理员需要以 bot 账号身份登录 VCS 平台完成 OAuth 授权，这是人工操作
2. client/secret 是平台级的——如果系统已配了 OAuth client/secret，bot 账号能用同一个 OAuth App 做授权，但授权流程仍需人工触发
3. 管理员配置全局 bot token 时当前是手动输入一个字符串（PAT），改成 OAuth token 流程复杂得多
4. 多实例部署下 bot token 是共享的（meta 表），OAuth refresh 需要考虑并发刷新问题

**代价**：管理员需以 bot 身份完成 OAuth 流程，比手动输 PAT 复杂；系统需扩展 bot token 的存储结构（加 refresh_token/expires_at 字段）和刷新逻辑。

#### 方案 B：使用永久 PAT

**原理**：GitCode PAT 创建时可选永久有效；GitHub classic PAT 也可设永久。

**可行性**：简单有效，但有平台限制。

**问题**：
1. GitCode：PAT 创建时可选 30天/90天/永久，选永久就不存在过期问题
2. GitHub：classic PAT 可设永久；fine-grained PAT **必须**设过期时间（最长 1 年），不能永久
3. 永久 PAT 被撤销（管理员手动撤销、账号密码修改等）后仍会失效
4. 安全风险：永久 PAT 泄露后无自动失效机制

**代价**：无代码改动，但依赖 VCS 平台支持且存在安全风险。

#### 方案 C：定期健康检查 + 过期通知 + 手动恢复（R17 已设计）

**原理**：不追求自动刷新，后台定期验证 token 有效性（调 VCS API GET /user），401 时标记过期 + 通知受影响用户 + 短路避免重试。管理员/用户重新配置后恢复。

**可行性**：完全可行，不依赖 VCS 平台特性。

**优势**：
1. 不需要 bot 账号完成 OAuth 流程
2. 不依赖 VCS 平台是否支持永久 PAT
3. 主动发现过期（不等 task 401），用户立即收到提示
4. 恢复路径清晰（重新配置 token 即可）

**代价**：token 过期后仍需人工恢复（不能自动续期），但健康检查 + 通知将恢复时间从"用户发现 task 失败 → 联系管理员 → 管理员排查 → 重新配置"缩短为"系统自动发现 → 通知管理员/用户 → 管理员重新配置"。

### 7.3 结论

| 方案 | 自动刷新 | 实现复杂度 | 平台依赖 | 安全风险 | 采纳 |
|------|---------|-----------|---------|---------|------|
| A: bot OAuth 授权 | ✓ | 高（OAuth 流程 + 存储扩展 + 并发刷新） | 需 OAuth App 配置 | 低 | ✗ 不采纳（代价过大） |
| B: 永久 PAT | 不需要 | 无 | GitCode 支持，GitHub fine-grained 不支持 | 高（泄露后无自动失效） | ✗ 不采纳（不可控） |
| C: 健康检查 + 通知 + 手动恢复 | ✗ | 中（R17 已设计） | 无 | 低 | ✓ 采纳 |

**采纳方案 C（R17）**：不追求自动刷新，靠主动健康检查 + 过期通知 + 快速手动恢复。这是最务实的方案——不依赖 VCS 平台特性，不增加管理员配置复杂度，将 token 过期的影响降到最小（主动发现 + 即时通知 + 短路保护）。

### 7.4 未来扩展

如果后续需要自动刷新，可在方案 C 基础上扩展为方案 A：
1. 全局/仓库级 bot token 存储增加 refresh_token / expires_at 字段
2. 配置时支持"OAuth 授权"方式（管理员以 bot 身份完成 OAuth 流程获取 token）
3. 健康检查发现 token 临近过期时调 refresh_oauth_token 自动刷新
4. 刷新失败再走方案 C 的过期通知 + 手动恢复流程

这属于后续迭代，当前不实现。

## 八、Token 获取统一接口设计（策略模式）

### 8.1 当前问题

当前 token 获取散落在 13 个函数中，分布在 2 个文件（db.py / deps.py），逻辑高度重复但又不完全一致：

| 函数 | 文件 | 职责 | 问题 |
|------|------|------|------|
| get_repo_token_with_refresh | db.py | GitCode 统一入口（4级 fallback） | 与 get_github_token 逻辑重复 |
| get_github_token | db.py | GitHub 统一入口（4级 fallback） | 与 get_repo_token_with_refresh 逻辑重复 |
| get_repo_bot_token | db.py | 跳过 devops_agent_session（#343 语义） | fallback 链又是另一套 |
| get_repo_bot_token_no_global | db.py | 不含全局兜底（A2A 鉴权用） | 又一套 fallback |
| get_bot_token_by_repo | db.py | 按 owner/repo 查 | host_user_id 守卫嵌入 |
| get_gitcode_token | db.py | 兼容包装 | 委托 get_repo_token_with_refresh |
| get_task_token | db.py | task dict 取 token | 按 provider 分流 |
| get_user_token_with_refresh | deps.py | 用户 GitCode token（带刷新） | 与 GitHub 版逻辑重复 |
| get_user_github_token_with_refresh | deps.py | 用户 GitHub token（带刷新） | 与 GitCode 版逻辑重复 |
| get_user_gitcode_token | db.py | 用户 token（不带刷新） | 又一个用户 token 入口 |
| _get_global_bot_token | db.py | 全局 GitCode bot token | |
| get_global_gitcode_bot_token | db.py | 同上（统一入口） | 命名重复 |
| get_global_github_bot_token | db.py | 全局 GitHub bot token | |

**核心问题**：
1. 每个函数自己实现一套 fallback 链，新增 token 类型时需要改所有函数
2. GitCode 和 GitHub 逻辑重复（两个平台只是 API 不同，fallback 策略相同）
3. 过期检测/短路逻辑（R16.4）需要嵌入每个函数，散落不可控
4. 调用方需要知道调哪个函数（13 个函数选哪个？），心智负担大

### 8.2 设计目标

- **统一入口**：调用方只需要一个函数 `get_token(repo_key, context)`，不需要知道 13 个函数选哪个
- **策略链**：token 获取按优先级链执行，每级是一个策略，可插拔
- **接口隔离**：GitCode 和 GitHub 各自实现 VCSProvider 接口，策略类通过接口调用 provider 能力，不耦合具体平台
- **过期短路**：过期检测/短路逻辑（R16.4）在策略基类中统一实现，不散落
- **向后兼容**：现有函数保留为薄包装，委托统一接口

### 8.3 VCSProvider 接口设计

#### 8.3.0 接口定义

```python
from abc import ABC, abstractmethod

class VCSProvider(ABC):
    """VCS 平台接口。GitCode / GitHub 各自实现，策略类通过此接口调用平台能力。"""

    @abstractmethod
    def verify_token(self, token: str) -> dict:
        """验证 token 有效性，返回用户信息 dict（含 login 字段）。"""

    @abstractmethod
    def get_user_login(self, token: str) -> str:
        """获取 token 对应的用户名。"""

    @abstractmethod
    def add_collaborator(self, api_token: str, owner: str, repo: str,
                         username: str, permission: str) -> bool:
        """邀 bot 为协作者。幂等：已是协作者返回 True。"""

    @abstractmethod
    def remove_collaborator(self, api_token: str, owner: str, repo: str,
                            username: str) -> bool:
        """移除协作者。best-effort，失败不阻断。"""

    @abstractmethod
    def setup_webhook(self, api_token: str, owner: str, repo: str,
                      webhook_url: str, webhook_secret: str) -> int:
        """创建 webhook，返回 webhook_id。"""

    @abstractmethod
    def remove_webhook(self, api_token: str, owner: str, repo: str,
                       hook_id: str) -> bool:
        """删除 webhook。"""

    @abstractmethod
    def token_storage_field(self) -> str:
        """仓库级 bot token 存储列名。"""

    @abstractmethod
    def webhook_token_field(self) -> str:
        """webhook token 存储列名。"""

    @abstractmethod
    def session_token_field(self) -> str:
        """devops_agent_session token 列名。"""

    @abstractmethod
    def global_bot_meta_key(self) -> str:
        """全局 bot token meta key。"""

    @abstractmethod
    def pat_create_url(self) -> str:
        """PAT 创建页链接。"""

    @abstractmethod
    def fetch_user_token(self, username: str, user_id: int) -> str:
        """获取用户 token（带刷新）。GitCode 调 get_user_token_with_refresh，GitHub 调 get_user_github_token_with_refresh。"""
```

#### 8.3.0.1 GitCodeProvider 实现

```python
class GitCodeProvider(VCSProvider):
    """GitCode 平台实现。"""

    API_BASE = "https://gitcode.com/api/v5"

    def verify_token(self, token: str) -> dict:
        from src.user.repo_manager import _req
        return _req("GET", f"{self.API_BASE}/user", token)

    def get_user_login(self, token: str) -> str:
        data = self.verify_token(token)
        return data.get("login", "") or ""

    def add_collaborator(self, api_token, owner, repo, username, permission="maintainer"):
        from src.user.repo_manager import add_collaborator
        ok, _err = add_collaborator(api_token, owner, repo, username, permission)
        return ok

    def remove_collaborator(self, api_token, owner, repo, username):
        from src.user.repo_manager import remove_collaborator
        try:
            remove_collaborator(api_token, owner, repo, username)
            return True
        except Exception:
            return False

    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
        from src.user.repo_manager import setup_webhook
        return setup_webhook(api_token, owner, repo, webhook_url, webhook_password=webhook_secret)

    def remove_webhook(self, api_token, owner, repo, hook_id):
        from src.user.repo_manager import remove_webhook
        try:
            remove_webhook(api_token, owner, repo, hook_id)
            return True
        except Exception:
            return False

    def token_storage_field(self) -> str:
        return "bot_gitcode_token"

    def webhook_token_field(self) -> str:
        return "gitcode_webhook_token"

    def session_token_field(self) -> str:
        return "repo_gitcode_token"

    def global_bot_meta_key(self) -> str:
        return "global_bot_gitcode_token"

    def pat_create_url(self) -> str:
        return "https://gitcode.com/profile/personal_access_tokens"

    def fetch_user_token(self, username: str, user_id: int) -> str:
        from src.api.deps import get_user_token_with_refresh
        return get_user_token_with_refresh(username, user_id) or ""
```

#### 8.3.0.2 GitHubProvider 实现

```python
class GitHubProvider(VCSProvider):
    """GitHub 平台实现。"""

    API_BASE = "https://api.github.com"

    def verify_token(self, token: str) -> dict:
        from src.user.repo_manager import _github_req
        return _github_req("GET", f"{self.API_BASE}/user", token)

    def get_user_login(self, token: str) -> str:
        from src.user.repo_manager import get_user_github_login
        return get_user_github_login(token)

    def add_collaborator(self, api_token, owner, repo, username, permission="push"):
        from src.user.repo_manager import add_github_collaborator
        return add_github_collaborator(api_token, owner, repo, username, permission)

    def remove_collaborator(self, api_token, owner, repo, username):
        from src.user.repo_manager import remove_github_collaborator
        try:
            remove_github_collaborator(api_token, owner, repo, username)
            return True
        except Exception:
            return False

    def setup_webhook(self, api_token, owner, repo, webhook_url, webhook_secret=""):
        from src.user.repo_manager import setup_github_webhook
        return setup_github_webhook(api_token, owner, repo, webhook_url, webhook_secret=webhook_secret)

    def remove_webhook(self, api_token, owner, repo, hook_id):
        from src.user.repo_manager import remove_github_webhook
        try:
            remove_github_webhook(api_token, owner, repo, hook_id)
            return True
        except Exception:
            return False

    def token_storage_field(self) -> str:
        return "bot_github_token"

    def webhook_token_field(self) -> str:
        return "github_webhook_token"

    def session_token_field(self) -> str:
        return "repo_github_token"

    def global_bot_meta_key(self) -> str:
        return "global_bot_github_token"

    def pat_create_url(self) -> str:
        return "https://github.com/settings/tokens"

    def fetch_user_token(self, username: str, user_id: int) -> str:
        from src.api.deps import get_user_github_token_with_refresh
        return get_user_github_token_with_refresh(username, user_id) or ""
```

#### 8.3.0.3 Provider 工厂

```python
def create_provider(provider: str) -> VCSProvider:
    """根据 provider 标识创建 VCSProvider 实例。"""
    if provider == "github":
        return GitHubProvider()
    return GitCodeProvider()  # 默认 gitcode
```

#### 8.3.0.4 GitCode vs GitHub 差异矩阵

| 维度 | GitCode | GitHub |
|------|---------|--------|
| 验证 token API | GET https://gitcode.com/api/v5/user | GET https://api.github.com/user |
| Accept header | application/json | application/vnd.github+json |
| User-Agent | 不需要 | Grape-DevOps |
| 仓库级 token 存储列 | bot_gitcode_token | bot_github_token |
| webhook token 存储列 | gitcode_webhook_token | github_webhook_token |
| devops_agent_session 列 | repo_gitcode_token | repo_github_token |
| 全局 bot token meta key | global_bot_gitcode_token | global_bot_github_token |
| 协作者权限枚举 | pull/push/admin/customized | pull/push/admin |
| 协作者默认权限 | maintainer → admin | push |
| 邀请幂等判断 | 中文关键词（"已经是项目中的成员"等） | 403 状态码 |
| webhook body | password 字段 + push_events 等布尔字段 | config.secret + events 数组 |
| webhook URL 后缀 | /webhook/gitcode | /webhook/github |
| PAT 创建页 | https://gitcode.com/profile/personal_access_tokens | https://github.com/settings/tokens |

### 8.4 策略模式设计

#### 8.4.1 Token 策略基类

```python
class TokenStrategy:
    """token 获取策略基类。每个子类代表 fallback 链中的一级。
    
    持有 VCSProvider 实例，通过接口获取存储字段名和 API 调用，不硬编码 provider 分支。

    方法分层契约（关键——决定了上层 get_token / read_stored_token 的语义分离）：
    - _fetch(): 纯读存储值。不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。
      供 read_stored_token() 和 get_token() 共用，是两者的公共底层。
    - _is_expired(): 查过期标记（managed_repos.bot_token_status / meta.global_bot_token_status / external_identities.status）。
      只读标记，不调 VCS API 验证。
    - get_token(): = _fetch() + _is_expired() 检查。过期则短路返回空串。
      这是"执行 token"语义——需要当前可用的 token。
    
    分层原则：过期检查和刷新逻辑在 get_token() / 上层方法中组合，_fetch() 保持纯读。
    这样 read_stored_token() 只调 _fetch() 就能保证"读原始值不做任何运行时处理"，
    不需要在策略类内部加任何 flag。
    """

    error_code = ""  # 子类覆盖

    def __init__(self, provider: VCSProvider):
        self.provider = provider  # VCSProvider 实例（GitCodeProvider / GitHubProvider）

    def get_token(self, repo_key: str) -> str:
        """获取可用执行 token。_fetch 读值 → _is_expired 检查过期 → 过期则短路返回空串。"""
        token = self._fetch(repo_key)
        if token and self._is_expired(repo_key):
            return ""  # 短路：已标记过期，不返回
        return token

    def _fetch(self, repo_key: str) -> str:
        """纯读存储值。不检查过期、不刷新、不调 VCS API。只查 DB / meta 表。
        
        契约：
        - 返回当前存储的 token 原始值（可能已过期但仍存储在 DB 中）
        - 不触发 OAuth refresh（refresh 是 UserToken._fetch 内部 get_user_token_with_refresh 的行为，
          但 read_stored_token 场景不应走 refresh——见 UserToken._fetch 特殊说明）
        - 不检查过期标记（_is_expired 是独立方法）
        - 走缓存层（如 _cached_get_meta 的 60s 缓存——缓存值就是"存储值"的快照）
        """
        raise NotImplementedError

    def _is_expired(self, repo_key: str) -> str:
        """查过期标记。只读标记字段，不调 VCS API 验证。"""
        return False

    def mark_expired(self, repo_key: str):
        """子类实现：标记 token 过期。"""
        pass

    def clear_expired(self, repo_key: str):
        """子类实现：清除过期标记。"""
        pass
```

#### 8.4.2 具体策略

```python
class DevopsAgentSessionToken(TokenStrategy):
    """第 1 级：devops_agent_session.{session_token_field}"""

    error_code = "SESSION_TOKEN_EXPIRED"

    def _fetch(self, repo_key):
        field = self.provider.session_token_field()  # repo_gitcode_token / repo_github_token
        # 查 devops_agent_session 表（列名通过 provider 接口获取）
        ...

    def _is_expired(self, repo_key):
        # 查 devops_agent_session.token_status == 'expired'（新增字段）
        # 如未实现独立过期标记，返回 False（不短路，由调用方 401 兜底）
        return False

    def mark_expired(self, repo_key):
        # 写 devops_agent_session.token_status = 'expired'
        # 如未实现独立过期标记，仅记日志不写 DB
        pass

class RepoBotToken(TokenStrategy):
    """第 2 级：managed_repos.{token_storage_field}"""

    error_code = "REPO_TOKEN_EXPIRED"

    def _fetch(self, repo_key):
        if get_host_user_id(repo_key) > 0:
            return ""  # 路径 B 跳过
        field = self.provider.token_storage_field()  # bot_gitcode_token / bot_github_token
        # 查 managed_repos（列名通过 provider 接口获取）
        ...

    def _is_expired(self, repo_key):
        # 查 managed_repos.bot_token_status
        ...

    def mark_expired(self, repo_key):
        # 写 managed_repos.bot_token_status = 'expired'
        ...

class GlobalBotToken(TokenStrategy):
    """第 3 级：全局 bot token（meta 表 {global_bot_meta_key}）"""

    error_code = "GLOBAL_TOKEN_EXPIRED"

    def _fetch(self, repo_key):
        if get_host_user_id(repo_key) > 0:
            return ""  # 路径 B 跳过
        meta_key = self.provider.global_bot_meta_key()  # global_bot_gitcode_token / global_bot_github_token
        # 查 meta 表（key 通过 provider 接口获取）
        ...

    def _is_expired(self, repo_key):
        # 查 meta.global_bot_token_status
        ...

    def mark_expired(self, repo_key):
        # 写 meta.global_bot_token_status = 'expired'
        ...

class UserToken(TokenStrategy):
    """第 4 级：用户 token（external_identities，带刷新）"""

    error_code = "TOKEN_EXPIRED"

    def _fetch(self, repo_key):
        host_user_id = get_host_user_id(repo_key)
        if host_user_id == 0:
            return ""  # 路径 A 不走用户 token
        # 通过 provider 接口获取用户 token（不硬编码 provider 分支）
        # GitCodeProvider.fetch_user_token → get_user_token_with_refresh
        # GitHubProvider.fetch_user_token → get_user_github_token_with_refresh
        from src.auth.grape_account_db import get_grape_account_by_id
        account = get_grape_account_by_id(host_user_id)
        if account and account.get("username"):
            return self.provider.fetch_user_token(account["username"], host_user_id)
```

#### 8.4.3 统一入口

```python
class TokenResolver:
    """token 获取统一入口。按优先级链执行策略，第一个返回非空 token 的策略胜出。

    上层语义方法（3 个入口，对应 3 种 token 语义）：
    - get_token(): 获取可用执行 token（含过期短路）。供 pipeline / agent / clone / push 用。
    - read_stored_token(): 读取存储原始值（不过期检查、不刷新）。供 A2A 鉴权 / session 写入用。
    - resolve(): 获取执行 token + 级别信息（TokenResult）。供需要级别差异操作的调用方用。

    底层共享策略链 self.strategies，三个方法走不同的检查路径：
    - get_token → strategy.get_token() = _fetch() + _is_expired()
    - read_stored_token → strategy._fetch()（纯读，不检查过期）
    - resolve → strategy.get_token() + 级别标识
    """

    def __init__(self, provider: VCSProvider):
        self.provider = provider
        self.strategies = [
            DevopsAgentSessionToken(provider),
            RepoBotToken(provider),
            GlobalBotToken(provider),
            UserToken(provider),
        ]

    def get_token(self, repo_key: str) -> str:
        """获取可用执行 token。含过期短路。返回空串=无可用 token。"""
        for strategy in self.strategies:
            token = strategy.get_token(repo_key)
            if token:
                return token
        return ""

    def read_stored_token(self, repo_key: str) -> str:
        """读取存储的原始 token 值。不过期检查、不刷新、不调 VCS API。

        契约（写死，不可变）：
        1. 不检查过期标记——即使 token 被标记 expired 也返回原始值
        2. 不触发 OAuth refresh——UserToken 策略的 _fetch 内部如果调 get_user_token_with_refresh
           （会触发 refresh），read_stored_token 场景必须跳过 refresh 逻辑
        3. 走缓存层——_cached_get_meta 的 60s 缓存仍生效（缓存值=存储值快照）
        4. 返回空串=该 repo 无任何 token 存储（不是"过期"，是"未配置"）

        UserToken._fetch 特殊说明：
        UserToken._fetch 内部调 provider.fetch_user_token → get_user_token_with_refresh，
        后者会检查 token_expires_at 并触发 OAuth refresh。这与 read_stored_token "不刷新"契约冲突。
        解决方式：UserToken 新增 _fetch_stored() 方法（只读 external_identities.access_token，
        不调 get_user_token_with_refresh），read_stored_token 调 _fetch_stored 而非 _fetch。
        其他策略 _fetch 本身就是纯读，无需 _fetch_stored。

        供 A2A 鉴权（get_repo_bot_token_no_global）/ session 写入（agents start/restart）用。
        """
        for strategy in self.strategies:
            # UserToken 策略用 _fetch_stored 跳过 refresh
            if hasattr(strategy, '_fetch_stored'):
                token = strategy._fetch_stored(repo_key)
            else:
                token = strategy._fetch(repo_key)
            if token:
                return token
        return ""

    def detect_and_mark_expired(self, repo_key: str) -> str:
        """fail_task 401 时调用：按优先级找到当前生效的策略，验证 token 有效性后标记过期，返回 error_code。

        注意：不能直接标记——task 401 可能是网络抖动而非真过期，必须先调 VCS API 验证（R16.1.3/R16.1.4）。
        """
        for strategy in self.strategies:
            token = strategy._fetch(repo_key)  # 不检查过期标记，查原始值
            if token:
                # 先验证 token 有效性（通过 provider 接口调 VCS API GET /user）
                try:
                    self.provider.verify_token(token)
                    # token 验证有效 → 401 是其他原因（权限不足等），不标记过期
                    return ""
                except Exception:
                    # token 验证无效（401）→ 标记过期
                    # 幂等：已标记过则不重复标记（R16.1.6）
                    if not strategy._is_expired(repo_key):
                        strategy.mark_expired(repo_key)
                    return strategy.error_code
        # 所有策略 _fetch 都返回空（token 已被短路或未配置）→ 无可用 token
        return "NO_TOKEN_AVAILABLE"  # R16.6.1
```

#### 8.4.4 调用方使用

```python
# 获取 token（替换 13 个函数）
provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
token = TokenResolver(provider).get_token(repo_key)

# fail_task 401 检测（统一过期检测入口）
error_code = TokenResolver(provider).detect_and_mark_expired(repo_key)
```

#### 8.4.5 向后兼容

现有函数保留为薄包装：

```python
def get_repo_token_with_refresh(repo_key):
    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
    return TokenResolver(provider).get_token(repo_key)

def get_github_token(repo_key=""):
    return TokenResolver(create_provider("github")).get_token(repo_key)

def get_repo_bot_token(repo_key):
    # #343 语义：跳过 devops_agent_session（第 1 级）
    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
    resolver = TokenResolver(provider)
    resolver.strategies = resolver.strategies[1:]  # 跳过第 1 级
    return resolver.get_token(repo_key)

def get_repo_bot_token_no_global(repo_key):
    # A2A 鉴权用：不含全局兜底（跳过第 3 级全局）
    # 现有代码 fallback 链：仓库级 → devops_agent_session → 用户 token（路径 B 降级）
    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
    resolver = TokenResolver(provider)
    resolver.strategies = [resolver.strategies[1], resolver.strategies[0], resolver.strategies[3]]  # 仓库级 + devops_agent_session + 用户 token，跳过第 3 级全局
    return resolver.get_token(repo_key)

def get_task_token(task):
    # task dict 取 token：按 provider 分流
    # 先取 task.repo_gitcode_token（task 行存入的 bot token），无则调 get_repo_token_with_refresh
    _tok = task.get("repo_gitcode_token") or ""
    if _tok:
        return _tok
    repo_key = task.get("repo_key") or ""
    provider = create_provider("github" if repo_key.startswith("github:") else "gitcode")
    return TokenResolver(provider).get_token(repo_key)
```

**不被 TokenResolver 替代的函数**：

- `get_user_token_with_refresh(username, user_id)` / `get_user_github_token_with_refresh(username, user_id)`（deps.py）— 这两个函数接受 username + user_id 参数，不走 repo_key。它们是 UserToken 策略 `_fetch` 内部通过 `provider.fetch_user_token(username, user_id)` 调用的底层函数，不是 repo_key → token 的映射。account 页绑定时直接调用这两个函数，保持原样不替代。
- `get_user_gitcode_token(user_id)`（db.py）— 用户 token 不带刷新版本，保持原样。

**get_github_token D32-补丁兼容**：

现有 get_github_token（db.py line 2278-2290）有一个 GitCode 版没有的 fallback：仓库 owner 的 external_identities GitHub token。这是 D32-补丁引入的逻辑——当仓库级 bot_github_token 为空时，查仓库 owner 的 GitHub identity access_token。

TokenResolver 的标准策略链不包含这个 fallback（UserToken 只查 host_user_id，不查 owner）。
处理方式：get_github_token 的薄包装中保留 D32-补丁逻辑，在 TokenResolver 返回空后追加 owner 查询：

```python
def get_github_token(repo_key=""):
    provider = create_provider("github")
    token = TokenResolver(provider).get_token(repo_key)
    if token:
        return token
    # D32-补丁：TokenResolver 返回空时，查仓库 owner 的 GitHub identity
    try:
        from src.repo_key import parse_repo_key, to_repo_key
        repo_key = to_repo_key(repo_key, provider="github")
        _prov, owner, _name = parse_repo_key(repo_key)
        if owner:
            from src.auth.grape_account_db import get_grape_account_by_username, get_user_identities_with_tokens
            acc = get_grape_account_by_username(owner)
            if acc:
                for i in get_user_identities_with_tokens(acc["id"]):
                    if i.get("provider") == "github" and i.get("access_token"):
                        return i["access_token"]
    except Exception:
        pass
    return ""
```

**devops_agent_session repo_github_token 列兼容**：

devops_agent_session 表（db_setup.py）只有 repo_gitcode_token 列，没有 repo_github_token 列。
GitHubProvider.session_token_field() 返回 "repo_github_token"——查 DB 时列不存在会报错。

处理方式：R16.5.3 已要求 ALTER TABLE devops_agent_session ADD COLUMN token_status。同时需新增 ALTER TABLE devops_agent_session ADD COLUMN repo_github_token TEXT DEFAULT ''（幂等 ALTER，try-except 保护）。

DevopsAgentSessionToken._fetch 内部 try-except 保护：如果列不存在（未 ALTER），降级为返回空（GitHub 仓库不走 devops_agent_session 第 1 级，直接走第 2 级 managed_repos）。

### 8.5 用户 token 特殊处理

用户 token 策略（UserToken）与其他策略的区别：

| 维度 | RepoBotToken / GlobalBotToken | UserToken |
|------|------------------------------|-----------|
| token 类型 | PAT（手动输入） | OAuth token 或 PAT |
| 自动刷新 | 无 | OAuth token 有 refresh_token 时自动刷新 |
| 过期检测 | 主动健康检查（R17.2） | 被动检测（R10，task 401 时触发） |
| 过期标记 | managed_repos / meta 表 | external_identities.status=4 |
| 过期后行为 | 短路返回空串 | 仍返回 token（供 best-effort 发评论） |
| 恢复方式 | 手动重新配置 | 用户重新绑定 |

UserToken 策略的 `_fetch` 内部调用 `get_user_token_with_refresh`（已有逻辑），包含：
1. `_has_refresh_token` 判断是否有 refresh_token
2. 有 → 检查过期 + 调 `refresh_oauth_token` 自动刷新
3. 无（PAT）→ 直接返回 token，不刷新
4. status=4 → 仍返回 token（R10 设计意图，best-effort 发评论）

UserToken 的 `_is_expired` 返回 False（不短路）——即使 status=4 也返回 token，保证 best-effort 发评论能力。这是用户 token 策略的特殊行为，在子类中实现，不影响其他策略。

### 8.5 需求

#### R18.1 Token 策略基类

- R18.1.1：定义 TokenStrategy 基类，包含 get_token / _fetch / _is_expired / mark_expired / clear_expired 方法
- R18.1.2：get_token 内部检查过期标记，过期则短路返回空串
- R18.1.3：每个策略有唯一 error_code 属性

#### R18.2 四级策略实现

- R18.2.1：DevopsAgentSessionToken — 第 1 级 devops_agent_session.repo_gitcode_token
- R18.2.2：RepoBotToken — 第 2 级 managed_repos.bot_token，含 host_user_id 守卫 + 过期短路
- R18.2.3：GlobalBotToken — 第 3 级全局 bot token，含 host_user_id 守卫 + 过期短路
- R18.2.4：UserToken — 第 4 级用户 token，含 OAuth 自动刷新 + status=4 不短路（best-effort）

#### R18.3 统一入口 TokenResolver

- R18.3.1：TokenResolver 按优先级链执行策略，第一个非空 token 胜出
- R18.3.2：detect_and_mark_expired 方法供 fail_task 401 统一调用
- R18.3.3：支持跳过特定策略（如 get_repo_bot_token 跳过第 1 级）

#### R18.4 向后兼容

- R18.4.1：现有 13 个函数保留为薄包装，委托 TokenResolver
- R18.4.2：调用方无需改动（函数签名和返回值不变）
- R18.4.3：现有测试全部通过
- R18.4.4：get_user_token_with_refresh / get_user_github_token_with_refresh（deps.py）不被 TokenResolver 替代——它们是 username → token 的函数，UserToken 策略内部调用它们，保持原样
- R18.4.5：get_repo_bot_token_no_global 的 fallback 链保持与现有代码一致（仓库级 → devops_agent_session → 用户 token，跳过全局）
- R18.4.6：所有短路检查（R16.4）新增在现有 token 获取逻辑之后，未标记过期的 token 走原有逻辑完全不变

#### R18.5 provider 接口隔离

- R18.5.1：定义 VCSProvider 抽象接口，包含 verify_token / get_user_login / add_collaborator / remove_collaborator / setup_webhook / remove_webhook / token_storage_field / webhook_token_field / session_token_field / global_bot_meta_key / pat_create_url 方法
- R18.5.2：GitCodeProvider 实现 VCSProvider——API https://gitcode.com/api/v5/user，存储列 bot_gitcode_token / gitcode_webhook_token / repo_gitcode_token，meta key global_bot_gitcode_token
- R18.5.3：GitHubProvider 实现 VCSProvider——API https://api.github.com/user，存储列 bot_github_token / github_webhook_token / repo_github_token，meta key global_bot_github_token
- R18.5.4：TokenStrategy 基类持有 VCSProvider 实例，_fetch / _is_expired / mark_expired 内部通过 provider 接口获取存储字段名，不硬编码 if provider == "gitcode" 分支
- R18.5.5：TokenResolver 通过 create_provider 工厂方法创建对应 VCSProvider 实例传入策略链
- R18.5.6：新增 provider 时只需实现 VCSProvider 接口 + 在 create_provider 工厂方法注册，不需要修改任何策略类代码
- R18.5.7：R15 仓库级 token 配置流程中的验证 token / 邀 bot / 重建 webhook 全部通过 VCSProvider 接口调用，不直接调 _req / _github_req / add_collaborator / add_github_collaborator。_rebuild_webhook_with_bot_token 内部的 if provider == "github" 分支也改为通过 provider.remove_webhook() / provider.setup_webhook() 调用，接收 VCSProvider 实例而非 provider 字符串
- R18.5.8：R17 健康检查也通过 provider.verify_token() 调用 VCS API，不直接调 _verify_bot_token。R17.2 健康检查遍历仓库级 token 时按 provider 创建 VCSProvider 实例调 verify_token
- R18.5.9：VCSProvider 接口新增 fetch_user_token(username, user_id) → str 方法——GitCodeProvider 实现调 get_user_token_with_refresh，GitHubProvider 实现调 get_user_github_token_with_refresh。UserToken._fetch 通过 self.provider.fetch_user_token(username, user_id) 调用，不判断 provider 类型（避免 isinstance 硬编码分支）
- R18.5.10：detect_and_mark_expired 中 provider.verify_token(token) 设短超时（5s），避免 VCS API 响应慢阻塞 fail_task。超时视为验证失败（标记过期），下次健康检查 200 时自动清除标记

### R18.6 Token 操作职责链（TokenActionHandler）

#### 问题

R18.1-R18.5 的策略链只解决"取 token"——TokenResolver 返回一个 token 字符串，所有调用方用同一个字符串执行操作。但 4 级 token 在"用 token 做什么"上有各自独立的职责差异：自循环检测逻辑不同、评论 fallback 链不同、过期标记位置不同、webhook 操作权限不同。这些差异当前散落在 fail_task、_post_result_to_issue、pipeline 自循环检测等处的 if _is_user_token_repo / if host_user_id > 0 分支中，不可控且不完整（路径 B 自循环缺口即因此产生）。

#### 设计

引入 TokenActionHandler 职责链，与 TokenStrategy 策略链串联。策略链负责"取 token + 级别标识"，职责链负责"按级别执行差异化操作"。

#### 4 级 token 职责差异矩阵

| 维度 | 第 1 级 session | 第 2 级 仓库级 bot | 第 3 级 全局 bot | 第 4 级 用户 token |
|------|----------------|------------------|----------------|------------------|
| actor | 取决于 token 所有者 | managed_repos.bot_username | global_bot_usernames | host_user VCS username |
| 自循环检测 | 查 session 配置 | managed_repos.bot_username | global_bot_usernames | host_vcs_username + 路径 B 判断（R20.1） |
| 过期影响 | agent 专属 | 单仓库 | 所有路径 A 仓库 | 用户路径 B 仓库 |
| 评论 fallback | 无（scope 受限） | 全局 bot（如果有效） | 仓库级 bot（如果有效）/ 无 | 无（路径 B 无 bot） |
| 过期标记 | devops_agent_session.token_status | managed_repos.bot_token_status | meta.global_bot_token_status | external_identities.status=4 |
| 短路后降级 | 第 2 级 | 第 3 级 | 第 4 级（路径 B）/ 无（路径 A） | 不短路（best-effort） |
| webhook 操作 | 不涉及 | R15 邀 bot + 重建 + 旧 bot 移除 | auto_upgrade | webhook_secret 解耦 |
| 配置入口 | agent 管理员 | PUT /repos/bot-token | save_config | 用户重新绑定 |

#### TokenResult

策略链返回 TokenResult 而非裸字符串，携带级别信息供职责链分发：

```python
class TokenResult:
    """token 获取结果，携带级别信息。"""
    token: str           # token 字符串（向后兼容：薄包装取 .token 返回 str）
    level: str           # "session" / "repo_bot" / "global_bot" / "user"
    provider: VCSProvider  # VCSProvider 实例
    repo_key: str        # 仓库标识
    expired: bool        # 是否已标记过期（用户 token status=4 时 expired=True 但仍返回 token）
```

#### TokenActionHandler 接口

```python
class TokenActionHandler(ABC):
    """token 操作职责链接口。按级别差异化执行 token 相关操作。"""

    @abstractmethod
    def is_self_loop_actor(self, actor: str, event_type: str,
                           result: TokenResult) -> bool:
        """判断 actor 是否是 agent 回声（自循环检测）。"""

    @abstractmethod
    def get_comment_token(self, result: TokenResult) -> str:
        """获取用于发 Issue/PR 评论的 token。
        可能不同于执行 token——例如仓库级 bot 过期时 fallback 到全局 bot。"""

    @abstractmethod
    def post_issue_comment(self, repo_key: str, issue_num: str,
                           body: str, result: TokenResult) -> bool:
        """发 Issue 评论。按级别差异化处理 actor 和标记。"""

    @abstractmethod
    def mark_expired(self, result: TokenResult) -> str:
        """标记当前级别 token 过期，返回 error_code。"""

    @abstractmethod
    def clear_expired(self, result: TokenResult):
        """清除当前级别过期标记（token 重新配置时调用）。"""
```

#### SessionTokenHandler（第 1 级）

```python
class SessionTokenHandler(TokenActionHandler):
    """第 1 级：devops_agent_session token。agent 专属配置，scope 可能受限。"""

    def is_self_loop_actor(self, actor, event_type, result):
        # session token 的 actor 取决于 token 所有者
        # 如未实现独立过期标记，降级为不检测
        return False

    def get_comment_token(self, result):
        # session token scope 可能受限，不能用于发评论
        # 降级到下一级：返回空，让调用方走 RepoBotTokenHandler
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result):
        # session token 通常不用于发评论
        return False

    def mark_expired(self, result):
        # 写 devops_agent_session.token_status = 'expired'
        # 如未实现独立过期标记，仅记日志
        return "SESSION_TOKEN_EXPIRED"

    def clear_expired(self, result):
        # 清 devops_agent_session.token_status = ''
        pass
```

#### RepoBotTokenHandler（第 2 级）

```python
class RepoBotTokenHandler(TokenActionHandler):
    """第 2 级：仓库级 bot token。单仓库独立 bot 账号。"""

    def is_self_loop_actor(self, actor, event_type, result):
        # actor = managed_repos.bot_username（仓库级 bot 账号）
        # 查 managed_repos.bot_username WHERE repo_key=?
        bot_username = get_repo_bot_username(result.repo_key)
        return actor == bot_username if bot_username else False

    def get_comment_token(self, result):
        # 仓库级 bot 有效 → 用仓库级 bot token
        if result.token and not result.expired:
            return result.token
        # 仓库级 bot 过期 → fallback 到全局 bot（如果有效）
        global_token = get_global_bot_token(result.provider)
        if global_token and not is_global_bot_expired(result.provider):
            return global_token
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result):
        comment_token = self.get_comment_token(result)
        if not comment_token:
            return False
        # actor = 仓库级 bot_username → 在 bot_usernames 中 → 自循环检测命中
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        return post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token)

    def mark_expired(self, result):
        # 写 managed_repos.bot_token_status = 'expired'
        set_repo_bot_token_status(result.repo_key, "expired")
        return "REPO_TOKEN_EXPIRED"

    def clear_expired(self, result):
        # 清 managed_repos.bot_token_status = ''
        set_repo_bot_token_status(result.repo_key, "")
```

#### GlobalBotTokenHandler（第 3 级）

```python
class GlobalBotTokenHandler(TokenActionHandler):
    """第 3 级：全局 bot token。所有路径 A 仓库共用。"""

    def is_self_loop_actor(self, actor, event_type, result):
        # actor = global_bot_usernames
        bot_usernames = get_global_bot_usernames(result.provider)
        return actor in bot_usernames if bot_usernames else False

    def get_comment_token(self, result):
        # 全局 bot 有效 → 用全局 bot token
        if result.token and not result.expired:
            return result.token
        # 全局 bot 过期 → fallback 到仓库级 bot（如果有效）
        repo_bot_token = get_repo_bot_token(result.repo_key, result.provider)
        if repo_bot_token and not is_repo_bot_expired(result.repo_key):
            return repo_bot_token
        # 路径 A 仓库无 host_user_id → 用户 token 也拿不到 → 无可用 token
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result):
        comment_token = self.get_comment_token(result)
        if not comment_token:
            return False
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        return post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token)

    def mark_expired(self, result):
        # 写 meta.global_bot_token_status = 'expired'
        set_meta_value("global_bot_token_status", "expired")
        return "GLOBAL_TOKEN_EXPIRED"

    def clear_expired(self, result):
        # 清 meta.global_bot_token_status = ''
        set_meta_value("global_bot_token_status", "")
```

#### UserTokenHandler（第 4 级）

```python
class UserTokenHandler(TokenActionHandler):
    """第 4 级：用户 token。路径 B 专属，actor = host_user VCS username。

    核心矛盾：路径 B 下 agent 和用户用同一个 token，actor 完全相同。
    不能仅基于 actor 判断——需要引入 task 执行状态作为额外维度。
    仅当"该 issue 有正在执行的 task" + "actor == host_vcs_username" + "事件类型是 agent 可能发的操作"
    + "评论内容不含开发者指令" 四条都满足时，才视为 agent 回声跳过。
    """

    def is_self_loop_actor(self, actor, event_type, result):
        # 条件 3：仅对 agent 可能发的操作类型检测
        if event_type not in ("issue_comment", "note_hook_pr", "issue_updated"):
            return False  # issue_opened 等用户正常操作豁免

        # 条件 2：actor == host_user 的 VCS 用户名
        host_vcs_username = get_host_vcs_username(result.repo_key, result.provider)
        if not host_vcs_username:
            return False  # 查询失败 → 降级为不检测
        if actor != host_vcs_username:
            return False  # actor 不是 host_user → 不是自循环

        # 条件 4：该 issue/PR 有正在执行的 task
        issue_id = resolve_issue_id_from_event(event_type, result)
        if not issue_id:
            return False  # 无法解析 issue_id → 不检测
        has_running_task = check_running_task(result.repo_key, issue_id)
        if not has_running_task:
            return False  # 无正在执行的 task → 用户主动操作 → 放行

        # 四条件全部满足 → agent 回声 → 跳过
        return True

    def get_comment_token(self, result):
        # 路径 B = 无全局 bot token
        # 用户 token 有效 → best-effort 用用户 token
        if result.token:
            return result.token
        # 用户 token 过期 → 尝试全局 bot token（可能已配置但尚未 auto_upgrade）
        global_token = get_global_bot_token(result.provider)
        if global_token:
            return global_token  # best-effort（可能 403）
        return ""

    def post_issue_comment(self, repo_key, issue_num, body, result):
        comment_token = self.get_comment_token(result)
        if not comment_token:
            return False
        # 注入标记——即使 agent 直接发评论也通过 Handler 注入标记
        # 但 agent 自己调 VCS API 发的评论不经过 Handler → 标记缺失 → 依赖 is_self_loop_actor 兜底
        body_with_marker = body + "\n<!-- grape-auto-comment -->"
        try:
            return post_comment_via_provider(result.provider, repo_key, issue_num, body_with_marker, comment_token)
        except Exception:
            # 用户 token 过期 → 回帖失败 → R20.2 降级处理
            return False

    def mark_expired(self, result):
        # 写 external_identities.status = 4
        host_user_id = get_host_user_id(result.repo_key)
        mark_identity_expired(host_user_id, result.provider.provider_name)
        return "TOKEN_EXPIRED"

    def clear_expired(self, result):
        # 用户重新绑定 → 恢复 status=1（已有逻辑）
        pass
```

#### 职责链分发

```python
def create_action_handler(result: TokenResult) -> TokenActionHandler:
    """根据 TokenResult.level 创建对应 Handler。"""
    if result.level == "session":
        return SessionTokenHandler()
    elif result.level == "repo_bot":
        return RepoBotTokenHandler()
    elif result.level == "global_bot":
        return GlobalBotTokenHandler()
    elif result.level == "user":
        return UserTokenHandler()
    return None  # 无 token → 无 Handler
```

#### 调用方使用

```python
# 取 token + 级别
result = TokenResolver(provider).resolve(repo_key)
if not result or not result.token:
    # 无 token → R16.6 NO_TOKEN_AVAILABLE
    pass

# 需要级别差异的操作 → 通过 Handler
handler = create_action_handler(result)

# 步骤 2.0b 开发者指令检测（移到自循环检测之前，R20.6.2）
# /done /cancel /resume 等指令优先处理，不进入自循环检测
if event_type in ("issue_comment", "note_hook_pr"):
    if check_developer_command(note_content):
        handle_developer_command(...)
        skip("developer_command")
        return

# 自循环检测（pipeline 步骤 2.0，通过 Handler 调用）
if handler and handler.is_self_loop_actor(actor, event_type, result):
    skip("self_loop")
    return

# 发评论（fail_task / _post_result_to_issue）
comment_token = handler.get_comment_token(result)
handler.post_issue_comment(repo_key, issue_num, body, result)

# 不需要级别差异的操作 → 直接用 token 字符串
# git push / git clone / API 查询等
git_push(result.token)
```

#### 辅助函数

```python
def get_host_vcs_username(repo_key, provider):
    """获取路径 B 仓库的 host 用户 VCS 平台用户名。
    优先从 managed_repos.host_vcs_username 读取（R16.5.5 新增列），
    为空时 fallback 到 external_identities 查询。"""
    # 1. 查 managed_repos.host_vcs_username
    username = get_managed_repo_field(repo_key, "host_vcs_username")
    if username:
        return username
    # 2. fallback: 从 external_identities 查 host_user_id 对应的 provider username
    host_user_id = get_host_user_id(repo_key)
    if host_user_id > 0:
        identity = get_external_identity(host_user_id, provider.provider_name)
        return identity.get("username", "") if identity else ""
    return ""

def check_running_task(repo_key, issue_id):
    """检查该 issue 是否有正在执行的 task（R20.1.8 + R20.10.5）。
    
    两级查询：
    1. agent_tasks.status=running/dispatched（webhook pipeline 触发的 task）
    2. devops_agent_session.last_active_at > now-300s AND status=active（飞书 VOD / Chat direct 等非 webhook 执行）
    """
    with db_lock:
        c = db.cursor()
        # 1. 查 agent_tasks
        c.execute(
            "SELECT 1 FROM agent_tasks WHERE repo_key=? AND issue_id=? "
            "AND status IN ('running','dispatched') LIMIT 1",
            (repo_key, issue_id)
        )
        if c.fetchone():
            return True
        # 2. 降级：查 devops_agent_session 最近活跃（覆盖非 webhook agent 执行）
        c.execute(
            "SELECT 1 FROM devops_agent_session WHERE repo_key=? "
            "AND status='active' AND last_active_at > datetime('now', '-300 seconds') LIMIT 1",
            (repo_key,)
        )
        return c.fetchone() is not None

def check_developer_command(note_content):
    """检查评论内容是否含开发者指令（/done /cancel /resume 等）。"""
    note_lower = (note_content or "").lower().strip()
    return any(kw in note_lower for kw in [
        "/done", "完成任务", "任务完成", "确认完成",
        "/cancel", "取消任务",
        "/resume", "恢复任务"
    ])
```

#### 向后兼容

- 现有 13 个函数保留为薄包装，返回 `result.token`（str），签名不变
- 新增 `TokenResolver.resolve()` 返回 `TokenResult`，需要级别信息的调用方调新接口
- `TokenResolver.get_token()` 保留，内部调 `resolve()` 取 `.token`，向后兼容
- pipeline 自循环检测、fail_task、_post_result_to_issue 改为调 `resolve()` + `create_action_handler()`
- git push / git clone 等无级别差异的操作继续调 `get_token()`（str）

#### 需求项

- R18.6.1：定义 TokenResult 结构（token + level + provider + repo_key + expired），TokenResolver 新增 resolve() 返回 TokenResult
- R18.6.2：定义 TokenActionHandler 抽象接口（is_self_loop_actor / get_comment_token / post_issue_comment / mark_expired / clear_expired）
- R18.6.3：SessionTokenHandler 实现——is_self_loop_actor 降级为不检测，get_comment_token 返回空（scope 受限不用于发评论），mark_expired 写 devops_agent_session.token_status
- R18.6.4：RepoBotTokenHandler 实现——is_self_loop_actor 查 managed_repos.bot_username，get_comment_token 仓库级过期时 fallback 全局 bot，mark_expired 写 managed_repos.bot_token_status
- R18.6.5：GlobalBotTokenHandler 实现——is_self_loop_actor 查 global_bot_usernames，get_comment_token 全局过期时 fallback 仓库级 bot，mark_expired 写 meta.global_bot_token_status
- R18.6.6：UserTokenHandler 实现——is_self_loop_actor 四条件组合判断：(1)路径B仓库 (2)actor==host_vcs_username (3)event_type in issue_comment/note_hook_pr/issue_updated (4)该issue有正在执行的task(status=running/dispatched)。条件4不满足→用户主动操作→放行。get_comment_token 用户 token 过期时 best-effort 全局 bot，mark_expired 写 external_identities.status=4。开发者指令(/done /cancel /resume)在自循环检测之前处理，不被误拦截
- R18.6.7：create_action_handler 工厂方法按 TokenResult.level 分发 Handler
- R18.6.8：pipeline 自循环检测改为通过 handler.is_self_loop_actor() 调用，消除现有 if actor in bot_usernames + if _is_user_token_repo 散落分支
- R18.6.9：fail_task 评论 token 选择改为通过 handler.get_comment_token() 调用，消除现有 if _is_path_b 散落分支
- R18.6.10：_post_result_to_issue 改为通过 handler.post_issue_comment() 调用，统一标记注入和 actor 处理
- R18.6.11：get_token_with_level() 新增——返回 TokenResult，需要级别信息的调用方调此接口；get_token() 保留——返回 str，向后兼容
- R18.6.12：向前兼容——现有 13 个薄包装函数签名不变返回 str，内部调 resolve().token；新增 resolve() 供需要级别信息的调用方使用
- R18.6.13：Repo.bot_token 改为调 service 层方法 get_repo_bot_token()（R20.11），内部调 TokenResolver.get_token()——所有调 Repo.bot_token 的场景（Chat/飞书/events/onboarding/project_info）自动继承短路/过期检测。Repo 数据模型不直接 import TokenResolver，避免循环依赖
- R18.6.14：Chat _resolve_gitcode_token 改为调 TokenResolver.resolve() + Handler.get_comment_token()——Chat agent 注入的 token 经过级别感知和过期检测（R20.7.2）
- R18.6.15：飞书 VOD handler 改为调 TokenResolver.resolve()——VOD agent 注入的 token 经过级别感知（R20.7.4）
- R18.6.16：check_running_task 补充降级查询——除 agent_tasks.status=running/dispatched 外，也查 devops_agent_session.last_active_at > now-300s AND status=active（覆盖飞书 VOD / Chat direct 等不走 agent_tasks 的执行场景，R20.10.5）

## 九、其他需求

### R19.1 日志脱敏

- R19.1.1：健康检查日志不包含 token 原文或片段
- R19.1.2：fail_task 日志不包含 token 原文或片段
- R19.1.3：error_code 中不包含 token 片段

### R19.2 前端 i18n

- R19.2.1：zh/en app.json 新增 error_code 映射 key（TOKEN_EXPIRED / GLOBAL_TOKEN_EXPIRED / REPO_TOKEN_EXPIRED / NO_TOKEN_AVAILABLE）
- R19.2.2：zh/en app.json 新增"PAT 无自动续期"提示 key
- R19.2.3：settings 页 token 状态提示 zh/en 双语

### R19.3 解绑/删除仓库时的清理

- R19.3.1：用户解绑时检查是否有路径 B 仓库依赖该用户 token
- R19.3.2：有依赖时返回仓库列表 + 警告提示
- R19.3.3：用户确认解绑后，路径 B 仓库的 host_user_id 仍保留（指向已删除的 user），get_repo_token_with_refresh 第 4 级查不到用户 token → 返回空 → task fail（NO_TOKEN_AVAILABLE）。仓库保持不可用状态，直到管理员配置全局 bot token 后 auto_upgrade_path_b_repos 将其切换到路径 A
- R19.3.4：删除仓库时清理 bot_token_status 过期标记 + 清理 devops_agent_session 中的 repo_gitcode_token + 清理 devops_agent_session.token_status

### R19.4 GitHub fine-grained PAT 过期提醒

- R19.4.1：GitHub token 过期提示中包含 GitHub PAT 创建页链接（通过 provider.pat_create_url() 获取，GitHubProvider 返回 https://github.com/settings/tokens）
- R19.4.2：GitCode token 过期提示中包含 GitCode PAT 创建页链接（通过 provider.pat_create_url() 获取，GitCodeProvider 返回 https://gitcode.com/profile/personal_access_tokens）

## 十、场景与场景用例

### 场景 1：无 OAuth client/secret + 无全局 bot token（纯路径 B）

**初始状态**：系统未配置 OAuth client/secret，未配置全局 bot token。

#### 用例 1.1：用户绑定 PAT 并托管仓库

1. 用户注册（用户名+密码，无邮箱验证码）
2. 用户在 account 页手动输入 GitCode PAT
3. 后端调 VCS API GET /user 验证 PAT 有效性 → 有效
4. 写入 external_identities（access_token + 无 refresh_token）
5. 用户托管仓库 → 路径 B（无全局 bot token）
6. 用用户 token 创建 webhook（随机密钥做密码）
7. DB 写入：host_user_id=用户ID, bot_gitcode_token="", webhook_secret=随机密钥
8. 运行时 get_repo_token_with_refresh 第 4 级命中用户 token

#### 用例 1.2：用户 token 过期

1. 用户 PAT 过期
2. agent 执行 task → 401 失败
3. fail_task 统一检测（R16.1）：host_user_id > 0 → 用户 token 过期
4. 标记 external_identities.status=4
5. task errors append TOKEN_EXPIRED
6. account 页显示过期提示
7. 用户重新绑定 PAT → 恢复 status=1

#### 用例 1.3：管理员配置全局 bot token 后自动升级

1. 管理员在 settings 页配置全局 bot token
2. save_config 检测 bot token 从无到有 → 触发 auto_upgrade_path_b_repos
3. 后台线程遍历路径 B 仓库
4. 用 host 用户 token 邀 bot 为协作者 → accept invite
5. 用 bot token 重建 webhook（删旧随机密钥 webhook，建新 bot token webhook）
6. 写 DB：bot_gitcode_token=bot token, host_user_id=0, webhook_secret=""
7. 仓库切换到路径 A

#### 用例 1.4：管理员配置全局 bot token 但 token 无效

1. 管理员输入错误的全局 bot token
2. auto_upgrade 用错误 token 邀 bot → VCS API 401
3. 升级失败，仓库保持路径 B（用户 token 仍可用）
4. 日志记录失败原因
5. 管理员在 settings 页看到全局 bot token 状态异常

#### 用例 1.5：用户 token 过期后 task 无 token 可用

1. 用户 PAT 过期 → status=4 标记
2. UserToken 策略不短路（status=4 仍返回 token，best-effort 发评论）
3. 但 token 实际已过期 → git clone/push 401 失败
4. fail_task 检测 401 → 已标记 status=4 → 不重复标记
5. task errors 已有 TOKEN_EXPIRED
6. dashboard 显示"你的 Token 已过期，请在设置页重新绑定"
7. 用户重新绑定 → 恢复

### 场景 2：有全局 bot token（路径 A）

**初始状态**：系统已配置全局 bot token，仓库走路径 A（host_user_id=0, bot_gitcode_token=全局 bot token）。

#### 用例 2.1：有仓库权限的用户配置仓库级 bot token

1. 用户 A 通过 PUT /repos/bot-token 给仓库配独立 bot token
2. 验证 token 有效性（调 VCS API GET /user）→ 有效
3. 获取 bot username
4. 如果新 bot 账号与旧 bot_username 不同 → 邀请新 bot 为协作者
5. 用新 bot token 重建 webhook（删旧 webhook，建新 webhook）
6. 写 DB：bot_gitcode_token=新 bot token + 清 host_user_id + 清 webhook_secret
7. 之后运行时 get_repo_token_with_refresh 第 2 级命中仓库级 bot token

#### 用例 2.2：用户配置了无效的仓库级 bot token

1. 用户输入错误的 bot token
2. 验证 token 有效性（调 VCS API GET /user）→ 401
3. 返回 400 + TOKEN_INVALID
4. 不写 DB、不重建 webhook、不清 host_user_id
5. 仓库保持原状态不变

#### 用例 2.3：全局 bot token 过期

1. 全局 bot token 过期
2. 所有路径 A 仓库的 task 401 失败
3. fail_task 统一检测（R16.1）：host_user_id=0 + 无仓库级 bot_token → 全局 bot token 过期
4. 验证全局 bot token 有效性（调 VCS API GET /user）→ 401
5. 标记 meta.global_bot_token_status=expired
6. task errors append GLOBAL_TOKEN_EXPIRED
7. 所有路径 A 仓库用户在 dashboard 看到"全局 Bot Token 已过期，请联系管理员重新配置"
8. 管理员在 settings 页看到全局 bot token 状态为"已过期"
9. _get_global_bot_token 短路返回空串（避免每次 401 重试）
10. 管理员重新配置全局 bot token → 清除过期标记 → 恢复

#### 用例 2.4：仓库级 bot token 过期

1. 仓库级 bot token 过期
2. 该仓库的 task 401 失败
3. fail_task 统一检测（R16.1）：host_user_id=0 + 有仓库级 bot_token → 仓库级 bot token 过期
4. 验证仓库级 bot token 有效性 → 401
5. 标记 managed_repos.bot_token_status=expired
6. task errors append REPO_TOKEN_EXPIRED
7. 该仓库用户在 dashboard 看到"仓库 Bot Token 已过期，请联系有权限的成员重新配置"
8. get_bot_token_by_repo 短路返回空串
9. 有仓库权限的用户重新配置 → 清除过期标记 → 恢复

#### 用例 2.5：全局 bot token + 仓库级 bot token 都过期

1. 全局 bot token 过期 → 健康检查标记 meta 过期
2. 仓库级 bot token 也过期 → 健康检查标记 managed_repos 过期
3. task 获取 token：第 2 级短路 + 第 3 级短路 + 第 4 级无 host_user_id → 返回空
4. task fail（不是 401，是"无 token"）
5. fail_task 检测无 token → 写入 NO_TOKEN_AVAILABLE error_code（R16.6）
6. dashboard 显示"仓库 Bot Token 已过期"（先显示仓库级，因为是当前生效的策略）
7. 有仓库权限的用户重新配置仓库级 token → 仓库恢复
8. 或管理员重新配置全局 bot token → 所有路径 A 仓库恢复

#### 用例 2.6：有仓库权限的用户清除仓库级 bot token 恢复使用全局 bot token

1. 仓库当前用仓库级 bot token（路径 A）
2. 有仓库权限的用户通过 PUT /repos/bot-token 传空串
3. 清除 managed_repos.bot_gitcode_token
4. 清除 bot_token_status
5. 之后 get_repo_token_with_refresh 第 2 级为空 → 第 3 级命中全局 bot token
6. 仓库恢复使用全局 bot token
7. webhook 仍用旧 bot token 的密码 → 与全局 bot token 不匹配 → 需要 repair_host 重建 webhook

### 场景 3：有 OAuth client/secret

**初始状态**：系统配置了 OAuth client/secret，用户通过 OAuth 绑定。

#### 用例 3.1：用户通过 OAuth 绑定并托管仓库

1. 用户在 account 页点"OAuth 绑定"
2. 跳转 GitCode/GitHub OAuth 授权页
3. 授权后回调 → 获取 access_token + refresh_token + expires_in
4. 写入 external_identities（access_token + refresh_token + status=1）
5. 如果有全局 bot token → 走路径 A 托管
6. 如果无全局 bot token → 走路径 B 托管（用户 token 驱动）

#### 用例 3.2：用户 OAuth token 临近过期自动刷新

1. get_user_token_with_refresh 被调用
2. _has_refresh_token=True（OAuth 绑定有 refresh_token）
3. 检查 token_expires_at → 临近过期（< 600 秒）
4. 调 refresh_oauth_token 用 refresh_token 换新 access_token
5. 更新 external_identities + user_credentials
6. 返回新 token
7. 用户无感知

#### 用例 3.3：用户 OAuth token 的 refresh_token 也过期

1. get_user_token_with_refresh 调 refresh_oauth_token → 失败（refresh_token 也过期）
2. 返回空 token
3. task 401 失败 → fail_task 检测 → 标记 status=4
4. account 页显示过期提示
5. 用户重新通过 OAuth 绑定 → 获取新 access_token + refresh_token → 恢复 status=1

### 场景 4：健康检查主动发现过期

#### 用例 4.1：健康检查发现全局 bot token 过期（task 还没执行）

1. 后台线程每小时用全局 bot token 调 VCS API GET /user
2. 401 → 标记 meta.global_bot_token_status=expired
3. _get_global_bot_token 短路返回空串
4. 下次 task 执行时第 3 级短路 → 走第 4 级用户 token（如果有）或返回空
5. dashboard 显示"全局 Bot Token 已过期，请联系管理员重新配置"
6. 管理员重新配置 → 清除标记 → 恢复

#### 用例 4.2：健康检查发现仓库级 bot token 过期

1. 后台线程遍历有仓库级 bot token 的仓库
2. 用仓库级 bot token 调 VCS API GET /user → 401
3. 标记 managed_repos.bot_token_status=expired
4. get_bot_token_by_repo 短路返回空串
5. 该仓库用户在 dashboard 看到提示
6. 有仓库权限的用户重新配置 → 清除标记 → 恢复

#### 用例 4.3：健康检查发现 token 恢复有效

1. 管理员重新配置全局 bot token
2. save_config 清除 meta 过期标记
3. 下次健康检查用新 token 调 VCS API GET /user → 200
4. 确认标记已清除 → 短路解除
5. task 正常执行

#### 用例 4.4：多实例同时健康检查

1. 集群有 3 个实例，每个实例每小时触发健康检查
2. 3 个实例同时尝试获取分布式锁 token_health_check
3. 只有实例 A 拿到锁 → 执行健康检查
4. 实例 B 和 C 跳过（yield False，body 不执行）
5. 实例 A 完成后释放锁
6. 无重复 API 调用、无重复 DB 写

#### 用例 4.5：大量仓库级 token 健康检查触发 rate limit

1. 系统有 100 个仓库各有仓库级 bot token
2. 健康检查分批执行（每 10 个一批，间隔 1 秒）
3. 100 个仓库分 10 批，总耗时约 10 秒
4. 不触发 VCS API rate limit

**分析**：GET /user 是最轻量的 API 调用。实际数量评估：
- 只有全局 bot token（无仓库级）：1 次/小时
- 10 个仓库有仓库级 token：11 次/小时
- 100 个仓库有仓库级 token：101 次/小时（GitHub 5000/h 不会触发）
- 仓库级 bot token 是罕见配置（大多数仓库用全局 bot token），不太可能有 100+ 仓库各自配独立 token
- 分批执行作为防御性措施保留，实际触发 rate limit 的概率极低

### 场景 5：配置 token 时提示无自动续期

#### 用例 5.1：用户手动输入 PAT 绑定

1. 用户在 account 页点"手动令牌"tab
2. 前端检测到未配 OAuth App → 显示"PAT 不会自动续期"提示卡片（R13.3 已有）
3. 用户输入 PAT → 后端验证 → 写入 external_identities（无 refresh_token）
4. _has_refresh_token=False → 不会尝试刷新

#### 用例 5.2：管理员配置全局 bot token

1. 管理员在 settings 页输入全局 bot token
2. 前端显示"此 Token 为 PAT 类型，不会自动续期，过期后需手动重新配置"
3. 管理员确认 → save_config 写入 meta 表
4. 健康检查每小时验证有效性

#### 用例 5.3：有仓库权限的用户配置仓库级 bot token

1. 用户通过 PUT /repos/bot-token 输入仓库级 bot token
2. 返回结果中包含"此 Token 为 PAT 类型，不会自动续期，过期后需手动重新配置"提示
3. 前端显示提示
4. 健康检查每小时验证该仓库 token 有效性

### 场景 6：多实例/集群部署

#### 用例 6.1：实例 A 标记过期后实例 B 的缓存延迟

1. 实例 A 健康检查发现全局 bot token 过期 → 标记 meta.global_bot_token_status=expired
2. 实例 B 的 _get_global_bot_token 有 60s 缓存（_cached_get_meta）→ 仍返回旧 token（未感知过期）
3. 实例 B 的 task 继续用过期 token → 401 失败
4. fail_task 检测 401 → 标记过期（幂等，已标记则不重复）
5. 60s 后实例 B 缓存过期 → 重新读 meta → 感知到过期标记 → 短路

**分析**：可接受 60s 延迟。原因：
- _get_global_bot_token 有 60s 缓存（_cached_get_meta），实例 B 最多 60s 内用旧缓存
- fail_task 的 401 检测是兜底：即使缓存没过期，task 用旧 token 调 API 也会 401，fail_task 会标记过期（幂等，已标记不重复）
- 最坏情况是 60s 内有几个 task 401 失败然后被标记，不影响正确性
- 不需要主动清除其他实例缓存（复杂度高，收益低）

### 场景 7：fail_task 与健康检查并发

#### 用例 7.1：健康检查标记过期后 fail_task 不再触发 401

1. 健康检查标记全局 bot token 过期
2. _get_global_bot_token 短路返回空串
3. task 获取不到 token → 直接 fail（不是 401）
4. fail_task 的 401 检测不触发（因为不是 401 错误）
5. fail_task 检测无 token → 写入 NO_TOKEN_AVAILABLE error_code（R16.6）
6. dashboard 显示"无可用 Token，请联系管理员配置"

### 场景 8：更换仓库级 bot token 时旧 bot 协作者处理

#### 用例 8.1：更换仓库级 bot token 时旧 bot 仍有其他仓库在用

1. 仓库 A 从 bot_old 换到 bot_new
2. 仓库 B 仍在用 bot_old
3. 移除 bot_old 在仓库 A 的协作者身份 → 不影响仓库 B（只移除该仓库的）
4. 如果移除 API 调用失败 → 不影响新 token 写入（best-effort）

### 场景 9：解绑/删除仓库时的清理

#### 用例 9.1：用户解绑 GitCode 账号

1. 路径 B 仓库的 host_user_id 指向该用户
2. 用户解绑 → external_identities 删除
3. get_repo_token_with_refresh 第 4 级查不到用户 token → 返回空
4. 所有该用户托管的路径 B 仓库的 task 无法执行
5. 解绑前提示用户"解绑后以下仓库将无法执行任务" + 仓库列表

#### 用例 9.2：删除仓库时清理过期标记

1. 删除仓库时清理 managed_repos.bot_token_status 过期标记
2. 清理 devops_agent_session 中的 repo_gitcode_token + repo_github_token + token_status
3. 不影响其他仓库

### 场景 10：GitHub fine-grained PAT 过期提醒

#### 用例 10.1：GitHub fine-grained PAT 过期

1. 用户用 GitHub fine-grained PAT 绑定（必须设过期时间，最长 1 年）
2. PAT 过期后无法在 GitHub 重新生成相同权限的 PAT
3. 用户在 account 页看到过期提示
4. 提示中包含"请前往 GitHub 重新创建 Personal Access Token 并重新绑定" + 链接

### 场景 11：日志脱敏

#### 用例 11.1：健康检查日志不泄露 token

1. 健康检查失败时日志记录 "global bot token expired for repo gitcode:owner/repo"
2. 不包含 token 原文或片段
3. fail_task 日志同理

### 场景 12：路径 B agent 评论自循环

**初始状态**：路径 B 仓库（host_user_id > 0, bot_gitcode_token="", webhook_secret=随机密钥），用户 PAT 未过期。

#### 用例 12.1：agent 直接发 Issue 评论触发重复执行

1. 用户在 GitCode 创建 Issue → webhook 触发 pipeline → agent 开始执行
2. agent 执行过程中用用户 token 调 GitCode API POST /repos/{owner}/{repo}/issues/{iid}/comments 发评论
3. 评论 actor = host_user 用户名（不是 bot）
4. 评论内容不含 `<!-- grape-auto-comment -->` 标记（agent 自己发的，不是系统回帖）
5. GitCode webhook 推送 issue_comment 事件
6. pipeline 步骤 2.0b：评论不含开发者指令 → 不命中
7. pipeline 步骤 2.0 自循环检测（通过 UserTokenHandler.is_self_loop_actor）：
   - 条件 1（路径 B）：满足
   - 条件 2（actor == host_vcs_username）：满足
   - 条件 3（issue_comment）：满足
   - 条件 4（task 正在执行）：agent_tasks 查到 running task → 满足
   - 四条件全满足 → 跳过（agent 回声被正确拦截）

#### 用例 12.2：系统回帖（_post_result_to_issue）不触发自循环

1. agent 执行完成 → _post_result_to_issue 用用户 token 发结果评论
2. 评论内容含 `<!-- grape-auto-comment -->` 标记
3. GitCode webhook 推送 issue_comment 事件
4. pipeline 步骤 2.0a：评论含标记 → 命中 → 跳过（system_auto_comment）
5. 不触发重复执行（此路径已正常工作）

#### 用例 12.3：agent 发 PR 评论触发重复执行

1. PR 订阅触发 → agent 执行 QA 审查
2. agent 用用户 token 调 GitCode API POST /repos/{owner}/{repo}/pulls/{pr_id}/comments 发 PR 评论
3. 评论 actor = host_user 用户名
4. pipeline 步骤 2.0 自循环检测（UserTokenHandler）：
   - 条件 3（note_hook_pr）：满足
   - 条件 4（task 正在执行）：满足
   - 四条件全满足 → 跳过

#### 用例 12.4：agent 编辑 Issue description 触发重复执行

1. agent 用用户 token 调 GitCode API PATCH /repos/{owner}/{repo}/issues/{iid} 编辑 issue description
2. GitCode webhook 推送 issue_updated 事件
3. pipeline 步骤 2.0 自循环检测（UserTokenHandler）：
   - 条件 3（issue_updated）：满足
   - 条件 4（task 正在执行）：满足
   - 四条件全满足 → 跳过

### 场景 13：路径 B fail_task 回帖必然失败

**初始状态**：路径 B 仓库，用户 PAT 已过期。

#### 用例 13.1：fail_task 无法发 Issue 评论通知用户

1. 用户 PAT 过期 → agent 执行 task → 401 失败
2. fail_task 检测 401 → 路径 B → 标记 status=4 → task errors append TOKEN_EXPIRED
3. fail_task 尝试发 Issue 评论通知用户（通过 UserTokenHandler.get_comment_token）
4. 用户 token 已过期 → 尝试全局 bot token → 路径 B 无全局 bot token → 空
5. 无可用评论 token → fail_task 记录 COMMENT_POST_FAILED（R20.2.1）
6. dashboard 显示增强提示"Token 已过期且无法发 Issue 评论"（R20.2.2）
7. 不重试（R20.2.3）

#### 用例 13.2：fail_task 回帖成功（全局 bot token 配置后）

1. 用户 PAT 过期 → fail_task 检测 401
2. 管理员已配置全局 bot token（路径 B 仓库尚未 auto_upgrade）
3. UserTokenHandler.get_comment_token：用户 token 过期 → 尝试全局 bot token → 非空
4. 用全局 bot token 发评论 → 可能 403（bot 无仓库权限）或成功
5. best-effort，结果记录日志

### 场景 14：路径 B agent 执行期间 token 过期

**初始状态**：路径 B 仓库，用户 PAT 即将过期（剩余 < 10 分钟）。

#### 用例 14.1：agent 结果回帖失败

1. agent 开始执行 → 获取用户 token（剩余 < 10 分钟）
2. agent 执行耗时 15 分钟（复杂任务）
3. 执行到第 10 分钟时 token 过期
4. agent 完成 → _post_result_to_issue 用用户 token 发结果评论
5. token 已过期 → 401 失败
6. UserTokenHandler.post_issue_comment 返回 False → task errors append RESULT_POST_FAILED（R20.3.2）
7. task 仍标记 completed（R20.3.3）
8. dashboard 显示"结果已执行但无法回帖到 Issue"

#### 用例 14.2：agent 执行中 git push 失败

1. agent 开始执行 → 获取用户 token
2. agent 修改代码 → git push 用用户 token 认证
3. 执行到中途 token 过期
4. git push → 401 失败
5. agent 报错退出 → fail_task 检测 401 → 标记 status=4（R20.3.4）
6. 代码修改未推送到远程

### 场景 15：路径 B auto_upgrade 删旧 webhook 失败

**初始状态**：路径 B 仓库（webhook 用用户 token 创建，webhook_secret=随机密钥），管理员配置全局 bot token 触发 auto_upgrade。

#### 用例 15.1：host 用户 token 过期导致 auto_upgrade 跳过

1. 管理员配置全局 bot token → auto_upgrade_path_b_repos 遍历路径 B 仓库
2. 获取 host 用户 token → host 用户 PAT 已过期 → token 为空
3. 跳过该仓库（R20.4.1）
4. 仓库保持路径 B → 用户 token 仍过期 → task 无法执行
5. 用户重新绑定 PAT 后 → 仓库仍是路径 B → 需要管理员再次触发 auto_upgrade

#### 用例 15.2：bot 无 admin 权限删旧 webhook

1. host 用户 token 有效 → 邀请 bot 为协作者 → 成功
2. _rebuild_webhook_with_bot_token 用 bot token 删旧 webhook
3. 旧 webhook 是用户 token 创建的 → 删 webhook 需要仓库 admin 权限
4. bot 刚被邀请为 maintainer/push（非 admin）→ 删 webhook 403
5. 删旧失败 → 不建新 webhook → 返回空串
6. 不写 DB（R20.4.2）→ 仓库保持路径 B
7. 管理员可手动 repair_host（R20.4.3）

#### 用例 15.3：auto_upgrade 后 webhook_secret 残留

1. auto_upgrade 成功：bot 协作者邀请成功 + webhook 重建成功 + DB 写入 bot_token
2. _update_repo_bot_token 清 webhook_secret='' + 写 webhook_id
3. 新 webhook 的 password 是 bot token（路径 A 模式）
4. get_webhook_token_by_repo：webhook_secret 已清空 → fallback 到 gitcode_webhook_token（=bot token）
5. GitCode webhook 推送：X-GitCode-Token = bot token → 验证匹配 → OK
6. 此场景正常工作

### 场景 16：路径 B webhook 创建权限不足

**初始状态**：用户 A 有仓库 write 权限但无 admin 权限，系统无全局 bot token。

#### 用例 16.1：非 admin 用户托管仓库失败

1. 用户 A 托管仓库 → 路径 B（无全局 bot token）
2. 调 setup_webhook(用户 token, owner, repo, webhook_url, webhook_password=webhook_secret)
3. GitCode/GitHub API 创建 webhook 需要仓库 admin 权限
4. 用户 A 无 admin 权限 → 403
5. 返回 HTTP 403 + error_code=WEBHOOK_PERMISSION_DENIED（R20.5.1）
6. 前端展示"需要仓库 admin 权限创建 webhook"指引文案（R20.5.2）

#### 用例 16.2：admin 用户托管后转移给非 admin 用户

1. 用户 A（admin）托管仓库 → 路径 B → 成功（webhook 用 A 的 token 创建）
2. 用户 A 的 token 过期 → 重新绑定 → token 变更
3. webhook 仍用旧的 webhook_secret（随机密钥，不随 token 变更）→ webhook 验证正常
4. agent 用新 token 执行 task → 正常
5. 此场景正常工作（webhook_secret 与 token 解耦的设计正确）

### 场景 17：路径 B get_all_bot_usernames 不含 host 用户

**初始状态**：路径 B 仓库，host_user_id 指向用户 A。

#### 用例 17.1：自循环检测因 actor 不在 bot_usernames 而失效

1. get_all_bot_usernames 查 managed_repos.bot_username + 全局 bot_usernames
2. 路径 B 仓库 bot_username 为空 → 不在列表中
3. host_user A 的用户名不在列表中
4. 路径 A 仓库（GlobalBotTokenHandler.is_self_loop_actor）：actor not in bot_usernames → 不命中
5. 路径 B 仓库（UserTokenHandler.is_self_loop_actor）：四条件组合判断 → 正确拦截

#### 用例 17.2：将 host 用户加入 bot_usernames 的副作用

1. 如果把 host_user 用户名加入 get_all_bot_usernames
2. host_user 的正常操作（创建 issue、发评论、提交 PR）都会被自循环检测拦截
3. host_user 无法正常使用系统 → 功能受损
4. 不能简单把 host_user 加入 bot_usernames → 设计采用四条件组合判断（R20.1）

### 场景 18：路径 B actor 相同无法区分 agent 和用户（核心矛盾）

**初始状态**：路径 B 仓库，用户 A 的 PAT 未过期，host_user_id = A。

#### 用例 18.1：简单 actor 判断导致用户评论被误拦截

1. 用户 A 在 GitCode issue 上发评论"请帮我修复这个 bug"
2. webhook 推送 issue_comment 事件 → actor = A 的 VCS username
3. 如果自循环检测简单判断 `actor == host_vcs_username → 跳过`
4. 用户 A 的评论被拦截 → 不触发流水线
5. 用户 A 等待 agent 响应 → 永远等不到
6. 设计方案：四条件组合（条件 4 task 正在执行不满足 → 放行）

#### 用例 18.2：agent 执行期间用户评论被误拦截

1. agent 正在执行 task（task status=running）
2. agent 用用户 token 发评论"正在分析问题..."
3. webhook → 四条件全满足 → 跳过（正确）
4. 用户 A 在 agent 执行期间发评论"顺便也看看测试覆盖"
5. webhook → 四条件全满足 → 跳过（误拦截）
6. 时序竞态可接受——agent 完成后用户可以再次评论触发

#### 用例 18.3：agent 完成后用户评论正常触发流水线

1. agent 执行完成 → task status=completed
2. 用户 A 发评论"结果不对，请重新检查"
3. webhook → 条件 4（task 正在执行）不满足 → 放行
4. pipeline 路由 → issue_comment → issue agent → 派发新 task
5. 此场景正确工作

### 场景 19：路径 B 用户通过评论触发流水线

**初始状态**：路径 B 仓库，用户 A 的 PAT 未过期，无正在执行的 task。

#### 用例 19.1：用户创建 issue 触发流水线

1. 用户 A 在 GitCode 创建 issue
2. webhook 推送 issue_opened → actor = A
3. 自循环检测：event_type=issue_opened 不在检测列表（条件 3 不满足）→ 放行
4. pipeline 路由 → issue_opened → VOD agent → 派发 task
5. 此场景正确工作

#### 用例 19.2：用户在 issue 上评论触发流水线（无 agent 执行中）

1. 用户 A 在已有 issue 上发评论"请继续处理"
2. webhook 推送 issue_comment → actor = A
3. 四条件检测：条件 4（task 正在执行）不满足 → 放行
4. pipeline 路由 → issue_comment → issue agent → 派发 task
5. 此场景正确工作

#### 用例 19.3：用户在 PR 上评论触发 QA（无 agent 执行中）

1. 用户 A 在 PR 上发评论"请审查这个 PR"
2. webhook 推送 note_hook_pr → actor = A
3. 四条件检测：条件 4 不满足 → 放行
4. pipeline 路由 → note_hook_pr → PR 订阅匹配 → 派发 QA task
5. 此场景正确工作

#### 用例 19.4：用户使用开发者指令不被拦截

1. agent 执行完成 → task status=completed → issue 进入 pending_review
2. 用户 A 发评论"/done"
3. pipeline 步骤 2.0b（开发者指令检测，移到自循环检测之前）：评论含 /done → confirm_issue_done → 跳过
4. 不进入自循环检测 → 不被拦截
5. 此场景正确工作

### 场景 20：路径 B 时序竞态

**初始状态**：路径 B 仓库，agent 正在执行 task（status=running）。

#### 用例 20.1：agent 收尾期间评论被放行（时序竞态）

1. agent 完成核心工作 → 开始收尾（准备发结果评论）
2. task 状态还是 running
3. agent 用用户 token 发结果评论"任务已完成"
4. webhook → 四条件满足 → 跳过（正确）
5. 如果在 agent 发评论前 100ms，task 状态恰好被更新为 completed
6. agent 发评论 → webhook → task status=completed → 条件 4 不满足 → 放行
7. pipeline 路由 → 派发新 task
8. 新 task 执行 → agent 发评论 → task status=running → 四条件满足 → 跳过
9. 最多多一次 pipeline 触发 → 可接受

#### 用例 20.2：agent 执行期间用户评论被放行（task 未创建）

1. 用户 A 创建 issue → pipeline 触发 → task 创建中（尚未写入 agent_tasks 表）
2. 用户 A 立即发评论"补充信息"
3. webhook → 条件 4：查 agent_tasks → task 尚未写入 → 无 running task
4. 条件 4 不满足 → 放行 → pipeline 路由 → 派发新 task
5. 两个 task 并行执行 → 可能冲突
6. 可接受——task 创建和 webhook 之间的时序窗口极小，且现有幂等保护有去重逻辑

### 场景 21：飞书 VOD agent 自循环

**初始状态**：路径 B 仓库，用户 A 通过飞书触发 VOD agent。

#### 用例 21.1：飞书 VOD agent 发 Issue 评论触发重复执行

1. 用户 A 在飞书群 @bot "分析这个仓库"
2. 飞书 VOD handler 启动 → 用用户 token 注入 agent → agent 执行
3. VOD agent 执行过程中用用户 token 调 GitCode API 发 Issue 评论
4. webhook → 自循环检测条件 4：查 agent_tasks → VOD agent 没创建 task 行 → 不满足
5. 四条件不全满足 → 放行 → pipeline 派发新 task → 重复执行

**问题**：VOD handler 未创建 agent_tasks 行（R20.10.1 修复前）

#### 用例 21.2：飞书 VOD agent 创建 task 行后自循环检测正确

1. 用户 A 在飞书群 @bot "分析这个仓库"
2. 飞书 VOD handler 启动 → 在 agent_tasks 表创建 task 行（status=running）（R20.10.1）→ agent 执行
3. VOD agent 发 Issue 评论 → webhook → 条件 4：查到 running task → 满足
4. 四条件全满足 → 跳过 → 不重复执行
5. VOD agent 完成 → 更新 task status=completed → 条件 4 不满足 → 用户后续评论可正常触发

#### 用例 21.3：飞书 VOD agent token 过期

1. 用户 A 在飞书群 @bot "分析这个仓库"
2. VOD handler 调 TokenResolver.resolve() → 返回空 token（用户 token 过期短路）
3. VOD agent 不启动（R20.9.2）
4. 飞书发卡片通知用户"Token 已过期，请在设置页重新绑定"（R20.9.2）
5. 不重试（R20.9.4）

#### 用例 21.4：飞书 sync mapping token 过期

1. 飞书 bot 同步群成员 ↔ VCS 协作者映射
2. 调 TokenResolver.get_token() → 返回空（用户 token 过期短路）
3. sync mapping 跳过（R20.9.1）
4. 飞书群发通知卡片"仓库 Token 已过期，协作者映射同步已跳过，请重新绑定 Token"（R20.9.1）
5. @mention 功能暂时失效
6. 用户重新绑定 token → 下次 sync 恢复

### 场景 22：Chat 会话 token 过期

**初始状态**：路径 B 仓库，用户 A 在 Web Chat 界面与 agent 对话。

#### 用例 22.1：Chat agent token 过期

1. 用户 A 在 Web Chat 发消息"帮我看看这个 issue"
2. Chat send_message → _resolve_gitcode_token → TokenResolver.resolve() → 用户 token
3. agent 执行 → 调 VCS API GET /issues → 401 失败
4. Chat 检测 401 → 调 TokenResolver.detect_and_mark_expired（R20.8.1）
5. error_code=TOKEN_EXPIRED → 返回友好提示"Token 已过期，请在设置页重新绑定 Token 后重试"
6. Web Chat 界面显示提示（R20.8.3）
7. 不重试（R20.8.4）

#### 用例 22.2：Chat direct 模式自循环

1. 用户 A 在 Web Chat 发消息 → Chat direct 模式执行 agent
2. agent 用用户 token 调 VCS API 发 Issue 评论
3. webhook → 自循环检测条件 4：查 agent_tasks → Chat direct 没创建 task → 不满足
4. 四条件不全满足 → 放行 → 可能重复执行
5. 修复方案：Chat direct 模式创建 agent_tasks 行（R20.10.2）→ 条件 4 满足 → 正确拦截

#### 用例 22.3：Chat ensure_issue_conversation token 过期

1. 用户 A 打开 Issue 关联会话 → Chat 同步 Issue 评论
2. Repo.bot_token → TokenResolver 短路返回空（用户 token 过期）
3. 同步 Issue 评论失败 → Chat 界面显示"无法同步 Issue 评论，Token 可能已过期"
4. 用户重新绑定 token → 再次打开会话 → 同步成功

### 场景 23：非 pipeline 场景 token 统一入口

**初始状态**：路径 B 仓库，用户 A 的 PAT 未过期。

#### 用例 23.1：events redispatch 通过 Repo.bot_token 获取 token

1. 用户 A 在 dashboard 对失败的事件点 redispatch
2. events redispatch → Repo(repo_key).bot_token → TokenResolver.get_token() → 返回用户 token
3. redispatch agent 用用户 token 执行 → 正常
4. 如果 token 过期 → TokenResolver 短路返回空 → redispatch agent 无 token → fail_task → NO_TOKEN_AVAILABLE

#### 用例 23.2：onboarding 通过 Repo.bot_token 创建仓库

1. 用户 A 执行 onboarding → Repo(repo_key).bot_token → TokenResolver.get_token() → 返回用户 token
2. 用用户 token git clone / git push → 正常
3. 如果 token 过期 → 短路返回空 → clone 失败 → onboarding 报错"Token 已过期"

#### 用例 23.3：project_info builder 通过 Repo.bot_token clone 仓库

1. pipeline 触发 project_info builder → Repo(repo_key).bot_token → TokenResolver.get_token()
2. 用用户 token git clone → 生成项目信息
3. 如果 token 过期 → 短路返回空 → clone 失败 → project_info 跳过（不影响主流程）

#### 用例 23.4：scenes experience 用用户 token 调 VCS API

1. 用户 A 在场景市场体验场景 → with_oauth_token → 用户 token → 调 GitCode API GET /user
2. token 过期 → with_oauth_token 尝试刷新（OAuth 有 refresh_token）→ 刷新成功 → 重试
3. 刷新失败（PAT 无 refresh_token）→ 返回友好提示"Token 已过期，请重新绑定"
4. scenes experience 保持 with_oauth_token 独立链路（R20.7.7）

### 场景 24：scheduler autopilot 自循环

**初始状态**：路径 B 仓库，autopilot 定时巡检触发。

#### 用例 24.1：autopilot agent 发 Issue 评论触发重复执行

1. scheduler 定时触发 autopilot → Repo.bot_token → TokenResolver.get_token() → 用户 token
2. autopilot agent 执行巡检 → 用用户 token 发 Issue 评论"巡检发现 3 个问题"
3. 评论 actor = host_user VCS username
4. webhook → 自循环检测条件 4：查 agent_tasks → autopilot 没创建 task 行 → 不满足
5. 四条件不全满足 → 放行 → pipeline 派发新 task → 重复执行

**问题**：autopilot 未创建 agent_tasks 行（R20.10.3 P0 修复前）

#### 用例 24.2：autopilot 创建 task 行后自循环检测正确

1. scheduler 定时触发 autopilot → 在 agent_tasks 表创建 task 行（status=running）（R20.10.3）→ agent 执行
2. autopilot agent 发 Issue 评论 → webhook → 条件 4：查到 running task → 满足
3. 四条件全满足 → 跳过 → 不重复执行
4. autopilot 完成 → 更新 task status=completed → 条件 4 不满足 → 用户后续评论可正常触发

#### 用例 24.3：autopilot token 过期

1. scheduler 定时触发 autopilot → Repo.bot_token → TokenResolver 短路返回空（用户 token 过期）
2. autopilot agent 无 token → 巡检失败 → 记录日志"autopilot: no token available, skip"
3. autopilot 跳过（不影响主流程）
4. 用户重新绑定 token → 下次 autopilot 正常执行

### 场景 25：Issues close/comment token 短路

**初始状态**：路径 B 仓库，用户 PAT 已过期。

#### 用例 25.1：confirm_done close issue token 过期

1. 用户 A 在 dashboard 点"确认完成" → _try_close_issue_for_repo
2. Repo.bot_token → TokenResolver 短路返回空（用户 token 过期）
3. close issue 无 token → 返回 bot_token_failed
4. dashboard 显示"Bot Token 可能失效，Issue 未关闭"
5. 用户重新绑定 token → 再次确认完成 → close 成功

#### 用例 25.2：comment issue token 过期

1. 用户 A 在 issues 页发评论 → Repo.bot_token
2. TokenResolver 短路返回空
3. 评论失败 → 返回明确错误"Token 已过期"
4. 用户重新绑定 → 再次评论 → 成功

#### 用例 25.3：SRE resolve_pr_issue_id token 过期降级

1. SRE agent 执行 → resolve_pr_issue_id 需要调 VCS API 查 PR 关联 Issue
2. Repo.bot_token → TokenResolver 短路返回空
3. resolve_pr_issue_id 失败（无 token 不能调 API）
4. 降级为 resolve_issue_id（不调 API，从 payload 解析）或 evt.issue_id
5. 可能解析不到 issue_id → SRE agent 无法关联 Issue → task 路由失败
6. 可接受——token 过期时整个 task 都会 fail（R16.6 NO_TOKEN_AVAILABLE）

### 场景 26：webhook 验证 token 与执行 token 解耦

**初始状态**：路径 B 仓库（webhook_secret=随机密钥），用户 PAT 已过期。

#### 用例 26.1：token 过期后 webhook 验证仍正常

1. 用户 PAT 过期 → 健康检查标记 status=4 → TokenResolver 短路返回空串
2. 用户在 GitCode 创建 Issue → GitCode webhook 推送 issue_opened 事件
3. webhook 验证：get_webhook_verify_token → 查 webhook_secret（随机密钥）→ 非空
4. X-GitCode-Token = 随机密钥 → 与 webhook_secret 匹配 → 验证通过
5. webhook 验证不受 token 过期影响（R20.12.3）
6. pipeline 触发 → agent 获取 token → TokenResolver 短路返回空 → fail_task → NO_TOKEN_AVAILABLE

#### 用例 26.2：路径 A 仓库 webhook 验证用 bot token

1. 路径 A 仓库（webhook_secret=空，gitcode_webhook_token=bot token）
2. get_webhook_verify_token → webhook_secret 为空 → 查 gitcode_webhook_token → bot token
3. X-GitCode-Token = bot token → 匹配 → 验证通过
4. 即使全局 bot token 过期短路，webhook 验证仍用 gitcode_webhook_token（存储的是创建时的值，不受运行时短路影响）

#### 用例 26.3：A2A 鉴权 token 过期后仍可通信

1. 仓库级 bot token 过期 → 健康检查标记 bot_token_status=expired
2. agent A 向 agent B 发 A2A 请求 → Authorization header = 仓库级 bot token（原始值）
3. webhook.py 验证：get_repo_bot_token_no_global → TokenResolver.read_stored_token() → 返回原始 token 值
4. 比对：Authorization header == 原始 token 值 → 匹配 → 鉴权通过（R20.13.3）
5. A2A 通信不受 token 过期影响

### 场景 27：建单场景用户 token 与 bot token 分离

**初始状态**：路径 A 仓库（有全局 bot token），用户 A 已绑定 GitCode PAT。

#### 用例 27.1：GitCode 建单用用户 token

1. 用户 A 在 dashboard 建单 → issues_center create_issue
2. GitCode 路径：get_user_token_with_refresh(user["username"], user["id"]) → 用户 A 的 token
3. 用用户 A 的 token 调 GitCode API POST /repos/{owner}/{repo}/issues → assignee=A 的登录名
4. GitCode assign 需用户身份 → 用 bot token 会 403
5. 此场景不走 TokenResolver（R20.14.1），保留 get_user_token_with_refresh

#### 用例 27.2：GitHub 建单用 repo bot token

1. 用户 A 在 dashboard 建 GitHub Issue → issues_center create_issue
2. GitHub 路径：get_github_token(repo_key) → repo bot token
3. GitHub assign 无用户身份限制 → bot token 可 assign
4. 此场景可走 Repo.bot_token（R20.14.2）

#### 用例 27.3：建单时用户 token 过期

1. 用户 A 的 GitCode PAT 过期 → get_user_token_with_refresh 返回空
2. 建单失败 → 返回"GitCode Token 未配置或已失效，请重新绑定/登录 GitCode 后重试"
3. 不 fallback 到 bot token（R20.14.3）——assign 需用户身份

### 场景 28：token 短路后 task fail 错误检测

**初始状态**：路径 B 仓库，用户 PAT 已过期，健康检查已标记 status=4。

#### 用例 28.1：token 短路后 git clone 失败

1. pipeline 触发 → agent 获取 token → TokenResolver 短路返回空串
2. agent 用空 token git clone → git 报错"fatal: could not read Username for 'https://gitcode.com': No such device or address"
3. fail_task 检测 error → 不含 "401 unauthorized" 关键词
4. 旧逻辑：不触发 token 过期检测 → 只写普通错误
5. 新逻辑（R20.19.4）：error 含 "could not read Username" → 扩展关键词匹配 → 触发 token 过期检测
6. 但 token 已被短路 → detect_and_mark_expired 所有策略 _fetch 返回空（已被短路）→ 返回 NO_TOKEN_AVAILABLE（R20.19.1）

#### 用例 28.2：token 短路后空 token 直接检测

1. pipeline 触发 → _get_task_repo_token → TokenResolver 短路返回空
2. task 行 repo_gitcode_token=空
3. agent 执行前 fail_task 检测 task token 为空（R20.19.1）→ 直接写入 NO_TOKEN_AVAILABLE
4. 不调 VCS API 验证（R20.19.3）
5. 不走 401 关键词检测（R20.19.2 空检测优先）

#### 用例 28.3：token 短路后 git push Authentication failed

1. agent 有缓存 token（task 创建时写入的非空 token，运行时已过期但 task 行仍存旧值）
2. agent git push → 401 → error="fatal: Authentication failed for 'https://gitcode.com/...'"
3. fail_task 检测 error → 含 "Authentication failed"（R20.19.4 扩展关键词）→ 触发 token 过期检测
4. detect_and_mark_expired → _fetch 获取 task 行旧 token → verify_token → 401 → 标记过期
5. 写入 TOKEN_EXPIRED error_code

### 场景 29：路径 B GIT_ASKPASS fallback 到 USER_TOKEN

**初始状态**：路径 B 仓库（host_user_id > 0, bot_gitcode_token=空），用户 A 的 PAT 未过期。

#### 用例 29.1：路径 B git clone fallback 成功

1. pipeline 触发 → _get_task_repo_token → TokenResolver.get_token → 用户 token（第 4 级）
2. task 行 repo_gitcode_token=空（路径 B 防过期 token 落库）
3. runtime/agent.py 注入 env：BOT_GITCODE_TOKEN=空（task 行空）, USER_GITCODE_TOKEN=用户 token（runtime 实时获取）
4. agent 执行 → git clone → GIT_ASKPASS 脚本执行
5. GIT_ASKPASS: `${BOT_GITCODE_TOKEN:-${USER_GITCODE_TOKEN:-...}}` → BOT_GITCODE_TOKEN=空 → fallback USER_GITCODE_TOKEN → 输出用户 token
6. git clone 用用户 token 认证 → 成功（R20.24.3 fallback 链修正后）

#### 用例 29.2：路径 B git clone fallback 失败（当前 bug，修正前）

1. pipeline 触发 → task 行 repo_gitcode_token=空
2. runtime/agent.py 注入：BOT_GITCODE_TOKEN=空, USER_GITCODE_TOKEN=用户 token
3. agent git clone → GIT_ASKPASS: `${BOT_GITCODE_TOKEN:-${BOT_TOKEN:-$BOT_GITCODE_TOKEN}}` → 全空 → 输出空串
4. git clone → "fatal: could not read Username" → 失败
5. 这是当前代码的实际 bug（R20.24.3 修正后修复）

#### 用例 29.3：路径 A token 短路后 git clone fallback

1. 全局 bot token 过期 → 健康检查标记 meta.global_bot_token_status=expired
2. pipeline 触发 → TokenResolver.get_token → 第 3 级短路返回空 → 第 4 级无 host_user_id → 返回空
3. task 行 repo_gitcode_token=空
4. runtime/agent.py 注入：BOT_GITCODE_TOKEN=空, USER_GITCODE_TOKEN=空（路径 A 无用户 token）
5. GIT_ASKPASS: 全空 → 输出空 → git clone 失败
6. fail_task 检测空 token → NO_TOKEN_AVAILABLE（R20.19.1）

### 场景 30：result_text token 泄露

**初始状态**：路径 B 仓库，用户 A 的 PAT 未过期，agent 执行中。

#### 用例 30.1：agent 复述 USER_TOKEN 到 result_text（当前 bug）

1. agent 执行 → 从 env 读取 USER_GITCODE_TOKEN 的值 → 在 result_text 中引用
2. result_text = "I used token abc123xyz to clone the repo..."
3. 脱敏逻辑：result_text.replace(repo_gitcode_token, "[REDACTED]") → repo_gitcode_token=空 → 不替换
4. user_token（abc123xyz）未被脱敏 → 写入 agent_tasks.result_text
5. dashboard 显示 result_text → token 泄露

#### 用例 30.2：修正后全 token 脱敏

1. agent 执行 → result_text 含 USER_GITCODE_TOKEN 的值
2. 脱敏逻辑（R20.25.1）：redact_tokens = [repo_gitcode_token, user_token] → 两者都替换
3. result_text.replace(user_token, "[REDACTED]") → "I used token [REDACTED] to clone..."
4. dashboard 显示脱敏后的 result_text → token 安全

#### 用例 30.3：agent_logs 也脱敏

1. agent 执行中 → agent_logs 写入实时日志
2. 日志内容含 BOT_GITCODE_TOKEN 的值
3. 脱敏逻辑（R20.25.2）：_msg.replace(bot_token, "[REDACTED]")
4. agent_logs 脱敏 → 日志安全

### 场景 31：关联仓库跨平台 token

**初始状态**：主仓库 gitcode:owner/repo（路径 A，全局 bot token），关联仓库 github:other/linked-repo（有独立 GitHub bot token）。

#### 用例 31.1：关联仓库用主仓库 token 失败（当前行为）

1. coding agent 完成 → _submit_direct_linked_prs(env_extra, repo_key, issue_id, repo_gitcode_token)
2. repo_gitcode_token = GitCode bot token
3. 遍历关联仓库 → 对 github:other/linked-repo 用 GitCode token
4. git push github:other/linked-repo → 用 GitCode token 认证 → 401（GitHub 不认 GitCode token）
5. 关联仓库 PR 提交失败 → warning 记录

#### 用例 31.2：修正后关联仓库独立获取 token

1. coding agent 完成 → _submit_direct_linked_prs 遍历关联仓库
2. 对 github:other/linked-repo → Repo("github:other/linked-repo").bot_token → GitHub bot token
3. git push 用 GitHub token 认证 → 成功
4. create_pr_for_repo 用 GitHub token → 成功（R20.26.3）

#### 用例 31.3：关联仓库 token 过期

1. 关联仓库 github:other/linked-repo 的 GitHub bot token 过期
2. Repo("github:other/linked-repo").bot_token → TokenResolver 短路返回空
3. 该关联仓库 clone/push 跳过 + warning（R20.26.4）
4. 不影响主仓库 PR 和其他关联仓库

### 场景 32：task 排队期间 token 过期

**初始状态**：路径 A 仓库（全局 bot token 有效），task 队列有 1 个排队中的 task。

#### 用例 32.1：排队期间全局 bot token 过期

1. T0: pipeline 触发 → _get_task_repo_token → 全局 bot token（有效）→ 写入 task 行 repo_gitcode_token=bot_token_A
2. T1: task 排队（status=queued），等待 worker claim
3. T2: 健康检查发现全局 bot token 过期 → 标记 meta.global_gitcode_bot_token_status=expired
4. T3: worker claim task → task 行 token 非空（bot_token_A）→ 新鲜度检查（R21.12.1）
5. 新鲜度检查：GlobalBotToken 策略 _fetch 返回 bot_token_A → _is_expired 返回 True（meta 标记 expired）
6. 重新获取：TokenResolver.get_token → GlobalBotToken 短路返回空 → 无其他策略 → 返回空串
7. task 行 repo_gitcode_token 注入空串（R21.12.2）
8. agent 运行时获取空 token → fail_task → NO_TOKEN_AVAILABLE（R20.19.1）
9. dashboard 显示"无可用 Token，请联系管理员配置"
10. 管理员重新配置全局 bot token → 清除过期标记 → 下次 task 正常

#### 用例 32.2：排队期间仓库级 bot token 过期

1. T0: task 创建 → task 行 repo_gitcode_token=repo_bot_token_X（仓库级 bot token）
2. T1: task 排队
3. T2: 健康检查发现仓库级 bot token 过期 → 标记 managed_repos.bot_token_status=expired
4. T3: worker claim → 新鲜度检查：RepoBotToken 策略 _fetch 返回 repo_bot_token_X → _is_expired 返回 True
5. 重新获取：TokenResolver.get_token → RepoBotToken 短路 → GlobalBotToken 有效 → 返回全局 bot token
6. task 行注入全局 bot token（R21.12.3）
7. agent 用全局 bot token 执行 → 正常（降级到全局 bot token）

#### 用例 32.3：排队期间 token 未过期（正常路径）

1. T0: task 创建 → task 行 repo_gitcode_token=bot_token_A
2. T1: task 排队
3. T2: 无健康检查触发（token 未过期）
4. T3: worker claim → 新鲜度检查：_is_expired 返回 False → 不重新获取
5. task 行 token 不变 → agent 用原 token 执行 → 正常
6. 新鲜度检查只查 DB 过期标记，不调 VCS API → 无延迟（R21.12.6）

#### 用例 32.4：路径 B 仓库排队期间用户 token 过期

1. T0: task 创建 → 路径 B 仓库 → task 行 repo_gitcode_token=空（防过期 token 落库）
2. T1: task 排队
3. T2: 用户 token 过期 → fail_task 401 检测标记 status=4（其他 task 触发）
4. T3: worker claim → task 行 token 为空 → 现有逻辑：_get_task_token 实时获取
5. get_repo_token_with_refresh → UserToken 策略 status=4 仍返回 token（best-effort）
6. agent 用过期 token → 401 → fail_task → TOKEN_EXPIRED
7. 不受 R21.12 影响（路径 B task 行本来就为空，走现有实时获取逻辑）
