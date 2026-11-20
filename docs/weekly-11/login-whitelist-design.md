# 登录白名单全渠道化（行式模型）设计

日期：2026-11-12 · 分支：feat/arch-weekly-11-yaw-dev · 需求：login-whitelist-requirements.md

## 对象模型定位

登录渠道域（providers.py 渠道卡目录，meta oauth_providers_config）与仓库平台域
（platform_card.py VCS 卡目录）分立，唯一刻意耦合点=oauth_fill。白名单属于登录
渠道域：判定与写入只看渠道目录，provider key 全链贯穿（登录按钮 value →
/auth/login/{provider} 路由参数 → oauth_states → 回调 → 白名单），零次转换、
零次查卡翻译。

存储与过滤键=provider key（渠道卡 JSON 顶层 key）；卡 "name" 字段仅做展示
（管理面下拉/行标签）。已裁定不采纳 name 作过滤键：name 是可被管理员随手改的
展示文案（改名瞬间白名单静默失配）、无唯一性约束；key 是 dict 主键天然唯一，
且改 key=换渠道（client_id meta 键/回调路由全断）天然稳定。

### D1 同表加列行式模型（用户拍板：表名不变、向前兼容、双读）

login_whitelist 纯增量加两列，不换表不搬数据：

    provider   VARCHAR(32)  DEFAULT NULL   -- OAuth 渠道 key（新行必填）
    username   VARCHAR(128) DEFAULT ''     -- 白名单用户名（新行必填）

- 新行：仅 provider/username/remark/created_* 有值；老列全空
- 老行：列式存量原样保留（gitcode_username/github_username/gitee_username...）
- 新行唯一性：SQLite 部分表达式索引
  `CREATE UNIQUE INDEX uq_login_whitelist_row ON login_whitelist(provider, LOWER(username)) WHERE provider IS NOT NULL AND username != ''`
  MySQL 生成列 `username_lower AS (LOWER(NULLIF(username,''))) STORED` +
  `UNIQUE(provider, username_lower)`（复用 _ensure_case_insensitive_indexes 的
  _mysql_targets 模式，不依赖 8.0.13 函数索引）
- 建列函数 `_ensure_whitelist_row_columns()` 幂等（PRAGMA/information_schema
  探测后 ALTER），启动序替换 _ensure_whitelist_columns()；后者整个退役
  （含 __all__ 导出），动态按卡建列机制随 whitelist_column 字段一起消亡
- DDL 内联保留 gitcode/github 老列（向前兼容存量行）；_normalize_whitelist_null_literals
  保留（老列仍参与兜底读，'NULL' 字面量清洗继续生效）

### D2 读路径：新行优先→老列兜底（逐人判定）

is_user_allowed 签名 (whitelist_column, login) → (provider, login)：

1. 开关未启用 → 放行（不变）
2. 表空 → **全拒（fail-closed，2026-11-12 拍板，取代 P2-6 全放行）**：开关已启用而
   表空=未配置任何人，即视为"谁都不许进"。误锁风险评估：白名单只在 OAuth 回调
   （grape_auth.py:1719/2179）拦人，管理台走独立密码链（admin/db.py verify_admin →
   admin_accounts），管理员可登入补条目即时恢复，无不可逆锁死；管理面配空表红条
   #loginWlEmptyWarn 显式警示
3. 新行查询：`SELECT 1 FROM login_whitelist WHERE provider=? AND LOWER(username)=LOWER(?)` 命中 → 放行
4. 老列兜底：列名由 provider key 派生 f"{provider}_username"（存量三列
   gitcode/github/gitee_username 全符合该规律，实查 db_setup.py:671/2373、
   providers.py:524），直接执行 SELECT 捕获"列不存在"异常=无老数据 → 拒绝。
   不读任何卡字段。provider 先过 `^[a-z0-9_]+$` 正则（列名派生防注入）
5. DB 异常 → 文件 login-whitelist.json 降级（仍为 fail-closed：开关启用时文件缺失/
   空/损坏一律拒绝，管理台密码链不受影响）

逐人兜底语义（用户拍板"新存在用新，不存在查老"）：按表/按渠道整体切换成
"新独占"会在管理员加首条新条目时锁死存量列式用户，违反生产安全底线，不采纳。

fail-open 洞随机制消失：原"卡无 whitelist_column → return True"路径不存在了
——任何渠道都走新行查询+老列兜底，无绕过状态。

### D3 写路径：新模式唯一

- add_login_whitelist(provider, username, remark)：
  `INSERT OR IGNORE INTO login_whitelist (provider, username, remark) VALUES (?,?,?)`
  （与 signup_whitelist 同款幂等形态）。_whitelist_column_for 退役
- _migrate_whitelist_from_file（login-whitelist.json 首启导入）：改写新行
  (provider='gitcode', username)——文件本就是 gitcode 时代产物
- delete_login_whitelist(item_id, provider=None)：provider 缺省删整行；带 provider
  时老列行只清派生列（行全空再删行），新行按 (id AND provider) 删——保住
  "一人多平台老行"的删除粒度

### D4 管理面 API（登录域自治）

- POST /whitelist：校验=provider ∈ list_providers()（渠道目录在册即可）。
  删 VCS 卡双轴判定（admin/routes.py get_platform_card 检查——登录域加白名单
  不欠仓库域点头；华为有渠道卡无 VCS 卡，双轴下被挡即耦合实锤）。D88 注释
  担心的"VCS 卡在册无渠道卡 INSERT 409 谎报"随 _whitelist_column_for 退役消失
- GET /whitelist：{whitelist, enabled, providers}。whitelist=统一行形态条目
  [{id, provider, username, remark, created_at, legacy}]，老列按
  f"{provider}_username" 规律展开成条目（legacy=true 供前端标记与删除路由用）
- DELETE /whitelist/{id}?provider=xxx：可选 provider 定位老列条目
- providers=[{provider, name}]（list_providers()+卡 name）供下拉动态渲染

### D5 前端窄表化

- admin.html：lw_provider 下拉去字面量 option（JS 动态填充）；表头
  col_gitcode/col_github → col_provider/col_username；标题 tag 去双平台字样
- admin.js：loadLoginWhitelist/_rerenderLoginWhitelist 改行式渲染（provider+
  username+legacy 标记），缓存 _lastLoginWlProviders 供语言切换重渲染；
  deleteLoginWhitelist 对 legacy 条目带 provider 参数
- locales zh/en admin.json：desc 改通用文案；删 provider_gitcode/provider_github；
  加 col_provider/col_username/legacy_tag；username_ph 去 "GitHub/GitCode" 字样
- admin.html 引用 admin.js?v= bump（只升不降）

### D5b 管理台 Vue 化后的重新落地（2026-11-14）

D5 的窄表化最初落在 admin.html + admin.js；管理台整体迁到 Vue 后该页退回旧列式形态（写死两平台下拉、
双平台列头），行式后端没有 UI。重新落地口径：

- 渠道下拉 = `GET /admin/whitelist` 返回的 `providers[]`（`list_providers()` → `grape_t_oauth_channels`），
  渠道下线/改 key 时自动纠偏选中值，无在册渠道给「去登录渠道启用」指引
- 行形态 = `{id, provider, username, remark, created_at, legacy}`；legacy=true 的存量列式条目按 provider 定向删除
- 空表红条：开关开 + 列表空 → fail-closed 语义显性提示（S6）
- 守卫：`tests/mgmt/test_login_whitelist_row_model.py` 的 S6/S7 断言打到 SFC 源码与 admin-view 版本位

### D6 改名联动双写

grape_account_db 改名同步第 4 步：新行
`UPDATE login_whitelist SET username=? WHERE provider=? AND username=? AND username!=''`
+ 老列 `UPDATE login_whitelist SET {派生列}=? WHERE {派生列}=?`（列不存在捕获跳过，
  UNIQUE 冲突保留旧行语义不变）。卡列名读取（get_provider whitelist_column）删除。

### D7 渠道卡 whitelist_column 字段删除

- providers.py：_DEFAULT_PROVIDERS 三处键删除（gitcode:48/github:63/gitee:524），
  模块 docstring 与 D90-N5 注释同步改写；生产 meta 卡 JSON 残留键成无消费方
  死配置，带外清理挂账下批
- config.py is_gitcode_user_allowed/is_github_user_allowed 退役（仅测试引用，
  平台名特化包装函数；行式模型 provider 直接传参）

### D8 测试与守卫

- 新文件 tests/mgmt/test_login_whitelist_row_model.py：S1-S11
- 存量适配：test_login_whitelist.py（符号迁移）、test_whitelist_logic.py（参数
  形态）、test_admin_whitelist_user_crud.py（响应行形态）、test_n5_platform_config.py
  / test_uc38_n5_closure.py / test_s73_family_closure.py /
  test_create_issue_and_lifecycle.py（S30 改名联动）/ test_s63（列驱动改行式）/
  test_s115_platform_gate_closure.py（双轴判定改单轴）
- 守卫（归入 S10）：src/**/*.py 中 whitelist_column 字样归零；
  is_gitcode_user_allowed 符号不存在（退役锁防回潮）

## 实施顺序

D7（卡字段删）→ D1（建列）→ D2（读链）→ D3（写链）→ D4（API）→ D6（联动）→
D5（前端）→ D8（测试适配+新用例）→ 定向回归（受影响文件隔离跑）→ 文档滚动节更新。

### D9 管理面卡管理（渠道轴/平台轴双菜单，2026-11-12 范围扩展）

复用既有后端积木（不新建域）：
- 登录渠道轴：GET/PUT /admin/auth-channels（providers 目录 + channel_strategy
  校验 + 合并式保存 + invalidate_provider_cache 热失效）——已有
- 仓库平台轴：GET/PUT /admin/vcs-platforms（PlatformCard Builder 校验 + host/
  webhook_path 唯一性 + invalidate_platform_card_cache 热失效）——已有

### D10 GET 扩全字段（S14 反例锁：编辑回写零丢失）

现状 GET 只回子集字段——前端 GET→编辑→PUT 回写会让未返回字段落回 Builder
默认值（S70 实弹事故同型：gitee issue_write_form 丢→建单 404 静默降级）。
修法：GET 回**全字段**：
- auth-channels list：每条渠道附完整原始卡 dict（扣 client_secret）+ client_id
  明文（非敏感，settings GET 同口径）+ client_secret_set 布尔
- vcs-platforms list：每张卡 dataclasses.asdict 全字段 + bot_token_configured
  （global_bot_meta_key 非空）

### D11 bot/botname 编辑（仓库平台轴独有）

bot_username 是 PlatformCard 卡字段——随卡 PUT 走 Builder 校验。
bot token 是**凭据**不入卡（build_platform_card 拒未知键）：PUT /admin/vcs-platforms
body 增可选 bot_tokens: {provider: token}，服务端按卡的 global_bot_meta_key
（空=派生 global_bot_<provider>_token）写 meta；不在册 provider 400。前端
bot token 输入框留空=不改（与 client_secret 同口径）。

### D12 前端（卡驱动动态渲染，零平台字面量）

- admin.html 侧边栏配置中心组加两菜单；新增 authChannelsView / vcsPlatformsView
  两个视图容器（列表表格 + 详情/编辑面板），复用 showCfg/showUsers 导航切换模式
- admin.js 详情表单**按卡 dict 键动态渲染**（布尔→checkbox，URL/文本→input，
  数字→number；client_secret/bot_token 特殊处理：留空=不改）——新平台/
  新卡字段自动出现，零 per-field 前端代码
- locales zh/en 新增段；admin.js?v= 再 bump（只升不降）
- 编辑保存走既有 PUT 端点（合并式），错误逐卡回显

### D13 A 层：平台凭据页 UI 收口（S18-S20/S24）

- GitCode 配置/GitHub 配置两卡**整卡删除**：6 个凭据字段（gitcode_api_token/
  github_bot_token/两对 oauth client_id+secret）与新双菜单同 meta key
  （config_store.py:86-98 实锤别名层），双入口=配置漂移源；admin.js
  _renderCfg/saveConfig 同步清理对应字段读写
- webhook_url 迁"运行参数"卡（部署级：本服务对外回调地址，所有平台共用，
  非平台差异；meta key 不变零后端）
- gitcode_webhook_token 输入迁仓库平台详情 gitcode 卡（真存储=managed_repos
  列，全局 meta 只是 seed 兜底通道，db_setup:5784-5787 消费链零改动）
- Bot 账户卡保留（self-comment 检测入口），文案改"兜底清单——卡 bot_username
  优先并集"（见 D15）
- verifyToken（校验按钮）随卡删除；新仓库平台详情无校验按钮（bot token 校验
  走既有 /admin/config/verify-token，挂账下批接线）

### D14 B 层原则：单点收口，消费方零改动

github bot 身份与 bot 名单的全部消费方（event_pipeline self-comment 检测
:874-878、repos 解绑协作者 :1080/:2356、db.py 派生表 :5136-5138）都经过
get_global_github_bot_username / get_global_bot_usernames 两个统一入口——
只改这两个函数的读链（卡优先→meta 兜底），下游零感知。

### D15 github_bot_username 卡化（S21/S23）

- get_global_github_bot_username 读链：github 卡 bot_username（get_platform_card）
  优先 → meta global_bot_github_username 兜底（卡空回退，兼容窗口）
- 存量迁移（注册式，幂等）：meta 值 → github 卡 bot_username 写入 meta
  vcs_providers_config；卡已有值不覆盖；meta 键保留兜底不删
- UI：github 卡 bot_username 在仓库平台详情已可编辑（本批已有能力）

### D16 bot_usernames 卡派生（S22）

- get_global_bot_usernames 读链：list_vcs_platforms() 各卡 bot_username
  非空并集 ∪ meta global_bot_usernames（去重保序）——新平台卡 bot 自动进
  self-comment 检测名单；meta 清单保留为兜底/补充（非卡渠道 bot 仍可配）

### D17 webhook_url / webhook token 语义边界（S19/S20）

- webhook_url=部署级（归运行参数）；webhook token=仓库平台凭据（列存储，
  管理面入口迁平台详情）——两者都不属于登录渠道域
- 旧 meta 键全部保留（只写不改语义），消费链零改动；后续批次再决定退役

### D18 bot 配置域卡迁移（S25，2026-11-12 补充拍板）

- Bot 身份兜底清单卡 + Bot Token 健康状态卡从原"平台凭据"页迁仓库平台页
  （bot 配置域跟平台卡走：清单是卡 bot_username 并集的补充，token 探活的是
  卡凭据通道落库的 bot token）
- 兜底清单自带保存按钮（PUT /admin/config 仅 bot_usernames 字段）；探活卡
  复用 loadTokenHealth（tkh_* 元素 id 不变零 JS 改动）
- 原"平台凭据"页收敛为"运行参数"（webhook_url + DevSpace 规格 + 保存），
  侧边栏菜单同步改名 admin.nav.credentials

### D19 登录页多渠道渲染修复（S26/S27，实弹系列）

- renderOAuthButtons 孤悬 else 块删除：needsRender=true（渠道数≠预置按钮数，
  恰为多平台场景）时按 providers 下标访问 existingBtns[j] 越界抛 TypeError，
  被 .catch 静默吞掉——登录页只剩 2 个静态预置按钮（用户实弹"没有 huawei/gitee"）。
  该块职责（预置按钮 aria-disabled 对齐）由 keyed 分支（按 onclick 匹配）承担
- #oauthBtns 静态预置双按钮删除（merge 回潮产物）：先渲染再被全量替换造成闪动；
  容器留空统一由 /auth/providers 动态渲染
- github 图标恒白（用户三次裁定：不要蓝 #0969DA、不要灰 #24292f 图标）：auth.css
  !important 压过 light 模式 var(--brand) 染色；卡 brand_color=#24292f 保留供
  引导页等其他消费点

### D20 管理面表单可用性（S28，实弹系列）

- 卡详情/凭据输入框显式内联白底黑字：无主题浏览器下 .ipt 白底+继承浅色文字
  =白底白字值不可见（用户实弹"一个值都看不到"）
- admin.js esc() 本地兜底：admin.html 不加载定义 window.escHtml 的 app.js/auth.js，
  全局代理必炸（管理面所有列表渲染中断）——兜底实现与 Auth.escHtml 同口径

### D21 探活状态平台化 + 详情页分段 Tab（S27a/S27b，2026-11-12 拍板）

- 探活跟平台走：GET /admin/vcs-platforms 每卡并入自身 bot_token_health
  （meta bot_token_health 天然 per-provider）+ 顶层 checked_at；前端列表
  逐行 token 健康列（绿●健康/橙●降级/红●不健康/灰—无数据，title 带错误
  摘要）；全局探活汇总卡与独立 loadTokenHealth 前端链退役（/bot-token-health
  API 端点保留）
- 详情页字段过多 → iOS 分段控件五 Tab（凭据/基础/能力与协议/存储与身份/
  其他）：字段按名字清单分组（_CARD_TAB_FIELDS），未匹配字段进"其他"零
  丢失（新卡字段自动落位）；_collectCardFields 扫全部 Tab 容器，保存语义
  与单页时代完全一致；Apple 风格（灰底圆角分段容器/白色活动段/label 上置
  行布局/组内行分隔线）
- 只读标记位（*_configured/*_supported/bot_token_health）不进表单

### D22 平台化到位收口（D105-19，2026-11-12 用户拍板解除 D105-17 冻结）

- 托管 webhook 三件套按平台卡 `webhook_dialect`（github_web/gitee_v5）经
  `_WEBHOOK_FUNC_FAMILIES` 注册表分派，api_base 从卡派生（partial 绑定），
  provider=="github" 特例删除；新平台=卡声明复用家族（零代码）或注册表加一行
- workflows 三态回退读卡 `workflows_write`；PR 列表 head 过滤形态读卡
  `pr_head_filter`（owner_ref=GitHub / bare_ref=gitcode·gitee——实弹实锤
  gitcode owner:ref 形态返回空列表会静默失守 branch_guard）
- 平台卡 meta 覆盖改逐键合并（stale 全量 dump 不再抹掉新增卡字段）；
  api_client 传输垫片删除（test-induced design damage，测试缝迁 _request）

### D23 登录页零延迟渲染（D108/D108-b，2026-11-12）

- 服务端注入完整渠道目录 `window.__OAUTH_PROVIDERS__`（providers_catalog 单一
  来源，与 /auth/providers 路由共用）；`oauth-buttons.js` 同步加载（无 defer）
  + 解析期内联调用——按钮随 HTML 解析进首帧；app.js 对账复用同一构建器
- 存量 token 首帧进 CSS 纯净 pending 态（bootSpinner），init 以 /auth/profile
  实验票后才 showApp，失效 token 登录页一次到位（无工作台闪现）；8s 兜底

### D24 验收修复批设计口径（S32-S38，2026-11-12 深夜）

- 代理建单平台契约：create 复用 update 的 `_issue_write_url` +
  ISSUE_UPDATE_OWNER_PATH/ISSUE_BODY_REPO_BARE 能力位（不另造开关），
  gitee/gitcode owner-path + body 补 repo；IK* 字符串 ident 端点参数 int|str
- bot 凭据读序三段式（卡列→rc 单表→存量列回落）：存在性（repo_has_bot_
  credential）与取值（get_bot_token_by_repo）共用 `_repo_bot_token_value`
  单一实现——终态口径不吞存量列数据（白名单行式双读同原则）
- 挂载谓词单一来源：engine_mounts_to_credential 同时供设备视图与
  credentials/{cid}/tasks 聚合（口径不再分叉）
- autopilot 统计回链：合成键 autopilot-<纯字母>（无数字，_issue_num→0 不误挂
  真实 issue），创建即落含失败路径；统计口径不变（vod 空回链=设计）
- 场景 apply 幂等（按 market_scene_id+repo_key 复用活跃场景）；experience
  身份缺失=409 MISSING_IDENTITY（客户端状态非网关故障），顶层补
  with_oauth_token import（NameError 被 generic except 吞成 502 的根因）

### D25 卡详情页 Apple 式可用性 + 代理能力位上翻（S40-S44，2026-11-13 拍板）

**用户裁定：53 个卡字段全暴露给管理员不可用（"参数太多用户不太会"）——两级曝光。**

- 常用键白名单（_COMMON_CARD_KEYS）：渠道卡 8 键（name/auth_url/token_url/user_url/
  scope/redirect_uri/pkce_enabled/brand_color）+ 平台卡 6 键（host/api_base/
  bot_username/webhook_path/pat_create_url/download_base）直接渲染；
  其余键进同 tab 内"高级字段"折叠区（原生 details/summary，默认收起零 JS
  状态管理）——改错能力位的暴露面从 53 键收窄到 14 键。
- 字段级配置指导（S43 核心交付）：每键配 i18n 词条 admin.field_docs.<key>
  （zh/en 双语 54 词条），渲染为输入框下方 .f-doc 小字（Apple 脚注风格：
  11.5px 灰 t3 色）——语义内置表单，不靠用户猜；存储字段词条显式写明
  空串=F4 终态（凭据只落 rc 单表）/填列名=存量列兼容期。
- 双域说明（S44）：渠道/平台详情页顶部各一条 domain-note（圆角灰底提示条），
  写明两卡是解耦域（登录绑定 vs 托管代理）——防管理员误以为改渠道卡影响托管。
- 代理能力位上翻（S40-S42）：providers/<p>.py 模块开关（MILESTONE_* /
  ISSUE_* / LABELS_* / CLOSING_* 等 16 项）上翻为卡能力键（小写下划线），
  _pflag 改为卡优先→模块回退；卡未声明时与模块开关完全一致（零回归）。
  效果：新平台接入任一代理契约=卡声明一个键，纯配置零 Python 模块；
  管理面"能力"tab 经 _renderCardFields 卡驱动自动出现新键（UI 免改），
  且这些键在高级折叠区内有 field_docs 指导。
- CSS：adv-fold（虚线分隔+⌄ 旋转指示器+hover 反馈）、f-doc、domain-note
  均 Apple 设计语言（灰底圆角/小字 t2/t3 层级/0.15s 过渡）；admin.js
  v=20261113-1 bump。
