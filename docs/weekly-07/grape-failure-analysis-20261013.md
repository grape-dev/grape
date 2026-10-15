# Grape 平台日失败统计分析 — 2026-10-13

> 统计窗口：`created_at >= date_sub(now(), interval 1 day)`（2026-10-12 20:00 ~ 2026-10-13 20:00 左右）
> 数据源：grape_system_db.events / agent_tasks

## 一、总体统计

### 事件级（events）

| 状态 | 数量 | 占比 |
|---|---|---|
| completed | 717 | 94.6% |
| failed | 32 | 4.2% |
| cancelled | 4 | 0.5% |
| running | 5 | 0.7% |
| **合计** | **758** | 成功率 **94.6%** |

### 任务级（agent_tasks）

| 状态 | 数量 | 占比 |
|---|---|---|
| completed | 708 | 90.3% |
| failed | 66 | 8.4% |
| cancelled | 8 | 1.0% |
| running | 2 | 0.3% |
| **合计** | **784** | 成功率 **90.3%** |

任务级失败（66）多于事件级失败（32）：一个事件底层 agent_task 可能重试多轮（每轮重试各自留一条 failed 任务行），另有少量 failed 任务行对应的事件在窗口外或重试后最终 completed。

## 二、归类汇总（事件级 32 条，逐条核对，合计=32 ✓）

| # | 类别 | 数量 | 占比 | 事件 seq |
|---|---|---|---|---|
| C1 | 业务性失败（SRE/vod 验收门槛未过或结论 blocked，exit=1） | 7 | 21.9% | 10631, 10818, 10822, 10914, 11020, 11090, 11122 |
| C2 | 静默超时（-1 半截文本 / -9 no output killed） | 11 | 34.4% | 10967, 10975, 10986, 11027, 11033, 11120, 11166, 11262, 11272, 11347, 11365 |
| C3 | 用户手动终止被记为 failed（状态口径问题） | 4 | 12.5% | 10673, 10693, 11207, 11298 |
| C4 | 引擎异常退出（exit=-1，errors 为空） | 4 | 12.5% | 10656, 11002, 11119, 11315 |
| C5 | 外层任务超时（task timed out after 3600s） | 2 | 6.3% | 10688, 10788 |
| C6 | 派发层判死（绑定引擎不可达，exit_code NULL） | 2 | 6.3% | 11256, 11299 |
| C7 | 引擎初始化超时（initialize timeout after 100s） | 1 | 3.1% | 11213 |
| C8 | 配置错误（缺少华为云凭证） | 1 | 3.1% | 10824 |

任务级补充（66 条中除与上述事件对应的重试行外的新根因）：

| 根因 | 任务数 | 代表 task_id |
|---|---|---|
| 绑定引擎不可达/健康分不足，停止派发（对应事件多在窗口外或重试成功） | 9 | t_047d23f8, t_05f8f77, t_0b4e7fc, t_26ec4bf, t_3899a73, t_6637594, t_6adc3a6, t_76c12b91, t_8c2bda65 |
| recover-orphans attempts cap reached（疑似自重启循环） | 4 | t_65ed6e1, t_880040b, t_e73c9c1, t_f6350f9 |
| 模型流异常（IAM 不可达 + engine exited 0 with empty output） | 1 | t_984cc8f |

## 三、逐类分析与设计

### C1 业务性失败（7 条）

**出错的逻辑**：这类不是平台故障。SRE agent（skill=forest-v2-codex-sre-test / deploy-cli / cloud-deploy）正常跑完，产出结论为 `failed/blocked`（远程验收 48 项未全过、验收工具 current_task_auth_failed、上线证据链不齐），按验收门槛返回 exit_code=1，平台如实记 failed。典型：seq 11020（48 项过 22 失败 2 跳过 24）、seq 11090（46 过 1 失败 1 跳过）、seq 11122（48/48 过但 401 根因未证明）。seq 10914 略特殊：本地验证实际通过，仅基线既有失败用例导致退出码 1。

**修正后的逻辑**：平台侧不需要"修复"；需要的是**结果分级**——在 result 中区分「agent 执行失败」与「agent 执行成功但业务结论为 blocked/failed」。建议 event 增加结论字段或在 status 枚举中引入 `blocked`，SRE 技能模板统一输出结构化结论（acceptance: passed/failed/blocked + 计数），统计口径将业务 blocked 从系统失败中剔除。

**影响面评估**：status 枚举变更影响所有消费方（前端 observability、dashboard 成功率、weekly 报表）；需 worker 端 executor 与服务端 event_store 同步发版，历史数据不回填。改动集中在 result 解析层则影响小。

**测试用例设计**：
1. SRE 任务返回 exit=1 且 result 含 "blocked" → 统计中归入 blocked 而非 failed。
2. SRE 任务返回 exit=1 且 result 含 "failed：远程验收" → 归入 blocked（业务验收未过）。
3. exit=1 且 errors 含堆栈/配置错误 → 仍归 failed（系统失败）。
4. 灰度期间旧 worker 不输出结构化结论 → 降级回 failed，前端不报错。
5. 事件重试后最终 completed → 统计只按事件终态计数。

### C2 静默超时（11 条，最大头）

**出错的逻辑**：worker 端 `src/agent/executor.py:663` 的静默看门狗——引擎 `_silent_window`（默认 300s）无输出先 SIGTERM，grace tail 有残余文本则 exit_code=-1、result 留半截工作文本（seq 10967/10975/11033/11120/11272/11347）；grace tail 为空则 -9 且 errors 记 "no output for 300s, killed"（seq 10986/11027/11166/11262/11365）。多发生在 coding/vod/issue 长任务：agent 正在探索代码库或跑构建/全量测试（如 build:test + node:test 门禁），单步耗时长于 300s 静默窗口即被杀。

**修正后的逻辑**：区分"真挂死"与"长时间无输出但仍在干活的原子步骤"。方案：executor 在下发长命令（构建/测试）前后向看门狗发 keep-alive 心跳（命令开始/结束事件即视为输出），或对带 `long_running` 标记的命令调大该步的静默阈值；同时对 coding 类任务把 `_silent_window` 提到 600s 并保留外层 3600s 总超时兜底。

**影响面评估**：executor.py 为 worker 端核心，13 台内部 worker（114-126）需批量升级；双主 scp 同步链路需同步推送；心跳机制要求 agent 端技能模板配合（旧技能不发心跳则退化为现行为，向后兼容）。`-9` 与 `-1` 的语义保留，避免下游报表口径震荡。

**测试用例设计**：
1. 模拟 agent 单步命令运行 400s 后正常完成（期间命令包装器发心跳）→ 不被杀，任务 completed。
2. 模拟 agent 400s 完全无任何心跳/输出 → 仍按静默超时杀（-9）。
3. 心跳后 300s 无输出且无新心跳 → 看门狗仍触发（心跳不无限续命，单步上限存在）。
4. 总时长逼近 3600s → 外层 RUNTIME_TASK_TIMEOUT 仍生效（exit=-1, "task timed out"）。
5. 旧版技能无心跳 → 行为与当前一致（兼容回归）。

### C3 用户手动终止被记为 failed（4 条）

**出错的逻辑**：`src/api/routes/events.py:339` —— 用户在 UI 终止事件时，worker 模式下事件被写为 `status='failed'`、`result='用户手动终止'`、exit_code=-1；direct 模式走 `fail_task`（events.py:346）同样落 failed。而 `src/api/routes/runtime.py:809` 的任务级路径却正确写 `cancelled`。即同一动作在两个入口的口径不一致，事件级把"用户主动取消"污染进失败统计。

**修正后的逻辑**：events.py 的两个终止分支改为 `status='cancelled'`、exit_code 置 NULL（或专用码），result 保留"用户手动终止"；scheduler.py:648/785 的 error 口径同步对齐。统计口径自然修正：事件失败数 -4，cancelled +4，成功率 94.6% → 95.1%。

**影响面评估**：改动小、纯服务端，不涉及 worker 升级；但下游若有按 result like '%手动终止%' 判断的报表需同步；status 枚举 cancelled 已存在，无兼容问题。

**测试用例设计**：
1. worker 模式任务运行中用户点终止 → event.status=cancelled、result=用户手动终止、exit_code NULL。
2. direct 模式同上 → cancelled 而非 failed。
3. 对已 completed 事件发终止 → 幂等不覆盖终态（沿用 2046 行既有保护）。
4. 终止后查询统计接口 → 该事件计入 cancelled 不计入 failed。
5. 并发：终止与 worker 完成同时到达 → 只落一个终态，无中间态。

### C4 引擎异常退出（4 条）

**出错的逻辑**：`src/runtime/agent.py:1914` 收到引擎退出码 -1 但 `_errs` 为空 → errors 文本 "engine exit_code=-1, errors: "（seq 11002/11315），或 result/errors 全空（seq 10656/11119，对应 t_8e2d712fae10、t_27d43b9492d7 等）。引擎进程异常终止时未留下任何错误信息，agent.py 原样透传空错误，排障无抓手。任务级另有 1 条 IAM 服务不可达 + 空输出（t_984cc8f，模型流异常），同属"引擎侧无声死亡"。

**修正后的逻辑**：agent.py 在引擎退出且无错误文本时，补齐上下文：抓取引擎最后 N 行日志、引擎名/版本、存活时长、最后心跳时间，写入 errors（如 "engine codex exited -1 after 754s, last log: ..."）；有条件的话保存 engine 侧 stderr 转储到日志归档路径。

**影响面评估**：agent.py 属 worker 端，13 台 worker 批量升级；纯诊断增强不改状态机，风险低。

**测试用例设计**：
1. 模拟引擎退出码 -1 且无 stderr → errors 含 "engine X exited -1 after Ns" 与最后日志摘要。
2. 引擎有 stderr → 保留原 errors 格式（前 3 条截断逻辑不变）。
3. 引擎日志为空 → errors 仍含引擎名与存活时长。
4. 日志抓取自身异常 → 不影响任务 fail 路径（降级为现格式）。

### C5 外层任务超时（2 条）

**出错的逻辑**：`src/runtime/agent.py:1902` —— 外层看门狗 `RUNTIME_TASK_TIMEOUT`（默认 3600s，agent.py:1860）到期调用 `client.fail(..., "task timed out after 3600s", exit_code=-1)`。seq 10688（grape-skill-generator）、10788（grape-vod-cli）各跑满 1 小时被外层杀。两例 1h 内未触发 300s 静默杀，说明期间有零星输出（如持续读文件），但整体卡在低效循环。`src/runtime/service.py:274-286` 已有 TASK_RECOVER_STALE_SECONDS 与该值的对齐告警（Issue #259）。

**修正后的逻辑**：平台行为正确（到点自杀防双跑）。改进方向是事前预警：dashboard.py:84 已有 22min stuck 预警口径，建议对 RUNNING 超 45min 的任务向事件订阅方发提醒；同时排查 grape-skill-generator 技能是否存在"读大文件循环"导致 1h 仍无进展的效率问题（属技能侧）。

**影响面评估**：预警为纯只读查询，无状态变更，风险低；技能侧优化另行立项。

**测试用例设计**：
1. 任务运行 3601s → fail，errors 含 "task timed out after 3600.0s"，exit_code=-1。
2. 运行 45min 未完成 → 预警查询能命中该事件。
3. RUNTIME_TASK_TIMEOUT 与 TASK_RECOVER_STALE_SECONDS 不等 → 启动期告警（既有 #259 逻辑回归）。
4. 任务 3599s 完成 → 不触发超时。

### C6 派发层判死（2 条事件 + 9 条任务行）

**出错的逻辑**：`src/runtime/service.py:113-133`（Issue #253 改动2）——绑定引擎全部物理不可达（离线/心跳过期/满载）→ 任务诚实失败，exit_code NULL；service.py:161（改动3）健康分不足等待 120s 未恢复同样判死。seq 11256（grape-dev/grape, skill 空）、11299（grape-admin, hermes 引擎不可达）。任务级 9 条同类（codex/hermes/opencode 各有）。pool 兜底路径覆盖，但 scene 绑定（pool_engine_ids 固定）路径不回退到公共池，绑定引擎一旦离线即整 scene 不可用。

**修正后的逻辑**：保留"宁失败不跑飞"原则，但增加**绑定引擎离线时的降级派发**：绑定引擎不可达且任务无强绑定要求时，允许回退公共池（需 scene 增加可降级标记）；同时把判死 errors 中的引擎名单、离线原因（心跳过期时长）写全，方便值班定位是哪台 114-126 掉线。

**影响面评估**：派发策略变更影响任务-引擎亲和性，需灰度；强绑定场景（验收类）必须保持不降级。服务端改动，无 worker 升级。

**测试用例设计**：
1. scene 绑定 2 台引擎全离线、scene 允许降级 → 回退公共池派发成功。
2. scene 标记强绑定 → 维持判死，errors 含引擎名单与心跳过期时长。
3. 健康分不足等待期内恢复 → 正常派发（120s 等待逻辑回归）。
4. 公共池也无可用引擎 → 判死，errors 指明两级都不可用。

### C7 引擎初始化超时（1 条）

**出错的逻辑**：seq 11213，errors "initialize failed: initialize timeout after 100s" —— 引擎（codex CLI）启动 100s 内未完成握手即判失败。偶发单例，多为引擎冷启动（首次拉起容器/CLI 自更新）超过 100s。

**修正后的逻辑**：初始化超时与任务执行超时分开统计；可将初始化超时任务自动重派一次（引擎二次启动通常命中缓存更快），重派仍失败才落 failed。100s 阈值可按引擎类型配置。

**影响面评估**：重派逻辑在 scheduler 层，需防重派风暴（限 1 次）；worker 端 executor 不动。

**测试用例设计**：
1. 引擎首次启动 120s 完成 → 初始化超时 → 自动重派 1 次 → 成功则事件 completed。
2. 重派仍初始化超时 → 落 failed，errors 保留两次超时记录。
3. 非 initialize 类失败 → 不触发该重派路径。

### C8 配置错误（1 条）

**出错的逻辑**：seq 10824（dhzss/bedcareproject，deploy-cli）—— "部署未执行：缺少华为云凭证"。该仓库未配置 OBS/部署所需凭证，SRE 如实失败。属用户侧配置缺失，非平台 bug。

**修正后的逻辑**：派发前置校验——deploy 类技能在派发前检查 repo 级凭证是否配置，缺失则直接以明确的"配置缺失"原因快速失败（不占用 worker 1 小时窗口），并在 Issue 评论中告知配置入口。

**影响面评估**：前置校验属服务端 scheduler 改动，影响小；需维护"技能→所需凭证"清单。

**测试用例设计**：
1. deploy 任务 + 无凭证 → 秒级失败，原因含"缺少华为云凭证"及配置指引。
2. 有凭证 → 正常派发执行。
3. 凭证过期 → 不在前置拦截范围（运行期失败原样上报）。

## 四、优先级排序（修好即清零、改动小优先）

| 优先级 | 类别 | 预期效果 | 改动量 |
|---|---|---|---|
| P0 | C3 手动终止口径（events.py 两行 status） | 事件失败 -4，统计即时修正 | 极小，纯服务端 |
| P0 | C8 deploy 前置凭证校验 | 配置类失败秒级反馈 | 小，服务端 |
| P1 | C2 静默超时心跳/阈值（executor 看门狗） | 最大头（11 条）大幅消化 | 中，13 台 worker 批量升级 |
| P1 | C4 引擎异常退出补诊断信息 | 不减失败但可定位 | 小-中，worker 升级 |
| P2 | C6 绑定引擎降级派发 | scene 绑定路径可用性提升 | 中，需灰度 |
| P2 | C7 初始化超时自动重派 | 偶发单例自愈 | 中，scheduler |
| P3 | C1 业务 blocked 分级 | 统计口径净化（非故障） | 中，涉及枚举与前端 |

## 五、一句话总结

32 条事件失败中：1/3 是长任务静默被 300s 看门狗误杀（executor.py:663，加心跳即可救）；1/8 是用户手动终止被 events.py:339 错记成 failed（改两行 status 即清零）；其余为业务验收未过（非故障）、引擎无声死亡、1h 外层超时、绑定引擎离线判死和缺凭证配置，各有明确单点修法。

---
*本报告由 grape-daily-failure-analysis 定时任务自动生成；只定位、不修复，等待用户指令。*
