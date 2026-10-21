# 健康检查与 PAT 无自动续期

> 源文档：`repo-token-config-design.md` 第六章（PAT 无自动续期问题）+ 第七章（全局/仓库级 Bot Token 自动刷新可行性分析）+ R21.3（健康检查/verify 超时误标记修复）
> 需求范围：R17 + R21.3
> 场景：4.1-4.5, 6

---

## 需求项

### R17.1 配置 token 时提示无自动续期

配置 PAT 类型 token 时明确提示不会自动续期。

- **R17.1.1**：用户 token PAT 绑定时提示（已有 R13.3）— `account.js` `_accountManualBindForm` 在未配 OAuth App 时显示 "PAT 不会自动续期" 提示
- **R17.1.2**：全局 bot token 配置时提示（settings 页）— 直接提示"此 Token 为 PAT 类型，不会自动续期，过期后需手动重新配置"
- **R17.1.3**：仓库级 bot token 配置时提示（PUT /repos/bot-token 返回提示或 settings 页）
- **R17.1.4**：统一提示文案："此 Token 为 PAT 类型，不会自动续期，过期后需手动重新配置"

**设计要点**：
- 用户 token 通过 OAuth 绑定有 refresh_token 可自动刷新；手动输入的 PAT 通过 `_has_refresh_token` 判断（False 时不刷新）
- 全局 bot token / 仓库级 bot token 手动输入天然是 PAT，一定没有 refresh_token，无需检测直接提示

### R17.2 全局/仓库级 bot token 定期健康检查

后台定期验证全局/仓库级 bot token 有效性，主动发现过期。

- **R17.2.1**：每小时后台线程用全局 bot token 调 VCS API GET /user。现有代码 `CHECK_INTERVAL=300s`（5min）+ `FAILURE_THRESHOLD=3`（最坏 15min 才标记），改为 `CHECK_INTERVAL=3600s` + `FAILURE_THRESHOLD=1`（1 次 401 失败即标记 expired）。理由：有 fail_task 401 兜底，不需要连续 3 次防误判——健康检查标记后短路，但 fail_task 验证 token 有效时会自动清除标记
- **R17.2.2**：401 → 标记 `meta.global_bot_token_status=expired`（per-provider：`global_gitcode_bot_token_status` / `global_github_bot_token_status`，R21.2）
- **R17.2.3**：200 → 清除过期标记（如果之前标记过）→ 短路自动解除（R16.4 短路机制感知标记清除后重新返回 token）
- **R17.2.4**：有仓库级 bot token 的仓库（含 GitCode `bot_gitcode_token` 和 GitHub `bot_github_token`），每小时验证仓库级 token
- **R17.2.5**：401 → 标记 `managed_repos.bot_token_status=expired`
- **R17.2.6**：200 → 清除过期标记 → 短路自动解除
- **R17.2.7**：健康检查在后台线程执行，失败不影响主流程
- **R17.2.8**：用户 token 不做主动健康检查（OAuth token 自动刷新，PAT 在使用时被动检测）
- **R17.2.9**：健康检查用分布式锁（`run_with_distributed_lock`），锁名 per-provider：`token_health_check:gitcode` / `token_health_check:github`（R24.6 修正：原 `token_health_check` 不区分 provider，GitCode 健康检查会阻塞 GitHub），多实例只有一个执行
- **R17.2.10**：仓库级 token 健康检查分批执行（每 10 个一批，间隔 1 秒），避免 rate limit
- **R17.2.11**：仓库级 token 健康检查遍历方式 — 查询 `managed_repos` 中 `bot_gitcode_token!=''` 或 `bot_github_token!=''` 的仓库，按 provider 调对应 VCS API GET /user 验证

### R17.3 健康检查结果通知

- **R17.3.1**：全局 bot token 健康检查发现过期 → dashboard API 返回标记 → 所有路径 A 仓库用户看到提示
- **R17.3.2**：仓库级 bot token 健康检查发现过期 → 该仓库用户看到提示
- **R17.3.3**：通知复用 R16.2 的统一通知机制（dashboard/task 详情 + settings 页）

### R17.4 短路机制复用

健康检查标记的过期状态复用 R16.4 的短路机制：

- **R17.4.1**：健康检查标记的过期状态复用 R16.4 短路机制
- **R17.4.2**：全局 bot token 标记过期 → `_get_global_bot_token` 短路返回空串
- **R17.4.3**：仓库级 bot token 标记过期 → `get_bot_token_by_repo` 短路返回空串
- **R17.4.4**：管理员/用户重新配置 token → 清除标记 → 短路解除

### R21.3 健康检查/verify 超时误标记修复（A3 级矛盾修复）

**问题**：R18.5.10"超时视为验证失败（标记过期）"+ R17.2.1 `FAILURE_THRESHOLD=1` + R16.1.3"必须先验证"三者矛盾。一次网络抖动/5xx → 全局 token 标记 expired → 短路 → 所有 task NO_TOKEN_AVAILABLE → 恢复要等下一小时健康检查。

**设计**：区分 401（真过期）和超时/5xx（可能网络问题），只对 401 标记过期。

- **R21.3.1**：`verify_token` 返回 401 → 标记过期（token 确实无效）
- **R21.3.2**：`verify_token` 超时或 5xx → 不标记过期，记录 warning，等下次健康检查重试
- **R21.3.3**：R18.5.10 修正：超时不视为验证失败（不标记过期），只记 warning。原"超时视为失败"设计取消
- **R21.3.4**：R17.2.1 修正：`FAILURE_THRESHOLD=1` 只对 401 生效。超时/5xx 不计入 `FAILURE_THRESHOLD`
- **R21.3.5**：`fail_task` `detect_and_mark_expired` 中 `provider.verify_token(token)` 区分：
  - HTTP 401 → 标记过期 + 返回 error_code
  - 超时 / 5xx → 不标记，返回空（401 可能是其他原因）
  - 其他异常 → 不标记，返回空
- **R21.3.6**：最坏情况分析：网络抖动导致 verify 超时 → 不标记 → task 仍用原 token 执行 → 如果 token 真过期 → task 401 → fail_task 再次 verify → 如果这次 verify 也超时 → 不标记 → task 失败但 error_code 为原始错误（不是 TOKEN_EXPIRED）。可接受——网络恢复后下次 verify 会正确判断
- **R21.3.7**：健康检查同理——401 才标记 expired，超时/5xx 跳过不标记

---

## 设计方案

### PAT 固有限制无法获取过期时间

PAT（Personal Access Token）有三个固有限制，决定了无法实现自动续期：

1. **无法获取过期时间**：GitCode/GitHub 的 PAT 不返回 `expires_at` 字段，系统无法知道 token 何时过期
2. **无法自动续期**：PAT 没有 `refresh_token`，不能像 OAuth token 那样自动刷新
3. **只能被动感知**：唯一发现 PAT 过期的方式是 API 调用返回 401

三层 token 受影响情况：

| 层级 | token 来源 | 有 OAuth client/secret | 无 OAuth client/secret |
|------|-----------|----------------------|----------------------|
| 用户 token | OAuth 绑定 | access_token + refresh_token，自动刷新 ✓ | 无法 OAuth 绑定，只能 PAT 手动输入，不刷新 ✗ |
| 用户 token | PAT 手动输入 | `_has_refresh_token=False`，不刷新 ✗ | 同左 ✗ |
| 全局 bot token | 手动输入 | PAT，无 refresh_token，不刷新 ✗ | 同左 ✗ |
| 仓库级 bot token | 手动输入 | PAT，无 refresh_token，不刷新 ✗ | 同左 ✗ |

**关键结论**：
- 用户 token 是唯一可能支持自动刷新的层级，前提是配置了 OAuth client/secret 并通过 OAuth 绑定
- 全局 bot token 和仓库级 bot token 无论是否有 OAuth 配置，都是手动输入的 PAT，不会有自动续期能力
- 无 OAuth client/secret 时，三层 token 全是 PAT，全部不刷新

当前系统对 PAT 过期的处理是**被动检测**——等 task 401 失败后才知道过期（R10/R16.1）。没有主动续期能力，没有过期前预警。

### 方案 C 采纳：健康检查 + 通知 + 手动恢复

#### 方案对比

| 方案 | 原理 | 自动刷新 | 实现复杂度 | 平台依赖 | 安全风险 | 采纳 |
|------|------|---------|-----------|---------|---------|------|
| **A: bot OAuth 授权** | bot 账号走 OAuth 授权流程获取 refresh_token | ✓ | 高（OAuth 流程 + 存储扩展 + 并发刷新） | 需 OAuth App 配置 | 低 | ✗ 不采纳（代价过大） |
| **B: 永久 PAT** | 使用 GitCode/GitHub 永久有效 PAT | 不需要 | 无 | GitCode 支持，GitHub fine-grained 不支持 | 高（泄露后无自动失效） | ✗ 不采纳（不可控） |
| **C: 健康检查 + 通知 + 手动恢复** | 定期验证 token 有效性，401 标记过期 + 通知 + 短路 | ✗ | 中（R17 已设计） | 无 | 低 | ✓ 采纳 |

#### 方案 A 不采纳原因

bot 账号需要完成 OAuth 授权流程——管理员需要以 bot 账号身份登录 VCS 平台完成 OAuth 授权，是人工操作。client/secret 是平台级的，授权流程仍需人工触发。管理员配置全局 bot token 时当前是手动输入一个字符串（PAT），改成 OAuth token 流程复杂得多。多实例部署下 bot token 是共享的（meta 表），OAuth refresh 需要考虑并发刷新问题。

#### 方案 B 不采纳原因

GitCode PAT 创建时可选永久，GitHub classic PAT 也可设永久，但 GitHub fine-grained PAT **必须**设过期时间（最长 1 年）。永久 PAT 被撤销后仍会失效，且安全风险高（泄露后无自动失效机制）。

#### 方案 C 优势

1. 不需要 bot 账号完成 OAuth 流程
2. 不依赖 VCS 平台是否支持永久 PAT
3. 主动发现过期（不等 task 401），用户立即收到提示
4. 恢复路径清晰（重新配置 token 即可）

**代价**：token 过期后仍需人工恢复（不能自动续期），但健康检查 + 通知将恢复时间从"用户发现 task 失败 → 联系管理员 → 管理员排查 → 重新配置"缩短为"系统自动发现 → 通知管理员/用户 → 管理员重新配置"。

### 健康检查 → 短路 → 无 token → fail_task 协调流程

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

### R21.3 区分 401 和超时

R21.3 修复了原设计中的 A3 级矛盾：R18.5.10"超时视为验证失败（标记过期）"+ R17.2.1 `FAILURE_THRESHOLD=1` + R16.1.3"必须先验证"三者矛盾。

**修复原则**：区分 401（真过期）和超时/5xx（可能网络问题），只对 401 标记过期。

- `verify_token` 返回 401 → 标记过期（token 确实无效）
- `verify_token` 超时或 5xx → 不标记过期，记录 warning，等下次健康检查重试
- `FAILURE_THRESHOLD=1` 只对 401 生效，超时/5xx 不计入

**最坏情况**：网络抖动导致 verify 超时 → 不标记 → task 仍用原 token 执行 → 如果 token 真过期 → task 401 → fail_task 再次 verify → 如果这次 verify 也超时 → 不标记 → task 失败但 error_code 为原始错误（不是 TOKEN_EXPIRED）。可接受——网络恢复后下次 verify 会正确判断。

---

## 涉及代码文件

| 文件 | 改动内容 |
|------|---------|
| `src/runtime/bot_token_health.py` | 核心改造：`CHECK_INTERVAL` 300→3600、`FAILURE_THRESHOLD` 3→1（只对 401）、`_verify_bot_token` 改为通过 `provider.verify_token()` 调用（R18.5.8）、新增仓库级 token 遍历检查（R17.2.4/R17.2.11）、区分 401 和超时（R21.3.7）、分批执行（R17.2.10） |
| `src/runtime/service.py` | `fail_task` 中 `detect_and_mark_expired` 逻辑：`provider.verify_token(token)` 区分 401/超时/5xx（R21.3.5）、设短超时 5s（R18.5.10 修正后只对 401 标记）、append error_code |
| `src/user/db.py` | `_get_global_bot_token` 短路检查（R16.4.1/R17.4.2）、`get_bot_token_by_repo` 短路检查（R16.4.2/R17.4.3）、`managed_repos` 新增 `bot_token_status` 列（R16.5.1）、meta 新增 `global_*_bot_token_status` per-provider key（R16.5.2/R21.2.5） |
| `src/admin/config_store.py` | `save_config` 更新全局 bot token 时清除过期标记（R16.3.2/R17.4.4） |
| `src/api/routes/repos.py` | `set_repo_bot_token`（PUT /repos/bot-token）更新仓库级 token 时清除 `bot_token_status` 过期标记（R16.3.3）、返回 PAT 无续期提示（R17.1.3） |
| `src/api/routes/settings.py` | settings 页配置全局/仓库级 bot token 时提示"此 Token 为 PAT 类型，不会自动续期"（R17.1.2） |
| `src/api/static/js/pages/account.js` | `_accountManualBindForm` PAT 无续期提示（已有 R13.3，R17.1.1） |
| `src/api/static/locales/zh/app.json` | `pat_no_refresh_hint` 国际化文案（已有，R17.1.4 统一文案） |
| `src/admin/routes.py` | `/bot-token-health` admin API 返回健康状态（保留 `bot_token_health` 三级状态 + 新增 per-provider 过期标记） |
| `src/api/main.py` | `start_bot_token_health()` 启动健康检查后台线程 |
| `src/lock.py` | 分布式锁 `run_with_distributed_lock` / `acquire_lock`（R17.2.9，已有基础设施） |

---

## 验收标准

1. **R17.1 — 配置时提示**：
   - 用户 token PAT 绑定时显示"PAT 不会自动续期"提示（已有 R13.3，验证不回归）
   - settings 页配置全局 bot token 时显示"此 Token 为 PAT 类型，不会自动续期，过期后需手动重新配置"
   - PUT /repos/bot-token 配置仓库级 token 时返回/显示同样提示

2. **R17.2 — 定期健康检查**：
   - `CHECK_INTERVAL=3600s`（1 小时），`FAILURE_THRESHOLD=1`（1 次 401 即标记）
   - 全局 bot token 每小时调 VCS API GET /user 验证
   - 仓库级 token 遍历 `managed_repos` 中 `bot_gitcode_token!=''` 或 `bot_github_token!=''` 的仓库，按 provider 调对应 VCS API 验证
   - 仓库级检查分批执行（每 10 个一批，间隔 1 秒）
   - 健康检查用分布式锁，多实例只有一个执行
   - 401 → 标记过期；200 → 清除过期标记
   - 健康检查在后台线程执行，失败不影响主流程

3. **R17.3 — 通知**：
   - 全局 bot token 过期 → dashboard 所有路径 A 仓库用户看到提示
   - 仓库级 bot token 过期 → 该仓库用户看到提示
   - 通知复用 R16.2 统一通知机制

4. **R17.4 — 短路复用**：
   - 健康检查标记的过期状态复用 R16.4 短路机制
   - 重新配置 token → 清除标记 → 短路解除

5. **R21.3 — 超时不误标记**：
   - `verify_token` 401 → 标记过期
   - `verify_token` 超时或 5xx → 不标记，记 warning
   - `FAILURE_THRESHOLD=1` 只对 401 生效，超时/5xx 不计入
   - `fail_task` 中 `detect_and_mark_expired` 同样区分 401 和超时
   - 健康检查同理——401 才标记 expired

6. **场景用例覆盖**：
   - 用例 4.1：健康检查发现全局 bot token 过期（task 还没执行）
   - 用例 4.2：健康检查发现仓库级 bot token 过期
   - 用例 4.3：健康检查发现 token 恢复有效
   - 用例 4.4：多实例同时健康检查（分布式锁）
   - 用例 4.5：大量仓库级 token 健康检查触发 rate limit（分批执行）
   - 用例 6.1：实例 A 标记过期后实例 B 的缓存延迟（60s _cached_get_meta + fail_task 401 兜底）

---

## 依赖场景

| 依赖 | 说明 |
|------|------|
| **02-expiry-framework（R16 + R21.2）** | 健康检查标记的过期状态复用 R16.4 短路机制；`bot_token_status` 列 / `global_*_bot_token_status` per-provider key 由 R16.5/R21.2 定义；通知复用 R16.2；恢复复用 R16.3 |
| **04-token-resolver（R18）** | 健康检查通过 `provider.verify_token()` 调用 VCS API（R18.5.8），不直接调 `_verify_bot_token`；策略链 `get_token` 感知标记清除后重新返回 token |
| **01-repo-token-config（R15）** | PUT /repos/bot-token 写入新 token 时清除 `bot_token_status` 过期标记（R15 步骤 6 / R16.3.3）；配置时返回 PAT 无续期提示（R17.1.3） |
| **10-global-token-swap（R21.1-R21.3）** | R21.3 超时误标记修复直接修改本文档 R17.2.1 的 `FAILURE_THRESHOLD` 语义；per-provider key（R21.2）影响健康检查标记位置 |

### 实现顺序

按 `00-index.md` 建议顺序：
1. 04-token-resolver — 基础设施（策略链 + VCSProvider）
2. 02-expiry-framework — 过期框架（标记 + 短路 + 恢复 + per-provider key）
3. 01-repo-token-config — 仓库级 token 配置流程
4. **03-health-check — 健康检查**（本文档）
5. 10-global-token-swap — 全局 token 更换 + 超时修复

### 未来扩展

如果后续需要自动刷新，可在方案 C 基础上扩展为方案 A：
1. 全局/仓库级 bot token 存储增加 `refresh_token` / `expires_at` 字段
2. 配置时支持"OAuth 授权"方式（管理员以 bot 身份完成 OAuth 流程获取 token）
3. 健康检查发现 token 临近过期时调 `refresh_oauth_token` 自动刷新
4. 刷新失败再走方案 C 的过期通知 + 手动恢复流程

这属于后续迭代，当前不实现。
