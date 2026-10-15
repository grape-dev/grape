# Grape 平台日失败统计分析（2026-10-10 · 23:00 收盘版）

- 窗口（固定，非滑窗）：`2026-10-09 23:00:00 → 2026-10-10 23:00:00`（北京时间）。`events.created_at` 存北京时间；`agent_tasks.created_at` 存 UTC ISO 串，已按 `+8` 换算对齐（UTC `2026-10-09T15:00 → 2026-10-10T15:01`，字符串序比较）。
- 数据源：`grape_system_db.events` / `grape_system_db.agent_tasks`，失败明细**全量**拉取逐条核对（未抽样）。
- 本文为当日第三次巡检（日间两版见 3a83c4d43 / 80fe84be4，均基于滑窗）。收盘版按固定窗口全量重算，**取代前两版口径**；并依据任务级明细把 seq 9664 从 D 类改归 A 类（见 §六）。
- 结论口径：事件级失败 22 条、任务级失败 38 条，逐条归类后各类之和与总数对齐。

## 一、总体统计

| 维度 | completed | failed | cancelled | running | 总数 | 成功率（已终态） |
|---|---|---|---|---|---|---|
| 事件（events） | 280 | 22 | 1 | 1 | 304 | 92.4%（280/303） |
| 任务（agent_tasks） | 286 | 38 | 4 | 2 | 330 | 87.2%（286/328） |

口径说明：
- 任务级 38 > 事件级 22，属正常：① 同一事件可多次派发（重试副本也算任务行，如 9664 两派、9722 两派）；② 8 条绑定引擎判死任务的 `event_seq` 落在 317-324 序号段（issue 流水线内部序号，不对应 events.seq）；③ 9755 的任务级已被正确记为 cancelled，仅事件级误记 failed（G 类，事件级独有）。
- 部分任务级失败对应的事件终态是 completed（重试后成功，如 9857 失败→9858 重试成功、9776→9777、9907/9908、9752）。

## 二、归类汇总

### 事件级（22 条）

| 类别 | 数量 | 占比 | 事件 seq |
|---|---|---|---|
| A. 外层 RUNTIME_TASK_TIMEOUT 到期（`task timed out after Ns`，exit=-1） | 2 | 9.1% | 9664, 9816 |
| B. 静默超时·SIGKILL（exit=-9，收尾窗口无输出） | 3 | 13.6% | 9787, 9791, 9795 |
| C. 静默超时·SIGTERM（exit=-1，result 带半截工作文本） | 6 | 27.3% | 9620, 9644, 9716, 9722, 9729, 9856 |
| D. 引擎包装层吞死因（`engine exit_code=-1, errors:` 为空） | 2 | 9.1% | 9699, 9782 |
| E. 派发层判死（exit_code NULL：绑定引擎不可达 / 健康分不足） | 6 | 27.3% | 9616, 9617, 9744, 9756, 9776, 9917 |
| F. 配置错误（exit=1：模型元数据缺失 / DEEPSEEK_API_KEY 缺失） | 1 | 4.5% | 9718 |
| G. 用户手动终止被记为 failed（口径问题，应为 cancelled） | 1 | 4.5% | 9755 |
| H. 云凭证未配置（exit=1：华为云 AK/SK 缺失，本地冒烟已通过） | 1 | 4.5% | 9877 |
| 合计 | 22 | 100% | - |

### 任务级（38 条）

| 类别 | 数量 | 占比 | 任务 id（节选 event_seq） |
|---|---|---|---|
| E. 派发层判死 | 18 | 47.4% | 不可达 15（t_fce22c216b34@317, t_3f3509528f2a@318, t_5dfb15c210b9@319, t_133d36c39a4e@320, t_acc923c3ea53@323, t_0c14a6d1e302@324, t_650349cd16a6@9752, t_c9a4dbcaf0f7@9756, t_517457cbcee5@9776, t_02daa6df3803@9777, t_d032ffebedb8@9744, t_85f22b711fb6@9907, t_0d58d541de7f@9908, t_de72a18a7056@9913, t_1504f807a567@9917）；健康分不足 3（t_7de1e7967269@9616, t_74cd6e2cc488@9617, t_4e2eb0e12949@9858——3 台 opencode 在线低分，事件 9858 已由后续重试成功） |
| C. 静默超时·SIGTERM 半截文本 | 9 | 23.7% | t_e688b4088428@9620, t_4ee29b9edfa2@9644, t_96ea55a85f54@9716, t_67dd01f7a36a@9722, t_1fe142a88a3e@9722, t_0c49a5013531@9729, t_ca4862eb8d0a@9856, t_e95618f38159@9830, t_d62e4a60bb8e@321 |
| B. 静默超时·SIGKILL | 4 | 10.5% | t_5cd9a1aa359a@9787, t_fe39ba46950a@9791, t_cf27a8c1abbf@9795, t_dc6c88f14d82@9857 |
| D. 包装层吞死因 | 3 | 7.9% | t_1f57d1ab3f55@9664, t_97ea95c64594@9699, t_0e6c4a88d65f@9782 |
| A. 外层 RUNTIME_TASK_TIMEOUT | 2 | 5.3% | t_9dd52344a0e3@9664（1200s）, t_ad1e9460d6ae@9816（3600s） |
| F. 配置错误 | 1 | 2.6% | t_b28d88b81eeb@9718 |
| H. 云凭证未配置 | 1 | 2.6% | t_1e1588c2759a@9877 |
| 合计 | 38 | 100% | - |

（注：t_d62e4a60bb8e@321 归 C 类——它是本日失败分析任务自身早前一次被静默击杀的执行，result 为分析过程文本。事件级 E 的 6 条 = 不可达 4（9744/9756/9776/9917）+ 健康分不足 2（9616/9617）。）

## 三、分类根因分析与设计方案

### 类别 A：外层 RUNTIME_TASK_TIMEOUT 到期（事件 2 条 / 任务 2 条）

**出错的逻辑**：worker 侧 `src/runtime/agent.py:1881` 读取 `RUNTIME_TASK_TIMEOUT`（默认 3600s，issue 场景 1200s），`asyncio.wait_for` 到期后走 `client.fail(_tid, "task timed out after Ns", exit_code=-1)`（agent.py:1891）。本次：seq 9816（`pvs-zombies-demo` build-debug，3600s 到期）；seq 9664（github `huaweicloud-mate` issue_comment，任务级 `t_9dd52344a0e3` 明细为 `task timed out after 1200.0s`，事件侧 result 被引擎包装成 `engine exit_code=1, errors:` 空串——这是它先前被误归 D 类的原因）。根因一句话：**任务工作量超出定死的超时阈值，到点被外层掐死**。

**修正后的逻辑**：
1. 按事件类型分档超时（issue 分析 1200s → 2400s；构建/部署类 3600s+），允许 skill 级 override（payload 带 `timeout_hint`）。
2. 超时前预警：把 `src/observability/__init__.py` 的 stuck 检测接到超时前 10% 水位发告警事件。
3. 断点续跑：超时 fail 时把 result 尾部 2000 字写入 `.sha` 断点，重派可续。

**影响面评估**：改 worker 端 agent.py 与环境变量模板，13 台 worker（114-126）批量升级滚动重启；核对 `TASK_RECOVER_STALE_SECONDS >= RUNTIME_TASK_TIMEOUT`（启动告警）；双主 scp 同步配置一致；exit_code=-1 语义不变，status 枚举无兼容问题。

**测试用例**：
1. 构造 sleep 1300s 假任务、timeout=1200s，断言 fail 且 errors=`task timed out after 1200.0s`。
2. payload 带 `timeout_hint=3000`，断言生效超时 3000s。
3. 超时后重派，断言从 `.sha` 断点续跑。
4. `TASK_RECOVER_STALE_SECONDS < RUNTIME_TASK_TIMEOUT` 启动，断言告警日志。

### 类别 B+C：静默超时（事件 9 条 = B 3 + C 6；任务 13 条 = B 4 + C 9）——最大头

**出错的逻辑**：引擎子进程长时间无输出（模型 API 挂起 / 长工具调用无流式事件），`src/agent/executor.py:650-664` 静默看门狗到期：先 SIGTERM（grace 10s），收尾窗口（executor.py:668-681）读尾输出——有尾输出 → exit=-1 + result 半截文本（C）；无输出 → exit=-9（B，executor.py:694-700 `"no output for Ns, killed"`）。本次新增证据：seq 9716 的 result 明确记载"write_file 工具结果中混入伪造系统消息/文件被写坏"——C 类半截文本本身就是宝贵的临终线索，当前只留在 result 里没有进诊断通道。根因一句话：**引擎子进程"假活着"被看门狗无声击杀，且没留下"为什么卡住"的痕迹**。

**修正后的逻辑**：
1. 击杀前落诊断包：`ps` + `py-spy dump`（或 /proc 栈）写入 agent_logs；-9/-1 的 result 统一补 `killed: no output for Ns` 后缀，杜绝空 result。
2. 流式保活：每 60s 无事件时记录心跳行/轻量 ping，区分"模型 API 挂起"与"本地工具卡死"。
3. C 类半截 result 尾部并入 errors 字段，方便事后归类。

**影响面评估**：改 `src/agent/executor.py` 终止流程 + `src/runtime/agent.py` 收尾；worker 13 台批量升级；`py-spy` 随部署包分发（新增依赖）；exit_code=-9 语义保留（服务端死 session 检测依赖连续负数 exit_code）；无 schema 变更。

**测试用例**：
1. mock 引擎 600s 不输出，断言被 SIGTERM→SIGKILL 且 result 含 `no output for` 诊断文本。
2. mock 子进程 SIGTERM 后 1s 输出一行，断言 exit=-1 且 result 含尾行。
3. 并发 5 任务同时触发静默击杀，断言诊断包按 tid 隔离不串目录。
4. 断言击杀后 60s 内 `client.fail` 一定送达。

### 类别 D：`engine exit_code=-1, errors:` 为空（事件 2 条 / 任务 3 条）

**出错的逻辑**：引擎包装层（executor `_run_once` done event）透传子进程 exit_code 但 errors 为空——超时/击杀细节在 `src/agent/executor.py:439` 附近兜底分支被丢弃。seq 9699（github `huaweicloud/huaweicloud-devkit` PR 审查）、9782（qa 网站测试）。seq 9664 的第二派任务 `t_1f57d1ab3f55` 同样呈现此形态（第一派已证实是 1200s 超时，重派后包装层又把死因吞了）。根因一句话：**包装层吞了底层死因，只留一个光杆 exit_code**。

**修正后的逻辑**：executor 生成 done event 时，`exit_code != 0` 且 `errors` 为空 → 强制填入 stderr 尾部 500 字或 `"no diagnostic captured"`；禁止空 errors 的 fail 下发。

**影响面评估**：单文件 executor.py，改动小，worker 升级即生效；observability 关键字统计需加 `no diagnostic`。

**测试用例**：
1. mock 子进程 exit=-1 无 stderr，断言 errors 非空。
2. stderr 有内容，断言 errors 携带尾部 500 字。
3. exit=0 正常路径断言不注入噪音。

### 类别 E：派发层判死——引擎不可达 / 健康分不足（事件 6 条 / 任务 18 条）——任务级最大头

**出错的逻辑**：任务绑定场景引擎（hermes×vod、opencode×issue、codex×coding/qa），派发时绑定引擎全部离线/满载/无标签 → `_fail_task_pool_unreachable`（`src/runtime/service.py:112-157`，Issue #253 改动2）直接置 failed、exit_code NULL、completed_at 空（9616/9617/9744/9756/9776/9917 及 317-324 段 6 条 + 9752/9777/9907/9908/9913 等）；物理在线但健康分不足 → `_fail_task_pool_unhealthy`（service.py:160-189，Issue #253 改动3）等 120s 未恢复即 fail（9616/9617 的 2 台 opencode+hermes、9858 曾同型）。pool 路径在 service.py 有"全不可达→清空池放行"兜底，但 **scene 绑定路径不经过该兜底**，绑死即判死。根因一句话：**场景绑定的引擎掉线/低分时没有降级放行，任务直接被判死刑**。

**修正后的逻辑**：
1. pool 的兜底统一接到 scene 绑定路径（派发入口收敛为 `_resolve_engines_with_fallback`）。
2. 健康分不足改延迟 requeue（带冷却，复用 agent.py 的 60s requeue cooldown 模式），3 轮后才 fail。
3. 判死事件落 `engine_unreachable` 明细（哪些引擎、什么状态、什么标签）。

**影响面评估**：改 `src/runtime/service.py` 派发主循环，主控侧（双主 scp 同步两台）；status 枚举不变、事件 result 语义变化对前端纯文本渲染无兼容问题；requeue 任务需在 stale 回收查询中排除，防与 Issue #259 running 回收互踩。

**测试用例**：
1. scene 绑定唯一引擎置 offline，断言任务放行给其他在线引擎而非 failed。
2. 3 台引擎健康分低于阈值，断言 requeue 而非 120s 后 fail。
3. requeue 3 轮仍无引擎，断言最终 fail 且 errors 含引擎明细。
4. 双主同时派发同一 requeue 任务，断言 claim 互斥不双跑。

### 类别 F：配置错误（事件 1 条 / 任务 1 条）

**出错的逻辑**：seq 9718（vod-cli-sync-code）exit_code=1，errors：`Model metadata for deepseek-v4-flash not found` + `Missing environment variable: DEEPSEEK_API_KEY`（出现两次）——某台 worker 的 vod 引擎环境缺 API key 且模型元数据未注册。根因一句话：**个别 worker 环境变量/模型注册表没配齐，任务一落上去就崩**。

**修正后的逻辑**：worker 心跳上报 env 缺失清单 + 已注册模型列表（health payload 扩展），健康分扣分项加"配置缺失"提前绕开坏 worker；补录 `deepseek-v4-flash` 元数据、补注入 DEEPSEEK_API_KEY。

**影响面评估**：13 台 worker 逐台核对 .env 模板（scp 同步）；health payload 扩展为向后兼容新增字段；模型元数据配置变更风险最低。

**测试用例**：
1. worker 上报缺 DEEPSEEK_API_KEY，断言健康分下降且派发跳过该 worker。
2. 补 key 后下轮心跳，断言健康分恢复。
3. 请求 `deepseek-v4-flash` 元数据，断言不再 fallback 告警。

### 类别 G：用户手动终止记为 failed（事件 1 条，任务级已正确 cancelled）

**出错的逻辑**：`src/api/routes/events.py:285-299` 终止路径调 `update_event_complete(seq, "failed", result="用户手动终止")` 写死 failed；autopilot 路径 `src/runtime/scheduler.py:637-640` 已正确同步 cancelled。本次 9755 任务级 t_68fe7a0c00bd=cancelled、事件级=failed，两处口径不一致实锤。根因一句话：**event 终止接口把用户主动杀的任务硬写成 failed，统计被污染**。

**修正后的逻辑**：events.py:292 的 `"failed"` 改 `"cancelled"`（result 保留"用户手动终止"）；worker 分支 `fail_task` 同步改 `cancel_task`。历史不回刷，报表从当日起新口径。

**影响面评估**：两行改动风险最低；events.status 枚举已含 cancelled，前端已支持 cancelled 展示；注意 `finalize_event_archive` 状态同步传对。

**测试用例**：
1. 对 running 事件调终止接口，断言 events.status=cancelled 且 result=用户手动终止。
2. worker 模式终止，断言 agent_tasks 同步 cancelled。
3. 终止已 completed 事件，断言幂等不报错。
4. 当日失败统计 SQL 断言不含"用户手动终止"行。

### 类别 H：云凭证未配置（事件 1 条 / 任务 1 条，收盘窗口新增）

**出错的逻辑**：seq 9877（sre domain-deploy，`jiuwenswarm-dev666666`）exit_code=1，result 自述"部署冒烟通过（前端构建✅ 后端构建✅ 本地HTTP 200✅）；云上线阻塞——华为云 AK/SK 未配置，无法执行 ECS 部署（A5）"。任务本身工作已完成，只因用户侧云凭证缺失被记为 failed——既污染失败统计，也丢失了"实际是 blocked"的语义。根因一句话：**外部凭证没配，任务被记成失败而不是明确的"被阻塞"**。

**修正后的逻辑**：
1. 新增/复用 blocked 语义：依赖外部凭证且缺失时，任务终态记 blocked（或 completed+附注），errors 明确列出缺哪个凭证、去哪里配。
2. 派发前依赖预检：`src/runtime/dependency_check.py` 的 `validate_task_dependencies` 增加"部署类 skill 需云凭证"检查项，缺凭证时提前 fail-fast 并给出配置指引（而不是干完本地活才失败）。
3. 事件详情透传配置入口（前端 settings 云凭证页链接）。

**影响面评估**：`validate_task_dependencies` 当前无覆盖测试（见项目知识 blast radius 标注），改动需补测试；blocked 若作为新 status 枚举需评估 events/agent_tasks 双表及前端 i18n 兼容——保守方案先复用 failed + result 前缀 `[blocked]`，观测端按前缀排除出失败率。

**测试用例**：
1. domain-deploy 任务在无 AK/SK 环境派发，断言 fail-fast 且 errors 含"华为云 AK/SK 未配置"指引。
2. 配置 AK/SK 后重派，断言走到 ECS 部署阶段。
3. 观测端日失败统计断言 `[blocked]` 前缀行不计入失败率。
4. 冒烟通过+云阻塞场景，断言 result 保留冒烟结论不丢。

## 四、优先级排序（修好即清零、改动小优先）

| 优先级 | 类别 | 理由 |
|---|---|---|
| P0 | G 手动终止口径 | 两行改动，立即修正失败率统计 |
| P0 | F 配置错误 | 补 key+元数据即清零 9718 类 |
| P1 | E 派发层判死 | scene 绑定接入 pool 兜底，任务级清零 18 条/日（近半），主控侧单点改动 |
| P1 | D 空 errors 包装层 | 单文件改动，先让超时族可诊断 |
| P2 | B/C 静默超时 13 条任务 | 诊断包+keepalive+续跑，工作量大，先靠 D 的诊断定位真凶 |
| P2 | A 外层超时阈值 | 分档超时+timeout_hint，配合续跑一起上 |
| P3 | H 云凭证 blocked 语义 | 单条/日，先加 fail-fast 预检即可 |

## 五、一句话总结（大白话版）

今天 22 条失败里：9 条是引擎"发呆"被看门狗无声掐死（还不知为什么发呆）、6 条是绑定的引擎掉线/低分任务被判死刑（任务级视角占近一半）、2 条活太多到点被掐、2 条死因被包装层吞了、1 条配置没配齐、1 条是用户自己杀的却被算成失败、1 条是云账号钥匙没配任务被记失败。先修"自己杀的算失败"和"补配置"两件小事，再给绑定引擎加降级放行，然后给静默击杀补"临终遗言"，失败就能砍掉一大半。

## 六、收盘复核与前两版差异记录

- 前两版（3a83c4d43 / 80fe84be4）用滑窗口径（事件 33 / 任务 28），窗口起点早于本版 23:00 固定界，且 18:10 版后新增了 18:04 的 9877（H 类）与 22:59 的 9917（E 类）等失败。
- **9664 改归 A 类**：18:10 版按事件 result `engine exit_code=1, errors:` 归 D；本次拉到任务级明细 `t_9dd52344a0e3` errors=`task timed out after 1200.0s`，证实为外层 1200s 超时（引擎包装层把死因吞了才呈现 D 形态）。A 2 条、D 2 条，总数不变。
- **9795 口径备注**：exit=-9 但 result 带降级处理尾文本（SIGTERM 前已有 collected_text），仍归 B 类（-9 击杀）。
- **任务级窗口对齐坑**：agent_tasks.created_at 为 UTC ISO 字符串，与 `date_sub(now(),...)`（北京时间 DATETIME）直接比较会因类型转换漏数（本日滑窗实拉 33 条 vs 对齐后 38 条，漏的 5 条恰是 9616/9617/9620/9644/9664 的任务行）。后续巡检 SQL 必须显式做 UTC 字符串窗口比较。
- 9857（本分析任务自身链路）引擎静默被杀后由 9858 重试成功，事件终态 completed，任务级失败已含在 38 条内。
