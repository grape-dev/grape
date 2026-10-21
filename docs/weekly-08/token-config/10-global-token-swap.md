# 全局 Bot Token 更换 + Per-Provider 过期标记 + 超时修复

> 提取自设计文档 `repo-token-config-design.md` R21.1-R21.3（第 962-1029 行）及需求文档 R21 修订章。

## 需求项

### R21.1 全局 bot token 更换（A1 级矛盾修复）

**问题**：管理员把全局 bot token 从 `bot_old` 换成 `bot_new`（有→有换账号）时：

1. `auto_upgrade` 不触发（`save_config` 只检测"从无到有"）→ 新 bot 未被邀请为任何路径 A 仓库的协作者 → 所有仓库 403
2. 所有路径 A 仓库 webhook 的 password 仍是旧 bot token → webhook 验证不匹配 → 事件断链
3. 旧全局 bot 在所有仓库的协作者身份未清理

- R21.1.1：`save_config` 检测全局 bot token 值变更（`old_token != new_token` 且 `old_token` 非空）→ 触发 `global_bot_token_swap` 流程
- R21.1.2：`global_bot_token_swap` 流程：验证新 token → 获取 bot username → 遍历路径 A 仓库 → 邀新 bot + 重建 webhook + 更新 webhook_token + best-effort 移除旧 bot
- R21.1.3：`global_bot_token_swap` 在后台线程执行，不阻塞 `save_config` 返回
- R21.1.4：失败时仓库保持旧状态（旧 bot token 仍有效直到被撤销），记录 warning
- R21.1.5：清除 `meta.global_*_bot_token_status` 过期标记（R16.3.2 已有，确认覆盖更换场景）
- R21.1.6：GitHub 全局 bot token 更换同理（provider 分流）
- R21.1.7：`global_bot_token_swap` 用分布式锁 `bot_token_ops`（R24.1 修正：与 auto_upgrade 共用锁，防止两个后台线程同时操作同一仓库），防止多实例同时执行

### R21.2 per-provider 过期标记 key（A2 级矛盾修复）

**问题**：R16.5.2/R16.4.1 的过期标记只有一个 `global_bot_token_status`，GitCode 全局 token 过期会把 GitHub 全局 token 一起短路。需要 per-provider key。

- R21.2.1：meta 表过期标记改为 per-provider key：`global_gitcode_bot_token_status`（GitCode 全局 bot token 过期标记）/ `global_github_bot_token_status`（GitHub 全局 bot token 过期标记）
- R21.2.2：R16.4.1 短路机制修正：`_get_global_bot_token` 按 provider 查对应 key——GitCode 查 `global_gitcode_bot_token_status`，GitHub 查 `global_github_bot_token_status`
- R21.2.3：R16.1.3 `fail_task` 检测修正：按 provider 标记对应 key——GitHub 仓库 401 标记 `global_github_bot_token_status`，GitCode 仓库 401 标记 `global_gitcode_bot_token_status`
- R21.2.4：R17.2.2 健康检查修正：按 provider 验证 + 标记对应 key
- R21.2.5：R16.5.2 修正：meta 表新增 `global_gitcode_bot_token_status` 和 `global_github_bot_token_status` 两个 key（替代单个 `global_bot_token_status`）
- R21.2.6：R16.5.4 修正：健康检查同时写 `bot_token_health`（admin 页展示用）+ 对应 provider 的 `global_*_bot_token_status`（短路用）
- R21.2.7：`GlobalBotToken` 策略的 `_is_expired` / `mark_expired` / `clear_expired` 按 provider 查对应 key
- R21.2.8：`GlobalBotTokenHandler` 同理按 provider 操作对应 key
- R21.2.9：向前兼容——旧数据如果已有 `global_bot_token_status` key，迁移时拆分为两个 per-provider key

**对 R20.13 A2A 鉴权的影响**：`read_stored_token` 不检查过期标记，per-provider key 不影响鉴权逻辑。

### R21.3 健康检查/verify 超时误标记修复（A3 级矛盾修复）

**问题**：R18.5.10"超时视为验证失败（标记过期）"+ R17.2.1 `FAILURE_THRESHOLD=1` + R16.1.3"必须先验证"三者矛盾。一次网络抖动/5xx → 全局 token 标记 expired → 短路 → 所有 task `NO_TOKEN_AVAILABLE` → 恢复要等下一小时健康检查。

- R21.3.1：`verify_token` 返回 401 → 标记过期（token 确实无效）
- R21.3.2：`verify_token` 超时或 5xx → 不标记过期，记录 warning，等下次健康检查重试
- R21.3.3：R18.5.10 修正：超时不视为验证失败（不标记过期），只记 warning。原"超时视为失败"设计取消
- R21.3.4：R17.2.1 修正：`FAILURE_THRESHOLD=1` 只对 401 生效。超时/5xx 不计入 `FAILURE_THRESHOLD`
- R21.3.5：`fail_task` `detect_and_mark_expired` 中 `provider.verify_token(token)` 区分：
  - HTTP 401 → 标记过期 + 返回 error_code
  - 超时 / 5xx → 不标记，返回空（401 可能是其他原因）
  - 其他异常 → 不标记，返回空
- R21.3.6：最坏情况分析：网络抖动导致 verify 超时 → 不标记 → task 仍用原 token 执行 → 如果 token 真过期 → task 401 → `fail_task` 再次 verify → 如果这次 verify 也超时 → 不标记 → task 失败但 error_code 为原始错误（不是 `TOKEN_EXPIRED`）。可接受——网络恢复后下次 verify 会正确判断
- R21.3.7：健康检查同理——401 才标记 expired，超时/5xx 跳过不标记

## 设计方案

### R21.1 `global_bot_token_swap` 流程

`save_config` 检测到全局 bot token 值变更（`old_token != new_token` 且 `old_token` 非空）时触发，流程分 7 步：

1. **验证新 token**：用新 bot token 调 VCS API `GET /user` 验证有效性 → 无效则拒绝配置（R15.1 逻辑）
2. **获取新 bot username**：从 `GET /user` 响应提取
3. **遍历路径 A 仓库**：筛选 `host_user_id == 0` 且 `bot_gitcode_token == 旧全局 token`（GitCode）或 `bot_github_token == 旧全局 token`（GitHub）的仓库
4. **邀请新 bot**：对每个仓库用新 bot token 邀请新 bot 为协作者（如果新 bot 与旧 `bot_username` 不同）→ accept invite
5. **重建 webhook**：用新 bot token 删旧 webhook + 建新 webhook，`password = 新 bot token`
6. **写 DB**：更新 `gitcode_webhook_token` / `github_webhook_token = 新 bot token` + 更新 `bot_username = 新 bot username`
7. **best-effort 移除旧 bot**：移除旧 bot 协作者身份（R15.6.3 best-effort 规则）

执行保障：

- 后台线程执行，不阻塞 `save_config` 返回
- 失败时仓库保持旧状态（旧 bot token 仍有效直到被撤销），记录 warning
- 用分布式锁 `global_bot_swap`，防止多实例同时执行
- 完成后清除 `meta.global_*_bot_token_status` 过期标记（R16.3.2 已有，确认覆盖更换场景）
- GitHub 全局 bot token 更换同理（provider 分流）

#### 与 `auto_upgrade` 的互斥关系

| 维度 | `auto_upgrade_path_b_repos` | `global_bot_token_swap` |
|---|---|---|
| 触发条件 | 全局 bot token"从无到有" | 全局 bot token"有→有换账号" |
| 遍历范围 | 路径 B 仓库（`host_user_id != 0`） | 路径 A 仓库（`host_user_id == 0`） |
| 操作 | 邀 bot + 重建 webhook + 写 DB（切换到路径 A） | 邀新 bot + 重建 webhook + 更新 webhook_token |
| 互斥 | 两者不会同时触发：从无到有走 `auto_upgrade`，有→有走 `swap` |

### R21.2 per-provider key 修正（9 个子项）

过期标记改为 per-provider，影响以下设计点：

1. **meta 表 key 拆分**：`global_bot_token_status` → `global_gitcode_bot_token_status` + `global_github_bot_token_status`
2. **短路机制修正**（R16.4.1）：`_get_global_bot_token` 按 provider 查对应 key
3. **fail_task 标记修正**（R16.1.3）：按 provider 标记对应 key——GitHub 仓库 401 标 `global_github_bot_token_status`，GitCode 仓库 401 标 `global_gitcode_bot_token_status`
4. **健康检查修正**（R17.2.2）：按 provider 验证 + 标记对应 key
5. **R16.5.2 修正**：meta 表新增两个 per-provider key（替代单个）
6. **R16.5.4 修正**：健康检查同时写 `bot_token_health`（admin 展示）+ 对应 provider 的 `global_*_bot_token_status`（短路用）
7. **GlobalBotToken 策略修正**：`_is_expired` / `mark_expired` / `clear_expired` 按 provider 查对应 key
8. **GlobalBotTokenHandler 修正**：同理按 provider 操作对应 key
9. **向前兼容**：旧数据 `global_bot_token_status` key 迁移时拆分为两个 per-provider key

**对 A2A 鉴权的影响**：`read_stored_token` 不检查过期标记，per-provider key 不影响鉴权逻辑。

### R21.3 区分 401 和超时（7 个子项）

设计原则：区分 401（真过期）和超时/5xx（可能网络问题），只对 401 标记过期。

1. **401 → 标记过期**（token 确实无效）
2. **超时/5xx → 不标记**，记录 warning，等下次健康检查重试
3. **R18.5.10 修正**：超时不视为验证失败（不标记过期），只记 warning。原"超时视为失败"设计取消
4. **FAILURE_THRESHOLD 修正**：`FAILURE_THRESHOLD=1` 只对 401 生效，超时/5xx 不计入
5. **detect_and_mark_expired 区分**：
   - HTTP 401 → 标记过期 + 返回 error_code
   - 超时 / 5xx → 不标记，返回空
   - 其他异常 → 不标记，返回空
6. **健康检查同理**：401 才标记 expired，超时/5xx 跳过不标记
7. **最坏情况分析**：网络抖动 → verify 超时 → 不标记 → task 用原 token → 如果真过期 → task 401 → fail_task 再次 verify → 如果这次也超时 → 不标记 → task 失败但 error_code 为原始错误（非 `TOKEN_EXPIRED`）。可接受——网络恢复后下次 verify 会正确判断

## 涉及代码文件

| 文件 | 位置 | 职责 |
|---|---|---|
| `src/admin/config_store.py` | `save_config` | 检测全局 bot token 值变更（`old_token != new_token` 且 `old_token` 非空）→ 触发 `global_bot_token_swap` 后台流程 + 分布式锁 `global_bot_swap` |
| `src/user/db.py` | `auto_upgrade_path_b_repos` | 路径 B 仓库 auto_upgrade 逻辑——与 `global_bot_token_swap` 互斥参照（从无到有 vs 有→有换账号） |
| `src/user/repo_manager.py` | webhook 重建 | 删旧 webhook + 建新 webhook（`password = 新 bot token`）+ 更新 `gitcode_webhook_token` / `github_webhook_token` + 邀请新 bot / 移除旧 bot（best-effort） |

**per-provider key 改动涉及**：
- meta 表 key 拆分（`global_gitcode_bot_token_status` / `global_github_bot_token_status`）
- `GlobalBotToken` 策略 `_is_expired` / `mark_expired` / `clear_expired`
- `GlobalBotTokenHandler` 按 provider 操作
- `fail_task` `detect_and_mark_expired`（区分 401 / 超时 / 5xx）
- 健康检查逻辑（R17.2.2）

## 验收标准

### R21.1 全局 bot token 更换

- [ ] 管理员将全局 GitCode bot token 从 `bot_old` 换成 `bot_new`（有→有），`save_config` 检测到值变更并触发 `global_bot_token_swap`
- [ ] `global_bot_token_swap` 用新 token 调 `GET /user` 验证有效性，无效则拒绝配置
- [ ] 遍历所有路径 A 仓库（`host_user_id == 0` 且 `bot_gitcode_token == 旧全局 token`）
- [ ] 每个仓库：邀新 bot 为协作者 → accept invite → 重建 webhook（password=新 token）→ 更新 DB `gitcode_webhook_token` / `bot_username` → best-effort 移除旧 bot
- [ ] `save_config` 不被阻塞（后台线程执行）
- [ ] 失败时仓库保持旧状态，记录 warning
- [ ] 完成后清除 `global_gitcode_bot_token_status` 过期标记
- [ ] GitHub 全局 bot token 更换同理（provider 分流）
- [ ] 分布式锁 `global_bot_swap` 生效，多实例不会同时执行
- [ ] E2E：全局 bot token 更换后，所有路径 A 仓库 webhook 事件正常接收 + agent 正常执行

### R21.2 per-provider 过期标记

- [ ] meta 表有 `global_gitcode_bot_token_status` 和 `global_github_bot_token_status` 两个独立 key
- [ ] GitCode 全局 token 过期标记 `global_gitcode_bot_token_status`，不影响 GitHub 全局 token 使用
- [ ] GitHub 全局 token 过期标记 `global_github_bot_token_status`，不影响 GitCode 全局 token 使用
- [ ] `_get_global_bot_token` 按 provider 查对应 key 做短路判断
- [ ] `fail_task` 按 provider 标记对应 key
- [ ] 健康检查按 provider 验证 + 标记对应 key
- [ ] `GlobalBotToken` 策略 `_is_expired` / `mark_expired` / `clear_expired` 按 provider 操作
- [ ] `GlobalBotTokenHandler` 按 provider 操作
- [ ] 旧 `global_bot_token_status` 数据迁移为两个 per-provider key（向前兼容）
- [ ] A2A 鉴权不受影响（`read_stored_token` 不检查过期标记）

### R21.3 超时误标记修复

- [ ] `verify_token` 返回 401 → 标记过期
- [ ] `verify_token` 超时 → 不标记过期，记录 warning
- [ ] `verify_token` 5xx → 不标记过期，记录 warning
- [ ] `FAILURE_THRESHOLD=1` 只对 401 生效，超时/5xx 不计入
- [ ] `detect_and_mark_expired` 区分 401 / 超时 / 5xx / 其他异常，只 401 标记
- [ ] 健康检查同理——401 才标记 expired
- [ ] 最坏情况验证：网络抖动 → verify 超时 → 不标记 → task 用原 token → 如果真过期 → 401 → 再次 verify → 网络恢复后正确判断

## 依赖场景

- **依赖 `02-expiry-framework` 的标记机制**：R21.2 per-provider key 拆分基于 R16.5.2 的 meta 表过期标记框架；R21.3 超时/401 区分基于 R16.1.3 `fail_task` 的 `detect_and_mark_expired` + R17.2.2 健康检查标记逻辑
- **依赖 `04-token-resolver` 的 `VCSProvider`**：R21.1 `global_bot_token_swap` 用 VCS API（`GET /user` 验证 + 邀请协作者 + webhook 管理）；R21.3 `verify_token` 由 `VCSProvider` 提供，返回值区分 401 / 超时 / 5xx
- **与 `auto_upgrade`（路径 B）互斥**：`global_bot_token_swap`（有→有，路径 A）与 `auto_upgrade_path_b_repos`（从无到有，路径 B）不会同时触发
