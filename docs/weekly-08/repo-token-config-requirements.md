# 仓库级 Token 配置处理逻辑 + Token 过期统一框架需求

> **实现状态声明（A4）**：本文档（R15-R21）为设计提案需求，当前零实现。
> 主线（issue-372-user-token-only-requirements.md，R1-R14）已实现并代码验证。
> 实现时应先对齐主线已实现内容，避免重复或矛盾。
> 与主线的关系详见设计文档（repo-token-config-design.md）头部声明。

## 一、总览

### 1.1 三层 Token 体系

| 层级 | 存储位置 | token 来源 | 配置者 | 自动刷新 |
|------|---------|-----------|--------|---------|
| 用户 token | external_identities | OAuth 绑定 / PAT 手动输入 | 用户 | OAuth 绑定可刷新，PAT 不刷新 |
| 全局 bot token | meta 表 | 手动输入（PAT） | 管理员 | 不刷新 |
| 仓库级 bot token | managed_repos | 手动输入（PAT） | 有仓库权限的用户 | 不刷新 |

### 1.2 自动续期能力矩阵

| 层级 | token 来源 | 有 OAuth client/secret | 无 OAuth client/secret |
|------|-----------|----------------------|----------------------|
| 用户 token | OAuth 绑定 | access_token + refresh_token，自动刷新 | 无法 OAuth 绑定，只能 PAT，不刷新 |
| 用户 token | PAT 手动输入 | _has_refresh_token=False，不刷新 | 同左 |
| 全局 bot token | 手动输入 | PAT，无 refresh_token，不刷新 | 同左 |
| 仓库级 bot token | 手动输入 | PAT，无 refresh_token，不刷新 | 同左 |

**关键结论**：
- 用户 token 是唯一可能支持自动刷新的层级，前提是配置了 OAuth client/secret 并通过 OAuth 绑定
- 全局 bot token 和仓库级 bot token 无论是否有 OAuth 配置，都是手动输入的 PAT，不会有自动续期能力
- PAT 不返回过期时间，系统无法预知何时过期，只能在 API 调用 401 时被动感知

**影响**：
- 无 OAuth client/secret 时，三层 token 全是 PAT，全部不刷新，全部会过期
- 有 OAuth client/secret 时，用户 token（OAuth 绑定）可刷新，但全局和仓库级仍不刷新
- 全局 bot token 过期影响所有路径 A 仓库（全部 task 401）
- 仓库级 bot token 过期影响单个仓库

### 1.3 当前问题

1. R10 过期检测只针对用户 token，全局/仓库级 bot token 过期后静默失败
2. 三层 token 过期处理逻辑分散，应统一为框架
3. 配置仓库级 token 时不校验有效性、顺序错误（先建 webhook 后邀 bot）、部分失败导致全断
4. token 获取散落在 13 个函数中，逻辑重复且不一致

---

## 二、场景与场景用例

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
5. fail_task 检测无 token → 写入 NO_TOKEN_AVAILABLE error_code
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
5. fail_task 检测无 token → 写入 NO_TOKEN_AVAILABLE error_code
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
2. 清理 devops_agent_session 中的 token
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

---

## 三、统一 Token 过期框架

### R16.1 统一 token 过期检测（fail_task 统一入口）

fail_task 的 401 检测不再只针对路径 B，按 token 来源统一分流：

1. host_user_id > 0 → 用户 token 过期 → 标记 external_identities.status=4
2. host_user_id == 0 + 有仓库级 bot_token → 仓库级 bot token 过期 → 标记 managed_repos.bot_token_status=expired
3. host_user_id == 0 + 无仓库级 bot_token → 全局 bot token 过期 → 标记 meta.global_bot_token_status=expired

- R16.1.1：fail_task 401 检测按 token 来源分流
- R16.1.2：用户 token 过期走现有 R10 逻辑
- R16.1.3：全局 bot token 过期验证有效性后标记 meta 表
- R16.1.4：仓库级 bot token 过期验证有效性后标记 managed_repos
- R16.1.5：task errors 统一 append 模式写入 error_code
- R16.1.6：幂等规则——已标记过期的 token 不重复标记、不重复写 error_code（设计补充）
- R16.1.7：第 1 级 devops_agent_session token 过期 → 标记 devops_agent_session.token_status=expired，短路返回空串。如未实现独立过期标记，降级为不标记但记录日志（设计补充）

### R16.2 统一过期通知

| token 来源 | error_code | 通知渠道 |
|-----------|-----------|---------|
| 用户 token | TOKEN_EXPIRED | /auth/me + account 页 + dashboard/task 详情（已有） |
| 全局 bot token | GLOBAL_TOKEN_EXPIRED | dashboard/task 详情（所有受影响用户可见） + settings 页（管理员可见） |
| 仓库级 bot token | REPO_TOKEN_EXPIRED | dashboard/task 详情（该仓库用户可见） + settings 页（有仓库权限的用户可见） |

- R16.2.1：task errors 统一 error_code 格式，所有用户在 task 详情中可见
- R16.2.2：dashboard/task 详情显示 error_code 映射的本地化文案
- R16.2.3：settings 页返回全局/仓库级 bot token 状态
- R16.2.4：issue 回帖 best-effort（有可用 token 时发评论）

### R16.3 统一过期恢复

- R16.3.1：用户重新绑定 → 恢复 status=1（已有）
- R16.3.2：save_config 更新全局 bot token → 清除 meta 过期标记
- R16.3.3：PUT /repos/bot-token 更新仓库级 token → 清除 managed_repos 过期标记
- R16.3.4：新 token 写入前验证有效性

### R16.4 短路机制

- R16.4.1：全局 bot token 过期 → _get_global_bot_token 返回空串
- R16.4.2：仓库级 bot token 过期 → get_bot_token_by_repo 返回空串
- R16.4.3：用户 token status=4 保持现有行为（返回 token 供 best-effort）
- R16.4.4：devops_agent_session token 标记过期后，第 1 级策略短路返回空串，降级到第 2 级。短路检查需 try-except 保护：如果表无 token_status 列，降级为不检查（设计补充）
- R16.4.5：短路机制向前兼容——所有短路检查新增在现有逻辑之后，不修改原有 token 获取路径。未标记过期的 token 走原有逻辑完全不变（设计补充）

### R16.5 DB schema 变更

- R16.5.1：managed_repos 新增 bot_token_status 列（TEXT DEFAULT ''，幂等 ALTER）
- R16.5.2：meta 表新增 global_bot_token_status 键
- R16.5.3：devops_agent_session 新增 token_status 列（TEXT DEFAULT ''，幂等 ALTER）+ repo_github_token 列（TEXT DEFAULT ''，幂等 ALTER）（设计补充）
- R16.5.4：健康检查同时写两个 meta key——现有 bot_token_health（admin 页展示用，保留）+ 新增 global_bot_token_status（短路机制用）（设计补充）
- R16.5.5：managed_repos 新增 host_vcs_username 列（TEXT DEFAULT ''，幂等 ALTER）——托管时从 external_identities 同步写入 host 用户 VCS 平台用户名，供路径 B 自循环检测使用（设计补充，对应 R20.1.4）

### R16.6 无 token 检测

- R16.6.1：task 获取不到 token 时 fail_task 写入 NO_TOKEN_AVAILABLE error_code
- R16.6.2：dashboard 显示"无可用 Token，请联系管理员配置"

---

## 四、仓库级 Token 配置处理逻辑

### R15.1 配置前先验证 token 有效性

- R15.1.1：调 VCS API 验证 token（GitCode: GET /api/v5/user，GitHub: GET /user）
- R15.1.2：无效 → 400 + TOKEN_INVALID，不写 DB、不重建 webhook
- R15.1.3：有效 → 获取 bot username，继续

### R15.2 先邀 bot 再重建 webhook

执行顺序：验证 token → 邀 bot → 重建 webhook → 写 DB

- R15.2.1：路径 B 仓库用 host 用户 token 邀请
- R15.2.2：路径 A 仓库新 bot 账号与旧不同时也需邀请
- R15.2.3：邀请失败记日志继续（幂等）
- R15.2.4：webhook 重建在邀请之后

### R15.3 部分失败保护

- R15.3.1：删旧失败 → 不建新、不写 DB、返回错误
- R15.3.2：删旧成功建新失败 → 不写 DB、500 + WEBHOOK_REBUILD_FAILED
- R15.3.3：webhook 重建全失败 → 不写 bot_token，原状态不变

### R15.4 返回值语义

- token 无效 → 400 + TOKEN_INVALID
- 仓库不存在 → 404
- host token 过期 → 400 + HOST_TOKEN_EXPIRED
- webhook 重建失败 → 500 + WEBHOOK_REBUILD_FAILED
- 全部成功 → 200 + bot_username

### R15.5 仓库级 token 清空/移除

- R15.5.1：PUT /repos/bot-token 传空串时清除仓库级 bot token
- R15.5.2：清除后不重建 webhook（用户需 repair_host）
- R15.5.3：清除后 get_repo_token_with_refresh 第 3 级命中全局 bot token
- R15.5.4：清除时同时清除 bot_token_status 过期标记（设计补充，避免 settings 页误导用户以为 token 过期而非已清除）
- R15.5.5：清除时 best-effort 移除旧 bot 协作者身份（设计补充，R15.6.3 best-effort 规则适用）

### R15.6 旧 bot 协作者移除

- R15.6.1：更换仓库级 bot token 时只移除该仓库的旧 bot 协作者身份
- R15.6.2：不移除其他仓库的旧 bot 协作者身份
- R15.6.3：移除失败 best-effort，不影响新 token 写入

---

## 五、PAT 无自动续期问题

### R17.1 配置 token 时提示无自动续期

**用户 token**：通过 OAuth 绑定的 token 有 refresh_token，可自动刷新；通过手动输入的 PAT 无 refresh_token，不会自动刷新。配置时通过 `_has_refresh_token` 判断是否有 refresh_token，无则提示。

**全局 bot token / 仓库级 bot token**：手动输入的 token 天然是 PAT，一定没有 refresh_token，无需检测，直接提示。

- R17.1.1：用户 token PAT 绑定时提示（已有 R13.3，通过 _has_refresh_token 判断）
- R17.1.2：全局 bot token 配置时提示（settings 页，直接提示，无需检测 refresh_token）
- R17.1.3：仓库级 bot token 配置时提示（PUT /repos/bot-token 返回提示或 settings 页，直接提示，无需检测 refresh_token）
- R17.1.4：提示文案："此 Token 为 PAT 类型，不会自动续期，过期后需手动重新配置"

### R17.2 全局/仓库级 bot token 定期健康检查

- R17.2.1：每小时后台线程用全局 bot token 调 VCS API GET /user。CHECK_INTERVAL 改为 3600s + FAILURE_THRESHOLD=1（1 次失败即标记 expired，有 fail_task 401 兜底）（设计补充）
- R17.2.2：401 → 标记 meta.global_bot_token_status=expired
- R17.2.3：200 → 清除过期标记
- R17.2.4：有仓库级 bot token 的仓库（含 GitCode bot_gitcode_token 和 GitHub bot_github_token），每小时验证仓库级 token
- R17.2.5：401 → 标记 managed_repos.bot_token_status=expired
- R17.2.6：200 → 清除过期标记
- R17.2.7：健康检查在后台线程执行，失败不影响主流程
- R17.2.8：用户 token 不做主动健康检查（OAuth token 自动刷新，PAT 在使用时被动检测）
- R17.2.9：健康检查用分布式锁（run_with_distributed_lock），多实例只有一个执行
- R17.2.10：仓库级 token 健康检查分批执行（每 10 个一批，间隔 1 秒），避免 rate limit
- R17.2.11：仓库级 token 健康检查遍历方式——查询 managed_repos 中 bot_gitcode_token!='' 或 bot_github_token!='' 的仓库，按 provider 调对应 VCS API GET /user 验证（设计补充）

### R17.3 健康检查结果通知

- R17.3.1：全局 bot token 健康检查发现过期 → 所有路径 A 仓库用户看到提示
- R17.3.2：仓库级 bot token 健康检查发现过期 → 该仓库用户看到提示
- R17.3.3：复用 R16.2 统一通知机制（dashboard/task 详情 + settings 页）

### R17.4 短路机制复用

- R17.4.1：健康检查标记的过期状态复用 R16.4 短路机制
- R17.4.2：全局 bot token 标记过期 → _get_global_bot_token 短路返回空串
- R17.4.3：仓库级 bot token 标记过期 → get_bot_token_by_repo 短路返回空串
- R17.4.4：重新配置 token → 清除标记 → 短路解除

---

## 六、Token 获取统一接口（策略模式）

### R18.1 Token 策略基类

- R18.1.1：TokenStrategy 基类含 get_token / _fetch / _is_expired / mark_expired / clear_expired
- R18.1.2：get_token 检查过期标记，过期则短路返回空串
- R18.1.3：每个策略有唯一 error_code

### R18.2 四级策略

- R18.2.1：DevopsAgentSessionToken — devops_agent_session.repo_gitcode_token / repo_github_token
- R18.2.2：RepoBotToken — managed_repos.bot_gitcode_token / bot_github_token + host_user_id 守卫 + 过期短路
- R18.2.3：GlobalBotToken — 全局 bot token（meta: global_bot_gitcode_token / global_bot_github_token）+ host_user_id 守卫 + 过期短路
- R18.2.4：UserToken — 用户 token + OAuth 自动刷新 + status=4 不短路（best-effort 发评论）

### R18.3 统一入口 TokenResolver

- R18.3.1：按优先级链执行策略，第一个非空 token 胜出
- R18.3.2：detect_and_mark_expired 供 fail_task 401 统一调用
- R18.3.3：支持跳过特定策略（如 get_repo_bot_token 跳过第 1 级）

### R18.4 向后兼容

- R18.4.1：现有 13 个函数保留为薄包装，委托 TokenResolver
- R18.4.2：调用方无需改动
- R18.4.3：现有测试全部通过

### R18.5 provider 接口隔离

基于接口编程，GitCode 和 GitHub 各自实现 VCSProvider 接口，不耦合。

- R18.5.1：定义 VCSProvider 接口（抽象基类 / Protocol），包含以下方法：
  - verify_token(token) → dict：验证 token 有效性，返回用户信息
  - get_user_login(token) → str：获取 token 对应的用户名
  - add_collaborator(api_token, owner, repo, username, permission) → bool：邀 bot 为协作者
  - remove_collaborator(api_token, owner, repo, username) → bool：移除协作者
  - setup_webhook(api_token, owner, repo, webhook_url, webhook_secret) → int：创建 webhook
  - remove_webhook(api_token, owner, repo, hook_id) → bool：删除 webhook
  - token_storage_field() → str：仓库级 bot token 存储列名（bot_gitcode_token / bot_github_token）
  - webhook_token_field() → str：webhook token 存储列名（gitcode_webhook_token / github_webhook_token）
  - session_token_field() → str：devops_agent_session token 列名（repo_gitcode_token / repo_github_token）
  - global_bot_meta_key() → str：全局 bot token meta key（global_bot_gitcode_token / global_bot_github_token）
  - pat_create_url() → str：PAT 创建页链接
- R18.5.2：GitCodeProvider 实现 VCSProvider 接口
  - verify_token: GET https://gitcode.com/api/v5/user，Bearer token，Accept: application/json
  - add_collaborator: permission 枚举 pull/push/admin/customized，幂等判断中文关键词
  - setup_webhook: body 含 password 字段 + push_events 等布尔字段
  - token_storage_field: bot_gitcode_token
  - webhook_token_field: gitcode_webhook_token
  - session_token_field: repo_gitcode_token
  - global_bot_meta_key: global_bot_gitcode_token
  - pat_create_url: GitCode PAT 创建页
- R18.5.3：GitHubProvider 实现 VCSProvider 接口
  - verify_token: GET https://api.github.com/user，Bearer token，Accept: application/vnd.github+json，User-Agent: Grape-DevOps
  - add_collaborator: permission 枚举 pull/push/admin，幂等判断 403 状态码
  - setup_webhook: body 含 config.secret + events 数组
  - token_storage_field: bot_github_token
  - webhook_token_field: github_webhook_token
  - session_token_field: repo_github_token
  - global_bot_meta_key: global_bot_github_token
  - pat_create_url: https://github.com/settings/tokens
- R18.5.4：TokenStrategy 基类持有 VCSProvider 实例，_fetch / _is_expired / mark_expired 内部通过 provider 接口获取存储字段名和 API 调用，不硬编码 provider 分支
- R18.5.5：TokenResolver 通过 provider 标识创建对应 VCSProvider 实例传入策略链
- R18.5.6：新增 provider 时只需实现 VCSProvider 接口 + 在 TokenResolver 工厂方法注册，不需要修改任何策略类代码
- R18.5.7：R15 仓库级 token 配置流程中的验证 token / 邀 bot / 重建 webhook 全部通过 VCSProvider 接口调用，不直接调 _req / _github_req / add_collaborator / add_github_collaborator。_rebuild_webhook_with_bot_token 也通过 provider.remove_webhook() / provider.setup_webhook() 调用
- R18.5.8：R17 健康检查也通过 provider.verify_token() 调用 VCS API，不直接调 _verify_bot_token
- R18.5.9：VCSProvider 接口新增 fetch_user_token(username, user_id) 方法，UserToken._fetch 通过此接口调用，不判断 provider 类型
- R18.5.10：detect_and_mark_expired 中 provider.verify_token(token) 设短超时（5s），避免阻塞 fail_task

### 用户 token 特殊处理说明

用户 token 策略（UserToken）与其他策略的区别：
- token 类型：OAuth token（有 refresh_token，自动刷新）或 PAT（无 refresh_token，不刷新）
- 过期检测：被动检测（task 401 时触发），不做主动健康检查（OAuth 自动刷新已覆盖）
- 过期后行为：仍返回 token（status=4 不短路），供 best-effort 发评论
- 恢复方式：用户重新绑定（create/update_external_identity 恢复 status=1）

### R18.6 Token 操作职责链（TokenActionHandler）（设计补充）

策略链只解决"取 token"，但 4 级 token 在"用 token 做什么"上有各自独立的职责差异：自循环检测逻辑不同、评论 fallback 链不同、过期标记位置不同。引入 TokenActionHandler 职责链，与 TokenStrategy 策略链串联。

- R18.6.1：定义 TokenResult 结构（token + level + provider + repo_key + expired），TokenResolver 新增 resolve() 返回 TokenResult
- R18.6.2：定义 TokenActionHandler 抽象接口（is_self_loop_actor / get_comment_token / post_issue_comment / mark_expired / clear_expired）
- R18.6.3：SessionTokenHandler——is_self_loop_actor 降级为不检测，get_comment_token 返回空（scope 受限），mark_expired 写 devops_agent_session.token_status
- R18.6.4：RepoBotTokenHandler——is_self_loop_actor 查 managed_repos.bot_username，get_comment_token 仓库级过期时 fallback 全局 bot
- R18.6.5：GlobalBotTokenHandler——is_self_loop_actor 查 global_bot_usernames，get_comment_token 全局过期时 fallback 仓库级 bot
- R18.6.6：UserTokenHandler——is_self_loop_actor 四条件组合判断（R20.1.1），get_comment_token 用户 token 过期时 best-effort 全局 bot
- R18.6.7：create_action_handler 工厂方法按 TokenResult.level 分发 Handler
- R18.6.8：pipeline 自循环检测改为通过 handler.is_self_loop_actor() 调用
- R18.6.9：fail_task 评论 token 选择改为通过 handler.get_comment_token() 调用
- R18.6.10：_post_result_to_issue 改为通过 handler.post_issue_comment() 调用
- R18.6.11：get_token_with_level() 新增——返回 TokenResult；get_token() 保留——返回 str，向后兼容
- R18.6.12：现有 13 个薄包装函数签名不变返回 str，内部调 resolve().token
- R18.6.13：Repo.bot_token 改为内部调 TokenResolver.get_token()（R20.11.1）
- R18.6.14：Chat _resolve_gitcode_token 改为调 TokenResolver.resolve() + Handler（R20.7.2）
- R18.6.15：飞书 VOD handler 改为调 TokenResolver.resolve()（R20.7.4）
- R18.6.16：check_running_task 补充降级查询——查 devops_agent_session.last_active_at > now-300s AND status=active（R20.10.5）

---

## 七、其他需求

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
- R19.3.3：用户确认解绑后，路径 B 仓库的 host_user_id 仍保留（指向已删除的 user），get_repo_token_with_refresh 第 4 级查不到用户 token → 返回空 → task fail（NO_TOKEN_AVAILABLE）。仓库保持不可用状态，直到管理员配置全局 bot token 后 auto_upgrade 将其切换到路径 A（设计补充）
- R19.3.4：删除仓库时清理 bot_token_status 过期标记 + 清理 devops_agent_session 中的 repo_gitcode_token + repo_github_token + token_status（设计补充）

### R19.4 GitHub fine-grained PAT 过期提醒

- R19.4.1：GitHub token 过期提示中包含 GitHub PAT 创建页链接（https://github.com/settings/tokens）
- R19.4.2：GitCode token 过期提示中包含 GitCode PAT 创建页链接

---

## 八、路径 B Webhook 过滤问题

### 8.1 问题本质

路径 B（用户 token 驱动）下，agent 用用户 token 调 VCS API，所有 API 操作（发评论、改 issue、创建 PR 等）的 actor 是用户而非 bot。现有 webhook 过滤链路的自循环检测依赖 actor 是否在 bot_usernames 列表中——路径 B 的 host 用户不在该列表中，导致 agent 自身动作的 webhook 回声不被拦截，触发重复执行。

路径 A 与路径 B 的自循环检测差异：

| 维度 | 路径 A（bot token） | 路径 B（用户 token） |
|------|---------------------|----------------------|
| agent 发评论的 actor | bot_username | host_user 用户名 |
| 步骤 2.0 自循环检测 | actor in bot_usernames → 命中 → 跳过 | actor = host_user → 不在 bot_usernames → 不命中 |
| 步骤 2.0a 标记检测 | 系统回帖有 `<!-- grape-auto-comment -->` → 命中 | 系统回帖有标记 → 命中；但 agent 直接调 API 发的评论无标记 → 不命中 |
| 结果 | 自循环被拦截 | agent 直接评论触发新 pipeline → 重复执行 |

### 8.2 场景与场景用例

### 场景 12：路径 B agent 评论自循环

**初始状态**：路径 B 仓库（host_user_id > 0, bot_gitcode_token="", webhook_secret=随机密钥），用户 PAT 未过期。

#### 用例 12.1：agent 直接发 Issue 评论触发重复执行

1. 用户在 GitCode 创建 Issue → webhook 触发 pipeline → agent 开始执行
2. agent 执行过程中用用户 token 调 GitCode API POST /repos/{owner}/{repo}/issues/{iid}/comments 发评论
3. 评论 actor = host_user 用户名（不是 bot）
4. 评论内容不含 `<!-- grape-auto-comment -->` 标记（agent 自己发的，不是系统回帖）
5. GitCode webhook 推送 issue_comment 事件
6. pipeline 步骤 2.0a：评论无标记 → 不命中
7. pipeline 步骤 2.0：actor = host_user，不在 bot_usernames → 不命中
8. pipeline 步骤 2.2 L1 过滤：issue_comment action=comment → 放行
9. pipeline 路由：issue_comment → issue agent → 派发新 task
10. 新 task 执行 → agent 再次发评论 → 再次触发 webhook → 无限循环（直到 rate limit 或用户干预）

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
4. 评论内容不含标记
5. GitCode webhook 推送 note_hook_pr 事件
6. pipeline 步骤 2.0a：评论无标记 → 不命中
7. pipeline 步骤 2.0：actor = host_user → 不命中
8. pipeline 路由：note_hook_pr → PR 订阅匹配 → 派发新 QA task
9. 新 QA task 执行 → agent 再次发 PR 评论 → 再次触发 webhook → 重复执行

#### 用例 12.4：agent 编辑 Issue description 触发重复执行

1. agent 用用户 token 调 GitCode API PATCH /repos/{owner}/{repo}/issues/{iid} 编辑 issue description
2. 编辑 actor = host_user 用户名
3. GitCode webhook 推送 issue_updated 事件（action=update, changes.description）
4. pipeline 步骤 2.0a2：description 不含 `<!-- grape-auto-comment -->` 标记 → 不命中
5. pipeline 步骤 2.1：changes 有 description（不是纯 labels）→ 不命中
6. pipeline 步骤 2.2 L1 过滤：issue_updated update + changes.description → 放行
7. pipeline 路由：issue_updated → issue agent → 派发新 task
8. 新 task 执行 → agent 再次编辑 issue → 再次触发 webhook → 重复执行

### 场景 13：路径 B fail_task 回帖必然失败

**初始状态**：路径 B 仓库，用户 PAT 已过期。

#### 用例 13.1：fail_task 无法发 Issue 评论通知用户

1. 用户 PAT 过期 → agent 执行 task → 401 失败
2. fail_task 检测 401 → 路径 B → 标记 status=4 → task errors append TOKEN_EXPIRED
3. fail_task 尝试发 Issue 评论通知用户
4. 选 token：先试全局 bot token → 路径 B 无全局 bot token → 空
5. fallback 到用户 token → 用户 token 已过期（这正是 fail_task 被调用的原因）
6. 用过期 token 发评论 → 401 失败
7. Issue 上没有任何提示 → 用户只能从 dashboard 看到 token 过期
8. 如果用户不主动查看 dashboard → 用户不知道 token 已过期

#### 用例 13.2：fail_task 回帖成功（全局 bot token 配置后）

1. 用户 PAT 过期 → fail_task 检测 401
2. 管理员已配置全局 bot token（路径 B 仓库尚未 auto_upgrade）
3. fail_task 选 token：全局 bot token 非空 → 用全局 bot token
4. 但路径 B 仓库 bot 账号未被邀请为协作者 → bot 无仓库权限
5. 用全局 bot token 发评论 → 403（bot 无权限）或成功（如果 issue 评论不需要仓库权限）
6. 结果不确定——取决于 VCS 平台的评论权限模型

### 场景 14：路径 B agent 执行期间 token 过期

**初始状态**：路径 B 仓库，用户 PAT 即将过期（剩余 < 10 分钟）。

#### 用例 14.1：agent 结果回帖失败

1. agent 开始执行 → 获取用户 token（剩余 < 10 分钟）
2. agent 执行耗时 15 分钟（复杂任务）
3. 执行到第 10 分钟时 token 过期
4. agent 完成 → _post_result_to_issue 用用户 token 发结果评论
5. token 已过期 → 401 失败
6. Issue 上没有 agent 结果评论
7. 用户不知道 agent 执行了什么、结果是什么
8. dashboard 有 task 状态但无 Issue 评论

#### 用例 14.2：agent 执行中 git push 失败

1. agent 开始执行 → 获取用户 token
2. agent 修改代码 → git push 用用户 token 认证
3. 执行到中途 token 过期
4. git push → 401 失败
5. agent 报错退出 → fail_task 检测 401 → 标记 status=4
6. 代码修改未推送到远程 → 工作丢失（仅本地 agent 侧）

### 场景 15：路径 B auto_upgrade 删旧 webhook 失败

**初始状态**：路径 B 仓库（webhook 用用户 token 创建，webhook_secret=随机密钥），管理员配置全局 bot token 触发 auto_upgrade。

#### 用例 15.1：host 用户 token 过期导致 auto_upgrade 跳过

1. 管理员配置全局 bot token → auto_upgrade_path_b_repos 遍历路径 B 仓库
2. 获取 host 用户 token → host 用户 PAT 已过期 → token 为空
3. 跳过该仓库（line 2413-2415: `if not host_token: skipped += 1; continue`）
4. 仓库保持路径 B → 用户 token 仍过期 → task 无法执行
5. 用户重新绑定 PAT 后 → 仓库仍是路径 B → 需要管理员再次触发 auto_upgrade 或用户手动配 bot token

#### 用例 15.2：bot 无 admin 权限删旧 webhook

1. host 用户 token 有效 → 邀请 bot 为协作者（maintainer/push 权限）→ 成功
2. _rebuild_webhook_with_bot_token 用 bot token 删旧 webhook
3. 旧 webhook 是用户 token 创建的 → GitCode/GitHub API 删 webhook 需要仓库 admin 权限
4. bot 刚被邀请为 maintainer/push（非 admin）→ 删 webhook 403
5. 删旧失败 → 不建新 webhook → 返回空串
6. DB 写入 bot_token + 空 webhook_id → webhook 不可用
7. 仓库 webhook 断链 → 后续事件无法触发 pipeline

#### 用例 15.3：auto_upgrade 后 webhook_secret 残留

1. auto_upgrade 成功：bot 协作者邀请成功 + webhook 重建成功 + DB 写入 bot_token
2. _update_repo_bot_token 清 webhook_secret='' + 写 webhook_id
3. 但旧 webhook 的 password 是旧的 webhook_secret（随机密钥）
4. 新 webhook 的 password 是 bot token（路径 A 模式）
5. get_webhook_token_by_repo：webhook_secret 已清空 → fallback 到 gitcode_webhook_token（=bot token）
6. GitCode webhook 推送：X-GitCode-Token = 新 webhook 的 password = bot token
7. 验证：bot token == bot token → 匹配 → OK
8. 此场景正常工作（webhook 重建成功时无问题）

### 场景 16：路径 B webhook 创建权限不足

**初始状态**：用户 A 有仓库 write 权限但无 admin 权限，系统无全局 bot token。

#### 用例 16.1：非 admin 用户托管仓库失败

1. 用户 A 托管仓库 → 路径 B（无全局 bot token）
2. 调 setup_webhook(用户 token, owner, repo, webhook_url, webhook_password=webhook_secret)
3. GitCode/GitHub API 创建 webhook 需要仓库 admin 权限
4. 用户 A 无 admin 权限 → 403
5. 托管失败 → 用户 A 无法使用系统

#### 用例 16.2：admin 用户托管后转移给非 admin 用户

1. 用户 A（admin）托管仓库 → 路径 B → 成功（webhook 用 A 的 token 创建）
2. 用户 A 的 token 过期 → 重新绑定 → token 变更
3. webhook 仍用旧的 webhook_secret（随机密钥，不随 token 变更）→ webhook 验证正常
4. 但 agent 用新 token 执行 task → 正常
5. 此场景正常工作（webhook_secret 与 token 解耦的设计正确）

### 场景 17：路径 B get_all_bot_usernames 不含 host 用户

**初始状态**：路径 B 仓库，host_user_id 指向用户 A。

#### 用例 17.1：自循环检测因 actor 不在 bot_usernames 而失效

1. get_all_bot_usernames 查 managed_repos.bot_username + 全局 bot_usernames
2. 路径 B 仓库 bot_username 为空（没有 bot）→ 不在列表中
3. host_user A 的用户名不在列表中
4. agent 用 A 的 token 发评论 → actor = A 的用户名
5. pipeline 步骤 2.0：actor not in bot_usernames → 不命中
6. 自循环检测失效 → 重复执行（同场景 12）

#### 用例 17.2：将 host 用户加入 bot_usernames 的副作用

1. 如果把 host_user 用户名加入 get_all_bot_usernames
2. host_user 的正常操作（创建 issue、发评论、提交 PR）都会被自循环检测拦截
3. host_user 无法正常使用系统 → 功能受损
4. 不能简单把 host_user 加入 bot_usernames

### 场景 18：路径 B actor 相同无法区分 agent 和用户（核心矛盾）

**初始状态**：路径 B 仓库，用户 A 的 PAT 未过期，host_user_id = A。

#### 用例 18.1：简单 actor 判断导致用户评论被误拦截

1. 用户 A 在 GitCode issue 上发评论"请帮我修复这个 bug"
2. webhook 推送 issue_comment 事件 → actor = A 的 VCS username
3. 如果自循环检测简单判断 `actor == host_vcs_username → 跳过`
4. 用户 A 的评论被拦截 → 不触发流水线
5. 用户 A 等待 agent 响应 → 永远等不到 → 用户体验受损

#### 用例 18.2：agent 执行期间用户评论被误拦截

1. agent 正在执行 task（task status=running）
2. agent 用用户 token 发评论"正在分析问题..."
3. webhook 推送 issue_comment → actor = A → task 正在执行 → 四条件满足 → 跳过（正确）
4. 用户 A 在 agent 执行期间发评论"顺便也看看测试覆盖"
5. webhook 推送 issue_comment → actor = A → task 正在执行 → 四条件满足 → 跳过（误拦截）
6. 用户 A 的补充说明被忽略 → agent 不知道用户补充了信息
7. 时序竞态可接受——agent 完成后用户可以再次评论触发

#### 用例 18.3：agent 完成后用户评论正常触发流水线

1. agent 执行完成 → task status=completed
2. 用户 A 发评论"结果不对，请重新检查"
3. webhook 推送 issue_comment → actor = A
4. 四条件检测：条件 4（task 正在执行）不满足 → 不跳过 → 放行
5. pipeline 路由 → issue_comment → issue agent → 派发新 task
6. 新 task 执行 → agent 处理用户的反馈
7. 此场景正确工作——用户可以正常触发流水线

### 场景 19：路径 B 用户通过评论触发流水线

**初始状态**：路径 B 仓库，用户 A 的 PAT 未过期，无正在执行的 task。

#### 用例 19.1：用户创建 issue 触发流水线

1. 用户 A 在 GitCode 创建 issue
2. webhook 推送 issue_opened → actor = A
3. 自循环检测：event_type=issue_opened 不在检测列表（条件 3 不满足）→ 放行
4. pipeline 路由 → issue_opened → VOD agent → 派发 task
5. agent 开始执行 → task status=running
6. 此场景正确工作

#### 用例 19.2：用户在 issue 上评论触发流水线（无 agent 执行中）

1. 用户 A 在已有 issue 上发评论"请继续处理"
2. webhook 推送 issue_comment → actor = A
3. 四条件检测：
   - 条件 1（路径 B）：满足
   - 条件 2（actor == host_vcs_username）：满足
   - 条件 3（event_type=issue_comment）：满足
   - 条件 4（task 正在执行）：不满足（无 running task）
4. 四条件不全满足 → 不跳过 → 放行
5. pipeline 路由 → issue_comment → issue agent → 派发 task
6. agent 开始执行 → 此场景正确工作

#### 用例 19.3：用户在 PR 上评论触发 QA（无 agent 执行中）

1. 用户 A 在 PR 上发评论"请审查这个 PR"
2. webhook 推送 note_hook_pr → actor = A
3. 四条件检测：条件 4 不满足（无 running task）→ 放行
4. pipeline 路由 → note_hook_pr → PR 订阅匹配 → 派发 QA task
5. 此场景正确工作

#### 用例 19.4：用户使用开发者指令不被拦截

1. agent 执行完成 → task status=completed → issue 进入 pending_review
2. 用户 A 发评论"/done"
3. webhook 推送 issue_comment → actor = A
4. 开发者指令检测（步骤 2.0b，已移到自循环检测之前）：评论含 /done → confirm_issue_done → 跳过
5. 不进入自循环检测 → 不被拦截
6. 此场景正确工作——开发者指令优先处理

### 场景 20：路径 B 时序竞态

**初始状态**：路径 B 仓库，agent 正在执行 task（status=running）。

#### 用例 20.1：agent 收尾期间评论被放行（时序竞态）

1. agent 完成核心工作 → 开始收尾（准备发结果评论）
2. task 状态还是 running（agent 尚未调 exec_agent_complete 更新状态）
3. agent 用用户 token 发结果评论"任务已完成"
4. webhook 推送 issue_comment → actor = A → task status=running → 四条件满足 → 跳过（正确）
5. 如果在 agent 发评论前 100ms，task 状态恰好被更新为 completed
6. agent 发评论 → webhook 推送 → task status=completed → 条件 4 不满足 → 放行
7. pipeline 路由 → issue_comment → issue agent → 派发新 task
8. 新 task 执行 → agent 发评论 → 此时新 task status=running → 四条件满足 → 跳过
9. 最多多一次 pipeline 触发 → 可接受的时序竞态

#### 用例 20.2：agent 执行期间用户评论被放行（task 未创建）

1. 用户 A 创建 issue → pipeline 触发 → task 创建中（尚未写入 agent_tasks 表）
2. 用户 A 立即发评论"补充信息"
3. webhook 推送 issue_comment → actor = A
4. 四条件检测：条件 4（task 正在执行）→ 查 agent_tasks 表 → task 尚未写入 → 无 running task
5. 条件 4 不满足 → 放行 → pipeline 路由 → 派发新 task
6. 两个 task 并行执行 → 可能冲突
7. 可接受——task 创建和 webhook 之间的时序窗口极小（< 100ms），且现有幂等保护（同一 issue 多 task 有去重逻辑）

### 场景 21：飞书 VOD agent 自循环

**初始状态**：路径 B 仓库，用户 A 通过飞书触发 VOD agent。

#### 用例 21.1：飞书 VOD agent 发 Issue 评论触发重复执行

1. 用户 A 在飞书群 @bot "分析这个仓库"
2. 飞书 VOD handler 启动 → 用用户 token 注入 agent → agent 执行
3. VOD agent 执行过程中用用户 token 调 GitCode API 发 Issue 评论
4. 评论 actor = A 的 VCS username
5. GitCode webhook 推送 issue_comment 事件
6. pipeline 自循环检测四条件：
   - 条件 1（路径 B）：满足
   - 条件 2（actor == host_vcs_username）：满足
   - 条件 3（issue_comment）：满足
   - 条件 4（task 正在执行）：查 agent_tasks → VOD agent 没创建 task 行 → 不满足
7. 四条件不全满足 → 放行 → pipeline 派发新 task → 重复执行

#### 用例 21.2：飞书 VOD agent 创建 task 行后自循环检测正确

1. 用户 A 在飞书群 @bot "分析这个仓库"
2. 飞书 VOD handler 启动 → 在 agent_tasks 表创建 task 行（status=running）→ agent 执行
3. VOD agent 发 Issue 评论 → webhook → 自循环检测条件 4：查到 running task → 满足
4. 四条件全满足 → 跳过 → 不重复执行
5. VOD agent 完成 → 更新 task status=completed → 条件 4 不满足 → 用户后续评论可正常触发

#### 用例 21.3：飞书 VOD agent token 过期

1. 用户 A 在飞书群 @bot "分析这个仓库"
2. VOD handler 获取用户 token → token 已过期
3. VOD agent 执行 → git clone / API 调用 401 失败
4. VOD handler 返回提示"Token 已过期，请在设置页重新绑定"
5. 飞书发卡片通知用户 A → 用户看到过期提示
6. 用户重新绑定 token → 再次触发 VOD → 正常执行

#### 用例 21.4：飞书 sync mapping token 过期

1. 飞书 bot 同步群成员 ↔ VCS 协作者映射
2. 获取仓库 token → 用户 token 已过期 → 返回空
3. sync mapping 跳过（不 401 报错）
4. 飞书群发通知卡片"仓库 Token 已过期，协作者映射同步已跳过，请重新绑定 Token"
5. @mention 功能暂时失效（映射不完整）
6. 用户重新绑定 token → 下次 sync 恢复

### 场景 22：Chat 会话 token 过期

**初始状态**：路径 B 仓库，用户 A 在 Web Chat 界面与 agent 对话。

#### 用例 22.1：Chat agent token 过期

1. 用户 A 在 Web Chat 发消息"帮我看看这个 issue"
2. Chat send_message → _resolve_gitcode_token → 用户 token → 注入 agent
3. agent 执行 → 调 VCS API GET /issues → 401 失败
4. agent 返回错误消息
5. Chat 检测 401 → 检测 token 过期 → 返回友好提示"Token 已过期，请在设置页重新绑定 Token 后重试"
6. Web Chat 界面显示提示
7. 用户重新绑定 token → 再次发消息 → 正常

#### 用例 22.2：Chat direct 模式自循环

1. 用户 A 在 Web Chat 发消息 → Chat direct 模式执行 agent（不创建 agent_tasks 行）
2. agent 用用户 token 调 VCS API 发 Issue 评论（如果 skill 有 VCS 操作）
3. webhook 推送 issue_comment → actor = A
4. 自循环检测条件 4：查 agent_tasks → Chat direct 没创建 task → 不满足
5. 四条件不全满足 → 放行 → 可能重复执行
6. 但 Chat agent 通常不调 VCS API（对话场景）→ 低风险
7. 如果 Chat direct 创建 task 行（R20.10.2）→ 条件 4 满足 → 正确拦截

#### 用例 22.3：Chat ensure_issue_conversation token 过期

1. 用户 A 打开 Issue 关联会话 → Chat 同步 Issue 评论
2. Repo(rk).bot_token → 路径 B 返回用户 token → token 已过期 → 返回空（短路）
3. 同步 Issue 评论失败 → Chat 界面显示"无法同步 Issue 评论，Token 可能已过期"
4. 用户重新绑定 token → 再次打开会话 → 同步成功

### 场景 23：非 pipeline 场景 token 统一入口

**初始状态**：路径 B 仓库，用户 A 的 PAT 未过期。

#### 用例 23.1：events redispatch 通过 Repo.bot_token 获取 token

1. 用户 A 在 dashboard 对失败的事件点 redispatch
2. events redispatch → Repo(repo_key).bot_token → TokenResolver.get_token() → 返回用户 token
3. redispatch agent 用用户 token 执行 → 正常
4. 如果 token 过期 → TokenResolver 短路返回空 → redispatch agent 无 token → fail_task → NO_TOKEN_AVAILABLE
5. dashboard 显示"无可用 Token"

#### 用例 23.2：onboarding 通过 Repo.bot_token 创建仓库

1. 用户 A 执行 onboarding → Repo(repo_key).bot_token → TokenResolver.get_token() → 返回用户 token
2. 用用户 token git clone / git push → 正常
3. 如果 token 过期 → TokenResolver 短路返回空 → git clone 失败 → onboarding 报错"Token 已过期"
4. 用户重新绑定 → 再次 onboarding → 正常

#### 用例 23.3：project_info builder 通过 Repo.bot_token clone 仓库

1. pipeline 触发 project_info builder → Repo(repo_key).bot_token → TokenResolver.get_token() → 返回用户 token
2. 用用户 token git clone → 生成项目信息
3. 如果 token 过期 → 短路返回空 → clone 失败 → project_info 跳过（不影响主流程）
4. 下次 pipeline 触发时 token 已恢复 → project_info 正常生成

#### 用例 23.4：scenes experience 用用户 token 调 VCS API

1. 用户 A 在场景市场体验场景 → with_oauth_token → 用户 token → 调 GitCode API GET /user
2. token 过期 → with_oauth_token 尝试刷新（OAuth 有 refresh_token）→ 刷新成功 → 重试
3. 刷新失败（PAT 无 refresh_token）→ 返回友好提示"Token 已过期，请重新绑定"
4. scenes experience 不走 Repo.bot_token / TokenResolver（用户主动操作，走 with_oauth_token 独立链路）

### 场景 24：scheduler autopilot 自循环

**初始状态**：路径 B 仓库，autopilot 定时巡检触发。

#### 用例 24.1：autopilot agent 发 Issue 评论触发重复执行

1. scheduler 定时触发 autopilot → get_github_token / get_gitcode_token → 路径 B 返回用户 token
2. autopilot agent 执行巡检 → 用用户 token 发 Issue 评论"巡检发现 3 个问题"
3. 评论 actor = host_user VCS username
4. webhook 推送 issue_comment → 自循环检测四条件：
   - 条件 4（task 正在执行）：查 agent_tasks → autopilot 没创建 task 行 → 不满足
5. 四条件不全满足 → 放行 → pipeline 派发新 task → 重复执行

#### 用例 24.2：autopilot 创建 task 行后自循环检测正确

1. scheduler 定时触发 autopilot → 在 agent_tasks 表创建 task 行（status=running）→ agent 执行
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

1. 用户 A 在 issues 页发评论 → get_repo_bot_token / get_github_token
2. Repo.bot_token → TokenResolver 短路返回空
3. 评论失败 → 返回明确错误"Token 已过期"
4. 用户重新绑定 → 再次评论 → 成功

#### 用例 25.3：SRE resolve_pr_issue_id token 过期降级

1. SRE agent 执行 → resolve_pr_issue_id 需要调 VCS API 查 PR 关联 Issue
2. Repo.bot_token → TokenResolver 短路返回空
3. resolve_pr_issue_id 失败（无 token 不能调 API）
4. 降级为 resolve_issue_id（不调 API，从 payload 解析）或 evt.issue_id
5. 可能解析不到 issue_id → SRE agent 无法关联 Issue → task 路由失败
6. 可接受——token 过期时整个 task 都会 fail（R16.6 NO_TOKEN_AVAILABLE）

---

## 九、路径 B Webhook 过滤修复需求

### R20.1 路径 B agent 评论自循环修复

**问题**：路径 B 下 agent 用用户 token 发评论，actor 是用户而非 bot，自循环检测不命中，导致重复执行。

**核心矛盾**：路径 B 下 agent 和用户用同一个 token（用户 token），actor 完全相同（host_user VCS username）。任何仅基于 actor 的检测都无法区分"agent 回声"和"用户主动操作"。简单判断 `actor == host_vcs_username → 跳过` 会导致用户自己发评论也被拦截，无法触发流水线。

**设计约束**：
- 不能将 host_user 加入 bot_usernames（会拦截用户的正常操作，见场景 17.2）
- 不能依赖评论标记（agent 直接调 API 发的评论无标记，见场景 12.1）
- 不能仅基于 actor 判断（actor 相同无法区分，见场景 18）

**解决方案**：引入 issue 状态 + task 执行状态 + 开发者指令豁免的三条件组合判断。仅当"该 issue 有正在执行的 task" + "actor == host_vcs_username" + "事件类型是 agent 可能发的操作" + "评论内容不含开发者指令" 四条都满足时，才视为 agent 回声跳过。

- R20.1.1：路径 B 自循环检测四条件——全部满足才跳过：
  1. 仓库是路径 B（host_user_id > 0）
  2. actor == host_vcs_username（host 用户的 VCS 平台用户名）
  3. event_type in (issue_comment, note_hook_pr, issue_updated)（agent 可能发的操作类型）
  4. 该 issue 有正在执行的 task（agent_tasks.status in (running, dispatched)）或该 PR 有正在执行的 task
  条件 4 不满足 → 说明没有 agent 在执行 → actor 的操作是用户主动操作 → 放行
- R20.1.2：开发者指令豁免——评论内容含 /done /cancel /resume /完成任务 /取消任务 /恢复任务 时，即使四条件满足也不拦截（用户指令优先，先于自循环检测处理）
- R20.1.3：host_vcs_username 获取方式——从 external_identities 查 host_user_id 对应的 provider username（VCS 平台 login 字段，不是 grape account username）
- R20.1.4：host_vcs_username 缓存——避免每次 webhook 都查 external_identities，可在 managed_repos 新增 host_vcs_username 列（托管时从 external_identities 同步写入），或从 external_identities 带 TTL 缓存查询
- R20.1.5：host_vcs_username 同步——每次验证 token 有效性（provider.verify_token）时，如果返回的 login 与 DB 中的 host_vcs_username 不一致 → 更新 DB（用户改了 VCS 平台用户名的场景）
- R20.1.6：向前兼容——路径 A 仓库（host_user_id == 0）不受影响，原有 bot_usernames 检测不变
- R20.1.7：查询失败降级——如果 host_vcs_username 查询失败（external_identities 已删除等）→ 降级为不检测（不阻断 pipeline），记录 warning
- R20.1.8：task 执行状态查询——查 agent_tasks 表 WHERE repo_key=? AND issue_id=? AND status IN ('running','dispatched') LIMIT 1，有结果说明 agent 正在执行

### R20.2 路径 B fail_task 回帖降级处理

**问题**：路径 B 下 fail_task 无法发 Issue 评论（无全局 bot token + 用户 token 已过期）。

- R20.2.1：fail_task 路径 B 回帖失败时不静默——记录 warning 日志 + task errors 额外 append COMMENT_POST_FAILED
- R20.2.2：fail_task 路径 B 回帖失败时 dashboard 显示增强提示——"Token 已过期且无法发 Issue 评论，请重新绑定 Token 后在 dashboard 查看任务结果"
- R20.2.3：fail_task 路径 B 回帖失败时不重试（避免用过期 token 反复 401）
- R20.2.4：如果路径 B 仓库有全局 bot token（尚未 auto_upgrade 但已配置）→ fail_task 优先用全局 bot token 发评论（best-effort，可能 403）

### R20.3 路径 B agent 执行期间 token 过期保护

**问题**：agent 执行耗时长，执行期间用户 token 可能过期，导致结果回帖失败或 git push 失败。

- R20.3.1：agent 执行前预检 token 有效性——如果 token 剩余有效期 < 预估执行时间，记录 warning 但不阻断（token 可能续期或任务提前完成）
- R20.3.2：_post_result_to_issue 失败时（401）→ task errors append RESULT_POST_FAILED + dashboard 显示"结果已执行但无法回帖到 Issue，请在 dashboard 查看任务详情"
- R20.3.3：_post_result_to_issue 失败时不影响 task 状态——task 仍标记 completed（agent 确实完成了工作，只是回帖失败）
- R20.3.4：agent git push 失败时（401）→ fail_task 正常处理（已有 R10/R16.1 逻辑）

### R20.4 路径 B auto_upgrade webhook 重建保护

**问题**：auto_upgrade 删旧 webhook 可能因 bot 无 admin 权限或 host token 过期而失败。

- R20.4.1：auto_upgrade 遍历路径 B 仓库时，host 用户 token 过期 → 跳过并记录日志，仓库保持路径 B（已有行为，需明确为设计意图而非 bug）
- R20.4.2：auto_upgrade 删旧 webhook 失败时（bot 无 admin 权限）→ 不写 DB（不写 bot_token + 不清 host_user_id），仓库保持路径 B
- R20.4.3：auto_upgrade webhook 重建失败时 → 仓库保持路径 B，记录日志，管理员可手动 repair_host
- R20.4.4：auto_upgrade 成功标准：邀 bot + accept + webhook 重建 + DB 写入 全部成功才算升级，任一步失败则回滚到路径 B

### R20.5 路径 B webhook 创建权限提示

**问题**：非 admin 用户托管仓库时创建 webhook 403。

- R20.5.1：托管时如果 setup_webhook 返回 403 → 返回明确错误"需要仓库 admin 权限创建 webhook，请联系管理员托管或获取 admin 权限"
- R20.5.2：前端展示托管失败原因 + 指引文案

### R20.6 路径 B 自循环检测与路径 A 的一致性保证

**问题**：路径 A 和路径 B 的自循环检测逻辑不一致，路径 B 有缺口。且路径 B 下 agent 和用户 actor 相同，简单判断会误拦截用户操作。

- R20.6.1：pipeline 步骤 2.0 自循环检测改为通过 TokenActionHandler.is_self_loop_actor() 调用（R18.6.8），消除现有 if actor in bot_usernames + if _is_user_token_repo 散落分支
- R20.6.2：开发者指令检测（/done /cancel /resume）移到自循环检测之前——确保用户指令不被自循环检测误拦截（调整 pipeline 步骤顺序：2.0b 开发者指令 → 2.0 自循环检测）
- R20.6.3：路径 B 自循环检测不影响用户正常操作——用户在 issue 上评论时，如果没有正在执行的 task（条件 4 不满足）→ 放行 → 用户可以正常触发流水线
- R20.6.4：路径 B 自循环检测的时序竞态——agent 完成执行到 task 状态更新为 completed 之间有窗口，期间 agent 发的评论可能被放行（task 已不是 running 但 agent 还在收尾）。可接受——最多多一次 pipeline 触发，由下次自循环检测（task 已完成 → 条件 4 不满足 → 放行 → 但新 task 执行后自循环检测会命中）兜底
- R20.6.5：如果 host_vcs_username 查询失败（external_identities 已删除等）→ 降级为不检测（不阻断 pipeline），记录 warning

### R20.7 非 webhook 场景的 token 使用覆盖

**问题**：当前设计只覆盖 webhook pipeline 的 token 使用，但 Chat 会话、飞书 IM、events redispatch、onboarding、project_info 等场景也使用 token，这些场景直接调 Repo.bot_token / get_repo_token_with_refresh / _resolve_gitcode_token，不经过 TokenResolver/TokenActionHandler，不享受短路/过期检测/职责链。

**token 使用场景全览**（29 个场景）：

| 场景 | token 获取方式 | 是否经 TokenResolver | 问题 |
|------|--------------|---------------------|------|
| webhook pipeline exec_agent | get_repo_token_with_refresh | 将改为 TokenResolver | 已覆盖 |
| webhook _post_result_to_issue | repo_gitcode_token | 将改为 Handler | 已覆盖 |
| webhook _post_result_to_pr | repo_gitcode_token | 将改为 Handler | 已覆盖 |
| webhook create_pr_for_repo | repo_gitcode_token | 将改为 TokenResolver | 已覆盖 |
| webhook SRE resolve_pr_issue_id | get_repo_token_with_refresh | 否 | 遗漏：token 过期时路由失败 |
| webhook PR resolve_pr_issue_id | get_repo_token_with_refresh | 否 | 遗漏：同上 |
| webhook project_info builder | Repo.bot_token | 将改为 TokenResolver（R20.11） | 已覆盖 |
| webhook fail_task | comment_token | 将改为 Handler | 已覆盖 |
| Chat send_message | _resolve_gitcode_token | 将改为 resolve()+Handler（R20.7.2） | 已覆盖 |
| Chat ensure_issue_conversation | Repo(rk).bot_token | 将改为 TokenResolver（R20.11） | 已覆盖 |
| Chat list_messages | Repo(_rk).bot_token | 将改为 TokenResolver（R20.11） | 已覆盖 |
| Chat _enqueue_chat | gitcode_token | 将改为 resolve() | 已覆盖 |
| 飞书 sync_repo_feishu_mapping | get_repo_token_with_refresh | 将改为 TokenResolver（R20.7.3） | 已覆盖 |
| 飞书 VOD handler | agent_session.repo_gitcode_token → Repo.bot_token | 将改为 resolve()（R20.7.4） | 已覆盖 |
| Issues confirm_done close | get_repo_bot_token / get_github_token → with_oauth_token | 否 | 遗漏：不享受短路 |
| Issues comment | get_github_token / get_repo_bot_token | 否 | 遗漏：同上 |
| agents start/restart | get_repo_token_with_refresh | 否 | 遗漏：写入 session token 不享受级别感知 |
| settings repair_host | get_repo_token_with_refresh | 否 | 遗漏：不享受短路 |
| settings verify token | _req / _github_req → GET /user | 将改为 provider.verify_token | 已覆盖 |
| settings update_credentials | 用户 token 绑定/验证 | 独立链路 | 无问题 |
| events redispatch | Repo.bot_token | 将改为 TokenResolver（R20.11） | 已覆盖 |
| events complete | Repo.bot_token | 将改为 TokenResolver（R20.11） | 已覆盖 |
| scheduler autopilot | get_github_token / get_gitcode_token | 否 | 遗漏：P0 自循环检测失效 |
| onboarding | Repo.bot_token | 将改为 TokenResolver（R20.11） | 已覆盖 |
| project_info builder | Repo.bot_token | 将改为 TokenResolver（R20.11） | 已覆盖 |
| templates read | get_global_*_bot_token | 独立（只读全局） | 无问题 |
| vcs/api_client create_client | get_gitcode_token | 否 | 遗漏：不享受短路 |
| vcs/providers bot_token | get_gitcode_token / get_github_token | 否 | 遗漏：同上 |
| vcs/providers user_token | get_user_token_with_refresh | 不替代（R18.4.4） | 无问题 |
| deps.py get_bot_token | get_repo_token_with_refresh | 否 | 遗漏：不享受短路 |
| scenes experience | with_oauth_token | 独立链路 | 无问题 |

- R20.7.1：Repo.bot_token 改为内部调 TokenResolver.get_token()（返回 str，向后兼容）——所有调 Repo.bot_token 的场景自动继承短路/过期检测，无需改动调用方
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

- R20.8.1：Chat agent 执行返回 401/auth 错误时 → 检测是否 token 过期（复用 R16.1 检测逻辑）→ 返回友好提示"Token 已过期，请在设置页重新绑定 Token 后重试"
- R20.8.2：Chat agent 执行返回 401 但 token 验证有效 → 返回"权限不足或 Token 权限范围不够"
- R20.8.3：Chat agent token 过期提示在 Web Chat 界面显示（不是 Issue 评论）
- R20.8.4：Chat agent token 过期时不重试（避免用过期 token 反复 401）

### R20.9 飞书场景 token 过期处理

**问题**：飞书 sync mapping / VOD agent 用用户 token，token 过期时操作静默失败。

- R20.9.1：飞书 sync_repo_feishu_mapping token 过期时（get_repo_token_with_refresh 返回空或 401）→ 跳过 sync + 飞书群发通知卡片"仓库 Token 已过期，协作者映射同步已跳过，请重新绑定 Token"
- R20.9.2：飞书 VOD agent token 过期时 → VOD agent 返回提示"Token 已过期，请在设置页重新绑定"→ 飞书发卡片通知用户
- R20.9.3：飞书场景 token 过期通知通过飞书消息卡片（不是 Issue 评论），因为飞书是独立通知渠道
- R20.9.4：飞书 VOD agent token 过期不重试

### R20.10 非 webhook agent 执行的自循环检测覆盖

**问题**：飞书 VOD agent、Chat direct 模式、scheduler autopilot 不走 agent_tasks 表 → 自循环检测条件 4（check_running_task）查不到 → agent 评论触发重复执行。

- R20.10.1：飞书 VOD agent 执行时在 agent_tasks 表创建 task 行（status=running）→ 自循环检测条件 4 可查到 → agent 评论被正确拦截
- R20.10.2：Chat direct 模式执行时在 agent_tasks 表创建 task 行（status=running）→ 同 R20.10.1
- R20.10.3：scheduler autopilot 执行时在 agent_tasks 表创建 task 行（status=running）→ 同 R20.10.1（P0 修复）
- R20.10.4：非 webhook agent 执行完成后更新 task status=completed → 条件 4 不满足 → 用户后续评论可正常触发
- R20.10.5：条件 4 补充查询——除 agent_tasks.status=running/dispatched 外，也查 devops_agent_session.last_active_at > now-300s AND status=active（覆盖非 webhook agent 执行场景，含飞书 VOD / Chat direct / autopilot）
- R20.10.6：Chat worker 模式（_enqueue_chat）低风险——Chat agent 通常不调 VCS API，如 skill 有 VCS 操作则走 R20.10.2 创建 task 行

### R20.11 Repo.bot_token 统一入口

**问题**：Repo.bot_token 是所有非 pipeline 场景的 token 入口，但未纳入 TokenResolver 体系。

- R20.11.1：Repo.bot_token 改为调 service 层方法 get_repo_bot_token(repo_key)（返回 str，签名不变）。Repo 数据模型不直接 import TokenResolver，避免循环依赖
- R20.11.2：所有调 Repo.bot_token 的场景（chat/飞书/events/onboarding/project_info）自动继承短路/过期检测，无需改动调用方
- R20.11.3：Repo.bot_token 返回空串时（token 过期短路）→ 调用方应处理空 token 场景（git clone 失败有明确日志，agent 注入空 token 有 warning）
- R20.11.4：需要级别信息的调用方（Chat _resolve_gitcode_token / 飞书 VOD handler）改为调 TokenResolver.resolve() 获取 TokenResult + Handler
- R20.11.5：Repo 是数据模型（domain object），不在属性内部直接 import TokenResolver——避免数据模型 → resolver → 策略类 → Repo 循环依赖。改为 service 层方法注入（src/token_service.py）

### R20.12 webhook 验证 token 获取（设计补充，不属于 token 体系）

**问题**：webhook 收到 VCS 推送时需验证 X-Bot-Token header。验证用的 token 是"webhook 创建时用的 password"——路径 A 是 bot token，路径 B 是随机密钥（webhook_secret）。TokenResolver 策略链不包含 webhook_secret。

**归属说明**：webhook 验证的语义是"secret 相等吗"的配置项比对，不是"获取可用 token"。概念上不属于 token 体系，应放在 config/secret 模块。本文档保留是因为实现上与 token 存储有交集。

- R20.12.1：webhook 验证不走 TokenResolver，走独立的 get_webhook_verify_token()
- R20.12.2：验证优先级：webhook_secret → webhook_token_field → get_repo_bot_token_no_global（兼容旧数据）
- R20.12.3：短路机制不影响 webhook 验证——即使 bot token 过期短路，webhook 验证仍用 webhook_secret / webhook_token
- R20.12.4：路径 B 仓库 webhook 验证用 webhook_secret（随机密钥），与 token 过期状态无关
- R20.12.5：验证 token（get_webhook_verify_token）与鉴权 token（read_stored_token）本质都是"原始值比对"，但不合并。原因：数据源不同——验证 token 查 webhook_secret（不在策略链中），鉴权 token 查策略链。合并需要传 source 参数，又是参数地狱

### R20.13 A2A 鉴权 token 保留独立逻辑（设计补充）

**问题**：A2A 鉴权用 get_repo_bot_token_no_global 比对 Authorization header。token 过期短路返回空串会导致鉴权失败 → agent 之间无法通信。

- R20.13.1：get_repo_bot_token_no_global 保留为薄包装，内部调 TokenResolver.read_stored_token()（不是 get_token()）
- R20.13.2：read_stored_token() 是独立方法——只调策略链的 _fetch()（纯读存储值），不调 _is_expired()（不检查过期），不触发 OAuth refresh。与 get_token() 共享底层策略链但走不同检查路径
- R20.13.3：A2A 鉴权即使 token 过期也返回原始值用于比对（鉴权不依赖有效性，只依赖一致性）
- R20.13.4：关联仓库信任域比对同样用 read_stored_token()
- R20.13.5：session token 鉴权不受影响，走独立链路
- R20.13.6：不使用 skip_expired_check 参数——布尔 flag 会掩盖语义差异，用独立方法 read_stored_token() 让方法名表达意图

### R20.14 建单场景保留用户 token（设计补充）

**问题**：GitCode 建单必须用用户自己的 token——assign 需用户身份，bot token 无 assign 权限会 403。

- R20.14.1：issues_center create_issue GitCode 路径保留 get_user_token_with_refresh（用户身份建单）
- R20.14.2：issues_center create_issue GitHub 路径可用 Repo.bot_token
- R20.14.3：建单时 token 缺失/失效 → 引导用户重新绑定，不 fallback 到 bot token
- R20.14.4：此场景不受 TokenResolver 改造影响

### R20.15 event dialogue 自循环检测覆盖（设计补充）

**问题**：event dialogue 不走 agent_tasks 表 → 自循环检测条件 4 查不到 → agent 评论触发重复执行。

- R20.15.1：event dialogue 执行时在 agent_tasks 表创建 task 行（status=running）——**R23.2 修正**：event dialogue 是内存执行架构（_conv_tasks 字典），不创建 DB task 行。改为 R20.15.4 session heartbeat 方案
- R20.15.2：event dialogue 完成后更新 task status=completed——**R23.2 修正**：改为更新 devops_agent_session.status='idle'
- R20.15.3：event dialogue token 获取改为 Repo.bot_token（R20.11）
- R20.15.4（R23.2 补充）：event dialogue 启动时更新 devops_agent_session.last_active_at + status='active' → 让 check_running_task session 兜底查询生效
- R20.15.5（R23.2 补充）：event dialogue 完成时更新 devops_agent_session.status='idle' → 兜底不再命中 → 用户后续评论可正常触发

### R20.16 setup_webhook password 缺省行为修正（设计补充）

- R20.16.1：setup_webhook 必须显式传 webhook_password（路径 A 传 bot token，路径 B 传随机密钥）
- R20.16.2：webhook_password 为空时不再 fallback 到 get_repo_token_with_refresh
- R20.16.3：托管逻辑负责生成随机密钥并传入 setup_webhook

### R20.17 VOD handler restart 重新获取 token（设计补充）

- R20.17.1：VOD handler restart 不从 session 读旧 token，改为调 TokenResolver.resolve()
- R20.17.2：session 中的 repo_gitcode_token 仍保留（兼容 A2A 鉴权），但 VOD handler 不直接使用
- R20.17.3：token 过期短路时 VOD handler 获取空 token → 不启动 agent → 飞书通知用户

### R20.18 session token 写入不走短路（设计补充）

- R20.18.1：agents start/restart 写入 session.repo_gitcode_token 时调 TokenResolver.read_stored_token()，写入原始 token 值（不过期检查、不刷新）
- R20.18.2：短路机制仅在运行时 token 获取（TokenResolver.get_token）时生效——write 用 read_stored_token，read 用 get_token
- R20.18.3：session 中的 token 即使已过期也保留原值，供 A2A 鉴权和 VOD restart fallback
- R20.18.4：健康检查标记过期后 session 中的 token 不清除

### R20.19 token 短路后 task fail 的 error_code 检测（设计补充）

- R20.19.1：fail_task 检测到 task token 为空时直接写入 NO_TOKEN_AVAILABLE
- R20.19.2：空 token 检测在 401 关键词检测之前执行
- R20.19.3：空 token 检测不调 VCS API 验证
- R20.19.4：扩展 401 关键词列表：增加 "Authentication failed" / "could not read Username" / "fatal: Authentication"

### R20.20 其他遗漏场景补充（设计补充）

- R20.20.1：submit_feedback 检查 bot_token_available 改为调 Repo.bot_token（R20.11 已覆盖）
- R20.20.2：templates verify 保留 get_user_token_with_refresh（独立链路）；templates read 用全局 bot token（独立只读）
- R20.20.3：events terminate → fail_task：error="用户手动终止"不触发 token 过期检测；如恰好 401 后 terminate，幂等不重复
- R20.20.4：list_repo_branches → Repo.bot_token（R20.11 已覆盖）。token 过期时前端显示"Token 未配置或已失效"
- R20.20.5：_post_subtask_completion_to_parent → handler.post_issue_comment()（R18.6.10 已覆盖）
- R20.20.6：Chat _resolve_gitcode_token 保留 user_token 优先逻辑。注意：Chat 的 token 语义与执行 token 相同（都需要有效 token），但策略优先级不同（标准 TokenResolver 是 bot-first，Chat 需要 user-first）。不合并的原因是避免参数膨胀，属于"策略差异为避免参数地狱而独立"，不是"语义不同导致必须独立"

### R20.21 pipeline 步骤顺序明确（设计补充）

完整步骤顺序：2.0b 开发者指令 → 2.0a 标记检测 → 2.0 自循环检测 → 2.0a2 issue_updated 标记检测 → 2.1 L1 过滤 → 2.2 路由。2.0a 在 2.0 之前执行（标记优先拦截），2.0b 在最前（指令优先于一切）。

### R20.22 路径 B 判断方式修正（设计补充）

R20.1.1 条件 1 修正为"仓库是路径 B（_is_user_token_repo(repo_key) == True 或 _is_github_user_token_repo(repo_key) == True）"，不用 host_user_id > 0。_is_user_token_repo 判断：host_user_id > 0 AND bot_gitcode_token 为空。

### R20.23 fail_task 去重逻辑保留在 handler 外部（设计补充）

handler.get_comment_token() 只负责 token 选择，_fetch_issue_comments 去重逻辑保留在 fail_task 调用方层面。原因：去重依赖 issue 已有评论内容（VCS API 调用），不属于 token 操作职责。

### R20.24 token 注入链路（设计补充）

**问题**：TokenResolver 改造只覆盖"从 DB 获取 token"，未覆盖"把 token 注入 agent 进程"的 3 层链路（task 行 → env_extra → GIT_ASKPASS）。路径 B 仓库 GIT_ASKPASS 不 fallback 到 USER_*_TOKEN → git clone 失败（实际 bug）。

- R20.24.1：token 注入链路 3 层全覆盖：task 行 → env_extra → GIT_ASKPASS
- R20.24.2：BOT_*_TOKEN 短路为空时 GIT_ASKPASS fallback 到 USER_*_TOKEN
- R20.24.3：GIT_ASKPASS fallback 链修正：`${BOT_*_TOKEN:-${USER_*_TOKEN:-${BOT_TOKEN:-$OTHER}}}`
- R20.24.4：agent 同时收到 bot_token + user_token，分别注入 BOT_*_TOKEN 和 USER_*_TOKEN
- R20.24.5：env_extra 注入逻辑不经过 TokenResolver，TokenResolver 只影响 task 行写入值
- R20.24.6：路径 B BOT_*_TOKEN=空时不报错，git 操作 fallback USER_*_TOKEN

### R20.25 token 脱敏全覆盖（设计补充）

**问题**：result_text 脱敏只 replace repo_gitcode_token，不脱 user_token → token 泄露到 dashboard。

- R20.25.1：result_text 脱敏覆盖所有注入 token 值（repo_gitcode_token + user_token）
- R20.25.2：agent_logs 脱敏同步覆盖
- R20.25.3：fail_task 评论内容不含 token 原文
- R20.25.4：脱敏列表从 task 行 + runtime 动态构建
- R20.25.5：扩展 R19.1.3 脱敏范围到 result_text / agent_logs 所有 token

### R20.26 关联仓库 token 独立获取（设计补充）

**问题**：关联仓库用主仓库 token，但可能跨 VCS 平台或不同 owner → token 不匹配。

- R20.26.1：关联仓库 token 独立获取——各自 Repo(repo_key).bot_token
- R20.26.2：ensure_repo_base_multi 改为 token_map: dict[repo_key, str]
- R20.26.3：_submit_direct_linked_prs 遍历时独立获取每个仓库 token
- R20.26.4：关联仓库 token 短路时跳过 + warning，不影响主仓库
- R20.26.5：跨平台由 Repo.bot_token 内部 provider 分流覆盖

### R20.27 OAuth refresh 分布式锁（设计补充）

**问题**：双主集群并发刷新同一用户 OAuth token → 竞态 → 旧 refresh_token 被覆盖。

- R20.27.1：OAuth refresh 加分布式锁 `oauth_refresh:{user_id}:{provider}`
- R20.27.2：refresh 前先读最新 token_expires_at，已刷新则跳过
- R20.27.3：刷新失败不清除旧 token
- R20.27.4：GitHub OAuth refresh 同样加分布式锁
- R20.27.5：分布式锁超时 30s

### R20.28 飞书 direct 模式 user_token 获取（设计补充）

**问题**：飞书 direct 模式 user_token=空，agent 无用户身份 → assign 等 VCS 操作 403。

- R20.28.1：飞书 direct 模式启动前调 get_user_token_with_refresh 获取用户 token
- R20.28.2：用户 token 注入 USER_*_TOKEN 环境变量
- R20.28.3：token 过期时返回友好提示
- R20.28.4：飞书 queue 模式同理

### R20.29 redispatch 时 token 刷新（设计补充）

**问题**：_redispatch_vod_after_recommend 传旧 task 的 repo_gitcode_token，冗余且可能过期。

- R20.29.1：redispatch 创建新 task 时 _get_task_repo_token 重新获取（已有逻辑）
- R20.29.2：_redispatch_vod_after_recommend 移除 repo_gitcode_token 参数（冗余）
- R20.29.3：events redispatch 调 Repo.bot_token 获取新 token（R20.7.5 已覆盖）
- R20.29.4：redispatch 时 token 短路 → 新 task 行空 → agent 运行时实时获取 → 也短路则 NO_TOKEN_AVAILABLE

## 十补、R21 修订章（矛盾修复与遗漏补充）

### R21.1 全局 bot token 更换场景（A1 级矛盾修复）

**问题**：管理员把全局 bot token 从 bot_old 换成 bot_new（有→有）时，auto_upgrade 不触发，新 bot 未被邀请 → 403，webhook password 不匹配 → 事件断链。

- R21.1.1：save_config 检测全局 bot token 值变更（old != new 且 old 非空）→ 触发 global_bot_token_swap
- R21.1.2：global_bot_token_swap 流程：验证新 token → 获取 bot username → 遍历路径 A 仓库 → 邀新 bot + 重建 webhook + 更新 webhook_token + best-effort 移除旧 bot
- R21.1.3：后台线程执行，不阻塞 save_config
- R21.1.4：失败时仓库保持旧状态，记录 warning
- R21.1.5：清除 per-provider 过期标记（R21.2）
- R21.1.6：GitHub 同理（provider 分流）
- R21.1.7：分布式锁 `global_bot_swap`，防多实例同时执行

### R21.2 per-provider 过期标记 key（A2 级矛盾修复）

**问题**：global_bot_token_status 单 key，GitCode 过期会短路 GitHub。

- R21.2.1：改为 per-provider key：global_gitcode_bot_token_status / global_github_bot_token_status
- R21.2.2：短路按 provider 查对应 key
- R21.2.3：fail_task 按 provider 标记对应 key
- R21.2.4：健康检查按 provider 验证 + 标记
- R21.2.5：R16.5.2 修正为两个 per-provider key
- R21.2.6：R16.5.4 修正为写对应 provider key
- R21.2.7：GlobalBotToken 策略按 provider 操作对应 key
- R21.2.8：GlobalBotTokenHandler 同理
- R21.2.9：向前兼容——旧 key 迁移拆分

### R21.3 健康检查/verify 超时误标记修复（A3 级矛盾修复）

**问题**：超时视为失败 + FAILURE_THRESHOLD=1 + "必须先验证"三者矛盾。一次网络抖动 → 全局不可用 1h。

- R21.3.1：verify_token 401 → 标记过期
- R21.3.2：verify_token 超时/5xx → 不标记，记 warning
- R21.3.3：R18.5.10 修正：超时不标记过期，原设计取消
- R21.3.4：FAILURE_THRESHOLD=1 只对 401 生效
- R21.3.5：detect_and_mark_expired 区分 401 / 超时 / 5xx
- R21.3.6：最坏情况分析：verify 超时 → 不标记 → task 用原 token → 如果真过期 → 401 → 再次 verify → 网络恢复后正确判断
- R21.3.7：健康检查同理——401 才标记

### R21.4 OAuth refresh 竞态补充（B1 遗漏）

**问题**：R20.27 分布式锁不能完全解决 refresh_token rotation 竞态。

- R21.4.1：锁内 refresh 前先读 token_expires_at，已更新则跳过
- R21.4.2：refresh 失败（rotation）→ 读最新 refresh_token 重试一次
- R21.4.3：重试仍失败 → status=4 → 用户重新绑定
- R21.4.4：双层锁保护（进程内 + 分布式）

### R21.5 PR 维度自循环检测（B2 遗漏）

**问题**：check_running_task 缺 PR 维度定义。

- R21.5.1：PR 类事件 issue_id 存 PR 号或关联 Issue 号
- R21.5.2：note_hook_pr 先按 issue_id 查，查不到按 repo_key + event_type 查
- R21.5.3：查询逻辑不变（issue_id 字段复用）
- R21.5.4：PR 无关联 Issue → 降级 session 兜底

### R21.6 非场景 task 行 issue_id 语义（B3 遗漏）

**问题**：飞书 VOD/Chat direct/autopilot/event dialogue 无 issue 上下文。

- R21.6.1-R21.6.4：各场景 issue_id 填关联 Issue 号，无关联则填空 → 降级 session 兜底
- R21.6.5：issue_id 空时条件 4 agent_tasks 查询不命中 → session 兜底
- R21.6.6：session 兜底是 repo 级，存在误拦风险（见 R21.7）

### R21.7 session 兜底误拦分析（B4 遗漏）

**问题**：session 兜底 repo 级查询，agent 完成后误拦最多 5 分钟。

- R21.7.1：agent 完成后更新 session status='idle'，缩小误拦窗口
- R21.7.2：兜底窗口从 300s 缩短到 60s
- R21.7.3：误拦场景用例：agent 完成 → 30s 后用户评论另一 issue → 误拦 → 60s 后放行
- R21.7.4：session status='idle' 后无误拦

### R21.8「有仓库权限的用户」鉴权规则（B5 遗漏）

- R21.8.1：定义：托管人 + 仓库协作者 + admin
- R21.8.2：PUT /repos/bot-token 只有托管人/admin 可配
- R21.8.3：settings 页 token 状态：托管人 + admin 可见
- R21.8.4：grape 侧权限独立于 VCS 侧
- R21.8.5：组织仓库：组织管理员视为有权限

### R21.9 GitHub 路径 B auto_upgrade（B6 遗漏）

- R21.9.1：auto_upgrade 按 provider 分流
- R21.9.2：GitHub 路径 B 判断：_is_github_user_token_repo
- R21.9.3：GitHub 流程与 GitCode 对称
- R21.9.4：GitHub 协作者邀请用 add_github_collaborator
- R21.9.5：GitHub webhook 用 setup_github_webhook
- R21.9.6：双 provider 并行执行

### R21.10 token 存储安全与测试需求（B7 遗漏）

- R21.10.1：token 存储安全——用户 token 加密（credential_crypto），bot token 明文（与现有一致），新列不含 token 原文
- R21.10.2：webhook_secret 脱敏——R19.1 扩展到 webhook_secret
- R21.10.3：A2A 比对日志已安全（只输出 True/False）
- R21.10.4：测试验收清单——单元/集成/E2E 共 8 项

### R21.11 其他轻量修正（C 级）

- R21.11.1：场景 13.2 评论权限——best-effort 403 不重试，降级 dashboard 通知
- R21.11.2：自循环检测性能——host_vcs_username 走缓存列，check_running_task 走索引
- R21.11.3：R20.19.4 误报声明——"could not read Username" 对私有仓库也会出现，verify 通过则不标记
- R21.11.4：DB 迁移降级——列不存在则短路检查跳过，不影响主链路

### R21.12 task 排队期间 token 过期窗口（设计补充）

**问题**：路径 A 仓库 task 创建时写入 bot token 到 task 行，排队期间 token 过期，claim 时不检查过期标记直接用旧 token → 401。

- R21.12.1：claim 时对路径 A 仓库 task 行 token 做新鲜度检查——查 DB 过期标记（meta / managed_repos）
- R21.12.2：已过期 → 重新调 TokenResolver.get_token → 短路返回空则注入空串 → fail_task NO_TOKEN_AVAILABLE
- R21.12.3：重新获取返回非空（其他策略有可用 token）→ 注入新 token
- R21.12.4：路径 B 不受影响（task 行本来就为空，走现有实时获取）
- R21.12.5：只对路径 A 生效
- R21.12.6：不调 VCS API，只查 DB 过期标记，无延迟
- R21.12.7：在现有 `if not task.get("repo_gitcode_token")` 逻辑之前执行
