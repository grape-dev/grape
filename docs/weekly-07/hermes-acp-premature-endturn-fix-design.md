# hermes ACP「text 后 60s 静默判完成」导致任务被提前截断：根因与修复方案

- 日期：2026-10-18
- 触发事件：seq **13315**（Issue #366 的 coding 环节）
- 引擎/机器：`hermes-worker-ecs-grape-de-0005` / credential `liufei-build-005`(id=220, last_ip 1.92.108.106)
- 代码位置：`src/agent/hermes_acp.py`（worker 侧适配器）+ `src/runtime/service.py`（平台终态）
- 结论一句话：**不是超时、不是被杀，是适配器的「模型输出 text 后 60 秒无事件 = 视为完成」这条兜底规则把「正在生成大补丁的模型」误判成「干完了」，按 exit 0 正常收尾并杀掉 hermes 进程，工作从中间被截断，平台还标了绿。**

---

## 1. 现象与证据

### 1.1 事件时间线（seq 13315，duration 487.9s）

| 时刻(UTC) | 事件 |
|---|---|
| 05:14:45 | A2A 创建事件，派给 runtime 13032（hermes 引擎） |
| 05:14:52 | `step_start: Step 1 started (session: ACP)` |
| 05:21:46 | `tool_use: patch .../src/mysql_compat.py`（两处 patch 落地） |
| **05:21:48.936** | **最后一条 text：「现在改造」** |
| **05:22:48.958** | **`step_finish: Step 1 done (stopReason=end_turn)` ← 间隔 60.02s** |
| 05:22:53.973 | 同一句话的后半截：「src/api/db_setup.py 的 init_database()：」 |
| 05:22:54.360 | `Task completed: t_bd9a2106ac0a exit=0` |

- 任务 `t_bd9a2106ac0a`：status=completed、exit_code=0、self_reported=0、`structured_output` 空、`result_text` 停在半句（1.3KB）。
- 服务端日志（79 主）**无任何 error/timeout/被杀**记录：真超时是 `stopReason=timeout → exit -1`，强杀是 `-9`，本例是干净的 `end_turn → exit 0`。
- 后果：工作区只落了 `docs/issue-366/{spec,plan,task}.md` 与 `mysql_compat.py` 两处 patch；`db_setup.py` 改造、测试、提交/PR、A2A 交棒全部未做。Issue #366 停在 `stage=coding`（updated_at 就是事件创建那一刻），此后无任何新事件，流水线静默停住。

### 1.2 判据就是那条 60 秒规则

```python
# src/agent/hermes_acp.py
_SILENT_TIMEOUT = 300        # 工具调用后 5 分钟无事件 → 超时终止
_TEXT_SILENT_TIMEOUT = 60    # text 输出后 60s 无后续 → 超时终止
...
if _last_event_type == "text" and elapsed_silent > _TEXT_SILENT_TIMEOUT:
    log.info(f"{label}hermes ACP silent {elapsed_silent:.0f}s after text, assuming done (response lost)")
    prompt_result["stopReason"] = "end_turn"      # ← 判成正常完成
    break
...
yield ("step_finish", f"Step 1 done (stopReason={stop_reason})")
if stop_reason == "end_turn" and not error_events:
    exit_code = 0                                  # ← 于是 exit 0
```

`step_finish` 相对最后一条 text 的间隔**正好 60.02 秒**，与常量完全吻合；这种「整 60 秒」的收尾在平台上是常态（见 §3 影响面）。

### 1.3 模型当时还在干活（不是在收尾）

- 最后一条 text 是半句话「现在改造 → `src/api/db_setup.py` 的 `init_database()`：」，是个**动作预告**，不是收尾结论；
- 这半句的**后半截在 `step_finish` 之后才到**，说明 hermes 进程在被判完成的那一刻仍在产输出；
- 任务输出里**没有** agent 正常收尾的结构化记录（`actions`/`conclusion`/`issue_summary`）。对照组：coding-cli 走正常响应通道（间隔 ≤5s）的 29 个任务里 28 个都有 `conclusion`。

---

## 2. 根因：这条规则为什么会被引入，又为什么现在不成立

### 2.1 引入历史（commit `8b72dd87e` / `0d56e0b89`，2026-09-08，XOcto）

提交原文：

> 问题根因：hermes ACP 协议兼容性问题导致不发 PromptResponse，grape 侧加了过多兜底（11 条 end_turn 路径 + 两层重试 50×49），重试时 continuation prompt 让 agent 重复执行导致 Issue 重复评论污染。

该提交拆掉的东西：

- 适配器内的多步重试循环 `MAX_ATTEMPTS = 50`：检测不到收尾块就**关进程、开新进程新 session**，把「你上一轮说要做什么但没做」当**新 prompt** 发过去（旧代码 `_step_prompt` 构造见历史版本）；
- 平台 `src/runtime/service.py` 的 `PREMATURE_END_TURN` requeue（最多 49 次）——两层叠加最多 2450 次执行；
- `prompt_builder.py` 的执行纪律段落；同时新增 `event_pipeline.py` 的「先查评论列表再回帖」去重。

换成：**单次执行 + 分级静默**（text 后 60s 视为完成、tool 后 300s 判超时）+ 回帖去重。

**当时的取舍是合理的**：拿不到 PromptResponse 时，如果一直等，所有 hermes 任务都会烧到全局超时（默认 3600s）并判 failed；而重复执行会真实污染 Issue（重复评论、重复建 PR）。所以宁可「偶尔截断」，也不要「大面积失败 + 重复污染」。

### 2.2 判据今天不成立的三个理由

1. **hermes 现在会正常返回 PromptResponse。** hermes 的 ACP `prompt()` 是整个 turn（含所有工具调用）跑完才 return 的（`acp_adapter/server.py`，`PromptResponse(stop_reason=...)`）。实测：hermes 引擎上有 `step_finish` 的任务 2651 个，其中 1479 个「最后 text → step_finish」间隔 ≤5s（响应秒回）。**没收到响应 ≠ 没干完，只等于 turn 还没结束。**
2. **60 秒分不清两件事：**
   - (a) 模型确实干完了、hermes 在收尾（响应 >60s 才回）→ 无害。60s 桶里 86% 都有 `conclusion` 收尾块，属于这类；
   - (b) 模型正在生成下一个大 tool_call（如大补丁），这期间 hermes **一个 ACP 事件都不发**（tool_call 只在工具调用生成完成/开始时才上报）→ 被误判完成、进程被杀、活干掉。**13315 与 13007 都属于 (b)。**
3. **同一类问题在流式引擎那边早就修对了，hermes_acp 是单点不一致：** `src/agent/executor.py` 用 `AGENT_SILENT_WINDOW`(默认 300s) + 「`proc.poll() is None`（进程还活着）就续期静默窗口，上限 `AGENT_SILENT_EXTEND_CAP`」（#501 修复，注释：*避免长命令被误杀*）。`hermes_acp.py` 的 60s 一刀切没有这层存活判据。

### 2.3 平台侧的「假完成门槛」没覆盖这条路径

- `src/api/event_pipeline.py` 有关卡B（假完成硬门槛，issue-P5）：coding/issue/qa/sre 任务 exit 0 还须有 `self_reported` 或真实写操作（POST comments/pulls、A2A），否则降 `failed`；
- 但**它只作用于主控自己跑 agent 的 exec 路径**。worker/A2A 任务的终态走 `src/runtime/service.py::complete_task`，那里 `exit 0 → completed`，没有产出判据（`service.py` 1844-1845 行的注释就是 2026-09-08 回滚留下的）；
- 13315 的 `self_reported=0`、无写操作、无结构化产出 —— 若走关卡B 会被降 failed，但因为是 remote worker 路径，直接绿了。

---

## 3. 影响面（实测）

| 指标 | 数值 |
|---|---|
| hermes 引擎有 `step_finish` 的任务 | 2651 |
| 其中间隔 ≤5s（PromptResponse 通道） | 1479 |
| 其中间隔 ≈60s（本文这条兜底） | 1034（占 39%） |
| ≈60s 桶里带 `conclusion` 收尾块（无害） | 819 / 1034（79%） |
| ≈60s 桶里 coding-cli：被切断（半句收尾） | 6 / 44 |
| **被 60s 切断但事件标绿**（agent_type∈{issue,coding,qa,sre}、exit 0、completed、无 conclusion） | **114 次**（2026-09-10 起；近 7 天 11 次、近 3 天 6 次） |

同类事件举例：`t_bd8f32c0ae61`(seq 13007, 2026-10-16, 同一台 worker、同 coding-cli)：最后 text 09:12:40 → `step_finish` 09:13:40（整 60s），最后一句停在「现在写 #362 测试文件：」。

---

## 4. 修复方案（分级）

### P0 · `src/agent/hermes_acp.py`：把 text 静默从「判完成」改成「判未完成 / 有据放行」

改动点（替换现有 457-464 行逻辑）：

1. 静默 >60s **不再直接 `end_turn`**，先查**完成证据**：对累计的 `collected_text` 尾部跑 `parse_agent_output_json`（现成的 `_detect_structured_output` 是子串匹配，建议升级为「解析出 actions + conclusion」），
   - **有收尾记录 → 判完成（exit 0）**，与现状一致，覆盖那 79% 无害场景（而且能更早结束，反而释放 worker 槽位）；
   - **没有 → 继续等**，窗口放到 300s（与 tool 静默同预算；新增 `RUNTIME_TEXT_SILENT_TIMEOUT` 可覆盖，默认 300）。
2. 300s 到点仍无证据时，**不要假装成功**：
   - 有 `tool_use` 产出（干了活但没收尾）→ `exit 0` + result 追加 `[PREMATURE_END_TURN]` + `errors` 写明「未检测到收尾记录」（交给平台决定是否续做）；
   - 完全无产出 → 按 `timeout` 处理（exit -1）。
3. 全程**不要提前 close stdin / 杀进程**，只有在 turn 真的结束（拿到 PromptResponse / 进程 EOF）或全局超时才收尾。
4. 判据原则一句话：**判成功必须有证据（PromptResponse 或收尾块），不能靠「安静了」。**

`_last_event_type == "tool_use"` 的 300s 分支保持不变（工具执行期间无事件是正常的）。

**测试**：扩展 `tests/test_hermes_global_timeout.py`（已有静默场景骨架）：
- text 静默 60s + 累计文本含合法收尾块 → `end_turn` / exit 0；
- text 静默 60s + 无收尾块 + 后续 300s 无事件 → 不判成功，带 `[PREMATURE_END_TURN]`；
- text 静默 60s + 无收尾块 + 300s 内收到 PromptResponse → 按响应结果收尾。

### P1 · 平台侧（`src/runtime/service.py::complete_task`）：别让「截断」静默标绿

- exit 0 但 `parse_agent_output_json` 解析不到 actions/conclusion、也没有可信写操作时，**至少写可见标记**（task.errors / 事件 result 前缀「疑似提前结束」+ 前端可见），不要把「无产出」和「有产出」都写成 completed。
- 建议**先只做可见性，不照搬关卡B 降 failed**：关卡B 对「只做分析、只回评论」的任务会误伤。
- 如果要做重跑：**只允许 1 次**，且重跑 prompt 必须带幂等约束（「先检查工作区已有改动 / 已有评论，不要重复提交、不要重复评论」）——这正是 2026-09-08 被回滚的根因，不写死这条就会复发 Issue 污染。

### P1 · 续步（谨慎，需先验证）

- 只对「turn 确实结束（拿到 `end_turn`）但没有收尾块」这一小类做续步；
- 必须**在同一个 session 内**续（`session/resume` 保留 history），不要再开新进程 + 新 session + 只丢一行摘要——旧方案就是那样让 agent 从零重做、重复评论的；
- 动手前先实测：turn 结束后同 session 再发一个 prompt，是正常开新 turn 还是被判成「纠正消息」（旧代码注释称后者，可能是老版本行为）。

### P2 · 治本：让「生成中」可观测

长 tool_call 参数生成期间 hermes 不发任何 ACP 事件，是「静默判据」永远会误伤的根源。建议 hermes 侧在生成大 tool call 期间发心跳（或把 tool_call 开始就作为 update 先发），让上层有活跃信号可看；否则适配器只能永远靠「等久点 + 要求收尾块」兜。

### E · 本次事件的收尾（人工）

worktree（`/home/liufei/grape-workspaces/gitcode/grape-dev/grape/issues/366/coding`）里已有 `docs/issue-366/` 三份文档 + `mysql_compat.py` 两处 patch；`db_setup.py` 改造、测试、提交/PR、A2A 均未做。续做建议在**同一 worktree** 重派 coding，并在 prompt 里加「先查看工作区已有改动，不要重做前两步」。

---

## 5. 风险与兼容性评估

| 项 | 评估 |
|---|---|
| worker 槽位占用（P0 把 60s 拉到 300s） | 净影响偏正面：60s 桶里 79% 带收尾块的任务会**更早**结束；只有少数（无收尾块）会多占 ≤240s，且受全局超时约束（最坏 300s + RUNTIME_TASK_TIMEOUT 3600s）。 |
| 误判「有收尾块 → 完成」 | 若模型在正文里**引用**了 JSON 格式样例（例如写文档时），子串匹配会误判。故 P0 要求用 `parse_agent_output_json` 解析尾部，而不是 `_detect_structured_output` 子串匹配；误判最坏结果与今天的 60s 规则等价（不会更差）。 |
| 真的挂死（模型流死掉）不再被 60s 快速收尾 | 由 300s 文本预算 + 全局超时接管，任务会多等最长 240s；属于可接受的时间换正确性。建议加环境变量 `RUNTIME_TEXT_SILENT_TIMEOUT` 便于现场调。 |
| P1 加标记而不降 failed | 不改变现有通过/失败口径，只增加可见性，零回归风险。 |
| P1 重跑 | 必须 1 次 + 幂等约束 + 沿用 `event_pipeline` 回帖去重；建议灰度（先在 1 台 worker 上生效）。 |
| 同类适配器 | `hwcloud_acp.py`、`opencode` 路径无此 60s 规则（后者走 executor 的窗口续期），本次只改 hermes 适配器，不影响其它引擎。 |
| 生效路径 | 适配器随 **worker 包**分发：只改主控不生效；需随下一次 worker 升级铺到 worker（含 刘菲 的 0005），worker 上另有 hermes 引擎版本需对齐。 |

## 6. 验证方法

1. 单测：`tests/test_hermes_global_timeout.py` 扩展用例（见 P0）。
2. 端到端：在 0005 上跑一个 coding 任务，用 seq 页对照「最后 text → step_finish」间隔与是否有 `[PREMATURE_END_TURN]` 标记；同机再跑一次「模型故意长思考」场景验证不再被 60s 切断。
3. 回归观测 SQL（双主/平台侧，只读）：
   - 60s 桶任务数是否下降（当前 1034）；
   - 「被切断但标绿」（exit 0 + 无 conclusion + 间隔 58-62s）事件数是否归零（当前累计 114）。

## 7. 结论

- 60 秒规则是 2026-09-08「去重试防重复评论」改造的产物，本意是「收不到 PromptResponse 时给任务一个体面的成功收尾」，不是完成度判据；
- 它今天把「模型正在生成大补丁」和「模型干完闲着」混为一谈，导致任务被从中间截断却标绿（累计 114 次）；
- 修复方向：**判成功必须有证据**（PromptResponse 或收尾记录块），静默只用来决定「还要不要等」，不再用来决定「算不算完成」；平台侧补齐可见性，重跑必须幂等且限 1 次。

---

## 8. 实施记录（P0 + P1 可见性已落地）

分支 `fix/hermes-acp-premature-endturn`（基于 `local-liufei`）。

### 8.1 改动清单

| 文件 | 改动 |
|---|---|
| `src/agent/hermes_acp.py` | ① 新增 `PREMATURE_END_MARKER = "[PREMATURE_END_TURN]"`（标记唯一来源）；② 新增 `_text_incomplete_timeout_for()`（默认 300s，`RUNTIME_TEXT_SILENT_TIMEOUT` 可覆盖，设 ≤60 即退回旧行为）；③ 新增 `_has_completion_record()`（严格解析 + `strict=False` + 键名 strip + `_robust_join_json` 兜底，只认 `actions`+`conclusion`）；④ 重写 text 静默分支：静默只决定「还要不要等」，「算不算完成」由证据决定（收尾记录 or PromptResponse），无证据等到上限 → `stopReason=premature_end`（不再提前 close stdin / 杀进程）；⑤ `premature_end` → `exit 0` + `errors` 追加标记 + result_text 追加标记；⑥ **修掉 `_on_prompt_done` 别名快照 bug**（见 8.2）。 |
| `src/runtime/service.py` | `complete_task` 只做可见性：`result_text` 带标记时 → 事件 `result` 前缀「⚠️ [PREMATURE_END_TURN] 疑似提前结束（未走到收尾）：」+ `agent_tasks.errors` 写标记。**状态仍是 completed，不降 failed、不重跑**；非疑似场景不写 `errors`，避免覆盖 fail 路径的真实错误。新增 `premature_end_marker()` 懒加载转发标记（避免导入期耦合）。 |
| `tests/test_hermes_global_timeout.py` | 新增 16 例：3 例 `_text_incomplete_timeout_for` 取值、6 例 `_has_completion_record`、3 例静默收尾行为（有收尾记录→end_turn；半句静默→premature_end + exit 0 + 标记；晚到 PromptResponse 必须被采纳）、1 例标记唯一来源；并为假 hermes 增加 3 种模式（`text_record_then_silent` / `text_partial_then_silent` / `text_partial_then_response`）。 |

### 8.2 关键新发现：ACP PromptResponse 通道从未生效（别名快照 bug）

写单测时假 hermes 明明回了 `{"id":3,"result":{"stopReason":"end_turn"}}`，适配器却继续等到静默上限才收尾。定位到：

```python
# _ACPSimpleClient.__init__（修复前）
self.on_prompt_done = on_prompt_done or (lambda r: None)
self._on_prompt_done = self.on_prompt_done      # ← 快照构造时的 no-op
```
而调用方是**构造后**才赋值：
```python
client.on_prompt_done = lambda result: (prompt_result.update(result), prompt_done.set())
```
`_handle_response` / `_handle_notification` 走的是 `self._on_prompt_done`（快照），因此 ACP 的 PromptResponse、`session/update` 的 stopReason **全部被丢弃**，`prompt_done` 只能由定时器 / 聊天稳定 / 发送失败 / 进程 EOF 置位。

- 证据：假 hermes 复现（收到 RESP id=3、`prompt_req_id=3`，但 `prompt_done` 未置位）；同日 `hwcloud_acp.py:673` 已有注释明确记录同一 bug 并已修复（「hermes_acp 的潜伏 bug，其生产靠 60s text 静默超时兜底硬撑」）。
- 影响：这解释了为什么约 39% 的 step 恰好停在 60s——**60s 兜底是事实上的唯一收尾通道**，而非「偶发兜底」。间隔≈0s 的那批（29 例中 28 例）应为 reader-EOF 通道，待 worker 日志确认。
- 修复：删掉快照，5 处调用点改读公开属性（与 `hwcloud_acp.py` 完全一致）。这样 PromptResponse 成为主通道，任务正常结束时**立刻**收尾，不再空等 60s。

### 8.3 验证

- `tests/test_hermes_global_timeout.py`：**21 passed**（原 5 例全部保留通过，新增 16 例）。
- 回归：10 个相关测试文件（runtime / a2a_queue / runtime_pool / event_status_protection / runtime_agent_concurrency / executor_hang_fallback / hwcloud_acp / hermes_acp_bootstrap / issue_session / session_token_lifecycle）结果与基线 `origin/local-liufei` **完全一致**：18 failed / 146 passed，失败集合逐条 diff 相同（均为既有的环境性失败，如 `FakeRegClient.register() got an unexpected keyword argument 'cli_version'`、需要真实 DB 的 session token 用例）。

### 8.4 未做（明确留出）

1. **worker 灰度**：适配器随 worker 包分发，需在 0005（可复现机）铺一次并对照 seq 页验证「最后 text → step_finish」与标记；其余靠自动升级拉平。
2. **P1 续步 / P2 引擎心跳**：不与 P0 混提；续步必须同 session + 幂等约束，动手前先实测「turn 结束后同 session 再发 prompt」的行为。
3. **#366 半截活**：worktree 里 `docs/issue-366/` 三份文档 + `mysql_compat.py` 两处 patch 仍在，需在同一 worktree 重派 coding（prompt 带「先看已有改动，不要重做前两步」）。
4. **回归观测**：60s 桶任务数（当前 1034）与被切断标绿数（当前 114）需在 worker 升级后按 §6 的 SQL 复测。

