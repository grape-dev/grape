> ⚠️ 本文为拆分分片（内容唯一权威源）。索引导航：./multi-platform-decouple-design.md
> 来源：multi-platform-decouple-design.md L2308-L3036（原文原样搬运，零改写）。
> 域定义：登记簿——挂账总清单与收官批（D89-D102：双卡修正/凭据治理线/N1-N5/D98-D102 rc 目标态、凭据管理能力批、task 行凭据快照退役与 Gitee 实弹缺陷批）。
> 全局约束：继承索引页「表设计铁律/实现纪律/验收纪律」（见索引页）。

## 挂账总清单（跨批次归属，合并前最终盘点 2026-11-06）

登记冻结/后续批次项汇总（均不阻塞合并；散落各 D 节的挂账在此汇总防散佚）：

**D100 新增挂账（2026-11-08，方案稿 §八 收口记录）**：
A. A1-A4 系统凭据收编（OAuth client/VLM/OBS/RESEND）——sc 表已建（迁移
   202611080001）但零消费方（A 类全部暂缓，用户裁定）；各带触发条件
   （OAuth client 运行时管理页 / VLM 运行时变更 / OBS 多环境 / 邮件管理页），
   "必须存在"追问为收编前置问句，终态方向挂挂账 #5 闸门 ③
B. crypto key 独立/轮转（P8）——sc 与 uc/rc 共用 key 泄露影响面=全平台 +
   JWT_SECRET 派生兜底链同源风险（security_config.py L83-93），KMS 评估
   与闸门 ③ 合并
C. uc provider 值域收紧（P5）——注册表守卫 warning 期（set_credential
   接线在批 2），零未登记命中后翻硬拒
D. 多租户 Org 层（P7）——scope_type 锁 {'system'}，放开走独立评审
E. **FR-9 存量评估范围补全（2026-11-08 D100 边界核查新发现）→
   【D101 方案二收口（同日）：repo_gitcode_token 部分已根治】**——
   原 FR-9 范围仅 devops_agent_session/issue_agent_session；实码核查
   发现 agent_tasks 行同族明文（①repo_gitcode_token bot token 明文、
   ②env_extra JSON 列含 GRAPE_SESSION_TOKEN/GRAPE_LINKED_REPO_TOKENS/
   云 AK-SK 明文，见 docs/weekly-07/grape-failure-analysis-20261019.md §265）。
   **D101 方案二落地后 ① 已根治**：task 行值恒空（claim 现取注入不落库），
   明文不存在而非加密——比 FR-9 加密方案更彻底；快照陈旧问题同批消除。
   **剩余 ②（env_extra 明文族）仍挂 FR-9 存量评估**，处置建议：按需注入
   不落库（对齐 claim 现取思路）或 env_extra 凭据键加密，立项时评审。
F. **新登录渠道接入前端 checklist（P2-7，D102 实弹流程缺口）**——每接一个
   新登录渠道前端有 5 处硬编码点需人工同步：①_OAUTH_ICONS 图标表（缺→首字母
   降级，华为实弹踩过）②index.html 预渲染按钮 DOM（缺→FOUC 闪烁）③
   oauth-<p>-not-configured 置灰 CSS 枚举 ④i18n 两语言 <p>_login 文案
   ⑤文档文案。D102 三轮清查修的均为此类残留。**长效方向**（二选一，立项
   时评审）：a) 前端完全由 /auth/providers 响应驱动（图标 brand_color+首
   字母兜底即可，去掉预渲染）；b) 本 checklist 固化进需求文档 + 接入流程。
   关联：集群配置收敛需求（独立立项 docs/weekly-09/multi-platform-
   decouple/cluster-config-convergence-requirements.md）落地后配置侧自动化，前端侧仍需本 checklist。

**功能批次归属**：
1. ~~前端 PAT 绑定入口（F7）~~——**D90-N5 已落地**（2026-11-07，519757650
   后续批）：账号页绑定清单含全渠道（providers 动态含 gitee）+ manual 提交
   体走 platform_tokens 泛型（存量双平台专用字段路径保持 S52 防双路径语义）。
   后端零改动（泛型入口本就就绪）。测试 test_n5 UC37⑤。
2. 关单 legacy 双 key（S97/D72 冻结，issues.py L585）：结果字典
   gitcode/github 双 key，存量前端消费者依赖——归关单链重构批（D71 原文
   "批次 3"）。
3. ~~前端 3 处 display strip~~——**D90-N5 已落地**（2026-11-07）：observability
   8 处统一 stripRepoPrefix（app.js helper 正则清单含 gitee），内联
   replace('gitcode:') 清零。测试 test_n5 UC37④。
4. ~~admin whitelist 新渠道列 DDL~~——**D90-N5 已落地**（2026-11-07）：实码
   核对发现 _ensure_whitelist_columns 已是配置驱动动态建列（provider 卡
   whitelist_column 派生，gitee 渠道卡在册即自动建 gitee_username 列——原
   登记"物理仅两列"已过时）；本批补 admin add/list 列驱动化
   （_whitelist_column_for 配置派生，原 _LOGIN_WHITELIST_COLS 写死双渠道
   映射删除）。测试 test_n5 UC37②③。
5. 双卡体系（VCS 平台卡 × 登录渠道卡）——**分离即目标态，现状已达成**
   （2026-11-07 方向修正 + 同日三次澄清；本条**不是待治理问题**）：
   **与业界主流实践一致**（非断言唯一主流形态）：登录身份域、VCS 资源集成域、
   共享基础设施域**三域解耦**（内部依据=设计文档 D10 双卡分离 + D40/S60 三域
   解耦，三域定义见该两节；业界对标=GitHub OAuth App 身份 vs GitHub App 资源
   双连接 / VeeCode-Backstage SCM 轴 × Identity 轴 / Keycloak 身份 broker 分层）。
   **归因三步法实锤：产品代码现状即目标态**（登录链五函数零仓库域引用/
   providers.py 唯一收敛点/external_identities 零 VCS 列）——零实现债，
   结构守卫 UC21 防回潮。默认禁止合并领域实体、权限模型和凭据语义；
   本条唯一开着的=三处薄共享的**未来评估闸门**（非修复计划），按风险升序：
   ① **配置校验框架**：共享校验框架/错误码格式/密钥引用校验；不共享
      provider 配置 schema 与字段语义。
   ② **展示元数据协议**：共享卡片描述协议/UI schema/展示规范；不共享
      登录字段与 VCS 字段的业务语义。
   ③ **凭据存储适配层（最高风险 gate，排最后）**：仅评估 secret reference /
      vault adapter / 加密信封 / 轮换钩子；**禁止共享 OAuth token 与 VCS
      installation token 的语义、scope、授权决策、审计口径**——先出威胁
      模型与接口边界，不先做实现。
   独立方案文档先行，输出物固定六件：可共享层/禁止合并语义边界矩阵、
   共享接口契约、安全威胁模型、迁移兼容策略、触发升级条件、Owner 与
   评审节点。**未满足触发条件前不启动评估**——这是未来共享基础设施的
   门槛流程，不是对现状（双卡分离）的修复计划；现状无需修复。

**合法冻结不动项**（语义即如此，防续审重复报）：
6. db.py get_webhook_verify_token / get_webhook_secret 的 gc/gh 历史列回落
   + gitcode `_req_query` 文件 fallback（S91 设计预期 + 基线预存）。
7. repos.py token 探测优先级序 ("github","gitcode")（语义常量）；
   scenes.py 同名仓归属双轮（生产事故修复结构）；webhook 分流语义；
   settings/_LEGACY_PAT_FIELD_PROVIDERS legacy DB 键（D71 分类）。
8. s66 list_collaborators / git_repo_api test_issues_crud_mapping 之外的
   mcp 4 failed / git_repo_api 6 净失败（D86/D87 已清）——预存失败以
   stash+基线双对照为准绳，续审新增失败先走归因三步法。
9. oauth_fill 落库消费端（D90，关联 D10，非 D89 方向修正范围）：**契约评审
   已拍板（2026-11-07，评审稿 docs/weekly-09/multi-platform-decouple/oauth-fill-contract-review.md
   第 5 节决议表）**。八项决议：P-1A 存引用（OAuth→external_identity_id 指针，
   悬空=stale 不级联）+P-2c 独立表 user_platform_credentials（作用域键
   user_id+provider，lazy upsert）+P-3A PAT 优先（feature flag 灰度+双凭据
   分轨监控+PAT 401 不回落）+P-4（NULL=legacy 集中 resolver）+P-5a 归属者
   语义（**硬前置 credential_usage_log**，executor/initiator 分列）+P-6A
   拦截器（并发 401 折叠 single-flight/凭据类型分流/invalid_grant 失效流程）
   +P-7A（adapter 层读 provider scope 信号，禁 403 一律判定）+P-8 4 周判据
   （+新列错误率基线+可逆 disable 先行）。交叉风险 5 项与业界校准采纳/不采纳
   清单见评审稿第 6 节。**消费端批已落地（2026-11-07，UC27-UC32/S123-S128，
   6 步全收口）**：P-2c 建表+ORM 三件套（f8e796964）→ provisioner 端口+4 处
   接线+S116 升级（460f55789）→ P-3A 调序+flag（ee9722d72）→ P-5 审计表+
   provision 闭环（c2bb0a188）→ P-6 401 兜底+invalid_grant 失效流程
   （df2000852）→ P-7/P-8 scope 判定+埋点（e9cd3a5fa）。落库消费端未接线
   的历史登记**闭合**；剩余 = 生产灰度期（flag credential_pat_first_enabled
   开启 + 4 周 P-8 观测：旧列读回落零命中/新列错误率不高于基线/无回滚事件；
   达标后 P-8 下线 user_credentials，替代表正式接位）。**部署观察手段已备
   （D96，2026-11-08）**：deploy-checklist-mysql-backfill.md 滚动观察表 +
   scripts/check_deploy_backfill.py 只读校验——4 周口径与 P-8 统一。
   原登记（保留供审计）：
   设计已拍板
   "三合一"、实现只有身份绑定+刷新托管两合一，落库消费端未接线（注册表
   声明面在，零消费点）；仓库域当前走"用时取用"lazy 链功能等效。~~要补，
   但先定跨域 provisioner 契约再补~~（已补：provisioner 端口=登录域唯一
   引用点，S116 守卫升级口径锁死）。触发条件与
   契约要点见 D90 节；"用时取用"定性=临时等效非长期方案。
10. 仓库凭据契约评审前置三项（D90 前置事实 6-8，2026-11-07 用户评审
    沉淀）——**N2 已拍板落地（2026-11-07，评审稿 d90-n2-repo-credential-
    contract-review.md 三项全 A）**：① grape_t_repo_credentials cred_type 值域 =
    bot 级专属（_CRED_COLS 白名单断言拒绝+抛错，禁 *_pat 形态；用户 PAT
    权威=user_credentials 用户级；两存储面粒度/消费链不同、互不同步、
    零迁移零行为变更——守卫测试 test_n2_1）；② 策略链序=架构常量
    （注释锁定，source/cred_type=元数据不驱动排序）；③ actor=凭据归属者
    定性（fetch_user_token docstring 锁定，触发者审计走 usage_log 专列）。
    **N2 派生挂账两件——均已拍板落地闭合（2026-11-07）**：
    (a) ~~bot token 明文存储~~ → **N3 通道加密落地**（f0442c2e8，评审稿
    d90-n3-bot-token-encryption-review.md 确认）：grape_t_repo_credentials.value +
    managed_repos 凭据列 8 cred_type 读写口全接 encrypt/decrypt（向前兼容
    =decrypt 明文兼容存量零迁移；等值查询双形态修正 FR-6.2/swap；幂等回填
    步骤 3）；性能补强批再接 bot_token_health verify 解密（防密文误标
    expired）——b55268434；(b) ~~bot token 消费未接 usage_log~~ → **N4
    审计覆盖落地**（c5c94354a，方案 d90-n4-audit-coverage-plan.md，
    拍板 1A/2B/3A）：rc 加 id TEXT 锚点（复合主键保留，MySQL TEXT 无法
    ALTER 成主键）+ usage_log 加 repo_key 列 + RepoBotToken 命中两分支
    接 log_usage（Core 通道）+ 幂等回填步骤 4——双方言安全（N1 教训规避）。
    **N4 性能补强**（b55268434 + 4202f89c9 用户逐条细节）：usage_log 双索引
    （credential_id/repo_key）+ 保留期清理周期化（保留 180 天/清理每 30 天
    节流，双时间源分离/per-process 预期/低频风险/分批 DELETE/不持久化
    五条细节全落）+ 异步批量写入（有界队列 10000 + 后台攒批；真根因
    = sqlite3 同连接跨线程共享事务态，独立连接修正——12 连跑验证）。
    rc 读缓存：实测瓶颈未现（主键索引 O(1)+解密 4μs），登记不预建。

**N3/N4 收官对账（2026-11-07）**：派生 (a)(b) 闭合后，D90 契约评审全部
决议 + 派生项落地完毕。凭据治理线全景：user_accounts 死表写入冻结（守卫
GC1-4）→ 用户 PAT 通道加密（AES-GCM）→ N2 值域隔离（rc bot 级专属）→
N3 bot token 通道加密（向前兼容）→ N4 审计覆盖（rc UUID 锚点 + repo 维度）
→ 性能补强（索引/保留期周期化/异步批量）。

**D98 rc 双权威分裂修复（2026-11-08）**：settings 手动设置 token 路径
（_update_repo_bot_token）漏迁写 rc（D58 执行清单漏项）——显式设置后
deps has_bot 闸门假空。补写 rc 两步法 + UC40 六用例（行为锁）。

**D102 Gitee 实弹缺陷批（2026-11-08，01269c02b+7cd8a4c1f+18b52bcb2+30f97187f）**：
用户实弹报两处引导页错误，深挖为前端 github/else 二分残留族 + 适配器权限口径缺陷：

**D102 前端二分残留清查（8 处，三类）**：
- 显示类 ×7：①PAT 帮助文案二分（gitee 落 gitcode 文案——引导指错站点，
  i18n 双语言新增 gitee_token_help + account.js 三分查表）②platformIcon
  gitee 落通用 git 分支图标兜底（Gitee 官方 G 形 SVG 从 _OAUTH_ICONS 移植
  专属分支，品牌色 #C71D23）③_accountPlatformName 兜底非 github 恒
  GitCode（查表化）④注销确认按钮 ⑤⑥templates 平台标签/名称三处
  ⑦白名单弹窗——全部查表化或分支入册
- **功能类 ×2（同根因功能性错误）**：templates.js repo_key 前缀构造——
  gitee fork 模板被错标 'gitcode:' 前缀（仓库选错平台）——三前缀查表化
- 合法冻结：app.js _repoWebLink/_repoWebHost 目录未就绪同步兜底
  （平台卡 host 优先路径已覆盖，注释声明存量双平台语义）
- 测试：i18n/frontend 60 passed（4 failed round3 注册页时序 stash 基线
  同败预存）；终验 grep 二分残留 CLEAN

**D102-A generic 适配器 owner 兜底（实弹权限缺陷）**：
用户实弹报告 gitee 自建仓库（owner）显示'成员'。因果链三层实锤：Gitee
/user/repos 列表响应不带 permissions.admin（owner 仓库也缺，GitHub 口径
差异，原注释自认'降级 False'）→ generic 适配器只认该字段 is_admin=0 →
repos.py 二值映射前端'成员'。修复（owner 兜底最小改动）：fetch_repos_
with_role 前置 GET /user 一次取 login（复用 get_user_login 零新依赖）；
is_admin = permissions.admin || (owner.login == my_login) 取或非覆盖；
login 失败降级纯 permissions 判定不炸列表；非 owner 判定不变（不误报
权限原则保留）。测试 test_d102a_generic_role_owner_fallback 4 用例
（RED 先行——owner 兜底用例改前失败）。回归凭据域 73 passed（2 failed
预存 stash 同败归因在册）。

**D102-B 登录页平台按钮优化（2026-11-08，f97c7837b+ddec66364）**：
参考 gitee.com/login。①/providers 固定展示序（用户拍板 gitcode→github→
gitee→gitlab→hwcloud→其余按名，grape_auth.py 排序键）；②>4 平台溢出
折叠（前 4 直接展示+"..."展开收起，MAX_VISIBLE=4，全量映射不过滤，
t6_04 源码锁窗口 2000→4000+断言 _mkBtn 化）；③hwcloud 花瓣 logo 入
_OAUTH_ICONS（华为入口此前首字母降级）。实施教训：Python 字符串注入
JS 转义两度破坏 onclick 拼接（\' 转义陷阱），git checkout 恢复后改用
patch 工具精确文本匹配。

**D102-D providers 缓存 TTL 重载（2026-11-08，a116af467）**：
P1-5 实弹（带外写库 40 分钟登录端点不可见）。get_provider/list_providers
无条件走 _load_providers（TTL 双重检查锁内），对齐 platform_card 统一
入口先例。行为锁 test_d102d 3 用例（含 TTL 过期重载行为级）。
集群收敛需求 R1 前置闭环（独立立项在册）。

**D102-E OIDC 引擎兜底（2026-11-08，a116af467，§5 热修语义重建）**：
华为实弹。①handle_callback _tokens 复用（code 一次性，二次 exchange
1101）；②/user 失败→id_token claims 解码兜底（不验签仅取 claims）；
③oauth_callback userinfo 失败时 oidc 渠道转引擎（D28 渐进迁移 oidc
先行）。守卫 test_d102e 4 用例。验证工作区不可达，按移交描述重建。

**D102-F 前端 host 派生通用化（2026-11-08，0d74afce4+870c1a704）**：
用户实弹两连报（仓库跳错站→Issue 跳错站）。三处字面量表收敛到
_repoWebHost 唯一权威派生器（卡目录优先+同步兜底一处维护）；
_vcsUrl（Issue/PR 链接 8 消费点）挂 window 暴露；repos.js 三函数
统一消费；平台过滤前缀精确匹配。配卡新平台零前端改动。
外部复核四处裁定同批：patrol import 不成立（分支无此调用 AST 实证）/
issues_center log 局部遮蔽 9 处清理/_repoWebLink 竞态/test_d102c
绝对路径相对化。

**D102-K 引导页数据源分离（2026-11-08，用户实弹+设计纠偏）**：
引导页误用 /auth/providers（登录渠道列表）渲染仓库绑定卡片——华为等
纯登录渠道混进"绑定代码平台"（用户实弹抓错+追问"两接口要分开"）。
D10 双卡建模本意两轴分离：登录渠道 ≠ 仓库平台。修：_renderBindingGuide
改拉 /api/settings/vcs/platforms（仓库平台卡目录，卡驱动 name/host/
pat_create_url）；登录页 renderOAuthButtons 继续用 /auth/providers
（正确用途不变）；绑定模态框 OAuth configured 判断经 #535 自fetch
双保险保持正确。守卫 test_d102k 3 用例（数据源各用各的防回潮）。
index.html bump v220。
**D102-K 续（用户设计纠偏：logo+登录布局）**：①华为 logo 换官方
八瓣花瓣形（此前自造两瓣对称近似形错误，app.js/repos.js 双处同源）；
②登录页 OAuth 按钮改 gitee 风格 icon-only——纯 logo 圆形横排（46px
圆、hover 提亮微浮、title/aria-label 平台名提示），溢出"..."按钮同
形态；未配置仍 aria-disabled 置灰+下方 hint（用户要求保留）；③预
渲染 DOM 同步 icon-only（FOUC 首帧一致）；④index.html bump v221；
⑤守卫+2 用例（icon-only 形态+八瓣 logo 防回潮）。

**D102-J 全局同族扫尾（2026-11-08，用户令全流程全场景全页面排查）**：
六类域扫描（双文件/前端链接/后端拼接/适配器缺位/平台名二分/守卫补位）。
修 2 处：observability.js _obsIssueUrl/_issueUrl 硬编码 gitcode.com
（gitee 观测页 issue 跳错站——前端同族第 3 处）；grape_auth.py:2529
p_name 二分（绑定转移确认页 gitee 显示原文）。合法冻结有据 5 处：
scene-market/templates（_repoWebHost 优先+降级在位）/prompt_builder
（卡派生优先）/repo_manager API_BASE（gitcode 专属模块调用方门控
正确）/account.js（目录卡 pat_create_url）/官方模板仓硬编码（宿主
事实）。守卫 test_d102j 3 用例 + index.html bump v219。

**D102-L 设置页第三方账号/仓库凭据职责拆分（2026-11-08，方案稿
d102l-settings-split-design.md v2——外部评审三修正采纳：职责4定性
'解绑的合法跨域副作用'非错位/边界声明'仅UI入口归组存储模型不变'/
PAT文案'显式回退正式路径非降级'；业界参照 GitHub 凭据分离+GitLab
PAT 归 Access Tokens）。实施：①绑定弹窗 OAuth-only（token tab 删除，
未配置→提示跳凭据管理）②凭据 tab 新增第三方仓库凭据卡（仓库平台
目录卡驱动+过期红标+PAT 表单复用 manual form，写入接口
/settings/credentials 原样）③安全 tab 过期告警迁凭据 tab（安全 tab
只展示绑定关系）④后端零改动。守卫 test_d102l 3 用例。bump v222。

**D102-K 续（引导页双路径，2026-11-08，用户指令"引导页也按这个模式+
未配置提示 OAuth+PAT 两条路"）**：引导页并拉两接口——仓库目录（绑定
列表）+ /auth/providers（仅取 OAuth 配置态，不再当绑定列表直渲）。
仓库平台卡片双按钮：[账号绑定]（OAuth；未配置置灰+卡内提示"暂未开通
OAuth 授权。可配置 PAT（仓库凭据）使用仓库功能，或联系管理员启用"）
+ [配置仓库凭据 PAT]（_accountOpenPatForm 表单）；纯登录渠道仅 OAuth
按钮。account.js bump v51/index v223。守卫更新：d102k 混轴断言精确化
（调用点实参检查，函数定义形参豁免）+ d102l 双路径用例。

**D102-M 双范式并存收口（2026-11-08，外部评审四层模型 paste_18/19 对账）**：
业界四层模型（存储分离/解析显式/验证 origin-agnostic/UX 状态可见）对账：
存储层 grape 已是策略二（manual PAT=user_credentials.<p>_pat 与
OAuth=external_identities 物理分离，S52 防双路径）；解析层 Manual > OAuth
已实现（fetch_user_token L373，D10 拍板背书——业界 PAT-as-fallback 与
grape PAT-first 两方向并存，维持现状记录分歧）。补两处精确差距：
①凭据 tab 仓库凭据行加来源徽章（手动 PAT 生效中/OAuth 自动供给中/
未配置，判定 Manual>OAuth 与解析序一致）；②fetch_user_token 命中来源
进 usage_log（_audit_source，"配了 PAT 走了 OAuth"可从审计排查）。
不建议本轮：AuthContext 句柄化（P8 既有挂账）。守卫 test_d102m 3 用例。
account.js bump v52。

**D102-K 续·二（用户纠偏提示语义，2026-11-08，0c39e5c7d 后续）**：
引导页 OAuth 已配置但用户尚未绑定的提示语义修正——原提示误导为
"admin 未开 OAuth"；正确语义=渠道在册、用户还没绑定，提示应为
"到用户设置 → 账号安全 tab 配置绑定关系"（绑定关系=账号域，设置页
是正式管理入口；引导页只是快捷入口）。修：OAuth 已配置卡片加
"未绑定？点击上方绑定，或到用户设置 → 账号安全 配置绑定关系"+
_accountGoSecurityTab 跳转；渠道未配置保持原提示（跳凭据管理配 PAT）。
bump v224。守卫 +1（安全 tab 入口/文案断言）。

**D102-I patrol ident 漏网第 16 处 + 缓存 bump（2026-11-08，69923703d，
外部复核三裁定采纳）**：外部复核推翻此前"分支无此调用"误判——误判根因
是同名词双文件（src/api/patrol.py 实现本体 vs routes/patrol.py 路由壳）
只扫了一处；已入 memory 教训。修：patrol.py:397 补 import（S76 第 16
处漏网，纯数字编号巡检即 NameError，ident 被 isdigit 门槛提前返回故仅
int 平台触发）；issues_center.py:2362 补 D102-C 教训注释；index.html
app.js v217→v218（870c1a704 改 app.js 未 bump——用户复验"跳转没修好"
的直接原因）。守卫 test_d102i 3 用例（import 探针/模块可导入/版本≥218）。

**D102-H 分支设计文档归拢（2026-11-08，6c59cbdc9，用户拍板方案 A）**：
13 件本分支自产文档 git mv 入 multi-platform-decouple/（索引页=既有
打断文件，补批次方案稿导航节 11 件表格）；基线 15 件零触碰（合并 diff
不含基线 rename）。引用同步 6 处跨目录路径 + 11 分片头导航 ../→./ +
索引分片路由去前缀；test_uc39 文档锚点（唯一代码消费点）修后复绿。
目录结构守卫 test_d102h 4 用例（防再散/导航完整/同目录/uc39 锚点）。

**D102-G A2A 字符串 ident 合法化（2026-11-08，用户实弹 IKTREU 移交 400）**：
两条历史决策打架——S70（issue ident 保持平台原生形态）vs 2026-10-14
事故防线（A2A 入口 isdigit）。实码链：需求中心移交 issues.py:1257 直调
a2a_endpoint，payload 从自家 DB 读出（_load_issue_repo_scene:1169）——
校验器拦自己刚写的数据。裁决：防线保留但语义改"合法形态"（int 或
字母数字 ident ≤32；垃圾：空白/#前缀/标题文本/连字符仍拒）。
同族清点全改：webhook:907（redo 查询同款）+ resolve_issue_card_id
（纯数字门槛→GRAPE_ISSUE_ID 对 ident 恒空，S75 修法先例）。
RED 先行 5 failed 精确命中→5 passed。issues.py:481 已兼容不动。

**D102-C/P0-2 纯登录渠道象限（2026-11-08，华为第一实例）**：
D10 双卡分离建模了 PAT-only 象限（有仓库无登录），未建模镜像象限
"仅登录渠道"（有登录无仓库）——华为账号 OAuth 首登自动建号后进工作台，
/repos/member 返回 400"API Token 未配置"（与刚 OAuth 登录完的事实矛盾）。
修复：repos.py 空交集改 200 + 空列表 + _no_vcs_binding 标记；前端 loadRepos
按标记走"绑定代码平台"引导空态；建号用户名 sub 形态（>24 字符）且有昵称
时昵称派生（_generate_username 清洗/冲突后缀复用）。测试
test_d102c_login_only_channel 3 用例。需求 S12b 象限行入册。

**D101 task 行凭据快照退役 + session 死列清理（2026-11-08，1996af748）**：
方案稿 d101-task-token-snapshot-retirement-design.md（外部评审五点 v2 采纳）。
方案二：agent_tasks.repo_gitcode_token 退役（claim 现取制——创建恒空、claim
无条件现取注入 task dict 不落库；校验/传输双语义拆分 resolve_repo_config_level；
重派现取消灭快照陈旧；dependency_check 新语义只读级别不实际解密；挂账 E①
根治=明文不存在而非加密）。方案三：session 死列清理（策略链四改三，
DevopsAgentSessionToken/SessionTokenHandler 删除；T8 b 案门面改名
detect_and_mark_repo_token_expired；agents 面板段/解绑清理段/upsert 死形参清除）。
测试：test_d101_task_token_retirement 7 用例 + test_d101s_session_cleanup 6 用例
（均 RED 先行+stash 反向验证）；372_user_token/r17 mock 三级化迁移。
宽域 5245 passed 零增量（四可疑文件 stash 基线同败实锤）。
收尾修复（2026-11-08 同日）：宽域分型 5 条触达域失败处置——event_loop_block
fixture 补 repo_key（W12 检查拦截测试态构造）×3、t5_01 审计锁按方案二语义
更新（无条件现取+防复活守卫）、s75 源码锁改写（scheduler 校验接缝新形态）；
worktree 物化用例失败定性预存（_remap_cwd 重映射 fixture tmp_path，stash
基线同败，与凭据链无交集）。
**复核裁决两项前提闭合（2026-11-08，69c0cb4df 后续）**：
① 存量 devops_agent_session token 列清点——本机实测各库 0 行（风险低），
   DDL 三列物理保留（声明面退役），存量清列迁移不做（无行可清，一迁移
   一语义纪律）；② db_setup legacy agent_sessions 搬迁 SQL 剔除 token 列
   （5058 原样 SELECT repo_gitcode_token 写死列=零写入终态唯一残留写入方，
   已修——旧表 token 值不迁移，防死列复写明文）。

**D99 rc 表目标态重建（2026-11-08，f12f8968a）**：rc DDL 目标态一步建对
（id VARCHAR(256) PK + UNIQUE(repo_key, cred_type)，零存量迁移）；
cred_type 归一值域 {'bot','webhook'}；读写点归一 12 处（token_resolver/
bot_token_health/config_store/db.py×3/repos.py×3/models/repositories/
settings/backfill 壳）；N4 补列迁移退役 no-op；UC41 七用例；混跑序污染
根因修复（pytest.main 内嵌套重新 exec conftest → GRAPE_PYTEST_NESTED
嵌套守卫，audit-hook 实锤归因）。方案稿 rc-redesign-design.md（v3）。

**D100 凭据类型 Schema 驱动 + grape_t_system_credentials 建能力 + upc 退役 +
OAuth 快照清退（2026-11-08，5c4254276 + 4f625b058，UC42/UC42B）**：
方案稿 d100-credential-type-schema-design.md（v3 冻结，外部评审 9 条 +
业界对标三轮全采纳，八项决策 P1-P8 用户逐项拍板）。四块交付：
① **sc 建能力**（批 1 零消费方）：建表迁移 202611080001 + credential_
registry.py 代码常量注册表（三段 16 键，fields 三元组 sensitive 脱敏
路由 + storage_mode 形态位）+ 门面五入口（create/update/delete admin
fail-closed 闸门 + auth_audit 变更审计值尾4位指纹 / use 受控消费入口
usage_log 审计 / resolve DB 权威+env 回落=批 2 迁移开关）。A1-A4 存量
凭据全部暂缓（用户裁定），各带触发条件挂账 A。
② **upc 退役**（P2=a）：provisioner+4 接线+ORM/DTO/Repository+迁移登记
整段删除（从未上生产，无需 DROP/无 no-op 壳）；s123/s124 删除；D10
"三合一"②正式定为用时取用 lazy 终态。
③ **OAuth 快照清退**：W1 刷新双写删 / R1 预刷新判定改读 ei.expires_at /
R2 fallback 删+防复活守卫 / R3 绑定判定只看 ei；存量行 P1=a 冻结。
④ **边界声明**（§1.0）：ei 定性=登录域凭据存储域内能力自持；分域存储
=爆炸半径隔离；闸门③=治理控制面接口先行版非迁移预备（触发条件量化
三选一）。存储边界声明 + 用例入册（UC42 十一用例/UC42B 五用例，
12-usecases + 13-mapping + 索引页同步；UC27/28 废弃标注）。
**D100 收尾清残留（4f625b058）**：usage_log kind 默认值/值域声明切
grape_t_repo_credentials（upc kind 退役存量保留）；provider_link 死指引改终态
声明；注册表精化（sensitive/storage_mode）入 UC42①c。

**D97 全域续审——死 seam 测试债同根因清零（2026-11-08）**：文档拆分后首轮
全域续审发现并修复 12 个预存死 seam/过时 mock 用例（基线同败实锤，非本轮
引入）：① test_issue_395 五用例重写——mock 缝从已删符号
get_user_token_with_refresh 迁至卡驱动端口 create_provider().get_user_login
（对齐 ed04df860 簇1 手法），预期按完整生产链锚定：404→REPO_NOT_FOUND /
401→with_oauth_token 刷新域 reauth_required（直连分支生产不可达）/ 403 业务
权限→TOKEN_PERMISSION_INSUFFICIENT / 5xx 与网络错→前置放行→下游
_host_502_detail 兜底 502 retryable；② test_issue_372_deep_analysis 七处
同族：2 处死符号 patch 迁 get_user_token_for 双点位（UC18 缝）、2 处
get_hosting_provider_config 缝迁 get_global_bot_token_for+resolve_bot_username
（S87 统一入口）、R10 用例改行为级（真库播种 status=4→恢复断言，对齐 S79
形态）、marker 用例对齐写侧语义净化后锚点（create_issue 加 marker、评论
端点有意不加——agent 回帖链 marker 在 token_resolver L1443）、auth_me
mock 自相矛盾修正（S96 后 expired 遍历实际绑定平台，mock [] 恒空）；
③ requirements 10-分片 S1-S11 用例列 9 个过时符号回写为现行实测符号
（对账三两分片全部符号 pytest --collect-only 实测在册）。**登记下批**：
test_api_routes 20 failed + test_issue_372_round9_audit 6 failed 同为死
seam 预存债（基线同败 FAIL-SETS-IDENTICAL 实锤，同根因但需逐用例语义
核对，26 个用例工作量大，另行批次）。

**D96 生产部署 Checklist 落地（2026-11-08，挂账 #9 剩余件闭环）**：
交付三件——`docs/weekly-09/multi-platform-decouple/deploy-checklist-mysql-backfill.md`（第 0 项
备份确认 + 首启观察 + 二启判定树含失败分支 + 完成判定权重 + 滚动观察表
承载 4 周时序判据 + FAIL 处置指引 + 残余风险声明）+
`scripts/check_deploy_backfill.py`（只读诊断：MySQL 事务级 READ ONLY 每查询
包显式事务 / SQLite query_only 连接级双保险；密文三段格式正向断言
`^[^:]{1,64}:b64:b64$` 与 _is_plaintext 镜像——JSON 与裸 token 明文双形态
命中；exit 三态 0 绿/1 fail/2 无法检查，表列不存在=1 非环境）+
`tests/test_uc39_deploy_checklist.py`（7 用例，实弹三态+双形态命中）。
关键裁定：完成判定=明文残留0+空id残留0+backfill failed 零命中三项全绿
（版本表盖章=辅助信号，与数据回填无因果）；下线观察期统一 P-8 的 4 周
口径（模块 docstring"连续观察期内"语义兼容不改码）。回填链零改动。
剩余 = 灰度后 P-8 下线（挂账 #12，触发条件未到）。

**N5 配置化平台化收口（2026-11-07，867b38f68，UC37）**：挂账 #1/#3/#4
落地——"OAuth 配置化平台化 + 仓库配置支持配置化平台化"需求完整兑现。
gitee OAuth 渠道卡在册（providers 内置默认，管理面填 client_id/secret
即启用）+ 前端 PAT 入口平台泛型（platform_tokens）+ 白名单列驱动
（_whitelist_column_for 配置派生）+ display strip 统一（stripRepoPrefix
正则清单）。新平台全链接接入 = 管理面配平台卡 + OAuth 渠道——纯配置
零代码。评审与落地记录：d90-n5-platform-config-review.md。
    原登记三项：

   沉淀）：① PAT 权威存储归属——grape_t_repo_credentials 存不存 PAT、source=pat
   是同步副本还是查询视图（PAT 现存 user_credentials+external_identities
   双写，与 source 三态的表达裂缝）；② TokenResolver 策略链固定四级还是
   按 source/cred_type 动态排序（决定 source 是元数据还是查询驱动）；
   ③ UserToken actor 语义显式化——现实现 actor=host_user_id（仓库归属者，
   非 API 调用者），触发者审计走操作日志（防后人当 bug 修签名）。
11. GlobalBot 缺失静默降级可观测性——**D91/S118 已修**（告警+守卫落盘
   2026-11-07）；**文件 fallback 命中告警——D92/S119 已修**（隐形第二
   权威源命中即告警，含 migrated 状态）。运维需将 "global bot token
   missing" 与 "FILE fallback" 两条告警纳入面板——后者清零是挂账 #12
   文件 fallback 下线的判据之一。
12. 读取侧历史列回落下线（S82 迁移中间态收尾）：写入侧已单写
   grape_t_repo_credentials（S82 双写移除），读取侧 RepoBotToken"rc 优先→历史列
   回落"仍在。下线条件（D90 四次扩项量化）：双写回填稳定后，旧列读回落
   命中计数连续 N 观测周期为零且无回滚事件（N 契约定；RepoBotToken
   回落已有 log.warning 观测基础，补命中计数即采集）→ 删回落分支+
   UC22④ 守卫同步收窄。触发前不动（回落是存量数据安全网）。
   **观测口径修正（2026-11-07 用户拍板）**：此前接的计数器模块（credential_
   observability）已删除——零消费方（无读取面、进程内存重启即清零），
   用不到不设计。下线判据的采集 = 既有 log.warning（'historical column
   fallback hit'）：运维 grep 该日志零命中 + 无回滚事件即判据，N 值/周期
   在触发评审时约定。
13. cred_type × source 组合约束（D90 最高优先禁止项，D92/S119 守卫
   在场）：oauth_fill 落库行禁占 bot 级 cred_type（独立命名如
   oauth_token——主键精确匹配下值域隔离=第一道免费防线），source 过滤
   为纵深第二层；UC23① 写入文件白名单守卫强制 provisioner 实现走
   D90 评审。同族禁止：oauth_fill 不得做成第四种隐式 PAT 来源。
14. 路径 A 失败语义显式化（D90 契约语言化）：三级全空→空串返回、
   调用方处理（UC23③ 行为锁定）；chat 链与 TokenResolver 底层同源
   实锤（UC23④ 守卫），actor 差异（触发者 vs 归属者）见 D90 前置 8。

### D89：双卡体系治理方向修正——领域分离 + 基础设施薄共享（S116，2026-11-07）

**来源与拍板**：续审追问"华为云 OAuth 登录（无仓库体系）是否支持"暴露挂账 #5
原措辞"两套卡体系统一"方向性偏差——业界共识是认证与资源访问在契约上分离
（GitHub OAuth App 身份 vs GitHub App 资源双连接；VeeCode/Backstage SCM 轴 ×
Identity 轴可混搭互不隐含；Keycloak 身份 broker 与 SCM 集成分层），共享的是
基础设施不是领域语义。用户拍板修正方向并二次收口四处措辞（三域自解释/
不断言唯一形态/凭据存储 gate/方案文档六输出物+触发条件）。

**归因三步法**：①基线对照——D10 双卡分离与 D40/S60 三域解耦在基线与分支均
已存在，产品代码现状即目标态（登录链五函数体零仓库域引用，providers.py 唯一
收敛点 _channel_emails_endpoint，external_identities 零 VCS 列）；②偏差仅在
挂账 #5 的登记措辞（统一=目标）与本设计文档无实现债；③本批改动=文档口径
修正 + 结构守卫防回潮，零产品代码变更。

**三域定义（自解释，依据 D10/D40）**：登录身份域（oauth_providers_config +
channel_strategy + external_identities——只答"你是谁"）；VCS 资源集成域
（vcs_providers_config 平台卡 + TokenResolver/VcsApiClient——只答"能访问
哪些仓库"）；共享基础设施域（OAuth 回调处理器、token 加密存储、审计事件
格式、配置校验框架——可收敛但不含领域语义）。

**薄共享边界（按风险升序，挂账 #5 全文为准）**：①配置校验框架（共享校验
框架/错误码格式/密钥引用校验；不共享 provider schema 与字段语义）；②展示
元数据协议（共享卡片描述协议/UI schema；不共享登录字段与 VCS 字段业务
语义）；③凭据存储适配层=最高风险 gate（仅 secret reference/vault adapter/
加密信封/轮换钩子；禁止共享 OAuth token 与 VCS installation token 的语义/
scope/授权决策/审计口径；先威胁模型后实现，排最后）。

**方案文档输出物（六件，未满足触发条件不进统一实现排期）**：可共享层/
禁止合并语义边界矩阵、共享接口契约、安全威胁模型、迁移兼容策略、触发
升级条件、Owner 与评审节点。

**实现（S116 结构守卫，tests/test_s116_dual_card_domain_separation.py）**：
UC21① 登录链五函数体零仓库域引用；UC21② providers.py 唯一收敛点守卫；
UC21③ external_identities DDL 零 VCS 列；UC21④ docs/ 零"卡体系统一"旧口径。
守卫口径先实弹盘点（五函数体逐个 grep 四令牌均零命中）再落守卫，对齐
s111 UC15 既有写法。

**RED 验证（真注入法）**：向 _handle_oauth_login 函数体注入
`from src.kernel.token_resolver import TokenResolver` → 守卫 1 failed 精确命中；
恢复后 4 passed；git status 确认 src/ 零改动。RED②（providers 第二收敛点
`_evil` 模拟）与 RED③（旧措辞正则自测命中/不误伤正常行）逻辑级验证通过。

**对账记录**：新守卫 4 passed；回归（s111/s115 相邻守卫域）见提交 message。
**明确不动项**：产品代码零变更（现状即目标态，无缺口可修）；D10/D40 原文
不动（本节是方向修正登记不是重定义）；s66 混合债；另一会话 untracked docs。

### D95：N5 收官补验——场景链用例 + 缓存版本纪律收口 + migrate 合并修复（UC38，2026-11-08）

**来源**：用户指令"对最新收官状态（D89-N5）补全场景/场景用例/测试用例章节
并同步优化既有代码"。收官状态补验三件套落地 + 实码审计揪出两个真实缺口。

**缺口一（测试基建纪律）：N5 批次缓存版本未随批 bump**。867b38f68 改
account.js/observability.js/app.js 三个页面模块，但 index.html 对应 ?v=
未 bump（account 49/observability 72/app 216）——UC11 纪律缺口，浏览器旧
缓存下 platform_tokens 泛型入口与 strip 统一不生效。本批随批 bump：
account→50、observability→74、app→217，test_uc38⑤ 把三模块纳入版本门槛
断言（UC11 清单延伸）防回潮。

**缺口二（真产品缺陷）：migrate 盲覆盖挤掉内置渠道卡**。
_sync_providers_from_config（user/db.py，oauth-providers.json→meta 同步）
原语义"文件是真相源，覆盖写 oauth_providers_config"——N5 之前内置默认只有
gitcode/github 时合理；N5 之后 gitee 渠道卡入 _DEFAULT_PROVIDERS，配置文件
只含部分渠道的部署（旧残件/测试环境）一执行 migrate 就把 gitee 从登录页挤掉。
**旧守卫假绿窗口实锤**：UC37① 单跑常绿纯属 import 时序侥幸（user/db 模块级
migrate 在 meta 表建表前执行→"no such table"失败→built-in 兜底含 gitee）；
meta 表先建好时（conftest 顺序变化）migrate 成功→覆盖写→UC37① 必红。
**修复（对齐 S74 admin PUT 既有写法）**：合并式写入——以 _DEFAULT_PROVIDERS
为基 merge，同键文件条目胜出（文件仍是真相源），未提及渠道保底。RED 实证：
stash 合并修复→UC38① 红（migrate 成功路径）；pop→绿。
**UC38① 同步升格**：双卡链接断言在 migrate 成功路径下仍绿（消灭假绿窗口）。

**场景链用例（UC38，tests/test_uc38_n5_closure.py 6 用例）**：
①双卡链接行为（渠道轴∩平台轴同含 gitee，migrate 成功路径下验证）；
②泛型 PAT 入口行为闭环（update_credentials 真函数实弹：合法 gitee PAT→
verify 后双写 user_credentials gitee_pat + external_identities、他人已绑
409、未注册平台 400 fail-closed、存量双平台专用字段 S52 语义）；
③白名单列驱动端到端（建列→add→list 真实 sqlite 落库 + 未注册渠道拒绝）；
④display strip 全链统一（页面模块零内联 strip + 零本地重定义）；
⑤缓存版本门槛（三 N5 模块 ?v= 下限断言）。
用户构造走 grape_accounts 权威路径（对齐 test_user_db，user_accounts 写入
冻结纪律）。

**明确不动项**：_load_providers 加载侧语义不动（meta 有配置→DB 全量读，
与写入侧合并修复配合后语义自洽）；oauth-providers.example.json 不动
（模板已含双渠道完整协议参数，gitee 走内置默认 + 管理面启用的 N5 语义）；
admin PUT 合并式保存不动（本批修的就是对齐它）。

### D94：OAuth 未配置 + 用户 PAT 场景——github PAT 流完好性守卫（S121，2026-11-07）

**来源**：用户指定场景深度核对——"GitHub OAuth 未配置，用户登录后配置
OAuth 的 PAT"。实码逐环核对结论：**功能完好，无破坏**。本批把核对结论
升为守卫（UC25）并登记一处文案不对称。

**场景链路（实码语义）**：
- **登录**：渠道卡在但 client_id 空 → `/api/auth/oauth/providers` 返回
  configured=False → 前端无 GitHub 登录按钮 → 用户走密码/邮箱注册
  （grape_accounts 本地账号体系，独立于 OAuth）。
- **PAT 配置**（专用字段路径，`_LEGACY_PAT_FIELD_PROVIDERS={"gitcode",
  "github"}`）：`PUT /settings/credentials {github_token}` → 验证
  api.github.com/user（无效 token 在此被拒）→ 写 **external_identities
  （access_token=PAT AES-GCM 加密，status=1，refresh_token 为空）** +
  unified_user_mapping.github_login；不写 user_credentials `<p>_pat`。
- **消费**：TokenResolver 第 4 级 `fetch_user_token` ①
  `get_user_token_for` → `_get_oauth_token_from_identity`（status IN
  (1,4) 解密）→ 命中 PAT 值；`_has_refresh_token`=False（refresh_token
  空）→ **跳过刷新直返**（R2：PAT 不过期不刷新，防误打刷新端点）。
  PAT-only 用户提单天然被覆盖（UC24⑥ 已锁语义）。第 4 级 ②
  `<p>_pat` 兜底对 github 空转（专用字段不写该键）——① 已命中无影响；
  ② 兜底定位=config-only 平台 PAT 通道（D90 契约可顺手明确）。
- **建单特例（Issue #343 拍板）**：`create_task_via_issue` github 分支
  统一走仓库 bot（`get_github_token`），用户 PAT 不参与 github 建单
  （归属=仓库 bot；用户 PAT 用于 chat 回帖等触发者场景）。

**401 文案不对称（现状登记，非缺陷）**：gitcode 用户路径=
GITCODE_USER_TOKEN_INVALID_MSG（"您的 GitCode 登录已失效"）；
github 用户路径走 BOT_TOKEN_INVALID_MSG（bot 文案）——Issue #343
"统一走仓库 bot"决策的连带。对称化属后续批次，UC25④ 锁现状防无意改写。

**S121 守卫（tests/test_s121_oauth_absent_pat_flow.py，4 用例）**：
UC25① PAT 写入链（external_identities 载体 + 读取链对称）；UC25② 刷新
豁免（豁免分支先于刷新调用，防 PAT 误送刷新端点）；UC25③ github 建单
bot 拍板（分支内禁用户 token）；UC25④ 文案不对称现状锁定。

**RED 验证**：① github 分支伪造切用户 token → UC25③ 红；② 伪造豁免
分支移除 → UC25② 红；恢复→4 passed、src/ 零改动。

**对账**：守卫域全量见提交 message。**明确不动项**：行为零变更（本批
纯守卫）；401 文案不对称（登记不修，对称化归后续批次）；
`<p>_pat` 兜底对 github 空转（① 已命中，行为正确）。

### D93：agent 全链路凭据流——双链收敛守卫（S120，2026-11-07）

> **D100 边界声明补充（2026-11-08）**：本节"双链"描述的是**服务端侧**
> 凭据流转；**执行侧（agent/worker 进程）自 vcs-env-token-boundary P2
> 收口起不持有任何平台凭据**（BOT_*/USER_* 不注入 env）——执行侧持有
> tk_ 会话凭证（TTL 6h/sha256 落库/scope 三重限定/对平台 API 无效），
> VCS 操作经 grape CLI 转发服务端。GRAPE_LINKED_REPO_TOKENS 的消费方
> 是 grape CLI/服务端本地链，非 agent 进程 env。详见方案稿 §1.0 消费域
> 边界段。**注意**：D93 描述的 pipeline env 派发链中 GRAPE_LINKED_REPO_
> TOKENS 是否仍有存续消费方，属 P2 后遗留核查项（登记不展开）。

**来源**：用户要求深度检查 chat/VOD agent 全链路 PAT 凭据流程是否被
D89-D92 改动破坏。实码逐层追溯（webhook→pipeline→agent_tasks→worker→
回帖）结论：**无破坏**——D91/D92 两条行为变更均为纯观测告警，token 值
流转逐字节未动。本批把核对结论升为守卫（UC24）。

**全链路凭据触点图（实码语义）**：
- **机器身份链（路径 A）**：webhook 验签（webhook_secret）→ A2A 归属
  （TokenResolver 卡派生）→ pipeline `_inject_linked_repo_tokens` →
  `get_repo_bot_token`（=TokenResolver）→ GRAPE_LINKED_REPO_TOKENS env
  派发（不落盘、与任务同生命周期）→ worker repogit 读 env + askpass
  （0o700，worktree cleanup 时随目录删除）；回帖/API 走 `_get_task_token`
  （R11.4 统一入口：task 行 bot token 优先 → 降级
  get_repo_token_with_refresh / get_github_token——两者均内部委托
  TokenResolver）。
- **用户身份链（chat/路径 B）**：worker agent.py 仅 chat 任务且
  user_id>0 → `get_user_token_for_repo` → `get_user_token_for`
  （600s 预刷新）→ `_stream_runtime_async` 以触发者身份回帖；Issue
  #344 语义=不读 task payload 缓存明文（实时取）。

**双入口同源（S119 UC23④ 的链路级确认）**：chat 链与 TokenResolver
第 4 级底层都收敛 get_user_token_for——D90 OAuth/PAT 调序一次生效
两条链。actor 双入口并存合法：chat=触发者（user_id），策略链=归属者
（host_user_id）。

**S120 守卫（tests/test_s120_agent_chain_credentials.py，4 用例）**：
UC24① 回帖链收敛（get_task_token 降级分支+github 分支必经
TokenResolver 委托函数，直连 db 读 token 列即红）；UC24② env 派发链
（_inject_linked_repo_tokens 必经 get_repo_bot_token，SQL 直查形态
[cursor().execute / FROM rc/managed_repos] 即红——注释提及不触发）；
UC24③ chat actor 行为验证（task user_id=触发者，非归属者）；UC24④
明文不落 payload（task.get("user_token") 回读形态禁绝）。

**RED 验证**：伪造 SQL 直查注入 _inject_linked_repo_tokens → UC24②
红；伪造 task.get("user_token") 回读 → UC24④ 红；恢复→4 passed、
src/ 零改动。

**chat 帮用户提单链（UC24⑤ 补，用户追问"chat 提单场景看了吗"后补齐）**：
建单统一入口 `create_task_via_issue`（issues_center，~48K 字符巨函数）
token 三态优先级：① `_preauth_token`（autopilot 等内部调用预注入 bot
token）② github 仓 → `get_github_token`（bot 链）③ 默认 → 触发者用户
token（`get_user_token_for_repo`，user["id"]=会话用户）。**401 反应式
刷新在建单链已存在**：`_http_code == 401 and _using_user_token` →
`_refresh_user_gitcode_token` 强制刷新 → 单次重试 `_create_issue_unified`
→ 仍失败走"重新登录"引导——**D90 对标差距 A（被动 401 兜底）在建单链
已有实现**，契约拍板时以建单链为参照实现 api_client 层通用兜底。
UC24⑤ 守卫断言：三态分支在场 + 401 刷新重试在场（count≥2）+ 统一入口
（禁直连平台 API）。回归：create_issue lifecycle/wiring 11 passed。

**调试教训**：UC24③ 首版 task 漏 agent_type="chat"（分支进入条件）
导致 token 空假红——复刻源码逻辑的用例必须逐条件对照源码（分支条件
也是语义的一部分）；UC24② 首版禁词含"managed_repos"误伤注释（docstring
解释为何 worker 无此表）——禁词改 SQL 特征形态（cursor().execute /
FROM 表名），注释不触发。

**对账**：d37 token 统一链 6 passed、agent env/bot identity 13 passed、
守卫域全量见提交 message。**明确不动项**：agent 链取用行为零变更
（本批纯守卫）；_get_task_token 的 github 分支双入口（已收敛
TokenResolver，行为等价）。

**UC24⑥ 用户提单凭据语义（2026-11-07 问答轮实锤入守卫）**：
建单链用户凭据的实际形态——external_identities 里"以该平台身份存的
token"：OAuth 登录用户 = OAuth access_token（带 600s 预刷新 + 401
反应式刷新）；PAT-only 用户（gitee）= S52 双写进身份域的 PAT 值
（`get_user_token_for` 直读，PAT 无 refresh_token 不刷新，R2 语义）。
**显式拒绝不降级是铁律**：token 空/失效 → 400 "{平台} Token 未配置
或已失效，请重新绑定/登录后重试"——绝不回落 bot（注释明示"token
缺失≠回落 bot"），保证 issue 作者归属真实性。UC24⑥ 守卫断言：拒绝
文案在场 + 按触发者会话用户取 token + identity 域读取链。RED：完全
移除拒绝 raise → 红（首版探针保留文案的假 RED 修正：RED 探针必须
移除被断言的目标本体，不是改写它）。

### D92：仓库凭据语义收口——三类凭据正名 + cred_type 隔离守卫 + 文件 fallback 告警（S119，2026-11-07）

**来源**：用户评审"三种 PAT 全景"的六点收口，实码核对定性采纳四条、
修正一条、否决一条（路径 A 第 4 级"未定义"不成立——host_user_id==0
显式短路且 UC22① 守卫已锁，D91 已显式化）。

**三类仓库相关凭据正名（替代"三种 PAT"）**：
1. **系统 bot PAT（全局 bot）**：meta `global_bot_{provider}_token`
  （gitcode 存量文件 fallback），admin 配置，平台级一份；路径 A 主力。
2. **仓库级 bot 凭据**（正名，非 PAT）：grape_t_repo_credentials cred_type=
   bot_*/webhook_token 族，建仓链派生，机器身份；路径 A 专属（路径 B
   跳过）。**迁移期含 managed_repos 历史列回落（读侧），下线条件挂账 #12**。
3. **用户 PAT**：user_credentials `<p>_pat` + external_identities 双写
  （S52），用户配置，用户身份；路径 B 第 4 级兜底。

**主体差异 = 路径分档根因**：机器身份（1/2）与用户身份（3）永不混用
——命名混同（都叫 PAT）会掩盖这一根本区别，契约讨论必须用正名。

**cred_type × source 隔离（D90 最高优先禁止项，UC23① 守卫）**：
oauth_fill 落库后 grape_t_repo_credentials 将并存机器类行（cred_type=bot_*）
与用户类行（source=oauth_fill）——RepoBotToken 按 (repo_key, cred_type)
主键精确匹配读 bot token，**若 oauth_fill 行复用 bot 级 cred_type，用户
凭据即被当机器凭据读取，路径 A 隔离破了**。防线两层：cred_type 值域
隔离（第一道，免费——oauth_fill 行用独立命名如 oauth_token，主键匹配
天然不命中）+ source 过滤（纵深第二层）。UC23① 守卫锁写入文件白名单
（既有 3 处：user/db.py、admin/config_store.py、api/db_setup.py），
新写入文件出现即红强制走契约评审。**同族禁止项：oauth_fill 不得做成
第四种隐式 PAT 来源。**

**文件 fallback 命中告警（本批产品代码唯一变更，_cached_get_meta）**：
文件是隐形第二权威源——meta 被误删/迁移未完成时静默生效，配置态不可
观测。补 log.warning（含 meta key/migrated 状态/下线指引）。命中≠错误
（迁移期合法），但挂账 #12 的下线判定依赖"此告警不再出现"。迁移完成
开关（global_bot_token_migrated=1）语义保持。

**chat 链收敛实锤（UC23④ 守卫）**：chat 链（get_user_token_for_repo）
与 TokenResolver 第 4 级**底层同源**——都收敛到 get_user_token_for
（刷新/600s 预刷新/OAuth-PAT 顺序物理共用），D90 调序一次生效两条链。
差异仅 actor：chat=任务触发者（user_id），TokenResolver=归属者
（host_user_id）——D90 前置 8 的 actor 语义，两入口并存合法。UC23④
两级链断言（get_user_token_for_repo→get_user_token_for；
UserToken→provider.fetch_user_token→get_user_token_for）防未来分叉。

**RED 验证**：① stash 告警代码→UC23② 红→pop 绿；② 伪造新写入文件
（db_oauth_fill_proto.py）→UC23① 红→删除绿。**调试教训**：守卫白名单
路径基准（_SRC vs _SRC.parent）首次跑偏产生假红——路径断言先独立脚本
验证再入测试；废话断言（`... or True`）写出来即删，不算验证（S118 同款
教训二次强化）。

**对账**：s118+s60+s116 守卫域 17 passed。**明确不动项**：三种凭据存储
与取用行为零变更（文件 fallback 告警纯观测）；路径 A/B 分档零变更；
oauth_fill 落库等 D90 契约（本批守卫是其实现时的第一道闸门）。

### D91：仓库凭据策略链收口——路径分档守卫 + GlobalBot 缺失告警（S118，2026-11-07）

**来源**：用户评审"仓库凭据整体逻辑"时对 TokenResolver 提出四个语义收口点，
实码核对后定性：路径分档是设计本意但只活在注释（守卫缺位）、GlobalBot
缺失静默（真差距）、PAT 权威存储归属/actor 语义/策略链排序三项进 D90 契约
（见 D90 前置事实 6-8）。

**策略链路径分档（实码语义，本批守卫锁死）**：
- 路径 A（平台 bot 托管，host_user_id=0）：Session → RepoBot（rc 权威→
  历史列回落）→ GlobalBot 兜底；**UserToken 不生效**（host_user_id==0
  短路）——机器身份语义：平台 bot 操作绝不落到用户 token。
- 路径 B（用户托管，host_user_id>0）：Session 跳过 → RepoBot/GlobalBot
  跳过（降级脏写防护）→ **归属者 User token 第一公民**（actor=host_user_id，
  从 repo_key 派生）——用户托管仓由归属者凭据执行，调用者身份不参与
  凭据选择（触发者审计走操作日志，不混入凭据域）。

**GlobalBot 缺失告警（本批产品代码变更，token_resolver.py GlobalBotToken
._fetch）**：路径 A 全局 bot 是主力凭据，UserToken 不生效 → 空串=策略链
必然失败；原静默返回让"未配置/配置错/被误删"三态不可区分，失败只在任务
报错时暴露。补 log.warning（含 provider/repo_key/meta key 指引），行为
不变（仍返回空串走完策略链）——告警是观测点，不是降级开关。

**S118 守卫（tests/test_s118_strategy_path_tiering.py，4 用例）**：
UC22① 路径分档结构守卫（RepoBot/GlobalBot._fetch 必含 _is_path_b 跳过、
UserToken._fetch 必含 host_user_id==0 短路）；UC22② GlobalBot 空值告警
结构守卫；UC22③ 行为验证（路径 A 缺失→空+告警 caplog 捕获；路径 B→
归属者 token 胜出 level=user）；UC22④ 写入侧单写守卫（S82 语义：
add_managed_repo 零 managed_repos 凭据列写回——迁移中间态只许在读取侧）。

**RED 验证**：git stash 告警代码 → UC22②③ 2 failed 精确命中 → pop →
4 passed。调试教训：告警捕获必须用 pytest caplog（手工 root handler 在
pytest logging 插件下 records 恒空——独立脚本同代码全通，pytest 内失败，
channel 差异实锤）；mock 网络缝在 provider.fetch_user_token（类级 patch），
不 mock deps 内部链。

**对账**：s60+s116+s118 守卫域回归见提交 message。**明确不动项**：策略链
四级顺序与行为零变更（告警是纯观测增量）；路径 A/B 分档行为零变更（守卫
锁现状）；PAT 归属/actor 语义/排序三项归 D90 契约不抢跑。

### D90：oauth_fill 落库消费端——跨域契约先行的待闭环登记（2026-11-07）

**N1 通道加密与 UUID 主键（2026-11-07 评审新发现 + 用户拍板，独立 commit a91718bdc + 后续 commit）**：
契约评审产出 N1 安全缺口（user_credentials 明文 JSON 落 PAT 本体与 OAuth 副本，
credential_crypto 零引用）+ 用户两条铁律（user_accounts 死表零写入；所有新增表
UUID 主键 + created_at/updated_at 双列应用层维护）。处置（详见
docs/weekly-09/multi-platform-decouple/oauth-fill-contract-review.md 第 5/6 节决议）：
- **写入面冻结**：user_accounts 零写入守卫（AST 级 GC1-GC4）+ 10 个 deprecated
  写函数删除 + 启动回填 INSERT 删除（a91718bdc）。
- **通道层加密**：set_credential 写前 encrypt / _load_credential_json 读后 decrypt
  （明文行兼容零迁移）——user_credentials 与 external_identities 两存储面对称，
  新平台/新凭据类型自动继承加密。
- **user_credentials UUID 主键**：id TEXT PRIMARY KEY（36 字符标准形式）+ 复合键
  降级 UNIQUE(user_id, provider) + 写入改 ON CONFLICT DO UPDATE（conflict 分支
  不动 id，审计锚点不悬空）+ created_at/updated_at 应用层维护 + 存量库幂等补列
  回填。provider 值域注释改派生规则三族（云凭据裸名/<p>_pat/<p>_oauth，卡驱动
  非固定枚举——原 DDL 注释 'gitcode','feishu','huawei','codearts' 是错的，feishu
  从未写入此表）。
- **表设计七条铁律**入册 requirements「表设计铁律」节（UUID 主键/时间双列/禁
  INSERT OR REPLACE/枚举实码核对/通道加密/审计表无级联+credential_kind 预留/
  P-2c 替代表定位），所有后续批次必须遵守。
- **P-2c 定位修正**：user_platform_credentials 是 user_credentials 的**替代表**
  （非补充），user_credentials 走 P-8 下线路径；credential_usage_log 预留
  credential_kind 字段。
- 待办：历史明文行加密回填脚本（可回滚 + 前后抽样校验，与 UUID 回填同批次但
  分开 migration——UUID 回填非破坏性、加密回填破坏性，回滚锚点=加密前备份）。

**UC26 场景-用例-测试映射（S122，2026-11-07）**：需求文档 UC26 行 +
「表设计铁律」节（七条）。场景=凭据存储面治理（写入冻结/通道加密/UUID 主键/
存量迁移/值域注释实码核对）。守卫用例：①写入冻结 GC1-GC4（AST 级，
tests/test_user_accounts_write_freeze_guard.py）；②通道加密三态（密文三段格式
落库/读回环/历史明文行兼容，test_s122 TestN1ChannelEncryption）；③UUID 主键
四断言（36 字符标准形式/ON CONFLICT 保 id/created_at 不漂移/updated_at 前移，
test_s122 TestN1UuidPrimaryKey——upsert_preserves_id 是 INSERT OR REPLACE
回潮的 RED 守卫）；④存量迁移幂等（旧 schema 补列+回填+重复跑不重填，
test_s122 TestN1MigrationLegacySchema）。方言翻译实弹：ON CONFLICT DO UPDATE
→ MySQL ON DUPLICATE KEY UPDATE（VALUES() 形态）、TEXT PRIMARY KEY →
VARCHAR(191)、UNIQUE 约束保留（mysql_compat translate_sql 实测输出核对）。
验证：128 passed（含 s122 7 用例 + s116/s119/s120 守卫族零污染）。
**架构收敛跟进（2026-11-07 同日，用户"架构简洁合理"拍板）**：init_db 巨函数
（231 行内联迁移）收敛为"DDL 声明 + 两个一次性迁移模块"形态——
- src/user/legacy_column_migrations.py：存量列补齐/重映射/孤儿清理/user_accounts
  死表收敛（5 个私有函数各带下线判据 docstring）
- src/user/credentials_backfill_n1.py：N1 UUID 补列 + 明文加密回填（此前已独立）
- init_db 收敛后 26 行（DDL 4 行 + 模块调用 2 行 + repo_key 迁移 1 行），
  src/user/db.py 净 -157 行（321 行变更，+82/-239）
- 迁移代码生命周期显式化：上线=init_db 一行调用；下线=删模块文件+删调用行，
  下线判据各函数 docstring（零命中观察期）
- 验证：139 passed + 旧 schema 全链实弹（补列/加密/列 DROP/值搬迁/幂等）+
  三探针复跑 ALL-PASSED
**收敛收尾（同日第二轮，同步优化）**：①init_db 重构残留清理——旧内联迁移函数
（_migrate_user_credentials_uuid_backfill/_encrypt_backfill）删残留 + db.py 侧
_is_plaintext_credential 重复定义删除（判定逻辑唯一归属 backfill 模块）；
②守卫固化：test_s122 新增 UC26⑤⑥——init_db 薄函数守卫（内联迁移特征
ALTER/UPDATE/PRAGMA 禁绝 + 行数≤35，RED 注入验证：塞内联 ALTER 即红）+
迁移模块在册可调用（run() 可导入 + 调用链完整 + 幂等重入零变更）；
③表设计铁律第 8 条入册 requirements（迁移逻辑模块化）。
最终验证：s122 9 passed（RED 注入 1 failed 精确命中→恢复绿）+ 宽域 139 passed。
**消费端批步 1 落地（2026-11-07，UC27/S123）→【D100 废弃（2026-11-08）】**：
user_platform_credentials 表 + ORM 三件套。原架构决策（用户拍板"按领域对象
模型区分"）历史记录保留供审计：models_platform_credential.py（UUID 主键/
双指针/status stale/时间双列）+ UserPlatformCredentialRepository（get/upsert/
mark_stale）+ 盖章迁移 202611070001。
**D100 裁定（用户拍板，2026-11-08）**：upc 方案整体废弃——三处逻辑错误
（指针的指针冗余绑定/零消费写入/审计锚点错位，方案稿 §五）；P2=(a)
provisioner 整体退役；R1-R3 删净（迁移登记整段删除——upc 从未上生产，
无需 DROP 迁移、无需 no-op 壳）；s123/s124 测试删除。替代形态：
user_credentials（用户级唯一权威）+ external_identities（OAuth 权威）+
登录成功零额外落库（与基线行为一致）。D10"三合一"表述收口：②仓库凭据
自动填充正式定为用时取用 lazy（注册表零消费死代码随批清理）。
**消费端批步 2 落地（2026-11-07，UC28/S124）→【D100 退役（2026-11-08，P2=a）】**：
provisioner 端口 + 4 处接线已随 upc 废弃整体删除——登录成功后零额外落库，
与基线行为一致（基线本无授权记录实体）；S116 守卫语义不变（豁免点随辅助
函数消失，守卫只收紧不放松）。原始实现记录：provision_from_oauth
（ProvisionResult 失败契约/幽灵 user_id 拒绝）+ grape_auth 辅助 4 处接线 +
s124 守卫（恰接线断言）。删除留痕：grape_auth.py provision 删除点注释 +
方案稿 §四 R7 原始目的链存档。原"实码发现"段（unbind platform_card 引用
不锁 unbind）随 s124 删除失效，登记不扩口径。
- 验证：s124 7 passed + s116 原守卫共存 + 宽域 139 passed
**消费端批步 3 落地（2026-11-07，UC29/S125；同日用户修正：删 flag）**：
P-3A PAT 优先调序（单序）。
- token_resolver.py GenericVcsProvider：fetch_user_token 单序
  ①<p>_pat（显式意图）→②OAuth 兜底——D10 原文无灰度，flag 双序分支是
  过度设计（维护者需理解两个序），2026-11-07 用户拍板删除
- 分轨监控：命中 level（pat/oauth）落 log——PAT 401 率归因信号
- UC29 五断言面（flag 双态行为矩阵 + 回落 + 空语义 + 监控信号）
- 验证：s125 5 passed + token_resolver 影响面（worker/agent/策略链）35 passed
  零污染 + s24 预存失败与本改动无交集（归因登记）
**消费端批步 4 落地（2026-11-07，UC30/S126）**：credential_usage_log 审计表。
- src/persistence/credential_usage_log.py：Core table 构造（列名/类型安全）+
  log_usage/list_by_credential（审计只增不改，无 update/delete API——AST
  守卫禁篡改通道）
- 盖章迁移 202611070002（safe）；credential_kind 预留（P-2c 零迁移）；
  无外键级联（软引用，凭据删除保留审计历史）
- provisioner 审计链闭环：步 2 provision 成功即记 usage（归属者=user_id，
  触发者=0=系统 OAuth 流程，上下文=provision）——P-3 并存审计门槛就位
- Core insert 走 ORM engine（StaticPool 同连接同事务世界）；失败不抛
  （审计失败不阻断业务，log.warning）
- 验证：s126 5 passed + provision 审计链实弹 + 宽域 114 passed
**消费端批步 5 落地（2026-11-07，UC31/S127）**：401 被动兜底五约束。
- src/vcs/oauth_401_retry.py：oauth_401_retry 包装（api_call(token) 形态 +
  current_token 参数；约束 1-5 全落地——单次重试/PAT 分流/single-flight
  锁/403 不刷/凭据类型判定）
- invalid_grant 失效流程接线点修正（实码定位）：契约原文"api_client 拦截器"
  不可行（VcsApiClient 无 user 上下文；bot token 无刷新语义）——落点二分：
  包装层（src/vcs/oauth_401_retry.py，用户 OAuth 链调用方用）+ 失效标记在
  refresh_oauth_token 的 HTTPError 400/invalid_grant 分支（身份域 R10 status=4
  语义同位）；bot token「失效」文案路径不变
- P-7A 前置就位：403 透传（insufficient_scope 判定归步 6 adapter 层）
- 验证：s127 7 passed + 宽域 121 passed
**消费端批步 6（2026-11-07，UC32；同日用户拍板部分删除）**：
- ~~src/vcs/credential_observability.py~~：已删除——classify_github_403 与
  CredentialMetrics 均零消费方（分类器无调用点、计数器无读取面且进程内存
  重启即清零）。"用不到不设计"；P-7 场景等真有 403 分类需求时再实现；
  观测口径 = 既有 log（level 命中/历史列回落 warning）grep 对账
- D90 消费端批 6 步中步 6 的产出回撤，步 1-5 不变
- D90 消费端批 6 步全落地：P-2c 建表（步1）→ provisioner 端口（步2）→
  P-3A 调序（步3）→ P-5 审计（步4）→ P-6 401 兜底（步5）→ P-7/P-8（步6）。
  剩余 = 生产灰度期：flag 开启 + 4 周观测（P-8 决议口径）
- 验证：s128 6 passed + 宽域 138 passed

**来源**：续审"代码能力与 D89 设计是否一致"逐原则核对发现——D10 拍板的
oauth_fill"三合一"（身份绑定 + 仓库凭据自动填充 + 生命周期托管）中，
身份绑定与刷新托管均已实现（provider_link 注册表 + deps 预刷新），
**落库消费端未接线**：登录回调成功后不产生 source=oauth_fill 的仓库凭据
记录。用户拍板：要补，但**先定跨域契约再补消费端**——直接在登录回调写
grape_t_repo_credentials 会亲手破坏 S116 守卫（登录链零仓库域引用）。

**归因三步法**：①基线对照——oauth_fill_targets()/refresh_token_for 注册
表为分支 D40/S60 新增；②diff 引入行——注册表是声明面，无消费端写入；
③行为现状——仓库域凭据取用走运行时 lazy 链（TokenResolver
fetch_user_token → get_user_token_for → external_identities 实时读+预刷新），
功能等效但无显式落库。

**"用时取用"定性（D90 时代：临时等效，非长期方案）→【D100 收口：lazy=终态
（2026-11-08 用户拍板）】**：lazy 链覆盖任务执行鉴权（gitee 用户 OAuth 登录后
绑仓库/建 webhook/agent 任务全链可用）。D90 时代定性"临时等效"的三条理由
在 D100 后的新答案：① 审计可见性——登录/绑定事件已有 auth_audit + ei 行
（upc 授权记录行被证伪为零消费冗余）；② 降级能力——"用时取用"本身就是
降级形态的终态（登录域不可用时任务本就无法以用户身份执行，预填凭据只是
把失败延迟）；③ 优先级表达——P-3A 单序已实现（PAT 优先，S125 守卫），
无需同表承载。原三缺口全部由新形态闭合或证伪，不再是要补的账。

**补齐路径（D90 时代设计 →【D100 已废弃：P2=a 整体退役】）**：
原 provisioner 契约设计（端口签名/存引用裁定/S116 扩展口径）实现过又随
upc 废弃删除——历史记录保留供审计，**不再是路线图**。现终态=登录成功零
额外落库（与基线行为一致），lazy 链即正式形态。
1. 先定跨域 provisioner 契约：仓库域暴露
   `RepoCredentialProvisioner.provision_from_oauth(user_id, provider,
   token_ref)` 端口，登录域回调成功后调用；仓库域决定是否以
   source=oauth_fill 落库（引用 LoginCredential 不复制 token，D9 语义）。
   否决方案 B（领域事件异步填充）：暂无最终一致性诉求，端口直调更简单
   且失败语义显式。**禁止登录域直接写 grape_t_repo_credentials 或读
   vcs_providers_config**。
2. 契约评审通过后补消费端（回调成功路径 + 绑定成功路径两处调用点）。
3. S116 守卫同步扩展：登录链零仓库域引用守卫升级为"零引用 **除
   provisioner 端口调用外**"——守卫加强而非绕过。工程口径：① 端口 FQN
   锚定稳定符号，守卫同时断言该 FQN 可导入（模块路径变更即红，防静默
   失效）；② 断言用 AST 级 import/Call 检查（非文本 grep——注释提及
   FQN 不触发，避免注释误报）；③ 约束面含**读写双向**：登录域不得写
   grape_t_repo_credentials、不得读 vcs_providers_config（原登记只提写，读约束
   一并锁死）。
4. grape_t_repo_credentials 表需扩 source 列（现 DDL 无此列，D9 的
   source(oauth_fill|pat|bot) 三态无承载物）。

**前置事实（实码核对 2026-11-07，2026-11-07 二次扩项）**：

1. **粒度错配（三选一评审，不只在两选项里选）**：grape_t_repo_credentials 主键
   (repo_key, cred_type) 是**每仓库**粒度；oauth_fill 是**用户平台级**凭据。
   候选：a) 虚拟键 `{provider}:__oauth__`——但会污染 repo_key 语义（仓库域
   查"该仓库有哪些凭据"需过滤虚拟键）；b) provisioning 延迟到首次绑仓库——
   但从不绑仓库的用户永不落库，"登录即填"审计价值打折；c) **独立表
   user_platform_credentials**（与 grape_t_repo_credentials 并列）——用户平台级
   凭据与仓库级凭据本就不同粒度，独立表最符合领域分离。契约文档三选一拍板。
2. **存 token 本体 vs 可刷新引用（first-class 决策，反向决定 provisioner
   端口职责边界）**：存本体 → 身份域刷新后必须同步更新 grape_t_repo_credentials
   （第二次跨域写入，端口职责从 provision 扩为 provision+sync，或 S116
   守卫为刷新同步开例外）+ 加密一致性约束（grape_t_repo_credentials 现 DDL 无加密，
   external_identities 是 AES-GCM——同 token 两处存储两套加密即安全裂缝）；
   存引用（external_identity_id 指针，不存 token）→ 刷新完全不影响仓库域，
   与"仓库域只消费活 token"原则最一致，但 source=oauth_fill 审计价值从
   "凭据快照"降级为"授权关系记录"。**倾向存引用**（与现行 lazy 链自洽、
   避免 token 双存），契约评审拍板。
3. **oauth_fill × PAT 优先级合并语义（DDL 表达不了，必须进契约）**：
   实码核对发现**现行运行时优先级与 D10 拍板相反**——D10 拍板"PAT 优先
   （显式意图），oauth_fill 兜底"，但 TokenResolver.fetch_user_token 现链
   是 ① OAuth 身份（get_user_token_for，有即返回）→ ② user_credentials
   `<p>_pat`：用户先配 PAT 后又 OAuth 登录时实际用的是 OAuth token。
   功能上两者都能用所以未暴露，但"显式意图优先"语义未实现。契约必须拍板：
   优先级方向（对齐 D10 则 fetch_user_token 需调序）、并存是覆盖/并存/分列、
   合并逻辑所在层（TokenResolver 还是 SQL）、source 三态查询合并写在哪。
4. **source 扩列细节**：backfill 策略（存量行默认 NULL 还是 'pat'——默认
   'pat' 会误标历史 bot 行）；约束形态（CHECK 枚举 vs 应用层校验；未来是否
   允许 manual_import 第四态）；查询若按 source 过滤则索引一并进 DDL。
5. **gitee scope 能力矩阵（五列，不只 push）**：读私有仓库 / push / 创建
   PR / 读组织与 webhook / token TTL 与 refresh 行为——契约评审输出
   "每渠道 oauth token 能力矩阵 vs PAT 能力矩阵"，缺项在渠道卡或文档
   显式标注"需 PAT 补充"。

6. **刷新链业界对标（2026-11-07 三次扩项，实码逐层核对）**：
   **已对齐业界共识的部分**——①主动预刷新：get_user_token_for 请求时
   检查+600s 缓冲（On-demand with Buffer 模式，高于 forge/OpenChamber/
   Kiln 的 5 分钟共识）；②并发竞态两层防御：分布式锁
   `oauth_refresh:{user_id}:{provider}`（TTL 30s，R20.27）+ 进程内锁 +
   抢锁失败后读最新 token 直接使用（= OpenChamber 文件锁模式）；
   ③刷新失败重读新 refresh_token 重试一次（R21.4.2，补偿式条件更新——
   接近 WorkOS 建议但非存储层 UPDATE...WHERE 形态）；④架构分层：
   刷新责任全在身份域（provider_link 注册表），仓库域只消费结果
   （= Backstage GithubCredentialsProvider 边界）。
   **真差距（契约评审新增两项）**——A) **被动 401 兜底缺失**：api_client
   收 401 只归因报错（"Bot Token 失效"文案），无"强制刷新一次+重试原
   请求"路径（Kiln 阶段化 on-auth-rejection → refresh+retry 缺位）；
   主动刷新覆盖不了的吊销/时钟偏差/调度失败场景会直接用户可感知失败。
   契约评审拍板：是否在 api_client 或 TokenResolver 层加 401→强制刷新
   →重试一次（重试必须绕过同一拦截器防死循环，业界共识）。B) **无定时
   扫描**：纯请求时预刷新，长期不活跃用户的 token 过期无人管——单机/
   双主规模可接受，登记不阻塞。**GitHub App 路径**：本集成走 OAuth
   App+PAT，不适用"JWT 换 installation token 幂等刷新"模式，PAT 保持
   "显式配置、手动轮换、不入自动刷新循环"（forge 同款语义）——写进
   契约前置约束，防止后续把 PAT 卷进自动刷新。

**前置事实 7-11（2026-11-07 业界对标四次扩项：凭据解析/存储/迁移模式）**：

7. **优先级语义的业界信号与分层答案**：open-swe/Backstage 显示"用户触发
   操作中用户级凭据优先（保留归属/attribution），bot 仅纯 bot 模式兜底"
   ——与我们**路径 B 现状一致**（归属者 User token 第一公民），无需改。
   分歧仅在**路径 A 场景的 UserToken 级内部**：D10 拍板"PAT 优先（显式
   意图）"、实现是 OAuth 优先。业界无单一共识，属合法取舍点——契约
   拍板时论证理由（OAuth scope 动态性 vs 代码路径短），坚持 OAuth 优先
   必须写明依据；同时补命中级别审计（TokenResult.level 已在，操作日志
   落 level 即可）。
8. **resolve 签名与 actor（接口级决策，三选一拍板）**：现实现
   actor=host_user_id（从 repo_key 派生）——与 open-swe 的
   "(thread_id, principal) 感知操作者"模式不同但自洽：它是 agent 运行时
  （每请求有明确用户上下文），我们是托管服务（仓库有稳定归属者），
   **open-swe 模式不直接适用，迁移需论证**。三选一：a) 维持归属者语义
  （签名不动）+ 触发者审计走操作日志；b) resolve(repo_key, actor) 扩展；
   c) 混合（webhook=归属者、任务=触发者）。现状=满足共享仓库正确性
  （统一归属者 token），审计可见性缺口已由 D91 告警 + level 字段部分
   覆盖。
9. **存储分层（业界收敛模式，支持"存引用"）**：长期秘密（PAT/私钥）
   存权威表（user_credentials/external_identities，已加密）；短期运行
   凭据（OAuth access token）身份域内存/缓存管理，不持久化到仓库域
   业务表——**grape_t_repo_credentials 只存"授权关系+指针"**（oauth_fill 行存
   external_identity_id 引用非 token 本体）。source 三态定位由此收敛：
   **审计溯源（credential provenance），非运行时解析条件**——
   TokenResolver 不读 source，策略链固定四级（前置 3 排序问题按"固定"
   收敛）。PAT 归属闭合：source=pat 行=指向 user_credentials 的授权
   记录（非秘密副本），无三写一致性问题。
10. **scope 不足显式化（OAuth 2.0 insufficient_scope 规范）**：OAuth
   token scope 不足时不静默回落 PAT——返回显式 insufficient_scope 类
   错误并提示 PAT 可用；fallback 若保留须"显式 opt-in 的最后手段"。
   启动/早期 scope 诊断可做但不成为新故障模式。gitee 五列能力矩阵
   实测输出=每渠道"oauth vs PAT"能力对照表。
11. **迁移下线判据量化（替代模糊"迁移完成"）**：managed_repos 历史列
   下线=双写回填稳定后，"旧列读回落命中计数连续 N 观测周期为零且无
   回滚事件"（N 与周期契约定）；次序=双写→回填→读切换→观测→下线
  （回填不得早于双写生效）。RepoBotToken 读侧回落已有 log.warning
  （观测基础在），补命中计数指标即满足判据采集。401 反应式刷新
  （对标差距 A）同步升格为契约必需项：触发精确匹配 401 状态码、
   强制刷新后单次重试且绕过同一拦截器、刷新失败区分"彻底失效（重新
   登录）"vs"暂时性（可重试）"——与主动预刷新并存构成完整生命周期
  （@doist/cli-core 双模式对齐）。

**优先级与触发条件（D90 时代 →【D100 收口：整体作废】）**：原四触发条件
（PAT-only 平台上线/占比阈值/反馈累积/D10 迭代）指向的"补落库消费端"
已随 upc 废弃失去对象——**新平台接入即自动获得 lazy 授权能力（卡在册
即可用），无任何待补动作**。 Owner/评审节点条目随之作废。

**本批动作（2026-11-07）**：本节登记 + 挂账总清单 #9 + provider_link.py
接线状态标注（声明面非死代码）+ requirements 范围外清单行。**零行为代码
变更**——消费端补齐等契约评审，不抢跑。

**明确不动项**：S116 守卫现行口径不动（本批零登录域仓库域引用变更）；
"用时取用"lazy 链不动（等效方案继续服务）；s66 混合债；另一会话 untracked docs。


| D102-N | 实弹验证轮六裁定收口（2026-11-08；用户实测连续抓错 + 三次口径裁定） | ①华为登录全挂：回调兜底门槛读 `channel_type` 键，而渠道卡存 `type: oidc`——兜底死代码（华为 userinfo 404 无兜底可用）；旧 test_d102e 源码字符串断言假绿 ②凭据卡"加载中"：account.js/app.js 四处 `API.get('/api/…')` 双前缀 404 ③第三方账号列表华为显示成 GitCode：_accountPlatformName 只查仓库目录、兜底硬编码 'GitCode' ④华为 logo 形不像：手搓八瓣被驳回 + 图标注册键 hwcloud 与绑定体系键 huawei 错位 ⑤无绑定页面：用户裁定 `_no_vcs_binding` 直达引导页（占位页+按钮驳回），并修 localStorage.bindings 登录快照时序洞 ⑥引导卡与凭据卡职责：绑定卡单路径（PAT 入口移除），凭据页 OAuth/PAT 双模式徽章矩阵（含"未配置+已配 PAT"双徽章并存、清除 PAT 回落 OAuth、平台取消 OAuth 不清绑定记录） | ①providers.is_oidc_channel(type/channel_type 双键)+回调门槛改走它+行为测试（d102e2，源码断言族退役）②四处去前缀+全仓守卫 test_frontend_api_double_prefix ③平台名三级来源（渠道卡→仓库目录→平台 id）+守卫 ④Simple Icons 官方华为 logo（24×24 单 path）+huawei 键归一（repos.js/app.js）+守卫 ⑤引导直达+快照清除（d102c 裁定更新）⑥凭据双模式渲染+_accountClearPat（PUT 空串）+test_credentials_dual_mode 六用例；版本 bump app v229/account v55/repos v66 | test_d102e2 + test_frontend_api_double_prefix + test_account_platform_label + test_huawei_icon_key + test_credentials_dual_mode + test_guide_status_badge + d102c/d102l/d102k 裁定更新 |
| D102-O | git-repo 传输层 issue/PR 编号原生保形（2026-11-08；实弹 Issue #IKTREU：gitee 仓 A2A 作业回写评论/标签/关单全被 422 拦死） | D102-G 只修了流水线进程内 ident 链路（isdigit 门槛删除），HTTP 契约面是另一层：git_repo_api 全部 issue/pulls 路由路径参数声明 `number: int`——FastAPI 路径校验阶段即拒（"IKTREU" 解析不成 int → 422），请求到不了业务代码，字符串 ident 平台的 HTTP 回写面（评论/标签/关单/查详情）整族不可用 | ①16 个路由签名 number: int → str（原生保形；体内仅 f-string 拼 URL 与 str(number) 审计，零 int 消费点实查确认）②milestone number 与 comment_id 保持 int（全平台数字，非本批放开面）③L676 milestone 回读校验 int() 不动 ④客户端层零改动（VcsApiClient/_native_issue_number D41/S61 已原生保形，鸭子类型收 str）⑤router 头部契约注释（勿回改 int） | test_gitrepo_string_ident_contract.py 新增 7 用例（RED 先行实锤 422 → GREEN）+ test_git_repo_api close_issue 契约更新（"5" 保形）+ 既有 77 用例零新增失败（1 失败为干净头可复现的环境性预存，与本批无关） |
| D102-P | 职责分域——第三方账号=OAuth 域 / 仓库凭据=PAT 域（2026-11-08；用户裁定+外部复核 C 情形：读写分离错位） | PAT 与 OAuth token 四层不可同类（来源/存储/生命周期/撤销）却被双写 ei.access_token：撤销/有效性/生命周期三污染 + 第三方账号行职责越界（PAT 归属显示已绑定/解绑） | ①归属行 access_token 恒空+metadata.source=pat 打标 ②has_oauth_token=access_token 非空（语义回归本义）③启动回填精确迁移（解密比对）④repos member/sync 归位 fetch_user_token 双通道 ⑤安全 tab 行 OAuth 域展示（PAT 归属蓝标去凭据管理，无解绑）⑥弹窗上下文分域（安全=仅 OAuth / 引导=双模式） | test_d102p_pat_domain_split 4 用例 + bind/guide/凭据/分域 36+ passed；解绑 OAuth 不连带 PAT | test_d102p_pat_domain_split + test_bind_context + test_d102l_settings_split + test_guide_status_badge + test_security_tab_bind_states |

| D105-2 | 主键 UUID 化第二批——grape_accounts + 引用列放宽 + v7（2026-11-09；用户裁定：一次性清完/VARCHAR(36)/UUIDv7/集群守则入档） | grape_accounts 仍 INTEGER AUTOINCREMENT，user_id 引用 29 表 1342 处；老 int() 强转/lastrowid 与 UUID 语义冲突 | ①迁移 202611090002（safe 幂等：grape_accounts.id→TEXT PK 保值 + 29 表 user_id 族列 MySQL→VARCHAR(36)），执行收敛到 UuidPkMigration 声明式迁移器（双后端/三态幂等/集群 GET_LOCK 语义继承，uuid_pk_migration.py）②调用方一次性清扫 63 文件：user_id 族签名 int→str、15 强转清零、lastrowid×3 退役、JWT sub 三入口 str 化、engine_select 取模改 crc32 ③主键生成统一切 UUIDv7（id_gen.uuid7_str，stdlib 优先/uuid4 兜底；仅 DB 主键，session/trace id 保持 v4）④集群守则入档（滚动窗口/锁写/操作二选一，设计稿第八节） | 定向 65-70 passed（含行为锁 6 用例）；:3000 验证实例已迁移盖章+重启 health 200，老账号 '1'/'2'… 零丢失 | test_d105_batch2_account_uuid_pk（6）+ test_d105_uuid_pk_migration（4） |
| D105-3 | 全链路扫雷收口三连（2026-11-09；用户裁定"我们的就要修复"+"要自动不要手动"+实弹 404/500 回归驱动） | 三路并行扫雷：①后端 int 消费点第四波（host_user_id 路径B判定全灭/admin 建号必炸/patrol 归属全丢/local_claimer）②SQL 语义层（第二批 sed 误伤的 SQL 语法错误、grape_accounts.id>0 数字比较残留 3 处 MySQL 恒假、resolve_user_id_by_platform_login 恒返 0）③前端+契约（admin.js 10 处 onclick 数字注入 UUID 即 ReferenceError、organizations.js 同族、webhook A2A fallback/vod MasterRun int 强转） | 每路命中即修+豁免清单判定（GitLab 上游 id/admin 域/非 user id 列维持 int），行为锁跟进；另补 UuidPkMigration 重建前自动备份（部署零人工最后一块） | 定向：D105 锁 36 passed、api_routes+grape_auth 174 passed/2 既有、admin.js node --check 过；宽回归残余全部归因（401 鉴权族已修 99→2、405 族已修 16→0、余为顺序污染/既有） | test_d105_batch2_account_uuid_pk + test_d105_uuid_pk_migration + test_d105_rowid_alias_uid_pk + test_tasks_lifecycle + 4a9da77a1/1a4c483aa/99483fb96 各自新增断言 |

| D105-4 | 实弹归属校验跨类型收口 + 测试欠账清零 + 部署自动化备份（2026-11-09；用户裁定"我们的就要修复/部署零人工/squash 单提交"） | 存量库 INTEGER 亲和列读回 int 与鉴权 str 混型：凭据页 500（get_user_token_for <=0 比较残留）、会话 404（chat 归属 7 处/feishu_onboard 2 处/settings 6 处/grape_auth 2 处）；api_routes 401/405 族测试欠账；UuidPkMigration 无自动备份 | ①归属比较 str() 双侧归一（同族 17 处清零）②tasks_lifecycle 路由迁移跟进+api_routes 裸 INSERT/lastrowid 跟进（401 族 99→0）③基线对照清零最后 2 例既有失败 ④重建前自动备份入 UuidPkMigration ⑤S22 守卫 UUID 跟进+调试残留清理 ⑥前端/SQL/worker 三路并行扫雷收口 | api_routes 132 passed 全绿；tasks_lifecycle 17 passed；行为锁含跨类型归属锁 | test_d105_batch2_account_uuid_pk（7，含跨类型归属锁） |

| D105-5 | issue ident 族全链路 + MySQL P0 + 复审采纳（2026-11-09；实弹 IKTVZQ 建需求流水线不触发驱动） | gitee 字符串 ident 建需求后 create_task 同步触发 int(issue_num) 炸（D102-O 漏网点）；三路扫雷再收后端/SQL/前端三面；MySQL 实弹暴露 UuidPkMigration 探针不带引号 P0（information_schema 1054 → 202611090002 永不盖章 → 32 表 INT → 写 UUID 全 500）；复审再收 ExternalIdentityModel.id ORM 漂移/预渲染按钮污染/TEMP-DEBUG 残留/backfill 解密顺序/守卫版本漂移 | ①number/iid 原生保形直传+结构守卫（int(issue_num) 归零）②SQL 层 0004 迁移：issue_number 混型归一 TEXT（DDL 原文改造重建，修三实现缺陷）、ORDER BY LENGTH 双键、二级排序键 id 补齐 5 处 ③前端 chat.js/observability.js ident 保形+版本 bump ④MySQL 探针加引号 ⑤ORM/DTO str 对齐、品牌按钮 provider 化、TEMP-DEBUG 删除、backfill 先 decrypt、守卫版本同步 | 守卫+行为锁：test_string_ident_pipeline_trigger_guard（含裸 ORDER BY 守卫）、test_issue_number_text_migration 8 用例、test_d105_batch2 7 用例（含跨类型归属锁）；api_routes 132 全绿；MySQL 实弹复验待跑（head 8f4992900+） | test_string_ident_pipeline_trigger_guard + test_issue_number_text_migration + test_d105_batch2_account_uuid_pk |

| D105-6 | 账号域 user_id 外键列 TEXT 归一（2026-11-09；用户实弹绑定转移秒 410 + 裁定"从根上解决"） | D105-2 切 UUIDv7 后 30 表 31 列 *user_id 外键仍 INTEGER：binding_tickets.initiating_user_id 存 int 24 vs 会话 '24' str——Python 裸比较恒 mismatch 本人票据秒 410（uv3000.log 实锤）；uuid 用户写入 SQLite 出混型互查不中、MySQL INT 列严格模式写崩 | ①迁移 202611091000（safe 幂等）：枚举 30 表 31 列复用 _rebuild_issue_text_columns 手法 TEXT 重建保值 ②db_setup/user-db/persistence 新装 DDL 声明同步 TEXT ③Python 层比较边界 str() 双侧归一（validate_binding_ticket 先行 fff452153 + 全族清扫）④int()/isdigit 数值假设与 ORDER BY id 数值依赖点清扫 ⑤新增场景用例 UC57-UC59 + 设计分片 d105b-account-fk-user-id-text-design.md | transfer-bind 全套 35 passed（3 个 SQLText 基线失败双侧 diff 排除）+ uuid-id 转移链回归；:3000/:3001 重启后用户 PAT 绑定转移确认实弹通过 | test_transfer_bind + test_issue_376_mysql_transfer_bind + test_account_fk_user_id_text_migration + test_issue_number_text_migration |

| D105-7 | 机制固化——不变式守卫 + 新用户 e2e（2026-11-09；四路深扫后用户裁定「防复发，加表必红」） | 四路深扫（前端/SQL/MySQL/残留面）收口后的系统性面：账号域外键 TEXT 化目前只靠 D105b 枚举与人工同步——**新表/新列引用 user id 声明 INTEGER 无任何自动拦截**；新用户 uuid7 全链（注册→票据→凭据审计）缺最小闭环回归；残留 2 列（grape_t_patrol_findings.owner、issue.release_approved_by，含 0=unowned 整型哨兵）仍 INTEGER 声明未清 | ①新增不变式守卫 test_schema_fk_type_invariant：清单 import db_setup._ACCOUNT_FK_USER_ID_COLS（不硬拷贝）+ D105-7 追补散列，断言声明全 TEXT、uuid/数字串双探针写入后 typeof 恒 text 无混型——**以后加表漏同步清单/声明必红**；残留 2 列以 _KNOWN_INTEGER_SENTINEL 登记表显式在册（修一列删一项自动落入强断言面，清零后退役）②新增 e2e test_new_user_uuid_lifecycle：uuid7 注册→save/validate(consume=False/True)/consume 票据全链→跨用户与存量 '24' 混型隔离→create_external_identity→credential_usage_log 审计落库→触达外键列 typeof 终检，零异常零 410；OAuth HTTP 握手不可行处不 mock，以 DB 直调等价覆盖并在文件尾注明 ③文档收口：d105b「机制固化」节 + deploy-checklist 注册新账号验证步 + UC60/UC61 入账 | 新增 6 用例两文件全绿（双序验证无顺序污染）；D105 家族 13 用例回归通过 | test_schema_fk_type_invariant + test_new_user_uuid_lifecycle |

| D105-8 | MySQL 演练 + 部署门禁自动化（2026-11-09；用户裁定"现网 MySQL 实存→门槛自动化 + grape-drill-db（自建演练库名，见部署配置） 本地演练 + 全修"） | 现网 MySQL 实存确认（有历史流量）；启动迁移链 SQLite-only 无法在 MySQL 跑；新装库 1101（TEXT DEFAULT）/50 列漏建/0004 漏 parent 列等仅 MySQL 演练可见 | ①G1 迁移链 MySQL 分支补全（typeof/sqlite_master/PRAGMA 三类处置 + legacy 首跑上下文修 50 列漏建）②G2 门禁脚本 backup/shadow/apply/smoke 四阶段 + fail-closed ③G3 run_migrations_for_mysql 连接注入（_mysql_target_swap 换绑全局连接 + env 还原）④G4 翻译层 TEXT 字面量 DEFAULT 剥除（7 表建表失败根修）⑤演练驱动修复：0004 补 parent/ident widen、202611091100/202611091200、重建正则引号兼容 ⑥grape-drill-db（自建演练库名，见部署配置） 四轮递进演练（1101→SQLite 风格 DDL→行级清扫→零失败） | 影子演练全绿（81 表对账/id 保值/引用列全 VARCHAR/幂等 0 pending/零建表失败）；门禁单测 18 passed + 真机 4 passed + SQLite 迁移族全绿 | test_deploy_mysql_gate + test_mysql_gate_migration_chain + test_mysql_dialect_compat + test_issue_number_text_migration |

| D105-9 | 真实 dev 库演练验收 + 演练用例代码化（2026-11-09；用户裁定"基于 dev 库建 grape-drill-db（自建演练库名，见部署配置） 本地演练"+提供 root 口令） | D105-8 的演练源是本机残留库（形态对数据老）；真实 dev 数据（678MB/40 账号/123 表）未验 | ①59 RDS grape-drill-db（自建演练库名，见部署配置） 建库+恢复+全链迁移：零失败零 1064、40 账号 id 保值、盖章 31 条、二次 0 pending ②演练验收代码化：GRAPE_GATE_REAL_DATA=1 门控用例（行数保值/str 形态/INT 残留清扫），一条命令可重复 ③挂账：login_whitelist 'NULL' 字面量 1062 索引告警 | 59 真机 pytest 5 passed（含真实数据演练用例）| test_mysql_gate_migration_chain（GRAPE_GATE_REAL_DATA=1） |
| D105-10 | 平台接入验收机制（扫雷⑦建议落地；批B3 收口） | 「配卡即接入」链路（卡注册/webhook/工厂/凭据/bot 五面）无端到端验收锁，接入面回归（退回硬编码/兜底）不可检 | 批A/B 两批修复汇总一并记账：后端 5 处硬编码白名单改卡驱动 catalog（e15eb）+ 前端 6 页平台目录加载（f96b5 等）+ 测试 3 红清零 + 中危四项（query_token 401 坑/PRIVATE-TOKEN 头/form 编码/TEXT-DEFAULT 同族审查）+ 本批验收机制：test_platform_onboarding_acceptance 8 用例（七断言+无卡不接入反向锁，tmp 卡注册手法=monkeypatch _CONFIGS_DIR+缓存失效，进程内零外网；资源 API 面门槛显性化为预期失败断言③，补 providers/<name>.py 后改绿） | 新建验收用例集 8 passed + 相邻平台族回归 60 passed 零污染 | test_platform_onboarding_acceptance（UC65）→ d105e-platform-onboarding-acceptance-design.md |

| D105-11 | 台阶3 资源 API 翻译卡驱动化（2026-11-09；用户裁定台阶3 立即做+五条收紧采纳：字段级缺省合并/双轨逐字段等价断言/③翻转保留负向/守卫路径模板规则精确化/报错三要素） | "零代码接入"最后防线 providers/<name>.py——纯配卡新平台 translate_inbound ValueError | ①PlatformCard.resources 段（repo_segment/path_map/query_state/rules；字段级缺省合并 github 形态；新资源类型不声明即 ValueError）②消费顺序卡优先→模块回退（TODO 弃用条件=四平台卡迁完）③card_module.py 垫片（URL/request/token 面纯配卡落通用链）④git_repo_api 四处消费点改道、_is_supported_provider=模块∪卡 ⑤守卫：段内声明子结构缺必填/路径模板无占位符才拒（静态路径不误伤），报错含卡 ID+字段路径+期望格式 | 双轨等价逐字段对比四平台全绿；验收③翻正（正向结构断言+负向保留）；家族守卫 74 passed | test_s3_card_driven_resources + test_platform_onboarding_acceptance + test_platform_card_matrix + test_git_provider + test_vcs_dialect_backend |

| D105-12 | 台阶1+2 验收矩阵 CI 常驻化 + 品牌资产进卡 + 侧栏分组修复（2026-11-09；用户裁定台阶1/2 立即做） | 配卡验收仅接入时手动跑一次（卡成活配置后漂移不可见）；品牌色/平台名三处静态表漂移（#F54A36 vs #DA203E）；工作区侧栏"非 github: 归 GitCode"二元分组致 gitee 仓错组（用户实弹发现） | ①test_platform_card_matrix 参数化遍历全卡六断言（新卡自动进矩阵；无模块平台资源 API 门槛显性化）②PlatformCard.brand_color（#RRGGBB 校验）+ 目录接口透出 + 四卡补色 + 前端三处收敛单源（目录优先/静态表兜底/原地刷 fill）③initSidebarRepos 按真实前缀分组、组名查目录 | 矩阵 25 用例全绿；总守卫 48/96 passed；浏览器实测三组并列（GitCode/Gitee/GitHub） | test_platform_card_matrix + 浏览器实测（fabdb3cb9）|

| D105-13 | 三路安全/稳定性深扫：authz 越权 + XSS 注入面 + except/竞态家族（2026-11-09；用户裁定"三批连打"） | ①authz：require_no_session_token 被误当权限墙——project-info 任读他仓知识库/runtime-credentials revoke 无 owner/engine 配置任改/runtime/admin/* 名不副实/task-logs 泄 prompt+IP（5 高危+11 中危）②XSS：全站 500+ 字符串拼 HTML 点转义覆盖 ~25%，9 个行为不一的 escape 函数，chat.js [chat_img:url] onclick 二次注入可实弹利用，issue 标题等自由文本即触发型存储 XSS ③稳定性：静默 except 1092 处/251 文件无 ratchet；async 内同步 run_pipeline（3s sleep）/subprocess 阻塞事件循环；三处无锁缓存竞态；双写残留经查干净 | 三批连打：批S1（authz 五高危 + chat.js XSS）；批S2（统一 escHtml + 高危清单逐点 + onclick data-* 改造 + skills 漏单引号补）；批S3（except ratchet 基线 1092 + issues_center/agent 异步解锁 + 三缓存补锁）；低危（平台命名隐性防御、P3 系统值转义）随批次铺开 | 进行中（三批子 agent 并行） | 详见扫雷报告与各批 commit |

| D105-13 续（批S2 落地） | 8c095db44：escHtml 单源收敛（9 helper 代理，补 skills/observability 漏引号）+ 高危清单 33 点全转义 + onclick 编码式收口 6 处 + 守卫测试（RED 实测） | 19 文件 / 78 passed / 基线 7 失败双侧 diff 排除 |

| D105-15 | org 域 id TEXT 化第三批 + 两个 1062 尾巴清偿（2026-11-09；用户裁定"都做"） | org 域仍 INTEGER（活病灶：_migrate_create_personal_orgs 把 TEXT uuid user id 写 organization.owner_id INT——SQLite 混型互查不中/MySQL 1366 或截 0）；挂账 'NULL' 1062 的清洗点在另一索引创建路径之后；gate 盖章断言只有 >=15 下限测不出新增迁移漏执行（59 实弹：gate 全绿但 202611091300 零盖章） | ①迁移 202611091300（safe 幂等）：organization.id/owner_id + org_member/org_invitation org_id/invited_by/inviter_id + 13 核心表 org_id → TEXT/VARCHAR(36)，int 保值、哨兵 0→''、名单预警日志 ②哨兵全链清扫 104 处（''=未归属，MySQL 下 TEXT 列 =0 数值强转误匹配故必须字面量）③'NULL' 清洗前置到 idx_login_whitelist_github 创建点；实弹发现该索引在 MySQL 端被兼容层降级为全列唯一（WHERE 丢弃），44 行合法空串必撞——MySQL 跳过此索引（生成列 uq_*_lower 已正确覆盖），SQLite 部分索引保留 ④gate 补盲区：盖章 ⊇ 注册链全覆盖断言 + 第三批代表列进 varchar 断言 | 守卫 8 passed；59 grape-drill-db（自建演练库名，见部署配置） 实跑：202611091300 盖章、org 域全 varchar、id 保值（'1'/'24'）、数值 org_id 列清零、whitelist 1062 消失；gate 5 passed；定向回归失败集与基线逐条一致零新增 | test_d105_org_domain_text_migration + test_mysql_gate_migration_chain + test_login_whitelist_null_literal_cleanup；设计分片 d105j |
| D105-14 | 平台硬编码棘轮扩维 + 模块回退审计 + 卡规则模板插值（设计方案，2026-11-09；外部评审对照业界实践采纳三项，v3 语义终稿经用户确认；**仅方案入册，代码未实施**） | ①单平台字面量比较分叉无守卫（现有二元 ratchet 不覆盖 `provider == "<p>"` 形态；repo_manager 34 处 gitcode 属 D105-3 豁免在册，缺防新增拦截）②module_for 落 _MODULES 回退零观测（"哪些平台依赖代码模块"不可追踪）③transform 回调是纯 JSON 卡唯一硬墙（拼接类语义必须写模块） | A：test_platform_whitelist_ratchet 扩 provider 直接比较扫描（BASELINE 冻结、扫描面诚实声明，字典/动态分派不在面）B：module_for 落模块时经 log_usage 记 module_fallback_<provider>（credential_kind=integration_path、每 provider 进程内去重、executor_context 带调用点、失败不阻断）C：卡规则第四形态 template（trigger/mode 显式解耦、mode=replace/append/prepend、占位符构建期全量校验∈中性字段集、缺失 strict 抛错不保留字面量、同 field 与 values/drop 互斥跨字段自由）| 验证：棘轮 RED 注入复验 + 模块审计 3 断言 + 模板 8 用例 + 家族回归全绿（实施后补记） | d105i-ratchet-module-audit-template-design.md；明确不做：SPI 核心层/JSONata/strict 开关（决策留档 §5） |

| D105-16 | 绑定域解耦收口：PAT/OAuth 写入路径+供给+存量搬家（2026-11-09；uid=24 gitcode"已绑定 来源：PAT"+解绑误显示，用户三轮纠偏定稿方案 A 无标记版） | ①专用字段路径仍把 PAT 写进 ei.access_token（供给链 get_user_token 直读 ei，退役即断供）②回填首档 json.loads NameError 被吞从未生效③ORM 引擎 URL 未编码凭据：密码含 URL 保留字符（@ 等）→ 全部 _uow 身份域写入现网 MySQL 连不上（59 实锤 P1）④前端 bound 与渠道配置态脱节（"未配置"角标+"已绑定+解绑"同屏）| ①专用字段路径改 v6（PAT 只进凭据域，ei 归属行 token 空；不 clear OAuth token——OAuth+PAT 共存回落能力锁）②get_user_token_for 加 ①凭据域 PAT 层（消费方零迁移自动获得 PAT 优先）③backfill 搬家语义：等值副本→清 ei／OAuth 痕迹→保留／渠道未配置（client_id 实配判据）→搬家／歧义→保守保留；落写全局连接裸 SQL；json.loads NameError 修复④安全 tab 纯 OAuth 三态（PAT 退出本行，D105k 简化模型）⑤engine URL quote_plus 编码⑥_str NameError 修复⑦清除 PAT 级联四象限（_cascade_pat_clear）：无 OAuth→平台级解绑（ei 归属行+凭据+user_repos 全清，修复'清 PAT 后仓库列表残留'实弹）；OAuth 共存→只清 PAT 供给②接管（回落能力锁） | 行为锁 9+安全页守卫 7 全绿；59 真机渠道在配零误搬家；本地 verify 库 uid=24 gitcode/github 实搬家（cred 落 <p>_pat、ei 清、供给 ① 层命中）| test_d105k_bind_provenance + test_security_tab_bind_states + test_uc38；设计分片 d105k |

| D105-16 续（D105-14 实施） | d105i 三改动点落地：A 棘轮扩维（provider 单平台字面量比较守卫，_COMPARE_BASELINE 8 点冻结，RED 注入复验）+ B 模块回退审计（module_fallback_<p> 每 provider 进程内去重，executor_context 取 sys frame 调用方零侵入）+ C 卡规则模板插值第四形态（trigger/mode 显式解耦、占位符构建期全量校验∈中性字段集∪_COMMON_TRIGGER_FIELDS{number}、strict 运行时缺数据抛错；apply_rules 前置 pass + _build_resources 构建期校验）| 实施（2026-11-09）| 行为锁 12 全绿（test_module_fallback_audit 3 + 模板 8 + 棘轮 RED 复验）+ 家族回归 89 绿 + silent_except 基线审阅上调（db_setup+3/grape_account_db+1，逐点理由入 EXEMPT 注释）| d105i 设计分片 |

| D105-16 续二（评审五差距收口） | 外部五差距分析裁定 3.5/5 采纳：差距一 capabilities 显式声明（_VALID_CAPABILITIES 枚举 fail-closed + card_capabilities/require_capability 查询与消费侧拒绝，未声明卡零影响）+ 差距二剩余覆盖一致性（声明的 <r>.<o> 必须有 rules 键或 _MODULES 承载，构建期红）+ 差距五通用链职责边界清单（d105i §8.2，六项禁止归位）；差距二主主张已存在（占位符来源校验 D105-14 已落地，实查 platform_card.py:195-204）不采纳；差距四回退熔断前提错位不采纳（存量四平台走模块是设计主路径，熔断对象应为平台 API 失败率，挂起）| 实施（2026-11-09）| test_capabilities_declaration 8 绿 + matrix/s3/onboarding 回归 60 绿 | test_capabilities_declaration + d105i §8 |

| 挂账注销 | 台阶4 接入向导（前端 UI 化）——用户 2026-11-09 裁定不做：接入流程已由卡驱动+矩阵验证闭环，向导 UI 价值不抵维护成本 | 注销 |

| D105-17 | repo_manager 存量治理：fail-closed + 卡驱动 + 调用方迁移（2026-11-09；用户裁定未知平台 fail-closed 废除 gitcode 静默兜底） | repo_manager 80 处平台字面量（host/bot username/模板源静默回退 gitcode；github webhook 函数指针 if 分叉）；get_user_token 族消费方直读 | 批次A src 治理（_host_of_url 空串不回退+push 前置守卫、resolve_bot_username ValueError、get_hosting_provider_config 卡驱动列名派生、push_template 目标卡 fail-closed 前置、调用方死导入清理）；批次B 行为锁 4+BASELINE 摘除 2；主会话收口：未知模板源 ValueError、workflows/webhook 两处 github 专属比较审阅保留登记（dialect 无法区分）、s88 测试翻转、user_repos 残留修复 | ratchet 比较点 8→2（剩余为 github 实现绑定合理保留）；行为锁 5 绿；家族失败集 diff 零新增 | test_repo_manager_governance + test_platform_whitelist_ratchet + test_s88 + d105l 设计分片 |

| D105-18 | 凭据级 workflows 写能力三态（2026-11-09；D105-17 审阅保留两处 github 专属比较之一升级收窄；gh-aw ADR-0002 显式声明制） | 模板 push 的 .github/workflows 删除怪癖以平台名判定（github 删/gitcode 不删），平台卡无对应能力位（dialect 无法区分）；GitHub token 权限模型 requested ∩ granted，运行时不可内省 | ①token_resolver.credential_workflows_write 三态查询（True/False/None，读凭据域 <p>_pat 的 workflows_write 键）②push_template_to_repo 判定改凭据能力三态、未声明回退平台名旧逻辑（github 删/gitcode 不删零回归），加 user_id 参 ③settings 泛型 platform_tokens 值支持对象形态 {token, workflows_write}（字符串形态向后兼容=未声明，显式 False 也落库），get_credentials 存在性响应回显该键 ④templates.py 补传 user_id（缺则能力恒 None=死代码） | 行为锁 6 绿（三态单元+四态 push 分流+OAuth 保守删）+ 场景用例 4 绿（声明落库回显/受限落库/旧前端零隐式/github 声明 true 端到端不删）；家族回归失败集 diff 零新增 | test_d105m_workflows_capability + test_s103_workflows_capability_scenarios；设计分片 d105m-workflows-capability-design.md |

| D105-18 续 | 续审九轮收口（审计 F1-F6 全实锤；2026-11-09） | F1 棘轮基线失配（D105-18 改三元式判定未同步 _COMPARE_BASELINE，双 RED，第 5 次守卫随行失守）；F2 模板源平台 raise 后 log.warning 死代码且注释与分支语义矛盾；F3 backfill 守卫 pin at != tok 落后于 strip 归一实码；F4 _resolve_issue_org_id explicit or 0 哨兵残留 + repo 路径 '0' 未归一；F5 security-tab docstring 谎报 token_source 字段（实码无此字段，判据=bool(ei.access_token)）；F6 台账 D105-17 行为锁计数 4≠实有 5 | ①基线改双键（三元式 ×1+webhook if ×1）摘除消失点 ②死代码删除+warning 移入缺失字段分支 ③守卫更新 strip 归一形态 ④哨兵统一 ''（repo 路径 0/'0' 归一、explicit 透传不 str 化）+S104 场景锁 5 用例 ⑤docstring 纠正 has_oauth_token 真实口径 ⑥台账计数更正 | 审计失败表 6 例中 4 例转绿（F1×2+F3+F4），余 2 例=预存悬空引用按归因表保留；定向集 9 文件 47 passed/2 failed；test_s104 5 绿 | test_platform_whitelist_ratchet + test_s104_issue_org_sentinel + test_d102p_pat_domain_split::test_backfill_semantics_precise_match + test_issue_org_attribution::TestResolveIssueOrgId；设计分片 d105n-audit-round9-closure-design.md |

| D105-18 续二 | 续审十轮收口（round-10 审计：四问全成立、F1-F6 六/六验真；2026-11-09） | ①遗留 RED 最后一项：TestGetGitcodeCredByGitcodeId 2 用例引用 deps._get_gitcode_credential_by_gitcode_id——D3 有意删除的兜底（删除前生产库出报告留痕），悬空连续 4 轮；②场景文件命名债：test_s94_workflows_capability_scenarios.py 与登记编号 S103 错位（S94 被 observability 占用） | ①2 用例转退役锁 test_dangling_fallback_retired（断言函数不得回潮，rationale 写明 D3 出处；现役供给链由 get_user_token_with_refresh 用例族覆盖）②文件改名 test_s103_*，5 处文档引用同步 | 分支 RED 归零（audit 归因表 2 例转绿）；实跑 test_issue_org_attribution+S103 场景全绿（372 文件 2 失败=台账在册测试基建专项预存族，非本批范围） | test_issue_org_attribution::TestGetGitcodeCredByGitcodeId + test_s103_workflows_capability_scenarios；收口记录见 d105n §续审十轮 |

| D105-18 续三 | 续审十一轮收口（round-11 审计：四问全成立、56/56 首次全绿；仅 2 个 P4；2026-11-09） | F1 test_s103_* 文件改名后自我描述未同步（docstring 三处 S94+NS 前缀 s94wf+4 个用例名 test_s94a-d，比审计报告指出的两行范围大）；F2 S104-e explicit_org=0 断言用 in ("",0,"0") 宽容形态——实码 or "" 恒返 ""，宽容断言即掩盖信号风格 | ①自我描述全量对齐 S103（docstring/NS 前缀 s103wf/用例名 test_s103a-d）②断言收紧 ==""（无宽容形态） | 15 passed 实跑（S103 场景 4+S104 哨兵 5+D105m 行为锁 6）；全测试面零 RED | test_s103_workflows_capability_scenarios + test_s104_issue_org_sentinel；收口记录见 d105n §续审十一轮 |
| D106 | 凭据存储终态化第一批（F4b 在 r3 集成树重实现，2026-11-11；D90-N6 八条拍板语义落地①②③④⑤；评审新增渐进置空/meta 不误伤/契约文档化并入） | S82 后「权威在 rc、幽灵列还在」双轨在 r3 树同样存在：冷启动仍自动补 gitee/gitlab 四列、ORM 仍声明、托管写路径仍列名键；另修本树实测实锤——`update_managed_repo_bot_token` provider 硬编码 gitcode/github（gitee 仓错路由）、`_rebuild_webhook` 族等 5 处基线红 | ①卡片显式空串语义（`_explicit_or_default`：显式 `""`=无物理列，键缺席才派生）+ gitee/gitlab 卡置空 + gitlab 代码兜底卡置空 + ORM 撤四列 + `_ensure_platform_storage_columns` 终态跳过；②写路径 rc 单写：`repo_cred_write_keys` 归一逻辑键、`_update_repo_bot_token` 卡列空分支 rc 单写（bot+webhook，wt=bot 对齐列语义）+ 基线列更新、`clear_repo_bot_token` db 层统一清除（晚绑定 db）、关联仓检测/路径 B 查询 rc 化、health verify rc 优先、`update_managed_repo_bot_token` provider 改 parse_repo_key 解析；③渐进收敛：写 rc 成功后同一事务置空对应平台列（只置空本次写过种类；SQL 精确限定 UPDATE managed_repos，meta 同名全局键 gitcode_webhook_token 严禁误伤）；④存量回填迁移 202611110001 backfill-gitee-gitlab-token-cols-to-rc（幂等只填空，密文/明文解密→加密归一，列存在守卫，不 DROP）；⑤rc 契约文档化（表注释+configs/platforms/SCHEMA.md）；⑥legacy 回落显式标记+独立消费审计 repo_bot_hit_legacy_column+退出 checklist | test_s128 9 用例全绿（冷启动子进程无四列/卡终态/ORM 撤列/rc 单写+upsert/回填+置空+resolver 仅凭 rc/不误清未写种类/meta 不误伤/_update_repo_bot_token 终态）；L1 批 156/0；凭据关联宽批（59 文件）双侧失败集 diff：修复 10 红转绿（s58×4/s63×2/s64×3/372_r14 webhook 写入审计）、零新增红（372_r14/r8 审计族 11 红、rebuild_webhook 3 红、s63_swap/s63_guard 2 红与基线同红=预存记档）；陈旧源码扫描锚点 2 处对齐卡驱动形态（372_r14 R14.1/round8 T24-01） | test_s128_gitee_gitlab_token_col_retirement + d106-cred-storage-terminal-batch1-design.md + configs/platforms/SCHEMA.md + d90-n6-repo-credential-finalization-design.md（§8 验收实名） |

| D107-A | 代理层 v5 上游契约适配（2026-11-11 部署实弹验证收口；UC80/FR-28 / d107-proxy-upstream-contract-adaptation-design.md） | ①gitee issues update 对 IK* ident 恒 404 "project or enterprise"（form/JSON 双体均 404）曾被误判为"平台限制"——实测对照实锤是集成路径错误：官方契约是 PATCH /repos/{owner}/issues/{number}（路径无 repo 段，body 必带 repo），repo 路径形态为集成自造（同案 imjoey/ge PR !45）；gitcode 恰好宽容侥幸工作，统一收敛官方契约 ②gitee body.repo 必须纯仓库名（发 owner/repo 全称 404 "Not Found Project"，同 token 同仓对照；gitcode 两种均收保持全称） ③gitcode labels 创建 JSON body 400 "Required request parameter 'name' is not present"（Spring form 形态）→ 仅 labels 端点 form 编码（其余写端点 JSON 不变，传输层测试 monkeypatch 契约保持） ④gitcode labels 色值必须带 #（GitHub 相反，Issue #580 口径失效） ⑤gitcode milestones due_on 只认 YYYY-MM-DD（ISO 时间戳 400） ⑥PR comment id gitcode/gitee 为 32 位 hex（github 数字）：int 声明对 hex 422 → str 放宽；delete 端点 hex 直传 400 "Orchestration error" → 先 hex→note_id 解析（#623 解析器复用） | 机制：provider 契约开关（LABEL_COLOR_PREFIX_HASH/MILESTONE_DUE_DATE_ONLY/ISSUE_UPDATE_OWNER_PATH，src/vcs/providers/gitcode.py、gitee.py），代理层 getattr 派生零平台枚举 | commit 50b92de31（comment_id str 放宽）+770b063bf（hex→note_id delete 解析）+590aa8a22（owner-path 官方契约+labels form 收窄+_req 延迟导入）+568ef2f0b（gitee 纯仓库名）；行为锁 test_proxy_contract_adaptations.py 13 用例隔离跑全绿 |
| D107-B | 迁移版本撞号自愈（2026-11-11 部署实弹验证；UC81） | dev 生产线台账与本链 db_setup 六处同版本号异名迁移：迁移器按版本号跳过，grape_t_repo_credentials/branch_guard_state/grape_t_system_credentials 三表建表被静默跳过——已盖章库（grape_dev_mgmt 派生测试库）三表缺失，rc 凭据链路整族隐性断供 | 三张表 CREATE 幂等重放于新版本号 202611110002（撞号已盖章库二次启动即补齐；幂等：已存在表不重写、数据保值；冷启动新装 36 条迁移链完整性保持） | commit c2ebe30fa；dev 派生测试库实弹复验通过 |
| D107-C | D105 int vs str 回归族——实弹归属校验跨类型残卷（2026-11-11；B1 批次） | MySQL 读回 int 行与 user["id"] str 裸比较恒 403/404：issues.py start/status 归属判断 + chat.py 七处会话归属（D105-4 收口后部署实弹再现的残存点） | 归属比较前 str() 双侧归一（同 D105-4 手法） | commit 1fc1397f0（issues）+6a0355e30（chat）；全部七处已修复入链，实弹复验：建会话→发消息→GET messages 全链 200 |
| D107-D | github bot token 供给缺口——创建需求 github 分支不回落用户 token（2026-11-11；B2 批次） | 建需求分支链路取 github token 只查全局 bot，缺失即失败，未回落用户 token（路径 B 语义缺口） | bot 缺失回落 get_user_token_for_repo（repo_key 前缀派生 provider）+ 建单入口裸名统一解析真前缀（裸名此前恒走 gitcode 链架空回落） | commit 194397b12（B2 搬迁）+ 0c7fd7d7c 前缀分流补口（实弹：managed 仓建单 200，issue #4 VCS 同步） |
| D108-A | 登录渠道配置解耦与分层（2026-11-13；登录渠道配置批） | ①渠道卡硬编码在 providers._DEFAULT_PROVIDERS，且文件内重复定义两份（后者覆盖前者，只改第一份无效）②oauth-providers.example.json 只含 gitcode/github，gitee/华为无样例 ③迁移语义是整块覆盖 oauth_providers_config（建文件重启会抹掉 DB 渠道卡）④configs/oauth-providers.json 被 .gitignore 忽略（真源化后新部署无渠道卡）⑤Gitee 每次弹授权页根因=scope 含非法 emails（Gitee 跳过授权页按请求 scope 与上次授权范围一致判定，官方文档原文）⑥华为无渠道卡 → /api/auth/login/huawei 400 不支持的平台 | 卡迁 configs/oauth-providers.json 真源 + meta 逐键覆盖 + fail-closed（不放回代码兜底）；删代码字面量；管理面保存基线改文件卡；.gitignore 移除忽略项；example 补 _readme/_modes/_note 说明（下划线=文档键约定）；分层守卫 11 条（OAuth 卡含托管字段 RED / 平台卡含 OAuth 字段 RED / 文件含凭据 RED / 未知键整卡拒绝 / example 可加载）；Gitee scope=user_info projects、pkce_enabled=false；华为卡（user_url 带 {access_token}、id/login=openID、avatar=headPictureURL、pkce=false） | 本批本地 commit（待推）；定向 43 passed；test_grape_auth 4 条 rate-limit/audit/throttle 为基线红（stash 对照实证）；设计分片 d108-oauth-channel-config-decoupling-design.md；华为 GOpen 返回结构仍待真 token 实弹 |
| D108-B | 登录令牌不回落仓库操作（选项 A，2026-11-13 用户拍板） | 外部评审建议登录/仓库拆两个 OAuth 应用；现状 gitee scope=user_info projects 且 get_user_token_for 第②序回落 ei OAuth 令牌充当仓库令牌（deps.py:560），登录应用实际握有仓库能力；评审「Gitee 规范要求凭据分库存储」一句经核 Gitee 官方 OAuth 文档全文无此表述（无出处） | 单应用 + scope 收窄 + 关回落（与双应用安全性等价、成本是零头）：deps.py 新增 _oauth_repo_token_fallback_enabled（GRAPE_OAUTH_TOKEN_FALLBACK 默认 0）；get_user_token_for PAT 未命中即返回空串、不再借用登录令牌；_legacy_token_resolver 遗留符号同受约束；gitee scope user_info projects→user_info；影响：只配 OAuth 未配 PAT 的用户仓库读变需配 PAT（生产 ei 有 token 仅 gitcode/gitee 各 1 条） | tests/mgmt/test_oauth_token_fallback_gate.py 5 条全绿；分片 §9（决策与实现）/§10（平台托管判定与 huawei 结构性不支持） |
| D109-A | 平台对象模型立模：四类配置 / 六张表 / 用户级平台关系（2026-11-13；D109 第一批设计定稿） | ①platforms/*.json 职责放大（方言 + 凭据 + 身份/展示混装）②无平台一等对象，登录与仓库两集合靠同名隐式关联，缺 grape_t_channel_repo_link / grape_t_user_repo_platform_link ③"是否支持仓库"4 套判据，gitlab=configured:true+hosting:false+login:false 是反例 ④平台可见性=卡在册即全可见（settings.py:2428），无用户维度 ⑤身份令牌可做仓库操作（deps:560 + token_resolver:438 ①PAT②OAuth），D108-B 用 env 开关堵属绕行口 | 四类配置：oauth-providers.json / platforms/*.json / repo-credentials.json（新增：drivers + supports_bot_token + bot_username + supports_webhook_secret + 存储列，禁止出现 token 字段）/ oauth 卡 repo_links；六张表：grape_t_oauth_channels / grape_t_repo_platforms / grape_t_repo_credential_caps / grape_t_channel_repo_link / grape_t_user_repo_platform_link（纯运行时，2026-11-14 改名）；用户关系=**纯对应关系表**（只存 user_id/repo_provider/linked_at/revoked_at/reason；删 via_bits，不存 status/来源渠道/可用性布尔；关系=准入清单，可用性读取时由 credential_resolver 现算：PAT(p) ∨ ∃活跃渠道∧link(c→p)）；第一批带数据回填（PAT + ei 两段，幂等 + 对账）；派生能力视图（login / login_supported 恒等 / configured / hosting 不落表 / credential_drivers / supports_bot_token / supports_webhook_secret / bot_username）；provider 命名规则；orphaned 恢复无视 hash；枚举校验以分片 §13 为唯一来源；扩展纪律：link 存在性校验（显式缺失→error / 缺省缺失→不派生）/ 单方言拆卡 / 过渡期基线+增量棘轮 / 非标准协议需注册策略 / 生效路径统一（运行时读 DB、文件仅导入源、删文件热重载、meta 置 migrated、三类 drifted 均可见）/ 启动扫描按规范化内容 hash 判定文件更新（非 mtime；导入/跳过/orphaned/manual跳过/恢复 + failed 计数 + strict 拒启动）/ 配置文件仅初始化/显式导入有效、运行时只读 DB、集群共享 DB + config_state.epoch 跨实例失效（导入锁+幂等）/ 字段归属裁定（storage 列 + owner_fallback + pat_create_url 迁 rc 单源；legacy_token_symbol + workflows_write 留卡）/ 五键同批删除 + grep 守卫 / bot 账号对象化（grape_t_bot_accounts 一等对象：一平台多账号+is_default；身份 仓库级>默认>池；token 在账号对象加密、webhook secret 独立；取消 bot_identity_mode、删两处代码字典与 meta 键；删两处代码平台字面量、补齐 gitee/gitlab 全局 bot token 缺口） / 场景用例 27 个（§15）+ 影响评估 12 项（§16，4 高等级=启动时序/读路径容错/凭据迁移不可逆/可见性收紧，均上线前置；决策A修正 meta 保留一版可回滚）/ 领域建模=D53持久化层（六聚合+值对象+Repository+UoW，状态计算在领域对象，uuid7_str 主键，ORM 只映射不 create_all，凭据列自动加解密，不新增裸SQL）/ 兼容层删除门=四条件AND（grep清零+迁移标志+发布周期+文件已删，未满足前只读）/ 凭据加密=AES-256-GCM+多kid（credential_keys>JWT_SECRET派生>dev禁）/ 凭据进表=决策A（meta→表加密列，迁完废弃+标志，不并存）/ 无平台级 webhook secret 表（仓库级 rc('webhook') 已权威）/ epoch=表状态版本号（与 config_hash 分工） / 运行时参数=meta+config.py getter 不新建表 / 配置统一=结构统一（对象/入口/命名统一；方言/能力位/端点不统一；三层键集不一致消除；管理面对象式 CRUD+旧扁平兼容一版后删；运行时参数走系统设置域）/ 废弃 global-bot-token.json（全字段进表；webhook_url 改派生不落表；停读 _GPT_PATH + grep 守卫；管理面入参名留别名）/ oauth 客户端凭据入 grape_t_oauth_channels 加密列 / 建表规范=grape_t_ 前缀+UUID 主键+created_at/updated_at+每字段注释（DDL d109-schema.sql 已实建验证）/ 分层纪律=配置派生 vs 运行态分表 + 导入表白名单（grape_t_bot_accounts/grape_t_user_repo_platform_link/凭据域绝不触碰）；配置声明 bot_username(1:1) vs 账号池(实例 N:1)、校验 A 弱校验（冷启动不死结）、视图摘要不含 token / bot 归属 repo_provider（oauth 不持有）+ 仓库 bot=全局默认账号快照（derived_from 精确轮换、五场景由 is_default 服务）/ §14 扩展 checklist；UserToken 内部改 PAT→oauth_identity 桩→NO_REPO_CREDENTIAL（三层链序不动，d101 §二）；删除 D108-B 开关 | **实施进度（2026-11-13 第一批块①）**：领域层 `src/kernel/platform_object/`（domain.py 六聚合+ViaBits+compute_status；canonical.py 规范化 hash）、ORM `src/kernel/persistence/models_platform_object.py`（六表映射，不 create_all）、Repository `repositories_platform_object.py`（六 Repo，只 flush、未知键 fail-closed、位运算原子更新）、建表迁移 `db_setup.py` 202611170009（DDL 单一来源 `src/api/platform_object_ddl.py`，幂等+rc 补列+MySQL 显式跳过）、测试 `tests/mgmt/test_d109_platform_object_model.py`（15 passed：位运算/六态/五语义/orphaned 恢复/manual 保护/bot 默认唯一/关系生命周期/DDL 幂等/迁移本体）。；块② `src/kernel/platform_object/importer.py`（canonical hash 六类判定、未知键 apply/preview 同校验、单条 SAVEPOINT 隔离、epoch 仅变更时 +1、导入锁租约、纯登录渠道不派生幽灵 link、显式 link 缺目标整渠道 failed、preview 不写库、run_startup_import）+ `tests/mgmt/test_d109_import_split_mode.py`（10 passed，合计 25 passed）。；块③ migration.py（oauth 凭据→加密列 / bot 全量入库含历史别名与池 / 关系两段回填 / 对账 / derived_from 推定 / meta 停读不删）+ test_d109_credential_migration（7 passed）；块④ hooks.py 三写入点（OAuth 登录 5 处成功点 + 解绑 + PAT 保存/删除，全 fail-safe、GRAPE_PLATFORM_LINK_HOOKS 可关）、capabilities.py 能力视图（hosting 实时查模块、表未就绪回落旧判据、bot 摘要不含 token）、判据收敛两处（repos._hosting_implemented_providers / repo_manager._KNOWN_VCS_PROVIDERS → capabilities）+ 对拍 4 条；合计 36 passed。回归：8 failed 全为基线红（4 条 test_grape_auth 已知 + 4 条 s119/s88 经 stash 对照证实同红）。；块⑤ 读路径回切（providers._channels_from_table / platform_card._raws_from_table；表就绪优先、表内 provider 覆盖文件卡、文件独有保留、读失败 fail-open 回落）+ test_d109_read_path（4 passed）；块⑥ 启动接线（main.py lifespan 调 _d109_platform_object_startup：扫描导入 → commit → bump epoch → migrated → 一次性凭据迁移/关系回填 credentials_migrated；全 fail-open）+ 真实配置端到端实跑（channel=4 platform=4 caps=4 failed=0 links gitcode/github/gitee=1 huawei=0；boot2 changed=0 epoch 不变、迁移不重跑）；真跑修出两个实施缺陷（文档键_未剥离致 4 平台整卡拒绝 / pat_create_url 双面字段需拆到 caps）。合计 57 passed（含回归 s83/oauth 一致性）。块⑦ **实弹验证（独立测试库 ~/.hermes/cache/scratch/d109-live/sessions.db，8095-8098 实例）**：启动自动盖章 202611170009 + 导入 channel=4 platform=4 caps=4 changed=12 failed=0 epoch 0→1 + 迁移 reconcile ok；改 gitee.json name/host → 重启 changed=1、epoch 1→2、API /api/settings/vcs/platforms 返回新值；恢复文件 → 重启 changed=1、epoch 2→3、API 回退。实弹抓出并修 **缓存失效缺陷**：导入后未主动 invalidate → 启动早期缓存的旧卡撑满 300s TTL，出现「改配置重启后 API 仍旧值」；已接 invalidate_platform_card_cache + invalidate_provider_cache。块⑧ **管理面对象式 API**：`src/kernel/platform_object/admin_sync.py`（save_config 末尾同步：gitcode_api_token/github_bot_token/bot_usernames → grape_t_bot_accounts 加密列；oauth client_id/secret → oauth_channels 加密列；写后 epoch+1 + 双缓存失效；fail-safe 不阻断旧保存）+ `src/domains/mgmt/admin/platform_object_routes.py`（capabilities / bot-accounts GET+PUT / channels credentials / platforms 与 channels 的 enabled，全 require_admin，响应不含 token）+ admin router include；测试 3 条（加密可解 / 缺渠道 fail-safe / 路由注册）。实弹 8099：无 token → 401（路由+鉴权生效）、health 200；`/openapi.json` 是静态快照不含新路径（前端契约需重生成）。回归 64 passed + 3 条 admin_card_mgmt 基线红（FileNotFoundError src/api/static/admin.html、beta-apply.html，实证与本次无关）。余：前端调用路径 + openapi 重生成已核查为不成立（对外契约只收 get_current_user_or_api_key 依赖路由，admin 实测 0 条；前端 JS 无 admin/config 调用点）。**已本地提交**（单提交含 D108+D109，53 files；回归与基线失败集同名同数；未 push）。；**r2 实弹修正（2026-11-14）**：①启动装配只挂 main.py 漏 A 进程 grape_server + `@asynccontextmanager` 被插入函数劫持 → 抽 `src/api/d109_startup.py` 两入口共用 + AST 守卫 test_d109_startup_wiring（反证改名/删装饰器即红）；②能力视图缺纯登录渠道（known_providers 未并入登录渠道）+ 表未就绪时 login 恒 False（MySQL 下全渠道不可登录）→ 加 `_login_providers()` 回落，实弹 huawei=T/F/F；③MySQL 建表曾 `executescript` + 后端 return 跳过 → 0ms 盖章六表未建；改逐条 execute + 生成列唯一键，MySQL 8.0.46 实测 rc=0/盖章 164ms/导入 changed=12/管理面写接口 200，守卫 test_d109_ddl_dialect；④s83 两条回落语义用例依赖环境（表有行即红）→ 固定 is_table_backed=False，9 文件组合回到与基线一致的 11 failed；⑤孤儿软删/原样加回恢复、enabled 不被配置冲回、幂等二次导入（changed=0）均实弹通过；⑥裁定待落：表改名+删 via_bits、绑定路径补写关系、credential_resolver 代理层、link 改 manual 优先合并、仓库侧配置入口、存量四平台声明 oauth_identity（见设计分片 d109 §4/§5/§6/§17 与 §15 UC-D109-28~34）。；**r3 模型关系与需求映射（2026-11-14 用户要求"模型要真实反映用户需求"）**：设计分片新增 §18（N1~N6 需求逐条映射、实体基数表、关系图、派生规则、反向检查=每个模型要素必须有需求支撑、显式不做、现状差距），需求文档新增 FR-29（三段模型硬规则 6 条 + 映射与现状 + 验收 UC-28~34 + N4 反证）；结论：N2（绑定 gitee 后用 gitee 令牌驱动 gitee 仓库）模型成立但现实受阻——gitee 渠道 scope=user_info、github=read:user user:email 均不含仓库权限，现算给 SCOPE_INSUFFICIENT（只有 gitcode scope=all 现成可用），补救=仓库 OAuth 应用 B 补充授权或 PAT；代码已同步落地：表改名+删 via_bits（DDL/ORM/Repo/domain）、hooks 归一为 link/revoke（撤销前现查其它来源）、migration 回填改写、importer 改 merge_for_oauth（manual 优先）、新建 configs/repo-credentials.json 四平台声明；待落：credential_resolver 门面 + token_resolver 接入 + capabilities 用户维度 + 管理面 GET 聚合/link 写。；**r4 取凭据口径（2026-11-14 用户拍板）**：`fetch_user_token` ①PAT②OAuth 解析序与四层链**保持现状不改**，credential_resolver 只作配置校验/诊断展示（available/source/reason），不接管取 token —— 对现网取 token 行为零影响；已落地：表改名+删 via_bits、hooks link/revoke（撤销现查其它来源、PAT 保留关系）、绑定路径写关系、闭环判定（同域或 identity_and_credentials 才建行）+ 异步刷新项目列表、importer merge manual 优先、configs/repo-credentials.json 四平台声明、capabilities enabled + user_platform_view；D109 全套 70 passed；回归 8 文件 11 failed 与基线同名同数；实弹 8096 六表/幂等/capabilities（enabled、drivers、huawei）全绿。；**r5 管理面接口按领域建模（2026-11-14 用户纠偏）**：初版 `channels`/`platforms` 复数泛称丢限定词、CredentialCaps/ChannelRepoLink/UserRepoPlatformLink 三聚合无路径、派生视图混进对象域 → 改为**路径 = 领域对象名、平铺在 /api/admin 下、无父容器**（oauth-channels / repo-platforms / credential-caps / channel-repo-links / user-repo-platform-links / bot-accounts）+ 派生视图 `/capabilities`、`/user-platforms`（非实体、不挂对象前缀）+ 无整卡 PUT（只允许子资源写）+ 契约守卫（禁虚构父容器 `/platform-objects/*`、禁整卡 PUT）；设计分片 §19、需求 FR-30、场景用例 UC-D109-35~39 已登记。 设计分片 d109-platform-object-model-design.md（§3 四类配置/§4 七表与状态计算/§5 关系写入点/§6 凭据链/§8 split mode+命名+orphaned/§10 回填/§11 验收/§13 枚举表）；需求 S107；第一批实现待落（schema 草案先评审）；第二批 user_repo_tokens 语义（引用 vs 独立授权）未定 |
| G3-1 | 存量库升级路径补全——weekly-10 基线迁移撞号重放（2026-11-11；外部评审两轮实查整改项①） | weekly-10 tip 9c4c770b8 的 4 个迁移 fn（_fn_add_agent_tasks_trace_context / _fn_add_ap_exec_agent_task_id / _fn_repair_ap_exec_agent_task_id / _fn_backfill_events_terminal_completed_at_1497）在本链零定义，版本号（202611100001 / 202611090001-2 / 202611080001）被本链同号占用——dev 台账已盖章的存量库升级时被跳过，agent_tasks.trace_context、autopilot_executions.agent_task_id 与索引 idx_ap_exec_agent_task 静默缺失 | 新迁移 202611110003 replay-weekly10-migrations-skipped-by-version-collision（safe 幂等）：参照基线原 fn 逐字重放——_ensure_column 补两列 + CREATE INDEX IF NOT EXISTS 补 idx_ap_exec_agent_task（add/repair 两 fn 等价合并）+ events 终态 completed_at 分批回填（COALESCE(updated_at, created_at, now)，每批 500） | mysql gate 全绿 + 现网盖章库真盖章 202611110003；迁移守卫 43 passed（536 同号异名 1 红为预存量）| src/api/db_setup.py（202611110003）|
| G3-2 | settings 最后平台硬比较清零（2026-11-11；整改项③；LABEL_COLOR 同款契约开关模式） | src/api/routes/settings.py 两处平台名硬比较（L679 探测 token 字段三元式 + /credentials 存在性回退 _p == "gitcode"/"github"）触发 s63 守卫红 | providers/gitcode.py、github.py 声明 SETTINGS_TOKEN_FIELD_SUFFIX = "token"（模块契约开关），settings.py getattr 派生（未声明回退 <p>_pat 泛型口径）+ _legacy_dedicated_pat 契约派生回退 | test_s63 8/8 全绿（guard 随硬比较清除转绿）| src/vcs/providers/{gitcode,github}.py + src/api/routes/settings.py |
| G3-3 | test_s63 两红修复（2026-11-11；整改项⑤） | s63_swap 断言已退役的 bot_gitee_token 列（D106 冻结口径漂移）；s63_guard 断言 settings 硬比较 | swap 用例改 grape_t_repo_credentials 口径重写（rc 三列 mock + 解密等值比对 + 步骤 6 cred_type bot/webhook 加密写回断言 + 物理列名不得出现）；guard 随 G3-2 转绿 | test_s63_credential_family_card_driven 8 passed | tests/test_s63_credential_family_card_driven.py |
| G3-4 | db_setup 重复 fn 死副本清零（2026-11-11；整改项④） | weekly-08 合入回放段 32 个顶层符号（10 个 _fn_ + _mysql_target_swap 类 + 常量/助手族，848 行）重复定义且晚绑定遮蔽——其中两处为 pre-F4/D106 陈旧形态实际在运行时执行（_ensure_platform_storage_columns 自动补已退役列、backfill 无列存在守卫）| 逐符号比对（除 4 处仅尾注释差异外全部字节一致）后整段删除第二副本；uuid-pk 三注册（202611090010-12）绑定第一份（等价体）| uniq -c 全 fn 无 >1；pyflakes 重定义警告清零；s63/s80/s82/uc41/迁移守卫 70 passed | src/api/db_setup.py |
| D102-P 挂账 | test_uc43_pat_domain_split 两个 strict=False xfail 转正式 issue 跟踪（2026-11-11；整改项⑥） | L73/L86 两用例（PAT 行后同账号 OAuth 绑定升级链：create/upgrade 未按预期落 token、解绑前置失败）自认未修（原注释拟移交 D103），需登记转 issue 跟踪防止长期沉默 | 本行登记：两 xfail 转 issue 跟踪（OAuth 升级链——ei.access_token 落写与解绑前置两条链路），修复后摘 xfail 转 issue 状态闭环 | 保持 xfail 亮黄（strict=False 不掩盖回归）；issue 跟踪中——GitCode #1928（https://gitcode.com/grape-dev/grape/issues/1928） | tests/test_uc43_pat_domain_split.py:73/86 |
| G3-5 | D106 回滚预案入册（2026-11-11；整改项⑤文档面） | 回滚到 weekly-10 后旧码不认 grape_t_repo_credentials，bot token 读侧消失（列已被 D106 置空，值只活 rc 密文）| d106 设计分片新增 §6 回滚预案（退出 checklist 附带项）：读侧凭据消失机理 + 重录/rc 导出解密回写旧列恢复动作 + schema 无不可逆 + 正常退出四步顺序 | d106-cred-storage-terminal-batch1-design.md §6 | docs/weekly-09/multi-platform-decouple/d106-cred-storage-terminal-batch1-design.md |
| D109-分层修正 | 平台对象模型移出 kernel（2026-11-14；用户质询「platform_object 名称路径不合理」） | D109 第一批把「领域层」放在 `src/kernel/platform_object/`，与分层纪律冲突：check-arch R1 硬门禁实测 13 条 kernel→domains 边（mgmt.auth.credential_crypto/providers、mgmt.user.db、runtime.vcs.platform_card/providers），`__init__` re-export 又触发 R5 伪环；基线 1456e24a1 无此包（由本分支 f07a4b993 引入，属自造债） | 整包 `git mv` 到 `src/domains/platform/`（内容逐字节不变，仅路径替换 29 个引用方文件）；`__init__` 去 re-export （消费方走 `from src.domains.platform import hooks` 包级取子模块，不依赖 re-export）；持久化 ORM/Repository 仍留 `src/kernel/persistence/*_platform_object.py`；新增 `registry.py` 作平台目录运行时读口（表优先 `grape_t_repo_platforms`+`repo_credential_caps`、卡兜底），settings `/vcs/platforms` 与用户↔平台关系推导共用；check-arch 复跑：R1 仅剩基线既有 `cross_write -> domains.runtime`（基线同样红），R5 平台环消失；registry 语义锁 test_platform_registry_source + test_platform_links_pat_derivation；`-k "d109 or platform"` 首跑与基线同 16 红（新树首跑对拍：零新增） |
| D109-管理面命名修正 | 去掉「平台对象」父容器（2026-11-14；用户质询「根本没有这个领域对象」） | 管理面把六个聚合塞进 `admin/platform_objects/` 目录 + `platform_object_routes.py` 汇总壳，凭空造出一个不存在的领域对象名，与 §19「路径=领域对象名、无父容器」冲突（路径层已改、目录层漏改） | 七个路由文件 `git mv` 到 `admin/` 平铺（oauth_channel_routes / repo_platform_routes / credential_caps_routes / channel_repo_link_routes / user_repo_platform_link_routes / bot_account_routes / views_routes），共享请求模型 `common.py` → `api_common.py`，删除 `platform_objects/` 与 `platform_object_routes.py`，改在 `admin/routes.py` 逐个 include；守卫 test_d109_admin_api_contract 改为按聚合模块清单扫（禁父容器路径、禁整卡 PUT 口径不变） |
| D109-授权配置显式化 | 渠道↔平台授权写入配置文件 + 管理台可改（2026-11-14；用户口径「应该基于配置的关联平台仓库判断」） | ①`configs/oauth-providers.json` 六张卡都没有 `repo_links` 实值，全靠导入期「同域派生 identity_only」隐式生成；②hooks 建行判据里同域捷径与「无 link 取自己」绕过配置（gitee/github 声明 identity_only 却建行、gitlab drivers=[pat] 也建行）；③管理台渠道页只读展示 repo_links，改不了 | ①配置显式声明：gitcode→gitcode `identity_and_credentials`（scope=all 实可驱动），gitee/github→自身 `identity_only`，huawei 空；②hooks 严格配置驱动：`channel_repo_link.relation==identity_and_credentials` ∧ 目标平台 `drivers_json` 含 `oauth_identity` 才建行，同域捷径与自兜底删除（fail-closed）；③AdminChannels.vue 增加授权编辑（选平台+relation 保存 / 删 manual 行，走 `/api/admin/channel-repo-links` PUT/DELETE，manual 行导入不覆盖），i18n 中英补齐。实弹：导入后 link 行变 source=config（gitcode=identity_and_credentials、gitee/github=identity_only）；管理面 PUT gitee→identity_and_credentials 后 hooks 立刻建行、DELETE 后回落 config 不建行；登录建行实测 gitcode=建、gitee/github/gitlab/huawei=不建 |
| S129-133 展示层收口与去敏 | 2026-11-15 用户实测破图 + 口径打回 | ①内联 SVG 拷成文件缺 xmlns → gitee/huawei 破图；②详情页头绕过语言包，huawei 标"华为云"与实际渠道不符；③渠道页"可接入的代码托管平台"三段口径讲不清，且不该让用户手设配对 | ①补 xmlns + `?v=3` + 独立文件可渲染守卫；②名称统一 `provider_name.*`，huawei→华为/Huawei（配置与线上 meta 同步）；③整段移除（状态/7 个辅助函数/13 个语言包键），加反向守卫禁回潮；④四页 cacheVer + 指纹 + admin-view + 图标 `?v=` 同批 bump | d110 §14/§15、requirements FR-32.11~13、d109 管理面变更标注 | `tests/api/test_admin_brand_icons.py` 6 passed；两张 ratchet 6 passed；线上五枚图标 Chrome 解码全 ok |
| S134-135 可见性与基址口径（可见性部分已被 S136 取代） | 2026-11-15 用户问“gitlab 明明有，用户面为什么查不到仓库” + 指 webhook_url 语义混乱 | ①隐藏名单只在 settings 一处被读，接口层 `?provider=<hidden>` 仍可列仓（口径分裂）；②`webhook_url` 的 docstring 与配置示例没写明路径由平台卡决定、配置里的路径会被丢弃 | ①判据收敛 `visibility.is_platform_visible`，用户面 403 + 合并列表剔除 + 管理面 hidden_by_deploy 标记（内网写入口保留）；②只补注释与示例/管理面提示，不改行为（用户裁定不合并 server_base_url） | d110 §16、requirements FR-32.15~16 | `tests/mgmt/test_platform_visibility.py` 7 passed；两 ratchet 绿 |
| S136 可见性收敛到 enabled | 2026-11-15 用户追问“这个配置会落库吗、配置文件怎么配不展示” + 指出“全关回退文件卡”的坑 | ①名单（meta）无 UI 且与行级 enabled 重叠；②registry 过滤后为空被当成表不可用 → 回退平台卡（全关=全开）；③关平台后存量用户仓库凭空消失且无提示；④配置侧无法声明默认不展示 | ①判据收敛 visibility（enabled；无行 fail-open）+ 迁移 021 搬名单→enabled=0 并删键；②registry 哨兵拆 None/[]；③403 语义改 PLATFORM_DISABLED 且先于未绑定早退；④关平台影响面预检 409+confirm；⑤配置播种键（A 不做 / B 待拍板） | d110 §16、requirements FR-32.15 + S136 | `tests/mgmt/test_platform_visibility.py` 13 passed；跨套件回归 63 passed |
| S137 默认不展示写死初始化 | 2026-11-15 用户问“配置文件里怎么配成不展示” 后裁定写死初始化代码 | ①平台卡加 enabled 会把部署口径混进能力描述；②既有实例升级需要一次性补默认；③导入/恢复路径不能把运维开过的开关翻掉 | ①`DEFAULT_HIDDEN_PLATFORMS` 落持久化层，仅新建行与迁移 021 生效；②`enabled` 保持 `_PROTECTED`（导入不覆盖）；③orphaned 恢复视为可用；④管理面开关 + 影响面预检 | d110 §16.4、requirements FR-32.17 + S137 | `tests/mgmt/test_platform_visibility.py` 16 passed；跨套件 56 passed |
| S138 管理面交互三连修 | 2026-11-15 用户实测“说明被遮住”/“开关失败”/“弹层跑到页面外” | ①气泡 absolute 挂在 overflow:hidden 容器里被裁；②关闭平台缺确认→409；原生 confirm 被否决；AGModal 依赖未加载的设计令牌导致遮罩无 fixed；③两页共用 enabled 说明但语义已分叉 | ①AGHelp Teleport+fixed+锚点定位+z3000；②页面内 Vue 弹层（自带定位样式、影响面数字、confirm=1 重发、取消回滚），后端关闭一律 409；③按页映射 enabled_channel/enabled_platform | d110 §17、requirements FR-32.18~20 + S138 | 浏览器实测通过 + 两条新守卫 5 passed，ratchet 15 passed |
