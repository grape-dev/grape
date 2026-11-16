# 登录白名单全渠道化（行式模型）需求

日期：2026-11-12 · 分支：feat/arch-weekly-11-yaw-dev · 基线：origin/arch-weekly-11 @ 62568298e

范围扩展（2026-11-12 同批）：admin 配置中心新增"OAuth 登录渠道"与"仓库平台"
两个管理菜单（列表/详情/编辑；仓库平台含 bot/botname 及平台私有字段），见 S12-S16。

范围扩展二（2026-11-12 拍板：A+B 都做）：管理台"平台凭据"页遗留收敛——
A 层 UI 收口（删同 meta key 双入口卡）+ B 层对象模型收敛（webhook token /
github_bot_username / bot_usernames 跟仓库平台卡走，webhook_url 归部署级
运行参数），见 M10-M13 / S18-S24 / 设计 D13-D17。

## 背景

管理面 OAuth 登录白名单（Issue #332）目前只支持 gitcode/github 两平台，gitee/华为等渠道
用户无法被加入白名单。根因不是"列没加够"，而是对象模型缺陷：login_whitelist 把平台集合
编码进了表结构（每平台一列），扩平台需要 DDL 建列 + 渠道卡声明 whitelist_column + 前端
加下拉选项三处同步；且管理面添加校验越界依赖仓库平台域（VCS 卡双轴判定）。

域边界（拍板口径）：登录渠道域（providers.py 渠道卡目录）与仓库平台域
（platform_card.py VCS 卡目录）分立，唯一刻意耦合点=oauth_fill（渠道名==平台名时
token 引用不复制）。白名单属于登录渠道域，判定与写入只看渠道目录，不欠仓库域任何东西。

## 现状问题表

| # | 位置 | 问题 |
|---|------|------|
| M1 | src/api/db_setup.py:671-680 | login_whitelist 每平台一列，平台集合编码进表结构（耦合根因：平台=开放集合（卡可配），表结构=封闭集合（DDL 定死）） |
| M2 | src/api/static/admin.html:368-372 | 白名单平台下拉写死 gitcode/github 两个 option |
| M3 | src/api/static/admin.html:353 | 卡片标题 tag 写死 "GitCode/GitHub 登录白名单" |
| M4 | src/api/static/admin.html:385-386 | 表头写死 GitCode/GitHub 用户名两列 |
| M5 | src/api/static/js/admin.js:769-783, 491-502 | loadLoginWhitelist / _rerenderLoginWhitelist 两处渲染写死双平台列 |
| M6 | src/domains/mgmt/admin/routes.py:427-431 | 添加白名单要求 VCS 卡在册——跨域越权（华为有渠道卡、无 VCS 卡，被挡在白名单外） |
| M7 | src/domains/mgmt/routes/grape_auth.py:1146-1149 | 登录校验依赖渠道卡 whitelist_column 声明；未声明渠道 fail-open 全放行（白名单开着也拦不住） |
| M8 | src/domains/mgmt/admin/db.py:308 vs db_setup.py:2364 | whitelist_column 派生兜底与动态建列口径不一致：兜底列不存在时 INSERT 失败被吞成 409 "已存在" 谎报（D88 注释点名的 gitlab 变体） |
| M9 | src/kernel/config.py:724, 743-750 | f-string 拼列名查 SQL（列名来自配置，注入面）+ is_gitcode/github_user_allowed 平台名包装函数 |
| M10 | admin.html:229-246 | 平台凭据页 GitCode/GitHub 双卡与新双菜单同 meta key 双入口（gitcode_api_token≡global_bot_gitcode_token、oauth_*≡渠道卡凭据），配置漂移源 |
| M11 | admin.html:248-252 | Bot 账户卡（self-comment 检测）挂"平台凭据"页且数据源是全局 meta 清单，与卡 bot_username 双轨 |
| M12 | user/db.py:2027 get_global_github_bot_username | github bot 身份读全局 meta global_bot_github_username，卡 bot_username 字段存在却闲置——存量双轨 |
| M13 | user/db.py get_global_bot_usernames | bot 名单全局 meta 与各卡 bot_username 无派生关系（新平台卡 bot 不进名单） |

## 目标形态

login_whitelist 同表加列行式化（与 signup_whitelist `(type, value)` 同构——仓库已有范本），
纯增量 DDL，不换表不搬数据，老列式数据保留：

- 同表新增 provider、username 两列；新行 = 一条 (provider, username) 白名单条目，
  provider 为 OAuth 渠道 key（回调路由参数，单一真源）
- 平台从"结构"降为"值"：加渠道零 DDL、零卡字段、零前端改列
- 读（双读兼容，逐人兜底）：先查新行 `WHERE provider=? AND LOWER(username)=LOWER(?)`；
  未命中再查老列兜底，列名由 provider key 派生 f"{provider}_username"（存量三列
  gitcode/github/gitee_username 全符合该规律，无需读卡）。命中=任一命中
  （不按表/按渠道整体切换"新独占"——整体切换会在加首条新条目时锁死存量用户）
- 写：管理面添加只写新模式行，永不写老列；删除按 id 通吃新老行
- 唯一性：部分唯一索引（provider, LOWER(username)）只约束新模式行
- 管理面前端窄表：一行=平台+用户名+备注，平台下拉从渠道目录动态渲染
  （存储/过滤用 provider key，卡 "name" 仅展示用）

## 范围

- login_whitelist 同表加列 provider/username（纯增量 DDL，无迁移无换表）+ 双读兼容读路径
- 登录校验链 is_user_allowed(provider, login) 新行优先、老列兜底
- 管理面 API（增删查）+ 前端窄表化与动态下拉
- 改名联动（grape_account_db）行式化
- 测试适配 + 新 S 用例 + grep 守卫

## 非目标

- signup_whitelist（注册邮箱/域名白名单）不动——本就独立运作
- oauth_fill 耦合点不动（设计文档写死的刻意耦合）
- unified_user_mapping 不动（另一张平台列表，另行立项）
- 渠道卡 whitelist_column 字段本批从代码删除（providers.py 内置卡三处键 + 全部读取点），
  行式模型下该字段无存在意义；生产 meta 卡 JSON 内的残留键成无消费方死配置，
  带外清理挂账下批（改生产卡另行操作）
- login-whitelist.json 文件降级路径语义保留（DB 不可用时兜底；2026-11-12 拍板 fail-closed：
  开关启用时文件缺失/空/损坏一律拒绝）
- 白名单作用域**只覆盖准入**（首登/新注册/绑定），已绑定用户的每次登录不查白名单
  （`grape_auth.py:1590` binding 命中即直接登录，需求4/P0-2 原口径；2026-11-12 复核拍板
  保持现状）：启用白名单后，白名单外的已绑定老账号**仍可正常登录**，属预期语义而非漏洞。
  存量库实证：live 库验收账号（gitee identity external_login=gitee-login，账号 id 以 live 库为准），白名单清空后 19:17:46 仍 login_success。
  注意条目匹配的是 **external_login（第三方原登录名）**，不是 grape 侧 username。
- 白名单开关 login_whitelist_enabled 语义：未启用全放行；**启用+表空=全部拒绝（fail-closed，
  2026-11-12 拍板，取代原 P2-6 全放行）**。误锁风险评估依据：白名单只拦 OAuth 回调
  （grape_auth.py:1719/2179），管理台走独立密码链（admin/db.py verify_admin →
  admin_accounts 表），可即时登入补条目，不构成不可逆锁死；管理面必须显示空表红条警示
  （#loginWlEmptyWarn）。

## 场景用例清单

| # | 场景 | 前置 | 动作 | 预期 | 用例名 |
|---|------|------|------|------|--------|
| S1 | 双读兼容 | 老列条目 (gitcode_username='alice') 与新行 (provider='gitee', username='bob') 并存 | is_user_allowed | alice/bob 均放行（新行优先→老列兜底，逐人判定，任一命中即放行——不整体切换，避免加首条新条目锁死存量用户） | test_login_whitelist_row_dual_read |
| S2 | 全渠道添加 | gitee/华为渠道卡在册（无 VCS 卡） | POST /whitelist {provider, username} | 200 入库（登录域自治，只验渠道目录，不查 VCS 卡）；未在册渠道 400 | test_login_whitelist_add_all_channels |
| S3 | 行式登录校验 | 白名单开启，新行 (gitee, bob) 在表 | is_user_allowed('gitee','bob') | bob 放行；is_user_allowed('gitcode','bob') 拒（provider 隔离） | test_login_whitelist_row_check |
| S4 | 无声明渠道不再绕过 | 渠道卡无 whitelist_column 字段（字段已删除） | 白名单开启，渠道用户登录 | 同样按行查+老列兜底（fail-open 状态消失，无"未纳管"绕过路径） | test_login_whitelist_no_bypass |
| S5 | 大小写不敏感唯一 | 表已有新行 (gitcode, Bob) | 再添加 (gitcode, bob) | 幂等拒绝（409）；登录 bob/Bob 均命中 | test_login_whitelist_ci_unique |
| S6 | 空表 fail-closed | 开关开启 + 表空（新老皆无） | 任意渠道登录 | 全部拒绝（fail-closed，2026-11-12 拍板取代 P2-6 全放行）；管理台密码链不受影响 + 管理面空表红条 | test_login_whitelist_empty_denies_all / test_login_whitelist_empty_warn_banner |
| S7 | 管理面行式渲染 | GET /whitelist | 前端渲染 | 行式表（平台/用户名/备注/时间）；平台下拉=渠道目录动态；admin.html/js 无 gitcode/github 字面量 option/列 | test_login_whitelist_admin_ui |
| S8 | 改名联动双写 | 新行 (gitee, old) 与老列 (gitcode_username='old') 并存 | 平台侧各自改名 old→new | 新行按 provider+username UPDATE；老列按派生列名 UPDATE；其他渠道同名不受影响 | test_login_whitelist_rename_sync |
| S9 | 退役锁 | — | hasattr(config, "is_gitcode_user_allowed"/"is_github_user_allowed") | False（平台名包装函数退役，rationale=行式模型 provider 传参取代列名特化） | test_login_whitelist_retired_symbols |
| S10 | 守卫 | — | 全 src 扫描 | whitelist_column 字样归零（卡字段+读取点全删）；login_whitelist 新增列写点仅限 db_setup 建列/兜底读取白名单文件内 | test_login_whitelist_guard |
| S11 | 存量条目删除粒度 | 老列行同时含 gitcode_username='alice'、github_username='alice' | DELETE /whitelist/{id}?provider=gitcode | 仅清 gitcode 列，github 条目保留；行全空则整行删 | test_login_whitelist_delete_scoped |
| S12 | 登录渠道管理列表 | admin 登录 | GET /admin/auth-channels | 返回全部在册渠道（provider/name/type/configured）+ 完整卡字段（auth_url/token_url/user_url/scope/...）+ client_id 明文 + client_secret_set 掩码标记 | test_admin_card_mgmt_api |
| S13 | 登录渠道详情/编辑 | 在册渠道 | 点击渠道→详情表单→编辑保存 PUT /admin/auth-channels | 合并式保存（不挤掉其他渠道），client_secret 留空=不改；校验失败逐卡报错；保存后 invalidate_provider_cache 热生效 | test_admin_card_mgmt_api |
| S14 | 仓库平台管理列表/详情 | admin 登录 | GET /admin/vcs-platforms | 返回全部平台卡**全字段**（含 bot_username/issue_write_form/branch_create_form/token_storage_field 等平台私有位）+ bot_token_configured——编辑回写零字段丢失（S70 反例锁） | test_admin_card_mgmt_api |
| S15 | 仓库平台编辑含 bot/botname | 在册平台 | 编辑 bot_username + bot_token 保存 PUT /admin/vcs-platforms {cards, bot_tokens} | bot_username 走卡字段 Builder 校验；bot_tokens 写各卡 global_bot_meta_key（不入卡 dict——build_platform_card 拒未知键）；平台私有字段全量可编辑 | test_admin_card_mgmt_api |
| S16 | 管理面新菜单 UI | 打开 admin | 侧边栏配置中心组出现"OAuth 登录渠道"与"仓库平台"菜单 | 列表→点击行→详情/编辑表单（字段由卡驱动动态渲染，零平台字面量）；admin.js?v= bump | test_admin_card_mgmt_ui |
| S18 | 旧双入口卡删除 | 新双菜单在位 | 打开平台凭据页 | GitCode/GitHub 配置两卡整卡移除（6 个重复凭据字段已由登录渠道/仓库平台菜单接管，同 meta key）；saveConfig/_renderCfg 同步清理 | test_platform_credentials_consolidation |
| S19 | webhook_url 归部署级 | admin 登录 | 平台凭据页运行参数卡编辑 webhook_url | 写读走原 meta key `webhook_url`，行为不变，位置从 GitCode 卡迁运行参数卡 | test_platform_credentials_consolidation |
| S20 | webhook token 入平台轴 | gitcode 卡在册 | 仓库平台详情 gitcode 卡编辑 webhook_token | 写原 meta key `gitcode_webhook_token`（managed_repos 列 seed 链零改动）；非 gitcode 平台该字段不展示 | test_platform_credentials_consolidation |
| S21 | github bot 身份卡化 | github 卡 bot_username=gh_bot；meta global_bot_github_username=旧值 | get_global_github_bot_username() | 卡值优先返回 gh_bot；卡空时回退 meta 旧值（兼容窗口）；消费方（event_pipeline/repos 解绑链）零改动 | test_bot_username_card_derivation |
| S22 | bot 名单卡派生 | gitcode 卡 bot_username=my_grape_bot | get_global_bot_usernames() | 名单=各在册卡 bot_username 非空并集 ∪ meta global_bot_usernames（去重）；新平台卡 bot 自动进名单 | test_bot_username_card_derivation |
| S23 | 存量迁移幂等 | meta global_bot_github_username 有值、github 卡 bot_username 空 | 启动 | 注册式迁移把 meta 值写入卡 bot_username（幂等，卡已有值不覆盖）；meta 键保留兜底 | test_bot_username_card_derivation |
| S24 | 守卫 | — | 全 src 扫描 | 平台凭据页旧字段 id（f_gitcode_api_token 等 9 项）归零；get_global_*_bot_username 读链=卡优先→meta 兜底（唯一消费口） | test_platform_credentials_consolidation |
| S25 | bot 卡迁平台页 | 新双菜单在位 | 打开仓库平台页 | Bot 身份兜底清单卡（自带保存，写 meta global_bot_usernames）+ Bot Token 健康状态卡（tkh_* 探活展示）从原"平台凭据"页迁入；原页收敛为"运行参数"（菜单同步改名） | test_platform_credentials_consolidation |
| S26 | 多渠道按钮渲染不崩 | 渠道目录 >2（4 渠道在册） | 打开登录页 | renderOAuthButtons 动态渲染 4 按钮无异常（孤悬 else 越界访问 existingBtns 抛 TypeError 被 .catch 吞掉的预存 bug 已删）；#oauthBtns 无静态预置按钮（闪动源） | test_login_ui_fixes |
| S27 | github 登录图标恒白 | 登录页 4 按钮 | computed style | github 图标 color=#fff（!important 压过 light 模式 var(--brand) 染色）；github.json brand_color=#24292f（正确品牌色，供其他消费点）；gitee/huawei/gitcode 图标取色不受影响 | test_login_ui_fixes |
| S28 | 管理面表单对比度 | 无主题/苹果两主题 | 详情表单输入框 computed style | 白底黑字（bg=#fff fg=#1d1d1f）——凭据输入（client_id/secret/bot_token/webhook_token）+ 卡字段输入显式内联配色；esc() 在 admin 页（不加载 app.js/auth.js）有本地兜底不抛 window.escHtml undefined | test_login_ui_fixes |
| S27a | 探活状态跟平台走 | bot_token_health meta 有 per-provider 数据 | GET /admin/vcs-platforms | 每卡并入 bot_token_health（healthy/degraded/error 摘要）+ 顶层 checked_at；全局汇总卡退役，仓库平台列表逐行展示 token 健康列（绿/橙/红/灰 + title 错误摘要） | test_admin_card_mgmt |
| S27b | 详情页 iOS 分段 Tab | 打开仓库平台/登录渠道详情 | 渲染 | 五 Tab（凭据/基础/能力与协议/存储与身份/其他）分段控件；卡字段按名字清单分组，未匹配进"其他"零丢失；_collectCardFields 扫全部 Tab 容器保存语义不变；只读标记位不进表单 | test_admin_card_mgmt |
| S29 | 已绑定用户免检（口径锁） | 开关开 + 表空（fail-closed 态） | 已绑定 identity 用户 OAuth 回调 / 未绑定用户回调 | 已绑定 → 直接登录（exchange_code，无 WHITELIST 拒绝，grape_auth.py:1590 早返回）；未绑定 → WHITELIST 拒绝（对照组，走真实 is_user_allowed 不 mock） | test_bound_user_bypasses_whitelist_when_enabled / test_unbound_user_denied_when_whitelist_empty_and_enabled |
| S31 | 仓库列表平台集合卡驱动 | 用户绑定 gitee（平台卡在册） | GET /api/repos/member?provider=gitee ／ POST /api/repos/sync | 可列仓平台集合=平台卡目录（不再二分字面量）；`?provider=` 逐平台生效；拉取走 create_provider(p).fetch_repos_with_role 端口；user_repos 落 gitee: 前缀行（实弹验收账号 7 条 gitee，total 81=58 gitcode+7 gitee+16 github） | test_repos_gitee_listing / test_s62_fetch_and_cache_gitee / test_s62_fetch_and_cache_gitcode_parity |
| S32 | 代理建单平台写契约 | gitee v5（owner-path 契约平台） | tk_ token POST /api/git-repo/{o}/{r}/issues | 200 建单（owner-path URL + body 补 repo=仓名），IK* 字符串 ident 在 detail/评论/关闭端点直通；github 维持 repo 路径零回归 | test_proxy_contract_adaptations（4 新例） |
| S33 | 事件流错误条目健壮性 | agent_tasks.errors 含 dict 条目（错误对象） | GET /api/obs/events?limit=1/5/50（含按仓过滤） | 全部 200，detail 为文本（_err_text 按 localized_message/message/error_code 降级取值），无 KeyError 500 | test_obs_events_view（dict-entries 用例） |
| S34 | 设备任务聚合含挂载引擎 | 本机引擎挂到凭据设备（credential_id=0 + host_name=设备名） | GET /api/runtime/credentials/{cid}/tasks | 聚合含挂载本机引擎任务（与逐引擎 runtimes/{rid}/tasks 计数/id 集合一致）；挂载谓词与设备视图同一来源 engine_mounts_to_credential | test_runtime_protocol（credential_tasks 用例） |
| S35 | bot 凭据三段读序 | gitee（空卡列平台）：rc 单表有值 / 仅存量列 bot_gitee_token 有值（老库） / 无凭据 | GET /api/repos/managed 与 /repos/bot-token/{o}/{r} | 读序=卡列→rc 单表→存量列回落，两端点同一实现同一答案（gitee:demo 存量列 True；无凭据 False；gitcode/github 列路径回归不变） | test_p9_managed_has_bot_token（7 例含存量列回落） |
| S36 | autopilot 任务统计回链 | autopilot 巡检单（issue 型）执行失败 | 创建入队/统计查询 | 创建即落合成回链键 autopilot-<纯字母>（失败路径不丢），issue 级统计（dashboard.py:565 口径）计入；vod 型空回链不计（设计口径）；存量 9/9 可回填 | test_p8_autopilot_issue_ref（T1-T4，T4=三数一致性场景锁） |
| S37 | 场景市场 apply 幂等 | 同 market_scene_id + repo_key 已有活跃场景 | POST /api/scenes/market/{id}/apply 二次调用 | 200 复用既有场景（reused=true，不重复创建）；不同 market_scene 同名冲突仍 409（消息指明非重复应用） | test_market_apply_independent_scene_id |
| S38 | experience 身份缺失语义 | 当前用户无可用的平台登录身份（API 200 空 login） | POST /api/scenes/market/{id}/experience | 409 MISSING_IDENTITY（消息含「设置→凭据 绑定/重新授权」指引）；上游真故障仍 502 不误伤；顶层 with_oauth_token import 在场（NameError 根因已修） | test_p3b_experience_identity_4xx（5 例） |
| S39 | 登录页零闪动 | 无 token / 带失效 token / 带有效 token 三种进入 | 首帧渲染 + 验票 | 无 token：登录卡+渠道按钮随首帧出现（oauth-buttons.js 同步构建+服务端注入目录）；失效 token：pending 启动态→登录页一次到位（无工作台闪现）；有效 token：pending→工作台 | 浏览器时间线实弹（D108/D108-b 节记录） |
| S40 | 代理能力位上翻进卡（issue 写契约族） | gitee 卡在册（模块开关 ISSUE_UPDATE_OWNER_PATH/ISSUE_BODY_REPO_BARE/MILESTONE_DUE_ON_REQUIRED/PR_COMMENT_ID_HEX 声明在 providers/gitee.py） | 卡 meta 覆盖 issue_write_form / 代理层 _pflag 读卡 | 卡声明（小写下划线键）优先、模块开关回退；卡未声明时行为与模块开关完全一致（零回归）；管理面详情页"能力"tab 自动出现新键（_renderCardFields 卡驱动免改） | test_pflag_card_precedence（卡优先/模块回退/卡清空回退三态）+ 既有契约套件回归 |
| S41 | 代理能力位上翻进卡（milestone 契约族） | gitcode/github（MILESTONE_VERIFY_READBACK/MILESTONE_ID_RESOLVE_TO_NUMBER/CLOSING_REFS_AUTO_CLOSE_DEFAULT/DUE_DATE_ONLY/DUE_TIME_ONLY） | 同 S40 | 同 S40 口径；卡覆盖后新平台接入 milestone 回读守卫=卡声明一个布尔键，零 Python 模块 | test_pflag_card_precedence + test_issue_640/test_1005 回归 |
| S42 | 代理能力位上翻进卡（labels/token 方言族） | gitcode LABEL_COLOR_PREFIX_HASH、github LABELS_ARRAY_FORM、gitcode LEGACY_REPO_TOKEN_GETTER、github SETTINGS_TOKEN_FIELD_SUFFIX | 同 S40 | 同 S40 口径 | test_pflag_card_precedence + test_proxy_contract_adaptations 回归 |
| S43 | 存储字段空串语义 UI 提示 | F4 终态卡（token_storage_field/webhook_token_field=""） | 仓库平台详情"存储与身份"tab | 两个键渲染为带 hint 的输入（空=凭据只落 grape_t_rc 单表；填列名=启用 managed_repos 存量列，仅兼容期平台）；hint 文案 i18n 双语 | test_admin_card_mgmt_ui（hint 在位断言） |
| S44 | 渠道/平台双域说明 | 打开登录渠道详情 / 仓库平台详情 | 渲染 | 渠道详情页顶部域说明（登录渠道卡=OAuth 登录/绑定凭据）；平台详情页同款（托管平台卡=仓库托管与代理行为）；i18n 双语 | test_admin_card_mgmt_ui |

## 实现状态（滚动维护）

已实现（2026-11-12，feat/arch-weekly-11-yaw-dev，全部实弹验证）：
- D1-D8 白名单行式化全链（S1-S11）：注册式迁移 202611150001；is_user_allowed(provider,login)
  双读；管理面 API 行式化；改名联动双写；前端窄表+动态下拉；渠道卡 whitelist_column 退役
- D9-D12 卡管理菜单（S12-S16/S17）：auth-channels 全卡字段+凭据掩码、vcs-platforms
  全字段+bot_token 通道、卡驱动动态表单、beta-apply 多渠道下拉（渠道目录驱动）
- D13-D17 平台凭据页 A+B 收口（S18-S24）：旧双卡删除；webhook_url 迁运行参数；
  webhook token 入 {provider}_webhook_token 通道；github bot 身份/名单单点读链
  卡优先→meta 兜底；迁移 202611150002；消费方零改动
- D18 bot 配置域卡迁移（S25）：兜底清单+token 探活卡迁仓库平台页；原页=运行参数
- 实弹修复（S26-S28）：renderOAuthButtons 孤悬 else 越界崩溃（多渠道按钮只剩 2 个静态
  的根因）；#oauthBtns 静态预置按钮删除（闪动）；github 图标恒白+卡 brand_color 归位
  #24292f；管理面表单白底黑字显式配色；admin.js esc() 本地兜底
- 空表语义 fail-closed（S6，2026-11-12 拍板）：config.is_user_allowed 开关开+表空→
  全拒（原 P2-6 全放行退役）；文件降级同口径（缺失/空/损坏一律拒）；管理面
  #loginWlEmptyWarn 红条 + zh/en i18n；admin.js?v=20261112-10；
  用例 test_login_whitelist_empty_denies_all / test_login_whitelist_empty_warn_banner /
  test_whitelist_empty_table_enabled_denies_all / test_file_fallback_fail_closed_when_enabled
- 作用域口径锁（S29，2026-11-12 复核拍板）：白名单=准入闸，已绑定用户登录免检
  （grape_auth.py:1590 早返回）；新增对比用例对（真实 is_user_allowed，开关开+表空）：
  已绑定放行 / 未绑定 WHITELIST 拒绝；live 依据=已绑定验收账号白名单清空后仍登录
- 管理台 Vue 迁移后重新落地（2026-11-14）：行式白名单页在 `app/src/views/AdminView.vue` 重建——
  渠道下拉由 `GET /admin/whitelist` 的 providers 动态渲染（grape_t_oauth_channels 真源，零平台字面量）、
  表头改 平台/用户名 + 存量列式标记、删除按 provider 定向（S11 粒度）、空表红条 fail-closed（S6）；
  守卫用例 S6/S7 由旧 admin.html/js 断言改写到 Vue 源码口径（`tests/mgmt/test_login_whitelist_row_model.py`）
- 仓库列表平台集合卡驱动（S31，2026-11-12 实弹修复）：repos.py 不再二分
  ("gitcode","github")——可列仓集合=平台卡目录，拉取走 create_provider(p)
  .fetch_repos_with_role 端口，repo_key 归属走 parse_repo_key 单源；
  gitee 绑定后 /repos/member?provider=gitee 生效、sync 落 7 条 gitee: 行；
  顺带修绿 2 条基线预存红（test_s62_fetch_and_cache_gitee / gitcode_parity）

已绿测试：test_login_whitelist_row_model（12）、test_admin_card_mgmt（6）、
test_platform_credentials_consolidation（4）、test_login_whitelist、
test_whitelist_logic、TestLoginWhitelist（10）、test_n5_platform_config、
test_uc38_n5_closure、test_s73 家族A、test_s63、test_login_whitelist_null_literal_cleanup、
test_migration_track_guard（双迁移合法）、S30 改名联动、test_login_ui_fixes（S26-S28）

基线预存红（非本批）：test_s73 家族C（engine/agent.py get_user_token_with_refresh
残留）、test_platform_whitelist_ratchet×3（repos.py 基线点消失）——基线 worktree 同红

- 验收修复批（S32-S39，2026-11-12 深夜，10 项验收矩阵全实弹）：gitee 代理建单
  owner-path（复用 _issue_write_url 能力位）；obs/events dict 错误条目 500；
  credentials/{cid}/tasks 挂载引擎聚合（谓词单一来源）；has_bot_token 三段
  读序（卡列→rc→存量列回落，get_bot_token_by_repo 同一实现）；autopilot
  issue_id 合成键创建即落+存量回填（scripts/backfill_p8_autopilot_issue_ref.py，
  9/9 回填）；场景 apply 幂等化；experience 身份缺失 409 MISSING_IDENTITY；
  登录页零闪动（D108/D108-b：服务端注入渠道目录+oauth-buttons.js 同步构建+
  pending 验票态）。实弹证据链与失败集对照（35 红全预存、+1 转绿）见
  platform-hardcode-audit.md 验收修复批节。

待验收/挂账：
- 实弹链路：华为 OIDC 渠道白名单登录（用户浏览器实测）；对话中出现过的明文凭据
  （gitee client_id/secret/PAT）收尾轮换
- 生产 meta 卡 JSON 残留 whitelist_column 死键带外清理（挂账下批）
- 仓库平台详情缺 bot token 校验按钮（/admin/config/verify-token 接线，挂账下批）
- 旧全局键（global_bot_github_username 等）兜底窗口退役时点（挂账下批）
