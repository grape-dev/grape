# 仓库级 Token 配置处理逻辑

## 需求项

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
- R15.5.2：清除后不重建 webhook（用户需 repair_host）。**R24.4 修正**：清除时不清 `webhook_secret`（保留旧值供 webhook 验证），直到 repair_host 重建 webhook 时才更新。避免清除后 webhook 验证断裂。
- R15.5.3：清除后 get_repo_token_with_refresh 第 3 级命中全局 bot token
- R15.5.4：清除时同时清除 bot_token_status 过期标记（设计补充，避免 settings 页误导用户以为 token 过期而非已清除）
- R15.5.5：清除时 best-effort 移除旧 bot 协作者身份（设计补充，R15.6.3 best-effort 规则适用），不移除其他仓库的旧 bot

### R15.6 旧 bot 协作者移除

- R15.6.1：更换仓库级 bot token 时只移除该仓库的旧 bot 协作者身份
- R15.6.2：不移除其他仓库的旧 bot 协作者身份
- R15.6.3：移除失败 best-effort，不影响新 token 写入

## 设计方案

### 3.1 问题：当前执行顺序错误

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

### 3.2 正确执行顺序（7 步流程）

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

### 3.4 与 auto_upgrade_path_b_repos 的关系：不重复邀 bot

`update_managed_repo_bot_token` 有两个调用方：

1. **set_repo_bot_token**（手动配置入口）— R15 改造后走完整 7 步流程
2. **auto_upgrade_path_b_repos**（自动升级入口）— 自己已完成邀 bot + accept invite，然后调 update_managed_repo_bot_token 写 DB

R15 改造原则：**update_managed_repo_bot_token 内部不重复邀 bot 逻辑**。邀 bot 逻辑在调用方（set_repo_bot_token 路由层 / auto_upgrade）完成，update_managed_repo_bot_token 只负责"重建 webhook + 写 DB"。

这样 auto_upgrade 调 update_managed_repo_bot_token 时不会重复邀请——auto_upgrade 自己邀了 bot，update_managed_repo_bot_token 只做 webhook + DB。

set_repo_bot_token 路由层在调 update_managed_repo_bot_token 之前先完成邀 bot（R15 步骤 4），update_managed_repo_bot_token 只做步骤 5-6（webhook + DB）。

**向前兼容**：update_managed_repo_bot_token 函数签名不变（repo_key + bot_token → bool），内部逻辑保持"重建 webhook + 写 DB"不变。R15 改造的是 set_repo_bot_token 路由层的调用顺序，不是 update_managed_repo_bot_token 内部逻辑。

**R16.3.3 恢复逻辑落地**：_update_repo_bot_token 的 UPDATE SQL 需新增 `bot_token_status=''` 清除——当 token 重新配置时必须清除过期标记，否则短路仍生效导致 token 被写入但读取时返回空串。

### 3.5 场景覆盖

#### 路径 A：全局 token 场景（路径 A → 路径 A，换 bot 账号）

仓库已通过全局 bot token 托管，有仓库权限的用户配不同的仓库级 bot token：

- 验证新 token → 获取新 bot username
- 新 bot 与旧 bot_username 不同 → 邀请新 bot 为协作者
- 新 bot 已是协作者 → 跳过邀请
- 旧 bot 可选移除协作者身份（best-effort）
- 用新 bot token 重建 webhook
- 写 DB：新 bot_token + 更新 bot_username

#### 路径 B：用户 token 场景（路径 B → 路径 A，首次配 bot token）

仓库通过用户 token 托管，有仓库权限的用户配 bot token：

- 验证新 token → 获取 bot username
- 用 host 用户 token 邀 bot 为协作者
- host 用户 token 过期 → 400 HOST_TOKEN_EXPIRED
- 用 bot token 重建 webhook（bot 已是协作者，不会 403）
- 写 DB：bot_token + 清 host_user_id=0 + 清 webhook_secret + 写 webhook_id

## 涉及代码文件

| 文件 | 函数/方法 | 职责 |
|------|----------|------|
| `src/api/routes/repos.py` | `set_repo_bot_token` | 路由层入口，编排 R15 步骤 1-4（检查仓库 → 验证 token → 获取 username → 邀 bot），然后调 update_managed_repo_bot_token 执行步骤 5-6 |
| `src/user/db.py` | `update_managed_repo_bot_token` | 总调度：调 `_rebuild_webhook_with_bot_token` + `_update_repo_bot_token`，函数签名不变 |
| `src/user/db.py` | `_rebuild_webhook_with_bot_token` | 步骤 5：用传入 bot token 删旧 webhook（provider.remove_webhook）+ 建新 webhook（provider.setup_webhook），部分失败保护 |
| `src/user/db.py` | `_update_repo_bot_token` | 步骤 6：写 DB（bot_token 分列写入 + 清 host_user_id + 清 webhook_secret + 写 webhook_id + 清 bot_token_status=R16.3.3 恢复逻辑） |
| `src/user/repo_manager.py` | `add_collaborator` | 步骤 4 子操作：通过 provider.add_collaborator() 邀 bot 为协作者（路径 B 用 host 用户 token，路径 A 新 bot 不同时邀请） |
| `src/user/repo_manager.py` | `setup_webhook` | 步骤 5 子操作：通过 provider.setup_webhook() 建新 webhook（bot 已是协作者，不会 403） |

## 验收标准

1. **token 验证**：配置无效 token 时返回 400 + TOKEN_INVALID，DB 和 webhook 均不变
2. **执行顺序**：邀 bot 在重建 webhook 之前完成，webhook 不会因 bot 未邀请而 403
3. **部分失败保护**：
   - 删旧 webhook 失败 → 不建新、不写 DB、原状态保留
   - 删旧成功建新失败 → 不写 DB、返回 500 + WEBHOOK_REBUILD_FAILED
   - 写 DB 失败 → webhook 已重建但 DB 未更新，用户可 repair_host 恢复
4. **返回值**：
   - token 无效 → 400 + TOKEN_INVALID
   - 仓库不存在 → 404
   - host token 过期 → 400 + HOST_TOKEN_EXPIRED
   - webhook 重建失败 → 500 + WEBHOOK_REBUILD_FAILED
   - 全部成功 → 200 + bot_username
5. **token 清空**：传空串清除仓库级 bot token，同时清除 bot_token_status 过期标记（R15.5.4），之后 get_repo_token_with_refresh 第 3 级命中全局 bot token
6. **旧 bot 移除**：更换 bot token 时只移除该仓库的旧 bot 协作者身份（R15.6.1），不移除其他仓库的旧 bot（R15.6.2），移除失败不影响新 token 写入（R15.6.3 best-effort）
7. **不重复邀 bot**：auto_upgrade 调 update_managed_repo_bot_token 时不重复邀 bot（邀 bot 在 auto_upgrade 内部完成）
8. **过期标记清除**：重新配置 token 时 _update_repo_bot_token 的 UPDATE SQL 清除 bot_token_status，否则短路仍生效导致 token 被写入但读取时返回空串

## 依赖场景

- **R16.3.3 统一过期恢复**：PUT /repos/bot-token 更新仓库级 token 时清除 managed_repos 过期标记，_update_repo_bot_token 的 UPDATE SQL 需新增 `bot_token_status=''` 清除
- **R16.4 短路机制**：仓库级 bot token 标记过期后 get_bot_token_by_repo 短路返回空串，重新配置 token 清除标记后短路解除
- **R16.5.1 DB schema**：managed_repos 新增 bot_token_status 列（TEXT DEFAULT ''，空=正常，expired=过期），R15.5.4 清空 token 时需同时清除此列
- **R17.2 健康检查**：仓库级 bot token 健康检查发现过期后标记 managed_repos.bot_token_status=expired，用户重新配置 token（R15 流程）清除标记恢复
- **R20.4 auto_upgrade webhook 重建保护**：auto_upgrade_path_b_repos 调 update_managed_repo_bot_token 时不重复邀 bot，update_managed_repo_bot_token 只做 webhook + DB
- **场景 2.1**：有仓库权限的用户配置仓库级 bot token（路径 A 换 bot）
- **场景 2.6**：清除仓库级 bot token 恢复使用全局 bot token
- **场景 8.1**：更换仓库级 bot token 时旧 bot 仍有其他仓库在用，只移除该仓库的旧 bot 协作者身份
