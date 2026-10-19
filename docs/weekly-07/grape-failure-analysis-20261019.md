# Grape 平台日失败统计分析（2026-10-19）

> 统计窗口：2026-10-18 ~ 2026-10-19（`created_at >= date_sub(now(), interval 1 day)`，当日二次巡检刷新版：较当日早前版本新增 11:02 之后产生的失败，事件/任务口径已对齐当前窗口）
> 数据源：the platform events / agent_tasks tables（全量失败明细，未抽样）
> 目标分支来源：环境变量 the scheduler-injected work branch (weekly series)

## 一、总体统计

### 事件级（events）

| 状态 | 数量 | 占比（终态） |
|---|---|---|
| completed | 138 | 93.9% |
| failed | 9 | 6.1% |
| running | 6 | （未终态，不计入成功率） |

终态 147，成功率 **93.9%**。

### 任务级（agent_tasks）

| 状态 | 数量 | 占比（终态） |
|---|---|---|
| completed | 113 | 91.1% |
| failed | 11 | 8.9% |
| running | 7 | （未终态） |

终态 124，成功率 **91.1%**。

---

## 二、归类汇总

### 事件级失败（9 条，当前窗口）

| 类别 | 数量 | 占比 | 事件 seq |
|---|---|---|---|
| C1 场景绑定引擎全部离线（scene=default, agent=sre，派发层判死） | 3 | 33.3% | 13331, 13332, 13333 |
| C2 派发层选不到引擎且"裸 failed"无原因落库（a2a → sre） | 1 | 11.1% | 13328 |
| C3 认领时池内引擎全部不可达（hermes 离线），事件侧无回写 | 1 | 11.1% | 13424 |
| C4 静默超时（SIGTERM 后 grace tail 有输出，exit=-1） | 1 | 11.1% | 13375 |
| C5 引擎 initialize 超时（100s） | 1 | 11.1% | 13452 |
| C6 hermes ACP 引擎异常退出（exit=-1，stderr 截断，无头死亡） | 1 | 11.1% | 13472 |
| C7 服务端中止 + recover-orphans 回炉封顶判死 | 1 | 11.1% | 13485 |
| **合计** | **9** | **100%** | |

（早前版本另含 13454/13455 两条，当前窗口已出窗/状态变更，不计入本次账目。）

### 任务级失败（11 条，当前窗口）

| 类别 | 数量 | task id / 关联 |
|---|---|---|
| D1 issue 池绑定引擎（opencode, tag=issue）不可达，Issue #253 四连失败 | 4 | t_<id>, t_<id>, t_<id>, t_<id>（event_seq=468~471） |
| D2 qa 池 hermes 引擎认领时不可达 | 1 | t_<id>（event 13424，同 C3） |
| D7 服务端中止 + recover-orphans 封顶（同 C7） | 1 | t_<id>（event 13485） |
| D3 coding 静默超时 | 1 | t_<id>（event 13375，同 C4） |
| D4 exit_code=0 却记 failed、无 errors（无效完成自动重跑路径） | 1 | t_<id>（event 13433，事件最终 completed） |
| D5 引擎 exit=-1 异常退出且诊断信息截断 | 2 | t_<id>（event 13345，重派后事件 completed）、t_<id>（event 13472，同 C6） |
| D6 vod 引擎 initialize 超时 | 1 | t_<id>（event 13452，同 C5） |
| **合计** | **11** | |

各类数量之和 = 失败总数，账目对齐。

---

## 三、逐类分析与设计

### C1（4 条）：场景绑定引擎全部离线 → 派发层判死

**出错的逻辑**：`src/api/event_pipeline.py:1886-1890`（`src/api/routes/webhook.py:1023-1032` 同款逻辑）。事件入队前调用 `get_scene_pool_engine_ids(scene_id, agent_type)`，该函数已按健康分过滤；返回 `None`（场景 default 绑定的 sre 引擎当时全部离线/健康分不足）时走"方案A"：不入队、`_fail_event(seq, "场景绑定的引擎全部离线…")`，事件直接 failed（exit_code=1，秒级完成）。`src/runtime/service.py:1114-1116`、`1385-1387` 在**认领路径**上有兜底（pool 引擎全不可达时清空 `pool_engine_ids`，让任意引擎认领），但 **scene 绑定的入队前检查路径不经过这个兜底**，直接判死。

**修正后的逻辑**：入队前 `get_scene_pool_engine_ids` 返回 `None` 时，不立即 `_fail_event`，而是降级为"无绑定池任务"入队（`pool_engine_ids=''`），交给认领层兜底逻辑（service.py 已有）让任意在线引擎认领；仅当全平台该 agent_type 无任何在线引擎时才判死，且判死前先做一次引擎健康探测刷新，避免用陈旧健康分误杀。

**影响面评估**：
- 改动集中在 event_pipeline.py / webhook.py 两处入队检查，认领层复用现有兜底，逻辑闭环。
- 风险：降级入队后任务可能被"任意标签"引擎认领，绕过场景隔离意图——需保留 scene_tags 过滤（认领仍要求 scene 匹配或 all 标签）。
- worker 无需升级（认领兜底已在双主的服务端）；双主 scp 同步只需同步 api 层代码。
- status 枚举不变，无存量数据兼容问题。

**测试用例设计**：
1. mock `get_scene_pool_engine_ids` 返回 None 且平台存在 sre 在线引擎 → 事件应入队（queued）而非 failed，最终被认领成功。
2. mock 全平台 sre 引擎离线 → 事件仍走 `_fail_event`，result 含明确原因。
3. 场景绑定引擎在线但健康分临界 → 不应因陈旧健康分判死（探测刷新后入队）。
4. 降级入队的任务被认领后，`pool_engine_source` 字段应能区分"降级认领"与"正常绑定"，便于审计。
5. 回归：scene_tags=all 的引擎不受降级逻辑影响，照常认领。

### C2（2 条）：pick_engine 返回 None → "裸 failed" 无原因落库

**出错的逻辑**：`src/api/event_pipeline.py:804` 与 `:1054`。a2a 订阅/PR 订阅派发路径中 `pick_engine(repo_key, agent_type)` 返回 None 时直接 `update_event_status(seq, "failed")`——不写 result、不写 exit_code、不写 completed_at（seq 13328/13455 三字段全空）。文件头部注释（event_pipeline.py:28）自己承认："原裸 update_event_status(seq, 'failed') 无原因落库，排障需倒查日志"。本次两条 sre a2a 事件正是踩中此路径，且与 C1 同时段，根因同为 sre 引擎当时不可选。

**修正后的逻辑**：两处统一改走现成的 `_fail_event(seq, reason)`（event_pipeline.py:2195），它自带 result/exit_code/completed_at 回写，并同步关联 agent_tasks 收口。

**影响面评估**：
- 改动极小（两行调用替换），`_fail_event` 已存在且带异常保护。
- 无 worker 升级、无双主同步、无 status 枚举兼容问题。
- 修好即清零"无头失败"记录，排障不再需要倒查日志。

**测试用例设计**：
1. a2a 订阅路径 mock `pick_engine` 返回 None → events 行应含 result 原因、exit_code=1、completed_at 非空。
2. `_fail_event` 内部异常（mock db 抛错）→ 事件状态仍被置 failed，不抛出到派发主流程。
3. 关联 agent_tasks 处于 running/queued 时被同步收口为 failed。
4. 回归：正常 pick_engine 路径行为不变。

### C3/D2（事件 13424，任务 t_<id>）：认领时池内引擎全部不可达，事件侧无回写

**出错的逻辑**：`src/runtime/service.py:108-126`。qa 任务（skill-tester）入队时绑定了 1 台 hermes 引擎（`pool_engine_ids`），到 `claim_by_tag` 认领时该引擎离线/失联/标签不匹配 → 任务停止派发、agent_tasks 置 failed（errors 有明确文案）。但**事件侧**（seq 13424）exit_code=NULL、result 空、completed_at 空——任务失败后事件未收口，状态虽为 failed 但零诊断信息。

**修正后的逻辑**：service.py 认领判死处（以及所有将 agent_tasks 置 failed 的路径）增加对关联 event 的回写（复用 `_fail_event` 语义：result/errors 原因 + exit_code + completed_at）。

**影响面评估**：
- 涉及 runtime/service.py 双主共跑模块，改动需双主 scp 同步后同时生效；建议放在一个独立小函数避免两处漂移。
- 与 skill 里已知的"service.py 有 pool 兜底但 scene 绑定路径不覆盖"（C1）是姊妹问题，修 C1 后本类出现频率会显著下降（兜底清空 pool_engine_ids 后可被任意引擎认领）。
- 事件表结构不变。

**测试用例设计**：
1. 入队绑定引擎 A，认领前将 A 置 offline → 任务 failed 且事件行含 result/exit_code/completed_at。
2. 认领判死后 `pool_engine_ids` 保留原值（审计需要），但事件有完整原因。
3. 双主同时运行时两个实例各判死一次 → 事件回写幂等（不重复叠加 result）。
4. 回归：pool 兜底路径（引擎恢复在线）任务仍可被认领。

### C4/D3（事件 13375，任务 t_<id>）：coding 静默超时

**出错的逻辑**：`src/agent/executor.py:806-843`。`AGENT_SILENT_WINDOW` 默认 300s（与 hermes ACP `_SILENT_TIMEOUT` 对齐）；进程存活时可续期，但累计续期受 `AGENT_SILENT_EXTEND_CAP`（默认 1×max_exec_seconds）硬上限约束。本次 coding-cli（opencode 引擎）任务运行 1275s 后触发静默超时：SIGTERM → 10s grace 收尾窗口内读到了半截工作文本（"已读取完整组件…按 TDD 先写测试（RED）"）→ `_killed="grace"`、exit_code=-1、errors 为空（有 grace tail 时不附 "no output for Ns"）。任务实际已接近完成，属于"长命令/长思考被静默窗口误杀"的边界场景（#501 已修一部分，续期额度耗尽仍会杀）。

**修正后的逻辑**：
- 短期：对 coding 类长任务调大 `AGENT_SILENT_EXTEND_CAP`（环境变量，无需改码）。
- 中期：grace tail 非空（agent 正在收尾产出）时，不立即判 failed——追加一次"收尾观察期"（如再给 60s），仅当收尾观察期后仍无完整 done 事件才判死；或把 grace tail 文本作为 result 落库并标记 `partial_output=1`，供上层决定是否自动重派续跑。

**影响面评估**：
- executor.py is worker-side code; all workers need a batch upgrade (rolling release)。
- 续期额度调大后需重新核算 `RUNTIME_TASK_TIMEOUT=3600s ≥ 2×(单次最坏)` 的预算（executor.py:692-694 注释约束）。
- status 枚举不变；`partial_output` 若落 result 需与展示层约定格式。

**测试用例设计**：
1. 模拟 300s 无输出但进程存活 → 续期日志出现，不判死（#501 回归）。
2. 续期额度耗尽 + SIGTERM 后 grace tail 有文本 → 新逻辑进入收尾观察期而非立即 failed。
3. 收尾观察期内产出 done 事件 → 任务 completed，result 含完整文本。
4. 收尾观察期仍无输出 → 判死且 errors 含 "no output for Ns"。
5. `RUNTIME_TASK_TIMEOUT` 预算校验：启动期告警逻辑对新的 EXTEND_CAP 值仍然成立。

### C5/D6（事件 13452，任务 t_<id>）：引擎 initialize 超时（100s）

**出错的逻辑**：`src/agent/hermes_acp.py:346-348`。hermes ACP 适配器 initialize 阶段硬编码 `timeout=100`（346/365/369/374/385 多处），超时后 `error_events.append(f"initialize failed: {init_err}")` → engine exit_code=-1，事件 failed。the vod agent failed to finish initialization within 102s。100s 对冷启动（首次加载模型配置/网络抖动）偏紧，且无重试。

**修正后的逻辑**：initialize 超时改为环境变量可配（如 `ACP_INIT_TIMEOUT`，默认提到 180s），并加一次带退避的自动重试（仅 initialize 阶段幂等，可安全重试）。

**影响面评估**：
- worker-side change; all workers need a batch upgrade;hermes_acp 与 hwcloud_acp（446 行同款）应同改。
- 重试只作用于 initialize 握手，不影响会话语义，风险低。
- 若网络持续不通，总耗时增加（180s+退避），需确认小于外层任务超时预算。

**测试用例设计**：
1. mock initialize 第一次 100s 无响应、第二次成功 → 任务最终 completed。
2. mock 两次都超时 → failed，errors 含 "initialize timeout after 180s"。
3. `ACP_INIT_TIMEOUT` 未设置时默认值生效。
4. hwcloud_acp 同场景回归一致。
5. initialize 重试期间不占用额外 busy_slots（不重复占用引擎并发额度）。

### D1（4 条）：issue 池绑定引擎不可达（Issue #253 四连失败）

**出错的逻辑**：与 C3 同源（service.py:108-126 认领判死）。Issue #253 派发的 4 个 issue-agent 任务 `pool_engine_ids` 各绑定 1 台 opencode 引擎（tag=issue），认领时全部不可达 → 停止派发。**额外发现数据完整性问题**：这 4 条任务的 `event_seq=468~471` 在 events 表中不存在（`SELECT count(*) FROM events WHERE seq<1000` = 0）——孤儿外键，排障时无法回溯事件上下文。the current online engines for the tag differ from the bound (offline) opencode engines，说明**池绑定时未按健康分过滤或绑定后引擎下线未解绑**。

**修正后的逻辑**：
- 入队时池引擎选择接入健康过滤（与 `get_scene_pool_engine_ids` 同标准）；认领判死后自动解绑失效引擎并在池内重选（service.py 兜底已具备清空能力，补上"重选"一步）。
- event_seq 外键写入前校验存在性，或提供事件清理时的级联策略说明，避免孤儿引用。

**影响面评估**：
- 服务端（service.py）+ 派发层双重改动；双主需同步。
- 重选逻辑要防循环（全部不可达时终止并判死，复用 C1 修正后的最终判死口径）。
- 存量孤儿 event_seq 任务不影响新逻辑，无需刷数据。

**测试用例设计**：
1. 池内唯一绑定引擎 offline → 入队时改选在线同标签引擎。
2. 全部 issue 标签引擎 offline → 任务判死且 errors 含完整原因（含引擎名单）。
3. 引擎恢复在线后，此前被解绑的任务可重新绑定/认领。
4. event_seq 指向不存在事件 → 记录告警日志，任务照常执行。
5. Issue 重复派发 4 任务场景 → 4 条任务共享一次健康检查结果（不重复探测）。

### D4（1 条）：exit_code=0 却记 failed（t_<id>，event 13433）

**出错的逻辑**：executor.py:692-694 注释所述"无效完成自动重跑"路径：第一次执行 960s 后以 exit_code=0 结束但被判为无效完成（result_text 为空），触发自动重跑并/或重派（17s 后新任务 t_<id> 创建并 2 分钟完成，事件最终 completed）。第一次尝试被置 failed 但 **errors 为空、result_text 为空**——"失败"没有任何原因记录，统计口径被污染（本条本质是重跑机制的一部分，不算真实业务失败）。

**修正后的逻辑**：重跑路径将首次尝试置 failed 时，errors 必须写入明确原因（如 `"invalid completion (empty result), auto re-run"`），或引入独立状态 `superseded`（需评估前端/统计枚举兼容），至少保证 result_text/errors 非空。

**影响面评估**：
- worker 端（executor.py）改动，13 台 worker 批量升级。
- 新增 status 枚举 `superseded` 影响面大（前端、统计 SQL、导出），建议先做"补 errors 文案"最小改法。
- 统计口径：修好后日报告中"真实失败"数下降，成功率计算更准。

**测试用例设计**：
1. 构造首次执行空结果、重跑成功 → 首次尝试 errors 含 "invalid completion" 原因。
2. 重跑也失败 → 事件最终 failed 且两次尝试原因都可见。
3. 正常完成（非空结果）→ 不触发重跑（回归）。
4. 统计脚本按 errors 前缀可区分"重跑淘汰"与"真实失败"。

### D5（1 条 + C6 同族 1 条）：引擎 exit=-1 异常退出且诊断信息截断（t_<id>，event 13345）

**出错的逻辑**：sre 部署任务（huawei-cloud-devbridge-deploy）在 hermes 引擎上跑 35 分钟后 engine exit_code=-1；errors 内嵌的 stderr 仅含 ACP 适配器启动两行日志，且在时间戳 `2026-10-18` 半截处被截断（engine 侧上报即截断），真实死因（OOM/网络/引擎崩溃）不可考。事件随后被重派，新任务 18 分钟部署成功（事件最终 completed）——说明任务本身可执行，是引擎实例异常。

**修正后的逻辑**：engine 上报 errors/stderr 的截断策略改为"头尾各保留 N 字节 + 中间省略标记"，并在截断时附上 exit 信号、进程存活时长、最后 10 行日志；必要时 stderr 全量落文件、errors 只放文件路径引用。

**影响面评估**：
- worker 端改动 + 可能需要对象存储/本地文件保留策略（磁盘占用评估）。
- 无 status 枚举变化；双主无关（worker 单侧）。
- 修好后此类"无头引擎死亡"可定位根因。

**测试用例设计**：
1. 构造超长 stderr（>截断阈值）→ errors 含头尾片段与省略标记，长度受控。
2. 构造引擎 SIGKILL → errors 含信号信息。
3. 正常完成 → errors 为空（回归）。
4. stderr 落文件路径可被下载/读取且与 errors 引用一致。

### C6/D5（事件 13472，任务 t_<id>）：hermes ACP 引擎异常退出（第二例，当天 513s 即死）

**出错的逻辑**：与 D5（t_<id>）同族。coding 任务(ops-plan-output, an internal ops repo issue)在 hermes 引擎上启动后仅 513s 即 engine exit_code=-1；errors 仅含 ACP 适配器启动两行 INFO 日志（`acp_adapter.entry: Loaded env... / Starting hermes-agent ACP adapter`）并在半截处被截断，真实死因不可考。与 D5 的 35 分钟死亡不同，本例启动后 ~8.5 分钟死亡，两例叠加说明 hermes ACP 适配器存在不稳定的崩溃模式（疑似与 the isolated ACP home directory loading/会话初始化相关）。

**修正后的逻辑**：同 D5 的 stderr 截断策略修复（头尾保留 + 信号/存活时长），另加：hermes ACP 适配器进程退出时强制 dump 最后 10 行日志与退出信号（SIGSEGV/SIGKILL/OOM 区分）；对同引擎短窗口内第二次 exit=-1 自动降低该 runtime 健康分（health_scorer 已有失败计数维度，确认 engine exit=-1 也计入而非仅任务级 failed）。

**影响面评估**：
- worker 端改动（hermes_acp / acp_adapter），13 台 worker 批量升级。
- 健康分联动在服务端，双主同步。
- 无 status 枚举变化。

**测试用例设计**：
1. 构造适配器启动 60s 后 SIGSEGV → errors 含信号、最后日志行与存活时长。
2. 同 runtime 30 分钟内两次 exit=-1 → 健康分下降，新任务改派其他引擎。
3. 正常长跑任务 → 健康分不受影响（回归）。
4. stderr 超长 → 头尾保留截断策略生效（与 D5 用例共用）。

### C7/D7（事件 13485，任务 t_<id>）：服务端中止 + recover-orphans 回炉封顶判死

**出错的逻辑**：`src/runtime/agent.py:1902`（"task terminated by server (aborted during execution)"）+ `src/api/routes/runtime.py:690-750`。coding-cli 任务（grape-dev/grape 自身仓库）运行仅 28s 即被服务端中止；agent_tasks 同条任务 result_text 为 "recover-orphans attempts cap reached (possible self-restart loop)"——即该任务此前已因 master/worker 重启或心跳断触被 recover-orphans 置回 queued，回炉 attempts 达到 `RECOVER_MAX_ATTEMPTS=3` 封顶后 force-fail。runtime.py:680-691 注释描述的自噬循环场景（部署类任务内含"重启 master"）已做活跃度检查，但本例任务 28s 即死说明中止发生在启动早期——活跃度窗口（task_messages 1200s 无新消息即判孤儿）对"刚认领还没来得及发消息"的任务误判为孤儿，回炉计数被快速耗尽。

**修正后的逻辑**：活跃度检查增加"启动宽限"：started_at 距今 < 启动宽限期（如 300s）的任务即使无 task_messages 也跳过回炉（新认领任务尚在初始化，hermes 长命令静默窗口本身就可能 300s 无消息）；force-fail 文案同时写入 events.result（当前事件侧只有 agent.py 的通用 aborted 文案，未带 attempts 回炉上下文）。

**影响面评估**：
- runtime.py 在服务端，双主 scp 同步。
- 启动宽限期参数化（环境变量），默认值需小于 RECOVER_ACTIVITY_WINDOW。
- 风险：真正秒死的孤儿任务会多等一个宽限期才回炉，可接受。

**测试用例设计**：
1. 任务认领后 60s、无 task_messages、master 重启触发 recover-orphans → 跳过回炉（启动宽限生效）。
2. 任务认领后 600s、无 task_messages → 照常回炉，attempts+1。
3. attempts=2 的任务再次回炉 → force-fail，且 events.result 含 "attempts cap" 上下文。
4. 回归：有 task_messages 的活跃任务永不回炉。
5. 启动宽限期内任务真死（进程退出）→ 由 sweeper 的 long_running/dead_runtime 规则兜底回收，不悬空。



| 优先级 | 项 | 预期清零 | 改动量 |
|---|---|---|---|
| P0 | C2 裸 failed 改走 `_fail_event`（两行） | 1 条事件级无头失败 | 极小 |
| P0 | 运维：恢复/更换 sre、issue 标签绑定引擎在线（含解绑离线 opencode） | C1 3 条 + D1 4 条 + C3/D2 1 条 | 无代码 |
| P1 | C1 scene 绑定路径接入认领层兜底（降级入队） | C1 3 条（与运维叠加） | 中 |
| P1 | C3 任务失败回写事件收口 | 1 条 + 未来同类 | 小（双主同步） |
| P1 | C7 recover-orphans 启动宽限期 + force-fail 事件回写 | 1 条 + 未来同类 | 小（双主同步） |
| P1 | C5/D6 initialize 超时可配 + 重试 | 1 条 | 小（worker 批量升级） |
| P2 | D4 重跑淘汰补 errors 原因 | 1 条统计口径 | 小（worker 批量升级） |
| P2 | C4/D3 静默超时收尾观察期 / 调大 EXTEND_CAP | 1 条 | 中（worker 批量升级） |
| P2 | C6/D5 hermes ACP 崩溃：stderr 头尾保留 + 健康分联动 | 2 条排障能力 + 自动止损 | 中（worker 批量升级） |

---

## 附：巡检中发现的独立安全问题（不计入失败账目）

**agent_tasks.env_extra 明文落库敏感凭据**：任务行 env_extra 含华为云 `HUAWEICLOUD_SDK_AK/SK`、`GRAPE_SESSION_TOKEN`、`TOKEN` 等，另有列 `repo_gitcode_token` 明文存储平台 token。任何能读该库/备份的角色（含巡检 SQL 本身）即可获取云凭据。仓库已有 `docs/vcs-env-token-boundary-audit-design.md` 做边界审计，建议将 env_extra 凭据改为加密存储（KMS/CSMS）或按需注入不落库，并轮换已暴露 AK/SK。

## 设计文档位置

- 仓库：grape-dev/grape
- 分支：特性分支 `docs/failure-analysis-20261019`(based on the protected weekly integration branch; delivered via MR, not pushed directly)
- MR：合并前见 gitcode MR 链接（见下方），合并后完整文件路径：`docs/weekly-07/grape-failure-analysis-20261019.md`

