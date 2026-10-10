# Grape 平台日失败统计分析（2026-10-10）

- 窗口：`created_at >= date_sub(now(), interval 1 day)`（2026-10-09 ~ 2026-10-10，分析时刻滑窗）
- 数据源：`grape_system_db.events` / `grape_system_db.agent_tasks`
- 结论口径：事件级失败 33 条、任务级失败 28 条，逐条核对归类，各类之和与总数对齐。

## 一、总体统计

| 维度 | 成功(completed) | 失败(failed) | running | cancelled | queued | 成功率 |
|---|---|---|---|---|---|---|
| 事件（events，360 条） | 323 | 33 | 3 | 1 | - | 90.5% |
| 任务（agent_tasks，230 条） | 195 | 28 | 3 | 3 | 1 | 85.5% |

## 二、归类汇总（事件级，33 条）

| 类别 | 数量 | 占比 | 事件 seq |
|---|---|---|---|
| A. 外层运行时超时（`task timed out after Ns`，exit=-1） | 5 | 15.2% | 9555, 9559, 9561, 9562, 9816 |
| B. 静默超时·SIGKILL（exit=-9，收尾窗口无输出） | 4 | 12.1% | 9533, 9787, 9791, 9795 |
| C. 静默超时·SIGTERM（exit=-1，result 带半截工作文本） | 8 | 24.2% | 9541, 9568, 9620, 9644, 9716, 9722, 9729, 9856 |
| D. 超时被引擎包装层吞细节（`engine exit_code=-1, errors:` 为空） | 5 | 15.2% | 9579, 9585, 9664, 9699, 9782 |
| E. 派发层判死（exit_code NULL：引擎不可达 / 健康分不足） | 5 | 15.2% | 9612, 9616, 9617, 9756, 9776 |
| F. 配置错误（exit=1：模型元数据缺失 / DEEPSEEK_API_KEY 缺失 / server error） | 3 | 9.1% | 9608, 9611, 9718 |
| G. 用户手动终止被记为 failed（口径问题，应为 cancelled） | 3 | 9.1% | 9530, 9588, 9755 |
| 合计 | 33 | 100% | - |

任务级（28 条）：派发层判死 11（不可达 10 + 健康分不足 1，exit NULL）；超时族 18（显式 timeout 2 + exit=-1 静默族 12 + exit=-9 共 4）；配置错误 1（exit=1，seq 9718）。11+18+1=30？不对——exit=-1 共 12 条中含 2 条显式 timeout，即超时族合计 12+4=16；16+11+1=28 ✓。

## 三、分类根因分析与设计方案

### 类别 A：外层 RUNTIME_TASK_TIMEOUT 到期（5 条）

**出错的逻辑**：worker 侧 `src/runtime/agent.py:1849` 读取 `RUNTIME_TASK_TIMEOUT`（默认 3600s，issue/vod 场景部署为 1200s），`asyncio.wait_for(_execute_task(...), timeout=task_timeout)` 到期后走 `client.fail(_tid, "task timed out after Ns", exit_code=-1)`（agent.py:1891）。本次 5 条集中在 `Lingxi-HandsOn/gallery` 的 issue/coding 任务（1200s 到期）与 `pvs-zombies-demo` 的 build-debug（3600s 到期，seq 9816）。根因一句话：**任务本身的工作量超出定死的超时阈值，到点被外层掐死**。

**修正后的逻辑**：
1. 按事件类型分档超时（issue 分析 1200s → 建议提到 2400s；含 npm ci/构建类任务走 3600s+），并允许 skill 级 override（任务 payload 里带 `timeout_hint`）。
2. 超时前预警：`src/observability/__init__.py:443` 已有 22min stuck 检测，把它接到超时前 10% 水位发告警事件，让人工/续跑机制有机会介入。
3. 断点续跑：超时 fail 时把 result 尾部 2000 字写入任务上下文（`.sha` 断点机制已有），下次重派可续。

**影响面评估**：仅改 worker 端 agent.py 与环境变量模板；需 13 台 worker（114-126）批量升级并滚动重启；`TASK_RECOVER_STALE_SECONDS >= RUNTIME_TASK_TIMEOUT` 前提（service.py:179-191 启动告警）需同步核对，双主 scp 同步注意两台主控配置一致；`exit_code=-1` 语义不变，status 枚举无需兼容处理。

**测试用例**：
1. 构造 sleep 1300s 的假任务，设 timeout=1200s，断言 fail 且 errors=`task timed out after 1200.0s`。
2. payload 带 `timeout_hint=3000`，断言实际生效超时为 3000s。
3. 超时后同任务重派，断言能从 `.sha` 断点继续而非从头执行。
4. `TASK_RECOVER_STALE_SECONDS < RUNTIME_TASK_TIMEOUT` 时启动，断言日志出现 Issue #259 告警。

### 类别 B+C：静默超时（共 12 条，B 4 条 + C 8 条）——本次最大头

**出错的逻辑**：引擎子进程长时间无输出（模型 API 挂起 / 长工具调用无流式事件），外层先 SIGTERM 整个进程组（`src/agent/executor.py:105-119`，3s 后 SIGKILL 兜底，agent.py:882-895 同逻辑）。收尾 grace 窗口有尾输出 → exit=-1 + result 带半截文本（C 类）；无输出 → exit=-9（B 类）。根因一句话：**引擎子进程"假活着"（无输出不退出），被看门狗静默击杀，但没有任何"为什么卡住"的痕迹留下来**。

**修正后的逻辑**：
1. 静默击杀时强制落盘诊断包：击杀前 `ps`+`py-spy dump`（或 /proc 栈）写入 agent_logs，把 -9/-1 的 result 统一补 `killed: no output for Ns` 后缀，杜绝空 result（9533 等 4 条 result 全空）。
2. 流式保活：`_execute_task` 内加应用层 keepalive——每 60s 无事件时向模型发一次轻量 ping/或记录心跳行，把"模型 API 挂起"和"本地工具卡死"区分开。
3. C 类的半截 result 不应丢弃：超时 fail 时把已有 `_text` 尾部并入 errors 字段，方便事后归类。

**影响面评估**：改 `src/agent/executor.py` 终止流程 + `src/runtime/agent.py` `_wrap` 收尾逻辑；worker 端 13 台批量升级；`py-spy` 需随部署包分发（新增依赖）；exit_code=-9 语义保留（服务端死 session 检测依赖连续负数 exit_code，勿改）；DB 无 schema 变更。

**测试用例**：
1. mock 引擎子进程 600s 不输出，断言被 SIGTERM→SIGKILL 且 result 含 `no output for` 诊断文本。
2. mock 子进程 SIGTERM 后 1s 内输出一行，断言 exit=-1 且 result 含该尾行。
3. 并发 5 任务同时触发静默击杀，断言诊断包互不串目录（按 tid 隔离）。
4. 断言击杀后 60s 内 `client.fail` 一定送达（当前 fail 在 timeout 外，网络异常时兜底）。

### 类别 D：`engine exit_code=-1, errors:` 为空（5 条）

**出错的逻辑**：引擎包装层（executor `_run_once` 的 done event）把子进程 exit_code 透传，但 errors 列表为空字符串——超时/击杀细节在 `src/agent/executor.py:439` 附近的兜底分支里被丢掉，上层只拿到光秃秃的 `engine exit_code=-1`。本质与 B/C 同源（超时族），**根因一句话：包装层吞了底层死因，只留下一个光杆 exit_code**。其中 seq 9664 是变体：worker 侧 exit_code=-1，但 result 为 `engine exit_code=1, errors:`——引擎侧退出码 1、错误文本同样为空，归入本类。

**修正后的逻辑**：executor 生成 done event 时，`exit_code != 0` 且 `errors` 为空则强制填入 `stderr` 尾部 500 字或 `"no diagnostic captured (killed at Ns)"`；禁止空 errors 的 fail 下发。

**影响面评估**：单文件 `src/agent/executor.py`，改动小；worker 升级即可；观测端（observability）按 errors 关键字统计的逻辑需加新关键字 `no diagnostic`。

**测试用例**：
1. mock 子进程 exit=-1 无 stderr，断言 errors 非空。
2. stderr 有内容时，断言 errors 携带尾部 500 字。
3. exit=0 的正常路径断言不受影响（不注入噪音）。

### 类别 E：派发层判死——引擎不可达 / 健康分不足（事件 5 条，任务 11 条）

**出错的逻辑**：任务绑定场景引擎（hermes×vod、opencode×issue 等），派发时发现绑定引擎全部离线/满载/无标签 → 任务停止派发直接 failed（exit_code NULL、completed_at 空：事件 9612/9616/9617/9756/9776）。`src/runtime/service.py:1015-1017、1285-1287` 对 **pool_engine_ids** 路径有兜底（全部不可达时清空池放行任意引擎认领），但 **scene 绑定路径不经过该兜底**，绑死即判死。seq 9858 是变体：3 台 opencode 在线但健康分不足，等 120s 未恢复停止派发。根因一句话：**场景绑定的引擎掉线时没有降级放行，任务直接被判死刑**。

**修正后的逻辑**：
1. 把 pool 的"全不可达→清空绑定放行"兜底同样接到 scene 绑定路径（service.py 派发入口统一走一个 `_resolve_engines_with_fallback`）。
2. 健康分不足改为**延迟重派**而非 fail：等 120s 未恢复先 requeue（带冷却，复用 agent.py:1820 的 60s requeue cooldown 模式），3 轮后才 fail。
3. 判死事件落 `engine_unreachable` 明细：哪些引擎、什么状态、标签是什么，当前只有一句汇总。

**影响面评估**：改 `src/runtime/service.py` 派发主循环，属主控侧改动（双主 scp 同步两台）；`status` 枚举不变，但事件 result 语义变化，前端失败详情页按文本渲染无需兼容；注意别与 Issue #259 的 running 回收逻辑互相触发（requeue 的任务不能同时被 stale 回收，需在回收查询排除 queued）。

**测试用例**：
1. scene 绑定唯一引擎置 offline，断言任务被放行给其他在线引擎而非 failed。
2. 3 台引擎健康分低于阈值，断言任务 requeue 而非 120s 后 fail。
3. requeue 3 轠后仍无可用引擎，断言最终 fail 且 errors 含引擎明细。
4. 双主同时派发同一 requeue 任务，断言服务端 claim 互斥（不双跑）。

### 类别 F：配置错误（3 条）

**出错的逻辑**：seq 9718（vod-cli-sync-code）`exit_code=1`，errors 明确：`Model metadata for deepseek-v4-flash not found` + `Missing environment variable: DEEPSEEK_API_KEY`（出现两次）——某台 worker 的 vod 引擎环境缺 API key 且模型元数据未注册。seq 9608/9611（issue-cli，`Unexpected server error`）为 grape CLI 服务端 5xx。根因一句话：**个别 worker 环境变量/模型注册表没配齐，任务一落上去就崩**。

**修正后的逻辑**：
1. worker 注册心跳时上报 `env 缺失清单 + 已注册模型列表`（health payload 扩展），健康分扣分项增加"配置缺失"，让派发层提前绕开坏 worker（与类别 E 的健康分机制打通）。
2. `deepseek-v4-flash` 元数据补录到模型注册表；DEEPSEEK_API_KEY 补注入到 vod 引擎环境模板。
3. `Unexpected server error` 需在 issue-cli 侧带上下文重试一次（当前 9608 三秒即死，completed_at=created+3s）。

**影响面评估**：环境变量修复需 13 台 worker 逐台核对（scp 同步 .env 模板）；health payload 扩展是双主侧 DB 字段/接口兼容变更（新增字段向后兼容）；模型元数据是配置文件变更，风险最低。

**测试用例**：
1. worker 上报缺 DEEPSEEK_API_KEY，断言健康分下降且派发层跳过该 worker。
2. 补齐 key 后下一轮心跳，断言健康分恢复。
3. mock CLI 返回 500，断言重试一次后才 fail。
4. 请求 `deepseek-v4-flash` 元数据，断言不再 fallback 告警。

### 类别 G：用户手动终止记为 failed（3 条）

**出错的逻辑**：`src/api/routes/events.py:285-299` 终止路径调 `update_event_complete(seq, "failed", result="用户手动终止")`——**写死 failed**；而 autopilot 路径 `src/runtime/scheduler.py:637-640` 已正确同步为 cancelled（"用户杀死：原样同步 cancelled，不翻译成 failed"）。两处口径不一致。根因一句话：**event 终止接口把用户主动杀的任务硬写成 failed，统计被污染**。

**修正后的逻辑**：events.py:292 的 `"failed"` 改为 `"cancelled"`，result 保留"用户手动终止"；`fail_task(task_id, ...)`（events.py:299 worker 分支）同步改 `cancel_task`。历史数据不回刷，报表从当日起新口径。

**影响面评估**：两行改动，风险最低；前端已支持 cancelled 展示（weekly-06 的 i18n/终止图标改动刚合并，0e7b11c70）；events 表 status 枚举已含 cancelled，无 schema 变更；注意 `finalize_event_archive` 同步传对状态。

**测试用例**：
1. 对 running 事件调终止接口，断言 events.status=cancelled 且 result=用户手动终止。
2. worker 模式终止，断言 agent_tasks 同步为 cancelled。
3. 终止已 completed 的事件，断言幂等不报错。
4. 当日失败统计 SQL 断言不含"用户手动终止"行。

## 四、优先级排序（修好即清零、改动小优先）

| 优先级 | 类别 | 理由 |
|---|---|---|
| P0 | G 手动终止口径 | 两行改动，立即修正失败率统计（-3 条/日） |
| P0 | F 配置错误 | 补 key+元数据即清零 seq 9718 类；CLI 5xx 加重试 |
| P1 | E 派发层判死 | scene 绑定接入 pool 兜底，清零 5 条事件/11 条任务，主控侧单点改动 |
| P1 | D 空 errors 包装层 | 单文件改动，先让超时族可诊断 |
| P2 | B/C 静默超时 13 条 | 工作量最大（诊断包+keepalive+续跑），先靠 D 的诊断信息定位真凶再治 |
| P2 | A 外层超时阈值 | 分档超时+timeout_hint，配合 B/C 的续跑一起上 |

## 五、一句话总结（大白话版）

今天 33 条失败里：12 条是引擎"发呆"被看门狗无声掐死（还不知为什么发呆）、5 条是活太多到点被掐、5 条是绑定的引擎掉线任务被判死刑、5 条死因被包装层吞了、3 条是配置没配齐、3 条其实是用户自己杀的却被算成失败。先把"自己杀的算失败"和"补配置"这两件小事修了，再给绑定引擎加降级放行，最后给静默击杀补上"临终遗言"诊断，失败就能砍掉一大半。

## 六、18:10 定时复核记录

- 复核窗口（2026-10-10 18:10 快照）：事件 356 条（completed 318 / failed 33 / running 4 / cancelled 1），任务 233 条（completed 196 / failed 28 / cancelled 4 / running 4 / queued 1），失败数与本文口径一致（33/28）。
- 复核中发现原文把 seq 9664 误归入 C 类（SIGTERM 半截文本），实际其 result 为 `engine exit_code=1, errors:`（引擎退出码 1、错误为空），已改归 D 类，C 9→8、D 4→5，总数不变。
- 另注：seq 9795 exit=-9 但 result 带降级处理尾文本（SIGTERM 前已有 collected_text），仍归 B 类（-9 击杀），仅作口径备注。
- 期间新增事件 9857/9858（本分析任务自身链路）：9857 引擎静默被杀后由 9858 重试成功，任务级失败 +1 已含在 28 条内。
