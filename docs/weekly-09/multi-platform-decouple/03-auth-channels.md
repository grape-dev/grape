> ⚠️ 本文为拆分分片（内容唯一权威源）。索引导航：./multi-platform-decouple-design.md
> 来源：multi-platform-decouple-design.md L421-L768（原文原样搬运，零改写）。
> 域定义：登录域与身份归属——协议策略/身份键/归属链/建单链/token 口径/oauth_fill。
> 全局约束：继承索引页「表设计铁律/实现纪律/验收纪律」（见索引页）。

### D26：登录渠道协议策略（华为云 SSO 接入方案，S38/S39）

- **形态拍板**：A（企业 IdP 联邦 SAML/OIDC，推荐并采用）；B（IAM 账密直登）
  否决——把密码引入 grape 是攻击面（旧 hw_iam_* 死列不再启用，随 M1 收列）；
  C（AK/SK）非登录问题，归 IntegrationCredential 族（批次 1）。
- **抽象（配置化定律，与 vcs 方言同构）**：协议引擎是模板（写一次），
  实例全部进卡——内置三引擎 oauth2 / oidc / saml（通用 SAML 引擎，IdP 差异
  全由卡字段承载：idp_sso_url / attribute_map / IdP 元数据）；**新增渠道或
  换 IdP = 一张卡，零平台代码**。register_channel_strategy 仅作非标准协议
  逃生口。协议引擎实现统一三步契约：
  ① initiate_auth(state, redirect_uri) → 重定向/表单参数
  ② handle_callback(request) → 归一化身份声明（external_id/login/name/email）
  ③ refresh(tokens)（可选，SAML 天然跳过）
- **关键不变量**：任何协议的产出都是同一身份声明结构——下游
  external_identities 绑定、白名单（whitelist_column 自动建列）、
  grape_accounts、JWT、S15 解绑级联全部协议无关零改动。
- **华为云落地**：SamlChannelStrategy（AuthnRequest 构造 → IAM 联邦登录页 →
  断言属性映射归一 → create_external_identity）；渠道卡
  {provider: "hw_sso", type: "saml", idp_metadata_url, acs_callback_path,
  attribute_map}；只动账号域，不碰凭据族。
- **管理面**：IdP 元数据（证书轮换）复用 S18 admin CRUD 模式（批次 1 前手动配置）。
- **OIDC 引擎内置**（S44）：OAuth2 家族标准变体，claims 映射
  （sub/preferred_username/email）进卡；端点直配（discovery 自动发现归
  实弹批次）。与 oauth2 共享委托机制，零平台代码实例化。
- **渠道卡字段契约校验**（S45）：每引擎 validate_card(card)——构建时校验
  必需字段（oauth2/oidc：auth_url/token_url/user_url；saml：idp_sso_url +
  attribute_map.external_id），缺失即拒绝并报字段名。配置错误前置暴露，
  不留到运行时 401/500。
- **实现骨架已落**（src/auth/channel_strategy.py）：三步契约 + 注册表
  （oauth2 内置不可覆盖、未注册 type 明确拒绝）+ 通用 SAML 引擎骨架
  （安全默认：未验签断言拒绝登录）+ 声明归一（S42 语义）；S39/S40/S42
  骨架用例绿，SAML 验签待 IdP 元数据实弹（S38/S41）。
- OAuth2 存量渠道 = type=oauth2 的策略实例（行为不变，S39 契约测试回归锁）。
- **策略注册表**（S40）：register_channel_strategy(type, strategy) 插件口与
  vcs 的 register_vcs_backend 同构；渠道卡 type 未注册时构建校验拒绝并报错
  （不静默回退 oauth2——协议错配是安全事故不是降级场景）。
- **元数据轮换**（S41）：IdP 元数据随渠道卡存储（复用 meta 表 + 缓存热失效
  P2-2 模式），证书更新后 invalidate 即生效；断言验签按当前元数据。
- **属性降级**（S42）：external_id 是绑定主键，缺失即拒绝；name/email 是
  展示字段，缺失降级空值——与 OAuth2 渠道的 id_field/login_field 必填语义
  对齐（同一校验层实现，不分协议）。
- **绑定冲突**（S43）：SSO 断言的 external_id 与 OAuth 的 external_id 走
  同一张 external_identities 唯一约束与恢复规则——协议策略不自带绑定逻辑，
  统一交下游（这是"下游协议无关"不变性的直接推论）。

### D27：身份键安全（S46/S47，外部评审吸收项）

- **复合键已是现状，不动 DDL**：external_identities 的唯一约束本就是
  UNIQUE(provider, external_id)（db_setup DDL）——provider 名即 issuer/IdP
  维度，两个 OIDC IdP（不同 provider 名）同 sub 不冲突。外部评审
  "改成复合键"的建议经核实为误报；采纳其反向诉求：**用例锁死**该约束
  （S47 结构性断言，退化成全局唯一即测试失败）。
- **配置纪律（文档级不变量）**：provider 名 = IdP 实例身份，**换 IdP /
  换 issuer 必须换卡名（新 provider）**，旧卡走 S15 解绑级联——同名换源
  会把旧用户的 external_id 与新 IdP 的主体错绑。归一层注释已声明。
- **external_id 稳定性双层校验**（S46）：构建时（SAML validate_card）按
  属性名形态拒绝 email/transient 类映射（报属性名 + 替换指引）；回调时
  （normalize_identity_claims）按值拒绝 email 形态（协议无关兜底）。
  启发式只能判形态，属性语义稳定性由卡作者保证——文档同步写明
  "persistent 标识或稳定数值 ID 才可作 external_id"。
- **单测边界（本单纪律）**：S46/S47 全部为 mock/meta 驱动的单元用例；
  SAML 实弹件（验签钩子注入、时钟偏移、断言重放 NotOnOrAfter/Conditions、
  ACS 绑定校验、元数据轮换）归 IdP 联调服务线，不入本交付。

### D28：登录路由策略分派接线（S48，外部评审吸收项）

- **能力分派，不比 type 字符串**：LoginChannelStrategy 声明协议能力
  （needs_oauth_client / post_callback）。GET /login/{provider} 统一
  `build_channel_strategy(provider).initiate_auth(...)`——oauth2 委托
  build_auth_url（行为零改动，S39 回归锁），SAML 由策略自落 state
  （RelayState 带回）。POST /callback/{provider}（SAML ACS）按
  post_callback 能力守门。对应业界形态：路由只认渠道标识
  （registrationId 等价物 = provider 名）+ 注册表查找，加渠道是加卡
  不是加 else-if（Spring Security RelyingPartyRegistration /
  Keycloak IdP SPI 同构）。
- **state 生命周期归位**（吸收外部评审，落点修正）：生成+存储归策略
  （SAML initiate_auth 自落；OAuth2 走 build_auth_url 内部落库）；
  校验/消费是服务端会话横切关注点而非协议差异，提取为
  `_validate_channel_callback` 共享 helper（GET 与 POST 两入口共用，
  消灭重复校验链——与库内"重复定义类族"清扫同纪律）。
- **GET 回调渐进迁移**：OAuth GET 回调仍走既有链路（最敏感登录路径），
  仅前置校验换用共享 helper（行为逐行保留）；整体迁移到策略层留批次 1。
- **不采纳**：模式四式独立 IdP discovery 层（单渠道规模属超前设计）；
  state 校验下沉进策略类（会让每个策略重复实现同一横切逻辑，Spring
  自身也用独立 repository 组件）。fail-closed 已满足：未注册 type /
  卡缺字段构建时拒绝（密码学操作之前），未验签断言拒绝登录。

### D29：评论投递族卡驱动（S49，外部评审吸收项）

- **消灭管道外围最后的活二分**：_post_result_to_issue /
  _post_subtask_completion_to_parent / _post_all_subtasks_done /
  _post_result_to_pr / _sync_issue_comments_to_conversation /
  _fetch_issue_assignee 六处 _is_github_repo 分流 + 硬编码双平台 URL，
  统一收敛到 VcsApiClient._request（卡驱动路径与四态鉴权）。
  github 存量形态逐字节保留（token 头 + v3 Accept）；gitcode 由 Bearer
  修正为 query_token（D18 已定的正确语义，评论族此前漏改——同族漏改
  家族的又一例，本单收口）。
- **PR 评论路径按方言派生**（不比 provider 名）：query_token 系 →
  /pulls/{n}/comments；gitlab 方言 → /merge_requests/{n}/notes；
  github 系 → PR 共用 Issue 评论 API。
- **台账 provider 标签**由 repo_key 前缀派生（_provider_of_repo_key），
  消灭 "github"/"gitcode" 字面量；_ledger_record_comment 契约不变。
- **删除死代码**：_github_fetch_issue / _github_list_issue_comments /
  _github_post_issue_comment（零调用方，全库 grep 实证）。
- **测试盲区补齐（评审根因项）**：新增行为级单测——mock 传输层断言
  评论确实投递到正确平台端点（URL + 鉴权形态 + body + 去重只 GET），
  同时锁住本族改造；此前该族无行为断言，丢代码测试照样绿。

### D30：外围第二批卡化——admin 面与路由解析点（S50）

- **admin/config_store**：verify_token 由 gitcode/github 双 URL 二分改为
  create_provider(provider).verify_token（卡 api_base/api_auth，任意已配
  平台可用）；bot token 升级触发的 bot 用户名获取改走 resolve_bot_username
  来源链（B2 抽象既有解）。遗留的 managed_repos 列名（bot_*_token）与
  协作者权限等级仍属批次 1 存储族，本单不动（声明）。
- **repos.py 托管路由解析**：_provider_from_hint 双前缀 + 双候选 SQL 改走
  repo_key_candidates（D16/S25 既有抽象）；_get_user_provider_and_token 与
  _provider_from_token 统一 get_user_token_for（原 gitcode 分支的
  with_refresh 薄委托等价消除），login_provider 支持任意已配平台优先、
  github/gitcode 存量顺序保持。
- **边界**：grape_auth 两处残留经核为 by-design（#455 GitHub 落库读回
  校验专属增强、文案平台名），不属本族。

### D31：身份优先归属 + PR 探测卡驱动（S51，M5 零危害优先解）

- **归属正确性（M5 病灶）**：事件/issue → 账号的归属反查原实现是
  unified_user_mapping 名字域 join——注册名 ≠ 平台登录名错位会静默把
  user_id 算成 0（无报错）。本单零危害优先解：新增
  resolve_user_id_by_platform_login（external_identities 按
  provider+external_login 精确匹配活跃绑定，平台登录名的权威域，
  协议无关、含 config-only 平台），issues_center 与 engine_select 两处
  归属反查身份命中即返回、未命中回退既有 um 链——不迁数据、不删表、
  错位场景行为从"算错"变"算对"。um 表全迁身份域与退役归批次 1。
- **PR 探测兄弟站点收尾**：resolve_pr_issue_id 第 4 步 PR 详情 API 兜底
  由 _is_github_repo + 双平台硬编码 URL 改为 create_client.get_pr
  （D29 评论族改造的漏改兄弟站点，本单收口；error 响应安全终止）。
- **边界**：deps.py 刷新实现仍 gitcode/github 二分（新 OAuth 平台刷新
  能力归渠道策略 refresh 契约，批次 1）；um 双 id 兜底表列结构不动。

### D32：验签头透传 + 方言 overrides + PAT 泛型入口（S52，先测后改纪律）

- **先测后改**（用户拍板纪律：可以优化但不能把对的改错）：每处改动先补
  单测——新能力 RED 断言 + 旧行为特征锁（验签失败仍拒、GitLab 内置
  kind 不变、存量 github/gitcode 字段路径零副作用）——全部转绿才入库。
- **webhook 头集合全量透传**：generic 路由原先把 headers 过滤成三头
  白名单再交方言 verify——多余封闭点（第 5 平台新头名被截断）。方言
  verify 本就只认各自头名，透传零行为差（特征测试锁）。第 5 平台新头
  名仍需在方言层支持（方言能力位，批次 1）；卡 webhook_verify 字段的
  消费点打通同批次（当前由方言家族决定，死配置已如实登记）。
- **GitLabDialect event_map_overrides 生效**：normalize 原接收参数但
  从不使用（死参数）——现命中 kind 即映射（优先于内置，与 GitHub 系
  语义一致）；gitee 独有头值由卡 overrides 配置（不在代码里猜 gitee
  的头值表，实弹按真实 IdP 头口径配卡）。
- **platform_tokens 泛型 PAT 入口**：settings 凭据端点新增
  platform_tokens={provider: token}——github/gitcode 走既有专用字段
  （零改动），config-only 平台验证（create_provider().verify_token，
  卡驱动）后双写 user_credentials <p>_pat（UserToken 级）+
  external_identities（D31 归属源——gitee 归属链自此有源）。边界：
  冲突 409 不含票据转移流（文档注明）；空值清除 PAT 凭据（identity
  解绑走既有流程）；未配卡平台 400 fail-closed。

### D33：归属加固（S53，先测后改）

- **复核结论记录**：D31 方案经真实数据端到端实测成立（活库 resolver
  返回正确 user_id、三条写路径正确维护 login）；上轮质疑清单两撤回
  一降级，成立两条小项本轮修毕。
- **幂等索引**：external_identities 增 (provider, external_login) 索引
  （DDL 无该覆盖、归属是每事件热路径）；走既有 _index_sqls 幂等清单，
  零数据变更，结构测试锁死。
- **异常留痕**：身份优先块 except: pass → log.warning（import 级失败
  不再永久静默），回退 um 兜底行为不变（行为锁）。

### D34：归属链收口——建档写入 + 通知链（S54，先测后改）

- **背景**：D31 只接了两个读点；归属错位家族的两端各漏一处——
  session_store 建档 INSERT 的 COALESCE um join 是错位数据的**生产端**
  （issue.user_id 落库即错，下游 scene_user_id/mine 过滤全链错），
  _get_feishu_open_id 按 um 平台列查是**消费端**（同一归属动作一半
  身份域一半名字域，错位用户收任务卡收不到飞书通知）。
- **实现**：两处均按 D31 同款零危害形态——resolve_user_id_by_platform_login
  命中即权威，未命中回退既有 um 路径（特征测试锁兜底语义）；异常
  log.warning 留痕（D33 纪律）。通知链复用既有
  _get_feishu_open_id_by_user_id（user_id 键查询，身份域正确）。
- **重构目标对齐**：本项服务于账号体系功能目标（归属正确性），
  非 ORM/分层等结构性重构——结构性项（Repository 收敛）按既有
  D 系列统一入口路径渐进，注册发现明确不做（用户拍板）。

### D35：建单链卡化（S55，先测后改）

- **真破绽定位**（第二轮扫描复核确认）：D21 接线的是 onboarding 建单
  主体，但三处旁路对 gitee 仍落 GitCode 分支——① 手工建单查重
  provider="github" if _is_github else "gitcode"；② 建单分派
  _post_github/_post_gitcode 二分；③ onboarding 查重默认 gitcode。
  gitee 仓查重打 GitCode URL → RuntimeError「bot token 失效」误报，
  建单直接失败。
- **实现**：非 github 查重分页统一走 create_client（卡端点 + 四态鉴权，
  分页终止/error 抛错语义逐行保持）；建单收敛 _create_issue_unified
  （委托 VcsApiClient.create_issue，存量平台由内置卡对齐 byte 语义）；
  三处 provider 口径改 parse_repo_key 派生。_post_github_issue /
  _post_gitcode_issue 保留（委托实现层，create_issue 卡路径的存量分支）。
- **特征锁**：gitcode 查重 URL/分页/响应形态（{data:[]}/list）、GitHub
  Search API 精确匹配、token 失效抛错、401 刷新重试链全部不变。

### D36：查重策略下沉适配器（S56，先测后改）

- **用户纠偏记录**：D35 把分支从调用方挪进适配器内部但仍写死
  provider == "github"——被指出后修正：**"有无搜索端点"是平台能力，
  不是平台名**。新增卡字段 issue_search（github 内置卡置 True，其余
  默认 False），策略按能力位分派；第 5 平台有搜索端点即配卡获得，
  无则分页。结构守卫测试双锁：调用方 wrapper 无平台分支/直调，适配器
  实现无 provider 名字面量。
- **行为契约防线**：查重失败 RuntimeError 携带 (NNN) 括号形态——
  token_error.extract_http_status_from_exc 按 \((\d{3})\) 提取，传输层
  "HTTP 401" dict 不换形态会把 github bot token 401 掉进通用文案分支
  （真实回归，行为锁 + 单测双防）。
- **流程防线生效**：D35.1 的接缝 grep 纪律在本轮直接命中——issues_api
  的 github 401 文案用例 mock 死接缝（打真实 API）暴露了上述形态回归，
  修复后全部对齐基线。

### D37：token 解析二分族收口（S57，先测后改）

- **功能性破绽**：agent 脱敏与 chat 直连取用户 token 落
  get_user_token_with_refresh（默认 provider=gitcode）→ gitee 仓取空；
  fail_task 评论 fallback 落 _get_global_bot_token（GitCode 全局）→
  gitee 仓无 token。
- **统一入口**：deps.get_user_token_for_repo(repo_key, username, user_id)
  ——provider 由 repo_key 前缀派生；**通用性原则（用户拍板）**：调用方
  不与任何平台名耦合、不与 OAuth 语义耦合——凭据来源（OAuth 身份域
  vs PAT 泛型表 user_credentials）由 get_user_token_for 内部解析，
  调用方只见 (repo_key, user, 结果)。fail_task 走 get_global_bot_token_for
  （存量平台即原函数包装，等价有锁）。
- **等价锁**：provider=gitcode 的 get_user_token_for 与
  get_user_token_with_refresh 薄委托等价（行为锁）；github/gitcode 全局
  token 包装等价（行为锁）；结构守卫锁调用方无平台字面量。

### D38：bot token 升级链卡化（S58，先测后改）

- **功能性破绽**（src/user/db.py 升级链三处，第二轮扫描复核确认）：
  ① update_managed_repo_bot_token provider 口径
  `github if startswith("github:") else gitcode` → gitee 折叠 gitcode；
  _bot_token_col 硬编码双列 → gitee 共用 token 查重打错列。
  ② _update_repo_bot_token SQL 列名硬编码（github/gitcode 两分支各写一套）
  → gitee 写 bot_gitcode_token/gitcode_webhook_token 错列（正确=卡派生
  bot_gitee_token/gitee_webhook_token）。
  ③ _rebuild_webhook_with_bot_token webhook 删/建 github/else 二分 import
  存量函数 → gitee 走 repo_manager（GitCode API_BASE + password 形态），
  打错平台。
- **实现（D22 端口复用，调用方零平台分支）**：provider 派生走
  parse_repo_key；列名走 create_provider().token_storage_field()/
  webhook_token_field()（内置卡=存量列名 → github/gitcode SQL 逐字节
  等价有锁；列名插 SQL 前正则 identifier 白名单校验，防注入面）；
  webhook 凭据形态（Gitee 系 password=bot_token / GitHub secret="" /
  卡平台 generic）下沉适配器新端口 **setup_repo_webhook(bot_token,
  owner, repo, url)**——GitCodeProvider/GitHubProvider 覆盖=存量委托
  原函数形态不变，GenericVcsProvider 基座=generic 形态（D36 同款
  "形态在适配器"）；删旧走 remove_webhook 端口（存量委托已在）。
  config-only 平台物理列存在性归批次 1 DDL（本批测试 ALTER 模拟）。
- **等价锁**：gitcode 列名/清洗语义（webhook_secret 清空、host_user_id=0、
  bot_token_status 清空、webhook_id int 化）逐字段行为锁；github 同；
  gitcode webhook 重建仍 repo_manager.setup_webhook(password=bot_token)、
  github 仍 setup_github_webhook(secret="")（mock 断言参数逐个对齐）。
- **结构守卫**：三个收口函数 AST 级禁平台名字面量（签名默认值与
  docstring 豁免——前者是存量兜底语义、后者是文档语义）。
- **残留站点（本轮扫描登记，留 D39 候选；D39 已全部收口见下节）**：get_host_vcs_username
  fallback provider 口径折叠（external_identities 查询键错位）；
  auto_upgrade_path_b_repos 协作者邀请链 github/else 二分（gitee
  best-effort 失败跳过，loud fail 可接受）；grape_auth.py 解绑白名单
  (gitcode, github)（gitee 身份无法解绑，400）；scenes.py 分支缓存失效
  双平台循环。批次 0 里程碑（S12/S21 gitee 实弹验收）前按破绽优先级
  逐个收口。

### D39：仓库平台绑定/解绑域收口（S59，先测后改，领域建模纠偏）

- **用户纠偏记录**：绑定/解绑是**仓库平台域**的平台无关操作，"支持的平台"
  是从目录派生的集合概念，不是字面量元组；三个领域对象（仓库平台域
  vcs_providers_config / OAuth 登录渠道域 oauth_providers_config+channel_strategy
  / 账号体系域 grape_accounts+external_identities）彼此解耦，唯
  一刻意耦合点=oauth_fill（渠道名==平台名时 token 引用不复制、刷新托管
  登录域、PAT 优先）。原白名单 (gitcode, github) 是 F6 加固时偷懒写的
  字面量——本轮按域口径修正。
- **功能性破绽（四族）**：① 解绑白名单字面量 → gitee 等已接入平台身份
  无法解绑（400）；合法集合应=登录渠道目录 ∪ 仓库平台目录（PAT 绑定
  S52 双写 external_identities）；② um_column_for_provider 未知平台折叠
  gitcode_user → 双向污染（绑定链 _sync_um_platform_col 把 gitee 登录名
  写进 gitcode_user 列、解绑误清别人的列、归属兜底五处读侧跨平台同名
  误命中，FR-17.1 反例）；③ get_host_vcs_username fallback 口径折叠；
  scenes._branches_cache_invalidate 双平台循环（gitee 建分支缓存不清）；
  ④ auto_upgrade host token/协作者邀请链 github-else 二分（host token
  折叠 gitcode、邀请走 GitCode API）。
- **实现**：解绑白名单 = list_providers() ∪ list_vcs_platforms()（正则
  identifier 前置校验保 F6 LIKE 前缀安全前提，目录加载异常 fail-closed）；
  um 列映射改白名单表 {gitcode, github}，无列平台返回空串且**全调用方
  显式跳过**（写侧 _sync_um_platform_col/解绑清列；读侧 issues_center ×2、
  pipeline、session_store、engine_select——兜底 miss 语义=原查询不命中，
  跨平台误命中消除）；host_vcs_username 走 parse_repo_key 派生；缓存失效
  平台集合走仓库平台目录；auto_upgrade host token 统一
  get_user_token_for(provider)（D37 等价锁覆盖）、协作者邀请/接受走适配器
  新端口 add_collaborator（permission 缺省=各适配器存量默认
  Gitee 系 maintainer / GitHub push）/ accept_collaborator_invite
  （generic 直加平台默认 True，邀请制平台子类覆写走存量流程）——调用方
  零平台分支（D36/D38 同款"形态在适配器"）。
- **接缝迁移**：auto_upgrade host token 接缝从 get_user_token_with_refresh
  迁至 get_user_token_for，两个旧用例 mock 接缝同步更新（D35.1 纪律）。
- **等价锁**：gitcode/github 解绑行为（um 清列仍执行）、um 映射与兜底
  命中、auto_upgrade gitcode/github 邀请参数逐个（maintainer/push 权限、
  accept 函数形态）全部行为锁；未配卡/未知平台 400 fail-closed。
- **结构守卫**：unbind_provider 函数体 AST 级禁平台名比较；
  auto_upgrade 源码禁 provider == 字面量。

### D40：oauth_fill 根治——显式链接注册表 + 凭据撤销域服务（S60，先测后改）

> **D100 收口（2026-11-08）**：本节六条验收门槛中"落库填充"相关条目随 upc
> 废弃退役；注册表现役职责=refresh 腿 + 链接清单声明面（lazy 终态，见
> provider_link 模块 docstring 与方案稿 §四 R7）。历史叙事保留供审计。

- **用户纠偏记录（"从根本上解决问题"）**：对 oauth_fill 耦合点的外部
  分析判定"有条件合理"，六条条件（显式映射、授权最小化、最终一致、
  短期 token、刷新并发/降级/审计、撤销传播与审计）中两条在当前代码
  实锤：① deps._try_refresh_user_token 刷新分派 provider=="gitcode"/
  "github" 字面量二分（"渠道名==平台名"隐式契约的残留路由点）；
  ② 解绑只清 {p}_oauth，S52 写入的 {p}_pat 残留可用（撤销不传播）。
  根治=不在调用点补丁，把两处根因各收敛为身份域域概念。
- **域概念一：显式链接注册表（src/auth/provider_link.py）**：oauth_fill
  链接=渠道目录 ∩ 平台目录（目录派生，调用点不再字符串相等路由）；
  刷新能力=注册声明（provider→refresh 实现，deps 底部注册 gitcode/
  github；未注册渠道返回 None——"PAT-only 平台无刷新"显式语义非
  else 兜底）；supports_refresh=False=渠道可授权但短期 token 过期需
  重新授权。刷新责任全部留在登录域，仓库域只消费结果。
- **域概念二：凭据撤销域服务（grape_account_db.revoke_provider_credentials）**：
  身份域拥有凭据键族规则（{p}_oauth / {p}_pat，集中一处不散落），
  解绑/吊销单入口传播；external_identities 软删仍走 delete_external_
  identity（keep-one 属账号域决策）。解绑路由由内联清 _oauth 改为
  调用域服务，并从静默吞错改 loud fail 留痕。
- **等价锁**：gitcode/github 刷新分派存量等价（注册表 mock 断言）、
  解绑 _oauth 清除语义保持；gitee（PAT-only）不在 oauth_fill 映射、
  刷新返回 None。
- **结构守卫**：_try_refresh_user_token 函数体 AST 级禁平台名字面量。
- **批次 1 验收门槛（用户六条条件，RepoCredential 建模时逐条对照）**：
  1. 渠道↔平台映射显式（provider_link 已落地，RepoCredential 引用
     必须声明 source 与能力）；
  2. OAuth 授权范围最小化/可增量，登录授权与仓库授权分开或 consent
     明示；
  3. 绑定+引用创建+刷新托管非原子——需幂等/重试/补偿，显式
     "登录成功但仓库凭据延迟就绪"状态；
  4. 仓库域只持短期 access token（加密、TTL 受限），不碰 refresh
     token；刷新加锁（现进程内锁，多实例需分布式锁）、处理 rotation/
     并发/撤销传播；
  5. PAT 优先可解释可审计：解析结果带来源+原因，PAT 失效不静默
     回退 oauth_fill（默认不回退或显式配置）；
  6. 撤销与审计闭环：解绑/平台侧失效传播到引用；刷新成功率/失败
     原因/PAT 与 oauth_fill 使用比例可观测。

