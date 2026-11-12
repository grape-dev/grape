> ⚠️ 本文为拆分分片（内容唯一权威源）。索引导航：./multi-platform-decouple-requirements.md
> 来源：multi-platform-decouple-requirements.md L470-L511（原文原样搬运，零改写）。
> 域定义：解耦主线用例族——S1-S11 场景 + UC1-UC20（占位符/卡驱动/守卫收官前全部执行批次；场景表全集含 S12/S12b/S21 等见索引页场景清单——S12b=仅登录渠道镜像象限 D102-C）。
> 全局约束：继承索引页「表设计铁律/凭据续期口径/验收纪律」（跨族生效，见索引页）。

## 场景与用例清单

解耦用例（承接 v3.27 §5.6 验收口径，MySQL 方言级、目标环境执行）：

| # | 场景 | 前置状态 | 动作 | 预期 | 用例 |
|---|------|---------|------|------|------|
| S1 | 登录平台 ≠ 仓库平台 | 用户 A 用 gitcode OAuth 登录；github:owner/repo 绑定 github PAT | A 访问该 github 仓库 API | 用 PAT 成功，登录身份不参与取凭据 | test_s1_login_platform_independent |
| S2 | OAuth 过期自动刷新 | A 的 gitcode OAuth token 过期 | 调用读 API | 自动刷新重试成功，无感 | test_s2_oauth_token_read_from_identity_only + test_s2_github_token_read_from_identity_only（D3/K2 后语义演进：读侧身份域唯一权威+零刷新调用） |
| S3 | PAT 不自动续期 | A 手配 gitee PAT | 调用 gitee 仓库 API | 成功且零 refresh 调用 | test_s3_no_refresh_token_skips_refresh_even_if_expired |
| S4 | PAT 失效明确报错 | PAT 人为置无效 | 调用 | 明确"PAT 已失效"文案（非模糊 401） | test_s121_oauth_absent_pat_flow（UC25④ 401 文案不对称现状锁定） |
| S5 | 结构守卫 | — | 重构后 | grep get_user_github_token_with_refresh / _get_gitcode_credential_by_gitcode_id 全仓零残留 | test_s5_structure_guard_k1_k4_symbols_removed（step1 文件） |
| S6 | gitee webhook 接入 | gitee 平台配置就位 | POST /webhook/gitee push 事件 | 归一化为统一事件格式，与 github 方言同构 | test_s6_gitee_push_normalize + test_s6_gitee_issue_normalize + test_s6_gitee_pr_normalize（vcs_dialect_backend） |
| S7 | git 层配置驱动 | gitee/gitlab host 配置就位 | clone_url_for("gitee:o/r") | https://gitee.com/o/r.git，不再回退 gitcode.com | test_s7_clone_url_from_card（repogit_platform_card） |
| S8 | git 鉴权 scheme 按平台 | 同上 | _basic_auth / askpass | gitee 用 basic(token 为密码)，gitlab 用 oauth2 scheme；gitcode/github 行为不变（回归） | test_s8_basic_auth_scheme_is_x_access_token + test_s8_clone_url_gitcode_and_github + test_s8_askpass_token_env_chain_order（step1）+ test_s16_gitee_verify_dispatch（验签分派） |
| S9 | 既有平台零回归 | — | 全量 pytest | gitcode/github 现有测试全绿 | CI 门槛 |
| S10 | 对象模型分离：登录变更不波及仓库凭据 | 用户 A 绑定 gitee 仓库（PAT 或 OAuth 填充） | A 登出 / 换绑登录平台 / 登录 token 吊销 | 仓库凭据不受影响，仓库 API 调用照常（bot token 场景）；OAuth 填充型凭据按自身生命周期刷新，不因登录事件被删 | test_s10_password_revocation_does_not_touch_repo_creds（create_issue_and_lifecycle） |
| S11 | 仓库凭据独立生命周期 | A 手配 PAT（仓库凭据） | PAT 失效 / 更新 | 仅仓库凭据状态变更，登录会话（JWT/OAuth）零改动；反向亦然（改密码吊销 JWT 不动 PAT） | test_s11_pat_delete_does_not_touch_login（create_issue_and_lifecycle） |

## 候选集占位符家族场景用例（D75/D76，S100-S102）

| # | 场景 | 前置状态 | 动作 | 预期 | 用例 |
|---|------|---------|------|------|------|
| UC1 | 默认部署 5 候选全链绑定 | 4 平台卡内置（candidates=5） | engine/dashboard/alerts 三族按 repo 查询 | 占位符 5 == 参数 5，命中归属行，无 ProgrammingError | test_s100_engine_select_live_bindings / test_s100_dashboard_live_bindings / test_uc3_alerts_repo_filter_live |
| UC2 | 单平台卡配置 | 仅 1 张卡（candidates=2） | repo_key_in_params 产出 + sqlite 查询 | 占位符 2 == 参数 2，命中 | test_uc2_single_candidate |
| UC3 | 空候选集恒假 | repo 为空串 → candidates=[] | IN 子句查询（表含空串/NULL/正常行） | IN (NULL) 恒不匹配任何行（0 命中），非语法炸、非空串误匹配 | test_s101_in_params_empty_set_always_false |
| UC4 | 卡缓存漂移原子性 | admin 改卡触发卡缓存重载窗口 | 单次 repo_key_in_params 调用 | candidates 仅求值 1 次，占位符/参数同源（漂移窗口构造性不存在） | test_uc4_arity_survives_card_drift |
| UC5 | alerts 仓库过滤实弹（P1 修复闭环） | obs_alert_events 含 gitcode: 与异平台行 | list_events_all 同形态 SQL 按 repo 过滤 | 命中本平台行、排除异平台（修复前=ProgrammingError→空列表） | test_uc3_alerts_repo_filter_live |
| UC6 | skills 多平台歧义守卫 | ≥3 平台同名仓 | strict 上传 | 400 拒绝（==2 写死时被绕过），文案动态列出命中平台 | test_s100_skills_strict_ge_two |
| UC7 | 结构守卫（防回归） | — | 源码断言 | engine_select 零固定 OR 链且走原子产物；dashboard 零 IN (?,?)/零位置解包；skills >=2；白名单只减不增 | test_s100_engine_select_dynamic_placeholders / test_s100_dashboard_no_fixed_in_clauses / test_s101_grep_guard_handwritten_placeholder_family |
| UC8 | 知识读侧前缀不一致（D77 P2 闭环） | agent_shared_knowledge 行存 gitcode: 前缀 | 读侧传裸键 / 异前缀 (github:) / 本身前缀 | 三种入参均命中既有行；无关仓不误命中（修复前精确匹配静默丢注入） | test_uc8_knowledge_read_candidates_live |
| UC9 | 前端二分残留收口（D77） | 平台卡目录（/api/settings/vcs/platforms） | repos.js 托管 / dashboard.js issue 链接 / templates.js、scene-market.js 跳转 | 四站点零 github/gitcode 三元硬编码，host/provider 均卡派生（gitee 仓不再误路由/错链） | test_uc9_frontend_binary_gone |
| UC10 | scene-market 平台信息下发（D77） | 模板注册表含 source_platform | 场景明细接口 | payload 下发 source_platform（原缺失，前端无从派生 host） | test_uc10_scene_market_platform_shipped |
| UC11 | 前端缓存版本纪律 | JS 页面模块变更 | index.html ?v= 检查 | 五处版本号 bump 且只升不降 | test_uc11_cache_version_bumped |
| UC12 | parse_repo_key 内联回潮收口（D78） | 全仓三类内联形态枚举（35 处） | 逐站点分类：6 处真回潮收口（issues/pipeline 双平台互查→candidates；event_pipeline provider 内联→parse_repo_key；issues_center 手写镜像→_mirror_repo_keys；webhook_token_field→卡派生；history 目录名冒号安全） | 超集查询安全（名空间唯一）；gitee/gitlab 绑定仓不再漏查；card 字段与二元逐字节一致 | 涉改族基线对照 3==3 + S100-S103 守卫 25 绿（内联守卫=UC7 源码断言族延伸） |
| UC13 | app.js stripRepoPrefix 闭合完整性（D83 P0 闭环） | app.js 含 S27/S103 PlatformCatalog 块（stripRepoPrefix 后追加顶层函数） | ①结构断言：stripRepoPrefix 函数体在 return 后立即闭合（大括号深度回到 0 后才允许出现下一个顶层 function）；②node 实弹：node --check + stub 全局直接 eval 全文件 + 函数行为验证 | 加载零 ReferenceError；stripRepoPrefix('github:a/b')='a/b'；_appLoadVcsCatalog/_repoWebLink/_repoWebHost 顶层可达且 gitee host 卡派生正确；缓存版本随内容变更递增 | test_uc13_appjs_strip_prefix_closure / test_uc13_appjs_toplevel_reachable / test_uc13_appjs_host_from_catalog |
| UC14 | web 凭据不入日志（D83 N1 闭环） | gitee 平台（query 参数鉴权）；UpstreamAPIError 携带 URL 进 log.warning | 构造含 access_token 的 URL 触发 _redact_url | token 值被 *** 替换（前缀参数/后缀参数/无参数三形态），URL 其余部分保留——str(e) 与日志渲染面均无凭据 | test_uc14_gitee_url_redaction |
| UC15 | 测试基建环境无关性（D83：/root 硬编码清零 + 全局 bot 缓存隔离） | 测试文件引用 src/**；开发机存在 configs/global-bot-token.json（文件 fallback 毒化源） | ①守卫：tests/ 零 <repo-root> 硬编码路径；②行为：s87/s88 fixture 写 meta 后必须失效 60s TTL 缓存（_invalidate_global_bot_cache），毒化文件在场断言仍绿 | 测试在任意检出路径/无 /root 环境通过；文件 fallback 值不再穿透 meta 种值（缓存隔离手法与 test_global_bot_token_db.py 对齐） | test_uc15_no_root_hardcoded_paths / test_s87_global_bot_usernames_for_provider / test_s88_bot_username_gitcode_pool_first |
| UC16 | worker 包 import 闭包完整性（D84 P0 闭环） | MANIFEST 闭包内模块新增顶层 import（runtime/service.py × src/vcs、api/session_store × api_client）；worker 独立 venv 部署 | ①结构守卫：MANIFEST 与 release_pack tools 闭包必须含 src/vcs/（service.py+session_store 顶层 import 面）；②依赖面守卫：ast 扫闭包内模块顶层 src.domains.runtime.vcs import ⇒ MANIFEST 必含 src/vcs/（通用化防回潮）；③名单完整性守卫（名单文件必须真有顶层 import，防死名单）；③消费循环语义守卫：release_pack 取项循环必须处理目录项（字面量守卫假绿教训）+双清单逐项对齐；运行时级 import 演练由既有 test_worker_package_import_closure（子进程干净隔离+实打包）覆盖 | worker 包解压后零 ModuleNotFoundError；import src.domains.runtime.engine.agent 全链路 OK；MANIFEST 变更与 src 顶层 import 面同步（防回潮） | test_s112_worker_pkg_closure（manifest_guard + tools_closure_guard + loop_dirs_guard + aligned_manifest_guard + import_face_guard + sites_documented_guard + vcs_third_party_guard） |
| UC17 | worker chat 用户 token 注入链修复（D85：基线预存降级收口） | worker 独立部署（SQLite 本地/MySQL 共享两形态）；chat 任务带 user_id>0；MANIFEST 含 deps.py+persistence+sqlalchemy+fastapi | ①闭包守卫：MANIFEST/release_pack 含 deps.py+persistence/，worker 清单含 SQLAlchemy+fastapi；②链探针守卫：worker 闭包内 get_user_token_for_repo/get_grape_account_by_id/resolve_user_id_by_platform_login 三入口 import 可达；③语义演练：SQLite 空库 resolve=0/token="" 零异常（与降级等价）；MySQL 形态功能恢复 | import 零 ModuleNotFoundError（不再 log.warning 降级）；SQLite 形态结果与降级一致；MySQL 形态 chat token 真注入、身份解析走 external_identities 权威域 | test_s113_worker_token_chain（closure_guard + chain_probe_guard + sqlite_semantics_probe） |
| UC18 | 测试迁移债清零 + host_repo 网络错放行（D86：30 净失败→0） | 域1 旧双符号 token 入口/身份探测端口（S62/D42）；域2 issue_372 旧凭据符号/ORM 化断言；域3 s17 卡文件化回落语义；连带产品修复：host_repo 身份+权限前置网络错/None 放行（issue455 基线语义恢复） | ①mock 口径：token 入口按 provider 分发双点位（repos 绑定+deps 源）ExitStack 组合；身份探测 mock 网络缝（repo_manager.get_user_*_login）不 mock 端口类；②ORM 化断言：spy 仓库层（_iar）+ 真库；③语义断言：403/404 从端口层穿透状态码匹配；网络错/None 放行 200 | 五文件 130 passed；403→TOKEN_PERMISSION_INSUFFICIENT（前端双 code 兼容）、404→REPO_NOT_FOUND、网络错/None→200 放行；S 系列+worker 域 156 passed | test_repos_host_provider / test_login_provider / test_issue455_oss_onboarding / test_issue_372_user_token / test_vcs_dialect_backend（迁移后全绿）+ s112/s113 守卫无回潮 |
| UC19 | git_repo_api 6 净失败归因修正 + 清零（D87/S115：误归预存的分支债清零） | 域 A：S107/G2 四平台注册后 gitlab"未接入"代表失效（403→502）；域 B：D42 TokenResolver 策略链迁移后旧 db mock 不在链上（GlobalBotToken 文件 fallback 毒化 m4kj4/github 侧 502）；连带：test_issues_crud_mapping 水印断言过时（基线既有行为，真预存） | ①域 A：未接入防御改用真未注册前缀 nosuchplatform（对齐 UC18 域3 s17 修法）；②域 B：弃 mock 走真库（对齐 UC18 域2 先例）——autouse fixture 按 repo_key 播种 managed_repos+grape_t_repo_credentials 双写，不同 repo_key 播不同 token 值等价验证单前缀传递；③基线对照落到用例名级差集（7 failed→基线仅 1 同败，6 个分支净回归）；④RED：stash 修复→4 failed 毒化复现→pop→全绿 | 本文件 60 passed；混跑（S114 五文件+守卫）211 passed 零污染；水印断言前缀匹配+锁定水印行为 | test_git_repo_api（test_unknown_provider_rejected / test_git_credential / test_repos_credential_provider_from_token / TestRepoCredentialProviderByPath×5 / test_issues_crud_mapping） |
| UC20 | 卡驱动漏网二元分叉收口（D88/S115 续批：A2A fallback + templates 白名单） | 域A：webhook _a2a_repo_fallback_check 基线二元判定 not in(gitcode,github)（gitee 仓 A2A 归属校验直接拒）；域B：templates create_from_template 显式 provider 白名单 in(gitcode,github)（gitee 模板创建被拦）；域C：admin verify-token 白名单（下游 config_store.verify_token 已卡驱动，入口白名单脱节）。三处均基线预存，D71 清点登记冻结，下游卡驱动链路 S107 后已全部就绪；同族全仓清点 9 处 6 处合法冻结登记（db.py 列回落/repos 探测序/scenes D19 已收口/webhook 分流语义/admin whitelist 表结构边界） | ①域A：get_platform_card(_provider) is None 卡派生判定（对齐 webhook L1751 既有写法），未知前缀/残缺 owner 照旧 unsupported；②域B：卡在册即走显式平台，无卡平台回落自动选择分支（不 500）；③域C：verify-token 纯卡派生（教训：首版复合形态被自家结构守卫抓出，if-else 按平台名分叉必须全改），未知平台/空 token 防御保留；④结构守卫圈定函数体（S112 教训）零二元判定+必含 get_platform_card；⑤mock 缝对齐既有写法（域A/B：TokenResolver+VcsApiClient 方法级；域C：config_store.verify_token 源模块 patch） | gitee/gitlab 仓 A2A verdict 真探测（ok/not_found/no_permission 全平台生效）；显式 gitee 模板创建走 gitee token 链；存量双平台行为逐字节不变；gitee/gitlab token 验证可用；新守卫 16 用例 + issue_671 回归 27 passed；模板/openapi/方言/守卫/admin 回归 174 passed（s66 失败=stash 对照预存） | test_s115_platform_gate_closure（TestA2AFallbackCardDriven×6 / TestTemplateProviderCardDriven×3 / TestAdminVerifyTokenCardDriven×5 / TestNoBinaryProviderGate×2） |


## D105b 账号域 user_id 外键 TEXT 归一场景用例（2026-11-09）

背景：D105-2 切 UUIDv7 后 30 表 31 列 *user_id 外键仍 INTEGER——实弹第一例：绑定转移票 validate_binding_ticket 24 != '24' 秒 410；uuid 用户写入 MySQL INT 列必崩。设计见 d105b-account-fk-user-id-text-design.md。

| # | 场景 | 前置状态 | 动作 | 预期 | 用例 |
|---|------|---------|------|------|------|
| UC57 | uuid 用户绑定转移全链 | 账号 id 为 36 字符 uuid 串 | save_binding_ticket → validate_binding_ticket(str id) → consume | 校验通过（不 410），票据正常消费，转移成功 | test_transfer_bind（uuid-id 回归用例） |
| UC58 | 混型存量库迁移保值 | 列混存 int(24) 与 uuid 行 | 跑迁移 202611091000 | 全列 typeof=text、'24' 保值不丢、幂等二次跑零改动、str 参数命中原 int 存量行 | test_account_fk_user_id_text_migration（或并入 test_issue_number_text_migration） |
| UC59 | 新装库 DDL 正确性 | 全新空库 | init_db 建表 | 枚举 31 列声明全 TEXT，uuid 直写无混型；MySQL 端落 VARCHAR 通道 | test_issue_number_text_migration + test_mysql_dialect_compat |

| UC60 | 账号域外键 TEXT 不变式守卫（防复发） | 全新库 init_db 建表 | 对枚举清单+追补散列逐列查声明/写入 uuid+数字串双探针查 typeof | 声明全 TEXT、探针写入后 typeof 恒 text；新表漏同步清单/声明必红 | test_schema_fk_type_invariant |
| UC61 | uuid7 新用户全生命周期（最小闭环） | 全新库 | 注册→save/validate/consume binding_ticket→跨用户与存量混型隔离→身份绑定→凭据审计落库→外键列 typeof 终检 | 全链零异常零 410、id 恒 uuid36、触达外键列 typeof 恒 text | test_new_user_uuid_lifecycle |


## D105d MySQL 演练与部署门禁场景用例（2026-11-09）

背景：现网 MySQL 实存确认后，用户裁定部署门槛自动化 + 基于测试库 grape-drill-db（自建演练库名，见部署配置） 本地演练。设计见 d105d-mysql-drill-and-deploy-gate-design.md。

| # | 场景 | 前置状态 | 动作 | 预期 | 用例 |
|---|------|---------|------|------|------|
| UC62 | 门禁脚本阶段编排 | 模拟 mysqldump/mysql 命令 | backup/shadow/apply/smoke 四阶段 | 校验失败中止保现场；缺 --allow-prod 只打 plan 退 0；幂等重入 | test_deploy_mysql_gate |
| UC63 | 存量 MySQL 库全链迁移演练 | 真实 dump 恢复的 81 表旧 schema 库 | 跑完整迁移链 | 零 1064/零 MANUAL REVIEW/零建表失败；id '1''2''3' 保值 str；引用列全 VARCHAR；二次 0 pending | test_mysql_gate_migration_chain（GRAPE_GATE_TEST=1 真机门控） |
| UC64 | 新装 MySQL 库 DDL 正确性 | 空库（含 TEXT-DEFAULT 剥除后的 7 表） | init_database | 全部表建成，零 1101/1064 | test_mysql_dialect_compat（TEXT-DEFAULT 剥除用例） |


## 台阶3 资源 API 翻译卡驱动化场景用例（2026-11-09，D105-11）

背景："零代码接入"的最后防线 providers/<name>.py 声明化——卡 resources 段（字段级缺省合并，github 形态为缺省）。设计见 d105f-card-driven-resource-translation-design.md。

| # | 场景 | 前置状态 | 动作 | 预期 | 用例 |
|---|------|---------|------|------|------|
| UC66 | 双轨等价性 | 四存量平台（卡无 resources 段走模块） | 同输入分别走卡路径与模块路径 | 逐字段结构化对比完全一致（transform 回调单独锁定） | test_s3_card_driven_resources |
| UC67 | 配新卡资源接入 | foo 卡带 resources 段 | translate_inbound('foo','issues','create',...) | 正向返回预期映射结构；非法资源/op 负向仍拒；报错含卡 ID+字段路径+期望格式 | test_platform_onboarding_acceptance（③翻正）+ test_platform_card_matrix |


## D105g 台阶1+2 与侧栏分组场景用例（2026-11-09）

| # | 场景 | 前置状态 | 动作 | 预期 | 用例 |
|---|------|---------|------|------|------|
| UC68 | 全卡验收矩阵 | configs/platforms 全部卡 | 跑参数化矩阵 | 每卡六断言全绿；新增卡自动进矩阵；无模块平台资源 API 断言 ValueError（门槛显性） | test_platform_card_matrix |
| UC69 | 品牌资产目录化 | 卡含 brand_color | 目录接口 + 前端消费 | 接口透出 brand_color；前端兜底表与卡同值；platformIcon 目录优先 | test_platform_card_matrix（brand_color 断言）|
| UC70 | 工作区侧栏按平台分组 | 用户绑定多平台、各平台有托管仓 | 进入工作区看侧栏仓库下拉 | 按 repo_key 真实前缀分组（Gitee 仓不混入 GitCode 组），组名查平台卡目录 | 浏览器实测（fabdb3cb9：GitCode/Gitee/GitHub 三组并列）|


## D105i 批S2 XSS 系统性收敛场景用例（2026-11-09）

| # | 场景 | 预期 | 用例 |
|---|------|------|------|
| UC71 | 用户可控字段（title/summary/display_name/description/message）进 innerHTML | 必经 escHtml 系 helper；裸拼新增即红（ratchet 基线 7 条豁免有据） | test_xss_escape_guard |
| UC71b | escape helper 单源 | 9 个本地 helper 全部代理到 window.escHtml（含 ' →&#39;）；skills/observability 漏单引号缺口补齐 | test_xss_escape_guard + node --check |
