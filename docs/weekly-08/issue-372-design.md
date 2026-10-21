# Issue #372 技术设计：用户级 Token 驱动全链路

> 状态：方案设计
> 基线：weekly-08（00835d2e0）
> 分支：feat/issue-372-user-token-driven
> 关联：#343 双轨 OAuth token 架构、#345 bot token 单点故障、#356 配置向导
> 需求文档：docs/weekly-08/issue-372-user-token-only-requirements.md

---

## 一、问题

### 1.1 现状

grape 平台有三类 token：全局 Bot Token (PAT)、仓库级 Bot Token（=全局副本）、用户级 Token (OAuth)。全局 Bot Token 是单点依赖——托管仓库时硬校验 `if not global_bot_token: raise 503`，没有就不能用系统。

### 1.2 目标

全局 Bot Token 优先，用户 Token 降级。已配置全局 Bot 的环境零影响；未配置的环境用户只需一个 GitCode PAT 即可跑通全链路。

---

## 二、设计方案

### 2.1 核心策略：路径 A / 路径 B 双路径

```
host_repo 入口
  ├─ get_global_gitcode_bot_token() 有值 → 路径 A（原流程，零改动）
  │   ├─ 用户 token 邀请 bot 为维护者
  │   ├─ 全局 bot token 接受邀请 + 自查权限
  │   ├─ 全局 bot token 建 webhook（password=bot token）
  │   └─ managed_repos: bot_gitcode_token=全局, host_user_id=0
  │
  └─ get_global_gitcode_bot_token() 为空 → 路径 B（新增降级路径）
      ├─ 跳过 bot 协作者邀请/接受/自查
      ├─ 用户 token 建 webhook（password=随机密钥 webhook_secret）
      └─ managed_repos: bot_gitcode_token='', host_user_id=当前用户, webhook_secret=随机密钥
```

### 2.2 Token 获取链路：get_repo_token_with_refresh

统一入口，全局 Bot 优先，用户 Token 降级。必须保留原 get_gitcode_token 的全部四级 fallback：

```python
def get_repo_token_with_refresh(repo_key: str) -> str:
    # 1. devops_agent_session.repo_gitcode_token（agent 专属配置）
    tok = _get_devops_agent_session_token(repo_key)
    if tok:
        return tok  # 不刷新
    # 2. managed_repos.bot_gitcode_token（仓库级 Bot Token，路径 A 托管写入）
    tok = get_bot_token_by_repo(owner, repo)
    if tok:
        return tok  # 不刷新
    # 3. 全局 Bot Token
    tok = _get_global_bot_token()
    if tok:
        return tok  # 不刷新
    # 4. 用户 Token 降级（路径 B）
    host_user_id = get_host_user_id(repo_key)
    if host_user_id > 0:
        username = get_grape_account_by_id(host_user_id)["username"]
        return get_user_token_with_refresh(username, host_user_id)  # 带刷新
    return ""
```

关键：前三级是 bot token（不过期不刷新），优先命中直接返回。只有三级全空时才降级到用户 token（带刷新）。

原 get_gitcode_token / get_agent_bot_token 内部改为调 get_repo_token_with_refresh，所有未显式替换的调用方（vcs/api_client.py、vcs/providers/gitcode.py、autopilot.py、repos.py:1423、scheduler.py 共 5 处）自动走降级链路。

get_repo_bot_token 保留原语义（跳过 devops_agent_session，#343 设计意图），不委托 get_repo_token_with_refresh。fallback 链：managed_repos.bot_gitcode_token → 全局 bot token → host_user_id 用户 token 降级。

### 2.3a task 创建时区分 token 来源

event_pipeline 中 task 创建（_create_direct_task / _create_failed_task / _enqueue_pipeline_task）调 get_repo_token_with_refresh 获取 token 写入 task 行 repo_gitcode_token。

问题：路径 B 场景返回的是用户 OAuth token（会过期 ~15 分钟），排队较久的 task 拿到过期 token，_get_task_token 因 task 行非空不触发实时刷新。

解决：新增辅助函数 `_is_user_token_repo(repo_key)` 判断当前仓库是否走路径 D（用户 token）。task 创建时：
```python
tok = get_repo_token_with_refresh(repo_key) or ""
if tok and not _is_user_token_repo(repo_key):
    repo_gitcode_token = tok  # bot token，安全写入
else:
    repo_gitcode_token = ""   # 用户 token 或空，运行时实时获取
```

`_is_user_token_repo` 逻辑：前三级（devops_agent_session / managed_repos.bot_gitcode_token / 全局 bot）全为空且 host_user_id > 0 → True。

### 2.3 PAT 直接绑定（复用已有端点）

复用已有 `PUT /api/settings/credentials` 端点（gitcode_token 分支），不新增端点。该端点已具备 PAT 验证（调 GitCode /user API）、external_identities 写入、转移绑定检查（409 + binding_ticket）、覆盖更新。前端 `_accountDoManualBind` 已调此端点。

get_user_token_with_refresh 兼容 PAT：无 refresh_token 时直接返回 access_token，不检查过期、不触发刷新。

### 2.4 Webhook 验证解耦

路径 B webhook password 用随机密钥（`secrets.token_urlsafe(32)`），存入 `managed_repos.webhook_secret`。与 token 解耦，避免 token 刷新导致 webhook 验证失败。

`get_webhook_token_by_repo` 优先读 `webhook_secret`，为空时 fallback 到原 `gitcode_webhook_token`（路径 A 兼容）。

### 2.5 自循环检测调整

当前顺序：1. bot 用户名检测 → 2. grape-auto-comment 标记检测

改为：1. grape-auto-comment 标记检测（优先） → 2. bot 用户名检测（兼容）

路径 A：actor=bot 用户名，第 2 层拦截。
路径 B：actor=用户名，但评论带标记，第 1 层拦截。

**A2 裁决声明（自循环检测分层关系）**：

本节 R4 标记优先方案（已实现）是第一道防线。支线文档（repo-token-config-design.md R20.1）的四条件组合判断是标记失效时的兜底。

为什么需要兜底：prompt_builder.py 强制 agent "Access ALL VCS operations ONLY through the grape CLI... NEVER call VCS APIs with raw tokens"（line 241/359），所有系统端点（create_issue_comment / update_issue / create_pull / _post_result_to_issue 等 10+ 处）已追加 grape-auto-comment 标记。但 agent 是 LLM，可能不遵守 prompt 约束，用 BOT_*_TOKEN 环境变量直接 curl 调 VCS API → 评论无标记 → R4 标记检测失效 → 需要四条件兜底。

分层关系（与支线 R20.21 pipeline 步骤一致）：
```
2.0b 开发者指令检测（/done /cancel /resume，优先于一切）
2.0a 标记检测（grape-auto-comment，主线 R4 已实现）→ 命中则跳过
2.0  自循环检测
     路径 A：actor in bot_usernames → 跳过（主线已有）
     路径 B：四条件组合判断（支线 R20.1，未实现）→ 命中则跳过
2.0a2 issue_updated description 标记检测（主线已有）
```

当前状态：2.0a 标记检测已实现且有效（所有系统端点已加标记）。路径 B 四条件兜底未实现——在 agent 严格遵守 grape CLI 约束时不会触发，但作为防御性措施应在后续迭代中实现。

### 2.6 A2A 鉴权 trust domain 新增 host_user_id

当前 `_authorize_a2a` 的关联仓库信任域判断：两个仓库的 `bot_gitcode_token` 相同 → 信任。

路径 B 场景下两仓 bot_gitcode_token 都为空，此判断失效。新增分支：两个仓库的 `host_user_id` 相同且非 0 → 信任。

改动：`_authorize_a2a` 中 trust domain 检查新增 `host_user_id` 比对，与原 bot token 比对并列（任一满足即信任）。

**F2 安全确认（基线校准后验证）**：`host_user_id` 比对有 `if _session_host > 0` 守卫（webhook.py:327），排除双 0 情况。路径 A 仓库 host_user_id 全为 0 → `_session_host > 0` 为 False → 不进入信任域 → 路径 A 仓库信任域不变。不存在"所有路径 A 仓库互相进入信任域"的越权风险。

R5.1（bot token 通道保留）：当前代码已支持 bot token 和 session token 二选一，路径 B 自动走 session token 通道，无需改动。

### 2.7 repair_host 支持路径 B

当前 `repair_host` 逻辑：owner token 邀请 + bot token 建 webhook。

改为：与 host_repo 一致——检测有全局 bot token 走原路径 A 修复，无全局 bot token 走路径 B（用户 token 检查/重建 webhook + 随机密钥）。

**统一 provider 配置**：新增 `get_hosting_provider_config(provider)` 函数（repo_manager.py），返回 provider 所需的全部配置（bot_token / bot_username / find_webhook / setup_webhook / remove_webhook / webhook_url_suffix / db_token_field / db_bot_token_field）。repair_host 调此函数统一获取，消除散落的 if github else gitcode。

**关键细节（P1-3 修复）**：路径 B 场景 find_existing_webhook 找到已有 webhook 时，旧 webhook 可能用旧密钥（路径 A 的 bot token 或之前生成的随机密钥）。repair_host 生成新 _webhook_secret 存入 DB，但 webhook 实际验证用的是创建时的密钥——不匹配会导致验证失败。

解决：路径 B 场景找到已有 webhook 时，先删除旧 webhook 再用新密钥重建（确保 DB 密钥与 webhook 实际密钥一致）。路径 A 场景不受影响（webhook_password=bot token，不依赖随机密钥）。

**P1-4 修复**：repair_host 按 provider 正确分流——GitHub 仓库用 `get_global_github_bot_token()` + GitHub 专用 webhook 函数（find_existing_github_webhook / setup_github_webhook / remove_github_webhook）+ webhook_url 替换为 /webhook/github；GitCode 仓库用原逻辑。通过 `get_hosting_provider_config` 统一获取，不再误用 `get_global_bot_credential()`（返回 GitCode bot token）对 GitHub 仓库判断路径。

### 2.8 无邮箱配置场景适配

当前注册/忘记密码/改邮箱三个流程硬依赖邮箱验证码。无邮箱服务时全部走不通。

策略：`is_email_configured()` 返回 False 时前端隐藏邮箱 UI，后端跳过验证码。

- 注册：无邮箱时用户名+密码直接注册，后端 `/auth/register/verify` 跳过验证码校验，email 允许为空
- 忘记密码：无邮箱时前端不显示"忘记密码"链接
- 用户设置：无邮箱时 account.js 不显示邮箱管理卡片
- 绑定弹窗：无 OAuth App 时只显示手动令牌输入，不显示 OAuth 授权 tab
- 登录页：GitCode/GitHub 按钮始终显示，未配 OAuth App 时点击提示"请用用户名+密码注册后绑定 token"
- API：`GET /auth/providers` 追加 `email_configured` 字段供前端判断

向前兼容：有邮箱配置时走原流程不变。

---

## 三、改动点清单

### 3.1 数据库迁移（DDL）

managed_repos 新增两列：

```sql
ALTER TABLE managed_repos ADD COLUMN host_user_id INTEGER DEFAULT 0;
ALTER TABLE managed_repos ADD COLUMN webhook_secret TEXT DEFAULT '';
```

位置：`src/user/db.py` 迁移段（L965 之后，与 bot_gitcode_token 等列迁移并列）。
DDL 定义同步更新（L577 CREATE TABLE）。
`add_managed_repo` 签名新增 `host_user_id` 和 `webhook_secret` 参数。

### 3.2 新增函数

| 函数 | 文件 | 说明 |
|------|------|------|
| `get_repo_token_with_refresh(repo_key)` | src/user/db.py | 统一 token 获取入口（四级 fallback） |
| `get_host_user_id(repo_key)` | src/user/db.py | 查 managed_repos.host_user_id |
| `get_webhook_secret(repo_key)` | src/user/db.py | 查 managed_repos.webhook_secret |
| `_has_refresh_token(user_id, provider)` | src/auth/grape_account_db.py | 判断是否有 refresh_token |
| `_get_task_token(task)` | src/runtime/agent.py | task token 获取（优先 task 字段，空则实时获取） |
| `_is_user_token_repo(repo_key)` | src/user/db.py | 判断仓库是否走路径 D（用户 token），用于 task 创建时区分 token 来源 |
| `get_hosting_provider_config(provider)` | src/user/repo_manager.py | 统一 provider 配置（bot token/username/webhook 函数集/URL 后缀/DB 列名） |

### 3.3 修改函数

| 函数 | 文件 | 改动 |
|------|------|------|
| `host_repo` | src/api/routes/repos.py | 路径 B：无全局 bot 时用用户 token 建 webhook + 随机密钥 |
| `host_repo` GitHub 分支 | src/api/routes/repos.py | 路径 B：无全局 GitHub bot 时跳过 bot 协作者，webhook 用用户 token + 随机密钥 |
| `host_repo` GitHub webhook | src/api/routes/repos.py | setup_github_webhook 传 webhook_secret 参数 |
| `create_from_template` | src/api/routes/repos.py | 路径 B 支持 + webhook_url 硬校验改为不阻断创建 |
| `get_webhook_token_by_repo` | src/user/db.py | 优先读 webhook_secret |
| `get_user_token_with_refresh` | src/api/deps.py | 无 refresh_token 时跳过刷新 |
| `Repo.bot_token` | src/repo.py | GitCode 路径改调 get_repo_token_with_refresh |
| `add_managed_repo` | src/user/db.py | 签名新增 host_user_id + webhook_secret |
| `get_gitcode_token` | src/user/db.py | 内部改为调 get_repo_token_with_refresh（统一入口） |
| `get_repo_bot_token` | src/user/db.py | 保留原语义（跳过 devops_agent_session），末尾加用户 token 降级 |
| `get_github_token` | src/user/db.py | fallback 链末尾加 host_user_id → get_user_github_token_with_refresh |
| `get_agent_bot_token` | src/user/db.py | 同 get_gitcode_token |
| `repair_host` | src/api/routes/repos.py | 路径 B：无全局 bot 时用用户 token 修复 webhook |
| `_authorize_a2a` trust domain | src/api/routes/webhook.py | 新增 host_user_id 比对分支 |
| 注册流程 | src/api/routes/grape_auth.py + register.html | 无邮箱配置时跳过验证码，用户名+密码直接注册 |
| 忘记密码 | 登录页 + forgot.html | 无邮箱配置时隐藏"忘记密码"入口 |
| 用户设置 | account.js | 无邮箱配置时隐藏邮箱管理卡片 |
| public-config API | src/api/routes/grape_auth.py | 返回 email_configured 字段 |

### 3.4 替换调用点

| 文件 | 原调用 | 改为 |
|------|--------|------|
| src/api/event_pipeline.py (8处) | `get_gitcode_token(repo_key=...)` | `get_repo_token_with_refresh(repo_key)` |
| src/api/event_pipeline.py (2处) | `get_agent_bot_token(repo_key)` | `get_repo_token_with_refresh(repo_key)` |
| src/runtime/agent.py (6处) | `task.get("repo_gitcode_token") or ""` | `_get_task_token(task)` |

### 3.5 API 端点

不新增端点。PAT 绑定复用已有 `PUT /api/settings/credentials`（gitcode_token 分支）。

### 3.6 自循环检测顺序调整

| 文件 | 改动 |
|------|------|
| src/api/event_pipeline.py | 2.0a 标记检测移到 2.0 bot 用户名检测之前 |

### 3.7 A2A 鉴权 trust domain

| 文件 | 改动 |
|------|------|
| src/api/routes/webhook.py `_authorize_a2a` | trust domain 新增 host_user_id 比对分支 |
| src/api/routes/webhook.py `_authorize_a2a` | bot token 通道保留（验证确认，无需改动） |

### 3.8 repair_host 路径 B

| 文件 | 改动 |
|------|------|
| src/api/routes/repos.py `repair_host` | 新增路径 B 修复（与 host_repo 一致） |

---

## 四、向前兼容

### 4.1 已有全局 Bot Token 的环境

- `get_repo_token_with_refresh` 第 1/2 步命中 bot token → 直接返回，不触发用户 token 路径
- 托管走路径 A → 写入 bot_gitcode_token，host_user_id=0
- pipeline/agent 拿到的 token 与改造前完全一致

### 4.2 数据库迁移

- 新增两列 DEFAULT 0/''，现有记录不迁移，走降级路径
- add_managed_repo INSERT 语句 try/except 降级链兼容未迁移的库

### 4.3 get_gitcode_token / get_agent_bot_token

原函数保留不动，内部改为调 `get_repo_token_with_refresh`（统一入口），不影响已有调用方。

### 4.4 get_repo_bot_token 保留原语义

get_repo_bot_token 不委托 get_repo_token_with_refresh（后者包含 devops_agent_session 层，会破坏 #343 设计意图）。保留原 fallback：managed_repos.bot_gitcode_token → 全局 bot token，末尾加 host_user_id 用户 token 降级。有 bot token 时返回值与改造前完全一致。

### 4.5 非 OAuth 场景 token 过期主动提示（R10）

PAT 直接绑定的 token 无 refresh_token，被撤销或过期后系统无自动刷新能力。检测到 401 时：

1. **检测**：pipeline/agent 执行 GitCode API 返回 401 → 调 `_is_user_token_repo(repo_key)` 判断是否路径 D
2. **标记**：路径 D + 401 → `mark_identity_expired` 设置 status=4（token 过期，不清空凭据）
3. **error_code**：后端返回结构化 `error_code: "TOKEN_EXPIRED"`（前端映射 i18n，原始 message 进 console.error）
4. **提示**：Web UI 检测 `token_expired_providers` 显示"Token 已过期"卡片 + 重新绑定按钮；task failed 时 issue 回帖提示
5. **恢复**：用户重新绑定 → `create_external_identity` / `update_external_identity_tokens` 恢复 status=1 → pipeline 正常

error_code 定义：`TOKEN_EXPIRED`（PAT 过期/撤销）、`TOKEN_NOT_BOUND`（路径 B 未绑定 token）、`WEBHOOK_URL_NOT_CONFIGURED`（webhook 未配置）、`PERMISSION_DENIED`（无托管权限）、`UNKNOWN`（fallback）。前端通过 `app.errMsg(e)` 统一映射 `I18N.t('app.error.' + error_code)`，原始 message 进 console.error。locale 文件 en/zh app.json 新增 `error` namespace。

OAuth 场景（有 refresh_token）不进入此流程——token 过期由 get_user_token_with_refresh 自动刷新。

---

## 五、风险分析

| 风险 | 严重度 | 对策 |
|------|--------|------|
| 用户 token 过期且 refresh_token 也失效 | P0 | 检测到 401 时 task 标记 failed + 通知用户重新登录 |
| 多用户托管同一仓库时 token 选择 | P1 | managed_repos.host_user_id 记录首个托管者 |
| 用户 token scope 不足 | P1 | 托管时已校验权限（owner/admin），scope 不足会被 GitCode API 拒绝 |
| webhook 重建 | P2 | 路径 A 旧 webhook 保持原验证方式；路径 B 新 webhook 用随机密钥 |
| git commit author 变成用户名 | P2 | 无功能影响 |

---

## 六、验证标准

1. 全新部署（无全局 bot token）：用户 PAT 绑定 → 托管仓库 → 建 issue → pipeline 全链路跑通 → 无死循环
2. 已有部署（有全局 bot token）：升级后现有仓库托管状态不变，pipeline 拿到的 token 与改造前一致
3. 混合场景：同一平台路径 A 和路径 B 仓库共存互不干扰
4. token 刷新：路径 B 场景下用户 token 过期后自动刷新
5. webhook 验证：路径 B 场景下 GitCode 推事件验证通过
6. 自循环检测：路径 A actor=bot 被拦截；路径 B 评论带标记被拦截
7. A2A 鉴权：路径 A 走 bot token 通道；路径 B 走 session token 通道
8. PAT token 过期提示：路径 B PAT 被撤销后 401 → task failed + errors 写明原因 + Web UI 提示 + issue 回帖提示

---

## 七、实现顺序

1. DDL 迁移 + add_managed_repo 签名（基础）
2. 新增函数：get_repo_token_with_refresh（四级 fallback）/ get_host_user_id / get_webhook_secret / _has_refresh_token / _is_user_token_repo
3. get_gitcode_token / get_agent_bot_token 内部改为调 get_repo_token_with_refresh；get_repo_bot_token 保留原语义 + 用户 token 降级
4. host_repo GitCode 路径 B
5. host_repo GitHub 路径 B + setup_github_webhook 传 webhook_secret
6. create_from_template 路径 B + webhook_url 硬校验改为不阻断
7. get_github_token 加用户 token 降级
8. get_webhook_token_by_repo 改造
9. pipeline/agent/repo.py token 获取替换
10. get_user_token_with_refresh 兼容 PAT（无 refresh_token 跳过刷新）
12. 自循环检测顺序调整
13. _get_task_token helper
14. repair_host 路径 B
15. A2A trust domain 新增 host_user_id
16. 无邮箱配置场景适配（注册/忘记密码/设置页/登录页 OAuth 按钮）
17. R10 非 OAuth token 过期提示（401 检测 + 标记 status=0 + UI/issue 提示）
18. 测试验证
19. squash + push

---

## 八、深度审计发现的问题与修复（第二轮）

> 以下问题在第一轮实现后的逐行深度审计中发现，需同步修复。

### 8.1 P0-1: R10 task errors 格式破坏 — dashboard 静默丢失失败任务

**问题**：`fail_task` 中 R10 代码用 JSON 对象 `{"error_code":"TOKEN_EXPIRED",...}` 覆写 `agent_tasks.errors` 字段。原 errors 格式是 JSON 列表 `["error message"]`。dashboard.py:173 用 `_json.loads(r[10])` 解析后调 `_errors[:3]` 切片——dict 不支持切片抛 TypeError，被外层 `except Exception: pass` 吞掉，导致 token 过期的失败任务从 dashboard 列表静默消失。

**修复**：R10 errors 写入改为列表格式 `[{"error_code":"TOKEN_EXPIRED","message":"...","provider":"...","rebind_url":"/#account"}]`，与原格式兼容。dashboard 切片取第一个元素即可展示 error_code。

**改动文件**：src/runtime/service.py

### 8.2 P0-2: R7 无邮箱注册 email_verified=1 语义错误

**问题**：无邮箱配置时跳过验证码直接注册，但 `create_grape_account` 仍硬编码 `email_verified=1`（P0-4 邮箱验证码注册语义），同时 email=NULL。email_verified=1 但无 email 语义矛盾，影响后续邮箱相关流程判断。

**修复**：`register_verify` 无邮箱配置时传 `email_verified=False` 给 `create_grape_account`（新增参数），或注册后单独 UPDATE `email_verified=0`。

**改动文件**：src/auth/grape_account_db.py（create_grape_account 新增 email_verified 参数）、src/api/routes/grape_auth.py

### 8.3 P1-1: 路径 B 删旧 webhook 失败时密钥不匹配

**问题**：host_repo / create_new_repo / repair_host 三处路径 B 场景，删旧 webhook 失败时 fallback 复用旧 webhook，但 DB 写入了新 `webhook_secret`。旧 webhook 实际密码是旧值，`get_webhook_token_by_repo` 返回新密钥 → 验证不匹配 → 所有事件 403。

**修复**：删旧 webhook 失败时，不写新 `webhook_secret` 到 DB，改为读旧 webhook 的实际密码（路径 A 旧 webhook 用 bot token，路径 B 旧 webhook 用旧 webhook_secret）。具体方案：删失败时 `existing_id` 保留非 None，`add_managed_repo` 调用时不传 `webhook_secret`（传空字符串），让 `get_webhook_token_by_repo` fallback 到 `gitcode_webhook_token`/`github_webhook_token`（旧值）。

**改动文件**：src/api/routes/repos.py（4 处）

### 8.4 P1-2/3/4: complete_task / report_messages / event_archive 空 token

**问题**：路径 B 场景 task 行 `repo_gitcode_token` 为空（设计意图——用户 token 不写入 task 行）。但 `service.py` 中 4 处直接读 `task.get("repo_gitcode_token")` 而非调 `_get_task_token(task)`：
- L2056 `complete_task`：系统回帖 `_token` 为空 → `if _issue_num and _repo and _token:` 跳过回帖
- L1737 `report_messages`：`tokens = [task.get("repo_gitcode_token") or ""]` 为空列表 → 脱敏失效
- L1980 / L2363 `finalize_event_archive`：同上

agent.py 的 6 处已改为 `_get_task_token(task)`，service.py 的 4 处遗漏。

**修复**：service.py 中 4 处 `task.get("repo_gitcode_token") or ""` 改为调 `_get_task_token(task)`（从 agent.py import 或在 service.py 内定义同等函数）。

**改动文件**：src/runtime/service.py

### 8.5 P1-5: repair_host GitHub 路径 A bot_ok=False 不阻断

**问题**：`repair_host` 中 GitHub 路径 A（有全局 GitHub bot token），`get_github_repo_permissions` 异常时 `bot_ok=False`，但不像 GitCode 分支那样有邀请+重试逻辑——`if not bot_ok:` 块的缩进在 `else`（GitCode）块内部，GitHub 分支不执行。GitHub bot 权限缺失时 repair_host 不报错也不修复，直接建 webhook，bot 无法操作仓库。

**修复**：将 `if not bot_ok:` 块的邀请+重试逻辑提到 `if _provider == "github":` / `else:` 的同级（`if not _use_path_b:` 块内），GitHub 和 GitCode 共用。GitHub 用 `add_github_collaborator` + `accept_invite_for_repo`（GitHub 自动 accept，不需要显式 accept）。

**改动文件**：src/api/routes/repos.py

### 8.6 P2-1: _has_refresh_token 只查 status=1

**问题**：`_has_refresh_token` 查 `WHERE status=1`，token 被 `mark_identity_expired` 标记 status=4 后返回 False。`get_user_token_with_refresh` 中 `if not _has_refresh_token: return token` 直接返回过期 token 不刷新。OAuth token 被误标过期后无法自动刷新恢复。

**修复**：`_has_refresh_token` 查询条件改为 `WHERE status IN (1, 4)`，与 `_get_oauth_token_from_identity` 对齐。

**改动文件**：src/auth/grape_account_db.py

### 8.7 P2-2: 前端 i18n 缺 3 个 key

**问题**：account.js 引用 `app.account.token_expired`、`app.account.token_expired_hint`、`app.account.re_bind` 三个 i18n key，但 en/zh app.json 的 account namespace 中没有这些 key，中文环境显示英文 fallback。

**修复**：en/zh app.json 的 account 对象新增这三个 key。

**改动文件**：src/api/static/locales/en/app.json、src/api/static/locales/zh/app.json

### 8.8 P2-3: host_repo / issues.py 无 token 错误缺 error_code

**问题**：`host_repo` 中 `if not api_token` 和 `issues.py` 中 `if not api_token` 的 raise HTTPException 没有 `error_code` 字段，前端 `app.errMsg(e)` 无法映射 i18n，直接展示中文 message。

**修复**：这些错误响应追加 `error_code: "TOKEN_NOT_BOUND"`。

**改动文件**：src/api/routes/repos.py、src/api/routes/issues.py

### 8.9 P2-4: _build_retry_env 漏 BOT_USERNAME host_user fallback

**问题**：event_pipeline.py 中 3 处 BOT_USERNAME 注入逻辑，路径 B 的 host_user external_login fallback 只加到了 `_enqueue_pipeline_task`（L2397）和 `_execute_agent_inner`（L3163），漏了 `_build_retry_env`（L2647）。task 重派时 BOT_USERNAME 为空，回退到硬编码 "grape_bot"，PR 审查人设置失败。

**修复**：`_build_retry_env` 中 BOT_USERNAME 注入逻辑末尾追加与 L2397 相同的 host_user fallback。

**改动文件**：src/api/event_pipeline.py

### 8.10 P2-5: 过期 token 发评论必然 401

**问题**：R10 issue 回帖用 `get_repo_token_with_refresh` 取过期 token 发评论，必然 401，评论发不出去，用户在 issue 上看不到 token 过期提示。

**修复**：R10 issue 回帖改为用 host 用户的另一个 provider token（如果可用），或者在 `_post_result_to_issue` 失败时不影响主流程（已有 except 兜底）。更合理的方案：回帖时用 `get_repo_bot_token_no_global`（A2A 通道返回的 token 可能与 pipeline 用的不同），但根本上过期 token 无法发评论——改为在 task 结果中写入 error_code，前端 dashboard 展示即可，issue 回帖作为 best-effort 不强求。

实际修复：保留 best-effort 行为（已有 except 兜底），在 `_comment_token` 为空时也尝试发帖（用空 token 调 API 会被拒绝但不崩溃）。关键是确保 error_code 已写入 task errors，前端能展示。

**改动文件**：src/runtime/service.py（确保 R10 errors 格式正确后，issue 回帖失败不影响主流程）

### 8.11 P2-7: create_new_repo GitHub 硬编码路径 B

**问题**：`create_new_repo` 中 GitHub 分支 `_use_path_b = True` 硬编码，即使有全局 GitHub bot token 也走路径 B，不邀请 bot 协作者，与 `host_repo` 的 GitHub 路径 A/B 分流逻辑不一致。

**修复**：`create_new_repo` GitHub 分支改为与 `host_repo` 一致——`_use_path_b = not gh_bot_token`，有全局 GitHub bot token 时走路径 A。

**改动文件**：src/api/routes/repos.py

---

## 九、第三轮前端深度审计发现的问题与修复

> 以下问题在前端 R7 降级逻辑逐行审计中发现。三类根因：A. R7 前端降级漏改、B. 动态文案未走 i18n、C. startCountdown 倒计时文案错误 + data-i18n 破坏。需求文档第十章有对应测试用例 T3-01 ~ T3-11。

### 9.1 问题一（P1）：renderOAuthButtons 未配 OAuth 时隐藏按钮

**修复**：`configured.length === 0` 时不隐藏 section（`section.style.display = ''`），保留预渲染按钮，点击时 `oauthLogin` catch 弹 `oauth_not_configured_hint` 提示。

**改动文件**：src/api/static/js/app.js

### 9.2 问题二（P2）：register.html 硬编码中文"注册"

**修复**：无邮箱配置时设置 `data-i18n='app.login.register_btn'` + `textContent = Auth.t('app.login.register_btn')`，语言切换时 I18N.apply 正确更新。

**改动文件**：src/api/static/register.html

### 9.3 问题三+七（P2）：startCountdown 文案错误 + data-i18n 破坏

**修复**：
- 倒计时开始时保存原始 `data-i18n` + `textContent`
- 倒计时期间移除 `data-i18n`（避免 I18N.apply 覆盖倒计时数字）
- 倒计时结束后恢复 `data-i18n` + 用 `t(original_i18n_key)` 恢复原始文案（`resend_code`"重新发送"），不再用 `toast_code_resent`

**改动文件**：src/api/static/js/auth.js

### 9.4 问题四（P1）：account.js 绑定弹窗未实现 R7.4

**修复**：`_accountBindProvider` 从 `_accountProvidersData` 查 provider `configured` 状态。`configured=false` 时只渲染手动令牌表单（`bindPanelToken`），不渲染 OAuth tab / `bindPanelOauth`。`configured=true` 时走原逻辑（OAuth tab + 手动令牌 tab）。

**改动文件**：src/api/static/js/pages/account.js

### 9.5 问题五（P2）：oauthLogin 提示文案不符合需求

**修复**：新增 `oauth_not_configured_hint` i18n key（zh: "请使用用户名+密码注册登录，然后在设置页绑定 Token"，en: "Please register with username + password, then bind your token in Settings"）。`oauthLogin` catch 优先使用 `oauth_not_configured_hint`，fallback 到 `oauth_not_configured`。

**改动文件**：src/api/static/js/app.js、src/api/static/locales/zh/app.json、src/api/static/locales/en/app.json

---

## 十、第四轮审计：R10 issue 回帖去重

### 10.1 P2-1: R10 issue 回帖无去重

**问题**：同一 issue 多个 task 401 失败时，每个 task 的 `fail_task` 都发一条"Token 已过期"评论，导致 issue 下多条重复评论。`_post_result_to_issue` 自带的去重匹配 `[AGENT_TYPE Agent]` 前缀，R10 消息格式是 `⚠️ Token 已过期`，不匹配。

**修复**：
- 新增 `_fetch_issue_comments(repo_key, repo, issue_num, token)` 辅助函数，按 provider 分流查询 issue 评论列表（GitHub → `_github_req`，GitCode → `urllib.request`），失败返回空列表。
- `fail_task` R10 回帖前调 `_fetch_issue_comments` 查已有评论，用 `any()` 检查是否含"Token 已过期"关键词。命中则跳过发帖，查询失败不阻断（降级为原行为）。

**改动文件**：src/runtime/service.py

---

## 十一、第五轮全链路模拟发现的问题与修复

### 11.1 P0-1：路径 D × 远程 worker git worktree 全链路断裂

**问题**：路径 B/D 场景 task 行 `repo_gitcode_token` 刻意写空。本地引擎 `_get_task_token` 实时查主控 DB 取 token——但远程 worker 用本地 `sessions.db`（空库），查不到 token → worktree clone/push 全部失败。

**修复**：`claim()` 返回 task dict 前，如果 `repo_gitcode_token` 为空，服务端现取 token（调 `_get_task_token`，按 provider 分流 + 带刷新）注入 task dict。不落库——agent_tasks 表 repo_gitcode_token 仍为空，注入只发生在 claim 响应的内存 dict 中。worker 拿到后 `task["repo_gitcode_token"]` 非空，`_get_task_token` 优先读 task 行直接返回，不查本地 DB。

**改动文件**：src/runtime/service.py

### 11.2 P1-1：R10 GitHub 过期回帖用错 token 链

**问题**：`fail_task` R10 中 `_comment_token = get_repo_token_with_refresh(_rk)` 没有 `github:` 前缀分流。GitHub 仓库 token 过期时拿 GitCode 链 token 发 GitHub 评论 → 必失败。

**修复**：按 `repo_key` 前缀分流——`github:` → `get_github_token(repo_key=_rk)`，其他 → `get_repo_token_with_refresh(_rk)`。

**改动文件**：src/runtime/service.py

### 11.3 P1-2：B→A 演进场景（文档补充，无代码修复）

**问题**：零管理员部署后管理员配全局 bot token，已有路径 B 仓库的 bot 未入协作组 → API 403。无自动迁移。

**处理**：需求文档风险表补充 B→A 演进运维步骤：逐仓手动 `repair-host` 邀 bot 加入协作者。

---

## 十二、用户菜单统一：反馈/帮助入口 + 工作区用户下拉

### 12.1 设计方案

提取统一的用户菜单项生成函数 `_buildUserMenuItems(includeSecurity)`，所有页面复用。菜单项顺序：用户设置 → 账号安全（可选）→ 反馈 → 帮助 → 退出登录。

### 12.2 改动点

**app.js**：
- 新增 `_buildUserMenuItems(includeSecurity)` 生成统一菜单 HTML
- `_loadUserMenu`（侧边栏）：调 `_buildUserMenuItems(true)`
- `_loadLobbyUserMenu`（Lobby）：调 `_buildUserMenuItems(true)`
- 新增 `_toggleTbUserMenu` / `_loadTbUserMenu` / `_closeTbUserMenuOnOutside`（toolbar 用户下拉）
- 新增 `tbUserBtn` 点击切换逻辑
- 反馈/帮助统一在用户下拉菜单中，不再在 toolbar 放独立按钮

**index.html**：
- toolbar 在 `tbOrgMenu` 后增加 `tbUserBtn`（头像+名称+下拉箭头）+ `tbUserMenu` 下拉容器
- 移除 toolbar 冗余入口：`tbFeedback`（独立反馈按钮）、`help-btn`（侧边栏顶部帮助图标）、`📖 帮助`（toolbar 帮助链接）——统一到用户下拉菜单

**organizations.js**：
- `renderOrganizations` 不再在页面内渲染用户下拉（toolbar 全局 `tbUserBtn` 已覆盖，避免 ID 冲突）

### 12.3 改动文件
- src/api/static/js/app.js
- src/api/static/index.html
- src/api/static/js/pages/organizations.js

---

## 十三、第六轮审计：B→A 演进兼容修复 + 自动升级

> 原设计 §11.3 将 B→A 演进记录为"无代码修复，需手动 repair-host"。深度分析发现核心矛盾：全局 bot token 配置后 `get_repo_token_with_refresh` 第 3 步自动命中全局 bot token 返回，但 bot 账号从未被邀请进路径 B 仓库协作组 → pipeline 拿到 bot token 调 API → 403。token 链路自动切换了，权限没同步切换。手动 repair_host 从"可选升级"变成"必须修复"。

### 13.1 P0：路径 B 仓库跳过全局 bot token 层（兼容修复）

**问题**：`get_repo_token_with_refresh` 四级 fallback 第 3 步 `_get_global_bot_token()` 有值就返回。路径 B 仓库（bot_gitcode_token 空 + host_user_id > 0）在全局 bot token 配置后：
- 第 2 步 bot_gitcode_token 为空 → 跳过
- 第 3 步全局 bot token 有值 → 直接返回 bot token
- 第 4 步用户 token → 永远走不到

bot 账号从未被邀请进路径 B 仓库协作组 → 403。

**修复**：第 3 步之前判断 `host_user_id`——路径 B 仓库（host_user_id > 0）跳过全局 bot token 层，继续走第 4 步用户 token。

```python
# 路径 C: 全局 Bot Token（非路径 B 仓库才走）
if get_host_user_id(repo_key) == 0:
    global_tok = _get_global_bot_token()
    if global_tok:
        return global_tok
```

效果：
- 路径 A 仓库（bot_gitcode_token 非空）：第 2 步命中，不受影响
- 路径 B 仓库（bot_gitcode_token 空，host_user_id > 0）：跳过第 3 步 → 走第 4 步用户 token → 正常工作
- 新仓库（无 managed_repos 记录）：host_user_id = 0 → 第 3 步正常生效
- repair_host 后：bot_gitcode_token 被写入 → 第 2 步命中 → 自动切路径 A

**`_is_user_token_repo` 不需要改**：它判断的正是"前三层全空且 host_user_id > 0"，与改后的 `get_repo_token_with_refresh` 行为一致。

`get_repo_bot_token` 和 `get_github_token` 的用户 token 降级也需同步加 `host_user_id` 判断——这两个函数在第 2 步（全局 bot token）之前已判断了 managed_repos.bot_gitcode_token，但全局 bot token 层需要加 host_user_id 跳过。

### 13.2 P2：管理员配置全局 bot token 时自动升级路径 B 仓库

**触发时机**：`config_store.save_config` 写入 `gitcode_api_token` 或 `github_bot_token` 时，检测从"无值→有值"的转换，异步触发升级。

**升级流程**（best-effort，失败不阻断）：

```python
def _auto_upgrade_path_b_repos(provider: str, bot_token: str, bot_username: str):
    """异步遍历路径 B 仓库，邀 bot 为协作者 + 写入 bot_gitcode_token。

    失败的仓库跳过（兼容修复保证仍走用户 token 正常工作）。
    """
    repos = _query_path_b_repos(provider)  # bot_gitcode_token 空 + host_user_id > 0
    for repo in repos:
        try:
            host_token = _get_host_user_token(repo["host_user_id"], provider)
            if not host_token:
                continue  # host user token 过期，跳过
            # 邀请 bot 为协作者
            _invite_bot(host_token, repo, bot_username, provider)
            _accept_invite(bot_token, repo, provider)
            # 写入 bot_gitcode_token → get_repo_token_with_refresh 第 2 步自动命中
            _update_repo_bot_token(repo["repo_key"], bot_token, provider)
        except Exception:
            continue  # 失败跳过，兼容修复保证仓库仍正常工作
```

**只邀 bot + 写 bot_gitcode_token，不重建 webhook**：
- webhook 验证用 webhook_secret（随机密钥），不依赖 token 类型
- bot 进协作组 + bot_gitcode_token 写入后，pipeline 自动切路径 A
- webhook 保持原样，下次 repair_host 时自然重建

**失败处理**：host_user token 过期 → 跳过，仓库仍走路径 B（兼容修复保证不 403）。下次该用户登录或 repair_host 时再试。

### 13.3 改动文件

| 文件 | 改动 |
|------|------|
| src/user/db.py `get_repo_token_with_refresh` | 第 3 步加 `host_user_id == 0` 判断 |
| src/user/db.py `get_repo_bot_token` | 全局 bot token 层加 `host_user_id == 0` 判断 |
| src/user/db.py `get_github_token` | 全局 bot token 层加 `host_user_id == 0` 判断 |
| src/admin/config_store.py `save_config` | bot token 从无到有时异步触发 `_auto_upgrade_path_b_repos` |
| src/user/db.py | 新增 `_query_path_b_repos` / `_auto_upgrade_path_b_repos` 辅助函数 |

### 13.4 向前兼容

- 路径 A 仓库：bot_gitcode_token 非空，第 2 步命中，不受 host_user_id 判断影响
- 新仓库（无 managed_repos 记录）：host_user_id = 0，全局 bot token 层正常生效
- 已有全局 bot token 的环境：所有仓库都是路径 A（bot_gitcode_token 非空），自动升级不触发（无路径 B 仓库）

---

## 十四、第七轮审计：webhook_url 自动发现补全

> 原 `_detect_server_base`（config.py:230）三级探测在华为云 ECS 上全部静默失败：外网 IP 服务出站被拒 + hostname 解析到 127.0.0.1。失败后 bare except 吞掉，无日志无标记，webhook_url 保持空。用户"绑定 PAT → 点托管"时报 400 WEBHOOK_URL_NOT_CONFIGURED，报错指向"系统设置"（管理员入口），与零管理员目标矛盾。

### 14.1 核心思路

用户访问 grape Web UI 时，HTTP 请求的 Host 头就是服务器对外可达的地址。用户能用这个地址访问我们，GitCode 就能用同一个地址推 webhook。零网络依赖、零出站探测。

### 14.2 方案

**14.2.1 启动时探测加失败可见性（config.py `_detect_server_base`）**

保留现有外网 IP 探测逻辑（对公网直连环境有效），但：
- 每级探测失败时输出 WARNING 日志（含失败原因）
- 全部失败时写 `meta.webhook_url_detect_failed=1` 标记
- 不再 bare except 静默吞掉

```python
def _detect_server_base():
    methods = ["https://api.ipify.org?format=json", ...]
    for url in methods:
        try:
            resp = urllib.request.urlopen(url, timeout=3)
            ...
            return f"http://{ip}:8080"
        except Exception as e:
            log.warning(f"_detect_server_base: {url} failed: {e}")  # 新增
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127.") and not ip.startswith("192.168."):
            return f"http://{ip}:8080"
        log.warning(f"_detect_server_base: hostname resolves to {ip} (loopback/private, skipped)")
    except Exception as e:
        log.warning(f"_detect_server_base: hostname resolution failed: {e}")
    # 全部失败 → 写标记
    from src.user.db import set_meta_value
    set_meta_value("webhook_url_detect_failed", "1")
    return ""
```

**14.2.2 入站 Host 头探测中间件（main.py request_id_and_log_middleware）**

在现有中间件中，已认证请求（Bearer token 验证通过）到达时，从请求头推导 webhook_url。

**支持三种访问场景**：
- **直连访问**（无 nginx）：用户访问 `http://IP:port` → Host 头含 `IP:port`
- **nginx 代理**：用户访问 `http://域名` 或 `https://域名` → nginx 传 `X-Forwarded-Host`（域名+端口）和 `X-Forwarded-Proto`（https）
- **域名访问**：与 IP 访问逻辑完全一致——Host 头是域名就推导域名，不区分 IP 和域名

```python
# 在中间件中，_uid 非空（已认证）时触发
if _uid:
    from src.user.db import get_meta_value, set_meta_value, detect_webhook_from_host
    if not get_meta_value("webhook_url"):
        _host = request.headers.get("host", "")
        _fwd_host = request.headers.get("x-forwarded-host", "")
        _fwd_proto = request.headers.get("x-forwarded-proto", "")
        _candidate = detect_webhook_from_host(_host, _fwd_host, _fwd_proto)
        if _candidate:
            set_meta_value("webhook_url", _candidate)
            if get_meta_value("webhook_url_detect_failed"):
                set_meta_value("webhook_url_detect_failed", "")
            _log.info(f"webhook_url auto-detected from Host header: {_candidate}")
```

**detect_webhook_from_host** 函数（db.py）封装推导逻辑：

```python
def detect_webhook_from_host(host: str, forwarded_host: str = "", forwarded_proto: str = "") -> str:
    # 优先 X-Forwarded-Host（nginx 代理场景含端口），fallback Host 头
    raw = (forwarded_host or host or "").strip().lower()
    if not raw:
        return ""
    # 排除本地开发地址
    for _lh in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        if raw == _lh or raw.startswith(_lh + ":"):
            return ""
    # 协议：X-Forwarded-Proto 优先，无则默认 http
    proto = (forwarded_proto or "").strip().lower()
    if proto not in ("http", "https"):
        proto = "http"
    return f"{proto}://{raw}/webhook/gitcode"
```

**Header 优先级**：
- host 来源：`X-Forwarded-Host` > `Host`（前者含用户真实地址+端口，后者可能是 nginx 内部地址）
- 协议来源：`X-Forwarded-Proto` > 默认 `http`

**nginx 配置要求**（文档中需说明）：
```nginx
# 标准 80 端口代理（Host 只有域名，省略端口=80，正确）
proxy_set_header Host $host;

# 非标准端口代理（需传 $http_host 含端口）
proxy_set_header X-Forwarded-Host $http_host;
proxy_set_header X-Forwarded-Proto $scheme;
```

安全约束：
- 只在 `_uid` 非空时触发（Bearer token 验证通过 = 已认证请求），不采信匿名请求防 Host 伪造
- 排除 localhost / 127.0.0.1 / 0.0.0.0（本地开发不自动发现）
- meta 已有 webhook_url 时跳过（幂等）
- GitHub 仓库托管时 `/webhook/gitcode` 后缀被 `replace("/webhook/gitcode", "/webhook/github")` 自动转换（已有逻辑）

**14.2.2a 中间件 JWT 解析修复（main.py request_id_and_log_middleware）**

**问题**：中间件用 `validate_token`（内存缓存 `_user_tokens`）解析 user_id。但注册/登录返回 JWT token（`_issue_jwt` → `create_access_token`），不走 `create_session_token`，JWT 不在 `_user_tokens` 里 → `validate_token` 返回 None → `_uid` 为空 → webhook_url 推导不触发。

**修复**：中间件改用 `verify_access_token`（JWT 验证）提取 user_id，与依赖层 `get_current_user` 一致。中间件只提取 user_id 用于日志/推导，不做完整验证（token_version 校验、禁用状态校验是依赖层职责）：

```python
# 修复前（bug）：
from src.user.db import validate_token
_uid_val = validate_token(_auth[7:])  # JWT 不在 _user_tokens → None

# 修复后：
from src.api.jwt_utils import verify_access_token
payload = verify_access_token(_auth[7:])
if payload:
    _uid = str(payload.get("sub", ""))
```

保留 `validate_token` fallback 兼容旧 session token（如果存在）。

**14.2.3 host_repo 报错加 guidance 字段（repos.py）**

WEBHOOK_URL_NOT_CONFIGURED 响应从泛化 400 改为带结构化指导：

```python
raise HTTPException(status_code=400, detail={
    "code": 400,
    "error_code": "WEBHOOK_URL_NOT_CONFIGURED",
    "message": "webhook_url not configured",
    "guidance": {
        "tried": ["server-config.json (not found)", "auto-detect (egress blocked, hostname loopback)"],
        "action": "Set webhook_url in Settings page, or access the Web UI via public IP so auto-detect can work",
        "example": "http://<public-ip>/webhook/gitcode"
    }
})
```

前端 toast 优先展示 guidance.action + guidance.example，非泛化 message。

### 14.3 改动文件

| 文件 | 改动 |
|------|------|
| src/config.py `_detect_server_base` | 每级探测失败加 WARNING 日志 + 全部失败写 meta 标记 |
| src/user/db.py | 新增 `detect_webhook_from_host(host, forwarded_host, forwarded_proto)` 辅助函数，支持 X-Forwarded-Host/Proto |
| src/api/main.py `request_id_and_log_middleware` | 已认证请求到达时从 Host/X-Forwarded-Host/X-Forwarded-Proto 推导 webhook_url + 写 meta + 清除标记 |
| src/api/routes/repos.py `host_repo` | WEBHOOK_URL_NOT_CONFIGURED 响应加 guidance 字段 |
| src/api/routes/repos.py `repair_host` | 同上 |

### 14.4 向前兼容

- 显式配置存在（server-config.json / meta.webhook_url 有值）：跳过所有探测，行为不变
- 公网直连环境：外网 IP 探测仍有效，启动时自动发现
- 本地开发（localhost）：Host 头探测排除 localhost，不自动发现（开发者手动配）
- 已有 webhook_url 的部署：中间件检测 meta 有值就跳过，不覆盖

---

## 十五、同类问题修复：硬编码地址 + 自动发现断裂

> §14 修复 webhook_url 后深度排查发现四个同类问题。

### 15.1 P1-A: settings.py webhook_url localhost 兜底

**问题**：`settings.py:464` — `get_webhook_url() or "http://localhost:8080/webhook/gitcode"` 展示假地址。

**修复**：去掉 localhost 兜底，返回空字符串。

### 15.2 P1-B: events.py A2A re-dispatch 硬编码 localhost:8000

**问题**：`events.py:975` — `f"http://localhost:8000/api/a2a"` 硬编码。

**修复**：改用 `get_server_base_url() + "/api/a2a"`，空时 fallback localhost。

### 15.3 P0-C: OAuth redirect_uri 不读 X-Forwarded-Host

**问题**：`providers.py:421` — `_get_trusted_host` 只读 `Host` 头，nginx 代理场景下拿到内网地址。

**修复**：优先读 `X-Forwarded-Host`，fallback `Host`。排除 localhost。TRUSTED_HOSTS 白名单校验最终值。

### 15.4 P2-D: _detect_server_base 硬编码 8080 端口

**问题**：`config.py:242` — `f"http://{ip}:8080"` 端口错误。

**修复**：从 `GRAPE_PORT` 环境变量读取，默认 3000。

### 15.5 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/settings.py | 去掉 localhost 兜底 |
| src/api/routes/events.py | A2A re-dispatch 改用 get_server_base_url |
| src/auth/providers.py `_get_trusted_host` | 优先 X-Forwarded-Host + 排除 localhost |
| src/config.py `_detect_server_base` | 端口从 GRAPE_PORT 读取，默认 3000 |
| src/user/db.py `get_server_base_url` | 最终 fallback 用 GRAPE_PORT，非 8080 |
| src/prompt_builder.py `_resolve_server_url` | 异常 fallback 用 `_local_port()`，非 8080 |
| src/auth/providers.py `_get_trusted_host` | Host header default 改空字符串，非 localhost:80 |

---

## 十六、路径 A bot 协作者失败降级到路径 B

> 路径 A 假设 bot token 有效且 bot 用户存在。实际环境 bot token 可能过期/无效，bot 用户名可能不存在。应降级到路径 B 而非报错终止。

### 16.1 host_repo GitCode 路径 A 降级（repos.py L949-985）

三个失败点改为降级而非 raise 502：

```python
# 降级辅助：路径 A → 路径 B
def _fallback_to_path_b():
    nonlocal _use_path_b, webhook_token, _webhook_secret
    _use_path_b = True
    webhook_token = api_token
    _webhook_secret = secrets.token_urlsafe(32)
    _mark("path_b_fallback")
    log.warning(f"host_repo: path A bot collaborator failed, falling back to path B")

# 1. add_collaborator 失败 + get_member_permission 兜底也失败 → 降级
if not ok:
    try:
        bot_perm = get_member_permission(...)
        if access_level >= 40:
            continue  # bot 已是维护者
        else:
            _fallback_to_path_b()  # 原 raise 502
    except _RateLimitedError:
        raise 503  # 限流仍报 503
    except Exception:
        _fallback_to_path_b()  # 原：穿透 502

# 2. bot 权限 access_level < 40 → 降级（原 raise 502）
if access_level < 40:
    _fallback_to_path_b()
```

### 16.2 repair_host GitCode 路径 A 降级（repos.py L1089-1135）

`bot_ok=False` 时不 return error，改为降级：

```python
if not bot_ok:
    # 降级到路径 B（原 return error）
    _use_path_b = True
    _wh_token = api_token
    _webhook_secret = secrets.token_urlsafe(32)
    log.warning(f"repair_host: path A bot failed, falling back to path B")
    # 继续走 webhook 流程
```

`add_collaborator` + `accept_invite_for_repo` 包 try/except：

```python
try:
    add_collaborator(api_token, owner, repo, bot_username, "maintainer")[0]
    accept_invite_for_repo(bot_token, owner, repo)
except Exception as _e:
    log.warning(f"repair_host: bot invite failed: {_e}")
    bot_ok = False
```

### 16.3 host_repo GitHub _try_github_bot_collaborator catch（repos.py L717）

`_try_github_bot_collaborator` 内部 catch 异常，不抛出：

```python
def _try_github_bot_collaborator(api_token, bot_token, bot_username, owner, repo):
    if not bot_token or not bot_username:
        return {"bot_collaborator_needed": False}
    try:
        perms = get_github_repo_permissions(bot_token, owner, repo)
        # ... 原逻辑
    except Exception as e:
        log.warning(f"_try_github_bot_collaborator: error: {e}")
        return {"bot_collaborator_needed": True, "bot_status": "error"}
```

调用方检测 `bot_status == "error"` 时降级到路径 B。

### 16.4 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/repos.py `host_repo` GitCode | 三处 raise 502 改为降级到路径 B |
| src/api/routes/repos.py `repair_host` GitCode | bot_ok=False 降级 + add_collaborator catch |
| src/api/routes/repos.py `_try_github_bot_collaborator` | 内部 catch 异常返回 error 状态 |
| src/api/routes/repos.py `host_repo` GitHub 分支 | add_managed_repo 前检查 bot_status=error → 降级到路径 B |

---

## 十七、PAT 绑定漏写 unified_user_mapping（8 环节受影响）

> 非 #372 引入，但 #372 零管理员部署场景下 PAT 绑定是唯一绑定方式，问题必然暴露。

### 17.1 根因

PAT 绑定（PUT /api/settings/credentials）调 `create_external_identity` 写 `external_identities`，但不写 `unified_user_mapping`。启动时回填用 `grape_accounts.username` 作为 `gitcode_user`——PAT 用户自定义 username ≠ GitCode login。

### 17.2 8 个受影响环节

pipeline 用 actor（GitCode login）查 `unified_user_mapping.gitcode_user` 找 grape user_id。查不到 → user_id=0，影响：issue 创建、引擎选择、issue session、onboarding、tasks API、我的需求查询、dashboard 统计、GitHub PAT 绑定。

### 17.3 修复

PAT 绑定成功后同步写入 `unified_user_mapping`（用 `INSERT OR IGNORE` + `UPDATE`，不用 `INSERT OR REPLACE`）：

```python
# GitCode（settings.py gitcode_token 分支，create_external_identity 后）
# INSERT OR IGNORE：行已存在不删不插；UPDATE：只改 gitcode_user 字段，不覆盖 github_login
db.execute("INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)", (user["id"],))
db.execute(
    "UPDATE unified_user_mapping SET gitcode_user=?, gitcode_nickname=?, updated_at=datetime('now') WHERE user_id=?",
    (gc_login, _gc_user.get("name", ""), user["id"])
)

# GitHub（settings.py github_token 分支，create_external_identity 后）
db.execute("INSERT OR IGNORE INTO unified_user_mapping (user_id) VALUES (?)", (user["id"],))
db.execute(
    "UPDATE unified_user_mapping SET github_login=?, updated_at=datetime('now') WHERE user_id=?",
    (gh_login, user["id"])
)
```

**为什么不用 INSERT OR REPLACE**：该表主键是 `user_id`，一个用户一行平铺多平台字段（gitcode_user/github_login/feishu_open_id 等）。`INSERT OR REPLACE` 会删整行重插，未传字段被 DEFAULT 覆盖为空——用户先绑 GitCode 再绑 GitHub 会丢失 `gitcode_user`。

### 17.4 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/settings.py `update_credentials` gitcode_token 分支 | create_external_identity 后 INSERT OR REPLACE unified_user_mapping (gitcode_user=gc_login) |
| src/api/routes/settings.py `update_credentials` github_token 分支 | create_external_identity 后 INSERT OR REPLACE unified_user_mapping (github_login=gh_login) |

---

## 十八、E2E 实测修复：_create_direct_task / _create_failed_task 缺 pool_engine_source

> E2E 实测发现 direct 模式下 task 记录未落库，agent 在内存中执行但前端看不到执行记录。

### 18.1 根因

`_TASK_COLUMNS_LIST`（service.py:374）包含 `pool_engine_source` 列。`_task_insert_row`（event_pipeline.py:203）对缺列抛 `ValueError`。`_create_direct_task`（L264）和 `_create_failed_task`（L359）的 `_task_insert_row` 调用未传 `pool_engine_source` → `ValueError` → 内部 try/except 捕获返回 `""` → 调用方不检查返回值直接调 `start_direct_agent` → agent 内存执行但 task 不在 DB。

基线 pre-existing bug，#372 零管理员 direct 模式场景首次完整触发。

### 18.2 修复

两处 `_task_insert_row` 调用追加 `pool_engine_source=""`：

```python
# _create_direct_task (L264)
row = _task_insert_row(_TASK_COLUMNS_LIST,
    ...,
    pool_engine_ids="",
    pool_engine_source="",  # 新增
    status="queued", ...)

# _create_failed_task (L359)
row = _task_insert_row(_TASK_COLUMNS_LIST,
    ...,
    pool_engine_ids="",
    pool_engine_source="",  # 新增
    status="failed", ...)
```

direct 模式的 task 不是广播入队，`pool_engine_source` 为空字符串（与 `_enqueue_pipeline_task` 非广播路径默认值一致）。

### 18.3 改动文件

| 文件 | 改动 |
|------|------|
| src/api/event_pipeline.py `_create_direct_task` | `_task_insert_row` 调用追加 `pool_engine_source=""` |
| src/api/event_pipeline.py `_create_failed_task` | `_task_insert_row` 调用追加 `pool_engine_source=""` |

### 18.4 向前兼容

`pool_engine_source` 列在基线 `_TASK_COLUMNS_LIST` 中已存在，DB 表也有该列（历史迁移已加）。本次只补 INSERT 值，不改表结构，不影响已有数据。

---

## 十九、get_user_identities_with_tokens 查询条件一致性修复

### 19.1 根因

`get_user_identities`（不含 token，供前端）已改为 `status IN (1, 4)`（R10 过期 token 也返回）。但 `get_user_identities_with_tokens`（含 token，供后端内部）仍只查 `status=1`。两个函数查询条件不一致。

### 19.2 修复

`get_user_identities_with_tokens` 查询条件改为 `status IN (1, 4)`：

```python
# 修复前
"FROM external_identities WHERE grape_user_id=? AND status=1"

# 修复后
"FROM external_identities WHERE grape_user_id=? AND status IN (1, 4)"
```

调用方使用返回的 token 时，status=4 的 token 已过期（API 返回 401），调用方已有 401 检测逻辑（R10 fail_task），不影响安全。

### 19.3 改动文件

| 文件 | 改动 |
|------|------|
| src/auth/grape_account_db.py `get_user_identities_with_tokens` | 查询条件 `status=1` → `status IN (1, 4)` |

### 19.4 向前兼容

- 旧库无 status=4 的行 → 查询结果不变
- status=4 的 token 过期，调用方 API 调用会被 401 拒绝 → 不影响功能
- 与 `get_user_identities` / `_has_refresh_token` / `_get_oauth_token_from_identity` 查询条件对齐

---

## 二十、测试隔离：conftest DB 绑定污染修复

### 20.1 根因

90+ 个旧测试模块在 import 时（collection 阶段）执行 `os.environ["DEVOPS_DB_PATH"] = "/tmp/xxx.db"`，覆盖 conftest 设的 `data/test_sessions.db`。`session_manager.db` 连接对象在首次 import 时绑定到当时的 env 值，后续 env 被其他模块改了但连接不重新绑定。全量 collection 时最后 import 的模块决定了 `session_manager.db` 指向哪个文件 → 新测试的 INSERT 写到一个库，断言读的是另一个库。

### 20.2 修复

conftest.py 加 function-scope autouse fixture `_reset_db_path`，在每个测试运行前重置 env + 重绑 db 连接：

```python
@pytest.fixture(scope="function", autouse=True)
def _reset_db_path():
    """每个测试前重置 DEVOPS_DB_PATH + 重绑 session_manager.db 连接。

    防止其他测试模块 import 时改 env 导致 session_manager.db 指向错误文件。
    """
    _test_db = os.path.join(ROOT, "data", "test_sessions.db")
    os.environ["DEVOPS_DB_PATH"] = _test_db
    # 重绑 session_manager.db 到正确路径
    import src.session_manager as _sm
    if _sm._db_path != _test_db or str(_sm.db) != str(_test_db):
        _sm._db_path = _test_db
        import sqlite3 as _sqlite3
        _sm.db = _sqlite3.connect(_test_db, check_same_thread=False)
        # 重新初始化表（幂等）
        from src.api.db_setup import init_database
        init_database()
    yield
```

### 20.3 改动文件

| 文件 | 改动 |
|------|------|
| tests/conftest.py | 新增 `_reset_db_path` function-scope autouse fixture |

### 20.4 向前兼容

- 不影响已有测试：`_reset_db_path` 只在 env 被污染时重绑，未污染时 `_db_path` 已正确 → 跳过重绑
- 不影响生产代码：仅 tests/conftest.py 改动

---

## 二十一、P0 修复：路径 A 降级后 bot_gitcode_token 仍写全局 bot token

### 21.1 根因

host_repo GitCode 路径 A 降级到路径 B 时（bot 协作者邀请/权限检查失败），`_use_path_b=True` 但 `global_bot_token` 非空。L1039/L1052 两处 `add_managed_repo` 无条件传 `bot_gitcode_token=global_bot_token` → DB 写入 bot token → `get_repo_token_with_refresh` 第 2 级命中 → 返回无协作权限的 bot token → API 403。

GitHub 分支已正确修复（`bot_github_token=("" if _gh_use_path_b else gh_bot_token)`），GitCode 分支遗漏。

### 21.2 修复

```python
# L1039 (existing_id 复用 webhook)
bot_gitcode_token=(global_bot_token if not _use_path_b else ""),

# L1052 (新建 webhook)
bot_gitcode_token=(global_bot_token if not _use_path_b else ""),
```

### 21.3 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/repos.py L1039 | `bot_gitcode_token=global_bot_token` → `bot_gitcode_token=(global_bot_token if not _use_path_b else "")` |
| src/api/routes/repos.py L1052 | 同上 |

### 21.4 向前兼容

- 路径 A 正常（未降级）：`_use_path_b=False` → `bot_gitcode_token=global_bot_token`，与原行为一致
- 路径 B 从一开始（无全局 bot）：`global_bot_token=""` → 写空，与原行为一致
- 路径 A 降级到路径 B：`_use_path_b=True` → 写空（修复后），bot token 不落库，用户 token 正常生效

---

## 二十二、P0 修复：repair_host 降级后仍无条件写 bot token

### 22.1 根因

repair_host L1236 `_db_kwargs[_cfg["db_bot_token_field"]] = bot_token` 无条件写入。降级到路径 B 后 bot_token 非空但 bot 无协作权限 → DB 写入 → 403。与 host_repo L1039/L1052 同根因，第 3/4 处。

### 22.2 修复

```python
_db_kwargs[_cfg["db_bot_token_field"]] = (bot_token if not _use_path_b else "")
```

### 22.3 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/repos.py repair_host L1236 | `bot_token` → `(bot_token if not _use_path_b else "")` |

---

## 二十三、P1 修复：auto_upgrade 不校验 add_collaborator 返回值

### 23.1 根因

db.py auto_upgrade_path_b_repos L2257 调 `add_collaborator` 不检查 `(ok, err)` 返回值，无条件 `_update_repo_bot_token`。bot 未进协作组但 token 已写入 → 403。

### 23.2 修复

```python
ok, _err = add_collaborator(host_token, owner, repo_name, bot_username, "maintainer")
if not ok:
    skipped += 1
    log.warning(f"auto_upgrade: add_collaborator failed for {repo['repo_key']}: {_err}, skipping")
    continue
accept_invite_for_repo(bot_token, owner, repo_name)
# 只有邀请成功才写 bot token
_update_repo_bot_token(repo["repo_key"], bot_token, provider)
```

GitHub 分支同理：`add_github_collaborator` 返回值也需检查。

### 23.3 改动文件

| 文件 | 改动 |
|------|------|
| src/user/db.py auto_upgrade_path_b_repos | add_collaborator 返回值校验 + 失败跳过 |

---

## 二十四、P2 修复：GRAPE_PORT / GRAPE_MAIN_PORT 命名统一

### 24.1 根因

config.py 和 db.py 用 `GRAPE_PORT`，prompt_builder.py 用 `GRAPE_MAIN_PORT`。两个变量名管同一件事，设一个另一个不生效。

### 24.2 修复

prompt_builder.py `_local_port()` 优先读 `GRAPE_PORT`，fallback `GRAPE_MAIN_PORT`（向后兼容已设此变量的部署）：

```python
def _local_port() -> str:
    return os.environ.get("GRAPE_PORT") or os.environ.get("GRAPE_MAIN_PORT") or "3000"
```

### 24.3 改动文件

| 文件 | 改动 |
|------|------|
| src/prompt_builder.py `_local_port` | 优先 `GRAPE_PORT`，fallback `GRAPE_MAIN_PORT` |

---

## 二十五、结构性修复：仓库级 bot token 层加 host_user_id 守卫 + create_new_repo 列名修正

### 25.1 根因

外部 AI 审查指出：与其逐处补传空值，不如在取 token 的路径 B 加 host_user_id 守卫，一次堵住所有同类脏写入。同时 create_new_repo 对 GitHub 仓库写错 bot token 列名。

### 25.2 修复

**仓库级 bot token 层守卫**（3 个函数）：

```python
# get_repo_token_with_refresh 第 2 级
# get_repo_bot_token 第 1 级
# get_github_token 第 1 级
if get_host_user_id(repo_key) == 0:
    # 查 bot_gitcode_token / bot_github_token
```

**create_new_repo 列名修正**：

```python
# 按 provider 取正确列名
if provider == "github":
    _bot_token_col = "bot_github_token"
else:
    _bot_token_col = "bot_gitcode_token"
# add_managed_repo 调用用 _bot_token_col + _use_path_b 守卫
**{wh_col: _wh_token, _bot_token_col: (_create_global_bot_token if not _use_path_b else "")}
```

### 25.3 改动文件

| 文件 | 改动 |
|------|------|
| src/user/db.py `get_repo_token_with_refresh` | 第 2 级加 `host_user_id == 0` 守卫 |
| src/user/db.py `get_repo_bot_token` | 第 1 级加 `host_user_id == 0` 守卫 |
| src/user/db.py `get_github_token` | 第 1 级加 `host_user_id == 0` 守卫 |
| src/api/routes/repos.py `create_new_repo` | GitHub 用 `bot_github_token` 列 + `_use_path_b` 守卫 |

### 25.4 向前兼容

- 路径 A 仓库（host_user_id=0）：守卫通过，行为不变
- 路径 B 仓库（host_user_id>0）：守卫跳过仓库级 bot token 层，走用户 token——即使列被脏写入也不影响
- 修复后（host_repo/repair_host/create_new_repo 写空）+ 守卫（取 token 跳过）= 双保险

---

## 二十六、P1 修复：降级后 bot_username 未清空

### 26.1 根因

host_repo L1041/L1054 和 repair_host L1235 的 `bot_username` 无条件写入全局 bot 用户名。降级到路径 B 时 bot 未加入协作组，但 `managed_repos.bot_username` 写了全局 bot 名 → `build_task_env` L2412 查 `bot_username` 命中 → `BOT_USERNAME=全局bot` → `create_skill_pr.sh` 审查人=全局 bot → PR 审查失败。L2420 有 host_user identities fallback，但 `bot_username` 非空时先命中不走 fallback。

### 26.2 修复

三处 `bot_username` 改为条件写空：

```python
# host_repo L1041/L1054
bot_username=(global_bot_username if not _use_path_b else "")

# repair_host L1235
"bot_username": (bot_username if not _use_path_b else ""),
```

### 26.3 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/repos.py host_repo L1041 | `bot_username=global_bot_username` → 条件写空 |
| src/api/routes/repos.py host_repo L1054 | 同上 |
| src/api/routes/repos.py repair_host L1235 | `"bot_username": bot_username` → 条件写空 |
| src/api/routes/repos.py host_repo GitHub L867/886 | `bot_username=gh_bot_username` → 条件写空 |

---

## 二十七、P2 修复：_update_repo_bot_token 升级后 host_user_id 未清零

### 27.1 根因

`_update_repo_bot_token` 只写 `bot_gitcode_token`，没清 `host_user_id=0`。升级后语义混合（声明路径 A 但保留路径 B 标记）。

### 27.2 修复

UPDATE SQL 同时 `SET host_user_id=0`：

```python
c.execute(
    "UPDATE managed_repos SET bot_gitcode_token=?, host_user_id=0 WHERE repo_key=?",
    (bot_token, repo_key),
)
```

### 27.3 改动文件

| 文件 | 改动 |
|------|------|
| src/user/db.py `_update_repo_bot_token` GitCode | SQL 加 `host_user_id=0` |
| src/user/db.py `_update_repo_bot_token` GitHub | 同上 |

### 27.4 向前兼容

- 升级前 `host_user_id > 0` + `bot_gitcode_token=""` → 路径 B 正常
- 升级后 `host_user_id=0` + `bot_gitcode_token=bot_token` → 路径 A 正常（守卫通过）
- 未升级的仓库不受影响（`_update_repo_bot_token` 未被调用）

---

## 二十八、P0 修复：token 验证失败 401→400 消除前端 refresh 循环

### 28.1 根因

settings.py L222/228/336/342 四处 GitHub/GitCode token 验证失败用 `status_code=401`。前端 api.js `handleResp` 只看 `r.status === 401` → 触发 `doRefresh` → refresh 200（JWT 没过期）→ 重发 → 业务 401 → 无限循环。

### 28.2 修复

4 处 `status_code=401` → `status_code=400`，detail 加 `error_code: "TOKEN_INVALID"`：

```python
# 修复前
raise HTTPException(status_code=401, detail={"code": 401, "message": "令牌无效或已过期"})

# 修复后
raise HTTPException(status_code=400, detail={"code": 400, "error_code": "TOKEN_INVALID", "message": "token invalid or expired"})
```

### 28.3 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/settings.py L222 | GitHub 401→400 + error_code |
| src/api/routes/settings.py L228 | GitHub else 分支 401→400 + error_code |
| src/api/routes/settings.py L336 | GitCode 401→400 + error_code |
| src/api/routes/settings.py L342 | GitCode else 分支 401→400 + error_code |

### 28.4 向前兼容

- 前端 `handleResp` 对 400 不触发 refresh → 循环消除
- 前端 `errMsg(e)` 可映射 `error_code=TOKEN_INVALID` → i18n 提示
- 已有调用方检查 `status_code=401` 的逻辑需同步改为检查 `error_code`（当前无此调用方）

---

## 二十九、P0 修复：claim_by_tag 漏注入 token

### 29.1 根因

`claim()` 在 L1284-1286 有 token 注入逻辑：路径 B task 行 `repo_gitcode_token` 为空时，服务端现取 token 注入 task dict（不落库）。`claim_by_tag`（L1316+）完全没有这段逻辑。广播模式的远程 worker 通过 `claim_by_tag` 认领 → task dict 里 `repo_gitcode_token` 为空 → 远程 worker 用本地空库查不到 token → git clone/push 失败。

### 29.2 修复

在 `claim_by_tag` 返回 task dict 前补充与 `claim()` 相同的 token 注入逻辑：

```python
# P0-1: 路径 D × 远程 worker token 注入（与 claim() L1284-1286 一致）
if task and not (task.get("repo_gitcode_token") or ""):
    try:
        task["repo_gitcode_token"] = _get_task_token(task)
    except Exception as _te:
        log.warning(f"claim_by_tag: inject token failed for task: {_te}")
```

### 29.3 改动文件

| 文件 | 改动 |
|------|------|
| src/runtime/service.py `claim_by_tag` | 返回 task dict 前补充 token 注入 |

---

## 三十、P1 修复：update_issue 无 grape-auto-comment marker

### 30.1 根因

`update_issue`（PATCH /issues/{number}）修改 issue body 时不加 `grape-auto-comment` 标记。路径 B 场景下 actor=用户 → webhook 事件 → 自循环检测标记层（2.0a）找不到标记 → 不拦截 → agent 修改 issue body 触发自循环。

`create_issue_comment`（POST /comments）已正确加标记（L355-359）。

### 30.2 修复

`update_issue` 的 body 字段追加 marker（仅当 body 非空且未包含 marker 时）：

```python
_body = body.body or ""
if _body and "<!-- grape-auto-comment -->" not in _body:
    _body = _body + "\n<!-- grape-auto-comment -->"
```

labels/assignee/milestone/state 不需要 marker（不产生可回声的 webhook 内容）。

### 30.3 改动文件

| 文件 | 改动 |
|------|------|
| src/api/git_repo_api.py `update_issue` | body 字段追加 grape-auto-comment marker |

---

## 三十一、P0 修复：R10 过期 token 发评论改用全局 bot token

### 31.1 根因

R10 issue 回帖用 `get_repo_token_with_refresh` 取已过期用户 token → 发评论必然 401 → 用户在 issue 上看不到过期提示。

### 31.2 修复

优先用全局 bot token（PAT 不过期），fallback 用户 token：

```python
# GitCode
_comment_token = _get_global_bot_token() or ""
if not _comment_token:
    _comment_token = get_repo_token_with_refresh(_rk) or ""

# GitHub
_comment_token = get_global_github_bot_token() or ""
if not _comment_token:
    _comment_token = get_github_token(repo_key=_rk) or ""
```

零管理员场景无全局 bot token 时仍 best-effort（用户 token 过期 → 评论 401 → except 兜底不影响主流程）。

---

## 三十二、P1 修复：webhook 删旧失败时清空 webhook_token

### 32.1 根因

删旧 webhook 失败 + 路径 B → `_webhook_secret=***` + `gitcode_webhook_token=api_token`（用户 token）。旧 webhook 密码是创建时的值。token 刷新后 stale → 验证 403 → 事件静默丢失。

### 32.2 修复

删旧失败时同时清空 `webhook_token`（4 处）→ loud fail → 管理员可见 → repair_host 修复。

---

## 三十三、P2 修复：guard TTL 保护运行中任务

### 33.1 根因

`purge_stale_event_guards` TTL=30min。长任务 guard 被清理 → 可能重复派发。

### 33.2 修复

purge SQL 加 `NOT EXISTS` 子查询，有运行中 task 的 guard 不清理。

---

## 三十四、P2 修复：detect_webhook_from_host 加 TRUSTED_HOSTS 校验

### 34.1 根因

中间件自动从 Host 头推导 webhook_url，排除 localhost 但无白名单 → 伪造 Host 头注入。

### 34.2 修复

读取 `TRUSTED_HOSTS` 环境变量，配置了则校验。未配置时放行（开发兼容）。

---

## 三十五、P1 修复：create_issue / create_pull / update_pull 补 grape-auto-comment marker

### 35.1 根因

深度扫描发现三个写 body 的 VCS API 端点不加 marker：`create_issue`（POST /issues）、`create_pull`（POST /pulls）、`update_pull`（PATCH /pulls/{number}）。路径 B 场景 actor=用户 → webhook 事件 → 2.0a 标记检测找不到 → 自触发循环。`create_pull` 影响最大（coding agent 常规操作）。

### 35.2 修复

三个端点 body 字段追加 marker：

```python
_body = body.body or ""
if _body and "<!-- grape-auto-comment -->" not in _body:
    _body = _body + "\n<!-- grape-auto-comment -->"
```

### 35.3 改动文件

| 文件 | 改动 |
|------|------|
| src/api/git_repo_api.py `create_issue` | body 字段追加 marker |
| src/api/git_repo_api.py `create_pull` | body 字段追加 marker |
| src/api/git_repo_api.py `update_pull` | body 字段追加 marker |

### 35.4 向前兼容

- body 为空时不加 marker（不改变无 body 请求的行为）
- 已含 marker 的 body 不重复追加
- 现有带 marker 的端点（create_issue_comment / create_pull_comment / update_issue）不受影响

---

## 三十六、P0 修复：_repo_user_id 不查 github_login 导致 GitHub issue user_id 错误

### 36.1 根因

`_repo_user_id`（engine_select.py:163）第 1 步只查 `unified_user_mapping WHERE gitcode_user=?`。GitHub webhook actor 是 GitHub 用户名，查 `gitcode_user` 查不到 → fallback `grape_accounts WHERE username=?` → 命中旧账号。#372 新增了 `github_login` 写入但没改查询逻辑。

### 36.2 修复

第 1 步 SQL 加 `OR github_login=?`：

```python
c.execute(
    "SELECT user_id FROM unified_user_mapping "
    "WHERE (gitcode_user=? OR github_login=?) AND user_id>0 LIMIT 1",
    (issue_creator, issue_creator))
```

### 36.3 改动文件

| 文件 | 改动 |
|------|------|
| src/runtime/engine_select.py `_repo_user_id` | 第 1 步 SQL 加 `OR github_login=?` |

### 36.4 向前兼容

- GitCode actor：`gitcode_user=?` 命中（`OR github_login=?` 不影响，gitcode_user 和 github_login 不会同值）
- GitHub actor：`github_login=?` 命中（不再走 grape_accounts fallback 到旧账号）
- 旧数据无 `github_login`：空字符串不匹配 → 走 fallback（兼容）

---

## 三十七、P0 修复：unified_user_mapping 全量 github_login 查询修复（8 处）

### 37.1 根因

unified_user_mapping 表有 gitcode_user 和 github_login 两字段，但所有查询只查 gitcode_user。GitHub webhook actor 是 GitHub 用户名，只能匹配 github_login。INSERT OR REPLACE 写入覆盖 github_login。

### 37.2 修复

**查询侧（6 处）统一加 `OR github_login=?`**：

| 文件 | 函数 | 修复 |
|------|------|------|
| issues_center.py L74/79 | `_get_user_id_by_gitcode` | `WHERE gitcode_user=?` → `WHERE (gitcode_user=? OR github_login=?)` |
| session_store.py L126 | `set_issue_stage` | 同上 |
| issues_center.py L95 | `_get_feishu_open_id` | 同上 |
| pipeline.py L53 | `_resolve_feishu_name` | 同上 |
| session_manager.py L520 | `get_feishu_open_id_by_gitcode` | 同上（两处 SQL） |

**写入侧（2 处）改 `INSERT OR IGNORE + UPDATE`**：

| 文件 | 函数 | 修复 |
|------|------|------|
| prompt_builder.py L327 | agent 指令模板 | `INSERT OR REPLACE` → `INSERT OR IGNORE` + `UPDATE`（只更新飞书字段） |
| session_manager.py L492 | `update_unified_user_info` | `INSERT OR REPLACE` → `INSERT OR IGNORE` + `UPDATE`（只更新飞书字段） |

### 37.3 向前兼容

- GitCode actor：`gitcode_user=?` 命中（OR 不影响）
- GitHub actor：`github_login=?` 命中
- 旧数据无 github_login：空字符串不匹配 → 走 fallback
- INSERT OR IGNORE + UPDATE 不覆盖已有 github_login / gitcode_user

---

## 三十八、第八轮审计安全修复（R11）

### 38.1 P0：detect_webhook_from_host 安全加固

**根因**：`detect_webhook_from_host` 和 `_get_trusted_host` 在 `TRUSTED_HOSTS` 未设时接受任意 `X-Forwarded-Host` header。nginx 未设 `proxy_set_header X-Forwarded-Host`，客户端可直接发送此 header 伪造 webhook_url。

**修复方案**：
- `detect_webhook_from_host`：未设 TRUSTED_HOSTS 时**不读 X-Forwarded-Host**，仅读 Host header。已设 TRUSTED_HOSTS 时正常优先读 X-Forwarded-Host（nginx 已配 `proxy_set_header X-Forwarded-Host $host` 的场景）。
- `_get_trusted_host`：未设 TRUSTED_HOSTS 时不读 X-Forwarded-Host（同源修复）。

```python
# detect_webhook_from_host 修复逻辑
_trusted = os.environ.get("TRUSTED_HOSTS", "").strip()
if _trusted:
    # 已设白名单 → 可以信任 X-Forwarded-Host（经 nginx 设置）
    raw = (fwd_host or "").strip().lower() or (host or "").strip().lower()
else:
    # 未设白名单 → 仅读 Host header（不读 X-Forwarded-Host，防伪造）
    raw = (host or "").strip().lower()
```

### 38.2 P1：_update_repo_bot_token 升级时清空 webhook_secret

**根因**：B→A 升级后 GitCode 上的 webhook 密码仍是旧 `webhook_secret`，但后续 `add_managed_repo` 路径 A 传入 `webhook_secret=""` → UPDATE 覆盖为空 → 验证不匹配。

**修复方案**：`_update_repo_bot_token` 升级时同步清空 `webhook_secret`。升级后路径 A 不再需要随机密钥（webhook 验证走 `gitcode_webhook_token` = bot token）。如果 GitCode 上 webhook 密码仍是旧 secret，repair_host 路径 A 会重建 webhook（用 bot token 做 password），重建后一致。

```python
# _update_repo_bot_token 修复
c.execute(
    "UPDATE managed_repos SET bot_gitcode_token=?, host_user_id=0, webhook_secret='' WHERE repo_key=?",
    (bot_token, repo_key),
)
```

### 38.3 P1：_get_task_token 抽取到 src/user/db.py

**根因**：agent.py 和 service.py 重复定义相同函数。

**修复方案**：在 `src/user/db.py` 新增 `get_task_token(task: dict) -> str` 公共函数。agent.py 和 service.py 改为 `from src.user.db import get_task_token`。保留 `_get_task_token` 别名兼容（避免 grep 遗漏调用方）。

### 38.4 P1：config_store.py save_config 加 db_lock

**修复方案**：用 `with db_lock` 包裹 bot_payload 的"检测从无到有 + set_meta_value"块。`auto_upgrade` 触发在锁外（daemon 线程不阻塞锁）。

### 38.5 P1：get_repo_token_with_refresh 等函数复用 host_user_id

**修复方案**：函数开头 `host_user_id = get_host_user_id(repo_key)` 查一次，后续路径 B/C/D 守卫复用该变量。`get_repo_bot_token` 和 `get_github_token` 同样处理。

### 38.6 P2：_query_path_b_repos 加 repo_key 前缀过滤

```python
if provider == "github":
    c.execute(
        "SELECT ... FROM managed_repos "
        "WHERE (bot_github_token IS NULL OR bot_github_token='') "
        "AND host_user_id > 0 AND repo_key LIKE 'github:%'"
    )
else:
    c.execute(
        "SELECT ... FROM managed_repos "
        "WHERE (bot_gitcode_token IS NULL OR bot_gitcode_token='') "
        "AND host_user_id > 0 AND repo_key NOT LIKE 'github:%'"
    )
```

GitCode 仓库 repo_key 无前缀或 `gitcode:` 前缀，用 `NOT LIKE 'github:%'` 更安全（兼容无前缀的老数据）。

### 38.7 P2：get_user_github_token_with_refresh 加 _has_refresh_token 守卫

```python
# 与 GitCode 版一致（deps.py L437-438）
from src.auth.grape_account_db import _has_refresh_token
if not _has_refresh_token(user_id, "github"):
    return token
```

### 38.8 P1：register.html / forgot.html 版本号同步

bump `api.js` 到 `v=29`、`app.css` 到 `v=84`。`i18n.js` 未改不需要 bump。

---

## 三十九、第三轮全场景审计修复（R12）

### 39.1 P0：GitHub 评论体提取

pipeline 中 3 处评论体提取（marker 拦截 L1198、指令检测 L1237、L1240）统一改为：
```python
_note = str(_oa.get("note") or _payload.get("comment", {}).get("body") or _payload.get("body") or "")
```

### 39.2 P0：auto_upgrade GitHub 加 accept_github_invite

```python
if provider == "github":
    from src.user.repo_manager import add_github_collaborator, accept_github_invite
    _gh_ok = add_github_collaborator(host_token, owner, repo_name, bot_username, "push")
    if not _gh_ok:
        skipped += 1; continue
    # R12.2: GitHub 需要显式 accept
    if not accept_github_invite(bot_token, owner, repo_name):
        skipped += 1; log.warning(...); continue
else:
    _ok, _err = add_collaborator(...)
    if not _ok:
        skipped += 1; continue
    # R12.8: 校验 accept 返回值
    if not accept_invite_for_repo(bot_token, owner, repo_name):
        skipped += 1; log.warning(...); continue
```

### 39.3 P1：host_repo GitHub invite_pending/add_failed 降级路径 B

`_try_github_bot_collaborator` 返回 `invite_pending` 或 `add_failed` 时，与 `error` 同等处理：`_gh_use_path_b = True`。

### 39.4 P1：R10 回帖路径 B 用用户 token

路径 B 仓库 R10 回帖时不走全局 bot token（无权限），直接用 host user token best-effort。

### 39.5 P1：webhook 无签名无 token 头拒绝

```python
else:
    # R12.5: 无签名无 token 头 → 拒绝（防伪造）
    log.warning(f"GitHub Webhook rejected: no signature and no token header for repo={repo}")
    return JSONResponse(status_code=403, content={"code": 403, "message": "missing signature"})
```

### 39.6 P1：repair_host GitHub 加 accept_github_invite

```python
if _provider == "github":
    add_github_collaborator(api_token, owner, repo, bot_username, "push")
    accept_github_invite(bot_token, owner, repo)  # R12.6
```

### 39.7 P1：升级后 repair/host_repo 复用 webhook 强制重建

repair_host 路径 A 复用已有 webhook 时，检查 DB 中 `webhook_secret` 是否非空（刚升级的仓库残留），非空则强制删旧重建。

### 39.8 P2：issue_updated 标记检测

```python
if event_type == "issue_updated":
    _desc = str(_oa.get("description") or _payload.get("changes", {}).get("body", {}).get("from") or "")
    if "<!-- grape-auto-comment -->" in _desc:
        skip...
```

### 39.9 P2：get_webhook_token_by_repo ORDER BY

```sql
SELECT ... ORDER BY webhook_secret DESC LIMIT 1
```

### 39.10 结构性修复：host_user_id 守卫下沉到 get_bot_token_by_repo

**根因**：守卫只加在 3 个主函数，长尾函数没同步。每轮审计都会捞出同类。

**修复方案**：守卫下沉到 `get_bot_token_by_repo` 单点：

```python
def get_bot_token_by_repo(repo_owner, repo_name):
    repo_key = to_repo_key(f"{repo_owner}/{repo_name}")
    # R12.12: 路径 B 仓库跳过（守卫下沉，12 个调用方自动继承）
    if get_host_user_id(repo_key) > 0:
        return ""
    # ... 原 SQL 查询 ...
```

`get_repo_bot_token_no_global` 的 devops_agent_session 层同步加 `host_user_id == 0` 守卫。`feishu_async.py` 改调 `get_repo_token_with_refresh` 统一入口。

### 39.11 call sites 统一切换到 get_repo_token_with_refresh

守卫下沉到 get_bot_token_by_repo 后，路径 B 仓库返回空。但 scenes.py 等 7 个调用方有 fallback 全局 bot token 的逻辑，拿到无权限的全局 token → 403。

修复：所有需要"获取仓库可用 token"的调用方改调 `get_repo_token_with_refresh`（路径 B 返回用户 token，有仓库权限）。get_bot_token_by_repo 保留给"检查 has_bot_token"和 get_repo_bot_token_no_global（A2A 鉴权专用）。

### 39.12 feishu_async 移除全局 bot token fallback + 最终扫描

R12.13: feishu_async.py sync mapping 移除 get_global_gitcode_bot_token fallback，无 token 时 best-effort 跳过。

R12.14: 全量扫描确认所有 get_bot_token_by_repo 直调（3 处）和 task.get("repo_gitcode_token") 直读（4 处）均有守卫或设计如此。10 轮审计全部收口。

---

## 四十、P1 修复：绑定后跳转项目页仓库列表 abort 竞态

### 40.1 根因

`_accountNavigateToRepos()`（account.js）绑定成功后同时调 `goToRepos()`（GET /repos/member）和 `_forceSyncRepos()`（POST /repos/sync）。两个请求并发，`_forceSyncRepos` 先完成 → `renderReposFromData` → `nav('dash')` → `_navAbortController.abort()` → 取消 `goToRepos` 的在飞请求 → catch 显示 "signal is aborted without reason"。

### 40.2 修复

- `_accountNavigateToRepos` 去掉并发 `_forceSyncRepos`，只调 `goToRepos`
- `goToRepos` catch 加 `if (e.silent) return;` 守卫兜底

### 40.3 向前兼容

- `goToRepos` 内部 GET /repos/member 缓存过期时自动调 VCS API 刷新，效果与 /repos/sync 一致
- stale 探活由 /repos/member 内部后台异步线程处理，不丢失
- `_forceSyncRepos` 保留不删，其他场景仍可调用

---

## 四十一、UI 交互优化设计（R13）

> 对应需求文档第十六章 R13.1-R13.3。纯前端改动，无后端/API 变更。

### 41.1 R13.1：新建项目后强制同步仓库列表

**文件**：src/api/static/js/pages/repos.js

**改动点**：`submitCreateRepo` 函数，API.post('/repos/create') 成功回调中。

**当前代码**（L785-794）：
```javascript
closeCreateRepoModal();
if (typeof _projectsRepos !== 'undefined') {
  API.get('/repos/member').then(d => {
    const repos = d.repos || [];
    renderRepoSelection(repos);
  }).catch(() => renderRepos());
} else {
  renderRepos();
}
```

**改为**：
```javascript
closeCreateRepoModal();
// 强制同步仓库列表（不走缓存），确保新建仓库立即出现在列表中
if (typeof app !== 'undefined' && app._forceSyncRepos) {
  app._forceSyncRepos();
} else if (typeof _projectsRepos !== 'undefined') {
  API.get('/repos/member').then(d => {
    const repos = d.repos || [];
    renderRepoSelection(repos);
  }).catch(() => renderRepos());
} else {
  renderRepos();
}
```

**原理**：`_forceSyncRepos`（app.js L748）内部调 `POST /repos/sync` 强制从 VCS 拉取不走缓存，同步完成后调 `renderReposFromData` 渲染。`/repos/member` 走 `user_repos` 缓存表，新建仓库后缓存可能未更新。`_forceSyncRepos` 失败时内部已降级为 `loadRepos(true)`，不需要外层再 catch。

**templates.js `doCreateFromTemplate`** 和 **`forkTemplate`**：

两处成功回调中，在 `selectRepo` 之前加一行：
```javascript
// R13.1: 强制同步仓库列表，确保返回项目大厅时新仓库在列表中
if (typeof app !== 'undefined' && app._forceSyncRepos) app._forceSyncRepos();
```

`_forceSyncRepos` 是后台异步执行（POST /repos/sync），不阻塞 `selectRepo` 进入仓库。用户返回项目大厅页时仓库列表已是最新。

### 41.2 R13.2：用户设置页不展示仓库信息

**文件**：src/api/static/js/app.js

**改动点**：`nav` 函数中 `view === 'account'` 分支。

**当前代码**（L1454-1459）：
```javascript
} else if (view === 'account') {
  title.textContent = I18N.t('app.account.center_title');
  // 用户设置：只隐藏侧边栏，保留顶栏，mainView 保持正常布局
  var _accSb = $('sidebar'); if (_accSb) _accSb.style.display = 'none';
  renderAccount(param || 'profile');
```

**改为**：在 `renderAccount` 调用前加一行 `repoTitle.textContent = ''` 清空顶栏仓库名。

```javascript
} else if (view === 'account') {
  title.textContent = I18N.t('app.account.center_title');
  // R13.2: 用户设置页不展示仓库信息——清空顶栏仓库名
  repoTitle.textContent = '';
  // 用户设置：只隐藏侧边栏，保留顶栏，mainView 保持正常布局
  var _accSb = $('sidebar'); if (_accSb) _accSb.style.display = 'none';
  renderAccount(param || 'profile');
```

**原理**：`repoTitle` 是顶栏中展示当前仓库名的元素（如 `xiaowu2119/my_demo_demo ↗`）。用户从仓库工作台进入用户设置页时，侧边栏已隐藏但顶栏保留——`repoTitle` 仍显示仓库名。加一行清空即可。与 `nav('organizations')` 的处理方式一致（L1460 已有 `repoTitle.textContent = ''`）。

**不改动**：
- 侧边栏"仓库设置"入口（`nav-settings-gitcode`）保留
- `renderSettings` 的 `gitcode` 分支保留
- 后端 `/settings/gitcode-repo/{owner}/{repo}` API 保留

### 41.3 R13.3：PAT 绑定时提示 token 不会自动续期

**文件1**：src/api/static/js/pages/account.js

**改动点**：`_accountManualBindForm` 函数。

**当前签名**：`_accountManualBindForm(provider, isZh, pName)`

**改为**：增加 `oauthConfigured` 参数，未配 OAuth 时在表单顶部渲染提示卡片。

```javascript
function _accountManualBindForm(provider, isZh, pName, oauthConfigured) {
  var helpSteps = provider === 'github'
    ? (I18N.t('app.account.github_token_help'))
    : (I18N.t('app.account.gitcode_token_help'));

  var html = '';
  // R13.3: 未配 OAuth App 时显示 PAT 不会自动续期提示
  if (!oauthConfigured) {
    html += '<div style="background:var(--bg);border:1px solid var(--b);border-radius:10px;padding:12px 14px;margin-bottom:12px;display:flex;gap:8px;align-items:flex-start">'
      + '<span style="font-size:14px;flex-shrink:0">ℹ️</span>'
      + '<div style="font-size:12px;color:var(--t2);line-height:1.6">'
      + (I18N.t('app.account.pat_no_refresh_hint'))
      + '</div></div>';
  }
  html += '<div class="srow"><label>' + (I18N.t('app.account.access_token')) + '</label>'
    + '<input type="password" id="manualTokenInput" class="kb-input" placeholder="' + (I18N.t('app.account.paste_token')) + '">'
    + '</div>'
    + '<div id="manualBindError" style="display:none;color:var(--r);font-size:12px;padding:4px 0"></div>'
    + '<details style="margin:8px 0"><summary style="cursor:pointer;font-size:12px;color:var(--a)">' + (I18N.t('app.account.how_to_get_token')) + '</summary>'
    + '<div style="font-size:12px;color:var(--t2);padding:8px 0;line-height:1.6">' + helpSteps + '</div></details>'
    + '<button class="bt bt-p" style="width:100%" id="manualBindBtn" onclick="_accountDoManualBind(\'' + provider + '\')">'
    + (I18N.t('app.account.confirm_bind')) + '</button>';
  return html;
}
```

**调用方改动**：`_accountBindProvider` 中两处调用 `_accountManualBindForm` 都传入 `oauthConfigured` 参数。

**文件2**：src/api/static/locales/zh/app.json

新增 i18n key：
```json
"pat_no_refresh_hint": "当前使用 Personal Access Token (PAT) 绑定。PAT 不会自动续期，被撤销或设置过期时间后需要手动重新绑定。如需自动续期，请联系管理员配置 OAuth App。"
```

**文件3**：src/api/static/locales/en/app.json

```json
"pat_no_refresh_hint": "You are binding with a Personal Access Token (PAT). PATs do not auto-renew — if revoked or expired, you will need to re-bind manually. To enable auto-renewal, ask your administrator to configure OAuth App."
```

### 41.4 R13.6：Dashboard 403 时自动切换到下一个有权限仓库

**文件**：src/api/static/js/app.js

**改动点**：`nav('dash')` 中 `loadDash` 的 catch 块。

**当前代码**：
```javascript
.catch(e => {
  if (e.name === 'AbortError' && e.silent) return;
  if (window._currentView !== 'dash') return;
  if (window._dashboardEditing) { app.showToast && app.showToast('刷新失败: ' + e.message); return; }
  pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>';
});
```

**改为**：在 `_dashboardEditing` 检查之后、显示错误之前，加 403 自动切换逻辑。

```javascript
.catch(e => {
  if (e.name === 'AbortError' && e.silent) return;
  if (window._currentView !== 'dash') return;
  if (window._dashboardEditing) { app.showToast && app.showToast('刷新失败: ' + e.message); return; }
  // R13.6: dashboard 403 = 当前仓库无权限，自动切换到下一个有权限仓库
  if (e.status === 403) {
    sessionStorage.removeItem('currentRepo');
    var _sel = $('repoSelect');
    if (_sel && _sel.options.length > 1) {
      for (var i = 0; i < _sel.options.length; i++) {
        var v = _sel.options[i].value;
        if (v && v !== '__add__' && v !== repo.fullName) {
          _sel.value = v;
          switchRepo(v);
          return;
        }
      }
    }
    goToRepos();
    return;
  }
  pc.innerHTML = '<div class="error">' + I18N.t('common.load_failed') + ': ' + e.message + '</div>';
});
```

**原理**：
- `e.status` 由 api.js L227 设置（`e.status = r.status`），403 时可用
- 清除 `sessionStorage.currentRepo` 避免下次还选到无权限仓库
- `repoSelect` 是侧边栏仓库下拉，遍历找第一个非 `__add__` 且非当前仓库的选项
- `switchRepo(v)` 切换到该仓库（内部调 `nav('dash')` 重新加载）
- 没有其他仓库 → `goToRepos()` 跳到项目大厅页
- 在 `_dashboardEditing` 检查之后，编辑模式下 403 只 toast 不切换

### 41.5 向前兼容

- R13.1：`_forceSyncRepos` 不可用时降级为原 `API.get('/repos/member')`，不影响已有环境
- R13.2：仅清空顶栏仓库名，不删除任何已有功能或入口
- R13.3：已配 OAuth App 时 `oauthConfigured=true`，不显示提示，行为不变
- R13.6：只在 403 时触发，其他错误（500/超时等）仍走原错误显示逻辑

---

## §42 R14 审计修复（深度审计 round 14）

### 42.1 R14.1 B→A 升级后 webhook 重建（P1-1 修复）

**问题**：`auto_upgrade_path_b_repos` 调用 `_update_repo_bot_token` 清空 `webhook_secret` 后，GitCode/GitHub 上的 webhook 密码仍是路径 B 创建时的随机密钥。`get_webhook_token_by_repo` 回退到 `gitcode_webhook_token`（bot token），与实际 webhook 密码不匹配 → 所有 webhook 事件被 403 拒绝。

**修复方案**：`auto_upgrade_path_b_repos` 在写入 bot token 前，重建 webhook：
1. 查旧 webhook_id（`_get_managed_webhook_id`）
2. 用 bot token 删除旧 webhook（`remove_webhook` / `remove_github_webhook`）
3. 用 bot token 创建新 webhook（`setup_webhook` password=bot_token / `setup_github_webhook` secret=""）
4. `_update_repo_bot_token` 同步写入 webhook_token=bot_token + webhook_id + 清 webhook_secret

**代码变更**：
- `_update_repo_bot_token` 新增 `webhook_token` 和 `webhook_id` 参数，UPDATE 语句同步写入
- `auto_upgrade_path_b_repos` 在邀 bot 成功后、写 token 前，执行 webhook 重建逻辑

### 42.2 R14.2 路径 B webhook_token 不写 stale 用户 token（P1-2 修复）

**问题**：路径 B 成功创建 webhook 时 DB 写入 `gitcode_webhook_token=api_token`（用户 OAuth token），token 刷新后 DB 值 stale。

**修复方案**：路径 B 成功时 `gitcode_webhook_token` / `github_webhook_token` 写空串，让 webhook 验证完全依赖 `webhook_secret`。路径 B 删旧失败时已做此处理（`_wh_token=""`），成功新建路径统一对齐。

### 42.3 R14.3 R10 路径 B issue 回帖尝试全局 bot token（P1-3 修复）

**问题**：路径 B 仓库 token 过期时，用过期用户 token 发 issue 评论必然 401，用户在 issue 中看不到过期提示。

**修复方案**：路径 B 仓库的 `_comment_token` 也先尝试全局 bot token（即使可能 403，bot 可能有 read 权限能发评论）。全局 bot token 为空时才 fallback 到用户 token（best-effort）。

### 42.4 R14.4 get_host_user_id 冗余查询消除（P1-5 修复）

**问题**：`get_repo_token_with_refresh` / `get_repo_bot_token` / `get_github_token` 每个函数在同一调用中发起 3 次独立的 `get_host_user_id` 查询。

**修复方案**：函数开头调用一次 `host_user_id = get_host_user_id(repo_key)`，后续复用变量。

### 42.5 R14.5 config_store TOCTOU 竞态确认（P1-6）

**问题**：`save_config` 中 `get_meta_value`（检测旧值）和 `set_meta_value`（写新值）之间无事务保护。

**确认**：代码在 R11.5 时已添加 `with db_lock:` 包裹整个 bot_payload 处理块（含 `_upgrade_triggers` 检测和写入）。无需额外代码变更。

### 42.6 R14.6 前端 error_code→i18n 映射启用（P0-1 修复）

**问题**：`app.errMsg(e)` 函数已实现 error_code → `I18N.t('app.error.' + code)` 映射，但未被任何 catch 块调用。

**修复方案**：在 app.js 的关键 catch 块（loadDash error display 等）中调用 `app.errMsg(e)` 替代 `e.message`。

### 42.7 R14.7 grape_accounts.status=4 注释修正（P0-2 修复）

**问题**：`get_grape_account_by_email` 注释误写 "4=token过期"，但 `grape_accounts.status=4` 实际是 "merged"。

**修复方案**：注释改回 "4=merged"。token 过期仅存在于 `external_identities.status=4`。

### 42.8 R14.8 dashboard 403 自动切换去重（P0-3 修复）

**问题**：R13.6 dashboard 403 自动切换无去重，可能遍历多个 403 仓库。

**修复方案**：添加 `_tried403Repos` 数组，每次 403 把当前 repo 加入已试列表，选下一个时跳过已试过的。全部试过则 `goToRepos()`。

### 42.9 R14.9 create_external_identity 清理 status=4 记录（P1-2 修复）

**问题**：用户绑不同账号 PAT 时，status=4 旧记录占 UNIQUE 约束，INSERT 失败。

**修复方案**：INSERT 前清理同 `(grape_user_id, provider)` 的 status=4 记录。

### 42.10 R14.10 events.json zh/en key 同步确认（P2-1）

**问题**：zh 新增 `terminate_confirm_ok/msg`，en 新增 `btn_a2a_redo/status_pending`，两套不重叠。

**确认**：前序子 agent 修复已将两套 key 互补同步到 zh 和 en。本轮无需额外 locale 文件变更。

### 42.11 R14.11 get_user_token_with_refresh dead code 清理（P2-3 修复）

**问题**：`deps.py` L470-474 `return token` 后有不可达代码。

**修复方案**：删除 dead code。

### 42.12 R14.12 add_managed_repo UPDATE 保留已有 webhook_secret / bot_token（P0-1/P0-2 修复）

**问题**：`add_managed_repo` UPDATE 路径无条件覆盖 `webhook_secret`、`bot_gitcode_token`、`bot_github_token` 为调用方传入值（默认空串）。B→A 升级后或路径 B rebind 时二次调用会清空已有值。

**修复方案**：SELECT 时一并查出 `webhook_secret`、`bot_gitcode_token`、`bot_github_token`，调用方传空串时保留已有值：
- `_final_secret = webhook_secret if webhook_secret else existing.webhook_secret`
- `_final_bot_gc = bot_gitcode_token if bot_gitcode_token else existing.bot_gitcode_token`
- `_final_bot_gh = bot_github_token if bot_github_token else existing.bot_github_token`

与 `host_user_id` 的 `_final_host` 保留逻辑对齐。

### 42.13 R14.13 _is_user_token_repo / _is_github_user_token_repo 路径 B 守卫补齐（P0-3 修复）

**问题**：`_is_user_token_repo` 和 `_is_github_user_token_repo` 的路径 B 查询（managed_repos.bot_gitcode_token / bot_github_token）缺少 `host_user_id == 0` 守卫，与 `get_repo_token_with_refresh` / `get_github_token` 主函数不一致。路径 B 仓库被脏写入 bot_gitcode_token 时判断不一致。

**修复方案**：在路径 B 查询前加 `host_user_id = get_host_user_id(repo_key)` + `if host_user_id == 0:` 守卫，与主函数对齐。同时在路径 2（owner external_identities）也加同样守卫。复用变量避免冗余查询。

### 42.14 R14.14 update_managed_repo_bot_token 清 host_user_id + provider 分列（Bug 2 修复）

**问题**：`update_managed_repo_bot_token` 只写 `bot_gitcode_token`，不清 `host_user_id`/`webhook_secret`，且对 GitHub 仓库也错写 `bot_gitcode_token` 列（应该写 `bot_github_token`）。

**修复方案**：
1. 按 `repo_key` 前缀分列写入（`github:` → `bot_github_token`，其他 → `bot_gitcode_token`）
2. 同时清 `host_user_id=0` + `webhook_secret=''`，确保写入 bot token 后读取层走路径 A

### 42.15 R14.15 add_managed_repo 路径 A 成功时显式清 host_user_id（Bug 3 修复）

**问题**：`_final_host` 保留逻辑在调用方传 `host_user_id=0` 时保留已有值。路径 B 仓库通过 `repair_host` 切路径 A 成功后（传 `host_user_id=0` + 非空 `bot_token`），`host_user_id` 残留非零值，读取层继续走用户 token 降级。

**修复方案**：`_final_host` 逻辑改为三分支：
- `host_user_id > 0`：用调用方值（路径 B 写入）
- `host_user_id == 0` 且 `bot_token` 非空：清零（路径 A 显式切换）
- `host_user_id == 0` 且 `bot_token` 为空：保留已有值（路径 B rebind / scene 变更等不涉及 token 的场景）

### 42.16 R14.16 统一 webhook 重建入口（Bug 2 同型 + Bug 1 部分失败兜底）

**问题**：
1. Bug2 同型：PUT /repos/bot-token 和 settings 更新两个手动入口清了 webhook_secret 却不重建 provider 上的 webhook → 手动配 token 后所有 webhook 403。R14.14 只在 auto_upgrade 修了这个洞，手动入口留了一模一样的洞。
2. Bug1 部分失败：auto_upgrade 删旧 webhook 成功但建新失败 → 仓库 webhook 直接没了，事件静默丢失还计成功。

**修复方案**：
1. 抽取 `_rebuild_webhook_with_bot_token(repo_key, bot_token, provider)` 统一重建函数，被 `update_managed_repo_bot_token`（手动入口）和 `auto_upgrade_path_b_repos`（自动升级入口）共用。
2. `update_managed_repo_bot_token` 重构为：调 `_rebuild_webhook_with_bot_token` → 调 `_update_repo_bot_token`（统一写入 token + 清 host_user_id + 写 webhook_id）。
3. `_rebuild_webhook_with_bot_token` 部分失败处理：
   - 删旧失败 → 不建新（返回空串），旧 webhook 保留，验证继续用旧密钥（loud fail 比静默丢事件好）
   - 建新失败 → 返回空串，DB 写空 webhook_id + log.error 告警，用户可 repair_host
   - webhook_url 未配置 → 返回空串，不阻断 DB 写入

### 42.17 R14.17 _tried403Repos 成功后重置

**问题**：dashboard 403 自动切换的 `_tried403Repos` 在成功加载后不重置，同一 session 内权限恢复后切不回之前 403 的仓库。

**修复方案**：dashboard `loadDash` 成功回调中加 `window._tried403Repos = null`。

### 42.18 R14.18 fail_task errors 改为 append 模式（P1-1 修复）

**问题**：`update_task_status` 先写原始错误 `[str(error)]`，R10 逻辑随后覆写为 `[{"error_code":"TOKEN_EXPIRED",...}]`。原始 401 错误详情丢失，dashboard 只显示 TOKEN_EXPIRED。

**修复方案**：R10 改为 append 模式：
1. 读取已有 errors（`SELECT errors FROM agent_tasks`）
2. JSON 解析为 list，非 list 时转为 `[str]`
3. append TOKEN_EXPIRED dict
4. 限制列表长度（保留最近 5 条，dashboard `_errors[:3]` 切片仍有效）
5. 写回 DB

### 42.19 R14.19 update_managed_repo_bot_token 恢复 404 检测 + 注释修正 + 统计修正

**问题 1**：`update_managed_repo_bot_token` 恒返回 True，PUT /repos/bot-token 对不在 managed_repos 里的仓库不再走 404 分支——会拿用户给的 token 真去 VCS 建 webhook，DB 更新 0 行被静默丢弃，接口报成功。

**修复**：先 `SELECT 1 FROM managed_repos WHERE repo_key=?` 检查仓库存在，不存在返回 False（调用方走 404），存在再重建 webhook + 写 DB。

**问题 2**：`_rebuild_webhook_with_bot_token` 删旧失败时注释声称"保留旧 webhook_secret 让验证继续用旧密钥"——实际后续无条件清 webhook_secret 并写 bot token，旧 webhook 必然 403。

**修复**：注释改为准确描述行为："返回空串：调用方会清 webhook_secret 并写 bot token，旧 webhook 的实际密码与 DB 新值不匹配 → webhook 403，loud fail（有日志），用户可 repair_host 恢复"。

**问题 3**：auto_upgrade 的 upgraded 计数把 webhook 重建失败的也算成功，统计失真。

**修复**：区分成功和部分失败——webhook 重建失败的也算成功（bot 协作者已邀请成功）但在日志中增加 `webhook_rebuild_failed=N` 统计。

---

## R22 修订章设计（基线校准后遗漏补充）

### R22.1 token 刷新并发竞态设计

多 worker 并发 claim 时 OAuth refresh 竞态：分布式锁 + 锁内重读 token_expires_at + rotation 失败重试。

```
worker A claim task → get_user_token_with_refresh → 临近过期 → 获取分布式锁 oauth_refresh:{user_id}:{provider}
worker B claim task → get_user_token_with_refresh → 临近过期 → 获取分布式锁（阻塞等待）
worker A 锁内：读 token_expires_at → 未更新 → 执行 refresh → 写新 token + token_expires_at → 释放锁
worker B 锁内：读 token_expires_at → 已更新（worker A 刷新成功）→ 直接读新 access_token 返回，不重复 refresh
```

rotation 失败场景：worker A refresh 成功但 refresh_token 被 rotation → worker B 拿到锁后用旧 refresh_token 刷新 → 失败 → 读最新 refresh_token（worker A 写入的新值）→ 重试一次。

### R22.2 路径 B session token 残留清理设计

get_repo_token_with_refresh 第 1 级 session token 查询时增加 host_user_id 守卫：

```python
# 现有逻辑（db.py:2078-2092）：
c.execute("SELECT repo_gitcode_token FROM devops_agent_session WHERE repo_key=? AND repo_gitcode_token IS NOT NULL AND repo_gitcode_token!='' LIMIT 1", ...)

# 修正后：增加 host_user_id 守卫
_hid = get_host_user_id(repo_key)
if _hid == 0:  # 路径 A 才查 session token
    c.execute("SELECT repo_gitcode_token FROM devops_agent_session WHERE ...")
# 路径 B（_hid > 0）跳过第 1 级，直接走第 4 级用户 token
```

清理时机：托管时 / 路径 A→B 降级时 / 解绑时，清 devops_agent_session.repo_gitcode_token=空串。

### R22.3 多用户托管换主设计

托管时检测已有 host_user_id：
```
if host_user_id != 0 and host_user_id != current_user_id:
    # 已有其他用户托管 → 提示"当前仓库已由用户 X 托管，是否切换？"
    # 用户确认 → 更新 host_user_id → 重建 webhook
    # 用户拒绝 → 不托管
```

### R22.4 agent 执行中 token 过期设计

_post_result_to_issue 失败时不影响 task 状态：
```
try:
    _post_result_to_issue(repo, issue_num, result_text, token, agent_type)
except Exception as e:
    if "401" in str(e) or "Authentication" in str(e):
        task errors append RESULT_POST_FAILED
        log.warning("result post failed (token may have expired during execution)")
    # task 仍标记 completed
```

### R22.5 fail_task 401 关键词补全设计

```python
# 现有关键词列表（service.py:2498）
_kw_list = ["401 unauthorized", "token invalid", "token expired", "authentication failed", "invalid username or token"]
# 扩展后
_kw_list += ["could not read username", "fatal: authentication failed", "fatal: could not read"]
```

空 token 检测在 401 关键词之前：
```python
_tok = get_task_token(task)
if not _tok:
    # 空 token 直接写 NO_TOKEN_AVAILABLE，不走 401 检测
    _errors.append({"error_code": "NO_TOKEN_AVAILABLE", ...})
    return
# 有 token 才走 401 关键词检测
```

### R22.7 C 级修正

- R22.7.2 PAT 过期表述修正：主线 R8 场景二改为"PAT 可设过期时间（GitCode 30/90/永久，GitHub fine-grained 最长 1 年），系统无法预知过期时间，只能 401 被动感知"
- R22.7.3 A2A 鉴权行为确认：get_repo_bot_token_no_global 路径 B 降级返回用户 token（status=4 仍返回），这是设计意图——A2A 鉴权不依赖 token 有效性，只依赖一致性。与支线 R20.13 read_stored_token 意图一致

### R22.8 显式兼容矩阵设计（F1/F3 修复）

**F1 修复（status IN (1,4) 全局行为变更）**：

_get_oauth_token_from_identity 从 WHERE status=1 改为 WHERE status IN (1,4)，影响所有用户 token 读取链路。修复方式：显式声明此行为是设计意图，非兼容性破坏。

```python
# deps.py _get_oauth_token_from_identity
# 基线：WHERE status=1（只返回活跃 token）
# 分支：WHERE status IN (1,4)（活跃 + 过期都返回，过期 token 供 best-effort）
# 设计意图：status=4 的 token 虽已过期，但 fail_task 需要用它发 Issue 评论（best-effort），
# 401 时不重试（R20.2.3）。基线上 status=4 拿不到 token → 无法发评论 → 用户无提示。
# 分支上能拿到过期 token → 尝试发评论 → 可能成功（GitCode 评论权限可能不依赖 token 有效性）
# 或 401 → COMMENT_POST_FAILED → dashboard 通知。语义更优。
```

**F3 修复（同文件两处过滤条件分裂）**：

deps.py:285 GitCode 专用查询保持 status=1，deps.py:329 通用查询改为 status IN (1,4)。

修复方式：统一两处查询为 status IN (1,4)，或在文档中显式声明差异原因：
- deps.py:285 GitCode 专用查询：服务于 OAuth 登录链路（用户登录时不需要过期 token）
- deps.py:329 通用查询：服务于 token 读取链路（fail_task 需要 best-effort 过期 token）
- 建议统一为 status IN (1,4)——OAuth 登录链路遇到 status=4 时应引导用户重新绑定，而非静默返回空

### R22.9 双向状态机矩阵设计（D3 修复）

14 轮审计修 bug 的模式证明缺四字段状态机矩阵。设计如下（与需求文档 R9.7 矩阵对应）：

```
字段：host_user_id × bot_gitcode_token × webhook_secret × webhook_id

合法迁移路径（每条路径四个字段的终态）：

[初始托管 A]    0          | =全局bot      | ''           | 新建
[初始托管 B]    =用户ID    | ''            | =随机密钥    | 新建
[B→A 升级]      0(清)      | =全局bot(写)  | ''(清)       | 重建
[A→B 降级]      =用户ID(写)| ''(清)        | =随机密钥(写)| 重建
[A→A 换bot]     0(不变)    | =新bot(更新)  | ''(不变)     | 重建
[A→A 配repo级]  0(不变)    | =repo级(更新) | ''(不变)     | 重建
[A→A 清repo级]  0(不变)    | ''(清)        | ''(不变)     | 不变(需repair)
[B→B 换用户]    =新ID(更新)| ''(不变)      | =新密钥(重建)| 重建
[解绑]          保留        | ''(不变)      | ''(不变)     | 不变
[删除仓库]      —          | —             | —            | —

非法终态检测（_is_user_token_repo + get_host_user_id 组合判断）：
- host_user_id>0 AND bot_gitcode_token非空 → 异常（路径B有bot token）
- host_user_id==0 AND bot_gitcode_token空 AND webhook_secret空 → 孤儿
- webhook_id存在但password不匹配 → 验证断裂
```

实现建议：在 add_managed_repo / update_managed_repo_bot_token / auto_upgrade_path_b_repos / repair_host 四个函数中，迁移完成后用非法终态检测做 assert/warning，防止 14 轮审计修的 bug 再次回归。

### R22.10 上线策略结论（灰度方案作废，最小必要集）

**前提分析**：

路径 B 只在"无全局 bot token"时触发。存量生产环境（0001，MySQL 全局 bot token 已配置）host_repo 永远走路径 A：

1. 存量部署到不了新代码路径——全局 bot token 有值 → 托管走 A → host_user_id 恒 0 → 四条 token 链全走原逻辑。影子期"预期全绿"不是风险验证，是必然结果。
2. 路径 B 真实受众是新部署（无全局 bot token 的外部开发者）——没有"存量业务"可保护。灰度白名单保护的是不存在的存量。
3. 回滚不需要开关——出问题就配一个全局 bot token（admin 一分钟），auto_upgrade 自动把仓库切回路径 A。回滚机制本来就在设计里（B→A 升级链）。

**结论：灰度/影子期/白名单方案作废。真正必要的只有三件事**：

**1. 修 F3（唯一影响存量的实测问题）**

deps.py:285（GitCode 专用查询）保持 status=1，deps.py:329（通用查询）改为 status IN (1,4)。两处过滤条件分裂是同文件隐性不兼容。统一为 status IN (1,4)——几行改动，与灰度无关。

**2. 确认 R10 守卫足够**

fail_task 的 401 检测已有 `_is_user_token_repo` 守卫（service.py:2506-2512）——只对路径 B/D 仓库（host_user_id>0 且 bot_gitcode_token 为空）触发 status=4 标记。路径 A 仓库（host_user_id=0）的 401 不触发 R10 → 不会误标。确认此守卫覆盖所有 401 入口即可，不需要新开关。

**3. 发布说明披露三处有意变更**

| 变更 | 位置 | 说明 |
|------|------|------|
| status IN (1,4) | deps.py _get_oauth_token_from_identity | 过期 token（status=4）仍返回供 best-effort，语义更优 |
| GitHub token 验证 401→400 | settings.py L222/228/336/342 | 防止前端 JWT refresh 无限循环 |
| GitCode token 验证 401→400 | settings.py 同上 | 同上 |

以上三处是有意变更，不是兼容性破坏，发布说明中披露即可，零代码改动。

**不需要做的事**：
- 不需要灰度/影子期/白名单——路径 B 不影响存量
- 不需要回滚开关——配全局 bot token 即回滚（auto_upgrade 自动切路径 A）
- 不需要 feature flag——host_user_id 守卫体系已是天然的"存量不受影响"开关

---

## 四十三、R23 深度分析遗漏修复设计（round 15）

> 本章对应需求文档 R23 章节，覆盖需求文档 + 设计文档 + 代码实现三方交叉审计发现的 6 个遗漏。

### 43.1 R23.1 _is_user_token_repo host_user_id 守卫

#### 问题

`_is_user_token_repo`（src/user/db.py）是独立路径判断函数，不走 TokenResolver 策略链。R22.2 修复时只在 `DevopsAgentSessionToken._fetch` 加了 `host_user_id > 0` 守卫，遗漏了 `_is_user_token_repo` 也有同样的 session token 直查。

#### 设计方案

在 `_is_user_token_repo` 函数开头查一次 `host_user_id = get_host_user_id(repo_key)`：
- `host_user_id > 0`（路径 B 仓库）→ 跳过 session token 查询，直接进入 bot_gitcode_token 检查（也被路径 B 守卫跳过）→ 最终返回 True
- `host_user_id == 0`（路径 A 仓库）→ 走原逻辑（查 session token → 查 bot_gitcode_token → 查全局 bot token）

```python
def _is_user_token_repo(repo_key: str) -> bool:
    # ...
    _hid = get_host_user_id(repo_key)
    if _hid > 0:
        # 路径 B 仓库：session token 层无关，直接跳到 bot_gitcode_token 检查
        pass
    else:
        # 路径 A: devops_agent_session.repo_gitcode_token（原逻辑）
        try:
            # ... 查 session token ...
            if c.fetchone():
                return False
        except Exception:
            pass
    # 路径 B: managed_repos.bot_gitcode_token
    # R14.13: 路径 B 仓库（host_user_id > 0）跳过此层
    if _hid == 0:
        # ... 查 bot_gitcode_token ...
    # 路径 C: 全局 bot token
    if _hid == 0:
        # ... 查全局 bot token ...
    return _hid > 0
```

#### 与 R22.2 的关系

R22.2.4 修复了 `get_repo_token_with_refresh`（TokenResolver 策略链入口）的 session token 守卫。R23.1 修复 `_is_user_token_repo`（独立路径判断函数）的同一守卫。两者是同一根因（session token 残留）的两个受影响代码路径，必须同步修复。

### 43.2 R23.2 event dialogue session heartbeat

#### 问题

event dialogue（events.py `_execute_conv_task`）用内存字典 `_conv_tasks` 存储 task 状态，不写入 `agent_tasks` 表，也不更新 `devops_agent_session.last_active_at`。`check_running_task` 条件 4 的两级查询都查不到 → 路径 B 自循环检测失效。

R20.15.1 原设计要求"在 agent_tasks 表创建 task 行"，但 event dialogue 的内存执行架构不适合创建 DB task 行（task 生命周期在内存中管理，与 agent_tasks 表的生命周期不同步）。

#### 设计方案（方案 A：session heartbeat）

不改变 event dialogue 的内存执行架构，而是通过更新 `devops_agent_session` 的 heartbeat 让 `check_running_task` 的 session 兜底查询生效。

**启动时**（`_execute_conv_task` 开头，获取 evt 后）：
```python
# R20.15.4: 更新 devops_agent_session.last_active_at → 让 check_running_task
# session 兜底查询能命中
try:
    from src.session_manager import db as _sdb, db_lock as _sdbl
    from datetime import datetime, timezone
    with _sdbl:
        c = _sdb.cursor()
        c.execute(
            "UPDATE devops_agent_session SET last_active_at=?, status='active' "
            "WHERE repo_key=? AND agent_type=?",
            (datetime.now(timezone.utc).isoformat(), repo_key, agent_type),
        )
        _sdb.commit()
except Exception:
    pass
```

**完成时**（`_execute_conv_task` finally 块，写 DB 后）：
```python
# R20.15.5: event dialogue 完成后将 session 置 idle → check_running_task
# session 兜底不再命中 → 用户后续评论可正常触发流水线（R21.7.1）
try:
    from src.session_manager import db as _sdb, db_lock as _sdbl
    with _sdbl:
        c = _sdb.cursor()
        c.execute(
            "UPDATE devops_agent_session SET status='idle' "
            "WHERE repo_key=? AND agent_type=?",
            (repo_key, agent_type),
        )
        _sdb.commit()
except Exception:
    pass
```

#### 与 R20.15 原设计的关系

R20.15.1 原设计要求创建 `agent_tasks` 行。方案 A 不创建 task 行，而是让 session 兜底查询生效。效果等价——`check_running_task` 条件 4 的 session 兜底能命中 → 四条件全满足 → 跳过自循环。

R20.15.1 原设计的 task 行方案适用于走 `enqueue_task` 队列的 agent（飞书 VOD / Chat worker / autopilot），这些 agent 本就走队列创建 task 行。event dialogue 是内存执行模式，方案 A 更适合。

#### 与 R21.7 的关系

R21.7.1 要求"agent 执行完成后立即更新 session status='idle'"。方案 A 的完成时置 idle 正好满足此要求。R21.7.2 要求 session 兜底窗口从 300s 缩短到 60s——代码已实现（`check_running_task` 用 `-60 seconds`）。

### 43.3 R23.3 R15 7 步流程完整性确认

#### 确认结果

代码层面确认：

| 步骤 | 实现位置 | 状态 |
|------|---------|------|
| 1. 检查仓库存在 | `set_repo_bot_token` 路由层 | ✅ 已实现 |
| 2. 验证 token 有效性 | `set_repo_bot_token` 路由层调 `provider.verify_token()` | ✅ 已实现 |
| 3. 获取 bot username | `set_repo_bot_token` 路由层调 `provider.get_user_login()` | ✅ 已实现 |
| 4. 邀 bot 为协作者 | `set_repo_bot_token` 路由层调 `provider.add_collaborator()` | ✅ 已实现 |
| 5. 重建 webhook | `update_managed_repo_bot_token` → `_rebuild_webhook_with_bot_token` | ✅ 已实现 |
| 6. 写 DB | `update_managed_repo_bot_token` → `_update_repo_bot_token` | ✅ 已实现 |
| 7. 返回结果 | `set_repo_bot_token` 路由层返回 200 + bot_username | ✅ 已实现 |

职责分离正确：路由层负责步骤 1-4（VCS API 调用），`update_managed_repo_bot_token` 负责步骤 5-6（webhook + DB）。`auto_upgrade` 调 `update_managed_repo_bot_token` 时不重复邀 bot（auto_upgrade 自己邀了 bot）。

### 43.4 R23.4-R23.6 场景用例

R23.4（多用户托管换主）、R23.5（agent 执行中 token 过期预检）、R23.6（OAuth refresh 分布式锁降级）的场景用例和验收标准已在需求文档 R23 章节中完整定义。设计层面无需额外方案——这些是后续迭代的 P2 项，当前设计已覆盖场景分析，实现时按需求文档的验收标准执行即可。

---

## 四十四、R24 全链路全场景深度审计遗漏修复设计（round 16）

> 本章对应需求文档 R24 章节，覆盖全链路全场景深度分析发现的 7 个遗漏。

### 44.1 R24.1 auto_upgrade 与 global_bot_token_swap 互斥

#### 问题

auto_upgrade（无→有）和 swap（有→有）各自持有不同的分布式锁，可能同时操作同一仓库导致 webhook 丢失和 DB 状态不一致。

#### 设计方案

两个后台操作共用同一个分布式锁 `bot_token_ops`：

```python
# auto_upgrade_path_b_repos
from src.lock import acquire_lock, release_lock
if acquire_lock("bot_token_ops", ttl=600):
    try:
        # 遍历路径 B 仓库...
    finally:
        release_lock("bot_token_ops")

# global_bot_token_swap
if acquire_lock("bot_token_ops", ttl=600):
    try:
        # 遍历路径 A 仓库...
    finally:
        release_lock("bot_token_ops")
```

TTL 600s（10分钟），足够遍历所有仓库。未获取锁的操作等待并重试（或跳过，由调用方决定）。

### 44.2 R24.2 verify_token 异常分类契约

#### 设计方案

`VCSProvider.verify_token` 异常契约明确化：

| 异常类型 | 触发条件 | extract_http_status_from_exc 返回 | 标记过期 |
|---------|---------|----------------------------------|---------|
| RuntimeError 含 `.code`/`.status_code` 属性 | HTTP 4xx/5xx | 从属性提取状态码 | 401→是，其他→否 |
| RuntimeError 无属性（文本含状态码） | _req/_github_req 抛出 | 从文本提取状态码 | 401→是，其他→否 |
| ConnectionError / socket.timeout | 网络超时/连接失败 | None | 否 |
| 其他 Exception | 未知异常 | None | 否 |

`extract_http_status_from_exc` 实现优先级：`e.code` → `e.status_code` → `e.response.status_code` → 文本正则匹配 `(\d{3})` → None。

### 44.3 R24.3 host_vcs_username OAuth refresh 同步

#### 设计方案

在 `get_user_token_with_refresh` 的 refresh 成功路径中，新增 `host_vcs_username` 同步逻辑：

```python
# refresh_oauth_token 成功后
if new_token:
    # best-effort 同步 host_vcs_username
    try:
        login = provider.get_user_login(new_token)
        if login:
            sync_host_vcs_username(repo_key, login)  # 已有函数
    except Exception:
        log.warning("host_vcs_username sync after refresh failed (best-effort)")
```

不阻断 refresh 主流程——同步失败只记 warning。

### 44.4 R24.4 R15.5 清除仓库级 token 时保留 webhook_secret

#### 设计方案

`_update_repo_bot_token` 的 UPDATE SQL 在清除场景（`bot_token=""`）时不清 `webhook_secret`：

```python
# 清除仓库级 token 时
if not bot_token:
    # 清 bot_gitcode_token + bot_token_status，但保留 webhook_secret
    c.execute(
        "UPDATE managed_repos SET bot_gitcode_token='', bot_token_status='' "
        "WHERE repo_key=?", (repo_key,))
else:
    # 正常配置时清 webhook_secret（切换到 bot token 验证）
    c.execute(
        "UPDATE managed_repos SET bot_gitcode_token=?, bot_token_status='', "
        "webhook_secret='', host_user_id=0 WHERE repo_key=?",
        (bot_token, repo_key))
```

### 44.5 R24.5 pipeline 步骤顺序修正

修正 `09-webhook-a2a-token.md` R20.21 的步骤顺序为 `2.0b → 2.0 → 2.0a → 2.0a2`（与代码实现和 05-path-b-self-loop.md 一致）。

### 44.6 R24.6 健康检查锁名 per-provider

锁名从 `token_health_check` 改为 `token_health_check:{provider}`。

### 44.7 R24.7 webhook_secret 脱敏扩展

R20.25.1 脱敏列表新增 `webhook_secret` 值（从 `managed_repos.webhook_secret` 查询）。

---

## 四十五、第八轮深度审计：get_repo_bot_token_no_global 语义破坏 + GitHub A→B 降级 webhook 不重建

> 以下两个问题在 feat/issue-372-user-token-driven 分支相对 weekly-08 的逐行深度审计中发现。审计方法：三个子 agent 并行审计（核心后端 token 链路 / 调用点替换与安全 / 代码范围），加主审计人独立验证（基线对比测试 + 实际运行验证）。

### 45.1 P0：get_repo_bot_token_no_global 语义被 read_stored_token 破坏

#### 问题

`get_repo_bot_token_no_global`（src/user/db.py L1777-1791）函数名明确语义为"no_global"——不含全局 bot token 兜底。weekly-08 基线实现只查两层：

1. `managed_repos.bot_gitcode_token`（通过 `get_bot_token_by_repo`）
2. `devops_agent_session.repo_gitcode_token`

两层都为空时返回空串。该函数用于 `_authorize_a2a`（webhook.py L304-306）的 `_expected` 值——A2A 鉴权对未托管仓库（无 bot token）放行（`if not _expected: return None`）。

issue-372 分支将实现改为调 `TokenResolver.read_stored_token()`，注释写"语义等价"。但 `read_stored_token` 走完整四级策略链，包含第 3 级 `GlobalBotToken`——对未托管仓库返回了全局 bot token。

实测验证：
```
# 对无 bot_gitcode_token 的托管仓库调用：
get_repo_bot_token_no_global('gitcode:owner/repo')
# 基线返回: ""（空串，正确——"no_global"语义）
# 分支返回: "m4kj4JfiLDKLxisQpbfjURwh"（全局 bot token，语义被破坏）
```

**影响**：
- A2A 鉴权 `_authorize_a2a` 中 `_expected` 非空 → 未托管仓库的 A2A 请求 403
- test_t1_queue_mode_enqueues 在 weekly-08 基线通过，在 issue-372 分支失败
- `get_webhook_verify_token`（db.py L2055）末尾也调此函数作为 webhook 验证 fallback——未托管仓库的 webhook 验证可能用全局 bot token 而非空串

#### 根因

`read_stored_token` 的策略链包含 GlobalBotToken（第 3 级），与函数名"no_global"语义矛盾。设计文档 §13.1 已为 `get_repo_token_with_refresh` 加了路径 B 跳过全局 bot token 的守卫，但 `get_repo_bot_token_no_global` 的改造遗漏了同样的守卫。

#### 修复方案

`get_repo_bot_token_no_global` 调 `read_stored_token` 时跳过 GlobalBotToken 策略，与 `get_repo_bot_token` 跳过 DevopsAgentSessionToken 的方式一致：

```python
def get_repo_bot_token_no_global(repo_key: str) -> str:
    from src.token_resolver import TokenResolver, create_provider_from_repo_key
    provider = create_provider_from_repo_key(repo_key)
    resolver = TokenResolver(provider)
    # 跳过第 3 级 GlobalBotToken（保持"no_global"语义）
    resolver.strategies = [s for s in resolver.strategies if s.level != "global_bot"]
    return resolver.read_stored_token(repo_key)
```

效果：
- 第 1 级 DevopsAgentSessionToken：读 devops_agent_session.repo_gitcode_token（不检查过期）
- 第 2 级 RepoBotToken：读 managed_repos.bot_gitcode_token（不检查过期）
- 第 3 级 GlobalBotToken：**跳过**（恢复"no_global"语义）
- 第 4 级 UserToken：读 external_identities access_token（路径 B 仓库降级，不触发 refresh）

路径 A 仓库（bot_gitcode_token 非空）：第 2 级命中，行为不变。
路径 B 仓库（host_user_id > 0）：第 1/2 级跳过（路径 B 守卫），第 3 级跳过（no_global），第 4 级返回用户 token——与设计文档 §13.1 的 `get_repo_token_with_refresh` 行为一致。
未托管仓库（无 managed_repos 记录）：全部为空，返回空串——恢复基线行为。

#### 场景用例

##### 场景 45.1：A2A 鉴权对未托管仓库放行（路径 A 环境）

前置条件：全局 bot token 已配置（meta.global_bot_gitcode_token 有值），仓库 owner/repo 未托管（managed_repos 无记录或 bot_gitcode_token 为空）。

1. 远程 worker 调 `POST /api/a2a`，body 中 repository=owner/repo
2. `_authorize_a2a` 调 `get_repo_bot_token_no_global('gitcode:owner/repo')` 取 `_expected`
3. 修复前：`read_stored_token` 走到第 3 级 GlobalBotToken → 返回全局 bot token → `_expected` 非空 → `_bot_ok` 为 False（worker 传的 auth 不是全局 bot token）→ 403
4. 修复后：跳过第 3 级 → 第 4 级 UserToken 的 host_user_id=0 → 返回空串 → `_expected` 为空 → `if not _expected: return None`（放行）

##### 场景 45.2：A2A 鉴权对路径 B 仓库用用户 token 验证

前置条件：仓库 owner/repo 是路径 B 仓库（host_user_id=5, bot_gitcode_token 为空）。

1. 远程 worker 调 `POST /api/a2a`，Authorization=用户 token
2. `_authorize_a2a` 调 `get_repo_bot_token_no_global('gitcode:owner/repo')`
3. 第 1 级 DevopsAgentSessionToken：路径 B 守卫跳过
4. 第 2 级 RepoBotToken：路径 B 守卫跳过
5. 第 3 级 GlobalBotToken：**跳过**（no_global 语义）
6. 第 4 级 UserToken：host_user_id=5 → 返回用户 token（read_stored_token 不触发 refresh）
7. `_expected` = 用户 token → `_bot_ok` = (_auth == _expected) → 匹配则放行

##### 场景 45.3：webhook 验证 fallback 不误用全局 bot token

前置条件：仓库 owner/repo 是路径 B 仓库，webhook_secret 已配置。

1. GitCode 推 webhook 事件，grape 验证 webhook token
2. `get_webhook_verify_token('gitcode:owner/repo')`：优先查 webhook_secret → 非空 → 返回 webhook_secret
3. 不会走到 fallback `get_repo_bot_token_no_global`——webhook_secret 优先级最高

如果 webhook_secret 为空（删旧 webhook 失败降级场景）：
4. fallback 到 `get_repo_bot_token_no_global`
5. 修复后：跳过全局 bot token → 返回空串或用户 token
6. 空串时 webhook 验证 loud fail（符合 P1-1 降级设计——删失败时不写新密钥，让验证 loud fail）

#### 改动文件

| 文件 | 改动 |
|------|------|
| src/user/db.py `get_repo_bot_token_no_global` | 跳过 GlobalBotToken 策略（`resolver.strategies = [s for s in resolver.strategies if s.level != "global_bot"]`） |

#### 向前兼容

- 路径 A 仓库（bot_gitcode_token 非空）：第 2 级命中，行为不变 ✅
- 未托管仓库：返回空串（恢复基线行为）✅
- 路径 B 仓库：返回用户 token（与 §13.1 get_repo_token_with_refresh 行为一致）✅

### 45.2 P1：GitHub host_repo 路径 A→B 降级时已有 webhook 不重建

#### 问题

`host_repo` GitHub 分支（repos.py L862-877）中，当 `existing_id` 非空（已有 webhook）时，代码先进入 `if existing_id:` 分支写 DB，然后在分支内部检查 bot 协作者状态：

```python
if existing_id:
    # 先检查 bot 协作者状态
    if not _gh_use_path_b:
        _bot_result = _try_github_bot_collaborator(...)
        if _bot_result.get("bot_status") in ("error", "invite_pending", "add_failed"):
            _gh_use_path_b = True          # 降级到路径 B
            _gh_webhook_secret = _secrets.token_urlsafe(32)  # 生成新密钥
    # 直接写 DB（existing_id 仍非空）
    user_db.add_managed_repo(..., webhook_secret=_gh_webhook_secret)
```

问题：A→B 降级时生成了新 `_gh_webhook_secret`，但 `existing_id` 仍指向旧 webhook（路径 A 创建时无 secret）。DB 写入新密钥，但 GitHub 上实际 webhook 没有 secret → `get_webhook_verify_token` 返回新密钥，与 webhook 实际无 secret 不匹配 → **webhook 验证永久失败，所有 push 事件被拒绝**。

**对比 GitCode**：GitCode 的 A→B 降级发生在 webhook 查找之前（L981-1010 先判 `_use_path_b` 再查 webhook），不存在此问题。GitHub 分支的降级发生在 `if existing_id:` 分支内部，existing_id 已固定。

#### 修复方案

A→B 降级时，如果 `existing_id` 非空，删除旧 webhook 并设 `existing_id = None`，使代码走到 `setup_github_webhook` 重建分支：

```python
if existing_id:
    if not _gh_use_path_b:
        _bot_result = _try_github_bot_collaborator(...)
        if _bot_result.get("bot_status") in ("error", "invite_pending", "add_failed"):
            _gh_use_path_b = True
            _gh_webhook_secret = _secrets.token_urlsafe(32)
            # R25.2: A→B 降级时删除旧 webhook，强制用新密钥重建
            if existing_id:
                try:
                    from src.user.repo_manager import remove_github_webhook
                    remove_github_webhook(api_token, owner, repo_name, existing_id)
                    existing_id = None
                except Exception as _re:
                    log.warning(f"host_repo GitHub A→B: remove old webhook failed: {_re}")
                    _gh_webhook_secret = ""  # 删失败时不写新密钥，loud fail
    if existing_id:
        # 复用已有 webhook（路径 A 正常或路径 B 初始创建）
        ...
    else:
        # 重建 webhook（A→B 降级或新建）
        hook_id = setup_github_webhook(api_token, ..., webhook_secret=_gh_webhook_secret)
        ...
```

#### 场景用例

##### 场景 45.4：GitHub 仓库从路径 A 降级到路径 B（bot token 过期）

前置条件：全局 GitHub bot token 已配置但已过期，仓库 owner/repo 之前用路径 A 托管（webhook 无 secret）。

1. 用户重新托管 owner/repo
2. `gh_bot_token = get_global_github_bot_token()` → 非空（但已过期）
3. `_gh_use_path_b = False`（有全局 bot token）
4. `existing_id = find_existing_github_webhook(...)` → 非空（路径 A 创建的旧 webhook）
5. 进入 `if existing_id:` 分支
6. `_try_github_bot_collaborator(api_token, gh_bot_token, ...)` → bot_status="error"（token 过期）
7. 降级：`_gh_use_path_b = True` + 生成新 `_gh_webhook_secret`
8. **修复前**：直接写 DB（existing_id 不变）→ DB 有新 secret 但 GitHub webhook 无 secret → 验证永久失败
9. **修复后**：删除旧 webhook → `existing_id = None` → 走到重建分支 → `setup_github_webhook(api_token, ..., webhook_secret=_gh_webhook_secret)` → 新 webhook 有 secret → DB secret 与 webhook secret 一致 → 验证通过

##### 场景 45.5：删除旧 webhook 失败时的降级

1. 步骤 1-7 同场景 45.4
2. `remove_github_webhook` 失败（GitHub API 超时/权限不足）
3. `_gh_webhook_secret = ""`（不写新密钥）
4. `existing_id` 保留非空 → 进入 `if existing_id:` 分支写 DB
5. DB `webhook_secret` 为空 → `get_webhook_verify_token` fallback 到 `github_webhook_token`（路径 A 旧值）
6. webhook 验证用旧 token（路径 A 的 bot token）——与 GitHub webhook 实际密码一致（路径 A 创建时用 bot token 作 password）
7. 验证通过，但 bot token 已过期 → 后续 API 调用 401 → 触发 token 过期检测
8. 符合 P1-1 降级设计：删失败时 loud fail 或用旧值

##### 场景 45.6：GitHub 仓库初始路径 B（无全局 bot token）

前置条件：无全局 GitHub bot token。

1. `_gh_use_path_b = True`（无全局 bot token）
2. `existing_id = find_existing_github_webhook(api_token, ...)` → 用用户 token 查
3. 如果 existing_id 非空（路径 B 之前创建的 webhook，已有 secret）：
   - `if _gh_use_path_b:` → 删旧重建（已有逻辑 L853-861）
   - 删成功 → `existing_id = None` → 重建
   - 删失败 → `_gh_webhook_secret = ""` → loud fail
4. 如果 existing_id 为空 → 直接创建新 webhook with secret
5. 不受本次修复影响（初始路径 B 的删旧重建逻辑已正确）

#### 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/repos.py `host_repo` GitHub `if existing_id:` 分支 | A→B 降级时删除旧 webhook + 设 `existing_id = None` + 重建；删失败时 `_gh_webhook_secret = ""` |

#### 向前兼容

- 路径 A 正常（bot 协作者 OK）：不进入降级分支，行为不变 ✅
- 初始路径 B（无全局 bot token）：已有删旧重建逻辑，不受影响 ✅
- A→B 降级（bot 协作者失败）：修复后重建 webhook，secret 一致 ✅
- 删旧 webhook 失败：降级为 loud fail（空 secret），与 P1-1 设计一致 ✅

### 45.3 改动文件汇总

| 文件 | 改动 | 问题编号 |
|------|------|---------|
| src/user/db.py `get_repo_bot_token_no_global` | 跳过 GlobalBotToken 策略 | P0 (45.1) |
| src/api/routes/repos.py `host_repo` GitHub `if existing_id:` 分支 | A→B 降级时删除旧 webhook 重建 | P1 (45.2) |

### 45.4 P1：UserToken status=4 后执行链仍拿坏 token（E2E 实测发现）

#### 问题

E2E 端到端实测发现：用户 token 被 `detect_and_mark_expired` 标记 status=4 后，后续新 issue 的 task 仍然拿到坏 token 继续跑 agent，必然 401 失败——浪费一整轮 agent 执行。

根因：`UserToken._is_expired()`（token_resolver.py L624-626）恒返回 `False`：

```python
def _is_expired(self, repo_key: str) -> bool:
    """用户 token 不短路——即使 status=4 也返回 token（R10 best-effort 发评论）。"""
    return False  # 恒 False
```

`TokenStrategy.get_token()` 逻辑是 `_fetch() + _is_expired()` 检查 → 过期则短路返回空串。UserToken 的 `_is_expired()` 恒 False → 永不短路 → 过期 token 照常返回。

**两个设计目标冲突**：
- **回帖路径**（best-effort）：期望拿到过期 token 试发评论（"Token 已过期"提示），发不出就算了——返回过期 token 是对的
- **执行路径**（claim/agent）：期望拿到可用 token 跑 agent——返回过期 token 浪费一整轮 agent + 必然 401

对比其他三层（DevopsAgentSessionToken / RepoBotToken / GlobalBotToken）的 `_is_expired()` 都正确检查过期标记——只有 UserToken 被特殊豁免。

**影响链路**：
1. issue 触发 → pipeline 建 task（token 不落库，正确）
2. claim → `get_task_token(task)` → `get_repo_token_with_refresh` → `TokenResolver.get_token` → UserToken._fetch 返回坏 token + _is_expired=False → 返回坏 token
3. agent 用坏 token 执行 → git clone 401 → fail_task
4. fail_task → detect_and_mark_expired → status 已是 4（幂等，不重复标记）→ 回帖"Token 已过期"
5. 下一个 issue → 回到步骤 1，无限循环

用户视角：token 过期后每个新 issue 仍会完整执行一遍然后失败，直到用户重新绑定。

#### 修复方案：执行链与回帖链语义分离

**UserToken._is_expired()** 改为检查 status=4 返回 True（与其他层一致）：

```python
def _is_expired(self, repo_key: str) -> bool:
    """R25.3: 检查 status=4（token 过期），执行链短路返回空串。"""
    host_user_id = self._get_host_user_id(repo_key)
    if host_user_id == 0:
        return False
    from src.auth.grape_account_db import is_identity_expired
    return is_identity_expired(host_user_id, self.provider.provider_name)
```

效果：`get_token()` 和 `resolve()` 对 status=4 的 UserToken 短路返回空串 → 执行链（claim/agent）拿不到过期 token → task 直接 fail_task + NO_TOKEN_AVAILABLE，不浪费 agent 执行。

**UserTokenHandler.get_comment_token()** 改为用 `_fetch()` 直接读 token（绕过过期检查），保留回帖 best-effort：

```python
def get_comment_token(self, result):
    # R25.3: 回帖链 best-effort——直接用 _fetch 读存储 token（不检查过期）
    if result.provider and result.repo_key:
        try:
            _ut = UserToken(result.provider)
            _tok = _ut._fetch(result.repo_key)  # 绕过 _is_expired，best-effort
            if _tok:
                return _tok
        except Exception:
            pass
    # 用户 token 完全不可用 → fallback 全局 bot token
    if result.provider:
        try:
            from src.user.db import _get_global_bot_token, get_global_github_bot_token
            if result.provider.provider_name == "github":
                return get_global_github_bot_token() or ""
            return _get_global_bot_token() or ""
        except Exception:
            pass
    return ""
```

效果：`post_issue_comment` 和 `fail_task` 回帖链通过 `get_comment_token` → `_fetch()` 直接读 token（不检查过期）→ 拿到过期 token 试发"Token 已过期"评论。发不出（401）时 `post_issue_comment` 的 except 兜底返回 False，不阻断主流程。

**语义分离契约**：

| 调用路径 | 方法 | status=4 行为 | 理由 |
|---------|------|--------------|------|
| 执行链（claim/agent） | `get_token()` / `resolve()` | 短路返回空串 | 过期 token 不可用于执行，避免浪费 agent |
| 回帖链（complete_task/fail_task） | `get_comment_token()` → `_fetch()` | best-effort 返回过期 token | 试发"Token 已过期"评论，发不出就算了 |
| A2A 鉴权 | `read_stored_token()` → `_fetch_stored()` | 返回存储 token（不检查过期） | A2A 鉴权用原始值比对，不需可用 |
| 自循环检测 | `resolve_config_level()` → `_fetch_stored()` | 返回存储 token（不检查过期） | 判断配置级别，不需可用 |

#### 场景用例

##### 场景 45.7：token 过期后新 issue 不再浪费 agent 执行

前置条件：路径 B 仓库 acme/demo（host_user_id=77），用户 OAuth token 已被标记 status=4（过期）。

1. 用户在 issue #42 创建评论触发 pipeline
2. pipeline 建 task（token 不落库，正确）
3. claim → `get_task_token(task)` → `get_repo_token_with_refresh` → `TokenResolver.get_token`
4. UserToken.\_fetch() 返回坏 token → _is_expired() 查 status=4 → 返回 True → **短路返回空串**
5. `get_task_token` 返回空串 → claim 注入空 token
6. **修复前**：agent 拿坏 token 执行 → git clone 401 → fail_task → 浪费一整轮
7. **修复后**：agent 拿空 token → fail_task 直接写 NO_TOKEN_AVAILABLE + 不执行 agent

##### 场景 45.8：token 过期后回帖仍 best-effort 试发

前置条件：同场景 45.7，task fail 后需要发"Token 已过期"评论。

1. fail_task → detect_and_mark_expired → status 已是 4 → 返回 TOKEN_EXPIRED
2. 回帖：`handler.get_comment_token(result)` → `UserToken._fetch(repo_key)` 绕过过期检查 → 返回坏 token
3. `post_issue_comment` 用坏 token 试发评论 → GitCode API 401 → except 兜底返回 False
4. 回帖发不出去（预期行为——过期 token 发不了评论）
5. **但 error_code 已写入 task errors** → dashboard 展示"Token 已过期"→ 用户在 Web UI 看到提示

##### 场景 45.9：用户重新绑定后 token 恢复

前置条件：用户看到"Token 已过期"提示后重新绑定 PAT/OAuth。

1. `create_external_identity` / `update_external_identity_tokens` → status 恢复为 1
2. 下一个 issue 触发 pipeline → claim → `get_task_token` → UserToken._fetch 返回新 token
3. _is_expired() 查 status=1 → 返回 False → 不短路 → 返回新 token
4. agent 正常执行

##### 场景 45.10：PAT 无 refresh_token 过期后的行为

前置条件：路径 B 仓库，用户绑定的 PAT（无 refresh_token）被标记 status=4。

1. PAT 无 refresh_token → `_has_refresh_token` 返回 False → `get_user_token_with_refresh` 直接返回 token 不刷新
2. token 被 401 检测标记 status=4
3. 修复后：执行链短路（_is_expired=True）→ 不浪费 agent
4. 回帖链 best-effort → PAT token 过期发不出评论
5. dashboard 展示"Token 已过期"→ 用户重新绑定

#### 改动文件

| 文件 | 改动 |
|------|------|
| src/token_resolver.py `UserToken._is_expired` | 改为检查 status=4 返回 True（与其他层一致） |
| src/token_resolver.py `UserTokenHandler.get_comment_token` | 改为用 `_fetch()` 直接读 token（绕过过期检查，best-effort） |

#### 向前兼容

- status=1（正常）：_is_expired=False，行为不变 ✅
- status=4（过期）：执行链短路（新行为），回帖链 best-effort（行为不变）✅
- 无 status 列（未迁移）：is_identity_expired 返回 False，行为不变 ✅
- 路径 A 仓库（host_user_id=0）：_is_expired 返回 False（UserToken 不生效），行为不变 ✅

---

## 四十六、第九轮审计：bot_token_health 测试适配 + 401 关键词常量化收尾

> 外部 AI 审计重审发现两个 P1：bot_token_health R17 改造未同步测试（8 failed）、AUTH_401_KEYWORDS 常量定义但零调用点（10 处内联列表未迁移）。

### 46.1 P1：bot_token_health 测试未适配 R17 改造（8 failed）

#### 问题

R17.2.1 将 `FAILURE_THRESHOLD` 从 3 改为 1（1 次 401 即标记 unhealthy），R18.5.8 将 `platform_label` 改为 `provider.capitalize()`。但 `tests/test_bot_token_health.py` 的 8 个用例仍基于旧阈值 3 和旧文案编写：

**两类失败**：

1. **大小写文案回归**（2 failed）：
   - `provider.capitalize()` 产出 "Gitcode" / "Github"
   - 测试断言 `"GitCode" in result["error"]` / `"GitHub" in result["error"]`
   - "Gitcode" 不含 "GitCode"，"Github" 不含 "GitHub" → AssertionError

2. **阈值语义变更未同步测试**（6 failed）：
   - 旧阈值 `FAILURE_THRESHOLD=3`：1-2 次失败 = degraded，3 次 = unhealthy
   - 新阈值 `FAILURE_THRESHOLD=1`：1 次失败 = unhealthy（`failures >= 1`），degraded 永远为 False（`failures < 1` 不可能成立当 failures >= 1 时）
   - 测试断言"首次失败=degraded" → 实际=unhealthy → AssertionError
   - 测试断言"3 次失败=unhealthy" → 实际第 1 次就 unhealthy，consecutive_failures 增长逻辑也变了

#### 修复方案

**46.1a platform_label 修复**（bot_token_health.py L59）：

```python
# 修复前
platform_label = provider.capitalize()

# 修复后
_PLATFORM_LABELS = {"gitcode": "GitCode", "github": "GitHub"}
platform_label = _PLATFORM_LABELS.get(provider, provider.capitalize())
```

**46.1b 测试适配 R17 阈值语义**（test_bot_token_health.py）：

测试用例重写为新阈值语义：
- `FAILURE_THRESHOLD=1`：首次 401 失败 = unhealthy（不是 degraded）
- degraded 语义变更为"仅 network 错误（超时/5xx）首次失败时"（如果需要保留 degraded 中间态）
- 或者：简化为 healthy / unhealthy 二态（R17 设计意图是 1 次 401 即标记，degraded 中间态不再有意义）

选择简化方案：R17 设计意图明确"1 次 401 即标记过期"，degraded 中间态是旧阈值 3 的残留语义。测试改为：
- 首次 401 失败 → unhealthy（不是 degraded）
- consecutive_failures 仍递增（用于日志展示，不影响状态判断）
- `test_first_failure_degraded` → `test_first_failure_unhealthy`
- `test_both_platforms_unhealthy` → 首次失败即 unhealthy，3 次仍是 unhealthy
- `test_config_platform_skipped_other_fails` → 首次失败即 unhealthy
- `test_independent_failure_counters` → 独立计数不变，但状态直接 unhealthy

#### 场景用例

##### 场景 46.1：GitCode bot token 401 后健康检查标记 unhealthy

前置条件：GitCode 全局 bot token 已配置，token 被 GitCode API 返回 401。

1. 健康检查调 `_verify_bot_token("gitcode")` → verify_token 401
2. R21.3.4：401 直接计入 FAILURE_THRESHOLD=1
3. consecutive_failures=1 → `failures >= FAILURE_THRESHOLD` → unhealthy=True, degraded=False
4. 标记 `meta.global_gitcode_bot_token_status = "expired"`
5. 下次 pipeline 取 token 时 GlobalBotToken 策略短路返回空串

##### 场景 46.2：GitHub bot token 超时后不标记 unhealthy

前置条件：GitHub 全局 bot token 已配置，网络超时。

1. 健康检查调 `_verify_bot_token("github")` → verify_token 超时
2. R21.3.4：超时不计入 FAILURE_THRESHOLD
3. consecutive_failures 不变 → healthy 保持（或 degraded 如果需要中间态）
4. 不标记过期

##### 场景 46.3：platform_label 文案正确

1. `_verify_bot_token("gitcode")` 返回 `{"error": "GitCode token not configured"}`
2. `_verify_bot_token("github")` 返回 `{"error": "GitHub token not configured"}`
3. 测试断言 `"GitCode" in result["error"]` / `"GitHub" in result["error"]` 通过

#### 改动文件

| 文件 | 改动 |
|------|------|
| src/runtime/bot_token_health.py L59 | platform_label 改为 `_PLATFORM_LABELS` 映射 |
| tests/test_bot_token_health.py | 8 个用例适配 R17 阈值语义（degraded→unhealthy） |

### 46.2 P1：AUTH_401_KEYWORDS 常量化半成品（10 处内联未迁移）

#### 问题

`token_service.py` L19-31 定义了 `AUTH_401_KEYWORDS` 常量 + `is_auth_401_error()` 函数，但全 `src/` 零调用点。10 处 401 检测仍用内联列表：

| 文件 | 行号 | 内联列表 |
|------|------|---------|
| src/api/routes/chat.py | L707-710, L995-998, L1669-1672, L2130-2133 | 4 处 |
| src/im/handler_async.py | L1962-1963 | 1 处 |
| src/im/vod_handler_async.py | L403-404 | 1 处 |
| src/api/event_pipeline.py | L3679-3680 | 1 处 |
| src/api/routes/events.py | L1831-1832 | 1 处 |
| src/runtime/service.py | L2595-2600 | 1 处 |

半成品比没有更危险：下次补关键词的人会看到常量以为已收敛，实际 10 处内联列表各自独立维护。

#### 修复方案

将 10 处内联 `any(kw in _err_lower for kw in [...])` 替换为 `is_auth_401_error(_err_str)`：

```python
# 修复前（10 处重复）
_is_401 = any(kw in _err_lower for kw in [
    "401 unauthorized", "token invalid", "token expired",
    "authentication failed", "invalid username or token",
    "could not read username", "fatal: authentication",
])

# 修复后
from src.token_service import is_auth_401_error
_is_401 = is_auth_401_error(_err_str)
```

`is_auth_401_error` 内部已做 `.lower()` + `any(kw in ... for kw in AUTH_401_KEYWORDS)`，语义等价。

#### 场景用例

##### 场景 46.4：agent 执行 401 后通过常量统一检测

前置条件：路径 B 仓库，用户 token 过期。

1. agent 执行 git clone → 401 "fatal: Authentication failed for..."
2. chat.py `is_auth_401_error(_err_str)` → "authentication failed" 命中 AUTH_401_KEYWORDS → True
3. 调 `detect_and_mark_expired(repo_key)` → 标记 status=4
4. R25.3 执行链短路 → 下次不浪费 agent

##### 场景 46.5：新增 401 关键词只改一处

1. 某 VCS 平台更新了 401 错误文案，新增 "access token revoked"
2. 修复前：需要 grep 10 处内联列表逐一添加
3. 修复后：只需在 `AUTH_401_KEYWORDS` 列表末尾加一条
4. 所有 10 处调用方自动继承新关键词

#### 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/chat.py L707-710, L995-998, L1669-1672, L2130-2133 | 4 处内联 → `is_auth_401_error()` |
| src/im/handler_async.py L1962-1963 | 1 处内联 → `is_auth_401_error()` |
| src/im/vod_handler_async.py L403-404 | 1 处内联 → `is_auth_401_error()` |
| src/api/event_pipeline.py L3679-3680 | 1 处内联 → `is_auth_401_error()` |
| src/api/routes/events.py L1831-1832 | 1 处内联 → `is_auth_401_error()` |
| src/runtime/service.py L2595-2600 | 1 处内联 → `is_auth_401_error()` |

#### 向前兼容

- `is_auth_401_error` 内部逻辑与原内联列表完全等价（`.lower()` + `any(kw in ...)`） ✅
- 关键词列表不变（7 个关键词原样保留） ✅
- 所有调用方的 `_is_401` 变量语义不变 ✅

---

## 四十七、第十轮：零配置时官方场景/技能同步被"无 token 即跳过"拦截

> 同步源仓库是公开的，匿名即可下载。`_get_sync_token()` 为空时提前 return 跳过整个远程同步，导致首次部署零配置时 scene_market 只有 8 个内置场景、skill 表为空。与 issue-372"零配置可用"目标冲突。

### 47.1 P1：scene_sync + skill_sync 无 token 跳过远程同步

#### 问题

`sync_official_scenes()`（scene_sync.py L309-313）和 `sync_official_skills()`（skill_sync.py L228-231）在 `_get_sync_token()` 返回空时提前 return，跳过整个远程同步。

但同步源仓库 `grape-dev/grape-skills` 和 `grape-dev/skill` 是公开的，匿名即可下载 ZIP：
- `https://raw.gitcode.com/grape-dev/grape-skills/archive/refs/heads/main.zip` → 无 token HTTP 200，1.9MB
- `_download_repo_zip` 已有 `if token: headers["Authorization"] = ...` 守卫——空 token 时匿名请求

**影响**：开发者第一次部署、什么都不配时，skill 表为空、scene_market 只有 8 个内置 featured 场景，官方场景/技能生态全部缺失。

#### 修复方案

去掉两处"空 token 即跳过"的提前 return，空 token 时仍发起匿名下载。下载失败（401/403/网络错误）时降级到 `_init_featured_scenes_if_empty()` 兜底。

**scene_sync.py 修复**：
```python
# 修复前
bot_token = _get_sync_token()
if not bot_token:
    log.warning("... skip remote sync")
    _init_featured_scenes_if_empty()
    return

# 修复后
bot_token = _get_sync_token()
# 公开仓库匿名即可下载，token 非硬前提
async with httpx.AsyncClient(timeout=60.0) as client:
    try:
        repo_root = await _download_repo_zip(client, bot_token)
    except Exception as e:
        log.warning(f"Scene sync: download repo zip failed: {e}")
        _init_featured_scenes_if_empty()  # 下载失败时兜底
        return
```

**skill_sync.py 修复**：同理去掉 `if not token: ... else:` 分支，直接发起下载。

#### 场景用例

##### 场景 47.1：首次部署零配置同步官方场景

前置条件：全新部署，无 GRAPE_DEV_TOKEN 环境变量、无 configs/scene-sync.json。

1. 服务启动 → `sync_official_scenes()` 执行
2. `_get_sync_token()` 返回空串
3. **修复前**：log warning "no GitCode token" → `_init_featured_scenes_if_empty()` → return（仅 8 个内置场景）
4. **修复后**：空 token → `_download_repo_zip(client, "")` → headers 为空 → 匿名下载 ZIP → HTTP 200 → 解压 → 同步 12+ 官方场景到 scene_market
5. scene_market 从 8 行增长到 20+ 行

##### 场景 47.2：私有仓库 401 降级

前置条件：同步源仓库改为私有，无 token。

1. `_download_repo_zip(client, "")` → 匿名请求 → HTTP 401
2. `resp.raise_for_status()` 抛 HTTPStatusError
3. except 捕获 → log warning → `_init_featured_scenes_if_empty()` 兜底
4. scene_market 仍有 8 个内置场景（降级可用）

##### 场景 47.3：有 token 时行为不变

前置条件：配置了 GRAPE_DEV_TOKEN。

1. `_get_sync_token()` 返回 token
2. `_download_repo_zip(client, token)` → headers 含 Authorization → 下载
3. 行为与修复前完全一致

#### 改动文件

| 文件 | 改动 |
|------|------|
| src/storage/scene_sync.py `sync_official_scenes` | 去掉空 token 提前 return，下载失败时降级 |
| src/storage/skill_sync.py `sync_official_skills` | 去掉空 token 提前 return |

#### 向前兼容

- 有 token 时：行为不变（token 传入 `_download_repo_zip`，headers 含 Authorization）✅
- 无 token 时：从"跳过同步"变为"匿名下载"（新行为，修复目标）✅
- 下载失败时：降级到 `_init_featured_scenes_if_empty()`（与原行为一致）✅
- `_get_sync_token` 读取优先级不变（env → configs/scene-sync.json）✅

---

## 四十八、第十一轮：仓库设置页 Bot Token UX 语义误导

> 仓库设置页 GET /api/settings/gitcode-repo/{owner}/{repo} 返回 `devops_bot_token_exists: bool`，前端显示"✓ 已配置"。但该字段通过 `get_repo_token_with_refresh()`（四级 fallback 链）取值——路径 B 仓库（host_user_id > 0）前三级为空时兜底到用户 token，返回 true。用户看到"Bot Token 已配置"但实际仓库级 bot_gitcode_token 列为空，生效的是托管用户的个人 token。

### 48.1 P1：devops_bot_token_exists 语义不精确 + 前端文案误导

#### 问题

**后端**（settings.py L516）：
```python
bot_token = get_repo_token_with_refresh(to_repo_key(f"{owner}/{repo}"))
# ...
"devops_bot_token_exists": bool(bot_token),  # 四级链兜底到用户 token 时也为 true
```

`get_repo_token_with_refresh` 走完整四级 fallback 链（session → repo bot → global bot → user token）。路径 B 仓库前三级为空 → 第 4 级用户 token 非空 → `bool(bot_token) = True`。

**前端**（settings.js L25）：
```javascript
${d.devops_bot_token_exists ? '<span class="ts">✓ 已配置</span>' : ''}
// placeholder: "已配置，留空保持不变"
```

文案"Bot Token 已配置"暗示用户填过的仓库级 bot token 存在，实际是解析链兜底到了用户个人 token。用户困惑："我没配过仓库 token，为什么说已配置？"

**根因**：`TokenResolver.resolve()` 已返回 `level` 信息（session/repo_bot/global_bot/user），但后端只取 `bool(token)` 丢弃了 level，前端没用 level 做分级展示。

#### 修复方案：后端返回 resolved_token_level + 前端分级文案

**后端**（settings.py）：
```python
from src.token_resolver import TokenResolver, create_provider_from_repo_key
_rk = to_repo_key(f"{owner}/{repo}")
_provider = create_provider_from_repo_key(_rk)
_result = TokenResolver(_provider).resolve(_rk)
bot_token = _result.token
# ...
return {
    "devops_bot_token_exists": bool(bot_token),
    "resolved_token_level": _result.level,  # "session"/"repo_bot"/"global_bot"/"user"/""
    "bot_token_status": _bot_token_status,
    # ...
}
```

**前端**（settings.js）：
```javascript
// 根据 resolved_token_level 分级显示
var _level = d.resolved_token_level || '';
var _tokenLabel = '';
if (_level === 'repo_bot') {
    _tokenLabel = I18N.t('settings.credentials.bot_configured');  // "✓ 已配置"
} else if (_level === 'user') {
    _tokenLabel = I18N.t('settings.credentials.user_token_active');  // "用户 Token 驱动中"
} else if (_level === 'global_bot') {
    _tokenLabel = I18N.t('settings.credentials.global_bot_active');  // "全局 Bot Token 生效中"
} else if (_level === 'session') {
    _tokenLabel = I18N.t('settings.credentials.session_token_active');  // "会话 Token 生效中"
}
```

placeholder 也分级：
- repo_bot: "已配置，留空保持不变"
- user: "用户 Token 驱动中，无需单独配置"
- global_bot: "使用全局 Bot Token，无需单独配置"
- 空: "不设置，将默认使用 Grape 官方机器人"

**新增 i18n key**（zh/en settings.json）：
- `user_token_active`: "用户 Token 驱动中" / "User token active"
- `global_bot_active`: "全局 Bot Token 生效中" / "Global bot token active"
- `session_token_active`: "会话 Token 生效中" / "Session token active"

#### 场景用例

##### 场景 48.1：路径 B 仓库设置页显示"用户 Token 驱动中"

前置条件：路径 B 仓库 acme/demo（host_user_id=2，bot_gitcode_token 为空），用户已绑定 PAT。

1. 用户打开仓库设置页 → `GET /api/settings/gitcode-repo/acme/demo`
2. 后端 `TokenResolver.resolve()` → level="user", token="用户PAT"
3. 返回 `{devops_bot_token_exists: true, resolved_token_level: "user"}`
4. 前端显示"用户 Token 驱动中"（非"✓ 已配置"）
5. placeholder: "用户 Token 驱动中，无需单独配置"
6. 用户明白当前生效的是自己的 token，不需要单独配仓库级 bot token

##### 场景 48.2：路径 A 仓库设置页显示"✓ 已配置"

前置条件：路径 A 仓库 acme/demo（bot_gitcode_token 非空）。

1. `TokenResolver.resolve()` → level="repo_bot", token="仓库bot token"
2. 返回 `{devops_bot_token_exists: true, resolved_token_level: "repo_bot"}`
3. 前端显示"✓ 已配置"
4. placeholder: "已配置，留空保持不变"
5. 用户看到的是真正的仓库级 bot token 状态

##### 场景 48.3：无 token 仓库设置页显示空

前置条件：仓库 acme/demo 未托管（无 managed_repos 记录）。

1. `TokenResolver.resolve()` → level="", token=""
2. 返回 `{devops_bot_token_exists: false, resolved_token_level: ""}`
3. 前端不显示配置标签
4. placeholder: "不设置，将默认使用 Grape 官方机器人"

##### 场景 48.4：全局 Bot Token 环境下路径 A 仓库

前置条件：全局 bot token 已配置，仓库 bot_gitcode_token 为空，host_user_id=0。

1. `TokenResolver.resolve()` → level="global_bot", token="全局bot token"
2. 返回 `{devops_bot_token_exists: true, resolved_token_level: "global_bot"}`
3. 前端显示"全局 Bot Token 生效中"
4. placeholder: "使用全局 Bot Token，无需单独配置"

#### 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/settings.py `get_gitcode_repo_config` | 用 `TokenResolver.resolve()` 替换 `get_repo_token_with_refresh`，返回 `resolved_token_level` |
| src/api/static/js/pages/settings.js | 根据 `resolved_token_level` 分级显示文案 |
| src/api/static/locales/zh/settings.json | 新增 `user_token_active` / `global_bot_active` / `session_token_active` |
| src/api/static/locales/en/settings.json | 同上 |

#### 向前兼容

- `devops_bot_token_exists` 字段保留（bool 语义不变），前端优先用 `resolved_token_level` 分级，旧前端仍读 bool ✅
- 路径 A 仓库（repo_bot）：显示不变（"✓ 已配置"）✅
- 无 token 仓库：显示不变（空）✅
- 路径 B 仓库（user）：从"✓ 已配置"变为"用户 Token 驱动中"（修复目标）✅

---

## 四十九、第十二轮：R25.3 回帖链断裂 + session 明文 token 外发

> 外部 AI 审计重审发现两个新 P1：R25.3 修复引入回帖链断裂（resolve 过期短路切断了 best-effort 回帖）、ensure_default_agent_sessions 把用户 OAuth token 明文写入 session 行且 agents API 原样外发。

### 49.1 P1：R25.3 短路把回帖链也切断了

#### 问题

R25.3 将 `UserToken._is_expired()` 改为检查 status=4 返回 True（执行链短路）。但 `complete_task`（service.py L2156）和 `fail_task`（L2614, L2706）的回帖链调的是 `resolve()`（含过期短路）→ status=4 时 UserToken 被短路 → `result.level=""` → `create_action_handler` 返回 None → 回帖分支进 `elif _token:` 而 `_get_task_token` 也被短路为空 → 什么都不发。

设计文档场景 45.8 明确承诺"回帖仍 best-effort 试发"，但实现与文档矛盾。

#### 修复方案

回帖链的 3 处 `resolve()` 改为 `resolve_config_level()`（不走过期短路，保留 level 信息）：

| 行号 | 函数 | 修复 |
|------|------|------|
| L2156 | `complete_task` | `resolve()` → `resolve_config_level()` |
| L2614 | `fail_task` R10 回帖 | 同上 |
| L2706 | `fail_task` 去重回帖 | 同上 |

`resolve_config_level()` 用 `_fetch_stored()` 读取 token（不检查过期、不触发 refresh），返回正确的 level="user" → `create_action_handler` 创建 UserTokenHandler → `get_comment_token()` 用 `_fetch()` 绕过过期检查 → 返回过期 token → best-effort 试发评论。

#### 场景用例

##### 场景 49.1：token 过期后回帖仍 best-effort 试发

前置条件：路径 B 仓库，用户 token status=4（过期）。

1. task fail → `detect_and_mark_expired` → 返回 TOKEN_EXPIRED
2. 回帖：`resolve_config_level(_rk)` → level="user"（不过期短路）
3. `create_action_handler` → UserTokenHandler
4. `get_comment_token` → `_fetch()` → 返回过期 token
5. `post_issue_comment` 用过期 token 试发 → GitCode API 401 → except 返回 False
6. 回帖发不出（预期），但 error_code 已写入 task errors → dashboard 展示

#### 改动文件

| 文件 | 改动 |
|------|------|
| src/runtime/service.py L2156, L2614, L2706 | `resolve()` → `resolve_config_level()` |

### 49.2 P1：session 行用户 token 明文写入 + agents API 外发

#### 问题

`ensure_default_agent_sessions`（session_store.py L531）用 `read_stored_token()` 取用户 token 写入 `devops_agent_session.repo_gitcode_token`。两个问题：

1. **过期/换绑后 session 残留旧值**：用户重绑新 token 后 session 行还是旧值，靠各处 R22.2 守卫兜底——守卫漏一处就吃旧 token
2. **agents API 原样外发**：agents.py L222/366 把 `repo_gitcode_token` 明文放进 API 响应——等于把用户 OAuth 凭据暴露给所有能看该 API 的用户

#### 修复方案

**session_store.py**：路径 B 仓库（level=user）时 session 行写空：

```python
_token = ""
if repo_key:
    _resolve = TokenResolver(...).resolve_config_level(repo_key)
    if _resolve.level != "user":  # 非 path B 才写入
        _token = _resolve.token
```

**agents.py**：token 脱敏为布尔值：

```python
# L222: "repo_gitcode_token": row[4] or ""  →  "repo_gitcode_token_exists": bool(row[4])
# L366: "repo_gitcode_token": session_data.get(...)  →  "repo_gitcode_token_exists": bool(...)
```

#### 场景用例

##### 场景 49.2：路径 B 仓库 session 行不写入用户 token

前置条件：路径 B 仓库 acme/demo（host_user_id=2）。

1. 托管时 `ensure_default_agent_sessions` → `resolve_config_level` → level="user"
2. `_token = ""`（不写入用户 token）
3. session 行 `repo_gitcode_token` 为空
4. 执行链实时取（claim 注入 + `_get_task_token`）

##### 场景 49.3：agents API 不外发 token 明文

1. GET /api/agents → 返回 `repo_gitcode_token_exists: true/false`
2. 不返回 `repo_gitcode_token: "用户PAT明文"`
3. 前端无引用（grep 确认零消费）

#### 改动文件

| 文件 | 改动 |
|------|------|
| src/api/session_store.py `ensure_default_agent_sessions` | 路径 B 仓库 session 行写空 |
| src/api/routes/agents.py L222, L366 | `repo_gitcode_token` → `repo_gitcode_token_exists`（布尔） |

#### 向前兼容

- 路径 A 仓库（level=repo_bot/global_bot）：session 行仍写入 bot token（不变）✅
- agents API 字段名变更：前端零消费（grep 确认），不影响 ✅

---

## 五十、第十三轮：设置页显示双维度——配置状态 + 生效级别

> R25.7 修复了"没配也说已配置"（显示 resolved_token_level 而非布尔）。但用户配了仓库 token 后，ensure_default_agent_sessions 把 token 复制到 session 层 → resolve() 第 1 级命中 → level="session" → 前端显示"Session token 生效中"，而非"✓ 已配置"。用户困惑"我配了仓库 token，为什么显示 session"。

### 50.1 P1：设置页只显示生效级别，不显示配置状态

#### 问题

用户在设置页配了仓库 token（`managed_repos.bot_gitcode_token` 非空），保存后：
1. `ensure_default_agent_sessions` 把 token 复制到 `devops_agent_session.repo_gitcode_token`（session 层）
2. 下次 `resolve()` 第 1 级 DevopsAgentSessionToken 命中 → level="session"
3. 前端显示"Session token 生效中"（R25.7 新增的分级文案）

用户期望看到"仓库 token 已配置"（回答"我配了什么"），实际看到"Session token 生效中"（回答"现在用哪级"）。两个问题用户都关心，但 UI 只回答了后者。

#### 修复方案：双维度显示

**后端**（settings.py `get_gitcode_repo_config`）：
新增 `has_repo_bot_token` 字段——直接查 `managed_repos.bot_gitcode_token` 列是否非空（不走 resolver 链）：

```python
_has_repo_bot_token = False
try:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT bot_gitcode_token FROM managed_repos WHERE repo_key=? LIMIT 1", (_rk,))
        _row = c.fetchone()
        if _row and _row[0]:
            _has_repo_bot_token = True
except Exception:
    pass

return {
    "devops_bot_token_exists": bool(bot_token),  # 向前兼容
    "has_repo_bot_token": _has_repo_bot_token,   # R25.9: 仓库级 token 配置状态
    "resolved_token_level": _resolved_level,      # 生效级别
    # ...
}
```

**前端**（settings.js）：
- 优先显示配置状态：`has_repo_bot_token` → "✓ 已配置"（绿色）
- 附带显示生效级别：`resolved_token_level` → 小字说明（"Session 层生效" / "用户 Token 驱动中" / "全局 Bot 生效中"）

```javascript
// 配置状态标签（回答"我配了什么"）
var _configLabel = '';
if (d.has_repo_bot_token) {
    _configLabel = '<span style="color:var(--g)">✓ 已配置</span>';
} else if (d.resolved_token_level === 'user') {
    _configLabel = '<span style="color:var(--a)">用户 Token 驱动中</span>';
} else if (d.resolved_token_level === 'global_bot') {
    _configLabel = '<span style="color:var(--a)">全局 Bot 生效中</span>';
}

// 生效级别说明（小字，回答"现在用哪级"）
var _levelHint = '';
if (d.has_repo_bot_token && d.resolved_token_level === 'session') {
    _levelHint = '<div style="font-size:10px;color:var(--t3)">Session 层生效（仓库 token 已同步到会话）</div>';
} else if (d.has_repo_bot_token && d.resolved_token_level === 'repo_bot') {
    _levelHint = '<div style="font-size:10px;color:var(--t3)">仓库级生效</div>';
}
```

#### 场景用例

##### 场景 50.1：配了仓库 token 后显示"✓ 已配置"+ 生效说明

前置条件：用户在设置页配了仓库 token，ensure_default_agent_sessions 已同步到 session 层。

1. `GET /api/settings/gitcode-repo/owner/repo`
2. `has_repo_bot_token=true`（bot_gitcode_token 列非空）
3. `resolved_token_level="session"`（第 1 级命中）
4. 前端显示："✓ 已配置"（绿色）+ 小字"Session 层生效（仓库 token 已同步到会话）"
5. 用户看到"已配置"→ 明确知道自己的仓库 token 生效了

##### 场景 50.2：路径 B 仓库显示"用户 Token 驱动中"

前置条件：路径 B 仓库（host_user_id > 0，bot_gitcode_token 为空）。

1. `has_repo_bot_token=false`
2. `resolved_token_level="user"`
3. 前端显示："用户 Token 驱动中"
4. 用户知道当前用的是自己的 token

##### 场景 50.3：未配仓库 token 但有全局 bot token

前置条件：bot_gitcode_token 为空，全局 bot token 有值，host_user_id=0。

1. `has_repo_bot_token=false`
2. `resolved_token_level="global_bot"`
3. 前端显示："全局 Bot 生效中"
4. 用户知道用的是全局 bot token

#### 改动文件

| 文件 | 改动 |
|------|------|
| src/api/routes/settings.py `get_gitcode_repo_config` | 新增 `has_repo_bot_token` 字段（直接查 DB 列） |
| src/api/static/js/pages/settings.js | 双维度显示：配置状态（优先）+ 生效级别（小字） |
| src/api/static/locales/zh/settings.json | 新增 `session_level_hint` / `repo_bot_level_hint` |
| src/api/static/locales/en/settings.json | 同上 |

#### 向前兼容

- `devops_bot_token_exists` 和 `resolved_token_level` 字段保留 ✅
- `has_repo_bot_token` 是新增字段，旧前端忽略它 ✅
- 路径 A 仓库配了 token：从"Session token 生效中"变为"✓ 已配置 + Session 层生效"✅
- 路径 B 仓库：显示不变（"用户 Token 驱动中"）✅
- 执行链不受影响（实时取 token）✅
