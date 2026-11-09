# 需求文档：OAuth 未配置的登录提示与状态前置（Issue #535）

> 状态：已实现（分支 `fix/weekly-09-yaw-oauth-login-hint`，commit 96a04351f → ca6672ef3 → de48a7c52）
> 方案拍板：**B+**（按 `configured` 置灰 + 原因/文档入口前置），**A 作状态未知时的兜底**。纯 B（只置灰不给原因）不合理，纯 A（点击后才提示）发现太晚。

## 1. 背景与问题

首次部署（OAuth App 未配置）时，登录页点击「GitCode 登录」/「GitHub 登录」的提示不合理：

1. 不说明原因——前端 catch 丢弃后端 400「{name} OAuth 未配置」，弹固定文案，用户不知道是部署问题还是自己操作问题
2. 指引不可达——旧文案「在设置页绑定 Token」对未登录用户走不通（还没账号）
3. 无平台名——GitCode / GitHub 共用同一文案
4. 缺文档入口——配置指南已存在于帮助文档（help/developer/01、02），但提示无链接

## 2. 方案（B+ + A 兜底）

**决策依据**：验收标准 = 未配置用户**不点击**即可看懂「为什么不能用」和「该看哪里」。

- **置灰**：未配置按钮 `aria-disabled="true"`（不用真 `disabled`——保留点击可解释），CSS opacity 0.45 + not-allowed
- **信息前置**：按钮组下方常显一行说明（含文档深链），全部已配置时自动移除
- **点击灰按钮**：不发请求，直接弹完整原因 toast + 文档链接
- **不闪灰**：`/auth/providers` 未返回前按钮保持原样（状态未知不置灰），点击走 A 兜底
- **A 兜底**（oauthLogin catch）：400 且 detail 含「未配置」→ 配置文案 + 帮助深链；网络/限流/5xx → 通用「登录请求失败，请稍后重试」
- **落地约束**：登录页无法区分管理员/普通用户（角色在 JWT，未登录拿不到），不做「去配置/联系管理员」分支；该区分只适合已登录的绑定页（account.js，R7.4 已覆盖，不在本需求内）

## 3. 场景与用例（UC1–UC15）

### S1 全未配置（新部署）
- UC1：两个按钮均置灰，常显「GitCode / GitHub 登录需要管理员先配置 OAuth 后才能使用 · 查看配置帮助」，深链 help.html GitCode 集成章节
- UC2：点击灰按钮 → 0 次 API 调用，toast 弹「系统未配置 {name} OAuth，{name} 登录暂不可用」+ 副文案 + 查看配置帮助链接

### S2 单平台配置
- UC3：已配置按钮正常、未配置按钮置灰；说明行只列未配置平台名，深链指向该平台集成章节

### S3 全部已配置（正常态）
- UC4：无置灰、无说明行；点击走正常 OAuth 重定向

### S4 状态未知（providers 未返回 / 检测失败）
- UC5：按钮不置灰、无说明；点击发请求 → 后端 400「未配置」→ catch 判定 status=400 且 detail 匹配「未配置」→ 配置文案 toast + 深链（A 兜底）

### S5 非配置类失败
- UC6：网络瞬断 / 429 限流 / 5xx → catch 不匹配「未配置」→ 通用「登录请求失败，请稍后重试」toast，不带配置文案（不误导用户查配置）

### S6 国际化
- UC7：EN 用户加载、`I18N.setLang` 切换语言时，说明行文本跟随切换（data-i18n + data-i18n-vars 自动刷新），无中英残留
- UC8：i18n 包慢于 providers 到达（慢网络）→ 包加载完成时 applyI18nToDOM 刷新说明行，fallback 中文不永久固化

### S7 同类「未配置」提示的体验对齐（P1 收口）
- UC9（忘记密码页）：邮箱未配置时页面预置提示行带「查看配置帮助」深链 help.html §5.2 邮件配置；服务端注入 `__EMAIL_CONFIGURED__` 零闪动，链接走 data-i18n 随语言切换
- UC10（账号安全页三入口）：重置密码/设置密码/换邮箱被邮箱未配置挡住时，toast 主文案**按场景说明被什么挡住**（三个 context key 区分），副文案给可行动指引，附「查看配置帮助」深链——普通用户知道要联系管理员，管理员知道按文档配置
- UC11（账号页邮箱卡片降级）：R7 原设计在邮箱未配置时**整卡隐藏**——用户只会疑惑「为什么没有换邮箱入口」。改为卡片保留但降级：说明「邮件服务未配置，邮箱相关功能暂不可用」+ 配置帮助链接，入口不再凭空消失
- UC13（引导页绑定弹窗数据流断裂，实测发现）：OAuth **已配置**时，新手引导页点绑定卡片弹窗只有手动令牌一个页签。根因：弹窗读的 `_accountProvidersData` 只在账号安全 tab 填充，引导路径缓存为 null 被保守猜成「未配置」。修复两层：①`_renderBindingGuide` 把 providers 数据写入共享缓存（引导路径零额外请求）；②`_accountBindProvider` 缓存缺失时主动 fetch `/auth/providers` 权威状态再渲染（兜住所有未来入口），fetch 失败才降级未配置
- 设计约束（用户友好性）：提示不只说「不能做什么」，同时给出「下一步做什么」；文档入口可直达，不要求用户自己去找帮助中心；功能入口不凭空消失（宁可降级展示也不隐藏）。绑定页（_accountManualBindForm）R7.4 已有 token 创建直达链接与 PAT 续期提示，体验达标不改。

### S8 OAuth 回调兑票链路（实测发现的登录失败根因，P0）
- UC14（票据 TTL 10s→60s，缓解）：回调成功后前端才兑票，但 SPA 首载（公网无缓存拉全量静态资源）经常 >10s——票已过期 → 401 → 被踢回登录页，用户视角是「GitHub 登录点了没反应/又被弹回」。`save_exchange_code` 过期窗口 10s→60s。安全论证：code 为 sha256(urandom(32)) 高熵值，威胁模型是截获而非枚举，放宽 TTL 不扩大枚举面；一次性原子消费（`validate_exchange_code` SELECT+DELETE rowcount）不变；兑换是 POST（链接预取/邮件扫描器只发 GET，不会预消费）
- UC15（内联兑票脚本，治本）：`main.py` `_inject_email_configured` 在 `</head>` 前注入 `_EARLY_EXCHANGE_SCRIPT`（与 UC12 注入同一函数、同一零缓存页面链路）——HTML 解析期（任何 defer JS 下载前）检测 URL `exchange_code` 参数即发起 POST `/api/auth/exchange`，成功后存 token + 清缓存 + 立即 `history.replaceState` strip URL（最小化票在地址栏/历史/Referer 暴露），sessionStorage 留 `__oauth_inline_login__` / `__oauth_inline_first_login__` flag；失败留 `__oauth_exchange_failed__` flag。app.js `init()` 的原 exchangeCode 分支前增加 flag 接力块：成功路径补上内联脚本做不了的 DOM/I18N/API 依赖（first_login UI、`showApp`、`API.scheduleProactiveRefresh`）；失败路径与原兑票失败同款 UX（`oauth_exchange_failed` toast + `showLogin`）。无参数时脚本快速 no-op，非回调访问零开销
- UC15b（i18n 三层同步）：`oauth_exchange_failed` 进入 zh/en 全量 locale + js/i18n.js `_INLINE_PACKS` 内置兜底（app.js init 可能在全量包异步加载窗口内执行 toast），i18n.js 引用页 `?v=` 全部递增
- UC15c（失败详情透传，收尾修复）：内联失败路径解析后端 `detail`（`r.json()` → `message`/`error_code`），与 app.js 原兑票分支 R5 同款——存 `__oauth_exchange_failed_msg__`，失败接力块优先展示具体原因、无则回退通用文案。不再有「内联失败只弹通用文案」的降级
- UC15d（popup 登录消费方补齐，收尾修复）：`/login/{provider}?popup=true` 回调经 oauth-popup-callback.html postMessage `{type:'oauth_result', action:'login'}` 此前在 app.js 无消费方（休眠路径）。app.js init 注册幂等监听：origin 校验 + type/action 白名单（与 account.js 同款防护）；成功→兑票→存 token→清缓存→`location.replace('/')` 整页重载走回访用户路径；失败→解析 detail 同款 toast。默认登录仍是全页重定向（oauthLogin 不带 popup），监听只激活休眠路径，不改变默认 UX

### S9 JWT 签名密钥持久化（实测发现的第二根因，P0-ops，修复「经常被踢回登录页」）
- 根因（三层证据链）：①`jwt_utils.py` 无强 JWT_SECRET 时走 `_ensure_dev_ephemeral_secret()` 进程内随机密钥；②本机 devops-bot.log 6 次「随机密钥」告警，其中 14:11:45 与用户 refresh 401 被踢同一分钟、14:29:54 与被迫重登时刻重合；③代码无 dotenv 加载，`.env` 文件通道实际不生效——部署文档的历史说法过期。任何 uvicorn 重启 = 全体用户 access+refresh 验签作废 = 踢回登录页，三种登录方式全中招
- UC16（方案决策——为什么不入库）：密钥是信任根不是数据，宿主必须比被保护物更不可丢、更不可见。入业务库的问题：①加密死锁——DB 内第三方凭据的 AES-GCM key 从 JWT_SECRET 派生（security_config.py:92），密钥入库=密文与密钥同库；②故障域耦合——本设计 token 验签不查库（DB 全丢会话仍活），入库把存储故障升级为登录故障；③暴露面——备份/慢日志/任意 SELECT 均可触达，文件 600 权限仅 root；④DB 可换（SQLite/MySQL 可切）密钥不可换，给所有 DB 运维加一致性前提；⑤轮换需表设计+双写竞争，文件只需换文件重启。结论：**文件（configs/security.json）> 环境变量（JWT_SECRET）> 随机兜底**，与代码既有三层 fallback 一致
- UC16a（载体）：`configs/security.json`（600，root only，不进 git）：`env: production`（打开启动门禁——忘配密钥直接拒绝启动，当场暴露而非静默裸奔）+ `jwt.secret_keys`（kid 池 `k1:<hex>,k2:<hex>` 两把，日常 k1，轮换时切 k2、k1 保留 30 天再摘——轮换路径现成）+ `credential_keys`（显式 32 字节，与 JWT 信任根解耦）
- UC16b（分发）：密钥进部署管道不进 git 仓库（仓库只提交 example 模板）。部署脚本三步：本机检查文件存在（缺失终止）→ scp 到对端 → chmod 600 → 双端 sha256 对账（不一致终止部署）。对账用指纹（sha256 前 8 位）可进日志，不可逆推内容。多机集群（174/79）必须同文件内容，靠对账强制而非纪律
- UC16c（启动）：统一启动器 `start-grape.sh`（pkill 旧进程 → 启动双 uvicorn）；启动后检查 `/tmp/grape-main.log` 零「随机密钥」告警，有告警=未生效立即回滚。严禁裸 nohup
- UC16d（集群互通验收金标准，四格全绿）：A 机签 token → A 机验 200；A 机签 → B 机验 200；B 机签 → B 机验 200；B 机签 → A 机验 200。同时覆盖「持久化」（重启后仍 200）与「双主互通」（ELB 轮询不掉线）
- UC16e（踢人时停后台轮询，次生体验修复）：密钥切换事故暴露——`doRefresh` 失败清 token + `showLogin()` 后，dashboard 30s 轮询定时器仍在裸发请求（token 已清走「无 token」静默分支），nginx 实证 16 点后 112 次无声 401，用户只看到数据不动、得不到「请重新登录」引导。修复：`showLogin()` 统一收口清理 `_refreshTimer`（一次覆盖 refresh 失败/手动登出/token 失效全部踢人入口）；`_refreshTimer` 声明前置到 showLogin 之后声明区并加守卫注释（防 TDZ 与未来重构踩雷）；app.js `?v=207`
- UC15f（竞态修复，外审确认）：内联兑票 fetch 进行中而 app.js init 先到（弱网/后端慢——恰是本修复目标场景族）时，URL 已 strip、接力 flag 未落，init 走默认路径把已入库 token 孤立在登录页。修复：内联脚本发起 fetch 前同步置 `__oauth_inline_pending__`（成败终态都摘）；app.js init 见 pending 则 100ms 轮询等终态 flag（成功/失败任一），5s 兜底超时防死等，到点重入 init 走正常接力。双重重入守卫 `_pfDone`（interval 与超时只放行一次）。app.js `?v=209`
- UC15g（popup 跳转丢 /en 前缀，外审发现）：popup 登录成功跳转曾硬编码 `location.replace('/')`——英文用户登录后被打回中文首页。改为与原 exchangeCode 分支同款 `I18N.getLang()` 派生前缀。app.js `?v=210`
- 兼容性（向前）：验签逻辑零改动，只换密钥值；无 kid 旧 token 回退 JWT_SECRET 的分支保留；exp/max_exp/token_version 生命周期语义不变。切换那次重启全员需重登一次（与随机密钥时代任何一次重启等价，最坏=现状，无更差路径）

### 加载时序（闪动审查）
- ~~状态未知窗口（~113–283ms 本地；公网 1–2s）内按钮常亮~~ → **UC12 已消除**：服务端在 HTML head 注入 `window.__OAUTH_CONFIGURED__` + `<html>` **按平台粒度**的 `oauth-{provider}-not-configured` 类（复用注册页 `__EMAIL_CONFIGURED__` 同一模式）。CSS 类在首帧（defer JS 运行前）即弱化对应按钮，实测 61ms 首帧 opacity=0.45，无「先常亮后置灰」闪动。
- **UC12b 按平台粒度（评审修复）**：初版类是 all-or-nothing（任一未配置 → 全部按钮首帧置灰），单平台配置时已配置按钮被误灰、providers 返回后纠正产生反向闪动。改为逐平台注入类后，单平台配置场景**首帧即终态**（实测：gitcode 已配置 opacity=1 常亮、github 0.45、说明行只提 GitHub，同一帧）。四条路径闪动分析：全未配置/单平台/全配置均首帧即终态零跳变；唯一理论窗口是页面渲染与 providers 返回之间管理员改配置（秒级），方向永远是「从错到对」的收敛，旧版同样存在。
- **UC12c 中性预置文案（遗漏扫描修复）**：预置说明行曾硬编码「GitCode / GitHub」名单——单平台配置时首帧点名了**错误平台**（~300ms 后 JS 才收窄为真实名单）。预置文案改用不点名的 `oauth_needs_config_generic` key，首帧永远准确（中性），JS 到场后切到带 `{name}` 的精确 key。实测时间线：68ms 中性文案 → 267ms「GitHub …」精确文案，无错误信息窗口。
- 注入快照与运行时不一致的闭环：renderOAuthButtons 拿到 `/auth/providers` 权威状态后**逐平台纠正类名**（`oauth-configured` 类保留为说明行显隐的整体锚点：全配置=隐藏）。已配置场景不注入任何 oauth 类（CSS 无规则、无中间态），行为与之前完全一致。服务端未注入（异常入口页）→ JS 同步块跳过，维持 A 兜底。

## 4. 用例代码（关键实现位置）

| 场景 | 代码位置 |
|---|---|
| UC1/UC3 说明行渲染 | `src/api/static/js/app.js` `_renderOAuthUnconfiguredHint`（data-i18n 结构，文本过 `_esc`） |
| UC1/UC3 置灰同步 | `renderOAuthButtons`：重渲染路径（`ariaVar`）与预渲染按钮同步路径（setAttribute/removeAttribute） |
| UC2/UC5 前置拦截 + A 兜底 | `oauthLogin`：`_oauthConfiguredMap[provider] === false` 前置拦截；catch 按 `e.status===400` + `/未配置|not configured/i` 分流 |
| UC6 通用失败文案 | catch 非匹配分支 → `app.login.oauth_request_failed` |
| UC7/UC8 语言跟随 | hint 文本挂 `data-i18n="app.login.oauth_needs_config"` + `data-i18n-vars`（name），`I18N.applyI18nToDOM` 原生刷新 |
| UC9 忘记密码页链接 | `forgot.html` `#forgotNotConfigured`：提示行 + `<a href="/static/help.html#/developer/04-storage-and-email">`，文案均挂 data-i18n |
| UC10 账号页三入口 | `account.js` `_accountShowEmailNotConfiguredToast(contextKey)`：主文案（reset/set/change 三 key）+ `email_not_configured_hint` 副文案 + link 参数深链 §5.2 |
| UC11 邮箱卡片降级 | `account.js` 安全 tab 卡片2 else 分支：`email_service_not_configured_card` 说明 + `<a>` 深链 §5.2（替代 R7 整卡隐藏） |
| UC12 首帧零闪动 | `main.py` 注入 `__OAUTH_CONFIGURED__` + **按平台** `oauth-{key}-not-configured` 类 → `auth.css` `[class*=...]` 首帧弱化对应按钮（UC12b：单平台配置不误灰另一平台）；`app.js` `_applyInjectedOAuthState` 同步块补 aria/说明行；`renderOAuthButtons` 逐平台纠正类名 |
| UC12c 中性预置文案 | `index.html` 预置说明用 `oauth_needs_config_generic` key（不点名平台）——首帧永远准确；`_renderOAuthUnconfiguredHint` 把 span 的 data-i18n 从 generic 切到 `oauth_needs_config` 并设 vars（真实未配置名单），语言切换后 applyI18nToDOM 用新 key+vars 渲染 |
| UC13 引导页绑定弹窗 | 层1：`app.js` `_renderBindingGuide` 写入 `_accountProvidersData`；层2：`account.js` `_accountBindProvider` 缓存缺失时 fetch `/auth/providers` 再渲染（`_renderBindModal` 闭包 + fetch 失败降级） |
| UC14 票据 TTL | `src/user/db.py` `save_exchange_code`：`expires = now + 60`（两条 INSERT 路径一致）；`validate_exchange_code` 原子消费 DELETE 带 `expires > ?` 条件不变 |
| UC15 内联兑票 | `main.py` `_EARLY_EXCHANGE_SCRIPT`（`_inject_email_configured` 注入 `</head>` 前，覆盖 /、/en、/register、/forgot）：解析期 POST `/api/auth/exchange` + strip URL + sessionStorage flag；`app.js` `init()` flag 接力块（成功补 DOM/I18N/API，失败 toast+showLogin） |
| UC15b i18n 三层 | `oauth_exchange_failed`：zh/en locale 全量包 + `js/i18n.js` `_INLINE_PACKS`（zh+en）+ 引用页 `?v=` 递增（index 20261030-4 / forgot、register 18 / admin 20261016-3；app.js v=206） |
| UC15c 失败详情透传 | `main.py` 内联脚本 `r.json()` 解析 `detail.message/error_code` → `__oauth_exchange_failed_msg__` → app.js 接力块 `_exMsg \|\| i18n` 优先级展示 |
| UC15d popup 登录消费方 | `app.js` init 幂等注册 `oauth_result action='login'` 监听（origin+type 校验）：成功兑票+重载；失败 detail toast |
| UC16a 密钥载体 | `configs/security.json`（600）+ `env:production` 启动门禁；`start-grape.sh` 统一启动器（本机已落地，带 `JWT_SECRET` export 与门禁告警检查） |
| UC16b 分发 | 部署管道：存在性检查→scp→chmod→sha256 对账，不一致终止；密钥不入 git（仓库仅 example 模板） |
| 样式 | `src/api/static/css/auth.css` `.oauth-btn[aria-disabled]` / `.oauth-unconfigured-hint` |
| i18n key | `oauth_not_configured` / `oauth_not_configured_hint` / `oauth_view_config_help` / `oauth_needs_config` / `oauth_request_failed` / `oauth_exchange_failed`（zh/en locale + js/i18n.js 内置兜底三处同步） |
| 后端（未改，依赖现状） | `GET /api/auth/providers` 返回 `configured`；`GET /api/auth/login/{provider}` 未配置 400「{name} OAuth 未配置」 |

## 5. 测试


UC↔测试映射（UC1–UC13，源码结构断言）：

| UC | 测试函数 | 状态 |
| --- | --- | --- |
| UC1 | `test_uc1_all_providers_still_rendered` | ✅ |
| UC1 | `test_uc1_css_greyed_state` | ✅ |
| UC1 | `test_uc1_hint_function_exists_and_removes_when_all_configured` | ✅ |
| UC1 | `test_uc1_hint_links_to_help_doc` | ✅ |
| UC1 | `test_uc1_state_map_filled_from_providers` | ✅ |
| UC1 | `test_uc1_unconfigured_button_gets_aria_disabled` | ✅ |
| UC2 | `test_uc2_known_unconfigured_short_circuits` | ✅ |
| UC3 | `test_uc3_prerendered_buttons_sync_aria` | ✅ |
| UC4 | `test_uc4_no_hint_element_in_static_html` | ✅ |
| UC4 | `test_uc4_oauth_redirect_flow_untouched` | ✅ |
| UC5 | `test_uc5_catch_distinguishes_not_configured_400` | ✅ |
| UC5 | `test_uc5_unknown_state_does_not_block` | ✅ |
| UC6 | `test_uc6_other_failures_use_generic_message` | ✅ |
| UC7 | `test_uc7_all_text_escaped` | ✅ |
| UC7 | `test_uc7_hint_uses_data_i18n_attributes` | ✅ |
| UC7 | `test_uc7_keys_present_in_all_three_i18n_sources` | ✅ |
| UC7 | `test_uc7_needs_config_has_name_placeholder` | ✅ |
| UC9 | `test_uc9_forgot_page_links_to_email_guide` | ✅ |
| UC10 | `test_uc10_account_helper_uses_showtoast_link` | ✅ |
| UC10 | `test_uc10_context_keys_in_both_locales` | ✅ |
| UC10 | `test_uc10_three_entry_points_use_helper` | ✅ |
| UC11 | `test_uc11_card_key_in_both_locales` | ✅ |
| UC11 | `test_uc11_email_card_degrades_not_hides` | ✅ |
| UC12 | `test_uc12_css_greys_on_first_frame` | ✅ |
| UC12 | `test_uc12_js_sync_block_reads_injection` | ✅ |
| UC12 | `test_uc12_runtime_state_corrects_html_class` | ✅ |
| UC12 | `test_uc12_server_injects_oauth_state` | ✅ |
| UC12C | `test_uc12c_generic_key_in_all_three_i18n_sources` | ✅ |
| UC12C | `test_uc12c_js_switches_generic_to_named_key` | ✅ |
| UC12C | `test_uc12c_prerendered_hint_is_neutral` | ✅ |
| UC13 | `test_uc13_guide_writes_shared_cache` | ✅ |
| UC13 | `test_uc13_modal_fetches_when_cache_missing` | ✅ |
| UC13 | `test_uc13_modal_uses_cache_without_extra_request` | ✅ |

`tests/test_issue_535_oauth_login_hint.py`：源码检查方式（前端 JS 无法在 pytest 中执行，与 test_issue_372_round3_frontend.py 同模式），覆盖 UC1–UC8 的结构断言。浏览器实测记录见 Issue #535 评论（四条路径 + 三配置态 + 闪动时间线 + 语言切换）。

UC14/UC15（S8 兑票链路）同为源码检查 + 结构断言：UC14 锁 TTL 60s（两条 INSERT 路径）与原子消费条件；UC15 锁注入链路（`_EARLY_EXCHANGE_SCRIPT` → `</head>` 前注入、POST 端点、URL strip、flag 传递）、app.js flag 接力块、`oauth_exchange_failed` 三层 i18n 同步与 `?v=` 缓存版本递增。部署后实测（目标环境）：真实回调慢载场景兑票成功率。

UC↔测试映射（S8）：

| UC | 测试函数 | 状态 |
| --- | --- | --- |
| UC14 TTL 60s | `TestUC14ExchangeCodeTTL::test_uc14_ttl_60s_on_both_insert_paths` | ✅ |
| UC14 原子消费不变 | `TestUC14ExchangeCodeTTL::test_uc14_atomic_consume_keeps_expiry_guard` | ✅ |
| UC15 注入链路 | `TestUC15InlineExchangeScript::test_uc15_script_defined_and_injected_before_head_close` | ✅ |
| UC15 POST+strip+存 token | `TestUC15InlineExchangeScript::test_uc15_script_posts_exchange_and_strips_url` | ✅ |
| UC15 flag 传递 | `TestUC15InlineExchangeScript::test_uc15_script_sets_relay_flags` | ✅ |
| UC15 成功接力块 | `TestUC15InlineExchangeScript::test_uc15_app_js_success_relay_block` | ✅ |
| UC15 失败兜底 | `TestUC15InlineExchangeScript::test_uc15_app_js_failure_relay_block` | ✅ |
| UC15b i18n 三层 | `TestUC15InlineExchangeScript::test_uc15b_exchange_failed_key_in_all_three_i18n_sources` | ✅ |
| UC15b ?v= 递增 | `TestUC15InlineExchangeScript::test_uc15b_cache_bust_versions_bumped` | ✅ |
| UC15c 内联解析 detail | `TestUC15cFailureDetailRelay::test_uc15c_inline_script_parses_error_detail` | ✅ |
| UC15c 详情 flag 存储 | `TestUC15cFailureDetailRelay::test_uc15c_failure_msg_flag_stored` | ✅ |
| UC15c 接力块优先级 | `TestUC15cFailureDetailRelay::test_uc15c_app_js_prefers_detail_over_generic` | ✅ |
| UC15d 幂等监听注册 | `TestUC15dPopupLoginConsumer::test_uc15d_listener_registered_with_idempotent_guard` | ✅ |
| UC15d origin+type 校验 | `TestUC15dPopupLoginConsumer::test_uc15d_listener_validates_origin_and_type` | ✅ |
| UC15d 成功兑票+重载 | `TestUC15dPopupLoginConsumer::test_uc15d_success_path_redeems_and_reloads` | ✅ |
| UC15d 失败 detail toast | `TestUC15dPopupLoginConsumer::test_uc15d_failure_path_parses_detail_too` | ✅ |
| UC16c 失败兜底日志锚 | `TestUC16JwtSecretPersistence::test_uc16_dev_ephemeral_allowed_but_warned_in_log` | ✅ |
| UC16a 文件优先于环境变量 | `TestUC16JwtSecretPersistence::test_uc16_security_config_prefers_file_over_env` | ✅ |
| UC16 门禁红测（弱密钥拒启动） | `TestUC16JwtSecretPersistence::test_uc16_production_gate_rejects_weak_config` | ✅ |
| UC16 门禁绿测（强密钥通过） | `TestUC16JwtSecretPersistence::test_uc16_production_gate_accepts_strong_secret` | ✅ |
| UC16 随机兜底仅最后回退 | `TestUC16JwtSecretPersistence::test_uc16_ephemeral_fallback_is_last_resort_only` | ✅ |
| UC16 无 kid 兼容分支保留 | `TestUC16JwtSecretPersistence::test_uc16_verify_fallback_keeps_legacy_no_kid_branch` | ✅ |
| UC16b/UC16c Ops 载体（security.json/.jwt_secret/start-grape.sh） | 不入库——部署验收（spec UC16d 四格金标准），本机已实测 before/after-restart 200 | ⏳部署后验收（174/79） |
| UC16e showLogin 停轮询 | `TestUC16ePollingStopsOnKick::test_uc16e_showlogin_clears_refresh_timer` | ✅ |
| UC16e 声明前置防 TDZ | `TestUC16ePollingStopsOnKick::test_uc16e_timer_declared_before_showlogin` | ✅ |
| UC16e ?v=207 | `TestUC16ePollingStopsOnKick::test_uc16e_cache_bust_bumped` | ✅ |
| UC15f pending 同步置位 | `TestUC15fInlinePendingRace::test_uc15f_inline_script_sets_pending_synchronously` | ✅ |
| UC15f 终态双侧摘 pending | `TestUC15fInlinePendingRace::test_uc15f_pending_cleared_on_both_outcomes` | ✅ |
| UC15f 等待窗口+超时 | `TestUC15fInlinePendingRace::test_uc15f_app_js_waits_for_pending_with_timeout` | ✅ |
| UC15f ?v=208 | `TestUC15fInlinePendingRace::test_uc15f_cache_bust_v208` | ✅ |
| UC15f 单次重入守卫 | `TestUC15fReentryGuard::test_uc15f_single_reentry_guard_present` | ✅ |
| UC15f ?v=209 | `TestUC15fReentryGuard::test_uc15f_cache_bust_v209` | ✅ |
| UC15g 语言前缀保留 | `TestUC15gPopupLangPrefix::test_uc15g_redirect_preserves_en_prefix` | ✅ |
| UC15g ?v=210 | `TestUC15gPopupLangPrefix::test_uc15g_cache_bust_v210` | ✅ |
| UC15 真实回调慢载 | （非单测材料）部署后目标环境按验收清单实测 | ⏳部署后验收 |

全流程审查结论（回调→存票→重定向→首载→兑票→接力）：
- ✅ 注入覆盖完整：`/`、`/en`（经 `_spa_response`）、`/register`、`/forgot` 全走 `_inject_email_configured`；页面 no-store，回访不缓存旧 HTML。
- ✅ 响应契约一致：`POST /auth/exchange` 返回 `{token, first_login}`，内联脚本与 app.js 原分支消费同字段。
- ✅ 接力块与原分支行为逐行对齐：token 存储、`app_cache_ver`、三项缓存清理、first_login UI（sidebar/tb/mainView）、`showApp` + `API.scheduleProactiveRefresh` 全一致；`app_cache_ver '46'` 两处同源同值（一处 bump 需两处同步，属既有约定）。
- ✅ index.html 首帧 `data-logged-in` 内联检测读 URL 参数，与内联兑票共存（各自独立，无时序冲突）。
- ✅ UC15c 收尾修复：内联失败路径现与原分支 R5 同款解析后端 `detail` 并透传展示（原「只弹通用文案」降级已消除）。
- ✅ UC15d 收尾修复：popup 登录 postMessage 已有 app.js 消费方（origin+type 校验、幂等注册、成功兑票+重载、失败 detail toast）——原休眠路径已激活，无遗留。
- 已同步更新 grape_auth.py 两处「10 秒过期」陈旧注释为 60s。

S9 全流程审查（密钥生成→分发→启动→验签→轮换）：
- ✅ 家族扫描（重启轮换类）：全 src/ `secrets.token_*/urandom` 枚举——PKCE verifier、binding ticket、会话 token、bot lease 等均为「每值应随机」类（非信任根，无持久化需求）；`credential_keys` 的 dev 兜底是静态字符串（非重启轮换，无杀会话风险）。**重启轮换家族仅 JWT 签名密钥一个成员**，已修
- ✅ 门禁红绿实测：无密钥+production → validate False（拒绝启动）；强密钥 → True。本机 dev 切 production 被门禁拦（要求邮件/COOKIE_SECURE/redirect_uri 生产级配置）——门禁行为符合设计，dev 机保留 env-var 持久通道
- ✅ 双通道兼容并存：security.json kid 池与 JWT_SECRET 环境变量互不干扰（文件优先、留空回退），dev/生产可异构
- ✅ 测试窗口陷阱已修：`_get_verify_secret`/`get_credential_keys` 的断言锚点曾命中 docstring 内的词面——改为跳过 docstring 的 body 锚（此为结构性测试的通用坑，已沉淀）
- 📌 已知边界（记录不修）：`credential_keys` dev 兜底为固定测试密钥——dev 环境可接受，生产由门禁强制显式配置（env:production 时必须给真密钥）；归入 174/79 落地清单一并处理
