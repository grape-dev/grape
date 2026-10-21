# 需求：去全局 Bot Token，用户级 Token 驱动全链路

> 状态：需求文档（R1-R14 已实现，代码已验证）
> 基线：weekly-07-clean
> 关联：#343 双轨 OAuth token 架构、#345 bot token 单点故障、#356 配置向导
> 前置：P2 改造已完成（agent env 不再注入 VCS token，统一走 GRAPE_SESSION_TOKEN + grape CLI 转发）
>
> **编号归属声明（A1 勘误）**：GitCode #372 实际标题为「知识库知识大小限制 10K→可配置默认 5M」，
> 由 feat/issue-372-knowledge-5mb-limit 分支实现。本分支 token 主题占用 #372 编号为历史遗留，
> 实际对应的需求追溯应关联 #343/#345/#356。合并时以此声明为准。
>
> **文档线关系（A2 裁决）**：本文档为已实现主线（R1-R14）。
> `repo-token-config-requirements/design.md`（R15-R21）为未实现的设计提案支线。
> 两条文档线的自循环检测方案不矛盾而是互补——主线 R4 标记优先（已实现）是第一道防线，
> 支线 R20.1 四条件组合判断是标记失效时的兜底（agent 绕过 grape CLI 直接调 VCS API 时标记缺失）。
> 两者分层关系：pipeline 步骤 2.0a 标记检测 → 2.0 自循环检测（先标记后四条件）。
>
> **status 值勘误（A3）**：本文档 R10.1 原写 `status=0`，实际代码（service.py:2491）用 `status=4`。
> `external_identities.status` 语义：0=解绑，1=活跃，4=token 过期。
> R10.1 正文应为 `status=4`（token 过期），非 `status=0`（解绑）。

---

## 一、背景

### 1.1 现状

grape 平台当前有三类 GitCode/GitHub token：

| Token 类型 | 存储位置 | 来源 | 过期/刷新 | 用途 |
|-----------|---------|------|----------|------|
| 全局 Bot Token (PAT) | DB meta `global_bot_gitcode_token` | admin 手动配置 | 不过期、不刷新 | 托管建 webhook、pipeline VCS 操作、agent git 操作、A2A 鉴权 |
| 仓库级 Bot Token | `managed_repos.bot_gitcode_token` | 托管时复制全局 bot token | 同全局 | per-repo 覆盖（实际=全局副本） |
| 用户级 Token (OAuth) | `external_identities.access_token`（加密） | 用户 OAuth 登录绑定 | 过期（最短 15min）、自动刷新 | chat 任务、仓库列表同步 |

### 1.2 问题

**P0-1：全局 Bot Token 是单点依赖**

托管仓库时硬校验 `if not global_bot_token: raise 503`。没有全局 bot token 就不能托管，系统完全跑不起来。外部开发者部署后必须先找 admin 配 PAT，与 #356 "快速上手" 目标矛盾。

**P0-2：仓库级 Token 是全局 Token 的副本**

`host_repo` 写入 `bot_gitcode_token=global_bot_token`，三级 fallback 最终都指向同一个 token。仓库级配置名存实亡——改全局 token 所有仓库跟着变，无法 per-repo 独立配置。

**P1-1：Bot Token 无刷新机制**

`get_gitcode_token` 直接从 DB 读，不检查过期。PAT 理论上不过期，但如果管理员手动撤销，整个系统所有仓库的 pipeline 操作全部 401，无自愈能力。

**P1-2：自循环检测强依赖 Bot 用户名**

pipeline 用 `actor in bot_usernames` 判断是否跳过 bot 回声。如果 agent 用其他 token 发评论，第一层检测失效。虽有 `<!-- grape-auto-comment -->` 标记作为第二层，但 agent CLI 自己用 curl 发的评论不一定带此标记。

**P1-3：托管流程多余复杂度**

当前流程：用户 token 邀请 bot → bot 接受邀请 → bot 自查权限 → bot 建 webhook。如果直接用用户 token 建 webhook，不需要邀请/接受/自查三步，也不需要全局 bot 用户。

**P2-1：A2A 鉴权混用两种 token 体系**

`_authorize_a2a` 支持 bot token 和 session token 二选一。如果去掉 bot token，A2A 只走 session token 通道——功能不丢，但关联仓库信任域的 "same bot token" 检查会失效。

---

## 二、目标

保留全局 Bot Token 作为优先选项，未配置全局 Bot Token 时自动降级到用户级 Token 驱动全链路。

**核心策略：全局 Bot 优先，用户 Token 降级。**

- 已配置全局 Bot Token 的环境：完全不变，走原链路（零影响）
- 未配置全局 Bot Token 的环境：自动降级到用户 Token（OAuth 登录或 PAT 直接绑定，即可托管仓库、跑通全链路）

这样外部开发者部署后无需 admin 预配 PAT，用户通过 OAuth 登录或直接输入 GitCode PAT 绑定即可跑通全链路（VOD → Issue → Coding → QA → SRE）。

**用户 Token 绑定有两种方式（二选一）：**
- OAuth 授权流程：需要 admin 配置 OAuth App（client_id + client_secret），用户跳转授权页绑定
- PAT 直接绑定：不需要 OAuth App，用户在设置页输入自己的 GitCode PAT 直接绑定

**最终效果：有公网 IP 的环境下，零 admin 配置，用户只需一个 GitCode PAT 即可跑通全链路。**

### 2.1 非目标

- 不改 scene_sync 的 token 体系（`scene_sync_token` / `GRAPE_DEV_TOKEN` 是平台级配置，拉官方场景仓库，与用户 token 无关）
- OAuth App 配置改为可选（配了走 OAuth 授权流程，没配走 PAT 直接绑定）
- 不删除全局 Bot Token 能力（保留为优先选项，不废弃）

---

## 三、需求清单

### R1：托管流程支持无全局 Bot Token 降级

**R1.1：托管流程新增用户 Token 降级路径**

当前 `host_repo` 流程：
1. 用户 token 验证身份 + 权限
2. 全局 bot token 邀请 bot 为维护者（硬依赖，无则 503）
3. 全局 bot token 接受邀请
4. 全局 bot token 自查权限
5. 全局 bot token 创建 webhook
6. 写入 managed_repos（bot_gitcode_token=全局 bot token）

改为**分两条路径**：

**路径 A（有全局 Bot Token，向前兼容）**：步骤不变，走原流程。

**路径 B（无全局 Bot Token，降级）**：
1. 用户 token 验证身份 + 权限（不变）
2. 跳过 bot 协作者邀请/接受/自查（不需要 bot 用户）
3. 用户 token 直接创建 webhook
4. 写入 managed_repos（host_user_id=当前用户，bot_gitcode_token=''）

路径选择逻辑：`global_bot_token = get_global_gitcode_bot_token(); if global_bot_token: 走 A else: 走 B`

**R1.4：GitHub 托管路径同步支持路径 B**

GitHub 仓库托管路径（host_repo GitHub 分支）当前 webhook 已用用户 token 创建（`setup_github_webhook(api_token, ...)`），但 `_try_github_bot_collaborator` 用全局 GitHub bot token，无全局 bot 时会失败。

改为：无全局 GitHub bot token 时跳过 bot 协作者流程，webhook 用用户 token 创建 + 随机密钥验证（与 GitCode 路径 B 一致）。

**R1.5：GitHub host_repo 传 webhook_secret 给 setup_github_webhook**

当前 GitHub 路径 B 生成了 `_gh_webhook_secret` 存入 managed_repos，但 `setup_github_webhook(api_token, owner, repo_name, webhook_url)` 调用没传 webhook_secret 参数。GitHub webhook 的 HMAC 签名验证用创建时传的 secret，没传则默认用 api_token（用户 token），token 刷新后签名验证失败。

改为：`setup_github_webhook(api_token, owner, repo_name, webhook_url, webhook_secret=_gh_webhook_secret)`，路径 B 传随机密钥，路径 A 传空（走默认 api_token）。

**R1.6：create_from_template 托管路径支持路径 B**

`create_from_template`（创建仓库后自动托管）是独立于 host_repo 的托管入口。当前：
- GitCode 路径：`webhook_token_for_db = _create_global_bot_token or api_token`，无全局 bot 时用用户 token，但没生成 webhook_secret、没写 host_user_id
- webhook_url 硬校验 `if not webhook_url: raise 400`，自动检测失败时直接报错

改为：与 host_repo 一致：
- 无全局 bot token 时走路径 B（用户 token 建 webhook + 随机密钥 + host_user_id）
- webhook_url 硬校验改为：无 webhook_url 时不阻断创建，只跳过托管（返回"仓库已创建，但 webhook_url 未配置，请稍后手动托管"）

**R1.2：去掉全局 bot token 硬校验**

`host_repo` 中 `if not global_bot_token: raise 503` 改为：无全局 bot token 时走路径 B，不报错。

**R1.3：managed_repos 新增 host_user_id 字段**

```sql
ALTER TABLE managed_repos ADD COLUMN host_user_id INTEGER DEFAULT 0;
```

- 路径 A 托管：host_user_id=0（走原 bot token 链路）
- 路径 B 托管：host_user_id=当前用户 ID（走用户 token 链路）

### R2：Token 获取链路改为"全局 Bot 优先，用户 Token 降级"

**R2.1：新增 get_repo_token_with_refresh 函数**

统一 token 获取入口，封装降级逻辑：

```python
def get_repo_token_with_refresh(repo_key: str) -> str:
    """获取仓库操作 token：全局 Bot 优先，用户 Token 降级。

    1. managed_repos.bot_gitcode_token（仓库级 Bot Token，路径 A 托管写入）
    2. _get_global_bot_token()（全局 Bot Token）
    → 以上任一有值则返回（走 Bot Token 链路，不刷新）

    3. managed_repos.host_user_id → get_user_token_with_refresh(username, host_user_id)
    → 用户 Token 路径，带自动刷新
    """
    # 1. 仓库级 Bot Token
    tok = get_bot_token_by_repo(owner, repo)
    if tok:
        return tok
    # 2. 全局 Bot Token
    tok = _get_global_bot_token()
    if tok:
        return tok
    # 3. 用户 Token 降级
    host_user_id = get_host_user_id(repo_key)
    if host_user_id > 0:
        username = get_username_by_user_id(host_user_id)
        return get_user_token_with_refresh(username, user_id=host_user_id) or ""
    return ""
```

**关键：全局 Bot Token 优先，不触发用户 Token 路径。只有全局和仓库级 Bot Token 都为空时，才降级到用户 Token（带刷新）。**

**R2.2：pipeline 中所有 token 获取点统一替换**

| 当前调用 | 改为 |
|---------|------|
| `get_gitcode_token(repo_key=repo_key)` | `get_repo_token_with_refresh(repo_key)` |
| `get_agent_bot_token(repo_key)` | `get_repo_token_with_refresh(repo_key)` |
| `Repo(repo_key).bot_token` | `get_repo_token_with_refresh(repo_key)` |
| `task.repo_gitcode_token`（task 字段） | 运行时通过 `get_repo_token_with_refresh(repo_key)` 实时获取 |

**向前兼容**：`get_gitcode_token` / `get_repo_bot_token` / `get_agent_bot_token` 内部改为调 `get_repo_token_with_refresh`（统一入口），所有未显式替换的调用方自动走降级链路。

**R2.3：get_repo_token_with_refresh 必须包含 devops_agent_session 层**

原 get_gitcode_token 三级 fallback：devops_agent_session → managed_repos → 全局。get_repo_token_with_refresh 必须保留全部三级，不能漏 devops_agent_session：

```
1. devops_agent_session.repo_gitcode_token（agent 专属配置）
2. managed_repos.bot_gitcode_token（仓库级 Bot Token）
3. _get_global_bot_token()（全局 Bot Token）
→ 以上任一有值则返回（不刷新）
4. managed_repos.host_user_id → get_user_token_with_refresh（用户 Token 降级）
```

**R2.4：task 存储改为 repo_key + user_id**

当前 task 行存 `repo_gitcode_token`（明文 token 副本）。改为：
- task 行仍存 `repo_gitcode_token`（向前兼容，路径 A 写入 bot token）
- 路径 B 托管的 task：`repo_gitcode_token=''`，运行时通过 `get_repo_token_with_refresh(repo_key)` 实时获取
- `runtime/agent.py` 中 `task.get("repo_gitcode_token")` 改为：先读 task 字段，为空时调 `get_repo_token_with_refresh(repo_key)`

**关键实现细节**：event_pipeline 中 task 创建时（_create_direct_task / _create_failed_task / _enqueue_pipeline_task 三处）必须区分 token 来源——get_repo_token_with_refresh 返回的 token 如果来自路径 D（用户 OAuth token，会过期），写入 task 行会导致排队较久的 task 拿到过期 token。区分方式：调 get_repo_token_with_refresh 后检查 host_user_id，若 host_user_id > 0 且前三级（devops_agent_session / managed_repos.bot_gitcode_token / 全局 bot）都为空，说明返回的是用户 token，task 行写空由运行时实时获取。

**R2.5：get_github_token 加用户 token 降级**

当前 `get_github_token` fallback 链：managed_repos.bot_github_token → 仓库 owner 的 external_identities → 全局。没有 host_user_id 降级路径。GitHub 仓库路径 B 托管后 host_user_id 写入了，但 get_github_token 不读它，pipeline 里 GitHub 仓库的 token 获取会返回空。

改为：fallback 链末尾加 `host_user_id → get_user_github_token_with_refresh`（与 get_repo_bot_token 的用户 token 降级一致）。

**R2.6：get_repo_bot_token 保留原语义 + 用户 token 降级**

`get_repo_bot_token` 是 Issue #343 建单/关单/评论等用户交互写操作的 token 入口，设计意图是**跳过 devops_agent_session**（agent 专属 token scope 可能受限，不适合用户交互写操作）。

改为：保留原语义（跳过 devops_agent_session），fallback 链末尾加用户 token 降级：
1. managed_repos.bot_gitcode_token（仓库级 Bot Token，不跳——这不是 agent session）
2. _get_global_bot_token()（全局 Bot Token）
3. managed_repos.host_user_id → get_user_token_with_refresh（用户 Token 降级，路径 B）

**不委托 get_repo_token_with_refresh**（后者包含 devops_agent_session 层，会破坏 #343 语义）。

### R3：Webhook 验证改为不依赖 Token 明文

**R3.1：webhook password 改为固定密钥**

当前 webhook 创建时 `webhook_password=bot_token`，验证时对比 `X-GitCode-Token` header 与 managed_repos 存的 token。

问题：用户 token 会刷新变化，webhook password 不会自动更新。

改为：webhook 创建时生成随机密钥（`secrets.token_urlsafe(32)`），存入 `managed_repos.webhook_secret`，与 token 解耦。GitCode 推 webhook 时带的 `X-GitCode-Token` 就是这个固定密钥。

```sql
ALTER TABLE managed_repos ADD COLUMN webhook_secret TEXT DEFAULT '';
```

**R3.2：get_webhook_token_by_repo 改造**

改为读 `managed_repos.webhook_secret`，不再读 `gitcode_webhook_token`（向前兼容：webhook_secret 为空时 fallback 到原字段）。

### R4：自循环检测支持用户 Token 降级场景

**背景**：全局 Bot Token 场景下，agent 发评论 actor=bot 用户名，第一层检测（`actor in bot_usernames`）有效。用户 Token 降级场景下，actor=用户名不在 bot_usernames 里，第一层检测失效。

**R4.1：agent 评论统一加 grape-auto-comment 标记**

当前 `_post_result_to_issue`（系统补发评论）已加 `<!-- grape-auto-comment -->` 标记。但 agent CLI 自己用 curl/grape CLI 发的评论不一定带此标记。

要求：agent 通过 grape CLI 发评论时，CLI 自动追加 `<!-- grape-auto-comment -->` 标记（HTML 注释，用户不可见，pipeline 可检测）。

改动点：`grape issues comment` 命令实现中，在 body 末尾自动追加标记。

**R4.2：pipeline 自循环检测调整为标记优先**

当前检测顺序：
1. `actor in bot_usernames` → 跳过（self_loop）
2. `<!-- grape-auto-comment -->` in note → 跳过（system_auto_comment）

改为：
1. `<!-- grape-auto-comment -->` in note → 跳过（标记优先，不依赖 actor）
2. `actor in bot_usernames` → 跳过（向前兼容，全局 Bot 场景仍有效）

两条路径都覆盖：全局 Bot 场景 actor=bot 用户名被第 2 层拦截；用户 Token 降级场景评论带标记被第 1 层拦截。

### R5：A2A 鉴权支持用户 Token 降级场景

**R5.1：_authorize_a2a 保留 bot token 通道，新增 session token 兜底**

当前已支持 bot token 和 session token 二选一。全局 Bot Token 场景下 bot token 通道仍有效，无需改动。

用户 Token 降级场景下：managed_repos 无 bot_gitcode_token，bot token 通道返回空 → 自动走 session token 通道（已有逻辑）。**当前代码已支持此降级，无需改动。**

**R5.2：关联仓库信任域新增 host_user_id 判断**

当前 "same bot token" 信任域：两个仓库的 `bot_gitcode_token` 相同 → 信任。

用户 Token 降级场景下两仓 bot_gitcode_token 都为空，"same bot token" 判断失效。新增判断：两个仓库的 `host_user_id` 相同 → 信任。

改动：`_authorize_a2a` 中 trust domain 检查新增 `host_user_id` 比对分支，与原 bot token 比对并列（任一满足即信任）。

### R6：托管修复流程（repair_host）同步改造

**R6.1：repair_host 支持路径 B 修复**

当前 repair_host 逻辑：owner token 邀请 + bot token 建 webhook。

改为：与 host_repo 一致，检测有全局 bot token 走原路径 A 修复，无全局 bot token 走路径 B（用户 token 检查/重建 webhook）。

**R6.2：repair_host 路径 B 找到已有 webhook 时重建密钥**

路径 B 场景 find_existing_webhook 找到已有 webhook 时，旧 webhook 可能用旧密钥（路径 A 的 bot token 或之前的随机密钥）。repair_host 生成新 _webhook_secret 存入 DB，但 webhook 实际验证用的是创建时的密钥——不匹配会导致验证失败。

改为：路径 B 场景找到已有 webhook 时，先删除旧 webhook 再用新密钥重建（确保 DB 密钥与 webhook 实际密钥一致）。路径 A 场景不受影响。

### R7：无邮箱配置场景适配

**背景**：当前注册/忘记密码/改邮箱三个流程都硬依赖邮箱验证码。系统未配置邮箱服务（SMTP/Resend）时这三个功能全部走不通。外部开发者部署后如果没有邮箱服务，用户无法注册。

**策略**：`is_email_configured()` 返回 False 时前端隐藏邮箱相关 UI，后端跳过验证码。返回 True 时走原流程不变。

**两个场景的行为对比：**

| 功能点 | 有邮箱配置（is_email_configured=True） | 无邮箱配置（is_email_configured=False） |
|--------|--------------------------------------|---------------------------------------|
| 注册页邮箱输入框 | 显示 | 隐藏 |
| 注册页验证码区域 | 显示 | 隐藏 |
| 注册流程 | 邮箱 → 发验证码 → 校验验证码 → 创建账号 | 用户名+密码 → 直接创建账号（email 为空） |
| 注册 send-code | 发验证码到邮箱 | 返回提示"邮箱服务未配置" |
| 注册 verify | 校验验证码 | 跳过验证码校验 |
| 忘记密码链接 | 显示 | 隐藏 |
| 忘记密码 send-code | 发验证码到邮箱 | 返回 503 |
| 设置页邮箱管理卡片 | 显示 | 隐藏 |
| 登录页 OAuth 按钮 | 始终显示（已配 → OAuth 授权登录；未配 → 提示用户名+密码注册后绑定 token） | 始终显示（同左） |
| 绑定弹窗 OAuth tab | 显示（需 OAuth App 已配） | 不显示（只显示手动令牌输入） |

**向前兼容**：已配置邮箱的环境 `is_email_configured()` 返回 True，所有行为与改造前完全一致。

**R7.1：注册流程无邮箱降级**

当前注册流程强制：邮箱输入 → 发验证码 → 验证码校验 → 创建账号。

无邮箱配置时改为：用户名 + 密码直接注册，跳过邮箱和验证码步骤。

- 前端 register.html：根据 `is_email_configured` 动态显示/隐藏邮箱输入框和验证码区域。无邮箱时只显示用户名 + 密码
- 后端 `/auth/register/verify`：无邮箱配置时跳过验证码校验，email 字段允许为空
- 后端 `/auth/register/send-code`：无邮箱配置时返回提示"邮箱服务未配置，请直接填写用户名和密码注册"
- `create_grape_account`：email 为空时允许创建（email 字段 nullable）

**R7.2：忘记密码无邮箱时隐藏入口**

当前 forgot.html 强制邮箱 + 验证码重置密码。无邮箱配置时验证码发不出去。

- 前端登录页：无邮箱配置时不显示"忘记密码"链接
- 后端 `/auth/forgot-password/send-code`：无邮箱配置时返回 503 "邮箱服务未配置，请联系管理员重置密码"

**R7.3：用户设置页无邮箱时隐藏邮箱管理**

当前 account.js 安全 tab 有邮箱管理卡片（显示邮箱 + 修改邮箱按钮）。无邮箱配置时修改邮箱走不通。

- 前端 account.js：无邮箱配置时不显示邮箱管理卡片
- 用户通过用户名+密码注册的（无邮箱），设置页不显示邮箱字段

**R7.4：无 OAuth App 时绑定弹窗只显示手动令牌**

当前绑定弹窗有两个 tab：OAuth 授权 + 手动令牌。未配 OAuth App 时点 OAuth 授权会 400 报错。

改为：根据 `/auth/providers` 返回的 `configured` 字段决定显示内容：
- `configured=true`：显示两个 tab（OAuth 授权 + 手动令牌），OAuth 默认选中
- `configured=false`：只显示手动令牌输入，不显示 tab 切换

改动：`account.js` 的 `_accountBindProvider` 函数加 `configured` 判断。

**R7.5：登录页始终显示 GitCode/GitHub 按钮**

当前登录页（index.html）硬编码预渲染 GitCode/GitHub 按钮，但未配 OAuth App 时点按钮会 400 报错。

改为：按钮始终显示（不管 OAuth App 是否配置），因为系统已支持用户 token 绑定，用户可以通过 GitCode/GitHub 身份注册后再绑定 PAT。点击行为分两种：
- OAuth App 已配 → 正常跳转 OAuth 授权登录
- OAuth App 未配 → 提示"OAuth 授权未配置，请使用用户名+密码注册登录，然后在设置页绑定 GitCode/GitCode token"

改动：`index.html` 的 OAuth 按钮区域保持显示，`app.oauthLogin()` 函数加 OAuth App 配置检测，未配时弹提示而非跳转。

**向前兼容**：已配 OAuth App 的环境点击行为不变，正常跳转授权页。

**R7.6：is_email_configured 统一检测**

前端需要一个 API 返回 `is_email_configured` 状态，用于控制 UI 显示/隐藏。复用已有的 `GET /auth/public-config` 或在 `GET /auth/providers` 中追加 `email_configured` 字段。

**R7.7：注册页无邮箱配置时无闪动体验**

**问题**：`checkEmailConfigured` 用异步 `API.get('/auth/providers')` 获取邮箱配置状态。页面初始 HTML 渲染时邮箱字段、步骤指示器、验证码区域、发送验证码按钮全部可见，API 返回后才隐藏——用户看到元素先显示再消失，产生闪动。

**修复**：页面初始状态设为"无邮箱模式"（邮箱字段、步骤指示器、验证码区域默认 `display:none`，按钮默认显示"注册"）。API 返回 `email_configured=true` 时才显示这些元素（从 hidden→visible，用户不会感到"消失了什么"）。API 返回 `email_configured=false` 或失败时保持初始状态（无邮箱模式），无闪动。

实现方式：HTML 元素初始加 `style="display:none"`，`checkEmailConfigured` 返回有邮箱时移除 `display:none`。

**R7.8：登录页"忘记密码"链接无闪动**

**问题**：index.html 中"忘记密码"链接和分隔符初始可见，`fetch('/api/auth/providers')` 异步返回后隐藏——闪动。

**修复**：链接和分隔符初始 `style="display:none"`，API 返回有邮箱时才显示。

**R7.9：account.js 邮箱管理卡片无闪动**

**问题**：`_accountLoadSecurity` 先获取 `email_configured` 再渲染。但 `window._emailConfigured` 初始 undefined，`!== false` 为 true → 邮箱卡片先显示。API 返回无邮箱后 `_emailConfigured = false` → 重新渲染隐藏——闪动。

**修复**：`window._emailConfigured` 初始设为 `false`（无邮箱模式默认），`_accountRenderSecurity` 中 `window._emailConfigured !== false` 为 false → 邮箱卡片不显示。API 返回有邮箱后设 `true` → 重新渲染显示。

### R8：PAT 直接绑定（复用已有 /settings/credentials 端点）

**背景**：当前用户 token 绑定只有 OAuth 授权流程一条路，硬依赖 OAuth App 配置（client_id + client_secret）。外部开发者部署后还需要在 GitCode 上创建 OAuth App，门槛高。PAT 直接绑定让用户输入自己的 GitCode PAT 即可完成绑定，不需要 OAuth App。

**方案**：复用已有的 `PUT /api/settings/credentials` 端点（gitcode_token 分支），不新增端点。该端点已具备：PAT 验证（调 GitCode /user API）、external_identities 写入、转移绑定检查（409 + binding_ticket）、覆盖更新。前端绑定弹窗的"手动令牌"tab 调此端点。

**三个场景的绑定方式与 token 有效期：**

**场景一：已配 OAuth App + 用户选 OAuth 授权（向前兼容）**

用户在绑定弹窗点"OAuth 授权"tab，跳转 GitCode 授权页。授权后 GitCode 回调返回 access_token + refresh_token + expires_in。
- access_token 有效期约 15 分钟（GitCode OAuth token 实测最短寿命）
- refresh_token 可自动刷新：`get_user_token_with_refresh` 在过期前 10 分钟预刷新，刷新后获得新的 access_token + refresh_token
- token_expires_at 存入 external_identities
- **token 不会过期**（refresh_token 自动续期，用户无感知）
- 行为与改造前完全一致，向前兼容

**场景二：未配 OAuth App + 只能输入 PAT 绑定**

绑定弹窗不显示 OAuth 授权 tab，只显示"手动令牌"输入框。用户在 GitCode 创建 PAT 后输入绑定。
- PAT 本身不过期（除非用户在 GitCode 手动撤销或设置了过期时间）
- 无 refresh_token（PAT 没有 refresh_token）
- `get_user_token_with_refresh` 检测到无 refresh_token → 跳过刷新，直接返回 token
- **token 不会过期**（PAT 本身长期有效）
- **前端需提醒用户**：
  - "当前系统未配置 OAuth 授权，您正在使用 Personal Access Token (PAT) 绑定"
  - "PAT 有效期取决于您在 GitCode 创建时的设置，如未设置过期时间则永久有效"
  - "如需撤销，请在 GitCode → 个人设置 → 个人访问令牌中删除该 PAT"

**场景三：已配 OAuth App + 两种方式都可选**

绑定弹窗显示两个 tab：OAuth 授权 + 手动令牌。用户自由选择。
- 选 OAuth 授权 → 场景一逻辑，token 自动续期不会过期
- 选手动令牌 → 场景二逻辑，PAT 不过期不需要刷新
- 两种方式都能保证 token 不会因过期而中断服务
- 两种方式互斥：同一用户同一 provider 只能有一种绑定，后绑定覆盖前一个

**token 有效期对比：**

| 场景 | 绑定方式 | token 类型 | 有效期 | 自动刷新 | 过期中断风险 | 撤销方式 |
|------|---------|-----------|--------|---------|------------|---------|
| 场景一 | OAuth 授权 | OAuth access_token | ~15 分钟 | 是（refresh_token 预刷新） | 无（自动续期） | GitCode 解除授权 |
| 场景二 | PAT 输入 | Personal Access Token | 永久（或用户设置的时间） | 否（不需要） | 无（PAT 不过期） | GitCode 删除 PAT |
| 场景三 | 任选其一 | 同上 | 同上 | 同上 | 无 | 同上 |

**结论**：三个场景都能保证 token 不会因过期而中断服务。场景一靠 refresh_token 自动续期，场景二/三 PAT 本身不过期。

**R8.1：复用 PUT /api/settings/credentials 端点**

不新增端点。已有的 `PUT /api/settings/credentials`（gitcode_token 分支）已覆盖 PAT 绑定需求：
- 验证 token：调 GitCode `/user` API（与 bind-token 设计相同的验证逻辑）
- 存入 external_identities：access_token = PAT（加密），refresh_token = 空，token_expires_at = 空
- 转移绑定检查：external_id 已绑定其他用户时返回 409 + binding_ticket（bind-token 设计缺失此检查）
- 覆盖更新：create_external_identity 的 UPDATE 分支已处理

前端 `_accountDoManualBind` 已调此端点（`API.put('/settings/credentials', {gitcode_token: token})`），无需改动。

**R8.2：get_user_token_with_refresh 兼容 PAT（无 refresh_token 时跳过刷新）**

当前逻辑：`token_expires_at=0` → 走降级分支 `fetched_at + 30min` → 30 分钟后触发刷新 → `refresh_oauth_token` 读 refresh_token → 空 → 返回 None → 最终返回原 token。功能上不坏，但每 30 分钟打一条 warning 日志。

改为：在检查过期之前，先判断是否有 refresh_token。无 refresh_token → 直接返回 access_token，不检查过期、不触发刷新。

```python
def get_user_token_with_refresh(username: str, user_id: int = 0) -> str:
    token = _get_oauth_token_from_identity(username, "gitcode")
    if token and user_id > 0:
        # PAT 场景：无 refresh_token → 不可刷新，直接返回
        if not _has_refresh_token(user_id, "gitcode"):
            return token
        # OAuth 场景：有 refresh_token → 检查过期 + 预刷新（原逻辑）
        ...
```

新增辅助函数 `_has_refresh_token(user_id, provider) -> bool`：从 external_identities 读 refresh_token，非空返回 True。

**R8.3：前端 PAT 绑定入口（已有，无需改动）**

用户设置页已有"手动令牌"tab（`_accountDoManualBind` 函数），调 `PUT /settings/credentials`（gitcode_token 字段）。绑定成功后显示已绑定状态，与 OAuth 绑定入口并列，用户二选一。

**R8.4：OAuth 绑定和 PAT 绑定互斥**

同一用户同一 provider 只能有一种绑定方式。已通过 OAuth 绑定的用户再绑 PAT → 覆盖更新（`create_external_identity` 的 UPDATE 分支已处理）。已通过 PAT 绑定的用户再走 OAuth → 同样覆盖更新。

`create_external_identity` 已有按 `(grape_user_id, provider)` 查活跃记录并 UPDATE 的逻辑，天然支持覆盖，无需改动。

### R9：向前兼容

**R9.1：已有全局 Bot Token 的环境完全不变**

所有改造都走 "全局 Bot 优先，用户 Token 降级" 路径。已配置全局 bot token 的环境：
- `get_repo_token_with_refresh` 前三级（devops_agent_session → managed_repos → 全局 bot）命中即返回，不触发用户 token 路径
- `get_gitcode_token` / `get_agent_bot_token` 内部改为调 `get_repo_token_with_refresh`，但前三级逻辑与原实现完全一致，返回值不变
- `get_repo_bot_token` 保留原语义（跳过 devops_agent_session），有 bot token 时返回值不变
- 托管走路径 A → 写入 bot_gitcode_token，host_user_id=0
- pipeline/agent 拿到的 token 与改造前完全一致
- webhook 验证：webhook_secret 为空（路径 A 不生成），fallback 到原 gitcode_webhook_token，验证逻辑不变
- 自循环检测：bot 用户名检测仍保留在第二层，路径 A actor=bot 用户名被第二层拦截，行为不变
- GitHub 托管：有全局 GitHub bot token 走原流程，_try_github_bot_collaborator 正常执行

**R9.2：已有邮箱配置的环境不受影响**

R7 无邮箱场景适配是条件性降级——`is_email_configured()` 返回 True 时走原流程（邮箱 + 验证码），返回 False 时才走降级路径。已配置邮箱的环境完全不变。

**R9.3：数据库迁移**

- `managed_repos` 新增 `host_user_id` 和 `webhook_secret` 两列（ALTER TABLE ADD COLUMN DEFAULT）
- 现有记录不迁移（host_user_id=0, webhook_secret=''），走降级路径
- 新托管记录（路径 B）写入新字段
- DDL 定义和迁移函数同步更新

**R9.4：函数签名兼容**

- `add_managed_repo` 新增 `host_user_id` 和 `webhook_secret` 参数，默认值 0/''，现有调用方不传这两个参数不受影响
- `get_gitcode_token` 保留原签名 `(repo_key, user_id)`，内部改为调 `get_repo_token_with_refresh`，调用方无感知
- `get_repo_bot_token` 保留原签名 `(repo_key)`，保留原语义（跳过 devops_agent_session），末尾加用户 token 降级，有 bot token 时返回值不变
- `get_agent_bot_token` 保留原签名 `(repo_key)`，间接通过 `get_gitcode_token` 走统一入口

**R9.5：OAuth 绑定和 PAT 绑定共存**

已有 OAuth 绑定的用户不受影响。PAT 绑定是新增能力，不替代 OAuth 绑定。两种方式存入同一张 external_identities 表，下游代码统一读 access_token，不区分来源。

**R9.6：显式兼容矩阵（F1/F2/F3 勘误，基线校准后补充）**

R9.1 原声明"已有全局 Bot Token 的环境完全不变"过强。以下是存量场景 × 行为变化 × 接受理由的显式矩阵：

| 存量场景 | 基线行为 | 分支行为 | 变化类型 | 接受理由 |
|---------|---------|---------|---------|---------|
| 路径 A 仓库 token 获取 | 前三级命中 bot token 返回 | 完全一致（host_user_id=0 守卫） | 无变化 | — |
| 路径 A 仓库 webhook 验证 | 读 gitcode_webhook_token | webhook_secret 为空→fallback 原字段 | 无变化 | — |
| 路径 A 仓库自循环检测 | bot 用户名检测拦截 | 标记检测优先→bot 用户名检测兼容 | 检测层增加，不破坏原行为 | 新增第一道防线 |
| **存量 OAuth 用户 token 读取（F1）** | _get_oauth_token_from_identity WHERE status=1 | WHERE status IN (1,4)，status=4 返回过期 token | **全局行为变更** | status=4=已标记过期，仍返回 token 供 best-effort（发评论等）。基线上 status=4 的账号拿不到 token 会走 refresh 分支，分支上会拿到过期 token 继续调 API → 401 → fail_task 检测。语义合理但 R9.1 未披露 |
| **GitCode 专用查询 vs 通用查询不一致（F3）** | deps.py 两处都 WHERE status=1 | deps.py:285 GitCode 专用保持 status=1；deps.py:329 通用改为 status IN (1,4) | **同文件分裂** | 两处查询服务于不同场景（GitCode 专用用于 OAuth 登录链路，通用用于 token 读取），但行为差异未在文档声明。应统一或显式说明差异原因 |
| **A2A trust domain host_user_id 比对（F2）** | 两仓 bot_gitcode_token 相同才信任 | 新增 host_user_id 相同也信任 | 新增信任路径 | **已确认安全**：代码有 `if _session_host > 0` 守卫（webhook.py:327），排除双 0（路径 A 仓库 host_user_id 全为 0 → 不进入信任域）。路径 A 仓库信任域不变 |
| 存量 agent session token 仓库（D1） | 第 1 级 session token 遮蔽 bot token（同语义） | 第 1 级 session token 遮蔽用户 token（语义不同） | **隐性不兼容** | 基线上 session token 遮蔽 bot token 是同语义（都是 bot token）。分支上路径 B 仓库 session token 残留会遮蔽用户 token → R22.2 修复（host_user_id 守卫 + 清理） |
| add_managed_repo 默认值（D2） | 无 host_user_id/webhook_secret 参数 | 新增参数默认 0/空，但 5 轮审计修了默认值语义 bug | **文档过时** | R9.4 声明"默认值无影响"未随 R21/§25/§26 审计更新。最终代码 _final_host 三分支是修出来的。R9.4 应补充"默认值在 5 个场景下曾被判错，已在 §21-27 修复" |

**R9.7：双向状态机矩阵（D3 补充，设计层根本遗漏修复）**

14 轮审计修 bug 的模式证明缺一张 `host_user_id × bot_gitcode_token × webhook_secret × webhook_id` 四字段状态机矩阵。以下是每个迁移路径的合法终态：

| 迁移路径 | host_user_id | bot_gitcode_token | webhook_secret | webhook_id | 触发条件 |
|---------|-------------|-------------------|----------------|------------|---------|
| 初始托管（路径 A） | 0 | =全局 bot token | ''（空） | 新建 webhook ID | 有全局 bot token |
| 初始托管（路径 B） | =用户 ID | ''（空） | =随机密钥 | 新建 webhook ID | 无全局 bot token |
| 路径 B→A（auto_upgrade） | 0（清） | =全局 bot token（写） | ''（清） | 重建（删旧建新） | 管理员配全局 bot token |
| 路径 A→B（降级，无全局 bot token） | =用户 ID（写） | ''（清） | =随机密钥（写） | 重建（删旧建新） | 全局 bot token 被清除/失效 |
| 路径 A→A（换 bot 账号，R21.1） | 0（不变） | =新全局 bot token（更新） | ''（不变） | 重建（删旧建新） | 管理员换全局 bot token |
| 路径 A→A（仓库级 token 配置，R15） | 0（不变） | =仓库级 bot token（更新） | ''（不变） | 重建（删旧建新） | 用户配仓库级 bot token |
| 路径 A→A（仓库级 token 清除，R15.5） | 0（不变） | ''（清） | ''（不变） | 不变（需 repair_host 重建） | 用户清除仓库级 bot token |
| 路径 B→B（用户换 token） | =新用户 ID（更新） | ''（不变） | =新随机密钥（重建） | 重建 | 用户重新托管 |
| 解绑（R19.3） | 保留（指向已删用户） | ''（不变） | ''（不变） | 不变 | 用户解绑 VCS 账号 |
| 删除仓库 | — | — | — | — | 所有字段随仓库删除 |

非法终态（必须避免）：
- host_user_id > 0 且 bot_gitcode_token 非空 → 路径 B 仓库有 bot token（异常状态，_is_user_token_repo 返回 False）
- host_user_id == 0 且 bot_gitcode_token 为空 且 webhook_secret 为空 → 无 token 无密钥（孤儿状态）
- webhook_id 存在但 webhook_secret/gitcode_webhook_token 与实际 webhook password 不匹配 → 验证断裂

**R9.8：支线文档向前兼容声明（D4 补充）**

支线文档（repo-token-config R15-R21）的向前兼容分析不足：
- R16.4.5 "短路检查新增在现有逻辑之后"只覆盖了短路机制的兼容性，未分析 Repo.bot_token 短路返回空串 vs 基线返回过期 token 的消费方行为差异
- R20.7 对 29 个场景只标"将改为"，未分析改动对基线调用方的返回值语义影响
- 支线实现时应补充每个场景的"基线行为 vs 改造后行为 vs 兼容性分析"三元组

### R10：非 OAuth 场景 token 过期主动提示

**背景**：非 OAuth 场景（PAT 直接绑定，无 refresh_token）下，token 被 GitCode 撤销或设置了过期时间到期后，系统没有自动刷新能力（R8.2 跳过刷新）。pipeline/agent 拿到过期 token 调 GitCode API 返回 401，当前行为是 task 标记 failed 但用户不知道是 token 过期导致的，需要排查日志才能定位。

**策略**：检测到 401 时主动标记 token 状态 + 通知用户重新绑定。

**R10.1：token 过期检测与标记**

pipeline/agent 执行 GitCode API 调用返回 401 时：
- 检测当前 repo_key 是否走路径 D（用户 token，通过 `_is_user_token_repo` 判断）
- 如果是用户 token 且 401 → 标记 `external_identities.status=4`（token 过期，A3 勘误：原误写 status=0）+ 在 task errors 字段写入明确原因 `"user token expired or revoked, please re-bind GitCode token"`
- 不静默重试（PAT 无 refresh_token，重试必然失败）

**R10.2：后端错误码 + 用户侧提示**

后端通过结构化 error_code 向前端传递 token 过期状态，前端根据 error_code 映射 i18n 文案，原始 message 仅进 console.error（不直接展示给用户）。

**R10.2.1：后端 error_code 定义**

| error_code | HTTP status | 含义 | 前端 i18n key |
|------------|-------------|------|--------------|
| `TOKEN_EXPIRED` | 200（task failed 响应）/ 401（API 直调） | 用户 PAT 已过期或被撤销 | `app.error.TOKEN_EXPIRED` |
| `TOKEN_NOT_BOUND` | 400 | 仓库走路径 B 但用户未绑定 token | `app.error.TOKEN_NOT_BOUND` |
| `WEBHOOK_URL_NOT_CONFIGURED` | 400 | 系统未配置 webhook_url | `app.error.WEBHOOK_URL_NOT_CONFIGURED` |
| `PERMISSION_DENIED` | 403 | 用户无仓库托管权限 | `app.error.PERMISSION_DENIED` |
| `UNKNOWN` | — | 未知错误（fallback） | `app.error.UNKNOWN` |

后端返回格式：
```json
{
  "error_code": "TOKEN_EXPIRED",
  "message": "user token expired or revoked",
  "provider": "gitcode",
  "rebind_url": "/#account"
}
```

- `error_code`：前端映射 i18n 文案的唯一标识，不随语言变化
- `message`：英文原始信息，仅进 console.error，不直接展示
- `provider`：过期的平台（gitcode/github）
- `rebind_url`：前端跳转到设置页重新绑定的 URL

task failed 时 errors 字段也写入 error_code：
```json
["{\"error_code\":\"TOKEN_EXPIRED\",\"message\":\"user token expired or revoked\",\"provider\":\"gitcode\"}"]
```

**R10.2.2：前端 error_code 映射**

- **Web UI**：account.js 加载 /auth/me 后检查 `token_expired_providers`，对过期 provider 显示醒目提示卡片（红色边框 + "Token 已过期"标签 + "重新绑定"按钮），按钮跳转到绑定弹窗。i18n 文案通过 `I18N.t('app.error.token_expired')` 获取
- **Issue 回帖**：agent task 因 token 过期 failed 时，在 issue 中回帖提示用户。回帖文案使用中文（issue 评论无 i18n）
- **设置页状态**：account.js 第三方账号绑定区域，token status=4 时显示"已过期"标签 + "重新绑定"按钮
- **API 错误响应**：前端 API 调用返回 401 且 response 含 `error_code: "TOKEN_EXPIRED"` 时，toast 显示 `I18N.t('app.error.token_expired')` 而非原始 message

**R10.3：token 状态恢复**

用户重新绑定 token（通过 OAuth 授权或 PAT 输入）后：
- `create_external_identity` / `update_external_identity_tokens` 恢复 status=1
- 下次 pipeline 执行时 `_is_user_token_repo` 仍返回 True（host_user_id 未变），但 `get_user_token_with_refresh` 返回有效 token，task 正常执行

**向前兼容**：OAuth 场景（有 refresh_token）的 token 过期由 `get_user_token_with_refresh` 自动刷新，不触发 401，不进入 R10 流程。R10 仅对 PAT 场景（无 refresh_token）生效。

---

## 四、改动点清单

| 编号 | 文件 | 改动 | 优先级 |
|------|------|------|--------|
| R1.1 | src/api/routes/repos.py `host_repo` | 新增路径 B（无全局 bot token 时用用户 token 建 webhook，跳过 bot 协作者流程） | P0 |
| R1.2 | src/api/routes/repos.py `host_repo` | 去掉 `if not global_bot_token: raise 503`，改为路径选择 | P0 |
| R1.3 | src/user/db.py + 迁移 | managed_repos 新增 host_user_id 列 | P0 |
| R1.4 | src/api/routes/repos.py `host_repo` GitHub 分支 | 路径 B：跳过 bot 协作者，webhook 用用户 token | P0 |
| R1.5 | src/api/routes/repos.py `host_repo` GitHub 分支 | setup_github_webhook 调用传 webhook_secret 参数 | P0 |
| R1.6 | src/api/routes/repos.py `create_from_template` | 路径 B 支持 + webhook_url 硬校验改为不阻断创建 | P0 |
| R2.1 | src/user/db.py 或 src/api/deps.py | 新增 `get_repo_token_with_refresh(repo_key)` 函数 | P0 |
| R2.2 | src/api/event_pipeline.py | 所有 `get_gitcode_token(repo_key)` / `get_agent_bot_token(repo_key)` 替换为 `get_repo_token_with_refresh(repo_key)` | P0 |
| R2.2 | src/runtime/agent.py | `task.repo_gitcode_token` 为空时调 `get_repo_token_with_refresh(repo_key)` | P0 |
| R2.2 | src/repo.py `Repo.bot_token` | 改为调用 `get_repo_token_with_refresh` | P0 |
| R2.3 | src/api/event_pipeline.py | task 行 `repo_gitcode_token` 路径 B 写空，运行时实时获取 | P0 |
| R2.5 | src/user/db.py `get_github_token` | fallback 链末尾加 host_user_id → get_user_github_token_with_refresh | P1 |
| R2.6 | src/user/db.py `get_repo_bot_token` | 保留原语义（跳过 devops_agent_session），末尾加用户 token 降级 | P1 |
| R3.1 | src/api/routes/repos.py `host_repo` | 路径 B webhook password 改为随机密钥 | P0 |
| R3.1 | src/user/db.py + 迁移 | managed_repos 新增 webhook_secret 列 | P0 |
| R3.2 | src/user/db.py `get_webhook_token_by_repo` | 改为读 webhook_secret，fallback 到原字段 | P0 |
| R4.1 | grape-cli `issues comment` 命令 | 自动追加 `<!-- grape-auto-comment -->` | P1 |
| R4.2 | src/api/event_pipeline.py | 自循环检测调整顺序：标记优先，bot 用户名第二 | P1 |
| R5.1 | src/api/routes/webhook.py `_authorize_a2a` | 当前已支持降级，无需改动（验证确认） | P1 |
| R5.2 | src/api/routes/webhook.py `_authorize_a2a` | trust domain 新增 host_user_id 比对分支 | P1 |
| R6.1 | src/api/routes/repos.py `repair_host` | 新增路径 B 修复（与 host_repo 一致） | P1 |
| R6.2 | src/api/routes/repos.py `repair_host` | 路径 B 找到已有 webhook 时删除重建 + 更新密钥 | P1 |
| R6.3 | src/user/repo_manager.py + src/api/routes/repos.py `repair_host` | 统一 provider 配置（get_hosting_provider_config），GitHub 仓库正确用 GitHub bot token + GitHub webhook 函数 | P1 |
| R2.4 | src/api/event_pipeline.py | task 创建时区分 token 来源，路径 B 用户 token 写空 | P1 |
| R7.1 | src/api/routes/grape_auth.py + register.html | 无邮箱配置时用户名+密码直接注册，跳过验证码 | P0 |
| R7.2 | 登录页 + forgot.html | 无邮箱配置时隐藏"忘记密码"入口 | P1 |
| R7.3 | account.js | 无邮箱配置时隐藏邮箱管理卡片 | P1 |
| R7.4 | src/api/routes/grape_auth.py | API 返回 email_configured 状态供前端控制 UI | P0 |
| R7.5 | index.html + app.oauthLogin() | 登录页 GitCode/GitHub 按钮始终显示，未配 OAuth 时点击提示 | P1 |
| R8.1 | 无需新增端点 | 复用 `PUT /api/settings/credentials`（gitcode_token 分支已覆盖 PAT 绑定） | — |
| R8.2 | src/api/deps.py `get_user_token_with_refresh` | 无 refresh_token 时跳过刷新，直接返回 access_token | P0 |
| R8.2 | src/auth/grape_account_db.py | 新增 `_has_refresh_token(user_id, provider)` 辅助函数 | P0 |
| R8.3 | 无需改动 | 前端 `_accountDoManualBind` 已调 `PUT /settings/credentials` | — |
| R8.4 | 无需改动 | `create_external_identity` 已支持覆盖更新（验证确认） | — |
| R9.1 | 全局 | 向前兼容：全局 bot 优先路径 | P0 |
| R9.2 | 全局 | 向前兼容：有邮箱配置环境不受影响 | P0 |
| R9.3 | src/user/db.py 迁移 | DDL 迁移函数 | P0 |
| R10.1 | src/api/event_pipeline.py + src/auth/grape_account_db.py | 401 检测路径 D 仓库 → 标记 external_identities.status=4（A3 勘误：原误写 status=0）+ task errors 写明原因 | P1 |
| R10.2 | src/api/static/js/ + src/api/routes/grape_auth.py | 后端 error_code（TOKEN_EXPIRED/TOKEN_NOT_BOUND）+ 前端 i18n 映射 + Web UI/issue 回帖提示 | P1 |
| R10.3 | 无需额外改动 | 重新绑定恢复 status=1（create_external_identity 已有逻辑） | — |

---

## 五、设计原则：后端错误码 + 前端错误码提示国际化

### 5.1 核心原则

**后端基于错误码返回，前端基于错误码提示用户。** 所有面向用户的 API 错误响应必须包含结构化 `error_code` 字段，前端根据 `error_code` 映射 i18n 文案展示给用户，后端原始 `message`（英文）仅进 `console.error`，不直接展示。

**禁止的做法**：
- 后端返回中文 message 直接展示给用户（英文模式下用户看到中文）
- 前端直接展示 `e.message`（混合语言、暴露内部信息）
- 后端不加 error_code 只返回 message（前端无法做 i18n 映射）

### 5.2 后端错误码规范

所有 HTTPException 的 detail dict 必须包含 `error_code` 字段：

```python
raise HTTPException(
    status_code=400,
    detail={
        "code": 400,
        "error_code": "WEBHOOK_URL_NOT_CONFIGURED",
        "message": "webhook_url not configured"  # 英文，仅 console
    }
)
```

task failed 时 errors 字段也写入结构化 error_code：

```json
[{"error_code":"TOKEN_EXPIRED","message":"user token expired or revoked","provider":"gitcode"}]
```

### 5.3 前端错误码映射规范

前端通过 `app.errMsg(e)` 统一映射：
1. 读取 `e.error_code`
2. 调 `I18N.t('app.error.' + error_code)` 获取本地化文案
3. 原始 `e.message` 进 `console.error`
4. 无 error_code 或 i18n key 不存在时 fallback 到 `e.message` 或 `I18N.t('app.error.UNKNOWN')`

### 5.4 已定义错误码

| error_code | HTTP status | 含义 | 前端 i18n key |
|------------|-------------|------|--------------|
| `TOKEN_EXPIRED` | 200/401 | 用户 PAT 已过期或被撤销 | `app.error.TOKEN_EXPIRED` |
| `TOKEN_NOT_BOUND` | 400 | 仓库走路径 B 但用户未绑定 token | `app.error.TOKEN_NOT_BOUND` |
| `WEBHOOK_URL_NOT_CONFIGURED` | 400 | 系统未配置 webhook_url | `app.error.WEBHOOK_URL_NOT_CONFIGURED` |
| `PERMISSION_DENIED` | 403 | 用户无仓库托管权限 | `app.error.PERMISSION_DENIED` |
| `UNKNOWN` | — | 未知错误（fallback） | `app.error.UNKNOWN` |

### 5.5 locale 文件

en/app.json 和 zh/app.json 新增 `error` namespace，每个 error_code 对应一条 i18n 文案。

---

## 六、深度分析发现的问题与修复（作为测试验收维度）

以下问题在与原 bot token 全链路对比分析中发现，已在代码中修复。每个问题同时作为验收测试场景。

### 6.1 A2A 鉴权 bot token 通道断裂（P0）

**问题**：路径 B 仓库 `managed_repos.bot_gitcode_token` 为空，`get_repo_bot_token_no_global` 返回空，A2A bot token 通道完全失效。远程 worker（无 session token 上下文）的 A2A 调用全部 401。

**修复**：`get_repo_bot_token_no_global` 路径 B 降级返回 host 用户 token（通过 `get_host_user_id` → `get_user_token_with_refresh`），确保 A2A bot token 通道在路径 B 场景下仍可用。

**验收测试场景**：路径 B 仓库的远程 worker agent 调 A2A 端点（如 coding 跨仓库拉代码），鉴权通过。

### 6.2 BOT_USERNAME 为空导致 git commit author 和 PR 审查人异常（P1）

**问题**：路径 B 无全局 bot username，`managed_repos.bot_username` 为空，`event_pipeline` 注入 `BOT_USERNAME` 为空 → git commit author fallback 到硬编码 "grape_bot"（GitCode 不存在此用户）→ `create_skill_pr.sh` 设 PR 审查人失败。

**修复**：`BOT_USERNAME` 注入逻辑在全局 bot username 为空时，fallback 到 `host_user_id` 对应用户的 `external_login`（GitCode/GitHub 平台用户名）。

**验收测试场景**：路径 B 仓库的 coding agent 提交 PR，git commit author 为 host 用户名（非 "grape_bot"），PR 审查人设置成功。

### 6.3 status=4 token 过期后 _get_oauth_token_from_identity 不返回 token（P1）

**问题**：`_get_oauth_token_from_identity` 查 `WHERE status=1`，R10 标记 status=4 后此函数返回空 → `get_repo_token_with_refresh` 路径 D 返回空 → pipeline 拿不到 token → R10 issue 回帖也拿不到 token 发评论。

**修复**：`_get_oauth_token_from_identity` 改为查 `WHERE status IN (1, 4)`，status=4 时仍返回 token（过期的 token——评论发不出去是预期行为，用户需重新绑定）。

**验收测试场景**：用户 PAT 被撤销后 → task failed + issue 回帖提示用户（回帖用过期 token 发，GitCode API 401 → try/except 记 warning，回帖失败但不影响 task failed 状态）。

### 6.4 多用户托管同一仓库时 host_user_id 被覆盖（P1）

**问题**：`add_managed_repo` 用 `INSERT OR REPLACE`，用户 A 路径 B 托管（host_user_id=A），用户 B 再托管（host_user_id=B）→ host_user_id 被覆盖为 B，用户 A 的 token 不再被使用。

**修复**：`add_managed_repo` 改为先查已有记录，有则 UPDATE（保留已有 host_user_id 不被覆盖，除非调用方显式传非零值），无则 INSERT。

**验收测试场景**：用户 A 托管仓库后，用户 B 再托管同一仓库 → host_user_id 仍为 A → pipeline 用 A 的 token。

### 6.5 grape CLI 发评论不带 grape-auto-comment 标记（P1）

**问题**：R4.2 要求 agent 通过 grape CLI 发评论时自动追加标记，但 grape CLI（独立仓库）未改。路径 B 场景 agent 用用户 token 发评论 → actor=用户名（非 bot）→ 第 2 层 bot 用户名检测不拦截 → 评论不带标记 → 第 1 层标记检测也不拦截 → 死循环。

**修复**：在主仓库的 `create_issue_comment` 和 `create_pull_comment` API 端点（agent 通过 grape API 发评论的入口）自动追加 `<!-- grape-auto-comment -->` 标记。grape CLI 后续同步修改。

**验收测试场景**：路径 B 场景 agent 通过 grape API 发 issue 评论 → 评论自动带标记 → pipeline 收到 webhook 事件 → 标记检测拦截 → 不触发重复执行。

### 6.6 remove_managed_repo 路径 B 冗余 bot 协作者移除（P2）

**问题**：解绑时尝试移除 bot 协作者，路径 B 仓库没有 bot 协作者（托管时跳过了邀请），GitCode API 删除不存在的协作者 → 404。

**修复**：`remove_managed_repo` 检测路径 B（无 bot token 且有 host_user_id）时跳过 bot 协作者移除。

**验收测试场景**：路径 B 仓库解绑 → 不调 remove_collaborator（无 404 warning 日志）。

### 6.7 host_repo 缺 webhook_url 空值校验（P2）

**问题**：`host_repo` 的 webhook_url 为空时路径 B 会用空 URL 创建 webhook → GitCode API 报错。`create_from_template` 已改为不阻断创建，但 `host_repo` 仍然是硬失败。

**修复**：`host_repo` 和 `repair_host` 都加 `if not webhook_url: raise 400` 校验，返回 `error_code: "WEBHOOK_URL_NOT_CONFIGURED"`。

**验收测试场景**：系统未配 webhook_url 时用户托管仓库 → 返回 400 + error_code=WEBHOOK_URL_NOT_CONFIGURED → 前端 toast 显示 i18n 文案。

### 6.8 repair_host GitHub 用错 bot token（P1）

**问题**：`repair_host` 用 `get_global_bot_credential()`（返回 GitCode bot token）对 GitHub 仓库判断 `_use_path_b`。有全局 GitCode bot token 但无全局 GitHub bot token 时，GitHub 仓库误判为路径 A。

**修复**：新增 `get_hosting_provider_config(provider)` 统一函数，GitHub 仓库正确用 `get_global_github_bot_token()` + GitHub 专用 webhook 函数。

**验收测试场景**：有 GitCode bot token 无 GitHub bot token 时，GitHub 仓库 repair_host → 走路径 B（用户 token 修复 webhook）。

### 6.9 _get_task_token 跨 provider token 错配（P1）

**问题**：`_get_task_token` 只有 `get_repo_token_with_refresh`（GitCode 链），无 github 分流。GitHub 仓库走路径 D → task 行 token 为空 → 运行时用 GitCode 链取 token → 拿到 GitCode token 操作 GitHub 仓库 → 401。

**修复**：`_get_task_token` 按 repo_key 前缀分流：`github:` → `get_github_token`，其他 → `get_repo_token_with_refresh`。

**验收测试场景**：GitHub 仓库路径 B 的 agent 执行 → worktree clone/push 用 GitHub token（非 GitCode token），无 401。

### 6.10 host_repo/create_new_repo 复用已有 webhook 时密钥不匹配（P1）

**问题**：host_repo/create_new_repo 的 existing webhook 分支：路径 B 生成新 _webhook_secret 写 DB，但复用的旧 webhook 实际密码是旧值。get_webhook_token_by_repo 优先返回新 webhook_secret → 校验必失败 → 所有事件 403。

**修复**：三处 existing webhook 分支（create_new_repo / host_repo GitHub / host_repo GitCode）照搬 repair_host R6.2 的删旧重建逻辑。

**验收测试场景**：曾托管过又解绑（webhook 残留）的仓库，路径 B 重新托管 → 旧 webhook 删除 → 新 webhook 用新密钥创建 → 事件验证通过。

### 6.11 R10 issue 回帖取 token 时序错误（P2）

**问题**：fail_task 先 `mark_identity_expired`（status→4）再取 `_comment_token`。虽然 `_get_oauth_token_from_identity` 改了查 `status IN (1,4)` 仍能取到 token，但语义上应在标记前取到"当前可用"的 token。

**修复**：把取 `_comment_token` 挪到 `mark_identity_expired` 之前。

### 6.12 fail_task 401 误报面（P2）

**问题**：用 "401" 子串匹配错误文本，issue 号 401、耗时 401s 都会命中 → 误标 token 过期 + 覆写 task errors 丢失原始报错。

**修复**：加 `exit_code < 0` 前置条件 + 按词匹配（"401 unauthorized" 而非 "401"）。

### 6.13 _execute_agent_inner 用 repo_name 而非 repo_key 查 token（P2）

**问题**：event_pipeline.py `_execute_agent_inner` 中 GitCode 路径 `get_repo_token_with_refresh(repo_key=repo_name)` 用的是 `repo_name`（仓库名不含 owner），`to_repo_key(repo_name)` 生成错误 key → 查不到 managed_repos → 路径 B 场景 token 获取可能失败。路径 A 不受影响（全局 bot token 不依赖 repo_key 匹配）。

**修复**：改为 `get_repo_token_with_refresh(repo_key=repo_key)`（完整带前缀的 repo_key）。

**验收测试场景**：路径 B 场景 `_execute_agent_inner` 获取 token 时用完整 repo_key 查询，能命中 managed_repos 记录。

### 6.14 token 刷新竞态（P2，暂未修复）

**问题**：`get_user_token_with_refresh` 刷新 token 时写 DB → 其他并发 task 同时调 `get_repo_token_with_refresh` 路径 D → 读到旧 token → 401。

**状态**：原链路不存在此问题（bot token 不过期不刷新）。路径 D 引入了刷新窗口。后续可通过加线程锁或乐观锁修复。

**验收测试场景**（已知 limitation）：并发 task 触发 token 刷新时，部分 task 可能拿到旧 token 401，下一个 pipeline 周期自动恢复。

---

## 七、风险与对策

| 风险 | 严重度 | 对策 |
|------|--------|------|
| 用户 token 过期且 refresh_token 也失效 | P0 | 检测到 401 时 task 标记 failed + 通知用户重新登录，不静默重试 |
| 多用户托管同一仓库时 token 选择 | P1 | managed_repos.host_user_id 记录首个托管者，后续用 host 的 token；或取仓库 owner 的 user_id |
| 用户 token scope 不足（无 repo 写权限） | P1 | 托管时校验 token scope（调 /user API 确认权限），权限不足时提示用户 |
| git commit author 变成用户名 | P2 | 无功能影响，只影响 GitCode 显示。如需统一，可在 repogit 中固定 commit author |
| webhook 重建（已有 webhook 用 bot token 创建，改密钥需重建） | P2 | 迁移函数检测旧 webhook，按需重建（或保持旧 webhook 验证方式向前兼容） |

---

## 八、验收标准

1. **全新部署（无全局 bot token）**：用户 OAuth 登录 → 托管仓库 → 建 issue → pipeline 全链路跑通（VOD → Issue → Coding → QA → SRE）→ agent 回帖到 issue → 无死循环
2. **已有部署（有全局 bot token）**：升级后现有仓库托管状态不变，pipeline 拿到的 token 与改造前完全一致，无回归
3. **混合场景**：同一平台上有全局 bot token 的仓库和无的仓库共存，分别走路径 A 和路径 B，互不干扰
4. **token 刷新**：路径 B 场景下用户 token 过期后 pipeline 自动刷新，无 401 中断
5. **webhook 验证**：路径 B 场景下 GitCode 推事件到 webhook，验证通过，不因 token 刷新导致验证失败
6. **自循环检测**：路径 A 场景 actor=bot 用户名被拦截；路径 B 场景评论带 grape-auto-comment 标记被拦截，均不触发重复执行
7. **A2A 鉴权**：路径 A 走 bot token 通道；路径 B 走 session token 通道 + host 用户 token 兜底；关联仓库信任域正确判断
8. **无邮箱注册**：系统未配邮箱服务时，用户名+密码直接注册成功；已配邮箱时走原验证码流程
9. **无邮箱隐藏 UI**：无邮箱配置时登录页不显示"忘记密码"、设置页不显示邮箱管理；有邮箱时正常显示
10. **PAT token 过期提示**：路径 B 场景下用户 PAT 被 GitCode 撤销后，pipeline 401 → task failed + task errors 写明 error_code=TOKEN_EXPIRED；Web UI 显示 i18n 过期提示；issue 回帖提示用户重新绑定；用户重新绑定后 pipeline 恢复正常
11. **error_code 国际化**：后端返回 error_code，前端 `app.errMsg(e)` 映射 i18n 文案；英文模式下不出现中文错误提示；原始 message 仅进 console.error
12. **BOT_USERNAME**：路径 B 场景 git commit author 为 host 用户名（非 "grape_bot"），PR 审查人设置成功
13. **多用户托管**：用户 A 托管仓库后用户 B 再托管，host_user_id 不被覆盖，pipeline 仍用 A 的 token
14. **grape API 评论标记**：agent 通过 grape API 发 issue/PR 评论，评论自动带 grape-auto-comment 标记
15. **解绑路径 B**：路径 B 仓库解绑时不调 remove_collaborator（无 404 日志）
16. **webhook_url 空值**：系统未配 webhook_url 时托管返回 400 + error_code=WEBHOOK_URL_NOT_CONFIGURED，前端显示 i18n 提示
17. **repair_host GitHub 分流**：有 GitCode bot token 无 GitHub bot token 时，GitHub 仓库 repair_host 走路径 B
18. **R10 errors 格式兼容**：token 过期 task 的 errors 字段保持 JSON 列表格式，dashboard 正常展示失败任务
19. **R7 email_verified 语义**：无邮箱注册时 email_verified=0（不标记为已验证）
20. **路径 B 删旧 webhook 失败安全**：删旧 webhook API 失败时，DB 不写新密钥，fallback 到旧 webhook token 验证
21. **路径 B 任务完成回帖**：路径 B 场景 task 完成后正确获取 token 发 issue 回帖（不因 task 行 token 为空跳过）
22. **路径 B token 脱敏**：路径 B 场景 report_messages / event_archive 正确脱敏用户 token
23. **repair_host GitHub bot 邀请**：GitHub 路径 A bot 权限不足时 repair_host 尝试邀请 bot（与 GitCode 对称）
24. **OAuth token 误标过期恢复**：OAuth token 被 mark_identity_expired 后仍能通过 refresh_token 自动刷新恢复
25. **前端 i18n 完整性**：account.js 所有 i18n key 在 en/zh locale 文件中均有定义
26. **无 token 错误 error_code**：host_repo / issues.py 无 token 时返回 error_code=TOKEN_NOT_BOUND
27. **重派 BOT_USERNAME**：task 重派时路径 B 正确注入 host_user 的 external_login 作为 BOT_USERNAME
28. **create_new_repo GitHub 分流**：create_new_repo GitHub 分支与 host_repo 一致，有全局 GitHub bot token 时走路径 A
29. **B→A 演进兼容**：路径 B 仓库（host_user_id > 0）在全局 bot token 配置后仍走用户 token，不 403；路径 A 仓库和新建仓库不受影响
30. **B→A 自动升级**：管理员配置全局 bot token（从无到有）时异步遍历路径 B 仓库邀 bot + 写 bot_gitcode_token，失败的仓库跳过不阻断
31. **B→A repair_host 升级**：路径 B 仓库手动 repair_host 后 bot_gitcode_token 被写入，pipeline 自动切路径 A

---

## 九、第二轮深度审计发现的问题与修复需求

> 以下问题在第一轮实现后的逐行深度审计中发现，需同步修复。设计文档第八章有详细方案。

### 9.1 P0-1: R10 task errors 格式破坏

**R10-1**：`fail_task` 中 R10 代码用 JSON 对象覆写 `agent_tasks.errors`，与原 JSON 列表格式不兼容。dashboard.py `_errors[:3]` 对 dict 切片抛 TypeError 被 except 吞掉，token 过期失败任务从 dashboard 静默消失。

**修复要求**：R10 errors 写入改为列表格式 `[{"error_code":"TOKEN_EXPIRED",...}]`。

**测试用例**：
- T2-01：`fail_task` 触发 R10 后 `agent_tasks.errors` 字段为 JSON 列表（非对象），`json.loads` 结果是 list，`[:3]` 切片不抛异常
- T2-02：dashboard `failed_tasks` 解析 R10 格式 errors 不崩溃，能展示 error_code

### 9.2 P0-2: R7 无邮箱注册 email_verified 语义错误

**R10-2**：无邮箱配置时 `create_grape_account` 仍设 `email_verified=1`，email=NULL 但标记已验证，语义矛盾。

**修复要求**：无邮箱配置时 `email_verified=0`。

**测试用例**：
- T2-03：`is_email_configured()` 返回 False 时 `create_grape_account(email_verified=False)` 写入 `email_verified=0`
- T2-04：`is_email_configured()` 返回 True 时 `create_grape_account(email_verified=True)` 写入 `email_verified=1`（回归保护）

### 9.3 P1-1: 路径 B 删旧 webhook 失败密钥不匹配

**R10-3**：host_repo / create_new_repo / repair_host 三处路径 B 删旧 webhook 失败时 fallback 复用旧 webhook，但 DB 写新密钥 → 验证不匹配 → 所有事件 403。

**修复要求**：删失败时不写新 `webhook_secret`，让 `get_webhook_token_by_repo` fallback 到旧 webhook token。

**测试用例**：
- T2-05：路径 B `remove_webhook` 失败时，`add_managed_repo` 写入的 `webhook_secret` 为空字符串
- T2-06：`get_webhook_token_by_repo` 在 `webhook_secret` 为空时 fallback 到 `gitcode_webhook_token`（旧值），验证通过

### 9.4 P1-2/3/4: service.py 空 token 传导

**R10-4**：`complete_task` / `report_messages` / `finalize_event_archive` 直接读 `task.get("repo_gitcode_token")`（路径 B 为空），未调 `_get_task_token(task)`。导致任务完成无回帖、token 脱敏失效。

**修复要求**：4 处改为调 `_get_task_token(task)`。

**测试用例**：
- T2-07：`_get_task_token(task)` 路径 B 场景（task 行 repo_gitcode_token 为空）能实时获取用户 token
- T2-08：`service._get_task_token` 与 `agent._get_task_token` 行为等价（GitHub 分流 + 路径 D 实时获取）

### 9.5 P1-5: repair_host GitHub bot_ok=False 不阻断

**R10-5**：GitHub 路径 A bot 权限检查异常时不邀请 bot，直接建 webhook，bot 无法操作仓库。

**修复要求**：GitHub 分支补齐与 GitCode 对称的邀请+重试逻辑。

**测试用例**：
- T2-09：GitHub 路径 A `get_github_repo_permissions` 返回无 push 权限时，`repair_host` 调用 `add_github_collaborator` 尝试邀请
- T2-10：GitHub 路径 A bot 邀请后权限仍不足时，`repair_host` 返回 `host_status=bot_missing`（不继续建 webhook）

### 9.6 P2-1: _has_refresh_token 只查 status=1

**R10-6**：OAuth token 被 mark_identity_expired（status=4）后 `_has_refresh_token` 返回 False，不触发刷新。

**修复要求**：查询条件改为 `status IN (1, 4)`。

**测试用例**：
- T2-11：`_has_refresh_token` 在 status=4（token 过期）且 refresh_token 非空时返回 True
- T2-12：`_has_refresh_token` 在 status=0（解绑）时返回 False

### 9.7 P2-2: 前端 i18n 缺 3 个 key

**R10-7**：account.js 引用的 `token_expired` / `token_expired_hint` / `re_bind` 在 locale 文件中未定义。

**修复要求**：en/zh app.json account namespace 新增这三个 key。

**测试用例**：
- T2-13：zh/app.json account 对象包含 `token_expired`、`token_expired_hint`、`re_bind` 三个 key
- T2-14：en/app.json account 对象包含同上三个 key

### 9.8 P2-3: 无 token 错误缺 error_code

**R10-8**：host_repo / issues.py 无 token 时 raise HTTPException 无 error_code。

**修复要求**：追加 `error_code: "TOKEN_NOT_BOUND"`。

**测试用例**：
- T2-15：`host_repo` 无 api_token 时返回 400 + `error_code=TOKEN_NOT_BOUND`
- T2-16：`issues.py` clarify GitCode 路径无 token 时返回 400 + `error_code=TOKEN_NOT_BOUND`

### 9.9 P2-4: _build_retry_env 漏 BOT_USERNAME fallback

**R10-9**：task 重派时 BOT_USERNAME 为空，回退到 "grape_bot"。

**修复要求**：`_build_retry_env` 补齐 host_user external_login fallback。

**测试用例**：
- T2-17：`_build_retry_env` 路径 B（无全局 bot username + host_user_id > 0）时 BOT_USERNAME 注入 host_user 的 external_login
- T2-18：`_build_retry_env` 路径 A（有全局 bot username）时 BOT_USERNAME 注入全局 bot username（回归保护）

### 9.10 P2-5: 过期 token 发评论必然 401

**R10-10**：R10 issue 回帖用过期 token，评论发不出去。

**修复要求**：保留 best-effort 行为（已有 except 兜底），确保 error_code 已写入 task errors 供前端展示。

**测试用例**：
- T2-19：R10 触发后 task errors 包含 `error_code=TOKEN_EXPIRED`（列表格式），issue 回帖失败不影响主流程（不抛异常）

### 9.11 P2-7: create_new_repo GitHub 硬编码路径 B

**R10-11**：create_new_repo GitHub 分支 `_use_path_b = True` 硬编码，与 host_repo 逻辑不一致。

**修复要求**：改为 `_use_path_b = not gh_bot_token`。

**测试用例**：
- T2-20：`create_new_repo` GitHub 分支有全局 GitHub bot token 时 `_use_path_b=False`（走路径 A）
- T2-21：`create_new_repo` GitHub 分支无全局 GitHub bot token 时 `_use_path_b=True`（走路径 B）

---

## 十、第三轮前端深度审计发现的问题与修复需求

> 以下问题在前端 R7 降级逻辑逐行审计中发现，涉及 app.js / register.html / auth.js / account.js 四个文件。分为三类根因：A. R7 前端降级漏改、B. 动态文案未走 i18n、C. startCountdown 倒计时文案错误 + data-i18n 破坏。

### 10.1 问题一（P1）：登录页未配 OAuth App 时隐藏 GitCode/GitHub 按钮

**位置**：app.js `renderOAuthButtons()` L1568-1571

**现象**：`configured.length === 0` → `section.style.display = 'none'` 直接隐藏整个 OAuth 区域。

**需求**：R7.5「按钮始终显示，未配时点击弹提示」。

**影响**：未配 OAuth App 的环境用户在登录页看不到 GitCode/GitHub 按钮，无法引导用户用用户名+密码注册后绑定 token。

**修复要求**：`configured.length === 0` 时不隐藏 section，保留预渲染按钮，点击时 `oauthLogin` catch 弹提示。

**测试用例**：
- T3-01：`renderOAuthButtons` 在 `configured.length === 0` 时不设置 `section.style.display = 'none'`
- T3-02：`oauthLogin` catch 时使用 `oauth_not_configured_hint` 文案（非 `oauth_not_configured`）

### 10.2 问题二（P2）：无邮箱注册按钮硬编码中文"注册"

**位置**：register.html L237 `span.textContent='注册'`

**现象**：无邮箱配置时按钮文案硬编码中文"注册"，不走 i18n。英文环境显示中文。且 `data-i18n` 属性未更新（仍为 `app.login.send_code`），语言切换时 `I18N.apply()` 覆盖回"发送验证码"。

**修复要求**：设置 `data-i18n='app.login.register_btn'` + `textContent = Auth.t('app.login.register_btn')`。

**测试用例**：
- T3-03：register.html 无邮箱配置时按钮 `data-i18n` 设置为 `app.login.register_btn`（非 `app.login.send_code`）
- T3-04：register.html 无邮箱配置时按钮文案走 `Auth.t('app.login.register_btn')`（非硬编码中文）

### 10.3 问题三（P2）：倒计时结束按钮显示"验证码已重新发送"

**位置**：auth.js `startCountdown()` L229

**现象**：倒计时结束 `sb.textContent = t('app.login.toast_code_resent')`（"验证码已重新发送"），应为 `t('app.login.resend_code')`（"重新发送"）。toast 文案当按钮文案语义不对。register.html 和 forgot.html 都调用 startCountdown，都有此问题。

**修复要求**：倒计时结束后恢复按钮原始 `data-i18n` + 对应文案（`resend_code`）。

**测试用例**：
- T3-05：`startCountdown` 倒计时结束后按钮文案为 `resend_code`（"重新发送"），不是 `toast_code_resent`（"验证码已重新发送"）

### 10.4 问题四（P1）：account.js 绑定弹窗未实现 R7.4

**位置**：account.js `_accountBindProvider()` L492-526

**现象**：始终显示 OAuth tab + 手动令牌 tab，不检查 provider `configured` 状态。未配 OAuth App 时用户看到 OAuth tab 默认选中，点"授权"→ 400 报错。

**需求**：R7.4「`configured=false` 只显示手动令牌输入，不显示 tab 切换」。

**修复要求**：`_accountBindProvider` 检查 `_accountProvidersData` 中 provider 的 `configured` 状态，未配时只渲染手动令牌表单，不渲染 OAuth tab。

**测试用例**：
- T3-06：`_accountBindProvider` 在 provider `configured=false` 时不生成 `bindTabOauth` / `bindPanelOauth` 元素
- T3-07：`_accountBindProvider` 在 provider `configured=true` 时生成 OAuth tab + 手动令牌 tab（回归保护）

### 10.5 问题五（P2）：oauthLogin 未配提示文案不符合需求

**位置**：app.js `oauthLogin()` catch L1540 + locale `oauth_not_configured`

**现状文案**："OAuth未配置，请在管理界面配置"

**需求文案**："请使用用户名+密码注册登录，然后在设置页绑定 token"

**影响**：引导用户找管理员，与"零 admin 配置"目标矛盾。

**修复要求**：新增 `oauth_not_configured_hint` i18n key，`oauthLogin` catch 优先使用新文案。

**测试用例**：
- T3-08：zh/app.json 包含 `oauth_not_configured_hint` 且文案含"用户名+密码"
- T3-09：en/app.json 包含 `oauth_not_configured_hint` 且文案含 "username + password"

### 10.6 问题七（P2）：startCountdown 倒计时期间破坏 data-i18n

**位置**：auth.js `startCountdown()` L223-231

**现象**：倒计时期间 `sb.textContent = "60s" / "59s"...` 覆盖了原始按钮文案，倒计时期间不保护 `data-i18n` 属性，结束后设为 `toast_code_resent`。语言切换时 `I18N.apply()` 因 `data-i18n` 仍指向原始 key 会覆盖倒计时数字。

**修复要求**：倒计时开始时保存原始 `data-i18n` + `textContent`，倒计时期间移除 `data-i18n`（避免 I18N.apply 覆盖数字），结束后恢复 `data-i18n` + 原始文案。

**测试用例**：
- T3-10：`startCountdown` 倒计时期间按钮 `data-i18n` 被移除（I18N.apply 不覆盖倒计时数字）
- T3-11：`startCountdown` 倒计时结束后 `data-i18n` 恢复为原始值

### 10.7 问题八（P1）：注册页无邮箱配置时闪动

**位置**：register.html `checkEmailConfigured()`

**现象**：页面初始 HTML 渲染时邮箱字段、步骤指示器、验证码区域、发送验证码按钮全部可见。`API.get('/auth/providers')` 异步返回后才隐藏——用户看到元素先显示再消失，产生闪动。

**修复要求**：页面初始状态为"无邮箱模式"（邮箱字段、步骤指示器、验证码区域 `display:none`，按钮显示"注册"）。API 返回有邮箱时才显示。

**测试用例**：
- T3-12：register.html 邮箱字段初始 `display:none`（无邮箱模式默认）
- T3-13：register.html 步骤指示器初始 `display:none`
- T3-14：register.html `checkEmailConfigured` 返回 `email_configured=true` 时移除 `display:none`（显示邮箱字段）
- T3-15：register.html `checkEmailConfigured` 返回失败时保持初始隐藏状态（无闪动）

---

## 十一、第四轮审计：R10 issue 回帖去重

> 审查报告指出：同一 issue 多个 task 401 失败时，每次都发"Token 已过期"回帖，导致 issue 下多条重复评论。

### 11.1 P2-1: R10 issue 回帖无去重

**位置**：src/runtime/service.py `fail_task` R10 逻辑

**现象**：同一 issue 的多个 task 因 token 过期 401 失败时，每个 task 的 `fail_task` 都会发一条"⚠️ Token 已过期"评论到 issue。一个 issue 5 个 task 失败 = 5 条相同提示，刷屏用户体验差。

**根因**：R10 回帖逻辑直接调 `_post_result_to_issue` 发帖，未检查 issue 是否已有相同的过期提示评论。`_post_result_to_issue` 自带的去重逻辑匹配的是 `[AGENT_TYPE Agent]` 前缀，R10 消息格式是 `⚠️ Token 已过期`，不匹配现有去重条件。

**修复要求**：发帖前查 issue 已有评论，若已含"Token 已过期"关键词则跳过。查询失败不阻断发帖（降级为原行为）。

**修复方案**：新增 `_fetch_issue_comments(repo_key, repo, issue_num, token)` 辅助函数（按 provider 分流查询评论列表），`fail_task` R10 回帖前调此函数检查去重。

**测试用例**：
- T4-01：`fail_task` R10 发帖前查 issue 已有评论是否含"Token 已过期"关键词
- T4-02：已有评论含"Token 已过期"时跳过发帖（`_already_notified = True`）
- T4-03：已有评论不含"Token 已过期"时正常发帖（`_already_notified = False`）
- T4-04：`_fetch_issue_comments` 按 provider 分流（GitHub → `_github_req`，GitCode → `urllib.request`）
- T4-05：`_fetch_issue_comments` 查询失败返回空列表（不抛异常）

---

## 十二、第五轮全链路模拟发现的问题

> 沿"注册 → PAT 绑定 → 托管 → webhook 事件 → task 入队 → worker 认领 → 引擎执行 → git/issue 操作 → 过期恢复"逐环节走链路，实测远程 worker 侧架构。

### 12.1 P0-1：路径 D × 远程 worker git worktree 全链路断裂

**位置**：src/runtime/service.py `claim()` + src/runtime/agent.py `_get_task_token()`

**现象**：路径 B/D 场景 task 行 `repo_gitcode_token` 刻意写空（防过期 token 落库）。本地引擎运行时 `_get_task_token` 实时查主控 DB 取 token——但远程 worker 用自己的本地 `sessions.db`（空库，无 managed_repos / external_identities），`_get_task_token` 回退查本地 DB 返回空 → `repogit.ensure_issue_worktree` / `_submit_linked_repo_prs` / worktree 清理全部拿空 token → 私有仓库匿名 clone/push 失败。

**根因**："运行时实时获取"只在主控机本地引擎有效，远程 worker 没有主控 DB。引擎内部的 git 操作走 grape CLI → 服务端 git-credential（路径 D 降级通），但 worker 自身的 worktree 准备是独立代码路径，漏了。

**影响**：零管理员部署 + 远程 worker + 私有仓库 = coding agent 完全无法 clone/push。这是 grape 标准部署形态，P0 级场景断裂。

**修复要求**：`claim` 响应中服务端现取 token（带刷新）注入 task dict（不落库），worker 拿到新鲜 token 直接用 `task["repo_gitcode_token"]`，无需查本地 DB。

**测试用例**：
- T5-01：`claim` 返回的 task dict 中 `repo_gitcode_token` 在路径 D 场景（task 行为空）被注入新鲜 token
- T5-02：`claim` 注入 token 不落库（agent_tasks 表 repo_gitcode_token 仍为空）
- T5-03：`claim` 注入 token 按 repo_key 前缀分流（GitHub → get_github_token，GitCode → get_repo_token_with_refresh）

### 12.2 P1-1：R10 GitHub 过期回帖用错 token 链

**位置**：src/runtime/service.py `fail_task` R10 逻辑 `_comment_token` 获取

**现象**：`_comment_token = get_repo_token_with_refresh(_rk)` 没有 `github:` 前缀分流。GitHub 路径 D 仓库 token 过期时，拿 GitCode 链的 token（或空）发 GitHub 评论 → 必失败。与已修的 6.9 同型错误，出现在另一处。

**修复要求**：按 repo_key 前缀分流取 token（GitHub → `get_github_token`，GitCode → `get_repo_token_with_refresh`）。

**测试用例**：
- T5-04：`fail_task` R10 `_comment_token` 对 GitHub 仓库（`github:` 前缀）调 `get_github_token`
- T5-05：`fail_task` R10 `_comment_token` 对 GitCode 仓库调 `get_repo_token_with_refresh`

### 12.3 P1-2：B→A 演进场景（兼容修复 + 自动升级）

**现象**：先零管理员部署（用户托管 N 个仓库，host_user_id>0、bot 未入协作组）→ 后来管理员配了全局 bot token。原设计中 `get_repo_token_with_refresh` 命中路径 C（全局 bot）→ 但 bot 不是协作者 → issue 建单/评论全 403。

**根因**：token 获取链路自动切换到全局 bot token，但 bot 账号从未被邀请进路径 B 仓库协作组——权限没同步切换。

**修复（两层方案）**：

**第一层 P0：兼容修复**——路径 B 仓库（host_user_id > 0）跳过全局 bot token 层，继续走用户 token。全局 bot token 配置后路径 B 仓库不 403，仍正常工作。

**第二层 P2：自动升级**——管理员配置全局 bot token 时（从无到有），异步遍历路径 B 仓库，best-effort 邀 bot 为协作者 + 写入 bot_gitcode_token。失败的仓库跳过（兼容修复兜底）。详见设计文档 §13。

**测试用例**：
- T8-01：路径 B 仓库（host_user_id > 0）在全局 bot token 配置后，`get_repo_token_with_refresh` 返回用户 token（非全局 bot token）
- T8-02：路径 A 仓库（bot_gitcode_token 非空）在全局 bot token 配置后，`get_repo_token_with_refresh` 返回 bot_gitcode_token（回归保护）
- T8-03：新仓库（无 managed_repos 记录，host_user_id = 0）全局 bot token 层正常生效
- T8-04：`get_repo_bot_token` 路径 B 仓库跳过全局 bot token 层，返回用户 token
- T8-05：`get_github_token` 路径 B 仓库跳过全局 GitHub bot token 层，返回用户 token
- T8-06：repair_host 后路径 B 仓库 bot_gitcode_token 被写入，`get_repo_token_with_refresh` 第 2 步命中（自动切路径 A）
- T8-07：`save_config` 写入 `gitcode_api_token`（从无到有）时触发异步升级
- T8-08：`save_config` 写入 `github_bot_token`（从无到有）时触发异步升级
- T8-09：`save_config` 更新已有 `gitcode_api_token`（有→有）时不触发升级
- T8-10：自动升级遍历路径 B 仓库，邀 bot + 写 bot_gitcode_token
- T8-11：自动升级失败的仓库（host token 过期）跳过，不阻断
- T8-12：自动升级后仓库 `get_repo_token_with_refresh` 返回 bot token（路径 A）

### 12.4 P0：webhook_url 自动发现三级探测在云服务器上全部静默失败

**现象**：全新部署 → 用户注册（R7 无邮箱）→ 绑定 PAT 成功（R8）→ 点"托管"→ host_repo 报 400 WEBHOOK_URL_NOT_CONFIGURED。绑定成功与托管失败相邻出现，用户无法理解为什么"凭据都好了还失败"——缺的不是用户侧的东西，而是服务端自己的回调地址。

**根因链（代码逐级核实）**：

`get_webhook_url()`（db.py:2554）只有两个来源：configs/server-config.json → DB meta.webhook_url → 空串。写入依赖启动时的自动发现（config.py:230 `_detect_server_base`）：

- 第 1 级 server-config.json：gitignored，新部署不存在 → 跳过
- 第 2 级外网 IP 探测（api.ipify.org ×2 / api.my-ip.io）：华为云 ECS 出站受限 → 全部 Connection refused → bare except 静默吞掉
- 第 3 级 hostname 兜底：`socket.gethostbyname(hostname)` 解析到 127.0.0.1（/etc/hosts 回环映射），代码排除 127.* → 跳过
- 全部失败后无警告、无状态标记，webhook_url 保持空

host_repo 的空值守卫（§6.7 加的，本身是对的——防止用空 URL 在 GitCode 注册永远打不通的坏 webhook）把失败暴露给用户，但报错文案指向"系统设置"（管理员入口），与"零管理员、一个 PAT 即可用"的产品承诺直接矛盾。

**中间件 JWT 解析 bug（实施 R11.2 时发现）**：`request_id_and_log_middleware`（main.py:700）用 `validate_token`（内存缓存 `_user_tokens`）解析 user_id。但注册/登录返回的是 JWT token（`_issue_jwt` → `create_access_token`），不走 `create_session_token`，JWT 不在 `_user_tokens` 里 → `validate_token` 返回 None → `_uid` 为空 → webhook_url Host 头推导不触发。同一个请求在依赖层（`get_current_user` → `verify_access_token` JWT 验证）认证成功 200，但中间件层 `_uid` 为空。修复：中间件改用 `verify_access_token` 解析 JWT 提取 user_id，与依赖层一致。

**为什么不合理**：
- 自动发现的意图存在，但对"云上出站受限"这一最常见的国内部署形态全部失效，且失效不可见
- 缺少最可靠的发现信号：服务器每天都在收用户的 HTTP 请求，每个请求的 Host 头就是自己对外可达的地址，这个信号零网络依赖、在代理/云环境都准，代码完全没用
- 失败提示发生在错误的时机（托管时）而非最早的可用时机，且没有告诉用户"系统试过什么、为什么失败、怎么配"

**修复方案（R11）**：

**核心思路**：用户访问 grape Web UI 时，请求的 Host 头就是服务器对外可达的地址。用户能用这个地址访问我们，GitCode 就能用同一个地址推 webhook（同一台服务器、同一个端口）。零网络依赖、零出站探测、在代理/云环境都准。

**R11.1：自动发现阶梯补全（按可靠性排序，逐级回退）**：

1. 显式配置（server-config.json / 设置页）— 最高优先级，有则跳过所有探测
2. DB meta.webhook_url（历史配置）— 已有值则跳过探测
3. 入站 Host 头探测（新增，最可靠）：已认证用户请求到达时，从 Host 头推导 `http(s)://<host>[:port]/webhook/gitcode`，写入 DB meta。安全约束：只采信已认证会话请求（Bearer token 验证通过），不采信匿名请求，防 Host 头伪造污染 webhook_url

   **nginx 代理场景处理**：
   - 优先读 `X-Forwarded-Host`（nginx 配 `proxy_set_header X-Forwarded-Host $http_host;` 时含端口），fallback 到 `Host` 头
   - 读 `X-Forwarded-Proto` 判断 https（nginx 配 `proxy_set_header X-Forwarded-Proto $scheme;`）
   - 直连场景：Host 头含 `IP:port` → 推导含端口
   - nginx 代理标准 80 端口：Host 头只有域名（无端口）→ 推导 `http://域名/webhook/gitcode` → 正确
   - nginx 代理非标准端口：需 nginx 配 `X-Forwarded-Host $http_host`（含端口），否则端口丢失

   **GitHub 仓库支持**：落库的 webhook_url 用 `/webhook/gitcode` 后缀，host_repo GitHub 分支用 `replace("/webhook/gitcode", "/webhook/github")` 转换（已有逻辑），无需探测时区分平台。

4. 外网 IP 服务（保留现状，启动时探测）：对公网直连环境有效，出站受限时静默失败

不设回环自检——服务端 GET 自身路由只能证明本地路由存在，不能证明公网可达。真正的验证交给 GitCode：host_repo 创建 webhook 时把 URL 注册到 GitCode，如果 URL 不可达 GitCode API 会返回错误，用户看到的是"webhook 创建失败"而非"webhook_url 未配置"——报错时机更准确。

**R11.2：失败可见性**：

启动时外网 IP 探测每级失败输出 WARNING（列出失败原因），全部失败时在 meta 写 `webhook_url_detect_failed=1` 标记。入站 Host 头探测成功后清除该标记。

**R11.3：报错时机与指导**：

host_repo 的 WEBHOOK_URL_NOT_CONFIGURED 响应附带结构化 `guidance` 字段：已尝试的发现方式及失败原因 + 配置入口（设置页自服务，不指向管理员页）+ 示例值格式（`http://<公网IP>/webhook/gitcode`）；前端 toast 展示该指导而非泛化 400 文案。

**用户流程（修复后）**：打开 Web UI → 登录 → 绑定 PAT → 点托管 → 成功（中间件已从 Host 头自动发现 webhook_url）。零手动配置。

**测试用例**：
- T9-01：出站受限环境启动 → 外网 IP 探测每级失败有 WARNING 日志 + meta 写 `webhook_url_detect_failed`
- T9-02：已认证用户请求到达 → 入站 Host 头探测成功 → webhook_url 自动落库 → 后续托管直接成功（无 400）
- T9-03：匿名请求伪造 Host（Host: evil.com）→ webhook_url 不被污染（只采信已认证请求）
- T9-04：全部探测失败（启动失败 + 无已认证请求）→ 托管时 400 响应带 guidance 字段（含已尝试方式 + 配置入口 + 示例值）
- T9-05：显式配置存在时 → 跳过所有探测，行为与现状一致（向前兼容）
- T9-06：meta 已有 webhook_url 时 → 跳过 Host 头探测（幂等，不重复写）
- T9-07：Host 头含非标准端口（如 `1.2.3.4:3000`）→ 推导的 webhook_url 含端口
- T9-08：Host 头探测成功后 → meta 中 `webhook_url_detect_failed` 标记被清除
- T9-09：nginx 代理场景，`X-Forwarded-Host` 含端口 → 推导的 webhook_url 含正确端口
- T9-10：nginx 代理场景，`X-Forwarded-Proto: https` → 推导的 webhook_url 用 https 协议
- T9-11：公网 IP 直连访问（无 nginx）→ Host 头含 `公网IP:端口` → webhook_url 正确推导
- T9-12：GitHub 仓库托管时，Host 头探测写入的 `/webhook/gitcode` 后缀被正确 replace 为 `/webhook/github`
- T9-13：nginx 代理标准 80 端口（Host 头只有域名无端口）→ 推导 `http://域名/webhook/gitcode`（端口 80 省略，正确）
- T9-14：域名直连访问（无 nginx）→ Host 头含 `域名:端口` → 推导 `http://域名:端口/webhook/gitcode`
- T9-15：域名 + https（nginx 代理 + SSL）→ X-Forwarded-Proto: https + X-Forwarded-Host: 域名 → 推导 `https://域名/webhook/gitcode`
- T9-16：域名 + 非标准端口（nginx 代理 8080）→ X-Forwarded-Host: `域名:8080` → 推导含端口 8080
- T9-17：X-Forwarded-Host 优先于 Host 头（nginx 内部地址 vs 公网域名 → 取公网域名）
- T9-18：nginx 未配 X-Forwarded-Host 时 fallback 到 Host 头（向后兼容旧 nginx 配置）
- T9-19：注册/登录后用户请求到达中间件 → 中间件用 JWT 解析 user_id（非 validate_token 内存缓存）→ _uid 非空 → webhook_url 推导触发
- T9-20：JWT 过期或无效时中间件 _uid 为空 → webhook_url 推导不触发（不报错，降级等下一个有效请求）

### 12.5 同类问题：server_base_url / OAuth redirect_uri / A2A re-dispatch / localhost 兜底

> webhook_url Host 头探测修复后，深度排查发现四个同类根因问题：均源于"自动发现断裂"或"硬编码地址"，在零管理员部署 + nginx 代理场景下静默失效。

**P1-A：settings.py webhook_url 有 localhost 兜底（settings.py:464）**

**现象**：`user_db.get_webhook_url() or "http://localhost:8080/webhook/gitcode"` — webhook_url 为空时设置页展示 `http://localhost:8080/webhook/gitcode` 假地址。用户以为已配置但实际不可用（localhost 只在本机可达）。

**修复**：去掉 localhost 兜底，返回空字符串。设置页展示"未配置"状态。

**测试用例**：
- T10-01：webhook_url 为空时 settings 返回空字符串（非 localhost:8080）

**P1-B：events.py A2A re-dispatch 硬编码 localhost:8000（events.py:975）**

**现象**：VOD re-dispatch 用 `f"http://localhost:8000/api/a2a"` 硬编码地址。同机部署 + 端口 8000 没问题，但 grape 默认跑 3000 端口、跨机器时失败。

**修复**：改用 `get_server_base_url() + "/api/a2a"`，与 `_resolve_server_url` 一致。

**测试用例**：
- T10-02：A2A re-dispatch URL 从 `get_server_base_url()` 获取（非硬编码 localhost:8000）
- T10-03：`get_server_base_url()` 有值时 A2A URL 含正确地址
- T10-04：`get_server_base_url()` 为空时 A2A URL fallback 到 localhost（不阻断，兼容本地开发）

**P0-C：OAuth redirect_uri 不读 X-Forwarded-Host（providers.py:421）**

**现象**：`_get_trusted_host` 只读 `Host` 头。nginx 代理场景下 Host 是 `127.0.0.1:3000` → OAuth redirect_uri 构造为 `http://127.0.0.1:3000/api/auth/callback/gitcode` → GitCode OAuth 回调到内网地址 → 失败。与 webhook_url Host 头探测完全同型问题。

但已有 `TRUSTED_HOSTS` 环境变量白名单保护——配了 TRUSTED_HOSTS 时不用 Host 头。零管理员部署不配 TRUSTED_HOSTS 时走 fallback 到 Host 头，nginx 代理场景下拿到内网地址。

**修复**：`_get_trusted_host` 优先读 `X-Forwarded-Host`，fallback 到 `Host` 头（与 `detect_webhook_from_host` 一致）。`TRUSTED_HOSTS` 白名单校验照常生效——校验的是最终值，不管来自哪个 header。

**测试用例**：
- T10-05：nginx 代理场景，X-Forwarded-Host 含公网域名 → redirect_uri 用公网域名
- T10-06：无 X-Forwarded-Host 时 fallback 到 Host 头
- T10-07：TRUSTED_HOSTS 白名单校验 X-Forwarded-Host 的值（不在白名单 → 用 fallback）
- T10-08：X-Forwarded-Host 是 localhost 时排除（与 detect_webhook_from_host 一致）

**P2-D：_detect_server_base 硬编码 8080 端口（config.py:242）**

**现象**：外网 IP 探测成功后返回 `f"http://{ip}:8080"`，但 grape 默认跑 3000 端口（nginx 反代 80→3000）。探测到的地址带错误端口 → webhook_url 和 server_base_url 都错。

**修复**：从 `GRAPE_PORT` 环境变量读取端口（已有），无则默认 3000（grape 实际默认端口），不用 8080。

**测试用例**：
- T10-09：`_detect_server_base` 探测成功时端口从 `GRAPE_PORT` 环境变量读取
- T10-10：`GRAPE_PORT` 未设置时默认 3000（非 8080）
- T10-11：`GRAPE_PORT=8080` 时探测地址含 8080 端口（向前兼容）

**补充：剩余硬编码 localhost:8080 fallback（同根因排查）**

深度排查发现 `_detect_server_base` 端口修复后，还有三处硬编码 `localhost:8080` / `localhost:80` 作为最终 fallback，端口同样错误：

**P2-E：get_server_base_url 最终 fallback 硬编码 localhost:8080（db.py:2551）**

**现象**：`get_server_base_url()` 所有来源（server-config.json / meta / webhook_url 剥后缀）都为空时返回 `http://localhost:8080`。grape 默认 3000 端口 → 远程 worker A2A 调用走 8080 → 失败。

**修复**：改为 `http://localhost:{GRAPE_PORT or 3000}`，与 `_detect_server_base` 端口一致。

**P2-F：prompt_builder _resolve_server_url 异常 fallback 硬编码 localhost:8080（prompt_builder.py:72）**

**现象**：`_resolve_server_url` 异常时返回 `http://localhost:8080`。本地引擎走 `127.0.0.1:{_local_port()}`（已正确用 `GRAPE_MAIN_PORT`），但异常 fallback 仍用 8080。

**修复**：改为 `http://localhost:{_local_port()}`，复用已有的 `_local_port()` 函数。

**P2-G：providers.py Host header default 硬编码 localhost:80（providers.py:427）**

**现象**：`request.headers.get("Host", "localhost:80")` — 请求无 Host 头时 default `localhost:80`。HTTP/1.1 要求 Host 头必传，但 unit test 或非标准 client 可能不传。default 80 端口与 grape 3000 不一致。

**修复**：default 改为空字符串，由 `_get_trusted_host` 逻辑处理空值（返回空 → `get_redirect_uri` 会报错或走 provider 配置）。

**测试用例**：
- T10-12：`get_server_base_url()` 所有来源为空时返回 `http://localhost:3000`（非 8080）
- T10-13：`get_server_base_url()` fallback 端口从 `GRAPE_PORT` 读取
- T10-14：`_resolve_server_url` 异常 fallback 用 `_local_port()`（非硬编码 8080）
- T10-15：`providers.py` Host header default 为空字符串（非 localhost:80）

### 12.6 路径 A bot 协作者失败不降级到路径 B

> 路径 A（有全局 bot token）的 bot 协作者流程假设 bot token 有效且 bot 用户存在，但实际环境中 bot token 可能过期/无效/被撤销，bot 用户名可能不存在。这些场景下应降级到路径 B（用户 token 建 webhook），而不是直接报错终止。

**P0-F：host_repo GitCode 路径 A 邀 bot 失败 → 502 不降级（repos.py L949-985）**

**现象**：三个失败点全部直接 raise 不降级：
1. `add_collaborator` 失败 + `get_member_permission` 兜底也失败 → raise 502
2. `get_member_permission` 兜底抛非限流异常（401/404 RuntimeError）→ 没 catch → 穿透 502
3. bot 权限校验 `access_level < 40` → raise 502

**修复**：bot 协作者流程任一步失败时，只要用户 token 有效，降级到路径 B（设 `_use_path_b=True`，`webhook_token=api_token`，生成随机 `_webhook_secret`），继续建 webhook 完成托管。

**P0-G：repair_host GitCode 路径 A 邀 bot 失败 → return error 不降级（repos.py L1089-1135）**

**现象**：bot 权限不足 → `add_collaborator` + `accept_invite` → `get_member_permission` 重试 → 仍失败 → return `{"status": "error", "host_status": "bot_missing"}`。不降级到路径 B。

**修复**：`bot_ok=False` 时降级到路径 B（设 `_use_path_b=True`，`_wh_token=api_token`，生成 `_webhook_secret`），继续建 webhook。

**P1-H：repair_host GitCode 路径 A add_collaborator/accept_invite 无 catch（repos.py L1127-1128）**

**现象**：`add_collaborator(api_token, ...)` 和 `accept_invite_for_repo(bot_token, ...)` 没有 try/except，bot_token 无效或 bot_username 不存在时抛 RuntimeError → 穿透到外层 except → 502。

**修复**：包 try/except，失败时设 `bot_ok=False` 降级到路径 B。

**P1-I：host_repo GitHub 路径 A _try_github_bot_collaborator 抛异常 → 502（repos.py L717）**

**现象**：`get_github_repo_permissions(bot_token, owner, repo)` 在 bot_token 无效时抛 RuntimeError → 调用方 except Exception → 502。

**修复**：`_try_github_bot_collaborator` 内部 catch 异常，返回 `{"bot_collaborator_needed": True, "bot_status": "error"}`，不抛异常。调用方检测 `bot_status == "error"` 时降级到路径 B。

**测试用例**：
- T11-01：host_repo GitCode 路径 A `add_collaborator` 失败 → 降级到路径 B（设 `_use_path_b=True`）
- T11-02：host_repo GitCode 路径 A `get_member_permission` 抛非限流异常 → 降级到路径 B
- T11-03：host_repo GitCode 路径 A bot 权限 `access_level < 40` → 降级到路径 B
- T11-04：repair_host GitCode 路径 A bot 权限不足 → 降级到路径 B（不 return error）
- T11-05：repair_host GitCode 路径 A `add_collaborator` 抛异常 → catch + 降级到路径 B
- T11-06：host_repo GitHub 路径 A `_try_github_bot_collaborator` 异常 → 不抛 + 降级到路径 B
- T11-07：路径 A bot 协作者成功 → 不降级（保持路径 A，回归保护）
- T11-08：host_repo GitHub 路径 A `_try_github_bot_collaborator` 返回 `bot_status=error` → 降级到路径 B（设 `_gh_use_path_b=True`，`bot_github_token=""`，`host_user_id=user["id"]`）
- T11-09：降级后 `get_github_token` 不命中 `bot_github_token`（空），走 `host_user_id` 降级 → 返回用户 token（非无效 bot token）

### 12.7 pipeline dedup guard 创建后异常不释放（pre-existing 缺陷）

> 非 #372 引入，是 pipeline 已有的 dedup guard 机制缺陷。在 #372 零管理员部署场景下更容易暴露（路径 B 环境 project-info worktree 可能因分支名不匹配而失败）。

**根因**：`run_pipeline`（event_pipeline.py）在 L1799 创建 `event_active_guard` 后，到 `return result` 之间没有 try/finally 包裹。如果中间代码抛异常（project-info worktree 失败、_enqueue_pipeline_task 内部异常穿透等），guard 留在 DB 不释放。后续所有同一 issue 的 webhook 事件被 dedup skip 跳过，事件永远卡在 pending。

现有失败路径（pick_engine 失败、场景引擎全灭等）都调 `_fail_event` → `_release_guard_by_seq`——这些有释放。但 guard 创建到 pick_engine 之间的代码，以及未被 `_fail_event` 覆盖的异常路径，不释放。

**修复**：
1. `run_pipeline` guard 创建后到 return 之间包 try/except，异常时调 `_fail_event` 释放 guard
2. `purge_stale_event_guards` TTL 从 6 小时缩短到 30 分钟（活跃 guard 不应存活这么久）

**测试用例**：
- T12-01：guard 创建后 pipeline 异常 → `_fail_event` 调用 → guard 释放
- T12-02：`purge_stale_event_guards` TTL 30 分钟（非 6 小时）
- T12-03：guard 创建后正常完成 → guard 释放（回归保护）

### 12.8 PAT 绑定漏写 unified_user_mapping（issue.user_id=0 根因，8 环节受影响）

**根因**：PAT 绑定（PUT /api/settings/credentials 的 gitcode_token/github_token 分支）写入 `external_identities` 后，没有同步写入 `unified_user_mapping`。启动时 `session_manager` 的回填逻辑用 `grape_accounts.username`（grape 用户名）而非 GitCode 用户名写映射，不匹配 webhook actor。

PAT 绑定的用户用用户名+密码注册（R7 无邮箱），自定义 username ≠ GitCode login → 回填也无效。

**8 个受影响环节（同一根因）**：

| ID | 环节 | 读取点 | 影响 |
|----|------|--------|------|
| I1 | issue 创建 | `_get_user_id_by_gitcode(actor)` → `issue.user_id` | user_id=0 → 我的需求看不到 |
| E1 | 引擎选择 | `_repo_user_id(repo_key, issue_creator=actor)` → `scene_uid` | scene_uid=0 → 场景引擎绑定可能选错 |
| S1 | issue session | `session_store` 按 assignee 查 mapping → `user_id` | session.user_id=0 → 会话无法关联用户 |
| O1 | onboarding | `_get_user_id_by_gitcode(owner)` → `scene_uid` | scene_uid=0 → 场景绑定失败 |
| T1 | tasks API | `_get_user_id_by_gitcode` → `user_id` | task.user_id=0 → 任务无法关联用户 |
| Q1 | 我的需求查询 | 按 grape user_id 查 `issue.user_id` | 查不到 user_id=0 的 issue |
| D1 | dashboard 统计 | 按 user_id 查统计 | user_id=0 的 issue/task 不计入 |
| G1 | GitHub PAT 绑定 | 同 GitCode PAT | GitHub webhook actor 同样查不到 mapping |

2 个不受影响环节：A2A 鉴权（用 host_user_id/bot_token）、token 获取（用 host_user_id → grape_account_by_id）。

**修复**：PAT 绑定成功后同步写入 `unified_user_mapping`：
- GitCode：`INSERT OR IGNORE` + `UPDATE gitcode_user, gitcode_nickname`（不覆盖 github_login 等其他平台字段）
- GitHub：`INSERT OR IGNORE` + `UPDATE github_login`（不覆盖 gitcode_user 等其他平台字段）

**注意**：不能用 `INSERT OR REPLACE`——该表主键是 `user_id`，一个用户一行平铺多平台字段。`INSERT OR REPLACE` 会删整行重插，未传字段被 DEFAULT 覆盖为空。用户先绑 GitCode 再绑 GitHub 会丢失 `gitcode_user`。

一处修复解决全部 8 个环节的问题。

**测试用例**：
- T13-01：PAT 绑定成功后写入 `unified_user_mapping`（gitcode_user = GitCode login, user_id = grape user id）
- T13-02：PAT 绑定更新（已有记录）时 `unified_user_mapping` 也更新（INSERT OR REPLACE 覆盖）
- T13-03：GitHub PAT 绑定后写入 `unified_user_mapping.github_login`
- T13-04：PAT 绑定写入的 `gitcode_user` 是 GitCode API 返回的 `gc_login`（非 grape_accounts.username）
- T13-05：pipeline actor 查 `_get_user_id_by_gitcode(gc_login)` → 命中 `unified_user_mapping` → 返回正确 user_id
- T13-06：用户先绑 GitCode PAT 再绑 GitHub PAT → 两字段都保留（gitcode_user + github_login 不互相覆盖）
- T13-07：用户先绑 GitHub PAT 再绑 GitCode PAT → 两字段都保留（顺序无关）

---

## 十三、平台入口始终可见

### 13.1 需求

无论是否配置了 OAuth App，GitCode/GitHub 平台入口在所有页面都应该默认可见：

- **登录页**：GitCode/GitHub 按钮始终显示，未配 OAuth App 时点击弹提示引导注册（R7.5 已实现）
- **注册页**：GitCode/GitHub 关联入口始终可见（预渲染，不依赖 configured 状态）
- **账户设置页**：第三方账号列表始终显示 GitCode/GitHub 行，未配 OAuth App 时点击绑定走手动令牌输入（R7.4 已实现绑定弹窗分流，但 provider 列表渲染漏改——filter configured 过滤掉了未配平台）

### 13.2 问题：account.js provider 列表过滤掉未配平台

**位置**：account.js `_accountRenderSecurity()` L176

**现象**：`var providers = (providersData.providers || []).filter(function(p) { return p.configured; });` 只显示 `configured=true` 的平台。未配 OAuth App 时 GitCode/GitHub 不出现在设置页，用户无法手动绑定 PAT。

**修复要求**：去掉 `configured` 过滤，所有 provider 都显示。`configured` 状态只在 `_accountBindProvider` 中区分走 OAuth 还是手动令牌（R7.4 已实现）。

**测试用例**：
- T6-01：account.js provider 列表渲染不过滤 `configured`（所有 provider 都显示）
- T6-02：未配 OAuth App 时 GitCode/GitHub 仍出现在第三方账号列表中
- T6-03：`_renderBindingGuide`（新手引导页）不过滤 `configured`，所有平台卡片始终显示
- T6-04：`renderOAuthButtons` 用全部 providers 渲染（部分配置场景不丢失未配平台按钮）

---

## 十四、用户菜单统一：反馈/帮助入口 + 工作区用户下拉

### 14.1 需求

所有页面的用户菜单统一下拉框内容，保持一致的入口结构：

1. **侧边栏底部（工作区）**：已有用户菜单（账号设置 + 账号安全 + 退出），在菜单底部增加「反馈」和「帮助」入口
2. **引导页/Lobby（我的项目页面）**：已有用户菜单（用户设置 + 退出），增加「反馈」和「帮助」入口
3. **组织管理页**：全屏页面无用户菜单，按 Lobby 同款增加用户下拉框（含用户设置 + 反馈 + 帮助 + 退出）
4. **工作区 Toolbar**：在组织下拉后面增加用户设置下拉框，样式与我的项目页面（Lobby）用户下拉一致。反馈/帮助统一在用户下拉菜单中，toolbar 不再放独立按钮（避免重复入口）。原来侧边栏底部的账号设置菜单也不变

### 14.2 统一菜单项

所有用户下拉框的菜单项统一为（按顺序）：
- 👤 用户设置（跳转 account/profile）
- 🔒 账号安全（跳转 account/security）— 工作区侧边栏已有，其他页面也补上
- 💬 反馈（打开反馈对话框 openFeedbackDialog）
- 📖 帮助（打开帮助抽屉 app.openHelp）
- ↩ 退出登录

反馈/帮助只在用户下拉菜单中存在，toolbar 和侧边栏顶部不再放独立按钮——所有用户操作集中在一个入口，避免重复。

### 14.3 实现要点

- **侧边栏 `_loadUserMenu`**：在退出登录前插入反馈 + 帮助两个菜单项
- **Lobby `_loadLobbyUserMenu`**：在退出登录前插入账号安全 + 反馈 + 帮助三个菜单项
- **组织管理页 `renderOrganizations`**：不再在页面内渲染用户下拉（toolbar 全局 `tbUserBtn` 已覆盖）
- **工作区 Toolbar**：在 `tbOrgBtn` 后面增加 `tbUserBtn`（用户头像+名称+下拉箭头），下拉框内容与侧边栏一致
- **移除冗余入口**：toolbar 的 `tbFeedback`（独立反馈按钮）、`help-btn`（侧边栏顶部帮助图标）、`📖 帮助`（toolbar 帮助链接）全部移除——统一到用户下拉菜单

### 14.4 测试用例

- T7-01：侧边栏 `_loadUserMenu` 包含反馈 + 帮助菜单项
- T7-02：Lobby `_loadLobbyUserMenu` 包含账号安全 + 反馈 + 帮助菜单项
- T7-03：组织管理页不重复渲染用户下拉（toolbar 全局用户下拉已覆盖）
- T7-04：工作区 Toolbar 有用户设置下拉框（`tbUserBtn`）
- T7-05：所有用户下拉框菜单项一致（用户设置 + 账号安全 + 反馈 + 帮助 + 退出）
- T7-06：toolbar 不再有独立的反馈/帮助按钮（统一到用户下拉菜单）

---

## 十五、E2E 实测发现的问题与修复需求

### 15.1 P0：_create_direct_task / _create_failed_task 缺 pool_engine_source 列值

**发现场景**：#372 零管理员部署 E2E 实测。用户 PAT 绑定 → 托管仓库 → 创建 issue → pipeline 触发。设 `execution_mode=direct` 后，direct 模式自动选机成功，但 task 记录未落库——agent 在内存中执行，前端看不到执行记录，"我的需求"查不到 task。

**根因**：`_TASK_COLUMNS_LIST`（src/runtime/service.py:374）包含 `pool_engine_source` 列。`_task_insert_row`（src/api/event_pipeline.py:203）对缺列抛 `ValueError: agent_tasks INSERT 缺少列值: ['pool_engine_source']`。`_create_direct_task`（L264）和 `_create_failed_task`（L359）的 `_task_insert_row` 调用未传 `pool_engine_source` 参数 → `ValueError` → 内部 try/except 捕获后返回空字符串 `""` → 调用方不检查返回值直接调 `start_direct_agent` → agent 在内存中执行但 task 记录不在 DB。

**影响**：
- direct 模式下所有 task 创建失败（`_create_direct_task` 返回空 task_id）
- failed task 快照也创建失败（`_create_failed_task` 同样不传 `pool_engine_source`）
- 前端 dashboard / 我的需求 / task 列表全部看不到记录
- 重派时找不到关联 task（task_id 为空）

**归属**：基线 pre-existing bug（`_TASK_COLUMNS_LIST` 加 `pool_engine_source` 列时未同步更新两个 `_task_insert_row` 调用），非 #372 引入。但 #372 零管理员场景（direct 模式 + PAT 绑定）是第一个完整 E2E 触发路径，在此修复。

**修复**：`_create_direct_task` 和 `_create_failed_task` 的 `_task_insert_row` 调用追加 `pool_engine_source=""` 参数。direct 模式的 task 不是广播入队，`pool_engine_source` 为空字符串即可（与 `_enqueue_pipeline_task` 中非广播路径的默认值一致）。

**改动文件**：src/api/event_pipeline.py（2 处）

### 15.2 测试用例

- T8-01：`_create_direct_task` 成功落库 → `SELECT * FROM agent_tasks WHERE id=?` 返回非空，`pool_engine_source=""`
- T8-02：`_create_failed_task` 成功落库 → `SELECT * FROM agent_tasks WHERE id=?` 返回非空，`pool_engine_source=""`
- T8-03：`_create_direct_task` 返回的 task_id 非空字符串（非 `""`）
- T8-04：direct 模式 E2E — pipeline 触发后 agent_tasks 表有记录，event status=running

### 15.3 P2：get_user_identities_with_tokens 未同步 status IN (1,4)

**问题**：`get_user_identities`（不含 token，供前端）已改为 `status IN (1, 4)`（R10 过期 token 也返回）。但 `get_user_identities_with_tokens`（含 token，供后端内部）仍只查 `status=1`。

**影响**：`get_user_identities_with_tokens` 被 `_is_github_user_token_repo`、`get_github_token`、`get_credentials` 三处调用。owner 的 GitHub token 过期（status=4）时查不到 → `_is_github_user_token_repo` 误判为路径 D（用户 token 降级），`get_github_token` 跳过 owner token 层 → 实际降级行为合理（owner token 过期时走用户 token 是正确的），但与 `get_user_identities` 的查询条件不一致，属于设计一致性问题。

**修复**：`get_user_identities_with_tokens` 查询条件改为 `status IN (1, 4)`，与 `get_user_identities` 对齐。调用方使用返回的 token 时，status=4 的 token 已过期（401），API 调用会被拒绝，不影响安全。

**改动文件**：src/auth/grape_account_db.py（1 处）

### 15.4 测试用例

- T9-01：`get_user_identities_with_tokens` 返回 status=1 的绑定
- T9-02：`get_user_identities_with_tokens` 返回 status=4 的绑定（过期 token 也返回）
- T9-03：`get_user_identities_with_tokens` 不返回 status=0 的绑定（已解绑）

### 15.5 P1：全量 collection 下测试 DB 绑定污染

**问题**：`test_issue_372_pool_engine_source.py` 的 T8-01/T8-02 在全量 `pytest tests/ -k ...` 跑法下稳定失败（单独跑或小范围组合跑全过）。

**根因**：90+ 个旧测试模块在 import 时（collection 阶段）执行 `os.environ["DEVOPS_DB_PATH"] = "/tmp/xxx.db"`，覆盖 conftest 设的 `data/test_sessions.db`。`session_manager.db` 连接对象在首次 import 时绑定到当时的 env 值，后续 env 被其他模块改了但连接不重新绑定。全量 collection 时最后 import 的模块决定了 `session_manager.db` 指向哪个文件 → 新测试的 INSERT 写到一个库，断言读的是另一个库。

**修复**：conftest.py 加 function-scope autouse fixture `_reset_db_path`，在每个测试运行前：
1. 重置 `DEVOPS_DB_PATH` 回 `data/test_sessions.db`
2. 重绑 `session_manager.db` 连接到正确路径
3. 确保 `agent_tasks` 等核心表存在（init_database 幂等）

**改动文件**：tests/conftest.py

### 15.6 测试用例

- T10-01：全量 collection `pytest tests/ -k pool_engine_source` 下 T8-01/T8-02 不再失败

### 15.7 P0：路径 A 降级到路径 B 后 bot_gitcode_token 仍写全局 bot token

**发现场景**：外部 AI 审查发现。host_repo GitCode 路径 A（有全局 bot token）中 bot 协作者失败降级到路径 B 时，`add_managed_repo` 仍写入 `bot_gitcode_token=global_bot_token`。

**根因**：repos.py L1039/L1052 两处 `add_managed_repo` 调用无条件传 `bot_gitcode_token=global_bot_token`。路径 A 降级到路径 B 时（bot 协作者邀请/权限检查失败），`global_bot_token` 非空但 bot 未加入仓库协作组 → `bot_gitcode_token` 被写入 DB → `get_repo_token_with_refresh` 第 2 级命中返回 bot token → API 403（bot 无协作权限），用户 token 永远走不到。

GitHub 分支（L863/882）已正确修复：`bot_github_token=("" if _gh_use_path_b else gh_bot_token)`。GitCode 分支漏了同样的修复。

**修复**：L1039/L1052 的 `bot_gitcode_token=global_bot_token` 改为 `bot_gitcode_token=(global_bot_token if not _use_path_b else "")`，与 GitHub 分支对齐。

**改动文件**：src/api/routes/repos.py（2 处）

### 15.8 测试用例

- T11-01：路径 A 降级到路径 B 后 `bot_gitcode_token` 为空字符串（非全局 bot token）
- T11-02：路径 A 正常（未降级）时 `bot_gitcode_token` 为全局 bot token
- T11-03：路径 B 从一开始（无全局 bot token）时 `bot_gitcode_token` 为空字符串

### 15.9 P0：repair_host 降级后仍无条件写 bot token（第 3/4 处同 bug）

**问题**：repair_host L1236 `_db_kwargs[_cfg["db_bot_token_field"]] = bot_token` 无条件写入全局 bot token。repair_host 降级到路径 B 后（bot 邀请/权限失败），bot_token 非空但 bot 无协作权限 → 写入 DB → 403。GitHub/GitCode 双 provider 都中招。

**修复**：`_db_kwargs[_cfg["db_bot_token_field"]] = (bot_token if not _use_path_b else "")`，与 host_repo 修复对齐。

### 15.10 P1：auto_upgrade_path_b_repos 不校验 add_collaborator 返回值

**问题**：db.py auto_upgrade_path_b_repos 调 `add_collaborator` 不检查返回的 `(ok, err)`，`accept_invite_for_repo` 也不查，随后无条件 `_update_repo_bot_token` 写入 bot token。bot 实际未进协作组但 token 已写入 → 403。

**修复**：`add_collaborator` 返回 `(ok, err)`，`ok=False` 时跳过该仓库（不写 bot token，继续走路径 B）。

### 15.11 P2：GRAPE_PORT / GRAPE_MAIN_PORT 命名分裂

**问题**：config.py 和 db.py 用 `GRAPE_PORT`（默认 3000），prompt_builder.py 用 `GRAPE_MAIN_PORT`（默认 3000）。运维设 `GRAPE_PORT=8080` 时 prompt_builder 仍用 3000。

**修复**：prompt_builder.py `_local_port()` 改为优先读 `GRAPE_PORT`，fallback `GRAPE_MAIN_PORT`（向后兼容），默认 3000。

### 15.12 测试用例

- T12-01：repair_host 降级到路径 B 后 bot_gitcode_token / bot_github_token 为空
- T12-02：auto_upgrade add_collaborator 失败时不写 bot token
- T12-03：_local_port 优先读 GRAPE_PORT，fallback GRAPE_MAIN_PORT

### 15.13 P0：create_new_repo GitHub 写错 bot token 列 + 路径 B 无条件写 bot token

**问题**：create_new_repo L672/683 对 GitHub 仓库也写 `bot_gitcode_token` 列（GitCode 列名），应为 `bot_github_token`。同时无 `_use_path_b` 守卫——路径 B 时仍写全局 bot token。

**修复**：按 provider 取正确的 bot token 列名（`_bot_token_col`），并加 `_use_path_b` 守卫写空。

### 15.14 P0：get_repo_token_with_refresh / get_repo_bot_token / get_github_token 第 2 级无 host_user_id 守卫

**问题**：路径 B（仓库级 bot token）层只要 `bot_gitcode_token` 列非空就命中返回，无 `host_user_id` 守卫。降级场景脏写入的 bot token 会被直接返回 → 403。

**修复**：三个函数的仓库级 bot token 层都加 `if get_host_user_id(repo_key) == 0:` 守卫，与路径 C（全局 bot token）层守卫对齐。结构性消除所有脏写入风险。

### 15.15 测试用例

- T13-01：create_new_repo GitHub 写 bot_github_token 列（非 bot_gitcode_token）
- T13-02：create_new_repo 路径 B 时 bot token 列为空
- T13-03：get_repo_token_with_refresh 路径 B 仓库跳过仓库级 bot token 层（即使列非空）
- T13-04：get_repo_bot_token 路径 B 仓库跳过仓库级 bot token 层
- T13-05：get_github_token 路径 B 仓库跳过仓库级 bot token 层

### 15.16 P1：host_repo / repair_host 降级后 bot_username 未清空

**问题**：host_repo L1041/L1054 和 repair_host L1235 的 `bot_username` 无条件写入全局 bot 用户名。降级到路径 B 时 bot 未加入协作组，但 `managed_repos.bot_username` 写了全局 bot 名 → `build_task_env` 查 `bot_username` 命中全局 bot → `BOT_USERNAME=全局bot` → `create_skill_pr.sh` 审查人=全局 bot → bot 无协作权限 → PR 审查失败。

**修复**：`bot_username=(global_bot_username if not _use_path_b else "")`，与 `bot_gitcode_token` 修复对齐。降级后 `bot_username` 为空 → `build_task_env` 走 host_user identities fallback。

**改动文件**：src/api/routes/repos.py（5 处：host_repo GitCode L1041/L1054 + host_repo GitHub L867/886 + repair_host L1235）

### 15.17 P2：_update_repo_bot_token 升级后 host_user_id 未清零

**问题**：`_update_repo_bot_token` 只写 `bot_gitcode_token`，没清 `host_user_id=0`。升级后 `bot_gitcode_token` 非空但 `host_user_id` 仍 > 0 → 语义混合状态（声明路径 A 但保留路径 B 标记）。功能不受影响（`bot_gitcode_token` 非空时路径 B 优先命中），但排查时困惑。

**修复**：`_update_repo_bot_token` 的 UPDATE SQL 同时 `SET host_user_id=0`。

**改动文件**：src/user/db.py（2 处：GitCode + GitHub）

### 15.18 测试用例

- T14-01：host_repo 降级后 bot_username 为空
- T14-02：repair_host 降级后 bot_username 为空
- T14-03：_update_repo_bot_token 升级后 host_user_id=0

### 15.19 P0：token 验证失败用 401 导致前端无限 refresh 循环

**问题**：settings.py L222/228/336/342 四处 GitHub/GitCode token 验证失败时 `raise HTTPException(status_code=401)`。前端 api.js `handleResp` 只看 `r.status === 401` 就当 JWT 过期 → 触发 `doRefresh` → refresh 200（JWT 没过期）→ 重发原请求 → 业务 401（token 验证失败）→ 无限循环。

**根因**：HTTP 401 语义是"身份未认证"（JWT 过期），GitHub/GitCode token 验证失败是"请求参数无效"，应用 400。

**修复**：4 处 `status_code=401` 改为 `status_code=400`，detail 加 `error_code: "TOKEN_INVALID"`，message 改英文 `"token invalid or expired"`。前端 `handleResp` 不再对 400 触发 refresh 循环。

**改动文件**：src/api/routes/settings.py（4 处）

### 15.20 测试用例

- T15-01：GitHub token 验证失败返回 400（非 401）+ error_code=TOKEN_INVALID
- T15-02：GitCode token 验证失败返回 400（非 401）+ error_code=TOKEN_INVALID

### 15.21 P0：claim_by_tag 漏注入 token

**问题**：`claim()` 在 L1284-1286 有 token 注入逻辑（路径 B task 行 `repo_gitcode_token` 为空时服务端现取注入 task dict）。`claim_by_tag`（L1316+）完全没有这段逻辑。广播模式（pool task）的 worker 通过 `claim_by_tag` 认领 → task dict 里 `repo_gitcode_token` 为空 → 远程 worker git clone/push 失败。

**修复**：在 `claim_by_tag` 返回 task dict 前补充与 `claim()` 相同的 token 注入逻辑。

**改动文件**：src/runtime/service.py

### 15.22 P1：update_issue 无 grape-auto-comment marker

**问题**：`update_issue`（PATCH /issues/{number}）修改 issue body/labels/state 时不加 `grape-auto-comment` 标记。`create_issue_comment`（POST /comments）已加标记（L355-359）。如果 agent 修改 issue body → webhook 事件 → actor=用户 → 标记检测层（2.0a）找不到标记 → 不拦截 → 自触发循环。

**修复**：`update_issue` 的 body 字段追加 `<!-- grape-auto-comment -->` 标记（与 `create_issue_comment` 逻辑一致）。

**改动文件**：src/api/git_repo_api.py

### 15.23 测试用例

- T16-01：claim_by_tag 返回的 task dict 包含非空 repo_gitcode_token（路径 B 场景）
- T16-02：update_issue 的 body 字段包含 grape-auto-comment marker

### 15.24 P0：R10 过期 token 发评论必然 401

**问题**：R10 issue 回帖用 `get_repo_token_with_refresh` 取已过期的用户 token 发评论 → 必然 401 → 评论发不出去 → 用户在 issue 上看不到 token 过期提示。

**修复**：优先用全局 bot token（PAT，不过期）发评论，fallback 到用户 token（best-effort）。零管理员场景无全局 bot token 时仍 best-effort。

**改动文件**：src/runtime/service.py

### 15.25 P1：webhook 删旧失败时用户 token 写入 webhook_token 列导致验证失配

**问题**：删旧 webhook 失败 + 路径 B 场景 → `_webhook_secret=*** 后 `gitcode_webhook_token=api_token`（用户 token）写入 DB。旧 webhook 实际密码是创建时的值（旧 bot token 或旧随机密钥）。token 刷新后用户 token 变化 → `get_webhook_token_by_repo` 返回 stale token → webhook 验证 403 → 事件静默丢失。

**修复**：删旧 webhook 失败时同时清空 `webhook_token`（写空字符串）→ `get_webhook_token_by_repo` 返回空 → webhook 验证 loud fail（返回 403 给 GitCode）→ 管理员可见 → repair_host 修复。比静默使用 stale token 更安全。

**改动文件**：src/api/routes/repos.py（4 处）

### 15.26 P2：detect_webhook_from_host 无 TRUSTED_HOSTS 白名单校验

**问题**：中间件自动从 Host 头推导 webhook_url 并写入 DB。排除 localhost 但无白名单校验 → 恶意已认证用户伪造 Host 头注入任意 webhook_url。

**修复**：`detect_webhook_from_host` 读取 `TRUSTED_HOSTS` 环境变量，配置了则校验 host 是否在白名单中。未配置时放行（开发环境兼容）。

**改动文件**：src/user/db.py

### 15.27 P2：guard TTL 30min 对长任务可能导致重复派发

**问题**：`purge_stale_event_guards` TTL=30min。长任务（>30min）的 guard 被清理 → 下一个同 issue 事件可创建新 guard → 可能重复派发。

**修复**：purge SQL 加 `NOT EXISTS (SELECT 1 FROM agent_tasks t WHERE t.status IN ('dispatched','running') AND t.event_seq = event_active_guard.seq)`——有运行中 task 的 guard 不被清理。

**改动文件**：src/api/event_store.py

### 15.28 测试用例

- T17-01：claim_by_tag 返回的 task 包含非空 repo_gitcode_token
- T17-02：update_issue body 含 grape-auto-comment marker
- T17-03：R10 评论优先用全局 bot token
- T17-04：detect_webhook_from_host 配置 TRUSTED_HOSTS 时拒绝非白名单 host
- T17-05：purge_stale_event_guards 跳过有运行中 task 的 guard

### 15.29 P1：create_issue / create_pull / update_pull 无 grape-auto-comment marker

**问题**：深度扫描发现 `create_issue`（POST /issues）、`create_pull`（POST /pulls）、`update_pull`（PATCH /pulls/{number}）三个端点写 body 时不加 marker。路径 B 场景 actor=用户 → webhook 事件 → 2.0a 标记检测找不到 → 不拦截 → 自触发循环。

`create_pull` 影响最大——coding agent 通过 grape CLI 创建 PR 是常规操作。

**修复**：三个端点的 body 字段追加 `<!-- grape-auto-comment -->` 标记（与 `create_issue_comment` / `update_issue` 逻辑一致）。

**改动文件**：src/api/git_repo_api.py（3 处）

### 15.30 测试用例

- T18-01：create_issue body 含 grape-auto-comment marker
- T18-02：create_pull body 含 grape-auto-comment marker
- T18-03：update_pull body 含 grape-auto-comment marker

### 15.31 P0：_repo_user_id 不查 github_login 导致 GitHub issue user_id 错误

**问题**：`_repo_user_id`（engine_select.py:163）第 1 步只查 `unified_user_mapping WHERE gitcode_user=?`，不查 `github_login` 字段。GitHub webhook actor 是 GitHub 用户名，查 `gitcode_user` 查不到 → 第 2 步 fallback `grape_accounts WHERE username=?` → 命中旧账号（username 与 GitHub login 同名但不是当前账号）。

**根因**：`_repo_user_id` 是既有代码（Issue #76/#211），只支持 GitCode actor。#372 新增了 `unified_user_mapping.github_login` 写入（settings.py 绑定时同步），但没改 `_repo_user_id` 的查询逻辑来支持 `github_login` 匹配。

**修复**：第 1 步 SQL 加 `OR github_login=?`：

```sql
SELECT user_id FROM unified_user_mapping
WHERE (gitcode_user=? OR github_login=?) AND user_id>0 LIMIT 1
```

**改动文件**：src/runtime/engine_select.py

### 15.32 测试用例

- T19-01：GitHub actor 查 github_login 命中正确 user_id（非 grape_accounts fallback）
- T19-02：GitCode actor 仍查 gitcode_user（不受影响）

### 15.32 P0：unified_user_mapping 全量 github_login 查询修复（8 处同类问题）

**问题**：unified_user_mapping 表有 gitcode_user 和 github_login 两字段，但所有查询只查 gitcode_user。GitHub webhook actor 是 GitHub 用户名，只能匹配 github_login——查不到后 fallback 到 grape_accounts.username，可能命中旧账号。INSERT OR REPLACE 写入时覆盖 github_login 字段。

**8 处未修复**（第 1 处 engine_select 已修）：

查询只查 gitcode_user（6 处）：
- issues_center.py L74/79 `_get_user_id_by_gitcode`
- session_store.py L126 `set_issue_stage`
- issues_center.py L95 `_get_feishu_open_id`
- pipeline.py L53 `_resolve_feishu_name`
- session_manager.py L520 `get_feishu_open_id_by_gitcode`

INSERT OR REPLACE 覆盖 github_login（2 处）：
- prompt_builder.py L327 agent 指令模板
- session_manager.py L492 `update_unified_user_info`

**修复方向**：查询统一加 `OR github_login=?`；写入统一改 `INSERT OR IGNORE + UPDATE`。

**改动文件**：src/api/issues_center.py、src/api/session_store.py、src/prompt_builder.py、src/api/routes/pipeline.py、src/session_manager.py

### 15.33 测试用例

- T20-01：issues_center _get_user_id_by_gitcode 查 github_login 命中
- T20-02：session_store set_issue_stage 查 github_login 命中
- T20-03：issues_center _get_feishu_open_id 查 github_login 命中
- T20-04：pipeline _resolve_feishu_name 查 github_login 命中
- T20-05：session_manager update_unified_user_info 不覆盖 github_login

---

## R11：第八轮深度审计安全修复

### 背景

对 feat/issue-372-user-token-driven 分支做三路并行子 agent 深度审计后，逐条代码级验证确认以下问题。

### R11.1：detect_webhook_from_host 未设 TRUSTED_HOSTS 时接受任意 Host 头（P0）

**问题**：`detect_webhook_from_host` 在 `TRUSTED_HOSTS` 环境变量未设置时，接受任意非 localhost 的 `X-Forwarded-Host` / `Host` header 值，写入 `meta.webhook_url`。当前 nginx 配置未设 `X-Forwarded-Host`，客户端可直接发送此 header。攻击者（任意已认证用户）可伪造 `X-Forwarded-Host: evil.com`，将 webhook_url 写为 `http://evil.com/webhook/gitcode`，导致后续所有 webhook 事件被劫持。

**触发条件**：webhook_url 为空（首次部署）+ TRUSTED_HOSTS 未设 + 攻击者是已认证用户。

**修复方向**：
- `detect_webhook_from_host` 未设 TRUSTED_HOSTS 时仅接受 Host header（不读 X-Forwarded-Host），或要求显式配置 `WEBHOOK_AUTO_DETECT=1` 才启用自动发现
- `_get_trusted_host` 未设 TRUSTED_HOSTS 时不读 X-Forwarded-Host（减少攻击面）

**测试用例**：
- T21-01：TRUSTED_HOSTS 未设时 X-Forwarded-Host 被忽略
- T21-02：TRUSTED_HOSTS 设定时 X-Forwarded-Host 在白名单内被接受
- T21-03：TRUSTED_HOSTS 设定时 X-Forwarded-Host 不在白名单被拒绝
- T21-04：detect_webhook_from_host 不接受 localhost / 127.0.0.1

### R11.2：B→A 升级后 add_managed_repo UPDATE 覆盖 webhook_secret 为空串（P1）

**问题**：`_update_repo_bot_token` 升级路径 B 仓库时写入 `bot_gitcode_token` + 清 `host_user_id=0`，但不清 `webhook_secret`。GitCode 上的 webhook 密码仍是升级前的随机 `webhook_secret`。后续如果该仓库被 repair/re-host 且复用已有 webhook（不重建），`add_managed_repo` 路径 A 传入 `webhook_secret=""` → UPDATE 覆盖为空串。此后 `get_webhook_token_by_repo` 返回空 → fallback 到 `gitcode_webhook_token`（也被覆盖为空或 stale）→ webhook 验证 403。

**触发条件**：B→A 升级后仓库被 repair/re-host + 复用已有 webhook 不重建。

**修复方向**：`_update_repo_bot_token` 升级时同步清空 `webhook_secret`（升级后路径 A 不再需要随机密钥）；或 `add_managed_repo` UPDATE 路径保留已有 `webhook_secret`（类似 `host_user_id` 保留逻辑）。

**测试用例**：
- T22-01：B→A 升级后 webhook_secret 被清空
- T22-02：升级后 add_managed_repo 路径 A 不会残留旧 webhook_secret
- T22-03：升级后 repair_host 复用 webhook 不 403

### R11.3：register.html/forgot.html 前端版本号未同步（P1）

**问题**：`register.html` 的 `api.js?v=27` 和 `app.css?v=75` 落后于 `index.html` 的 `v=29` / `v=84`。`api.js` 本次有改动（加 `error_code` 字段），register.html 加载旧缓存。

**修复方向**：bump register.html / forgot.html 的 api.js 到 v=29、app.css 到 v=84。

**测试用例**：用 grep 断言版本号一致。

### R11.4：_get_task_token 重复定义（P1）

**问题**：`src/runtime/agent.py:70` 和 `src/runtime/service.py:14` 各自定义完全相同的 `_get_task_token` 函数，修改一方未同步另一方导致行为不一致。

**修复方向**：抽取到 `src/user/db.py`（已有 `_get_task_repo_token` 在 event_pipeline 中），两处 import。

**测试用例**：断言两处引用同一函数。

### R11.5：config_store.py save_config TOCTOU 竞态（P1）

**问题**：`get_meta_value`（检测 bot token 从无到有）和 `set_meta_value`（写入）之间无 `db_lock` 包裹。多管理员并发保存可能重复触发 `auto_upgrade_path_b_repos`。

**修复方向**：用 `with db_lock` 包裹 bot_payload 检测+写入块。

**测试用例**：T23-01 并发 save_config 不重复触发 upgrade。

### R11.6：get_repo_token_with_refresh 等函数重复调用 get_host_user_id 3 次（P1）

**问题**：`get_repo_token_with_refresh` 在路径 B 守卫、路径 C 守卫、路径 D 获取三处分别调 `get_host_user_id`，每次独立 DB 查询。`get_repo_bot_token` 和 `get_github_token` 同样。

**修复方向**：函数开头查一次 `host_user_id = get_host_user_id(repo_key)`，后续复用。

**测试用例**：断言单次调用内 get_host_user_id 只查 1 次。

### R11.7：_query_path_b_repos 缺 repo_key 前缀过滤（P2）

**问题**：`_query_path_b_repos(provider="gitcode")` 查询不按 `repo_key LIKE 'gitcode:%'` 过滤。GitHub 仓库的 `bot_gitcode_token` 天然为空会被误查出。实际影响被 host_token 获取逻辑天然阻止（GitHub 仓库用户无 GitCode token → host_token 空 → skipped），但代码正确性应加过滤。

**修复方向**：SQL 加 `AND repo_key LIKE 'gitcode:%'`（或 `github:%`）。

**测试用例**：T24-01 gitcode 升级不查出 github 仓库；T24-02 github 升级不查出 gitcode 仓库。

### R11.8：GitHub PAT 缺 _has_refresh_token 守卫（P2）

**问题**：`get_user_github_token_with_refresh` 没有 R2 `_has_refresh_token` 守卫。GitHub PAT（无 refresh_token）每次 `fetched_at > 1800s` 会触发 `refresh_oauth_token` → 因无 refresh_token 返回 None → 函数返回原 token。不损坏数据但白调一次刷新 API。

**修复方向**：添加与 GitCode 版一致的 `_has_refresh_token` 守卫。

**测试用例**：T25-01 GitHub PAT 不触发 refresh_oauth_token。


---

## R12：第三轮全场景深度审计修复（GitCode/GitHub 双平台）

### 背景

三路子 agent 深度审计 + 逐条代码级验证后，发现 11 个跨平台场景问题。最严重的是 GitHub 评论体提取缺失（自循环+指令全失效）和 B→A 升级不确认邀请就切路径。

### R12.1：GitHub 评论体提取缺失（P0）

**问题**：pipeline 中 marker 拦截、/done /cancel /resume 指令、澄清恢复都读 `_oa.get("note") or _payload.get("body")`。GitCode 评论在 `object_attributes.note`，但 GitHub 评论在 `payload.comment.body`——三个字段都不匹配。GitHub issue_comment 事件的自循环检测和指令检测全部失效。

**修复方向**：评论体提取改为 `_oa.get("note") or _payload.get("comment", {}).get("body") or _payload.get("body")`，覆盖 GitCode + GitHub 两种格式。

**测试用例**：
- T28-01：GitHub issue_comment 评论体含 marker → 被拦截
- T28-02：GitHub issue_comment /done 指令 → 被识别
- T28-03：GitCode issue_comment 评论体含 marker → 仍被拦截（回归保护）

### R12.2：auto_upgrade GitHub 不调 accept_github_invite（P0）

**问题**：`auto_upgrade_path_b_repos` GitHub 分支注释 `# GitHub 不需显式 accept` 是错误的。`add_github_collaborator` 只发送邀请，被邀请方需 accept 才生效。邀请未接受就写 bot token + 清 host_user_id → 卡死在坏状态。

**修复方向**：GitHub 分支加 `accept_github_invite(bot_token, owner, repo_name)` 调用。GitCode 侧 `accept_invite_for_repo` 已调但返回值不校验——加返回值检查，失败则 skip 不写 token。

**测试用例**：
- T29-01：auto_upgrade GitHub 调 accept_github_invite
- T29-02：auto_upgrade GitCode accept_invite 返回 False 时不写 token

### R12.3：host_repo GitHub invite_pending/add_failed 不降级路径 B（P1）

**问题**：`_try_github_bot_collaborator` 返回 `invite_pending` 或 `add_failed` 时，代码仍按路径 A 写 bot token。

**修复方向**：`invite_pending` 和 `add_failed` 时降级到路径 B（与 `error` 状态同等处理）。

**测试用例**：
- T30-01：bot_status=invite_pending → 降级路径 B
- T30-02：bot_status=add_failed → 降级路径 B

### R12.4：R10 过期回帖路径 B 仓库 bot 无权限（P1）

**问题**：R10 回帖优先用全局 bot token，路径 B 仓库 bot 无协作权限 → 评论发不出去 → 用户收不到过期通知。fallback 到用户 token 也过期（401 触发 R10 的原因）。

**修复方向**：路径 B 仓库（`_is_user_token_repo`）R10 回帖直接用 host user token（即使过期也尝试，best-effort），并在 task errors 中写 error_code 供前端展示。

**测试用例**：
- T31-01：路径 B 仓库 R10 回帖用用户 token

### R12.5：webhook 无签名无 token 头时放行（P1）

**问题**：GitHub webhook 验证逻辑：if signature → 验证；else if token_header → 验证；**else 放过**。攻击者不带签名和 token 头即可伪造事件。

**修复方向**：else 分支改为 return 403（拒绝无签名请求）。GitHub webhook 配了 secret 时必带 X-Hub-Signature-256。

**测试用例**：
- T32-01：无签名无 token 头 → 403

### R12.6：repair_host GitHub 路径 A 不调 accept_github_invite（P1）

**问题**：`repair_host` GitHub 分支注释 `# GitHub: add_github_collaborator 发邀请，bot 不需显式 accept（GitHub 自动处理）`——同样错误。

**修复方向**：加 `accept_github_invite` 调用。

**测试用例**：
- T33-01：repair_host GitHub 调 accept_github_invite

### R12.7：B→A 升级后 repair/host_repo 复用 webhook 不重建导致 403（P1）

**问题**：升级后 webhook_secret 被清空（R11.2 修复），但 GitCode 上 webhook 密码仍是旧 secret。后续 repair_host/host_repo 路径 A 复用已有 webhook 不重建时，DB webhook_secret=空 → fallback gitcode_webhook_token=bot_token → 与 GitCode 上的旧 secret 不匹配 → 403。

**修复方向**：repair_host 路径 A 复用已有 webhook 时，如果 DB 中 `host_user_id == 0` 且 `webhook_secret` 非空（刚升级的仓库），强制删旧重建。

**测试用例**：
- T34-01：升级后 repair_host 路径 A 复用 webhook 时强制重建

### R12.8：auto_upgrade GitCode 不校验 accept_invite 返回值（P1）

**问题**：GitCode 侧 `accept_invite_for_repo` 被调用但返回值不校验。如果邀请未真正接受就写 bot token → bot 无权限 → 403。

**修复方向**：校验 `accept_invite_for_repo` 返回值，False 时 skip 不写 token。

**测试用例**：
- T35-01：accept_invite_for_repo 返回 False 时不写 token

### R12.9：issue_updated description 编辑无入站标记检测（P2）

**问题**：出站写了 `<!-- grape-auto-comment -->` marker，但入站 issue_updated 只查评论类事件（issue_comment/note_hook_pr），不查 issue_updated。agent 编辑 issue description 的回声不会被标记拦截。

**修复方向**：issue_updated 事件也检查 description 是否含 marker，含则跳过。

**测试用例**：
- T36-01：issue_updated description 含 marker → 跳过

### R12.10：get_webhook_token_by_repo 列优先级 + LIMIT 无排序（P2）

**问题**：同时查 `gitcode:` 和 `github:` 前缀，但 `LIMIT 1` 无 `ORDER BY`，如果两行都存在可能返回任意一行。

**修复方向**：加 `ORDER BY webhook_secret DESC LIMIT 1`（优先返回有 webhook_secret 的行）。

**测试用例**：
- T37-01：两行存在时优先返回有 webhook_secret 的行

### R12.11：host_user_id 守卫下沉到 get_bot_token_by_repo 单点（结构性修复）

**问题**：host_user_id 守卫只加在 get_repo_token_with_refresh / get_repo_bot_token / get_github_token 三个主函数，长尾读 token 函数没同步：
- get_bot_token_by_repo 无守卫——被 scenes.py 3处、feishu_async.py、chat.py、agents.py、session_store.py、settings.py、repos.py、deps.py、repo_manager.py 共 12 处调用
- get_repo_bot_token_no_global devops_agent_session 层无守卫——A2A 鉴权信任凭据分裂
- feishu_async.py 直接调 get_bot_token_by_repo 而非统一入口

**修复方向**：守卫下沉到 get_bot_token_by_repo 单点（`if get_host_user_id(repo_key) > 0: return ""`），12 个调用方自动继承。get_repo_bot_token_no_global 的 devops_agent_session 层同步加守卫。feishu_async.py 改调 get_repo_token_with_refresh。

**测试用例**：
- T38-01：路径 B 仓库调 get_bot_token_by_repo 返回空
- T38-02：路径 A 仓库调 get_bot_token_by_repo 返回 bot token

### R12.12：call sites 统一切换到 get_repo_token_with_refresh

**问题**：虽然守卫已下沉到 get_bot_token_by_repo（R12.12），但 scenes.py 等 7 个调用方仍直接调 get_bot_token_by_repo，路径 B 仓库返回空后 fallback 到全局 bot token → 403。

**修复方向**：所有需要"获取仓库可用 token"的调用方改调 get_repo_token_with_refresh（统一入口，路径 B 返回用户 token 有权限）：
- src/api/routes/scenes.py（3 处）
- src/api/routes/chat.py（1 处）
- src/api/routes/agents.py（1 处）
- src/api/session_store.py（1 处）
- src/api/routes/settings.py（1 处）
- src/api/deps.py get_bot_token（1 处）
- src/user/repo_manager.py setup_webhook default password（1 处）

repos.py:1300 get_repo_bot_token 端点不改（检查 has_bot_token，路径 B 返回 false 是正确行为）。

**测试用例**：
- T39-01～08：各调用方不再直接调 get_bot_token_by_repo

### R12.13：feishu_async 移除全局 bot token fallback

**问题**：feishu_async.py:740-742 在 get_repo_token_with_refresh 返回空后 fallback get_global_gitcode_bot_token()，路径 B + host token 失效场景会拿无权限全局 token 调 API → 403。

**修复方向**：移除 fallback，无 token 时 best-effort 跳过（日志 warning + return 0）。

**测试用例**：T39-08 已覆盖（feishu_async 不调 get_bot_token_by_repo）。

### R12.14：最终全面扫描结论

全量扫描 `get_bot_token_by_repo` 直调和 `task.get("repo_gitcode_token")` 直读：

**get_bot_token_by_repo 直调（3 处，全部正确）**：
- db.py get_repo_bot_token_no_global：已有守卫（R12.12 下沉 + devops_agent_session 守卫）
- db.py get_repo_bot_token：已有 host_user_id 守卫（R11.6）
- repos.py:1300 has_bot_token 检查端点：路径 B 返回 false 是正确行为

**task.get("repo_gitcode_token") 直读（4 处，全部正确）**：
- db.py get_task_token 函数自身第一行（设计如此）
- service.py claim/claim_by_tag 注入逻辑（先检查空再注入 _get_task_token）
- dependency_check.py（claim 后执行，task dict 已注入 token）

**issues_center.py _refresh_user_gitcode_token**：仅在 `_using_user_token=True`（GitCode 非 GitHub）时触发，GitHub 仓库不可达此路径。无实际问题。

**结论**：10 轮审计全部收口，无残余问题。

---
### 15.34 P1：绑定后跳转项目页仓库列表 abort 竞态

**问题**：`_accountNavigateToRepos()` 绑定成功后同时调 `goToRepos()`（GET /repos/member）和 `_forceSyncRepos()`（POST /repos/sync），两个请求并发。`_forceSyncRepos` 先完成 → `nav('dash')` → `_navAbortController.abort()` → 取消 `goToRepos` 的请求 → catch 显示 "signal is aborted without reason"。

**修复**：`_accountNavigateToRepos` 去掉并发 `_forceSyncRepos`，只调 `goToRepos`。`goToRepos` 内部 GET /repos/member 缓存过期时会自动调 VCS API 刷新，效果与 /repos/sync 一致。`goToRepos` catch 加 `if (e.silent) return;` 守卫兜底。

**改动文件**：src/api/static/js/pages/account.js、src/api/static/js/app.js

**场景用例**：

- 场景1：用户绑定 GitCode PAT 成功 → 800ms 后跳项目页 → GET /repos/member 缓存过期自动调 VCS API → 渲染项目大厅页，无错误
- 场景2：用户绑定 GitHub PAT 成功 → 800ms 后跳项目页 → GET /repos/member 拉取双平台仓库 → 渲染项目大厅页，无错误
- 场景3：用户快速切 tab 导致 /repos/member 被 abort → catch 检查 e.silent=true → return，不显示错误
- 场景4：网络错误或服务端 500 → e.silent 为 undefined → 走原错误处理，显示"加载失败"

### 15.35 测试用例

- T40-01：_accountNavigateToRepos 函数体中不包含 _forceSyncRepos 调用
- T40-02：goToRepos catch 块包含 e.silent 检查
- T40-03：_forceSyncRepos 函数仍然存在（其他场景可调用）

## 十六、UI 交互优化（R13：项目页面 + 设置页 + PAT 绑定提示）

> 背景：issue-372 上线后在实际使用中发现 3 个前端交互问题，需要优化用户体验。

### R13.1：新建项目和模板创建后强制同步仓库列表

**用户故事**：作为用户，我希望新建项目或从模板创建项目后仓库列表立即显示新仓库，以便我可以马上进入新项目开始工作。

**当前问题**：
1. `submitCreateRepo`（新建项目）成功后调 `API.get('/repos/member')` 获取仓库列表，该接口走 `user_repos` 缓存表。新建仓库后 VCS 侧已创建，但本地缓存可能尚未更新，导致新仓库不立即出现。
2. `doCreateFromTemplate`（模板一键创建）成功后直接调 `selectRepo` 进入仓库，没有刷新仓库列表。如果用户返回项目大厅页，新仓库可能不在列表中（缓存未更新）。
3. `forkTemplate`（一键 Fork）同理，成功后直接 `selectRepo`，未刷新列表。

**业务规则**：
- 新建项目成功后局部刷新项目列表——调 `app._forceSyncRepos()` 强制同步后渲染当前页面，不整页刷新、不跳转
- 模板创建、一键 Fork 成功后调 `app._forceSyncRepos()` 再 `selectRepo`（同步是后台异步，不阻塞进入仓库）
- `_forceSyncRepos` 不可用时降级为 `API.get('/repos/member')` + `renderRepoSelection`（原行为）
- 同步失败不阻断用户体验——降级为普通加载
- 新建项目场景：关闭弹窗后原地刷新项目列表，用户看到新仓库卡片出现在列表中，不需要手动刷新页面

**验收标准**：
- 新建项目后原地局部刷新，新仓库立即出现在项目列表中（无整页刷新、无跳转）
- 从模板创建项目后，返回项目大厅页时新仓库在列表中
- 一键 Fork 后，返回项目大厅页时新仓库在列表中
- 网络异常时降级为普通加载，不报错

### R13.2：用户设置页不展示仓库信息

**用户故事**：作为用户，我希望进入用户设置页时不看到仓库名称（如 `xiaowu2119/my_demo_demo ↗`），以便用户设置页只展示用户级配置（密码/邮箱/第三方账号绑定/云凭据/外观），不混入仓库信息。

**当前问题**：用户从仓库工作台进入用户设置页（`nav('account')`）时，顶栏（`tb`）保留了仓库标题（`repoTitle`），仍然显示当前仓库名（如 `xiaowu2119/my_demo_demo ↗`）。用户设置页与仓库无关，展示仓库名不合理。

**业务规则**：
- 进入用户设置页（`nav('account')`）时清空顶栏仓库名（`repoTitle.textContent = ''`）
- 侧边栏已隐藏（原逻辑），不需要改动
- 仓库设置页（`nav('settings','gitcode')`）和侧边栏"仓库设置"入口保留不动——那是仓库配置功能，不是用户设置

**验收标准**：
- 用户设置页顶栏不显示仓库名称
- 仓库设置页功能不受影响

### R13.3：PAT 绑定时提示 token 不会自动续期

**用户故事**：作为用户，我希望在未配置 OAuth App 时通过 PAT 绑定 token 的弹窗中看到明确提示，以便我了解 PAT 不会自动续期，被撤销或过期后需要手动重新绑定。

**当前问题**：`_accountManualBindForm` 生成的手动令牌输入表单只有"如何获取令牌"折叠帮助，没有 PAT 续期提示。用户不知道 PAT 与 OAuth token 的区别，也不知道 PAT 不会自动续期。

**业务规则**：
- 提示只在未配 OAuth App 时显示（`_oauthConfigured === false`）
- 已配 OAuth App 时不显示（OAuth token 有 refresh_token 会自动续期）
- 提示内容：当前使用 PAT 绑定 / PAT 不会自动续期 / 被撤销或过期后需手动重新绑定 / 如需自动续期请联系管理员配置 OAuth App
- 提示文案通过 i18n key 管理，支持中英文
- 提示样式为信息提示卡片（浅色背景），不是错误警告
- GitCode 和 GitHub 两个平台共用同一 i18n key

**验收标准**：
- GitCode 未配 OAuth App 时，绑定弹窗显示 PAT 续期提示
- GitHub 未配 OAuth App 时，绑定弹窗显示 PAT 续期提示
- 已配 OAuth App 时，绑定弹窗不显示该提示
- 提示文案中英文正确

### R13.6：Dashboard 403 时自动切换到下一个有权限仓库

**用户故事**：作为用户，我希望当当前仓库无权限（如仓库被删除/转移/权限被收回）时，系统自动切换到下一个有权限的仓库，而不是显示"加载失败"，以便我不需要手动处理无权限的脏数据。

**当前问题**：用户从用户设置页返回工作台（`_accountGoBack` → `nav('dash')`）时，`sessionStorage` 里存的 `currentRepo` 可能是已无权限的仓库（如 `yangaiwu/demo`）。dashboard API 返回 403，前端只显示"加载失败: No permission to access this repository"，用户卡在错误页面。

**业务规则**：
- dashboard API 返回 403 时，清除 `sessionStorage` 中的 `currentRepo`（脏数据）
- 从侧边栏仓库下拉列表（`repoSelect`）找下一个有权限仓库
- 有 → 自动 `switchRepo` 切换到该仓库
- 没有 → 跳到项目大厅页（`goToRepos`）
- 只在非编辑模式（`!_dashboardEditing`）下触发，编辑中出错只 toast

**验收标准**：
- 当前仓库 403 时自动切换到下一个有权限仓库，不显示"加载失败"
- 没有其他仓库时跳到项目大厅页
- 编辑模式下 403 不触发切换（只 toast）

### R13.4：改动点清单

| 编号 | 文件 | 改动 |
|------|------|------|
| R13.1 | src/api/static/js/pages/repos.js `submitCreateRepo` | 成功后调 `API.post('/repos/sync')` + `renderRepoSelection` 局部刷新，不跳转 |
| R13.1 | src/api/static/js/pages/templates.js `doCreateFromTemplate` | 成功后调 `app._forceSyncRepos()` 再 `selectRepo` |
| R13.1 | src/api/static/js/pages/templates.js `forkTemplate` | 成功后调 `app._forceSyncRepos()` 再 `selectRepo` |
| R13.2 | src/api/static/js/app.js `nav('account')` | 进入用户设置页时清空 `repoTitle.textContent` |
| R13.3 | src/api/static/js/pages/account.js `_accountManualBindForm` | 增加 `oauthConfigured` 参数，未配 OAuth 时渲染 PAT 续期提示 |
| R13.3 | src/api/static/js/pages/account.js `_accountBindProvider` | 两处调用 `_accountManualBindForm` 传入 `oauthConfigured` |
| R13.3 | src/api/static/locales/zh/app.json | 新增 `app.account.pat_no_refresh_hint` i18n key |
| R13.3 | src/api/static/locales/en/app.json | 新增 `app.account.pat_no_refresh_hint` i18n key |
| R13.6 | src/api/static/js/app.js `loadDash` catch | 403 时清除 currentRepo + 切换到下一个仓库或跳项目大厅 |

### R13.5：场景用例

**R13.1 场景用例：**

- 场景1：用户在项目大厅页点"新建项目" → 填写仓库名 → 提交成功 → 关闭弹窗 → 原地局部刷新项目列表 → 新仓库卡片出现在列表中（无整页刷新、无跳转）
- 场景2：用户在模板页点"从模板创建" → 进度条跑完 → `app._forceSyncRepos()` 被调用 + `selectRepo` 进入仓库 → 用户点 Grape Logo 返回大厅 → 新仓库在列表中
- 场景3：用户在模板页点"一键 Fork" → Fork 成功 → `app._forceSyncRepos()` 被调用 + `selectRepo` 进入仓库 → 用户点 Grape Logo 返回大厅 → 新仓库在列表中
- 场景4：新建项目时网络异常 → `_forceSyncRepos` 内部降级为 `loadRepos(true)` → 不报错，列表正常渲染（可能缺新仓库，但不阻断）

**R13.2 场景用例：**

- 场景1：用户在仓库工作台（如 `xiaowu2119/my_demo_demo`）→ 点用户菜单 → 进入用户设置页 → 顶栏不显示 `xiaowu2119/my_demo_demo ↗`
- 场景2：用户在用户设置页 → 点返回工作台 → 顶栏恢复显示仓库名（回到 `nav('dash')` 逻辑）
- 场景3：用户在仓库工作台 → 点侧边栏"仓库设置" → 仓库设置页正常展示 Bot Token 配置（不受 R13.2 影响）

**R13.3 场景用例：**

- 场景1：系统未配 OAuth App → 用户点绑定 GitCode → 弹窗显示 PAT 提示卡片（ℹ️ 图标 + "当前使用 PAT 绑定"文案）+ 令牌输入框 + 确认按钮
- 场景2：系统未配 OAuth App → 用户点绑定 GitHub → 弹窗同场景1（共用 i18n key）
- 场景3：系统已配 OAuth App → 用户点绑定 GitCode → 弹窗显示 OAuth tab + 手动令牌 tab → 切到手动机令牌 tab → 不显示 PAT 提示卡片
- 场景4：系统已配 OAuth App → 用户点绑定 GitHub → 同场景3

**R13.6 场景用例：**

- 场景1：用户当前仓库 `yangaiwu/demo`（无权限脏数据）→ 进用户设置页 → 点返回工作台 → dashboard 403 → 清除 currentRepo → 侧边栏有其他仓库 `xiaowu2119/hello-flask` → 自动切换到该仓库 → dashboard 正常加载
- 场景2：用户当前仓库无权限 → dashboard 403 → 清除 currentRepo → 侧边栏无其他仓库 → 跳到项目大厅页
- 场景3：用户在 dashboard 编辑模式 → 轮询刷新 403 → 不切换仓库 → 只 toast 提示"刷新失败"
- 场景4：用户当前仓库有权限 → dashboard 200 → 正常加载，不触发切换逻辑

**测试用例编号：**
- T41-01：repos.js `submitCreateRepo` 成功回调中调 `API.post('/repos/sync')` + `renderRepoSelection` 局部刷新
- T41-02：templates.js `doCreateFromTemplate` 成功回调中包含 `app._forceSyncRepos` 调用
- T41-03：templates.js `forkTemplate` 成功回调中包含 `app._forceSyncRepos` 调用
- T41-04：app.js `nav('account')` 分支中包含 `repoTitle.textContent = ''`
- T41-05：account.js `_accountManualBindForm` 函数签名含 `oauthConfigured` 参数
- T41-06：account.js `_accountManualBindForm` 中 `!oauthConfigured` 时渲染 `pat_no_refresh_hint`
- T41-07：account.js `_accountBindProvider` 两处调用传入 `oauthConfigured`（true 和 false）
- T41-08：zh/app.json 包含 `pat_no_refresh_hint` key
- T41-09：en/app.json 包含 `pat_no_refresh_hint` key
- T41-10：已配 OAuth 时 `_accountManualBindForm` 不含 `pat_no_refresh_hint`（oauthConfigured=true 跳过）
- T41-11：app.js `loadDash` catch 中含 `e.status === 403` 判断
- T41-12：403 时清除 `sessionStorage.removeItem('currentRepo')` + 调 `switchRepo` 或 `goToRepos`
- T41-13：403 切换逻辑在 `!_dashboardEditing` 之后（编辑中不触发）


---

## 十七、R14 深度审计修复需求（round 14）

### R14.1 B→A 升级后 webhook 重建

**需求描述**：`auto_upgrade_path_b_repos` 升级路径 B 仓库时，必须在写入 bot token 前重建 webhook，确保升级后 webhook 验证用 bot token 而非旧的随机密钥。

**验收标准**：
- R14.1.1：`_update_repo_bot_token` 接受 `webhook_token` 和 `webhook_id` 参数，UPDATE 同步写入
- R14.1.2：`auto_upgrade_path_b_repos` 在邀 bot 成功后、写 token 前执行 webhook 重建（删旧+建新）
- R14.1.3：webhook_url 未配置时降级为只写 bot token（不阻断升级）

### R14.2 路径 B webhook_token 不写 stale 用户 token

**需求描述**：路径 B 成功创建 webhook 时，`gitcode_webhook_token` / `github_webhook_token` 字段写空串，避免 token 刷新后 DB 值 stale。

**验收标准**：
- R14.2.1：路径 B 成功新建 webhook 时 wh_col 对应值写空串
- R14.2.2：路径 B 复用已有 webhook 删旧失败时保持空串（已有逻辑不变）
- R14.2.3：路径 A 时 wh_col 对应值写 bot token（已有逻辑不变）

### R14.3 R10 路径 B issue 回帖尝试全局 bot token

**需求描述**：路径 B 仓库 token 过期时，issue 回帖也尝试用全局 bot token（即使可能 403）。

**验收标准**：
- R14.3.1：路径 B 仓库 fail_task 的 _comment_token 先尝试全局 bot token
- R14.3.2：全局 bot token 为空时 fallback 到用户 token（best-effort）
- R14.3.3：task errors 中 TOKEN_EXPIRED error_code 仍正常写入（不依赖 issue 回帖成功）

### R14.4 get_host_user_id 冗余查询消除

**需求描述**：token 获取函数中 `get_host_user_id` 每调用 3 次冗余 DB 查询改为 1 次。

**验收标准**：
- R14.4.1：`get_repo_token_with_refresh` 函数内 get_host_user_id 调用次数 ≤ 1
- R14.4.2：`get_repo_bot_token` 函数内 get_host_user_id 调用次数 ≤ 1
- R14.4.3：`get_github_token` 函数内 get_host_user_id 调用次数 ≤ 1

### R14.5 config_store TOCTOU 竞态确认

**需求描述**：确认 save_config 中 bot token 检测和写入在同一 db_lock 内。

**验收标准**：
- R14.5.1：`_upgrade_triggers` 的检测（get_meta_value）和写入（set_meta_value）在同一 `with db_lock:` 块内（R11.5 已实现，本轮确认无需改代码）

### R14.6 前端 error_code→i18n 映射启用

**需求描述**：`app.errMsg(e)` 在关键 catch 块中被调用，error_code 映射到本地化文案。

**验收标准**：
- R14.6.1：app.js loadDash catch 中使用 errMsg(e) 替代 e.message
- R14.6.2：error_code 未命中 i18n key 时 fallback 到 e.message

### R14.7 grape_accounts.status=4 注释修正

**验收标准**：
- R14.7.1：`get_grape_account_by_email` 注释中 status=4 描述为 "merged"，不是 "token过期"

### R14.8 dashboard 403 自动切换去重

**验收标准**：
- R14.8.1：403 自动切换时维护已尝试仓库列表，跳过已试过的
- R14.8.2：全部仓库都 403 时 goToRepos() + toast 提示

### R14.9 create_external_identity 清理 status=4 记录

**验收标准**：
- R14.9.1：INSERT 前清理同 (grape_user_id, provider) 的 status=4 记录
- R14.9.2：用户绑不同账号 PAT 时不会 UNIQUE 冲突

### R14.10 events.json zh/en key 同步确认

**需求描述**：确认 events.json zh/en key 完全一致。

**验收标准**：
- R14.10.1：zh/events.json 和 en/events.json 的 key 集合完全一致（前序子 agent 已修复，本轮确认）

### R14.11 get_user_token_with_refresh dead code 清理

**验收标准**：
- R14.11.1：deps.py get_user_token_with_refresh 函数无不可达代码

### R14.12 add_managed_repo UPDATE 保留已有 webhook_secret / bot_token

**需求描述**：`add_managed_repo` UPDATE 路径在调用方传空串时保留已有 `webhook_secret`、`bot_gitcode_token`、`bot_github_token`，防止 B→A 升级后二次调用覆盖。

**验收标准**：
- R14.12.1：UPDATE 时 SELECT 查出 webhook_secret / bot_gitcode_token / bot_github_token
- R14.12.2：调用方传 webhook_secret="" 时保留已有值
- R14.12.3：调用方传 bot_gitcode_token="" 时保留已有值
- R14.12.4：调用方传 bot_github_token="" 时保留已有值
- R14.12.5：调用方传非空值时正常覆盖（显式更新场景）

### R14.13 _is_user_token_repo / _is_github_user_token_repo 路径 B 守卫补齐

**需求描述**：`_is_user_token_repo` 和 `_is_github_user_token_repo` 的路径 B 查询加 `host_user_id == 0` 守卫，与主函数 `get_repo_token_with_refresh` / `get_github_token` 对齐。

**验收标准**：
- R14.13.1：`_is_user_token_repo` 路径 B 查询前有 `host_user_id == 0` 守卫
- R14.13.2：`_is_github_user_token_repo` 路径 1（bot_github_token）查询前有 `host_user_id == 0` 守卫
- R14.13.3：`_is_github_user_token_repo` 路径 2（owner external_identities）查询前有 `host_user_id == 0` 守卫
- R14.13.4：`_is_user_token_repo` 函数内 `get_host_user_id` 调用次数 ≤ 1（复用变量）
- R14.13.5：`_is_github_user_token_repo` 函数内 `get_host_user_id` 调用次数 ≤ 1（复用变量）

### R14.14 update_managed_repo_bot_token 清 host_user_id + provider 分列

**需求描述**：`update_managed_repo_bot_token` 写入 bot token 时同时清 host_user_id/webhook_secret，并按 repo_key 前缀分列写入。

**验收标准**：
- R14.14.1：github: 前缀的 repo_key 写入 bot_github_token 列
- R14.14.2：非 github: 前缀的 repo_key 写入 bot_gitcode_token 列
- R14.14.3：写入时同时清 host_user_id=0
- R14.14.4：写入时同时清 webhook_secret=''

### R14.15 add_managed_repo 路径 A 成功时显式清 host_user_id

**需求描述**：`add_managed_repo` UPDATE 路径在调用方传非空 bot_token + host_user_id=0 时显式清零 host_user_id。

**验收标准**：
- R14.15.1：host_user_id > 0 时用调用方值（路径 B）
- R14.15.2：host_user_id=0 且 bot_token 非空时清零（路径 A 显式切换）
- R14.15.3：host_user_id=0 且 bot_token 为空时保留已有值（不涉及 token 的场景）

### R14.16 统一 webhook 重建入口

**需求描述**：所有写 bot token 的路径（手动设置 + 自动升级）统一走 `_rebuild_webhook_with_bot_token` 重建 webhook。

**验收标准**：
- R14.16.1：`_rebuild_webhook_with_bot_token` 函数存在，被两个入口共用
- R14.16.2：`update_managed_repo_bot_token` 调用 `_rebuild_webhook_with_bot_token`
- R14.16.3：`auto_upgrade_path_b_repos` 调用 `_rebuild_webhook_with_bot_token`
- R14.16.4：删旧 webhook 失败时不建新（返回空串），保留旧 webhook
- R14.16.5：建新 webhook 失败时返回空串，DB 仍写入 token（webhook_id 为空），用户可 repair_host
- R14.16.6：webhook_url 未配置时不阻断 DB 写入

### R14.17 _tried403Repos 成功后重置

**需求描述**：dashboard 成功加载时清除 403 去重列表。

**验收标准**：
- R14.17.1：loadDash 成功回调中有 `_tried403Repos = null`
- R14.17.2：_tried403Repos = null 在代码中出现 >= 2 次（成功 + 全部试过）

### R14.18 fail_task errors 改为 append 模式

**需求描述**：R10 token 过期检测写入 errors 时保留原始错误详情。

**验收标准**：
- R14.18.1：R10 写 errors 前先读取已有 errors
- R14.18.2：append TOKEN_EXPIRED dict 到已有列表，不覆写
- R14.18.3：errors 列表限制长度（最近 5 条）
- R14.18.4：已有 errors 非 JSON list 时转为 list 再 append

### R14.19 update_managed_repo_bot_token 恢复 404 检测 + 注释修正 + 统计修正

**需求描述**：手动设置 bot token 时先检查仓库存在，不存在的返回 False 走 404。

**验收标准**：
- R14.19.1：update_managed_repo_bot_token 先 SELECT 检查仓库存在
- R14.19.2：仓库不存在时返回 False（调用方走 404）
- R14.19.3：仓库存在时才调 _rebuild_webhook_with_bot_token
- R14.19.4：_rebuild_webhook_with_bot_token 删旧失败注释准确描述行为（webhook 403）
- R14.19.5：auto_upgrade 日志区分 upgraded 和 webhook_rebuild_failed

---

## R22 修订章（基线校准后遗漏补充）

> 本章基于 weekly-08 基线校准后的深度审查，补充主线 R1-R14 未覆盖的遗漏点。
> 与支线（repo-token-config R15-R21）的关系：支线是未实现的设计提案，本章是主线已实现基础上的补充需求。

### R22.1 token 刷新并发竞态（B1，P1）

**问题**：get_user_token_with_refresh 刷新写 DB 期间，并发 task 读旧 token → 401。生产 0001-0007 多 worker 并发 claim，OAuth token 15 分钟寿命下是高频窗口。

**当前代码**：deps.py _gitcode_refresh_locks 是进程内 threading.Lock，只防单实例并发，不防跨 worker。claim 时（service.py:1278）注入 token → 多 worker 同时 claim 不同 task → 同时发现临近过期 → 并发 refresh。

**需求**：
- R22.1.1：分布式锁 `oauth_refresh:{user_id}:{provider}`（run_with_distributed_lock），多 worker 只有一个执行 refresh
- R22.1.2：锁内 refresh 前先读 token_expires_at，已刷新则跳过
- R22.1.3：refresh 失败（refresh_token rotation）→ 读最新 refresh_token 重试一次
- R22.1.4：重试仍失败 → status=4 → 用户重新绑定
- R22.1.5：双层锁保护（进程内 threading.Lock + 分布式锁）

### R22.2 路径 B devops_agent_session token 残留清理（B2，P1）

**问题**：get_repo_token_with_refresh 第 1 级 session token 永远最优先且不过期检查。仓库曾配过 agent session token（或路径 A→B 降级）后，devops_agent_session.repo_gitcode_token 残留 → 用户 token 永远不被使用。

**当前代码**：get_repo_token_with_refresh（db.py:2078-2092）第 1 级查 devops_agent_session.repo_gitcode_token，host_user_id>0 时跳过 B/C 层但第 1 级仍最优先。无清理逻辑。

**需求**：
- R22.2.1：托管时（路径 B）清理 devops_agent_session.repo_gitcode_token（设为空串）
- R22.2.2：路径 A→B 降级时清理 session token
- R22.2.3：解绑时清理 session token（主线 §6.6 清协作者时同步清 session）
- R22.2.4：get_repo_token_with_refresh 第 1 级 session token 查询时检查 host_user_id 守卫——host_user_id>0 时跳过第 1 级（与 B/C 层守卫一致）
- R22.2.5（R23.1 补充）：_is_user_token_repo 独立函数也加 host_user_id 守卫——路径 B 仓库（host_user_id > 0）跳过 devops_agent_session.repo_gitcode_token 查询。R22.2.4 只修复了 TokenResolver 策略链入口，遗漏了 _is_user_token_repo 这个独立路径判断函数的同一 session token 直查。路径 B + session 残留 → _is_user_token_repo 误返回 False → task token 来源误判

### R22.3 多用户托管换主后 host_user_id 权限漂移（B3，P2）

**问题**：B 托管时 webhook 用 B 的 token 建，host_user_id 仍指向 A。若 A 被移出仓库或 PAT scope 收窄，pipeline 全走 A 的 token → 403/401，fail_task 标 A 过期，B 收不到指引。

**当前代码**：§6.4 修复了"B 覆盖 A"（host_user_id 指向新托管人），但 webhook 仍用 B 的 token 建。如果 A 后来被移出仓库，A 的 token 仍被 pipeline 使用。

**需求**：
- R22.3.1：托管时检测是否已有其他用户托管（host_user_id != 0 且 != 当前用户）→ 提示"当前仓库已由用户 X 托管，是否切换？"
- R22.3.2：切换托管时更新 host_user_id → 新用户 → 重建 webhook（用新用户 token）
- R22.3.3：旧托管人 token 过期时 fail_task 检测 → 标记旧用户 status=4 → 新用户（当前 host）收到 dashboard 提示"旧托管人 token 已过期，请确认是否切换托管"

### R22.4 agent 执行中 token 过期（B4，P2）

**问题**：远程 worker claim 时实时取 token → 长任务执行超 15 分钟时执行中 git push/API 调用 401，结果回帖失败。主线十四轮审计从未触及执行中过期场景。

**需求**：
- R22.4.1：_post_result_to_issue 失败时（401）→ task errors append RESULT_POST_FAILED + dashboard 显示"结果已执行但无法回帖到 Issue"
- R22.4.2：_post_result_to_issue 失败不影响 task 状态——task 仍标记 completed
- R22.4.3：agent git push 失败时（401）→ fail_task 正常处理（R10/R16.1 逻辑）
- R22.4.4：agent 执行前预检 token 有效性（PAT 无 expires_at，预检仅对 OAuth token 有效）

### R22.5 fail_task 401 关键词补全（B5，P1）

**问题**：代码现有列表（service.py:2498）含 "authentication failed" / "invalid username or token"，但缺 "could not read Username" / "fatal: Authentication"——空 token git clone 的典型报错。

**需求**：
- R22.5.1：扩展 401 关键词列表，增加 "could not read Username" / "fatal: Authentication failed" / "fatal: could not read"
- R22.5.2：空 token 检测在 401 关键词检测之前执行——get_task_token 返回空串时直接写 NO_TOKEN_AVAILABLE

### R22.6 验收清单汇总（B6，P2）

**问题**：R11-R14 十几个 P0/P1 修复各自散落"测试用例"小节，无汇总验收清单。

**需求**：
- R22.6.1：汇总 R1-R14 + R22 的验收清单到独立文档（docs/weekly-08/token-config/00-index.md 已有测试清单框架）
- R22.6.2：验收清单按优先级分类：P0 必须有回归测试，P1 应有测试，P2 可选
- R22.6.3：分支已带 20 个 test_issue_372_* 文件，验收清单应映射这些文件到对应 R 编号

### R22.7 C 级轻量修正

- R22.7.1：分支内混入无关提交（2731a3498b fix(i18n): repos 页语言切换）——合并评审时注意区分，不影响 token 功能
- R22.7.2：主线 R8 场景二"PAT 不会过期（永久有效）"表述修正——PAT 可设过期时间（GitCode 30/90/永久，GitHub fine-grained 最长 1 年），只能 401 被动感知。与支线 1.2 一致
- R22.7.3：get_repo_bot_token_no_global 路径 B 降级返回用户 token（status=4 仍返回），与支线 R20.13 read_stored_token 意图一致但属巧合（无文档确认）——补充文档确认此行为是设计意图（A2A 鉴权不依赖 token 有效性，只依赖一致性）

### R22.10 上线策略结论（灰度方案作废，最小必要集）

**前提**：路径 B 只在"无全局 bot token"时触发。存量生产环境全局 bot token 已配置 → host_user_id 恒 0 → 四条 token 链全走原逻辑。路径 B 受众是新部署，无存量业务需保护。回滚机制已在设计中（配全局 bot token → auto_upgrade 切回路径 A）。

**结论：灰度/影子期/白名单方案作废。必要操作只有三件**：

- R22.10.1：修 F3——deps.py:285 和 :329 status 过滤统一为 status IN (1,4)，几行改动
- R22.10.2：确认 R10 守卫足够——_is_user_token_repo 守卫已覆盖所有 401 入口，路径 A 仓库 401 不触发 R10，不需要新开关
- R22.10.3：发布说明披露三处有意变更——status IN (1,4) / GitHub token 验证 401→400 / GitCode token 验证 401→400，文档工作零代码

**不需要**：灰度/影子期/白名单（路径 B 不影响存量）、回滚开关（配全局 bot token 即回滚）、feature flag（host_user_id 守卫体系已是天然开关）

---

## R23 深度分析发现的遗漏与修复（round 15）

> 基于 weekly-08 基线对 feat/issue-372-user-token-driven 分支做需求文档 + 设计文档 + 代码实现三方交叉审计后发现。每个遗漏含问题描述 + 影响分析 + 场景用例 + 修复方案。

### R23.1 _is_user_token_repo 缺 host_user_id 守卫（P1）

**问题**：`_is_user_token_repo`（src/user/db.py）第一步查 `devops_agent_session.repo_gitcode_token` 时无 `host_user_id` 守卫。TokenResolver 的 `DevopsAgentSessionToken._fetch`（token_resolver.py）已有 `host_user_id > 0` 守卫（R22.2.4），但 `_is_user_token_repo` 是独立函数，不走 TokenResolver，守卫未同步。

**根因**：R22.2 修复时只关注了 TokenResolver 策略类的守卫，遗漏了 `_is_user_token_repo` 这个独立路径判断函数也有同样的 session token 直查。

**影响**：路径 B 仓库（host_user_id > 0）如果 devops_agent_session.repo_gitcode_token 残留（路径 A→B 降级、曾配过 agent session token），`_is_user_token_repo` 第一步命中 session token → 返回 False → task 创建时把 token 写入 task 行 → 违反 R2.4 设计意图（"用户 token 不应写入 task 行，应由运行时实时获取"）→ 排队期间 token 过期 → claim 时用旧 token → 401。

**场景用例**：

#### 场景 23.1：路径 A→B 降级后 session token 残留导致 task token 来源误判

1. 仓库初始走路径 A（有全局 bot token），agent session 配置了 repo_gitcode_token
2. 全局 bot token 被清除 → 仓库降级到路径 B（host_user_id 写入用户 ID）
3. devops_agent_session.repo_gitcode_token 未清理（R22.2.1 要求清理，但可能时序窗口内残留）
4. pipeline 创建 task → _is_user_token_repo 第一步查到 session token → 返回 False
5. task 行写入 repo_gitcode_token=session token（不是用户 token）
6. session token 可能已过期 → task 排队期间过期 → claim 时用旧 token → 401

#### 场景 23.2：修复后路径 B 仓库跳过 session token 查询

1. 仓库走路径 B（host_user_id > 0）
2. _is_user_token_repo 第一步检查 host_user_id > 0 → 跳过 session token 查询
3. 继续检查 bot_gitcode_token（路径 B 守卫跳过）→ 继续检查全局 bot token（host_user_id > 0 跳过）
4. 前三级全空 + host_user_id > 0 → 返回 True
5. task 行 repo_gitcode_token 写空 → 运行时实时获取用户 token → 正确

**修复**：`_is_user_token_repo` 第一步查询前加 `host_user_id > 0` 守卫——路径 B 仓库直接跳过 session token 查询，与 `DevopsAgentSessionToken._fetch` 守卫对齐。

**验收标准**：
- R23.1.1：`_is_user_token_repo` 在 `host_user_id > 0` 时跳过 `devops_agent_session.repo_gitcode_token` 查询
- R23.1.2：路径 B 仓库 + session token 残留时 `_is_user_token_repo` 返回 True（非 False）
- R23.1.3：路径 A 仓库（host_user_id == 0）+ session token 非空时 `_is_user_token_repo` 返回 False（回归保护）

**测试用例**：
- T23-01：路径 B 仓库（host_user_id > 0）+ session token 残留 → `_is_user_token_repo` 返回 True
- T23-02：路径 A 仓库（host_user_id == 0）+ session token 非空 → `_is_user_token_repo` 返回 False（回归）
- T23-03：路径 B 仓库 + 无 session token → `_is_user_token_repo` 返回 True（回归）

### R23.2 event dialogue 不创建 agent_tasks 行——自循环检测失效（P1）

**问题**：event dialogue（事件对话，events.py `_execute_conv_task`）用内存字典 `_conv_tasks` 存储 task 状态，不写入 `agent_tasks` 表。设计文档 R20.15.1 明确要求"event dialogue 执行时在 agent_tasks 表创建 task 行（status=running）"，但代码未实现。

**根因**：event dialogue 的架构是内存执行模式（`_conv_tasks` 字典 + `run_in_executor`），不走标准 `enqueue_task` 队列，因此不创建 `agent_tasks` 行。设计文档 R20.15 写需求时未检查 events.py 的实际架构。

**影响**：路径 B 仓库 + event dialogue 执行期间，agent 用用户 token 发 Issue 评论 → webhook 推送 → `check_running_task` 条件 4 查 `agent_tasks` → 查不到（内存字典不在 DB）→ session 兜底查 `devops_agent_session.last_active_at` → event dialogue 不更新 `last_active_at` → 兜底也不命中 → 四条件不全满足 → 放行 → pipeline 派发新 task → 重复执行。

**修复方案**：两个方向择一：
- 方案 A（推荐）：event dialogue 启动时更新 `devops_agent_session.last_active_at` + `status='active'`，完成时更新 `status='idle'` → 让 session 兜底查询生效。改动最小，不改变 event dialogue 的内存执行架构。
- 方案 B：event dialogue 启动时在 `agent_tasks` 表创建 task 行，完成时更新 status=completed。改动较大，需适配内存执行模式的 task 生命周期管理。

**采纳方案 A**（已实现）：在 `_execute_conv_task` 启动时更新 session heartbeat，完成时置 idle。

**场景用例**：

#### 场景 23.3：修复前——event dialogue 执行期间自循环

1. 路径 B 仓库，用户 A 的 PAT 未过期
2. 用户在 event dialogue 中发消息 → agent 开始执行（内存字典 _conv_tasks，无 DB 记录）
3. agent 用用户 token 调 VCS API 发 Issue 评论
4. webhook 推送 issue_comment → actor = A
5. check_running_task 条件 4：查 agent_tasks → 无记录 → session 兜底查 last_active_at → 未更新 → 不命中
6. 四条件不全满足 → 放行 → pipeline 派发新 task → 重复执行

#### 场景 23.4：修复后——session heartbeat 让兜底生效

1. 路径 B 仓库，用户 A 的 PAT 未过期
2. 用户在 event dialogue 中发消息 → _execute_conv_task 启动
3. **新增**：更新 devops_agent_session.last_active_at=now, status='active'
4. agent 用用户 token 调 VCS API 发 Issue 评论
5. webhook 推送 issue_comment → actor = A
6. check_running_task 条件 4：查 agent_tasks → 无记录 → session 兜底查 last_active_at > now-60s AND status='active' → **命中**
7. 四条件全满足 → 跳过 → 不重复执行

#### 场景 23.5：修复后——event dialogue 完成后用户评论正常触发

1. event dialogue 完成 → _execute_conv_task finally 块
2. **新增**：更新 devops_agent_session.status='idle'
3. 用户 A 在 Issue 上发评论"请继续处理"
4. webhook 推送 issue_comment → actor = A
5. check_running_task 条件 4：查 agent_tasks → 无记录 → session 兜底查 status='active' → **不命中**（已置 idle）
6. 四条件不全满足 → 放行 → pipeline 派发新 task → 正确

**验收标准**：
- R23.2.1：`_execute_conv_task` 启动时更新 `devops_agent_session.last_active_at` + `status='active'`
- R23.2.2：`_execute_conv_task` 完成时更新 `devops_agent_session.status='idle'`
- R23.2.3：event dialogue 执行期间 `check_running_task` session 兜底查询命中（返回 True）
- R23.2.4：event dialogue 完成后 session 兜底查询不命中（返回 False）

**测试用例**：
- T23-04：`_execute_conv_task` 启动后 `devops_agent_session.status='active'` 且 `last_active_at` 为近期时间
- T23-05：`_execute_conv_task` 完成后 `devops_agent_session.status='idle'`
- T23-06：event dialogue 执行期间 `check_running_task(repo_key, issue_id)` 返回 True
- T23-07：event dialogue 完成后 `check_running_task(repo_key, issue_id)` 返回 False

### R23.3 R15 仓库级 token 配置 7 步流程完整性确认（P2）

**问题**：设计文档 tc-01 详细描述了 7 步流程（检查仓库→验证 token→获取 username→邀 bot→重建 webhook→写 DB→返回），代码中 `update_managed_repo_bot_token` 只做步骤 5-6（重建 webhook + 写 DB），步骤 1-4（验证 token/邀 bot）在调用方 `set_repo_bot_token` 路由层。需确认路由层完整实现了步骤 1-4。

**影响**：如果路由层缺少 token 验证或 bot 邀请步骤，无效 token 会被写入 DB 导致全断，或 bot 未邀请就建 webhook 导致 403。

**确认结果**：`set_repo_bot_token` 路由层已实现 token 验证（调 VCS API GET /user）和 bot 协作者邀请逻辑。`update_managed_repo_bot_token` 内部调 `_rebuild_webhook_with_bot_token` + `_update_repo_bot_token`，职责分离正确。

**场景用例**：

#### 场景 23.6：手动配置仓库级 bot token 完整流程

1. 有仓库权限的用户通过 `PUT /repos/bot-token` 输入新 bot token
2. 路由层步骤 1：检查仓库存在（不存在 → 404）
3. 路由层步骤 2：调 `provider.verify_token(token)` 验证 token 有效性（无效 → 400 TOKEN_INVALID）
4. 路由层步骤 3：获取 bot username（`provider.get_user_login(token)`）
5. 路由层步骤 4：邀 bot 为协作者（`provider.add_collaborator()`，用用户 token）
6. `update_managed_repo_bot_token` 步骤 5：`_rebuild_webhook_with_bot_token` 删旧 webhook + 建新 webhook
7. `update_managed_repo_bot_token` 步骤 6：`_update_repo_bot_token` 写 DB（bot_token + 清 host_user_id + 清 webhook_secret + 写 webhook_id + 清 bot_token_status）
8. 返回 200 + bot_username

**验收标准**：
- R23.3.1：`set_repo_bot_token` 路由层在调 `update_managed_repo_bot_token` 前完成 token 验证（R15.1）
- R23.3.2：`set_repo_bot_token` 路由层在调 `update_managed_repo_bot_token` 前完成 bot 协作者邀请（R15.2）
- R23.3.3：`update_managed_repo_bot_token` 只负责重建 webhook + 写 DB（步骤 5-6），不重复邀 bot

### R23.4 R22.3 多用户托管换主后权限漂移——场景用例补充（P2）

**问题**：R22.3 需求已定义但缺少详细场景用例。补充场景用例以明确修复方向。

**场景用例**：

#### 场景 23.7：用户 A 托管后用户 B 托管同一仓库（host_user_id 不覆盖）

1. 用户 A 路径 B 托管仓库 → host_user_id=A，webhook 用 A 的 token 建
2. 用户 B 尝试托管同一仓库 → §6.4 修复：host_user_id 不被覆盖，仍指向 A
3. pipeline 仍用 A 的 token → 正常（A 的 token 有效）

#### 场景 23.8：用户 A 被移出仓库后权限漂移

1. 用户 A 路径 B 托管仓库 → host_user_id=A
2. 仓库 owner 把 A 移出协作者 → A 的 token 失去仓库权限
3. pipeline 用 A 的 token → git push 403 → fail_task 标记 A 的 token 过期
4. 用户 B（当前 host 的替代者）收不到指引——dashboard 显示 A 的 token 过期，但不提示"请切换托管"
5. 需要 R22.3.1 的"切换托管"提示引导 B 接管

#### 场景 23.9：切换托管后 webhook 重建

1. 用户 B 通过 host_repo 重新托管 → R22.3.2：更新 host_user_id=B
2. 重建 webhook（用 B 的 token 建新 webhook，删旧 webhook）
3. pipeline 用 B 的 token → 正常

**验收标准**：
- R23.4.1：托管时检测已有其他用户托管 → 返回提示（R22.3.1）
- R23.4.2：切换托管后 host_user_id 更新为新用户 + webhook 重建（R22.3.2）
- R23.4.3：旧托管人 token 过期时新用户收到 dashboard 提示（R22.3.3）

### R23.5 R22.4 agent 执行中 token 过期——场景用例补充（P2）

**问题**：R22.4 需求已定义但缺少详细场景用例。补充场景用例以明确预检逻辑。

**场景用例**：

#### 场景 23.10：OAuth token 执行中过期——git push 失败

1. claim 时获取 OAuth token（剩余有效期 5 分钟）
2. agent 执行复杂任务（耗时 15 分钟）
3. 执行到第 5 分钟时 token 过期
4. agent git push → 401 失败
5. fail_task 检测 401 → 标记 status=4 → task failed
6. 代码修改未推送到远程 → 工作丢失

#### 场景 23.11：OAuth token 执行前预检——warning 但不阻断

1. claim 时获取 OAuth token（剩余有效期 5 分钟）
2. **新增**：预检 token 剩余有效期 < 预估执行时间（10 分钟）
3. 记录 warning："token expires in 300s, estimated execution 600s"
4. 不阻断执行（token 可能续期或任务提前完成）
5. agent 执行 → 提前完成（8 分钟）→ token 还有效 → 正常

#### 场景 23.12：PAT token 无需预检

1. claim 时获取 PAT（无 expires_at）
2. 预检跳过（PAT 无过期时间信息，无法预判）
3. agent 执行 → PAT 在执行期间被撤销 → git push 401
4. fail_task 正常处理 401

**验收标准**：
- R23.5.1：claim 时对 OAuth token（有 token_expires_at）检查剩余有效期，不足预估执行时间则 warning（不阻断）
- R23.5.2：PAT（无 token_expires_at）跳过预检
- R23.5.3：预检失败（查不到 token_expires_at）不阻断执行

### R23.6 OAuth refresh 分布式锁降级策略（P2）

**问题**：R20.27/R22.1 要求 OAuth refresh 加分布式锁，但未明确锁服务不可用时的降级策略。

**场景用例**：

#### 场景 23.13：分布式锁服务正常——跨实例互斥

1. master-001 和 master-002 同时发现用户 A 的 OAuth token 临近过期
2. master-001 获取分布式锁 `oauth_refresh:{A}:gitcode` → 执行 refresh
3. master-002 获取锁失败 → 等待
4. master-001 refresh 完成 → 释放锁
5. master-002 获取锁 → 读 token_expires_at 已更新 → 跳过 refresh

#### 场景 23.14：分布式锁服务不可用——降级为进程内锁

1. 分布式锁服务（如 Redis）不可用
2. `run_with_distributed_lock` 调用失败 → 捕获异常
3. 降级为进程内 `threading.Lock`（`_gitcode_refresh_locks`）
4. 单实例内仍然互斥，跨实例可能竞态
5. 记录 warning："distributed lock unavailable, falling back to process-local lock"
6. 竞态窗口最坏情况：两个实例各 refresh 一次，第二个 refresh 用旧 refresh_token 失败 → 标记 status=4 → 用户重新绑定

**验收标准**：
- R23.6.1：分布式锁服务不可用时降级为进程内锁，不抛异常阻断
- R23.6.2：降级时记录 warning 日志
- R23.6.3：降级后 refresh 仍执行（不因锁不可用而跳过 refresh）

### R23.7 验收清单映射（P2）

**需求**：将 R23 新增验收标准映射到测试文件。

| 遗漏编号 | 优先级 | 测试文件（建议） | 验收标准 |
|---------|--------|----------------|---------|
| R23.1 | P1 | tests/test_issue_372_r15_audit.py（新增或扩展） | T23-01～T23-03 |
| R23.2 | P1 | tests/test_issue_372_r15_audit.py（新增或扩展） | T23-04～T23-07 |
| R23.3 | P2 | 代码确认，无需新测试 | R23.3.1～R23.3.3 |
| R23.4 | P2 | 后续迭代 | R23.4.1～R23.4.3 |
| R23.5 | P2 | 后续迭代 | R23.5.1～R23.5.3 |
| R23.6 | P2 | 后续迭代 | R23.6.1～R23.6.3 |

---

## R24 全链路全场景深度审计遗漏修复（round 16）

> 基于全链路全场景深度分析（需求文档+设计文档+代码实现三方交叉审计），补充 R1-R23 未覆盖的设计遗漏。每个遗漏含问题描述+影响+场景用例+修复方案。

### R24.1 auto_upgrade 与 global_bot_token_swap 互斥未定义（P0）

**问题**：`auto_upgrade_path_b_repos`（无→有触发）和 `global_bot_token_swap`（有→有换账号触发）是两个独立后台线程，各自持有不同的分布式锁（`token_health_check` / `global_bot_swap`）。如果管理员先配全局 bot token（触发 auto_upgrade），然后在 auto_upgrade 执行期间又更换 bot token（触发 swap），两个线程可能同时操作同一仓库——都调 `_rebuild_webhook_with_bot_token` → 删旧 webhook → 旧 webhook 已被另一个线程删了 → 404 → DB 状态不一致。

**影响**：webhook 丢失或 DB 状态混乱，仓库事件断链。

**场景用例**：

#### 场景 24.1：auto_upgrade 执行期间更换全局 bot token

1. 管理员配置全局 bot token（从无到有）→ `save_config` 触发 `auto_upgrade_path_b_repos`
2. auto_upgrade 遍历路径 B 仓库，正在处理仓库 A（邀 bot + accept + 重建 webhook）
3. 管理员在 auto_upgrade 未完成时更换全局 bot token（有→有）→ `save_config` 触发 `global_bot_token_swap`
4. swap 遍历路径 A 仓库（已升级的仓库 A 此时 host_user_id=0, bot_gitcode_token=旧全局 token）
5. auto_upgrade 线程：删仓库 A 旧 webhook → 建新 webhook（用旧全局 token）
6. swap 线程：删仓库 A webhook → webhook 已被 auto_upgrade 删除 → 404 → 建新 webhook（用新全局 token）
7. 两个线程都调 `_update_repo_bot_token` → DB 写入冲突 → 最终状态不确定

#### 场景 24.2：修复后——互斥锁保证串行执行

1. 管理员配置全局 bot token → auto_upgrade 获取 `bot_token_ops` 锁
2. 管理员更换 bot token → swap 尝试获取 `bot_token_ops` 锁 → 失败 → 等待
3. auto_upgrade 完成 → 释放锁
4. swap 获取锁 → 遍历所有路径 A 仓库（包括刚升级的）→ 用新 bot token 重建 webhook
5. 串行执行，无竞态

**修复**：auto_upgrade 和 swap 共用同一个分布式锁 `bot_token_ops`，保证串行执行。

**验收标准**：
- R24.1.1：auto_upgrade 和 global_bot_token_swap 共用分布式锁 `bot_token_ops`
- R24.1.2：auto_upgrade 持有锁时 swap 等待（不并行执行）
- R24.1.3：swap 持有锁时 auto_upgrade 等待

**测试用例**：
- T24-01：auto_upgrade 执行时 swap 等待锁释放后再执行
- T24-02：swap 执行时 auto_upgrade 等待锁释放后再执行

### R24.2 verify_token 异常分类契约不完整（P1）

**问题**：R21.3 设计区分 401（标记过期）和超时/5xx（不标记），但 `VCSProvider.verify_token` 的异常类型契约未定义。`detect_and_mark_expired` 调 `provider.verify_token(token)` 后用 `extract_http_status_from_exc(e)` 提取 HTTP 状态码，但该函数对不同异常类型（HTTPError / RuntimeError / ConnectionError / socket.timeout）的分类完整性未在设计中明确。

**影响**：误标记（网络抖动→标记过期→全局不可用1h）或漏标记（真401被当网络错误→死循环）。

**场景用例**：

#### 场景 24.3：网络超时被误判为 401

1. fail_task 检测到 401 → 调 `detect_and_mark_expired` → `provider.verify_token(token)`
2. VCS API 网络超时（不是 401，是 socket.timeout）
3. `extract_http_status_from_exc` 无法提取 HTTP 状态码 → 返回 None
4. None != 401 → 不标记 → 正确（R21.3.5）

#### 场景 24.4：RuntimeError 含 "401" 但不是 HTTP 401

1. fail_task 检测到错误 → `provider.verify_token(token)`
2. VCS API 返回 403 但 `_req` 抛 `RuntimeError("403 Forbidden")`
3. `extract_http_status_from_exc` 提取到 403 → 403 != 401 → 不标记 → 正确
4. 但如果 `_req` 抛 `RuntimeError("HTTP 401: Unauthorized")`（文本含 401）
5. `extract_http_status_from_exc` 需正确提取到 401 而非从文本匹配

**修复**：明确 `verify_token` 异常契约：token 无效时抛 `RuntimeError` 含 HTTP 状态码属性；网络超时/连接失败抛 `ConnectionError` / `socket.timeout`；`extract_http_status_from_exc` 优先从异常对象的 `code` / `status_code` 属性提取，fallback 到文本匹配。

**验收标准**：
- R24.2.1：`verify_token` token 无效（401）时抛含 HTTP 状态码的异常
- R24.2.2：`verify_token` 网络超时时抛 `ConnectionError` / `socket.timeout`（非 HTTPError）
- R24.2.3：`extract_http_status_from_exc` 优先从异常属性提取状态码，fallback 到文本匹配
- R24.2.4：网络超时/5xx 不标记过期（R21.3.5 确认覆盖）

### R24.3 host_vcs_username OAuth refresh 时未同步（P1）

**问题**：R20.1.5 定义 `host_vcs_username` 同步时机为"每次验证 token 有效性（`provider.verify_token`）时"。但 OAuth token 自动刷新（`get_user_token_with_refresh` → `refresh_oauth_token`）时不调 `verify_token`——刷新直接用 refresh_token 换新 access_token，不调 GET /user。如果用户在 VCS 平台改了用户名，OAuth 刷新后 host_vcs_username 不同步 → 自循环检测条件 2 不满足 → 重复执行。

**影响**：用户改名后路径 B 自循环检测失效。

**场景用例**：

#### 场景 24.5：用户改名后 OAuth 刷新不同步 host_vcs_username

1. 用户 A 路径 B 托管仓库 → host_vcs_username = "old_name" 写入 DB
2. 用户 A 在 GitCode 平台改用户名为 "new_name"
3. OAuth token 临近过期 → `get_user_token_with_refresh` → `refresh_oauth_token` 换新 token
4. refresh 不调 `verify_token`（GET /user）→ host_vcs_username 仍为 "old_name"
5. agent 用用户 token 发评论 → actor = "new_name"
6. 自循环检测条件 2：actor("new_name") != host_vcs_username("old_name") → 不满足 → 不拦截
7. 重复执行

#### 场景 24.6：修复后——OAuth refresh 后同步 host_vcs_username

1. OAuth refresh 成功 → 获取新 access_token
2. **新增**：用新 token 调 `provider.get_user_login(token)` 获取当前 login
3. 如果 login 与 DB 中 host_vcs_username 不一致 → 更新 DB
4. 下次 webhook 推送时 actor = "new_name" = host_vcs_username("new_name") → 条件 2 满足

**修复**：OAuth refresh 成功后，调 `provider.get_user_login(new_token)` 获取当前 login，与 DB 中 host_vcs_username 不一致则更新。

**验收标准**：
- R24.3.1：OAuth refresh 成功后调 `get_user_login` 获取当前 login
- R24.3.2：login 与 DB 中 host_vcs_username 不一致时更新 DB
- R24.3.3：查询失败不阻断 refresh（best-effort，记 warning）

**测试用例**：
- T24-03：OAuth refresh 后 host_vcs_username 与 VCS 平台 login 一致
- T24-04：用户改名后 refresh 同步 host_vcs_username → 自循环检测条件 2 满足

### R24.4 R15.5 清除仓库级 token 后 webhook 验证断裂（P1）

**问题**：R15.5.2 "清除后不重建 webhook（用户需 repair_host）"。但清除后 `_update_repo_bot_token` 会清 `webhook_secret`。此时 webhook 的实际 password 仍是旧 bot token（路径 A 创建时用的），DB 中 `webhook_secret` 已清空 → `get_webhook_verify_token` fallback 到 `gitcode_webhook_token`（也可能被清）→ 返回空或 stale 值 → webhook 验证 403。

**影响**：清除仓库级 token 后仓库 webhook 事件全部被拒绝，直到用户手动 repair_host。

**场景用例**：

#### 场景 24.7：清除仓库级 token 后 webhook 验证 403

1. 仓库走路径 A，有仓库级 bot token → webhook password = 仓库级 bot token
2. 用户清除仓库级 token → `_update_repo_bot_token` 清 `bot_gitcode_token` + 清 `webhook_secret`
3. GitCode 推 webhook → `get_webhook_verify_token`：webhook_secret=空 → gitcode_webhook_token=空 → fallback get_repo_bot_token_no_global → 返回全局 bot token
4. webhook 实际 password = 仓库级 bot token（已清除但 webhook 仍存在）
5. 全局 bot token != 仓库级 bot token → 验证 403

#### 场景 24.8：修复后——清除时保留 webhook_secret

1. 用户清除仓库级 token → `_update_repo_bot_token` 清 `bot_gitcode_token` + 清 `bot_token_status`
2. **修复**：不清 `webhook_secret`（保留旧值，webhook 验证仍用旧密钥）
3. GitCode 推 webhook → `get_webhook_verify_token`：webhook_secret=旧值 → 验证通过
4. 用户需 repair_host 重建 webhook（切换到全局 bot token 密钥）— 但在 repair 前 webhook 不断链

**修复**：R15.5.2 清除仓库级 token 时不清 `webhook_secret`（保留旧值供 webhook 验证），直到 repair_host 重建 webhook 时才更新。

**验收标准**：
- R24.4.1：清除仓库级 token 时 `webhook_secret` 保留旧值（不清空）
- R24.4.2：清除后 `get_webhook_verify_token` 返回旧 webhook_secret → webhook 验证通过
- R24.4.3：repair_host 重建 webhook 后 `webhook_secret` 更新为新值

### R24.5 pipeline 步骤顺序文档矛盾修正（P1）

**问题**：两份文档对 pipeline 步骤顺序描述矛盾：
- `05-path-b-self-loop.md` §5.6：`2.0b → 2.0 → 2.0a → 2.0a2`
- `09-webhook-a2a-token.md` R20.21：`2.0b → 2.0a → 2.0 → 2.0a2`

代码实现（event_pipeline.py L1283+）是 `2.0b → 2.0 → 2.0a → 2.0a2`（与 05-path-b-self-loop.md 一致）。

**修复**：修正 `09-webhook-a2a-token.md` R20.21 的步骤顺序为 `2.0b → 2.0 → 2.0a → 2.0a2`。

**验收标准**：
- R24.5.1：所有文档中 pipeline 步骤顺序统一为 `2.0b → 2.0 → 2.0a → 2.0a2`

### R24.6 健康检查分布式锁名未区分 provider（P2）

**问题**：R17.2.9 分布式锁名 `token_health_check`，GitCode 和 GitHub 的健康检查共用一个锁。GitCode 健康检查拿锁时 GitHub 健康检查被阻塞，延迟最多 1h。

**场景用例**：

#### 场景 24.9：GitCode 健康检查阻塞 GitHub

1. 实例 A 获取 `token_health_check` 锁 → 执行 GitCode 健康检查（遍历 100 个仓库，耗时 10s）
2. 实例 B 尝试获取锁执行 GitHub 健康检查 → 失败 → 跳过
3. GitHub 全局 bot token 已过期但 10s 内未被健康检查发现

**修复**：锁名改为 per-provider：`token_health_check:gitcode` / `token_health_check:github`。

**验收标准**：
- R24.6.1：健康检查锁名改为 `token_health_check:{provider}`
- R24.6.2：GitCode 和 GitHub 健康检查互不阻塞

### R24.7 webhook_secret 脱敏未覆盖 result_text/agent_logs（P2）

**问题**：R19.1 + R21.10.2 扩展脱敏到 webhook_secret，但只覆盖日志。R20.25 的 result_text/agent_logs 脱敏只覆盖 `repo_gitcode_token` + `user_token`，未覆盖 `webhook_secret`。agent 如果在输出中复述 webhook_secret 值 → 泄露到 dashboard。

**场景用例**：

#### 场景 24.10：agent 输出中包含 webhook_secret

1. agent 执行 skill 检查环境变量 → 输出 `WEBHOOK_SECRET=abc123...`
2. 输出写入 `agent_tasks.result_text` → dashboard 可见
3. 攻击者获取 webhook_secret → 伪造 webhook 事件

**修复**：R20.25.1 脱敏列表新增 `webhook_secret`。

**验收标准**：
- R24.7.1：result_text/agent_logs 脱敏覆盖 `webhook_secret` 值
- R24.7.2：脱敏列表从 task 行 + runtime + managed_repos.webhook_secret 动态构建

### R24.8 验收清单映射

| 遗漏编号 | 优先级 | 验收标准 |
|---------|--------|---------|
| R24.1 | P0 | T24-01～T24-02 |
| R24.2 | P1 | R24.2.1～R24.2.4 |
| R24.3 | P1 | T24-03～T24-04 |
| R24.4 | P1 | R24.4.1～R24.4.3 |
| R24.5 | P1 | R24.5.1 |
| R24.6 | P2 | R24.6.1～R24.6.2 |
| R24.7 | P2 | R24.7.1～R24.7.2 |

---

## 第二十五章 R25：第八轮审计修复——no_global 语义 + GitHub A→B webhook 重建

### R25.1 get_repo_bot_token_no_global 语义被 read_stored_token 破坏（P0）

**需求来源**：feat/issue-372-user-token-driven 分支深度审计发现。

**问题描述**：

`get_repo_bot_token_no_global`（db.py）函数名明确语义为"no_global"——不含全局 bot token 兜底。原基线实现只查 managed_repos.bot_gitcode_token + devops_agent_session.repo_gitcode_token，两层为空返回空串。issue-372 分支改为调 `TokenResolver.read_stored_token()`，策略链包含第 3 级 GlobalBotToken——对未托管仓库返回了全局 bot token，破坏"no_global"语义。

**影响**：
- A2A 鉴权 `_authorize_a2a` 对未托管仓库返回 403（`_expected` 非空导致 `_bot_ok` 校验失败）
- test_t1_queue_mode_enqueues 在基线通过，在分支失败
- `get_webhook_verify_token` fallback 可能误用全局 bot token

**修复要求**：
- R25.1.1：`get_repo_bot_token_no_global` 调 `read_stored_token` 时跳过 GlobalBotToken 策略
- R25.1.2：跳过后路径 A 仓库（bot_gitcode_token 非空）行为不变
- R25.1.3：跳过后未托管仓库返回空串（恢复基线行为）
- R25.1.4：跳过后路径 B 仓库返回用户 token（与 get_repo_token_with_refresh 行为一致）

**场景用例**：

#### 场景 25.1：A2A 鉴权对未托管仓库放行

前置条件：全局 bot token 已配置，仓库 owner/repo 未托管。

1. 远程 worker 调 `POST /api/a2a`，repository=owner/repo
2. `_authorize_a2a` 调 `get_repo_bot_token_no_global('gitcode:owner/repo')`
3. 修复前：返回全局 bot token → `_expected` 非空 → 403
4. 修复后：跳过 GlobalBotToken → 返回空串 → `_expected` 为空 → 放行

**验收标准**：
- R25.1.5：test_t1_queue_mode_enqueues 通过（A2A 对未托管仓库放行）
- R25.1.6：`get_repo_bot_token_no_global` 对无 bot_gitcode_token 的托管仓库返回空串（有全局 bot token 时）

### R25.2 GitHub host_repo 路径 A→B 降级时已有 webhook 不重建（P1）

**需求来源**：feat/issue-372-user-token-driven 分支深度审计发现。

**问题描述**：

`host_repo` GitHub 分支中，当 `existing_id` 非空（已有 webhook）时，在 `if existing_id:` 分支内部检查 bot 协作者状态。A→B 降级时生成新 `_gh_webhook_secret`，但 `existing_id` 仍指向旧 webhook（路径 A 创建时无 secret）。DB 写入新密钥，GitHub webhook 实际无 secret → webhook 验证永久失败。

GitCode 分支不存在此问题（A→B 降级在 webhook 查找之前）。

**修复要求**：
- R25.2.1：GitHub host_repo A→B 降级时（bot_status in error/invite_pending/add_failed），如果 `existing_id` 非空，删除旧 webhook 并设 `existing_id = None`
- R25.2.2：删除成功后走 `setup_github_webhook` 重建分支，用新 secret 创建 webhook
- R25.2.3：删除失败时不写新 `_gh_webhook_secret`（设为空串），保留 `existing_id`，降级为 loud fail 或用旧值
- R25.2.4：路径 A 正常（bot 协作者 OK）不进入降级分支，行为不变
- R25.2.5：初始路径 B（无全局 bot token）已有删旧重建逻辑，不受影响

**场景用例**：

#### 场景 25.2：GitHub 仓库 A→B 降级（bot token 过期）

前置条件：全局 GitHub bot token 已配置但已过期，仓库之前用路径 A 托管（webhook 无 secret）。

1. 用户重新托管
2. `_gh_use_path_b = False`（有全局 bot token）
3. `existing_id` 非空（路径 A 旧 webhook）
4. `_try_github_bot_collaborator` → bot_status="error"（token 过期）
5. 降级：`_gh_use_path_b = True` + 生成新 secret
6. 修复前：直接写 DB → DB 有新 secret 但 GitHub webhook 无 secret → 验证永久失败
7. 修复后：删除旧 webhook → `existing_id = None` → 重建 webhook with secret → DB secret 与 webhook secret 一致 → 验证通过

#### 场景 25.3：删除旧 webhook 失败时的降级

1. 步骤 1-5 同场景 25.2
2. `remove_github_webhook` 失败
3. `_gh_webhook_secret = ""`（不写新密钥）
4. `existing_id` 保留 → 写 DB 时 webhook_secret 为空
5. `get_webhook_verify_token` fallback 到 `github_webhook_token`（路径 A 旧值）
6. webhook 验证用旧 token（与 GitHub webhook 实际密码一致）
7. 验证通过，后续 API 调用 401 触发 token 过期检测

**验收标准**：
- R25.2.6：A→B 降级时删除旧 webhook 并重建，DB webhook_secret 与 GitHub webhook secret 一致
- R25.2.7：删除旧 webhook 失败时 webhook_secret 为空串，不写新密钥
- R25.2.8：路径 A 正常（bot OK）不删除旧 webhook

### R25.3 UserToken status=4 后执行链仍拿坏 token（P1）

**需求来源**：E2E 端到端实测发现（路径 B 仓库 acme/demo + host_user_id=77 + 用户 OAuth token 逐环验证）。

**问题描述**：

用户 token 被 `detect_and_mark_expired` 标记 status=4（过期）后，后续新 issue 的 task 仍然拿到坏 token 继续跑 agent，必然 401 失败——浪费一整轮 agent 执行。

根因：`UserToken._is_expired()` 恒返回 `False`，导致 `get_token()` 永不对 status=4 的用户 token 短路。原设计意图是 best-effort 发评论（R10），但执行链（claim/agent）也走同一个 `get_token()` → 过期 token 被用于 git clone/push → 必然 401。

**两个设计目标冲突**：
- 回帖路径（best-effort）：期望拿到过期 token 试发"Token 已过期"评论——返回过期 token 是对的
- 执行路径（claim/agent）：期望拿到可用 token 跑 agent——返回过期 token 浪费一整轮 agent + 必然 401

**修复要求**：
- R25.3.1：`UserToken._is_expired()` 改为检查 `external_identities.status=4` 返回 True（与 DevopsAgentSessionToken / RepoBotToken / GlobalBotToken 三层一致）
- R25.3.2：执行链（`get_token()` / `resolve()`）对 status=4 的 UserToken 短路返回空串 → claim/agent 拿不到过期 token
- R25.3.3：回帖链（`UserTokenHandler.get_comment_token()`）改为用 `_fetch()` 直接读 token（绕过 `_is_expired` 检查），保留 best-effort 行为
- R25.3.4：回帖失败（401）时 `post_issue_comment` 的 except 兜底返回 False，不阻断主流程
- R25.3.5：error_code（TOKEN_EXPIRED / NO_TOKEN_AVAILABLE）写入 task errors，dashboard 展示
- R25.3.6：用户重新绑定后 status 恢复为 1，_is_expired 返回 False，执行链恢复正常

**语义分离契约**：

| 调用路径 | 方法 | status=4 行为 | 理由 |
|---------|------|--------------|------|
| 执行链（claim/agent） | `get_token()` / `resolve()` | 短路返回空串 | 过期 token 不可用于执行，避免浪费 agent |
| 回帖链（complete_task/fail_task） | `get_comment_token()` → `_fetch()` | best-effort 返回过期 token | 试发"Token 已过期"评论，发不出就算了 |
| A2A 鉴权 | `read_stored_token()` → `_fetch_stored()` | 返回存储 token（不检查过期） | A2A 鉴权用原始值比对，不需可用 |
| 自循环检测 | `resolve_config_level()` → `_fetch_stored()` | 返回存储 token（不检查过期） | 判断配置级别，不需可用 |

**场景用例**：

#### 场景 25.4：token 过期后新 issue 不再浪费 agent 执行

前置条件：路径 B 仓库 acme/demo（host_user_id=77），用户 OAuth token 已被标记 status=4（过期）。

1. 用户在 issue #42 创建评论触发 pipeline
2. pipeline 建 task（token 不落库，正确）
3. claim → `get_task_token(task)` → `TokenResolver.get_token`
4. UserToken._fetch() 返回坏 token → _is_expired() 查 status=4 → 返回 True → 短路返回空串
5. claim 注入空 token
6. 修复前：agent 拿坏 token 执行 → git clone 401 → fail_task → 浪费一整轮
7. 修复后：agent 拿空 token → fail_task 写 NO_TOKEN_AVAILABLE → 不执行 agent

**验收标准**：
- R25.3.7：status=4 时 `get_token()` 对 UserToken 短路返回空串
- R25.3.8：status=4 时 claim 不注入过期 token

#### 场景 25.5：token 过期后回帖 best-effort 试发

前置条件：同场景 25.4，task fail 后需要发"Token 已过期"评论。

1. fail_task → detect_and_mark_expired → status 已是 4 → 返回 TOKEN_EXPIRED
2. 回帖：`get_comment_token(result)` → `UserToken._fetch(repo_key)` 绕过过期检查 → 返回坏 token
3. `post_issue_comment` 用坏 token 试发 → GitCode API 401 → except 返回 False
4. 回帖发不出去（预期——过期 token 发不了评论）
5. error_code 已写入 task errors → dashboard 展示"Token 已过期"

**验收标准**：
- R25.3.9：status=4 时 `get_comment_token` 仍返回过期 token（best-effort）
- R25.3.10：回帖 401 时不阻断主流程（except 兜底）

#### 场景 25.6：用户重新绑定后 token 恢复

前置条件：用户看到"Token 已过期"提示后重新绑定 PAT/OAuth。

1. `create_external_identity` → status 恢复为 1
2. 下一个 issue → claim → `get_task_token` → UserToken._fetch 返回新 token
3. _is_expired() 查 status=1 → 返回 False → 不短路 → 返回新 token
4. agent 正常执行

**验收标准**：
- R25.3.11：status 恢复为 1 后 `get_token()` 正常返回 token
- R25.3.12：agent 正常执行不 401

#### 场景 25.7：PAT 无 refresh_token 过期后的行为

前置条件：路径 B 仓库，用户绑定的 PAT（无 refresh_token）被标记 status=4。

1. PAT 无 refresh_token → `_has_refresh_token` 返回 False → 不触发刷新
2. token 被 401 检测标记 status=4
3. 修复后：执行链短路（_is_expired=True）→ 不浪费 agent
4. 回帖链 best-effort → PAT 过期发不出评论
5. dashboard 展示"Token 已过期"→ 用户重新绑定

**验收标准**：
- R25.3.13：PAT（无 refresh_token）status=4 时执行链短路
- R25.3.14：PAT status=4 时回帖链 best-effort 行为不变

### R25.4 验收清单映射

| 遗漏编号 | 优先级 | 验收标准 |
|---------|--------|---------|
| R25.1 | P0 | R25.1.1～R25.1.6 |
| R25.2 | P1 | R25.2.1～R25.2.8 |
| R25.3 | P1 | R25.3.1～R25.3.14 |

### R25.4 bot_token_health 测试未适配 R17 改造（P1）

**需求来源**：外部 AI 审计重审发现。

**问题描述**：

R17.2.1 将 `FAILURE_THRESHOLD` 从 3 改为 1（1 次 401 即标记 unhealthy），R18.5.8 将 `platform_label` 改为 `provider.capitalize()`。但 `tests/test_bot_token_health.py` 的 8 个用例仍基于旧阈值 3 和旧文案编写，导致 8 failed。

两类失败：
1. 大小写文案：`provider.capitalize()` 产出 "Gitcode"/"Github"，测试断言 "GitCode"/"GitHub"
2. 阈值语义：旧阈值 3 时首次失败=degraded，新阈值 1 时首次失败=unhealthy

**修复要求**：
- R25.4.1：`platform_label` 改为 `_PLATFORM_LABELS` 映射，正确返回 "GitCode"/"GitHub"
- R25.4.2：测试用例适配 `FAILURE_THRESHOLD=1` 语义——首次 401 失败 = unhealthy（不是 degraded）
- R25.4.3：`degraded` 中间态语义保留用于 network 错误（超时/5xx），401 直接 unhealthy
- R25.4.4：`consecutive_failures` 仍递增用于日志展示
- R25.4.5：基线 weekly-08 上 test_bot_token_health.py 28 passed → 修复后分支也全绿

**场景用例**：

#### 场景 25.8：GitCode bot token 401 后标记 unhealthy

前置条件：GitCode 全局 bot token 已配置，token 被 API 返回 401。

1. 健康检查调 `_verify_bot_token("gitcode")` → 401
2. consecutive_failures=1 → unhealthy=True
3. 标记 `meta.global_gitcode_bot_token_status = "expired"`
4. 下次 pipeline 取 token 时 GlobalBotToken 策略短路返回空串

**验收标准**：
- R25.4.6：401 失败时 unhealthy=True（不是 degraded）
- R25.4.7：platform_label 返回 "GitCode"/"GitHub"（不是 "Gitcode"/"Github"）

#### 场景 25.9：GitHub bot token 超时后不标记 unhealthy

前置条件：GitHub 全局 bot token 已配置，网络超时。

1. 健康检查调 `_verify_bot_token("github")` → 超时
2. 超时不计入 FAILURE_THRESHOLD
3. healthy 保持，不标记过期

**验收标准**：
- R25.4.8：超时/5xx 时不标记 unhealthy

### R25.5 AUTH_401_KEYWORDS 常量化半成品（P1）

**需求来源**：外部 AI 审计重审发现。

**问题描述**：

`token_service.py` 定义了 `AUTH_401_KEYWORDS` 常量 + `is_auth_401_error()` 函数，但全 `src/` 零调用点。10 处 401 检测仍用内联列表（chat.py 4处 + handler_async 1处 + vod_handler_async 1处 + event_pipeline 1处 + events.py 1处 + service.py 1处）。

半成品比没有更危险：下次补关键词的人会看到常量以为已收敛，实际 10 处各自独立维护。

**修复要求**：
- R25.5.1：10 处内联 `any(kw in _err_lower for kw in [...])` 替换为 `is_auth_401_error(_err_str)`
- R25.5.2：`is_auth_401_error` 内部逻辑与原内联列表完全等价（`.lower()` + `any(kw in AUTH_401_KEYWORDS)`）
- R25.5.3：关键词列表不变（7 个关键词原样保留）
- R25.5.4：所有调用方的 `_is_401` 变量语义不变
- R25.5.5：grep 确认全 src/ 无残留内联 401 关键词列表

**场景用例**：

#### 场景 25.10：agent 执行 401 后通过常量统一检测

前置条件：路径 B 仓库，用户 token 过期。

1. agent 执行 git clone → 401 "fatal: Authentication failed for..."
2. chat.py `is_auth_401_error(_err_str)` → "authentication failed" 命中 → True
3. 调 `detect_and_mark_expired(repo_key)` → 标记 status=4
4. R25.3 执行链短路 → 下次不浪费 agent

**验收标准**：
- R25.5.6：10 处调用方均通过 `is_auth_401_error` 检测 401
- R25.5.7：新增关键词只需改 `AUTH_401_KEYWORDS` 一处

#### 场景 25.11：新增 401 关键词只改一处

1. 某 VCS 平台更新了 401 错误文案，新增 "access token revoked"
2. 修复前：需要 grep 10 处内联列表逐一添加
3. 修复后：只需在 `AUTH_401_KEYWORDS` 列表末尾加一条
4. 所有 10 处调用方自动继承新关键词

**验收标准**：
- R25.5.8：新增关键词只需修改 `AUTH_401_KEYWORDS` 一处

### R25.6 验收清单映射

| 遗漏编号 | 优先级 | 验收标准 |
|---------|--------|---------|
| R25.1 | P0 | R25.1.1～R25.1.6 |
| R25.2 | P1 | R25.2.1～R25.2.8 |
| R25.3 | P1 | R25.3.1～R25.3.14 |
| R25.4 | P1 | R25.4.1～R25.4.8 |
| R25.5 | P1 | R25.5.1～R25.5.8 |

### R25.6 零配置时官方场景/技能同步被"无 token 即跳过"拦截（P1）

**需求来源**：实际部署验证发现（首次部署零配置时 scene_market 仅 8 行、skill 表为空）。

**问题描述**：

`sync_official_scenes()` 和 `sync_official_skills()` 在 `_get_sync_token()` 返回空时提前 return，跳过整个远程同步。但同步源仓库是公开的，匿名即可下载 ZIP（实测 HTTP 200，1.9MB）。`_download_repo_zip` 已有 `if token: headers["Authorization"] = ...` 守卫——空 token 时匿名请求可以正常工作。

**影响**：开发者第一次部署、什么都不配时，skill 表为空、scene_market 只有 8 个内置 featured 场景，官方场景/技能生态全部缺失——与 issue-372"零配置可用"目标冲突。

**修复要求**：
- R25.6.1：`_get_sync_token()` 为空时不提前 return，仍发起下载（headers 为空，匿名）
- R25.6.2：下载返回 401/403 时降级到 `_init_featured_scenes_if_empty()` 兜底
- R25.6.3：scene_sync 与 skill_sync 两处同步修改
- R25.6.4：`_get_sync_token` 读取优先级不变（env → configs/scene-sync.json）
- R25.6.5：有 token 时行为不变

**场景用例**：

#### 场景 25.12：首次部署零配置同步官方场景

前置条件：全新部署，无 GRAPE_DEV_TOKEN、无 configs/scene-sync.json。

1. 服务启动 → `sync_official_scenes()` 执行
2. `_get_sync_token()` 返回空串
3. 修复前：log "skip remote sync" → 仅 8 个内置场景
4. 修复后：空 token → 匿名下载 ZIP → HTTP 200 → 同步 12+ 官方场景
5. `SELECT COUNT(*) FROM scene_market` 从 8 增长到 20+

**验收标准**：
- R25.6.6：零配置时 scene_market 行数 > 8（官方场景同步成功）
- R25.6.7：零配置时 skill 表有官方技能入库
- R25.6.8：日志不出现 "skip remote sync"

#### 场景 25.13：私有仓库 401 降级

前置条件：同步源仓库改为私有，无 token。

1. 匿名下载 → HTTP 401
2. `raise_for_status()` 抛异常
3. except 捕获 → `_init_featured_scenes_if_empty()` 兜底
4. scene_market 仍有 8 个内置场景

**验收标准**：
- R25.6.9：401/403 时降级到内置场景，不崩溃

#### 场景 25.14：有 token 时行为不变

前置条件：配置了 GRAPE_DEV_TOKEN。

1. `_get_sync_token()` 返回 token
2. 下载 headers 含 Authorization
3. 行为与修复前完全一致

**验收标准**：
- R25.6.10：有 token 时同步行为不变

### R25.7 验收清单映射

| 遗漏编号 | 优先级 | 验收标准 |
|---------|--------|---------|
| R25.1 | P0 | R25.1.1～R25.1.6 |
| R25.2 | P1 | R25.2.1～R25.2.8 |
| R25.3 | P1 | R25.3.1～R25.3.14 |
| R25.4 | P1 | R25.4.1～R25.4.8 |
| R25.5 | P1 | R25.5.1～R25.5.8 |
| R25.6 | P1 | R25.6.1～R25.6.10 |

### R25.7 仓库设置页 Bot Token UX 语义误导（P1）

**需求来源**：实际部署验证发现（路径 B 仓库设置页显示"Bot Token 已配置"但实际仓库级 bot token 列为空）。

**问题描述**：

GET /api/settings/gitcode-repo/{owner}/{repo} 返回 `devops_bot_token_exists: bool`，通过 `get_repo_token_with_refresh()`（四级 fallback 链）取值。路径 B 仓库（host_user_id > 0）前三级为空时兜底到用户 token → `bool(bot_token) = True` → 前端显示"✓ 已配置"。但仓库级 bot_gitcode_token 列实际为空，生效的是托管用户的个人 token。用户困惑"我没配过仓库 token，为什么说已配置"。

**修复要求**：
- R25.7.1：后端 `get_gitcode_repo_config` 用 `TokenResolver.resolve()` 替换 `get_repo_token_with_refresh`，返回 `resolved_token_level` 字段
- R25.7.2：`resolved_token_level` 值为 "session"/"repo_bot"/"global_bot"/"user"/""（与 TokenResult.level 一致）
- R25.7.3：`devops_bot_token_exists` 字段保留（向前兼容旧前端）
- R25.7.4：前端 settings.js 根据 `resolved_token_level` 分级显示文案
- R25.7.5：路径 B 仓库（level=user）显示"用户 Token 驱动中"（非"✓ 已配置"）
- R25.7.6：路径 A 仓库（level=repo_bot）显示不变（"✓ 已配置"）
- R25.7.7：新增 i18n key（zh/en）：`user_token_active` / `global_bot_active` / `session_token_active`

**场景用例**：

#### 场景 25.15：路径 B 仓库设置页显示"用户 Token 驱动中"

前置条件：路径 B 仓库 acme/demo（host_user_id=2），用户已绑定 PAT。

1. 用户打开仓库设置页
2. 后端 resolve() → level="user"
3. 前端显示"用户 Token 驱动中"
4. 用户明白当前生效的是自己的 token

**验收标准**：
- R25.7.8：路径 B 仓库设置页显示"用户 Token 驱动中"（非"✓ 已配置"）

#### 场景 25.16：路径 A 仓库设置页显示不变

前置条件：路径 A 仓库（bot_gitcode_token 非空）。

1. resolve() → level="repo_bot"
2. 前端显示"✓ 已配置"

**验收标准**：
- R25.7.9：路径 A 仓库设置页显示不变

#### 场景 25.17：全局 Bot Token 环境下未托管仓库

前置条件：全局 bot token 已配置，仓库 bot_gitcode_token 为空，host_user_id=0。

1. resolve() → level="global_bot"
2. 前端显示"全局 Bot Token 生效中"

**验收标准**：
- R25.7.10：全局 bot token 环境下显示"全局 Bot Token 生效中"

### R25.8 验收清单映射

| 遗漏编号 | 优先级 | 验收标准 |
|---------|--------|---------|
| R25.1 | P0 | R25.1.1～R25.1.6 |
| R25.2 | P1 | R25.2.1～R25.2.8 |
| R25.3 | P1 | R25.3.1～R25.3.14 |
| R25.4 | P1 | R25.4.1～R25.4.8 |
| R25.5 | P1 | R25.5.1～R25.5.8 |
| R25.6 | P1 | R25.6.1～R25.6.10 |
| R25.7 | P1 | R25.7.1～R25.7.10 |

### R25.8 R25.3 回帖链断裂 + session 明文 token 外发（P1）

**需求来源**：外部 AI 审计重审发现。

**问题描述**：

R25.3 执行链短路修复引入回帖链断裂：complete_task/fail_task 的回帖链调 `resolve()`（含过期短路）→ status=4 时 level='' → handler=None → 什么都不发。设计文档场景 45.8 承诺"回帖仍 best-effort 试发"与实现矛盾。

同时发现 `ensure_default_agent_sessions` 把用户 OAuth token 明文写入 session 行，agents API 原样外发——用户凭据暴露。

**修复要求**：
- R25.8.1：complete_task/fail_task 回帖链 3 处 `resolve()` → `resolve_config_level()`（不走过期短路）
- R25.8.2：status=4 时回帖链仍能创建 UserTokenHandler + get_comment_token 返回过期 token（best-effort）
- R25.8.3：路径 B 仓库 session 行不写入用户 token（level=user 时写空）
- R25.8.4：agents API `repo_gitcode_token` 字段改为 `repo_gitcode_token_exists`（布尔，脱敏）
- R25.8.5：路径 A 仓库 session 行仍写入 bot token（不变）

**场景用例**：

#### 场景 25.18：token 过期后回帖仍 best-effort

前置条件：路径 B 仓库，用户 token status=4。

1. task fail → 回帖链 `resolve_config_level` → level="user"
2. UserTokenHandler.get_comment_token → 返回过期 token
3. best-effort 试发（401 则不发，不阻断主流程）

**验收标准**：
- R25.8.6：status=4 时回帖链 handler 非 None
- R25.8.7：status=4 时 get_comment_token 返回过期 token

#### 场景 25.19：agents API 不外发 token 明文

1. GET /api/agents → 返回 `repo_gitcode_token_exists: bool`
2. 不返回 token 明文

**验收标准**：
- R25.8.8：agents API 响应不含 token 明文

### R25.9 验收清单映射

| 遗漏编号 | 优先级 | 验收标准 |
|---------|--------|---------|
| R25.1 | P0 | R25.1.1～R25.1.6 |
| R25.2 | P1 | R25.2.1～R25.2.8 |
| R25.3 | P1 | R25.3.1～R25.3.14 |
| R25.4 | P1 | R25.4.1～R25.4.8 |
| R25.5 | P1 | R25.5.1～R25.5.8 |
| R25.6 | P1 | R25.6.1～R25.6.10 |
| R25.7 | P1 | R25.7.1～R25.7.10 |
| R25.8 | P1 | R25.8.1～R25.8.8 |

### R25.9 设置页显示双维度——配置状态 + 生效级别（P1）

**需求来源**：实际部署验证发现（用户配了仓库 token 后显示"Session token 生效中"而非"已配置"）。

**问题描述**：

用户配了仓库 token（bot_gitcode_token 非空）后，ensure_default_agent_sessions 把 token 同步到 session 层 → resolve() 第 1 级命中 → level="session" → 前端显示"Session token 生效中"。用户期望看到"已配置"（回答"我配了什么"），实际看到生效级别（回答"现在用哪级"）。

**修复要求**：
- R25.9.1：后端新增 `has_repo_bot_token` 字段——直接查 managed_repos.bot_gitcode_token 列是否非空（不走 resolver 链）
- R25.9.2：前端优先显示配置状态：has_repo_bot_token → "✓ 已配置"
- R25.9.3：前端附带显示生效级别小字说明：resolved_token_level → "Session 层生效" / "用户 Token 驱动中" / "全局 Bot 生效中"
- R25.9.4：路径 B 仓库（has_repo_bot_token=false, level=user）显示"用户 Token 驱动中"
- R25.9.5：有全局 bot token 但无仓库 token（has_repo_bot_token=false, level=global_bot）显示"全局 Bot 生效中"
- R25.9.6：devops_bot_token_exists 和 resolved_token_level 字段保留（向前兼容）

**场景用例**：

#### 场景 25.20：配了仓库 token 后显示"✓ 已配置"+ 生效说明

前置条件：用户配了仓库 token，已同步到 session 层。

1. GET /api/settings/gitcode-repo → has_repo_bot_token=true, resolved_token_level="session"
2. 前端显示"✓ 已配置" + 小字"Session 层生效"

**验收标准**：
- R25.9.7：配了仓库 token 时显示"✓ 已配置"

#### 场景 25.21：路径 B 仓库显示"用户 Token 驱动中"

1. has_repo_bot_token=false, resolved_token_level="user"
2. 前端显示"用户 Token 驱动中"

**验收标准**：
- R25.9.8：路径 B 仓库显示"用户 Token 驱动中"

### R25.10 验收清单映射

| 遗漏编号 | 优先级 | 验收标准 |
|---------|--------|---------|
| R25.1 | P0 | R25.1.1～R25.1.6 |
| R25.2 | P1 | R25.2.1～R25.2.8 |
| R25.3 | P1 | R25.3.1～R25.3.14 |
| R25.4 | P1 | R25.4.1～R25.4.8 |
| R25.5 | P1 | R25.5.1～R25.5.8 |
| R25.6 | P1 | R25.6.1～R25.6.10 |
| R25.7 | P1 | R25.7.1～R25.7.10 |
| R25.8 | P1 | R25.8.1～R25.8.8 |
| R25.9 | P1 | R25.9.1～R25.9.8 |
