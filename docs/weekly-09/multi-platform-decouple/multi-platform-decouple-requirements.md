# 多平台支持 + OAuth 凭据解耦 — 需求文档（索引导航页）
> ⚠️ **本页为导航入口 + 全局约束唯一权威源；用例正文唯一权威源在 `multi-platform-decouple/` 子目录——请勿在本页编辑用例内容**（防双源漂移）。
> 设计文档：multi-platform-decouple-design.md（同构索引页）。
> 编号规则：本目录分片 `10-13` 为需求用例族；设计域分片 `00-06` 见设计索引页——两套编号区分子文档类型。
> 行数守恒式（拆分校验记录）：原文件 568 行 = 全局约束节（本页正文）495 行 + 用例分片正文 73 行（10-13 四分片，头部声明另计）。

## Objective


- **对象模型解耦（本需求核心）**：登录凭据与仓库凭据是两类领域对象，不是一个模型——
  登录凭据属账号域（身份会话，OAuth access/refresh，随登录产生、登出吊销、自动刷新）；
  仓库凭据属资源域（仓库访问 token，独立来源与生命周期，与登录无关）。存储、读写路径、
  生命周期三者全部分离。
- 任何标准 OAuth2 + webhook 方言兼容平台（gitee 为首个验收对象）通过
  一条配置接入，四层同源：OAuth 登录 / webhook 事件归一 / git 传输鉴权 / API 客户端。
- 取仓库凭据只按 (user_id, repo provider) 决定，登录身份不参与；
  OAuth token 到期自动刷新，手配 PAT 不自动续期且 UI 明示。
- 修复 git 传输层现存平台硬编码 bug（gitlab: 仓库 clone URL 静默回退 gitcode.com）。

## 凭据续期口径（已拍板）


| 凭据来源 | 对象 | 到期行为 |
|---|---|---|
| OAuth 登录自动填充 | 登录凭据（external_identities），OAuth 登录即绑定写入；仓库域授权走用时取用 lazy（D100 终态：登录即完成授权，不落仓库凭据行） | refresh_token 自动换新，用户无感 |
| 用户手配 PAT | 仓库凭据 | 不自动续期（平台无 PAT 刷新机制）；失效时报"PAT 已失效，请重新生成"，UI 明示 |

边界：不拿 OAuth token 反向调 PAT 创建 API 生成"伪 PAT"——凭据只有上述两种来源两条生命周期。

## 表设计铁律（2026-11-07 用户拍板，所有后续批次必须遵守）


1. **UUID 主键**：所有新增表必须有逐行主键 id——应用层 `uuid.uuid4()` 生成、TEXT 存储、
   36 字符标准形式（8-4-4-4-12），**不混 32 hex**（混用会让 credential_id 跨系统比对静默失败）。
   存量无 id 表随各域批次幂等补列回填（只处理 id 为空行，分批，不全表重写）。
2. **时间双列**：同批必须有 created_at + updated_at 两列，应用层维护（不依赖 DB
   datetime 函数，SQLite/MySQL 双方言零翻译成本）；update 路径禁止动 created_at。
3. **写入路径禁 INSERT OR REPLACE**（有 UUID 主键的表）：换 `INSERT ... ON CONFLICT
   DO UPDATE` 且 conflict 分支不覆盖 id——REPLACE 删旧行插新行会让 id 漂移，
   审计 log（credential_usage_log）的历史 credential_id 全部悬空。
4. **枚举值注释必须实码核对**：DDL/设计文档里的字段值域注释禁止凭印象写固定枚举——
   动态派生字段（如 user_credentials.provider 三族：云凭据裸名/`<platform>_pat`/
   `<platform>_oauth`，平台清单卡驱动）写派生规则不写死值。
5. **凭据通道加密**：凭据存储面统一"通道层透明加密"（写前 encrypt/读后 decrypt，
   历史明文行读侧兼容）——user_credentials 接 credential_crypto（AES-GCM）与
   external_identities 对称；新凭据表默认继承，不允许明文落库。
6. **审计表无外键级联**：credential_usage_log 用 credential_id 软引用 + 应用层校验，
   删凭据行必须保留审计历史；log 表预留 credential_kind 字段
   （'user_credentials' | 'user_platform_credentials'）防 P-2c 上线时迁移。
7. **关联表定位**：P-2c user_platform_credentials 是 user_credentials 的**替代表**
   （非补充）——user_credentials 走 P-8 下线路径；禁止做成两表并存各留一份并存语义。
8. **迁移逻辑模块化**（2026-11-07 架构收敛）：init_db 保持"薄函数"（DDL 声明 +
   迁移模块调用，≤35 行）——一次性迁移独立成模块（如 src/user/credentials_backfill_n1.py、
   legacy_column_migrations.py），生命周期：上线=init_db 一行调用；下线=删模块文件
   +删调用行，主文件零残留；每个迁移函数 docstring 带下线判据（生产零命中观察期）。
   禁止在 init_db 内联追加迁移段（守卫 test_s122 UC26⑤ 锁死）。
9. **数据访问分层（ORM 默认，原生 SQL/Core 显式豁免）**（2026-11-07 拍板，业界
   分层共识；SQLAlchemy 2.0 为默认持久化栈，Core 与 ORM 是两个一等组件）：
   常规 CRUD 与领域模型持久化**默认走 ORM**（persistence 域 Model+Repository+uow，
   S79 起先例）；以下场景允许并应使用**参数化**原生 SQL 或 **SQLAlchemy Core/
   批量接口**，且须经代码评审并在开发规范中记录退出条件：数据迁移与回填（含加密
   回填/UUID 回填——批量写入性能数量级差异，逐行 ORM hydration 是反模式）、批量
   导入/更新、高频审计写入（credential_usage_log 走 Core insert）、复杂聚合/报表、
   性能敏感路径、数据库专有特性、以及 ORM 抽象会遮蔽必要语义控制的场景（如
   upsert 冲突策略、审计表保留语义）。**所有数据库访问一律参数化，禁止字符串
   拼接 SQL**——安全底线，不随 ORM/原生/Core 选择而变；ORM 的 raw/text() 逃逸口
   同属注入面，显式管控。**新写 SQL 一律标准语法**（2026-11-07 追加）：禁用
   SQLite 方言构造（rowid/裸 PRAGMA/sqlite_master/INSERT OR REPLACE 等）——
   既有代码的方言构造由 mysql_compat 翻译层兜底（历史债不动），但新代码直接写
   标准形态（业务键定位行/信息标准查询/显式 upsert 两步形态），不新增翻译层
   依赖（实证教训：rowid 回填在 MySQL 下报错被 except 吞掉，存量库回填静默
   空转）。

## FR-27：仓库凭据存储终态化（2026-11-11 用户拍板，FR-26 顺延；设计文档：d90-n6-repo-credential-finalization-design.md）

| # | 需求 | 验收锚点 |
|---|---|---|
| FR-27.1 | grape_t_repo_credentials(repo_key, cred_type∈{bot,webhook}, value) 为仓库级凭据唯一权威：读 rc 优先无条件、managed_repos 平台 token 列=legacy 读回落；写 rc 单写且同事务内置空对应列（渐进收敛：每次轮换自动完成一条迁移） | F4 验收用例②（文件名以 F4 交付为准） |
| FR-27.2 | 每仓库每 cred_type 至多一条（UNIQUE(repo_key, cred_type)）为显式契约，写入表注释与卡片 schema；不加 cred_slot；未来多 webhook 需求走单表加 cred_slot DEFAULT 'default' 轻迁移 | DDL/模型注释断言 |
| FR-27.3 | gitee/gitlab 四列（bot_gitee_token/gitee_webhook_token/bot_gitlab_token/gitlab_webhook_token）撤出 DDL 与动态补列白名单；卡片 token_storage_field 留空=无托管列存储；gitcode/github 基线列保留为读回落，物理退役走后续批次 | F4 验收用例①（冷启动建库无该四列） |
| FR-27.4 | 回填幂等只填空（rc 有值不覆盖）；列值已加密，no_plaintext 用例锁 | F4 验收用例③ + N3 用例锁 |
| FR-27.5 | resolver 读回落分支显式标 legacy + 独立消费审计；退出 checklist：回填完成→置空→关回落→删分支（顺序不可倒置） | 消费审计零命中 + 回归 |
| FR-27.6 | repo_key 由 src/repo_key.py 单一产出/解析；回填直搬同表字符串，join 字节一致 | 行为锁 |
| FR-27.7 | admin 隔离：admin config_store 只读写 meta 全局凭据（global_bot_*）与 bot_token_status 状态列；managed_repos 列与 meta 同名键（gitcode_webhook_token）同名不同表，脚本严禁按名误伤 meta | 结构守卫/回归 |
| FR-27.8 | 措辞口径限定："接新平台零 DDL"指接入层（rc 仍是唯一演进表）；"调用方零分支"指经 resolver 接口 | 文档口径（本文档登记） |

## FR-28：代理多平台契约适配（2026-11-11 部署实弹验证收口；设计文档：d107-proxy-upstream-contract-adaptation-design.md）

| # | 需求 | 验收锚点 |
|---|---|---|
| FR-28.1 | 上游 API 方言差异以 **provider 契约开关**声明（providers/<name>.py 模块级常量，如 LABEL_COLOR_PREFIX_HASH / MILESTONE_DUE_DATE_ONLY / ISSUE_UPDATE_OWNER_PATH），代理层 getattr 派生消费——新增平台时差异用既有开关可描述即零代理层改动，**无平台枚举分支** | 开关存在性 + 消费点无枚举（行为锁） |
| FR-28.2 | issues update 遵循 v5 官方 owner-path 契约（PATCH /repos/{owner}/issues/{number}，路径无 repo 段，body 必带 repo；title 缺失回取回填）——repo 路径形态是集成错误不是平台限制（gitee IK* ident 恒 404 的真根因；同案 imjoey/ge PR !45） | URL 拼接 + body.repo 形态断言 |
| FR-28.3 | gitee body.repo 必须是纯仓库名（owner/repo 全称 404）；gitcode 两种均收（保持全称，零变更） | gitee 纯仓库名断言（实测对照在案） |
| FR-28.4 | gitcode 差异三件：labels 色值必须带 #（GitHub 相反）；milestones due_on 只认 YYYY-MM-DD；labels 创建端点 form 编码（其余写端点 JSON 不变，传输层测试契约保持） | 色值/日期化/编码行为锁 |
| FR-28.5 | PR comment id 形态兼容：gitcode/gitee 返回 32 位 hex（github 数字），update/delete 前先解析为数字 note_id（#623 解析器复用），纯数字直传向后兼容 | hex→note_id 解析 + 数字直传断言 |

## 双配置轴场景（本需求的目标形态）


两条独立配置轴，正交组合：
- **登录渠道轴**：加登录渠道 = 纯配置，且含协议维度（D26：type=oauth2/oidc/saml，
  标准 OAuth2 加配置即接入 ✓ 已达成；SAML 类协议 = 卡 + 协议策略类）
- **仓库平台轴**：加仓库托管平台 = 纯配置（host/webhook 方言/clone 鉴权/API base），**不要求 OAuth**——
  平台不注册 OAuth app、用户不走 OAuth 登录，纯 PAT/bot token 完整可用

四种合法组合：OAuth+仓库 / 仅 OAuth / 仅仓库（PAT-only，如内部 GitLab）/ 都无。

| 阶段场景 | 前置状态 | 动作 | 预期 | 用例 |
|---|---|---|---|---|
| S12 | PAT-only 平台（无 OAuth app） | 管理员配置 vcs 平台卡（host+方言+鉴权+API base，零 OAuth 字段）；用户手填 PAT | 绑定仓库 → webhook 事件进入 → agent 任务 → git push → 创建 PR 全链路成功，全程零 OAuth 代码路径、external_identities 无记录 | test_s12_pat_only_platform_end2end |
| S12b | 仅登录渠道账号（S12 镜像象限：有登录无仓库，华为第一实例 D102-C） | 用户经纯登录渠道（OAuth OIDC 渠道卡，非仓库平台卡）登录自动建号，名下零代码平台绑定 | 进工作台 → /repos/member | **200 + 空列表 + _no_vcs_binding 标记**（原 400"API Token 未配置"与刚 OAuth 登录完的事实矛盾）；前端空态显示"绑定代码平台"引导 + 去绑定按钮；建号用户名有昵称时昵称派生（sub 形态 external_login 不直接作用户名） | test_d102c_login_only_channel |
| S12c | 字符串 ident 平台的 A2A 移交（D102-G：S70 与 10-14 事故防线裁决） | gitee 仓（issue ident 形如 IKTREU）在需求中心点"移交到其他流水线" | 移交 → /issues/{id}/handoff → 内部直调 /api/a2a | **200 放行**（ident 为合法编号形态）；垃圾输入仍 400（空白/#前缀/标题文本/连字符/超长——10-14 防线不回潮）；redo 查询接口同语义；GRAPE_ISSUE_ID 对 ident 可解析（resolve_issue_card_id 门槛删除） | test_d102g_a2a_string_ident |
| S12d | 引导页绑定卡单路径 + 三态角标（D102-N，2026-11-08 用户裁定——**替代原"双路径并列"口径**） | 新用户进引导页，查看任一仓库平台的绑定状态 | 引导页 → 平台绑定卡片 | 绑定卡只做绑定：logo 右上角三态角标（灰ⓘ=平台未配置 OAuth，title"该平台暂未开通 OAuth 授权，暂时无法绑定。仓库凭据（PAT）可在 用户设置 → 凭据管理 配置"；蓝ⓘ=已开通未绑定，title"已开通 OAuth 授权，点击绑定后可管理该平台仓库"；绿✓=已绑定）；点击绑定 → 弹层双模式（OAuth 授权页签 + PAT 配置表单；OAuth 未配置时弹层内仅 PAT 表单；PAT 写用户凭据 /settings/credentials）；卡面仅 logo+平台名+角标+绑定按钮；三态角标同款（未配置灰标 / 未绑定蓝标 / 已绑定绿✓，title 说明双路径）。**同口径延伸到 用户设置→账号安全 第三方账号行**：三态角标同款（未配置灰标+绑定按钮置灰 / 未绑定蓝标 / 已绑定绿✓）。平台可见性：meta vcs_platforms_hidden 隐藏名单（实例隐藏 gitlab） | test_guide_status_badge + test_guide_shell_renders_cards（沙箱执行 shell：卡片数==平台数、角标、置灰——防"孤儿表达式空白"回潮）+ test_security_tab_bind_states + test_vcs_platforms_hidden + test_d102l_settings_split（裁定更新 v2）+ test_d102k_guide_uses_vcs_catalog |
| S12f | 无仓库平台绑定直达引导页（D102-N，2026-11-08 用户裁定） | 用户解绑了唯一代码平台（或纯登录渠道账号）后进入仓库页 | 仓库页（原"纯登录渠道账号"占位页场景） | `/repos/member` 返回 `_no_vcs_binding` → 前端**直接渲染绑定引导页**（不再渲染占位页+去绑定按钮）；同时清掉 localStorage.user.bindings 登录旧快照（修复解绑后不进引导的时序洞） | test_d102c_login_only_channel（裁定更新） |
| S12g | 凭据页双模式——OAuth / PAT 两源徽章矩阵（D102-N，2026-11-08 用户裁定） | 用户在 用户设置 → 凭据管理 查看第三方仓库凭据行 | 凭据管理 → 第三方仓库凭据行 | 每行双徽章并存：OAuth 徽章四态（灰"平台未配置 OAuth"+入口置灰 / 蓝"未绑定"+入口跳 用户设置→账号安全 / 绿"OAuth 已绑定（生效中）"（无 PAT）/ 绿"OAuth 可用"（PAT 生效时备用源））；PAT 徽章两态（"PAT 生效中"/"PAT 未配置"）；操作=配置/重配 PAT + 清除（PUT 空串显式清除，回落 OAuth 供给）；平台取消 OAuth 配置后按未配置显示、**不静默清除绑定记录** | test_credentials_dual_mode |
| S12h | 华为 OIDC 登录——userinfo 404 走 id_token 兜底（D102-N 缺陷修复） | 用户用华为账号 OAuth 登录（华为 OIDC 无 userinfo 端点，discovery userinfo_endpoint=None） | 登录 → /api/auth/callback/huawei | 回调兜底门槛必须按渠道卡 `type`（或 `channel_type`）键判定 oidc——只读 `channel_type` 会使兜底死代码、华为登录全挂（实弹事故）；兜底=id_token claims 解码（_tokens 复用，不重换 code） | test_d102e2_oidc_gate_type_key + test_d102e_oidc_engine_fallback |
| S12i | git-repo 传输层 issue/PR 编号原生保形——字符串 ident 平台 HTTP 面打通（D102-O，S12c 的 HTTP 契约延伸） | A2A 作业对 gitee 仓（issue ident 形如 IKTREU）回写：评论落地/标签更新/关单/查详情（实弹 Issue #IKTREU：D102-G 只修了流水线进程内入口，HTTP 面仍被 `number: int` 路径校验整族拒 422） | POST/PATCH/GET /api/git-repo/{owner}/{repo}/issues/{number}[...] | **200 放行**——git_repo_api 全部 issue/pulls 路由 number 路径参数改原生保形 str（IKTREU 直达业务代码，URL 原样拼上游）；纯数字平台（gitcode/github #123）行为与改前一致（URL 内形态等价，客户端层仅 f-string 拼 URL）；milestone number 与 comment_id 保持 int（全平台数字，非本批放开面）；VcsApiClient 契约=接收原生形态（D41/S61 _native_issue_number 先例） | test_gitrepo_string_ident_contract（7 用例：评论/详情/关单/标签/PULLS 字符串 ident + gitcode 数字回归锁）+ test_git_repo_api（close_issue 契约更新 "5"） |
| S12j | 账号主键 UUID 化第二批——grape_accounts + 引用列放宽 + 向前兼容锁（D105 第二批，已拍板一次性清完/VARCHAR(36)） | 存量库已有整型 id 账号（'1'/'2'…），grape_accounts 仍 INTEGER AUTOINCREMENT；29 张表 user_id 列、1342 处引用 | 迁移 202611090002 启动执行（safe 幂等）：①存量迁移老 id 字符串化保留零丢失 ②新装库 DDL 直出 TEXT PK ③老 JWT（int 形态 sub）+ 字符串化老账号照常过鉴权 ④user_id 引用列 UUID 字符串写读往返 | test_d105_batch2_account_uuid_pk（迁移保值/幂等/老 int sub 鉴权/引用列往返 4 锁） |
| S12k | 主键生成 UUIDv7 + 集群迁移守则（D105 决策 3 + 第八节） | uuid4 主键无时序；集群滚动升级窗口老实例 int 强转遇 UUID 行 | 新装/新行主键 UUIDv7（version 位=7，uuid4 兜底）；集群迁移挂 GET_LOCK 链互斥、幂等三态判定；滚动窗口操作守则二选一（快速滚动/严格窗口） | test_d105_batch2_account_uuid_pk（v7 version 位断言）+ 设计稿第八节守则 |
| S12l | 存量库归属校验跨类型兼容锁（D105-4 实弹回归） | 存量库 user_id 族 INTEGER 亲和列读回 int，鉴权链 str，混型比较恒不等 | 任意归属/所有权判定 str() 双侧归一（chat/feishu_onboard/settings/grape_auth 17 处），行为锁用存量整型行验证 | test_d105_batch2_account_uuid_pk::test_batch2_ownership_str_int_compat |
| S12m | issue 编号原生保形全链路（D105-5，实弹 IKTVZQ） | gitee 平台 issue 编号为字符串 ident（IKTVZQ），建需求后同步触发流水线被 int(issue_num) 炸掉 | 建单同步触发 payload 原生保形；后端 9 文件 int/isdigit 假设清查（PR/milestone/comment 保持 int 豁免）；SQL 层 0004 迁移混型归一 TEXT；前端 chat.js/observability.js 保形+双键排序；结构守卫防回潮 | test_string_ident_pipeline_trigger_guard + test_issue_number_text_migration（8） |
| S12n | MySQL 迁移实弹 P0——information_schema 探针引号（D105-5 伴随） | MySQL 部署 UuidPkMigration 探针 TABLE_NAME 不带引号 → 1054 → 迁移永不盖章 → 32 表 user_id 留 INT 写 UUID 全 500；conftest 强制 sqlite 致 MySQL 零 CI 覆盖 | 探针加引号；实弹复验口径（head 8f4992900+ 重跑 init_database 应见 0002 盖章+32 表 VARCHAR(36)+api-keys 200）；MySQL 后端可选测试开关立项挂账 | 定向：d105 行为锁全绿；MySQL 实弹复验清单见设计稿第八节 |

| S12e | 双范式并存——凭据来源可见可审计（D102-M，四层模型对账） | 用户已在凭据管理配置 gitee 手动 PAT，同时保有 gitee OAuth 绑定；或仅有 OAuth 绑定未配 PAT | 凭据管理 → 第三方仓库凭据行；agent 消费 token 时 | 行内来源徽章三态（手动 PAT 生效中/OAuth 自动供给中/未配置，判定 Manual>OAuth 与解析序一致）；fetch_user_token 命中来源写 usage_log（user_token_hit_{pat|oauth}）；解析优先级 Manual>OAuth 不回归（D10 拍板） | test_d102m_dual_paradigm |
| S13 | 登录渠道与仓库平台独立变更 | 已运行系统单独新增登录渠道 / 单独新增仓库平台 | 两轴互不干扰：新增登录渠道不影响已有仓库凭据；新增仓库平台不要求动登录配置 | test_s13_two_axes_orthogonal |
| S14 | API 命令层配置化 | gitee 仓库平台卡含 api 段（api_base/auth/dialect/paths/fields），零代码 | 通过 VcsApiClient 走 gitee 调 comment/create_pr/get_pr/add_collaborator/setup_webhook | 全部命令命中配置卡 endpoint，鉴权走 query_token，响应经 field_map 归一化；gitcode/github 既有行为不变（回归） | test_s14_api_commands_config_driven |
| S15 | OAuth 解绑级联 | 用户 A 在 provider X 只有 OAuth 授权（无手配 PAT） | A 解绑 X（或 admin 吊销其 external_identity） | X 域授权即时失效（token 清空，get_user_token_for 返回空——lazy 链断供即失效，D100 语义）；手配 PAT 不受影响；反向：删除 PAT 不动登录绑定 | test_s15_unbind_cascade（用例实测 ei 链，场景描述 D100 对齐改写） |
| S16 | webhook 验签策略分派（W2/D12） | gitee/gitcode/github 平台卡 webhook_verify 就位 | 分别发送错误签名 / 正确签名的 webhook | 错误签名 401 拒绝且区分"缺签名/签名不符"文案；正确签名放行；gitcode/github 现有 HMAC 行为回归不变 | test_s16_webhook_verify_dispatch |
| S17 | webhook 路由动态注册（W4/D14） | 新增 gitee 仓库平台卡（webhook_path=/webhook/gitee） | 服务启动后 POST /webhook/gitee | 路由命中统一 handler，provider 由路径反查；移除卡后路由不再注册；存量 /webhook/gitcode、/webhook/github 别名回归 | test_s17_dynamic_webhook_routes |
| S18 | 平台卡管理面校验+热失效（A1/D14） | admin 管理面就位 | 提交非法 dialect / 重复 host / 冲突 token_env 的卡；再提交合法卡 | 非法卡拒绝并返回具体校验错误；合法卡保存后缓存热失效、新配置即时生效无需重启 | test_s18_platform_card_validation |
| S19 | bot 身份配置化（B2） | gitee 卡含 bot_identity.username | 触发仓库认领 add_collaborator | 使用卡内 bot 账号名授权，gitcode/github 现有 bot 行为回归不变 | test_s19_bot_identity_from_card |
| S20 | webhook 回调 URL 派生（W3/D12） | meta webhook_url=服务基址，gitee 卡 webhook_path 就位 | setup_webhook（gitee 仓库） | 回调 URL = 基址 + /webhook/gitee（非字符串替换历史补丁路径）；gitcode/github 回调 URL 回归不变 | test_s20_webhook_url_from_card |
| S21 | gitee OAuth+仓库全链路（S12 的 OAuth 对照版；D100 对齐：lazy 终态） | gitee 登录渠道卡 + 仓库平台卡齐备，用户未手配 PAT | 用户 gitee OAuth 登录 → 绑仓库 → webhook 事件 → agent 任务 → git push → 创建 PR | 登录即完成仓库域授权（无需手配 PAT）；全链路鉴权走运行时 lazy 链（ei 实时读+按需刷新，**不产生 source=oauth_fill 仓库凭据行——D100 收口**）；token 过期自动刷新无感 | ~~test_s21_gitee_oauth_fill_end2end~~ 未实现（待目标环境验收项，随 D100 语义改写：产物断言从"凭据行存在"改为"lazy 链全绿"） |
| S22 | K4 错位数据检测（删除兜底前置） | 存量库（含/不含双 id 错位数据） | 运行检测工具（随 CI 用例，只读） | 输出错位清单（哪些 user 的凭据将被 K4 删除兜底影响）零改动数据；无错位时输出空清单 | test_s22_k4_mismatch_detect_report_only |
| S23 | K3 工厂：配置即 provider | gitee 平台卡就位（零平台代码） | create_provider("gitee") 并调 verify_token（mock 平台 /user 响应） | 返回 GenericVcsProvider；verify_token 命中卡 api_base+api_auth 并归一 login；PAT 获取走 user_credentials `<p>_pat`（D15 批次 0 存储）；存量三平台 create_provider 返回原类（回归） | test_s23_create_provider_factory |
| S24 | 两轴正交（S13 落地） | gitee 仅有仓库平台卡 | 新增/删除 gitee 的登录渠道卡（oauth_providers_config） | 仓库平台卡解析、webhook 路由、取 PAT 全部不受影响；反向：动仓库卡不动登录渠道 | test_s24_two_axes_orthogonal |
| S25 | 裸 repo_key 跨平台候选集抽象 | gitee 卡就位，用户绑定 gitee 仓库 | 访问该仓库的权限校验/API（check_repo_access 等） | 候选集含全部已配平台前缀（gitcode:/github:/gitee:），权限通过——修复"候选集只认双平台"的功能 bug | test_s25_repo_key_candidates |
| S26 | issue/PR web 链接按卡派生 | gitee 仓库事件流入 | 生成 issue/PR 评论中的平台链接 | 链接 host 来自平台卡（gitee.com/o/r/issues/5），gitcode/github 链接回归不变 | test_s26_issue_web_url_from_card |
| S27 | 前端仓库跳转链接数据驱动 | 前端 catalog 就位 | 点击 gitee 仓库跳转 | 链接 host 来自 /api/settings/vcs/platforms catalog，无平台名分支 | （前端，随 F1 验收） |
| S28 | Issue 核实/标题获取走统一命令层 | gitee 卡就位 | 管道内 issue 存在性核实与标题回写 | 走 VcsApiClient（卡驱动鉴权），gitee 命中 query_token 正确认证；gitcode/github 回归；404→missing 语义不变 | test_s28_issue_fetch_via_client |
| S29 | 分支列表命令 + bot 巡检清单卡驱动 | gitee 卡 + meta global_bot_gitee_token 配置 | 查询 gitee 仓库分支；触发全局 bot token 巡检 | 分支列表走 VcsApiClient.list_branches（卡 endpoint+query_token，scenes 不再错配 gitcode API）；巡检平台自动含配了 token 的 config-only 平台；返回体平台键向后兼容扩展 | test_s29_list_branches / test_s29_bot_health |
| S30 | 登录名变更白名单列取登录渠道卡 | gitee 登录渠道卡（whitelist_column=gitee_username） | 用户 gitee 登录名变更联动白名单 | 列名来自渠道卡（不再 gitcode/github 二元），未配列的平台跳过联动（行为=现状） | test_s30_whitelist_column_from_card |
| S31 | 建 Issue 走统一命令层 | gitee 卡就位 | issues_center 建 Issue（子需求/幂等建档） | VcsApiClient.create_issue（卡驱动+方言 assignee 语义），gitee 自动正确；github/gitcode 存量委托函数回归 | test_s31_create_issue_via_client |
| S10a | 登录生命周期独立（显式化） | 用户 A（gitcode OAuth + gitee PAT） | 改密码（token_version+1 吊销全部 JWT） | PAT 与 OAuth token 可用性不受影响（get_user_token_for 照常返回） | test_s10_password_revocation_repo_creds |
| S11a | 仓库凭据生命周期独立（显式化） | 同上 | 删除 gitee PAT | JWT/OAuth 身份零改动（token_version 不变、identity 照常） | test_s11_pat_delete_login_untouched |
| S32 | 建 Issue 调用方全量接线 | gitee 卡就位 | 发布子单创建 / onboarding 幂等建档 | 两处 _provider 二分支替换为 VcsApiClient.create_issue（存量平台委托不变），gitee 自动正确 | test_s32_create_issue_call_sites |
| S33 | labels 归一方言语语 | gitee 仓库打标签 | 透传 labels 给平台 API | 归一语义按卡方言（github=数组，gitee 系=逗号串），不再 provider 二元 | test_s33_labels_dialect |
| S34 | 跨平台候选集（agents 会话聚合） | gitee 卡就位 | agents 会话查询跨平台 repo_key | 候选集走 repo_key_candidates（含 gitee 前缀），任务/会话聚合不再丢新平台 | test_s34_agents_candidates |
| S35 | 会话 token 列按 provider 派生 | gitee 仓库的 devops_agent_session | agents 会话取 token 列 | 列名来自平台卡 session_token_field（provider 派生），gitee 查 repo_gitee_token | test_s35_session_token_column |
| S36 | agent 运行时注入卡派生 | gitee 卡就位 | agent prompt/环境注入（API base、token 环境变量名、平台名） | 全部来自平台卡（api_base/token_env/name），gitee agent 不再拿 gitcode 的 API base 与空 token 变量名；gitcode/github 回归 | test_s36_agent_env_from_card |
| S37 | 流水线 bot 身份卡消费 | gitee 卡含 bot_username | agent 提交身份解析 | 走 resolve_bot_username 来源链（github/gitcode 全局→卡→host_user 降级），gitee 用卡 bot | test_s37_pipeline_bot_identity |
| S38 | SAML 渠道接入（华为云 SSO，形态 A） | hw_sso 渠道卡（type=saml + IdP 元数据 + 属性映射） | 发起登录 → IdP 回调 → 断言解析 | 归一化身份声明（external_id/login/name/email）→ 走既有 create_external_identity 绑定；external_identities/白名单/JWT 全复用零改动 | test_s38_saml_channel_end2end |
| S39 | 协议策略契约 | 任一渠道 type（oauth2/saml/oidc） | 调用三步契约 | initiate/handle_callback/refresh 接口一致、身份声明结构一致；OAuth2 存量渠道回归 | test_s39_channel_strategy_contract |
| S40 | 协议策略注册表 | 自定义协议渠道 | register_channel_strategy(type, strategy) 后配置对应卡 | 卡 type 命中策略即接入，未注册 type 配置校验拒绝（明确报错）；oauth2 内置 | test_s40_strategy_registry |
| S41 | IdP 元数据轮换 | hw_sso 已接入，IdP 证书更新 | 更新渠道卡元数据 + 缓存热失效 | 新断言验签通过，无需重启；旧证书断言拒绝 | test_s41_idp_metadata_rotation |
| S42 | 断言属性缺失降级 | SAML 断言缺 name/缺 external_id | 归一化身份声明 | external_id 缺失 → 拒绝绑定（明确报错）；name/email 缺失 → 空值降级 + 白名单仍按 login 判定 | test_s42_assertion_attribute_fallback |
| S43 | SSO 身份绑定冲突 | external_id 已绑定账号 A | SSO 用户以同 external_id 登录 | 走既有 OAuth 绑定语义（复用 create_external_identity 的恢复/冲突规则），不新发明冲突逻辑 | test_s43_sso_bind_conflict |
| S44 | OIDC 渠道纯配置接入 | OIDC IdP 渠道卡（type=oidc + 标准端点 + claims 映射） | 走 OIDC 引擎三步契约 | 引擎内置、实例全卡（标准 claims sub/preferred_username/email 映射可配），零平台代码 | test_s44_oidc_engine |
| S45 | 渠道卡字段契约校验 | 提交缺字段的卡（saml 缺 idp_sso_url、oidc 缺端点） | build_channel_strategy 构建 | 引擎 validate_card 在构建时拒绝并报缺失字段名（配置错误前置暴露，不留到运行时 401） | test_s45_card_field_contract |
| S46 | 身份键稳定性校验（D27） | SAML 卡把 emailAddress/transient 类属性配成 external_id；或回调归一时 external_id 值为 email 形态 | build_channel_strategy 构建 / normalize_identity_claims 归一 | 构建时按属性名形态拒绝（报属性名与替换指引）；回调按值拒绝（协议无关兜底）——绑定主键必须是稳定持久标识，email 漂移/transient 每次登录变值都会误建新绑定 | test_s46_email_attribute_as_external_id_rejected / test_s46_email_shaped_external_id_value_rejected |
| S47 | 复合键不变量（D27） | 两个 IdP（不同 provider 名）产出相同 sub/external_id；或未来改动 external_identities 唯一约束 | 归一声明 / DDL 结构 | (provider, external_id) 复合唯一：跨 IdP 同 sub 互不冲突各自绑定；DDL 唯一约束被结构性用例锁死，退化成全局唯一即测试失败。配置纪律：provider 名 = IdP 实例身份，换 IdP 必须换卡名（新 provider），旧卡走 S15 解绑级联 | test_s47_composite_key_two_providers_same_external_id / test_s47_ddl_composite_unique_pinned |
| S48 | 登录路由策略分派接线（D28） | SAML 渠道卡就位（hw_sso）；存量 gitcode/github 渠道 | GET /login/{provider} 发起 / POST /callback/{provider}（SAML ACS） | 路由按策略能力分派（needs_oauth_client / post_callback），不比 type 字符串：SAML 无 client_id 门槛、重定向 IdP（RelayState=state，策略自落 state）；存量 oauth2 渠道委托 build_auth_url 行为零改动（回归锁）；POST 回调公共前置校验链（state 原子消费/provider 一致/nonce）与 GET 入口共用一个 helper；归一声明复用 delete/bind/login 三分支（S43）；未验签断言安全默认拒绝 | test_s48_saml_login_redirect_via_strategy / test_s48_oauth_login_legacy_regression / test_s48_saml_card_invalid_rejected_at_build / test_s48_post_callback_* / test_s48_shared_helper_get_semantics_preserved / test_s48_capability_matrix |
| S49 | 评论投递族卡驱动（D29） | gitee/gitlab 卡就位，agent 执行完成回帖 Issue/PR；或已有 agent 评论（去重） | _post_result_to_issue / _post_subtask_completion_to_parent / _post_all_subtasks_done / _post_result_to_pr / _sync_issue_comments_to_conversation / _fetch_issue_assignee | 全部走 VcsApiClient 卡驱动（URL/鉴权按卡：query_token 系无鉴权头+查询参数、github token 头逐字节保留、gitlab PRIVATE-TOKEN）；PR 评论路径按方言派生（v5→pulls、github→issues、gitlab→merge_requests notes）；provider 台账标签由 repo_key 前缀派生；方案 B 去重语义保持 | test_s49_post_comment_{gitee,github,gitcode}_endpoint / test_s49_pr_comment_dialect_paths / test_s49_list_comments_card / test_s49_post_result_to_issue_{dedup_skip,posts_when_no_agent_comment} / test_s49_provider_of_repo_key |
| S50 | 外围第二批卡化：admin 面与路由解析点（D30） | gitee 卡就位；管理员校验 token / bot token 升级触发；托管路由裸名解析与用户 token 选择 | config_store.verify_token / 升级触发 bot 用户名 / repos.py _provider_from_hint / _get_user_provider_and_token / _provider_from_token | verify_token 走 create_provider().verify_token（卡 api_base+api_auth，任意已配平台可用）；bot 用户名走 resolve_bot_username 来源链（B2）；裸名 provider 解析走 repo_key_candidates 全平台候选（不再双前缀硬编码）；用户 token 选择统一 get_user_token_for（login_provider 任意已配平台优先，github/gitcode 存量顺序保持）；遗留列名/权限字段仍属批次 1 存储 | test_s50_provider_from_hint_{gitee_registered,legacy_regression} / test_s50_get_user_token_platform_order / test_s50_config_store_verify_token_{card_driven,gitee_real_card} / test_s50_resolve_bot_username_gitee_from_card |
| S51 | 身份优先归属 + PR 探测卡驱动（D31，M5 零危害优先解） | 事件/issue 归属反查（注册名 ≠ 平台登录名错位）；PR 详情 API 兜底 | issues_center._get_user_id_by_gitcode / engine_select._repo_user_id / event_pipeline.resolve_pr_issue_id | 归属反查先走 external_identities（平台登录名权威域，provider+external_login 精确匹配活跃绑定，协议无关含第 5 平台），未命中回退既有 unified_user_mapping 名字域链（不迁数据不删表，零危害）；PR 详情 API 兜底走 create_client.get_pr（卡驱动，双平台硬编码 URL 消除）；um 表退役与归属全迁身份域留批次 1 | test_s51_resolve_by_platform_login_{hit,miss_shapes} / test_s51_issues_center_identity_first_um_fallback / test_s51_repo_user_id_identity_first / test_s51_resolve_pr_issue_id_{card_driven,error_response_short_circuits} |
| S52 | 验签头透传 + GitLab overrides 生效 + PAT 泛型入口（D32，先测后改） | 第 5 平台新验签头名；gitee PAT 绑定；卡 event_map_overrides（gitlab 方言） | webhook generic 路由验签头传递 / GitLabDialect.normalize / settings.update_credentials(platform_tokens) | 路由头集合全量透传（方言只认各自头名，白名单过滤是多余封闭点；特征测试锁旧行为：验签失败仍拒）；gitlab 方言 overrides 命中即优先于内置映射；platform_tokens={"gitee": tok} 验证（create_provider().verify_token 卡驱动）后双写 user_credentials gitee_pat + external_identities（D31 归属源打通）；github/gitcode 专用字段路径零改动；未配卡平台 400 fail-closed | test_s52_webhook_headers_passthrough / test_s52_webhook_verify_failure_still_rejected / test_s52_gitlab_event_map_overrides_honored / test_s52_gitlab_builtin_kinds_unchanged / test_s52_platform_tokens_{gitee_pat_entry,conflict_409,omitted_no_side_effects,rejects_unregistered_provider} |
| S53 | 归属热路径索引 + 异常留痕（D33，先测后改） | external_identities 表随用户/平台增长；身份优先块 import 级异常 | db_setup 幂等索引清单 / issues_center._get_user_id_by_gitcode | (provider, external_login) 幂等索引覆盖归属热路径（每 webhook 事件走，原先全表扫）；身份优先失败 log.warning 留痕 + 回退 um 行为不变；测试锁索引存在与回退语义 | test_s53_attribution_index_exists / test_s53_identity_failure_logs_and_falls_back / test_s53_identity_hit_unchanged |
| S54 | 归属链收口：建档写入 + 通知链（D34，先测后改） | issue 首次建档（assignee 有身份行、um 无行）；任务通知（um 平台列空/错位） | session_store._sync_issue_fields 建档 INSERT / issues_center._get_feishu_open_id | 建档 user_id 身份优先（external_identities 命中即权威，um COALESCE 兜底不变）；飞书 open_id 先经身份域解析 user_id 再查（_get_feishu_open_id_by_user_id 复用，um 名字域兜底不变）；异常 log 留痕（D33 纪律）；错位数据的生产端与消费端各收口一处 | test_s54_issue_row_insert_{identity_first,um_fallback_unchanged,no_source_zero} / test_s54_feishu_open_id_{identity_first,um_fallback_unchanged,miss_empty} |
| S55 | 建单链卡化：查重/建单/provider 口径（D35，先测后改） | gitee 卡就位，用户手工建单或 onboarding 建任务 | issues_center._find_open_issue_by_title / 手工建单查重+分派 / create_standard_task 查重 / creator login 身份 provider | 非 github 查重分页统一走卡端点（gitee 命中卡 URL+query_token；gitcode 内置卡对齐原 URL；GitHub Search API #344 语义不变；token 失效抛错不静默）；建单统一 _create_issue_unified（VcsApiClient.create_issue 卡驱动+方言 assignee，_post_github/_post_gitcode 分派收敛）；查重/身份 provider 由 repo_key 前缀派生（gitee 不再折叠 gitcode）；401 刷新重试语义保持 | test_s55_find_open_issue_{gitee_hits_card_endpoint,gitcode_parity,dict_data_shape,github_search_unchanged,token_error_raises} / test_s55_create_issue_unified_{gitee,github_parity} |
| S56 | 查重策略下沉适配器（D36，先测后改） | 平台卡含/不含搜索端点；调用方查重 | issues_center._find_open_issue_by_title（薄委托） / VcsApiClient.find_open_issue_by_title（端口方法） / 平台卡新字段 issue_search | 调用方零平台分支（结构守卫测试锁死，无 _github_req 直调、无 provider 名比较）；策略按卡能力位分派——issue_search=True 走 Search API 精确匹配（github 内置卡置位，#344 语义），False 走分页遍历（gitee/gitcode，终止/形态/401 契约不变）；查重失败 RuntimeError 携带 (NNN) 可分类形态（token_error.is_auth_error_exc 契约，github bot token 401 命中 Bot Token 文案分支） | test_s56_find_open_issue_branchless_structure / test_s56_find_impl_no_provider_literal / test_s56_card_issue_search_field / test_s56_client_error_form_classifiable / S55 全量行为锁 |
| S57 | token 解析二分族收口：用户 token provider 口径 + fail_task 全局 token（D37，先测后改） | gitee 卡就位；agent 执行脱敏 / chat 直连 / 失败回帖 | deps.get_user_token_for_repo（新统一入口） / event_pipeline 脱敏块 / im chat 直连块 / runtime fail_task ×2 | 用户 token 按 repo_key 前缀派生 provider（gitee 不再折叠 gitcode 取空；裸 key=存量默认 gitcode，与 get_user_token_with_refresh 薄委托等价有锁）；fail_task 全局 bot token 走 get_global_bot_token_for（存量平台即原函数包装，gitee 走 meta global_bot_gitee_token）；结构守卫锁死调用方无平台字面量；**通用性原则：调用方不与任何平台名/OAuth 语义耦合——凭据来源（OAuth 身份域/PAT 泛型表）由统一入口内部解析** | test_s57_user_token_for_repo_{provider_derivation,empty_key} / test_s57_redact_and_chat_sites_use_helper / test_s57_fail_task_uses_unified_global_token / test_s57_global_token_wrappers_equivalent / test_s57_user_token_gitcode_parity |
| S58 | bot token 升级链卡化：provider 口径 + 列名卡派生 + webhook 重建端口（D38，先测后改） | gitee 卡就位；管理员手动设置 bot token / 路径 B→A 自动升级 | user/db.update_managed_repo_bot_token / _update_repo_bot_token / _rebuild_webhook_with_bot_token / token_resolver 新端口 setup_repo_webhook | provider 派生走 parse_repo_key（gitee 不折叠 gitcode）；列名走 create_provider().token_storage_field()/webhook_token_field()（内置卡=存量列名，github/gitcode SQL 逐字节等价有锁；插 SQL 前 identifier 正则白名单）；webhook 凭据形态（Gitee 系 password=bot_token / GitHub secret="" / 卡平台 generic）下沉适配器端口 setup_repo_webhook，删旧走 remove_webhook——调用方零平台分支；gitcode/github 委托形态逐参数行为锁；结构守卫 AST 级禁三函数体平台字面量 | test_s58_update_repo_bot_token_{gitee_columns,gitcode_parity,github_parity} / test_s58_related_repos_gitee_token_share / test_s58_update_bot_token_function_columns_gitee / test_s58_rebuild_webhook_{uses_provider_port,gitcode_parity,github_parity} / test_s58_structure_no_platform_literals_in_upgrade_chain / test_s58_provider_has_setup_repo_webhook_port |
| S59 | 仓库平台绑定/解绑域收口：平台集合目录派生 + um 列防折叠 + 升级链端口（D39，先测后改） | gitee 卡就位；用户解绑 gitee 身份 / gitee PAT 绑定写 um / gitee 仓归属兜底 / gitee 建分支缓存 / gitee 路径 B 自动升级 | grape_auth.unbind_provider / repo_key.um_column_for_provider + 7 调用点 / get_host_vcs_username / scenes._branches_cache_invalidate / auto_upgrade_path_b_repos + token_resolver 新端口 accept_collaborator_invite | 解绑合法平台=登录渠道目录∪仓库平台目录（identifier 正则+目录异常 fail-closed，F6 LIKE 前缀安全前提保持）；um 列白名单表，无列平台返回空串且读写两侧全调用方显式跳过（绑定不污染 gitcode_user、解绑不误清、归属兜底跨平台同名误命中消除，兜底 miss=原查询不命中）；host 用户名 provider 口径 parse_repo_key；缓存失效平台集合=仓库平台目录；auto_upgrade host token 统一 get_user_token_for(provider)+协作者邀请/接受走适配器端口（permission 缺省=存量默认，generic 直加平台 accept=True）；接缝迁移旧用例同步更新；结构守卫 AST+源码禁平台比较 | test_s59_unbind_{gitee_with_card,unknown_provider_still_400,gitee_without_card_400,gitcode_parity} / test_s59_um_column_mapping / test_s59_sync_um_platform_col_gitee_no_pollution / test_s59_get_user_id_by_gitcode_no_cross_platform_hit / test_s59_get_host_vcs_username_gitee / test_s59_branches_cache_invalidate_all_platforms / test_s59_auto_upgrade_{gitee_provider_ports,gitcode_parity,github_parity} / test_s59_structure_unbind_and_upgrade_no_literal |

| S60 | oauth_fill 根治：显式链接注册表 + 凭据撤销域服务（D40，先测后改） | 默认目录（gitcode/github 链接成立）；gitee 只配平台卡；用户解绑 gitee | src/auth/provider_link.py（新）oauth_fill_targets/refresh_token_for/register_refresh_implementation / deps._try_refresh_user_token / grape_account_db.revoke_provider_credentials（新）/ grape_auth 解绑路由 | 链接=渠道目录∩平台目录派生（调用点不再字符串相等路由）；刷新能力=注册声明（未注册返回 None，PAT-only 语义显式非 else 兜底），刷新责任全留登录域；凭据撤销域服务拥有键族规则 {p}_oauth/{p}_pat 集中一处，解绑单入口传播（_pat 残留缺口闭合），静默吞错改 loud fail；批次 1 六条验收门槛登记（映射显式/授权最小化/最终一致/短期 token/可审计/撤销闭环） | test_s60_oauth_fill_targets_{default,pat_only_platform_not_linked,linked_when_both_cards} / test_s60_refresh_dispatch_{no_literals,parity} / test_s60_revoke_{clears_all_credential_forms,unknown_provider_no_side_effects} / test_s60_unbind_{gitee_revokes_pat_and_oauth,gitcode_revokes_both} |

| S61 | gitee issue 写路径方言收口：卡能力位 issue_write_form + number 原生形态（D41，先测后改，实弹观测驱动） | gitee PAT + 真实仓建单/查重/关闭 | vcs/api_client.create_issue/close_issue/find_open_issue_by_title / platform_card 新字段 issue_write_form + _native_issue_number | 建单 POST /repos/{owner}/issues+repo 入 body、关单 PATCH /repos/{owner}/issues/{ident}+state=closed+repo+title，按卡能力位派生（repo_path=存量默认零变化）；读路径两形态同形零改动；查重返回 number 平台原生形态（int 仍 int、gitee ident 字符串不炸）；实弹形态录制为 mock 断言（_do_request URL/body 逐字段）；生产 gitee 卡已配 issue_write_form 并实弹全链路绿 | test_s61_card_issue_write_form_default_and_validation / test_s61_create_issue_{gitee_owner_param,gitcode_parity} / test_s61_close_issue_{gitee_owner_param,gitcode_parity} / test_s61_find_issue_number_{native_ident,int_platform_parity} |

| S62 | repos.py 托管主路径收口：36 处 github-else 二分迁工厂+端口（D42，先测后改，用户纠偏驱动） | 非 PAT-only 第 5 平台托管/建仓/解绑/修复/换券/探活全链 | repos.py 12 函数 + token_resolver 新端口 create_repo/find_existing_webhook/fetch_repos_with_role/get_collaborator_permission | host_repo 双流合一（权限归一/bot 协作/路径降级/列名卡派生）；探活卡派生 URL/头 + hooks 404 行为派生（重查仓库本体）；权限闸门合一 _require_vcs_maintainer；展示名/host/目录卡派生；文件级 AST 守卫=平台名比较归零（验收门）；gitcode parity 行为锁 + 端口契约实弹形态 mock；接缝迁移旧用例同步更新 | test_s62_structure_guard_repos_no_platform_compare / test_s62_port_{create_repo_generic_form,create_repo_legacy_delegation,find_existing_webhook_generic,fetch_repos_with_role_generic,legacy_fetch_delegation,get_collaborator_permission} / test_s62_host_repo_{gitcode_parity,gitee_provider_ports} / test_s62_fetch_and_cache_{gitee,gitcode_parity} / test_s62_repo_platform_token_gitee |

| S63 | 凭据/列名族卡化：admin 面 + settings + bot 健康 + 路径 B 查询（D43，先测后改） | gitee 卡就位；管理员换全局 bot token / 保存 PAT / bot 健康巡检 / 路径 B 升级查询 | config_store.global_bot_token_swap + save_config 触发口径 / settings.get_gitcode_repo_config+update_credentials / bot_token_health._verify_single_repo_token / user/db._query_path_b_repos+_path_b_prefix_clause / admin/db.add_login_whitelist | 列名一律卡派生（token_storage_field/webhook_token_field）；权限缺省=适配器默认；PAT 探测 URL/鉴权形态按卡；provider 口径字段名派生；路径 B 前缀子句目录派生（默认平台含裸键=旧 NOT LIKE 语义泛化）；白名单列映射表（未知平台拒绝）；legacy PAT 平台集合常量化；四文件+user/db 三函数 AST 守卫 | test_s63_guard_no_platform_compare_in_credential_family / test_s63_swap_columns_card_driven / test_s63_query_path_b_repos_{gitee,gitcode_parity} / test_s63_bot_token_health_columns / test_s63_settings_repo_config_columns / test_s63_whitelist_{unknown_provider_rejected,gitcode_parity} |

| S64 | gitee 托管硬阻塞修复：平台卡存储列 DDL + add_managed_repo 动态列（D44a，先测后改） | gitee 卡就位；gitee 仓走路径 A（仓库级 bot token） | db_setup._ensure_platform_storage_columns（新，白名单列同款先例） / user/db.add_managed_repo(**extra_cols) | 启动时按仓库平台卡幂等加列（identifier 白名单、已存在跳过、无卡不加）；add_managed_repo 动态列支持（无 extras 时 SQL 逐字节等价，非法列名拒绝返回 False）；批次 1 grape_t_repo_credentials 分表前的列式等价形态 | test_s64_ensure_columns_{adds_gitee,no_card_noop} / test_s64_add_managed_repo_{extra_columns,rejects_illegal_column,no_extras_parity} |

| S65 | 展示/方言回退族收口（D44 批次 1，先测后改） | gitee 卡就位；前端展示/读侧回退路径 | repo_key 方言回退映射 / git_repo_api 无卡回退 / issues.py 冗余折叠 / issues_center auto-close token / runtime/service 过期提示 / 前端品牌 | repo_key dialect 回退→映射表；无卡回退→常量集合；issues.py 冗余 provider 折叠消除；auto-close token 走 TokenResolver 统一链；过期提示平台名卡派生；前端 gitee 品牌红+未知平台通用 git 图标（repos.js ?v= 只升纪律 62→63） | 既有回归集 + 文件级 AST 守卫 offenders=0 |

| S66 | 展示/IM/读侧族 11 处收口（D44 批次 2，先测后改） | gitee/新平台走分支端口/模板/协作者同步/角色判定/台账对账 | scenes list_repo_branches+create_repo_branch / templates ×4 / feishu_async ×2 / auth/repo_roles ×2 / outbound_ledger.extract_event_content_id / VcsApiClient.create_branch+get_readme（新命令）/ token_resolver.list_collaborators（新端口）/ repo_key.um_login_is_grape_account（新白名单）/ 平台卡新能力位 branch_create_form | scenes 分支端点三路（github 手拼/gitcode 存量/config-only）合一为 VcsApiClient 命令层（token 统一 TokenResolver，github 读侧不再走 D32 owner-identity 补丁链=登记语义差）；建分支按卡能力位 branch_create_form（form=Gitee 系 v5 存量 / refs_sha=GitHub 两步存量 / gitlab 参数建）；README 读侧方言模板（/readme→/contents/README.md→小写回退链，token 走卡用户级 env）；templates bot 配置走来源链、显式平台 token 走 D3 入口、建仓+login 走适配器端口；feishu 协作者拉取走 list_collaborators 端口（存量委托保 parity）、um 写入按同源白名单 um_login_is_grape_account（github/gitee 不污染 gitcode_user，FR-21.2）；repo_roles 身份解析统一 _get_platform_login、admin 判定形态归一 _is_admin_permission（dict/str/None fail-closed）；outbound_ledger 内容 ID 按 payload 形态派生（行为派生，provider 参数不参与分支）；mock 接缝迁移（D35.1）：_req→VcsApiClient 命令、_check_*_admin→_is_admin_permission、templates 模块属性→GitCodeProvider 类方法 | test_s66_card_branch_create_form_{default_and_validation,builtin_defaults} / test_s66_create_branch_{gitcode_form_parity,github_refs_sha_parity,github_default_branch_fallback,gitlab_path} / test_s66_get_readme_{github_endpoint,gitcode_contents_fallback,all_miss_empty} / test_s66_templates_{bot_config_source_chain,explicit_provider_token_unified,readme_card_env_token} / test_s66_list_collaborators_port_{gitcode_delegates,github_delegates,generic_card_driven} / test_s66_feishu_sync_mapping_um_branch_by_whitelist / test_s66_um_login_is_grape_account_whitelist / test_s66_repo_roles_{platform_login_unified,admin_check_via_port} / test_s66_extract_content_id_{shape_derived,source_guard} / 接缝迁移回归（test_scenes_branches_cache / test_project_templates / test_issue_491 / test_issue_393） |


| S67 | project-info 后台刷新单飞 + fixture 排空接线（D45 段错误专项，先测后改，faulthandler 驱动） | webhook 事件密集（全量混跑/线上风暴）；两轮全量均 43% SIGSEGV——崩点=fixture teardown `_conn.close()` × 唯一 pi-refresh 线程在 `_write_to_obs` 执行 DB 语句（换出连接被并发 close 的 sqlite C 层 UAF） | src/api/event_pipeline.py 裸 Thread(maybe_refresh_project_info) 每事件一个（944 线程风暴放大竞争窗口） | _spawn_project_info_refresh 单飞+全局并发上限 4+finally 释放；wait_pi_refresh_drain(timeout) 公共排空助手，4 个同族换库 fixture（453_ledger/453_self_loop/comment_resume/bot_opened）teardown 关连接前排空，超时告警不阻断；零危害（跳过/等待即返回），builder 内 TTL 节流+分布式锁兜底不变 | test_s67_same_repo_single_flight / test_s67_concurrency_cap / test_s67_slot_released_after_finish / test_s67_exception_releases_slot + 定向 95 绿 + 全量过 43% 崩点零 Fatal |

| S68 | 混跑污染首批：模块缓存/DB 绑定污染源收口（D46，先测后改，全量对账驱动） | 全量混跑 1430F+711E 而定向单跑全绿；E 族家族=换库泄漏（no such table×239）/模块缓存清空+reload(main)（app ImportError×56+F×75）/私有库 env 永久改写（locked+错库） | test_scenes_detail（sys.modules 全清+reload main）/ test_issue_337（env+DB_PATH 永久改私有库）/ test_recover_orphans_slots（3 表内存库换入无重绑）/ 6 个 env 污染文件 | scenes_detail 缓存清空改空操作+去 reload；337 module teardown 恢复 env+config+reload+重绑扫描；recover_orphans teardown 重绑（先重绑后 close）；6 文件 module 级恢复 fixture；零危害（恢复/重绑，不删不改断言） | 9 文件单跑=基线逐文件一致 + 全量对账：E 711→440、F 1430→1250、通过 4855→5306（原始消除 692，新增 220 大头为 E→F 转化显形）；段错误四轮零复现 |

| S70 | gitee 实弹批次（一）：建单链 4 缺陷收口（D47a，先测后改，服务级实弹驱动） | gitee PAT + 真实仓全流程：绑卡→绑 token→托管→统一建单；建单 400「GitCode Token 未配置」→ 修后 404 → 修后 int() 崩 → 三连环实锤 | src/api/issues_center.create_task_via_issue（token 解析 else 分支） / src/admin/routes.vcs_platforms_put（清洗白名单） / _native_issue_number 接线 ×3 | ①用户 token 按 repo_key 前缀派生（get_user_token_with_refresh→get_user_token_for_repo，401 重试 provider 同步派生，文案平台名卡派生）；②admin 卡清洗白名单补能力位整族（issue_write_form/branch_create_form/存储字段等全字段持久化，卡保存不再静默降级）；③gitee 字符串 ident（"IKSZ3I"）建单链 3 处 int() 崩→统一接 _native_issue_number；mock seam 迁移 6 文件 | test_s70_gitee_user_token_derived / test_s70_gitcode_parity / test_s70_gitee_str_ident_preserved / test_s18_put_capability_fields_roundtrip + 服务级实弹：POST /api/issues 200、gitee 侧 issue IKSZ4V 回读对账 |

| S71 | gitee 实弹批次（二）：webhook 回执链 3 缺陷收口（D47b，先测后改，官方文档+本地模拟投递驱动） | 托管后 webhook_secret 为空、派生列不存在、gitee 投递超时后本地模拟投递暴露验签 403 与事件头 unknown | src/api/db_setup._ensure_platform_storage_columns（盖章迁移跳过） / src/api/routes/repos.repair_host（provider 误派生） / src/vcs/dialects.GitHubDialect（verify+事件头） | ①幂等派生列 DDL 与盖章一次性迁移解耦——init_database 每次启动执行（复现：删列后 init 不补列）；②repair_host 优先从 managed_repos 反查真实 provider 前缀；③gitee 原生投递形态：X-Gitee-Token 密码模式常量时间比对 + 事件头规范化（'Note Hook'→note 等去 Hook 后缀+空格转下划线） | test_s64_columns_created_on_every_boot / test_s70_gitee_native_event_header_forms / test_s70_gitee_token_header_verify + 实测：本地模拟原生投递 200、验签通过、pipeline 派发 issue-agent（gitee 专属工作空间）；repair-host gitee 仓 200 |

| S72 | GitLab 托管链收口（D47c，先测后改，基座形态驱动）+ 工厂注册表化 | GitLabBackend 缺 verify（gitlab 投递 500）；基座 _http 对 private_token 发 Bearer（verify_token 必 401）；协作者/webhook/建仓路径 github 形态对 gitlab 全断（GitLab=/projects/:id 家族、username 字段、members 按 user_id）；create_provider if 链开闭点 | src/vcs/gitlab.GitLabBackend.verify（补齐） / src/token_resolver._http（private_token→PRIVATE-TOKEN 头） / GitLabProvider（新覆盖层：projects 路径/user_name/members/建钩 token+布尔位） / _PROVIDER_OVERRIDES 注册表（if 链→数据） | 支持分层固化：config-only 平台零代码（gitee 样板，卡能力位派生）；方言偏离基座注册一行覆盖类（gitlab）；register_vcs_backend escape hatch 保留。gitee 有意不进注册表=零代码样板 | test_s72_private_token_header / test_s72_get_user_login_gitlab_field / test_s72_webhook_projects_path_and_flags / test_s72_find_and_remove_webhook_projects_path / test_s72_create_repo_projects / test_s72_add_collaborator_member / test_s72_fetch_repos_membership / test_s72_gitcode_github_parity_unchanged / test_s70_gitlab_backend_verify |

| S73 | 同型家族收口（D48，先测后改，全仓扫描驱动）：S70/S64 缺陷形状的剩余实例 | 家族 B：事件管道 int() 强转 issue 号无守卫（event_pipeline 809/1715/5090、issues_center 11 处——gitee 字符串 ident 回流即 ValueError，云层放行后必炸）；家族 A：_ensure_whitelist_columns 被盖章迁移罩住（config-only 渠道后注册→login_whitelist 无 gitee 列，且 whitelist_column 无派生默认）；家族 C：get_user_token_with_refresh 残留 3 点（issues.py release 子单 / runtime/agent chat 注入 / agent/executor 兜底——repo 上下文在作用域却折叠 gitcode） | src/api/event_pipeline.py / src/api/issues_center.py / src/api/db_setup.py / src/api/routes/issues.py / src/runtime/agent.py / src/agent/executor.py | B：统一接 _native_issue_number（/done 确认与等待态恢复去掉 isdigit 门槛，字符串 ident 平台功能恢复）；A：_ensure_whitelist_columns 挪 init_database 每启动执行 + whitelist_column 按 {provider}_username 派生；C：三处改 get_user_token_for_repo（agent/executor 取 task.repo_key）；seam 迁移 test_runtime_agent；挂账：issues_center 附件同步评论仍 gitcode 硬编码（token+provider 双点，另行批次） | test_s73_whitelist_column_created_on_every_boot / test_s73_pipeline_no_bare_int_ident_cast（结构守卫） / test_s73_repo_context_token_sites_use_provider_derivation（结构守卫）+ 回归对账零新增失败（基线 diff 法，实弹服务停机后原序复跑） |

| S74 | 剩余挂账收口（D49，先测后改）：附件同步评论卡化、落库静默失败响亮化、登录渠道管理面 | ①附件同步评论 `if _prov != "gitcode": return False` 把 gitee 等平台整族静默跳过 + token 走 gitcode 专用列；②add_managed_repo 非法列/SQL 失败静默 return False（10 调用点零检查，S64 事故形状）且两处"列不存在降级"分支无日志；③oauth_providers_config 无 admin 路由——config-only 渠道（gitee OAuth）只能直写 DB/文件迁移 | src/api/issues_center.sync_issue_attachment_comment / src/user/db.add_managed_repo / src/admin/routes（新 /auth-channels GET+PUT） | ①去 gitcode 门禁：token 走 get_repo_token_with_refresh（provider 感知四级策略链）、客户端按 provider 构造；②非法列 raise ValueError、SQL 硬失败 raise RuntimeError（调用方全在 try/except 内→异常路由 Issue #671 兜底自愈）；降级分支补响亮日志（兼容未迁移库的有意设计保留）；③登录渠道卡管理面：渠道策略校验（S45 构建即校验报字段名）+ **合并式**保存（不挤掉存量渠道）+ client 凭据写 oauth_{p}_client_id/secret meta + 缓存热失效 | test_s74_attachment_sync_comment_gitee / test_s74_add_managed_repo_raises_on_illegal_column / test_s74_add_managed_repo_raises_on_sql_failure / test_s74_admin_auth_channels_roundtrip（含合并式+凭据不入协议卡断言）/ test_s74_admin_auth_channels_invalid_card_rejected + 回归单文件隔离与基线一致（7 预存量 stale seam） |

| S79 | 身份域 ORM 切换（D54，先测后改，双路径对账驱动）：grape_account_db 7 函数 | 语义保持清单：token 加密存储（DTO 携带 DB 原值=密文）、状态机 0/1/4、create 三分支（新建/活跃重绑/解绑恢复）、update 无条件恢复 status=1、delete require_keep_one 原子检查与清 token | src/auth/grape_account_db（get_external_identity/any_status/get_user_identities/with_tokens/create/delete/update + IdentityAdminRepository 扩展） | GRAPE_PERSISTENCE_ORM=0/1 双路径对账：7 用例逐字段等值（get/create 三分支/delete+keep_one/update 恢复过期/落库密文形态）；评审三缺口落定：①reconcile 静默空集反模式（models 未注册→metadata 空）→ assert 非空；②conftest 删测试库后裸 SQL 工作在幽灵 inode（session_manager.db 旧句柄）→ engine creator+StaticPool 挂接同一连接（ORM 与裸 SQL 同一事务世界）；③DTO 缺字段被函数吞成 None → 对账用例暴露补齐 | test_s79_identity_domain_switch.py 7 用例（get/any_status/user_identities/with_tokens/create 三分支/delete keep_one/update 恢复过期/落库密文）+ S78 回归 |

| S80 | 凭据域单表+双写（D55，先测后改）：grape_t_repo_credentials 收口 managed_repos 平台列家族 | 范围裁定（D53 ADR）："分表"=bot_*/​*_webhook_token ×4 平台共 8 列的列式等价收口——单表 grape_t_repo_credentials（repo_key, cred_type, value），非水平分片（动态表名/ShardedSession/中间件全不需要）；策略=双写（列保留，存量读取方零改动）+ 存量 backfill（幂等只填空）；读取方切换（TokenResolver/managed repo 查询族）归 S80b/批次 1 | src/api/db_setup（safe 迁移建表+backfill_repo_credentials_from_columns）/ src/user/db.add_managed_repo（同事务双写仅凭据列）/ src/persistence（RepoCredentialModel/RepoCredentialDTO/RepoCredentialRepository）/ reconcile._MAPPED_TABLES +grape_t_repo_credentials | 表由 safe 迁移创建（patrol 先例）；双写仅凭据列（webhook_secret/dev_scene 等不进）；Repository CRUD；backfill 幂等（列回填+已有值不覆盖）；schema 对账清单纳入新表 | test_s80_repo_credentials_table_exists / test_s80_add_managed_repo_dual_write / test_s80_dual_write_non_platform_columns_no_rows / test_s80_repo_credential_repository_crud / test_s80_backfill_from_columns_idempotent + 涉改族基线 diff 零新增（6 预存量 372 环境依赖型） |

| S80b | 凭据读取方切换（D55 续，先测后改）：grape_t_repo_credentials 优先 + 列回落（灰度开关） | 两个读取点：①TokenResolver.RepoBotToken._fetch（bot token 策略链第 2 级，按卡 token_storage_field 读列）；②get_webhook_token_by_repo（webhook token 回落链——原只认 gitcode/github 两列，卡平台 {p}_webhook_token 列不在查询面） | src/token_resolver.RepoBotToken._fetch / src/user/db.get_repo_credential_value（新 helper，开关控制）+ get_webhook_token_by_repo 卡平台分支 | flag on：grape_t_repo_credentials 有值优先（双写期两源一致）、空则回落列；flag off：纯存量列语义。回归基线 diff 零新增 | test_s80b_credential_readers.py 3 用例（rc 优先/空回落/webhook 卡平台）+ 涉改族基线 diff 零新增（5 预存量） |

| S81 | 仓库域查询族接 Repository（D56，先测后改）：get_webhook_secret_by_repo_key_any 双路径 | W3 通用面（webhook_secret 反查）+ S77 repair-host provider 反查的底层。Repository 扩展：find_by_full_name_any_provider（LIKE '%:full_name' 等价）/ exists / get_webhook_secret | src/user/db.get_webhook_secret_by_repo_key_any（GRAPE_PERSISTENCE_ORM 分支）/ src/persistence/repositories.ManagedRepoRepository（3 新方法） | flag on 走 Repository（同库同值行为等价锁）；flag off 存量 raw SQL 语义不变（回归锁） | test_s81_repo_domain_reads.py 4 用例（flag on/off 对账 + find_by_full_name 任意 provider + exists/webhook_secret）+ 涉改 11 文件族基线 diff 零新增（9 预存量） |
| S81b | 仓库域单列助手直切（D57，用户拍板不加双路径开关）：managed_repo_exists / get_host_user_id | 两函数为自愈链路（Issue #671）与路径 B 降级链（Issue #372，17 调用点）的判据——行为语义不变，仅数据访问切 Repository（ManagedRepoRepository.exists / DTO.host_user_id） | src/user/db.managed_repo_exists / get_host_user_id | 直接切（无 GRAPE_PERSISTENCE_ORM 分支）：UoW + Repository；语义保持：exists 空键 False、host_user_id 0=路径 A/未配置/不存在 | test_s81b_repo_reads_switch.py 2 用例（exists 存在/不存在/空键；host_user_id 路径B/路径A/不存在/空键）+ 19 文件族基线 diff 零新增（18 预存量） |

| S82 | 双写移除（D58，先测后改，用户拍板方案 B 全量冻结）：grape_t_repo_credentials 唯一权威 | managed_repos 8 平台凭据列冻结（add_managed_repo INSERT 写 ''、UPDATE 不再 SET、降级分支同步）；读取方去 GRAPE_PERSISTENCE_ORM 门（rc 优先无条件，列=历史回落）：RepoBotToken/get_webhook_token_by_repo/get_webhook_verify_token；4 类未切换读者同批收口：bot_token_health 扫描 rc（cred_type→provider 映射）、config_store 全局换发（步骤 3 查 rc JOIN managed_repos、步骤 6 写 rc+保留 webhook_id/bot_username）、deps has_bot 闸门 rc 感知（any_repo_credential_for_repos 新 helper）、repos has_bot 展示/repo_bot_token 读取 rc 回落 | src/user/db.add_managed_repo / get_repo_credential_value / any_repo_credential_for_repos / src/token_resolver.RepoBotToken / src/runtime/bot_token_health / src/admin/config_store.global_bot_token_swap / src/api/deps / src/api/routes/repos | 写冻结断言（8 列全空+rc 权威）；读者去门；health/swap/has_bot 三类读者 rc 感知；评审三挂账处置：空值覆写依赖（回落语义已写死入 D58）、双写原子性（随双写移除消失）、parse_repo_key 内联回潮（登记下批） | test_s82_dualwrite_removal.py 7 用例（写冻结×2/rc 优先无条件/health 扫描/全局换发离线 mock/has_bot rc 感知）+ S80/S80b 旧语义用例同步更新（flag 门移除/backfill 改存量形态造数）+ 涉改族基线 diff 零新增（7 预存量） |
| S101 | repo_key_in_params 原子产物——候选集占位符/参数同源构造（D76，先测后改，外部评审四点采纳） | repo_key.py 新增 repo_key_in_params：一次调用返回 (占位符串, 参数元组)，arity 由构造保证（消灭两次独立调用的卡缓存 TTL 漂移窗口）；空集恒假防御 IN (NULL)（评审抓出 docstring/实现不一致硬伤：IN ('') 会误匹配空串行，否决）；类型收窄/命名校正/调用点拼接记档；守卫=engine_select 强制走 helper 零 candidates 直调 + 全 src 手写占位符×candidates 共存 grep 守卫（白名单只减不增）+ 契约 3 用例 | src/repo_key / src/runtime/engine_select / tests/test_s100_candidates_placeholder_guard.py（+4） | 空集恒假（空串行实测 0 命中）；engine_select 零 candidates 直调；白名单站点逐批替换带空集/单候选回归 | S100+S101 12 绿 + engine_select/s75 族失败集与基线一致（零新增） |
| S102 | repo_manager 存量治理：fail-closed + 卡驱动 + 调用方迁移（D105-17，d105l 设计；用户裁定未知平台 fail-closed 废除 gitcode 静默兜底） | repo_manager 80 处平台字面量：host/bot username/模板源三处静默回退 gitcode（未配卡平台被静默当作 gitcode 操作）；github webhook 函数指针 if 分叉；调用方直调死导入 | _host_of_url 空串不回退+push 前置守卫（不回显含凭据 URL）；resolve_bot_username 未知平台 ValueError（含平台名+配卡指引）；get_hosting_provider_config 卡驱动列名派生（gitcode 与存量列名逐字节一致）无卡 fail-closed；push_template 目标卡 fail-closed 前置；未知模板源 ValueError（list_vcs_platforms 判定）；调用方死导入清理、gitcode-only 直调注释锚定；两处 github 专属比较审阅保留登记 _COMPARE_BASELINE（dialect 无法区分，端口收口归 S62）| src/user/repo_manager.py / src/api/routes/{repos,scenes}.py / tests/{test_repo_manager_governance,test_platform_whitelist_ratchet,test_s88_bot_chain_and_dialect} | 棘轮比较点 8→2（剩余 github 实现绑定审阅冻结）；行为锁 4 绿；家族失败集 diff 零新增 | test_repo_manager_governance + test_platform_whitelist_ratchet + d105l 设计分片 |
| S100 | D16/S25"占位符半截工程"全场景收口（D75，先测后改，外部扫描报告核验采纳） | P0×2+P1×1 一轮统一修：engine_select OR 占位符 candidates 数派生（原写死 3 vs 5 → ProgrammingError → 引擎候选静默空，test_query_scopes 0==1 实锤）；dashboard get_dashboard 9 处 IN (?,?) → _rk_placeholders + L1360 位置解包连带修（主页面全卡片静默空）；skills 歧义守卫 ==2 → >=2 + 动态平台文案（≥3 平台同名仓绕过 strict）；守卫=占位符数==候选数行为锁（真实 sqlite 5 候选）+ 三站点源码守卫；P2 知识读侧/前端二分维持登记 | src/runtime/engine_select / src/api/routes/dashboard+skills / tests/test_s100_candidates_placeholder_guard.py（守卫 3+行为 2） | 默认部署（4 卡 5 候选）占位符严格配对；dashboard 全卡片恢复真实数据；gitcode/github 行为不变（原 3 元组 ⊂ 5 候选） | S100 5 绿 + test_engine_select 5→3 failed（query_scopes 族转绿）+ 涉改族失败集 FAIL-SETS-IDENTICAL（28==28）+ S96-S99 回归绿 |
| S101 | 平台能力显式声明 + 通用链职责边界（D105-14 评审五差距采纳 3.5/5：差距一/二剩余/五；差距四前提错位不采纳——存量四平台走模块是设计主路径非逃逸，熔断对象应为平台 API 失败率另批） | 能力隐式于 resources.rules 缺席（无法查询/校验）；声明的Capability可能无承载（真机验证才发现）；通用链职责边界无显式清单（能力位渗透风险） | 平台卡可选 capabilities 数组（枚举 _VALID_CAPABILITIES fail-closed + 覆盖一致性：声明的 <r>.<o> 必须有 rules 键或 _MODULES 承载，构建期红）；card_capabilities/require_capability 查询与消费侧 fail-closed（未声明卡零影响）；d105i §8.2 通用链禁止事项清单（凭据/重试/权限/存储/org 过滤/副作用六项归位） | tests/test_capabilities_declaration.py（8 用例）| 8 绿 + 家族回归（matrix/s3/onboarding 60 绿）| test_capabilities_declaration + test_platform_card_matrix |
| S99 | um 退役批次 3——IM 通道目录 channel_directory（D74，先测后改，用户拍板方案 B） | feishu/gitcode_nickname 通道数据收敛单一模块（um 列降级私有存储）：bind（app_id 必填关键字成对护栏）/write_back（FR-18.4）/resolve_by_login（FR-20.8/FR-22.5 回退链）/resolve_by_user/resolve_for_login（S54 顺序内置）/reverse_lookup/clear_for_app/clear_for_user（FR-21.10）/set_nickname；六处裸 SQL 读写收敛（issues_center S54 链薄包装、settings 昵称、chat 清空×2、handler_async 反查×2）；依赖方向单向守卫；存储切换批出口条件钉死（回归全绿+后端方案评审+IM 实弹） | src/im/channel_directory（新）/ src/api/issues_center / src/api/routes/settings+chat / src/im/handler_async / tests/test_s99_channel_directory.py（8 用例） | 六类行为零变化；app_id 成对（TypeError 锁）；身份域零反向依赖（用例锁）；update_unified_user_info 残留登记 | S99 8 绿 + 涉改族回归与基线一致（feishu_chat_unified 1 失败=预存测试间污染，同顺序基线复现） |
| S100 | 平台硬编码棘轮扩维 + 模块回退审计 + 卡规则模板插值（D105-14，d105i 设计落地） | provider 单平台字面量比较分叉无守卫（二元 ratchet 不覆盖）；module_for 落 _MODULES 回退零观测；transform 回调是纯 JSON 卡唯一硬墙（拼接类语义必须写模块） | A：test_platform_whitelist_ratchet 扩 _COMPARE_PATTERN 单平台比较扫描（_COMPARE_BASELINE 8 点逐个审阅冻结，只拦 provider 直接比较形态并诚实声明边界）；B：module_for 回退审计 module_fallback_<p>（每 provider 进程内去重防 10000 有界队列洪峰，executor_context 走 sys frame 调用方零侵入）；C：卡规则 template 第四形态（trigger/mode 显式解耦、占位符构建期全量校验∈github 缺省∪卡字段∪_COMMON_TRIGGER_FIELDS、strict 运行时缺数据抛错、同字段 template 与 values/drop/transform 互斥） | src/vcs/platform_card.py / src/vcs/providers/{__init__,base,github}.py / tests/{test_platform_whitelist_ratchet,test_module_fallback_audit,test_s3_card_driven_resources} | 棘轮 RED 注入复验（注入即红/撤销复绿）；审计 3 断言（落一条/去重/纯配卡不落）；模板 8 用例；家族回归 89 绿 | test_module_fallback_audit + test_platform_whitelist_ratchet + test_s3_card_driven_resources（模板 8 用例）|
| S98 | 澄清评论发送统一——关单链同族收口（D73，先测后改） | clarify_issue is_github 二元分叉退役：token 入口按 provider（与 S97 同构：github=get_github_token D32 兜底，其余=get_repo_bot_token rc 权威）；发送统一 VcsApiClient.post_issue_comment（卡驱动方言派生+#453 台账内置）；github 裸调 _github_req 分支退役（同端点同鉴权对齐，错误文案并入"评论失败"族）；issues.py _github_req 依赖清零 | src/api/routes/issues / tests/test_s98_clarify_unified.py（守卫 2+行为 2） | #453 台账/#345 401 提示/TOKEN_NOT_BOUND/会话写入/awaiting_input 恢复不动；gitee 仓澄清不再错发 gitcode.com | S98 4 绿 + test_issue_453/372_round2/s75/tasks_clarify 失败集 FAIL-SETS-IDENTICAL（12 failed 两侧）+ S97 回归绿 |
| S97 | confirm-done 关单链多平台重构——仓库能力抽象（D72，先测后改，用户拍板） | _try_close_issue_for_repo 去 is_github 参数（token 入口按 provider 选函数：github=get_github_token 含 D32 兜底逐字节保持，其余=get_repo_bot_token rc 权威平台通用）；镜像查找 _find_github_mirror_repo_key（github-only）→ _mirror_repo_keys 全平台行（repo_key_candidates 卡派生），原函数薄包装保持 #656 语义；_close_vcs_issues 主平台+镜像全平台逐一尝试，结果字典 legacy 双 key 冻结、新平台增量；bot_token_failed 提示遍历 values() | src/api/routes/issues / src/api/issues_center / tests/test_s97_close_chain_multi_platform.py（守卫 1+行为 4） | gitcode/github 路径逐字节；降级不阻断/权限校验/幂等不动；gitee 绑定仓关单不再静默 skipped | S97 5 绿 + test_issue_209/393/372 失败集与 stash 基线 FAIL-SETS-IDENTICAL + S96 回归绿 |
| S96 | D69"待人工"余量批次——登记站点卡/注册表派生收口 + 合法冻结分类（D71，先测后改） | 收口三件：bot_token_health _PLATFORM_LABELS 字面映射→平台卡 name（卡缺位回退 capitalize）；探活基集 {gitcode,github} 字面→provider 翻译模块注册表 _MODULES；grape_auth 过期检查字面二元组→遍历用户实际绑定平台（gitee 绑定不再漏检，真缺陷形状）；issues 镜像查找循环同走注册表。四项登记合法冻结（webhook alias、repos 优先级序/回退集、关单双平台结果字典归批次 3、settings/config_store legacy 键名）；scenes 探测对实测 S94 已收口 | src/runtime/bot_token_health / src/api/routes/grape_auth / src/api/routes/issues / tests/test_s96_pending_bucket_batch.py（守卫 1+行为 2） | 结构守卫=四站点零字面量残留；gitcode/github 行为逐字节不变；注册新模块自动纳入探活/镜像查找 | S96 3 绿 + S94 aggregation 1 失败=基线预存量（stash 口径）+ 相关族 42 失败/106 过与基线逐项一致 |
| S95 | GRAPE_PERSISTENCE_ORM 开关退役补落（D70，先测后改，S91 挂账补完） | S91 修了 engine 重绑但退役代码只留工作树未提交——HEAD 开关仍存活默认 0。8 守卫站点块级收口为 ORM 唯一路径（grape_account_db 7 处 + user/db 1 处，raw 分支删除，ast 校验）；orm_enabled() 删除；test_s78 默认关断言→退役锁 | src/auth/grape_account_db / src/user.db / src/persistence/repositories+__init__ / tests/test_s78_persistence_skeleton.py | src 零开关引用；身份域 ORM 唯一路径 | S78/S79/S80b/S81/S82 共 28 绿（脏树同套件 12 failed 对照） |
| S94 | observability 候选集统一 + health 聚合动态化（D69，先测后改，"待人工"桶首批） | observability 族 8 处 to_repo_key(repo,"github") 双键集 → repo_key_candidates（gitee 仓观测数据被可见性过滤排除的真缺陷形状）；bot_token_health 聚合循环硬编码双平台 → 遍历 platforms 全集（config-only 不再漏计）+ gc/gh 死赋值移除；守卫=observability 零双键字面量 | src/observability 7 文件 + src/api/routes/observability.py / src/runtime/bot_token_health / tests/test_s94_observability_candidates.py（守卫 2+行为 1） | gitee 仓进可见性过滤；聚合含 config-only；D19 巡检语义保持 | S94 3 绿 + bot_token_health 17 失败=基线预存量（stash 口径）+ S92/S84 回归绿 |

| S92 | um 平台归属列运行时停写（D68，先测后改，um 退役第一步） | um=两表粘合：平台归属列（身份域喂养，运行时写冗余）+ feishu/昵称列（身份域无对应物，保留）；_sync_um_platform_col no-op 化、prompt_builder/settings/session_manager 平台列写停（feishu/nickname 保留）、解绑清空保留、启动回填桥保留（守卫断言）；S59 污染用例语义更新 | src/auth/grape_account_db / src/prompt_builder / src/api/routes/settings ×2 / src/session_manager / tests/test_s92_um_platform_write_retire.py（行为 2+守卫 1） | 绑定后 um.gitcode_user 冻结；解绑清空保留；守卫=运行时写清零+回填桥存在；S59 污染语义更新 | S92 3 绿 + S59/S60/D31/D34/user_db 回归 71 绿 |
| S93 | 绑定域解耦收口：PAT/OAuth 写入路径+供给+存量搬家（D105k，先诊后改，用户三轮纠偏） | uid=24 gitcode 行"已绑定 来源：PAT"+解绑误显示（历史专用路径把 PAT 写进 ei.access_token，has_oauth_token=非空推断误判）；回填首档 json.loads NameError 被吞从未生效；ORM 引擎 URL 未编码凭据（密码含 @ → 全部 _uow 身份域写入现网 MySQL 连不上，59 实锤 P1） | settings.py 专用字段路径改 v6 形态（PAT 只进凭据域 <p>_pat，ei 归属行 token 空，不清 OAuth 回落 token——OAuth+PAT 共存能力锁）；get_user_token_for 头部加 ①凭据域 PAT 层（与 fetch_user_token 单序一致）；backfill 改搬家语义（等值副本→清 ei／OAuth 痕迹→保留／渠道未配置（client_id 判据，卡在键不算）→搬家／歧义→保守保留；落写走全局连接裸 SQL）；_oauth_channel_configured 改 client_id 实配判据；persistence/engine.py quote_plus 编码；_str NameError 修复 | test_d105k_bind_provenance（9）+ test_security_tab_bind_states（重写，D105k 三态）+ test_uc38 门槛清单 |
| S103 | 凭据级 workflows 写能力三态（D105-18，D105-17 审阅保留收窄） | 用户在凭据 tab 配平台 PAT | 保存时对象形态声明 workflows_write（true/false/不声明三态，旧前端字符串形态=未声明）；保存后创建含 .github/workflows 的模板仓库 | true → push 不删（github 平台同样生效）；显式 false → 删；未声明 → 回退平台名旧逻辑（github 删/gitcode 不删，存量零回归）；OAuth 路径 → 保守删；get_credentials 回显 workflows_write 供凭据 tab 判定 | test_d105m_workflows_capability（6）+ test_s103_workflows_capability_scenarios（4）
| S104 | 审计续审九轮收口：org 哨兵族场景锁（D105-18 续，F4 固化） | managed_repos.org_id 持 D105-15 TEXT 保值后形态（'' =未归属，0/'0'=历史哨兵） | 创建需求时解析归属组织：repo 关联 org=0 / org='0' / 真实 org / 无托管记录；组织级 explicit=31/0/'' | 0 与 '0' 与无记录一律出 ''（哨兵跨维度统一）；真实归属原值返回（'7'）；explicit 31 透传（==31 契约不 str 化）、0/'' 严格 ==''（round-11 收紧，无宽容 in-元组形态） | test_s104_issue_org_sentinel（5）+ test_issue_org_attribution::TestResolveIssueOrgId（回归）
| S105 | 已删兜底退役锁（D105-18 续二，审计 round-10 遗留 RED 清偿） | D3 有意删除 _get_gitcode_credential_by_gitcode_id 兜底（跨 user_id 凭据匹配收口进现役供给链） | 测试套件运行 | 退役锁断言 hasattr=False——兜底回潮即 RED；悬空引用用例（连续 4 轮 RED）清零，分支 RED 归零 | test_issue_org_attribution::TestGetGitcodeCredByGitcodeId（退役锁）
| S106 | 登录渠道配置解耦：oauth-providers.json 文件真源 + 与仓库托管配置分层（D108，2026-11-13） | 仓库侧平台卡已是文件真源（S83/D59），登录侧渠道卡仍硬编码在 providers._DEFAULT_PROVIDERS（文件内还重复定义两份，后者覆盖前者）；example 只有 gitcode/github（gitee/华为无样例）且迁移是整块覆盖（建文件重启会抹掉 DB 渠道卡）；真文件被 .gitignore 忽略（真源化后新部署无渠道卡、登录全断）；Gitee 每次弹授权页根因=scope 含非法 emails（Gitee 按请求 scope 与上次授权范围一致判定是否跳过授权页） | ①渠道卡迁 configs/oauth-providers.json（一份聚合、四渠道、无凭据），优先级 meta 逐键合并 > 文件 > 无（fail-closed，不放回代码兜底）；②删两份代码字面量 + 加载器（未知键整卡拒绝/缺三端点拒绝/凭据剥离告警/下划线文档键跳过）；③管理面保存基线改读文件卡；④.gitignore 移除该忽略项（与 platforms/*.json 同口径）；⑤example 补 _readme/_modes/每渠道 _note；⑥分层守卫（两类卡字段互不渗透）；⑦Gitee scope=user_info projects、pkce=false，补华为渠道卡。文件：configs/oauth-providers.json、configs/oauth-providers.example.json、src/domains/mgmt/auth/providers.py、src/domains/mgmt/admin/routes.py、.gitignore | 定向 43 passed（含 11 条新守卫）；Gitee scope ⊆ 官方合法集；gitee/华为 pkce=false；华为卡经构建校验；OAuth 卡含托管字段、平台卡含 OAuth 字段、配置文件含凭据三类越界均 RED；example 可直接复制成真文件加载。测试：test_oauth_channel_consistency（11）+ test_channel_strategy / test_d28_channel_login_wiring / test_s74_family_closure（回归 32） |
| S107 | 平台对象模型：三类配置 / 五张表 / 派生能力视图 + 用户级平台关系（D109，2026-11-13） | ①configs/platforms/*.json 职责被放大（方言 + 凭据 + 身份/展示混装），"用哪种凭据"无独立配置位；②登录渠道与仓库平台是两个平行集合，靠同名文件隐式关联，无 grape_t_channel_repo_link / grape_t_user_repo_platform_link；③"是否支持仓库"有 4 套判据（list_vcs_platforms / repos.py:141 模块位 / repo_manager:1146 已知集合 / 前端），实锤 gitlab=configured:true+hosting:false+login:false；④平台可见性=平台卡在册即全可见（settings.py:2428 注释原文），无用户维度；⑤身份令牌可做仓库操作（deps.py:560 同函数两域 + token_resolver.py:438 ①PAT②OAuth 隐式序），D108-B 曾用 env 开关堵（绕行口） | ①三类配置：oauth-providers.json（怎么登录）/ platforms/*.json（怎么说话，含 pat_create_url 等原 caps 字段）/ channel-repo-links.json（谁能用谁；文件缺席→按 provider 同名默认，存在即权威）；②五张表：grape_t_oauth_channels / grape_t_repo_platforms / grape_t_channel_repo_link / grape_t_user_repo_platform_link（纯运行时，无配置来源）+ 派生能力视图（login / login_supported（第一批恒等 login）/ configured / hosting  / supports_bot_token / supports_webhook_secret / bot_username，无 credential_drivers），hosting 不落表；③用户关系=**纯对应关系表**（2026-11-14 裁定，取代 via_bits 生命周期表）：grape_t_user_repo_platform_link（改名，体现"仓库平台"）只存 user_id/repo_provider/linked_at/revoked_at/revoked_reason，**不存 status、不存来源渠道、不存可用性布尔**；关系=准入清单（收窄），可用性读取时现算（有 PAT(p) 或 ∃活跃渠道∧link(c→p)）；写入三路=OAuth 登录 / OAuth 绑定（2026-11-14 补，此前绑定路径漏写）/ PAT 保存（不经渠道直插）；撤销写 revoked_at+reason（user_unbind/credential_deleted/oauth_unbound/admin/disabled_by_user）；④**第一批必带数据回填迁移**：凭据域 PAT → 关系行；ei 的 (user,oauth_provider) 按 link 展开 → 关系行；幂等 upsert + 回填前后对账；⑤取 token 链保持三层（d101 §二 契约），只改 UserToken 内部：PAT → oauth_identity（第一批桩）→ NO_REPO_CREDENTIAL；⑥split mode：定义字段配置真源、enabled 归 DB、行级 config_hash、orphaned 软删与"无视 hash"的恢复、drifted 单义、meta 两键降级、双向导入规则（凭据能力有方言卡无→拒绝；反之→默认补齐）；⑦GRAPE_OAUTH_TOKEN_FALLBACK 降为运维硬关（=0 才禁 OAuth 驱动仓库；默认按关系表放行）；⑧判据收敛两处 + 对拍；⑨provider 命名规则（^[a-z][a-z0-9_-]{1,31}$ / 小写 / 禁保留字 / 与目录名+oauth key+repo_key 前缀+webhook_path 末段一致）；⑩字段值只准取真实枚举（§13 为唯一校验规则来源）；⑪link 目标存在性校验（显式声明缺失→导入 error；文件缺席时同名默认配对，无同名平台不派生，纯登录渠道无幽灵 link）；⑫一个 provider 一种方言（多方言拆卡，禁止数组）；⑬过渡期护栏=基线+增量棘轮（新增旧判据调用即红）；⑭非标准协议渠道（SAML/非标准扫码）需在 channel_strategy 注册策略；⑮**生效路径统一**：三类配置运行时真源=DB 表、文件仅导入源、删除文件 TTL 热重载（实查两类现为 300s TTL+主动失效）、行级 hash + 显式导入/dry-run、文件改不导入则不生效但 drifted 可见、meta 一次性迁移置 migrated 后不再逐键覆盖；⑯**配置初始化=启动扫描：按规范化内容 hash（非 mtime）逐 provider 判定 导入/跳过/orphaned/manual 跳过/恢复，日志五项，strict 可拒启动；配置文件只在初始化/显式导入时有效，运行时只读 DB；集群共享 DB + meta.config_state.epoch 跨实例失效（导入即全集群生效，多实例启动靠导入锁+幂等不重复导入）**；⑰字段归属裁定（2026-11-14 收敛）：token_storage_field/webhook_token_field/owner_fallback/pat_create_url 与 legacy_token_symbol/workflows_write 一律留在平台卡（repo-credentials.json 已删除，字段并入 grape_t_repo_platforms）；⑱**bot 三层分层**：能力位由账号事实派生（有账号即 supports_bot_token，无配置声明），账号与 token 进 grape_t_bot_accounts 对象（bot_identity_mode 取消）；职责收敛：grape_t_bot_accounts=平台账号目录（定义+tokens）、managed_repos.bot_username=仓库绑定、is_default=平台默认；身份解析=绑定>默认>无（第二批池选号）；git 签名与统计名单均从 grape_t_bot_accounts 派生（取绑定账号；env BOT_USERNAME 仅显式覆盖，删 grape_bot 兜底）；token 优先级=仓库级 grape_t_repo_credentials > grape_t_bot_accounts（加密，不落 meta、不散键名）；fail-closed 不变。⑲bot 归属与快照：bot 归 repo_provider（平台卡只声明 supports_bot_token、grape_t_bot_accounts 存账号、managed_repos 存绑定、oauth 不持有）；仓库级 bot=全局默认账号快照（托管时复制 username+token，routes/repos.py:911），补 grape_t_repo_credentials.derived_from（provider:username，空=用户自定义）做轮换精确传播；bot 全量入库清单（含历史别名 gitcode_api_token / github_bot_token、单值 username 键、gitcode 池、状态键、四平台 oauth client_id/secret(→oauth_channels 加密列)、redirect_uri，迁后旧键停读+迁移标记）/ 全局 bot 五场景（托管/webhook、评论兜底、巡检、self-loop、统计名单）统一由 is_default 账号服务，降级=其他 active 账号→仓库快照→无。㉖**凭据代理模式**（2026-11-14 裁定 + 同日简化）：凭据不在关系表、不在绑定时指定、不挂 managed_repos；统一门面 credential_resolver 按 (user_id, repo_provider, need_scope) 现算，返回 (token, source, reason)；解析序=①PAT（优先，显式意图）②渠道令牌（**只要有配对**：活跃渠道 ∧ channel-repo-links 里有该 (c→p)；relation 档位/drivers/scope/audience 一律不参与判定）③空+原因码（NO_PAT/NO_CHANNEL/LINK_MISSING/DRIVER_NOT_DECLARED/AUDIENCE_MISMATCH/TOKEN_EXPIRED）；**OAuth 仓库能力保留**（关系=identity_and_credentials 即可用，scope 由上游回答；收口对象是"无声明静默借用"，不是禁用）；存量四平台默认声明 drivers=[pat,oauth_identity]+oauth_requires_scope（迁移零感知）；source 写审计；缓存按 epoch 失效；bot 三层链序与回落不变（**无 bot 时用户 PAT/个人 OAuth 照常驱动托管动作，能力保留**）；actor 与凭据分维（审计记清用了谁的令牌、以谁的名义），两者不得互相冒充；㉗**仓库侧配置登录渠道 + link 的 DB 权威**（2026-11-14 裁定）：管理台入口改为「仓库平台卡 → 接受哪些登录渠道的令牌」，底层仍 grape_t_channel_repo_link(oauth_provider, repo_provider, relation)（键对称）；导入规则由全量替换（importer.py:401 replace_for_oauth）改为**合并、manual 优先**（表无配置有→插入；config/derived 且配置有→更新；config/derived 且配置无→删除；**manual 行无论配置有无都保留**），管理面写 link 标 source=manual；孤儿 link（目标平台已删/停用）告警但不自动删；㉘**建表 DDL 的 MySQL 方言适配**（2026-11-14 实锤修正）：曾用 executescript（sqlite3 专有，绕过 mysql_compat）+ MySQL 分支 return 跳过建表 → 迁移 0ms 照样盖章、六表未建、版本号盖死；改为**逐条 cursor.execute**（走翻译层，对齐 db_setup.py:4788/5021）；唯一不兼容=bot 部分唯一索引 → MySQL 用 STORED 生成列 is_default_uniq + 复合唯一键（NULL 不参与唯一）；补列 MySQL 用 VARCHAR(32) DEFAULT ''；实测 MySQL 8.0.46 五表 rc=0、盖章 164ms、启动导入 changed=12、管理面写接口 200；守卫 test_d109_ddl_dialect（反证 executescript 即红）；㉕**场景用例与影响评估**：设计分片 §15 列 44 个 UC（40–44=关系即判据/渠道关系独立配置文件）（导入/扫描/多实例/用户关系/纯登录渠道无幽灵 link/bot 选号轮换巡检/token 链/迁移回填/strict/双判据；28–34=绑定写关系/PAT-only 用户可见/代理优先级/scope 原因码/OAuth 保留/link manual 优先/用户禁用平台），§16 列 12 项影响，4 个高等级（启动时序=异步初始化 vs 读表、读路径 64+19+25 处容错、凭据迁移不可逆、用户可见性收紧）均为第一批上线前置条件；决策 A 修正为 meta 停读+保留旧值一版（回滚可用），物理删除放第二批。㉔**领域建模与持久化**：新表一律走 D53 持久化层（SQLAlchemy 2.0 + UoW + Repository + DTO），五聚合（OAuthChannel/RepoPlatform/BotAccount/UserPlatformLink/ChannelRepoLink）+ 值对象（ViaBits/ClientSecret/Dialect/Relation）；状态计算在领域对象（status() 读取算），事务边界=UoW（导入/回调/PAT/撤销/轮换各一个）；主键 uuid7_str；建表仍 db_setup+register_migration、ORM 只映射不 create_all；凭据列 ORM 层自动加解密；不新增裸 SQL（存量 2809 处不迁）。㉓**四项裁定**：①凭据进表=决策 A（client_id/secret 与 bot token 从 meta 迁入表加密列，迁完 meta 废弃+迁移标志，不做并存双源）；②不新建平台级 webhook secret 表（仓库级 grape_t_repo_credentials('webhook') 已权威，平台级共享更弱）；③epoch=表状态版本号（任何表写入同事务+1，读取前比对刷新；与 config_hash 分工：hash=行级内容指纹答 drifted，epoch=表级版本答缓存重载，不冗余）；④运行时参数落点=现有 meta + config.py getter（env>meta>runtime-config.json>默认），不新建 settings 表；⑤兼容层删除门=四条件 AND（grep 读取清零+迁移标志齐备且生产自检+一个发布周期无回滚需求+配置文件已删），未满足前兼容层只读不写，删除放第二批；⑥凭据加密=AES-256-GCM+多 kid（security.credential_keys > JWT_SECRET 派生(单kid不可轮换) > dev 零密钥仅测试），不引 Vault/KMS，将来接 KMS 只换 _get_current_key。㉒**配置统一=结构统一（非语义统一）**：统一对象结构/写入入口/命名（消除模板 12 键·ConfigBody 16 键·代码 26 键三层不一致）；不统一方言字段、能力位、端点（强统一=假一致，如 Gitee token_compare 当 hmac）；管理面对象式 CRUD + 旧扁平 ConfigBody 兼容一版后删；运行时参数走系统设置域。㉑废弃 configs/global-bot-token.json：全字段进表（bot→bot_accounts、webhook_token→grape_t_repo_credentials('webhook')（仓库级）、oauth client→oauth_channels 加密列）、webhook_url 改派生（server_base_url+平台 webhook_path，不落表）、代码停读 _GPT_PATH 加 grep 守卫、文件第二批删除、管理面入参名留兼容别名。⑳**建表规范：grape_t_ 前缀 + UUID 主键（uuid4().hex，禁自增）+ created_at/updated_at + 每字段注释，DDL=d109-schema.sql**；分层纪律：配置派生（四张定义/关联表，导入可覆盖）与运行态（grape_t_bot_accounts / grape_t_user_repo_platform_link / 凭据域）必须分表，导入器按表白名单工作；repo-credentials 的 bot_username 是「配置声明（1:1）」，grape_t_bot_accounts 是「运行态实例（N:1）」，两者非重复；校验选 A 弱校验（导入不查 DB，运行时不一致告警不阻断，避免冷启动死结）；能力视图的 grape_t_bot_accounts 摘要不含 token。扩展 checklist 见分片 §14。落点：db_setup 7 表 + 回填迁移 / 四类配置解析与导入服务 / 能力视图 / token_resolver UserToken / deps 删开关 / grape_auth 回调 + settings 凭据 + 撤销写关系 / repos.py + repo_manager.py 判据 | 导入幂等（连跑两次无 diff）、enabled 不被配置冲回、orphaned 软删与"内容未变加回仍能恢复"、双向导入规则、行级 config_hash、能力视图字段完整（gitee TTT+drivers / gitlab FTF / huawei TFF）、用户关系=纯对应关系（登录写/绑定写/PAT 直写三条路各自建行；撤销写 revoked_at；可用性由代理层现算不落表）；代理层四例（PAT 优先 source=pat / 无 PAT 时渠道令牌 source=oauth:<c> / 无渠道给 NO_CHANNEL / 过期给 TOKEN_EXPIRED）；link manual 优先（导入不覆盖 manual、config 行文件删行即清理）；MySQL 五表 rc=0 + 盖章非 0ms + 管理面写接口 200、回填后无用户可见平台集合缩小、枚举非法值整卡拒绝、无 PAT 且桩不可用→NO_REPO_CREDENTIAL、bot 两层不变、link 唯一性导入期报错、旧判据对拍一致、三类配置 drifted 均可查、字段无双源（grep 守卫）、纯登录渠道无幽灵 link、过渡期新增平台双判据一致。测试：test_d109_platform_object_model / test_d109_import_split_mode / test_d109_read_path / test_d109_admin_object_api / test_d109_credential_migration / test_d109_dual_predicate / test_d109_startup_wiring / test_d109_ddl_dialect / test_d109_credential_resolver / test_d109_user_repo_platform_link / test_oauth_token_fallback_gate + test_s118 / test_s119 / test_s127 回归 |
| S108 | 仓库驱动判据收敛为“关系配置”+ 渠道关系独立配置文件（D109 续，2026-11-14 用户拍板） | ①驱动判据链过长：relation 档位 × drivers × scope × audience 四层与事实/上游能力纠缠，配置改一处不生效、403 归因困难；②relation 档位语义（identity_only/identity_and_credentials）与实际需求不符——“只作登录身份”就是没配配对；③登录/绑定建关系行依赖档位+drivers 双重判据，gitee/github 同域登录不建行 | ①渠道↔平台关系独立成 `configs/channel-repo-links.json`（links[]：oauth_provider / repo_provider，**无档位**）；②判据只有一条：配了配对 ⇒ 该渠道令牌可驱动该平台、登录即建用户平台关系；没配 ⇒ 不驱动不建行；③relation 档位 / drivers / scope / audience 退出判定：credential_resolver 与 hooks 只查配对存在性；④GRAPE_OAUTH_TOKEN_FALLBACK 降为运维硬关（=0 才禁）；⑤表列 relation 保留仅 DB 兼容，读路径不读；⑥`configs/repo-credentials.json` 删除，caps 表存活字段并入平台表并删表（迁移 202611170010） | configs/channel-repo-links.json、src/domains/platform/{importer,hooks,credential_resolver}.py、src/api/deps.py、src/kernel/persistence/repositories_platform_object.py、docs d109 §3/§5/§6/§15/§18/§19 | 配对存在即驱动（UC-40）/ 未配对不建行（UC-41）/ 无 PAT 用渠道令牌且 scope 不判（UC-42）/ 硬关=0 全覆盖（UC-43）/ 文件缺席同名默认、存在即权威（UC-44）；d109 套件 171 passed（15 条失败全为基线红，失败集与基线逐条同名） |
| S109 | 档位列彻底退役 + 清 PAT 引导门二次收口 + 管理面列表/详情收敛（D109 续二，2026-11-14 用户拍板） | ①第一批只做到「读路径不读 relation」，列/枚举/请求体/管理面控件仍在，出现「代码没判定、数据还有列、界面还让人选」三重不一致；②用户实测「清空全部 PAT 且无 OAuth」仍落项目页：`_bootRestore` 先用 URL 深链/残留 currentRepo 恢复仓库并 `nav('dash')`，绕过关系门；`_hasVcsBinding` 在关系快照缺失时回落 identities（Vue 线无平台目录 → 任意登录身份都算绑定）；账号页清 PAT 后不刷新关系快照 | ①relation 全链删除：DDL/ORM/Repository/`domain.Relation`+`allows_credentials`/`ChannelRepoLinkBody`/管理面控件；`ChannelRepoLinkRepo.set_manual(s,c,p)` 两参；迁移 202611170011 幂等 DROP COLUMN（cleanup 语义 + min_code_version 门槛）；②引导门：`_bootRestore` 无关系→清 currentRepo→goToRepos；`_hasVcsBinding` 只认 table/identities 快照的 has_any，快照缺失按无关系；`clearPat()` 成功后 `refreshRepoLinks()` + 无关系即落引导页；③管理面：登录渠道/仓库平台改「列表 → 详情」，平台详情补 bot 账号增/轮换/删（`PUT|DELETE /bot-accounts/{p}/{u}`，token 空=不改）与逐平台 token 健康，渠道详情只显示配对+来源；④平台凭据页只留 webhook_url + 运行参数，退役 phToken/oauthPh/verifyToken/loadTokenHealth/health* 等辅助代码 | src/domains/platform/domain.py、src/kernel/persistence/{models_platform_object,repositories_platform_object}.py、src/domains/mgmt/admin/{channel_repo_link_routes,oauth_channel_routes,api_common}.py、src/api/{db_setup,platform_object_ddl}.py、src/api/frontend/shell/js/app.js、src/api/frontend/app/src/views/{AdminView,AdminChannels,AdminPlatforms,AccountView}.vue、docs d109 §4/§5/§20 | 档位列归零（PRAGMA/information_schema 无 relation；`Relation`/`allows_credentials` 符号不存在）；配对即授权（`test_link_pair_is_the_only_criterion`）；清 PAT → `/auth/platform-links` has_any=false 且前端落引导页（`tests/js/test_boot_gate_no_relation.js` + `test_app_deeplink.js` 7k/7l）；快照缺失按无关系；bot 账号 PUT/DELETE 实弹 200、渠道配对空体 PUT 落 source=manual；d109/管理面定向 143 passed（3 条 platform_whitelist_ratchet 为基线红同名） |
| S110 | 同域配对隐式化 + 管理台术语/文案收口（D109 续三，2026-11-14 用户裁定 + 外部评审意见） | ①第一批「配对即授权」要求渠道给自己的平台也配一条 (c→c)：不表达新能力，界面读出「GitCode 可驱动 GitCode」，UI 隐藏后成「配了看不到也删不掉」的垃圾数据；②术语歧义：外部评审指出「仓库平台」易读成仓储、「可驱动」在技术语境强指硬件驱动；③管理台文案改动后新词条空白——语言包缓存键未随批 bump；④列表→详情切换点了没反应（`current` 是 computed，赋值被 Vue 忽略） | ①同域**隐式**：`hooks._targets()` = 跨名配对 ∪ 渠道自身（渠道在平台在册时）；`_default_links` 不再派生同域行，显式 `(c→c)` 告警忽略；管理面 `PUT /channel-repo-links/{c}/{p}` 同域返回 422 `SAME_DOMAIN_PAIR`；迁移 202611170012 删存量同域行（backfill 语义）；②术语：仓库平台→**代码托管平台**、可驱动的仓库平台→**可接入的代码托管平台**；「登录渠道」保留（与 `grape_t_oauth_channels`/`/admin/oauth-channels`/`OAuthChannel` 同名），只把说明改成「查看已支持的登录渠道并可更新各渠道配置」；代码标识符不动；③版本位纪律：view bundle 与 `I18N.init` 语言包键同批 bump（本批 v=29 / 20261114-10）；④返回列表改为 `back()` 清 `selected` ref | configs/channel-repo-links.json、src/domains/platform/{hooks,importer}.py、src/domains/mgmt/admin/{channel_repo_link_routes,repo_platform_routes,user_repo_platform_link_routes}.py、src/api/db_setup.py（202611170012）、src/api/frontend/shell/{admin.html,js/i18n.js,locales/*/admin.json}、src/api/frontend/app/src/views/{AdminChannels,AdminPlatforms}.vue、docs d109 §20.6-§20.9 | 同域不配也建关系（UC-51）/ 显式同域 422（UC-52）/ 导入不造同域行（UC-53）/ 文案中英一致 + 新词条不空白（UC-54）；线上实测 4 渠道 `repo_links=[]`、同域行 0、admin v=29；定向 85 passed |
| S111 | 托管平台判定与 i18n/迁移收口（第五批，2026-11-14 用户实弹） | ①gitee 仓库托管失败（日志实锤用 GitHub token 查 gitee 仓库 → 404）：前端 provider 只认 github/gitcode 前缀、后端无线索时按 token 顺序兜底挑到 github；②Gitee v5 用 namespace 表示归属，owner 解析落空 → 「我明明是管理员」读成 Member；③旧壳按「非 github 即 gitcode」二分，gitee 仓库被归到 GitCode 分组、前缀不剥；④线上迁移 202611170010 盖章空转（5 列没建、caps 表还在），ORM 读平台表恒抛 Unknown column → 管理面静默退回文件兜底；⑤语言切换 en→zh 不重渲染（setLang 在 loadPacks 之前派发事件，之后无二次派发）、i18n.js 脚本 URL 版本位长期未 bump 导致中英互换 | ①`providerFromRepoKey()`（前缀即平台）+ 后端 `_provider_from_user_repos()`（缓存唯一命中反查）；②解析顺序 owner.login → namespace.path → full_name 段；③旧壳 `stripRepoPrefix` 结构化剥离、侧边栏按前缀分组、名表统一 `platformLabel()`；④010 加最终状态复检并 raise，线上补列+回填+删表；⑤setLang 在语言包就位后二次派发；i18n.js?v 全壳页 bump 到 20261114-16、auth.js→8 | src/api/frontend/app/src/lib/{repo-provider,bot-summary}.js、ReposView.vue、AdminPlatforms.vue、src/domains/mgmt/routes/repos.py、src/kernel/token_resolver.py、src/api/db_setup.py、src/api/frontend/shell/{app.js,index.html,js/i18n.js,locales/*/admin.json}、docs d109 §20.10-§20.15 | 托管 200（webhook 2148107）/ 缓存反查 UC-61..63 / 迁移复检 UC-64 / 语言双向切换 UC-55；实弹管理面 `source=table`、fallback 告警 0；定向 12 + 4 + 2 + 6 passed |
| S112 | 顺序/集合去字面量与缓存写入范围（第六批，2026-11-14 深度复查） | ①登录渠道展示序是 5 个 key 的字面表（加渠道必改码）；②托管兜底序 `("github","gitcode")` 二元枚举且默认打到 github；③`platform/migration.py` 用 `if provider == "gitcode"` 读历史 bot 池；④webhook 预检在卡片 import 失败时回落到平台白名单；⑤`save_user_repos` 先删全部再写 → 某平台刷新会抹掉其它平台缓存 | ①`_catalog_order_index()`：`_ui_order` → 配置键序 → 其余按名；②`_default_token_provider_order()` = `sorted(hosting_providers())`；③历史池改 `_HISTORICAL_BOT_POOL_KEYS` 查表；④兜底直接 `unsupported`/`unknown`，守卫去掉 webhook.py 整文件豁免；⑤`save_user_repos(..., providers=ok_providers)`，member/sync/重授权部分成功三路接线 | src/domains/mgmt/auth/providers.py、src/domains/mgmt/routes/repos.py、src/domains/platform/migration.py、src/domains/runtime/routes/webhook.py、src/domains/mgmt/user/db.py、tests/mgmt/{test_d109_platform_order_data_driven,test_d109_user_repos_partial_save}.py、docs d109 §20.17-§20.18 | 渠道序随配置变（UC-66）/ 兜底序含 gitee（UC-67）/ 无顺序字面表（UC-68）/ 部分刷新保留他平台（UC-69）；`test_platform_whitelist_ratchet` 由 3 failed → 4 passed；定向 25 passed / 2 skipped |
| S113 | 仓库维度读路径的真实前缀 + gitee 端到端收口（第八批，2026-11-14 用户接入 gitee PAT 后实弹） | ①`project-info`/`agents` 用裸 `to_repo_key` 默认 gitcode 前缀 → gitee 仓读错行、基线恒空；②`events` 候选写死 gitcode/github → gitee 事件恒不可见；③引擎 scope 的 repo 级凭据只匹配双前缀；④IM 卡片前缀剥离枚举三家漏 gitee；⑤issue URL→repo_key 域名表漏 gitee 且未知域名默认 gitcode（本批前一条修） | ①改用 `resolve_managed_repo_key()`（managed_repos 候选探针）；②改用 `repo_key_candidates()`（卡派生），会话 token 校验同集；③抽 `_repo_scope_clause()` 动态占位符；④结构化剥离；⑤host→provider 由平台卡派生 | src/domains/mgmt/routes/{repos,agents}.py、src/domains/runtime/routes/events.py、src/domains/runtime/im/handler_async.py、src/api/feedback_status.py、tests/mgmt/{test_repo_prefix_resolution_guard,test_issue_url_provider_mapping}.py、docs d109 §20.20-§20.21 | 实弹三平台 58/7/16 + project-info/issues/dashboard/events 全 200（UC-72/73）；守卫 3 条 + issue URL 3 条（UC-74/75/76）；串行双测：基线 191/68 → 当前 192/68，唯一差异用例隔离复跑两侧 5 passed（基线第二次复跑同为 192/68，证实为该用例套件内抖动） |
| S114 | 补证 P1 三项收口：UUID 刷新失效 / 合并残留方法 / 版本位登记落后（第九批，2026-11-14 评审） | ①`hooks` 写死 `int(user_id)` → UUID 用户关系变更后的项目列表刷新从未执行（异常被吞）；②`ChannelRepoLinkRepo` 混入 6 个 bot 方法；③i18n/cache 守卫的四张登记表落后于实际值 | ①去 int() 直传 str + 行为级 uuid7 用例（回退 int 即红）；②删除冒名 33 行（AST 复核）；③同步指纹/CACHE_VER/I18N_JS_VER/FROZEN 九项 | src/domains/platform/hooks.py、src/domains/mgmt/user/db.py、src/kernel/persistence/repositories_platform_object.py、tests/mgmt/test_d109_relation_hooks_refresh_projects.py、tests/api/{test_i18n_locale_cache_ratchet,test_frontend_cache_version_ratchet}.py、docs d109 §20.22-§20.23 | UC-77/78/79 三向（uuid PAT、uuid OAuth、存量 "33"）；ratchet 4+2 passed；`-k bot` 与仓库域用例串行双测：差异全部为套件内顺序抖动（同文件隔离复跑两侧一致） |
| S115 | P2 四项整治：关系写入原子性 / 渠道开关 fail-closed / DDL 切分 / 半建表补列（第十批，2026-11-14 评审） | ①link 两步写在并发窗口下唯一键冲突→异常被吞、uow 回滚；②enabled=0 只挡目录，直达登录可绕过；③iter_statements 正则切分被注释内分号击穿（注入实测 22 碎片）；④半建表（表在列缺）不报错不补齐 | ①方言 upsert（SQLite ON CONFLICT / MySQL ON DUPLICATE KEY UPDATE）+未知方言回退；②`is_channel_enabled()` + oauth_login 403 `CHANNEL_DISABLED`（表读失败 fail-open）；③逐字符扫描切分（注释/引号感知）；④`reconcile_missing_columns()` 按模型补列，启动时调用 | src/kernel/persistence/repositories_platform_object.py、src/domains/mgmt/{auth/providers.py,routes/grape_auth.py}、src/api/{platform_object_ddl.py,d109_startup.py}、tests/mgmt/{test_d109_user_repo_platform_link.py,test_channel_enabled_fail_closed.py,test_p2_ddl_and_schema_guards.py}、docs d109 §20.24 | UC-80 重复 link 单行+可复活 / UC-81 403 disabled & 表三态 / UC-82 四类切分样例 / UC-83 补列+幂等；合计 21 passed |
| S116 | 增量两条：LIKE 通配符误删 / 顺序索引无缓存（第十一批，2026-11-14 评审增量） | ①`save_user_repos` 用 `repo_key LIKE 'p:%'`，provider 允许下划线 → 新增 `gitee_enterprise` 时会连 `giteeXenterprise:` 一起删；②`_catalog_order_index` 每请求 open+parse，绕开同文件的 TTL 缓存纪律 | ①改 `substr(repo_key,1,?)=?` 前缀相等；②缓存键=(路径,mtime,size)，配置改动自动失效 | src/domains/mgmt/user/db.py、src/domains/mgmt/auth/providers.py、tests/mgmt/{test_d109_user_repos_partial_save.py,test_d109_platform_order_data_driven.py}、docs d109 §20.25 | UC-84（回退 LIKE 即红）/ UC-85（缓存命中，json.load 抛错仍返回）；两文件 7 passed |
| S133 | 文档/源码去敏（第十三批，2026-11-15 用户要求） | 设计稿里曾把线上 gitcode bot token 与 webhook secret 原文写进"配置文件示例"（weekly-06，weekly-08 有引用），等于把真凭据提交进仓库 | 两文件三处替换为 `<REDACTED_REAL_TOKEN>` / `<REDACTED_WEBHOOK_SECRET>`；新增扫描守卫（已知前缀 + 敏感键值形态 + 占位放行 + 键名放行） | docs/weekly-06/oauth-provider-plugin-design.md、docs/weekly-08/issue-372-design.md、tests/api/test_no_secrets_in_docs_and_src.py、d110 §14.5 | 守卫 1 passed；探针自测（塞入三条真值必红、占位不红） |
| S134 | 部署级隐藏名单语义（第十四批，2026-11-15 用户拍板 B；**S136 已改为行级 enabled**） | 隐藏名单此前只有一处读取（settings 目录），`_repo_listable_providers()` 与 `platform_tokens` 写入口都不过滤 → "UI 看不到 gitlab、接口却能 `?provider=gitlab` 列仓"的口径分裂；线上 `vcs_platforms_hidden=["gitlab"]` | 判据收敛 `domains/platform/visibility.py`；`/repos/member` 显式隐藏平台 403 `PLATFORM_HIDDEN`、合并列表剔除；管理面项加 `hidden_by_deploy`；写入口保留（内网可用） | src/domains/platform/visibility.py、src/domains/mgmt/routes/{repos.py,settings.py}、src/domains/mgmt/admin/repo_platform_routes.py、tests/mgmt/test_platform_visibility.py、d110 §16 | 新用例 7 passed（显式 403 / 合并不拉取 / 目录判据 / 带外垃圾名单 / 管理面标记 / 基址路径替换 / 基址不变） |
| S135 | `webhook_url` 口径写明（第十四批，用户拍板"只补注释"） | `get_webhook_url()` docstring 写"含 /webhook/gitcode 后缀"与实际不符（路径其实由平台卡决定、配置里的路径被丢弃），配置示例与管理面提示也没说明 | 行为不动；补 docstring、`server-config.example.json` `_comment_server_base_url`、`global-bot-token.example.json` 字段说明、管理面 `webhook_url_hint`（zh/en） | src/domains/mgmt/user/db.py、configs/server-config.example.json、configs/global-bot-token.example.json、shell/locales/{zh,en}/admin.json、d110 §16.2 | 两条语义锁用例通过与路径替换断言；两键线上实测一致 |
| S136 | 可见性收敛到平台行 enabled（第十五批，2026-11-15 用户拍板"甲"） | 隐藏名单与行级开关语义重叠、名单无管理面入口；旧 registry 把"过滤后为空"当"表不可用"，导致**全关=全开**（回退平台卡）；关平台会让存量用户项目凭空消失且无提示 | ①`visibility` 改读 `enabled`（无行/未迁移 fail-open）；②迁移 202611170021 把名单写成 `enabled=0` 并删 meta 键；③registry 哨兵拆 `None`/`[]`；④`?provider=` 403 改为 `PLATFORM_DISABLED` 并保持"先于未绑定早退"；⑤管理面 `hidden_by_deploy`→`disabled`；⑥关平台影响面预检 409+`?confirm=1` | src/domains/platform/{visibility.py,registry.py}、src/domains/mgmt/routes/{repos.py,settings.py}、src/domains/mgmt/admin/repo_platform_routes.py、src/api/db_setup.py、tests/mgmt/test_platform_visibility.py、d110 §16 | 用例 13 passed（判据 / 403×2 / 合并不拉取 / registry 哨兵×3 / 预检×3 / webhook 语义×2）；回归 63 passed |
| S137 | 默认不展示写在初始化代码（第十六批，2026-11-15 用户拍板"写死初始化"） | 平台卡方案需要新增 dataclass 字段 + 播种语义，卡片与"能力描述"职责混住；而"上线默认不展示 gitlab"本质是部署口径 | `DEFAULT_HIDDEN_PLATFORMS={"gitlab"}` 写在持久化层；生效点=新建平台行 + 迁移 021 补默认；导入不覆盖、orphaned 恢复视为可用；平台卡保持无 enabled 键 | src/kernel/persistence/repositories_platform_object.py、src/api/db_setup.py（021）、tests/mgmt/test_platform_visibility.py、d110 §16.4 | 新用例 3 条 + 恢复语义回归；可见性套件 16 passed；跨套件 56 passed |
| S138 | 管理面交互三连修（第十七批，2026-11-15 真浏览器实测） | ①说明气泡被 `.ap-group{overflow:hidden}` 裁切（`elementFromPoint` 命中页面容器）；②关闭平台无确认→409 失败，随后改用 `window.confirm`（用户否决），再改 `AGModal` 又因 admin 壳未加载设计令牌导致遮罩失去 `position:fixed`、弹层跑到页面外；③两页共用 `field.enabled` 说明，平台页文案与"用户面可见"语义不符 | ①AGHelp Teleport+fixed+按锚点定位+z-index 3000+滚动/缩放重算；②页面内 Vue 弹层（自带 fixed/inset 样式、ghost/danger 按钮、影响面数字、`?confirm=1` 重发、取消回滚），后端关闭一律 409 预检；③按页映射 `field.enabled_channel`/`field.enabled_platform` | AGHelp.vue、AdminPlatforms.vue、AdminChannels.vue、shell/locales/{zh,en}/admin.json、tests/api/test_admin_help_popover.py、tests/api/test_admin_enabled_semantics.py、d110 §17 | 浏览器实测：气泡 `pos=fixed,z=3000,parent=BODY,inViewport=true,hitIsPop=true`；确认弹层 `mask fixed/z2000`、dialog 在视口内；两守卫 5 passed；ratchet 15 passed |
| S129 | 品牌图标破图（第十三批，2026-11-15 用户实测） | 内联 SVG（`shell/js/oauth-buttons.js`）原样拷成 `.svg` 文件缺 `xmlns`，浏览器拒绝当图片解码：gitee / huawei 两枚破图，带 xmlns 的 gitlab / gitcode / github 正常 | 两枚补 `xmlns`；资产 URL 加 `?v=3`；新增"独立文件可渲染"守卫（xmlns + viewBox + 解析成功） | src/api/frontend/shell/img/{gitee,huawei,gitlab}.svg、AdminChannels.vue、AdminPlatforms.vue、tests/api/test_admin_brand_icons.py | 5 枚端点 200 且真实 Chrome `new Image()` 全 ok；守卫 6 passed |
| S130 | 渠道命名国际化 + huawei 定名（第十三批） | 详情页头用 `current.name`（配置原名）绕过语言包，中文名漏进英文界面；huawei 标"华为云"与实际渠道（华为统一 OAuth 登录）不符 | 头改 `channelName(current)`；语言包 zh 华为 / en Huawei；配置文件与线上 meta `name` → Huawei | AdminChannels.vue、shell/locales/{zh,en}/admin.json、configs/oauth-providers.json、tests/api/test_admin_brand_icons.py | 线上 `/static/locales/{zh,en}/admin.json` 实测 华为/Huawei；meta 实测 Huawei |
| S131 | 配对设置下线（第十三批，用户拍板） | "可接入的代码托管平台"三段口径（同域/跨名/未配置）讲不清，且不该让用户手配关系 | 整段删除（pills/选择框/保存/删除）＋`linkEdit` 状态与 7 个辅助函数 ＋ 列表副标题配对字样 ＋ 13 个孤儿语言包键；后端端点与 `channel-repo-links.json` 导入语义保留；反向守卫禁 vue 再现写入 | AdminChannels.vue、shell/locales/{zh,en}/admin.json、shell/js/i18n.js、tests/api/test_admin_brand_icons.py | `test_channel_page_offers_no_pairing_settings` 绿；页面实渲无该分节 |
| S132 | 展示层版本位同步（第十三批） | 语言包 / 视图 / 图标都变了但版本位不齐 → 老用户持旧缓存（破图或旧口径） | 四页 cacheVer + 指纹 + `admin-view.js?v` + 图标 `?v=` 同批 bump，两张 ratchet 登记同步 | shell/{index,admin,register,forgot}.html、tests/api/{test_i18n_locale_cache_ratchet,test_frontend_cache_version_ratchet}.py | 两 ratchet 6 passed；线上 `/admin` 引用 `v=41`、`i18n.js?v=20261114-19` |
| S117 | bot 单一归属（D110）：bot 落到平台行、删 bot 账号目录（第十二批，2026-11-14 用户拍板） | ①D109 把 bot 设计成平台级多账号目录 + is_default，运行时取 token 链（仓库级 → 全局 bot → 用户 PAT）从不读它；管理面写的是它、真源在 meta，82502db1f 删掉全局 bot 输入后页面写没人读、真源没入口；②`is_default`/`get_default` 零调用者、`mark_token_status`/`touch_last_used` 零调用者；③`capabilities.supports_bot_token = bool(bot_accounts)` 属假阳性，`bot_username` 恒空；④平台页仍按“多账号 + 设为默认”呈现，与“一平台一个 bot”的真实语义冲突 | ①平台行加 `bot_token_enc/bot_token_status/bot_checked_at`，`bot_username` 从配置声明升为事实；②读链平台行优先、meta/文件兜底；③写口收敛 `PUT|DELETE /admin/repo-platforms/{p}/bot`（加密、不回显）；④导入写保护 + 空值播种；⑤轮换触发 `global_bot_token_swap`（path A 仓库 webhook token 传播）；⑥巡检双写平台行；⑦删除 `grape_t_bot_accounts` 全代码面 + 前端单 bot 面板；⑧迁移 202611170013 加列 / 014 meta 播种 / 016 `derived_from` 32→36；不写 DROP 迁移 | src/kernel/persistence/{models_platform_object,repositories_platform_object}.py、src/domains/{platform/capabilities,platform/migration,platform/admin_sync,mgmt/user/db,mgmt/admin/config_store,mgmt/admin/repo_platform_routes,runtime/engine/bot_token_health}.py、src/kernel/token_resolver.py、src/api/{db_setup,platform_object_ddl,d109_startup}.py、src/api/frontend/app/src/views/AdminPlatforms.vue、docs d110 + d109 §4/§18.2/§18.3 | 冷启动 SQLite/MySQL：四张表、平台三列齐、`derived_from varchar(36)`、013/014/016 盖章且幂等（UC-D110-01/08）；读序平台优先（UC-D110-02）；导入播种一次、改名不被覆盖（UC-D110-03）；轮换触发 swap（UC-D110-04）；池丢弃、名单=平台行∪卡（UC-D110-05）；巡检写表（UC-D110-06）；能力视图按平台行（UC-D110-07）；退役面守卫（UC-D110-08）；另含 P1-4 修复（UC-86）与守卫盲区补齐（UC-87）。定向 76+7 passed，仅剩基线同红。 |


| S91 | ORM 开关退役 + engine 重绑跟随修复（D67，先测后改，批次 2 收口） | 身份域 7 函数 _orm_enabled 分支删除（ORM 唯一路径，S79 对账转常态回归）；get_webhook_secret_by_repo_key_any 去门；orm_enabled 删除；test_s78 改退役锁。engine get_engine 缓存键含当前连接 id+路径、creator 动态解析（修复重绑后 ORM 停留旧 inode 的世界分裂残留盲区）；ast 块界折叠（行级脚本切错块界教训入册） | src/auth/grape_account_db / src/user.db / src/persistence/repositories+__init__+engine / tests/test_s78_persistence_skeleton.py | src 零开关引用（退役锁）；S79/S81 对账用例常态回归绿；372 文件原始 mock 用例 ORM 常开下全通过；5 预存量失败与开关无关（raw/ORM 双态失败集一致实测） | S78/S79/S81/S80b/S82/S60 全绿（5 预存量=372 基线）；372 文件 5 失败=测试基建专项（读回可见性异常）登记独立轮 |

| S90 | 模板 push 目标 host 卡派生 + 源平台 tarball 分派表（D66，先测后改，批次 2 续） | 真二分缺陷：push_template_to_repo 非 github 目标硬编码 gitcode.com——gitee 建模板仓内容推错 host；目标 host 改平台卡派生（gitcode 逐字节不变）；tarball 三分支 if → _TEMPLATE_SOURCE_DOWNLOADERS 分派表（github 源误配置顺带修正） | src/user/repo_manager / tests/test_s90_template_push_derivation.py（3 host 语义+分派表+github 源派发） | gitee 目标 remote URL=卡 host；gitcode parity 逐字节；github 存量分支不变；仓内 push 模板用例（oauth2:user_tok@ 断言）绿 | S90 5 绿（先 4 RED）+ templates 族回归 40 绿 + S84-S89 回归 38 绿 |

| S89 | 树 API 家族卡位 + OAuth email 补抓卡位（D65，先测后改，批次 2 续） | git_repo_api 树家族 5 处 p=="github" → 卡能力位 git_tree_family（github_api/v5；builtin github=github_api，其余 v5=存量；gitee 实弹后可配置翻转）；守卫口径修订：provider 变量比较清零、card.dialect 判断=方言域合法保留；providers.py 渠道 emails_endpoint 能力位（github 内置 /user/emails）平台比较清零 | src/api/git_repo_api（_tree_family helper+5 sites）/ src/vcs/platform_card / configs/platforms/github.json / src/admin/routes（白名单）/ src/auth/providers / tests/test_s89_tree_family_and_email.py（能力位 1+守卫 3+回退 1） | github/v5 两家族行为语义保持；无卡回退 v5；渠道 email 补抓 github 行为不变 | S89 5 绿 + S84-S88 回归 33 绿 + git_repo_api stash 对照 IDENTICAL（7 预存量） |

| S88 | bot 身份链数据驱动 + github 怪癖能力位（D64，先测后改，批次 2 续） | resolve_bot_username if 分支 → GLOBAL_BOT_USERNAME_SOURCES 数据驱动（github 单值/gitcode pool/gitlab 仅卡/config-only 仅卡/未知=pool 兜底，5 边界语义锁）；labels 拆分与 PR 评论端点从 dialect 误抽象纠正为卡能力位 pr_comment_endpoint/pr_labels_via_issues（github 怪癖≠方言：gitee 同方言但 v5 端点正常）；字段进 dataclass/Builder/builtin 卡/github.json/admin 白名单（S70 纪律） | src/user/repo_manager / src/user.db（来源表）/ src/api/git_repo_api（_pr_cap helper）/ src/vcs/platform_card / configs/platforms/github.json / src/admin/routes / tests/test_s88_bot_chain_and_dialect.py（语义锁 6+能力位 2+守卫 2） | 五边界语义保持；github 卡能力位生效、gitee/gitcode 不受影响；admin 全字段回写 | S88 8 绿（先 2 RED）+ S84-S87 回归 28 绿 + git_repo_api/581/admin stash 对照零新增（17 预存量 stale-seam） |

| S87 | 二分全量枚举台账 + 真二分收口第一批（D63，先测后改，批次 2 续） | vcs 外 265 处字面量逐条分类（方言保留 5 / gitcode 专属登记 3 组 / 真二分 8 组收口 / 登记批次项）；token_resolver 全局 bot 链 4 处统一 get_global_bot_token_for + 新 helper get_global_bot_usernames_for；feedback converter 统一入口；grape_auth OAuth 读回校验全渠道化（纯加固）；user/db 孪生 token 函数泛化 get_user_platform_token；storage 路径派生 parse_repo_key | src/token_resolver ×4 / src/api/feedback_issue_converter / src/api/routes/grape_auth ×2 / src/user.db（2 新 helper + 孪生委托）/ src/storage/__init__ / tests/test_s87_true_dichotomy_closure.py（守卫 3+行为 3） | 三文件平台比较清零（文件级守卫）；github 单值/集合语义保持；读回校验全渠道；孪生委托等值；config-only 平台 token 可读 | S87 6 绿 + S84/S85 回归 16 绿 + global-bot/self-loop/user_db/s59 隔离全绿 |

| S86 | 健康扫描去映射表 + 事件族 fixture 修复 + 阶段路由死锁修复（D62，先测后改，批次 2 续） | P0：event_pipeline 7 处裸名 _native_issue_number（S75/S76 机械收口遗留）NameError 被 safe-fail 吞——阶段路由整体静默不分发；模块级统一导入根修。health 扫描 _CRED_PROVIDER 字面映射移除（provider=repo_key 派生，bot 族过滤下沉 SQL，第 5 平台自动纳入）。test_event_action_filter fixture 引用不存在列修复（~30 预存量转绿）+ 两类陈旧断言按现状矩阵特征更新 | src/api/event_pipeline（模块级 import）/ src/runtime/bot_token_health / tests/test_event_action_filter（fixture+2 断言特征更新）/ tests/test_s86_health_scan_derivation.py（守卫 2+行为 1） | 路由 4 用例首跑转绿；health 守卫（无映射表+bot 过滤语义）+ 第 5 平台派生；S75/S76/S82/S83/S59 回归 54 绿 | test_event_action_filter 全绿（含首跑）+ S86 3 绿 + 回归 54 passed |

| S85 | S82 读者收口补漏 + 全仓派生清零（D61，先测后改，批次 2 首批） | 真回归：S82 列冻结漏切 get_bot_token_by_repo（12 调用点）与 get_webhook_token_by_repo gitcode/github 侧（rc 分支只盖卡平台）——新托管仓凭据读空（merge-base 过/S82 挂，worktree 实证）；全仓 10 处三元式内联派生（platform_card/events/scenes/event_pipeline）改 parse_repo_key，守卫扩全仓 rglob | src/user.db 两读者（列回落在前+rc 兜底，路径 B 守卫保持）/ src/vcs/platform_card ×2 / src/api/event_pipeline ×1 / src/api/routes/events ×5 / scenes ×2 / tests/test_s85_reader_closure_and_derivation_sweep.py（回归锁 3 + 全仓守卫 + 行为锁） | rc 感知（新仓 btok 可读）/ 列回落（历史行不破坏）/ 路径 B 守卫保持 / 全仓守卫清零 / source 派生 gitee 不折叠；test_add_and_scene 转绿 | S85 5 绿 + S84 5 绿 + S80/S80b/S82 回归绿；涉改族 stash 对照零新增（12 预存量 372/stale-seam）；对账基建教训入 D61（family 双侧存在性校验 + 共享测试库重建） |

| S84 | 评审收口三件（D60，评审采纳）：直切准入制度化 / ORM 开关退役计划 / provider 派生统一 | ①直切准入三条件（只读+等价锁+同事务世界，S81b 追溯合规）；②GRAPE_PERSISTENCE_ORM 退役节奏（批次 2 删分支，删除前语义冻结不新增 flag 调用点）；③S80b 内联 `split(":",1)[0]` ×2（user/db 同形块，get_webhook_token_by_repo/get_webhook_verify_token）改 parse_repo_key + 结构守卫防回潮；④空值回落语义依赖写死（删回落前先删列，顺序不可倒置） | docs（D60 节+本行+状态行）/ tests/test_s84_provider_derivation_unification.py（守卫：内联派生清零 + 回落语义锁）/ 代码收口（user/db 同形块两处）随批次 2 | 守卫用例：user/db 无内联 provider 派生（结构）；get_webhook_token_by_repo 裸键回落 gitcode 语义（行为）；rc 空串回落列语义锁（依赖声明） | test_s84_provider_derivation_unification.py（结构守卫+行为锁）；代码收口受并行重构 WIP 阻塞，随批次 2 落地后守卫转绿 |
| S83 | 平台卡文件外置（D59，先测后改，评审论证采纳）：每平台一个配置文件=能力聚合点 | 论证采纳：卡片方案>目录包（差异度/运维语义/叠加纪律三论据实测成立），但补目录包"聚合"优点——S70 漏字段根因=字段权威（dataclass）与值载体（代码 dict+DB KV）分离。新增 configs/platforms/{gitcode,github,gitlab,gitee}.json；优先级链 meta>file>代码兜底；meta 覆盖=整卡替换（admin 全字段持久化天然完整，部分覆盖语义不提供）；gitee 升级文件内置（config-only 语义退役） | src/vcs/platform_card（builtin_cards_from_dir 加载器+键⊆dataclass 守卫+_CONFIGS_DIR）/ configs/platforms/*.json ×4 / tests/test_admin_vcs_platforms（S18 语义更新） | 文件加载逐字段 parity（vs _DEFAULT_CARDS_RAW）；优先级链 meta>file；未知键整卡拒绝（error 响亮）；repo 文件键⊆dataclass 守卫；目录缺失回退代码兜底；S18 清 meta 后 gitee 回落文件默认（能力位完整） | test_s83_platform_card_files.py 5 用例（parity/优先级/未知键拒绝/文件键守卫/目录缺失回退）+ S18 语义更新 + 大回归零失败 |

| S75 | 全量枚举收口（D50，先测后改，枚举+分类+计数对账驱动）：11 组确认洞 | 方法论升级：形状扫描从抽样 grep 改为全量枚举+逐条分类（总命中=确认洞+基线预存+误报，数字闭环）。确认洞 11 组——崩溃类：issues 列表/展示 int(number)×3（gitee 仓 issues 列表 API 500）；错误 token 类：chat×4（双平台绑定用户拿 gitcode token 注入 gitee 会话——D37 声称已修实未接）、repos 移除流 else 折叠、autopilot/scheduler github/else 二分（gitee 仓 autopilot 取空 token）；功能拒掉类：管道 ISSUE_NUMBER 注入 isdigit 门、cancelled 检查门、SRE 回填门、附件 prompt 门、IM 任务号门/链接；硬编码 URL 类：澄清评论 else=gitcode（gitee 仓发 404）、fail_task 去重读评论 else=gitcode（重复回帖） | src/api/routes/issues.py / chat.py / repos.py / autopilot.py / runtime/scheduler.py / service.py / api/event_pipeline.py / prompt_builder.py / im/handler_async.py | 崩溃类接 _native_issue_number；token 类统一 get_user_token_for_repo / get_repo_token_with_refresh（provider 感知）；isdigit 门改非空校验+native；硬编码 URL 改 VcsApiClient(provider)（澄清评论与去重读评论均含 #453 台账方言派生） | 守卫 7 + 行为 1（tests/test_s75_exhaustive_closure.py）+ chat/im 涉改族回归基线 diff 零新增（5 预存量 tooluse 环境依赖型） |

| S76 | 族B/族A 终收口（D51，先测后改，第三轮枚举）：ident 崩溃点 15 处 + 展示错派 4 处 | 族B 过滤后真 ident int() 崩溃点 15 处（变量名各异——_n/_num3/_ce_num/_sub_iss_num/issue_number/num/parent_issue_number/_iss，S75 守卫正则没罩住）：管道关单/建档/子单标题/查询参数、patrol 巡检同步、patrol_compensate、session_store 父任务号、webhook carried_context 与 A2A 标题 ×4、autopilot 任务查找、tasks/issues 查询——gitee 字符串 ident 一到即 ValueError；族A 分类出展示错派 4 处：chat/IM 401 提示平台名二分显示 "GitCode"（gitee 仓错平台名）、issues 会话 issue_url host 二分指 gitcode.com（gitee 仓链接错平台） | src/api/event_pipeline.py / issues_center / patrol / patrol_compensate / session_store / routes/webhook / autopilot / tasks / issues / chat / im/handler_async + vcs/platform_card（新 display_name_for_repo_key / host_for_repo_key 助手） | 崩溃点统一 _native_issue_number（含 isdigit 守卫内 2 处——同门族语义：守卫拒 str ident 平台）；展示错派接新助手（卡 name/host 派生，无卡回退存量二分默认=裸键 gitcode 语义） | 守卫 1（15 处崩溃点正则清零）+ 助手行为 1 + 展示点守卫 1 + 既有族回归 22 绿 + 涉改 11 文件族批零失败（基线 diff） |

| S77 | IM 域终残留（D52，先测后改，"other"族抽样分类驱动）：绑定 provider 写死 "gitcode" | handler_async 3 处 upsert_repo_im_binding 第二参硬编码 "gitcode"——gitee 仓 IM 绑定行 provider 错标（FR-21.2 家族尾巴：读侧按 repo_key 派生、写侧折叠）；函数签名默认值（provider="gitcode" 等）与 gitcode 专属子系统（OAuth 刷新注册表/settings 存量字段/scenes 登录名）确认为设计语义不修 | src/im/handler_async.py ×3 调用点 | provider 按 repo_key 前缀派生（parse_repo_key，裸键=gitcode 存量语义）；守卫精确匹配旧形态（provider 参数位置字面量），`or "gitcode"` 裸键兜底默认合法 | test_s77_im_binding_provider_not_hardcoded（精化守卫）/ test_s77_im_provider_derivation + IM 族回归 96 绿 |

| S78 | 持久化骨架（D53，先测后改）：身份/凭据/仓库域 ORM 化（桥接模式 SQLAlchemy 2.0） | 评审定稿：①UoW 用例级 session+Service 唯一提交点（Repository 只 flush）；②Repository 返 DTO 不出口 ORM 实体（守卫固化）；③分表=单表重构非分片（动态表名/ShardedSession/中间件全部不需要，PoC 降级为单模型映射+数据迁移对账）；④DDL 唯一出口=盖章迁移（禁 create_all/Alembic——双迁移系统会让门禁裂开），模型为声明+对账基准，compare_metadata 漂移守卫；⑤敏感模型 __repr__ 脱敏；⑥运行时开关 GRAPE_PERSISTENCE_ORM（灰度切回）；⑦桥接模式=SQLAlchemy Dialect 承接 sqlite/mysql 方言（JDBC 等价，单实现双方言，修正此前"两实现"表述） | src/persistence/（engine 桥接工厂 pool_pre_ping / base 禁 create_all / models 四域逐列对齐 PRAGMA 实测 / dto / uow / repositories / reconcile 对账三件套） | 引擎 PoC（连接/事务/pool_pre_ping）；schema 对账零漂移（列/可空/PK，PK 自增方言差异豁免）；字段值对账（裸 SQL 写→ORM 读同值）；UoW commit/rollback；DTO 返回形态静态守卫；repr 脱敏（token/secret/password）；开关默认关 | test_s78_persistence_skeleton.py 7 用例（engine PoC/schema drift/reconciliation/UoW/DTO guard/repr masked/switch）|

## 实现状态（批次 0，滚动更新）


- 已实现+实弹绿（D47 gitee/gitlab 实弹批次，2026-11-05）：S70/S71/S72——
  gitee 服务级全流程（绑卡→绑 PAT→托管→统一建单→Gitee 侧回读对账，
  提交 68730dd28/503de6829）+ 本地模拟 gitee 原生投递（X-Gitee-Token
  + 'Note Hook'，200/验签/pipeline 派发）+ GitLab 托管链收口
  （e694e89f2 + GitLabProvider/注册表）。10 个缺陷收口，59 用例绿。
  挂账：①gitee webhook 端到端回流差云入口层放行 gitee 投递 IP（国内
  浏览器/gitcode 投递可达、gitee 投递集群超时，需控制台查安全组+EIP
  CC 拦截记录）——服务端回执链已就绪（本地模拟闭环）；②gitlab 托管链
  未实弹（需 gitlab 账号+PAT），形态断言按官方文档录制；③
  test_repos_host_provider 7 预存量 stale seam（符号
  get_user_github_token_with_refresh 已不存在，b95fdfff4 遗留）。

- 已实现+守卫绿（D48 同型家族收口，2026-11-05）：S73——事件管道
  ident 原生保形（B 族 14 处）、白名单列每启动补齐+派生（A 族）、
  token 派生残留 3 点接 get_user_token_for_repo（C 族）。挂账：附件
  同步评论 gitcode 硬编码；add_managed_repo 10 调用点零返回值检查
  （静默 False 家族）；config-only 登录渠道无管理面（E 族，D49 候选）。

- 已实现+用例绿（D49 剩余挂账收口，2026-11-05）：S74——附件同步评论
  卡化（gitee 等平台附件回流评论可用）、add_managed_repo 响亮失败
  （raise+降级日志）、登录渠道管理面 GET/PUT /api/admin/auth-channels
  （合并式保存+凭据 meta+策略校验）。D48 挂账三项全部闭环。

- 已实现+用例绿（D53 持久化骨架，2026-11-05）：S78——src/persistence/
  四域 ORM 骨架（桥接模式单实现双方言/UoW/DTO/repr 脱敏/运行时开关/
  对账三件套），7 用例绿。评审缺口八项全部落进 D53：UoW 事务边界、
  分表=单表非分片（PoC 降级）、DTO 守卫、repr 脱敏、禁 create_all、
  schema 漂移对账、显式加载约定、运行时开关。修复 S75 误删
  get_user_token_for 符号的已提交回归（import 行，test_s62 暴露）。

- 已实现+对账绿（D58 双写移除，2026-11-05）：S82——grape_t_repo_credentials
  唯一权威（8 列冻结、读者去门、health/swap/has_bot 三类读者收口）；
  评审挂账处置：双写原子性随双写移除消失、空值覆写依赖（回落语义）
  写死入 D58、parse_repo_key 内联回潮登记下批。

- 已拍板+收口完成（D60 评审收口三件，2026-11-05）：S84——直切准入
  三条件制度化（D57 实践追溯合规）、GRAPE_PERSISTENCE_ORM 退役节奏
  （批次 2 删分支，语义冻结不新增 flag 调用点）、provider 派生统一
  代码收口落地（user/db 三处内联改 parse_repo_key：webhook token
  读取同形块 ×2 + webhook_secret 通用面 ×1，守卫 5/5 绿）；空值回落
  依赖写死（删回落前先删列）。登记扩展项：全仓同款三元式派生尚有
  ~10 处（events/scenes/event_pipeline/platform_card 展示助手族），
  批次 2 扩守卫范围收口。注：并行会话 runtime 重构 WIP 已 park 至
  wip/runtime-refactor-park 分支（含 S25/S59/S66 回退 hunks，禁直并）。

- 已实现+对账绿（D54 身份域 ORM 切换，2026-11-05）：S79——身份域 7 函数
  双路径（GRAPE_PERSISTENCE_ORM 开关）对账 7 用例逐字段等值；评审三缺口
  （reconcile 静默空集/幽灵 inode 世界分裂/DTO 缺字段）全落定并登记。

- 已实现+对账绿（D55 凭据域单表+双写，2026-11-05）：S80——
  grape_t_repo_credentials 表（safe 迁移）、add_managed_repo 双写（仅凭据列）、
  RepoCredentialRepository CRUD、backfill 幂等；schema 对账清单纳入
  新表。读取方切换（TokenResolver 等）归 S80b。

- 已实现+对账绿（D56 仓库域查询族，2026-11-05）：S81——
  get_webhook_secret_by_repo_key_any 双路径（flag on 走
  ManagedRepoRepository.find_by_full_name_any_provider，flag off 存量
  raw SQL 语义不变）；Repository 扩展 find_by_full_name_any_provider/
  exists/get_webhook_secret。S80b 读取方切换（grape_t_repo_credentials 优先+
  列回落，灰度开关）同期完成。

- 已实现+回归绿（D57 单列助手直切，2026-11-05）：S81b——
  managed_repo_exists / get_host_user_id 直接切 Repository（用户拍板
  不加双路径开关）；19 文件族基线 diff 零新增。

- 已实现+用例绿（D55 续 S80b 读取方切换，2026-11-05）：RepoBotToken._fetch
  与 get_webhook_token_by_repo 双源读取（grape_t_repo_credentials 优先+列回落，
  开关灰度）。注意：get_webhook_verify_token 与 get_webhook_token_by_repo
  形态相近——首轮补丁插错函数（结构定位教训：改函数前先 awk 全函数体）。

- 已实现+守卫绿（D50 全量枚举收口，2026-11-05）：S75——崩溃/错 token/
  功能拒掉/硬编码 URL 四类 11 组洞收口（chat×4、autopilot、scheduler、
  issues 列表、澄清评论、去重读评论等）。方法论固化：形状扫描必须
  全量枚举+逐条分类+计数对账（抽样 grep 是本轮前历轮遗漏的根因）。

- 已实现+守卫绿（D51 族B/族A 终收口，2026-11-05）：S76——15 处
  ident int() 崩溃点（变量名各异，S75 守卫正则盲区）+4 处展示错派
  （平台名/issue 链接）。新增 display_name_for_repo_key /
  host_for_repo_key 卡派生助手。第三轮枚举后五族（int/token 折叠/
  isdigit 门/SQL ident/硬编码 URL+展示二分）确认洞全部清零；
  误报/预存量清单见 D51（含 token 分流 4 处已 provider 感知、
  gitcode 镜像关单设计、repogit 平台卡分支）。

- 已实现+守卫绿（D52 IM 域终残留，2026-11-05）：S77——绑定 provider
  派生 ×3。至此仓库域+IM 域平台名字面量分支清零；剩余字面量仅
  函数签名默认值（裸键语义）与 gitcode 专属子系统（设计语义），
  已在 D51 误报/设计清单登记。

- 已实现+全量对账（D46 混跑污染首批）：S68——9 文件污染源收口
  （模块缓存清空/reload main/换库泄漏/env 永久改写四族），全量
  E 711→440、F 1430→1250、通过 +451。
- 已实现+最小集验证（D46 混跑污染批次 2，**部分回退**）：autopilot stub
  恢复 None 分支（未导入真身时 pop 而非跳过）保留——skills_obs E 消 28、
  机制独立零风险；337 teardown reload+init+commit 方案**全量回归
  （E 440→954、locked 321→645）已回退至批次 1 形态**——最小集修复
  （locked 6→0）代价是 teardown 中途 reload+重跑 init_database 搅乱
  全局绑定。教训登记：mid-suite reload db_setup 属破坏性操作，
  locked 族的正确修法在事务源头（session_manager/db_setup 模块级
  init 自身收尾提交，或 S69 架构级每测试独立库），归 S69。
  **过程铁律（用户点名）：不得用 3 小时级全量当迭代环，迭代一律
  定向集，全量只在收口跑一次。**全量时长膨胀（50min→3h）本身是
  污染症状：数据污染使本地 miss 用例退化为真实平台 API+重试
  （既是 F 失败源也是时长源）。

- 已实现+单测绿（D45 段错误专项·线程风暴治理）：S67——faulthandler
  定位崩溃现场（Thread-944 于 _write_to_obs DB 段，事件驱动裸 Thread
  每事件一个、944 存活线程堆积），_spawn_project_info_refresh 单飞
  +并发上限 4+finally 释放；第二轮 faulthandler 实锤根因（fixture
  teardown 关连接 × 在飞 pi-refresh 写并发 = sqlite C 层 UAF），
  wait_pi_refresh_drain 排空接线 4 个同族 fixture。S67 用例 4 绿；
  定向 95 绿。**全量验收（ac034e338 后）：EXIT=1 正常跑完全程（非
  139），观察至 85% 零 Fatal、43% 历史崩点区干净通过——段错误根治
  达标**（基线：修复前两轮全量均于 43% SIGSEGV exit 139）。

- 已实现+单测绿（D44 批次 2，展示/IM/读侧族 11 处收口）：S66——
  scenes 分支端点三路合一（VcsApiClient.create_branch 卡能力位
  branch_create_form + get_readme 读侧方言模板）；templates 4 处
  （bot 来源链/D3 入口/适配器端口/README 卡 env）；feishu 协作者
  list_collaborators 端口 + um 同源白名单 um_login_is_grape_account
  （修正中途引入的 github→gitcode_user 误写隐患，FR-21.2 锁）；
  repo_roles 形态归一（_get_platform_login/_is_admin_permission）；
  outbound_ledger 内容 ID 行为派生。mock 接缝迁移四测试文件
  （D35.1）。定向回归 76+182 绿。**追加（793b84249）：12 个存量基线失败
  归零**——outbound_ledger×9（陈旧接缝：函数改名 D29/命令入口 D22 +
  MagicMock __enter__ 子 mock 陷阱）、im_async b1/b5（R20.11.1 token 入口
  迁移后 patch 目标失效）、template_usage×1（a34315f2 已退役表误锁，改锁
  featured_templates UNIQUE 契约）。分支已知失败清零（全量 pytest 的
  ~43% 段错误除外，aarch64 py3.14 待 gdb 专项）。文档对齐：批次 1
  （60fbc36e4，S65）当时漏更文档，本批一并补 S65/S66 行与本 D44 设计节。
- 已实现+单测绿（D44 批次 1，展示/方言回退族 5 处）：S65——见
  60fbc36e4（repo_key 映射表/git_repo_api 常量集合/issues 折叠/
  auto-close TokenResolver/过期提示卡派生/前端 gitee 品牌）。
- 已实现+单测绿（问题清单修复批次）：S64 DDL 硬阻塞 + 陈旧测试修复——
  基线 19 个存量失败在本分支归零（2 个真解析 bug：agent_runtimes age
  与 _parse_utc_timestamp 不认 ISO 混存口径【Issue #512 同族】；17 处
  陈旧断言/接 seam：add_collaborator 元组返回、host_repo §16.1 降级
  语义、T13 token 接缝、白名单 i18n、server_base_url 默认端口）；
  定向对照集 199/199 全绿
- 已实现+单测绿（D43 凭据/列名族卡化）：S63——admin/settings/
  bot 健康/路径 B 查询 11+6 处列名与口径全卡派生；路径 B 前缀子句
  目录派生（裸键=默认平台特例保 parity）；白名单列映射表未知平台
  拒绝；四文件 AST 守卫零平台比较；零新增回归
- 已实现+单测绿（D42 托管主路径收口）：S62——repos.py 36 处
  github-else 二分全部迁移到既有工厂+适配器端口（用户领域纠偏：判据
  是分派写法，非会不会踩）；新端口 create_repo/find_existing_webhook/
  fetch_repos_with_role/get_collaborator_permission；host_repo 双流合一；
  探活卡派生+hooks 404 行为派生；**文件级 AST 守卫：repos.py 平台名
  比较归零**；零新增回归（stash 基线逐条对照）
- 已实现+单测绿+实弹绿（D41 gitee issue 写方言）：S61——S12 首轮实弹
  （真实仓 example-org/example-repo）：读路径（verify/列仓/repo_info/查重/
  分支/GET 单条）零改动即绿；写路径两真差异（owner-path+repo 入 body、
  number 字符串 ident）按卡能力位 issue_write_form 收口；实弹复测建单
  →查重→关闭全绿，临时单已清理。webhook/协作者写形态未实弹（已登记）
- 已实现+单测绿（D40 oauth_fill 根治）：S60——显式链接注册表
  provider_link（渠道∩平台目录派生+刷新能力注册声明，_try_refresh_
  user_token 消字面量分派）+ 凭据撤销域服务（键族 {p}_oauth/{p}_pat
  集中身份域，解绑传播闭合 _pat 残留缺口）；批次 1 六条验收门槛
  （用户分析）登记进设计文档 D40 节
- 已实现+单测绿（D39 绑定/解绑域收口）：S59——解绑合法平台集合=
  登录渠道目录∪仓库平台目录派生（用户领域建模纠偏：三域解耦，
  oauth_fill 为唯一耦合点）；um_column_for_provider 白名单表+空列
  全调用方跳过（绑定污染/误清/归属跨平台误命中三处消除）；
  get_host_vcs_username/scenes 缓存失效/auto_upgrade（host token 统一
  入口+协作者邀请端口 accept_collaborator_invite）收口；auto_upgrade
  host token 接缝迁移两个旧用例同步更新（D35.1 纪律）
- 已实现+单测绿（D38 升级链卡化）：S58——user/db 三处收口：provider
  口径 parse_repo_key 派生、列名卡派生（identifier 白名单）、webhook
  删/建走适配器端口 setup_repo_webhook/remove_webhook（凭据形态下沉
  适配器，调用方零平台分支）；github/gitcode 委托形态与 SQL 逐参数
  行为锁；残留站点（get_host_vcs_username / auto_upgrade 协作者链 /
  解绑白名单 / scenes 缓存失效）登记 D39 候选
- 已实现+用例绿（D105-18 续三 续审十一轮收口）：F1 S103 文件自我
  描述全量对齐（docstring/NS/用例名）+ F2 S104-e 哨兵断言收紧 ==""；
  15 passed，全测试面零 RED
- 已实现+用例绿（D105-18 续二 续审十轮收口）：S105——悬空兜底用例
  转退役锁（hasattr=False，D3 删除不得回潮）+ 场景文件命名债清偿
  （test_s103_* 改名，5 处引用同步）；分支 RED 归零
- 已实现+用例绿（D105-18 续 审计续审九轮收口）：S104——F1 棘轮基线双键同步
  （守卫随行纪律收口）+ F2 死代码归位 + F3 守卫 strip 归一 + F4 org 哨兵
  族统一 ''（场景锁 5 用例）+ F5 docstring 纠正 + F6 台账计数更正；
  审计失败表 6 例中 4 例转绿，余 2 例预存悬空引用保留登记
- 已实现+用例绿（D105-18 凭据级 workflows 写能力）：S103——三态判定
  credential_workflows_write（true 不删/显式 false 删/未声明回退平台旧逻辑
  github 删 gitcode 不删零回归）+ settings 对象形态声明落库与 get_credentials
  回显 + templates 接线 user_id；行为锁 6 + 场景用例 4 全绿；OAuth 路径
  保守删（oauth_scopes 入口预留）
- 已实现+用例绿：S1-S8、S10/S11、S14-S18、S20、S22-S37（含 D22 基座合一、
  D24 合并前修复、D25 agent 注入/流水线身份）
- 已实现+单测绿（D27 身份键安全）：S46/S47——external_id 稳定性
  双层校验（构建时属性名形态 + 回调值形态）与复合键不变量用例锁；
  SAML 实弹三件套（时钟偏移/断言重放 NotOnOrAfter-Conditions/ACS 绑定校验）
  归 IdP 联调批次，不入本单
- 已实现+单测绿（D28 登录路由接线）：S48——登录发起与 SAML ACS 回调
  按策略能力分派（needs_oauth_client / post_callback，路由不比 type
  字符串）；GET/POST 回调公共校验前置提取为 _validate_channel_callback
  共享；存量 oauth2 渠道委托链回归锁；SAML 验签钩子注入后全链归一声明
  → 既有三分支。全单测（mock/meta 驱动），实弹件归 IdP 联调批次
- 已实现+单测绿（D26 收口）：S39/S40/S42/S44/S45——协议三步契约、
  注册表、属性降级、OIDC 引擎与卡字段契约校验（原漏登，实测补记）
- 已实现+单测绿（D37 token 口径收口）：S57——用户 token 统一入口
  get_user_token_for_repo（repo_key 前缀派生 provider，零平台字面量/
  零 OAuth 耦合）+ fail_task 全局 token 统一入口；存量等价有行为锁
- 已实现+单测绿（D36 查重端口化）：S56——查重策略从调用方下沉
  VcsApiClient 端口方法，分派依据为卡字段 issue_search（能力位），
  调用方零平台分支；异常形态契约（(NNN) 可分类）由行为锁守住
- 已实现+单测绿（D35 建单链卡化）：S55——手工建单/onboarding 的查重与
  建单对 gitee 不再落 GitCode 分支（查重分页卡端点、建单统一 helper、
  provider 口径三处收敛）
- 已实现+单测绿（D34 归属链收口）：S54——D31 的两个漏网点补齐：issue
  建档写入路径与飞书通知链接入身份优先（错位数据生产端+消费端收口，
  um 兜底与异常留痕语义与 D31/D33 一致）
- 已实现+单测绿（D33 归属加固）：S53——(provider, external_login) 幂等
  索引（DDL 结构锁）+ 身份优先异常留痕（外部复核两撤回一降级后成立的
  两条实锤小项，D31 复核确认方案在真实数据上工作正常）
- 已实现+单测绿（D32 先测后改）：S52——webhook 头透传（消路由白名单
  封闭点）、GitLab 方言 event_map_overrides 生效（原死参数）、
  platform_tokens 泛型 PAT 入口（gitee 归属源+UserToken 级双写）
- 已实现+单测绿（D31 身份优先归属）：S51——M5 零危害优先解：归属反查
  external_identities 命中即权威、um 名字域降级兜底（错位静默算错
  user_id 的病灶在身份域消解；um 全迁/退役留批次 1）；PR 探测 API
  兜底卡驱动（D29 评论族的漏改兄弟站点收尾）
- 已实现+单测绿（D30 外围第二批）：S50——admin 面（verify_token 卡驱动、
  bot 用户名来源链）与 repos.py 托管路由解析点（候选集+统一 token 入口）
- 已实现+单测绿（D29 评论投递族）：S49——event_pipeline 六处投递/拉取/
  探测点统一卡驱动（行为级单测断言评论确实投递到正确平台端点），
  存量 gitcode/github 端点与鉴权形态回归锁（github token 头逐字节对齐）
- 设计已封档待实现：S38/S41/S43（D26 华为云 SSO 实弹件——需 IdP 元数据
  联调；S43 绑定冲突语义随实弹补录）
- 待目标环境验收：S12/S21（PAT-only 与 oauth_fill 端到端，工具不入库）、
  gitee 实弹清单见 D24/D25 前置项
- 拍板（D15）：新平台 PAT 批次 0 存储 = user_credentials 泛型表（零 DDL）；
  批次 1 grape_t_repo_credentials 表落地后迁移

## 现状问题（实测 2026-11-03，head=weekly-09）


| # | 位置 | 问题 |
|---|------|------|
| K1 | src/api/deps.py:400/547 | 两个按平台分叉的预刷新包装器 get_user_github_token_with_refresh / get_user_token_with_refresh |
| K2 | src/api/oauth_token.py:74-79 | if provider == "gitcode" 分叉取 token |
| K3 | src/token_resolver.py:235-242 | create_provider 硬编码二分（github/其余=gitcode），GitLabProvider 存在但不可达 |
| K4 | src/user/db.py:453 get_user_by_gitcode_id | 登录身份全局匹配兜底取凭据（登录平台污染仓库凭据选择） |
| G1 | src/runtime/repogit.py:15 _HOSTS={gitcode,github} | clone_url_for 对 gitlab:/gitee: 仓库静默回退 gitcode.com —— 现存 bug |
| G2 | src/runtime/repogit.py:109 _basic_auth | 固定 x-access-token: scheme；GitLab 需 oauth2:，Gitee 需 token 作用户名/密码 |
| G3 | src/runtime/repogit.py:377 _write_askpass | token 环境变量 fallback 链按 github:/else 二分硬编码，gitlab/gitee 会拿 gitcode token |
| G4 | src/user/repo_manager.py:500/575 | token 内联进 URL（https://oauth2:token@gitcode.com），gitcode 专用且 token 进进程参数 |
| W1 | src/vcs/adapter.py | webhook 后端为每平台一个类，新平台必须写代码；无配置驱动路径 |
| W2 | src/api/routes/webhook.py:307/406-412 | webhook **验签层**按平台 if/elif 硬编码（GitCode 事件头/GitHub X-Hub-Signature-256 HMAC）；gitee 的 X-Gitee-Signature 是 token 明文比对语义，未接入任何方言——验签必须进 D2 方言模板（验签风格：hmac_sha256 / token_compare） |
| W3 | repo_manager.py:722-747、db.py:1865、config.py:283 | webhook **注册路径**硬编码 github/gitcode 二分（webhook_url_suffix if/else、字符串 replace）——新平台 setup_webhook 拼不出自己的回调 URL；应从平台卡 webhook_path 派生 |
| B2 | managed_repos.bot_username 单列 + VCSProvider.global_bot_meta_key | bot 身份半配置化：加 collaborator 需要各平台 bot 账号名，平台卡缺 bot_identity 字段（username + 状态 meta key） |
| SC1 | worker_package.py:19 GRAPE_CLI_REPO 默认 gitcode.com/grape-dev/grape-cli.git | worker CLI 分发供应链绑定单一平台——内网/PAT-only 环境装 worker 走哪里；平台卡化后需支持 CLI 源可配（env 覆盖已有，缺的是文档与验收） |
| F1 | 前端凭据/平台 UI | PAT 填写、平台选择、登录渠道按钮等前端面是否动态渲染自配置卡未核验——实现批次需核验前端模板是否按 gitcode/github 硬编码（登录按钮已确认走 list_providers 动态） |
| W4 | routes/webhook.py:295/367 | FastAPI webhook 路由每平台一个硬编码 endpoint（@router.post("/webhook/gitcode") 等）——D2 配了 webhook_path 但路由未动态注册，新平台 webhook 仍 404；需按配置卡动态生成 /webhook/{provider} 路由 |
| E1 | OAuth 解绑/吊销 × 仓库域授权 | 解绑 external_identities 后该用户该平台授权失效——D100 终态已定义：lazy 链断供即失效（token 清空，调用报"请重新授权"，S15 实测 ei 直清）；原"oauth_fill 行悬空引用降级"对象已随落库填充退役 |
| A1 | vcs 平台卡管理面 | 登录渠道卡有 admin CRUD+缓存热失效（P2-2 现成），仓库平台卡缺管理界面/配置校验（dialect 合法、host 唯一、token_env 冲突）/热失效 |
| M1 | src/user/db.py:43 user_accounts 表 | 登录域（password_hash/token_version/gitcode_id）与仓库凭据（gitcode_api_token/github_api_token）、IM/云凭据（feishu_*/huawei_*）混在同一对象模型；取 PAT 从登录账号表挖列 |
| M2 | 全局 bot token 存 meta KV | 仓库凭据族散落的第 4 处：get_gitcode_token 全局兜底从 meta 表读，无加密/审计/权限模型 |
| M3 | feishu secret、huawei AK/SK | IM/云凭据（IntegrationCredential 族）无对象模型：user_accounts 明文列 + user_credentials 明文 JSON 混存 |
| M4 | managed_repos.webhook_secret | 1 行 16 字符明文（审计 v3.27 S3 挂账，随批次 1 收列一并处理） |
| M5 | unified_user_mapping | 双 id 运行时兜底（K4 同型，登录身份污染数据归属），审计 F1 挂账批次 0.5 |
| M6 | admin_accounts | 管理员平行账号体系，审计 G2 结论：维持既有同批处理 |
| M7 | configs/.jwt_secret | JWT 签名密钥未进 .gitignore（untracked 无保护），误提交即可伪造全部会话——一行 .gitignore 修复，随本批带上 |
| M8 | event_pipeline.py:3479 GRAPE_LINKED_REPO_TOKENS | 多仓库 bot token 明文 JSON 塞任务环境变量，agent 任意子进程可读、无 reveal 审计（批次 1 与凭据 SDK 归属 B1 一并设计注入通道收敛） |
| M9 | db_setup.py:65 im_app_secret 及 4527 行回填 | IM secret 明文 TEXT 列（im_bot_config / devops_agent_session），feishu 凭据族与 M3 同病，批次 1 IntegrationCredential 建模一并处理 |

## 范围


**本批（批次 0）**：K1-K4 消除、方言模板 + 配置驱动 webhook 层、git 层 G1-G3 去硬编码
（G4 收敛到统一 git 传输函数）、gitee 全链路接入（纯新增）。

**非目标（批次 1，另行立项）**：managed_repos 表按平台分列（bot_gitcode_token 等）泛型化
与死列收列——涉及 DDL 与数据迁移，零 DDL 纪律本批不动。

## 范围外 / 后续批次清单（2026-11-06 合并前盘点）


以下项**不在本需求（多平台解耦）范围内**，已登记冻结/挂账，不阻塞 UC1-UC20 验收。
归属批次见设计文档「挂账总清单」节：

| # | 项 | 现状 | 归属批次 |
|---|---|---|---|
| 1 | 前端 PAT 绑定入口（account.js 硬编码双平台字段） | 后端 platform_tokens 泛型入口已就绪（UC18 前置 D32/S52），前端未接 | 前端改造批 |
| 2 | 关单结果字典 legacy 双 key（前端消费契约） | S97/D72 冻结，issues.py L585 登记在案 | 关单链重构批 |
| 3 | 前端 3 处低风险 display strip | D78 挂账（功能已通，仅展示层形态） | 前端批 |
| 4 | admin whitelist 新渠道列（login_whitelist 表物理双列） | D43/S63"无列平台拒绝不折叠"，D88 冻结例外 | 登录渠道列 DDL 批 |
| 5 | 双卡体系（VCS 平台卡 × 登录渠道卡）——**分离即目标态，现状已达成**（2026-11-07 方向修正 + 三次澄清；本条非待治理问题） | 与业界主流实践一致：登录身份域/VCS 资源集成域/共享基础设施域三域解耦（依据 D10/D40；业界锚点=GitHub OAuth App/GitHub App 双连接、VeeCode SCM 轴×Identity 轴、Keycloak broker）；归因实锤=产品代码现状即目标态，零实现债（UC21 结构守卫防回潮）；默认禁止合并领域实体/权限模型/凭据语义；唯一开着的=三处薄共享的**未来评估闸门**（配置校验框架、展示元数据协议、凭据存储适配层——最高风险 gate：仅 secret reference/加密/轮换适配，不共享 token 语义与授权决策，先威胁模型后实现） | 独立方案文档先行，输出物六件：边界矩阵/共享接口契约/安全威胁模型/迁移兼容策略/触发升级条件/Owner 与评审节点；未满足触发条件不启动评估——这是未来共享基础设施的门槛流程，非对现状的修复计划，现状无需修复 |
| 6 | oauth_fill 落库消费端（D90，关联 D10；设计已拍板、实现缺消费端） | "三合一"缺落库段：注册表在、零消费点；仓库域走"用时取用"lazy 链临时等效（审计可见性/降级/PAT 优先级承载三缺口） | **先定跨域 provisioner 契约再补消费端**（禁登录域直写 grape_t_repo_credentials，S116 守卫不许绕过）；触发条件：PAT-only 平台上线/gitee/gitlab 占比超阈值/绑 PAT 反馈累积/D10 迭代排期；契约评审含粒度错配（repo_key vs 用户平台级）与 gitee oauth scope 能力矩阵实测 |

**验收口径**：上述项的存在不影响 UC1-UC20 任一用例；续审若发现新疑似遗漏，
先走设计文档「归因三步法」定性（基线预存 / 分支引入 / 登记冻结），再归类到
对应批次，不混入本需求验收。

## 验收纪律

- 用例随 CI pytest 运行（单测/结构守卫）；MySQL 方言级用例在目标环境执行，
  验收工具不入交付分支。
- K4 兜底删除前先跑双 id 错位数据检测（检测+报告，零危害，不自动改数据）。
- 回滚单位 = 一个 MR（分支 feat/weekly-08-yaw-multi-platform-decouple）。

| S83 | 平台卡文件外置（D59，先测后改，评审论证采纳）：每平台一个配置文件=能力聚合点 | 论证采纳：卡片方案>目录包（差异度/运维语义/叠加纪律三论据实测成立），但补目录包"聚合"优点——S70 漏字段根因=字段权威（dataclass）与值载体（代码 dict+DB KV）分离。新增 configs/platforms/{gitcode,github,gitlab,gitee}.json；优先级链 meta>file>代码兜底；meta 覆盖=整卡替换（admin 全字段持久化天然完整，部分覆盖语义不提供）；gitee 升级文件内置（config-only 语义退役） | src/vcs/platform_card（builtin_cards_from_dir 加载器+键⊆dataclass 守卫+_CONFIGS_DIR）/ configs/platforms/*.json ×4 / tests/test_admin_vcs_platforms（S18 语义更新） | 文件加载逐字段 parity（vs _DEFAULT_CARDS_RAW）；优先级链 meta>file；未知键整卡拒绝（error 响亮）；repo 文件键⊆dataclass 守卫；目录缺失回退代码兜底；S18 清 meta 后 gitee 回落文件默认（能力位完整） | test_s83_platform_card_files.py 5 用例（parity/优先级/未知键拒绝/文件键守卫/目录缺失回退）+ S18 语义更新 + 大回归零失败 |
## 验收纪律


- 用例随 CI pytest 运行（单测/结构守卫）；MySQL 方言级用例在目标环境执行，
  验收工具不入交付分支。
- K4 兜底删除前先跑双 id 错位数据检测（检测+报告，零危害，不自动改数据）。
- 回滚单位 = 一个 MR（分支 feat/weekly-08-yaw-multi-platform-decouple）。

| S83 | 平台卡文件外置（D59，先测后改，评审论证采纳）：每平台一个配置文件=能力聚合点 | 论证采纳：卡片方案>目录包（差异度/运维语义/叠加纪律三论据实测成立），但补目录包"聚合"优点——S70 漏字段根因=字段权威（dataclass）与值载体（代码 dict+DB KV）分离。新增 configs/platforms/{gitcode,github,gitlab,gitee}.json；优先级链 meta>file>代码兜底；meta 覆盖=整卡替换（admin 全字段持久化天然完整，部分覆盖语义不提供）；gitee 升级文件内置（config-only 语义退役） | src/vcs/platform_card（builtin_cards_from_dir 加载器+键⊆dataclass 守卫+_CONFIGS_DIR）/ configs/platforms/*.json ×4 / tests/test_admin_vcs_platforms（S18 语义更新） | 文件加载逐字段 parity（vs _DEFAULT_CARDS_RAW）；优先级链 meta>file；未知键整卡拒绝（error 响亮）；repo 文件键⊆dataclass 守卫；目录缺失回退代码兜底；S18 清 meta 后 gitee 回落文件默认（能力位完整） | test_s83_platform_card_files.py 5 用例（parity/优先级/未知键拒绝/文件键守卫/目录缺失回退）+ S18 语义更新 + 大回归零失败 |

## 用例分片路由

| 分片 | 域定义 | UC |
|---|---|---|
| [10-usecases-decouple.md](10-usecases-decouple.md) | 解耦主线用例族（S1-S11 + UC1-UC20） | 20 |
| [11-usecases-guards.md](11-usecases-guards.md) | 守卫批用例族 | UC21-UC26 + UC79 |
| [12-usecases-consumer.md](12-usecases-consumer.md) | 消费端与收官用例族 | UC27-UC42C+D101/D102（UC27/28 已退役） |
| [13-uc-test-mapping.md](13-uc-test-mapping.md) | UC↔测试 ID 对账（跨族聚合点） | — |

全局约束标注：上方 Objective/续期口径/表设计铁律/双配置轴/实现状态/现状问题/范围/范围外/验收纪律各节为**全局约束，跨族生效**——各子分片头部已注明继承。

## FR-29：身份 / 关系 / 凭据三段模型（2026-11-14 用户拍板；设计分片 d109 §4/§5/§6/§18）

### 29.1 用户需求（模型逐条接住，缺一即不通过）

| # | 需求（用户原话口径） | 判定 |
|---|---|---|
| N1 | 华为账号登录 grape | 必须支持任意已配置渠道登录 |
| N2 | 登录后绑定 gitee 的 OAuth 账号，用 gitee 的令牌驱动 gitee 仓库 | 身份可多渠道；平台级凭据可独立于登录渠道获取 |
| N3 | 没登录过某平台，只配了 PAT，也要能驱动该平台仓库 | 凭据建立关系，关系不依赖登录 |
| N4 | 没有 bot 时，托管动作（建仓/推送/评论/巡检）用用户配置的 PAT 跑 | **不得把能力切成 bot-only** |
| N5 | 管理台能看到"这个仓库平台接受哪些登录渠道"，关系只读、不可用要有原因 | 入口在仓库侧；状态可解释 |
| N6 | 主动禁用某平台后不再出现，但凭据不删 | 关系撤销 ≠ 凭据删除 |

### 29.2 三段模型（三张事实表 + 一个派生）

```
身份段:  user ──N:M── oauth_channel      (external_identities：绑定 + 令牌)
配置段:  oauth_channel ──N:M── repo_platform  (channel_repo_link：允许哪种关系，仓库侧配置)
关系段:  user ──N:M── repo_platform      (user_repo_platform_link：**只存关系与时间**)
凭据段:  user ──N:M── PAT(repo_platform) (user_credentials kind=pat)
        repo_platform ──1:N── bot_accounts (平台账号目录)
派生:    可用性 / 凭据选择 / 能力视图 —— 全部读取时算，不落表
```

硬规则：

1. **关系表只存关系**（user_id / repo_provider / linked_at / revoked_at / reason）：不存来源位、
   不存状态、不存可用性布尔、不存偏好渠道。理由：来源无渠道标识（gitee 与 huawei 都是"oauth"）、
   一次登录写多行时间无信息量、配置变更会让表内来源漂移。
2. **凭据使用保持现有逻辑（2026-11-14 拍板）**：`fetch_user_token` 的 ①PAT ②OAuth 解析序不动，
   `credential_resolver` 只做配置校验/诊断展示（drivers 声明 ∧ link relation ∧ scope 覆盖 ∧ audience 匹配），
   **不接管取 token**；命中问题以原因码呈现给用户，不静默 401。
3. **OAuth 仓库能力必须保留**：同域 + scope 足够即可用；收口对象是"无声明静默借用 / scope 不足照用"，
   不是禁止 OAuth。存量四平台在 `configs/repo-credentials.json` 声明 `drivers` 即可迁移零感知。
4. **三层链与回落不变**：`RepoBotToken → GlobalBotToken → UserToken`；**无 bot 时用户 PAT/个人 OAuth
   照常驱动托管**（N4）。actor（记在谁名下）与凭据（用谁的令牌）是两个维度，不得互相冒充。
5. **link 以 DB 为权威**：配置是首次种子，导入合并（manual 优先）；管理面写 link 标 `source=manual`。
7. **闭环规则（本次实现）**：登录/绑定成功后判断该渠道能否**驱动**平台（同域，或 link 声明
   `identity_and_credentials`），能驱动即 upsert 关系行并**异步刷新项目列表**（`user_repos` 标记过期，
   下次 `/repos/member` 回源）；解绑时判断是否还有其它来源（其它活跃渠道 / PAT），**有 PAT 就保留关系**，
   都没有才 `revoke(oauth_unbound)`；A 已登录再绑定 B' → 按 B' 的 link 追加关系行并刷新列表。
6. **关系写入三条路**：OAuth 登录、**OAuth 绑定**（此前漏写，已补）、PAT 保存（不经渠道直插）；
   撤销在"该平台再无其它来源"时写 `revoked_at + reason`（user_unbind / credential_deleted /
   oauth_unbound / admin / disabled_by_user）。

### 29.3 需求 ↔ 模型 映射与现状（实查，不掩盖）

| 需求 | 模型要素 | 现状 |
|---|---|---|
| N1 | `external_identities` + `oauth_channels` | ✅ |
| N2 | `ei(gitee)` + `link(gitee→gitee)` + `caps(gitee).drivers` 含 `oauth_identity` + `oauth_requires_scope` ⊆ 渠道 scope | ⚠️ 模型成立、**现实受阻**：gitee 渠道 scope=`user_info`（缺 projects/issues），github=`read:user user:email`（缺 repo）→ 现算给 `SCOPE_INSUFFICIENT`；只有 gitcode（scope=all）现成可用。补救=补充授权（应用 B）或 PAT |
| N3 | `user_credentials(kind=pat)` + 关系直写 | ✅（hooks 已实现） |
| N4 | 三层链回落 | ✅（本批不改链序） |
| N5 | `link.source=manual` + reason 码 | ⚠️ 后端链路已定，管理面写接口与前端未开（规划） |
| N6 | 关系 `revoked(disabled_by_user)`，凭据不动 | ✅ 模型/hook 已实现，管理面按钮待接 |

不成立项一律以原因码呈现（NO_PAT / NO_CHANNEL / LINK_MISSING / DRIVER_NOT_DECLARED /
SCOPE_INSUFFICIENT / AUDIENCE_MISMATCH / TOKEN_EXPIRED），**不允许静默 401**。

### 29.4 显式不做

- 关系表不加 status / 来源 / 偏好列；偏好真需要时单开表（不预留列）
- 不建平台级 webhook secret 表（仓库级凭据已权威）
- 不做组织/租户维度（第二批另立模型）
- 不在本批实现跨名换域（仓库 OAuth 应用 B）——列第二批，契约待实弹

### 29.5 验收（对应设计分片 d109 §15）

UC-D109-28（绑定写关系）/ 29（只配 PAT 可见）/ 30（PAT 优先，source 可审计）/ 31（scope 不足给原因）/
32（同域渠道令牌可用，**锁住 OAuth 能力未被砍**）/ 33（link manual 优先）/ 34（用户禁用平台）。

补充回归：**无 bot 时托管链路仍用用户 PAT**（N4 反证：把三层链改成 bot-only 即必须红）。

## FR-30：管理面接口按领域对象建模（2026-11-14 用户拍板；设计分片 d109 §19）

### 30.1 问题

初版读接口用 `channels` / `platforms` 复数泛称，丢了领域限定词（OAuthChannel / RepoPlatform）；
`CredentialCaps` / `ChannelRepoLink` / `UserRepoPlatformLink` 三个聚合**没有路径**（被塞进
channels/platforms 的响应字段里）；派生视图 `capabilities` 混在对象域前缀下 → 路径读不出模型。

### 30.2 目标形态（路径 = 聚合根）

| 聚合（路径 = 领域对象名，无父容器） | 路径（`/api/admin` 下） | 方法 |
|---|---|---|
| OAuthChannel | `/oauth-channels`、`/{channel}`；子资源 `/{channel}/credentials`、`/{channel}/enabled` | GET；PUT |
| RepoPlatform | `/repo-platforms`、`/{provider}`；子资源 `/{provider}/enabled` | GET；PUT |
| ChannelRepoLink | `/channel-repo-links`、`/{oauth}/{repo}` | GET / PUT(manual) / DELETE |
| UserRepoPlatformLink | `/user-repo-platform-links?user_id=`、`/{user_id}/{repo}` | GET / DELETE(禁用) |
| BotAccount | `/bot-accounts?provider=`、`/{provider}/{username}` | GET / PUT / DELETE |
| 派生视图 | `/capabilities`、`/user-platforms?user_id=` | GET |

### 30.3 硬规则

1. **路径 = 领域对象名**，不造父容器（无 `/platform-objects/*`；credential-caps 聚合已随 repo-credentials.json 删除）；item 标识只用该对象领域键（provider / (oauth,repo) / (user,repo) / (provider,username)）
2. **没有整卡 PUT**；定义字段只读，写入 = 改配置文件 + 导入；运行时只写 `enabled` / 凭据 / bot 账号 / manual link
3. 凭据只回"有没有"（布尔），值与前缀永不出库；bot 响应只回 username/is_default/status/last_used_at
4. 派生视图独立域（`/views/*`），`available` 与真实取 token 口径一致（关系有效 ∧ 有凭据来源），
   scope/audience 只进 `diagnosis`
5. 错误口径：404 带 code+resource+id；409 唯一冲突；422 校验失败；503 表未就绪（写不静默、读标 `source=fallback`）
6. 守卫：**不得出现虚构父容器**（没有 PlatformObject 这个聚合，`/platform-objects/*` 禁止）；路径名必须是领域对象名；不得出现整卡 PUT（AST/grep 断言）

### 30.4 验收

UC-D109-35（五聚合集合 GET，凭据只回布尔、路径名=聚合名）/ 36（单条 GET 与 404）/
37（channel-repo-links PUT 标 manual，导入不覆盖）/ 38（视图独立域，available 与真实口径一致）/
39（契约守卫：无裸名、无整卡 PUT）。


## FR-31：仓库驱动判据＝渠道关系配置（2026-11-14 用户拍板；设计分片 d109 §3/§5/§6/§15 UC-40~44）

| 编号 | 规则 | 类型 |
|---|---|---|
| FR-31.1 | 渠道↔平台关系独立配置文件 `configs/channel-repo-links.json`，条目只有 `oauth_provider` / `repo_provider`（无 relation 档位） | 配置契约 |
| FR-31.2 | 判据唯一：配了 (c → p) ⇒ c 的令牌可驱动 p、OAuth 登录/绑定即自动写 `grape_t_user_repo_platform_link`；没配 ⇒ 不驱动、不建行 | 行为锁（UC-40/41） |
| FR-31.3 | relation 档位 / drivers / scope / audience 一律退出判定；能力是否真够由上游 API 在调用时回答（403/401） | 行为锁（UC-42） |
| FR-31.4 | PAT 永远第一且不依赖任何渠道关系；`GRAPE_OAUTH_TOKEN_FALLBACK=0` 是运维硬关（一律不用渠道令牌），未设时按关系配置放行 | 行为锁（UC-43） |
| FR-31.5 | 文件缺席 → 按 provider 同名默认配对（source=derived）；文件存在即权威（links 为空 = 显式关闭全部关系）；跨名配对同样生效 | 行为锁（UC-44） |
| FR-31.6 | `relation` 档位**全链删除**（DDL/ORM/Repository/领域枚举/请求体/管理面 UI），迁移 202611170011 执行 DROP COLUMN；配对存在即授权，不存在「保留兼容」的列（UC-46） | 结构契约 |
| FR-31.7 | 清空 PAT 的级联（`_cascade_pat_clear`）必须调用 `hooks.on_pat_deleted` 同步关系表；前端引导门以关系优先，残留 currentRepo 不得绕过引导页（UC-45） | 行为锁 |
| FR-31.8 | `configs/repo-credentials.json` 删除；`grape_t_repo_credential_caps` 的存活字段（pat_create_url / token_storage_field / webhook_token_field / bot_username / owner_fallback）并入 `grape_t_repo_platforms`，drivers / oauth_requires_scope / supports_* 语义取消；迁移 202611170010 负责补列+回填+删表（cleanup 语义） | 结构契约 |
| FR-31.9 | 引导门（前端三处收口）：`_bootRestore` 先判关系、不复用 URL 深链/残留 `currentRepo`；`_hasVcsBinding` 只认关系快照（缺失=无关系，不回落到 identities）；账号页清 PAT 后必须重取 `/auth/platform-links` 并在 `has_any=false` 时清残留、落引导页（UC-48/49） | 行为锁 |
| FR-31.10 | 管理面登录渠道/仓库平台一律「列表 → 详情」：列表含状态、健康与计数；详情承载只读定义字段与写面（渠道：client_id/secret + 渠道↔平台配对；平台：bot_username/PAT 地址 + bot 账号增删与 token 轮换）。渠道↔平台不再有档位控件（UC-50） | 接口/UI 契约 |
| FR-31.11 | 平台凭据页只保留部署级配置（webhook_url + runtime/devstage 运行参数）；平台/渠道凭据一律在各自菜单写，保存体不提交跨域字段（杜绝双入口互相覆盖） | 结构契约（UC-50） |
| FR-31.12 | 同域（渠道 == 平台）**隐式可接入**：运行时 `_targets` = 跨名配对 ∪ 渠道自身；配置/导入不得派生或写入 `(c→c)` 行（显式写入告警忽略）；管理面 `PUT /channel-repo-links/{c}/{p}` 同域返回 422 `SAME_DOMAIN_PAIR`；迁移 202611170012 清理存量同域行（UC-51/52/53） | 行为+数据契约 |
| FR-31.13 | 对外术语：平台集合叫**代码托管平台**、关系叫**可接入的代码托管平台**；「登录渠道」保留（与对象模型同名），说明改为「查看已支持的登录渠道并可更新各渠道配置」；代码标识符不随文案改名（UC-54） | 文案契约 |
| FR-31.14 | 文案/bundle 改动必须同批 bump 两个版本位：`admin-view.js?v=`（bundle）与 `I18N.init('<ver>')`（语言包 `?v=`）；漏 bump 的症状=新词条空白/回显原始 key、旧 bundle 不认新视图（UC-54） | 发布纪律 |
| FR-31.15 | 托管请求的平台判定：**前缀即平台**（前端任何在册平台前缀都取到 provider，不再二分 github/gitcode）；后端在无 provider 线索时先用本账号 `user_repos` 缓存反查（唯一命中才采用，同名跨平台不猜），最后才走 token 可用性兜底（UC-60/61/62） | 行为契约 |
| FR-31.16 | 仓库归属解析：`owner.login` → `namespace.path/name`（Gitee v5）→ `full_name` 第一段；`full_name` 缺失时用 `owner/name` 反拼，保证「owner == 我的 login ⇒ admin」判定不落空（UC-63） | 数据契约 |
| FR-31.17 | DDL 迁移必须按**最终状态复检**并在不满足时 `raise`（执行器：抛错=不盖章）；已知漏检项 202611170010（5 列 + caps 表）与 202611170011（relation 列）（UC-64） | 发布纪律 |
| FR-31.18 | 前端与后端**不得出现二元平台枚举**：显示名/域名/品牌色/图标一律平台目录派生；前缀↔平台用 `providerFromRepoKey`/`repoKeyOf`；「是否接入」以用户↔平台关系为准（OAuth 身份行不作判据）；后端能力判定按平台卡在册（UC-65） | 结构契约（棘轮守卫锁定） |
| FR-31.19 | 顺序与集合一律数据驱动：登录渠道集合=配置+meta+表；渠道展示序=`_ui_order`/配置键序；托管兜底序=`hosting_providers()` 卡目录；bot 名历史池走 `_HISTORICAL_*` 键映射。源码中不得再出现渠道/平台顺序字面表（UC-66/67/68） | 结构契约（棘轮守卫锁定） |
| FR-31.20 | `user_repos` 缓存写入按平台范围生效：只替换本次刷新成功的平台，失败平台保留旧缓存；全量覆盖仅限显式 `providers=None`（UC-69） | 数据契约 |
| FR-31.21 | 引导页绑定成功（OAuth/PAT）必须离开引导态进入项目页：出口 = `goToRepos()`（摘 `ob-mode`、置 `_currentView='repos'`、改路由），且跳转前先刷新关系快照（`/auth/platform-links`，失败用 `/auth/profile.repo_platform_links` 兜底）；文案 key 解析须跨 `app.account.*`/`app.login.*` 两个命名空间，且两组件监听语言切换事件（UC-70/71） | 行为+文案契约（守卫锁定） |
| FR-31.22 | 仓库维度读路径（`<域>/{owner}/{repo}`，URL 不带平台前缀）必须解析真实前缀：已托管仓走 `resolve_managed_repo_key()`；批量查询走 `repo_key_candidates()`；不得直接用 `to_repo_key("{owner}/{repo}")` 当数据 key（UC-72/73/74） | 结构契约（守卫锁定） |
| FR-31.23 | 引擎 scope 的 repo 级凭据匹配、会话 token 作用域校验、IM 显示前缀剥离，一律按平台卡候选集/结构化判定，不得写死 `gitcode:/github:` 双前缀（UC-73/75/76） | 结构契约（守卫锁定） |
| FR-31.24 | 关系变更钩子（OAuth 登录/绑定、PAT 保存/删除、解绑、平台停用）触发项目列表刷新时必须按**原值**传递 user_id，禁止 `int()` 强转（D105 起主键为 UUIDv7 字符串）；刷新失败只告警不阻断（UC-77/78/79） | 行为契约（行为级用例锁定） |
| FR-31.25 | 版本位登记侧与页面实际值必须同步：`LOCALE_FINGERPRINT` / `CACHE_VER` / `I18N_JS_VER` / `FROZEN` 四张登记表随文案与 bundle 改动同批更新（否则守卫红着进主分支，等于 §20.8 纪律失效） | 发布纪律（ratchet 锁定） |
| FR-31.26 | 关系写入必须单语句原子完成（方言 upsert），不得 UPDATE→INSERT 两步；并发冲突即复活而非抛错回滚（UC-80） | 数据契约 |
| FR-31.27 | 登录渠道 `enabled=0` 必须 fail-closed 覆盖**直达入口**（`/auth/login/<p>`），不能只过滤目录；表不可读时 fail-open 以保可用性（UC-81） | 行为契约 |
| FR-31.28 | DDL 切分按词法扫描（注释/引号感知），半建表按模型逐列补齐；两者都以"最终状态"判定而非"调用未抛异常"（UC-82/83） | 结构契约 |
| FR-31.29 | 按平台前缀操作 `user_repos` 必须用前缀相等（`substr(...)=?`）而非 `LIKE`；provider 名允许下划线，`_` 是 LIKE 单字符通配符，会误删同形前缀行（UC-84） | 数据契约（红绿用例锁定） |
| FR-31.30 | 被每请求调用的配置读取函数必须走缓存（键含 mtime/size 以便带外改动自动失效），不得每次 open+parse（UC-85） | 性能纪律 |
| FR-31.31 | 方言 upsert（SQLite `ON CONFLICT DO UPDATE` / MySQL `ON DUPLICATE KEY UPDATE`）**不参与 ORM identity-map 同步**：写入后必须过期该行已加载实例（只 `expire(row)`，不得 `expire_all()`——会波及调用方同 session 的其它脏对象）；否则同 session 内后续读拿到陈旧 `revoked_at`（UC-86） | 数据契约（行为级用例锁定） |
| FR-31.32 | 前缀 LIKE 守卫必须覆盖**内嵌 f-string** 形态（`LIKE '{provider}:%'`）：原 `:%"` 正则只拦参数形态，拦不到写进 SQL 文本的 `:%%`。唯一豁免 `mgmt/user/db.py::_path_b_prefix_clause`（只读查询 / provider 取平台卡枚举 / 默认平台需匹配裸键的存量语义），豁免数量用 `==` 登记，新增或减少即红（UC-87） | 结构契约（守卫锁定） |

## FR-32：bot 单一归属（D110，2026-11-14 用户拍板；设计文档 d110-bot-single-home-design.md）

背景：D109 把 bot 设计成"平台级多账号目录 + is_default"，但运行时取 token 链里从来没有这张表，
管理面写的没人读、真源没入口（82502db1f 删掉全局 bot 输入后彻底断）。结论：**归属与粒度设计错误**，
不是实现缺口；修法是把 bot 挪到平台行、删表。

| 编号 | 规则 | 类型 |
|---|---|---|
| FR-32.1 | bot 归属 `grape_t_repo_platforms` 一行（**一个平台一个 bot**）：`bot_username`（事实，非配置声明）/ `bot_token_enc`（密文）/ `bot_token_status` / `bot_checked_at`；仓库级覆盖仍在 `grape_t_repo_credentials(repo_key,'bot')`；**不建仓库↔账号绑定表、不做多账号/选号/池**（UC-D110-01） | 结构契约 |
| FR-32.2 | `grape_t_bot_accounts` 及其全部代码面删除（模型 / 仓储 / `/admin/bot-accounts` 路由 / DDL 表与部分唯一索引 / MySQL 生成列等价实现 / 前端多账号面板）；**不写 DROP 迁移**——该表只存在于跑过本分支验证轮的库，发布面从未建过，残留表手工清（命令见 d110 §5）（UC-D110-08） | 结构契约 |
| FR-32.3 | 取 token 读序：仓库级凭据 → 平台行 `bot_token_enc` → meta `global_bot_<p>_token` → 配置文件（仅迁移标记未置位）；bot 身份读序：`managed_repos.bot_username` → 平台行 → 平台卡 → meta。级别与链序不变（UC-D110-02） | 行为契约 |
| FR-32.4 | 写口收敛到聚合根：`PUT /admin/repo-platforms/{provider}/bot`（username/token，token 留空=不改、落库前加密、响应永不含 token）、`DELETE …/bot`（清 token，保留名字）；旧 `PUT /admin/config` 的 bot 字段保留为兼容壳并转写平台行（UC-D110-03） | 接口契约 |
| FR-32.5 | 导入写保护 + 空值播种：`bot_token_enc/bot_username/bot_token_status/bot_checked_at` 进 `_PROTECTED`；其中 `bot_token_enc/bot_username` **仅当当前为空时**接受配置/平台卡播种，之后导入永不覆盖；`enabled` 等维持硬保护（`0` 是有效状态，不被配置复位）（UC-D110-03） | 数据契约 |
| FR-32.6 | 轮换传播：平台 bot token 变更（管理面端点或旧 config 保存两条路径任一）必须触发 `global_bot_token_swap` 批量刷新 path A 仓库的 webhook 验签 token；**首配（旧值为空）与同值不触发**。漏了这条 = 所有已托管仓库 webhook 立刻 403（UC-D110-04） | 行为契约（红绿用例锁定） |
| FR-32.7 | 能力视图：`supports_bot_token` / `bot_accounts` 摘要来自平台行（一平台一行；`token_present` 决定 `active`/`disabled`），`bot_username` 回显平台行；不再有"表里有行就算支持"的假阳性（UC-D110-07） | 接口契约 |
| FR-32.8 | 巡检写平台行：`bot_token_status` / `bot_checked_at` 为真源，meta `global_<p>_bot_token_status` 兼容期双写；`mark_bot_status` 不再零调用者（UC-D110-06） | 行为契约 |
| FR-32.9 | `bot_usernames` 池丢弃（一平台一 bot）；bot 名单 = 各平台行 `bot_username` ∪ 平台卡 `bot_username`（UC-D110-05） | 数据契约 |
| FR-32.15 | 用户面平台可见性（方案甲）：唯一判据 = `grape_t_repo_platforms.enabled`（`visibility.is_platform_visible`；`meta.vcs_platforms_hidden` 由迁移 202611170021 退役）；目录不返回 disabled 平台、`?provider=` → 403 `PLATFORM_DISABLED`（无绑定同样 403）、合并列表不拉取；内网写入口照常可用；管理面项带 `disabled`；关平台需影响面预检（409 + `?confirm=1`）；全禁用时目录为空且不回退平台卡（UC-D110-14/16/17） | 可见性契约（用户拍板：甲） |
| FR-32.18 | 管理面字段说明气泡必须脱离容器裁切：Teleport 到 body + `position: fixed` + 按触发器矩形定位 + `z-index: 3000` + 滚动/缩放重算；禁 `position: absolute`（会被 `overflow:hidden` 容器裁掉）（UC-D110-19） | 交互契约 |
| FR-32.19 | 关闭平台必须用**页面内 Vue 弹层**确认（自带 `position:fixed/inset:0` 样式，不依赖设计令牌、不用 `window.confirm`），展示影响面数字，确认后带 `?confirm=1` 重发；取消回滚开关。后端关闭一律要求确认（UC-D110-20） | 交互契约（用户拍板） |
| FR-32.20 | `enabled` 双语义的文案分叉：登录渠道=`是否允许登录`（403 CHANNEL_DISABLED）、代码托管平台=`是否在用户面显示`；两页分别映射 `field.enabled_channel` / `field.enabled_platform`（UC-D110-21） | 契约一致性 |
| FR-32.17 | 默认不展示的平台写在初始化代码：`DEFAULT_HIDDEN_PLATFORMS`（当前 `{"gitlab"}`）只在**新建平台行**与**迁移 202611170021** 生效；`enabled` 属导入保护列（配置永不覆盖）、orphaned 恢复视为可用（`enabled=1`）；平台卡不承载可见性（UC-D110-18） | 部署口径（用户拍板 B：写死初始化） |
| FR-32.16 | `webhook_url` 语义=公网基址（非平台回调全路径）：平台路径由平台卡 `webhook_path` 追加，配置里的 `/webhook/xxx` 被整体替换；本轮**只补注释/示例提醒，不改行为**；缺失时托管注册 422 + 健康告警（UC-D110-15） | 配置口径（用户拍板：不合并两键） |
| FR-32.14 | 设计/需求文档与源码不得含真实凭据：示例只写占位形态（`<REDACTED_...>` / `<your_...>` / CHANGE_ME / ${ENV}），真实 token / webhook secret / 口令一律不写入；守卫 `test_no_secrets_in_docs_and_src` 实跑为门（UC-D110-13） | 安全红线（用户要求） |
| FR-32.11 | 渠道卡 / 平台卡展示契约：名称取 `provider_name.<provider>`（zh/en 两语言包，配置 name 只兜底），图标取 `/static/img/<provider>.svg`（独立文件必须 xmlns + viewBox + `?v=`），无图回退文字；huawei 定名 华为 / Huawei（UC-D110-09/10） | 展示契约 |
| FR-32.12 | 登录渠道管理页不提供"渠道↔平台配对"设置：移除"可接入的代码托管平台"分节与 `channel-repo-links` 写入口；关系只由配置文件导入与同域隐式产生，运行时判定不变（UC-D110-11） | 范围收缩（用户拍板） |
| FR-32.13 | 展示层变更的缓存版本纪律：改语言包 → 四页 cacheVer + LOCALE_FINGERPRINT，改管理台视图 → `admin-view.js?v`，改图标 → `?v=`；守卫用例实跑为门（UC-D110-12） | 工程纪律 |
| FR-32.10 | 初始化种子文件 `configs/global-bot-token.json`（`.gitignore`；兼容名 `platform-bots.json` 亦认）：支持 `platform_bot[]` 数组（`provider`/`bot_name`/`bot_token`，别名 `platform_type`/`username`/`token|bit_token`）与旧扁平键并存，解析器唯一（`read_bot_seed_file`）；迁移 202611170017 只填空位、加密写入平台行，运行时不读文件；沿用原名 `global-bot-token.json`，中间版本名 `platform-bots.json(-.example.json)` 兼容一版（UC-D110-09） | 配置契约 |

## FR-33：升级不丢平台关系（2026-11-16 用户拍板；设计分片 d109 §20）

**问题（脱敏）**：一次部署后用户反馈「配了 PAT，但平台关系不出现」——用户面
`/auth/platform-links` 恒空、仓库页落到「无仓库平台绑定」引导页；接口层全是 200。
定位结论：① 平台对象四张表没有域归属（四进程各建一份或整批被归属守卫跳过）；
② 迁移台账与实表不一致（`202611170030` 已盖章，四表却不存在），PAT 保存钩子
写 `grape_t_user_repo_platform_link` 报 `no such table`，异常被 fail-safe 吞成一条 warning。

**要求（四条，全部可机器验证）**：

| # | 要求 | 验收 |
|---|---|---|
| R1 | 四表单一 owner = mgmt；DDL 一律过表归属守卫；`grape_t_repo_credentials` 补列只在 insight 进程执行 | `tests/api/test_platform_object_domain_boundary.py` + `test_table_ownership.py` |
| R2 | runtime/insight 禁止 ORM 直连平台对象表；跨域读写只走 `mgmt.public` 具名查询/写原语，凭据列不出域 | 同上（源码断言 + 列白名单） |
| R3 | 台账说谎可自愈：新增幂等迁移 `202611170041 ensure-platform-object-tables`，表缺失即补建 | `tests/api/test_platform_migration_self_heal.py` |
| R4 | 部署前自检：角色 env / 台账非空 / 同号同名 / owner 表实存；FAIL 不得启动 | `tests/api/test_deploy_preflight.py` + `scripts/deploy_preflight.py` |
| R5 | 凭据全部失效（清 PAT / 平台吊销）时工作区降级为引导页（200 + `_no_vcs_binding`），文案按来源区分 OAuth「重新授权」/ PAT「重新配置」；显式 `?provider=` 仍 401 | `tests/mgmt/test_repos_member_reauth_degrade.py` |
| R6 | 工作区 / 我的项目 / 项目模板 三入口判据=用户↔平台关系表；进页前刷新关系快照；来源全无的残留关系读口自愈撤销 | `tests/api/test_workspace_entry_relation_gate.py` + `tests/mgmt/test_d109_pat_relation_scenario.py::test_dangling_relation_is_pruned_when_no_source_left` |
| R7 | 判据叠加凭据可用性：身份行 token 解不出（历史派生密钥）且无 refresh/无 PAT 时不算已接入，返回 `needs_reauth` 提示重新授权；member 不再因此 401 | `tests/mgmt/test_dead_identity_not_counted.py` |

**用户可见链路的验收口径**：PAT 保存 → 关系行写入 → `/auth/platform-links` 返回该 provider
（`source=table`）→ 仓库页不再走引导页；删除 PAT → 仅该平台撤销，其它平台保留。
场景用例 UC-D109-45~48（`10-usecases-decouple.md`），测试见 `13-uc-test-mapping.md`。

**范围外**：全仓既有「裸跨域读」欠账（设计文档 §2.2 统计）与 252 个跨域视图退役，不在本 FR 内。
