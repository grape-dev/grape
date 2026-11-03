# Design: A2A 链路巡检器（拉模型兜底）

> 需求: docs/weekly-09/a2a-chain-patrol-requirements.md
> 基线: origin/weekly-09 @ caded1c4c | 状态: 评审中
> 分阶段: Phase 1 只读检测（本设计落地范围）；Phase 2 自动补偿（默认关）；Phase 3 僵尸终态化（不做）

## 一、决策表

| # | 决策点 | 选项 | 选择 | 理由 |
|---|--------|------|------|------|
| D1 | 巡检载体 | a) 独立进程/容器 b) 主进程 asyncio 循环 c) 系统 cron 调 API | **b** | 仿 src/runtime/scheduler.py 既有范式（60s asyncio 循环、指数退避、_RUNNING_TASKS 内存互斥），零新增部署面；a/c 引入新故障面（进程管理、认证、网络），用户否决新基建 |
| D2 | 载体进程 | :3000 主进程 vs :8080 webhook 进程 vs 都跑 | **仅 :3000 主进程** | scheduler.py 已证明主进程可承载长循环；webhook 进程职责单一不该背巡检。多实例/集群下仍需 D3 |
| D3 | 多实例选主 | a) DB 谓词原子抢锁 b) get/set 心跳锁 c) 配置指定 | **a（硬性前置；实现形态经评审三轮修正）** | 集群双机双主是真实形态（issue-444、MySQL RDS 共库），且 `db_lock` 是进程内 threading 锁跨机无效、get/set_meta_value 是 check-then-act 两步（set 为 INSERT OR REPLACE 会互相覆盖）→ 心跳锁有双主窗口。实现（patrol.py claim_patrol_leader，value 格式 `holder\|aware-iso`）：**①自续约** `UPDATE meta SET value=? WHERE key=? AND substr(value,1,len+1)=?`（holder 前缀精确匹配=自家持有，原子续——修正自续约失效缺陷；**评审四轮修正：不用 LIKE**——holder 含 `_`/`%` 时 LIKE 单字通配符让仅差一个字符的主机名互开对方的锁造成双主扫描，substr 精确前缀比较在 SQLite/MySQL 语义一致）；**②CAS 接管** 读现值判过期（`_leader_expired`：holder\|ts 取时间戳段比较，兼容历史纯 ISO 值）后 `UPDATE ... WHERE value=<读到的旧值>`（双主并发只有一方 rowcount=1）；**③引导期** 行不存在 → INSERT OR IGNORE（良性竞态，db_setup.py:4158 先例同款）。过期窗 = 2×周期（时钟漂移余量）。单条 UPDATE 在 SQLite/MySQL 都行级原子，无双主窗口 |
| D4 | 检测窗口判定期 | 固定 10min vs 可配 | **可配 meta `patrol:confirm_minutes`，默认 10（二轮修正原 30；决策变更 2026-11-02：env → DB meta 真源，每周期重读改即生效）** | 初判 30 是纯观测侧妥协；a2a 端点代码实证（webhook.py:617-621 #329 场景活跃去重 + :1031-1045/:1344-1353 幂等键前置与竞态双保险）后重估：误发最坏结果被端点防重降级为"一次被拒调用+日志"。唯一残余伤害路径=#329 只拦 running/dispatched、**已终态事件不阻断新交接**（Issue #587，下游恰好跑完时误补会真唤起新一轮）——但下游事件若已落库（任意状态）L1 即查得到，终检（D11②）把该路径暴露窗压到秒级竞态。三层判定+终检下 10min 窗增量风险≈秒级竞态，换恢复速度提升 3 倍，成立。注意：防重以"补发必须走原生 /api/a2a 链路"为前提（直写库=绕过防重，D5 已约束） |
| D5 | 补偿调用方式 | 重建 a2a payload 直调内部函数 vs 复用 a2a-redo 端点函数 | **直调原生 a2a_endpoint（实现落地于 D20 DispatchHandler 适配器）**——比原案更靠底层一档：不走 a2a-redo 端点包装，直接 `await a2a_endpoint(None, body, user)`（与手工端点共用同一端点函数），端点三重防重全量生效；HTTP 自调引入认证与网络故障面，已否。**硬约束（已决策+落地）：compensated_by 标记必进 context 字符串（a2a_endpoint 组装事件只取 context，标记在载荷层不落事件）——patrol 侧 `compensated_by=patrol:{task}:{ts}`，手工端点侧 `compensated_by=manual:{username}`（D5 评审修正③），两路径从事件层永久可区分** |；**决策 A（2026-11-02）：幂等键收敛为规范交棒键 handoff:{task_id}:{target_agent}（单源派生自 patrol.handoff_idempotency_key，skill 守门件未来同式）——迟到原交棒撞同键被端点幂等命中，残余竞态降级为良性先到先得；skill 侧落地前行为等价、落地后窗口闭合**
| D6 | next_step 读取 | 解析 result_text JSON vs 新增列 | **解析 JSON，不加列** | agent_tasks 无 next_step 列（PRAGMA 实测）；加列要迁移+双写，改动面大。JSON 解析一次取 conclusion/next_step 双字段即可 |
| D7 | 巡检发现落库 | 新表 vs 复用 events | **新表 `grape_t_patrol_findings`** | 巡检发现不是平台事件（不能进事件管线，否则被自己下轮扫描看见——R7 风暴）；events 表 schema（payload/actor）不适配清单语义。新表小且只增 |
| D8 | 告警通道 | bot 评论 vs 告警表+前端清单 | **后者** | bot 评论走事件管线会被 self_loop 拦（断点2 的机制不能绕）；issue 明确否决放行。告警=grape_t_patrol_findings 状态位 + 前端管理页只读清单（Phase 1 最小版：仅 API） |
| D9 | 巡检周期 | 1/5/10 min | **5min 可配 meta `patrol:interval_seconds`（决策变更 2026-11-02：env → DB meta 真源，每周期重读，含选主 TTL 派生）** | 断链本来小时级才人肉发现，5min 已是数量级提升；扫描成本可控（见 §6） |
| D10 | 僵尸任务处置 | Phase 1 就置终态 vs 只告警 | **只告警**（Phase 3 再议） | 阈值拍太松=继续挂着，太紧=误杀长任务；先收集真实 running 时长分布（grape_t_patrol_findings 记 duration），数据到了再定终态化，零危害优先 |
| D11 | 断链判定的层次 | a) 纯时间窗（L1 无记录+窗满即补） b) 三层判定 L1→L2→L3 + 补偿前终检 | **b（已决策：负责人提出，时间窗是最后一层兜底而非主判定）** | "查不到"混合三种成因（真漏发/入库慢/写失败），库内同刻不可区分，但 L2 查下游可前置排除大半误判：同 issue/同场景管线节点下 created_at 晚于本任务 completed_at 的**任意状态**任务（含 completed/failed，与 L1 同一全状态口径——#587 的"已终态不阻断"盲区不能只堵 L1 不堵 L2，否则误判换位）= 交棒已发生的正向证据。两条护栏：① L2 必须带关联性约束（时间晚于+同管线节点）——下游忙可能是上一轮/PR 订阅/人工重派任务，无关联的下游任务不算证据，否则误报换成漏报；② 补偿前强制终检（状态流转抢占成功后再查 L1+L2，**且 L1 查全状态而非只查 running**——#329 防重只拦 running/dispatched、已终态事件不阻断新交接[Issue #587]，下游已完成是误补唯一残余伤害路径，L1 终检查全状态后该路径暴露窗收敛到秒级竞态；有任一证据即置 false_positive 放弃）。根治方案（交棒接口同步确认返回 seq，at-most-once→有回执）方向正确但属平台契约级改动管不了存量 agent，明确范围外，skill 守门件已在 skill 层先行。**端点自带防重（#329 场景级去重 + 同 agent 去重 + 幂等键前置/竞态双保险，webhook.py:617-621/:1287-1298/:1031-1045/:1344-1353）是本设计的安全网前提：巡检补发必须走原生 /api/a2a 链路（D5 直调复用同路径），任何直写库都绕过防重，禁止** |；**评审五轮：终检前置 issue 终态守卫（issue_terminal 谓词——completed/pending_review/cancelled 即 false_positive，端点只拦 cancelled 巡检自守）**
| D12 | 僵尸判定 | a) 纯时间窗（running 2h / queued 24h） b) 证据化两档/确定性归类 | **b（正向证据优先于时间窗，与 D11 同一方法论）** | agent_tasks 已有单信号仪器列：`stream_ended`（worker 隐式，引擎流结束即置位，event_pipeline.py:4225"引擎流结束即终态触发点"）+ `self_reported`（agent 显式自报，service.py:2472 条件置位防重放）。**running 两档**：`stream_ended=1 AND status='running'`=终信号已到状态未流转，确定性高置信即时告警（引擎不会再有输出，Phase 3 终态化候选，误杀≈0）；无信号+2h=低置信登记（进程崩得连信号都没发）。**queued 确定性归类**：引擎健康判定现成且**分形态**（engine_select.py:103-118：远端 credential_id>0 = status=online+last_seen_at 新鲜[_RUNTIME_STALE_SECONDS=100s]；**本机 cid=0 = installed+healthy，无心跳语义**——判定必须复用该分形态逻辑，照搬远端心跳口径会把本机引擎的 queued 任务误判"永不调度"）+ 场景存在性（scenes 路由 404 语义）→ 远端不可达/场景不存在 = 确定性"永不调度"即时归类（issue A 类 17 条全中此判定）；24h 只兜引擎在线但不调度的边界。附带收益：信号到达时刻 vs 状态流转时刻的差值即 Phase 3 需要的真实时长分布数据源（D10 的数据前置落地） |
| D13 | 停摆链判定 | a) 独立时间窗（stage 非终态+无活跃任务超 N 分钟） b) broken_chain 的 issue 视图 | **b（同一证据链双视图）** | 停摆最常见根因就是断链：链停在 coding 位 = coding 任务 completed 后无人接棒。stalled_issue 不另设时间窗，直接复用 L1/L2 判定：stage 停在的节点任务 completed 后无后继 → 同一 finding 的两种聚合（任务视角=broken_chain，issue 视图=stalled_issue），同轮同源可对账。收益：独立 N 分钟窗不用拍脑袋、两个 finding 不互相矛盾、存量核对一套证据出两份视图 |
| D14 | 补偿调用身份 | a) 一律用户 JWT b) 一律 bot token c) 身份跟随托管模式 | **c（已决策二轮修正：负责人指出 bot token 之外还有仓库 token / 用户 PAT 驱动场景）** | 原则：身份 = 该仓库**当前托管凭据对应的主体**（managed_repos），不发明新身份、不冒用无关个人。三分支：① 路径 B 用户 PAT 托管（host_user_id>0，Issue #372）→ 用该托管用户的平台身份——PAT 由平台为该仓持有，配置即授权，这是仓库运维主体而非冒用；② 路径 A bot token 托管 → bot token 服务端主体（直调形态若 jwt 分支需用户主体，按 host_user_id→托管用户映射回落）；③ 无任何托管主体（已解除托管/凭据轮换干净）→ 降级挂起+告警（finding 记 auth_unavailable）。实现锚点：a2a-redo 直调形态 `await a2a_endpoint(None, body, user_dict)`（events.py:1482，request=None 走 jwt 分支），巡检构造 user dict 按上述分支取主体。审计双字段正交：**身份回答"谁配的仓在动"，`compensated_by=patrol`+原任务 id+巡检时间回答"是人还是机器"**——机器补偿/人工重派靠标记区分，与通道身份无关 |
| D15 | 补偿 payload 模式字段 | a) 只发基本字段 b) 模式跟随原交棒从库恢复 | **b（跟随既有模式，与 D14 同族）** | a2a 事件带模式字段：is_subtask_mode / parent_issue / carried_context / engine_config（webhook.py:1340）。原交棒没发生（断链定义），但模式信息全部持久化在库：carried_context 在 issue 表（session_store.py:261 get_carried_context）、is_subtask_mode+parent_issue_id 在 issue_state（:78/:321）——补偿前从这两处恢复模式字段，恢复完整才补；不可恢复（如 is_subtask_mode=1 但 parent_issue 缺失）→ 挂起人工记 mode_unrecoverable，不猜（守"只补确定性缺失"）。子任务链丢 parent_issue 会让下一棒脱离父任务上下文跑偏，违反等价性标准 |
| D16 | findings 归属人 | a) 无归属（清单落库了事） b) 按托管模式落归属字段 | **b（A 类根因修复；实现落点评审十四轮修正①/S26：`_resolve_owner` 双源——host_user_id 优先（路径 B），无值回落 user_repos 管理员映射（路径 A，与 IdentityHandler.ManagedUserFallbackStrategy 同口径），双源皆空=0）** | issue A 类 17 条挂两月无人知晓的根因=告警没有归属人。grape_t_patrol_findings 落库时带 owner 字段，解析逻辑与 D14 同源（路径 B=host_user_id 用户；路径 A=托管用户映射；无主体=unowned 标记并清单置顶——unowned 是"无人知晓"高危项）。Phase 1 仍 API-only 不出通知通道，但归属字段先行，后续邮件/IM 通知按此路由，避免字段缺失导致通知层再改 schema |
| D17 | 判定范围与扫描模式 | a) 一刀切全量候选+固定窗口 b) 范围跟随链路模式 + 两档 LOOKBACK | **b（模式跟随+存量口径补全）** | ① **候选范围跟随链路模式**：PR 订阅链（issue_id `#pr_` 前缀+scene_id）被 a2a 端点设计性拦截（只审查不流转，events.py:1240），本无交棒语义——不排除则 PR 订阅任务的 next_step=a2a* 全是纯误报，污染误报=0 硬线；子任务链 L2 关联键=parent_issue+agent_type（主链同 issue 任务不构成其正向证据，防误配）。② **两档 LOOKBACK**：存量核对模式（Phase 1 人工核对期，覆盖全历史/60 条最老完成时间）与常规模式（运行窗如 7 天）分档；**部署实测注记（2026-11-02）：#463 存储清理每 6h 将 7 天以上 completed 任务的 result_text null 化——next_step 自报的可检测视界天然 = 7 天，runtime 窗与其咬合；更老任务不可判定即不报（不猜原则）**——固定近窗则"存量 60 条全量发现"验收是空话；切换不改判定逻辑仅窗口参数，历史 finding 靠唯一约束不重插 |
| D18 | 僵尸终态化通道 | a) 巡检自己写终态 b) 复用 scheduler #537 唯一通道 | **b（防双通道语义漂移）** | 仓库已存在时间窗终态化机制：scheduler 的 PENDING_SELF_REPORT_TIMEOUT（scheduler.py:30-32，1800s 兜底置 timeout_failed）。边界：**终态化永远走 #537 通道**（复用/扩展），巡检只做发现源（stream_ended 高置信证据喂给它，未来可作 #537 的加速信号）；patrol 模块零 `UPDATE agent_tasks SET status` 终态写路径（结构断言守卫）。两条终态化路径并存 = 两个时间窗各自为政，语义漂移只是时间问题 |
| D19 | 补偿开关语义 | a) env 常量+重启生效 b) meta 动态开关+仓库白名单 | **决策变更 2026-11-02（负责人拍板）：补偿默认开+黑名单紧急控制** | ① 巡检功能本身默认启用（无总开关，main lifespan 挂载即跑）；② 补偿默认开——能力即开即用（原"默认关+白名单空=零放行"被否：逐仓白名单规模化后无人维护=功能空转）；③ 运行期紧急控制走 meta（每周期重读，改即生效无需重启）：总开关 `patrol:compensate_enabled`（一键全关；非法值按关+warning，fail-fast）；黑名单 `patrol:compensate_blocklist`（逗号分隔 repo_key，空=不拦全仓生效，`*`=全拦紧急刹车）；④ 防误发的真守卫=责任链终检（L1/L2 全状态复查）+幂等键+finding 状态原子抢占（结构性），非入口闸门——黑名单仅为运维摘除通道 |
| D20 | 补偿实现结构 | a) 单体大函数（patrol.py 里写完补偿） b) 责任链+策略拆分独立模块 | **b（已决策：负责人要求抽象、禁止单一对象过大）** | 补偿链路=责任链（Chain of Responsibility）：六个处理器顺序编排——开关门卫→状态流转抢占→终检（L1+L2 全状态）→身份解析→模式恢复→A2A 派发，任一处理器返回"拒绝"即短路落状态返回，绝不继续；每个处理器=单一职责小对象（可单测、可重排、Phase 3 加处理器零改动既有）。身份解析=策略族（Strategy）：路径 B 用户 PAT / 路径 A 托管用户映射 / 无主体降级三策略按 managed_repos 状态选择；模式恢复=策略族（普通链 / 子任务链）。派发=适配器：复用 webhook.a2a_endpoint 直调形态，compensated_by 标记为必传构造参数。模块划分：`patrol.py`（检测职责，不动）/ `patrol_compensate.py`（补偿动作，责任链编排+处理器+策略）/ 测试各归其位。禁止把检测与补偿耦在同一对象——巡检器本体保持"观察者"，补偿器独立成"执行者"，职责分离即最坏伤害半径的分界线 |

| D21 | findings 保留策略（实现后审计落地） | a) 永不清理 b) 终态行 TTL 随轮清理 c) 独立清理任务 | **b** | 审计证实表只增不减=无界增长（本机 45G 日志撑爆根分区同款模式）；永不清理在长周期运行下必然爆表；独立任务引入新调度面。TTL 只碰四终态（dismissed/false_positive/compensated/permanent_failure——不再驱动任何动作），open/claiming/needs_human 永不清理（活性队列/未决人工信号，删=丢线索）；默认 90 天走 meta `patrol:findings_retention_days`（0=显式永不清理=保留全量审计轨迹的紧急语义；坏值回落默认，口径同 _meta_int）；随 run_patrol_once 每轮执行零新调度面，purged 入回执可对账 |
| D22 | 手动触发形态（实现后审计落地） | a) 端点内跑补偿 b) 端点只做检测面 c) 不做手动触发 | **b** | 运营介入面补口（审计：API 只有 list+dismiss，控制手段只剩改 meta 开关——粗粒度）。刻意只做检测面：不补偿（补偿走选主门控自动循环，手动触发补偿=绕过 D3 总闸的多机双发面）、不抢主（扫描幂等——findings 唯一约束与自动轮汇合，非主实例手动扫零副作用）。端点 POST /api/patrol/scan（user_only，同 findings 口径），异常 500 收口 |
| D23 | 扫描执行线程（实现后审计落地） | a) 主循环裸调 b) asyncio.to_thread 卸载 c) 行锁事务根治 | **b** | run_patrol_once 持 db_lock 同步全表扫描，裸调卡住整个事件循环（任务量增长后每 5 分钟 API 毛刺）；to_thread 后 API 不被整轮冻结，扫描语义不变（首轮即时/选主门控/顺序约束保持）。c 登记 S33 数据触发项（共享单连接事务跨 await 挂 DB 车道）不做；DB 车道竞争残留已知 |

| D24 | findings 清单可见口径（全链路审计） | a) 全量可见（管理页原意） b) admin 全量+非 admin 归属过滤 | **b（方案 A 拍板）** | 读口子与 dismiss 写口子同口径锁（S30 对称）：此前任意登录用户可见全平台 repo_key/issue_id/detail=BOLA 面。admin 全量保留管理页能力（unowned 置顶不变）；非 admin WHERE owner=?；unowned（owner=0）仅 admin——高危"无人知晓"项不向普通用户敞开。admin 台账查询异常 fail-safe 降级为归属过滤（宁可少看不可多看） |
| D25 | 手动轮并发闸（全链路审计） | a) 无闸 b) single-flight 在跑标志 c) 队列排队 | **b** | 自动轮有选主总闸（D3），手动轮无对称闸=N 并发全表扫描互踩 db_lock（audit 模式 HTTP 还无限挂）。排队（c）收益低——手动轮本就是核对型操作，409 让调用方稍后重试语义更诚实；跨实例手动轮靠 findings 唯一约束幂等汇合，进程内单闸覆盖唯一并发形态；异常 finally 释放防闸死锁 |

| D26 | single-flight 闸层级（闸收口） | a) 路由层标志（第 15 轮形态） b) 函数级线程安全闸（全入口） c) DB 级锁 | **b** | 路由层快检只挡手动轮对手动轮——自动轮（选主循环 to_thread 中）×手动轮并发时快检未命中，两轮全表扫描互踩 db_lock。闸下沉到 run_patrol_once（threading.Lock）：所有入口共用、线程安全、异常 finally 释放；语义分流：自动轮捕获 PatrolScanInProgress 记 skip 不退避（退避人为拉长周期），手动轮映射 409。c（DB 锁）引入跨实例协调面，单实例内并发是唯一形态，不做 |

## 二、架构与数据流

```
:3000 主进程启动
  └─ start_patrol_loop()（asyncio task，仿 scheduler.py）
       每周期（meta patrol:interval_seconds，默认 300s，每周期重读）：
       1. 谓词 UPDATE 原子选主（D3，rowcount=1 才是主）→ 非主直接 skip 整轮
          （评审十四轮修正②/S27：选主是总闸——claiming 孤儿回收、补偿批处理、
          扫描全部在选主判定之后执行；原实现回收/补偿先于选主，多机部署下
          每台都跑补偿批处理，与"非主 skip"设计意图不符。防双发仍由
          ClaimHandler rowcount + 幂等键结构性兜底，本修治重复扫描/日志/DB 压力）
          → 主实例：先无条件 reclaim（S23：租约回收是卫生操作，不受补偿开关
            门控，仅被选主门控）→ 开关开则 compensate_open_findings → 扫描
       2. 扫描（三条纯 SELECT，见 §3）
       3. 三层判定（D11，纯函数：L1 事件表全状态反查 → L2 下游关联活跃任务 → 均空且超窗才出 finding）
       4. Phase 1：INSERT grape_t_patrol_findings（INSERT OR IGNORE，唯一约束去重，不改既有表）
          Phase 2（D19 决策变更：补偿默认开+黑名单紧急控制，D20 责任链，patrol_compensate.py 独立执行者模块）：
            → ① GateHandler：开关+黑名单门卫（meta 紧急关/仓在黑名单 → 零动作返回；默认全放行）
            → ② ClaimHandler：状态原子流转抢占（UPDATE ... SET status='claiming' WHERE id=? AND status='open'，rowcount=1 才继续）
            → ③ RecheckHandler：终检，再查 L1（全状态）+L2，任一有证据 → 置 false_positive 放弃
            → ④ IdentityHandler：身份策略族（路径 B host_user_id → 路径 A user_repos 映射回落 → 无主体 auth_unavailable 降级，绝不冒用）
            → ⑤ ModeHandler：模式恢复策略（子任务链恢复 parent/carried_context；不可恢复 → mode_unrecoverable 挂起）
            → ⑥ DispatchHandler：派发适配器直调原生 a2a_endpoint（compensated_by=patrol:{task}:{ts} 标记进 context 落事件、
              idempotency_key=规范交棒键 handoff:{task_id}:{target_agent}（决策 A 2026-11-02，单源派生；追溯由 action_meta+事件 compensated_by 承载））
            任一处理器拒绝即短路落状态返回，绝不继续。
       5. 全程 try/except + 退避（仿 scheduler _BASE_RETRY_DELAY），异常绝不外溢
```

扫描 SQL（按现有列索引可走；agent_tasks.status 无索引则补一条 CREATE INDEX——见 §5 性能）：

1. 断链候选（L1+L2）：`SELECT ... FROM agent_tasks WHERE status='completed' AND completed_at > now - LOOKBACK（meta lookback_mode 分档）`（**两档 LOOKBACK，D17：存量核对模式覆盖全历史，常规模式运行窗**）→ Python 解析 result_text JSON 过滤 next_step LIKE 'a2a%'（**排除 PR 订阅链：issue_id LIKE '#pr_%' 直接跳出，D17**），再两步反查：**L1** events 按 (issue_id, a2a_event) 查（**全状态含 completed**，不看 running）；**L2** agent_tasks 按 (issue_id, 场景管线下一节点) 查**任意状态**任务（含 completed/failed）且 created_at > 本任务 completed_at（关联性约束 + 全状态口径，D11①；**子任务链关联键换 parent_issue+agent_type，D17**）。两步均无证据 + 超窗（confirm 窗，meta patrol:confirm_minutes）→ finding。
2. 僵尸（D12）：running 两档——`status='running' AND stream_ended=1`（确定性高置信，即时）∪ `status='running' AND started_at < now-2h AND stream_ended=0`（低置信登记）；queued 确定性——`status='queued'` 且（**分形态引擎不可达**：远端 credential_id>0 看 status/last_seen_at，本机 cid=0 看 installed+healthy[复用 engine_select 健康判定] **或 场景不存在**：issue_state.scene_id 经 get_pipeline_config 四源解析后 `fallback=true`——注意 is None 判定不成立，不存在场景回退 default 带 fallback 标记，函数文档约定调用方按 fallback 拒绝静默回退）即时归类"永不调度"（detail 记 cause），`created_at < now-24h` 仅兜引擎在线边界。
3. 停摆链（D13）：非独立扫描——由断链判定结果按 issue 聚合派生（stage 停在的节点 = broken_chain 任务的节点），同轮同证据，无独立时间窗 SQL。

**结构性无风暴（R7）论证**：巡检只读两张表+写 grape_t_patrol_findings；Phase 2 补发产生的新 a2a_event 其关联任务的 completed_at 必然早于补偿时刻 ≥ confirm 窗（meta patrol:confirm_minutes），扫描窗口 `completed_at > now - LOOKBACK（meta lookback_mode 分档）` 且判定窗 `now - completed_at > confirm 窗` 与补发产物（新事件 created_at ≈ now）天然错开——补发动作不满足"completed 且超窗无后继"判据，反馈回路在数据谓词层闭合，非口头保证。

## 三、新表 DDL（grape_t 常规迁移，仿 db_setup.py 既有模式）

```sql
CREATE TABLE IF NOT EXISTS grape_t_patrol_findings (
  id            TEXT PRIMARY KEY,              -- uuid（硬性要求：新表主键一律 UUID，Python 侧生成，禁自增）
  kind          TEXT NOT NULL,
  task_id       TEXT NOT NULL DEFAULT '',     -- agent_tasks.id（合成键用空串不用 NULL；引用 UUID 主键）
  event_seq     INTEGER NOT NULL DEFAULT 0,   -- 关联 events.seq（合成键用 0 不用 NULL）
  repo_key      TEXT, issue_id TEXT,
  owner         INTEGER NOT NULL DEFAULT 0,     -- 归属人 user_id（D16：路径 B=host_user_id；路径 A=托管用户映射；0=unowned 置顶）
  detail        TEXT,                     -- 判定输入快照（next_step/时长/双源结果）
  suggestion    TEXT,                     -- 建议处置（Phase1 只写建议不执行）
  status        TEXT NOT NULL DEFAULT 'open',  -- open | claiming | compensated | dismissed | false_positive
  action_meta   TEXT DEFAULT '',          -- 补偿回执（D5 调用结果/幂等键）
  created_at    TEXT, updated_at TEXT,
  UNIQUE(kind, task_id, issue_id, event_seq)  -- S18：issue_id 进键（issue 级 finding task_id 恒空串，三列键会吞行；迁移 202611020002 保数据重建）
);
```

> 外部评审修正（三处，均并入实现）：① UNIQUE 键列 **NOT NULL DEFAULT ''/0**——SQLite/MySQL
> 的 UNIQUE 对 NULL 互不相等，可空列会让 stalled_issue（task_id 空）等 finding 每轮重插、
> 去重失效；空串/0 参与唯一约束。② 选主谓词引导期：`patrol:leader` 行不存在时
> UPDATE rowcount=0 永远抢不到——先 SELECT 检查无行则 INSERT（竞态双写同键同值，良性）；
> value 与 cutoff 统一 aware ISO 同格式字符串比较（跨格式字符串比较错位=过期抢占失效）。
> ③ 手工端点规约：a2a-redo 手工路径走 _a2a_redo_impl 时**必传 compensated_by=manual+操作
> 用户 id**（不给默认值）——否则审计底线被默认值稀释。

幂等键与集群并发（关键修正：跨机原子性只认 DB 谓词，不认进程锁）：

- **选主**：`patrol:leader`（D3 谓词 UPDATE 原子抢占，持有者 id+时间戳）。
- **findings 去重**：`UNIQUE(kind, task_id, event_seq)` + `INSERT OR IGNORE`（mysql_compat.py:302 已翻译为 MySQL `INSERT IGNORE`）——双主同轮同发现，后插者静默忽略，无需选主也天然收敛。
- **补偿防双发**：不用 meta get-then-set 键（非原子）。用 grape_t_patrol_findings 的**状态原子流转**抢占：`UPDATE grape_t_patrol_findings SET status='claiming' WHERE id=? AND status='open'`，rowcount=1 才允许补（仿 event_active_guard seq=0 抢占语义），补偿成功置 compensated、失败回 open。`compensated:{task_id}` meta 键仅作跨周期审计标记（复检兜底），不作并发互斥依据。

## 四、改动文件清单

| 文件 | 改动 |
|------|------|
| src/api/db_setup.py | grape_t_patrol_findings DDL（UUID 主键）+ 迁移登记 |
| src/api/patrol.py | 巡检循环（**评审十三轮修正：先干活后睡，启动即出首轮清单**；**评审十四轮修正②/S27：选主总闸——回收/补偿/扫描全部在选主判定之后**）、扫描、判定纯函数、选主（holder\|iso 自续约+CAS，评审三轮修正+四轮通配符互斥；**评审十五轮修正③/S28：持有者标识收紧 hostname#pid，同机多 worker 不再共享 hostname 互开自续约**）、两档 LOOKBACK、meta 运行真源配置（五键，决策变更 2026-11-02）、队列僵尸单次探活（评审十三轮修正：判定与 cause 拼接复用同一次 _engine_unreachable 调用）、归属解析 _resolve_owner（**评审十四轮修正①/S26：路径 B host_user_id → 路径 A user_repos 管理员映射回落双源**）、L1 棒次过滤（**评审十六轮修正④/S32：target 已知只认 agent_type 匹配的 a2a_event**）、已知名短路（**评审十六轮修正③/S31：audit 重扫不重查已落库 (kind, task_id)**） |
| src/api/patrol_compensate.py（新） | Phase 2 补偿执行者（D20）：责任链六处理器 + 身份/模式策略族 + 派发适配器（**评审十六轮修正①/S29：verdict 分桶——不自愈三类置 needs_human 终态；批处理捞单带 30 分钟失败冷却，卡死 finding 不再饥饿 FIFO 队头**；**评审十七轮修正/S33：派发后终态回查——命中即复用 _sync_issue_pipeline_terminal 对齐收敛（completed 路径官方动作，不 kill 引擎）+ 5 秒延迟二次扫 + race_detected 观测回执；零窗根治（行锁事务）登记为数据触发项不做**） |
| src/api/main.py | 启动时 spawn 巡检 asyncio task + 挂载 patrol 路由 |
| src/api/routes/events.py | a2a-redo 手工端点 context 加 compensated_by=manual:{username} 标记（D5 评审修正③，行为其余不变） |
| src/api/routes/patrol.py（新） | 只读 API：GET /api/patrol/findings + dismiss（评审十三轮修正：unowned 置顶之外按 created_at 倒序——id 是 UUID，按 id 排序=随机序；**评审十六轮修正②/S30：dismiss 归属校验——owner 或 admin 才可收敛，unowned 仅 admin；返回 finding_status 消除重复 status 键**） （实现后审计补口：POST /api/patrol/scan 手动触发一轮检测面——R12/D22，只检测不补偿不抢主，D23 to_thread 卸载）|
| src/api/patrol.py（配置面） | **决策变更 2026-11-02：DB meta 五键运行真源**（patrol:interval_seconds / patrol:confirm_minutes / patrol:lookback_mode / patrol:compensate_enabled / patrol:compensate_blocklist）——每周期重读、改即生效、双机主从共享（MySQL 共库，runtime-config 优先级链同款：meta > 文件 > 代码默认）；configs/patrol-config.json（example 入库、正式文件 gitignored）仅首次启动 seed 进 meta（login-whitelist 先例，已有键不覆盖）；**patrol 模块零 os.environ 读取**（结构断言防回涨）；坏值回落默认+告警（fail-fast 对齐 GateHandler meta 口径）。无调参决策阈值走模块常量：runtime 回看窗 7 天、僵尸窗 2h/24h；引擎心跳新鲜度单源引用 engine_select._RUNTIME_STALE_SECONDS |
| configs/patrol-config.example.json | seed 示例（含 _comment 说明五键与紧急控制语义），复制为 patrol-config.json 后首次启动灌 meta |
| tests/test_patrol_unit.py | 单元测试（mock 数据访问接口，65 用例随 CI pytest）：判定/编排/配置/策略/结构断言全量覆盖，跑时 ~1.5s；真库验证项（唯一约束/迁移/谓词 UPDATE 原子性/全链路/API）按交付纪律不进分支，收敛为需求文档 §七.3 目标环境验收清单（验收时执行） |
| help/api/18-patrol.md | 新端点契约块 |

**不改动清单（防回归，逐条对应 S7）**：
- src/api/routes/tasks.py complete 单信号逻辑（362-374）——不加同步兜底
- src/api/event_pipeline.py self_loop 拦截（1577-1648）——不放行任何 bot 评论
- 假完成硬门槛（4288）——语义不重叠，各管各
- a2a-redo 手工端点行为与 30s 幂等窗——只抽函数不改语义
- events.py:478-492 redispatch 拒绝 self_loop 事件判定——原样
- 不改 agent_tasks/events 任何既有列，不写 events 表（补偿产生的 a2a_event 走 /api/a2a 标准链路，与手工补发同源）

## 五、影响访问与兼容性

- **A 既有调用方**：全部 API 零变化。巡检是旁路只读组件；唯一共享改动是 a2a-redo 端点抽函数（行为字节级不变，端点契约不动）；新增 GET /api/patrol/findings 为纯增量。
- **B 破坏点**：无。无字段删除、无端点迁移、无 DB 既有列变更；grape_t_patrol_findings 为新表只增。
- **C 明确不受影响面**：DB 既有 schema、前端页面（Phase 1 不动 UI）、webhook 链（:8080 不载巡检）、A2A 正常交棒路径、autopilot 业务巡检。
- **D 正向**：DB 加 1 张表 + agent_tasks 一条索引；每 5min 3 条 SELECT；补发仅复用已有 /api/a2a 链路。
- **部署顺序约束**：无原子交付约束（全增量）。集群并发已闭合：双主同轮最坏并发为 findings 插入静默去重 + 补偿状态原子抢占（rowcount 判定），无双主补偿窗口；选主谓词 UPDATE 保证单写者。worker 侧不载巡检（D2），多 worker 无冲突面。

## 六、性能影响

- 扫描量级：agent_tasks 千级行 × 5min 一次，三条 SELECT + 内存判定，预估 <50ms。grape_t_patrol_findings 里持续记录扫描耗时（UC5-2），超过阈值只记日志。
- 断链候选的 result_text JSON 解析在 Python 侧做（D6），只对 completed 且近窗行解析，不做全表。
- 索引前置：探测 agent_tasks 是否有 (status) 或 (status, completed_at) 索引（information_schema/PRAGMA 实测后决定），无则 `CREATE INDEX IF NOT EXISTS idx_agent_tasks_status_completed ON agent_tasks(status, completed_at)`——注意 MySQL 方言规则：索引列集变更需显式 DROP+重建，但这是新索引无此问题。
- 最坏情况自审（用户首要标准）：巡检器最坏故障 = 循环抛异常 → 被自身 try/except 捕获 + 退避（仿 scheduler），主服务零影响；Phase 2 误补偿最坏半径 = 一次多余交棒事件 → 靠 CONFIRM 窗 + 幂等键 + 补偿标记可追溯可人工撤销（下棒看到标记可先核对上游产物），无不可逆状态变更。破坏性操作（僵尸终态化）明确留给 Phase 3 数据支撑后再议，本设计零破坏路径。

## 七、测试设计（mock 策略：DB 读全部 monkeypatch 内存 dict，不连真库/真 OBS；仅 grape_t_patrol_findings 建表语句用临时 SQLite）

| # | 用例 | 断言要点 |
|---|------|---------|
| T1 | UC1-1 断链检测命中 | seed completed+next_step=a2a*+L1 空+L2 空+超窗 → grape_t_patrol_findings 出现 broken_chain |
| T1b | D11 L2 关联性 | 下游有活跃任务但 created_at 早于本任务 completed_at（上一轮遗留）→ 不算交棒证据，finding 照报 |
| T1c | D11 L2 正向证据 | 下游同管线节点活跃任务 created_at 晚于本任务 completed_at → 判交棒已发生，无 finding |
| T1d | R4 补偿前终检 | 状态流转抢占成功后 L1 出现 a2a_event（任意状态含 completed，入库慢）→ 放弃补偿置 false_positive |
| T1e | D14 补偿身份（跟随托管模式） | 路径 B（host_user_id>0）→ 以托管用户身份直调成功；bot token 托管 → 服务端主体；无任何托管主体 → auth_unavailable 降级，不调用不报错 |
| T1f | D11 L2 全状态 | 下游节点已有 created_at 晚于本任务 completed_at 的已完结任务（completed/failed，无活跃）→ L2 命中，无 finding 不补 |
| T1g | D15 模式恢复 | 子任务链（issue_state is_subtask_mode=1 + parent_issue_id 在）→ 补发 payload 带齐 parent_issue/is_subtask_mode/carried_context（断言 payload 字段）；is_subtask_mode=1 但 parent_issue 缺失 → mode_unrecoverable 挂起不补 |
| T2 | UC1-2 未超窗静默 | completed 5 分钟 → 无 finding（防误补核心） |
| T3 | UC1-3 非 a2a 前缀不报 | next_step 为空/其他 → 无 finding |
| T4 | UC1-4/R4 幂等 | 双补竞态：两路同时状态流转同一条 open finding → 仅一路 rowcount=1（另一路 0），成功路置 compensated、action_meta 记录 |
| T5 | UC2 僵尸两档+queued 归类 | ①stream_ended=1+running → 高置信；②2h 无信号 → 低置信；③远端引擎 offline 的 queued → 即时归永不调度；④24h 引擎在线 → 观察；⑤阈值内健康 → 不报；⑥**本机引擎（cid=0，installed+healthy，无 last_seen_at）的 queued → 不归永不调度**（远端口径误用防回归，UC2-6） |
| T6 | UC3/D13 停摆=issue 视图 | 断链 finding 按 issue 聚合出 stalled_issue，同源可对账；L1 有 completed 事件 → 两视图都不报；stage 终态不报 |
| T7 | R7 风暴谓词 | 构造"补偿刚发生"状态 → 谓词返回 False（窗口错开断言） |
| T8 | D3 选主（集群） | 谓词 UPDATE：过期→抢占成功（rowcount=1）；新鲜→rowcount=0 skip；他主续约不可被覆盖（value 持有者比对）；通配符互斥（UC12-1：`host_a` 不得续 `host-a` 的锁——LIKE `_` 单字通配符防回归，substr 精确前缀实现） |
| T8b | 集群 findings 去重 | UNIQUE 约束 + INSERT OR IGNORE：同 (kind,task_id,event_seq) 二插静默忽略，无双行 |
| T9 | S5 巡检异常退避 | 扫描抛异常 → 循环存活、退避记录、主流程不受影响 |
| T10 | S7 结构守卫四条 | 源码断言：self_loop 块无放行分支；complete 无路由逻辑新增；a2a-redo 端点仍存在且语义未变；redispatch 判定原样 |
| T11 | UC4 正常链零误报 | 正常交棒（有 a2a_event）→ 无 finding |
| T12 | 端点契约 | GET /api/patrol/findings 出入参 + help/api 文档同步测试 |
| T13 | D16 归属字段 | 路径 B finding owner=host_user_id；路径 A=托管用户映射；无主体=0（unowned）且清单查询置顶；NOT NULL 断言 |
| T14 | D17 PR 订阅排除+两档窗口 | `#pr_` 前缀任务（next_step=a2a*）→ 零 finding；存量模式 LOOKBACK=全历史扫出历史断链；常规模式窗口收敛且历史 finding 不重插（唯一约束） |
| T15 | D18 终态化唯一通道 | 结构断言：patrol 模块源码零 `UPDATE agent_tasks SET status` 写路径；scheduler #537 常量与行为回归绿 |
| T16 | D19 决策变更·门卫场景 | ①零配置（meta 全空）→ 补偿全链路 compensated（开箱即用，UC14-1）；②黑名单命中仓 → repo_blocklisted 零派发、finding 保持 open（运维摘除通道不吞数据，UC14-2）；③`*` → 全拦紧急刹车（UC10-4） |
| T17 | S13 决策变更·配置真源场景 | meta patrol:confirm_minutes 运行期收紧（10→3）→ 判定窗下周期即生效不重启（UC14-3）；文件 seed 幂等（无键灌入/有键不覆盖/再跑零 seed，UC13-4）；meta 坏值回落默认+告警（UC13-3） |
| T18 | 评审五轮·issue 终态守卫+结论等价性 | ①终态 issue（completed/pending_review）检测层排除、终检前置守卫（false_positive 零派发，端点只拦 cancelled 巡检自守）；②补偿 context 携带原任务 result_text 结论（600 字符截断），下游拿到上游结论（UC15-1..3 / test_s15_1..3） |
| T19 | 评审六轮·liveness+判定精度 | ①永久失败（4xx/skipped 语义拒绝）终态 permanent_failure 不再无限重试，瞬态 5xx 回挂 open（UC16-1）；②子任务 L2 关联键接线 _subtask_parent——主链同 issue 任务不构成子任务链证据（UC1-12/UC16-2）；③批处理捞单 FIFO（created_at 排序，test_s16_3 行为断言：limit=1 先消费最老 open） |
| T20 | 评审七轮·双源与结论 | ①_subtask_parent 双源（issue 单表权威+issue_state 兜底，get_issue_stage 同款——C3 新建子任务链不退化主链键）；②结论结构化提取 _conclusion_snippet（conclusion 字段优先，非整段 JSON 下发）（UC17-1/2 / test_s17_1/2） |
| T21 | 部署实测·迁移与唯一键 | ①存量库（台账盖章）自动建表迁移（UC18-1）；②issue 级 finding 四列唯一键防吞行（UC18-2）；③旧形态表迁移重建保数据（UC18-3）——均为本机全流程部署实测抓到的真缺陷修复 |
| T22 | 评审八轮·活性与审计口径 | ①claiming 孤儿租约回收（10min，回收重试幂等键保证不双发，UC19-1）；②duplicate_handoff → false_positive（交棒已发生正向证据，UC19-2）；③compensated meta 审计键退役（action_meta+事件标记双通道）；④JSONResponse 解析契约钉死（UC19-3） |
| T23 | 评审九轮·provider 无关判例 | github: 键形态五判例官方钉死（命中/L1 掩蔽/终态/黑名单精确/前缀剥离，UC20-1）——官方套件补边，防 provider 分叉回归 |
| T24 | 决策 A·规范交棒键 | handoff:{task_id}:{target_agent} 单源派生 + 非空护栏 + idx_events_idem 索引存在断言（UC21-1）；幂等命中 action_meta 记 idempotent_hit（UC21-2）——残余竞态从"理论双跑"降级为良性先到先得 |
| T25 | 评审十轮·MySQL 方言 | 租约回收读-改-写（零 \|\| 拼接，MySQL OR 静默毁数据防回归）+ sqlite_master 字面量特判命中断言（UC22-1）——本机无 MySQL，用兼容层机制做方言级验证 |
| T26 | 第十二轮·A6 命名 + 无条件回收 | ①表名 grape_t_patrol_findings（A6 规则，迁移重跑盖章）；②reclaim_stale_claimings 独立函数每周期无条件执行（开关关闭也回收，实测孤儿→open 且 action_meta 读-改-写无损） |

边界注意（沿测试既定坑）：谓词判定为纯函数直接单测（不依赖管线 guard 消费顺序）；SQL 截止时间 Python 侧预计算传参（禁 datetime('now','-N')）；巡检模块不 import session_manager 裸跑（conftest 测试库隔离规则）。

## 七.5 契约对账补充（第十七轮）

mock 策略不变，新增两个零覆盖契约面的行为断言（均 mock DB / 纯函数）：
- insert_finding（S43）：FakeDb 记录 INSERT 语句与参数，断言 UUID 主键、
  owner 归属、默认 open、rowcount=1/0 语义、异常吞掉（cursor() 在 try 外的
  缺陷即由此用例抓出修复）；
- build_chain（S44）：责任链类型顺序钉死 + DispatchHandler 不入同步链
  （async 签名隔离），六段编排结构断言。

## 六.5 性能基准与两段式拉取（第十八轮，实测口径）

磁盘 SQLite 合成基准（500/5k/20k/100k 候选）：全判定 18μs/候选线性；
audit 20k=354ms；短路路径 20k=90ms；cleanup 10 万行=129/17ms；insert 0.9ms。
当前量级无事故面（生产实测 7-87ms/轮，S40 告警线 30s 差两个数量级）。

落地优化（R18/S45）：detect 两段式拉取——阶段 1 轻量列+全部纯谓词短路前置，
阶段 2 幸存候选 id IN 500 分块回捞 result_text。动机：大字段随候选拉取
=20k×2KB=40MB/轮（MySQL 网络 470MB/h 纯浪费）；最坏情形实测 368→237ms，
语义零变化。基准数字为作者环境报告值（单机合成基准）——验收时目标环境复测；
受益面为 MySQL/RDS 部署（SQLite 进程内无网络搬运，收益≈零、无害）；
1.2MB 估算依赖幸存者占比 ~3% 经验分布，真实短路率决定真实收益。

数据触发项（不预防性做，S40 告警命中即动）：
- P2 N+1 反查批量化（issue 终态/L1/L2 各一条 IN 预取）——MySQL 网络下
  20k 级未知候选会撞告警线；
- P4 findings 清单 (owner, created_at) 索引——retention 已约束表有界。
P3 cleanup 降频/索引收益 17ms/轮，登记不修。

## 八、风险与残留

| 风险 | 缓解 |
|------|------|
| 存量 60 条判定的正确率未知（nodes_json 缺失/场景变更） | Phase 1 只读跑 2 天人工核对；验收分母=双源可判定子集 |
| 误补偿（agent 慢收尾被误判） | CONFIRM 窗可配 + 幂等键 + Phase 2 默认关 + 灰度开关 |
| 双实例短暂双主 | 选主谓词 UPDATE 原子抢占无双主窗口；即便选主失效，findings 唯一约束去重 + 补偿状态流转抢占兜底，最坏重复扫描不重复动作 |
| 集群时钟漂移 | 选主 cutoff 为 Python 算的本地时间，多机漂移理论上影响心跳新鲜度判断——已被覆盖：心跳窗 2×周期自带漂移余量，且选主失效有唯一约束+状态流转两层兜底（最坏=重复扫描）；不做 NTP 依赖 |
| 扫描慢查询拖主进程 | 独立 asyncio task 不阻塞事件循环热点（await sleep + 单轮扫描毫秒级）；耗时指标落 findings |
| result_text JSON 结构漂移（D6 无 schema 版本控制） | backlog：解析失败计数进 findings（自监控"巡检发现自己瞎了"），上游改结构时巡检不静默失效 |
| webhook 去重口径未来变更牵动巡检安全网 | backlog：在 webhook.py #329/#587 去重处加注释回链本设计文档，建立双向引用防不知情改动 |
| Phase 3 僵尸终态化误杀 | 本设计明确不做，数据先行（D10） |
| 7 天视界时效（#463 清扫 null 化老任务 result_text） | 上线顺序硬约束：首日切 audit 跑一轮存量核对（验收清单登记）；数据被清后不可判定符合不猜原则 |
| skill 侧幂等键未对齐（落地前迟到原生交棒与补偿两键，窗口期双发） | 跨仓另行立项；平台侧终检+状态流转+端点三重防重护栏生效，双发最坏=幂等拒绝一次；**平台侧键契约已测试冻结（S37/R13：模板字面量+单源同构+空白规整+空参拒绝，CI 级防漂移）** |
| db_lock 车道竞争（扫描与 API 共享进程内锁） | D23+S42：扫描与 reclaim 两段均已 to_thread 卸载出事件循环（卸载面完整）；锁竞争本身不消解，量级增长后根治走 S33 行锁数据触发项 |
| retention × audit 复活交互（终态行被 TTL 删除后 S31 短路失键，audit 重扫复活 dismissed 项） | audit 前置核对时 set meta patrol:findings_retention_days=0；runtime 模式 7 天窗天然免疫；compensated 行有 L1 补偿事件兜底不复活 |
| 5 秒延迟二次扫为进程内 task（重启窗口丢 race 观测回执） | 对齐动作首轮已同步执行，链路无损；数据触发项口径同 S33 |
| _known_task_ids 全表集合查询 | retention TTL 已约束规模；元组集合内存可承受，登记观察 |
