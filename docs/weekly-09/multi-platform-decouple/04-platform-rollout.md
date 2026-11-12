> ⚠️ 本文为拆分分片（内容唯一权威源）。索引导航：./multi-platform-decouple-design.md
> 来源：multi-platform-decouple-design.md L769-L913, L985-L1069, L1146-L1405, L1443-L1462（原文原样搬运，零改写）。
> 域定义：核心落地主线——gitee 实弹批、持久化 ORM、存储单表生命周期、方言注册。
> 全局约束：继承索引页「表设计铁律/实现纪律/验收纪律」（见索引页）。
> ⚠️ 阅读顺序：本分片 D53-62 与 05 分片 D63 交错（05 在 D62 与 D64 之间）——连续阅读该段按 **本片(D53-62) → 05(D63) → 本片(D64)**；04/05 交错的归属依据见索引页「阅读与结构说明」。

### D41：gitee issue 写路径方言收口（S61，先测后改，实弹观测驱动）

- **实弹背景（2026-11-03，S12 首轮）**：用户提供 gitee PAT，对真实仓
  example-org/example-repo 实弹——verify_token/列仓库/repo_info/查重分页/
  list_branches/GET 单条全部即绿（读路径 repo-path 与 GitCode 同形，
  零改动）；写路径暴露两个真方言差异。
- **实弹观测（真实 API 形态，已录制为断言）**：① 建单 = POST
  /repos/{owner}/issues + form {repo: 仓名, title, body}（非 GitCode 的
  /repos/{o}/{r}/issues）；② 关单 = PATCH /repos/{owner}/issues/{ident}
  + form {state: closed, repo: 仓名, title}；③ issue number 是字符串
  ident（"IKSQ5F"）——查重 int() 强转会炸。
- **实现（能力位，非平台分支）**：卡新字段 issue_write_form
  （repo_path=存量默认 / owner_param=gitee 形态，Builder 校验），
  create_issue/close_issue 按能力位派生路径与 body（读路径两形态同形
  零改动）；查重 number 走 _native_issue_number（int 平台仍 int，ident
  平台返 str，端口契约=平台原生形态）。生产 meta 的 gitee 卡补配
  issue_write_form 后实弹复测：建单→查重命中（ident）→读回→关闭→
  终态 closed 全绿。
- **等价锁**：gitcode 建单（repo_path + form {title,body}）与关单
  （PATCH repo_path + dict {state: close, repo: owner/repo, title}）
  逐参数行为锁；github 同形态默认路径不变。
- **遗留登记**：gitee webhook 建删（hooks API 形态）与 bot 协作者
  邀请形态未实弹（需建 webhook/加协作者的写权限场景，随 S12 后续
  验收项进行）。

### D42：repos.py 托管主路径收口（S62，先测后改，用户领域纠偏）

- **用户纠偏记录**：对"剩 66 处调用方残留可缓"的外部质疑判定成立——
  判据"第 5 平台走不走这条路"是循环判据（gitee 是 PAT-only 特例，用
  特例证普遍=幸存者偏差）；实弹对 repos.py 覆盖率为 0，"未检验"≠
  "已知安全"；"不变量立住"是守卫范围定义的不变量（定义漂移）。
  用户拍板：**写法本身不对就该修（策略模式/工厂/配置驱动），判据是
  分派写法对不对，不是会不会踩**。
- **扫描定位（36 处同一形态）**：`if provider == "github": <github 流>
  else: <GitCode 存量流>`——分布：create_new_repo 7 / remove_managed_repo
  6 / _probe_stale_repos 5 / host_repo 5（两段 200 行平行实现）/ repair_host
  5 / 权限闸门 x2 + 杂项 8。非 PAT-only 第 5 平台全部折叠打 GitCode API。
- **实现（迁移到既有工厂+端口，非新抽象）**：调用方零平台分支——
  ① token 统一 get_user_token_for(provider)（D37 等价锁）；
  ② webhook 查/删/建走适配器端口 find_existing_webhook/setup_webhook/
  remove_webhook（新增前两端口，存量委托原实现）；
  ③ 建仓新端口 create_repo；仓库列表新端口 fetch_repos_with_role；
  权限查询新端口 get_collaborator_permission（GitCode dict/GitHub str
  双形态归一 _perm_at_least_maintainer）；
  ④ host_repo 双流合一（权限前置/bot 协作者/路径 A→B 降级/webhook/
  列名全端口化，GitHub 流与 GitCode 流语义归一：admin|maintain ≡
  access_level>=40）；
  ⑤ 两个权限闸门合一 _require_vcs_maintainer（unmanage/migrate 薄委托）；
  ⑥ 探活卡派生 _probe_url_headers（URL/头/鉴权形态按卡，token map
  数据化；hooks 404 语义**行为派生**——重查仓库本体 200=权限跳过、
  404=stale，替代平台名分支）；
  ⑦ 展示名/host/目录集合卡派生（_card_name/_card_host/_catalog_platforms）。
- **验收门（文件级）**：repos.py AST 守卫——平台名字面量不得出现在
  任何比较节点（==/!=/in/元组）；传 provider 值参合法。RED 时 36 处，
  迁移后归零。
- **等价锁**：host_repo gitcode parity（token/password 回填语义、列名、
  add_managed_repo kwargs）；端口契约（generic form/URL 形态实弹录制，
  存量委托逐参 mock）；_fetch_and_cache/_repo_platform_token 双平台。
- **接缝迁移**：set_repo_bot_token 的 get_user_token_with_refresh→
  get_user_token_for（D35.1 纪律，旧用例同步更新）。
- **语义微调（有意，登记）**：hooks 404 行为派生使 gitcode 病理场景
  （repo 200+hooks 404）从"误判 stale"变"跳过"（零危害方向）；裸键
  repo_key 归单配置平台（github-only 时裸键从排除变归入）；错误文案
  平台名参数化（card.name）。

### D43：凭据/列名族卡化——admin 面 + settings + bot 健康 + 路径 B 查询（S63，先测后改）

- **定位**：D42 后剩 30 处调用方站点的第一族（凭据/列名，11 处+扫描
  补充 6 处同文件漏网），与 D38 卡派生列同型：
  config_store（swap 触发 provider 口径/列名/权限默认）、settings
  （repo config 列名 + PAT 保存前探测 URL + legacy PAT 平台集合）、
  bot_token_health（仓库级 token 列）、user/db（_query_path_b_repos
  前缀子句 + webhook URL 无卡回退）、admin/db（登录白名单列映射）。
- **实现**：列名一律 create_provider(p).token_storage_field()/
  webhook_token_field()；权限缺省=适配器存量默认；探测 URL/鉴权形态
  按平台卡（query_token→参数，其余→Bearer 头）；provider 口径从
  字段名派生（field.split）；**路径 B 前缀子句目录派生**
  _path_b_prefix_clause——默认平台（内置卡序首=gitcode）匹配自身
  前缀或裸键（无前缀存量数据，语义=旧 NOT LIKE 'github:%' 泛化），
  其余平台精确前缀；登录白名单列映射表（无列平台拒绝不折叠写别人
  的列，新渠道列 DDL 归批次 1）；legacy PAT 专用字段平台集合常量化
  （数据非分支）。
- **验收门**：config_store/admin/db/settings/bot_token_health 四文件
  AST 守卫（平台名比较零）；user/db 含平台名访问器存量函数
  （get_gitcode_token 等），只守本单收口三函数。
- **等价锁**：gitcode parity（路径 B 列+裸键语义、白名单列、swap
  流）；gitee 新路径（bot_gitee_token 列、卡派生 URL）。
- **语义微调（登记）**：webhook URL 无卡回退不再做 github 字符串替换
  （内置三平台恒有卡，分支实际不可达，行为等价）。

### D44：展示/IM/读侧族收口（S65/S66，先测后改）

批次 1（S65，60fbc36e4，5 处）：repo_key 方言回退→映射表；git_repo_api
无卡回退→常量集合；issues.py 冗余 provider 折叠消除；issues_center
auto-close token→TokenResolver 统一链；runtime/service 过期提示平台名→
卡派生；前端 gitee 官方品牌红 + 未知平台通用 git 图标（repos.js ?v=
只升纪律 62→63）。

批次 2（S66，11 处）——判据仍是分派写法（平台名比较节点），非会不会踩：

- **scenes 分支端口 ×2**：list_repo_branches / create_repo_branch 三路
  （github 手拼 URL / gitcode 存量 / config-only）合一为 VcsApiClient
  命令层。新命令 `create_branch(branch_name, refs)`：方言模板（gitlab
  =POST /repository/branches 参数建）+ 卡能力位 **branch_create_form**
  （form=Gitee 系 v5 POST /branches {branch_name,refs} 存量默认；
  refs_sha=GitHub 查 base sha→POST /git/refs 两步存量形态，github 内置
  卡置位）。token 统一 get_repo_token_with_refresh（TokenResolver 策略
  链）；**登记语义差**：github 读侧不再走 get_github_token 的 D32
  owner-identity 补丁链（读侧，缺 token=503/空列表，零破坏半径）。
- **templates ×4**：_load_global_bot_config→来源链
  （get_global_bot_token_for + resolve_bot_username）；README 拉取→
  VcsApiClient.get_readme 读侧方言模板（/readme→/contents/README.md→
  小写回退探测链，token=平台卡用户级 env，空=匿名）；显式平台 token→
  get_user_token_for(provider 传参)（K1 口径，gitcode 专属包装退役）；
  建仓+取 login→create_provider().get_user_login/.create_repo 端口
  （存量子类委托，parity）。
- **feishu_async ×2**：协作者拉取→新端口 **list_collaborators**
  （generic=GET /repos/{o}/{r}/collaborators 卡驱动分页；gitcode/github
  子类委托 feishu_async 存量实现）；um 写入策略→repo_key 新白名单
  **um_login_is_grape_account**（login 与 grape 账号同源=仅存量 gitcode，
  同源写 gitcode_user 列；异源（github/新平台）按平台 login 解析 uid 后
  只绑飞书身份——FR-21.2。中途曾误用 um_column_for_provider（github
  返回 github_login 真值会误写 gitcode_user），行为级用例锁死）。um 表
  退役归批次 1。
- **repo_roles ×2**：_get_gitcode/_github_login→_get_platform_login
  (user, provider) 身份域按 provider 检索；_check_gitcode/_github_admin→
  适配器端口 get_collaborator_permission + 形态归一 _is_admin_permission
  （dict=GitCode 判定链【role_name 最前，#491】/ str=GitHub role_name /
  None=generic fail-closed）；with_oauth_token 直传 provider。
- **outbound_ledger ×1**：extract_event_content_id 按 payload **形态**
  派生（行为派生：comment.id 在=评论事件优先，object_attributes.id=Gitee
  系 Note Hook；provider 参数仅保签名兼容不参与分支）——gitcode/github/
  generic 三口径行为不变（Gitee 系 Note Hook 无顶层 comment 键）。
- **mock 接缝迁移（D35.1）**：scenes 分支缓存用例 _req→VcsApiClient
  命令方法；491/393 用例 _check_*_admin→_is_admin_permission（真实形态
  payload 用例保 _req 接缝——经 get_member_permission 委托仍有效）；
  templates 用例模块属性→GitCodeProvider 类方法。
- **回归口径**：定向集（s62-s66+接缝迁移四文件+feishu/ledger/scene 族）
  绿；基线 stash 对照零新增失败。追加批次（793b84249）：12 个存量基线
  失败归零——outbound_ledger×9 陈旧接缝（改名+命令入口+MagicMock
  __enter__ 陷阱：with urlopen 场景须 fake_resp.__enter__.return_value=
  fake_resp）；im_async b1/b5 patch 目标迁移（token_service.get_repo_bot_token）；
  template_usage 退役表改锁 featured_templates。分支已知失败清零
  （全量 ~43% 段错误除外，待 gdb 专项）。

### D47：gitee/gitlab 实弹批次——建单链/回执链/托管形态 10 缺陷收口（S70/S71/S72，先测后改，服务级实弹+官方文档驱动）

现场（gitee PAT + 真实仓全流程，三轮连环）：
- 建单链三连环：token 解析落 get_user_token_with_refresh（gitcode 专用，
  gitee PAT 绑定后仍取空）→ 400；修后 admin 卡清洗白名单丢
  issue_write_form → 建单 404；修后 gitee 字符串 ident（"IKSZ3I"）
  int() 强转 ×3 处崩。全为"能力位/助手已存在、调用点漏接"形态。
- 回执链：_ensure_platform_storage_columns 被盖章式一次性迁移罩住——
  首次盖章时 gitee 卡未注册，之后每次启动整段跳过 → 派生列永不创建 →
  add_managed_repo 静默 False → webhook_secret 不落库 → 验签必 404。
  幂等 DDL 与盖章迁移语义解耦：init_database 每次启动执行。
- gitee 原生投递形态（官方文档实锤）：密码模式 X-Gitee-Token（=password
  明文）+ 事件头值带 ' Hook' 后缀（'Note Hook'/'Merge Request Hook'）。
  方言补 X-Gitee-Token 常量时间比对 + 事件头规范化（去 Hook 后缀）。
- 建钩事件集：GitHub events 列表被 Gitee v5 全忽略 → push-only 钩。
  query_token（Gitee 系）派生布尔位事件集（与 repo_manager 存量
  GitCode 序列对齐）；GitHub/GitCode 路径零改动。
- GitLab：GitLabBackend 缺 verify（generic 路由 AttributeError 500）；
  基座 _http 对 private_token 发 Bearer（应 PRIVATE-TOKEN）；github
  形态路径对 GitLab 全断 → GitLabProvider 覆盖层（/projects/{o}%2F{r}
  家族、username 字段、members 按 user_id、建钩 token+布尔位）。
- 工厂收口（用户评审拍板）：create_provider if 链 → _PROVIDER_OVERRIDES
  注册表（数据消开闭点）。支持分层固化：config-only 零代码（gitee 样板，
  卡能力位派生，有意不进注册表）/ 方言偏离注册一行覆盖类（gitlab）/
  register_vcs_backend escape hatch。不引入 Creator 类层次——子类差异
  仅在方法形态不在创建过程，Creator 层零行为收益纯间接。

对账：服务级实弹（issue IKSZ4V 回读对账）+ 本地模拟 gitee 原生投递
（200/验签/pipeline 派发 issue-agent）+ gitlab 入站模拟 200 + 59 用例绿
（S70/S71/S72 + S62/S58/方言/卡管理回归）；test_repos_host_provider
失败集与 head 一致（7 预存量 stale seam 非本轮引入）。

挂账（外部前提，非代码）：①gitee webhook 端到端回流需云入口放行
gitee 投递 IP（用户浏览器/gitcode 投递可达、gitee 集群超时）；②gitlab
托管链未实弹（需账号+PAT）；③gitee OAuth 行权需用户浏览器授权
（渠道卡+client 凭据已就位，授权 URL 已验通到登录页）。

### D48：同型家族收口——S70/S64 缺陷形状的全仓扫描（S73，先测后改）

方法论（本轮提炼）：实弹缺陷不是孤例，是"形状"。按四个形状做全仓扫描：
- 形状 1（round-trip 丢失）：管理面保存白名单 vs 值对象字段——已修 S70②；
- 形状 2（盖章迁移罩住幂等派生 DDL）：_ensure_* 族——S64 修了
  platform_storage，本轮补 whitelist_columns（同款两断点：盖章跳过 +
  whitelist_column 无派生默认）；
- 形状 3（int() 强转 issue ident）：S70 修建单链 3 处，本轮收事件管道
  14 处（event_pipeline 3 + issues_center 11）——/done 确认与等待态恢复
  的 isdigit 门槛同时去掉（字符串 ident 平台的功能性拒掉，不只是崩溃）；
- 形状 4（provider 折叠取 token）：get_user_token_with_refresh 残留
  repo 上下文调用点 3 处（release 子单 / runtime chat 注入 / executor
  兜底）→ get_user_token_for_repo（agent/executor 取 task.repo_key）。

剩余挂账（扫描发现、未收口，各另行批次）：
- 附件同步评论（issues_center ~2650）gitcode 硬编码（token+provider 双点）；
- add_managed_repo 全仓 10 调用点零返回值检查（静默 False 家族，
  S64 的"托管成功但未落库"即其实例）——raise 化需过 10 个调用方容错语义；
- config-only 登录渠道无管理面：oauth_providers_config 无 admin 路由
  （协议卡/凭据/白名单列当前只能文件迁移或直写 DB），D49 候选。

对账：结构守卫 3 用例（沿用 S5 grep 守卫先例）+ 行为用例 1（白名单列
补齐）绿；涉改 15 文件回归对账零新增失败（基线 diff 法；实弹服务停机
后按晨间原序复跑，5 预存量均为基线内环境依赖型）。

### D49：剩余挂账收口——附件同步卡化 / 落库响亮化 / 登录渠道管理面（S74，先测后改）

三件（D48 挂账清单逐项闭环）：
- 附件同步评论（sync_issue_attachment_comment）：去 gitcode 门禁（原
  `if _prov != "gitcode": return False` 整族静默跳过）；token 从
  get_gitcode_token（gitcode 专用列）改 get_repo_token_with_refresh
  （TokenResolver 四级策略链，provider 感知）；客户端按 provider 构造。
- add_managed_repo 响亮失败：非法列 raise ValueError、SQL 硬失败 raise
  RuntimeError——调用方（repos.py 托管/迁移/修复 7 点）全在 try/except
  内，异常即路由 Issue #671 兜底 partial 行（设计的自愈路径）。两处
  "列不存在降级"分支补响亮日志（兼容未迁移库的有意设计保留——与
  S64 教训同源：静默降级掩盖派生列缺失）。
- 登录渠道管理面（E 族闭环）：GET/PUT /api/admin/auth-channels——
  渠道策略校验（S45 构建即校验，缺字段报字段名）、**合并式**保存
  （覆盖式会杀掉存量 gitcode/github 渠道——_load_providers 是 replace
  语义，实弹踩过的坑）、client 凭据剥离写 oauth_{p}_client_id/secret
  meta、缓存热失效。config-only 渠道（gitee OAuth）从"只能直写 DB"
  变为管理面可配。

对账：S74 用例 5 绿（含合并式与凭据不入协议卡断言）；涉改族回归
（S62/S64/S58/S70/S73/admin）绿；test_repos_host_provider 单文件隔离
与基线一致（7 预存量 stale seam，批跑第 8 个为混跑序效应）。

### D53：持久化骨架——身份/凭据/仓库域 ORM 化（S78，先测后改，业界参考评审驱动）

范围（评审定稿）：仅领域建模过的低频结构化域（身份/凭据/仓库/渠道）
ORM 化；高频事件流保持 raw SQL。桥接模式：SQLAlchemy 2.0 Dialect 承接
sqlite/mysql 方言（JDBC 等价），Repository 单实现双方言（修正此前
"两实现"表述）；mysql_compat 保留给存量裸 SQL 路径。

评审八缺口落定（本节即 ADR）：
- UoW：用例级 session + 唯一提交点（Gaia session-per-use-case 本地化
  ——写入口不止 HTTP，边界=用例方法；Repository 只 flush）；
- DTO：Repository 返回 dataclass，ORM 实体不出口，返回形态静态守卫；
- 分表=单表重构非分片：grape_t_repo_credentials 是 managed_repos 泛型列家族的
  列式等价收口（一张表），动态表名/ShardedSession/中间件全部不需要，
  PoC 降级为单模型映射+存量列数据迁移对账（S80 载体）；
- DDL：模型=权威声明，建表唯一出口=盖章迁移（禁 create_all、不引入
  Alembic——双迁移系统会让门禁裂开，S64 教训：配置派生 DDL 不能脱离
  门禁）；compare_metadata 漂移守卫（值对账抓不到类型/可空漂移）；
- repr 脱敏（token/secret/password/config_json）；token 状态机在
  Service 不在模型；显式 selectinload（禁 lazy）；运行时开关
  GRAPE_PERSISTENCE_ORM（默认关，灰度切回）。

事故登记（本轮现场，验证了纪律项）：
- git stash pop 弹出并行会话的 WIP stash（scene-501-502）造成 4 文件
  冲突——恢复=checkout HEAD + 归还 stash（pop 冲突时自动保留）。
  纪律：共享工作树上 stash pop 前必须先 git stash list 核对归属；
- S75 曾误删 repos.py import 行的 get_user_token_for 符号（守卫正则
  盲区，test_s62 单文件隔离暴露）——本轮修复并回归。

对账：S78 用例 7 绿（engine PoC/schema drift 零漂移/字段值对账/
UoW commit+rollback/DTO 守卫/repr 脱敏/开关）；S62/S70/S73-S77 回归
全绿（修复后）；repos_host_provider 7 预存量不变。

### D54：身份域 ORM 切换——双路径对账与三缺口落定（S79，先测后改）

切分（评审纪律"签名不变、调用方零改动"）：grape_account_db 7 函数
加 GRAPE_PERSISTENCE_ORM 分支——业务语义全部留在 Service 层（token
加密 encrypt/decrypt、状态机 0/1/4、create 三分支、require_keep_one），
ORM 只承载数据访问；DTO 携带 DB 原值（密文），解密与裸 SQL 路径同位。
IdentityAdminRepository 扩展（statuses 参数化/update_by_id/create）。

三缺口落定（对账用例逐一暴露）：
- reconcile 静默空集：models 模块未 import → Base.metadata 空 →
  漂移检查空转绿。反模式与 S64 同源（静默空集≠通过），加 assert
  metadata 非空 + 显式 import 注册；
- 幽灵 inode 世界分裂：conftest 删测试库文件后，session_manager.db
  旧句柄上的裸 SQL 全部自洽（表/数据齐全），ORM NullPool 新连接打开
  同路径新空文件 → no-such-table。解法：engine creator+StaticPool
  挂接 session_manager 同一 sqlite3 连接（业界共享连接官方形态）——
  ORM 与裸 SQL 同一事务世界，天然对账且消除双连接写锁；
- DTO 缺字段：IdentityDTO 少 external_avatar，AttributeError 被
  函数级 except 吞成 None（静默失败形状再现）——对账用例的
  逐字段断言暴露。DTO 与 model 字段映射完整性纳入守卫范围。

对账：S79 双路径 7 用例绿（flag 0/1 逐字段等值 + 落库密文形态断言）；
S78 回归绿；身份域涉改 14 文件族基线 diff 零新增失败（26 预存量=
git_repo_api stale seam + 372 环境依赖型）。

### D55：凭据域单表+双写——grape_t_repo_credentials 收口（S80，先测后改）

范围裁定（对 D53 ADR"分表映射 PoC"的收敛）：D44 登记的"grape_t_repo_credentials
物理分表"实为**列式等价收口**——managed_repos 的平台凭据列家族
（bot_*/​*_webhook_token ×4 平台共 8 列）→ 单表
grape_t_repo_credentials(repo_key, cred_type, value)。非水平分片：
动态表名/ShardedSession/代理中间件全部不需要（业界参考的两条分片路线
均不适用），PoC 结论=单模型映射+数据迁移对账，半天量级。

策略（读取方零改动优先）：
- **双写**：add_managed_repo 在同事务内对凭据列 extras 双写
  grape_t_repo_credentials（仅凭据列；webhook_secret/dev_scene 等不进）；
  managed_repos 列保留——TokenResolver/managed repo 查询族等读取方
  零改动，切换归 S80b（切换完成前双写不停）；
- **backfill**：backfill_repo_credentials_from_columns 幂等回填存量列
  数据（INSERT OR IGNORE，已有值不覆盖），挂 safe 迁移（patrol 先例：
  存量库启动自动补建）；
- 表纳入 reconcile._MAPPED_TABLES（schema 漂移守卫覆盖）。

对账：S80 用例 5 绿（表存在/双写/非凭据列不进/Repository CRUD/
backfill 幂等）；涉改 8 文件族基线 diff 零新增（6 预存量 372
环境依赖型）。

#### S80b：读取方切换（D55 续）

两个读取点切双源（grape_t_repo_credentials 优先 + managed_repos 列回落，
GRAPE_PERSISTENCE_ORM 灰度）：
- RepoBotToken._fetch（bot token 策略链第 2 级）：按卡
  token_storage_field 读 credential；
- get_webhook_token_by_repo：卡平台 {p}_webhook_token（原实现只认
  gitcode/github 两列，卡平台列不在查询面——gitee 仓 webhook token
  读取漏）。

事故登记：首轮补丁插错函数——get_webhook_verify_token 与
get_webhook_token_by_repo 体形相近，字符串锚点二次命中同形块。
教训（D50 纪律的函数级版）：改函数前先 awk 全函数体确认唯一性，
不依赖局部锚点。

### D56：仓库域查询族接 Repository（S81，先测后改）

get_webhook_secret_by_repo_key_any（W3 通用面 + S77 repair-host
provider 反查的底层）双路径：flag on 走
ManagedRepoRepository.find_by_full_name_any_provider（LIKE
'%:full_name' 等价），flag off 存量 raw SQL 语义不变（回归锁）。
Repository 扩展三方法：find_by_full_name_any_provider / exists /
get_webhook_secret。

对账：S81 用例 4 绿（flag on/off 行为等价锁 + 反查多 provider 形态）；
涉改 11 文件族基线 diff 零新增（9 预存量）。

### D57：仓库域单列助手直切（S81b，用户拍板：不加双路径开关）

managed_repo_exists（Issue #671 自愈链路判据）与 get_host_user_id
（Issue #372 路径 B 降级链判据，17 调用点）直接切
ManagedRepoRepository（exists / DTO.host_user_id）——语义不变，
不加 GRAPE_PERSISTENCE_ORM 分支（单列只读无灰度价值，双路径徒增
复杂度）。

对账：S81b 用例 2 绿（exists 三态 / host_user_id 三态）；19 文件族
基线 diff 零新增（18 预存量）。

### D58：双写移除——grape_t_repo_credentials 唯一权威（S82，先测后改，用户拍板方案 B）

评审（6cca89322）三挂账的处置与一次拍板：
- 空值覆写依赖：回落语义（rc 空串→列）**写死为本设计依赖**，任何人
  "优化掉回落"即破坏 S80b 前存量行的取值——已在本节显式声明；
- 双写非原子：随双写移除消失（本节）；
- parse_repo_key 内联回潮：登记下批收（S80b 两处 split(":") 派生）。

方案 B 全量冻结（用户拍板）执行清单：
- 写冻结：add_managed_repo 的 8 平台凭据列冻结——extras 凭据列分流到
  rc 单写（同事务）、基线 4 列参数（bot_gitcode_token 等）INSERT 写
  ''、UPDATE 从 SET 移除、两个降级分支同步；repair-host write-back
  的 webhook_token_field/token_storage_field extras 同批迁移（写 rc）；
- 读者去门：get_repo_credential_value / RepoBotToken._fetch /
  get_webhook_token_by_repo rc 优先无条件（列=历史回落，backfill 已
  迁入存量值）；
- 4 类未切换读者同批收口：bot_token_health 扫描 rc（cred_type→
  provider 映射）、config_store.global_bot_token_swap（步骤 3 查
  rc JOIN managed_repos 过滤 host_user_id=0；步骤 6 写 rc+保留
  webhook_id/bot_username）、deps has_bot 闸门与 repos has_bot
  展示/repo_bot_token 读取 rc 感知（any_repo_credential_for_repos
  新 helper）。

现网注意（部署时）：202611050001 迁移建表+backfill 必须在本次代码
部署前/同批应用（列冻结后 rc 是唯一权威，未 backfill 的存量行凭据
不可读——backfill 从列回填兜底，需先于流量）。

对账：S82 用例 7 绿；S80/S80b 旧语义用例同步更新（flag 门移除/
backfill 存量形态造数）；17 文件族基线 diff 零新增（7 预存量）。

### D59：平台卡文件外置——能力聚合点补完（S83，先测后改，评审论证采纳）

形态裁决（评审论证，实测背书）：卡片方案 > 目录插件包——
①差异度不支撑（全平台=2 webhook 方言/3 鉴权形态/2 API 风格；gitee
全部差异被 ~20 卡字段表达）；②改配置≠改代码（S12 纯配置路径是核心
卖点，目录包=每平台一次发布）；③叠加纪律已买单（语义差异均收敛为
一行注册或卡能力位）。

但卡片方案输在能力聚合点：S70 根因=字段权威（PlatformCard
dataclass）与值载体（代码 _DEFAULT_CARDS_RAW + DB KV）分离。补完：

- 新增 configs/platforms/{provider}.json（每平台一个文件≈半个目录）：
  gitcode/github/gitlab/gitee 四卡；值载体从代码 dict 外置；
- 优先级链：meta（admin 运行时覆盖）> 文件（内置默认载体）>
  _DEFAULT_CARDS_RAW（代码兜底，目录缺失才用）。meta 覆盖=整卡替换
  （admin 面经 S70 全字段持久化天然完整；字段级部分覆盖语义不提供）；
- 守卫：文件键集合 ⊆ PlatformCard dataclass 字段名——未知键整卡
  拒绝且 error 级响亮（静默丢键=能力静默丢失，S69 反模式）；
  S70 形状事故结构性免疫；
- gitee 升级文件内置（config-only 语义退役；S18 清 meta 后回落文件
  默认而非消失）；
- 热失效语义保持：文件读取挂现有 _load_cards TTL 缓存口子，改文件
  →失效→重读，零重启；
- 文件入 git 可 review/diff，平台能力变更有单一权威载体。

盖章迁移不动（卡不在迁移域）；半天量级兑现。

对账：S83 用例 5 绿；S18 语义更新；12 文件族大回归零失败。

### D60：评审收口三件——直切准入制度化 / ORM 开关退役计划 / provider 派生统一（S84，评审采纳）

评审多轮挂账的收口决策（本节即拍板记录）：

- **直切准入标准（制度化 D57 实践）**：不经 GRAPE_PERSISTENCE_ORM
  开关直接切 Repository 的准入三条件，全部满足才可直切——
  ①只读（失败半径=返回空/False 级别，无写路径）；②flag on/off
  行为等价用例锁死（逐字段/逐形态）；③同库同事务世界（StaticPool
  共享连接，D54 落定）。写路径与跨表事务一律保留开关灰度。
  追溯合规：S81b（repo_exists/get_host_user_id）满足三条 ✓。
- **ORM 开关退役计划**：S82 后凭据域已实质全量 rc 化（读无条件、
  列冻结），S81 是开关的最后一个用户族。退役节奏＝
  批次 2 收口时删除 GRAPE_PERSISTENCE_ORM 分支与
  get_repo_credential_value 空实现门（届时 identity_admin 域
  Repository 化完成、对账用例转常态回归）；删除前开关语义冻结
  （不再新增 flag 分支调用点——直切按上述三条件走，不再加开关）。
- **provider 派生统一（S80b 内联回潮收口）**：S80b 在 user/db
  （get_webhook_token_by_repo / get_webhook_verify_token 同形块，
  2354/2360 两处）内联的 `repo_key.split(":",1)[0]` 改走
  parse_repo_key 单一入口（S77 立的语义锚点；token_resolver
  提交层实测干净）；结构守卫
  （正则清零）防回潮。依赖声明同步固化：S80b 空值回落语义
  （rc 空串=无值→回落 managed_repos 列）是列冻结期正确性的
  依赖链一环，删除回落前必须先删列（批次 2 事项，顺序不可倒置）。

### D61：S82 读者收口补漏 + 全仓派生清零（S85，先测后改，批次 2 首批）

**真回归发现（worktree 三点实证：merge-base 过/S82 挂）**：S82 列冻结
漏切第 5/6 类读者——`get_bot_token_by_repo`（12 调用点：scenes/
feishu_async/chat/agents/session_store/settings）与
`get_webhook_token_by_repo` gitcode/github 侧（rc 分支只盖卡平台）。
新托管仓凭据只进 rc，两读者读冻结列返回空。修法=S82 同款双源：
列回落在前（历史行）+ rc 兜底（新仓），路径 B 守卫保持。

**全仓派生清零**：S84 只罩 user/db；本批扩全 src——platform_card
（display_name/host 助手 ×2）/ event_pipeline ×1 / events ×5 /
scenes ×2 共 10 处三元式内联改 parse_repo_key，守卫升级为全仓
rglob 扫描（repo_key.py 自身豁免）。

**方法论教训（对账基建）**：本轮基线对账两次被假象污染——①共享
测试库 data/test_sessions.db schema 陈旧（conftest 固定仓库根路径，
worktree 侧各自新建库造成 base 假绿）；②family 混入 merge-base
不存在的分支期用例文件（s59/s83）致 pytest abort 出 base=0/1 垃圾
结果。修订纪律：基线对账前先校验 family 文件在两侧 commit 均存在；
共享测试库每次对账前删除重建。test_event_action_filter fixture 引用
产品从不存在的 repo_key 列（事件族 ~30 失败真预存量，测试侧陈旧）。

对账：S85 用例 5 绿 + S84 5 绿 + S80/S80b/S82 回归绿；
test_user_db::test_add_and_scene（S82 回归标记用例）修复转绿；
涉改族 stash 对照零新增失败（12 预存量 372/stale-seam 家族逐条一致）。

对账：S84 守卫用例（内联派生清零 + 回落语义锁）；文档先行，
代码收口随批次 2 落地。

### D62：健康扫描去映射表 + 事件族 fixture 修复 + 阶段路由死锁修复（S86，先测后改，批次 2 续）

**P0 产品 bug（fixture 修复首跑暴露）**：S75/S76 机械收口在
event_pipeline 留下 7 处裸名 `_native_issue_number` 调用，而该模块
无此名字（模块级只有别名字导入 `_nin_*`，与本函数无关）——全部
NameError 被各自 try/except 吞成静默 no-op。最重的一处：路由中枢
`_resolve_issue_stage_agent` 的 cancelled 检查 safe-fail 返回 ""，
**分支上所有带 issue_state 的事件阶段路由整体静默不分发**。
根修=模块级统一导入一行；别名字导入口保留。
教训（D50 纪律的执行层版）：机械替换后除 ast.parse 外，必须至少
一条端到端行为用例真跑过被改路径——守卫查形状，查不出绑定错误。

**健康扫描去映射表**：bot_token_health 的 `_CRED_PROVIDER` 字面
映射（cred_type→provider）移除，provider 由 repo_key 前缀派生
（parse_repo_key），bot 凭据族过滤下沉 SQL（cred_type LIKE 'bot\_%'）——
第 5 平台（config-only）自动纳入巡检，cred_type 冗余裂缝消除一处。

**事件族 fixture 修复（测试侧，~30 预存量失败转绿）**：
test_event_action_filter 清理语句自创建起引用产品从不存在的
repo_key 列（建表 DDL 两代均无此列，产品代码零读写）——改按
event_type 清理。首次真跑暴露两类陈旧断言：issue_comment
coding→issue（实为阶段自归 coding）、pr_merged qa→qa（实为 #544
有意设计 qa→sre）——按现状矩阵特征更新并注明缘由。

对账：event_action_filter 全绿（含 4 条首跑路由用例）+ S86 3 绿 +
S75/S76/S82/S83/S59 回归绿（54 passed）。

对账（S84 存档）：S84 守卫用例（内联派生清零 + 回落语义锁）。

### D64：bot 身份链数据驱动 + github 怪癖能力位（S88，先测后改，批次 2 续）

**bot 身份来源链数据驱动**：resolve_bot_username 的 github/gitcode
if 分支 → user/db 新增 GLOBAL_BOT_USERNAME_SOURCES（provider→callable，
github 单值源 / gitcode pool）；gitlab 无源=仅卡（S87 语义陷阱规避：
不得误挂 gitcode pool）；未知平台=gitcode pool 兜底（存量语义）。
五条边界语义用例锁（github/gitcode/gitlab/gitee/unknown）。

**github 怪癖能力位（D63 登记项落地，含一次抽象纠偏）**：labels 拆分
请求 / PR 评论端点路径首版误用 dialect 判定——gitee 同 github 方言但
API 跟随 gitcode v5（pulls 端点工作正常），方言≠怪癖。纠正为卡能力位：
pr_comment_endpoint（github="issues"：其 /pulls/{n}/comments 是
inline-review 端点；gitcode/gitee/gitlab="pulls" 存量语义）+
pr_labels_via_issues（github=True：PATCH /pulls 不接受 labels 字段，
422 实测）。字段进 dataclass/Builder 校验/builtin github 卡/
configs/platforms/github.json/admin 清洗白名单（S70 全字段纪律）。

对账：S88 8 用例绿；S84-S87 回归 28 绿；git_repo_api/581/admin 族
stash 对照失败集逐条相同（17 预存量 stale-seam，零新增）。

