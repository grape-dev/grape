# 飞书 Chat Agent 处理进度反馈优化方案

> 版本：v2.0（方案稿 · 统一心跳轮询，放弃 direct 流式）
> 日期：2026-08-17
> 说明：解决飞书 Chat Agent 消息处理后长时间无反馈（除一个 ✓ emoji 外无任何中间信息，直到最终结果才回复）的问题。本文为方案，基于现有代码（`src/im/handler_async.py`、`src/im/feishu_async.py`、`src/runtime/service.py`）分析。**v2.0 采用统一心跳轮询代替 direct 流式转发**——性能开销更低、API 调用少 60-70%、代码更简单、direct/queue 统一逻辑。**不含代码改动。**

---

## 一、问题现象与现状

### 1.1 现象

用户通过飞书向 Chat Agent 发消息后：
1. 消息上出现一个 `✓` emoji（`add_reaction`，秒回）；
2. 之后**完全无任何反馈**（数秒到数十秒不等）；
3. 直到 agent 执行完，才一次性收到最终回复卡片。

### 1.2 现状链路（`src/im/handler_async.py:428-802`）

```
飞书消息 → process_chat_async
  → 路由/绑定/命令处理（快速返回）
  → get_or_create_session
  → async with lock（同仓库串行，等待前一条处理完）
  → _process_chat_inner_async
      → 写 user 消息 → 查 token/配置 → 构建 prompt
      → await api.add_reaction(ctx, msg_id, "OK")        ← 【唯一即时反馈】
      → 执行 agent：
           direct 模式：async for consume_stream_async(...)
                        → text/reasoning 事件【已 yield 但被丢弃，不转发给飞书】
           队列模式：await wait_task_terminal(tid)
                        → 纯轮询（3s/次），无中间进度
      → await api.reply_card(ctx, msg_id, output)        ← 【唯一最终回复】
```

### 1.3 根因

**引擎（opencode）具备流式产出能力，但飞书 Chat 侧只消费了 `done` 事件。**

- `src/agent/executor.py` `stream_agent` 已 yield 以下事件：
  - `step_start`：步骤开始（含 session id）
  - `text`：生成文本增量
  - `reasoning`：思考过程
  - `tool_use`：工具调用（tool + input）
  - `error` / `other`
- 但 `_process_chat_inner_async` 的 direct 分支（handler_async.py:762-772）只取 `done` 更新 `final_text`，中间事件全部丢弃；
- 队列模式 `wait_task_terminal` 是黑盒轮询（每 3s 查 `agent_tasks` 状态，无中间态）。

**因此用户感知 = 消息发出后一片死寂，直到结果出现。**

---

## 二、目标

1. 消息发出后 **1 秒内**有明确"已收到 + 处理中"反馈；
2. 处理过程中**持续可见进度**（分析中 / 生成中 / 调用工具中 / 已跑时长）；
3. 不阻塞 agent 执行、不引入高频请求、不影响现有会话/串行机制；
4. direct 与队列两种执行模式都覆盖。

---

## 三、方案设计

> 复用飞书**消息编辑**能力 `PATCH /im/v1/messages/{message_id}`（已有 `update_message`，feishu_async.py:329），通过「先发占位卡片 → 后续 patch 更新内容」实现进度展示。无需新增 API。

### 3.1 总体流程（统一心跳）

```
收到消息
  → reply_text 发占位消息 "⏳ 正在处理..."
        ↓
  轮询 task 状态（每 3s，direct 和 queue 统一逻辑）
      queued/dispatched → patch占位 "⏳ 任务已排队/执行中... (Ns)"
      running           → patch占位 "⏳ 执行中... (Ns)"
        ↓
      completed/failed/cancelled → patch 占位为最终回复（或删除占位、再发 reply_card）
```

> **为什么不用 direct 流式转发**：direct 模式的 `consume_stream_async` 虽然能产出 `text`/`reasoning`/`tool_use` 事件，但 remote queue 完全拿不到（流在远端 Worker 进程内）；流式转发还需节流映射，API 调用量是心跳的 3-4 倍。统一心跳轮询对两种模式完全一致，代码更少、成本更低、体验已够用（大多数对话 5-30s，3s 心跳能更新 1-5 次）。

### 3.2 关键实现点

| # | 点 | 说明 |
|---|---|---|
| 1 | **占位消息** | `reply_text` 返回 `reply_id`，后续用 `update_message(ctx, reply_id, text)` 编辑同一消息（不产生消息刷屏） |
| 2 | **心跳轮询** | 自定义 poll 循环（替代 `wait_task_terminal`），每 3s 用 `get_task(task_id)` 查状态 + `update_message` 更新占位卡片；direct 模式也统一走此逻辑 |
| 3 | **快回短路** | 执行时长 < 2s（心跳未触发）→ 直接发最终回复，不产生占位消息（优化快场景，避免无意义的占位闪烁） |
| 4 | **最终替换** | 任务终态后把占位卡片内容替换为最终回复（复用同一条消息 `update_message`） |
| 5 | **失败兜底** | `update_message` 抛错（如消息被删/限频）→ 静默忽略，仍用 `reply_card` 发最终结果 |
| 6 | **direct 与 queue 统一** | 两种模式共用**同一套**「占位→心跳→替换」逻辑，无需分支区分 |

### 3.3 心跳文案映射

| 状态 | 占位 / 心跳文案 |
|---|---|
| 初始占位 | `⏳ 正在处理...` |
| `queued` | `⏳ 任务已排队（等待可用引擎）... (Ns)` |
| `dispatched` / `running` | `⏳ 执行中... (Ns)` |
| `completed` | 替换为最终 `result_text`（不超过 2000 字） |
| `failed` / `cancelled` | 替换为 `❌` + 错误信息 / 超时提示（见 §3.5 错误场景枚举） |

### 3.5 错误场景枚举

| 场景 | 来源 | 可用信息 | 展示文案 |
|---|---|---|---|
| 无可用引擎 | `handler_async.py:714-718`，`_rid=0` | `exit_code=-1`，无 task 行 | 替换占位为 `⚠️ 无可用引擎，请先在 Agent Runtimes 页刷新或配置引擎` |
| 入队失败 | `enqueue_task` 抛异常（无可用引擎/DB 错误等） | Exception message | 替换占位为 `❌ 入队失败: {message}` |
| 执行超时 | `wait_task_terminal` 抛 `TimeoutError`（默认 1800s） | 无 task 行 | 替换占位为 `❌ 执行超时，请稍后重试` |
| 任务执行失败 | task `status=failed`，`exit_code≠0` | `stderr`（前 500 字）/ `errors` / `result_text` | 替换占位为 `❌ 任务执行失败: {stderr 或 result_text[:200]}` |
| 任务被取消 | task `status=cancelled` | `result_text` 可能为空 | 替换占位为 `❌ 任务已取消` |
| 进程异常（direct 模式） | `consume_stream_async` 抛异常 | Exception message | 替换占位为 `❌ 执行异常: {message}` |
| 外层异常（通用兜底） | `_process_chat_inner_async` 外层 `except Exception`（line 803） | Exception message | 替换占位为 `❌ 处理消息时出错，请稍后重试` |
| agent 决定 SKIP | `output == "SKIP"`（line 790） | 无错误 | **不替换占位也不发最终回复**（或静默删除占位） |

> 所有错误场景的占位替换失败时，外兜 `reply_card` 补充发送错误信息（确保用户总能看到最终结果）。

### 3.4 与现有机制的关系

- **不阻塞**：轮询 `await asyncio.sleep(3)` 不耗 CPU、不阻塞事件循环；
- **不动串行锁**：`async with lock` 照旧，心跳在锁内执行（同一条消息处理流程内）；
- **不动会话/知识注入**：prompt 构建、session 复用、knowledge 注入等均不变；
- **兼容飞书限频**：每 3s 一次 PATCH，远低于 5 次/秒上限，无需额外节流；
- **失败静默**：patch 失败不影响最终结果。

---

## 四、改动范围

| 文件 | 改动 |
|---|---|
| `src/im/handler_async.py` | `_process_chat_inner_async`：direct 和 queue 分支改为**统一自定义 poll + 心跳 patch**（占位 → 轮询状态 → 定时更新 → 终态替换）；快回短路（<2s 不产生占位） |
| `src/im/feishu_async.py` | 无需改动（复用现有 `reply_text` + `update_message`） |
| `src/runtime/service.py` | 无需改动（现有 `get_task` 轮询逻辑直接可用） |
| `tests/` | 新增用例：占位→心跳 poll→patch 调用链；快回短路；最终替换；direct/queue 统一行为（见 §五） |

---

## 五、测试用例设计

| 测试类 | 用例 | 覆盖点 / 断言 |
|---|---|---|
| **TestProgressHeartbeat** | `test_reply_sends_placeholder_first` | 收到消息后先 `reply_text` 发占位"⏳ 正在处理..."，返回 reply_id 被保存 |
| | `test_heartbeat_polls_and_patches` | 轮询期间每 3s 调 `get_task` + `update_message`，内容含 status 与秒数 |
| | `test_queued_status_shows_waiting` | status=queued → 占位更新为"已排队"文案 |
| | `test_running_status_shows_elapsed` | status=dispatched/running → 占位更新为"执行中(Ns)" |
| | `test_short_reply_no_placeholder` | 执行 < 2s → 不产生占位消息，直接 reply_card 最终结果（快回短路） |
| | `test_done_replaces_placeholder` | 终态后 `update_message` 被调用替换为 `result_text` |
| | `test_failed_status_replaces_with_error` | task status=failed/exit_code≠0 → 替换为 `❌` + stderr/result_text |
| | `test_cancelled_status_shows_cancelled` | status=cancelled → 替换为 `❌ 任务已取消` |
| | `test_timeout_replaces_placeholder` | `wait_task_terminal` 超时（TimeoutError）→ 替换为超时提示，不卡死 |
| | `test_no_engine_replaces_placeholder` | 无可用引擎 → 替换为「无可用引擎」文案 |
| | `test_patch_failure_falls_back_to_reply_card` | `update_message` 抛错 → 不中断主流程，最终仍发 `reply_card` |
| | `test_direct_and_queue_same_behavior` | direct 模式和 queue 模式都走同一心跳逻辑（mock `wait_task_terminal` / `consume_stream_async`） |
| | `test_skip_output_quietly` | output=="SKIP" → 不替换占位也不发最终回复 |

---

## 六、实施步骤（建议顺序）

| 阶段 | 内容 | 说明 |
|---|---|---|
| P0 | `_process_chat_inner_async`：新增自定义 poll + 心跳 patch 逻辑（占位→定时更新→终态替换） | 独立可测 |
| P1 | 快回短路：执行 < 2s 不发占位消息（直接 reply_card） | 优化快场景 |
| P2 | 测试 + 全量回归 + 部署 new-version2 | |

---

## 七、风险与注意点

- **飞书消息限频**：每 3s 一次 PATCH，远低于 5 次/秒上限，**无需额外节流**；
- **占位消息清理**：终态后占位消息被 patch 替换为最终结果（不残留占位消息），失败时仍 `reply_card` 补充；
- **快回短路**：执行 <2s → 不产生占位，避免快回复时占位闪烁消失；
- **不回归**：现有 `reply_card` 最终回复、会话复用、串行锁、事件存储全部保持不变。

---

## 八、结论

根因是「引擎有流、Chat 侧只取 done」。方案采用**统一心跳轮询**（direct 与 queue 共用一套「占位→3s 心跳→终态替换」逻辑）代替 direct 流式转发——API 调用量少 60-70%、代码更简单、remote queue 也能覆盖、体验已够用（大多数对话 5-30s，心跳可更新 1-5 次）。改动集中在 `handler_async.py`（约 50 行）+ 测试。
