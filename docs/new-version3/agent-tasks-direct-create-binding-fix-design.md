# agent_tasks 直接创建 INSERT 绑定数不匹配 — 修复设计（Seq #1991/#1992 事故）

> 版本：v1.5 ｜ 日期：2026-09-02 ｜ 状态：**P0 + P1 + §十 + §十一（模拟 webhook 走 run_pipeline）已实施并部署**
> 事故对象：生产 `Jason7602/hello-flask-9` 模板引导事件 `Seq #1991`（onboarding_exp）/ `#1992`（onboarding_explore）失败
> 结论：**系统级代码缺陷**（`_create_direct_task` / `_create_failed_task` 的 agent_tasks INSERT 列数与值数不匹配 39/37），非环境偶发。

---

## 一、现象

| 项 | 值 |
|----|----|
| 仓库 | `gitcode:Jason7602/hello-flask-9`（模板一键创建 → 首跑引导 Onboarding） |
| Seq | `1991` onboard_exp（引导·快速部署体验）→ failed，`exit_code=1` |
|      | `1992` onboarding_explore（引导·探索方向）→ failed，无 exit_code |
| 结果文本 | `中断：running 超时无任务执行记录（sweeper 兜底回收）` |
| `agent_logs` | `[]`（两个事件均无任何执行日志） |
| `agent_tasks` | 该仓库 **0 条**（事件未创建出任务行） |
| 附带证据 | 生产日志自 16:10 起 seq=1982~1988 连续同报错 |
| 引导结果 | 仅一个 target 落地 Issue #1（`issue_state #1 vod` + 子事件 #1993），#2 缺失，`onboarded:gitcode:...` 已置 1 |

生产日志（关键，process 2642196）：

```
create_direct_task failed for seq=1982: Incorrect number of bindings supplied. The current statement uses 39, and there are 37 supplied.
create_direct_task failed for seq=1983 ... 1988 ... （同样 39/37）
```

---

## 二、根因

**`src/api/event_pipeline.py` 的 `_create_direct_task`（:119–135）与 `_create_failed_task`（:199 同款字面量）值元组硬编码 37 个占位，但列清单 `_TASK_COLUMNS_LIST` 已是 39 列。**

```python
# event_pipeline.py:119-135（直接执行前创建任务 + 设 started_at）
from src.runtime.service import _TASK_COLUMNS_LIST          # ← 39 列（含 pool_tag / pool_engine_ids）
c.execute(
    f"INSERT INTO agent_tasks ({','.join(_TASK_COLUMNS_LIST)}) VALUES "
    f"({','.join(['?'] * len(_TASK_COLUMNS_LIST))})",        # ← 生成 39 个 ?
    (task_id, agent_session_id, engine_id, seq, issue_id, repo_key or "",
     agent_type or "", skill_name or "", skill_id or 0,
     "", "", "", "", "", 0, repo_gitcode_token or "", "{}",
     "{}", "", "", "{}", "{}", "{}", 0, "", 0, "", "",
     "queued", None, "", "", "", "{}", now, None, now)      # ← 硬编码 37 个值 → 39? / 37 bindings
)
# 走队列路径的 enqueue_task 能正确补齐 pool_tag/pool_engine_ids（service.py:431），故只影响 direct 路径
```

- `service.py:132` `_TASK_COLUMNS` 末尾含 `..., pool_tag, pool_engine_ids` → 39 列；
- `event_pipeline.py` 两处字面量元组仍是 37 值，缺 `pool_tag`、`pool_engine_ids` → SQLite 抛绑定数错误 → `create_direct_task` 返回 `""`，**任务行未创建**。

**链路**：
```
direct 事件 → _create_direct_task：
   update_event_config(seq, inst, skill, now)   # 写 started_at（1991 有值，1992 更早中断无）
   → INSERT agent_tasks 39/37 失败 → 返回 ""
→ 无任务行、无 agent_logs → 事件卡 running → sweeper 兜底置 failed
```

---

## 三、为何现在频发（触发背景）

1. 该缺陷由「插件模式 / 多任务」主链路合入带入（历史上 `c3cd1dd4` 修复过、后 `1a609558` 又被 revert，当前远端 93730855 存在此 bug）；
2. **引擎治理把服务器改为「本机直连」后事件更多走 direct 执行** → 缺陷从"偶发"变"必现"；
3. 队列（queue_remote）路径不受影响，只 direct 受影响，易被误判为"本机直连有问题"。

---

## 四、次要问题（一并记录）

| 问题 | 位置/现象 |
|------|----------|
| 重派失效 | 对 1992 重派 → `Pipeline skipped: repository='' or event_type=... not recognized`（redispatch payload 未带 repository，`gitcode:1992/redispatch`）→ UI 重派救不回引导事件 |
| 引导状态不一致 | `onboarded:*` 标记已置 1，但仅 1/2 引导落地 → 该仓库后续不会自动补 2 |
| sweeper 文案误导 | `running 超时无任务执行记录`掩盖真实根因（实为创建任务失败非超时） |

---

## 五、修复方案

### 5.1 P0（必做，一行级）：对齐 `_TASK_COLUMNS_LIST` 补齐 39 值

`event_pipeline.py`：
- `_create_direct_task`（:125-129）与 `_create_failed_task`（同款）的值元组末尾按列顺序补 `""` 占位两个：

```python
# 改后（39 值，顺序须与 _TASK_COLUMNS_LIST 完全一致，pool_tag、pool_engine_ids 补空串）
(..., "queued", None, "", "", "", "{}", now, None, now, "", "")
```

> 安全性要求：不要手数位置——建议以 `_TASK_COLUMNS_LIST` 为唯一来源生成占位（如 `dict`/`functools.partial` 或测试断言 `len(tuple) == len(_TASK_COLUMNS_LIST)`），防止下次加列再漂移。

### 5.2 P1：重派链路补齐 repository

事件重派（`events.py` redispatch）构造 pipeline 入参时带上 `repository`/`owner`/`repo` 完整字段，避免 onboarding 等 `source='template'` 事件重派 `repo=''` 被静默跳过。

### 5.3 P1：引导状态自愈

`trigger_onboarding`/`_finalize_onboarding` 增加幂等补做：若某 target 主事件 failed 且未产出对应 Issue，允许在该 target 上重试（不因 `onboarded:*` 全量标记一次性放弃两个 target）。

### 5.4 P2：观测与防御

- sweeper 文案区分「running 超时」与「创建任务失败」（后者结果写明 `create_direct_task failed` 原因）；
- `tests` 增加 `_create_direct_task` 契约测试：`len(_TASK_COLUMNS_LIST)` 与 INSERT 占位数一致性断言；mock 一次 direct 事件全链路（append_event → create_direct_task → 执行 → 完成）回归。

---

## 六、测试用例设计

基于 P0/P1 修改补充如下用例（与 `tests/` 既有 event_pipeline/onboarding 测试并存，建议与修复同批落地）：

| 用例 | 级别 | 描述 / 断言 |
|------|------|-------------|
| `test_task_insert_row_fully_covers_columns` / `test_task_insert_row_missing_column_raises` | 契约 | `_task_insert_row` 组装值数恒等于 `len(_TASK_COLUMNS_LIST)`；缺列即抛 `ValueError`  |
| `test_create_direct_task_inserts_row` / `test_create_failed_task_inserts_failed_snapshot` / `test_create_direct_task_no_bindings_error_in_db` | 回归 | direct 落行 queued / failed 快照 / 直插不抛绑定错误 |
| `test_trigger_submits_two_webhooks_for_targets` | 集成(§11 v1.5) | `trigger_onboarding` 对 experience/explore 各提交一次模拟 `issue_opened` webhook → `run_pipeline` |
| `test_submit_webhook_body_carries_onboarding_context` | 契约(§11 v1.5) | webhook 体 `object_attributes.open` → issue_opened；`_onboarding` 上下文含 target/skill/scene/result_path |
| `test_onboarding_ctx_applied_and_broadcast_dispatches` | 集成(§11 v1.5) | plugin 广播：pick_engine→0 → 入队 pool_tag=vod，任务 skill=repo-onboarding、ONBOARDING_RESULT 注入——**不依赖仓库 runtime 绑定** |
| `test_onboarding_ctx_legacy_no_engine_fails_event` | 集成(§11 v1.5) | legacy 未绑定引擎 → 与普通事件一致 engine_unavailable（不隐式执行） |
| `test_retry_onboarding_target_submits_webhook` / `test_retry_onboarding_target_already_done` | 回归(§5.3) | target 自愈：retry 走 webhook；已完成不重复 |
| `test_finalize_creates_stage_and_event` / `test_finalize_missing_result_noop` | 回归 | 事件终态后 `_finalize_onboarding` 补 issue_state + onboarding_issue 子事件 |
| `test_redispatch_onboarding_carries_repo` | 集成(P1) | 对 failed 的 onboarding 事件重派 → 走 `retry_onboarding_target`（携带 repo_key/owner） |

> 结束条件：`pytest -m "not e2e_ui"` 全绿；再以一条直连/本机引擎 E2E 走通「事件→direct task→执行→完成」。

---

## 七、改动清单

| # | 文件 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `src/api/event_pipeline.py` | ✅ **已实施**：`_task_insert_row()`（列驱动、缺列抛错）；`_create_direct_task`/`_create_failed_task` 列清单驱动（修复 39/37、39/36） | P0 |
| 2 | `src/api/event_pipeline.py` | ✅ **已实施（v1.5）**：run_pipeline 新增 onboarding 上下文钩子——payload._onboarding（target/skill/scene/result_path）覆盖技能 + 注入 ONBOARDING_* 环境变量；`_enqueue_pipeline_task` 合并 `evt._env_extra` | P1(§11) |
| 3 | `src/api/onboarding.py` | ✅ **已实施（v1.5）**：`trigger_onboarding`/`retry_onboarding_target` 改为构造 GitCode `issue_opened` webhook 体 → `run_pipeline`；删除自建事件/引擎门禁/_create_failed_task 快照/直线程/自定入队；新增 `_submit_onboarding_webhook`（广播模式不需绑定引擎）与 `_await_event_and_finalize`（事件终态兜底补库） | P1(§11) |
| 4 | `src/api/routing.py` | ✅ **已实施**：`onboarding_exp/explore/issue` 注册到 `NON_ISSUE_EVENT_ROUTE`（保留，重派/状态机可识别） | P1(§11) |
| 5 | `src/api/routes/events.py` | ✅ **已实施**：redispatch onboarding 事件走 `retry_onboarding_target`（target 级自愈） | P1(§5.2) |
| 6 | `src/runtime/cleanup.py` | ✅ **已实施**：sweeper 文案区分「创建任务失败」与「running 超时」 | P2 |
| 7 | `src/api/routes/tasks.py` | ✅ **已实施**：`_run_notify` 删除后台 ChatAgent/用户 token，改静态模板消息 + 飞书通知（§十） | P0(§10) |
| 8 | 测试 | ✅ **已实施**：`test_agent_tasks_insert_contract.py`（5 用例）、`test_onboarding.py`（webhook 提交/上下文/自愈/幂等）、`test_issue_lifecycle_routing.py`（广播入队 + legacy engine_unavailable） | P2 |

---

## 八、验证与回归

1. 单元：新增列/值数一致性断言通过；`pytest -m "not e2e_ui"` 全量回归不红；
2. 端到端（生产预检）：新模板仓库一键创建 → 引导两事件 completed → `agent_tasks` 出现 direct 任务行、有 agent_logs；重派一个 failed 事件能恢复；
3. 观测：`sweeper` 不再把"创建失败"误标为 running 超时。

---

## 九、风险

| 项 | 说明 |
|----|------|
| 手数占位错序 | 强制以 `_TASK_COLUMNS_LIST` 驱动生成占位 + 契约测试，禁止手工数 |
| direct 事件在修复前持续 fail | 修复上线部署前，direct 类事件（含新仓库引导）仍会失败——建议尽快合入 new-version3/main 并部署 |
| 存量 failed 事件 | 本方案不回溯清理已 failed 的 1982-1988/1991-1992；如需可在部署后按需重派或补引导标记 |

## 十、补充：任务分配不得触发 ChatAgent（原则与整改）

> **原则（必须遵守）**：ChatAgent 只在「用户真实回复消息」时执行；任务分配 / 通知**不得**触发 ChatAgent，也不得注入用户个人 token 用于 GitCode 写操作。

### 10.1 事故回放（本次实锤）

- 引导 VOD Agent 执行 `repo-onboarding` 时调 `POST /api/tasks/notify`（`tasks.py:419`，接口注释即含"…+ **后台 chat-agent** + 飞书通知"）；
- `:479` 无条件 `asyncio.create_task(_run_notify(...))` → **无任何用户输入即后台执行一次完整 chat-agent**；
- `_run_notify` 内：`get_user_gitcode_token(user_id)`（注入用户个人 token）→ `_build_task_prompt(…)`（**任务对话 prompt**，含"澄清需求并回帖"职责）→ `_consume`（实际跑 opencode，cwd=`chat/{uid}/{conv}`）；
- 该自动会话按"回帖"职责把结果 POST 到 GitCode Issue → **作者 = 用户本人**（本次 Jason7602，评论 `187623090` @10:51:44，webhook `seq 2002`）；
- 对比：引导结果评论 `187623013`（grape_bot @10:50:59）走 bot token、作者为 grape_bot —— 同 Issue 上两条评论分属两条链路，正是误解来源。

### 10.2 ChatAgent 触发点核对

| 触发路径 | 用户真实消息 | 符合原则 |
|---|---|---|
| Web `/api/chat/send*` `/submit` | ✅ | ✅ |
| 飞书 `im/handler_async.process_chat_async` | ✅ | ✅ |
| `tasks/notify → _run_notify` 后台自动执行 | ❌ | ❌（**唯一违规点**） |
| VOD/事件 direct 执行、`vod_handler_async` | — | 非 ChatAgent 流程 |

### 10.3 与既有状态机原则的一致性

`task-status-processing-trigger-fix.md §3.3` 已确立"任务状态**仅在任务会话收到真实用户消息**时翻转 `processing`"（`mark_task_processing_by_conversation`），`/api/tasks/{id}/start` 只读不翻转。即**状态侧早已遵循"用户主动权"，唯独 `_run_notify` 的自动 agent 执行漏网**，两者语义矛盾（自动跑了一次对话，任务却仍 `active`）。

### 10.4 整改方案（待实施）

1. **`tasks/notify` 去掉后台 LLM 执行**：`_run_notify` 删除 `_consume`，改为**静态模板** agent 消息（「📋 任务已分配 · Issue #N · 请到任务中心『去对话』查看」）+ 飞书通知，不调 opencode/stream；
   - （可选）首条欢迎推迟到用户真正进入对话后的第一条回复时，由 chat 正常链路生成。
2. **收敛 ChatAgent 触发**：仅 Web `send*`/`submit` 与飞书 `process_chat_async`（用户消息）执行；`start_task`/会话列表保持只读。
3. **写操作身份隔离**：任何"非用户主动提交"路径不注入 `USER_GITCODE_TOKEN`；回帖/评论/建 Issue 一律走仓库 bot token（grape_bot）并署名"系统代发"。
4. **回归看护**：`tasks/notify` 后断言**不产生** agent 执行（conv 首条为模板消息、无 stream 调用）；`/api/chat/send` 才触发 agent。

---

## 十一、Onboarding 应收敛为「普通 VOD 事件」（原则与整改）

> **原则**：Onboarding 除「托管时向 VOD 主动提交」这一触发差异外，**执行语义必须与普通事件完全等价**（同一套引擎门禁、任务登记、监督、重派、状态机）。

### 11.1 现状：onboarding 是"旁路执行"

`onboarding.py trigger_onboarding` 与普通事件（webhook → `run_pipeline`）的偏差：

| 环节 | 普通事件 | onboarding 现状 | 偏差 |
|---|---|---|---|
| 触发 | GitCode webhook | 模板一键创建回调（仅托管时） | ✅ 允许（触发差异） |
| 事件类型 | 可路由（`issue_opened` 等） | `onboarding_exp/onboarding_explore`，**不在路由映射** | 不可被 pipeline/重派识别（重派 `not recognized`） |
| 路由/反循环/enable 检查 | ✅ | ❌ 不经过 | 直接发 |
| 引擎门禁 | `pick_engine=None → engine_unavailable`，不执行 | **死守卫**（`if engine_id is None` 永不成立）→ **无引擎照跑本机 opencode** | 核心漏洞 |
| 执行方式 | direct 分支统一 `_execute_agent`（登记 `agent_tasks`） | 自开线程 `_execute_agent_wrapper → _execute_agent`，**不经任务登记** | 无任务行 |
| 任务登记 | 有行 → sweeper 不杀 | 无行 → **sweeper 秒判 failed** | 主事件假失败 |
| GitCode 写操作 | 按 skill/bot 治理 | 引导 skill 直接建 Issue/发评论，token 回退用户 | 无人把关的写副作用 |

### 11.2 由此导致的已发故障（全部对得上）

1. 无引擎也派发 → `Seq 1998/1999` 无任务行、`event.runtime=''`；
2. sweeper 按"无活跃任务"把 running 主事件判死 → 事件 failed，但本机 opencode 后台跑完（建 Issue #1/#2、以用户名义评论 `187623090`）；
3. 子事件 `2000/2001` 由 `_finalize_onboarding` 以 `completed` 记账（构造即完成，非执行结果）→ "无引擎却 completed" 表象；
4. 重派 onboarding 事件报 `repository='' not recognized` → 无法恢复；
5. `onboarded` 一旦置位不再重讲 → 状态不可自愈。

### 11.3 收敛目标（整改方向，v1.5 刷新：对齐广播模式）

> **关键认知（2026-09-02 复盘）**：生产引擎为**广播（plugin）模式**——引擎按 `pool_tag`/`claim_by_tag` 竞争认领，**不依赖 per-repo 的 `devops_agent_session.runtime_id` 绑定**。`pick_engine` 在 plugin 模式下恒返回 `0`（广播），`run_pipeline` 尾部（`event_pipeline.py:1051`）对 `engine_id==0 + plugin` 走 `_enqueue_pipeline_task` 入队广播 → 引擎认领执行。
>
> 因此 onboarding 的**正确收敛方式不是"给仓库绑定 runtime"，而是"把自己伪装成一条普通 GitCode webhook 事件，完全走 run_pipeline"**。这样广播模式天然可用，且与普通事件的引擎门禁/任务登记/监督/重派完全同一套。

整改方向（刷新）：

1. **触发 = 模拟 GitCode webhook 事件体**：`templates.create_from_template` 托管完成后，不再自建 `onboarding_exp/explore` 事件，而是**构造等价于 GitCode `issue_opened` 的 webhook 事件体**（repo_key/owner/仓库名的 `object_attributes` 结构），直接提交 `run_pipeline(raw, headers, account, repo_key_override)`——与 webhook.py:115 完全同一条路径。
   - `run_pipeline` 内部自会：归一化 → enable 检查 → 阶段路由（`issue_opened → vod`）→ 会话查找 → 引擎门禁 `pick_engine` → 广播入队或 direct 执行 → `_create_direct_task`/`enqueue_task` 登记任务。
   - **不再需要 onboarding 自己 `pick_engine`、自己拦截 engine_unavailable、自己写 failed 快照**——这些都是 run_pipeline 的职责。
2. **引导技能注入**：webhook 事件体 payload 携带引导目标（`ONBOARDING_TARGET`）+ 场景（`ONBOARDING_RESULT`）所需上下文；`repo-onboarding` 技能名经事件体的 skill 字段/场景 skill 解析（与普通事件取技能完全一致，不改技能分发）。
3. **删除 onboarding 引擎旁路**：`trigger_onboarding` 内不再套用 `pick_engine`+`engine_unavailable` 门禁与 `_create_failed_task` 快照；`_dispatch_onboarding_target` → 直接构造 webhook 体提交 `run_pipeline`。`_execute_agent_wrapper` 直线程、`_enqueue_onboarding_task` 自定入队一并移除（由 run_pipeline 统一 direct/queue）。
   - plugin（广播）模式：`pick_engine→0` → 广播入队（pool_tag=vod）→ 引擎认领，**仓库无需绑定 runtime**，Seq 2008/2009 的 `engine_unavailable` 不再发生。
   - legacy 模式：仓库若绑了引擎走 direct/queue；未绑则与普通事件一致 engine_unavailable（不隐式执行，行为正确可见）。
   - 保留 baseline：`onboarded` 幂等、双 target（#1/#2）、`repo-onboarding` 技能，只改"丢给 run_pipeline"，改"何时/做什么"。
4. **`_finalize_onboarding` 依托事件终态**：执行仍产出 `onboarding-result-{target}.json`；由 run_pipeline 执行终态后（completed/failed）触发兜底补库（issue_state + 子事件），不再"线程内自收尾"。
5. **GitCode 写操作治理统一**：引导建 Issue/评论一律走仓库 bot token，不注入用户个人 token（§十 3）。

### 11.4 影响评估

- 涉及：`onboarding.py`（触发→**模拟 `issue_opened` webhook 体提交 `run_pipeline`**，删除自建事件/引擎门禁/直线程/自定入队）、`routing.py`（已有 `onboarding_*` 注册保留，事件类型改为 `issue_opened` 后走原生路由）、webhook 事件体构造（`templates.create_from_template` 调用点）、`_finalize_onboarding` 触发位置（事件终态兜底）、事件分类与存量恢复（`Seq 2008/2009、1991/1992、1998/1999` 之类已 failed 事件可重派走广播）。
- 收益：**广播（plugin）模式下 onboarding 不再依赖仓库绑定 runtime**，`engine_unavailable` 消失；引导与普通事件共用引擎门禁/任务登记/监督/重派/状态机；杜绝无监督写操作与人名冒用。
- 风险：模板一键创建引导路径需回归验证——plugin 广播认领、legacy 绑定与未绑定两种模式，双 target（#1/#2）各走通，`onboarding-result` 落盘与 `_finalize_onboarding` 兜底正常。

---

*P0 + P1 + §十 + §十一（v1.5 模拟 webhook 走 run_pipeline）均已实施并部署；广播模式 onboarding 无需绑定 runtime；存量 failed 事件见 §九按需重派。*