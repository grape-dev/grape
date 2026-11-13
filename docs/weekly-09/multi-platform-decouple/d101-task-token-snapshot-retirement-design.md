# D101：task 行凭据快照退役 + session 死列清理 方案稿

状态：评审中（用户拍板"方案二+方案三"方向，本文档细化改动点/影响面/验证计划）
日期：2026-11-08
前置：D100 边界核查（挂账 E）+ 方案比选（FR-9 加密 vs 列退役，用户选退役）

## 〇、裁定基线（已确认事实）

1. `agent_tasks.repo_gitcode_token`：任务派发时的 bot token 快照，明文落库
   （挂账 E①）。六个写入点全部经 `_get_task_repo_token`（bot 系值，
   路径 D 用户 token 恒空——写隔离已由 UC42C④ 锁定）。
2. claim 时已有双模式：非空行 → DB 过期标记复核（R21.12.1）；
   空行 → 现取注入 task dict 不落库（L1568-1570，即"方案二终态"的既有先例）。
3. `devops_agent_session.repo_gitcode_token / repo_github_token / token_status`：
   R25.9 回声治理后**全部写入方传 None**（保留语义=不写），事实死列；
   TokenResolver 第 1 级 DevopsAgentSessionToken 永远 fetch 到空串——空转策略。
4. P2 收口后 agent 进程零平台凭据（UC42C①锁），task 行 token 的唯一消费方
   是 worker 侧 git 链（get_task_token）与服务端回帖/归档链。

## 一、方案二：agent_tasks 列退役（claim 现取制）

### 1.1 核心语义变更

**前**：创建时快照落行 → claim 时复核/补注 → worker 读行。
**后**：创建时**不写行**（恒空串）→ claim 时**永远现取**注入 task dict
（不落库）→ worker 读 task dict（代码路径不变，值来源从"行"变"claim 注入"）。

worker 侧零改动：`get_task_token(task)` 优先读 task 行的逻辑保留
（行值恒空后自然落到实时获取分支——该分支本来就有完整降级链），
task dict 值由 claim 注入优先命中。

### 1.2 改动点清单（逐点）

| # | 位置 | 改动 | 风险 |
|---|---|---|---|
| W1 | event_pipeline.py:430/438 | `repo_gitcode_token = _get_task_repo_token(repo_key)` → 恒 `""`（删 resolve 调用）；行值写空串 | 低：行值消费者全走 claim 注入 |
| W2 | event_pipeline.py:530/543 | 同 W1（失败快照路径） | 低 |
| W3 | event_pipeline.py:3290/3377 | 同 W1（exec 路径） | 低 |
| W4 | vod_handler_async.py:288-303 | `_queue_repo_token` 恒空串；resolve 调用改用 **`resolve_config_level()`**（校验语义钉死，评审 v2）：只取 `.level` 判 `=="user"`（R20.17.3 空短路启动守卫），**禁止消费 `.token` 字段**——resolve_config_level 走 `_fetch`/`_fetch_stored` 纯读（不检查过期、不触发 OAuth refresh），token 只经 claim 注入 task dict 一条路径 | 中：校验/传输双语义拆分，校验侧零 token 暴露 |
| W5 | scheduler.py:485 | autopilot 同 W4 语义：传输值恒 ""，校验改 `resolve_config_level().level`（禁消费 .token） | 中：同 W4 |
| W6 | events.py:1808 | event-dialogue 路径 bot_token 参数 → enqueue 内部改 ""（bot_token 形参保留=API 兼容） | 低 |
| W7 | runtime/service.py claim L1542-1557 | **删除** R21.12.1 过期复核段。评审澄清（v2）：该段条件=`行值非空`，**只对存量旧行生效**——但其所做之事（发现过期→重取→覆写）与 W8 无条件现取**功能完全重叠**（get_token L770 自带过期短路，现取天然跳过过期值）。删除前提=职责被 W8 吸收：现取制对存量旧行同样生效（旧行值被忽略、claim 现取新鲜值），**旧行兜底不降级反升**。P2 兜底语义由此段迁移至 W8 承担 | 中：删除正确性依赖 W8 落地，两改动必须同 commit |
| W8 | runtime/service.py claim L1567-1570 | 保留并升级为**唯一注入点**：无条件现取 `_get_task_token(task)`（含 401 刷新链）。**失败语义（评审 v2 补钉）**：resolve 异常与路径 D 空值在 `_get_task_token` 内都返回空串——claim 注入失败**不阻断派发**（保持现有 worker 行为，不引入新派发失败模式），但必须打 `claim_token_resolve_failed` 结构化日志；空值暴露点由 W12 新语义前置拦截（执行前检查代替 git 401 事后暴露）。路径 D（user-level）空值属正常语义非失败，不告警 | 中：性能+一次 resolve/claim（DB 级，毫秒）；语义=现取制 |
| W9 | runtime/service.py claim_by_tag L1973-1975 | 同 W8（无条件现取） | 中 |
| W10 | events.py:710/1229 重派快照 | `old.get("repo_gitcode_token")` → `_get_task_repo_token(old.get("repo_key"))` 现取（重派天然新鲜，顺带修快照陈旧） | 中 |
| W11 | user/db.py get_task_token L2020 | 保留（行值恒空自然走分支）+ 注释更新"行值已退役，保留兼容存量库" | 低 |
| W12 | dependency_check.py:55-65 | **检查语义钉死（评审 v2）**：`repo_key 在册 且 resolve_config_level(repo_key).level ∉ {"", "user"}`——只读配置级别**不实际解密 token 值**。resolve 异常/空 level 时**跳过检查项**（不判失败——检查器自身故障不阻断任务，空值由 claim 现取自然暴露）；user level 时记 warning 不拦截（路径 B 任务本就靠 claim 现取注入，非 git agent 失败前兆）。误报面由此收窄到"repo 在册但完全无 bot 配置"单一形态 | 中：误报风险已收敛，实测前置检查触发频率后可再调 |
| W13 | db_setup.py agent_tasks DDL + service.py _TASK_COLUMNS | **列保留**（SQLite 删列需重建表，收益低），列注释改"退役：恒空，claim 现取（D101）"；_TASK_COLUMNS 保留列名（SELECT 兼容存量库） | 低 |
| W14 | 挂账 E① | 状态更新：明文不存在（列恒空），FR-9 范围收窄到 env_extra 列 | — |

### 1.3 保留不变式

- 路径 D 写隔离：现取制的 `_get_task_token` 内部含 `_get_task_repo_token`
  的 user-level 空语义 → 用户 token 依旧不落 dict/不落行（UC42C④ 断言不动）
- UC42C④"现状锚点"用例：翻转——`_get_task_repo_token` 的**写入调用点**
  断言从"无加密调用"改为"调用点归零"（grep 级守卫：enqueue 写行参数恒 ""）
- prompt_builder 的 grape CLI 禁令（L265）不动——agent 依旧不持有平台凭据，
  task dict 里的 token 只到 repogit/git 链，不进 agent env

### 1.4 已知代价

- claim 每任务 +1 次 resolve（原空行路径本来就有，非空行路径新增；DB 查询
  毫秒级，claim 频率低——可接受）
- 存量库旧行的非空 token：读取路径仍在（get_task_token 兼容分支），随任务
  终态自然消亡；不做清空迁移（一迁移一语义，且旧行本就有 claim 复核）

## 二、方案三：session 死列 + 策略链第 1 级清理

### 2.1 事实基线

- 三列死数据：devops_agent_session.{repo_gitcode_token, repo_github_token,
  token_status}——R25.9 后零非 None 写入（VOD/sessions/agents/scenes 全查证）
- 第 1 级策略 DevopsAgentSessionToken 恒空串 → 每次resolve空转一次 DB 查询
- 现存读取方：TokenResolver 第 1 级、agents.py:201（admin 面板列派生展示）、
  db.py:2804（解绑清理段——清一个永远为空的列）

### 2.2 改动点清单

| # | 位置 | 改动 | 风险 |
|---|---|---|---|
| T1 | token_resolver.py:1191-1196 | 策略链四改三（删 DevopsAgentSessionToken）+ 链序注释更新（架构常量变更，本方案稿=契约评审载体） | 中：链序=架构常量（D90-N2②），本节即评审记录 |
| T2 | token_resolver.py:816-876 | DevopsAgentSessionToken 类删除（含 _is_expired/mark_expired） | 低：mock 它的测试同步迁移 |
| T3 | tests：test_issue_372_user_token/round10_audit/test_health_check_r17 | 第 1 级 mock 改为链三级的直接断言；level=="session" 断言族删除 | 中：用例数变化需逐条过 |
| T4 | agents.py:194-201 | admin 面板 token 列派生展示段删除（列不存在了） | 低 |
| T5 | db.py:2801-2807 | 解绑清理段删除（清死列无意义） | 低 |
| T6 | session_store.py upsert | repo_gitcode_token 形参+UPDATE/INSERT 段删除（所有调用方已传 None，形参删除零调用方改动） | 低 |
| T7 | db_setup.py | DDL 三列保留（同方案二 W13 理由）+ R16.5.3 ALTER 迁移壳化（no-op 化——版本号保留防盖章断链，函数体清空+注释）；_AGENT_SESSIONS_DDL 列注释"退役 D101" | 低 |
| T8 | error_code "SESSION_TOKEN_EXPIRED" | **已拍板 b 案（评审 v2，对外行为不留实施期）**：`detect_and_mark_expired` 整函数退役，消费方（vod_handler:428 / handler_async:1991，401 归因提示用）改为直接调 TokenResolver RepoBot 级过期检测（`RepoBotToken._is_expired` + mark_expired 语义）或读 token_status；service.py:24 文案映射表 "SESSION_TOKEN_EXPIRED" 行删除。理由：第 1 级删除后"session 级过期"概念消亡，保留改名版（REPO_TOKEN_EXPIRED）只会制造"还有 session 级判定"的假象；"RepoBot 级过期"区分需求由 token_status/status key 直接承载，无需 error_code 包一层 | 中：对外错误文案变化，消费方两处 + 映射表一处同步改 |
| T9 | platform_card.py:154 session_token_field | 卡字段保留（声明面），值不再被策略链消费；注释标注退役 | 低 |
| T10 | 挂账/V13 登记 | credential-inventory V13 行退役标注 | — |

### 2.3 与 S118 守卫的关系（评审焦点）

S118 AST 守卫锁的是 RepoBot/GlobalBot 的 _is_path_b 跳过 + UserToken 的
host_user_id 短路——**不锁第 1 级存在性**（grep 实锤 test_s118 零
DevopsAgentSessionToken 引用）。删第 1 级不触守卫，但链序注释是
"架构常量须走契约评审"——本方案稿 §二即评审记录，需用户在本文件上确认。

### 2.4 收益

- resolve 每调用 -1 次空转 DB 查询（高频路径：每次任务执行×多策略命中）
- 三张假象清除：策略链第 1 级"存在但不工作"、session 表"有 token 列"、
  "agent 专属 token"概念（V13 退役——它从未有真实配置入口，R25.9 已证）
- 概念收敛：四级→三级，与 D91 路径分档文档口径完全对齐

## 三、实施顺序与验证计划

1. **先方案二**（task 行，安全修复属性强）：W1-W14 一次分支提交；
   回归面=claim/dispatch/redispatch/VOD/autopilot/dialogue 七链 +
   UC42C④ 翻转 + n3/n4/s120/s126/s127
2. **后方案三**（策略链，架构简化属性）：T1-T10 独立提交；
   回归面=token_resolver 全族（372/round10/r17/s118/s119）+ worker 链
3. 每步：改前 RED 断言（现行为锁）→ 改 → 绿 → 宽域回归 → 用户确认 → push

## 四、评审结论（v2，2026-11-08 外部评审五点全部采纳）

| # | 评审意见 | 处置 |
|---|---|---|
| 1 | W7 删除与 P2 兜底冲突（复核段只对存量旧行生效） | **成立**——W7 删除前提改为"职责被 W8 吸收"（现取的 get_token 自带过期短路，旧行兜底不降级反升）；W7/W8 必须同 commit |
| 2 | W8/W9 失败语义未定义 | **成立**——claim 注入失败不阻断派发（保持现行为），claim_token_resolve_failed 结构化日志 + W12 前置拦截；路径 D 空值非失败 |
| 3 | 校验用 resolve 不得带出 token 值 | **成立**——W4/W5 钉死 `resolve_config_level().level` 只读校验，禁消费 .token；传输路径唯一化=claim 注入 |
| 4 | W12 判定依据模糊 | **成立**——钉死 `level ∉ {"", "user"}` + 异常跳过不阻断 + user level 只 warning；误报面收敛到"在册但无 bot 配置"单一形态 |
| 5 | T8 现在拍不留实施期 | **成立**——拍 b 案：函数退役，消费方直调 RepoBot 级检测/读 token_status，映射表删行 |

**P1-P4 确认点全部闭合**：P1=校验/传输拆分采纳（第 3 点）；P2=兜底语义迁移 W8（第 1 点，非"依赖复核"原表述）；P3=b 案现拍（第 5 点）；P4=两步各一 commit 无异议。

**实施放行**：方案二可开工（W1-W14，RED 先行）；方案三待方案二宽域回归通过后依次实施。
