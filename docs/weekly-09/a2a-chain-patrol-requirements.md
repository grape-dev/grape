# Spec: A2A 链路巡检器（拉模型兜底）— 平台链路停摆根因与自动兜底

> Issue: https://gitcode.com/grape-dev/grape/issues/635（迁移自 GTDTeam/CodeArts#280）
> 分支: `feat/issue-635-a2a-chain-patrol`，基线: origin/weekly-09 @ caded1c4c
> 状态: 评审中 | 类型: 平台可靠性（reliability）

## 一、背景与根因（实测核验，非推测）

流水线推进依赖 agent 自觉交棒（A2A 事件），平台对"agent 忘了交棒"没有任何兜底。
全仓统计：1,264 个声称 `next_step=a2a*` 的完成任务中 142 个（~11%）无对应
a2a_event，抽样 60 条全部断链，含 34h 僵尸任务。漏发源头是 LLM 长任务尾部执行
漂移（同 skill 同引擎不同跑：#245 成功 433 步 9 次交棒 vs #279 失败 63 步 0 次），
**概率性、不可预测、无法在源头根治，只能平台层兜底**。

### 三个断点（全部在 caded1c4c 逐条核实，file:line 为本分支实测）

| # | 断点 | 实测证据 |
|---|------|---------|
| 1 | **next_step 哑字段** | `src/api/routes/tasks.py:362-374`：complete 接口收 `body.next_step`，仅拼进 result_text 结构化 JSON 归档（371 行），随后 `set_task_signal(task_id, "self_reported", result_text)`。注释明示"单信号（C）模式：自报=记录权威结构化结论（产物而非闸门）"，next_step 从设计上就不是路由指令，agent 填了也没人消费 |
| 2 | **bot 评论被 self_loop 拦截** | `src/api/event_pipeline.py:1577-1648`：issue_comment/note_hook_pr + bot actor 直接 `append_skipped_event(reason="self_loop")`（event_store.py:244）。防线方向正确（防 agent 自唤自），但把断链时唯一的人可见痕迹也吞了。放大器：`src/api/routes/events.py:478-492` redispatch 双源判定把 self_loop 拦下的事件**拒绝重派**——被拦事件连人工恢复入口都堵死 |
| 3 | **无独立巡检** | `src/api/event_pipeline.py:4288` 起有"假完成硬门槛（issue-P5 关卡B）"，但只防"无写操作"的假完成（证据看 tool_uses 里的真实 POST），防不住"发了评论但漏 A2A"——评论本身是写操作。全库无任何事件流之外的事后巡检组件（src/ 里"巡检"命中均为业务域 autopilot LLM 任务，另一回事） |

三断点叠加：漏发一次 = 链必停 + 无告警 + 无人工恢复入口。

### 评论补充的实据（纳入巡检扫描范围）

- A 队列僵尸：queued 积压 20 条，其中 17 条来自 2026-08-30/31 已挂两月（外部测试仓），引擎/场景已变更永不调度，无人收到通知。
- B 失败无因：10-25 以来 failed/timeout_failed 762 条，592 条（78%）result_text 为空，失败原因完全不可追溯。空失败原因本身就是缺陷：巡检对 failed 任务只能报"失败但不知道为啥"。
- C 事件层浪费：event_skipped 全周期 self_loop 14,405 / no_route 3,862 / issue_done 2,001。no_route（push 等事件无路由静默丢弃）建议巡检/周报显性化。
- D 跨仓引擎错绑：user 级引擎 16929/16930/12714/12866 被 5 仓 18 场景引用，外部 user_id=0 进链必断。巡检只产清单，修复人工。
- E offline 引擎 115 台。巡检只报"被活跃场景引用的 offline 引擎"，不全量告警。

## 二、需求（R#）

| # | 需求 | 验收 |
|---|------|------|
| R1 | 独立巡检进程（主进程 asyncio 循环，仿 scheduler.py 范式），定期扫 agent_tasks + events 两张表，纯平台客观数据，不认识业务语义 | 巡检器启动日志 + 周期可见；集群多主下单写者（DB 谓词原子选主，跨机仅认 DB 谓词不认进程锁） |
| R2 | 异常检测**三层**（已决策：时间窗是最后一层兜底，不是主判定）：**L1** 查事件表——有无交棒 a2a_event（**查全状态**，不只 running）；**L2** 查下游状态——同 issue/同场景管线节点下，created_at 晚于本任务 completed_at 的**任意状态**任务（含 completed/failed，与 L1 同一全状态口径——只查活跃会把已跑完的下游误判为断链，#587 盲区换位）存在即视为交棒已发生（防误报核心：下游忙须与本任务时间+管线关联，否则上一轮/PR 订阅/人工重派任务会掩盖真漏发→误报换漏报）；**L3** 两者皆查不到才启用等待窗（**默认 10 分钟，已决策二轮修正**——端点防重实证后收紧，依据见设计 D4），窗满仍无 → 真漏发或写失败，均须补。**断链候选范围跟随链路模式（D17）**：PR 订阅链（issue_id `#pr_` 前缀）被 a2a 端点设计性拦截（只审查不流转，events.py:1240），本无交棒语义 → 排除断链候选，否则纯误报污染误报=0 硬线；子任务链 L2 关联键= parent_issue+agent_type（非 issue+场景节点，防与主链同 issue 误配正向证据）。**僵尸判定证据化（D12）**：running 两档——`stream_ended=1 AND status='running'`（终信号已到状态未流转）为确定性高置信即时告警；无信号+超 2h 为低置信只登记。queued 确定性归类——绑定引擎不可达或场景不存在 → 即时判"永不调度"，24h 只兜引擎在线但不调度的边界情形。**引擎不可达判定跟随引擎形态（D12）：远端引擎（credential_id>0）= status=online + last_seen_at 心跳新鲜；本机引擎（cid=0）= installed+healthy，无心跳语义——照搬远端心跳口径会把本机引擎的 queued 任务误判"永不调度"**。**停摆链（D13）**：非独立时间窗判定，是 broken_chain 的 issue 视图——stage 停在的节点任务 completed 后 L1/L2 无后继即停摆，复用同一证据链，独立 N 分钟窗取消 | 三类各有单测 + 存量 60 条断链全量发现（验收分母=双源可判定子集）+ L2 关联性判例（无关联下游任务不算证据）+ L1 全状态断言 + 僵尸两档判例 + queued 确定性归类判例（引擎 offline 即时归类，不待 24h）+ 停摆/断链两视图同源一致性断言 |
| R3 | Phase 1 只读：检测 + 异常清单落库 + 告警，零状态变更 | 跑 2 天人工核对存量 60 条判定正确率；巡检自身扫描耗时进清单（索引健康度指标） |
| R4 | Phase 2 自动补偿（默认开，决策变更 2026-11-02）：对 L1/L2 皆空且窗满的断链合成一次与原交棒同构的 A2A payload，调平台已有 a2a-redo 原语（events.py:1427）补发，外带补偿标记。**补偿身份（D14 已决策二轮修正）：身份跟随托管模式——① 用户 PAT 托管（路径 B，host_user_id>0）用该托管用户平台身份（PAT 平台持有、配置即授权，属仓库运维主体非冒用）；② bot token 托管用 bot token 服务端主体；③ 无任何托管主体（解绑/凭据轮换干净）→ 降级挂起+告警（auth_unavailable），绝不冒用无关个人身份；agent 会话 token 一律不用（冤枉漏棒 agent+临时凭据过期）**。**补偿 payload 模式跟随原交棒模式（D15）：is_subtask_mode / parent_issue / carried_context / engine_config 从库内恢复（carried_context 在 issue 表[session_store.py:261 get_carried_context]、is_subtask_mode+parent_issue_id 在 issue_state[:78/:321]），恢复完整才补，不可恢复（模式字段缺失）→ 挂起人工，不猜——子任务链丢 parent_issue 会让下一棒脱离父任务上下文跑偏，违反等价性标准**。**补偿前强制终检（已决策）：状态流转抢占成功后再查一次 L1（全状态）+L2，任一有证据即放弃补偿置 false_positive——入库慢无论多慢，只要补偿前落库就当场拦住，时间窗退化为纯兜底****；评审五轮补 issue 终态守卫（S15）：终检前置 issue_terminal 谓词，issue 已被用户收敛（completed/pending_review）→ false_positive 零派发——L1/L2 只找交棒证据，人已处置证据必须另查**。**开关语义（D19 决策变更 2026-11-02 负责人拍板）：巡检功能默认启用；补偿默认开（能力默认生效）；运行期紧急控制走 meta 键——总开关 `patrol:compensate_enabled=false` 一键全关、黑名单 `patrol:compensate_blocklist` 逐仓排除（逗号分隔 repo_key，`*`=全拦紧急刹车），均每周期重读无需重启；原白名单设计（空=零放行）删除——逐仓白名单在托管仓规模化后无人维护，等于能力空转，且防误发真正依靠终检+幂等键+状态流转抢占（结构性守卫），非入口闸门** | 补发后链状态与 agent 不漂移时一致（等价性标准）；并发防双补以 finding 状态原子流转为准（DB 谓词 rowcount 判定，集群双主下同任务只补一次），审计标记经 finding.action_meta + 事件 compensated_by 承载（评审八轮：原 compensated:{task_id} meta 键退役）；无任何托管主体的仓库不补偿只告警；开关动态生效与白名单各判例 |
| R5 | 补偿边界：只补确定性缺失。下一棒引擎不可用/错绑（D 类）→ 只挂起+通知不补偿；任务僵尸（未跑完）→ 不代跑不补发 | 分场景补偿矩阵入设计文档，三类各有判例测试 |
| R6 | 全量可追溯：巡检发现、补偿动作、幂等键全部写事件（仿 events.py `redispatch:{seq}` 写法），补偿 payload 带来源标记（补偿触发+原任务 id+巡检时间）——**补偿标记为直调入口必传参数（已决策，不传不通），保证手工重派与机器补偿可区分**。**硬性要求（负责人强调）：① grape_t_patrol_findings.id 一律 UUID（新表主键 UUID，Python 侧生成，禁自增）；② grape_t_patrol_findings.task_id 一律使用 agent_tasks 的 UUID 主键（agent_tasks.id 即 uuid），禁止自增 id、禁止序号、禁止拼接键——跨表回溯（finding→task→event→issue 全链）以平台签发主键为准，任何非 UUID 形态视为实现缺陷** | 任一补偿可从事件链回溯原任务，且能区分手工/机器来源；task_id 全量 UUID 形态断言 |
| R7 | 防自激：巡检器动作产生的新事件不在自己的扫描窗口内（结构性无风暴） | 补发事件与扫描窗口时间错开的代码断言 + 测试 |
| R8 | **告警受众归属（D16）**：每条 grape_t_patrol_findings 落库时带归属人维度——按 D14 同一托管模式解析（路径 B=host_user_id 用户；路径 A=托管用户映射；无主体=标记 unowned），后续通知通道按归属路由 | findings 每条均有归属字段；Phase 1 仍 API-only（前端/通知通道不在本分支），归属设计先行避免"清单落库但无人知晓 2.0"（issue A 类 17 条挂两月无人通知的根因） |
| R9 | **扫描模式分档（D17）**：存量核对模式（PATROL_LOOKBACK 覆盖全历史/60 条最老完成时间，Phase 1 人工核对期用）与常规模式（收敛到运行窗）分离；否则"存量 60 条全量发现"验收是空话 | 存量核对模式跑出的发现条数 ≥ 存量 60 条（程序化对账）；切换常规模式后窗口收敛 |
| R10 | **终态化唯一通道边界（D18）**：僵尸终态化永远走 scheduler 既有 #537 通道（PENDING_SELF_REPORT_TIMEOUT，scheduler.py:30-32），巡检只做发现源（喂 stream_ended 高置信证据），patrol 模块零终态化写路径 | 结构断言：patrol 模块无 UPDATE agent_tasks SET status 终态写；#537 通道行为回归绿 |
| R11 | **findings 保留策略（实现后审计落地，D21）**：grape_t_patrol_findings 终态行 TTL——只清理 dismissed/false_positive/compensated/permanent_failure 四状态且 updated_at 超过保留期（meta `patrol:findings_retention_days`，默认 90 天，0=显式永不清理，坏值回落默认）的行；open/claiming（活性队列）与 needs_human（未决人工信号，删了=丢线索）**永不清理**。清理随每轮扫描执行，删除数 purged 入回执与日志可对账——审计发现表只增不减（runtime 模式每 5 分钟全量重扫 + audit 全历史扫描）属无界增长模式，本需求封口 | SQL 参数状态集合钉死（四终态在列、三活性/未决不在列）+ 0 值零 DELETE + 坏值回落 + purged 入回执，各有单测（UC34-1..4） |
| R12 | **运营介入面 + 事件循环卸载（实现后审计落地，D22/D23）**：①POST /api/patrol/scan 手动触发一轮巡检——不等 5 分钟自动周期；**刻意只做检测面**（检测+落库+R11 清理），不补偿、不抢主（补偿仍走选主门控自动循环，D3/D20），手动轮与自动轮靠 findings 唯一约束幂等汇合；②主循环与手动轮的同步扫描（持 db_lock 全表扫描）一律经 asyncio.to_thread 卸载出事件循环——裸调会卡住整个 FastAPI 事件循环（审计实测项），卸载后 DB 车道竞争仍在但 API 不被整轮冻结 | 端点契约（scan 统计回显、mode=运行真源档）+ 500 收口不外溢 + to_thread 结构断言（主循环与路由两处）+ 裸调形态防回退断言（UC35-1/2、UC36-1） |
| R13 | **规范交棒键契约冻结（实现后审计收尾，D5 决策 A 的跨仓对齐靶面）**：handoff:{task_id}:{target_agent} 模板字面量与派生函数（handoff_idempotency_key 单源）以独立测试冻结——skill/worker 侧守门件按同式派生是对齐前提，平台侧任何静默改形（加前缀/改分隔符/换字段序/空白不规整）都会在 CI 爆红而不是等 skill 对齐后双发才被发现；空白规整（前后空白不产生新键）与空参拒绝（护栏②）同为冻结面 | UC37-1 模板字面量+单源同构+确定性；UC37-2 空白规整同键；UC37-3 None/空拒绝（test_s37_1/2/3） |
| R14 | **findings 清单归属过滤（全链路审计落地，D24）**：GET /api/patrol/findings 读口子与 dismiss 写口子同口径锁——admin 全量可见（管理页口径，unowned 置顶）；非 admin 仅见 owner=自己 的行，unowned（owner=0 高危项）仅 admin 可见；admin 台账查询异常 fail-safe 降级为归属过滤（宁可少看不可多看）。此前 WHERE 只有 status/kind——任意登录用户可见全平台清单（repo_key/issue_id/detail 快照，BOLA 面） | admin 全量+unowned 置顶回归 + 非 admin owner=? 过滤 + 台账异常降级路径，各有单测（UC38-1/2） |
| R15 | **手动轮 single-flight 闸 + 慢扫描告警（全链路审计落地，D25/F）**：①POST /api/patrol/scan 进程内在跑标志——第二发并发请求 409（自动轮有选主总闸，手动轮此前无对称闸：N 并发=N 全表扫描互踩 db_lock）；异常路径 finally 释放（闸不死锁）；②run_patrol_once duration 超 _SLOW_SCAN_WARN_MS（30s 模块常量）warning 告警不熔断（检测+报告零危害口径；约 60× 实测上界，命中=量级变化需复核索引/短路健康度） | 并发 409+闸释放（成功/异常两路径）+ 告警触发/边界严格比较/默认阈值钉死（UC39-1/2、UC40-1/2） |
| R16 | **全入口 single-flight 闸 + reclaim 卸载（全链路审计·闸收口，D26）**：①闸下沉到 run_patrol_once（threading.Lock 线程安全）——路由层快检只挡手动轮对手动轮，自动轮×手动轮并发时两轮全表扫描互踩 db_lock（第 15 轮修复的推演缺口）；在跑时任一入口第二发抛 PatrolScanInProgress：自动轮记 skip 日志不进退避（退避会把周期人为拉长），手动轮 409；异常路径 finally 释放；②reclaim_stale_claimings 也是持 db_lock 的表扫描同步段，主循环内与 run_patrol_once 同款经 to_thread 卸载（D23 此前只覆盖扫描段漏了回收段）；顺序约束（选主→回收→开关）不因卸载改变 | 函数级闸并发/异常释放两路径 + skip 良性语义结构断言 + reclaim 卸载形态与顺序保持（UC41-1/2/3、UC42-1） |
| R17 | **落库与责任链契约的行为断言（覆盖率对账落地）**：①insert_finding——R6 硬性要求此前零用例：id UUID 形态（禁自增）、owner 归属随行（R8）、默认 open、created/updated 同刻、INSERT OR IGNORE rowcount=1/0→True/False（双主×手动轮幂等汇合的行为面）、异常吞掉返回 False（S5——本轮实测抓出 cursor() 在 try 外的真实缺陷并修复，单条落库失败不再把整轮扫描打进退避）；②build_chain——D20 编排红线此前零用例：五同步处理器顺序钉死（门卫→抢占→终检→身份→模式，重排即破坏设计：终检挪到抢占前=先查后抢竞态回潮、门卫离首=开关失效仍耗 DB）、DispatchHandler 不得混入同步链（async 签名不兼容=静默失效） | UC43-1/2/3（UUID/归属/rowcount/异常吞掉）；UC44-1/2（顺序冻结+派发隔离） |
| R18 | **两段式拉取（性能实测落地，P1）**：detect_broken_chains 阶段 1 只拉轻量列（id/repo/issue/agent/completed_at），已知/PR 订阅/issue 终态/缺键/未超窗短路全部前置（均为纯谓词，重排不改变结果集与产出顺序）；阶段 2 只对幸存候选按 id IN 分块（500/批）回捞 result_text。动机（磁盘 SQLite 实测）：候选拉取带 result_text（2KB/行）= 39.8MB/轮 vs 不带 1.2MB——20k 任务下 **MySQL 网络读 40MB/轮 ≈ 470MB/小时纯浪费**（S31 短路在 Python 侧，大字段已拉回才被丢弃）；回捞单块异常吞掉（缺文本判 "" 走不报口径，S5） | 幸存者独享回捞（IN 参数钉死）+ 500 分块（1001→500/500/1）+ 回捞异常吞掉（UC45-1/2/3）；最坏情形（全未知候选）实测 368→237ms（**作者环境报告值，验收时目标环境复测**；**受益面注记：本优化收益集中在 MySQL/RDS 部署**——SQLite 进程内库无网络搬运，两段式等于多一次毫秒级 IN 查询，无害但收益≈0；网络面 1.2MB 估算依赖幸存者占比 ~3% 的经验分布，真实短路率决定真实收益，最坏情形数字即为此兜底） |
| R19 | **探活去重缓存 + 谓词顺序修正（性能·分析人清单落地）**：①detect_zombies 轮内缓存——引擎探活按 runtime_id 去重、场景解析按 (repo_key, issue_id) 去重，同键多任务共享一次查询（跨任务 N+1 → O(去重键数)；缓存生命周期=单轮，心跳/场景变更下周期自然重判）；②detect_broken_chains 阶段 1 谓词重排——缺键/未超窗（零成本纯谓词）提到 issue_terminal（每候选一条 issue 表查询）之前，未超窗任务占候选大头时不再先查库再丢弃；两者均为纯谓词重排/ memoization，结果集与产出顺序零变化 | 探活去重（同 runtime 三任务一次探活+跨轮重探）+ 窗内候选零 issue 查询+超窗候选照常判+结果集不变（UC47-1/2） |
| R20 | **写入端批量落库 + 候选流式分页（性能·分析人清单 A/C 落地）**：①insert_findings_batch——攒批 executemany+每块单次 commit（逐条 INSERT+commit：SQLite=逐条 fsync、MySQL=逐条事务往返，100 条 finding=100 次事务提交）；owner 归属按 repo_key 锁外去重解析（原逐条 _resolve_owner 也是 N+1，且其内部持锁不可在批量锁内调）；单块失败吞掉（S5 口径）；UUID 主键/owner 随行/默认 open 契约在批量路径同样成立；②_iter_completed_tasks keyset 分页——audit 全历史一次 fetchall 的峰值内存 O(全历史行) 改为 id>游标 ORDER BY id LIMIT 500 逐页（每页独立完整查询：锁内取页锁外处理，cursor 不跨锁存活规避共享连接并发风险；并发插入由 INSERT OR IGNORE 幂等兜底） | 100 条 finding 恰 1 次 commit+executemany 一批 / owner 去重解析 / 单块失败隔离 / 游标逐页推进至取尽（含终止探针）/ 跨页已知分布判定不变（UC48-1/2/3、UC49-1/2） |
| R21 | **seed 契约 + 批量落库接线钉死（覆盖对账·第22轮）**：①_seed_meta_from_file 首次有行为断言——文件只 seed meta 空键（已有键不覆盖，S13/login-whitelist 先例语义）、bool 序列化小写、meta 读写异常吞掉返回 0 不炸启动；②run_patrol_once→insert_findings_batch 接线钉死——检测产物必须整批进批量落库、回执 inserted=批量返回值（S48 只测函数未测接线，退回逐条写不爆红的缺口封口） | 接线（整批进批量+回执取批量返回值）/ seed 空键才写+已有键跳过+bool 小写 / seed 异常吞掉（UC50-1/2/3） |
| R22 | **已知集轮内共享 + zombie fetchall 登记（覆盖对账·第23轮）**：①detect_broken_chains/detect_zombies 增加 known 共享参数（None=自行拉取，旧签名兼容）——run_patrol_once 轮内一次拉取、双检测共享同一集合对象（原两检测各自全表拉同一集合=轮内双拉）；语义等价依据：落库发生在两检测之后（批量落库），轮内两检测看到同一快照恒成立；②zombie 检测 fetchall 全量形态登记为数据触发项——自指放大风险：僵尸检测的对象恰是滞留 running 的行，僵尸堆积→fetchall 行数随之增长（万级 running 才有感），与 S33 零窗项并列 | 轮内恰一次拉取+共享引用（id 同一）+独立调用自行拉取兼容（UC51-1/2） |
| R23 | **known 共享参数生效语义 + meta 坏值回落补口（覆盖对账·第24轮）**：①R22 的 known 共享参数此前只在 mock 接收者层验证（s51 收到参数），真 detect 函数消费该参数的语义（传入集合直接短路、不再自查 findings 表）无断言——补口后共享优化防回退完整；②lookback_mode 的 meta 层坏值回落（非 audit/runtime → 回落 _cfg 默认并告警）此前只有文件层覆盖（t9），meta 层缺口封口 | 传入 known 短路生效+零 findings 自查（双检测各一）+meta 坏值回落/合法透传（UC52-1/2/3） |
| R24 | **shutdown cancel 补齐 + 补偿链卸载（外部复审收口）**：①main.py 进程关闭 cancel 名单补 _patrol_task（与 obs 三循环同款收口：cancel + wait_for 5s）——此前漏 cancel，patrol_loop 无限循环写库任务在任何启动过 app 的进程（含 pytest TestClient）残留后台继续写库，是 main.py 注释记录过的同类真实事故（漏 cancel→用例成片假失败）复发；②compensate_finding 的载入与五同步 handler（持锁 DB 查询）经 asyncio.to_thread 卸载（D23 口径补全——扫描/回收已卸载而补偿链裸跑）；派发保持 async await、六段编排与短路语义不变 | cancel 在清理段+wait_for 收口（结构断言）/ 同步链工作线程执行+完整路径派发 / 短路零派发 / missing 早退（UC53-1..4） |

## 三、场景与场景用例（11+19 个场景）

- S1-S3 核心修复（检测/补偿/幂等），S4-S5 兼容回归，S6 观察口径，S7 防回归守卫，S8 归属与通知，S9 扫描模式与存量核对，S10 开关动态化与灰度，S11 补偿责任链全链路；S34-S36 实现后审计补口（findings 保留策略 / 手动触发 / 事件循环卸载）；S37 契约冻结；S38-S40 全链路审计（归属过滤 / single-flight / 慢扫描告警）；S41-S42 闸收口（全入口 single-flight / reclaim 卸载）；S43-S44 落库与责任链契约对账；S45 两段式拉取（性能 P1）；S46 窗口分支补口；S47 探活去重+谓词顺序；S48 批量落库；S49 keyset 分页流式；S50 seed 契约+接线钉死；S51 已知集轮内共享；S52 共享语义+meta 坏值补口；S53 shutdown cancel+补偿链卸载。

### S1【核心修复】声称交棒未交检测（R2 三层/R3）
环境：agent_tasks 有 completed 任务，result_text JSON `next_step="a2a-qa"`，完成 11 分钟；L1 事件表（全状态，含 completed）无对应 a2a_event；L2 同 issue/同场景管线节点下无 created_at 晚于本任务 completed_at 的活跃任务。
触发：巡检周期到达。预期：异常清单落库 + 告警；Phase 2 开启时按任务自报 next_step 解析目标棒（节点合法性由端点 _scene_node_check 兜底拒绝）→ finding 状态流转抢占（open→claiming）→ **终检 L1（全状态）+L2，仍无证据才合成 payload 直调原生 a2a_endpoint 补发**（带 compensated_by 标记进 context 落事件，防重由端点三重防重兜底），补后 meta 写 `compensated:{task_id}`（仅审计）。
用例：UC1-1 检测命中（completed+next_step=a2a*+L1 空+L2 空+超窗）；UC1-2 未超窗不报（完成 5 分钟内静默）；UC1-3 next_step 非 a2a 前缀不报；UC1-4 补偿后写审计标记（并发互斥靠状态流转，T4 验证）；UC1-5 L1 有 completed 事件（下游已跑完）→ 判交棒已发生不报；UC1-6 仓库无任何托管主体（已解除托管）→ 不补偿，finding 记 auth_unavailable 并告警（D14）；UC1-7 用户 PAT 托管仓（路径 B，host_user_id>0）→ 以托管用户身份补发成功（D14 适配非 bot token 驱动场景）；UC1-8 下游节点已有 created_at 晚于本任务 completed_at 的**已完结任务**（completed/failed，无活跃任务）→ L2 全状态命中，判交棒已发生不报（D11-L2 全状态）；UC1-9 子任务模式链（is_subtask_mode=1，parent_issue 在 issue_state）→ 补发 payload 从库恢复 parent_issue/is_subtask_mode/carried_context，与原交棒同构（D15）；UC1-10 模式字段不可恢复（is_subtask_mode=1 但 parent_issue 缺失）→ 挂起人工不补，finding 记 mode_unrecoverable（D15，不猜原则）；UC1-11 PR 订阅链任务（issue_id=`#pr_123`，next_step=a2a*）→ **排除出断链候选，零 finding**（PR 订阅只审查不流转，本无交棒语义，D17）；UC1-12 子任务链 L2 关联键=parent_issue+agent_type，主链同 issue 任务不构成子任务链的正向证据（D17 防误配）。

### S2【核心修复】僵尸任务证据化两档 + 队列僵尸确定性归类（R2/D12、A 类）
环境：①`stream_ended=1 AND status='running'` 的任务（终信号已到、状态未流转）；②running 超 2h 且 stream_ended=0（无信号）；③queued 任务且绑定**远端**引擎不可达（status≠online 或 last_seen_at 超时）或场景已不存在（四源解析 fallback=true，D12 评审三轮补实现）；④queued 超 24h 但引擎在线；⑤queued 任务绑定**本机**引擎（credential_id=0，installed+healthy）；⑥queued 任务 issue 无场景绑定（scene_id 空）。
触发：巡检周期到达。预期：①确定性高置信即时告警（Phase 3 终态化候选，引擎不会再有输出，误杀风险≈0）；②低置信只登记（进程崩得连信号都没发，数据供 Phase 3 分布分析）；③即时归类"永不调度"清单（引擎或场景任一确定性失效，detail 记 cause；issue A 类 17 条 8-30 老任务全中此判定），不待 24h；④仍登记观察（引擎在线但不调度，可能是别的故障）；⑤**不误判**——本机引擎无心跳语义，健康按 installed+healthy 判定（D12 形态跟随），运行正常则不报；⑥**不判死**——无场景绑定不构成"场景已不存在"证据（不猜）。
用例：UC2-1 stream_ended=1+running → 高置信 zombie_signal；UC2-2 running 超 2h+无信号 → 低置信登记；UC2-3 远端引擎 offline 的 queued → 即时归永不调度（cause=engine_unreachable）；UC2-4 queued 超 24h 引擎在线 → 登记观察；UC2-5 阈值内且引擎健康 → 不报；UC2-6 本机引擎（cid=0）健康但无 last_seen_at 的 queued → **不归永不调度**（远端心跳口径误用防回归判例，D12）；UC2-7 场景缺失（引擎健康+scene_id 指向不存在场景，解析 fallback=true）→ 即时归永不调度（cause=scene_missing）；无场景绑定（⑥）→ 不判死。

### S3【核心修复】停摆链 = 断链的 issue 视图（R2/D13）
环境：issue stage 非终态，stage 停在的节点任务 completed 后 L1/L2 均无后继。
触发：巡检周期到达（同一轮、同一证据链）。预期：输出 stalled_issue finding，与对应 broken_chain finding 同源（同 task/同证据输入），仅聚合视图不同；不再有独立的"N 分钟无事件"时间窗判定；双源可判定时 suggestion 给建议下一棒，对不上进人工清单。
用例：UC3-1 停摆 issue 出 stalled finding 且与 broken_chain 同源可对账；UC3-2 L1 有 completed 事件（其实交棒了）→ 两种视图都不报；UC3-3 stage 终态 → 不报。

### S4【兼容回归】既有机制不受影响
环境：正常链路在跑（agent 正常交棒）。
预期：巡检器零动作；complete 接口、self_loop 防线、假完成门槛、a2a-redo 手工补发按钮行为与改动前完全一致（不改动清单见设计文档 §4）。
用例：UC4-1 正常交棒链零误报；UC4-2 既有 tests/test_events_redispatch* 与 a2a-redo 相关测试全量回归绿。

### S5【兼容回归】巡检器自身故障不伤主链
环境：巡检扫描 SQL 慢/表锁/异常。
预期：巡检循环异常捕获 + 落 fallback 记录（仿 scheduler.py 退避），绝不影响 :3000 主服务与 :8080 webhook；扫描耗时超阈值只记日志不告警轰炸。
用例：UC5-1 巡检内部异常被吞并记录，主服务健康；UC5-2 扫描耗时指标落清单。

### S6【观察口径】no_route 事件显性化（C 类）
环境：push 等事件无路由被静默跳过。
预期：巡检清单/周报统计 no_route 计数与样例，不修复（记录不修型）。
用例：UC6-1 no_route 样例进观察清单；UC6-2 与 self_loop（设计正确的防线）区分统计。

### S7【防回归守卫】正确机制保持原样
预期（实现禁令，逐条写成测试）：① self_loop 拦截不因本需求放行任何 bot 评论（补偿通知不带评论路由语义，走告警通道不进事件管线）；② complete 单信号模式不变（不在 complete 里加同步兜底——被否方案）；③ a2a-redo 手工端点与幂等键 `a2a-redo:{seq}:{agent}` 不动，巡检只调用不复写；④ redispatch 拒绝 self_loop 事件的判定不变。
用例：UC7-1..UC7-4 各守卫一条结构断言测试；UC7-5 终态化唯一通道守卫——patrol 模块零 `UPDATE agent_tasks SET status` 终态写路径（结构断言），终态化只属 scheduler #537 通道（D18）。

### S8【归属与通知】findings 归属人维度（R8/D16，A 类根因修复）
环境：①断链 finding 属于路径 B 用户 PAT 托管仓（host_user_id>0）；②finding 属于 bot token 托管仓（host_user_id=0，有托管用户映射）；③finding 所属仓库已解除托管。
触发：巡检落库 finding。预期：①归属=host_user_id 用户；②归属=托管用户；③归属=unowned 标记（该条是"无人知晓"高危项，清单置顶）；Phase 1 通知通道不出（API-only），但归属字段每条必有——后续邮件/IM 通知按此路由，不再出现 A 类 17 条挂两月无人知晓。
用例：UC8-1 路径 B finding 归属 host_user_id；UC8-2 bot token 托管 finding 归属托管用户（S26 接线后真覆盖：test_s26_1）；UC8-3 无主体 finding 归属 unowned 且清单置顶；UC8-4 归属字段 NOT NULL 断言（每条 finding 必有归属）。

### S9【扫描模式与存量核对】两档 LOOKBACK（R9/D17）
环境：①存量核对模式（Phase 1 人工核对期）；②常规模式（Phase 2 起常态运行）。
触发：巡检周期到达（模式由配置切换）。预期：①LOOKBACK 覆盖全历史（≥60 条最老断链的 completed_at），发现条数 ≥ 存量 60（程序化对账，此乃验收第一条的数据来源）；②LOOKBACK 收敛到运行窗（如 7 天），历史 finding 不重扫（已有唯一约束防重）。
用例：UC9-1 存量模式扫出 ≥60 条并逐条落库；UC9-2 切常规模式后窗口收敛、历史 finding 不重插（唯一约束）；UC9-3 模式切换不改判定逻辑（仅窗口参数不同）。

### S10【开关与紧急控制】补偿默认开 + meta 运行期紧急控制（R4/D19 决策变更）

环境：①meta 全未配置（默认态）；②meta 置 `patrol:compensate_enabled=false`（运行中服务，不重启）；③黑名单 `patrol:compensate_blocklist` 列出 1 个仓；④黑名单=`*`；⑤开关值非法（如 banana）。
触发：巡检周期到达（每周期重读 meta）。预期：①补偿默认开、全仓生效——**能力即开即用，零配置**；②下个周期即全关——**无需重启**（紧急总闸）；③黑名单命中仓不补，其余仓照常；④全拦（紧急刹车）；⑤按关处理+warning（fail-fast，不静默吞配置错误）。Phase 2 已实现（S11）：开关开+仓不在黑名单即进入补偿责任链。
用例：UC10-1 未配置→默认开全仓生效；UC10-2 meta false→下一周期即时全关（不改代码不重启）；UC10-3 黑名单命中→该仓不补、其他仓放行；UC10-4 `*`→全拦（紧急刹车）；UC10-5 非法值→按关+告警；UC10-6 配置文件 `compensate_enabled: false`→seed 后静态关（meta 可再开）。

### S11【核心修复·Phase 2】补偿责任链全链路（R4/D20，已实现）
环境：一条 open 的 broken_chain finding，任务 next_step=a2a-qa、L1/L2 空、超窗；补偿默认开、仓不在黑名单。
触发：巡检周期到达（compensate_open_findings 批处理）。预期：责任链六处理器顺序执行——门卫放行 → 状态流转抢占（open→claiming，rowcount=1） → 终检 L1/L2 复查仍空 → 身份解析（策略族选主体） → 模式恢复（子任务链补 parent/carried_context） → 派发适配器直调原生 a2a_endpoint（带 compensated_by 标记 + 规范交棒键 handoff:{task_id}:{target_agent}，决策 A）→ finding 置 compensated + 回执 + meta 审计标记。**任一处理器拒绝即短路**：终检命中→false_positive；无托管主体→auth_unavailable 回挂零派发；模式不可恢复→mode_unrecoverable 挂起；二次进入→not_open 零重复派发。
用例：UC11-1 全链 compensated（目标棒/身份/标记/幂等键/审计五断言）；UC11-2 终检命中（入库慢）→false_positive 不补；UC11-3 无托管主体→零派发+finding 回挂；UC11-4 子任务模式恢复（parent/is_subtask 进 payload）与不可恢复挂起；UC11-5 防双发（同 finding 二次补偿→not_open，派发数不增）；UC11-6 抢占原子性（二次 ClaimHandler rowcount=0）。

### S12【防回归守卫·评审四轮】选主自续约通配符互斥（R1/D3）

环境：双机集群，主机名仅差一个 LIKE 通配符敏感字符（持有者 `host-a`，来者 `host_a`），`host-a` 持有新鲜 leader 锁（value=`host-a|iso`）。
触发：`host_a` 调 claim_patrol_leader 尝试自续约。预期：①`host_a` 被拒（走"他主持有且新鲜"分支返回 False）——旧实现 `value LIKE 'host_a|%'` 中 `_` 是 SQL 单字通配符，会匹配 `host-a|...` 跨主机续锁造成双主扫描（findings 唯一约束去重+补偿状态流转抢占兜底，零数据伤害，只是重复扫描）；②leader 值不被越权改写；③本主 `host-a` 自续约不受精确前缀改造影响（True）。实现落点：自续约改 `substr(value,1,len+?)=holder|` 精确前缀比较（patrol.py 自续约分支，substr 语义 SQLite/MySQL 一致）。**同族提醒已拆除（评审十五轮修正③/S28，原"本单不修登记在案"）：同一主机多 uvicorn worker 共享 hostname 全部通过自续约——已把持有者标识收紧为 `hostname#pid`（_hostname()），同机每 worker 唯一（live 进程 pid 不重复），选主恢复每 worker 一主；进程崩溃 pid 复用时新持有者续旧锁是安全的（旧持有者已死不会再动）。**
用例：UC12-1 通配符互斥——`host_a` 续 `host-a` 锁返回 False 且 leader 值仍以 `host-a|` 开头（无越权改写断言）；本主 `host-a` 自续约 True。

### S13【配置真源·决策变更】DB meta 运行真源 + 文件 seed + 零 env（D4/D9/D17/D19）

环境：①DB meta 键（patrol:interval_seconds / patrol:confirm_minutes / patrol:lookback_mode / patrol:compensate_enabled / patrol:compensate_blocklist，集群共享，MySQL 共库）；②configs/patrol-config.json（gitignored，example 入库）；③meta 值非法（如 `banana`）；④无调参决策的阈值。
触发：①主进程启动（首次 seed）；②巡检周期到达（每周期重读）；③运行期改 meta。预期：①**运行真源=DB meta**——改值即生效无需重启、双机主从共享一份配置（决策变更 2026-11-02 负责人拍板，runtime-config 优先级链同款：meta > 文件 > 代码默认）；②文件仅在 meta 无对应键时 seed 一次（login-whitelist→表迁移先例，db_setup._migrate_whitelist_from_file 同款语义），此后以表为准；无文件则纯代码默认（能力全开）；③**patrol 模块零 os.environ 读取**（结构断言防配置面回涨，原 8 项 env 已全数删除）；④坏值回落默认+告警，不炸循环不静默（fail-fast 口径与 GateHandler 对非法 meta 值一致）；⑤无调参决策的阈值（runtime 回看窗 7 天、僵尸窗 2h/24h）走模块常量，引擎心跳新鲜度单源引用 engine_select._RUNTIME_STALE_SECONDS。
用例：UC13-1 配置文件坏值逐键回落（test_t9）；UC13-2 源码结构断言零 env 读取+默认全开（test_t9b）；UC13-3 meta 真源——meta 改值即生效、坏值回落、缺省走默认（test_t10）；UC13-4 文件 seed 幂等——无键灌入、有键不覆盖、再跑零 seed（test_t10b）。

### S14【场景验收·决策变更】开箱即用与运行期处置端到端（R4/D19/S13 交叉）

环境：①全新部署（meta 全空、无配置文件）；②某托管仓断链 finding 已落库且黑名单摘除该仓；③测绘期出现"慢到"误报苗头。
触发：①补偿责任链批处理；②运行期改 meta；③巡检周期判定。预期：①**开箱即用**——零配置下断链 finding 从发现到补发全链路走通（门卫默认放行→抢占→终检→身份→模式→派发，compensated_by 标记与幂等键齐全）；②**摘除即止**——黑名单命中仓零派发、finding 保持 open 供人工处置（运维摘除通道不吞数据）；③**运行期调窗**——meta 收紧 confirm 窗（10→3 分钟）下个周期生效，不重启不清 finding。
用例：UC14-1 零配置全链补发（test_s14_1：meta 全空 → compensated + 路径 B 身份 + 标记断言）；UC14-2 黑名单摘除零派发（test_s14_2：repo_blocklisted + fake.calls=0 + finding 保持 open）；UC14-3 确认窗运行期收紧（test_s14_3：默认 10 分钟不报 → meta=3 即时报）。

### S15【评审五轮守卫】issue 终态盲区 + 补偿结论等价性（R4/R5/D11）

环境：①断链任务的 issue 已被用户直接收敛（confirm_issue_done → status='completed'，或审阅流 pending_review）；②finding 已 open 后 issue 才被收敛（检测-补偿竞态窗）；③补偿派发的下游 agent 读取 context。
触发：①巡检检测周期；②补偿责任链终检；③派发组装 payload。预期：①检测层排除终态 issue（completed/pending_review/cancelled）——L1/L2 只找"交棒证据"，找不到"人已处置证据"，链已被人关闭不是断链（issue_terminal 谓词，双守卫之一）；②终检前置 issue 终态守卫——命中即 false_positive 零派发（a2a 端点只拦 cancelled 不拦 completed/pending_review，webhook.py:1010 实钉，巡检必须自守），否则补偿会复活用户已亲手收敛的链（派发下游 agent、评论、回推 stage）——**这是对"误报=0"威胁最大的一类**；③补偿 context 携带原任务 result_text 结论（截断 600 字符）——手工 a2a-redo 带"基于原事件结论继续"指针，补偿不带则下游缺上游结论，等价性不成立。
用例：UC15-1 检测层终态排除（test_s15_1：completed/pending_review 零 finding，active 正常报）；UC15-2 终检守卫短路（test_s15_2：竞态窗内收敛 → false_positive + 零派发 + action_meta 注明"已终态"）；UC15-3 结论随 context 下发（test_s15_3：compensated + "原任务结论"+结论原文断言）。

### S16【评审六轮修正】永久失败终态化 + 子任务 L2 接线（R4/D17/D11）

环境：①补偿派发命中端点永久拒绝（agent disabled 403 / not_in_scene 409 / skipped 类语义拒绝）；②派发命中 5xx/网络异常（瞬态）；③子任务链（issue_state is_subtask_mode=1）断链候选做 L2 反查。
触发：①②补偿责任链派发处理器；③检测周期与补偿终检。预期：①永久失败**终态化**——finding 置 permanent_failure（非 open，批处理不再捞），verdict=dispatch_permanent_failure；旧实现一律回挂 open 每 5 分钟重试到永远（action_meta 反复截断、日志刷屏，幂等键保证不重复所以是"吵"不是"错"，本修治运维无伤）；②瞬态失败回挂 open 下轮再试（行为不变）；③**子任务 L2 关联键接线**——l2_downstream_exists 的 parent_issue 形参此前无人传（D17 承诺未兑现），现检测与终检都经 _subtask_parent 取 issue_state 的 parent_issue：主链同 issue 任务不构成子任务链正向证据（防误配→漏报），parent_issue+agent_type 命中才构成；批处理捞单改 ORDER BY created_at（FIFO——id 是 UUID，按 id 排序=随机挑 20 条，六轮小项）。
用例：UC16-1 永久失败终态化（test_s16_2：403→permanent_failure 非 open；500→open 重试）；UC16-2 子任务 L2 接线（test_s16_1：同 issue 主链任务不构成证据出 finding，parent_issue 命中不出）；UC16-3 批处理 FIFO（test_s16_3：三条 open 错开 created_at，limit=1 先消费最老）。

### S17【评审七轮修正】子任务键双源 + 结论结构化提取（D17/D15）

环境：①子任务标记只在 issue 单表（Phase C3 停写 issue_state，C3 新建子任务链的常态形态）；②补偿 context 的结论来源。
触发：①检测/终检 L2 关联键解析；②派发组装。预期：①`_subtask_parent` 双源读取——与 ModeHandler/get_issue_stage 同款（issue 单表权威 + issue_state 过渡兜底），只读 issue_state 会漏 C3 新建链（L2 退化主链键、丢 D17 防误配保护，与同文件 ModeHandler 口径不一致）；②补偿 context 结论优先取 result_text JSON 的结构化 `conclusion` 字段（整段 JSON 下发=下游自己再解析一遍），解析失败/无字段回落原文，600 字符截断不变。
用例：UC17-1 issue 单表形态子任务键（test_s17_1：无 issue_state 行仍取 parent 键，主链同 issue 不构成证据、parent 命中构成）；UC17-2 结论结构化提取（test_s17_2：conclusion 值下发且 context 不含 JSON 键名）。

### S18【部署实测修正】存量库建表迁移 + issue 级唯一键（2026-11-02 本机部署实测发现）

环境：①存量库（grape_t_schema_migrations 台账已盖章，legacy 全量首跑已完成）启动；②issue 级 finding（stalled_issue，task_id 恒空串）落库。
触发：①主进程启动迁移段；②巡检批处理插入 stalled_issue。预期：①grape_t_patrol_findings 经注册 safe 迁移（202611020001）自动建表——原 _NEW_TABLES 注册只服务 legacy 首跑路径，已盖章库启动只跑 _MIGRATIONS，表永不创建（测试全绿是因测试手工执行 DDL 掩盖了启动链路，部署实测当场暴露）；②唯一键含 issue_id——原三列键 UNIQUE(kind, task_id, event_seq) 使全部 issue 级发现共享一键，实测 14 条 stalled 只落 1 条；迁移 202611020002 将旧形态表重建为四列键（保数据）。
用例：UC18-1 存量库迁移自动建表（test_s18_1：注册存在性/幂等/DROP 后重建）；UC18-2 issue 级行不被吞（test_s18_2：三 issue 各一条+同 issue 去重）；UC18-3 旧键表重建保数据（test_s18_3：形态断言+幂等）。

### S19【评审八轮修正】claiming 租约回收 + duplicate 分桶 + 审计键退役（R4/R6/D20）

环境：①进程在终检/派发途中崩溃（kill -9/OOM），finding 永久停在 claiming；②终检后、派发前下游新事件到达，端点返回 duplicate_handoff；③补偿成功后的审计通道。
触发：①批处理周期；②派发处理器失败分支；③派发成功分支。预期：①claiming 带租约（10 分钟，复用 updated_at）——超时孤儿回收为 open 下轮重试（幂等键保证不双发），租约内的新鲜 claiming 不动（他实例处理中）；②duplicate_handoff 是"交棒已发生"的正向证据 → false_positive 分桶（原落 permanent_failure 会污染误报率统计口径）；③compensated:{task_id} meta 审计键退役（无 TTL 无清理线性膨胀 + check-then-act 两步写）——追溯由 finding.action_meta 回执全文 + 事件 context 的 compensated_by 标记双通道承载。
用例：UC19-1 租约回收（test_s19_1：孤儿回收→重试 compensated、新鲜 claiming 不动）；UC19-2 duplicate 分桶（test_s19_2：false_positive）；UC19-3 响应契约钉死（test_s19_3：JSONResponse body 解析 + dict 失败形态）。

### S20【评审九轮】github 键形态官方判例（provider 无关性钉死）

环境：官方套件哨兵原全为 gitcode: 前缀（仿真已证 github 形态无 provider 分叉，但官方判例缺一边）。
触发：github: 前缀 repo_key 的检测/掩蔽/终态/黑名单/前缀剥离。预期：①断链命中；②L1 掩蔽同构；③issue 终态排除同守卫；④黑名单按全前缀精确匹配（github 拦、gitcode 哨兵不受误伤）；⑤派发载荷前缀剥离（github:owner/repo → repository/account 同构）。
用例：UC20-1 github 键五判例一体的官方判例（test_s20_1）——未来有人引入 provider 分叉时测试报警。

### S21【决策 A】规范交棒键 handoff:{task_id}:{target_agent} + 三护栏（R4/D5）

环境：①patrol 派发组装幂等键；②端点返回幂等命中响应（200 ok + idempotent:true，原生交棒先到或本键先补过）；③键派生入参。
触发：①②③派发处理器。预期：①派发键改用平台规范交棒键（单源函数派生，与 skill 守门件未来同式）——迟到原交棒撞同键被端点幂等命中，残余竞态降级为良性先到先得；skill 侧落地前行为与旧 patrol-comp 键完全等价（单调不劣）；②幂等命中 → compensated 且 action_meta 显式记 idempotent_hit（护栏③）；③空/空白 task_id 或 target_agent 即抛 ValueError（护栏②：唯一索引对 NULL/空互不相等，空键=永不幂等）；端点 idx_events_idem 唯一索引存在性断言（A 的实证基础，护栏①配套）。
用例：UC21-1 单源派生 + 非空护栏 + 索引存在断言（test_s21_1）；UC21-2 幂等命中打标（test_s21_2：compensated + action_meta 含 idempotent_hit）。

### S22【评审十轮修正】MySQL 方言纪律（租约回收去 || 拼接）

环境：双后端（SQLite 现网 / MySQL 兼容层）下的巡检 SQL；本机无 MySQL 实例，用兼容层机制做方言级验证。
触发：①claiming 孤儿租约回收 UPDATE；②迁移 202611020002 的建表形态判断。预期：①回收改读-改-写两段参数化 UPDATE（_set_finding 同款模式）——原 `action_meta=action_meta||'…'` 在 MySQL 默认 sql_mode 下 || 是逻辑 OR，静默把审计回执写成 '0'/'1' 且无告警（兼容层 _CONCAT_RE 只翻译 'gitcode:'||col 形态，其余穿透）；②sqlite_master 形态判断保持字面量名形态——命中兼容层 _SQLITE_MASTER_SQL_RE 特判（Cursor 走 SHOW CREATE TABLE，双方言一致）；评审所指"参数化穿透"经机制核实与本实现不符（原代码即字面量形态），已加注释钉死口径。
用例：UC22-1 方言级判例（test_s22_1：源码零 || 拼接结构断言 + 回收 UPDATE 参数化形态 + _SQLITE_MASTER_SQL_RE 命中 + _CONCAT_RE 不匹配防回退）。

### S23【第十二轮修正】A6 表名规则 + 孤儿回收无条件执行（全家族规则/R4）

环境：①主控新增数据库表（A6 命名规则：grape_t_ 前缀）；②补偿总闸关闭状态下的 claiming 孤儿。
触发：①建表迁移执行；②巡检周期到达（任一开关状态）。预期：①表名 grape_t_patrol_findings（全链 95 处引用同步：DDL/迁移/业务 SQL/测试/API 文档/规格文档），本机重置台账后实测新名表自动建出 + 双迁移盖章；②回收是卫生操作不受补偿开关门控——原实现埋在 compensate_open_findings 内被开关门控（实测开关关死时孤儿滞留 claiming），现提取 reclaim_stale_claimings() 独立函数，patrol_loop 每周期在开关判定**之前**无条件执行（回收只改状态不派发，开关关闭时零副作用）；实测：开关关死 + 造孤儿（带审计回执）→ 下周期回收为 open、action_meta 读-改-写保留原文追加回收标记。
用例：UC23-1 A6 命名结构断言（test_s23_1：sqlite_master 表名前缀 + 源码零裸名引用 + reclaim 在开关判定前无条件执行）；UC19-1 语义升级（test_s19_1 改用独立函数双判例：孤儿回收≥1 / 新鲜 claiming==0）。

### S24【评审十三轮修正】首轮即时巡检 + 清单排序稳定 + 队列僵尸单次探活（体验/性能）
环境：①服务冷启动（patrol_loop 首轮）；②GET /api/patrol/findings 多条 finding、连续刷新两次；③queued 任务引擎不可达。
触发：①启动后立即；②清单查询；③队列僵尸判定执行。
预期：①主循环**先干活后睡**（run_patrol_once 在周期 sleep 之前）——启动即出第一份清单，不等首个 interval（原实现启动后首个 5 分钟为空窗）；②unowned 置顶之外按 created_at 倒序（id 是 UUID，按 id 排序=每次刷新随机序、列表序不稳定）；③同一 runtime 的引擎探活只执行一次（判定与 detail.cause 拼接复用同一次调用——原实现同一条件对库查两次）。
用例：UC24-1 首轮即时（test_s24_1：循环体结构断言 run_patrol_once 先于周期 sleep）；UC24-2 排序稳定（test_s24_2：SQL 文本守卫 ORDER BY (owner=0) DESC, created_at DESC）；UC24-3 单次探活（test_s24_3：调用计数 mock 断言恰好一次 + cause=engine_unreachable）。

### S25【PR #802 评审缺口闭环】巡检路由断言覆盖（闭 N2 BLOCK：dismiss 端点零覆盖）
环境：GET /api/patrol/findings 与 POST /findings/{id}/dismiss 两条路由（PR #802 原子回归报告二.5 行为级覆盖门 BLOCK 项：写端点 dismiss 无任何断言引用；应补用例含 GET/POST 的成功面字段契约与不存在拒绝面）。
触发：路由函数直调（鉴权依赖 require_no_session_token 由平台层 401 原子用例看护），数据访问全部 mock（脚本化 _RouteDb：SELECT 行脚本 + UPDATE rowcount 队列），不连真实数据库。
预期：①GET 成功面——status=ok、total、13 字段契约齐全、unowned 置顶（行序模拟 ORDER BY 语义，SQL 钉死由 UC24-2 双保险）、owner 归一（兼容层 str→int，非 int→0）；②status/kind 过滤参数进 WHERE；③dismiss：open→200 dismissed；已收敛（rowcount=0）→ 404。
用例：UC25-1 成功面契约+置顶+owner 归一（test_s25_1）；UC25-2 过滤面（test_s25_2）；UC25-3 dismiss 200/404（test_s25_3）。

### S26【评审十四轮修正①】findings 归属路径 A 接线（R8/D16/UC8-2 补口）
环境：①bot token 托管仓（managed_repos.host_user_id 无值，user_repos 有 is_admin=1 行）；②路径 B 用户 PAT 托管仓（host_user_id>0）；③双源皆空（已解除托管）。
触发：insert_finding 落库前解析归属（_resolve_owner）。
预期：①归属=托管用户映射（user_repos 管理员，与 IdentityHandler.ManagedUserFallbackStrategy 同口径），不再误标 unowned（误置顶=信号过载，后续通知路由漏派）；②路径 B 命中即返回、优先级高于路径 A；③双源皆空=0（unowned 置顶，语义不变）。原实现只查 host_user_id，路径 A 托管仓全量误标 unowned。
用例：UC26-1 路径 A 回落命中（test_s26_1：FakeDb 脚本双查询）；UC26-2 路径 B 优先 + 双空 unowned（test_s26_2）。

### S27【评审十四轮修正②】选主总闸：非主实例 skip 整轮动作（R1/D3 对齐）
环境：多机部署（或同库多实例），非主实例的巡检周期到达。
触发：patrol_loop 单周期执行。
预期：claim_patrol_leader 判定先行——非主实例跳过整轮（claiming 孤儿回收、补偿批处理、扫描），与设计 §二"谓词选主 → 非主 skip"对齐。原实现回收/补偿在选主判定之前执行：防双发由 ClaimHandler rowcount + 幂等键结构性兜底（零数据伤害），但每台都跑补偿批处理=重复扫描/日志/DB 压力。S23 语义保持：回收仍在补偿开关判定之前（开关关也回收，仅被选主门控）；S24 语义保持：首轮先干活后睡。
用例：UC27-1 选主门控结构断言（test_s27_1：claim 先于 reclaim/compensate/scan，reclaim 仍先于开关判定）。

### S28【评审十五轮修正③】选主持有者标识收紧 hostname#pid（S12 同族拆除）
环境：同一主机跑多个 uvicorn worker（扩容形态），共享同一 hostname；worker A（pid=111）持新鲜 leader 锁（value=`h#111|iso`）。
触发：同主机另一 worker B（pid=222）调 claim_patrol_leader。
预期：①`h#222` 的自续约 substr 精确前缀不命中（`h#222|` ≠ 锁前缀 `h#111|`），读到新鲜锁 → False，锁原值不动——hostname 相同不再互开自续约（原 holder 仅 hostname 时同机全部 worker 通过前缀自续约=双主扫描，S12 同族）；②本持有者 `h#111` 自续约 True 不受影响；③持有者标识形态断言 hostname#pid（`#` 分隔不与 `holder|iso` 的 `|` 冲突；进程崩溃 pid 复用续旧锁安全——旧持有者已死）。
用例：UC28-1 标识形态（test_s28_1）；UC28-2 同机不同 pid 不能互续 + 前缀参数含 pid（test_s28_2）；UC28-3 本持有者自续约（test_s28_3）。

### S29【评审十六轮修正①】补偿批处理饥饿治理：verdict 分桶 + 失败冷却（R4/R5/D20）
环境：①target_unresolvable / task_missing / mode_unrecoverable 的 finding（数据不自愈，永不恢复）；②auth_unavailable / dispatch 瞬态失败的 finding（可自愈）；③新落库 open finding；④存量卡死类 finding > 20 条。
触发：compensate_open_findings 每周期捞单。
预期：①不自愈三类置终态 needs_human（不进批处理，不再每 5 分钟空转重走全链——action_meta 反复追加截断 + 日志刷屏治掉）；②可自愈类保留 open 但按 updated_at 冷却（30 分钟窗内失败过的不捞；action_meta 为空=从未尝试，不受冷却限制，新 finding 首轮即处理）；③FIFO 队头不再被永不自愈类永久占据——原实现 >20 条卡死 finding 时，后面真正可补偿的 finding 永远轮不到（饥饿缺陷）；④S16 已修的 dispatch_permanent_failure 分桶语义不变。auth_unavailable 保留 open 而非 needs_human 的理由：管理员补绑凭据后可自愈，终态化会锁死恢复路径。
用例：UC29-1 不自愈三类终态 + 捞单 SQL 钉死（test_s29_1）；UC29-2 冷却谓词 SQL + 行为面（test_s29_2）；既有 test_recheck_handler_evidence_and_guards / test_mode_handler_subtask_recovery 断言同步 needs_human。

### S30【评审十六轮修正②】dismiss 归属校验（R8/D16 权限缺口闭口）
环境：①归属人（owner=user.id）dismiss 自己的 finding；②非归属人普通用户；③unowned（owner=0）高危项；④admin。
触发：POST /api/patrol/findings/{id}/dismiss。
预期：①200 dismissed；②③非归属人/unowned 仅 admin（admin_accounts 台账核对）可收敛，其余 403 零写入——owner 落库的归属人维度必须在唯一人工收敛入口生效，否则任意登录用户可消音他人告警（含"无人知晓"高危项）；④admin 兜底放行。附带修掉返回字面量的重复 status 键（{"status":"ok",...,"status":"dismissed"} 是无效 JSON 形态，Python dict 取后者、"ok" 恒不可见）——改为 finding_status 字段，契约文档同步。
用例：UC30-1 归属人 200 / 非归属非 admin 403 / admin 放行（test_s25_3 扩展 + test_s30_1）。

### S31【评审十六轮修正③】audit 重扫已知名短路（R9/R1 性能）
环境：lookback_mode=audit（存量核对模式）长期运行，findings 表已有 N 条（含 dismissed/false_positive/compensated/needs_human 各终态）。
触发：每周期全历史重扫。
预期：唯一约束只防重插不防重扫——已落库 (kind, task_id) 短路逐任务反查（L1/L2/引擎探活），重扫成本从 O(任务数×反查) 收敛到 O(任务数)+1 条集合查询；audit 忘切回 runtime 不再是慢性病。僵尸档位翻转（running_window→signal）键不同仍可重新判定，不丢升级证据。db_lock 内只剩候选 fetchall（毫秒级）与集合查询，长锁窗口消除。
用例：UC31-1 已知名不重查不重产出（test_s31_1：broken_chain + queued 僵尸双面）。

### S32【评审十六轮修正④】L1 棒次过滤（R2/D11/L1 口径收紧）
环境：任务 T（next_step=a2a-qa）completed 后，同 issue 出现过其他棒次的 a2a_event（如人工补发早前链路的 reviewer 交棒）。
触发：L1 反查（检测与终检同口径）。
预期：target_agent 已知时只认 agent_type 匹配该棒的 a2a_event——其他棒次事件不再构成"本棒已交棒"证据（原宽口径会把真断链误判为已交接：漏报且检测层无痕，事后无从发现）。口径与端点 active-guard（webhook.py:86 同按 agent_type 过滤）对齐，比较大小写不敏感；target 不可解析时保持宽口径（任意 a2a_event 即证据，误报=0 优先）。风险自审：next_step 名与事件 agent 名漂移时 L1 会漏证据 → 检出 finding → 终检同口径仍空 → 派发撞端点 #329 活跃去重/duplicate_handoff → false_positive 分桶，闭环不产生重复派发（名字漂移本身即端点 not_in_scene 拒绝的形态）。
用例：UC32-1 他棒事件非证据/本棒匹配证据/宽口径兜底（test_s32_1）。

### S33【评审十七轮修正】派发后终态回查 + 对齐收敛（R4/S15 残余竞态，消灭伤害而非消灭窗口）
环境：补偿责任链终检通过（issue 当时非终态）→ 派发成功 → 用户在派发前后任意时点将 issue 收敛为 completed/pending_review。**实核支点（修正评审十六轮前的错误认知）：confirm_issue_done（用户主路径，issues.py:677-703）只置 issue.status='completed' + VCS 关单，不清扫 agent_tasks/events——清扫只存在于 cancel_issue（含引擎 kill）与 admin-confirm 的 _sync_issue_pipeline_terminal 两条路。故 completed 路径下收敛晚于派发的任意时点，孤儿下游任务都存活（收敛早于派发被终态守卫拦截），对齐收敛是该路径唯一清障手段。**
触发：DispatchHandler 派发成功后的终态回查。
预期：①回查命中终态 → 立即调用 **_sync_issue_pipeline_terminal（复用 completed 路径官方收敛动作，admin-confirm 同款：任务→cancelled、事件→completed、不做运行时 kill——其 docstring 明示"避免误杀仍在写回的引擎"，为 completed 路径既定取舍，巡检沿用；引擎继续写回的残响由观测层留痕）**；②排 5 秒延迟二次扫（下游任务由事件总线异步创建，派发返回≠任务已诞生，首轮扫空时兜底；_sync 幂等重复调用零副作用）；③race_detected:{终态}→aligned:{n} 回执进 action_meta（观测数据源：毫秒窗在真实流量的发生频率由此说话）；④不撤销派发本身（零破坏：不补偿补偿、不删事件——收回动作只走 _sync 通道）。**第 3 层零窗根治（FOR UPDATE 行锁事务串行化派发与收敛）明确不做、登记为数据触发项：共享单连接架构下事务跨 await 会挂住整进程 DB 车道；触发条件=观测层证明 race 真实发生且对齐收敛有残留场景。**
用例：UC33-1 命中终态→对齐调用+回执+延迟扫排程（test_s33_1）；UC33-2 非终态零动作（test_s33_2）；UC33-3 委托 _sync+D18 红线结构断言（test_s33_3：patrol 双模块零 agent_tasks 终态化 SQL，语义单 owner）；UC33-4 延迟二次扫幂等（test_s33_4）。

### S34【实现后审计】findings 终态行保留策略（R11/D21）
环境：grape_t_patrol_findings 累积运行——dismissed/false_positive/compensated/permanent_failure 四终态行随核对推进持续增长（runtime 模式每周期全量重扫已知集，audit 模式全历史扫描，表只增不减=无界增长模式）。
触发：巡检周期到达（cleanup_terminal_findings 随 run_patrol_once 执行）。
预期：①四终态且 updated_at 早于 retention 天数（meta `patrol:findings_retention_days`，默认 90）的行被删，删除数 purged 入回执与日志；②open/claiming（活性工作队列）与 needs_human（未决人工信号）**永不清理**——删 needs_human=丢人工线索，违反零危害口径；③retention=0 显式永不清理（保留全量审计轨迹的紧急语义）；④坏值回落默认 90 并告警，不静默不清扫（fail-fast 口径与 _meta_int 一致）。
用例：UC34-1 终态集合钉死+90 天 cutoff（test_s34_1）；UC34-2 retention=0 零 DELETE（test_s34_2）；UC34-3 坏值回落默认（test_s34_3）；UC34-4 purged 入 run_patrol_once 回执契约（test_s34_4）。

### S35【实现后审计】手动触发巡检（R12/D22 运营介入面）
环境：运维发现清单疑似滞后（自动周期最长 5 分钟延迟）或想立即核对判定口径；API 此前只有 findings 查询 + dismiss，无手动触发面。
触发：POST /api/patrol/scan（user_only 认证，同 findings 端点口径）。
预期：①立即执行一轮检测（mode=运行真源档），回显 scan 统计（broken/zombie/stalled/inserted/purged/duration_ms）；②**刻意只做检测面**——不补偿、不抢主：补偿仍走选主门控的自动循环（D3/D20），手动轮与自动轮靠 findings 唯一约束幂等汇合（同发现不重插）；③终态行保留清理（R11）随手动轮一并执行；④扫描异常 500 收口不外溢（S5 同款：巡检自身故障不伤主链）；⑤同步扫描经 asyncio.to_thread 卸载（D23）。
用例：UC35-1 端点契约+to_thread 卸载+mode 语义（test_s35_1）；UC35-2 异常 500 收口隔离（test_s35_2）。

### S36【实现后审计】事件循环卸载（R12/D23）
环境：run_patrol_once 是持 db_lock 的同步全表扫描（detect_broken_chains 全表 + 逐任务反查 events）；此前主循环裸调——扫描期间整个 FastAPI 事件循环被卡住，任务量增长后每 5 分钟出现 API 毛刺（审计实测项）。
触发：巡检周期到达（自动轮）或手动 scan（S35）。
预期：①两处扫描调用一律 `await asyncio.to_thread(run_patrol_once)`——同步函数卸载到工作线程，API 事件循环不再被整轮冻结；②卸载不改变扫描语义（首轮即时、选主门控、顺序约束全部保持——S24-1/S27-1 结构断言改钉 to_thread 形态后仍全绿）；③DB 车道竞争（db_lock 仍被扫描持有）登记为已知残留，根治需行锁事务方案（S33 已登记为数据触发项，不在本单）。
用例：UC36-1 to_thread 卸载形态+裸调防回退（test_s36_1）。

### S37【实现后审计收尾】规范交棒键契约冻结（R13/D5 决策 A 的跨仓对齐靶面）
环境：skill/worker 侧守门件尚未按决策 A 同式派生（另行立项，见 §五）；迟到原生交棒与补偿撞同键的前提=两侧键格式逐字符一致——键格式是跨仓契约，平台侧单方面改形即静默撕毁契约。
触发：任何改动 handoff_idempotency_key / HANDOFF_KEY_TEMPLATE 的提交跑 CI。
预期：①模板字面量 handoff:{task_id}:{target_agent} 冻结；②派生函数与模板严格同构（单源护栏①）、同输入恒同键（确定性）；③前后空白规整为同一键（" t-abc " 与 "t-abc" 不产生两个键——否则迟到交棒仅因空白差异绕开幂等）；④None/空/纯空白任一即 ValueError（护栏②：空键=唯一索引对 NULL 互不相等=永不幂等）。
用例：UC37-1 模板冻结+同构+确定性（test_s37_1）；UC37-2 空白规整同键（test_s37_2）；UC37-3 空参拒绝（test_s37_3）。

### S38【全链路审计】findings 清单归属过滤（R14/D24）
环境：GET /api/patrol/findings 此前 WHERE 只有 status/kind——任意登录用户可见全平台所有仓库的清单（repo_key、issue_id、detail 判定快照）；写口子 dismiss 有 S30 归属校验，读口子没锁（不对称=BOLA 面）。
触发：任意非 admin 用户调用 findings 清单。
预期：①admin 全量可见（管理页口径，unowned 置顶排序不变）；②非 admin 仅见 owner=自己 的行（WHERE owner=?），unowned（owner=0，"无人知晓"高危项）仅 admin 可见（与 S30 的收敛口径对称）；③admin 台账查询异常 fail-safe 降级为归属过滤（宁可少看不可多看）；④R8 归属字段仍在行内，告警路由不受可见面收窄影响。
用例：UC38-1 非 admin owner 过滤（test_s38_1）；UC38-2 台账异常降级（test_s38_2）；admin 全量+unowned 置顶回归并入 test_s25_1（admin 视角改造）。

### S39【全链路审计】手动轮 single-flight 闸（R15/D25）
环境：POST /api/patrol/scan 直接 to_thread 执行——自动轮有选主总闸（D3），手动轮无对称闸：N 个并发请求=N 个并发全表扫描，各自抢 db_lock 互踩（audit 全历史扫描期间 HTTP 请求还无限挂住）。
触发：手动轮在跑期间第二发并发请求。
预期：①409 拒绝第二发（在跑标志，进程内单闸足够——单实例内并发是唯一形态，跨实例手动轮靠 findings 唯一约束幂等汇合）；②首轮完成（成功/异常两路径）finally 释放闸，闸不死锁；③409 语义与 500（扫描自身异常）可区分——闸状态不掩盖扫描故障。
用例：UC39-1 并发 409+成功路径释放（test_s39_1）；UC39-2 异常路径释放+可再触发（test_s39_2）。

### S40【全链路审计】慢扫描告警（R15/F 项，检测+报告零危害）
环境：detect_broken_chains 为 N+1 反查形态（每候选 2-3 条索引查询，idx_events_repo_issue/idx_agent_tasks_status 在位）——当前量级毫秒级（实测 7-87ms/轮），MySQL 10 万级任务时靠 S31 短路收敛重扫面；量级突变无显性信号。
触发：run_patrol_once duration 超过 _SLOW_SCAN_WARN_MS（30s 模块常量——无调参依据前不进 meta，与 _LOOKBACK_RUNTIME_DAYS 同款纪律）。
预期：①warning 告警（含候选数与复核指引：S31 短路健康度+索引），不熔断不阻断回执；②阈值语义 > 严格比较（恰等于不告警，防噪音）；③默认阈值钉死 30000ms（约 60× 实测上界）。
用例：UC40-1 超阈值告警+不熔断+默认值钉死（test_s40_1）；UC40-2 未超阈值零告警+边界严格比较（test_s40_2）。

### S41【全链路审计·闸收口】全入口 single-flight（R16/D26）
环境：第 15 轮的 _scan_in_flight 在路由层——只挡手动轮对手动轮；自动轮正在扫描（to_thread 中）时手动请求进来，或手动轮在跑时自动周期到达：两轮全表扫描并发互踩 db_lock（路由快检未命中的窗口）。
触发：任一入口在 run_patrol_once 在跑期间再次进入（自动轮/手动轮任意组合）。
预期：①函数级闸（threading.Lock，线程安全）挡第二发，抛 PatrolScanInProgress——inner 不执行；②自动轮捕获该异常记 skip 日志（良性跳过，不进退避——退避会把 5 分钟周期人为拉长）；③手动轮映射 409；④inner 异常路径 finally 释放闸（闸不死锁）；⑤闸语义与扫描故障可区分（500 来自 inner 本身）。
用例：UC41-1 函数级闸并发+释放（test_s41_1）；UC41-2 异常释放+可再进入（test_s41_2）；UC41-3 自动轮 skip 良性语义结构断言（test_s41_3）。

### S42【全链路审计·D23 收尾】reclaim 卸载出事件循环（R16）
环境：D23 只卸载了 run_patrol_once——loop 内 reclaim_stale_claimings 仍是持 db_lock 的表扫描同步段直调（claiming 全表扫描），扫描轮已卸载而回收轮裸调=卸载面不完整。
触发：巡检周期到达（选主成功后的孤儿回收段）。
预期：①reclaim 经 await asyncio.to_thread 卸载（与扫描同款）；②卸载不改变顺序约束：选主 → 回收 → 补偿开关判定（S23/S27 语义保持，回收仍无条件执行仅被选主门控）；③不留裸调形态（结构断言防回退）。
用例：UC42-1 卸载形态+顺序保持+防回退（test_s42_1）。

### S43【覆盖率对账】insert_finding 落库契约（R17/R6/R8）
环境：insert_finding 此前零用例直接覆盖（detect 测试 mock 在 detect_broken_chains 层，run_once 测试又把 insert 打桩掉）——R6 的"id 一律 UUID 禁自增"、R8 的"owner 随行落库"两条硬性要求从未被行为断言；且实测抓出 cursor() 在 try 外的缺陷（DB 异常外抛，违反 S5 吞掉语义）。
触发：任一轮扫描的 findings 落库（含双主/手动轮×自动轮并发汇合）。
预期：①id 为 UUID 形态（Python 侧生成）、task_id 为 agent_tasks UUID 或空串、owner=归属解析结果随行、status 默认 open、created_at/updated_at 同刻；②唯一约束去重命中（rowcount=0）→ False；③落库异常吞掉返回 False 不外抛（单条失败不伤整轮循环——缺陷已修：cursor() 移入 try）。
用例：UC43-1 UUID/归属/默认值/rowcount=1（test_s43_1）；UC43-2 rowcount=0 幂等汇合（test_s43_2）；UC43-3 异常吞掉（test_s43_3）。

### S44【覆盖率对账】补偿责任链顺序冻结（R17/D20）
环境：build_chain 此前零用例——D20 编排顺序是设计红线（门卫→抢占→终检→身份→模式→派发），任何重排都是静默语义破坏；DispatchHandler 为 async 接口须在同步链后单独 await。
触发：补偿批处理对单条 finding 的责任链组装。
预期：①同步链五处理器顺序钉死：GateHandler→ClaimHandler→RecheckHandler→IdentityHandler→ModeHandler（重排实例：终检挪到抢占前=先查后抢=竞态窗口回潮；门卫离首=开关/黑名单失效仍耗 DB）；②DispatchHandler 不在同步链（async handle 在同步循环里只拿到协程对象=派发静默失效）；③compensate_finding 内同步链跑完后单独 await 派发段（六段编排不变）。
用例：UC44-1 顺序冻结（test_s44_1）；UC44-2 派发隔离+六段编排（test_s44_2）。

### S45【性能·P1】两段式拉取：result_text 只为幸存候选回捞（R18）
环境：候选拉取此前整表带 result_text（MEDIUMTEXT 2KB+/行）——S31 短路在 Python 侧、SQL 已把 7 天窗内全部 completed 任务的大字段拉回（磁盘 SQLite 实测：20k 任务带文本 39.8MB/轮 vs 轻量列 1.2MB；MySQL RDS 下每轮 40MB 网络读 ≈ 470MB/小时，纯浪费——已知/终态/未超窗任务占大头）以下数字均为**作者环境报告值**（单机合成基准），验收时在目标环境复测；SQLite 本机场景无网络搬运、收益≈零，受益面为 MySQL/RDS 部署。）
触发：每轮 detect_broken_chains。
预期：①阶段 1 只拉轻量列（id/repo/issue/agent/completed_at）；②全部不依赖 result_text 的短路前置：已知→PR 订阅→issue 终态→缺键→未超窗（纯谓词重排，结果集与产出顺序不变）；③阶段 2 只对幸存候选按 id IN 回捞 result_text，超 500 分块（不生成超宽 IN）；④回捞单块异常吞掉——缺文本候选判 "" 走不报口径（不猜，S5）；⑤判定语义零变化（L1/L2/产出不变，回归 93 用例全绿）。
用例：UC45-1 幸存者独享回捞+阶段 1 无大字段（test_s45_1）；UC45-2 500 分块（test_s45_2）；UC45-3 回捞异常吞掉（test_s45_3）。

### S46【覆盖对账·P1 补口】runtime 窗口分支的两段式行为（R18 延伸）
环境：覆盖对账发现 detect 全部既有用例均传空 cutoff（audit 分支）——生产默认路径（runtime 模式每 5 分钟、cutoff=7 天前）的两段式行为零覆盖：窗口谓词丢失/参数错位/大字段回流都不会爆红。
触发：runtime 常态轮（cutoff 非空）与 audit 轮（cutoff 空）两分支。
预期：①runtime 分支阶段 1 SQL 带 `completed_at > ?` 谓词、cutoff 以绑定参数承载（谓词丢失=静默退化为全历史扫描）、仍不含 result_text 大字段，幸存者照常 id IN 回捞；②audit 分支无窗口谓词、无绑定参数（全历史口径）——两分支形态互斥钉死（文本守卫对，防互相漂移）。
用例：UC46-1 runtime 窗口下推+参数承载+大字段排除（test_s46_1）；UC46-2 audit 无谓词无参数（test_s46_2）。

### S47【性能·分析人清单】探活去重缓存 + 谓词顺序修正（R19）
环境：分析人清单两项——①queued 僵尸判定的跨任务 N+1：引擎探活（agent_runtimes 单查）与场景解析（issue/issue_state 双源 + 管线四源解析链）逐任务执行，同 runtime/同场景多任务重复查（S24-3 只做了同任务内复用，跨任务未去重；S31 known 短路只护已落库任务，首扫/档位翻转全量暴露）；②issue_terminal 排在未超窗之前——confirm 窗内近期任务（候选大头）先做 issue 表查询再被超窗丢弃，查询白做。
触发：每轮 detect_zombies / detect_broken_chains。
预期：①轮内字典缓存：runtime_id → 不可达、(repo_key, issue_id) → 场景缺失，同键多任务共享一次探活/解析；缓存生命周期=单轮（每周期重建，心跳/场景变更下轮自然重判，无陈旧判定）；②阶段 1 顺序改为：已知→PR 订阅→缺键→未超窗→issue 终态（零成本纯谓词前置）；③两者均纯谓词重排/memoization——结果集与产出顺序零变化（回归全绿）。
用例：UC47-1 探活去重+单轮生命周期（test_s47_1）；UC47-2 窗内候选零 issue 查询+超窗照常+结果集不变（test_s47_2）。

### S48【性能·分析人清单 A】批量落库：攒批 executemany + 单块单 commit（R20）
环境：run_patrol_once 逐条 insert_finding（每条 INSERT + db.commit()）——SQLite 下 commit≈fsync、MySQL 下每条一个事务网络往返，100 条 finding=100 次事务提交全程串行持锁；与检测端"重扫收敛到 O(任务数)+1 条查询"的设计不对称。
触发：每轮 findings 落库（含 audit 存量核对首轮的大批量）。
预期：①≤500 条一批 executemany、每块恰好 1 次 commit；②owner 归属按 repo_key 去重解析且在锁外（_resolve_owner 内部持锁，锁内调用会死锁）；③单块失败吞掉、其余块照常（S5）；④R6/R8 契约在批量路径成立（主键全 UUID、owner 随行、默认 open）；⑤executemany rowcount 只计真实插入（OR IGNORE 幂等语义保持）。
用例：UC48-1 单 commit+一批写入+契约（test_s48_1）；UC48-2 owner 去重（test_s48_2）；UC48-3 单块失败隔离（test_s48_3）。

### S49【性能·分析人清单 C】keyset 分页流式拉取（R20）
环境：detect 主扫描一次 fetchall 全量行入内存——runtime 窗行数可控，audit 全历史一次载入 completed 任务全集（万级任务 Python 元组 ~500B/行），叠加逐条写=audit 一轮内存与事务数双峰值 O(全历史)；而 audit 存量核对正是上线后第一个动作。
触发：每轮候选拉取（runtime/audit 两分支同构）。
预期：①id > 游标 ORDER BY id LIMIT 500 逐页：每页独立完整查询，锁内取页锁外处理（cursor 不跨锁存活——规避共享连接上并发执行语句的迭代语义风险，不用跨锁长事务）；②游标逐页推进至取尽（含终止探针页）；③分页期间并发插入由 INSERT OR IGNORE 幂等兜底；④P1/S45 轻量列语义与 S47 谓词顺序不变，幸存判定不受页边界影响；⑤id 为 TEXT 主键（自动索引），id > '' 匹配全部非空 id。
用例：UC49-1 游标推进+取尽终止+行序一致（test_s49_1）；UC49-2 跨页已知分布判定不变（test_s49_2）。

### S50【覆盖对账·第22轮】seed 契约 + 批量落库接线（R21）
环境：第22轮函数级对账发现两处零覆盖——①_seed_meta_from_file（S13 声称"文件仅首次 seed、meta 已有键不覆盖"从未被断言）；②run_patrol_once→insert_findings_batch 接线（S48 测了批量函数本身，但检测产物进哪个落库路径没有钉死——退回逐条 insert_finding 的旧形态所有测试照样绿）。
触发：进程启动 seed / 每轮扫描落库。
预期：①seed 只写 meta 空键（已有键不覆盖）、bool 序列化为小写字符串、异常吞掉返回 0（继续以代码默认+meta 为准，不炸启动）；②run_patrol_once 检测产物（broken+ombies+stalled）整批进 insert_findings_batch，回执 inserted=批量返回值。
用例：UC50-1 批量接线钉死（test_s50_1）；UC50-2 seed 空键才写+bool 小写（test_s50_2）；UC50-3 seed 异常吞掉（test_s50_3）。

### S51【覆盖对账·第23轮】已知集轮内共享（R22）
环境：外部深度复查发现 _known_task_ids 每轮被独立调用两次（detect_broken/detect_zombies 各一次全表拉同一集合）——轮内双拉的小浪费；同时确认 zombie 检测仍是 fetchall 全量（自指放大风险：僵尸堆积→行数增长，数据触发级）。
触发：每轮 run_patrol_once。
预期：①轮内 _known_task_ids 恰好一次、双检测共享同一集合对象（引用传递）；②独立调用（不传 known）自行拉取，旧签名兼容（路由/直调路径不变）；③语义等价：落库发生在两检测之后（S48 批量落库），轮内两检测看到同一快照恒成立——共享不改判定。
用例：UC51-1 轮内一次拉取+共享引用（test_s51_1）；UC51-2 独立调用自行拉取（test_s51_2）。

### S52【覆盖对账·第24轮】known 共享参数生效语义 + meta 坏值回落（R23）
环境：行为层对账两处缺口——①R22 落地的 known 共享参数只在 mock 接收者层验证（run_patrol_once 传了、mock 收到），真 detect 函数消费该参数的语义（用传入集合短路、不再自查 findings 表）从未被断言，共享优化若退化为"接收但忽略"不爆红；②lookback_mode 坏值回落只在配置文件层测过（t9），meta 层（运行期被写成 garbage 的形态）无覆盖。
触发：run_patrol_once 共享调用 / meta 被运行期写坏。
预期：①detect_broken_chains/detect_zombies 传入 known 时：候选按传入集合短路、不再发 findings 表查询（共享语义生效而非仅签名兼容）；②meta 层 lookback_mode 坏值回落 _cfg 默认、合法值（audit）照常透传。
用例：UC52-1/2 双检测 known 生效+零自查（test_s52_1/2）；UC52-3 meta 坏值回落+合法透传（test_s52_3）。

### S53【外部复审·收口】shutdown cancel 补齐 + 补偿链卸载（R24）
环境：外部合并前复审发现两项——①main.py 创建的 patrol 后台任务 shutdown 不 cancel（main.py 742 行注释自己记录过"漏 cancel→循环残留→用例成片假失败"的同类真实事故，patrol_loop 无限写库循环复发了这个模式）；②补偿链五个同步 handler 的持锁 DB 查询在事件循环上裸跑（D23 只卸载了扫描/回收，补偿链是漏网段）。
触发：进程关闭 / 每轮补偿批处理。
预期：①shutdown 清理段 cancel _patrol_task 并 wait_for 5s 收口；②同步链（载入+五 handler）在工作线程执行，派发保持 async；③编排语义不因卸载改变：链短路→零派发返 verdict、链跑完→派发一次、finding 缺失→"missing" 早退。
用例：UC53-1 cancel 收口结构断言（test_s53_1）；UC53-2 同步链工作线程+派发（test_s53_2）；UC53-3 短路零派发（test_s53_3）；UC53-4 missing 早退（test_s53_4）。

## 四、验收清单（映射 Issue 验收指标）

- [x] 存量 60 条断链发现率 = 双源可判定子集的全量（部署实测：109=109 独立对账精确一致，≥60）
- [ ] Phase 1 跑 2 天，人工核对判定正确率
- [ ] **误报核对（决策变更 2026-11-02：从事前硬线改为事后观测——补偿默认开即生效，误报靠责任链终检拦存量慢到 + dismiss 收敛 + false_positive 回挂三通道事后对账；出现系统性误报时紧急关走 meta patrol:compensate_enabled=false，回头改判定逻辑）**：核对对象为存量 60 条全量（非抽样），人工判定逐条登记
- [ ] 补发后链恢复率（Phase 2 灰度后统计）
- [x] 风暴检查：巡检器动作不进自己扫描窗口（R7 测试绿；部署实测补偿事件被终检识别为 L1 证据即风暴闭环佐证）
- [x] 幂等：同任务重复扫描周期不二次补发（R4 测试绿；部署实测补偿后重开→false_positive、下游任务恒 1）
- [x] 巡检扫描耗时 < 阈值且记录在案（实测 7-87ms/轮，duration 入日志）
- [ ] **上线首日切 audit 跑一轮存量核对（7 天视界时效约束——#463 存储清理每 6h 将 7 天以上 completed 任务的 result_text null 化，巡检判据随之失效；切晚一天存量即少一批不可判定，实现后审计登记）**
- [ ] S4 回归套件全绿

## 五、不在范围

- 不做 complete 接口内同步兜底（阻塞收尾，已否——issue §五）
- 不做 self_loop 白名单放行 bot 评论（推模型反馈回路风暴，已否——issue §五）
- 不根治 LLM 漏发源头（skill 层 skill-batch-generator v1.0.3 守门已另行上线，本单只做平台兜底）
- 不自动修复 D 类引擎错绑（只出清单，修复人工另开任务）
- 不自动置僵尸任务终态（Phase 3 待真实时长分布数据后再定，本分支只做告警+登记）
- 不补 B 类历史空 result_text 的失败原因（数据已丢不可补；只提规范：任务失败必须写入失败摘要，已另立 #637）
- 不做 skill/worker 侧幂等键同式派生（另行立项：skill 守门件按 handoff:{task_id}:{target_agent} 同式落地，落地前补偿与迟到原生交棒为两键、残余竞态=双发窗口，见 S21/设计 D5——实现后审计登记为跨仓悬空头寸）
- 不改 a2a-redo / redispatch 手工端点自身的 30s get-then-set 幂等窗（相邻发现：与已修选主同病的 check-then-act，双击/双主可双发；受端点 #329+幂等键兜底伤害半径小，记 backlog 待巡检落地后收敛为谓词 UPDATE）
- 强实时场景不适用（分钟级巡检延迟，对断链场景无影响——本来要小时级人肉发现）

## 六、实测核验

本文档所有 file:line 断言均在 `feat/issue-635-a2a-chain-patrol`（= origin/weekly-09 @ caded1c4c）上 grep 复核：tasks.py:362-374 / event_pipeline.py:1577-1648、4288 / events.py:478-492、1386-1498 / event_store.py:244 / db_setup.py:42、323。agent_tasks 现有列（status/result_text/completed_at/event_seq/self_reported 等）经 PRAGMA 实测，无 next_step 列（在 result_text JSON 内）——巡检需解析 JSON，此为性能设计输入。全仓"巡检"命中均为 autopilot 业务域（src/runtime/scheduler.py:60s asyncio 循环，即本巡检器的实现范式参照），平台层巡检为零，需求不与现有功能重叠。a2a 端点防重实证（D4/D11 依据，三重）：webhook.py:617-621（#329 场景级去重）、:1287-1298（同 issue+同 agent_type 活跃去重，两处均判定键=running/dispatched，已终态不阻断——Issue #587）、:1031-1045（幂等键前置命中返已有事件）、:1344-1353（并发撞唯一索引 race-hit 兜底）。僵尸证据化依据（D12）：agent_tasks 信号列（service.py:2472-2486 set_task_signal 条件置位 / event_pipeline.py:4225-4231 stream_ended 置位点）、引擎健康**分形态**判定（engine_select.py:103-118：远端 credential_id>0 心跳 / 本机 cid=0 installed+healthy）、场景存在性（scenes 路由 404 语义）。补偿模式恢复依据（D15）：carried_context 在 issue 表（session_store.py:261 get_carried_context）、is_subtask_mode+parent_issue_id 在 issue_state（session_store.py:78/:321）；a2a 事件模式字段（webhook.py:1340）。判定范围与终态化通道依据（D17/D18）：PR 订阅链设计性拦截（events.py:1234-1240，issue_id `#pr_` 前缀+scene_id 不走 A2A）；scheduler 既有时间窗终态化（scheduler.py:30-32 PENDING_SELF_REPORT_TIMEOUT=1800s）。统计数据（1264/142/60、762/592、queued 20/17）引自 issue #635 正文当次查库结果，非本文档复算。

## 七、UC↔测试映射（交付形态：mock 数据访问的单元测试）

测试分层纪律（交付约定）：随 CI pytest 运行的测试一律为**单元测试**——mock
数据访问接口（Repository seam：l1_event_exists / l2_downstream_exists /
issue_terminal / _subtask_parent / _meta_value / _engine_unreachable /
_scene_missing / get_issue_stage / 脚本化 FakeDb cursor），不连真实数据库；
判定/编排/配置/策略/结构断言全部在该层覆盖（`tests/test_patrol_unit.py`，
44 用例，跑时 ~1.5s；评审十三轮修正后 47 用例；PR #802 缺口闭环后 50 用例；评审十四轮修正后 53 用例；评审十五轮修正后 56 用例；评审十六轮修正后 61 用例；评审十七轮修正后 65 用例；外部复审收口后 114 用例）。需要真实数据库才能验证的行为（唯一约束、迁移、
谓词 UPDATE 原子性、全链路补偿、HTTP API）**不进交付分支**，收敛为
§七.3 的目标环境验收清单，验收时在目标环境执行——真库脚本入库即违反
交付分支内容单一纪律。

### 七.1 单元测试文件与 mock 边界

- 文件：`tests/test_patrol_unit.py`
- mock 策略：patch patrol 模块事实 Repository 函数与 `db`（FakeDb 按 SQL
  片段返回脚本行）；`a2a_endpoint` 打桩在源模块（patch
  `src.api.routes.webhook.a2a_endpoint`）；配置面 patch `_meta_value`。
  **不 mock SQL 细节之外的东西**：SQL 正确性、约束、事务归验收层。

### 七.2 UC↔单元用例映射

| UC | 单元测试函数（tests/test_patrol_unit.py） |
|----|------------------------------------------|
| UC1-1 断链命中 | test_t1_broken_chain_detected |
| UC1-2 未超窗静默 | test_t2_within_window_silent |
| UC1-3 非 a2a 前缀不报 | test_t3_non_a2a_prefix_not_reported |
| UC1-5 L1 全状态证据 | test_t11_normal_chain_no_finding |
| UC1-8 L2 全状态证据 | test_t1f_l2_completed_downstream_counts |
| UC1-8b L2 无关联非证据 | test_t1b_l2_stale_correlation_not_evidence |
| UC1-10 target 不可解析→人工 | test_t1_unparseable_target_suggestion_manual |
| UC1-11 PR 订阅链排除 | test_t7_pr_subscription_chain_excluded / test_t14_pr_subscription_excluded_from_candidates |
| T7 解析纯函数 | test_t7_extract_next_step_and_target |
| UC3-1 停摆=断链 issue 视图 | test_t6_stalled_issue_is_aggregate_view |
| UC2-1 僵尸确定性档 | test_t5_zombie_running_signal_tier |
| UC2-2 僵尸低置信档 | test_t5_zombie_running_window_tier |
| UC2-5 阈值内不报 | test_t5_zombie_running_fresh_not_reported |
| UC2-3 queued 确定性归类 | test_t5_zombie_queued_dead_engine / test_t5_zombie_queued_scene_missing |
| UC2-6 本机引擎防误判 | test_t5_zombie_queued_local_healthy_not_dead + test_t5_engine_unreachable_predicate_forms（分形态谓词六判例） |
| UC2-4 引擎在线观察档 | test_t5_zombie_queued_window_fallback |
| 场景存在性谓词 | test_t5_scene_missing_predicate |
| UC13 文件配置坏值回落 | test_t9_config_file_bad_values_fall_back |
| UC13-2 零 env 结构断言 | test_t9b_no_env_reads_config_file_only |
| UC13-3 meta 真源 | test_t10_runtime_config_meta_is_source_of_truth |
| UC10 开关/黑名单 | test_t19_dynamic_switch_and_blocklist |
| UC14-3 窗口运行期收紧 | test_s14_3_confirm_window_runtime_change |
| S15① issue 终态排除 | test_s15_1_issue_terminal_excluded_from_detection |
| S16② 子任务 L2 接线 | test_s16_1_subtask_l2_key_wired（透传断言） |
| S17③ 子任务键双源 | test_s17_1_subtask_parent_reads_stage |
| T6/D18 终态写零路径 | test_t15_patrol_module_has_no_terminal_write（patrol+compensate 双模块） |
| 门卫处理器 | test_gate_handler_switch_and_blocklist |
| 终检处理器 | test_recheck_handler_evidence_and_guards（终态/L1/L2/task_missing/target_unresolvable 五分支） |
| UC1-7 身份策略族 | test_identity_handler_strategy_chain + test_identity_strategy_sql_seam |
| UC1-9/10 模式恢复 | test_mode_handler_subtask_recovery |
| UC1-6 无主体降级 | （并入身份策略族用例：auth_unavailable 分支） |
| 派发适配器（键/标记/结论/前缀） | test_dispatch_conclusion_and_key_and_marker |
| S16① 永久/瞬态分桶 | test_dispatch_verdict_buckets（403/500/duplicate/异常四桶） |
| S19② duplicate→false_positive | test_dispatch_verdict_buckets |
| S19③/④ 响应契约 | test_s19_3_summarize_jsonresponse_contract |
| S21① 幂等键单源+非空护栏 | test_s21_1_handoff_key_single_source_and_nonempty_guard |
| S22 方言纪律结构断言 | test_s22_1_mysql_dialect_compliance |
| S23① A6 表名+无条件回收 | test_s23_1_a6_table_name_and_unconditional_reclaim |
| S20 github 键形态 | test_s20_1_github_key_forms |
| S24① 首轮即时巡检 | test_s24_1_first_cycle_immediate |
| S24② 清单排序稳定 | test_s24_2_findings_list_stable_order |
| S24③ 队列僵尸单次探活 | test_s24_3_queued_zombie_single_engine_probe |
| S25① 路由成功面契约+置顶 | test_s25_1_list_findings_contract_and_unowned_pin |
| S25② 过滤面 | test_s25_2_list_findings_filters |
| S25③ dismiss 200/404 | test_s25_3_dismiss_ok_then_404 |
| S26① 归属路径 A 回落 | test_s26_1_resolve_owner_path_a_fallback |
| S26② 路径 B 优先+unowned | test_s26_2_resolve_owner_host_user_wins_and_unowned |
| S27 选主总闸门控 | test_s27_1_leader_gate_before_reclaim_and_compensate |
| S28① 持有者标识形态 | test_s28_1_holder_identity_includes_pid |
| S28② 同机不同 pid 互斥 | test_s28_2_same_host_diff_pid_cannot_renew |
| S28③ 本持有者自续约 | test_s28_3_same_holder_self_renews |
| S29① 不自愈三类终态+捞单钉死 | test_s29_1_not_selfhealing_verdicts_are_terminal |
| S29② 冷却谓词+行为面 | test_s29_2_batch_cooldown_excludes_recent_failures |
| S30 admin 放行 | test_s30_1_dismiss_owner_or_admin_only（403/404 面并入 test_s25_3） |
| S31 已知名短路 | test_s31_1_known_findings_short_circuit_rescan |
| S32 L1 棒次过滤 | test_s32_1_l1_agent_filter |
| S33① 回查命中→对齐+回执+排程 | test_s33_1_race_detected_aligns_and_records |
| S33② 非终态零动作 | test_s33_2_non_terminal_zero_action |
| S33③ 委托 _sync+D18 红线 | test_s33_3_align_delegates_to_sync_and_guards |
| S33④ 延迟二次扫幂等 | test_s33_4_delayed_resweep_runs_and_idempotent |
| S34① 终态集合钉死+90 天 cutoff | test_s34_1_retention_deletes_only_terminal_old |
| S34② retention=0 零 DELETE | test_s34_2_retention_zero_disables |
| S34③ 坏值回落默认 | test_s34_3_retention_bad_value_falls_back |
| S34④ purged 入回执契约 | test_s34_4_run_once_reports_purged |
| S35① scan 端点契约+卸载 | test_s35_1_scan_endpoint_contract_and_offload |
| S35② 异常 500 收口 | test_s35_2_scan_endpoint_failure_isolated |
| S36① to_thread 卸载+防回退 | test_s36_1_loop_offloads_scan_to_thread |
| S37① 模板冻结+同构+确定性 | test_s37_1_handoff_key_template_frozen |
| S37② 空白规整同键 | test_s37_2_handoff_key_whitespace_normalized |
| S37③ 空参拒绝 | test_s37_3_handoff_key_none_rejected |
| S38① 非 admin owner 过滤 | test_s38_1_non_admin_sees_own_rows_only |
| S38② 台账异常降级 | test_s38_2_admin_gate_query_failure_degrades_to_owner_filter |
| S39① 并发 409+成功释放 | test_s39_1_concurrent_scan_second_gets_409 |
| S39② 异常释放+可再触发 | test_s39_2_gate_released_on_error |
| S40① 超阈值告警+不熔断 | test_s40_1_slow_scan_warns_and_does_not_block |
| S40② 边界严格比较 | test_s40_2_threshold_boundary_strict |
| S41① 函数级闸并发+释放 | test_s41_1_function_level_gate_blocks_second_entry |
| S41② 异常释放+可再进入 | test_s41_2_gate_released_on_inner_error |
| S41③ 自动轮 skip 良性语义 | test_s41_3_loop_skip_is_benign_not_backoff |
| S42① reclaim 卸载+顺序保持 | test_s42_1_reclaim_offloaded_to_thread |
| S43① UUID/归属/rowcount=1 | test_s43_1_insert_finding_uuid_owner_and_defaults |
| S43② rowcount=0 幂等汇合 | test_s43_2_insert_duplicate_rowcount_zero |
| S43③ 异常吞掉（缺陷修复回归） | test_s43_3_insert_error_swallowed_not_raising |
| S44① 责任链顺序冻结 | test_s44_1_chain_order_frozen |
| S44② 派发隔离+六段编排 | test_s44_2_dispatch_handler_not_in_sync_chain |
| S45① 幸存者独享回捞 | test_s45_1_result_text_fetched_only_for_survivors |
| S45② 500 分块 | test_s45_2_batch_chunking_500 |
| S45③ 回捞异常吞掉 | test_s45_3_fetch_error_swallowed |
| S46① runtime 窗口下推+参数承载 | test_s46_1_runtime_window_pushed_into_phase1_sql |
| S46② audit 无谓词无参数（互斥钉死） | test_s46_2_audit_branch_has_no_window_filter |
| S47① 探活去重+单轮生命周期 | test_s47_1_queued_probe_dedup_by_runtime_and_issue |
| S47② 窗内零 issue 查询+结果集不变 | test_s47_2_terminal_query_skipped_for_within_window |
| S48① 单 commit+一批写入+契约 | test_s48_1_batch_insert_single_commit_per_chunk |
| S48② owner 去重解析 | test_s48_2_owner_resolved_once_per_distinct_repo |
| S48③ 单块失败隔离 | test_s48_3_chunk_failure_isolated |
| S49① 游标推进+取尽终止 | test_s49_1_keyset_pagination_terminates_and_orders |
| S49② 跨页判定不变 | test_s49_2_detect_streaming_semantics_unchanged |
| S50① 批量接线钉死 | test_s50_1_run_once_wires_detect_to_batch_insert |
| S50② seed 空键才写+bool 小写 | test_s50_2_seed_skips_existing_meta_keys |
| S50③ seed 异常吞掉 | test_s50_3_seed_failure_swallowed |
| S51① 轮内一次拉取+共享引用 | test_s51_1_known_set_shared_within_round |
| S51② 独立调用自行拉取兼容 | test_s51_2_standalone_calls_still_compute_known |
| S52① broken detect known 生效+零自查 | test_s52_1_broken_detect_honors_shared_known |
| S52② zombie detect known 生效+零自查 | test_s52_2_zombie_detect_honors_shared_known |
| S52③ meta 坏值回落+合法透传 | test_s52_3_meta_mode_bad_value_falls_back |
| S53① shutdown cancel 收口 | test_s53_1_main_shutdown_cancels_patrol_task |
| S53② 同步链工作线程+派发 | test_s53_2_compensate_sync_chain_offloaded |
| S53③ 短路零派发 | test_s53_3_short_circuit_skips_dispatch |
| S53④ missing 早退 | test_s53_4_missing_finding_returns_early |
| 批处理编排 | test_compensate_open_findings_order_and_batch + test_compensate_batch_order_sql_pinned |
| S19① 租约回收编排 | test_reclaim_stale_claimings_lease_logic |

### 七.3 目标环境验收清单（真库验证，验收时执行，不进分支）

以下项依赖真实 SQLite/MySQL 行为（约束、迁移台账、谓词 UPDATE rowcount、
全链路、HTTP），按交付纪律不在仓库内挂 pytest，验收时在目标环境按清单执行：

1. **UC-T8b 唯一约束去重**：同 (kind, task_id, issue_id, event_seq) 二次
   INSERT OR IGNORE，rowcount=0 静默忽略；`SELECT COUNT(*)` 恒 1。
2. **S18_1 存量库建表迁移**：对已盖章（grape_t_schema_migrations 有
   202611020001/2）的库副本启动，确认 grape_t_patrol_findings 自动补建；
   幂等重跑无错。
3. **S18_2 issue 级唯一键**：三个不同 issue 的 stalled_issue 各落一条
   （四列键），同 issue 重复插入被吞。
4. **S18_3 旧三列键重建**：构造旧形态表（UNIQUE 三列）后跑迁移 2，保数据
   重建为四列键；新形态重跑直接返回。
5. **T8/T8c 选主谓词 UPDATE**：引导期 INSERT、自续约、他主新鲜拒、
   过期 CAS 接管、历史纯 ISO 兼容、host_a 与 host-a 不互开锁
   （substr 精确前缀）。
6. **claim 抢占原子性**：open→claiming rowcount=1；二次抢占 rowcount=0
   判 not_open（防双发）。
7. **claiming 租约回收**：updated_at 超 10 分钟的 claiming 回收为 open；
   新鲜 claiming 不动；补偿开关关闭时回收仍执行（卫生操作）。
8. **全链路补偿**（a2a_endpoint 打桩亦可）：open finding → 门卫放行 →
   抢占 → 终检空 → 身份（host_user_id=999 断言）→ 模式 → 派发，
   compensated 落 action_meta；二次补发 not_open；无主体仓 auth_unavailable
   零派发回挂。
9. **批处理 FIFO**：三条 created_at 错开的 open finding，limit=1 先消费最老。
10. **HTTP API**：GET /api/patrol/findings 过滤/unowned 置顶；POST
    dismiss 200→再 dismiss 404；未认证 401/403。
11. **run_patrol_once audit 档**：全历史窗口 stats.broken/inserted ≥ 1，
    duration 记录。
12. **S21 幂等索引存在**：events 表 idempotency_key 唯一索引
    （idx_events_idem）实际存在（决策 A 实证基础）。
13. **零残留**：哨兵前缀（patrol-test/%）四表清零 + patrol:leader meta 清除。
14. **MySQL 全新库首启**（R24 外部复审低项）：全新 MySQL 库首次启动，确认迁移 202611020002 形态检查命中新四列键早退、不触发表重建（日志无 rebuild 记录）；若触发重建（数据无损、仅一次）登记形态判断的方言差异。

### 七.4 交付物构成

- 产品代码：src/api/patrol.py、src/api/patrol_compensate.py、
  src/api/routes/patrol.py、src/api/db_setup.py（DDL+迁移）、
  src/api/main.py（挂载）、src/api/routes/events.py（manual 标记）
- 单元测试（随 CI）：tests/test_patrol_unit.py
- 本需求文档 + 设计文档 + help/api/18-patrol.md + example 配置
- **不含**：真库验收脚本（本节七.3 清单即其全部内容）

## 八、外部评审收口记录（两轮，全部处置完毕）

评审材料为外部 AI 对设计文档的独立审查；两轮均按"逐条对当前 HEAD 核验 →
已修/真新/误报三分类"处置，无未收口残留。

### 第一轮（实现前，3 缺陷 → 全部采纳修复）

| 评审发现 | 裁决 | 处置（含落点） |
|---|---|---|
| UNIQUE(kind,task_id,event_seq) NULL 语义使去重失效 | 属实 | DDL 改 `NOT NULL DEFAULT ''/0`（空串/0 参与唯一约束），并加码 id 改 UUID 主键；T8b 断言同键二插静默忽略 |
| D3 选主谓词引导期未闭合（行不存在 rowcount=0） | 属实 | claim_patrol_leader 引导期 SELECT 检查+INSERT（良性竞态，db_setup.py:4158 同款先例）；补统一 aware ISO 格式要求 |
| 手工端点 compensated_by 未规定 → 默认值稀释审计底线 | 属实 | events.py:1484 手工路径必传 `compensated_by=manual:{username}` 进 context 落事件 |

另采纳两条 backlog：result_text 解析失败自监控、webhook 去重处回链注释（设计风险表，见下）。

### 第二轮（实现后，3 缺陷 → 全部"已修复"裁决，基线过期）

评审审查基线为 7 个 commit 前的纯文档态（称"分支尚无任何代码"），三缺陷与第一轮
同族。对当前 HEAD 逐条核验：

| 评审发现 | 裁决 | 证据（当前 HEAD 实查） |
|---|---|---|
| 缺陷 1 UNIQUE NULL | **已修复**（第一轮同项） | db_setup.py:146-147 NOT NULL DEFAULT ''/0 + UUID 主键 |
| 缺陷 2 选主引导期 | **已修复且超出**：实现期另发现评审未见的真 bug——cutoff 原为 epoch 字符串而 value 为 ISO，跨格式字符串比较致过期抢占永不生效；统一 aware ISO 后修复（patrol.py:148） | patrol.py:168 引导 INSERT + 统一格式注释 |
| 缺陷 3 手工标记规约 | **已修复**（第一轮同项） | events.py:1484 manual 标记进 context |

评审确认的正向判断（四条方法论主线、防自激结构性论证、全增量兼容、db_setup:4158
先例印证）与实现互证，作为独立佐证收档。评审描述"补偿直调 a2a-redo 抽函数"已被
D20 演进超越（直调原生 a2a_endpoint，D5 落地注记）。

### 第三轮（实现后审计，2 CONFIRMED → 已修复，1 措辞残留 → 已同步）

| 评审发现 | 裁决 | 处置 |
|---|---|---|
| claim_patrol_leader 无自续约（docstring 声称续约、实测 False，扫描周期实际 ≈2×interval、主节点乒乓） | CONFIRMED，已修复 | 重写为 holder 感知 value（`holder\|iso`）+ CAS：①自续约 `value LIKE 'holder\|%'` 原子续；②读现值判过期（_leader_expired 兼容历史纯 ISO 格式）；③CAS 接管（UPDATE WHERE value=<旧值>，双主并发只有一方成功）；④引导期 INSERT OR IGNORE。T8 补自续约断言（首次 True→同主机再调 True）+ 历史格式兼容断言 |
| R2 明文的 queued"场景已不存在"分支未实现（只查引擎） | CONFIRMED，已修复 | _scene_missing：issue_state 取 scene_id，get_pipeline_config 判定——**修正判定语义：fallback=true 即"未解析到真实场景"（get_pipeline_config 文档约定调用方按 fallback 拒绝静默回退；is None 判定不成立，不存在场景会回退 default）**；queued 判死条件 = 引擎不可达 OR 场景缺失，detail 带 cause 字段。新测试 T5b（场景缺失→即时永不调度 + 无绑定不判死） |
| S1:54 旧措"a2a-redo 原生链路/双源一致"与实现（直调 a2a_endpoint、仅自报 next_step，节点合法性由端点 _scene_node_check 兜底）不同步 | 措辞残留，已同步 | 本轮 §S1 预期行改为直调口径 |
| R6"补偿标记不传不通"仅在两个调用方约定、端点层未强制 | 采纳为 backlog | 端点层强制需动 a2a_endpoint 契约（影响所有既有调用方），风险大于收益；当前调用点仅两处（手工 redo + 巡检派发），约定+测试双断言已覆盖；记 backlog 待 a2a 契约演进时一并处理 |

另：评审确认基线核对（merge-base=weekly-09 head、纯 fast-forward）、基线失败集一致（test_a2a_redo 4 + api_docs_sync 2 在基线逐条相同）、MySQL 方言约定合规——均与本地核验一致，收档。

### 第四轮（squash 后复审，1 低危 → 已修复）

| 评审发现 | 裁决 | 处置 |
|---|---|---|
| 自续约 `value LIKE 'holder|%'` 未转义通配符：`host_a` 的模式可续掉 `host-a` 的锁（`_` 为 SQL 单字通配符，SQLite/MySQL 同），触发条件=两主机名仅差一个字符，后果=双主同时扫描（零数据伤害） | 属实（低危），已修复 | 自续约改 `substr(value,1,len(holder)+1)=holder|` 精确前缀比较（substr 语义 SQLite/MySQL 一致，patrol.py 自续约分支）；新增 UC12-1 / test_t8c_leader_renewal_wildcard_guard（host_a 续 host-a 锁→False + leader 值无越权改写 + 本主续约 True）。同族登记：同主机多 uvicorn worker 共享 hostname 全部通过自续约——单 worker 部署无影响，扩 worker 前收紧 holder 到 hostname+pid |
| 配置面过宽：8 项 env 中 4 项无调参决策（LOOKBACK_DAYS/RUNNING/QUEUED/STALE），且 int() 裸解析——坏值（如 `banana`）在 import 期抛 ValueError，经 main.py 启动 try 兜成一条 warning 后**巡检循环静默停摆**（兜底能力无声消失）；meta 动态通道反而有 UC10-6 fail-fast，设计不自洽 | 属实，已修复 | 配置面收窄：env 只留四项决策项（INTERVAL=D9/CONFIRM=D4/LOOKBACK_MODE=D17/COMPENSATE_ENABLED=R4 兜底），其余阈值转模块常量；引擎心跳新鲜度删 patrol 副本，单源引用 engine_select._RUNTIME_STALE_SECONDS；env 整数改 `_env_int` 安全解析（坏值回落默认+告警，对齐 GateHandler fail-fast 口径）。新增 S13 / UC13-1 / UC13-2（test_t9 守卫判例 + test_t9b 源码结构断言防配置面回涨） |

### 第五轮（合并前复核，2 缺陷 → 已修复，1 噪音级 → 登记 backlog）

| 评审发现 | 裁决 | 处置 |
|---|---|---|
| issue 终态盲区（行为伤害·中）：链断后用户直接关 issue（confirm_issue_done → completed / pending_review），L1/L2/终检全空（只找交棒证据不找人已处置证据）→ 判定断链成立补发；a2a 端点只拦 cancelled（webhook.py:1010）→ 补偿复活已收敛 issue | 属实，已修复 | 双守卫：①检测层 issue_terminal 谓词排除终态 issue（不出 finding 防噪音）；②RecheckHandler 终检前置同谓词（竞态窗内收敛 → false_positive 零派发）。新增 UC15-1/15-2 + test_s15_1/s15_2 |
| 补偿 context 不携带原任务结论（行为降级·低）：手工 a2a-redo 带"基于原事件结论继续"指针，补偿只有 task_id——下游缺上游 result_text 结论，等价性不成立 | 属实，已修复 | RecheckHandler 保存 result_text 入 ctx，DispatchHandler 结论摘要（600 字符截断）拼进 context；result_text 空时明示"仅按自报 next_step 补发"。新增 UC15-3 + test_s15_3 |
| dispatch_failed 的 finding 回挂 open 后每 300s 无限重试（无退避/上限）：日志噪音+空转，无数据伤害 | 噪音级，登记 backlog | 加重试上限/指数退避随下次触碰补偿链时一并处理（Backlog #4） |

### 第六轮（合并前复核·派发链路全走查，2 缺口 → 已修复，1 设计粒度 + 3 小项 → 登记）

| 评审发现 | 裁决 | 处置 |
|---|---|---|
| 永久失败无限重试（liveness 缺陷·最实质）：403（agent disabled）/409（not_in_scene）/skipped 类语义拒绝不会自愈，一律回挂 open → 每周期重试到永远，action_meta 反复截断、日志刷屏 | 属实，已修复 | DispatchHandler._is_permanent_failure 分类：dict 语义拒绝（skipped/非 ok 状态）或 HTTP 4xx → finding 终态 permanent_failure（批处理只捞 open，自然收敛）；5xx/异常仍回 open 重试。API status 枚举补 permanent_failure。新增 UC16-1 / test_s16_2 |
| D17 子任务 L2 关联键没接线：l2_downstream_exists 有 parent_issue 形参但检测/终检都不传，is_subtask_mode 从未被读——UC1-12 承诺未兑现（后果偏漏报侧，方向无害） | 属实，已修复 | _subtask_parent 谓词（issue_state 取 is_subtask_mode+parent_issue_id），检测层与 RecheckHandler 双接线；新增 UC16-2 / test_s16_1（UC1-12 首个判例：主链同 issue 任务不构成子任务链证据） |
| L1 证据粒度只到 issue 级，不区分目标 agent（同 issue 向别的 agent 交过棒会掩盖真正断掉的交棒）→ 漏报 | 设计口径本身如此（非实现走样），登记 | Backlog #5：L1 细化到 agent 级（a2a_event payload 含 agent 字段，判定层过滤），随判定精度演进一并处理 |
| 小项三项：批处理 ORDER BY id（UUID 随机挑 20 条，FIFO 未实现）；D15 engine_config 未恢复（走引擎默认，偏差方向安全）；补偿循环不受选主门控（正确性靠 ClaimHandler DB CAS，选主只门控扫描——设计"单写者"表述需按现实理解） | 已处置/登记 | FIFO 已修（ORDER BY created_at，UC16-3）；engine_config 与"单写者"表述修正登记设计文档风险表（Backlog #5 附注） |

### 第七轮（合并前复核，1 新发现 + 1 沿袭项 → 已修复，2 沿袭措辞级 → 登记）

| 评审发现 | 裁决 | 处置 |
|---|---|---|
| _subtask_parent 只查 issue_state，漏 C3 权威的 issue 单表形态——实测子任务标记在 issue 表、无 issue_state 行时返回 ''（L2 退化主链键），Phase C3 已停写 issue_state，新建子任务链全走 issue 表，此修对它们不生效；与同文件 ModeHandler（get_issue_stage 双源）口径不一致 | 属实，已修复 | _subtask_parent 改用 get_issue_stage 同款双源（issue 单表权威 + issue_state 过渡兜底）。新增 UC17-1 / test_s17_1（issue 单表形态 + 防误配双向判例） |
| 补偿 context 带整段 result_text JSON（下游要自己再解析一遍，结论等价性打折） | 属实，已修复 | _conclusion_snippet：优先取结构化 conclusion 字段，解析失败/无字段回落原文，截断不变。新增 UC17-2 / test_s17_2 |
| "无可读结论"回退分支在当前解析链下不可达（next_step 可解析 ⇒ result_text 非空）| 措辞/死代码级，保留 | 防御性保留（result_text 语义若演进为非 JSON 前置字段，回退即生效），零行为影响 |
| 响应丢失（事件已建但异常返回）下轮 L1 命中标 false_positive——账目标签措辞与"误报"语义有出入 | 措辞级，登记 | 行为安全（幂等键保证不双发），标签语义随 verdict 枚举演进时一并梳理（Backlog #5 附注） |

### 第八轮（推演级复审，2 缺陷 → 已修复，1 契约决策 → 待拍板，1 通用性 → 登记）

| 评审发现 | 裁决 | 处置 |
|---|---|---|
| claiming 孤儿（活性缺陷）：进程崩溃后 finding 永久停在 claiming，批处理只扫 open 无回收 | 属实，已修复 | compensate_open_findings 前置租约回收（10 分钟，复用 updated_at，幂等键保证回收重试不双发）。UC19-1 / test_s19_1 |
| duplicate_handoff 落 permanent_failure：该拦截是"交棒已发生"正向证据，应 false_positive，否则误报率统计口径被污染 | 属实，已修复 | DispatchHandler 失败分支前置 duplicate_handoff 识别 → false_positive。UC19-2 / test_s19_2 |
| compensated:{task_id} meta 审计键无界膨胀（无 TTL 无清理 + check-then-act 两步写） | 属实，已修复 | meta 键退役，追溯由 action_meta 回执 + 事件 compensated_by 标记双通道承载（R4/R6 措辞同步） |
| _summarize 对 JSONResponse 的 body 解析依赖 .body 属性（FastAPI 版本演进风险） | 属实，已钉死 | UC19-3 / test_s19_3 契约测试 |
| 交棒键收敛契约：patrol 用 patrol-comp:{task_id}，agent 原生交棒不带同式键——迟到原交棒与补偿撞不上同一幂等键，残余竞态窗从"良性先到先得"退化为双跑风险 | **已决策（2026-11-02 负责人拍板）：方案 A 落地** | patrol 派发键改规范交棒键 **handoff:{task_id}:{target_agent}**（单源派生函数 patrol.handoff_idempotency_key）。实证基础：idx_events_idem 唯一索引（SQLite/MySQL 双方言）+ 端点三层防重全部锚定它；skill 侧落地前 patrol 单侧使用，键空间与原生交棒不相撞，行为与旧键完全等价（单调不劣、零新风险面），skill 守门件渐进覆盖后窗口闭合。**三护栏**：①键派生式单源（严禁 patrol/skill 各拼一遍，函数即规范）；②键非空强校验（唯一索引对 NULL 互不相等，空键=永不幂等，缺参即抛）；③幂等命中（idempotent:true）在 action_meta 显式记 idempotent_hit——区分"我补发的"与"发现已被原生交棒命中"，Phase 2 误报统计干净口径（UC21-1/21-2）。skill 侧守门件按同式派生**另行立项**（落地前 A=B 行为等价、落地后窗口闭合，无需权衡 timing） |
| 通用性两处：parse_target_agent 正则与 a2a_redo 手工端点 agent 白名单两处口径无单源；naive 时间戳 mysql=CST 假设是部署期烙印（跨时区迁移静默错位，无防御性断言） | 登记 backlog | Backlog #6：agent 集合单源化 + 时区口径防御断言 |

### 第九轮（provider 无关性复核，代码零分叉实证 + 2 条轻遗漏 → 1 已补 1 待 PAT）

| 评审发现 | 裁决 | 处置 |
|---|---|---|
| 官方测试套件哨兵全为 gitcode: 前缀，github 键形态零官方判例（功能已由批次 5 仿真 7/7 证明） | 属实，已补 | UC20-1 / test_s20_1：github 键五判例（命中/L1 掩蔽/终态排除/黑名单精确不误伤/前缀剥离） |
| github 仓补偿默认开后成为常态路径，引擎侧凭据链（repo_key 前缀 → github 凭据）属端点既有职责但从未在补偿路径实测 | 待有效 PAT | **首日观测项**：获取有效 GitHub PAT 后托管真仓、造一条 github 仓断链走一轮补偿，确认引擎侧凭据链实测走通（当前 PAT 401 待重发） |
| 代码层 provider 无关性：三模块零 provider 字面量、链上敏感点（前缀剥离/_resolve_a2a_repo_key/PR 伪 id 三形态/身份/黑名单/归属/终态/场景）全部按 repo_key 键控 | 采信 | 仿真 7/7 + UC20-1 官方判例双证 |

### 第十轮（MySQL 方言级复审，1 真缺陷 → 已修复，1 指控经机制核实不成立）

| 评审发现 | 裁决 | 处置 |
|---|---|---|
| 租约回收 SQL 用 \|\| 拼接 action_meta：兼容层 _CONCAT_RE 只翻译 'gitcode:'\|\|col 形态原样穿透；MySQL 默认 sql_mode 下 \|\| 是逻辑 OR → 静默数据损坏（审计回执写成 '0'/'1' 无告警）。现网 SQLite 不受影响，但违反双后端兼容纪律 | 属实（中危），已修复 | 回收改读-改-写两段参数化 UPDATE（_set_finding 同款模式）。UC22-1 / test_s22_1 含方言级结构断言防回退 |
| 迁移 202611020002 的 sqlite_master 查询为参数化形态，兼容层不翻译 → MySQL 每次启动失败永不盖章 | **与实态不符**：源码本为字面量名形态，命中 _SQLITE_MASTER_SQL_RE 特判标记（translate_sql 层原样返回是标记机制，消费在 Cursor.execute → SHOW CREATE TABLE），双方言合规 | 加注释钉死口径（禁参数化写法），test_s22_1 断言正则命中 |
| 三个大问题（stalled 唯一键/存量库建表/claiming 孤儿）由部署实测抓到已修，本轮验证修复有效 | 采信 | s18_1/s18_2/s19_1 判例已在；评审五批仿真复跑零回归（4 处 FAIL 均为新语义预期变化） |

### 第十一轮（方言家族全扫描收口，0 新缺陷 → 记录归档）

| 评审发现 | 裁决 | 处置 |
|---|---|---|
| 上一轮所指 sqlite_master 参数化穿透为**误报**（评审自更正）：实际代码是字面量名形态，兼容层 SHOW CREATE TABLE 特判正常 | 与实态一致 | 与第十轮核清结论一致，口径已钉死（test_s22_1 断言正则命中） |
| \|\| 拼接家族全扫：全库唯一一处非 'gitcode:'\|\|col 形态即租约回收行（已在 05a3cdcb4 修复）；user/db.py 5 处均已被兼容层翻译；help_page_gen.py 命中为 JS 非 SQL | 已修复收口 | 读-改-写参数化 UPDATE + test_s22_1 结构断言防回退 |
| 排除项核清：sqlite_master 家族 12+ 处均为兼容层覆盖形态；INSERT OR IGNORE/REPLACE 双方言翻译正常且值有界；datetime('now')/CAST/ON CONFLICT 零命中（时间戳全 Python 侧预生成）；时间戳字符串比较两侧同格式成立；ORDER BY 布尔表达式双方言合法 | 采信 | 无需处置 |
| 新观察（低危，不修）：迁移 202611020002 的唯一键形态检测匹配 SQLite 建表语句格式——MySQL 上 SHOW CREATE TABLE 输出反引号 UNIQUE KEY 形态必然失配 → 首次启动走一次无条件重建（RENAME/INSERT SELECT 保数据后盖章，幂等只跑一次） | 登记不修 | 数据无损幂等，仅 MySQL 后端首次启动多一次重建；如未来 MySQL 启用后想消除，将检测正则改为兼容双形态（去空格 + 去反引号归一化）即可 |

### 第十二轮（QA 原子回归 + A6 命名规则 + 复验实测，2 真缺陷 → 已修复）

| 评审发现 | 裁决 | 处置 |
|---|---|---|
| A6【Critical】新增数据库表名规则：主控表须 grape_t_ 前缀——patrol_findings 裸名违规 | 属实，已修复 | 全链改名 **grape_t_patrol_findings**（DDL/迁移/业务 SQL/测试/API 文档/规格文档共 95 处引用）；本机重置迁移台账后重启实测：新名表自动建出 + 双迁移盖章 |
| 部署复验实测：补偿总闸关闭时 claiming 孤儿不被回收——回收逻辑埋在 compensate_open_findings 内被开关门控（孤儿在开关重开前永远滞留 claiming） | 属实（活性缺陷），已修复 | 回收提取为独立函数 reclaim_stale_claimings()，patrol_loop 每周期**无条件**执行（回收是卫生操作：只改状态不派发，开关关闭时回收为 open 也零副作用）。实测：开关关死 + 造孤儿 → 下周期回收为 open、action_meta 保留原文追加回收标记（读-改-写，无方言损坏） |
| QA 原子回归（PR #802 三轮）：47 红 = 46 登录节流 429 + 1 基线红（dev 基线复跑仍红，PR 未触及 event_pipeline）——全部与本 PR 无关；dismiss 端点原子覆盖 BLOCK（覆盖缺口非产品缺陷，grape-test#79 补单）；灰 2 面 = ①纯文档零风险 ②R2 误报（JSONResponse 桩 "skipped" 字面量误判，QA 人工复核已推翻） | 采信 | 终判"通过（增量面）·建议可合并·灰不阻塞"；待部署复验清单 5 项中 ①建表②findings API③scan done④meta 免重启⑤claiming 回收已全部在本机实测闭环（本轮新增⑤的开关无关性实测） |

### 第十三轮（实现后审计，2026-11-02，3 硬伤+3 中风险 → 3 落地/3 登记）

外部审计（基于代码实读非提交自述）发现项与处置：

| 审计发现 | 裁决 | 处置 |
|---|---|---|
| 高风险①：7 天回看窗外断链永久漏检——#463 存储清理每 6h null 化 7 天前 completed 任务 result_text，巡检判据 extract_next_step 随之失效；audit 存量核对有部署时效性 | 属实（平台互动事实，需求文档 §五.529 原已登记为设计约束） | 登记为**上线顺序硬约束**入验收清单（上线首日切 audit 跑一轮）；无自动保障——数据被清后不可判定符合不猜原则，人工时效动作不代做 |
| 高风险②：幂等键 skill 侧未对齐——dispatch 已用规范交棒键，全仓 grep skill/worker 侧零实现，落地前迟到原生交棒与补偿撞不上同键，窗口期双发风险真实 | 属实（设计文档 D5/决策 A 原文自认"skill 侧另行立项"） | 登记 §五 不在范围（跨仓协同，无排期即悬空头寸）；平台侧护栏（终检+状态流转抢占+端点三重防重）已全量生效，双发最坏=端点幂等拒绝一次 |
| 高风险③：findings 表无清理/TTL——终态行永久保留 + runtime 每周期全量重扫，表和扫描量无界增长 | 属实（唯一未登记缺口） | **本轮落地 R11/D21**：终态四状态行 TTL（默认 90 天，meta 可调，0=永不清理），随轮执行、purged 入回执 |
| 中风险④：主循环同步 DB 扫描阻塞事件循环 | 属实（run_patrol_once 持 db_lock 裸调） | **本轮落地 R12/D23**：asyncio.to_thread 卸载（两处调用）；DB 车道竞争登记残留（根治=行锁事务，S33 数据触发项） |
| 中风险⑤：断链判定依赖自报 next_step 字段 | 属实（设计固有，D6） | 已有约束不新增——字段是 complete API 合同字段（服务端组装结构化 JSON，非 LLM 自由文本解析）；审计原文"解析自然语言标记"经核实为机制误述 |
| 中风险⑥：无手动触发/运营介入面 | 属实 | **本轮落地 R12/D22**：POST /api/patrol/scan（只检测面，不补偿不抢主） |
| 低风险⑦-⑨：单测全 mock DB seam / leader 锁在 meta 表与共享 db_lock / 补偿默认全开 | 属实（交付纪律既定形态/已知设计取舍） | 维持现状——⑦ 是交付分支内容单一纪律（真库验收走 §七.3 清单）；⑧ 双主最坏=重复扫描不重复动作（唯一约束+状态流转兜底）；⑨ **维持 D19 拍板：上线即默认开（审计方"先关观察"建议不采纳——防误发靠结构性守卫非入口闸门，实测幂等闭环已验证；误报收敛通道=终检 false_positive+dismiss，系统性误报的紧急刹车=meta patrol:compensate_enabled=false / blocklist，事后关断不改上线口径）** |

### 第十四轮（实现后审计收尾，1 契约冻结落地 + 1 不改动项论证）

| 事项 | 裁决 | 处置 |
|---|---|---|
| 审计第②条的平台侧残余动作：skill 侧对齐前，平台侧键格式是单方面可漂移的跨仓契约 | 属实 | **落地 R13/S37**：模板字面量、单源同构、空白规整、空参拒绝四属性测试冻结（test_s37_1/2/3）——平台侧改形在 CI 爆红，跨仓对齐靶面钉死 |
| 审计第⑤条隐含的"叙述形态 next_step 漏检"面：result_text 为纯文本"任务自报完成 / a2a-qa"时 extract_next_step 返回空 | 经查证为**空集**（不改动项） | complete 接口自 fcbb5c78f（2026-09-09）起**始终**存结构化 JSON（tasks.py:363-373：叙述文本只作 JSON 内 conclusion/issue_summary 回填，next_step 恒为 JSON 字段）；叙述形态行只存在于该提交之前，早已超出 7 天回看窗与 #463 清扫线——为其加解析=死代码，按收窄纪律不做。引用纪律：该结论锚定 tasks.py 实读，非提交自述 |

### 第十五轮（全链路全场景扫描，6 项 → 2 代码落地 + 1 口径落地 + 3 登记）

对 head 697347354 逐段实读 patrol.py / patrol_compensate.py / routes / DDL 的全链路扫描（双发/误补偿/双主/风暴四条守卫链经推演均成立，无 P0）：

| 扫描发现 | 裁决 | 处置 |
|---|---|---|
| B【P2】手动扫描无并发闸/无超时——N 并发请求=N 并发全表扫描互踩 db_lock，audit 模式 HTTP 无限挂住 | 属实（R12 落地时带入的缺口，与选主总闸不对称） | **本轮落地 R15/S39**：single-flight 在跑标志，第二发 409，异常 finally 释放 |
| A【P2】findings 清单无归属过滤——任意登录用户可见全平台清单（repo_key/issue_id/detail），与 dismiss 的 S30 归属校验不对称 | 属实（BOLA 面，设计缺口） | **本轮落地 R14/S38（方案 A 拍板）**：admin 全量+unowned 置顶（管理页口径）；非 admin 仅 owner=自己；台账异常 fail-safe 降级 |
| C【P2】retention × audit 复活交互——R11 删终态行使 S31 短路集合失键，audit 重扫时 dismissed false_positive 复活为 open（runtime 模式 7 天窗免疫；compensated 行 L1 能查到补偿事件不再判断链，无重新派发） | 属实（第 13/14 轮未考虑的交互） | 登记：audit 前置核对时将 retention 暂设 0（set meta patrol:findings_retention_days=0）；audit 是一次性动作，不为交互改代码 |
| E【P3】时间维度两项：_align_convergence_delayed 5 秒延迟扫为进程内 task 无持久化（重启窗口丢 race 观测回执，对齐动作首轮已同步做、链路无损）；interval 运行期改小致旧租约被提前 CAS 接管（D3 收敛语义内，接管时刻不可预期） | 属实（低危） | 登记；数据触发项口径同 S33（观测层证明伤害再修） |
| F【P3】扫描 N+1 形态——每候选 2-3 条索引查询（索引实测在位），量级突变无显性信号 | 属实 | **告警落地 R15/S40**：duration>30s warning（不熔断）；阈值常量钉死，有调参依据后再进 meta |
| D【P3】_known_task_ids 全表集合查询进内存——retention 落地前无界，落地后受 TTL 约束 | 属实（已收敛） | 登记观察（元组集合内存可承受） |

### 第十六轮（闸收口推演，2 项 → 均落地）

对第 15 轮修复本身的推演复查发现两项收口缺口：

| 推演发现 | 裁决 | 处置 |
|---|---|---|
| _scan_in_flight 在路由层只挡手动轮对手动轮——自动轮×手动轮并发时两轮全表扫描互踩 db_lock（路由快检未命中窗口） | 属实（第 15 轮修复的不完整面） | **落地 R16/S41**：闸下沉到 run_patrol_once 函数级（线程安全，全入口共用）；自动轮 skip 不退避、手动轮 409、异常 finally 释放 |
| D23 只卸载了扫描段——loop 内 reclaim_stale_claimings 仍为持 db_lock 表扫描同步段直调 | 属实（卸载面不完整） | **落地 R16/S42**：与扫描同款 to_thread 卸载；选主→回收→开关顺序约束不因卸载改变（结构断言钉死） |

### 第十七轮（覆盖率对账，2 项零覆盖契约 → 1 缺陷修复 + 5 用例落地）

对两模块全部 19+14 个公开函数与测试文件做名字级对账，发现两个零用例覆盖的契约面：

| 对账发现 | 裁决 | 处置 |
|---|---|---|
| insert_finding 零直接覆盖——R6（id 一律 UUID 禁自增）/R8（owner 随行）硬性要求从未被行为断言；实测抓出 cursor() 在 try 外的真实缺陷：DB 异常外抛违反 S5 吞掉语义，单条落库失败会把整轮扫描打进退避 | 属实（含 1 个活性缺陷） | **缺陷修复**：cursor() 移入 try；**落地 R17/S43**：UUID/归属/默认值/rowcount 语义/异常吞掉三用例 |
| build_chain 零覆盖——D20 编排顺序红线（门卫→抢占→终检→身份→模式→派发）无断言，重排=静默语义破坏（终检挪到抢占前=先查后抢竞态回潮）；DispatchHandler 混入同步链=async 静默失效 | 属实 | **落地 R17/S44**：顺序冻结+派发隔离两用例 |

### 第十八轮（性能实测与 P1 落地，1 优化落地 + 3 数据触发登记）

磁盘 SQLite + 真模块函数的合成规模基准（500/5k/20k/100k 候选；**以下为作者环境报告值，非独立复现结论**）：全判定路径 18μs/候选线性（20k=368ms、100k=1810ms）、audit 全历史 20k=354ms、短路路径 20k=90ms、cleanup 10 万行 129ms（全过期）/17ms（零过期）、insert 0.9ms——当前量级单轮毫秒级，与 S40 30s 告警线差两个数量级，无事故面。

| 基准发现 | 裁决 | 处置 |
|---|---|---|
| P1：候选拉取整表带 result_text——S31 短路在 Python 侧，大字段已拉回才被丢弃（20k×2KB=40MB/轮；MySQL 网络读 470MB/小时纯浪费） | 属实 | **本轮落地 R18/S45**：两段式拉取（轻量列+短路前置+幸存者 IN 回捞 500 分块+异常吞掉）；最坏情形实测 368→237ms，判定语义零变化 |
| P2：N+1 反查（每候选 3-4 条查询）——SQLite μs 级无感，MySQL 网络下 20k 级未知候选 ≈15-30s 会撞 S40 告警线 | 属实（量级触发） | 登记数据触发项：S40 告警命中即做批量预取（issue 终态/L1/L2 各一条 IN 查询，N+1→常数条）——与 S33 行锁同款纪律，不预防性做 |
| P3：cleanup 每轮全表扫描 10 万行 17ms | 属实（可忽略） | 登记不修（加索引/降频收益 17ms/轮，不值）；retention TTL 已约束表规模 |
| P4：findings 清单排序无 (owner, created_at) 索引 | 属实（量级触发） | 登记量级触发（retention 已约束表有界，非 admin 有 owner=? 过滤） |

基准过程插曲（已排除、非产品缺陷）：cleanup 一度复现"purged=0"疑似缺陷，纯 SQLite 隔离定位为基准脚本自身 INSERT 列错位（status 列被塞时间戳）——产品 SQL 语义正确（s34 用例钉死状态集合参数），记录在案防复panic。

### 第十九轮（覆盖对账·P1 补口，1 项 → 落地）

| 对账发现 | 裁决 | 处置 |
|---|---|---|
| detect 全部既有用例传空 cutoff（audit 分支）——runtime 常态分支（生产默认路径）的两段式行为零覆盖：窗口谓词丢失=静默退化全历史扫描、参数错位、大字段回流均无测试拦截 | 属实（第十八轮 P1 落地时只改了实现未补分支级用例） | **落地 S46**：两分支互斥文本守卫对（runtime 带 `completed_at > ?`+绑定参数+无大字段；audit 无谓词无参数）——SQL 文本守卫先例：批处理 FIFO 钉法 |

### 第二十轮（分析人性能清单核验，2 项 → 落地 + 1 项维持登记）

| 清单项 | 裁决 | 处置 |
|---|---|---|
| 问题 1：zombie 检测逐任务探活 N+1——_engine_unreachable/_scene_missing 逐任务执行，同 runtime/同场景重复查（S24-3 只做同任务内复用） | 属实 | **落地 R19/S47**：轮内字典缓存（runtime_id / (repo,issue) 去重键），生命周期=单轮；分析人建议的"批量 IN 预取"改为等价的惰性 memoization——同样的去重效果，改动面更小 |
| 问题 2：issue_terminal 排在未超窗之前——窗内任务先查库再被丢弃 | 属实 | **落地 R19/S47**：缺键/超窗零成本纯谓词前置，纯谓词交换不变性 |
| 问题 3：L1/L2 逐条持锁 | 属实但维持登记（S40 告警+duration 逐轮日志兜底，到量级再动） | 登记不变（第十八轮 P2 项） |
| 清单"101 个既有单测"计数 | 更正：当前 tip 实测 95（patrol 单测），+test_openapi 8 合计 103——清单数字取自中间 head | 对账以 collect-only 实测为准 |

边界声明采纳：dashboard.py failed 列表无 LIMIT+result_text、events 详情大字段、agent_tasks 复合索引均非 635 引入，各自走独立需求，不混入本分支。

### 第二十一轮（分析人性能清单第二版核验，2 项 → 落地 + 1 项已修 + 2 项维持登记）

| 清单项 | 裁决 | 处置 |
|---|---|---|
| A：findings 逐条 INSERT+commit（SQLite 逐条 fsync / MySQL 逐条事务往返） | 属实 | **落地 R20/S48**：insert_findings_batch（executemany 攒批+每块单 commit+owner 锁外去重+单块容错）；清单漏列的"逐条 _resolve_owner 也是 N+1"一并修 |
| C：audit 全历史一次 fetchall 进内存 | 属实 | **落地 R20/S49**：keyset 分页（id>游标 LIMIT 500 逐页，每页独立查询锁内取页锁外处理——不采用跨锁 cursor 流式，规避共享连接迭代语义风险） |
| B：zombie 探活 N+1 + 场景解析无缓存 | **已于第二十轮修复**（清单基于旧 tip cbdb2c684）：轮内 memoization（runtime_id/(repo,issue) 去重），test_s47_1 钉死 | 无需再动；清单建议的 IN 批量预取与已落地 memoization 等价 |
| 谓词顺序（issue_terminal 在超窗前） | **已于第二十轮修复**（S47）：test_s47_2 钉死 | 无需再动 |
| D：补偿链同 (repo,issue) 重复查询 | 属实但维持登记 | 按清单自身风险标注（改动最大、可数据触发）；**正确性红线确认入档：终检 issue_terminal/L1/L2 与派发后回查是竞态防线，任何轮内缓存都不得覆盖（防"缓存终态→复活已收敛 issue"）**——可缓存的仅 mode 恢复的 get_issue_stage 与身份归属只读查询，补偿量到 S40 告警量级再做 |
| 清单"101 个既有单测" | 更正：当前 tip 实测 102（本轮 +5 后），此前 97 | 对账以 collect-only 为准 |

### 第二十二轮（覆盖对账·第22轮，2 项 → 均落地）

| 对账发现 | 裁决 | 处置 |
|---|---|---|
| _seed_meta_from_file 零用例——S13 声称的"文件仅首次 seed、meta 已有键不覆盖"从未被行为断言 | 属实 | **落地 R21/S50**：空键才写+已有键跳过+bool 小写序列化+异常吞掉 |
| run_patrol_once→insert_findings_batch 接线无钉死——S48 测批量函数本身，退回逐条写（每条一 commit 旧形态）不爆红 | 属实（S48 落地时接线未钉） | **落地 R21/S50**：检测产物整批进批量+回执 inserted=批量返回值 |
| 其余名字级缺口（_conclusion_snippet/_post_dispatch_race_check/permanent_failure/_meta_mode 等） | 经查均已有行为覆盖（dispatch payload/race 对齐/批处理分桶/t10 配置真源） | 无需新增 |

### 第二十三轮（外部深度复查核验，1 项 → 落地 + 2 项维持登记）

| 复查发现 | 裁决 | 处置 |
|---|---|---|
| _known_task_ids 轮内双拉（detect_broken/detect_zombies 各自全表拉同一集合） | 属实 | **落地 R22/S51**：known 共享参数（轮内一次拉取、双检测共享引用；None=自行拉取兼容旧签名）；语义等价依据=落库在两检测之后 |
| detect_zombies 仍 fetchall 全量——自指放大风险（僵尸堆积→running 行数增长→fetchall 内存增长，万级才有感） | 属实（数据触发级） | **登记**（与 S33 零窗项并列）：活跃集 fetchall 当前百级无感；到量级时与 broken 链路同款 keyset 化 |
| 复查报告"102 个 patrol 单测" | 更正：当前 tip 实测 107（本轮 +2 后） | 对账以 collect-only 为准 |
| keyset 分页×补偿批交互、缓存生命期、生成器安全 | 复查确认无问题（分页快照语义由 INSERT OR IGNORE+下轮重扫兜底，发现不丢至多晚一轮） | 无需动作 |

### 第二十四轮（行为层覆盖对账，2 项 → 均落地）

| 对账发现 | 裁决 | 处置 |
|---|---|---|
| R22 known 共享参数只在 mock 接收者层验证——真 detect 函数消费参数的语义（传入集合短路、不再自查表）无断言，共享优化退化为"接收但忽略"不爆红 | 属实 | **落地 R23/S52**：双检测 known 生效+零 findings 自查两用例 |
| lookback_mode 坏值回落只有文件层覆盖（t9），meta 层（运行期写坏形态）缺口 | 属实 | **落地 R23/S52**：meta 坏值回落 _cfg 默认+合法透传回归 |

### 第二十五轮（外部合并前复审，2 中 → 落地 + 2 低 → 登记/核验）

| 复审发现 | 裁决 | 处置 |
|---|---|---|
| 【中】main.py patrol 后台任务 shutdown 不 cancel——main.py 742 行注释记录的同类真实事故（漏 cancel→用例成片假失败）复发 | 属实 | **落地 R24/S53**：cancel+wait_for 5s 收口（与 obs 三循环同款） |
| 【中】补偿链五同步 handler 持锁查询在事件循环裸跑（D23 漏网段） | 属实 | **落地 R24/S53**：同步链 to_thread 卸载，派发保持 async，六段编排与短路语义不变（三用例钉死） |
| 【低】MySQL 全新库首启疑似误判唯一键形态白跑一次重建（数据无损） | 代码推演：全新库走 _NEW_TABLES 新 DDL（四列键），迁移 2 形态检查应命中早退——**待目标环境验收时实测确认**（列入 §七.3 清单） | 验收项 |
| 【低】路由层 409 快检无锁非原子 | 属实（设计如此）：函数级闸为权威闸（S41），路由快检仅省线程开销 | 已有 S41 兜底，登记不改 |
| docs-sync 2 失败 | 基线存量（历轮两侧对照复现） | 非本分支引入 |

### 部署实测记录（2026-11-02，本机全流程全场景真实验证）

环境：内部 dev 主机（主控+worker 同机，主机名按开源规范掩码），基线 weekly-09 @ caded1c4c + 本分支单提交，双平台仓库（GitCode 真仓 hello-flask-2 托管 + github: 平台行检测层），meta 真源配置，巡检循环 20-60s 快周期实测。

**部署即抓到两个真缺陷（均已修复+迁移+回归用例，S18）：**
1. 存量库（迁移台账已盖章）上 grape_t_patrol_findings 永不建表——_NEW_TABLES 只走 legacy 全量首跑路径；测试全绿是因测试手工执行 DDL 掩盖了启动链路。修复：register_migration 202611020001（safe）。
2. issue 级 finding（stalled_issue）task_id 恒空串，原三列唯一键使全部 stalled 共享一键——实测 14 条只落 1 条。修复：UNIQUE 加 issue_id 维度 + 迁移 202611020002 重建保数据。

**验证通过矩阵（全部真实验测，非单测）：**
- 存量发现：109 条断链，独立口径复算（双源判定独立实现）与巡检结果 **109=109 精确一致，零多报零漏报**；发现数 ≥ 60（验收第 1 条 PASS）
- 三层判定：L1 事后事件掩蔽、L2 下游任务掩蔽（含同刻边界 >）、issue 终态排除（completed/pending_review）、PR 订阅排除（#pr_ 前缀零 finding）、子任务键（parent_issue 命中构成证据/主链同 issue 不构成）全部实测正确
- 僵尸：running 两档（signal 确定性即时/window 2h 低置信）+ queued 引擎 offline 即时判死，全部实时命中
- 停摆链：issue 聚合逐 issue 落行（修复后）；扫描幂等（重复周期 inserted=0 不重插、dismissed 不复活）
- 配置真源：meta 改 interval 60→20 下一周期即生效（免重启）；启动行如实打印五键状态
- API：真实 JWT → findings 列表 200 + unowned（GH 未托管）置顶 + dismiss 收敛（原因入库、防复活）
- 补偿全链（真端点真引擎）：gitcode 子任务断链 → compensated（真 a2a_event seq=12 落库 + 真下游 qa 任务派发至本机引擎并真实执行完成，10 步 43,568 tokens，事件 #587 收敛）；补偿后重开同 finding → 终检 L1 命中 false_positive（防双发闭环，下游任务恒 1）；GH 未托管仓 → auth_unavailable 零派发回挂；409 not_in_scene → permanent_failure 终态不再无限重试；blocklist='*' → gate repo_blocklisted 零派发
- 平台互动事实（巡检设计约束，登记）：**#463 存储清理每 6h 将 7 天以上 completed 任务的 result_text 等 10 大字段 null 化**——巡检对 next_step 自报的存量可检测视界天然 = 7 天（更老的不可判定即不报，符合不猜原则；runtime 回看窗 7 天与该视界咬合）；真调度器会回收长时 running 合成僵尸（巡检僵尸档与 #537 回收共存）

**实测过程还发现并修正的测试盲区**：meta 键写入必须用 set_meta_value（对不存在行的裸 UPDATE 静默无效）——黑名单紧急刹车首测未生效即因测试脚本用了裸 UPDATE。

### 决策变更（2026-11-02，负责人拍板三项，均已于本提交落地）

| 项 | 原设计 | 变更后 | 理由 |
|---|---|---|---|
| 补偿开关默认值 | 默认关+白名单灰度 | **默认开**，能力即开即用 | 需求本意是上能力；"先关着"仪式化等于功能空转 |
| 仓库准入 | 白名单 `patrol:compensate_repos`（空=零放行） | **删除白名单**，改黑名单 `patrol:compensate_blocklist`（空=不拦，`*`=全拦紧急刹车） | 逐仓白名单规模化后无人维护=能力没做；防误发真守卫是终检+幂等键+状态流转抢占（结构性），非入口闸门 |
| 配置载体 | 8 项 env（后收窄为 4 项） | **DB meta 五键运行真源**（patrol:interval_seconds / confirm_minutes / lookback_mode / compensate_enabled / compensate_blocklist），每周期重读、改即生效、集群共享（MySQL 共库）；configs/patrol-config.json 仅首次启动 seed（login-whitelist 先例）；**patrol 模块零 os.environ 读取** | runtime-config 优先级链先例（管理台/meta > 文件 > 默认）；集群共享一份配置；运行期可调不重启 |
| 误报=0 前置硬线 | 核对误报=0 才放开自动补偿 | 改**事后观测**：终检拦存量慢到 + dismiss 收敛 + false_positive 回挂三通道对账；系统性误报紧急关走 meta 总闸 | 补偿默认开后前置硬线语义不再成立；误发可追溯可回滚（审计标记+幂等键） |

配套：UC10/UC13 用例族重写（test_t19 黑名单版、test_t9 文件守卫版、test_t9b 零 env 结构断言、test_t10 meta 真源、test_t10b seed 幂等），GateHandler verdict not_in_whitelist→repo_blocklisted。

### backlog 状态（三条，待办非阻塞）

1. result_text JSON 结构漂移自监控（解析失败计数进 findings）——设计风险表已登记
2. webhook.py #329/#587 去重处回链注释（防未来改口径者不知巡检依赖）——设计风险表已登记
3. compensated_by 端点层强制（当前两调用方约定+测试断言覆盖；待 a2a 契约演进一并处理）——第三轮评审采纳
4. dispatch_failed finding 回挂 open 后无限重试——加重试上限/指数退避（第五轮登记，噪音级无伤害）
5. L1 证据粒度细化到 agent 级（当前 issue 级，同 issue 向别的 agent 交棒会掩盖真断链→漏报）+ engine_config 恢复 + 设计文档"单写者"表述按补偿循环不受选主门控的现实修正（第六轮登记）
6. agent 集合单源化（parse_target_agent 动态解析 vs a2a_redo 白名单两处口径）+ naive 时间 mysql=CST 部署烙印防御断言（第八轮登记）

### 评审三轮/四轮修正 + 决策变更场景用例代码（追加于 Phase 2 段后）

```python
# UC2-7 场景缺失即时判死（评审三缺陷修正②）
def test_t5_zombie_queued_scene_missing():
    rid = 987657
    _seed_runtime(rid, cred_id=9, status="online", last_seen=_iso())   # 引擎健康
    tid = str(uuid.uuid4())
    _seed_task(tid, issue="#401", status="queued", created_at=_old(10), runtime_id=rid)
    # issue_state 绑定不存在场景（get_pipeline_config 四源不中 → fallback=True → 判死）
    _seed_issue_state(SENTINEL_REPO, "#401", subtask=0, scene_id="patrol-nonexistent-scene-x")
    hit = [f for f in patrol_mod.detect_zombies() if f["task_id"] == tid]
    assert hit and hit[0]["kind"] == "zombie_queued_dead"      # 即时归永不调度
    assert json.loads(hit[0]["detail"])["cause"] == "scene_missing"
    # 无场景绑定 → 不判死（不猜）
    tid2 = str(uuid.uuid4())
    _seed_task(tid2, issue="#402", status="queued", created_at=_old(10), runtime_id=rid)
    assert not [f for f in patrol_mod.detect_zombies() if f["task_id"] == tid2
                and f["kind"] == "zombie_queued_dead"]

# UC8-1..3 选主（评审三缺陷修正①：自续约必须真实生效）
def test_t8_leader_bootstrap_and_expiry():
    assert patrol_mod.claim_patrol_leader("h1", 300) is True    # 引导期 INSERT 获得主
    assert patrol_mod.claim_patrol_leader("h1", 300) is True    # 同主机自续约（holder|iso 前缀原子续）
    assert patrol_mod.claim_patrol_leader("h2", 300) is False   # 他主新鲜 → 非主
    # 过期值（新格式 holder|ts 与历史纯 ISO 各一轮）→ CAS 接管成功
    assert patrol_mod.claim_patrol_leader("h2", 300) is True
    assert patrol_mod.claim_patrol_leader("h3", 300) is True    # 历史格式兼容

# UC12-1 选主自续约通配符互斥（评审四轮：LIKE `_` 单字通配符防跨主机续锁）
def test_t8c_leader_renewal_wildcard_guard():
    # 持有者 host-a 持新鲜锁，host_a 不得自续约（旧 LIKE 实现会互开）
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE meta SET value=? WHERE key='patrol:leader'",
                  ("host-a|" + _iso(),))
        db.commit()
    assert patrol_mod.claim_patrol_leader("host_a", 300) is False
    with db_lock:   # leader 值未被越权改写
        c = db.cursor()
        c.execute("SELECT value FROM meta WHERE key='patrol:leader'")
        assert c.fetchone()[0].startswith("host-a|")
        db.commit()
    assert patrol_mod.claim_patrol_leader("host-a", 300) is True  # 本主续约不受影响

# UC13-3（决策变更）meta 真源：改值即生效 / 坏值回落 / 缺省走默认
def test_t10_runtime_config_meta_is_source_of_truth():
    set_meta_value("patrol:confirm_minutes", "7")
    set_meta_value("patrol:lookback_mode", "audit")
    rc = patrol_mod.patrol_runtime_config()
    assert rc["confirm_minutes"] == 7 and rc["lookback_mode"] == "audit"
    set_meta_value("patrol:confirm_minutes", "banana")   # 坏值 → 回落默认+告警
    assert patrol_mod.patrol_runtime_config()["confirm_minutes"] == 10

# UC13-4（决策变更）文件 seed 幂等：无键灌入、有键不覆盖、再跑零 seed
def test_t10b_seed_meta_from_file_once():
    patrol_mod._cfg = {"interval_seconds": 999, "confirm_minutes": 11,
                       "lookback_mode": "audit", "compensate_enabled": True}
    set_meta_value("patrol:confirm_minutes", "5")       # 已有键 → 不覆盖
    assert patrol_mod._seed_meta_from_file() == 3        # 其余三键 seed
    assert patrol_mod._seed_meta_from_file() == 0        # 幂等：再跑零 seed

# UC14-1（决策变更场景）零配置开箱即用：meta 全空 → 全链补发
def test_s14_1_default_on_zero_config_compensates():
    # （meta 全空，无白名单/开关配置）
    assert _aw(pc.compensate_finding(fid)) == "compensated"
    assert fake.calls[0]["user"]["id"] == 999            # D14 路径 B 身份
    assert "compensated_by=patrol:" in fake.calls[0]["payload"]["context"]

# UC14-2（决策变更场景）黑名单摘除：零派发，finding 保持 open
def test_s14_2_blocklisted_repo_no_dispatch():
    set_meta_value("patrol:compensate_blocklist", repo)
    assert _aw(pc.compensate_finding(fid)) == "repo_blocklisted"
    assert len(fake.calls) == 0                          # 零派发
    assert _get_finding(fid)[0] == "open"                # 人工处置通道保留

# UC14-3（决策变更场景）确认窗运行期收紧：默认 10 分钟不报 → meta=3 即时报
def test_s14_3_confirm_window_runtime_change():
    _seed_task(tid, result_text=json.dumps({"next_step": "a2a-qa"}), completed_at=_old(5))
    assert not [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]
    set_meta_value("patrol:confirm_minutes", "3")
    assert [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]

# UC16-1（评审六轮修正①）永久失败终态化 / 瞬态失败回挂
def test_s16_2_permanent_failure_terminal_transient_retries():
    with patch("src.api.routes.webhook.a2a_endpoint", fake_403):   # agent disabled 403
        assert _aw(pc.compensate_finding(fid1)) == "dispatch_permanent_failure"
    assert _get_finding(fid1)[0] == "permanent_failure"   # 非 open：批处理不再捞
    with patch("src.api.routes.webhook.a2a_endpoint", fake_500):   # 瞬态 5xx
        assert _aw(pc.compensate_finding(fid2)) == "dispatch_failed"
    assert _get_finding(fid2)[0] == "open"                # 回挂下轮再试

# UC16-2（评审六轮修正②，即 UC1-12）子任务 L2 关联键接线
def test_s16_1_subtask_l2_key_wired():
    _seed_issue_state(repo, "#S1", subtask=1, parent="#P1")
    _seed_task(tid_main, issue="#S1", agent="qa", created_at=_old(30))   # 主链同 issue
    assert [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]  # 不构成证据
    _seed_task(tid_sub, issue="#P1", agent="qa", created_at=_old(30))    # parent_issue 下
    assert not [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]  # 构成证据

# UC16-3（评审六轮小项）批处理 FIFO：created_at 最老先消费
def test_s16_3_batch_picks_oldest_open_first():
    # 三条 open finding 错开 created_at（最老=tids[1]），隔离存量 open 后 limit=1
    with patch("src.api.routes.webhook.a2a_endpoint", fake_ok):
        counts = _aw(pc.compensate_open_findings(limit=1))
    assert counts.get("compensated", 0) == 1
    assert 消费的 task_id == tids[1]

# UC15-1（评审五轮修正①·检测层）issue 终态排除：用户直接关 issue 不是断链
def test_s15_1_issue_terminal_excluded_from_detection():
    _seed_task(tid, issue="#405", result_text=json.dumps({"next_step": "a2a-qa"}), completed_at=_old(60))
    _seed_issue(SENTINEL_REPO, "#405", "completed")       # confirm_issue_done 所置
    assert not [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]
    _seed_issue(SENTINEL_REPO, "#405", "pending_review")  # 审阅终态同守卫
    assert not [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]
    _seed_issue(SENTINEL_REPO, "#405", "active")           # 非终态正常报
    assert [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]

# UC15-2（评审五轮修正①·终检版）竞态窗内收敛 → false_positive 零派发
def test_s15_2_recheck_issue_terminal_aborts_compensation():
    _seed_issue(repo, "#406", "completed")                # finding open 后用户才收敛
    with patch("src.api.routes.webhook.a2a_endpoint", fake):
        assert _aw(pc.compensate_finding(fid)) == "false_positive"
    assert len(fake.calls) == 0                            # 不复活已收敛链
    assert _get_finding(fid)[0] == "false_positive" and "已终态" in _get_finding(fid)[1]

# UC15-3（评审五轮修正②）补偿 context 携带原任务结论（等价性）
def test_s15_3_dispatch_context_carries_conclusion():
    _seed_task(tid, issue="#407",
               result_text=json.dumps({"next_step": "a2a-qa", "conclusion": concl},
                                      ensure_ascii=False), completed_at=_old(60))
    with patch("src.api.routes.webhook.a2a_endpoint", fake):
        assert _aw(pc.compensate_finding(fid)) == "compensated"
    ctx_str = fake.calls[0]["payload"]["context"]
    assert "原任务结论" in ctx_str and concl in ctx_str    # 下游拿得到上游结论

# UC17-1（评审七轮修正③）子任务键双源：issue 单表权威
def test_s17_1_subtask_key_reads_issue_table_authority():
    _seed_issue(repo, "#707", "active")            # 仅 issue 单表，无 issue_state 行
    # parent_issue_id + is_subtask_mode 写 issue 行（Phase C3 常态形态）
    assert patrol_mod._subtask_parent(repo, "#707") == "#P707"
    _seed_task(tid_main, issue="#707", agent="qa", created_at=_old(30))
    assert [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]  # 主链不构成证据
    _seed_task(tid_sub, issue="#P707", agent="qa", created_at=_old(30))
    assert not [f for f in patrol_mod.detect_broken_chains("") if f["task_id"] == tid]  # parent 命中

# UC17-2（评审七轮修正④）结论结构化提取，非整段 JSON
def test_s17_2_conclusion_extracted_structured_not_raw_json():
    with patch("src.api.routes.webhook.a2a_endpoint", fake):
        assert _aw(pc.compensate_finding(fid)) == "compensated"
    ctx_str = fake.calls[0]["payload"]["context"]
    assert concl in ctx_str and '"next_step"' not in ctx_str

# UC18-1（部署实测修正①）存量库迁移自动建表：台账盖章后 _NEW_TABLES 不再执行
def test_s18_1_migration_creates_grape_t_patrol_findings_on_stamped_db():
    mig = [m for m in _dbs._MIGRATIONS if m.name == "create-patrol-findings-table"]
    assert mig and mig[0].version == "202611020001" and mig[0].kind == "safe"
    mig[0].fn()            # 幂等：表已存在也成功
    with db_lock:          # 模拟存量库缺表 → DROP 后 fn 重建
        c = db.cursor(); c.execute("DROP TABLE grape_t_patrol_findings"); db.commit()
    mig[0].fn()
    with db_lock:
        c = db.cursor(); c.execute("SELECT COUNT(*) FROM grape_t_patrol_findings")
        assert c.fetchone()[0] == 0

# UC18-2（部署实测修正②）issue 级 finding 不被唯一键吞行（14 条只落 1 的回归）
def test_s18_2_stalled_issue_rows_not_swallowed():
    for iss in ["#920", "#921", "#922"]:
        assert patrol_mod.insert_finding("stalled_issue", task_id="", repo_key=SENTINEL_REPO,
                                         issue_id=iss, detail="{}")
    # 三个不同 issue 各一条；同 issue 二插仍去重

# UC18-3（部署实测修正②）旧三列唯一键表 → 迁移重建为四列键（保数据）
def test_s18_3_migration_rebuilds_old_unique_shape():
    # 模拟旧形态表（UNIQUE(kind,task_id,event_seq)）仅留一行 → fn 重建保数据
    # → sqlite_master SQL 断言 UNIQUE(kind,task_id,issue_id,event_seq) → 再跑幂等返回

# UC19-1（八轮修正①）claiming 孤儿租约回收（10 分钟，复用 updated_at）
def test_s19_1_claiming_orphan_recycled_by_lease():
    # 孤儿（claiming + updated_at=30 分钟前）→ 批处理回收为 open → 重试 compensated
    with patch("src.api.routes.webhook.a2a_endpoint", fake_ok):
        counts = _aw(pc.compensate_open_findings(limit=10))
    assert counts.get("claiming_recycled", 0) >= 1
    # 新鲜 claiming（他实例处理中）租约内不动

# UC19-2（八轮修正②）duplicate_handoff = 交棒已发生正向证据 → false_positive 分桶
def test_s19_2_duplicate_handoff_is_false_positive():
    async def fake_dup(*a, **k):
        return {"ok": False, "error": "duplicate_handoff"}
    with patch("src.api.routes.webhook.a2a_endpoint", fake_dup):
        assert _aw(pc.compensate_finding(fid)) == "false_positive"

# UC19-3（八轮修正④）_summarize 对 JSONResponse 的 body 解析契约
def test_s19_3_summarize_jsonresponse_contract():
    r409 = JSONResponse(status_code=409, content={"status": "skipped", "skipped": "not_in_scene"})
    ok, summary = pc.DispatchHandler._summarize(r409)
    assert ok is False and "409" in summary and "not_in_scene" in summary

# UC20-1（九轮）github 键形态五判例——官方套件补边，防 provider 分叉回归
def test_s20_1_github_key_form_full_judgment_suite():
    GH = "github:patrol-test/gh-form"
    # ① 断链命中 ② L1 掩蔽 ③ issue 终态排除 ④ 黑名单全前缀精确（gitcode 哨兵不误伤）
    # ⑤ 派发载荷前缀剥离（github:owner/repo → repository=owner/repo 同构）

# UC21-1（决策 A 护栏①②）键单源派生 + 非空强校验 + 索引存在断言
def test_s21_1_handoff_key_single_source_and_nonempty_guard():
    assert patrol_mod.handoff_idempotency_key("t-abc", "qa") == "handoff:t-abc:qa"
    assert patrol_mod.HANDOFF_KEY_TEMPLATE == "handoff:{task_id}:{target_agent}"
    for bad in (("", "qa"), ("t-abc", ""), (" ", "qa"), ("t-abc", "  ")):
        with pytest.raises(ValueError):
            patrol_mod.handoff_idempotency_key(*bad)
    # 端点 idx_events_idem 唯一索引真实存在（A 的实证基础）

# UC21-2（决策 A 护栏③）幂等命中 → compensated + action_meta 记 idempotent_hit
def test_s21_2_idempotent_hit_recorded_in_action_meta():
    async def fake_idem(*a, **k):
        return JSONResponse(status_code=200, content={
            "status": "ok", "seq": 42, "idempotent": True,
            "message": "duplicate idempotency_key, existing event returned"})
    with patch("src.api.routes.webhook.a2a_endpoint", fake_idem):
        assert _aw(pc.compensate_finding(fid)) == "compensated"
    assert "idempotent_hit" in _get_finding(fid)[1]

# UC22-1（十轮）方言级判例：零 || 拼接 + 字面量特判命中 + 防回退
def test_s22_1_mysql_dialect_compliance():
    from src.mysql_compat import _CONCAT_RE, _SQLITE_MASTER_SQL_RE
    src = open("src/api/patrol_compensate.py", encoding="utf-8").read()
    assert "action_meta=action_meta||" not in src          # MySQL || 是 OR，静默毁数据
    assert "SET status='open', action_meta=? WHERE id=?" in src  # 参数化读-改-写
    assert _SQLITE_MASTER_SQL_RE.match(                    # 字面量名 → SHOW CREATE TABLE 特判
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='grape_t_patrol_findings'")

# UC23-1（十二轮）A6 表名 + 无条件回收结构断言
def test_s23_1_a6_table_name_and_unconditional_reclaim():
    with db_lock:
        c = db.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%patrol%'")
        names = [r[0] for r in c.fetchall()]
    assert names and all(n.startswith("grape_t_patrol_findings") for n in names)  # A6 前缀
    for f in ("src/api/patrol.py", "src/api/patrol_compensate.py",
              "src/api/routes/patrol.py", "src/api/db_setup.py"):
        src = open(os.path.join(ROOT, f), encoding="utf-8").read()
        assert not re.findall(r"(FROM|INTO|UPDATE|TABLE)\s+(?:IF NOT EXISTS\s+)?patrol_findings\b", src)
    loop_src = open(os.path.join(ROOT, "src", "api", "patrol.py"), encoding="utf-8").read()
    i, j = loop_src.find("reclaim_stale_claimings()"), loop_src.find('if rc["compensate_enabled"]')
    assert 0 < i < j, "reclaim 必须在补偿开关判定之前无条件执行"

```
