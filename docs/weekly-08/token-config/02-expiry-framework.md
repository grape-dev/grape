# 统一 Token 过期框架

> 源文档：`repo-token-config-design.md` 第二章（18-135 行）+ R16.1-R16.6（264-310 行）+ R21.2（993-1012 行）
> 需求文档：`repo-token-config-requirements.md` R16.1-R16.6 + 设计补充项 + R21.2

---

## 需求项

### R16.1 统一 token 过期检测（fail_task 统一入口）

fail_task 的 401 检测不再只针对路径 B，按 token 来源统一分流处理。

- **R16.1.1**：fail_task 401 检测判断 token 来源（用户 / 全局 / 仓库级 / devops_agent_session 四级）
- **R16.1.2**：用户 token 过期 → 现有 R10 逻辑（标记 `external_identities.status=4`）
- **R16.1.3**：全局 bot token 过期 → 按 provider 验证全局 token 有效性（GitCode: `GET /api/v5/user`，GitHub: `GET /user`，复用 R15.1.1 逻辑），无效则标记 `meta.global_bot_token_status=expired`。**R21.2.3 修正**：按 provider 标记对应 key——GitHub 仓库 401 标记 `global_github_bot_token_status`，GitCode 仓库 401 标记 `global_gitcode_bot_token_status`
- **R16.1.4**：仓库级 bot token 过期 → 按 provider 验证仓库级 token 有效性，无效则标记 `managed_repos.bot_token_status=expired`（新增列）
- **R16.1.5**：task errors 统一 append 模式写入 error_code（`TOKEN_EXPIRED` / `GLOBAL_TOKEN_EXPIRED` / `REPO_TOKEN_EXPIRED`）
- **R16.1.6**：幂等规则——已标记过期的 token 不重复标记、不重复写 error_code（避免每次 401 重试都 append）（设计补充）
- **R16.1.7**：第 1 级 `devops_agent_session` token 过期 → 标记 `devops_agent_session.token_status=expired`，短路返回空串。`devops_agent_session` 是 agent 专属配置（非用户直接管理），过期后由 agent 管理员重新配置；如未实现独立过期标记，则降级为不标记但记录日志，task 获取 token 时第 1 级查到过期 token 直接返回（由调用方 401 兜底）（设计补充）

### R16.2 统一过期通知

- **R16.2.1**：task errors 统一 error_code 格式，所有用户在 task 详情中可见
- **R16.2.2**：dashboard / task 详情显示 error_code 映射的本地化文案
- **R16.2.3**：settings 页返回全局 / 仓库级 bot token 状态
- **R16.2.4**：issue 回帖 best-effort（有可用 token 时发评论）

### R16.3 统一过期恢复

- **R16.3.1**：用户重新绑定 token → 恢复 `external_identities.status=1`（已有）
- **R16.3.2**：管理员 `save_config` 更新全局 bot token → 清除 meta 过期标记
- **R16.3.3**：有仓库权限的用户 `PUT /repos/bot-token` 更新仓库级 token → 清除 `managed_repos` 过期标记
- **R16.3.4**：新 token 写入前验证有效性（R15.1 逻辑）

### R16.4 短路机制

- **R16.4.1**：全局 bot token 标记过期后，`_get_global_bot_token` 短路返回空串。短路检查在 60s `_cached_get_meta` 缓存之后执行：先走缓存取 token，再查 `meta.global_bot_token_status`，如果 expired 则返回空串。集群下其他实例最多 60s 延迟感知过期，由 fail_task 401 兜底覆盖延迟窗口（用例 6.1）。**R21.2.2 修正**：按 provider 查对应 key——GitCode 查 `global_gitcode_bot_token_status`，GitHub 查 `global_github_bot_token_status`
- **R16.4.2**：仓库级 bot token 标记过期后，`get_bot_token_by_repo` 短路返回空串
- **R16.4.3**：用户 token status=4 保持现有行为（返回 token 供 best-effort 发评论）
- **R16.4.4**：`devops_agent_session` token 标记过期后（R16.1.7），第 1 级策略短路返回空串，降级到第 2 级仓库级 token。短路检查需 try-except 保护：如果 `devops_agent_session` 表无 `token_status` 列（未 ALTER），降级为不检查（返回 token，由调用方 401 兜底）（设计补充）
- **R16.4.5**：短路机制向前兼容——所有短路检查新增在现有逻辑之后，不修改原有 token 获取路径。未标记过期的 token 走原有逻辑完全不变（设计补充）

### R16.5 DB schema 变更

- **R16.5.1**：`managed_repos` 新增 `bot_token_status` 列（`TEXT DEFAULT ''`，空=正常，`expired`=过期），幂等 ALTER
- **R16.5.2**：meta 表新增 `global_bot_token_status` 键（空=正常 / 未检测，`expired`=过期）。**R21.2.5 修正**：改为 per-provider key——`global_gitcode_bot_token_status` 和 `global_github_bot_token_status` 两个 key（替代单个 `global_bot_token_status`）
- **R16.5.3**：`devops_agent_session` 新增 `token_status` 列（`TEXT DEFAULT ''`，幂等 `ALTER TABLE devops_agent_session ADD COLUMN token_status TEXT DEFAULT ''`，try-except 保护）。同时新增 `repo_github_token` 列（`TEXT DEFAULT ''`，幂等 `ALTER TABLE devops_agent_session ADD COLUMN repo_github_token TEXT DEFAULT ''`）——`GitHubProvider.session_token_field()` 返回此列名，现有表只有 `repo_gitcode_token`（设计补充）
- **R16.5.4**：健康检查同时写两个 meta key——现有 `bot_token_health`（admin 页展示用，保留 `healthy` / `degraded` / `unhealthy` 三级状态）+ 新增 `global_bot_token_status`（短路机制用，`expired`=过期）。两个 key 独立，现有 admin 页读 `bot_token_health` 不受影响。**R21.2.6 修正**：写对应 provider 的 `global_*_bot_token_status` key（而非单个 `global_bot_token_status`）（设计补充）
- **R16.5.5**：`managed_repos` 新增 `host_vcs_username` 列（`TEXT DEFAULT ''`，幂等 ALTER）——托管时从 `external_identities` 同步写入 host 用户的 VCS 平台用户名（login 字段），供路径 B 自循环检测使用（R20.1.4）。每次 `verify_token` 时如果返回的 login 与 DB 不一致则更新（R20.1.5）（设计补充）

### R16.6 无 token 检测

- **R16.6.1**：task 获取不到 token 时 fail_task 写入 `NO_TOKEN_AVAILABLE` error_code
- **R16.6.2**：dashboard 显示"无可用 Token，请联系管理员配置"

---

## 设计方案

### 2.1 设计原则

一个检测入口，按 token 来源分流，标记对应存储，统一 error_code 格式。

### 2.2 检测入口分流

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
```

**改造方式**：在现有 `if _hid > 0 and _expired_provider:` 分支后新增 else 分支，不修改原有路径 B 逻辑（R10 检测保持不变）。

### 2.3 统一 error_code 表

| token 来源 | error_code | message | 通知角色 |
|-----------|-----------|---------|---------|
| 用户 token | `TOKEN_EXPIRED` | user token expired or revoked | 用户（account 页提示重新绑定） |
| 全局 bot token | `GLOBAL_TOKEN_EXPIRED` | global bot token expired or revoked | 所有受影响用户 + 管理员 |
| 仓库级 bot token | `REPO_TOKEN_EXPIRED` | repo bot token expired or revoked | 该仓库用户 + 管理员 |
| 无可用 token | `NO_TOKEN_AVAILABLE` | no token available for this repo | 该仓库用户 + 管理员（dashboard 显示"无可用 Token，请联系管理员配置"） |

所有 error_code 走 append 模式写入 task errors（R14.18），保留原始错误详情。

**通知角色说明**：
- 用户 token 过期：只有该用户受影响，account 页提示重新绑定即可恢复
- 全局 bot token 过期：所有路径 A 仓库的用户都受影响（task 全部 401 失败）。用户在 dashboard / task 详情看到"全局 Bot Token 已过期，请联系管理员重新配置"提示；管理员在 settings 页看到全局 bot token 状态为"已过期"
- 仓库级 bot token 过期：只有该仓库的用户受影响。用户在 dashboard / task 详情看到"仓库 Bot Token 已过期，请联系有权限的成员重新配置"提示；有仓库权限的用户在 settings 页看到该仓库 bot token 状态为"已过期"

### 2.4 统一标记位置（per-provider）

| token 来源 | 标记方式 | 清除方式 |
|-----------|---------|---------|
| 用户 token | `external_identities.status=4` | 用户重新绑定（`create/update_external_identity` 恢复 `status=1`） |
| 全局 bot token（GitCode） | `meta.global_gitcode_bot_token_status=expired`（R21.2 修正） | `save_config` 更新 GitCode bot token 时清除 |
| 全局 bot token（GitHub） | `meta.global_github_bot_token_status=expired`（R21.2 修正） | `save_config` 更新 GitHub bot token 时清除 |
| 仓库级 bot token | `managed_repos.bot_token_status=expired`（新列） | `PUT /repos/bot-token` 写入新 token 时清除 |
| devops_agent_session token | `devops_agent_session.token_status=expired`（新列） | agent 管理员重新配置 |

> **R21.2 per-provider key 修正**：原 R16.5.2 / R16.4.1 的过期标记只有一个 `global_bot_token_status`，GitCode 全局 token 过期会把 GitHub 全局 token 一起短路。改为 per-provider key 后各 provider 独立标记 / 独立短路（R21.2.1-R21.2.9）。

### 2.5 统一通知

#### 后端 API 返回

- `/auth/me`：返回 `token_expired_providers`（用户 token 过期，已有）
- `/settings/credentials` 或 `/settings/bot-status`：返回全局 / 仓库级 bot token 状态（管理员和有仓库权限的用户可见）
- task errors：统一 error_code 格式，所有用户在 task 详情中可见
- dashboard API：返回仓库 token 状态标记（全局过期时所有路径 A 仓库标记受影响）

#### 前端展示

- account 页：用户 token 过期提示（已有）
- settings 页：全局 / 仓库级 bot token 过期提示（管理员 / 有仓库权限的用户可见）
- dashboard：task failed 时显示 error_code 映射的本地化文案
  - `TOKEN_EXPIRED` → "你的 Token 已过期，请在设置页重新绑定"
  - `GLOBAL_TOKEN_EXPIRED` → "全局 Bot Token 已过期，请联系管理员重新配置"
  - `REPO_TOKEN_EXPIRED` → "仓库 Bot Token 已过期，请联系有权限的成员重新配置"

#### issue 回帖（best-effort）

- 路径 B 仓库：用全局 bot token 发评论（可能 403）
- 路径 A 仓库全局 token 过期 + 无仓库级 token：无可用 token 发评论 → 只靠 dashboard / settings 通知
- 路径 A 仓库全局 token 过期 + 有仓库级 token（未过期）：用仓库级 token 发评论
- 路径 A 仓库级 token 过期 + 全局 token 未过期：用全局 bot token 发评论
- 路径 A 仓库级 token 过期 + 全局 token 也过期：无可用 token 发评论 → 只靠 dashboard / settings 通知

### 2.6 短路机制

token 被标记过期后，`get_repo_token_with_refresh` 等函数检查过期标记，短路返回空串，避免每次都 401 重试：

- 用户 token `status=4` → `_get_oauth_token_from_identity` 已返回 token（R10 设计意图，发评论 best-effort）
- 全局 bot token meta 标记过期 → `_get_global_bot_token` 检查对应 provider 的 per-provider key（`global_gitcode_bot_token_status` / `global_github_bot_token_status`），过期返回空串（R21.2.2 修正）
- 仓库级 bot token `managed_repos` 标记过期 → `get_bot_token_by_repo` 检查标记，过期返回空串
- `devops_agent_session` token 标记过期 → 第 1 级策略短路返回空串，降级到第 2 级（R16.4.4）

**短路检查位置**：在现有 token 获取逻辑之后新增检查，不修改原有路径（R16.4.5 向前兼容）。

### 2.7 健康检查 → 短路 → 无 token → fail_task 协调流程

#### 健康检查发现 token 过期

```
健康检查发现 token 过期（R17.2）
  → 标记过期（meta per-provider key / managed_repos）
  → 短路机制生效（R16.4）：_get_global_bot_token / get_bot_token_by_repo 返回空串
  → task 执行时 TokenResolver.get_token 所有策略返回空 → 返回空串
  → task fail（不是 401，是"无 token"）
  → fail_task 的 401 检测不触发（因为不是 401 错误）
  → fail_task 检测无 token → 写入 NO_TOKEN_AVAILABLE error_code（R16.6）
  → dashboard 显示"无可用 Token，请联系管理员配置"
```

#### 健康检查发现 token 恢复有效

```
健康检查发现 token 恢复有效（200）（R17.2.3 / R17.2.6）
  → 自动清除过期标记
  → 短路自动解除（R16.4 感知标记清除后重新返回 token）
  → task 正常执行
```

#### 管理员 / 用户手动重新配置 token

```
save_config 更新全局 bot token（R16.3.2）
  → 清除 meta.global_*_bot_token_status 过期标记（per-provider，R21.2）
  → 下次健康检查验证新 token → 200 → 确认标记已清除
  → 短路解除 → task 恢复

PUT /repos/bot-token 更新仓库级 token（R16.3.3）
  → 清除 managed_repos.bot_token_status 过期标记（R15 步骤 6）
  → 短路解除 → task 恢复
```

---

## 涉及代码文件

| 文件 | 改动内容 |
|------|---------|
| `src/user/db.py` | fail_task 401 检测分流（`_detect_token_expiry`）、`_mark_global_bot_token_expired`（per-provider key）、`_mark_repo_bot_token_expired`、`_mark_session_token_expired`、幂等检查；`_get_global_bot_token` 短路（per-provider key 查询）、`get_bot_token_by_repo` 短路、`get_repo_token_with_refresh` 第 1 级短路；DB schema ALTER（`managed_repos.bot_token_status`、`managed_repos.host_vcs_username`、`devops_agent_session.token_status` + `repo_github_token`）；`_update_repo_bot_token` 清除 `bot_token_status`；`update_managed_repo_bot_token` / `set_repo_bot_token` |
| `src/runtime/service.py` | fail_task 调用 `_detect_token_expiry`，无 token 检测写 `NO_TOKEN_AVAILABLE` |
| `src/admin/config_store.py` | `save_config` 更新全局 bot token 时清除 per-provider 过期标记（`global_gitcode_bot_token_status` / `global_github_bot_token_status`） |
| `src/api/routes/repos.py` | `PUT /repos/bot-token` 更新仓库级 token 时清除 `bot_token_status` 过期标记 |
| `src/repo.py` | `managed_repos` / `devops_agent_session` 表定义与 ALTER 语句 |
| `src/api/static/js/pages/account.js` | 用户 token 过期提示展示（已有，扩展） |
| 前端 dashboard / settings 页 | error_code 本地化文案映射、全局 / 仓库级 bot token 状态展示 |
| GlobalBotToken 策略 + GlobalBotTokenHandler（新建） | `_is_expired` / `mark_expired` / `clear_expired` 按 provider 操作对应 key（R21.2.7 / R21.2.8） |

---

## 验收标准

1. **R16.1 统一检测**：fail_task 401 时按四级 token 来源分流标记，用户 / 全局 / 仓库级 / session 各自标记到对应存储
2. **R16.1.3 per-provider 标记**：GitCode 仓库 401 标记 `global_gitcode_bot_token_status`，GitHub 仓库 401 标记 `global_github_bot_token_status`，互不影响（R21.2.3）
3. **R16.1.5 error_code**：task errors append `TOKEN_EXPIRED` / `GLOBAL_TOKEN_EXPIRED` / `REPO_TOKEN_EXPIRED` / `NO_TOKEN_AVAILABLE`
4. **R16.1.6 幂等**：已标记过期的 token 再次 401 时不重复标记、不重复 append error_code
5. **R16.2 通知**：dashboard / task 详情 / settings 页 / account 页按角色展示对应本地化文案
6. **R16.3 恢复**：用户重新绑定 / `save_config` / `PUT /repos/bot-token` 分别清除对应过期标记
7. **R16.4 短路**：全局（per-provider key）、仓库级、session token 标记过期后对应获取函数返回空串；用户 token status=4 仍返回 token（best-effort）
8. **R16.4.4 try-except**：`devops_agent_session` 表无 `token_status` 列时降级为不检查，不报错
9. **R16.4.5 向前兼容**：未标记过期的 token 走原有逻辑完全不变
10. **R16.5 schema**：`managed_repos.bot_token_status` + `host_vcs_username`、`devops_agent_session.token_status` + `repo_github_token`、meta per-provider key 全部幂等 ALTER
11. **R16.6 无 token**：所有策略返回空时 fail_task 写 `NO_TOKEN_AVAILABLE`，dashboard 显示"无可用 Token"
12. **R21.2 per-provider key**：`global_gitcode_bot_token_status` / `global_github_bot_token_status` 两个 key，GitCode 过期不短路 GitHub（R21.2.1-R21.2.9）
13. **R21.2.9 迁移**：旧 `global_bot_token_status` key 迁移拆分为两个 per-provider key
14. **协调流程**：健康检查标记 → 短路 → 无 token → fail_task 写 `NO_TOKEN_AVAILABLE` → dashboard 提示，全链路端到端验证

---

## 依赖场景

| 依赖项 | 关系 |
|--------|------|
| **04-token-resolver**（R18） | 过期框架依赖策略链短路机制——`TokenStrategy.get_token` 检查过期标记后短路返回空串；`GlobalBotToken` 策略的 `_is_expired` / `mark_expired` / `clear_expired` 按 provider 操作对应 key（R21.2.7） |
| **03-health-check**（R17） | 健康检查主动发现过期后标记 meta / managed_repos，复用 R16.4 短路 + R16.2 通知 + R16.6 无 token 检测 |
| **01-repo-token-config**（R15） | R16.3.3 恢复逻辑依赖 `PUT /repos/bot-token` 清除 `bot_token_status`；R15 步骤 6 写 DB 时清除过期标记 |
| **10-global-token-swap**（R21.1-R21.3） | R21.2 per-provider key 是 R16.5.2 / R16.4.1 / R16.1.3 / R17.2.2 的修正项；R21.1.5 清除 per-provider 过期标记 |
| **05-path-b-self-loop**（R20.1） | R16.5.5 `host_vcs_username` 列供路径 B 自循环检测使用（R20.1.4） |
| **12-i18n-frontend**（R19） | error_code 本地化文案映射依赖前端 i18n 体系 |
| **R14.18** | task errors append 模式（error_code 写入机制基础） |
| **R10** | 用户 token 过期检测（路径 B），R16.1 在 R10 基础上扩展 else 分支 |
