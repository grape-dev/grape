# 凭据对象全景需求 — 系统级凭据清单与基线

> 状态：需求定义（梳理基线，非缺陷修复需求）
> 基线：weekly-08 head 2d97419c9（工作区干净，实测核对）
> 类型：架构基线文档（供后续凭据相关需求引用的"单一事实来源"）
> 敏感信息声明：本文档不含任何真实 IP / 域名 / 密码 / 账号 / token（全部 `<xxx>` 占位符）
> 边界：**仅梳理现状，不含修复方案**。发现的缺口（AK/SK 明文存储等）单列 §七作为后续需求入口，不在本文档内设计。

## 〇、背景与目标

grape 系统内凭据对象分散在 5 大域、20+ 张表、10+ 个配置文件/环境变量中，长期缺少一份统一的全景清单。token 体系有 weekly-08/token-config/ 目录（Issue #372，覆盖 VCS token 专项），但平台会话凭据、云厂商 AK/SK、加密密钥体系无对应文档。各凭据需求文档（如 issue372-user-token-core-requirements.md）各自引用其中片段，缺乏统一引用基线。

本文档目标：

1. 建立全系统凭据对象的**权威清单**（对象 × 存储位置 × 格式特征 × 使用场景）
2. 标注每个凭据的**存储保护级别**（哈希 / 加密 / 明文），暴露保护不对称
3. 作为后续凭据相关需求的**需求范围锚点**——新需求先对照本文档定位涉及的凭据对象，再写专项设计

## 一、凭据域总览

```
① Grape 平台会话凭据（登录态）
② VCS 平台凭据（GitCode/GitHub：系统级/用户级/仓库级/会话级 + 统一策略链）
③ Worker/引擎接入凭据（rta_ / engine / run_token / lease）
④ 云厂商与外部服务凭据（华为云 AK/SK、CodeArts、OBS、飞书、邮件、VLM、MySQL）
⑤ 加密密钥体系（保护以上所有凭据的密钥）
```

依赖关系：② ③ ④ 的明文凭据均依赖 ⑤ 的加密能力；① 的 JWT 签名依赖 ⑤ 的 JWT 密钥池。

## 二、① Grape 平台会话凭据（登录态）

### 2.1 凭据清单

| # | 凭据对象 | 存储位置 | 格式/特征 | 使用场景 |
|---|---------|---------|----------|---------|
| S1 | JWT access_token | 无状态（不落库） | `src/api/jwt_utils.create_access_token` 签发，payload 含 sub/username/ver/role | 全部 Web API `Authorization: Bearer`；无状态验证（签名+过期），token_version 对库实现踢人 |
| S2 | JWT refresh_token | HttpOnly cookie `refresh_token`（src/auth/cookie_utils.py，path=`/api/auth`） | `create_refresh_token` 签发；JS 不可读（防 XSS） | 登录续期（grape_auth.py 4 处 set_refresh_cookie） |
| S3 | token_version | `grape_accounts.token_version` / `admin_accounts.token_version`（int） | 改密码/登出时 `increment_token_version`（grape_account_db.py:348） | 旧 JWT 失效（集群多实例一致——JWT 无状态+版本对库） |
| S4 | 用户 API Key | `api_tokens` 表：token_sha256（sha256，不存明文）+ scopes + revoked | 前缀 `grape_`；`verify_api_token`（auth_token.py:163） | 对外业务 API 长期凭据；三通道认证之一（deps.py:137 分流） |
| S5 | 会话级临时 token | `session_api_tokens` 表：token_sha256 + expires_at + session_key/repo_key/agent_type | 前缀 `tk_`；`issue_session_token`（auth_token.py:37） | Agent 执行期凭据：A2A 鉴权、仓库级上下文；`require_no_session_token` 禁止其调账户管理端点（scope 隔离，weekly-03 token-scope） |
| S6 | 管理员凭据 | `admin_accounts.password_hash`（bcrypt）+ token_version | `require_admin` 校验 role=admin + 版本对库 | admin 后台鉴权；旧哈希登录时自动升级 bcrypt（admin/db.py verify_admin） |
| S7 | 用户密码 | `grape_accounts.password_hash`（bcrypt，旧 SHA-256 登录时自动升级） | `verify_grape_account`（grape_account_db.py:202） | 用户名/邮箱登录 |
| S8 | 邮箱验证码 | `verification_codes.code_hash`（sha256）+ attempts/expires | 签名密钥复用 JWT 密钥（auth/verification.py:52） | 邮箱验证/注册/找回密码；防暴力（attempts 计数） |
| S9 | OAuth state | `oauth_states` 表（state PK → provider + data_json + expires） | 一次性，带过期 | OAuth 防 CSRF；`_safe_decrypt_state_token` 解 state 携带的 token（grape_auth.py:98） |
| S10 | binding_ticket | `binding_tickets` 表（grape_account_db.py:1394） | 一次性票据，10 分钟过期 | 已登录用户绑定第三方账号（popup 与已登录 bind 流程）；**安全设计：票据只存定位信息，刻意不存 access_token/refresh_token** |

### 2.2 认证通道与分流（deps.py:120-240）

三通道取值（Bearer / PRIVATE-TOKEN header / ?access_token= query）后按前缀分流：

| 前缀 | 类型 | 校验函数 | auth_type |
|------|------|---------|-----------|
| `tk_` | 会话级临时 token | verify_session_token | session_token |
| `grape_` | 用户级 API Key | verify_api_token（+用户 status 校验 SEC-3） | api_key |
| 其他 | JWT access token | get_current_user（签名+ver 对库+status 缓存 5s） | jwt |

## 三、② VCS 平台凭据（GitCode/GitHub）

> 专项细节见 `docs/weekly-08/token-config/` 目录（Issue #372 R15-R24 已实现）。本节只做清单化收编。

### 3.1 系统级（DB meta 表，admin 配置）

| # | 凭据对象 | meta key | 使用场景 |
|---|---------|----------|---------|
| V1 | OAuth App client_id/secret | `oauth_gitcode_client_id` / `oauth_gitcode_client_secret` / `oauth_github_*` / `oauth_*_redirect_uri` | OAuth 流程发起与 code 换 token（auth/providers.py exchange_token:251） |
| V2 | 全局 bot PAT | `global_bot_gitcode_token` / `global_bot_github_token` | TokenResolver 第 3 级兜底：建 webhook、bot 邀协作者、pipeline 写操作兜底；60s TTL 缓存 + 集群 meta 共享（Issue #345 DB 化，文件 configs/global-bot-token.json 降级为只读迁移源） |
| V3 | 全局 bot 用户名 | `global_bot_usernames`（JSON 数组） | 自循环检测（bot 自己的操作不触发 pipeline） |
| V4 | webhook 回调地址 | `webhook_url` | GitCode/GitHub 事件推回入口 |

### 3.2 仓库级（managed_repos 表）

| # | 字段 | 使用场景 |
|---|------|---------|
| V5 | `bot_gitcode_token` / `bot_github_token` | TokenResolver 第 2 级；A2A 鉴权严格比对（`get_repo_bot_token_no_global`，webhook.py:298）；写操作用 `get_repo_bot_token`（跳过 agent session 层） |
| V6 | `gitcode_webhook_token` / `webhook_secret` | webhook 入口验证：X-Gitcode-Token 比对（webhook.py:178）/ GitHub X-Hub-Signature-256 HMAC（webhook.py:249）；#372 起与 bot token 解耦 |
| V7 | `bot_token_status` | R16 过期框架标记（expired 短路） |
| V8 | `host_user_id` | 路径 B 托管用户——token 降级到该用户 OAuth token（get_repo_token_with_refresh 路径 C） |
| V9 | `bot_username` | 自循环检测 + 邀协作者目标 |

### 3.3 用户级（external_identities 表，AES-256-GCM 加密）

| # | 字段 | 使用场景 |
|---|------|---------|
| V10 | `access_token`（加密） | 用户 OAuth/PAT token：建 issue/PR、仓库操作、Chat、agent 执行（`get_user_token_with_refresh`）；status=4 标记过期（status 语义见 3.6） |
| V11 | `refresh_token`（加密） | OAuth 自动刷新：401 重试 + 预刷新（10 分钟阈值；无 expires 时 fetched_at+30min 降级）+ 并发刷新锁（`_gitcode_refresh_locks`，Issue #52） |
| V12 | `token_expires_at`（TEXT UTC） | 过期判断；**两套格式并存**：external_identities 存 TEXT（strftime UTC），user_credentials.config_json 存 epoch int——读取侧兼容（deps.py `_parse_utc_timestamp`） |

### 3.4 会话级（devops_agent_session / issue_agent_session / chat_agent_session 表）

| # | 字段 | 使用场景 |
|---|------|---------|
| V13 | `repo_gitcode_token` / `repo_github_token` | TokenResolver 第 1 级（agent 专属）；`token_status` 过期标记 |
| V14 | `im_app_id` / `im_app_secret` | 会话绑定的 IM bot 凭据（见 §五 V22 汇聚逻辑） |

### 3.5 TokenResolver 统一策略链（src/token_resolver.py / src/token_service.py）

四级 fallback：DevopsAgentSessionToken（1级）→ RepoBotToken（2级，host_user_id 守卫）→ GlobalBotToken（3级，per-provider status）→ UserToken（4级，best-effort 不短路）。

四个语义入口（方法分层契约：`_fetch` 纯读 / `_is_expired` 只读标记 / `get_token` 含短路）：

| 入口 | 过期检查 | OAuth refresh | 消费场景 |
|------|---------|--------------|---------|
| `get_token()` | 是（短路） | 是 | 执行 token：pipeline/agent/clone/push（~15 场景） |
| `read_stored_token()` | 否 | 否 | A2A 鉴权 / session 写入（原始值比对） |
| `resolve()` | 是 | 是 | 需要级别差异的调用方（TokenResult.level） |
| `resolve_config_level()` | 否（用 _fetch/_fetch_stored） | 否（跳过 refresh） | 自循环检测（配置级别，token 可能已过期） |

配套机制：TokenActionHandler 4 个 Handler 职责链（自循环检测/评论 token/回帖/标记）；`detect_and_mark_expired` 401 探活（区分 401 标记 vs 超时/5xx 不标记，R21.3.5）；bot_token_health.py 每小时健康检查（分布式锁 `token_health_check`，FAILURE_THRESHOLD=1 仅对 401）；全局 token 换号 swap 七步流程（分布式锁 `global_bot_swap`）。

### 3.6 external_identities status 语义（与 grape_accounts 不同！）

| 值 | external_identities | grape_accounts |
|----|--------------------|----------------|
| 0 | 解绑（unbound_at 记录时间） | 禁用 |
| 1 | 活跃 | 活跃 |
| 4 | **token 过期**（凭据保留，重绑恢复 1） | **merged**（账号合并占位） |

两表 status=4 含义完全不同，注释/代码不可混用。UNIQUE(grape_user_id, provider) 约束：INSERT 前须清 status IN (0,4) 旧行。

### 3.7 其他 VCS 相关

| # | 凭据 | 位置 | 使用场景 |
|---|------|------|---------|
| V15 | 场景/技能同步 token | 环境变量 `GRAPE_DEV_TOKEN` → configs/scene-sync.json `gitcode_api_token`（scene_sync.py:226 `_get_sync_token`） | scene_sync / skill_sync 定时拉仓库 ZIP（grape-skills 开源仓等），独立于用户 token 体系 |
| V16 | GIT_ASKPASS 注入链 | repogit.py:297 `_write_askpass`，环境变量 fallback：gitcode=`BOT_GITCODE_TOKEN→USER_GITCODE_TOKEN→BOT_TOKEN→BOT_GITHUB_TOKEN`（github 对称） | agent clone/push，token 不落盘（clone 用 `http.extraheader` 内联注入） |

## 四、③ Worker/引擎接入凭据

| # | 凭据对象 | 存储位置 | 使用场景 |
|---|---------|---------|---------|
| W1 | Worker rta_ token | `runtime_credentials` 表：token_hash（sha256，不存明文）+ name（worker 名，全局唯一）+ scope（user/repo/global）+ owner_id/owner_name/repo_key + status(active/revoked) | 全部 `/api/runtime/*` 协议端点（register/heartbeat/claim/report）；`_auth_credential` Bearer 解析→sha256→查表（runtime.py:27，前缀校验 rta_）；install 脚本 `--token <rta_...>` 引导新 worker；BOLA 授权走 CredentialContext（认证+授权一次 SELECT 6 字段） |
| W2 | run_token | agent_tasks.started_at（worker_report_queue.py） | worker 上报幂等守卫：master 恢复后重放 complete/fail（带 run_token）幂等去重 |
| W3 | lease_token | `global_im_bot_config.lease_token` + lease_expires_at/lease_owner（im/bot_lease.py） | 飞书 bot 集群租约：同 app_id 同一时刻单实例持有；owner 存活预检 + steal_after 抢占 |
| W4 | 服务端凭据下发 | `/api/runtime/tasks/{task_id}/env` 端点（runtime.py:529）→ `_collect_server_env`（runtime/service.py:479） | 远端 worker 无本地 DB 时执行前拉最新凭据（AK/SK 注入为环境变量，见 V25/V26） |

## 五、④ 云厂商与外部服务凭据

| # | 凭据对象 | 存储位置 | 使用场景 |
|---|---------|---------|---------|
| V25 | 华为云 AK/SK | `user_credentials` 表（user_id+provider 主键，config_json 含 ak/sk） | 用户设置页写入（settings.py `PUT /settings/credentials`，占位符 `_is_placeholder` 拒绝写入）；SRE cloud-deploy 注入 `HUAWEICLOUD_SDK_AK/SK` 环境变量（service.py:495） |
| V26 | CodeArts AK/SK | 同上（provider='codearts'） | 注入 `CODEARTS_CLI_AK/SK` 环境变量（service.py:501） |
| V27 | OBS AK/SK | 环境变量 `OBS_ACCESS_KEY` / `OBS_SECRET_KEY`（+`OBS_ENDPOINT`/`OBS_BUCKET`/`OBS_PREFIX`，config.py:154-157） | 文件存储/skill ZIP/知识库 OBS 直连（storage/__init__.py:147 初始化 ObsClient） |
| V22 | 飞书 app_id/app_secret | 多表汇聚（feishu_async.py:1540 起 BotManager 加载逻辑）：① `chat_agent_session.im_app_id/im_app_secret`（用户级 chat bot）② `devops_agent_session.im_app_secret`（repo 维度 vod bot）③ `global_im_bot_config`（登记表，**只存 bot_app_id/bot_type/lease 字段，不存 secret**）；旧表 im_bot_config 已迁移 DROP（db_setup.py:3726-3740） | 换 tenant_access_token（feishu_async.py:243 `/auth/v3/tenant_access_token/internal`，payload 含 app_secret）；IM 消息收发/卡片 |
| V28 | SMTP 密码 / Resend API Key | configs/email.json（gitignored，动态读 mtime 检测改后免重启）> 环境变量 SMTP_PASSWORD / RESEND_API_KEY fallback | 邮件验证码发送（auth/email_service.py：smtp 中继或 resend Bearer 两种模式） |
| V29 | VLM API Key | 环境变量 `VLM_API_KEY` > configs/vlm-config.json（api_key/base_url/model） | 图片理解（agent/image_understanding.py:157 `_load_vlm_config`） |
| V30 | MySQL 密码 | configs/db-backend.json（gitignored） | 主库连接（生产 MySQL 集群库；本地默认 SQLite 兜底） |
| V31 | LLM provider api_key | 引擎本地配置文件：hermes 写 `~/.hermes/config.yaml`、opencode 写 opencode.json（`{env:VAR_NAME}` 引用模式，runtime/agent.py:2829 `_check_opencode_api_keys` 检查引用环境变量是否已注入） | 引擎推理认证 |

## 六、⑤ 加密密钥体系（凭据的凭据）

| # | 密钥 | 来源（优先级） | 加密对象 |
|---|------|--------------|---------|
| K1 | credential_keys | configs/security.json `credential_keys`（`kid:base64(32B)` 逗号分隔，支持轮换，security_config.py:84）> 从 JWT_SECRET 派生 sha256 > dev 默认 | **external_identities.access_token / refresh_token**（AES-256-GCM，src/auth/credential_crypto.py：存储格式 `{kid}:{nonce_b64}:{ct_b64}`；空串不加密直存；无冒号值兼容明文迁移期；解密失败抛 ValueError 不静默；生产（security.json 配置）强校验 cryptography 已装，拒绝降级明文） |
| K2 | JWT 密钥池 | security.json `jwt.secret_keys`（`kid1:secret1,kid2:secret2` 轮换池）+ `kid_current` > 环境变量 JWT_SECRET（无 kid 轮换）> dev 临时随机（`_ensure_dev_ephemeral_secret`） | access/refresh token 签名 + 验证码签名（复用同一密钥，verification.py:52） |

## 七、存储保护级别全景（不对称暴露）

| 保护级别 | 凭据对象 |
|---------|---------|
| **sha256 哈希**（最佳） | S4 api_tokens / S5 session_api_tokens / S8 verification_codes / W1 runtime_credentials |
| **AES-256-GCM 加密** | V10/V11 external_identities 的 access_token/refresh_token |
| **明文存储** | V2 全局 bot PAT（meta）/ V5 仓库级 bot token / V6 webhook secret / V13 session token / V25-V26 user_credentials AK/SK（config_json 明文 JSON）/ V22 飞书 app_secret（多表）/ V15 同步 token（文件/环境变量）/ V28-V30 各配置文件 / K1/K2 密钥本身 |
| **不落盘** | S1 JWT access_token / S2 refresh_token（HttpOnly cookie）/ V16 GIT_ASKPASS（token 不落盘） |

明文风险已知项（issue372-user-token-core-requirements.md 边界声明指向的"历史债"范围）：

| # | 缺口 | 证据 | 已关联事项 |
|---|------|------|-----------|
| G1 | user_credentials AK/SK 明文 JSON 存储 | user/db.py:530 set_credential 无 encrypt 调用 | weekly-06 设计已声明 crypto.json 废弃迁 security.json，但 AK/SK 写入路径未接入 credential_crypto |
| G2 | settings.py `GET /credentials` 把明文 ak/sk 原样返回前端 | settings.py:102-103 `"ak": hw_cred.get("ak",""), "sk": ...` | 回显面（响应含明文） |
| G3 | bot token（全局/仓库级）明文存 meta/managed_repos | Issue #349 立项未闭环 | cluster-bot-token-management.md Follow-up |
| G4 | session token（devops_agent_session.repo_gitcode_token 等）明文 | db_setup.py DDL 无加密 | — |
| G5 | configs/global-bot-token.json 保留真实凭据明文（已降级只读迁移源） | #345 决策：文件保留不删除不写 | — |
| G6 | crypto.json 按设计废弃但文件仍在 | weekly-06 文档 §迁移 vs configs/crypto.json 存在 | — |

> 以上 G1-G6 缺口已立项：修复需求+设计见《credential-governance-requirements-design.md》（同目录，业界方法论对照 + FR-1~FR-9）。G1→FR-3、G2→FR-1、G3→FR-4、G5→FR-7、G6→FR-8；G4（session token 明文）作为 FR-9 视 FR-4 落地后的存量评估决定。

## 八、需求项（本文档自身）

| 编号 | 需求 | 类型 |
|------|------|------|
| FR-1 | 凭据对象全景清单作为权威基线，后续凭据相关需求文档（含 weekly-08/token-config 体系、issue-372 系列、issue-345 系列）引用本文档作为对象/存储/场景的统一锚点，避免各文档各自定义不一致 | 文档基线 |
| FR-2 | 新增凭据对象时（新表/新配置文件/新环境变量承载 secret 类值），实现前先更新本文档清单——避免凭据面无限膨胀无登记 | 流程约束 |
| FR-3 | G1-G6 缺口清单保持追踪：修复立项时在此表补"已立项文档"链接，形成凭据债务台账闭环 | 债务追踪 |

## 九、不在范围

- 任何修复方案设计（G1-G6 的加密接入、脱敏、文件清理）——需专项需求+设计文档逐项确认
- VCS token 体系的行为语义（过期框架/健康检查/自循环检测细节）——归 weekly-08/token-config/ 目录
- #372 系列缺陷修复——归 issue372-user-token-core-requirements.md
- 前端凭据展示/输入交互——归各页面需求

## 十、验证方式（本文档事实核对方法）

本文档全部事实基于 weekly-08 head 2d97419c9 实测（非推断），核对命令模式：

```bash
# 表结构
grep -n "CREATE TABLE IF NOT EXISTS <table>" src/api/db_setup.py src/user/db.py src/session_manager.py
# 加密调用覆盖面（明文写入点排查）
grep -rn "set_credential\|INSERT.*user_credentials" src/ --include="*.py"
# meta key 清单
grep -rn "get_meta_value\|set_meta_value" src/user/db.py | grep -o 'oauth_[a-z_0-9]*\|global_[a-z_0-9]*'
```

后续引用本文档时若发现与代码不符，以代码为准并回改本文档（基线漂移维护义务）。

## References

- docs/weekly-08/token-config/00-index.md（VCS token 专项体系索引）
- docs/weekly-08/issue372-user-token-core-requirements.md（#372 缺陷需求，其"边界"声明指向本文档收编的历史债范围）
- docs/weekly-06/grape-account-system-tech-design-v2.md §2818/2869（crypto.json 废弃决策）
- src/auth/credential_crypto.py / src/auth/security_config.py（加密体系实现）
- src/token_resolver.py / src/token_service.py（策略链实现）
- src/api/deps.py / src/api/auth_token.py（认证通道分流）
