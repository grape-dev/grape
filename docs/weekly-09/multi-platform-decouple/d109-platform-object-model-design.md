> ⚠️ 本文为批次方案稿（D109 实施设计）。索引导航：./multi-platform-decouple-design.md
> 域：平台对象模型——三类配置（登录渠道 / 仓库方言 / 渠道↔平台关系）+ 四张表 + 派生能力视图。
> **bot 部分已被 D110 取代**（`bot` 归 `grape_t_repo_platforms` 行，`grape_t_bot_accounts` 已删除）：见 `d110-bot-single-home-design.md`。
> 关系：D108 解决"登录渠道配置解耦（文件真源、代码零平台字面量）"；D109 在其上立"平台"一等对象、
> 仓库凭据能力与用户级平台关系。D108-B 的环境变量开关（GRAPE_OAUTH_TOKEN_FALLBACK）由 D109 第一批删除。
> 全局约束：继承索引页「表设计铁律/凭据续期口径/验收纪律」。

# D109 平台对象模型（三类配置 / 四张表 / 派生能力视图）

## 1. 问题

1. **"平台卡"职责被放大**：configs/platforms/*.json 现在混了三类内容——方言/协议（dialect、host、
   api_base、webhook_*、issue_write_form、branch_create_form、pr_head_filter、git_tree_family、
   clone_auth）、凭据相关（token_storage_field、webhook_token_field、legacy_token_symbol、
   pat_create_url）、身份/展示（bot_username、brand_color、download_base、user_images_base）。
   后两类不是方言，"用哪种凭据"因此没有独立配置位。
2. **没有"平台"一等对象**：登录渠道与仓库平台是两个平行集合，靠"同名文件"隐式关联；
   grape_t_channel_repo_link / grape_t_user_repo_platform_link 这层关系完全不存在。
3. **"某 provider 是否支持仓库"有 4 套判据**（实查 2026-11-13）：
   platform_card.list_vcs_platforms()（scene_sync.py:91、vcs/adapter.py:64、bot_token_health.py:102、
   chat.py:110/458、repo_manager.py:547）、repos.py:141 _hosting_implemented_providers()、
   repo_manager.py:1146 _KNOWN_VCS_PROVIDERS()、前端三处。
   实锤差异：HOSTING_FULL_FLOW=True 仅 gitcode/gitee/github（gitlab 没有），却 4 个都在卡列表与
   已知集合里 → gitlab 同时 configured=true / hosting=false / login=false。
4. **用户级关系缺失**：settings.py:2428 GET /vcs/platforms 的唯一口径是"平台卡在册即可见"
   （注释原文：纯 PAT 部署不配 OAuth 也能绑定 gitee/gitlab；不再按 OAuth 配置筛选）——
   所有在册平台对所有用户可见，与"该用户是否登录过该平台、是否配了 PAT"无关。
   OAuth 回调只写 external_identities，PAT 保存只写凭据域，都不记录"用户获得了哪个平台的访问关系"。
5. **身份令牌越权可做仓库操作**：deps.py:560 get_user_token_for 同函数服务身份域与仓库域；
   token_resolver.py:438 fetch_user_token 的隐式序（①PAT ②OAuth）让登录令牌成为仓库操作供给源，
   并为它把 gitee scope 扩到 projects。

## 2. 目标与边界

目标：三类配置分层；登录（身份）与仓库（资源）两个对象；provider 关联；配置是定义源、DB 是运行时
权威；仓库凭据能力与用户级平台关系显式化。

**不做**：
- 一个登录 token 跨域复用到多个仓库平台；
- 协议级 token exchange（RFC 8707/8693）——Gitee/GitCode/GitHub/华为 AGC 的 OAuth 均不支持
  resource 参数与 token exchange 端点（Gitee 官方 OAuth 文档参数仅 client_id/redirect_uri/
  response_type/scope/state）。受众绑定只作建模语言，落地形式是"第二次独立授权"（应用 B 或 PAT）。
- 第一批不建用户凭据收敛表（user_repo_tokens）：oauth_identity 的 token 语义（引用 vs 独立授权
  产物）未定，先固化 schema 等于把未决语义写死。

## 3. 配置分层（三类配置）

| 配置文件 | 职责（只回答什么） | 字段（真实枚举见 §13） | 导入目标表 |
|---|---|---|---|
| `configs/oauth-providers.json` | **怎么登录**（身份域 OAuth 应用） | auth_url / token_url / user_url / scope / pkce_enabled / id_field / login_field / name_field / avatar_field / email_field / accept_header / claims_map / redirect_uri（**不含关联声明**：授权只在 channel-repo-links.json） | grape_t_oauth_channels |
| `configs/platforms/<p>.json` | **怎么和这个平台说话**（方言/协议） | dialect / host / api_base / api_auth / webhook_path / event_header / webhook_verify / webhook_dialect / issue_write_form / branch_create_form / pr_head_filter / pr_comment_endpoint / pr_labels_via_issues / git_tree_family / issue_search / clone_auth / resources / capabilities；展示字段（brand_color / download_base / user_images_base）留在本文件，用 `_presentation_fields` 文档键标注 | grape_t_repo_platforms |
| `configs/channel-repo-links.json`（关系配置） | **登录成功后要不要写用户↔平台关系**（不判定取 token） | links[]：oauth_provider / repo_provider（**只有配没配，没有档位**）；**文件缺席** → 按 provider 同名默认配对；**文件存在**即以其为准（links 为空 = 显式关闭） | grape_t_channel_repo_link |

边界纪律：
- 方言卡不含凭据驱动、不含关联、不含凭据值；展示字段用 `_presentation_fields` 文档键标注（不改
  数据结构、不加嵌套——避免破坏"键集 ⊆ dataclass 字段"守卫）。
- 凭据能力配置不含 OAuth 端点字段（auth_url/token_url/scope/pkce_enabled），不含任何凭据值
  （client_id/secret/token）——与 D108-A"凭据不进文件"同口径，需守卫。
- 凭据能力配置**不写 oauth_channel**：身份源与仓库平台的对应关系属 grape_t_channel_repo_link，避免两处
  重复表达同一事实。
- `bot_username` 与 `supports_bot_token` 配对，必须同处一个对象；全局 bot token 本身仍走
  meta global_bot_<p>_token（凭据不进配置）。
- **bot 归属 repo_provider，不归 oauth**：bot 是「在仓库平台上执行写操作的账号」——跨名场景（huawei 登录
  → codearts 仓库）证明它只跟仓库平台有关。平台卡只声明「支不支持 bot」（supports_bot_token），
  grape_t_bot_accounts 存「有哪些账号」，managed_repos 存「这个仓库用哪个」，oauth 渠道不持有 bot。

- **配置派生 vs 运行态必须分表（第 5 条）**：判据是「能否进 git、能否被导入覆盖」。
  配置派生（grape_t_oauth_channels / grape_t_repo_platforms / grape_t_repo_credential_caps / grape_t_channel_repo_link）：导入器可覆盖
  定义字段，有 config_hash / drifted / orphaned；运行态（grape_t_bot_accounts / grape_t_user_repo_platform_link / 凭据域 PAT 与
  grape_t_repo_credentials）：含 token 或用户关系生命周期，导入器**绝不触碰**。合并的后果是 split mode
  失效或凭据被覆盖，两者都不可接受。跨表查询用能力视图聚合，不物理合并。
- **一个 provider 只对应一种方言**：dialect 是单值枚举（github / gitlab）；同一平台若有原生与兼容
  两套 API，拆成两个 provider（如 x / x-legacy），禁止把 dialect 改成数组或优先级列表。

**字段归属裁定（2026-11-13，实查消费者后定）**

| 字段 | 归属 | 依据（实查） |
|---|---|---|
| `token_storage_field` / `webhook_token_field` | → repo-credentials.json | `contracts/local/mgmt_local.py:40-52` 用它们决定凭据落哪一列 |
| `owner_identity_token_fallback` | → repo-credentials.json（drivers 的 `owner_fallback` 位或单布尔） | `kernel/token_resolver.py:116` 用它派生 owner-identity 回落策略，是凭据策略不是方言 |
| `pat_create_url` | → repo-credentials.json **单源**；登录域改经 provider 关联读 | 现两卡都可声明（`auth/providers.py:65` + `vcs/platform_card.py:461`）＝双源 |
| `legacy_token_symbol` | **留平台卡**（不迁、不废弃） | `api/deps.py:700-703` 用 PEP 562 `__getattr__` 按卡动态解析遗留函数名，消费者仍在 |
| `workflows_write` | **留平台卡**（第一批），第二批能力矩阵迁入 | `mgmt/user/repo_manager.py:563-570` 读卡默认值 + `credential_workflows_write` 三态覆盖 |
| `issue_search` | bool，留平台卡 | `vcs/platform_card.py:69 issue_search: bool`，`vcs/api_client.py:442` 按布尔分派 |

迁移纪律：`repo-credentials.json` 落地与从 `platforms/*.json` 删除上述五个字段（token_storage_field /
webhook_token_field / owner_identity_token_fallback / pat_create_url / bot_username）**必须在同一次提交**，
否则中间态出现"改哪边生效"的双源；配 grep 守卫（§11）。

- **配置统一 = 结构统一，不是语义统一（第 6 条）**：
  **统一什么**：每个平台同一套对象（OAuthChannel / PlatformCard / CredentialCaps / BotAccount）与同一套字段名
  （username / token_enc / is_default / supports_bot_token / drivers），同一条 CRUD 写入路径。
  **不统一什么**：方言字段（dialect / webhook_verify / clone_auth / issue_write_form / branch_create_form /
  pr_head_filter）、能力位（supports_* / drivers）、端点（auth_url / token_url / user_url / api_base / webhook_path）。
  **操作判据**：一个字段若在所有平台上「类型相同 + 含义相同 + 值域相同」→ 可统一；若值域因平台而异
  （枚举不同 / 格式不同 / 存在性不同）→ 属语义差异，不统一，且必须显式声明（缺省即"不支持"，不隐式回落）。
  **反例**：把 `token_compare` 当 `hmac` → webhook 验签全失败；把 gitee `pkce_enabled` 设 true → 每次登录弹授权。
  **运行时例外**：与 provider 无关的全局参数（超时 / flavor / 资源规格）走系统设置域，不进 channel/platform 表。
  **落点定死**：沿用现有系统设置域 = `meta` 键 + `src/kernel/config.py` 统一 getter（优先级
  env > meta > `configs/runtime-config.json` > 默认；实查 `get_runtime_task_timeout`(config.py:522) /
  `get_devstage_template_id`(592) / `get_devstage_flavor`(605)），**不新建 settings 表**（实查无 grape_t_settings）。
  判据：与 provider 无关 + 全局唯一 + 非凭据 → 系统设置域；进 channel/platform/bot 表即违规。

## 4. 对象模型（四张表 + 一个派生视图）

| 表 | 来源 | 关键列 |
|---|---|---|
| `grape_t_oauth_channels` | oauth-providers.json | provider PK / name / type / endpoints / scope（**只作登录协议声明，不参与仓库驱动判定**）/ pkce / 字段映射 / **client_id_enc / client_secret_enc（加密，配置 JSON 不含凭据）** / source(config\|manual) / config_hash；enabled 归 DB |
| `grape_t_repo_platforms` | platforms/*.json | provider PK / name / dialect / host / api_base / api_auth / webhook_* / 能力位 / 展示字段 / source / config_hash；enabled 归 DB |
| `grape_t_channel_repo_link` | channel-repo-links.json（缺省同名派生） | oauth_provider / repo_provider / source(config\|derived\|manual)；唯一索引 (oauth_provider, repo_provider)。**relation 列已于 2026-11-14 第二批删除（迁移 202611170011，§20）** |
| `grape_t_user_repo_platform_link`（**纯运行时，无配置来源**；2026-11-14 改名，原名 user_platform_link 看不出"仓库"） | OAuth 登录/绑定 / PAT 保存 / 撤销 / 回填迁移 | **只有关系与时间**：user_id / repo_provider / linked_at / revoked_at / revoked_reason(user_unbind\|credential_deleted\|oauth_unbound\|admin\|disabled_by_user) / **不存 via_bits、不存 status、不存来源渠道**（全部读取时算，见下）；唯一键 (user_id, repo_provider) |
| ~~`grape_t_bot_accounts`~~（D110 已删除） | 原设计为平台多账号目录；运行时从未读取（取 token 链里没有它），已退役 | bot 改为**平台行三列**：`grape_t_repo_platforms.bot_username / bot_token_enc / bot_token_status / bot_checked_at`（一平台一个 bot） |
| `platform_capabilities`（派生视图，不建表） | 实时派生 | login / login_supported（第一批恒等）/ configured / hosting（实时查模块 HOSTING_FULL_FLOW） / supports_bot_token / supports_webhook_secret / bot_username / grape_t_bot_accounts 摘要（username / is_default / status / last_used_at，**不含 token 值与 token 前缀**） |

说明：
- **建表规范（yangaiwu 拍板 2026-11-13）**：表名一律 `grape_t_` 前缀（与既有 grape_t_repo_credentials /
  grape_t_patrol_findings 一致）；主键一律 UUID（`TEXT PRIMARY KEY`，Python 侧 `uuid4().hex` 生成，禁自增）；
  每表带 `created_at` / `updated_at`（TEXT，默认 `datetime('now')`，updated_at 由应用层写入时刷新）；
  **每个字段必须有注释**；DDL 以 `src/api/platform_object_ddl.py` 为准（D110 起：四张表，无部分唯一索引——bot 默认位概念随账号目录一起消失）。
- **四张表 = 三类配置派生的三张定义/关联表 + 一张纯运行时表**（grape_t_user_repo_platform_link 用户↔仓库平台
  对应关系）。该运行表没有配置文件，文档必须明说
  （schema 草案常被问"它的配置在哪"）。凭据分层（2026-11-13 拍板）：能力声明在 caps、bot 凭据在
  bot_accounts、OAuth 客户端凭据在 oauth_channels 加密列、仓库级 webhook secret 在既有
  grape_t_repo_credentials（cred_type='webhook'）——同一 provider 聚合、不散落成 meta 键。
  **不新建"平台级 webhook secret"表**：现状是仓库级（managed_repos.webhook_secret 与 rc('webhook')，
  Issue #372「与 token 解耦」），平台级共享 secret 安全上更弱且无需求（该表已从 DDL 删除）。
- **hosting 不落表**：托管实现是代码声明（模块 HOSTING_FULL_FLOW），落表会让 config_hash 语义崩坏
  （代码改了、文件没变 → 表里旧值、无法漂移检测）。
- **credential 能力不是单布尔**：能力视图直接返回 drivers 数组 + supports_bot_token +
  supports_webhook_secret + bot_username，前端不必回查表。
- **grape_t_user_repo_platform_link = 纯对应关系表（2026-11-14 裁定，取代 via_bits 生命周期表）**：
  **表只存"这个用户与这个仓库平台是否存在接入关系"与两个时间点**，不存 status、不存来源、
  不存可用性布尔。理由（实查裁定）：①via_bits 只有 bit0/bit1/bit2，**没有渠道标识**，
  gitee 与 huawei 两条来源都记成 `oauth_login`，要区分只能反查 external_identities + link，
  既然能反查该列即冗余；②一次登录按 link 展开写多行，`first_linked_at` 全是同一时刻，无信息量；
  ③link 是配置、会变，表内来源位不会跟着变 → 必然漂移（绑定/解绑路径漏写即已发生过）。
  **替代规则**：关系行 = 准入清单（收窄语义），可用性一律读取时现算：
  `关系行(user,p) 有效 ∧ ( 有 PAT(p) ∨ ∃c: 用户有活跃 c ∧ link(c→p) )`。
  状态文案（pending_credential / active(pat) / token_available 等）由凭据代理层算出，不落表（§6）。
- **既有凭据快照表 `grape_t_repo_credentials`（不进新建七表，但模型必须列出）**：`repo_key × cred_type`
  （bot / webhook），S82/D58 起是仓库级凭据唯一权威；D109 后加 `derived_from` 记录来源账号，轮换按它
  精确传播（见 §6）。凭据分层：OAuth 客户端凭据在 `grape_t_oauth_channels` 加密列、bot 凭据在 `grape_t_bot_accounts`、
  仓库级 webhook secret 在既有 `grape_t_repo_credentials`（cred_type='webhook'），三者边界不混。
- **login_supported 第一批恒等于 login**（唯一来源是 grape_t_oauth_channels 行存在）；字段位保留给将来
  "平台支持登录但未配置"三态。
- **hosting 是粗粒度布尔**：只表达"托管全流程是否实现"；细粒度能力位（pr / issue 分项等）后续按需在
  模块侧扩展（能力视图加字段即可），第一批不做。

### 领域建模与持久化（新模块一律走 D53 持久化层，2026-11-13 拍板）

实查：项目已有 SQLAlchemy 2.0 持久化层 `src/kernel/persistence/`——`engine.py`（sqlite / mysql 双 backend，
与裸 SQL 同库）、`uow.py`（用例级 Session，出口统一 commit/rollback）、`repositories.py`（DTO 出入、接受外部
session、只 flush 不 commit）、`models.py`、`credential_usage_log.py`（审计）、`masking.py`（脱敏）、
`id_gen.uuid7_str`（时间有序 UUID）。仓库内仍有 2809 处裸 `.execute()`（legacy 域），**新表一律走 ORM、
不新增裸 SQL**（与 S91「调用方一律走本层」一致）。

**聚合与值对象（领域模型，不是表映射）**

| 聚合根 | 值对象 / 实体 | 领域行为 |
|---|---|---|
| OAuthChannel | ClientSecret（密文，永不 repr）、ChannelType（oauth2/oidc/saml）、RepoLinkSpec | build_auth_url()、is_pkce_enabled() |
| RepoPlatform | Dialect、WebhookSpec、CapabilityFlags | supports(webhook_dialect)、webhook_path() |
| CredentialCaps（实体，依附 platform） | Drivers、StorageField | allows(driver)、requires_scope() |
| BotAccount | BotToken（密文）、AccountStatus | rotate(token)、mark_expired()、is_default |
| UserPlatformLink | ViaBits、RevocationReason | add_via() / remove_via()（原子位运算）、status(creds, identities) 读取计算 |
| ChannelRepoLink | —（无档位：配对即授权） | —（allows_credentials 随档位退役删除） |

**持久化约束（六条）**
1. 每聚合一个 Repository（OAuthChannelRepo / RepoPlatformRepo / BotAccountRepo / UserPlatformLinkRepo /
   ChannelRepoLinkRepo）；方法接受外部 `Session`、**只 flush 不 commit**（D53 口径）。
2. 事务边界 = UoW：导入/扫描（四表 + epoch）一个 uow；OAuth 回调 / PAT 保存 / 撤销 各一个 uow；
   凭据轮换（bot_accounts + repo_credentials.derived_from）一个 uow。
3. **状态计算放领域对象**（`UserPlatformLink.status()`），不在 SQL 里 join 算；仓储只负责取原始行。
4. 主键用 `id_gen.uuid7_str()`（时间有序、索引友好），不用 uuid4。
5. **建表仍走 `db_setup` + `register_migration`**；ORM models 只做映射（**不 create_all**），避免双建表/双真源。
6. 凭据字段在 ORM 层用类型装饰器自动加解密（复用 `credential_crypto`），DTO 不携带密文；
   查询用 ORM 表达式（select/where），禁 f-string 拼 SQL。

## 5. 关联与用户关系

配置级关联（谁能用谁）：
- **配置入口在仓库侧**（2026-11-14 裁定）：管理台在「仓库平台卡 → 接受哪些登录渠道的令牌」里配置，
  底层仍是 `grape_t_channel_repo_link(oauth_provider, repo_provider)`（键对称，视角可换；档位列已删）。
  理由：运维的实际动作是"给这个仓库接登录"，不是"给这条渠道找仓库"；且 `relation` 容易被误读成开关，
  放在仓库侧把语义固定在"该平台接受谁"，渠道侧只读展示。
- **管理面口径变更（2026-11-15 用户拍板）**：配对关系**不再提供管理台人工编辑**——渠道页"可接入的代码托管平台"
  分节（选平台保存 / 删除 manual 行）已整段移除，理由是该分节的同域/跨名/未配置三段口径讲不清，且关系本不该让用户配。
  后端 `PUT/DELETE /api/admin/channel-repo-links/*` 与 manual 行语义保留（脚本/导入仍可用），运行时关系判定不变（见 d110 §14.3）。
- 配置文件形态（2026-11-14 起）：独立文件 `configs/channel-repo-links.json`，`links: [{"oauth_provider": "...", "repo_provider": "..."}]`（**只有配对，无档位**）；
  oauth 卡里的 `repo_links` 已删除（导入器拒收该键，整卡拒绝）；文件缺席 → 按 provider 同名默认关联；
  缺省派生仅在「渠道自己就是仓库平台」时指向自己。
- 第一批 DB 只建 `(oauth_provider, repo_provider)` 唯一索引；**"repo_provider 唯一"放导入期校验 +
  应用层守卫**（第二批放宽为"授权行唯一"时零 schema 变更）。
- `identity_and_credentials` 即使 oauth 卡 scope 不含仓库权限也允许存在（运行期不可用，见 §6），
  导入期不报错——否则跨名 link 第一批建不起来。
- **link 目标必须存在，但两种失败处理不同**：
  ① 文件缺席（初始化默认按 provider 同名关联）—— 无同名平台（huawei）→ **不派生、不告警**
     （纯登录渠道的正常状态，如 huawei；能力视图应为 login=true / configured=false）；
  ② 显式声明（links 指向某 repo_provider）—— 目标不存在 → **导入期 error**（配置写错必须
     响亮，不能静默跳过，否则运维以为配好了其实没生效）。
- 分层守卫放行：`repo_provider` / `oauth_provider` 是"跨层关联键"，不是托管能力位，D108-A 守卫需显式放行。

- **关系配置只管一件事：登录成功之后要不要写用户↔平台仓库关系**（2026-11-14 用户口径）：
  `channel-repo-links.json` 里写了 (渠道 c → 平台 p) ⇒ c 登录/绑定成功时建 `user_repo_platform_link(user,p)`；
  没写 ⇒ 不建行（huawei 这类纯登录渠道就是"没配置"）。
  **它不是取 token 的判据**：deps / credential_resolver 恒定 ①PAT ②OAuth，PAT 缺席就回落登录令牌，
  不看这份配置、不看 relation 档位、不看 drivers/scope/audience（能力是否真够由上游 API 在调用时回答）。
  （2026-11-14 第二批）`relation` 列已从表、ORM、Repository、管理面请求体删除，迁移 202611170011 负责 DROP COLUMN；
  `repo-credentials.json` 的 `oauth_requires_scope` 同理，只剩运维说明作用。
- **link 的权威分层（2026-11-14 裁定，与 enabled 同口径）**：配置是**首次种子**，DB 是权威。
  导入规则从"全量替换"（现 `importer.py:401 replace_for_oauth`）改为**合并、manual 优先**：
  ① 表无、配置有 → 插入（source=config/derived）；② 表有 config/derived、配置有 → 更新 relation/hash；
  ③ 表有 config/derived、配置无 → 删除（保留"文件删行能清理"）；④ 表有 **manual** → 无论配置有无都保留。
  管理面写 link 一律标 `source=manual`；因此"以数据库为准"落地为"manual 行不被导入覆盖"，
  而不是取消文件种子（换环境/新增平台仍靠文件首次建行）。
- **孤儿 link**：DB 权威后删平台卡/停用平台不会自动清 link。导入/读取时校验 `repo_provider` 是否仍在
  `grape_t_repo_platforms`，不在则告警并在能力视图标"link 目标已不存在"，**不自动删**
  （删了丢失用户关系依据）。

用户级关系（grape_t_user_repo_platform_link，2026-11-14 裁定：**只写对应关系，不写状态/来源**）：

写入三条路（每条都是"人真的做过的事"，幂等 upsert / 幂等撤销）：

1. **OAuth 登录成功**（grape_auth.py `_hook_platform_link`）：按 `grape_t_channel_repo_link(oauth_provider)`
   展开，对每个 repo_provider upsert 一行（`linked_at` 仅首次写，`revoked_at` 清空 = 复活）。
2. **OAuth 绑定成功**（`_handle_oauth_bind`，2026-11-14 补）：与登录同一条路 —— 绑定其它渠道身份后，
   该渠道 link 展开的目标平台也必须建关系。**此前只在登录路径调 hooks，绑定路径漏写**（实查缺口）。
3. **PAT 保存**（settings 凭据 / 账号页）：**不经渠道**、不查 link —— provider 即 repo_provider，
   直接 upsert 一行。理由：PAT 是平台级凭据（用户在平台后台生成，audience 天然匹配），
   若只靠渠道登录写关系，"只配 PAT 从不登录"的用户会被判成"未接入任何平台"（仓库列表空、
   token_resolver 直接 NO_REPO_CREDENTIAL），明明 token 合法却用不了。

撤销四条路（写 `revoked_at + revoked_reason`，幂等）：OAuth 解绑（按 link 展开的目标平台）、
PAT 删除（`credential_deleted`）、用户主动断开（`user_unbind`）、管理面撤销（`admin`）；
用户主动禁用某平台记 `disabled_by_user`。撤销只写关系，**不回删凭据**（凭据归凭据域）。

**闭环写入规则（2026-11-14 用户拍板，实现已按此落码）**

| 事件 | 判断 | 动作 |
|---|---|---|
| 渠道 A **登录成功**（写入 ei 之后） | 该渠道能否**驱动**平台 p：`link(A→p)` 存在（**只看配对**，档位已于 2026-11-14 退役） | 对每个可驱动的 p upsert 关系行（幂等、复活 revoked）；随后**异步**刷新项目列表（`invalidate_user_repos`，下次 `/repos/member` 回源，不必等 30 分钟 TTL） |
| 用户**绑定渠道 B'**（A 已登录） | 同登录：按 B' 的 link 展开可驱动平台 | 追加 B' 对应的关系行（A 的关系不动）；同样异步刷新项目列表 |
| **解绑渠道** | 该平台是否还有其它来源：其它活跃渠道按 link 展开 ∧ 凭据域 PAT | 无其它来源 → `revoke(oauth_unbound)`；**仍有 PAT 或其它渠道 → 保留关系**；随后异步刷新 |
| **PAT 保存** | 不经渠道、不查 link（平台级凭据） | 直接 upsert 该平台关系行；异步刷新 |
| **PAT 删除** | 该平台是否还有可用渠道 | 有 → 保留关系；无 → `revoke(credential_deleted)` |
| 用户**主动禁用平台** | — | `revoke(disabled_by_user)`，凭据不删；异步刷新 |

要点：①"支持驱动"才有仓库能力，但**同域渠道永远算支持**（`p==A`），否则用户登录自己平台却看不到"已接入待配凭据"；②（2026-11-14 第二批）**配了对就建行，档位不再参与**——`identity_only` 这个"只作登录身份"的状态已不存在，纯登录渠道=没有配对；③撤销前必须现查其它来源，**PAT 绑定场景保留关系**（这正是删掉 via_bits 后必须由读取算的部分）；④项目列表用"标记过期 + 下次回源"而不是登录热路径里同步打网络。

**关系 ≠ 可用**：关系行存在只表示接入过；能不能用由 §6 的凭据代理层现算
（PAT / 渠道令牌三关 / NO_REPO_CREDENTIAL + 原因码）。因此本表**不再需要位运算与并发保护**，
也不再出现"降级还是撤销"判断矩阵——那是把状态塞进关系表才产生的问题。

**关系表不参与凭据选择**：一个平台由多个渠道供令牌时（gitee 渠道 + huawei 跨名 link），
不在这里存"偏好渠道"；第一批按固定序解析（PAT 优先 → 渠道按 link 顺序），
真出现"用户可选"需求再单开偏好表（不预留列，对齐"显式契约而非预防性加列"口径）。

两个读取点：
- 前端"仓库平台" tab / 引导页：读 grape_t_user_repo_platform_link（关系行）+ credential_resolver 现算的可用性（revoked / active(pat) /
  pending_credential / manual-only）与 `token_available` 分开显示（"已开通但缺凭据"是真实中间态）；
  未关联平台的显示策略待拍（建议"平台目录 + 状态叠加"：显示但置灰并给"去登录/配 PAT"入口）。
- repos 列表 / 托管入口：按 link 过滤 provider 集合，再叠加用户面可见性（2026-11-15 方案甲：平台行 `enabled`；原 vcs_platforms_hidden 名单由迁移 202611170021 退役）。

## 6. 凭据驱动与取 token 链

### bot：一个对象 + 一处绑定 + 三条派生链（职责表）

bot 在 grape 里只定义两件事：**平台上的操作身份（账号）** 和 **它的 token**。其余用法都是消费或无关域。

"很乱"的实查根因：`bot_username` 同时出现在四处（平台卡 `bot_username`、`managed_repos.bot_username`、
`meta.global_bot_<p>_username`、`user_accounts.bot_usernames`），四处都像真源；`bot_name` 又作为局部变量
被四个不同域复用。收敛后职责如下（2026-11-13）：

| 名 | 是什么 | 归属维度 | 谁写 | 谁读 |
|---|---|---|---|---|
| `grape_t_bot_accounts` | 平台 bot 账号目录（username + token_enc + status） | **平台**（一平台可多账号） | 管理面写 / meta 与池迁移 | 选号、git 签名、统计 |
| `grape_t_bot_accounts.is_default` | 该平台默认账号（仓库未绑定时用） | 平台 | 管理面 | 选号兜底 |
| `managed_repos.bot_username` | 该仓库用哪个账号（**绑定**，不是身份定义） | **仓库** | 托管流程写 | 选号 |
| git 提交签名 | 本次提交的 `user.name` / `user.email` | **执行上下文（派生，不存储）** | 派生 | `git config` |
| `raw.bot_name`（IM 适配器） | 企微/Telegram 机器人显示名 | **IM 域**（与 VCS 无关，仅撞名） | IM 配置 | `strip_mention` @ 解析 |
| bot 名单（统计） | "谁是 bot"的只读视图 | 无（派生） | 无 | 效率看板 |

**三条链都从同一对象出发，不各自找源**：

1. **选号（谁去平台操作）**：仓库绑定 `managed_repos.bot_username` → 平台默认 `is_default=1` → 无账号
   → 无 bot 降级（不借 owner，Issue #345 口径）。
2. **git 签名（以谁的名义提交）**：选中账号的 username → `env BOT_USERNAME` 显式覆盖 → `host_user`
   （真实托管人，Issue #372 口径）→ 无则报错。**取消 `"grape_bot"` 硬编码兜底**——假名会让平台当成
   未知用户，正是 Issue #345「commit 身份错乱、PR 审查人失败」那类事故的成因。
3. **统计名单（谁是 bot）**：各平台行 `bot_username` ∪ 各仓库绑定的
   username —— 一处查询，gitee/gitlab 自动覆盖（现状只从 `managed_repos` + 全局配置拼，这两平台统计不出来）。

**全局 bot 的五个真实消费场景（实查，决定它必须是对象而不是散键）**

| 场景 | 位置 | 用的是 |
|---|---|---|
| 托管建仓 / 建 webhook | `repo_manager.py:772` `bot_token` + `resolve_bot_username` | token + username |
| token 链第 3 级兜底（评论等写操作） | `token_resolver.py` GlobalBotTokenHandler（R18.6.5，所有路径 A 仓库共用） | token |
| 巡检 / 过期标记 | `bot_token_health.py:63,101`（401 → `global_<p>_bot_token_status=expired`） | token + status |
| self-loop 判定 | `token_resolver.py:1312 is_self_loop_actor` | username 集合 |
| bot 名单（统计 / assignee 拒绝） | `dashboard.py:119`、`event_pipeline.py:1698`、`service.py:3330`、`tasks.py:250` | username 集合 |

所以「全局 bot」不是特殊概念，就是**平台行上的那一个 bot**（D110）：username 服务场景 1/4/5，
token 服务场景 1/2，status 服务场景 3。**降级顺序**（场景 2）：默认账号 token 不可用 → 同平台其他
`status=active` 账号 → 仓库级快照 → 无（不再直接断，也不再回落到用户 token）。

**仓库 bot 是快照，不是账号（实查 routes/repos.py:911）**

- 托管路径 A：`_create_global_bot_token = _hcfg["bot_token"]`（默认账号 token），
  `_wh_token = _create_global_bot_token or api_token`（注释原文「路径 A 用 bot token，路径 B 用用户 token」），
  同时写 `managed_repos.bot_username = resolve_bot_username(provider)`；路径 B（无全局 token）仓库级 bot 为空。
- token 落 `grape_t_repo_credentials(repo_key, cred_type='bot')`（加密），用户可在设置页覆盖。
- **缺的来源标记**：该表只有 repo_key / cred_type / value，没有「来自哪个账号」。全局轮换时无法判断某行是
  「复制来的」（该刷新）还是「用户自定义」（不该动），只能粗放全刷或不管。
- **补 `derived_from`**（值 `provider:username`，空=用户自定义）：托管复制时写来源、用户手动设置时置空；
  轮换只刷 `derived_from` 命中的行。历史行的处理见 §12。

**边界（三条，别越界）**：
- IM 的 `bot_name` 不进本模型、不并表、不改名（不同域撞名，文档标注即可）。
- `token_enc` 属于账号对象；`webhook_secret` 属于**仓库级**凭据（`grape_t_repo_credentials` cred_type='webhook'），两者不混。
- 不新增 `bot_account_id` 列：`managed_repos.bot_username` + 唯一键 (provider, username) 已能唯一定位账号
  （不预防性加列）。

**配置声明 vs 运行态实例（不重复、不合并）**

| | `repo-credentials.json` 的 `bot_username` | `grape_t_bot_accounts` 行 |
|---|---|---|
| 性质 | 配置声明：支持 bot，默认账号名是 X | 运行态：实际存在的账号（N 个，含加密 token） |
| 基数 | 1 provider = 1 条声明 | 1 provider = N 行 |
| 来源 | 配置文件（可进 git） | 管理面写入 / meta 迁移（永不进 git） |
| 被导入覆盖 | 是 | 绝对不能 |
| 作用 | 校验（supports_bot_token=true 时必须有）+ 能力视图展示期望默认名 | 取 token 的实际来源 |

**校验关系选 A（弱校验，2026-11-13 拍板）**：配置声明「默认账号应该是谁」，账号池是实例；导入只依赖
配置 + hash、**不查 DB**（幂等、可重放）；运行时若 `is_default` 账号 username ≠ 配置 `bot_username`
→ **告警不阻断**（能力视图标「声明与实例不一致」+ 启动日志 + 巡检）。
不选 B 的决定性理由：新平台接入是「先写配置、后配 token」，按 B 在账号池还空时导入必被拒——冷启动死结。
不选 C：`bot_username` 退化为纯装饰，失去校验价值。

- `grape_t_repo_credential_caps.drivers`（平台能接受的凭据种类，默认 ["pat"]）×
  `grape_t_channel_repo_link`（配对行存在）**AND** 才可用；档位列已退役，见 §20。

**凭据在驱动时现算（代理模式，2026-11-14 裁定）**

> 分层修正（2026-11-14，用户质询「platform_object 这个名称路径地址不合理」）：平台对象模型
> 落点是**领域层** `src/domains/platform/`，不是 `src/kernel/platform_object/`。该包要读凭据域、
> 登录渠道与平台卡，放在 kernel 违反 check-arch R1（kernel 不得 import domains，硬门禁，实测
> 13 条边），并因 `__init__` re-export 触发 R5 伪环。持久化（ORM/Repository）仍留
> `src/kernel/persistence/*_platform_object.py`：纯映射、无领域依赖。平台目录的运行时读口
> 收敛为 `src/domains/platform/registry.py`（表优先、卡兜底），settings 平台目录接口与用户↔
> 平台关系推导共用它。

凭据不进关系表、不挂在 managed_repos、不在绑定时指定：用户驱动仓库的那一刻，
由统一门面 `src/domains/platform/credential_resolver.py` 按
`(user_id, repo_provider, need_scope)` 解析，返回 `(token, source, reason)`：

```
① PAT（用户显式配置 = 意图最明确，优先）
   repo_credentials.get(user, repo_provider, kind="pat") 有效 → source="pat"
② 渠道令牌（PAT 缺席时的正常回落；OAuth 与 PAT 两个模式并存）
   用户有活跃渠道 c（external_identities status=active, 未过期）→ source="oauth:<channel>"
   **不查 channel-repo-links / drivers / scope / audience**：那份配置只决定"登录成功后要不要写
   用户↔平台仓库关系"，与取 token 无关；能力是否真够由上游 API 在调用时回答（403/401）。
③ 都不满足 → (空, reason) ；reason ∈ {NO_CHANNEL, TOKEN_EXPIRED}
```

要点：

- **OAuth 与 PAT 两个模式并存是硬要求**（2026-11-14 用户口径）：`token_resolver.py:438` 的 ①PAT ②OAuth
  序不变，PAT 缺席就回落登录令牌；不存在开关或关系门禁。代理层只回答"这次用的是谁的令牌"，
  **不是**禁止 OAuth 仓库操作——把能力砍掉才是改坏系统。
- **返回 source 并写审计**：必须能回答"这次操作用了谁的令牌"（`pat` / `oauth:gitee`），
  否则 scope 不同、提交身份不同、401 归因永远查不清。
- **存量四平台默认声明 `oauth_identity`**：`repo-credentials.json` 给 gitcode/gitee/github/gitlab
  补 `drivers: ["pat","oauth_identity"]` + `oauth_requires_scope`（gitee=projects issues pull_requests、
  github=repo、gitcode 同理），迁移零感知；新平台未声明即 PAT-only（自然状态，不影响存量）。
- **缓存按 epoch 失效**：解析结果按 `(user, repo_provider, need_scope)` 缓存，键里带 `config_state.epoch`
  与凭据版本；凭据变更 / 解绑 / 导入 bump epoch 即失效（epoch 机制见 §8）。
- **取 token 链保持现有逻辑（2026-11-14 用户拍板，零改动）**：`fetch_user_token` 的解析序
  ① `<p>_pat` ② OAuth 身份（`token_resolver.py:438-441`）**保持现状**，`credential_resolver` **不接管**
  取 token —— 它只用于三处：①配置保存时的前置校验与提示（drivers 声明 / scope / 受众）；
  ②管理台诊断视图（`user_platform_view` 的 available/source/reason）；③测试与审计。
  因此本批**不改**四层链（`RepoBotToken → GlobalBotToken → UserToken`）与 UserToken 内部序的调用面，
  对现网取 token 行为零影响；跨名/换域能力将来是否启用，仍按 §7 的应用 B 另立批次。
- **三层链序与回落保留（本批不动）**：`TokenResolver.strategies = [RepoBotToken, GlobalBotToken, UserToken]`
  （`token_resolver.py:1050`）是**回落关系**——**没有 bot 时，用户凭据（PAT / 个人 OAuth）照常驱动托管动作**
  （建仓/推送/评论/巡检），这是既有能力，必须保留。本代理只改 UserToken **内部**来源
  （PAT 优先 → 渠道令牌过三关），不改三层链序、不改"bot 缺失时回落用户凭据"。
- **actor 与凭据是两个维度**：凭据决定"用什么令牌调用"，actor 决定"这次操作记在谁名下"
  （git 签名 / 评论作者 / 审计 user_id），由调用方显式指定。用用户 PAT 跑托管动作时 actor 记用户；
  有 bot 且选中 bot 时 actor 记 bot。两者都不能互相冒充，也不能因为选定的 actor 不同就改变凭据链序。
- **首批现状**：应用 B（跨名换域）未实现（§12 待实弹），因此跨名渠道（huawei→codearts）第二批才可用；
  同域渠道（gitee↔gitee 等）第一批即可用。
- ①②（仓库 bot / 全局 bot）不受 link / drivers 影响——否则自动流水线（发评论/建 PR）会断。
- bot 身份解析迁移：resolve_bot_username / _card_bot_username 现读平台卡 bot_username，改为读
  grape_t_repo_credential_caps（与 supports_bot_token 同源）。**token 值仍走 meta global_bot_<p>_token，
  禁止在 caps 中出现任何 token 字段**（凭据不进配置，D108-A 已定）。

## 7. 身份与仓库的权限边界

- grape_t_oauth_channels.scope 永远只放身份范围：gitee=user_info、github=read:user user:email、
  gitcode=all、huawei=openid。
- （历史）relation=identity_and_credentials 的语义曾是「该渠道允许为 repo_provider 发起独立仓库授权」；
  2026-11-14 第二批已删除该档位，配对存在即可驱动（§20）
  （仓库 OAuth 应用 B 或 PAT），**不是复用登录 token**。
- 登录令牌从第一批起不进入仓库取 token 链，且不留环境变量开关（删除 GRAPE_OAUTH_TOKEN_FALLBACK）。

## 8. 配置与 DB（split mode + 导入规则）

| 字段类别 | 谁管 | 导入行为 |
|---|---|---|
| 定义字段（端点 / host / api / dialect / scope / drivers / repo_links…） | 配置 | 文件 hash 变更 → 覆盖；未变更 → 保留 DB |
| enabled（及运维 override） | DB / 管理面 | 配置**不声明**；首次创建默认 true；导入永不改 |
| 配置删除的行 | — | 软删 source=orphaned + enabled=false；查询必须显式过滤 |
| 配置加回的行 | 配置 | upsert 命中 orphaned → 恢复 source=config、enabled 按首次创建默认、重算 hash |
| DB 手工加的行 | DB | source=manual，导入不动 |

**link 存在性校验**：channel-repo-links.json 里显式 repo_provider 必须存在于 grape_t_repo_platforms（否则导入 error）；
缺省派生不满足时不派生（见 §5）。

**双向导入规则（grape_t_repo_credential_caps ↔ grape_t_repo_platforms）**：
- 凭据能力有、方言卡无 → **拒绝该条 + error**（"repo-credentials.json 声明了 X，但 platforms/X.json
  不存在，忽略"），不建幽灵平台行。
- 方言卡有、凭据能力无 → **默认补齐** drivers=["pat"] / supports_bot_token=false /
  supports_webhook_secret=false / bot_username=null。

其他：
- config_hash **行级**（按 provider），只算定义字段，不含 enabled / source / updated_at / hosting。
- drifted 只表示"文件 hash ≠ 表记录 hash"（文件变了没导入）；DB 手工改定义字段属越权，由"定义字段
  只允许导入路径写"的仓储层限制兜底。
- **orphaned 行恢复必须无视 hash**：配置删了又原样加回时 hash 相同，若走"hash 未变→保留 DB" 会
  永远停在 orphaned + disabled。命中 source=orphaned 时跳过 hash 判定，直接恢复 source=config +
  enabled=true + 刷新 hash。
- **枚举字段校验以 §13 为唯一规则来源**：dialect / api_auth / webhook_verify / webhook_dialect /
  issue_write_form / branch_create_form / pr_head_filter / clone_auth / issue_search 的取值越界一律
  整卡拒绝（构建期 error），配置与示例只准抄 §13 表。
- 导入入口：启动 upsert + 显式"重新导入"（dry-run diff + 审计）。
- meta 降级：meta.oauth_providers_config / meta.vcs_providers_config → 一次性导入源，migrated 标记后
  表优先，meta 仅兜底。
- **导入器按表白名单工作**：只允许写 grape_t_oauth_channels / grape_t_repo_platforms / grape_t_repo_credential_caps /
  grape_t_channel_repo_link；grape_t_bot_accounts / grape_t_user_repo_platform_link / 凭据域（PAT、grape_t_repo_credentials）不在清单中，
  导入代码触碰即测试失败（CI 守卫）。
- **grape_t_user_repo_platform_link 是纯运行时关系表**：只有关系事件（OAuth 登录/绑定、PAT 保存、撤销）与回填脚本
  写它；导入 / 重新导入 / 改配置都不写（link 变只影响派生，关系行不动）；删除配置行
  也不级联删关系（关系是用户侧事实，不是配置的投影）。
- **provider 命名规则与一致性校验（导入期）**：`^[a-z][a-z0-9_-]{1,31}$`，统一小写存储；禁用保留字
  （local / default / meta / system / admin / session / anonymous）；必须与 platforms/<p>.json 文件名、
  oauth-providers.json 的 key、managed_repos.repo_key 前缀、webhook_path 末段完全一致。任一不符 →
  导入期 error（禁止 Gitee / gitee 两行并存）。

**生效路径统一（2026-11-13 补，实查纠错）**

三类配置必须走同一套机制，否则 §8 的 drifted / orphaned 只覆盖 1/3：

- **实查现状（不是"三套"，是两套 + 一个设计）**：`oauth-providers.json` 与 `platforms/*.json` 各自
  有 **300s TTL 缓存 + 管理面主动失效**（`providers.py:140 _CACHE_TTL=300`、
  `platform_card.py:511 _CACHE_TTL=300` / `invalidate_platform_card_cache`），都不是"重启生效"，
  也不是 60s；`repo-credentials.json` 设计为导入进表。
- **第一批统一为**：运行时真源 = **DB 表**（grape_t_oauth_channels / grape_t_repo_platforms / grape_t_repo_credential_caps /
  grape_t_channel_repo_link）；文件只是**导入源**；**删除文件 TTL 热重载**（表查询缓存可保留 300s，但语义是
  "缓存表"，不是"读文件"）；文件改了不导入 → **不生效，但 drifted 可见**；启动时行级 hash 不一致则
  导入（strict 可关）；显式导入接口 + dry-run（preview：新增/更新/软删/drifted 行数）。
- **meta 彻底降级**：`meta.oauth_providers_config` / `meta.vcs_providers_config` 一次性迁移进表并置
  `migrated` 标记，之后**不再逐键覆盖**；再发现覆盖行为 → 告警（防第二真源）。
- 管理面保存路径改为**写表**（`admin/routes.py` 保存基线从卡/文件改为表）。
- **凭据进表 = 决策 A（变更，非并存，2026-11-13 拍板）**：`*_oauth_client_id/secret`、bot token 从 meta
  迁入表加密列（`grape_t_oauth_channels.client_id_enc/client_secret_enc`、`grape_t_bot_accounts.token_enc`），
  **迁移完成后 meta 键停读、值保留一个版本（回滚可用），物理删除放第二批**（标志 `oauth_secrets_migrated=1` /
  `global_bot_token_migrated=1`；理由见 §16 第 12 条：物理删后无法回滚）。
  不允许"表里加两列、meta 继续用"的并存形态——那会形成永久双源。仓库级 webhook secret 不进新表，
  继续在 `grape_t_repo_credentials(cred_type='webhook')`。
- **配置文件只在初始化 / 显式导入时有效（2026-11-13 拍板）**：建表后首次导入（空表 → 从文件导入全量）；
  此后**运行时任何路径都不得读配置文件**（能力视图、平台卡解析、渠道卡解析、取 token 全走表）；
  文件变更必须走导入。这条是"DB 是运行时权威"的操作定义——文件不是第二真源，也不是热重载源。
- **集群一致（多实例共享同一 DB）**：导入即全集群生效，不需要逐实例重启或同步；跨实例失效用
  `meta.config_state`（JSON：`epoch` int + `migrated` 标记）——每次导入 / 管理面写表 `epoch += 1`，
  各实例每次取表缓存时比对 epoch，变化即刷新（300s TTL 降为兜底，防 epoch 读取失败）。
  启动导入**只做 hash 校验**，不一致时抢导入锁（meta 层 lease / 单实例执行 + 其余跳过），
  保证多实例并发启动不重复导入、不互相覆盖。
- **epoch 语义定死（2026-11-13）**：`epoch` = **表状态版本号**，不是配置内容指纹、不是导入批次号。
  写入侧：任何一次成功的表写入（导入 / 管理面 save / 凭据轮换）在同一事务内 `epoch += 1`；
  读取侧：各实例缓存记下自己的 epoch，每次取缓存前比对 `meta.config_state.epoch`，不同即整体刷新
  （300s TTL 只是防护兜底）。
  **与 config_hash 的分工**：`config_hash` 是**行级内容指纹**——只覆盖定义字段，回答"这个 provider 的配置
  文件是否变了（drifted）"；`epoch` 是**表级版本号**——覆盖定义字段 + enabled + 凭据列，回答"缓存要不要
  重载"。两者不冗余：管理面把某平台 enabled 从 1 改 0（hash 不变、epoch 变）、仅改文件未导入（hash 变、
  epoch 不变、drifted=true），两种情形各由一条判据回答。
- **禁止跨实例不一致窗口**：epoch 未实现前不允许开多实例；第一批必须带 epoch，否则导入后最长 300s
  内不同实例能力视图不一致（会出"一台能建仓、一台报不支持"）。

**启动扫描：怎么判断"文件是否更新了"（2026-11-13 补）**

- **扫描范围**：`configs/oauth-providers.json`（每渠道一个对象）、`configs/platforms/*.json`
  （每文件一个 provider）、`configs/repo-credentials.json`（每对象一个 provider）。
- **判定依据 = 规范化内容 hash，不用 mtime**：mtime 在 git checkout / 容器镜像层 / 时钟回拨下不可靠，
  会把"没改内容"判成"更新"（触发无意义覆盖）或反过来漏判。规范化 = JSON 键排序 + 去多余空白 +
  剥离 `_` 前缀文档键（凭据本就不在文件里，无需剥离凭据）。
  **导入写入与启动比对必须共用同一个 canonical 函数**——两边算法漂移会导致永远判『更新』、每次启动全量覆盖。
- **逐 provider 四类结果 → 动作**：
  | 判定 | 动作 |
  |---|---|
  | 表空（首次初始化） | 全量导入，写 config_hash |
  | 文件 hash 与 DB 行 config_hash 不同 | 导入该行（覆盖定义字段、刷新 hash、epoch+=1） |
  | hash 相同（内容未变，哪怕 mtime 变了） | **跳过，不写 DB** |
  | DB 有、文件里没有 | `source=orphaned` + `enabled=false`（软删） |
  | DB 有、文件有、`source=manual` | 跳过（保留 DB 编辑） |
  | DB 有、文件有、`source=orphaned` | 恢复 `source=config` + `enabled=true`（**无视 hash**） |
- **单文件解析失败**：只拒该 provider（error 日志 + 健康检查暴露），不阻塞其他 provider 导入。
- **启动日志必须可审计**：`scanned files=N providers=M changed=X orphaned=Y manual_skipped=Z failed=W`。
- **运行期不轮询文件**：只在启动 / 显式导入时扫；管理面另给"检测文件变更"按钮——只报告 drifted
  与差异，不自动导入（防运维以为在热加载）。
- **strict 模式**（`GRAPE_CONFIG_IMPORT_STRICT=1`）：检测到 hash 变即**拒绝启动并提示导入命令**
  （GitOps 场景要求"改文件必须走发布流程"）；默认非 strict = 启动自动导入。

- **明确不采用"仅声明 split mode 只对 repo-credentials.json 生效"的降级方案（C）**：那会让 D109 的
  "配置是定义源、DB 是运行时权威"只在 1/3 的配置上成立。

**废弃 `configs/global-bot-token.json`（2026-11-13 拍板）**

该文件是 Issue #345 之前的一次性迁移源（`config_store.py:_GBT_PATH`，注释已写"保留只读，仅供启动迁移"），
本次彻底废弃——它不是对象模型的一部分，是历史扁平配置。全字段去向：

| 键（文件 / 管理面入参） | 去向 |
|---|---|
| `gitcode_api_token` / `github_bot_token` | `grape_t_bot_accounts.token_enc`（对应平台 is_default 账号） |
| `github_bot_username` | `grape_t_bot_accounts.username` |
| `bot_usernames`（gitcode 池数组） | `grape_t_bot_accounts` 多行，首个 `is_default=1` |
| `gitcode_webhook_token` | `grape_t_repo_credentials(cred_type='webhook')`（仓库级；不新建平台级表） |
| `gitcode/github_oauth_client_id` / `client_secret` | `grape_t_oauth_channels.client_id_enc` / `client_secret_enc` |
| `gitcode/github_oauth_redirect_uri` | `grape_t_oauth_channels.redirect_uri`（空=由 server_base_url 派生） |
| `webhook_url`（全局基址） | **不落表**：派生 = server_base_url + `grape_t_repo_platforms.webhook_path`（每平台已有路径） |
| `description`（模板说明） | 删除 |

废弃动作：①启动迁移读一次 → 迁完置 `global_bot_token_migrated=1`；②代码停读 `_GPT_PATH`（grep 守卫：
`src/` 不得再出现 `_GPT_PATH` / `load_config(`）③管理面入参名保留为别名（映射到表），前端不改；
④文件与 example 第二批删除；⑤`webhook_url` 改为按平台派生后，回调地址必须与各平台后台登记逐字节一致（§12）。

配套（配置统一第 6 条的落地）：⑥管理面改为**对象式 CRUD**（`/api/admin/channels|platforms|bot-accounts|
secrets/{provider}`），一条 save 路径写表 + `epoch += 1`；⑦旧扁平 `ConfigBody`（gitcode_api_token 等入参名）
保留为**兼容映射**，解析到对象表，**一个版本后删除**——**删除门 = 四条件 AND，全满足才删；未满足前兼容层只读、不写**：
  ① `src/` 与前端 grep 0 命中 `gitcode_api_token` / `github_bot_token` / `oauth_<p>_client_*`（读取侧清零）；
  ② 迁移标志齐备（`oauth_secrets_migrated=1` / `global_bot_token_migrated=1`）且生产库自检通过；
  ③ 已过一个完整发布周期且无回滚需求；
  ④ `configs/global-bot-token*.json` 已删除。删除动作放第二批，不悬空；⑧运行时参数（runtime_* / devstage_*）迁到系统设置域，
不进入 channel/platform/bot 对象。

## 9. 判据收敛（本批只迁两处）

- repos.py:141 _hosting_implemented_providers() → 能力视图 hosting
- repo_manager.py:1146 _KNOWN_VCS_PROVIDERS() → 能力视图 configured
- 其余枚举类调用（scene_sync.py:91、vcs/adapter.py:64、bot_token_health.py:102、chat.py:110/458、
  repo_manager.py:547）本批加 TODO(platform-capabilities)，下批迁。
- 前端不需逐点改：取源都落在后端接口（settings.py:2428 GET /vcs/platforms、repos/member、
  repos/managed），接口改读能力视图 + grape_t_user_repo_platform_link。
- **过渡期护栏（基线 + 增量棘轮）**：把当前旧判据调用点快照为基线；新增调用
  list_vcs_platforms() / _KNOWN_VCS_PROVIDERS() / _hosting_implemented_providers() 即测试失败
  （与既有 whitelist / silent-except 棘轮同款）。第二批迁移完成后一次性删除旧判据函数，不留兼容层。
- **新增平台的双判据一致性**：dry-run 除对拍现有四平台，还要断言"本次配置新增的 provider 在未迁移的
  旧判据调用点也能被识别"——避免过渡期新平台恰好踩到未迁移点。

## 10. 迁移批次

第一批（可回滚、可对拍）
0. **数据回填迁移（上线即需要，缺了就是事故）**：①凭据域 PAT 的每个 (user, provider) → 直写
   `grape_t_user_repo_platform_link` 一行（**不经渠道、不写来源位**）；②external_identities 每个
   (user, oauth_provider) 按 grape_t_channel_repo_link 展开 → 每个目标 repo_provider 写一行；
   幂等 upsert（可重复执行）；附回填前后对账。**边界如实声明**：只能从现存凭据/身份推导，历史上已删除
   的凭据无法回填（那些关系永久丢失）；对账口径只能是"回填后可见集合 ⊇ 改动前集合"，不承诺完全一致。
1. 三类配置解析 + 建 6 张表（含 grape_t_user_repo_platform_link）+ 派生能力视图
2. 导入（启动 / 显式重新导入 / dry-run / 行级 hash / 软删 / 恢复 / drifted / 双向规则 / 命名校验）
   运行时读取链同期切 DB：平台卡 / 渠道卡 / 能力视图 / 取 token 全部读表，删文件读取路径；
   带 `config_state.epoch` 做跨实例失效（§8）。
   **启动扫描**：按规范化内容 hash（非 mtime）逐 provider 判定 导入/跳过/orphaned/manual 跳过/恢复，
   日志 scanned/changed/orphaned/failed 五项；实现 strict 开关。
   **同一次提交**删除 platforms/*.json 的五个凭据相关键（token_storage_field / webhook_token_field /
   owner_identity_token_fallback / pat_create_url / bot_username），并删文件 TTL 热重载、meta 置 migrated；
   加 grep 守卫（§11）。
3. 用户级关系三写入点（OAuth 回调、PAT 保存、撤销）+ 两读取点（仓库平台页、repos 过滤）
3.5 **bot 账号对象化：所有来源键全量入库（漏一个 = 一套 token 静默失效）**
   目标表 `grape_t_bot_accounts`（provider + username + token_enc + is_default + status + token_status）。
   来源清单（实查 2026-11-13）：

   | 来源 | 键 / 位置 | 迁移动作 |
   |---|---|---|
   | 通用 meta token | `global_bot_<p>_token`（db.py:5258 派生） | → 默认账号 `token_enc` |
   | **历史别名 token** | `gitcode_api_token` → `global_bot_gitcode_token`、`github_bot_token` → `global_bot_github_token`（config_store.py:91-92 映射；db.py:5461 `_GLOBAL_BOT_FILE_FIELDS`） | 一并迁；迁后停读别名 |
   | 文件兜底 token | `configs/*.json` 的 `gitcode_api_token` / `github_bot_token`（`_read_gbt_file_field`） | 值迁表后从配置移除（凭据不进文件） |
   | 单值 username | `global_bot_github_username`、`global_bot_gitee_username` | → 账号行 `username` |
   | 多账号池 | `user_accounts.bot_usernames`（gitcode 专用，db.py:1009 实锤） | → 每名一行，首个 `is_default=1` |
   | 状态键 | `global_<p>_bot_token_status`（expired / ok） | → `token_status` |
   | **OAuth 客户端凭据** | `gitcode/github/gitee/huawei_oauth_client_id` / `client_secret`（meta + ConfigBody 8 键） | → `grape_t_oauth_channels.client_id_enc` / `client_secret_enc` |
   | redirect_uri（模板独有） | `gitcode_oauth_redirect_uri` / `github_oauth_redirect_uri`（ConfigBody 里没有此字段，只在文件模板） | → `grape_t_oauth_channels.redirect_uri`（空=派生） |
   | 全局 webhook_url | `webhook_url`（gitcode 基址） | **不落表**：派生 server_base_url + repo_platforms.webhook_path |
   | 代码字典 | `GLOBAL_BOT_USERNAME_SOURCES` / `_GLOBAL_BOT_FILE_FIELDS` | 删除 |

   迁移后：meta 写 `global_bot_token_migrated=1`（标记已存在 db.py:2212）、停读旧键、从 configs 移除 token 键；
   管理面入参名 `gitcode_api_token` / `github_bot_token` 保留为**兼容别名**（解析到该平台 is_default 账号，
   避免前端/脚本断裂）；`resolve_bot_username` 改读表（仓库级 > is_default > 池）。
3.6 **凭据进表迁移（决策 A）**：`oauth_<p>_client_id/secret`（含 oauth_providers_config 卡内凭据）→
   `grape_t_oauth_channels` 加密列；bot token/username → `grape_t_bot_accounts`；迁完置
   `oauth_secrets_migrated=1` / `global_bot_token_migrated=1`，停读 meta；自检：表内每渠道
   `client_secret_enc` 非空、每平台默认账号 token_enc 非空、meta 旧键 0 命中。
4. UserToken 内部来源改为"PAT → oauth_identity(桩) → NO_REPO_CREDENTIAL"；删除 D108-B 开关；
   三层链序不动；bot 身份解析改读 grape_t_repo_credential_caps
5. 判据收敛两处 + 对拍（gitlab 三元组必须在）
6. meta 两键降级为导入源
8. **管理面入口收口**：对象式 CRUD 上线 + 旧扁平 ConfigBody 兼容映射（一版后删）+ 运行时参数迁系统设置域；
   前端只改调用路径，不改字段语义。
7. 守卫：唯一性、drivers 合法性、凭据能力配置无 OAuth 端点/无凭据值、config_hash 幂等、orphaned
   恢复、drifted 单义、bot 层不受影响、provider 命名一致性、旧判据对拍

第二批
8. 平台枚举类调用 + 前端接口收口；repo_provider 唯一放宽为授权行唯一
9. user_repo_tokens 建表（oauth_identity 真实 token 语义此时定：引用登录 token vs 独立授权产物；
   优先独立授权；token exchange 仅在平台支持时启用）
10. 未关联平台的显示策略若第一批选了"只显示已关联"，此时补引导

第三批（可选）
11. 仓库 OAuth 应用 B 接入流程（/api/auth/callback/<p>-repo + 独立 client/secret + scope）

## 11. 验收（第一批）

- 导入幂等：连跑两次无 diff；enabled=false 后重新导入仍 false；删配置 → orphaned 且不在能力视图；
  加回 → 恢复 config + enabled=true。
- 双向规则：凭据能力有、方言卡无 → 该条被拒且 error；方言卡有、凭据能力无 → 默认补齐 pat / bot=false。
- 能力视图：gitee={login:T,configured:T,hosting:T,drivers:["pat"],supports_bot_token:T}；
  gitlab={login:F,configured:T,hosting:F}；huawei={login:T,configured:F,hosting:F}。
- 用户关系（纯对应关系表，三条写入路 + 撤销）：①OAuth 登录 gitee → 按 link 展开写关系行（无来源位）；
  OAuth **绑定**其它渠道身份同样写行（此前漏写，已列入 UC-28）；②保存 PAT → 直写该平台关系行（不经渠道），
  可用性 active(pat)、source=pat；③只配 PAT 从不登录 → 关系行存在、仓库列表可见（UC-29）；④解绑 OAuth
  但有 PAT → 关系行保留，可用性仍 active(pat)；⑤删 PAT 但有可用渠道 → 关系行保留，可用性转渠道令牌
  （scope 不足由上游 403/401 暴露，不是静默失败）；⑥全途径移除 → 写 revoked_at + reason；
  ⑦撤销后重新登录/重配 PAT → 复活（revoked_at 清空）。
- 取 token：无 PAT 且桩不可用 → NO_REPO_CREDENTIAL（不借用登录令牌）；bot 两层行为逐字节不变。
- link：同 repo_provider 两行 → 导入期 error（DB 无该唯一索引）。
- 对拍：两处新判据对每个 provider 与旧判据一致（gitlab 用例存在）。
- **回填对账**：回填后无任何用户的可见平台集合变小（部署隐藏名单不变前提下）。
- **枚举纪律**：给任意枚举字段写入 §13 之外的值 → 整卡拒绝并 error（配置与示例同规则）。
- **orphaned 恢复**：配置删行 → orphaned；原样加回（内容未变、hash 相同）→ 仍能恢复为 config + enabled=true。
- **纯登录渠道无幽灵 link**：huawei 导入后不得产生 link 行（任何模式下）；显式 links 指向不存在平台 → 导入 error。
- **过渡期双判据一致**：新增平台配置导入后，旧判据调用点与能力视图对同一 provider 判定一致。
- **生效路径统一**：改文件不导入 → 运行值不变且 drifted=true；导入后生效；meta 覆盖已 migrated 后再写入 → 告警；
  三类配置（oauth / platforms / repo-credentials）drifted 均可查。
- **启动扫描**：①内容未变但 mtime 变 → 不导入、不写 DB；②内容变 → 该 provider 导入并刷新 hash、epoch+=1；
  ③删文件 → orphaned 软删；④manual 行不被文件覆盖；⑤orphaned 行原样加回 → 恢复；⑥单文件 JSON 坏 →
  只拒该 provider、其他照常导入且日志 failed=1。
- **无双源字段**：grep 守卫——`configs/platforms/*.json` 不得含 token_storage_field / webhook_token_field /
  owner_identity_token_fallback / pat_create_url / bot_username；`configs/oauth-providers.json` 不得含 pat_create_url。
- **字段归属**：legacy_token_symbol 仍在平台卡且 deps 动态解析可用；workflows_write 仍从卡读默认值。
- **bot 账号对象化**：一平台可多账号（gitcode 池迁成多行）、`is_default` 每平台至多一行（唯一索引拒第二行）；
  仓库级绑定优先于默认账号；仓库未绑定时取 is_default；无账号 → 无 bot 降级。
- **bot 三平台对拍**：字典删除后 github/gitee/gitcode 存量用例行为一致；meta 无 `global_bot_<p>_token` 残留；
  仓库级 webhook secret 只在 `grape_t_repo_credentials`（不含 bot token）。
- **凭据对象化**：写入平台级 bot token 只落 `grape_t_bot_accounts`（加密列，库里 grep 不到明文）；
   meta 无 `global_bot_<p>_token` 残留（迁移后 migrated=1）；gitee/gitlab 能写平台级 bot token 且生效
  （现状它们无全局通道）；配置 JSON 无任何凭据值。
- **仓库 bot 快照来源**：托管路径 A 写入的行 `derived_from` = 默认账号；用户手动设置的行留空；
  全局账号轮换后前者被刷新（新 token 生效）、后者不变。
- **全局 bot 降级链**：默认账号 token 401 → 同平台其他 active 账号 → 仓库级快照 → NO_REPO_CREDENTIAL。
- **声明 vs 实例**：账号池为空时导入成功（不因 bot_username 未落地而拒）；is_default 账号改名后能力视图
  标「声明与实例不一致」且不阻断登录/托管。
- **bot 零字面量**：grep 守卫——`src/` 不得再出现 `GLOBAL_BOT_USERNAME_SOURCES` / `_GLOBAL_BOT_FILE_FIELDS`
  或平台名到 token 键的映射字面量。
- 基线红不计入：test_grape_auth 4 条 rate-limit/audit/throttle（stash 对照已实证）。

## 12. 风险与待办

- 表化与 meta 的过渡：migrated 标记前的读取链要同时支持"表空 → meta"与"表有 → 表"，迁移可重复执行。
- bot 身份解析改读 cred caps 会触碰 resolve_bot_username 的 fail-closed 语义（未知平台 ValueError），
  迁移要一并复验。
- **`webhook_url` 派生后必须逐字节一致**：废弃全局基址后，回调 = server_base_url + 平台 webhook_path；
  各平台后台登记的回调地址必须与派生结果完全一致，否则 webhook 401/404（Gitee 尤其敏感）。
- **迁移漏键 = 静默失效**：`gitcode_api_token` / `github_bot_token` 是历史别名（config_store.py:91-92 映射到
  meta），只迁 `global_bot_<p>_token` 会漏掉 gitcode/github 两套 token；迁移脚本必须按 §10 3.5 清单逐键比对，
  迁完 grep 断言旧键 0 命中（meta 与 configs 两侧）。
- **历史 `rc('bot')` 行没有 `derived_from`**：迁移只能保守推定——value 与当时全局 token 相等者视为派生
  （回填 provider:username），不等者视为用户自定义（留空）；无法判定者留空并记待人工确认，**不默认全刷**。
- **bot 池选号是第二批**：第一批只取 is_default，`status` 只做 active/disabled 二态；round_robin / sticky
  与风控退避在第二批做，别第一批就上选号策略（会引入"同一仓库两次操作用了不同 bot"的不确定性）。
- **凭据加密密钥模型（实查 2026-11-13：credential_crypto.py + security_config.py:83-94）**：
  AES-256-GCM 认证加密，密文格式 `{kid}:{nonce_b64}:{ciphertext_b64}`；密钥优先级 =
  `security.json` 的 `credential_keys`（`kid:base64key` 逗号分隔，**支持多 kid 轮换**）> 由 `JWT_SECRET`
  sha256 派生（单 kid `derived`，**不可轮换**）> dev 全零密钥（**生产禁用**）。
  D109 所有凭据列（`grape_t_oauth_channels.client_id_enc/client_secret_enc`、`grape_t_bot_accounts.token_enc`、
  `grape_t_repo_credentials.value`）共用这一套；轮换 = 新增 kid 后新写入走新 kid、旧密文按 kid 解密，
  不需重写历史行。**不引 Vault / KMS**：单机部署、无 KMS 依赖，kid 轮换已覆盖需求；将来接 KMS 只替换
  `credential_crypto._get_current_key()` 实现即可，表结构不变。
  运维要求：生产必须配 `credential_keys`（而非依赖 JWT_SECRET 派生），否则轮换能力缺失；dev 零密钥仅限本地测试。
- **bot 池目前只有 gitcode**：`user_accounts.bot_usernames` 注释实锤是 GitCode 专用列表
  （`mgmt/user/db.py:1009` 「user_accounts.bot_usernames 是 GitCode 的 bot 列表」）。通用 bot 池不是第一批范围，
  第一批只把 `bot_identity_mode=pool` 的来源指向现有池；其他平台先 `single` / `none`。

- user_repo_tokens 语义未定（第二批开工前必须拍"引用 vs 独立授权产物"）。
- oauth_identity 桩必须显式不可用并有守卫，避免"看起来支持、实际拿空 token"的静默失败。
- 华为 GOpen 返回结构、仓库 OAuth 应用 B 端点契约仍待实弹。
- **多实例导入竞争**：并发启动时必须靠导入锁 + 幂等 upsert 兜住；锁失效（lease 过期）时以行级 hash 判定收敛，
  不允许"后启动者全量覆盖先启动者的 DB 编辑"。
- **运维体验变化**：删文件热重载后，"改配置文件即生效"不再成立——必须导入；靠 drifted 提示 + 管理面一键导入兜底，
  发布说明里要写。
- **回填只能覆盖现存凭据**：历史上已删除的凭据/身份无法回填关系，对账口径为 ⊇ 而非 =（见 §10）。
- **关系表只存关系**：表内没有来源位/状态列，可用性由 credential_resolver 现算；排查"为什么不能用"看 reason 码
  （NO_PAT / NO_CHANNEL / LINK_MISSING / DRIVER_NOT_DECLARED / LINK_IDENTITY_ONLY / AUDIENCE_MISMATCH / TOKEN_EXPIRED），
  不要期待从关系表读出"用什么凭据"。
- **用户级关系不覆盖组织/租户维度**：grape_t_user_repo_platform_link 的键是 (user_id, repo_provider)；若将来出现
  "组织统一绑定、成员继承"，另立模型，不要在这张表上加 org 列的临时方案。
- **非标准协议渠道需要代码注册**：标准 oauth2 渠道加配置即可；SAML / 非标准扫码（如微信）必须在
  src/kernel/auth_sdk/channel_strategy.py 注册策略（register_channel_strategy），否则 build_channel_strategy
  fail-closed（400 渠道配置无效）。

## 13. 字段值必须取真实枚举（防臆造，实查 2026-11-13）

平台卡 Builder 对这些字段做枚举校验，越界整卡拒绝。任何文档/示例里的值都必须来自下表：

| 字段 | 合法值（platform_card.py） |
|---|---|
| dialect | `github` \| `gitlab`（**没有 gitee**；Gitee 走 github 方言） |
| api_auth | `query_token` \| `bearer_header` \| `token_header` \| `private_token`（Gitee=query_token） |
| webhook_verify | `hmac` \| `token_compare`（Gitee=token_compare） |
| webhook_dialect | `github_web` \| `gitee_v5` |
| issue_write_form | `repo_path` \| `owner_param`（Gitee=owner_param） |
| branch_create_form | `form` \| `refs_sha`（Gitee=form） |
| pr_head_filter | `bare_ref` \| `owner_ref`（Gitee=bare_ref） |
| clone_auth | `x_access_token` \| `oauth2` \| `basic_password`（Gitee=basic_password） |
| issue_search | bool（True/False），不是字符串 |
| token_storage_field / webhook_token_field | 显式 `""` = 无物理列（gitee/gitlab 口径，走 rc 单表）；键缺席 = 派生列名 |

历史教训（2026-11-13 外部评审示例，逐条认领为无效值）：`dialect:"gitee"`（合法只有 github/gitlab）、
`webhook_verify:"hmac_sha256"`（只有 hmac/token_compare）、`issue_write_form:"json"`（只有
repo_path/owner_param）、`branch_create_form:"json"`（只有 form/refs_sha）、`clone_auth:"token_in_url"`
（只有 x_access_token/oauth2/basic_password）、`issue_search:"native"`（bool）、
`token_storage_field:"access_token"`（gitee 现为显式 ""，非空会改变凭据存储口径）。
上述任一写入配置都会被构建器整卡拒绝。文档、示例、外部评审结论只准抄本表。

## 14. 扩展 checklist（新增平台 / 渠道）

新增仓库平台（按序）：
1. `configs/platforms/<p>.json` —— 只放方言字段（枚举见 §13）；展示字段用 `_presentation_fields` 标注。
2. 平台卡（`configs/platforms/<p>.json`）—— 可选声明 pat_create_url / token_storage_field /
   webhook_token_field / bot_username / owner_identity_token_fallback；凭据种类与 scope 不再声明
   （configs/repo-credentials.json 已删除，2026-11-14）。
3. `configs/oauth-providers.json` —— 若该平台有登录：加渠道卡（不含 repo_links）；同批在
   `configs/channel-repo-links.json` 加 `{oauth_provider, repo_provider}`（无档位；缺省按同名派生）
   （目标必须存在于 grape_t_repo_platforms）。
4. `src/domains/runtime/vcs/providers/<p>.py` —— 实现方言 + `HOSTING_FULL_FLOW = True`；
   非标准协议另需在 `src/kernel/auth_sdk/channel_strategy.py` 注册策略。
5. 导入 dry-run —— 校验能力视图 {login, configured, hosting, supports_bot_token} 正确。
6. 对拍 —— 旧判据与新判据对同一 provider 一致（过渡期必做）。

新增纯登录渠道：oauth-providers.json 加一条（标准 oauth2）+ 不派生 link（目标平台不存在即跳过）；
非标准协议按第 4 步注册策略。

改完配置：重启（自动扫描导入）或跑显式导入；strict 模式下必须走导入命令，否则启动被拒。

**不需要改**：TokenResolver 三层链序（含 bot 缺失回落用户凭据）/ grape_t_channel_repo_link 解析 / grape_t_user_repo_platform_link 写入点 / 能力视图派生逻辑。
这是本方案相对现状的最大收益——扩展点收敛到"配置 + 方言模块"两处。

## 15. 场景用例（第一批，UC-D109-01 ~ 45；28–34 关系/凭据裁定，35–39 管理面接口契约，40–44 关系即判据）

每个用例都对应一条落实现或一条守卫；"涉及"列是它必须触碰的表（导入器不得触碰运行态表）。

| ID | 场景 | 前置 | 步骤 | 预期 | 涉及 | 测试 |
|---|---|---|---|---|---|---|
| 01 | 首次部署导入 | 空库、configs 三类文件齐 | 启动 | 四张配置派生表全量写入，每行 config_hash 非空，epoch=1 | 四配置表 | test_d109_import_split_mode |
| 02 | 新增仓库平台 | 已有 gitee/github | 加 platforms/bitbucket.json + repo-credentials 条目 + 重启 | 自动导入；能力视图 configured=true、hosting 按模块、drivers 默认 ["pat"] | 四配置表 | test_d109_platform_object_model |
| 03 | 新增纯登录渠道 | 无该平台仓库卡 | oauth-providers.json 加 wechat + 重启 | channels 有行；**不产生 link 行**；能力视图 login=true/configured=false | channels | test_d109_pure_login_no_ghost_link |
| 04 | 修改平台配置 | 已有行 | 改 host 后重启 | 该行定义字段更新、hash 刷新、epoch+=1；其他行不动 | platforms | test_d109_import_split_mode |
| 05 | 只改文件不导入 | epoch 就绪 | 改文件 + 不重启不导入 | 运行值不变、drifted=true；管理面"检测文件变更"可见 | 无写入 | test_d109_drifted_visibility |
| 06 | 删除配置行 | 已有行 | 删 platforms/x.json + 重启 | source=orphaned + enabled=false；能力视图 configured=false；**不级联删 bot/关系** | platforms | test_d109_orphaned_soft_delete |
| 07 | 配置原样加回 | 已在 06 状态 | 原文件恢复 + 重启 | 恢复 source=config、enabled=true（**无视 hash**） | platforms | test_d109_orphaned_restore |
| 08 | 多实例并发启动 | 两实例同库 | 同时启动 | 一个抢到导入锁执行导入，另一个跳过并等 epoch 刷新；无重复导入、无互相覆盖 | config_state | test_d109_import_lock |
| 09 | 管理面保存 | 服务在跑 | PUT 改某平台 name | 写表 + epoch+=1；其他实例 ≤1 次读周期内刷新 | 四配置表 | test_d109_admin_save_epoch |
| 10 | OAuth 登录成功 | 用户首次登录 gitee | 回调 | 按 link 展开 upsert `user_repo_platform_link`（**不写 via_bits**）；可用性由代理层现算（无凭据→NO_PAT） | link、user_repo_platform_link | test_d109_user_repo_platform_link |
| 11 | 保存 PAT | 已在 10 状态 | 设置页保存 PAT | 直写/复活关系行（不经渠道）；可用性 active(pat)，source=pat | user_repo_platform_link、凭据域 | test_d109_pat_only_user_visible |
| 12 | 解绑 OAuth 但有 PAT | 在 11 状态 | 解绑 gitee | 关系行保留（PAT 来源仍在）；可用性仍 active(pat)；不写 revoked_at | user_repo_platform_link | test_d109_user_repo_platform_link |
| 13 | 删 PAT 但有 OAuth | 在 10+11 状态 | 删 PAT | 关系行保留；可用性改由渠道令牌现算（配对存在 → oauth:<c>，无配对 → 无凭据来源；档位列已退役） | user_repo_platform_link、凭据域 | test_d109_credential_resolver |
| 14 | 全途径移除 | 仅有单一来源 | 删最后一个（PAT 或解绑最后一个渠道） | 写 `revoked_at + revoked_reason`；可用性 NO_PAT/NO_CHANNEL | user_repo_platform_link | test_d109_user_repo_platform_link |
| 15 | 撤销后重新获得 | 在 14 状态 | 重新登录或重配 PAT | 复活：revoked_at=null、置位、last_linked_at 更新 | user_platform_link | test_d109_user_platform_link |
| 16 | 显式 link 指向不存在平台 | 无 codearts 卡 | channel-repo-links.json 里 huawei→codearts | 导入期 error（响亮），不静默跳过 | link | test_d109_link_target_exists |
| 17 | 托管建仓选号 | 平台有多账号 | 建仓 | 选中顺序：仓库绑定 > is_default > 无（降级，不借 owner） | bot_accounts | test_d109_bot_selection |
| 18 | bot 轮换 | 仓库快照 derived_from 已写 | 换默认账号 token | 只刷新 derived_from 命中的行；用户自定义行不变 | bot_accounts、repo_credentials | test_d109_bot_rotation |
| 19 | bot 巡检 401 | 默认账号 expired | 巡检 | token_status=expired；取 token 降级到其他 active 账号 | bot_accounts | test_d109_bot_health |
| 20 | 取 token 链 | 无 PAT | 仓库操作 | PAT → oauth_identity(桩，不可用) → NO_REPO_CREDENTIAL；不借登录 token | 无写入 | test_d109_user_token_source |
| 21 | 能力视图三元组 | 四平台 | GET 视图 | gitee TTT+drivers / gitlab FTF / huawei TFF；bot_accounts 摘要无 token | 派生视图 | test_d109_platform_object_model |
| 22 | 枚举非法值 | — | 平台卡写 dialect=gitee | 整卡拒绝 + error（配置与示例同规则） | 无写入 | test_d109_enum_reject |
| 23 | 凭据迁移 | meta 有 client/bot 键 | 跑迁移 | 表加密列非空、meta 停读（值保留一版）、旧键读取 0 命中 | oauth_channels、bot_accounts | test_d109_credential_migration |
| 24 | 回填对账 | 存量 PAT/ei | 跑回填 | 每用户可见平台集合 ⊇ 改动前；差异清单为空 | user_platform_link | test_d109_backfill |
| 25 | strict 模式 | strict=1 | 改文件后启动 | 拒绝启动并提示导入命令 | 无写入 | test_d109_import_strict |
| 26 | 单文件坏 | 某卡 JSON 语法错 | 启动 | 只拒该 provider（failed=1），其他照常导入 | 四配置表 | test_d109_import_resilience |
| 27 | 过渡期双判据 | 已迁两处判据 | 新增平台后查旧判据点 | 旧判据与能力视图对同一 provider 判定一致 | 无写入 | test_d109_dual_predicate |
| 28 | 绑定其它渠道身份（华为登录 + 绑定 Gitee） | 用户已用华为登录 | 用户中心点"绑定 Gitee"→ 回调 | `external_identities` 增加 gitee 行；**同时**按 link 展开写 `user_repo_platform_link(user,gitee)`（此前只在登录路径调 hooks，绑定漏写——本用例锁住） | ei、user_repo_platform_link | test_d109_bind_path_writes_link |
| 29 | 只配 PAT 从不登录 | 该用户无任何渠道身份 | 设置页保存 GitCode PAT | 直写 `user_repo_platform_link(user,gitcode)`（**不查 link、不经渠道**）；仓库列表出现 gitcode；可用性判定 active(pat) | user_repo_platform_link、repo_credentials | test_d109_pat_only_user_visible |
| 30 | 多凭据共存 → PAT 优先 | 同平台既有 PAT 又有可用渠道令牌 | 驱动仓库 | 返回 PAT，`source="pat"`；操作日志/审计记录来源；不会静默改用渠道令牌 | 无写入 | test_d109_credential_resolver_priority |
| 31 | 渠道 scope 不足 | gitee 渠道 scope=user_info，无 PAT | 建 issue | **不由解析层判死**：关系=identity_and_credentials 即取渠道令牌，scope 不足由上游返回 403（PAT 为兜底来源） | 无写入 | test_scope_does_not_gate |
| 32 | 渠道令牌可用（同域，无 PAT） | gitee 渠道 scope 含 projects/issues、link relation=identity_and_credentials、caps 声明 oauth_identity | 建 issue | 返回 gitee OAuth 令牌，`source="oauth:gitee"`；**证明 OAuth 仓库能力保留**，未被切成 PAT-only | 无写入 | test_d109_credential_resolver_oauth |
| 33 | link 的 manual 权威 | 管理面在仓库侧配置了 link（source=manual） | 重启导入（文件里没有该条） | manual 行保留、不被删除；反之 config/derived 行在文件删行后被清理 | channel_repo_link | test_d109_link_manual_precedence |
| 34 | 用户主动禁用平台 | 有有效关系行 | 用户在仓库列表禁用该平台 | 写 `revoked_at + revoked_reason=disabled_by_user`；仓库列表/能力视图过滤；凭据不删 | user_repo_platform_link | test_d109_user_disable_platform |
| 35 | 五聚合集合 GET | 已有四平台四渠道 | GET `/api/admin/oauth-channels`、`/repo-platforms`、`/channel-repo-links`、`/user-repo-platform-links?user_id=`、`/bot-accounts` | 每聚合只回自己的字段；**凭据只回布尔**（无 token/secret 值或前缀）；**路径 = 领域对象名，平铺在 `/api/admin` 下** | 五表 | test_d109_admin_api_contract |
| 36 | 单条 GET 与 404 | 不存在 provider | GET `/repo-platforms/nope`、`/oauth-channels/nope` | 404 + 明确 code（不返回空对象）；存在的回自身字段 + `enabled` + `capability` | 表 | test_d109_admin_object_api |
| 37 | 关系 manual 写 | 管理面在仓库侧配置 | PUT `/channel-repo-links/{ch}/{repo}` | 写入 `source=manual`；随后导入（文件无该条）**不覆盖/不删除**；同 pair 幂等 | channel_repo_link | test_d109_link_manual_precedence |
| 38 | 派生视图 | 有用户与关系 | GET `/views/capabilities`、`/views/user-platforms?user_id=` | 视图路径独立于对象域；`available` 与实际取 token 口径一致（关系有效 ∧ 有凭据来源），scope/audience 只作 `diagnosis` | 表 | test_d109_platform_object_model |
| 39 | 契约守卫 | — | AST 扫 `platform_object_routes.py` + `platform_objects/*.py` | **禁止虚构父容器**（路径/模块字符串不得出现 `platform-objects`）；**禁止整卡 PUT**（PUT 只允许 `…/credentials`、`…/enabled`、或关系二元路径）；路径名必须是领域对象名 | 无写入 | test_d109_admin_api_contract |
| 40 | OAuth 登录后有配对 → 自动建用户平台关系 | `channel-repo-links.json` 配了 (gitee → gitee) | 用户 OAuth 登录/绑定成功 | 按配对展开 upsert `grape_t_user_repo_platform_link`；**不看 relation 档位、不看 drivers、不看 scope** | user_repo_platform_link | test_d109_user_repo_platform_link::test_link_pair_is_the_only_criterion |
| 41 | 没配对 → 不建行 | 渠道同域存在但文件里没有该配对 | 用户 OAuth 登录 | 不建关系行（纯登录渠道即"没配置"）；同域不隐式派生 | 无写入 | test_no_link_pair_writes_nothing |
| 42 | 无 PAT → 用渠道令牌驱动 | 用户仅 OAuth | 驱动仓库 | 取渠道令牌 `source=oauth:<c>`；**不看 channel-repo-links**；scope 是否够由上游 403/401 回答 | 无写入 | test_channel_token_when_pat_missing / test_scope_does_not_gate |
| 43 | PAT 优先（两模式并存） | 同平台既有 PAT 又有 OAuth 绑定 | 驱动仓库 | 返回 PAT（`source=pat`）；无 PAT 时才用登录令牌——不存在开关或关系门禁 | 无写入 | test_oauth_token_fallback_gate::test_pat_always_wins |
| 44 | 关系文件缺席/存在 | 文件不存在；或存在但 `links` 为空 | 启动导入 | 缺席 → 按 provider 同名默认配对（`source=derived`）；存在 → 以文件为准（空 = 登录不再写任何用户平台关系）；跨名配对同样生效 | channel_repo_link | test_missing_link_file_falls_back_to_same_name / test_link_file_present_is_authoritative |
| 45 | 清空全部 PAT 且无 OAuth | 用户只配过 PAT，关系行存在 | 设置页把 PAT 清空 | `_cascade_pat_clear` 连带 `hooks.on_pat_deleted` → 关系行写 revoked_at；`/auth/platform-links` 变 has_any=false；前端 `_restoreNavAfterLogin` 以关系优先（丢弃残留 currentRepo）→ 引导页 | user_repo_platform_link | test_clear_pat_cascade_syncs_relation |

## 16. 系统影响与伤害评估（实查，2026-11-13）

| # | 影响面 | 实查依据 | 风险 | 伤害等级 | 缓解 |
|---|---|---|---|---|---|
| 1 | **启动时序冲突** | db_setup.py:6230「异步执行…不阻塞启动」：DB 初始化是 daemon thread | 服务先起、表未建/未导入 → 读表得空 → 登录与托管全挂 | **高（唯一可能全站不可用）** | ①导入放进 lifespan 同步段且**先只做 hash 扫描**（无变更不写）；②读路径保留"表空/异常 → 回落文件+meta"一个版本，导入完成标志置位后才纯表；③导入失败 fail-open（保留旧表 + 健康检查告警），仅表空时 fail-closed |
| 2 | **读路径规模** | get_platform_card 64 处、get_provider 19 处、list_vcs_platforms 25 处 | 一次读表异常 = 登录 + 托管 + 判据全挂 | **高** | 进程内缓存 + epoch 校验（每次一次轻量 meta 读）；读失败 stale-while-revalidate + 告警，不抛空 |
| 3 | **凭据迁移不可逆** | meta 键为唯一现网凭据源 | 迁移失败/漏键 → 平台 token 全丢，用户须重配 | **高** | 迁移前自动备份 DB；幂等可重跑；迁完校验（每渠道 client_secret_enc 非空、默认账号 token 可解密且探活 200）；**meta 值不物理删除，只停读并保留一个版本**（回滚可用），第二批再删 |
| 4 | **用户平台可见性收紧** | user_platform_link 新表 + 按 link 过滤 | 回填漏 → 用户平台入口"消失"，且难自查 | **高** | 生产库 dry-run 生成对账（⊇ 报告）；影子读一个周期（旧逻辑仍返回，新逻辑只记录差异）；有差异不切流量 |
| 5 | **多实例一致性** | epoch 依赖 meta 单行 | 未实现 epoch 就开多实例 → 实例间最长 300s 不一致 | 中 | 第一批单实例上线；epoch 未实现前禁止多实例（写进 §8） |
| 6 | **配置删除语义** | orphaned 软删 = enabled=false | 删卡即该平台所有仓库操作失败（用户视角"平台没了"） | 中 | 软删 + 管理面显式提示受影响仓库数 + 保留 30 天可恢复 |
| 7 | **判据收敛** | repos.py:141 / repo_manager:1146 替换 | 新旧判定不一致 → 某些平台能/不能托管翻转 | 中 | 对拍测试（gitlab 三元组必须在）+ 过渡期基线+增量棘轮 |
| 8 | **性能** | 能力视图多表 join；平台列表被频繁 GET | 每次请求打 DB | 中 | epoch 缓存 + provider/user_id 索引；视图结果可内存缓存（按 epoch 失效） |
| 9 | **安全（凭据进表）** | 加密密钥 = security.credential_keys > JWT_SECRET 派生 > dev 零密钥 | 生产未配 credential_keys → 用不可轮换的派生密钥；dev 零密钥上线 = 明文等价 | 中 | 启动自检：使用 dev 零密钥时 error 日志 + 健康检查不健康；生产强制 credential_keys |
| 10 | **管理面/前端契约** | ConfigBody 扁平 → 对象式 | 前端不改则字段回显缺失 | 低 | 兼容别名一版（删除门四条件）；脱敏字段（*_present/_tail）保持同形 |
| 11 | **测试基线** | 全量基线本就有 ~2000 失败 | 新增守卫撞红，难判断是新增还是存量 | 低 | 失败集合 diff + 逐文件隔离双侧复跑（既有纪律） |
| 12 | **回滚能力** | 决策 A 原写"meta 键废弃" | 物理删 meta 后无法回滚到旧实现 | **高（已修正）** | **改为：停读 + 保留旧值一个版本，物理删除放第二批**；并保留"回滚 = 代码回退 + 停用新表读路径"的操作手册 |

**结论**：方案对系统的**结构影响是可控的**（新表、新服务、读路径改造都在既有 D109 边界内），真正的伤害集中在 **4 个高等级点**：①启动时序（异步初始化 vs 读表）；②读路径 64+19+25 处调用的容错；③凭据迁移不可逆；④用户平台可见性收紧。这四点都有明确的缓解动作，其中 ①和③必须作为第一批的**上线前置条件**（不加缓解不上线），④必须走"影子读一个周期"再切流量。第 12 点是对已拍决策 A 的修正：**保留旧值一版**，否则回滚即丢凭据。

## 17. 建表 DDL 的 MySQL 方言适配（2026-11-14 实锤修正）

**曾经的错误形态（本分支实施期，已修）**：建表函数用 `c.executescript(PLATFORM_OBJECT_DDL)`（sqlite3 专有
API，不经过 `mysql_compat` 翻译层），为绕开 MySQL 1064 又加了 `if get_db_backend()=="mysql": log.error(); return`
—— 结果是**迁移 0ms 照样盖章（202611170009 写进 grape_t_schema_migrations），六表一张没建**，
且版本号盖死后永不再跑（正是 `db_setup.py:3546` 明文禁止的"吞错盖章"形态）。MySQL 部署上 D109 会静默
降级成"读路径全回落 + 管理面 5 个对象接口全 500"。

**实锤（MySQL 8.0.46）**：把六表 DDL 逐条过 `translate_sql()` 后直接执行 → **6/6 rc=0**；
唯一不兼容的是 bot 默认位的**部分唯一索引**（`WHERE is_default = 1`，MySQL 不支持）→ 1064。

**落地规则**（`platform_object_ddl.py` + `db_setup._fn_create_platform_object_tables`）：

| 项 | 规则 |
|---|---|
| 执行方式 | **逐条 `cursor.execute()`**（走 `mysql_compat` 翻译），与既有建表迁移一致（db_setup.py:4788/5021）；**禁用 `executescript`** |
| 部分唯一索引 | MySQL 用 STORED 生成列 `is_default_uniq AS (CASE WHEN is_default=1 THEN 1 ELSE NULL END)` + 复合唯一键 `(provider, is_default_uniq)`（NULL 不参与唯一，语义等价）；SQLite 保留原部分索引 |
| 补列 `created_at/derived_from` | SQLite `TEXT NOT NULL DEFAULT ''`；MySQL `VARCHAR(32) NOT NULL DEFAULT ''`（TEXT 不能带 DEFAULT） |
| 幂等 | MySQL 分支先查 `information_schema.columns/statistics` 再 ALTER，不依赖 `IF NOT EXISTS` |
| 迁移纪律 | 建表 fn **不得吞错**（抛错=不盖章、下次重试）；MySQL 分支不得"跳过建表但返回成功" |

**实测（8097 + `grape_d109_mysql`，先删盖章模拟未执行）**：`MySQL 方言已适配` → 盖章 164ms →
六表齐 → 生成列/唯一键齐 → 启动导入 `channel=4 platform=4 caps=4 changed=12 epoch 0->1` →
`PUT /api/admin/repo-platforms/gitlab/enabled` 200、`GET bot-accounts` 200。

**守卫**：`tests/mgmt/test_d109_ddl_dialect.py`（禁止 executescript / 禁止 MySQL 跳过 / DDL 必须切成
6 表 + 1 部分索引 / 生成列写法），反证：改回 executescript → 1 failed。

## 18. 模型关系与需求映射（2026-11-14；用来验证"模型是否真的反映需求"）

### 18.1 需求（用户原文口径，模型必须逐条接住）

| # | 需求 | 说明 |
|---|---|---|
| N1 | 用华为账号登录 grape | 登录渠道可以是任意一个已配置渠道 |
| N2 | 登录后**绑定 gitee 的 OAuth 账号**，用 gitee 的令牌驱动 gitee 仓库 | 身份多渠道 + 平台级凭据 |
| N3 | 从没登录过某平台，只配了该平台 PAT，也要能驱动该平台仓库 | 凭据可独立于登录存在 |
| N4 | **没有 bot 时，托管动作（建仓/推送/评论/巡检）用我配置的 PAT 跑** | 三层链回落，不能被切成 bot-only |
| N5 | 管理台能看到"这个仓库平台接受哪些登录渠道"，关系只读、状态可解释 | 配置入口在仓库侧；不可用要给原因 |
| N6 | 我主动禁用某平台后它不再出现在可用列表，但凭据不删 | 关系撤销 ≠ 凭据删除 |

### 18.2 实体与基数（唯一键 + 谁写）

| 实体 | 表 / 落点 | 唯一键 | 基数 | 谁写 |
|---|---|---|---|---|
| 用户 | `grape_t_user_accounts` | id | — | 注册 / 登录 |
| 登录渠道（配置） | `grape_t_oauth_channels` | provider | 1 渠道 1 行 | 导入（`configs/oauth-providers.json`），凭据列管理面写 |
| **登录身份（绑定）** | `external_identities` | (provider, external_id) | 用户 N 渠道；渠道 N 用户 | OAuth 登录 / 绑定回调 |
| 仓库平台（配置） | `grape_t_repo_platforms` | provider | 1 平台 1 行 | 导入（`configs/platforms/*.json`），enabled 管理面写 |
| 凭据能力（配置） | `grape_t_repo_credential_caps` | provider | 1:1 依附平台 | 导入（`configs/repo-credentials.json`） |
| **渠道 ↔ 平台授权** | `grape_t_channel_repo_link` | (oauth_provider, repo_provider) | M:N | 导入做种子 + 管理面标 `source=manual`（DB 权威） |
| **用户 ↔ 仓库平台（关系）** | `grape_t_user_repo_platform_link` | (user_id, repo_provider) | M:N | 登录 / 绑定 / PAT 保存 / 撤销（**只写关系**） |
| **用户 PAT** | `user_credentials(kind=pat)` | (user_id, provider, kind) | 用户 N 平台 | 设置页 / 账号页 |
| bot（D110） | `grape_t_repo_platforms.bot_*` 列 | provider | 平台 1 个 bot；仓库级覆盖在 rc('bot') | 管理面 / 迁移 / 巡检 |
| 仓库级凭据 | `grape_t_repo_credentials` | (repo_key, cred_type) | 仓库 × 类型 | 托管流程 / 迁移 |

### 18.3 关系图（ASCII）

```
        ┌──────────────┐  N:M (绑定, 含令牌)   ┌──────────────────┐
        │     user     │──── external_identities ────│ oauth_channel │
        └──────┬───────┘                        └────────┬─────────┘
               │ N:M (准入关系, 只存关系+时间)              │ N:M (允许哪种关系)
               │  user_repo_platform_link                 │  channel_repo_link
               │                                          │  （无档位：配对即授权）
               │                                          │           | identity_and_credentials
               ▼                                          ▼
        ┌──────────────────────────────────────────────────────────┐
        │                    repo_platform (仓库平台)               │
        │  配置: platforms/<p>.json   │  能力: caps.drivers 声明     │
        └───────┬───────────────────────────────────┬──────────────┘
                │ 1:N                               │ 1:N
        ┌───────▼─────────┐              ┌──────────▼──────────────┐
        │ bot_accounts    │              │ user PAT (user_credentials│
        │ (平台账号目录)   │              │  kind=pat, 用户×平台)     │
        └───────┬─────────┘              └──────────┬──────────────┘
                │ 仓库级快照 (derived_from)          │
                ▼                                   │
        ┌──────────────────┐                        │
        │ repo_credentials │◀───────────────────────┘  三者共同喂给：
        │ (仓库级 bot/webhook)│         credential_resolver（驱动时现算）
        └──────────────────┘
```

### 18.4 派生规则（一律不落表）

- **用户在平台 p 可用** ⟺ `user_repo_platform_link(user,p)` 有效 ∧（`user_credentials(kind=pat, p)` 存在 ∨ ∃活跃渠道 c：`channel_repo_link(c→p)` 存在）
- **凭据诊断（不接管取 token）**：`credential_resolver(user, repo_provider, need_scope)` →
  `PAT → 渠道令牌（link 配对存在 ∧ audience 匹配）→ 空 + reason`（drivers/scope/档位均不参与，见 §20）
- **能力视图**：login / configured / hosting / supports_bot_token / …（实时派生，见 §4）
- **状态文案**：active(pat) / via-oauth / pending_credential / revoked(reason) / LINK_IDENTITY_ONLY —— 全部由上面的规则算出

### 18.5 需求 → 模型映射（逐条校验，缺一即为模型没接住需求）

| 需求 | 模型要素 | 校验用例 | 现状是否成立 |
|---|---|---|---|
| N1 华为登录 | `external_identities(huawei)` + `oauth_channels(huawei)` | UC-10 | ✅ 成立 |
| N2 登录后绑定 gitee 并用其令牌驱动 gitee 仓库 | `ei(gitee)`（登录令牌）+ `user_repo_platform_link(user,gitee)`（登录建行） | UC-40 / UC-42 | ✅ 成立：取 token ①PAT ②OAuth（两种模式并存），关系配置只决定建不建行；scope 由上游 403 回答 |
| N3 只配 PAT 也能用 | `user_credentials(kind=pat)` + 关系行直写（不经渠道） | UC-29 | ✅ 成立（hooks 已按此实现） |
| N4 无 bot 时托管用用户 PAT | 三层链 `RepoBotToken → GlobalBotToken → UserToken` 回落 | UC-20 | ✅ 成立（本批不改链序） |
| N5 仓库侧配置/只读展示/可解释 | `link.source=manual`（DB 权威）+ `credential_resolver` 的 reason | UC-30 / UC-31 / UC-33 | ⚠️ 后端 write 接口未开（规划）；reason 码已定义 |
| N6 禁用平台不删凭据 | 关系 `revoked_at + disabled_by_user`；凭据表不动 | UC-34 | ✅ 模型成立（`on_platform_disabled` 已实现，管理面按钮待接） |

### 18.6 反向检查（每个模型要素都要有需求支撑，否则属过度建模）

| 要素 | 服务哪条需求 | 若无则 |
|---|---|---|
| `channel_repo_link.relation` 两值 | N2/N5：回答"这条渠道的令牌能不能用于仓库" | 只能靠隐式猜，401 无法归因 |
| `caps.drivers` | N2/N3：回答"这个平台接受哪种凭据" | 无法前置校验，配置了不可用也不告知 |
| `caps.oauth_requires_scope` | N2/N5：scope 不足要提前提示 | 用户只能撞 401 |
| `user_repo_platform_link` | N3/N6：回答"我接入过哪些平台"、"我禁用了哪个" | 无准入清单，只能靠凭据反推；禁用无处表达 |
| `user_credentials(kind=pat)` | N3/N4 | 没有平台级凭据来源 |
| `bot_accounts` | N4 另一分支（有 bot 时用 bot 身份） | 平台账号只能散在 meta |
| ~~`via_bits`~~（已删） | 无需求支撑（"来源位"没有任何界面/判定消费它，且无渠道标识） | 删掉后来源改为现算，见 §6 |

### 18.7 明确不建模（防误读）

- 关系表**不存** status / 来源渠道 / 可用性布尔 / 偏好渠道（全部派生；偏好真需要时单开表）
- **不建**"平台级 webhook secret"表（仓库级 `repo_credentials('webhook')` 已权威）
- **不建**组织/租户维度（第二批；届时另立模型，不在本表加 org 列）
- **不建**"渠道→平台"的反向冗余表（link 键对称，视角由 UI 决定）

### 18.8 本轮实查的现状差距（不掩盖）

1. `configs/oauth-providers.json` 实际 scope：`gitcode=all` / `github=read:user user:email` / `gitee=user_info` / `huawei=openid`
   → N2 在 gitee/github 上**当前不成立**（scope 不含仓库权限）；只有 gitcode 的 `all` 现成可用。
   这不是模型缺陷，是平台授权范围事实；模型不再预判 scope（2026-11-14 用户裁定「只要关系就行了，为什么要关系 scope」），由上游 403 暴露，配置里把 scope 写对即可。
2. 仓库 OAuth 应用 B（跨名换域，如 huawei→codearts）未实现（§12 待实弹）→ 跨名渠道第二批可用。
3. `configs/repo-credentials.json` 本轮新建（此前缺失，caps 全靠 DDL 默认 `["pat"]`）；
   四平台声明见该文件（gitcode/gitee/github 含 `oauth_identity`，gitlab 仅 `pat`）。
4. `supports_bot_token` 未在文件中声明（默认 false），与"gitcode/github 有 bot token"现状不一致，
   属第二批补齐项（不影响 N4 的 PAT 回落）。

## 19. 管理面接口契约（按领域对象建模，2026-11-14 用户拍板）

原则：**路径 = 聚合根**。一个领域对象一个集合；item 标识只用该对象的领域键；派生视图单独域，
不混进对象域；**没有整卡 PUT**（定义字段真源是配置，写入=改文件 + 导入，运行时只写 enabled / 凭据）。

| 聚合（领域对象） | 集合 / item 路径（前缀 `/api/admin`，**路径=领域对象名，无父容器**） | 方法 | 说明 |
|---|---|---|---|
| `OAuthChannel` | `/api/admin/oauth-channels`<br>`/oauth-channels/{channel}` | GET | 定义字段 + `credential_present{client_id,client_secret}`（布尔）+ `enabled` + 展开后的 `repo_links`（读 grape_t_channel_repo_link，不来自文件）+ `capability` |
| | `/oauth-channels/{channel}/credentials` | PUT | 写 `client_id_enc/client_secret_enc`（留空=不改；未知键拒绝） |
| | `/oauth-channels/{channel}/enabled` | PUT | 只改 `enabled`（DB 权威） |
| `RepoPlatform` | `/api/admin/repo-platforms`<br>`/repo-platforms/{provider}` | GET | 方言字段 + 能力位 + `enabled` + `credential_caps` + `bot_accounts` 摘要 + `capability` |
| | `/repo-platforms/{provider}/enabled` | PUT | 只改 `enabled` |
| `ChannelRepoLink` | `/api/admin/channel-repo-links?oauth_provider=&repo_provider=`<br>`/channel-repo-links/{oauth_provider}/{repo_provider}` | GET / PUT / DELETE | PUT 写 `source=manual`（DB 权威；请求体 `relation` 字段仅为兼容，不参与判定）；DELETE 仅删 manual 行 |
| `UserRepoPlatformLink` | `/api/admin/user-repo-platform-links?user_id=`<br>`/user-repo-platform-links/{user_id}/{repo_provider}` | GET / DELETE | 管理面只读为主；DELETE = 用户禁用（`disabled_by_user`），不动凭据 |
| `BotAccount` | `/api/admin/bot-accounts?provider=`<br>`/bot-accounts/{provider}/{username}` | GET / PUT / DELETE | 默认账号由 body `is_default` 决定（不进路径）；响应永不含 token |
| 派生视图（非实体） | `/api/admin/capabilities`<br>`/user-platforms?user_id=` | GET | 明确标 view；`available` 与真实取 token 口径一致，scope/audience 只进 `diagnosis` |

错误口径：

- 404：item 不存在（返回 `{"detail":{"code":"NOT_FOUND","resource":"repo-platforms","id":"..."}}`，不回空对象）
- 409：唯一约束冲突（如同一 (oauth, repo) 已存在 manual 行再 PUT）
- 422：字段校验失败（未知键 / 枚举越界 / 凭据全空）
- 503：表未就绪（`is_table_backed()` 为假）时，读接口回落文件+meta 并标 `"source":"fallback"`；写接口返回 503（不静默写假）

**实现落点（一个聚合一个文件，平铺）**：`src/domains/mgmt/admin/`
（`oauth_channel_routes.py` / `repo_platform_routes.py` / `credential_caps_routes.py` /
`channel_repo_link_routes.py` / `user_repo_platform_link_routes.py` / `bot_account_routes.py` /
`views_routes.py` + 共享请求模型 `api_common.py`），在 `admin/routes.py` 里逐个 include。

> 2026-11-14 用户口径：**不存在「平台对象」这个领域对象**，所以既不该有 `/platform-objects`
> 父容器路径，也不该有 `admin/platform_objects/` 这种父容器目录和 `platform_object_routes.py`
> 汇总壳（该壳已删除）。目录名只允许是领域对象名本身，与路径同构。
理由：六个聚合 + 视图塞一个文件会超 600 行（与 AdminView.vue 单文件同型问题），
按对象分文件后每个模块只依赖自己的仓储与 Body 模型。

**禁止项（守卫）**：路径里不得出现 `channels` / `platforms` 裸名；不得出现整卡 PUT
（`PUT /oauth-channels/{c}`、`PUT /repo-platforms/{p}`）；派生视图不得挂进对象集合路径。

## 20. 配对即授权收口与管理面收敛（2026-11-14 第二批）

第一批把「档位不参与判定」落成读路径口径，但列、枚举、请求体、管理面 UI 都还在，形成「代码里没有、
数据里还有、界面上还让人选」的三重不一致。第二批按用户裁定「'仅登录身份'这个逻辑就不要了」把关系层
彻底收成**配对即授权**，同时把管理面从「一页长卡片」改成列表→详情，补上 bot 账号与逐平台 token 健康。

### 20.1 relation 档位退役（列级）

删除点（一条链上全清，不留"仅兼容"的半成品）：

| 层 | 对象 | 处理 |
|---|---|---|
| DDL | `platform_object_ddl.py` / `d109-schema.sql` | `grape_t_channel_repo_link.relation` 列删除 |
| ORM | `models_platform_object.ChannelRepoLinkModel` | 列映射删除 |
| Repository | `ChannelRepoLinkRepo.set_manual(...)` / 导入写入 | 签名去掉 relation 参数；导入只写 (oauth, repo, source, hash) |
| 领域 | `domain.Relation` 枚举 / `ChannelRepoLink.relation` / `allows_credentials()` | 全部删除（零调用方，属死门禁） |
| 管理面 API | `ChannelRepoLinkBody` / `PUT /channel-repo-links/{c}/{p}` | 请求体删除（空体即可），响应不再回 relation |
| 管理面 UI | 登录渠道详情「可驱动的仓库平台」 | 只显示配对与来源（配置/手工），不再显示档位、不再让选 |
| 迁移 | `202611170011 drop-channel-link-relation-column` | 幂等判列 + `_destructive_ddl`（cleanup 语义 + min_code_version 门槛） |

判定口径（唯一真源）：**表里有 (c → p) 这一行 ⇔ c 的令牌可驱动 p**。纯登录渠道（huawei 等）= 没有任何配对；
删配对即不再驱动。`_targets()` 只做 `list_by_oauth()`，不做任何档位/驱动位/scope 推断。

### 20.2 清 PAT 与引导门（用户实弹后二次收口）

第一次修的是后端级联（`_cascade_pat_clear` → `hooks.on_pat_deleted`），HTTP 层已验；用户复测仍落项目页，
二次排查出三处前端口径缺口，全部补上：

1. **`_bootRestore()` 绕过关系门**：启动时先按 URL 深链/sessionStorage `currentRepo` 恢复仓库并 `nav('dash')`，
   关系判定只在后面的 `_restoreNavAfterLogin()` 里——而那时已经回不去了。
   修法：`_bootRestore` 第一步判 `_hasVcsBinding()`；无关系 → 清残留 `currentRepo` → `goToRepos()`，不再走仓库恢复。
2. **`_hasVcsBinding()` 在快照缺失时回落 identities**：Vue 线没有 `_appVcsCatalog`，回落分支把任意登录身份
   （含纯登录渠道）当成"已接入仓库平台"。修法：只认 `/auth/profile` 带入的关系快照（source=table/identities 都按
   `has_any`），快照缺失一律按无关系处理，终裁权交给后端 `/repos/member` 的 `_no_vcs_binding`。
3. **账号页清 PAT 后不刷新关系快照**：`clearPat()` 只刷新凭据卡片。修法：清成功后 `refreshRepoLinks()` 重取
   `/auth/platform-links`，`has_any=false` 时一并清 `currentRepo` 并 `goToRepos()`，让用户当场落到引导页。

配套锁：`tests/js/test_boot_gate_no_relation.js`（源码级：门禁位置/无 identities 回落/无关系落引导页）+
`test_app_deeplink.js` 7k/7l（行为级：关系空与快照缺失都不复用残留仓库）。

### 20.3 管理面收敛（列表 → 详情）

用户裁定：两个对象页进去应是列表，点行才进详情。落地：

- **列表**：名称 / 标识 / 状态 /（平台）bot token 健康 + bot 账号数 /（渠道）可驱动的仓库平台（纯配对）。
- **详情**：域说明条 + 启用开关 + 只读定义字段（带 `field.*` 说明）+ 各自的写面：
  渠道 → client_id/client_secret 保存 + 渠道↔平台配对增删（manual 行可删）；
  平台 → `bot_username` / PAT 创建地址 / 健康失败摘要 + bot 账号增删与 token 轮换（`PUT|DELETE /bot-accounts/{p}/{u}`，token 空=不改）。
- 写口复用对象模型既有端点，不新增后端写面；「凭据」只在各自详情页写，杜绝双入口互相覆盖。

### 20.4 平台凭据页只留部署级

D13 的「平台凭据页收敛」在 Vue 管理台里没有真正落地（GitCode/GitHub 两张卡 6 个凭据字段 + bot 名单 +
整块 token 探活仍在）。第二批收口：凭据页只保留 **webhook_url（部署级）+ 运行参数（runtime_task_timeout /
devstage_*）**；保存体不再提交任何平台/渠道字段；随之退役的辅助代码（`phToken`/`oauthPh`/`verifyToken`/
`loadTokenHealth`/`health*`/`provLabel`/`pMeta`/`ERROR_TYPE_CLS`）一并删除。探活改由仓库平台列表逐行展示。

### 20.5 场景用例（第二批，UC-46 ~ UC-50）

| # | 场景 | 前置 | 动作 | 预期 | 用例 |
|---|---|---|---|---|---|
| 46 | 档位列彻底退役 | 已升级库 | `PRAGMA table_info(grape_t_channel_repo_link)` / `information_schema` | 无 relation 列；`ChannelRepoLinkRepo.set_manual(s,c,p)` 两参可用；`domain.Relation` / `allows_credentials` 符号不存在 | test_d109_admin_api_contract::test_manual_link_put_and_delete + 迁移 202611170011 |
| 47 | 配对即授权 | 只有 (gitee→gitee)、(huawei→codearts) 两行 | `hooks.on_oauth_login(u,'huawei')` | 建 codearts 行；不读任何档位；删配对后不再建行 | test_d109_user_repo_platform_link::test_link_pair_is_the_only_criterion |
| 48 | 清空全部 PAT 且无 OAuth → 引导页（前端全链） | 用户 only-PAT、sessionStorage 有 currentRepo、URL 是深链 | 账号页清 PAT → 刷新首页 | 后端 has_any=false；`_bootRestore` 不复用残留仓库、清 currentRepo、落引导页 | tests/js/test_boot_gate_no_relation.js + test_app_deeplink.js 7k/7l |
| 49 | 快照缺失不猜绑定 | profile 未返回/接口失败 | 启动 | `_hasVcsBinding()` 返回 false（不按 bindings 猜）→ 引导页；后端 `_no_vcs_binding` 终裁 | test_app_deeplink.js 7l + test_boot_gate_no_relation.js |
| 50 | 管理面列表/详情与 bot 管理 | 平台/渠道在册 | 列表点行 → 详情 → bot 增/轮换/删、渠道配对增删 | 行可进详情、写口 200、列表刷新后状态同步；渠道页无档位控件 | test_d109_admin_api_contract + 实弹（8096 PUT/DELETE bot、PUT 空体配对） |

### 20.6 同域配对隐式化（2026-11-14 用户裁定「同域配对是错误配置」）

第一批把「配对即授权」落到极致，连渠道自己的平台也要配一条 `(c → c)`。用户质询：这条配置既不能表达新能力
（渠道自己就是那个平台），又会在界面上读出「GitCode 可驱动 GitCode」这种废话；一旦 UI 不再展示，它就变成
「配了却看不到、也删不掉」的历史垃圾数据。定稿：

| 维度 | 口径 |
|---|---|
| 语义 | **同域不是授权**：渠道自己即代码托管平台时，天然可接入自己（登录即建本平台关系）；**跨名才是显式授权** |
| 运行时 | `hooks._targets()` = 跨名配对 ∪ 渠道自身（当渠道 provider 在 `grape_t_repo_platforms` 在册） |
| 配置 | `configs/channel-repo-links.json` 只写跨名对；示例文件清空（同域无需声明） |
| 导入 | 文件缺席 → 不再派生同域行（`_default_links` 返回空）；显式 `(c→c)` → 告警并忽略（不写库） |
| 管理面 | `PUT /channel-repo-links/{c}/{p}` 当 `c == p` → 422 `SAME_DOMAIN_PAIR`（附原因文案） |
| 数据 | 迁移 `202611170012 drop-same-domain-channel-links` 删除存量同域行（纯数据清理，backfill 语义） |
| UI | 关系照常展示：一枚「同域：本平台」标签 + 说明「同域天然可接入（无需配对）；跨名需显式配对」。若库里仍有同域行 → 灰字提示为上一版遗留、可删 |

效果：登录建关系的行为不变（同域用户依然在登录后拿到本平台关系），但配置面不再要求管理员为「自己接自己」
造一行数据；线上存量 3 条同域行已清，4 个渠道 `repo_links` 均为空。

### 20.7 术语与文案口径（2026-11-14 评审意见采纳）

外部评审指出两处用词歧义，采纳其「文案建议」、不采纳其「对象改名建议」：

- **代码托管平台**（替代「仓库平台」）：「仓库」在中文语境易读成仓储；本系统该对象承载 Issue/PR/CI 等能力，
  叫「代码托管平台」准确（评审首选）。
- **可接入的代码托管平台**（替代「可驱动的…」）：「驱动」在技术语境强指硬件/底层控制，作定语歧义大；
  本系统实际语义是「该登录渠道的凭据可用于该平台的仓库操作」→「可接入」。
- **登录渠道**（保留，不改成「登录方式/第三方登录配置」）：对象模型里就是这个名字
  （`grape_t_oauth_channels` / `/admin/oauth-channels` / `OAuthChannel` / `channel-repo-links.json`），
  改名会让 UI 与模型/文档出现第二套叫法；且渠道类型含 oauth2/oidc/saml，叫「第三方」会漏。
  只把说明写准：「查看当前已支持的登录渠道，并可更新各渠道配置」。
- 文案层替换范围：zh/en 语言包 + `i18n.js` 内联包 + 管理面接口 summary/错误文案；**代码标识符不动**
  （`repo_platform` / `channel` 保持原名，避免无收益的重命名扩散）。

### 20.8 前端版本位纪律（本批实锤）

管理台静态资源带版本位，语言包另有独立缓存键，两者都要在改文案/改 bundle 时 bump：

| 资源 | 位置 | 本批终值 |
|---|---|---|
| 管理台 view bundle | `admin.html` 的 `admin-view.js?v=` | 29 |
| 语言包缓存键 | `admin.html` 的 `I18N.init('<ver>')`（i18n.js 拉 `locales/<lang>/*.json?v=<ver>`） | `20261114-10` |
| 应用壳 view bundle | `index.html` 的 `account-view.js?v=` / `app.js?v=` | 61 / 273 |

漏 bump 的现象：新词条在库里但界面空白/回显原始 key（浏览器命中旧语言包），或旧 bundle 不认新路由（`view==='lwl'`）。

### 20.9 场景用例（第三批，UC-51 ~ UC-54）

| # | 场景 | 前置 | 动作 | 预期 | 用例 |
|---|---|---|---|---|---|
| 51 | 同域无需配对即建关系 | 平台在册、关系表无任何行 | `hooks.on_oauth_login(u,'gitcode')` | 建 `(u, gitcode)` 行（隐式同域）；纯登录渠道（huawei）无配对不建行 | test_d109_user_repo_platform_link::test_self_domain_is_implicit_but_cross_name_needs_a_pair |
| 52 | 显式同域配对被拒 | 管理面 | `PUT /channel-repo-links/gitee/gitee` | 422 `SAME_DOMAIN_PAIR`，附原因；库中不产生行 | test_d109_admin_api_contract（同域分支）+ 实弹 |
| 53 | 配置/导入不造同域行 | 无 `channel-repo-links.json` | 启动导入 | 不派生同域行，告警「同域渠道天然可接入本平台」；显式 `(c→c)` 告警忽略 | test_d109_import_split_mode::test_missing_link_file_means_no_cross_pairs |
| 54 | 管理台文案与版本位 | admin 打开渠道/平台页 | 中/英切换、强刷 | 「代码托管平台 / 可接入」术语中英一致；新词条不空白（语言包版本位已 bump） | test_admin_field_docs（i18n 键集与说明）+ 实弹 v=29 |

### 20.10 语言切换的渲染时序（第四批，用户实弹出"中英互换"）

症状：中文界面出现英文（`gitcode · can access: same domain (itself)`、`enabled`），
英文界面出现中文（渠道名「华为云」）。三条独立成因，前两条在 i18n 运行时，第三条在数据展示：

| # | 成因 | 机制 | 修法 |
|---|---|---|---|
| 1 | 内联包载体不刷新 | `i18n.js` 的**脚本 URL 版本位**长期停在 `20261114-3`，而语言包分两处：`locales/<lang>/*.json`（异步真源）与 `js/i18n.js` 内联包（首帧兜底）。改文案只 bump 了 `I18N.init('<ver>')`，浏览器仍用旧内联包 → 首帧与切换后的文本错位 | 改文案必须同批 bump `i18n.js?v=`（全部壳页：admin / index / register / forgot）；`auth.js?v=` 也要跟（它内部是 `I18N.init`） |
| 2 | 切换后视图不重渲染 | `setLang()` 在 `loadPacks()` **之前**派发 `grape:langchange`，之后再没派发；Vue 视图收到事件时新包未到，按旧包渲染 → 「点 EN 页面不变 / 点中文还是英文」，重挂载（切菜单再回来）才恢复 | 状态提交 + `applyI18nToDOM()` 之后再派发一次 `grape:langchange`；视图用 `langVer` 计数重渲染 |
| 3 | 显示名取自数据 | 渠道名来自 `grape_t_oauth_channels.name`（本库为中文），英文界面直接印 → 漏中文 | 显示名走 `admin.provider_name.<key>`（zh/en 各一份），缺词条才回退库值；渠道页/平台页/白名单三处统一 |

版本位真值表（第四批终值）：

| 资源 | 位置 | 终值 |
|---|---|---|
| 管理台 view bundle | `admin.html` `admin-view.js?v=` | 35 |
| 语言包缓存键 | `admin.html` `I18N.init('<ver>')` + `i18n.js?v=` | `20261114-16` |
| 应用壳 | `index.html` `app.js?v=` / `account-view.js?v=` / `i18n.js?v=` | 273 / 61 / `20261114-16` |
| 注册/找回密码 | `register.html` / `forgot.html` `i18n.js?v=` / `auth.js?v=` | `20261114-16` / 8 |

守卫：`tests/mgmt/test_admin_i18n_locale_consistency.py`（7 条：en 无中文 / 模块键集一致 /
内联包无孤儿键 / 内联包两段语言方向 / 术语不回潮 / 渠道名覆盖 / 壳页版本位下限）。

### 20.11 bot 摘要语义（第四批，用户质询「bot 账号数 0 是什么意思」）

列表那一列原本印 `(p.bot_accounts || []).length`。两个问题：

1. **印计数不解释后果**：`0` 不是缺陷，它意味着托管动作回落到用户 PAT——
   `src/domains/platform/credential_resolver.py` 模块注释写明：bot 不在用户凭据层，
   托管链是 `RepoBotToken → GlobalBotToken → UserToken`。列表该说的是这句话，不是数字。
2. **把失效行算成可用**：`BotAccountRepo.list_by_provider` 只按
   `is_default desc, username` 排序，**不筛 status**；bot 被吊销后行仍在表里，
   界面照样计入 → 会读出「有 bot，托管走 bot」的错误结论。

定稿（`src/api/frontend/app/src/lib/bot-summary.js`，纯函数、可 node 直测）：

| 数据形态 | 列表文案（zh / en） |
|---|---|
| 无 bot 行 | `bot 未配置（回落用户 PAT）` / `no bot (falls back to your PAT)` |
| 有行但无 `status='active'` | `bot 全部不可用（回落用户 PAT）` / `all bots inactive (falls back to your PAT)` |
| 有 active 行 | `bot 账号数 N · 默认:<username>`（无默认只给计数） |

`status` 缺省按 `active`（与后端口径一致）；`bot_accounts` 缺失 / 平台为 null 按未配置处理。
同时把详情页的 bot 区改成与「登录渠道 → 凭据」同一种行式写法（标签左、输入右、
底部一条说明 + 主按钮），消除"配置与展示分开"的割裂。

### 20.12 场景用例（第四批，UC-55 ~ UC-59）

| # | 场景 | 前置 | 动作 | 预期 | 用例 |
|---|---|---|---|---|---|
| 55 | 语言切换即时生效（双向） | 管理台已登录、停在登录渠道页 | 点 EN、再点「中」，不刷新、不切页 | 行文本从 `可接入：同域自身 启用` ↔ `can access: same domain (itself) enabled` 双向即时切换；渠道名 `华为云` ↔ `Huawei Cloud` | 实弹（CDP 抓 DOM 文本）+ 10 条守卫 |
| 56 | 内联包与异步包不漂移 | 改了语言包文案 | 只 bump `I18N.init`、不 bump `i18n.js?v=` | 守卫红：壳页版本位回退被 `test_shell_script_cache_versions_bumped` 拦下 | test_admin_i18n_locale_consistency::test_shell_script_cache_versions_bumped |
| 57 | 新增渠道必须给显示名 | 加一条渠道到 `configs/oauth-providers.json` | 不补 `provider_name` | 守卫红：`test_every_configured_provider_has_a_localised_name` 报缺键；英文界面不会漏出中文名 | 同上 |
| 58 | 无 bot → 回落用户 PAT | 平台 0 条 bot 行 | 打开平台列表 | 行内写`bot 未配置（回落用户 PAT）`，不出现 `0` 这个裸计数 | node tests/js/test_bot_summary.js（6 检查） |
| 59 | 失效 bot 不计入可用 | 一行 `status='revoked'` + 一行 `status='active'` | 打开平台列表 | 计数为 1（不是 2）；若全部 revoked 则显示「全部不可用（回落用户 PAT）」 | node tests/js/test_bot_summary.js |

### 20.13 托管平台判定（第五批，用户实弹「gitee 仓库托管失败」）

现象：gitee 仓库明明显示自己是管理员，点托管返回 404。日志实锤：

```
host_repo GitHub: permission pre-check upstream failed:
GitHub API GET https://api.github.com/repos/yang-aiwu-new/livefire-tpl-verify-1010 → 404
POST /api/repos/host 404 Not Found
```

三家各自的缺陷叠在一起：

| 层 | 缺陷 | 修法 |
|---|---|---|
| 前端（Vue lobby） | `provider` 只认 `github:` / `gitcode:` 两个前缀，gitee 仓得到空串 → `/repos/host` 不带 provider | 前缀即平台：`providerFromRepoKey()`（`app/src/lib/repo-provider.js`），任何在册平台前缀都取得到 |
| 后端（兜底） | provider 为空 → `_provider_from_hint` 无前缀线索（body 里 full_name 不含前缀）→ `_provider_from_token` 按「token 可用性顺序」挑到 github | 新增 `_provider_from_user_repos()`：用本账号 `user_repos` 缓存的 repo_key 前缀反查，**唯一命中**才采用（同名仓库跨平台仍不猜） |
| 数据（归属解析） | Gitee v5 用 `namespace` 表示归属，`owner.login` 缺失 → owner 空 → 「owner == 我的 login ⇒ admin」判定全部落空 | generic provider 解析顺序：`owner.login` → `namespace.path/name` → `full_name` 第一段；`full_name` 缺失时用 `owner/name` 反拼 |

结果（实弹）：`POST /api/repos/host?provider=gitee` 与不带 provider 两种调用都返回
`{"message":"仓库托管成功","webhook_id":2148107}`，`managed_repos` 落 `gitee:yang-aiwu-new/livefire-tpl-verify-1010`。

同批清掉的二元平台枚举（旧壳 `shell/js/app.js`）：

- `stripRepoPrefix` 只剥 `gitcode|github|gitlab` → gitee 前缀残留，owner 解析成 `gitee:xxx`；
  改为结构化剥离（`key:` 且后面是 `owner/repo` 就剥），不再枚举平台。
- `initSidebarRepos` 用 `startsWith('github:')` 二分，gitee 仓库被塞进「GitCode」分组；
  改为按 repo_key 前缀分组 + `platformLabel()` 取名。
- 四处 `{gitcode:'GitCode', github:'GitHub'}[k]` 名表 → `platformLabelOf()`。

### 20.14 迁移"空转盖章"的通用收口（第五批，线上 MySQL 实锤）

`202611170010`（凭据能力表并入平台）在线上库**盖了章但什么也没做**：5 个列一个没建、
`grape_t_repo_credential_caps` 还在 → ORM 每次读平台表都抛 `Unknown column 'pat_create_url'`，
管理面静默退回文件/meta 兜底（`source=file`），bot_username 等字段读不到库值。

定稿纪律：**DDL 语句返回成功 ≠ 库真的变了**，凡 DDL 迁移都必须在末尾按"最终状态"复检，
不满足就 `raise`（执行器约定：抛错=不盖章，下次启动重试）。

| 迁移 | 复检内容 |
|---|---|
| `202611170010` | 5 列都在平台表 + caps 表已不存在 |
| `202611170011` | `grape_t_channel_repo_link.relation` 已删除 |

线上修复步骤（已执行并复核）：补 5 列 → 从 caps 表回填 4 个平台 → DROP caps 表 →
重启后 `GET /api/admin/repo-platforms` 返回 `source=table`，`journalctl` 里
`repo_platforms table read failed` 计数为 0。

### 20.15 场景用例（第五批，UC-60 ~ UC-63）

| # | 场景 | 前置 | 动作 | 预期 | 用例 |
|---|---|---|---|---|---|
| 60 | gitee 仓库托管不再走错平台 | 账号有 gitee 仓库缓存、Gitee 上有该仓库管理权 | 点托管（前端传 `?provider=gitee`） | 200 托管成功，webhook 建在 gitee；日志不再出现 `host_repo GitHub` | 实弹（webhook_id=2148107）|
| 61 | 无 provider 时用缓存反查 | 前端漏传 provider、body.full_name 无前缀 | `POST /api/repos/host` | 命中 `user_repos` 唯一前缀 → 用 gitee 凭据，200 | test_d109_repo_provider_routing::test_provider_resolved_from_user_repos_cache |
| 62 | 同名仓库跨平台不猜 | 缓存里 `gitee:o/r` 与 `gitcode:o/r` 同时存在 | 同上 | 返回空 → 交显式 provider/人工，不误判 | test_d109_repo_provider_routing::test_provider_cache_hint_requires_unique_hit |
| 63 | Gitee namespace 补归属 | 仓库对象只有 `namespace.path` | 拉仓库列表 | owner 取到 namespace.path；owner==我 → is_admin=True；`full_name` 缺失时反拼 | test_d109_repo_provider_routing::test_generic_fetch_fills_owner_from_gitee_namespace |
| 64 | 迁移空转必须报错 | 迁移 DDL 未生效（列没建） | 跑 `202611170010` | 抛 `复检未通过`/`no such column`，不盖章 | test_d109_merge_caps_into_platforms::test_merge_caps_into_platforms_raises_when_ddl_noop |

### 20.16 二元平台枚举的清零（第五批深度检查，2026-11-14）

触发：修完 gitee 托管后按同一形状全仓检索「非 github 即 gitcode」，共 13 处，症状各异。
逐处清单（均已修 + 上棘轮守卫）：

| 位置 | 原写法 | 症状 |
|---|---|---|
| 旧壳 boot/rerender/nav 三处 | `bindings.length === 0` 当作「未接入」 | 纯 PAT 账号被送进引导页，明明有仓库却看不到 |
| `app.js repoTitle` | `_isGithub ? github.com : gitcode.com` + 内联两枚 SVG | gitee 仓标题链接指向 gitcode.com、显示 GitCode 图标 |
| `app.js _vcsUrl` | 同上三元 | Issue/仓库外链错平台 |
| `app.js` 登录/绑定 toast + 两处名表 | `{gitcode,github}` 字面映射 | gitee/gitlab 显示原始 key |
| `app.js` OAuth 按钮品牌色 | 覆盖表只有两家 | gitee/gitlab 落到后端原始色 |
| `TemplatesView` 创建页平台清单 | `localStorage.user.bindings`（OAuth 身份行） | 纯 PAT 平台不出现、下拉整体隐藏（本次用户提问的根因） |
| `TemplatesView` 平台名/域名/派生标签 | 二元三目 | gitee 模板显示 GitCode、链接错域名 |
| `TemplatesView`/`SettingsView`/`AutopilotView` | `'gitcode:' + fullName` 硬拼前缀 | gitee/gitlab 建仓、迁移、分支加载全部打错平台 |
| `DashboardView` Issue 链接 | 域名三元 | gitee 仓 Issue 外链错 |
| `OnboardingView` 已接入徽标 | 只认 bindings | 纯 PAT 账号显示「未绑定」 |
| `RegisterView` 平台名 | 二元三目 | 三方绑定页显示 key |
| 后端 `webhook.py` 两处预检 | `provider not in ("gitcode","github")` → unsupported/unknown | gitee/gitlab 仓永远拿不到可达性/Issue 存在性判定 |
| 后端 `event_pipeline` bot 名 | github 特例 + 其余取全平台并集 [0] | gitee 仓可能拿到别家平台 bot 名（提交身份/审查人错乱） |
| 后端 insight `dashboard.py` 出参 | `to_repo_key(_base)` 丢入参前缀 | gitee 仓仪表盘被标成 gitcode（缓存键/链接错） |

口径（与 §20.13 同源）：
- **名字/域名/品牌色/图标** → `platformLabel` / `platformHost` / `platformColor` / `platformIcon`（`shell/js/platform-icons.js` 单点真源）。
- **前缀 → 平台** → `providerFromRepoKey()`；**平台 → 前缀** → `repoKeyOf()`（无 provider 线索时留空，不猜）。
- **「有没有接入」** → 用户↔平台关系快照（`_hasVcsBinding()` / `/auth/platform-links`），不是 OAuth 身份行。
- **后端能力判定** → `get_platform_card(provider) is not None`，不是平台白名单。

棘轮守卫：`tests/mgmt/test_no_binary_platform_dispatch.py`（7 类禁止写法 + 目录真源白名单）。
回归口径：相关套件失败集**改动前后逐条相同**（61 baseline / 0 新增）——按「失败集 diff」而非绝对数判定。

| 65 | 二元平台枚举棘轮 | 有人再写 `? 'github:' : 'gitcode:'` / `{gitcode:..github:..}` / `bindings.length===0` 当接入判据 / 后端 `not in ("gitcode","github")` | 跑守卫测试 | `test_no_binary_platform_dispatch` 红并给出 file:line | tests/mgmt/test_no_binary_platform_dispatch.py |

### 20.17 顺序、集合与写入范围的真源（第六批，2026-11-14 深度复查）

§20.16 清的是「判定二分」，本批清的是**顺序与集合**——同样是写死就会让新平台/新渠道
静默排错、写错的东西。逐项定稿：

| 对象 | 原实现 | 现实现（真源） | 症状/风险 | 守卫 |
|---|---|---|---|---|
| 登录渠道**集合** | 本就数据驱动 | `configs/oauth-providers.json` + meta 逐键覆盖 → `grape_t_oauth_channels` 表（表就绪=唯一真源） | 无（本次确认，非缺陷） | `test_n5_platform_config` / `test_admin_vcs_platforms` |
| 登录渠道**展示序** | `_CATALOG_ORDER = {"gitcode":0,"github":1,"gitee":2,"gitlab":3,"hwcloud":4}` 字面表（D102-B 拍板序） | `_catalog_order_index()`：配置文件 `_ui_order`（可选）→ 配置文件键顺序 → 其余按名排尾 | 新增渠道必须改代码才能得到预期排序；与"集合数据驱动"口径不一致 | `test_d109_platform_order_data_driven` |
| 托管**兜底顺序** | `_DEFAULT_TOKEN_PROVIDER_ORDER = ("github","gitcode")` | `_default_token_provider_order()` = `sorted(hosting_providers())`（实装托管全流程的卡） | 二元枚举（棘轮红）+ 无线索请求默认打 github：gitee 仓库被当 GitHub 托管的根因之一 | 同上 + `test_platform_whitelist_ratchet` |
| bot 名历史池 | `if provider == "gitcode":` 读 `global_bot_usernames` | `_HISTORICAL_BOT_POOL_KEYS` 数据映射（与既有 TOKEN/USERNAME 历史键同构） | 平台分支（棘轮红） | `test_platform_whitelist_ratchet` |
| webhook 预检兜底 | 卡片 import 失败时回落 `not in ("gitcode","github")` | 失败即 `unsupported`/`unknown`，不回落白名单 | 卡片不可用时反而放行了 gitee/gitlab 的判定 | `test_no_binary_platform_dispatch` |
| `user_repos` 写入 | 先删该用户全部行再写本次结果 | `save_user_repos(..., providers=[...])`：只替换本次**刷新成功**的平台 | 某平台 token 失效时，其它平台缓存被连带抹掉（81 → 7 的观感来源，虽本例实为用户清 PAT） | `test_d109_user_repos_partial_save` |

分层口径（回答"是不是完全不写死字面量了"）：
- **必须卡目录/配置驱动**：能力判定、路由分派、兜底顺序、显示名/域名/品牌色、渠道集合与展示序、缓存写入范围。
- **允许写死**：`domains/runtime/vcs/{gitcode,github,gitlab}.py` 等**平台实现模块**本身；
  `platform_card.py` / `adapter.py` / `api_client.py` 的**注册表**（平台 → 实现类）；
  迁移与历史 meta 键映射（`_HISTORICAL_*`）、`repo_key` 的裸键默认前缀。
  这些是"实现层"而非"判定层"，改成表驱动只会多一层间接。

判据（可机检）：`provider == "<平台>"` 形式的判定分支在 `src/` 内应为 0 处（注释除外）；
证明：`grep -rn 'provider == "git( code|hub )"' src` 唯一命中为一行注释。

### 20.18 场景用例（第六批，UC-66 ~ UC-69）

| # | 场景 | 前置 | 动作 | 预期 | 用例 |
|---|---|---|---|---|---|
| 66 | 渠道展示序数据驱动 | 配置文件写 `_ui_order: ["gitee","gitcode"]`，另有 github | 调 `providers_catalog()` | 顺序 gitee → gitcode → github（未列出的按名排尾） | test_d109_platform_order_data_driven::test_ui_order_drives_channel_display_order |
| 67 | 兜底顺序 = 实装托管的卡 | 平台卡目录 | 调 `_default_token_provider_order()` | 等于 `sorted(hosting_providers())`，含 gitcode/github/gitee | 同文件::test_host_fallback_order_is_catalog_derived |
| 68 | 顺序不再回潮到字面表 | 源码 | grep `_CATALOG_ORDER` / `_DEFAULT_TOKEN_PROVIDER_ORDER` | 0 命中 | 同文件前两条断言 |
| 69 | 部分刷新不抹掉别的平台 | 缓存有 gitcode/github/gitee 三平台行 | 只刷新 gitee 并落库 | gitcode/github 行原样保留 | test_d109_user_repos_partial_save |

### 20.19 绑定成功后的出口与文案（第七批，2026-11-14 用户实弹）

现象：引导页绑定 OAuth / PAT 成功后**仍停在引导页**；且成功提示显示成 `bind_success` 这类原始 key。

| 缺陷 | 机制 | 修法 |
|---|---|---|
| 停在引导页 | `onBound()` 只调 `app.loadRepos()`。它不摘 `pageContent.ob-mode`（全屏引导布局）、不改 `_currentView`、不 `_pushHistory('repos')` —— 引导页的整页布局因此一直生效 | 出口统一走 `app.goToRepos()`：`_obExitMode()` + `_pushHistory('repos')` + `_currentView='repos'` + 渲染项目页；顺序为 `refreshRepoLinks()` → `/auth/profile`（带回 `repo_platform_links` 兜底快照）→ `goToRepos()` |
| 显示原始 key | `BindDialog.t()` 只查 `app.account.<key>`；`bind_success` 只存在于 `app.login.<key>`（`app.account.bind_success` 不存在）→ 回退返回 key 本身 | `t()` 依次查 `app.account.*` → `app.login.*` → key；插值传 `{label}`（`_interpolate` 支持 `{x}` 与 `{{x}}`） |
| 切语言后文案不跟随 | 引导页与绑定弹层都没有 `grape:langchange` 监听（AdminChannels/AdminPlatforms/RegisterView 有）→ 切换语言只有外层头部变化，卡片/弹层仍是旧语言 | 两个组件加 `langVer` ref + 监听，`t()` 内 `void langVer.value` 建立渲染依赖 |

守卫：`tests/mgmt/test_onboarding_bind_flow_guard.py`（4 条：BindDialog 键在中英双包可解析 /
引导页键可解析 / 两组件都监听语言事件 / 绑定出口含 goToRepos 且不再只调 loadRepos）。

用例：

| # | 场景 | 动作 | 预期 |
|---|---|---|---|
| 70 | 绑定成功即进项目页 | 引导页绑定 OAuth 或 PAT | 页面切到项目页（侧栏/顶栏恢复），不再停留在全屏引导布局 |
| 71 | 成功提示本地化 | 绑定成功后看 toast，再切中/英 | 显示「<平台> 绑定成功」/「<平台> linked」，切换语言文案跟随 |

### 20.20 仓库维度读路径的真实前缀（第八批，gitee 端到端实弹后收口）

URL 形如 `/api/<域>/{owner}/{repo}` 的路由拿不到平台前缀，`to_repo_key("{owner}/{repo}")`
默认产出 `gitcode:`。凡直接拿它当 key 的路径，gitee/github 托管仓都会落在错行。
逐处清单（均已修，实测见"验证"列）：

| 路径 | 原写法 | 症状（实锤） | 修法 |
|---|---|---|---|
| `repos/{owner}/{repo}/project-info` | `to_repo_key(f"{owner}/{repo}")` | 对 `gitee:yang-aiwu-new/livefire-tpl-verify-1010` 返回 `repo_key="gitcode:…"` + 空基线 | `resolve_managed_repo_key(owner, repo)`（managed_repos 候选探针）→ 实测返回 `gitee:` 前缀 |
| `mgmt/agents/{owner}/{repo}/…`（两处） | 同上 | agent 会话/runtime 行落在 `gitcode:` key 上，gitee 仓的会话查不到 | 同上 |
| `events/{owner}/{repo}?…` | 候选写死 `[gitcode:, github:]`；会话 token 作用域也只认这两种 | gitee/gitlab 仓事件列表恒空（实测 gitee 仓 total=0 且查询里根本没有 gitee 前缀） | `repo_key_candidates()`（卡派生）；会话 token 校验用同一候选集 |
| `events` 三处引擎 scope 子查询 | `rc.owner_id IN (裸键, gitcode:, github:)` 固定 3 占位符 | gitee 仓匹配不到自己的 repo 级凭据，只能退到 global/user scope | 抽 `_repo_scope_clause(repo_key, uid)`，占位符随候选集动态生成 |
| `im/handler_async` 卡片显示 | 枚举 `("gitcode:","github:","gitlab:")` 剥前缀 | gitee 仓在 IM 卡片里带着 `gitee:` 前缀显示 | 结构化剥离（`:` 前是 key、后含 `/`） |

口径（与 §20.13 同源）：
- 路由拿到的 `{owner}/{repo}` **不是 repo_key**，必须解析真实前缀：已托管 → `resolve_managed_repo_key()`；
  批量查询 → `repo_key_candidates()`（超集安全，命中即算）。
- 只有"用户/凭证归属"类判断可直接用裸键（它本身不指向数据行）。

验证（用户三平台同时接入：gitcode/gitee/github，PAT 均在）：
- `platform-links` = `["gitcode","gitee","github"]`，`has_any=true`
- `/repos/member?provider=gitcode|gitee|github` = **58 / 7 / 16**
- `project-info`（gitee 仓）= 200 + `repo_key=gitee:…`
- `issues/{owner}/{repo}`、`dashboard/{owner}/{repo}`、`events/{owner}/{repo}` = 200
- `git-credential` = 403 属设计（仅 agent 会话 token tk_ 可用），非缺陷

守卫：`tests/mgmt/test_repo_prefix_resolution_guard.py`（3 条）——占位符数随候选集变化、
events 不再写死双前缀、project-info/agents 走解析函数。写这条守卫时它当场抓出
"events 的候选键补丁曾被一次 `git checkout` 回滚"，说明机检比人眼可靠。

### 20.21 场景用例（第八批，UC-72 ~ UC-76）

| # | 场景 | 前置 | 动作 | 预期 | 用例 |
|---|---|---|---|---|---|
| 72 | gitee 仓 project-info 前缀正确 | gitee 仓已托管 | `GET /repos/{owner}/{repo}/project-info` | `repo_key` 以 `gitee:` 开头；基线按该 key 读取（空则空，不错位） | 实弹 + 守卫 |
| 73 | gitee 仓事件可见 | gitee 仓有 event 行 | `GET /events/{owner}/{repo}` | 候选集含 `gitee:` → 能查到 | test_repo_prefix_resolution_guard（候选集断言） |
| 74 | agent 落对前缀 | gitee 仓 | 起/清 agent 会话 | runtime 行 key 为 `gitee:` 前缀 | 守卫断言 agents 用 resolve_managed_repo_key |
| 75 | repo 级凭据可匹配 | gitee 仓 + repo 级凭据 | 派发任务 | `_repo_scope_clause` 参数含 `gitee:` 候选 → 命中 | test_repo_prefix_resolution_guard::test_repo_scope_clause_follows_candidate_set |
| 76 | issue URL 反推平台 | 历史行 repo_key 为空、issue_url 指向 gitee | 反馈状态推导 | `gitee.com/o/r/issues/N` → `gitee:o/r`（未知域名返回空，不默认 gitcode） | test_issue_url_provider_mapping |

### 20.22 补证后的三项收口（第九批，2026-11-14 评审 P1 清单）

| 项 | 缺陷 | 证据 | 修法 |
|---|---|---|---|
| P1-3 | `hooks._kick_user_projects_refresh` 写死 `invalidate_user_repos(int(user_id))`；主键自 D105 起是 UUIDv7 字符串 → `int("01a1d…")` 抛 ValueError 被 `_do()` 的 except 吞成 warning，**关系变更后的项目列表刷新对所有 UUID 用户从未执行**（要等 30 分钟 TTL） | 全链路只有 hooks 加了 `int()`：`repos.py:1305/2564`、`templates.py:233` 都是原值直传；活库 `user_repos.user_id` 实测 `varchar(36)`、已有 72 行 UUID 形态 | 去 `int()`，`str(user_id)` 直传；`invalidate_user_repos` 签名/注释改为接受 str。**行为级用例**（不 mock 刷新函数、真实 uuid7 id 走 `on_pat_saved`/`on_oauth_login`）：回退到 `int()` 时红，修复后绿 |
| P1-1 | `repositories_platform_object.py` 的 `ChannelRepoLinkRepo` 类体内混入 6 个 BotAccount 方法（`list_by_provider/get_by_username/get_default/upsert_account/mark_token_status/touch_last_used`），`BotAccountRepo` 随后完整重复定义 | AST 实测该类方法列表含这 6 个 | 删除 33 行冒名方法；全仓 grep `ChannelRepoLinkRepo.<这6个>` 零命中（无调用方被误伤） |
| P1-2 | i18n/缓存版本位守卫红：`LOCALE_FINGERPRINT` 登记 `023638f3` vs 实际 `852e2ff5`；`CACHE_VER.admin` 登记 `20261114-6` vs 实际 `20261114-16`；`I18N_JS_VER` 登记 `20261114-3` vs 实际 `20261114-16`；`FROZEN` 映射 9 项落后（app.js 272→275、admin-view 17→35、auth.js 7→8、dashboard-view 65→66、repos-view 54→57、account-view 60→62、onboarding-view 5→7、autopilot-view 50→51、login-view 1→2） | 两个 ratchet 文件实跑 | 一次性同步四个登记值 + 指纹；两个 ratchet 现 2 passed / 4 passed |

附带的核实结论（评审提出但结论相反）：`user_repos.user_id` 在 SQLite DDL 文本里仍写 `INTEGER`，
但活库 MySQL 实测是 `varchar(36)` 且有 72 行 UUID 数据 —— 不是"严格模式下写不进"，只是
DDL 文本与实际不符，无功能影响（SQLite 动态类型亦无碍）。

守卫/用例：
- `tests/mgmt/test_d109_relation_hooks_refresh_projects.py`（3 条，双向验证：int() 红 / str() 绿）
- `tests/api/test_i18n_locale_cache_ratchet.py`（4 passed）、`tests/api/test_frontend_cache_version_ratchet.py`（2 passed）

### 20.23 场景用例（第九批，UC-77 ~ UC-79）

| # | 场景 | 前置 | 动作 | 预期 | 用例 |
|---|---|---|---|---|---|
| 77 | UUID 用户 PAT 变更后列表刷新 | uuid7 主键用户 + user_repos 有缓存 | `hooks.on_pat_saved(uuid, "gitee")` | `user_repos.synced_at` 置 NULL → 下次 `/repos/member` 回源 | test_d109_relation_hooks_refresh_projects::test_uuid_user_pat_saved_invalidates_projects_cache |
| 78 | UUID 用户 OAuth 登录后列表刷新 | 同上 | `hooks.on_oauth_login(uuid, "gitee")` | 同上 | 同文件::test_uuid_user_oauth_login_invalidates_projects_cache |
| 79 | 存量整型 id 字符串化不回归 | id="33" | `hooks.on_pat_saved("33", "gitee")` | 同上（修复不能只服务 UUID） | 同文件::test_legacy_numeric_string_id_still_works |

### 20.24 P2 四项整治（第十批，2026-11-14 评审 P2 清单）

| 项 | 缺陷 | 证据 | 修法 | 守卫 |
|---|---|---|---|---|
| P2-3 | `UserRepoPlatformLinkRepo.link` 是 UPDATE→INSERT 两步：并发窗口里两个会话都可能 UPDATE 0 行后各插一行 → 唯一键 (user_id,repo_provider) 冲突，异常被 hooks 吞掉、外层 uow 连带回滚（同事务的撤销/审计一起丢） | 代码 L441-465 | 方言级 upsert 单语句：SQLite `ON CONFLICT DO UPDATE`、MySQL `ON DUPLICATE KEY UPDATE`（复活语义：`revoked_at/reason` 置空），未知方言回退两步 | test_d109_user_repo_platform_link::test_link_is_single_statement_idempotent（重复 link 只一行、撤销后可复活） |
| P2-4 | 渠道 `enabled=0` 只挡登录**目录**（`_catalog_from_object(enabled_only=True)`），`GET /api/auth/login/<provider>` 直达照样发起授权 | 代码路径 + 目录过滤点 | 新增 `providers.is_channel_enabled(provider)`（真源=表 enabled 位；表未就绪/读失败 fail-open，避免 DB 抖动打挂全部登录）；`oauth_login` 在策略构建**之前**判 403 `CHANNEL_DISABLED` | test_channel_enabled_fail_closed（403 / 未知平台仍 400 / 表标志 0/1/异常三态） |
| P2-1 | `iter_statements` 用 `;` + 换行正则切 DDL：注释里出现"分号+换行"会把注释尾巴拼进下一条语句（评审注入实测 22 条碎片） | 原实现 `re.split(r";\s*\n", ...)` | 逐字符扫描：识别 `--` / `#` / `/* */` 注释与 ' " ` 引号，仅"语句态"分号断句，注释整段丢弃 | test_p2_ddl_and_schema_guards 前四条（行注释/字符串/块注释/真实 DDL 无碎片） |
| P2-2 | `_ensure_platform_tables` 幂等靠 `CREATE TABLE IF NOT EXISTS` + "already exists" 字符串忽略：半建表（表在、列缺）既不报错也不补列 | 代码 L41-52 | 新增 `reconcile_missing_columns(conn, log, only_tables)`：按 SQLAlchemy 模型逐列比对（MySQL information_schema / SQLite PRAGMA），缺列 ALTER ADD；整表不存在交给 CREATE；失败逐列告警 fail-open。启动建表后调用 | 同文件后三条（补列成功 / 整表缺失不误 ALTER / 幂等） |

口径：四条都属"静默失效"型缺陷——不报错、不崩，只是功能悄悄不生效。共同处置原则与
§20.22 一致：**按最终状态判定**（行数/列集/语句边界），而不是相信"调用没抛异常"。

### 20.25 增量两条：LIKE 通配符与顺序索引缓存（第十一批，2026-11-14 评审增量）

| 项 | 缺陷 | 证据 | 修法 |
|---|---|---|---|
| P2-6 | `save_user_repos` 的按平台删除分支用 `repo_key LIKE ?` + `f"{p}:%"`。provider 命名允许下划线（`^[a-z][a-z0-9_-]{1,31}$`），而 `_` 是 LIKE 的单字符通配符 → 新增 `gitee_enterprise` 时 `LIKE 'gitee_enterprise:%'` 会连 `giteeXenterprise:…` 一起删，**恰好复现该函数刚修掉的"缓存段消失"** | 代码 + 红绿实测：回退 LIKE 时用例红（"同形平台的行不得被 a_b 的前缀误删"），substr 版本绿 | 改 `substr(repo_key, 1, ?) = ?`（SQLite/MySQL 语义一致，天然无通配符）；当前四平台无下划线，属预防性修复 |
| P2-7 | `_catalog_order_index` 每次调用 `open()+json.load()`，而它被 HTML head 注入与 `/auth/providers` 每请求调用；同文件 `_builtin_channels_from_file` 有 300s TTL 缓存，新函数绕开了 | 代码 | 缓存键 = (路径, mtime, size)，带外改动配置自动失效；命中时跳过 open/parse |

定点复核（同一形状在全仓扩散，本轮一并改掉，共 8 处，行号为 39b3e2f3e 实测）：

| 文件:行 | 用途 | 修法 |
|---|---|---|
| `mgmt/user/db.py:3969`（save_user_repos） | 按平台替换缓存行 | `substr(repo_key,1,?)=?` |
| `mgmt/user/db.py:2190`（update_managed_repo_bot_token） | 查"共享同一 bot token"的仓库（LIKE 会带出同形平台） | 同上 |
| `mgmt/routes/grape_auth.py:954`（解绑影响面：路径 A 仓库列表） | 解绑前统计受影响仓库 | `substr(m.repo_key,1,?)=?` |
| `mgmt/routes/grape_auth.py:964`（解绑影响面：路径 B 仓库列表） | 同上 | `substr(repo_key,1,?)=?` |
| `mgmt/routes/grape_auth.py:1022`（解绑影响面：COUNT） | 同上 | `substr(m.repo_key,1,?)=?` |
| `mgmt/routes/grape_auth.py:1051`（解绑清理） | 删该平台 user_repos 行 | `substr(repo_key,1,?)=?` |
| `mgmt/routes/settings.py:2634`（_cascade_pat_clear） | 清 PAT 后删该平台缓存行 | 同上 |
| `api/onboarding.py:160`（cleanup_stale_repo_data） | 清 `onboarded_task:<k>:` 前缀 meta 键 | `substr(\`key\`,1,?)=?` |

守卫：`tests/mgmt/test_prefix_match_guard.py`（2 条）——全仓禁止 `:%"` 形态前缀参数
（mysql_compat 的约定文档除外），且 8 个改写点按"文件 × 片段 × 出现次数"登记，
少一处或多一处都红（新增前缀点必须同步登记，防回退与漏登）。
用例：UC-84（下划线 provider 不误删同形平台）/ UC-85（第二次调用命中缓存，把 `json.load` 换成抛错仍能返回）。

已知残留（豁免，2026-11-14 复核登记）：`mgmt/user/db.py::_path_b_prefix_clause` 仍用
`f"repo_key LIKE '{provider}:%'"`——它是**只读查询**（path B 判定），provider 取平台卡枚举非用户输入，
且默认平台还要匹配"裸键"（`NOT LIKE '%:%'`）的存量语义，无法机械换成 `substr`。
守卫正则 `:%"` 形态拦不到这种"内嵌进 SQL 文本"的写法，属守卫盲区；已另加一条守卫
（`test_no_embedded_like_prefix_with_interpolated_provider`）覆盖该形态，并把这 2 处登记为唯一豁免。

口径补充：**凡"按前缀操作数据"一律用前缀相等而非 LIKE**——LIKE 的通配符与 provider 命名
规则天然冲突，这类缺陷不会报错，只会在某天新增一个带下划线的平台时静默删数据。

## 20. 域归属、跨域访问与升级自愈（2026-11-16 三库四进程实弹收口）

> 触发：现网副本上一轮部署出现「PAT 保存后平台关系不出现」——接口层全 200，唯一异常是
> 钩子写关系表报 `no such table`。定位到两件事：四表没有域归属（四进程各建一份 / 整批被跳过）、
> 迁移台账与实表不一致（记账了但 DDL 没落地）。本节把三件收口写死。

### 20.1 四表单一 owner（mgmt）

| 表 | owner 域 | 备注 |
|---|---|---|
| `grape_t_oauth_channels` | mgmt | 登录渠道卡真源 |
| `grape_t_repo_platforms` | mgmt | 仓库平台卡 + bot 单归属（D110） |
| `grape_t_channel_repo_link` | mgmt | 渠道↔平台关系配置派生 |
| `grape_t_user_repo_platform_link` | mgmt | 用户↔平台关系（判据） |
| `grape_t_repo_credentials` | insight | 凭据域（列补齐只在 insight 进程做） |
| `meta` | runtime | 系统设置域 |

落地：
- `configs/table_ownership.json` 登记四表 owner=mgmt（视图映射另记 `grape_t_repo_platforms → runtime`，
  仅用于「同实例 + 视图」回滚形态；目标态仍是零跨域视图 + lib 通道）。
- `src/api/db_setup.py::_fn_create_platform_object_tables` 逐条 DDL 先解析表名过 `_owned_skip`；
  `grape_t_repo_credentials` 的 `created_at/derived_from` 补列只在 owner 进程执行。
- 实弹（三实例 MySQL，端口 33xx，库名 `grape_{mgmt,runtime,insight}_db`）：以 runtime 角色重跑同一迁移，
  日志逐条 `[DDL_SKIP] D109 create table ...: owner=mgmt current=runtime`，四表只在 mgmt 库生成；
  三库表数各自独立（mgmt 59 / runtime 32 / insight 12），无全量复制。

### 20.2 跨域读写只走 mgmt public（禁止 runtime 直连本域库）

- 读：`src/domains/mgmt/public/__init__.py::repo_platform_cards(enabled_only=True)`
  —— `_ANY_QUERIES` 具名查询（可移植 `?` 占位符），自有 DSN 走 mgmt 连接、单库回落进程库；
  **凭据列 `bot_token_enc` / `client_*_enc` 不在跨域列白名单内**。
- 写：`mark_repo_platform_bot_status(provider, status, checked_at)` 幂等 UPDATE 原语。
- 调用点：`src/domains/runtime/vcs/platform_card.py::_raws_from_table`、
  `src/domains/runtime/engine/bot_token_health.py::_mark_platform_bot_status`。
- 实弹：runtime 进程（自有库无该表）读到 mgmt 真源行；写 bot 状态后 mgmt 库该行更新、
  runtime 库仍无此表 —— 证明不是本地副本自嗨。
- 守卫：`tests/api/test_platform_object_domain_boundary.py`（清单归属 / DDL 过守卫 /
  runtime·insight 禁 import 平台 ORM / 跨域只走 public）。

### 20.3 迁移台账自愈（`202611170041 ensure-platform-object-tables`）

- 现场（脱敏）：存量库副本首启，`202611170030` 记账 `duration_ms=2`，四表却不存在，
  紧随的三条 backfill 未盖章；用户配 PAT 后钩子写关系表报 `no such table`，
  用户面 `/auth/platform-links` 恒空、仓库页回落「无仓库平台绑定」引导页。
- 处置：新增幂等迁移 `202611170041`，重放同一份四表 DDL 并校验存在性；
  任何「台账说建了、实表没有」的库在下次启动自动收敛，不依赖人工补表。
- 不变量：新迁移号必须大于既有号（renumber 纪律），且台账号不得被复用。
- 锁：`tests/api/test_platform_migration_self_heal.py`（台账说谎 → 自愈后四表齐；表已在 → 幂等不改数据）。

### 20.4 部署前提（自动，不靠人工记忆）

1. **角色 env**：四个进程入口 `os.environ.setdefault("GRAPE_PROCESS_ROLE", "<role>")`
   （mgmt / runtime / runtime / insight）。缺角色时归属守卫按「非 owner」跳过本域 DDL，
   症状与 20.3 完全一致。
2. **部署预检**：`scripts/deploy_preflight.py` 在起服务前查四件事 —
   角色 ∈ {mgmt,runtime,insight}、台账非空、台账↔代码注册表同号同名、
   本域 owner 表实存（单库拓扑额外要求 `meta` 在库）。任一项 FAIL 即退出码 1，不得启动。
3. **空库首装顺序**：先起 runtime（`meta` 归 runtime），再起 mgmt；现网升级（台账非空）不受影响。

场景用例：UC-D109-45~48（见需求分片 `10-usecases-decouple.md`）。
