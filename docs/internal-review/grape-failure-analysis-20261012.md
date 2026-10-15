# Grape 平台日失败统计分析（2026-10-12）

- 统计窗口：`created_at >= date_sub(now(), interval 1 day)`（2026-10-11 ~ 2026-10-12）
- 数据源：grape_system_db.events / agent_tasks

## 1. 总体统计

| 维度 | 成功 | 失败 | 其他（running/cancelled/queued） | 成功率 |
|---|---|---|---|---|
| 事件级（events） | 482 | 27 | 2（running） | 94.3% |
| 任务级（agent_tasks） | 487 | 50 | 2（running 1 + cancelled 1） | 90.4% |

> 任务级失败数（50）大于事件级（27）：一个事件会跨多次任务重试（引擎不可达后重派、超时后重跑），且部分失败任务挂在窗口外的历史事件上（seq 343-353、8216、9877、10141、10176）。

## 2. 归类汇总（事件级，共 27 条，各类之和 = 27 ✓）

| # | 类别 | 数量 | 占比 | 事件 seq |
|---|---|---|---|---|
| A | 静默超时被杀（exit -9，"no output for Ns, killed"） | 5 | 18.5% | 10162, 10185, 10285, 10544, 10617 |
| B | 超时 SIGTERM 收尾有输出（exit -1，result 半截工作文本） | 7 | 25.9% | 10178, 10323, 10381, 10390, 10582, 10587, 10593 |
| C | 外层 RUNTIME_TASK_TIMEOUT（exit -1，"engine exit_code=-1, errors:"） | 3 | 11.1% | 10457, 10528, 10590 |
| D | 用户手动终止被记为 failed（口径问题） | 1 | 3.7% | 10172 |
| E | 派发层判死：引擎不可达 / 健康分不足（exit NULL，result 空） | 7 | 25.9% | 10163, 10186, 10188, 10226, 10319, 10435, 10440 |
| F | 场景绑定引擎全部离线（exit 1，event_pipeline 直接 fail） | 1 | 3.7% | 10231 |
| G | 配置错误：缺少华为云 AK/SK（exit 1） | 1 | 3.7% | 10337 |
| H | 引擎异常退出 exit 1 且输出截断（errors 为空） | 2 | 7.4% | 10353, 10392 |

任务级 50 条拆分：引擎不可达 13、健康分不足 3、场景引擎离线 1、recover-orphans attempts cap 2（t_717ed145589f / t_9945550c3cab）、手动终止 1（t_b997a6dfb04c）、engine exit_code=-1 3、result/errors 双空 3（t_4afe251715a3 / t_7f166983c52c / t_8b647543cb1e）、超时半截文本 24。合计 50 ✓。

## 3. 各类分析

### A/B/C：超时三兄弟（合计 15 条，55.6%，最大头）

**出错的逻辑**：A 类是 `src/agent/executor.py:663-700` 静默看门狗（`_silent_window` 无输出）触发 SIGTERM，grace tail 为空则记 `exit_code=-9`；B 类同路径但 SIGTERM 后 grace tail 有半截文本，记 `-1`；C 类是 `src/runtime/agent.py:1892-1902` 外层 `RUNTIME_TASK_TIMEOUT` 到期，或 agent.py:1914 透传 `engine exit_code=-1, errors:`。典型场景：coding/issue agent 执行 20-60 分钟长任务（浏览器验收、bun 依赖安装、大仓库探索），单次产出间隔超过静默窗口，或总时长超 RUNTIME_TASK_TIMEOUT。

**修正后的逻辑**：
1. 静默窗口判定改为"无结构化心跳"而非"无 stdout"：executor 层在子进程无输出时周期性写心跳事件；长命令（npm/bun install、浏览器测试）按命令类型放宽 `_silent_window` 2-4 倍。
2. RUNTIME_TASK_TIMEOUT 按场景/技能分档（implementation/sre 类长任务 2-4×默认值），不再全局一刀切。
3. B/C 类失败时把已完成的部分工作快照（git worktree diff、todo 清单）写入 result，供重试续跑。

**影响面评估**：executor.py 是 worker 端核心路径，13 台内部 worker（114-126）需批量升级；双主 scp 同步窗口内行为不一致，需先灰度 1-2 台观察一周；exit_code 枚举不变（-1/-9），前端无需兼容改动；心跳事件小幅增加 events 写入量（估算 <5%）。

**测试用例设计**：
1. 子进程 sleep 超静默窗口+60s 但周期性输出心跳 → 断言不被 -9 杀死。
2. 子进程完全无输出超静默窗口 → 断言 10s 后被 SIGTERM、grace tail 为空记 -9。
3. 任务总时长超场景化 RUNTIME_TASK_TIMEOUT → 断言 fail 消息含 "task timed out after Ns" 且 exit_code=-1。
4. 长任务超时后 result 包含已完成步骤快照字段。
5. 旧 worker（无心跳）+ 新 master 混跑 → 断言超时判定回退到纯 stdout 口径不误杀。

### E：派发层判死——引擎不可达 / 健康分不足（7 条）

**出错的逻辑**：`src/runtime/service.py:132-133`（"绑定的 N 台引擎…全部不可达…停止派发"）与 service.py:180-181（"在线但健康分不足，等待 120s 未恢复"）。scene 绑定单一引擎（opencode/codex/hermes），该引擎离线或健康分掉线 120s 后任务直接判死；全局引擎池有兜底，但 scene 绑定路径不覆盖。

**修正后的逻辑**：scene 绑定引擎不可达时降级到全局池同标签引擎（先同 agent_type 标签，再任意健康引擎），整池无可用才 fail；"等待 120s" 改指数退避（120s→300s→600s），期间任务保持 queued。

**影响面评估**：改 service.py 派发决策，master 端改动，无 worker 升级负担；降级可能违反部分 scene 的绑定意图（专用机器），需 scene 配置加 `allow_pool_fallback` 开关，默认关、灰度开。status 枚举不变。

**测试用例设计**：
1. scene 绑定 1 台离线引擎 + 池内同标签健康引擎 + fallback 开 → 断言派发到池引擎并成功。
2. fallback 关 → 断言 fail 且 reason 与现网一致。
3. 健康分不足 → 断言三次退避后再判死。
4. 整池不可达 → 断言 fail 消息列出全部引擎名。
5. 引擎在退避期间恢复 → 断言任务继续派发而非 fail。

### D：手动终止口径（1 条，seq 10172）

**出错的逻辑**：`src/api/routes/events.py:300-325`——worker 路径下手动终止调用 `update_event_complete(seq, "failed", result="用户手动终止")`，把主动取消写成 failed；task 侧 `cancel_task` 却标 cancelled，事件与任务状态不一致。

**修正后的逻辑**：events.py 中事件终态由 `"failed"` 改为 `"cancelled"`；direct 模式 `fail_task` 分支改为 `cancel_task`；scheduler.py:638/775 已是 cancel 语义无需改。

**影响面评估**：改动极小（约 2 行）；统计口径会把这批从"失败"移到"取消"，日失败率数字下降，需在周报注明口径切换日；DB 无 schema 变更。

**测试用例设计**：
1. worker 模式手动终止 → 断言 events.status='cancelled' 且 agent_tasks.status='cancelled'。
2. direct 模式手动终止 → 同上。
3. 终止不存在 task 的事件 → 断言 404 且不落脏状态。
4. 统计接口按 status 分组 → 断言 cancelled 计数包含手动终止。

### F：场景绑定引擎全部离线（1 条，seq 10231）

**出错的逻辑**：`src/api/event_pipeline.py:1793-1797`——事件入队前发现 scene（c495ff46-…）绑定的 sre 引擎全部离线，直接 `_fail_event(seq, "场景绑定的引擎全部离线…")`，exit_code=1。与 E 类同根：绑定路径无兜底，且入队阶段就判死不给重试。

**修正后的逻辑**：与 E 类共享 pool fallback；`_fail_event` 前先把事件置 queued 挂起 10 分钟延迟重评估，引擎恢复则正常派发。

**影响面评估**：event_pipeline 是入队主路径，需回归 PR merged/release 事件流；测试用例见 E 类，另加"引擎 10 分钟内恢复 → 事件正常完成"。

### G：配置错误——缺 AK/SK（1 条，seq 10337）

**出错的逻辑**：任务行为本身正确（前置检查发现 `HUAWEICLOUD_SDK_AK/SK` 为空即停止，t_7d916838d5f0 的 actions 有完整决策链）。根因是用户/仓库级凭证未配置，且"尝试 Issue 评论告知（API 404 失败）"——评论回帖也失败了。

**修正后的逻辑**：sre 部署类任务入队前增加凭证预检（读 repo/global 凭证配置），缺失直接拒绝入队并给用户明确的配置引导链接，而非让 agent 跑到一半才发现；评论 404 需排查 token 权限（repo token 是否有 issue 评论 scope）。

**影响面评估**：入队预检是 master 端小改动；无 worker 升级；需要在任务创建 API 响应里新增一个明确错误码（如 credentials_missing），前端展示引导文案。

**测试用例设计**：
1. 无 AK/SK 配置的仓库发起 sre 部署 → 断言入队即拒绝，错误码 credentials_missing。
2. 配置凭证后重发 → 断言正常入队执行。
3. 评论回帖 404 → 断言降级为任务 result 内提示，任务不因此 fail。
4. 凭证配置了但无效（过期）→ 断言预检通过、执行期报具体 IAM 错误。

### H：引擎异常退出 exit 1 且输出截断（2 条，seq 10353、10392）

**出错的逻辑**：result 以半截句子结尾（"已有" / "ngin"），exit_code=1，errors 为空。engine 进程非正常结束（worker 侧 crash/被外部 kill），executor 把 returncode 透传（executor.py:795），没有捕获任何错误信息。同时 10353 的任务链里还有一次"引擎不可达"重试（t_993c0d66bba6），说明该引擎当时状态已经不稳。

**修正后的逻辑**：executor 在 `proc.returncode != 0` 且 stderr/errors 为空时，补充采集引擎侧日志尾部（agent_logs 最后 N 行）和进程资源快照（OOM killer dmesg 检查）写入 errors；对 exit 1 且无错误信息的任务自动重试一次（幂等由 worktree 保证）。

**影响面评估**：executor.py worker 端改动，随 A/B/C 一起批量升级即可，不单独发版；自动重试会增加约 2/27 ≈ 7% 的任务量，可接受。

**测试用例设计**：
1. 模拟 engine 进程被 kill -9 → 断言 errors 含日志尾部与 "killed" 信息。
2. 模拟 exit 1 无 stderr → 断言自动重试一次，成功则事件 completed。
3. 重试仍失败 → 断言事件 failed 且两次尝试都记录在任务链。
4. OOM 场景 → 断言 errors 提示内存不足。

## 4. 附注：recover-orphans attempts cap（任务级 2 条）

t_717ed145589f（seq 10141）、t_9945550c3cab（seq 10271）报 "recover-orphans attempts cap reached (possible self-restart loop)"——孤儿任务恢复器达到尝试上限，疑似自重启循环。建议调大 cap 并对同一 event_seq 的重复恢复加去重冷却，值得明天单独观察一天再定位。

## 5. 优先级排序（修好即清零、改动小优先）

1. **D 手动终止口径**（2 行改动，立即清零 1 条/天 + 消除事件/任务状态不一致）
2. **F/E 引擎绑定兜底**（master 端改动，清零 8 条/天，占比 30%）
3. **G 凭证预检**（小改动，清零 1 条/天并改善用户体验）
4. **A/B/C 超时分档+心跳**（收益最大 15 条/天，但涉及 13 台 worker 批量升级，需灰度）
5. **H 引擎异常退出信息补全**（随 A/B/C 同批发版）
