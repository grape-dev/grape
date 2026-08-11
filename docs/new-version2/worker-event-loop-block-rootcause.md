# Worker 事件循环阻塞根因分析与修复方案（任务卡 dispatched / 日志停更）

> 版本：v1.0
> 日期：2026-08-11
> 状态：待评审
> 触发背景：gitcode-pr-assistant 多 Issue 并发时，远端 worker（123.249.12.67）日志停更、新任务（#8 seq499 vod）卡在 `dispatched` 无法执行；此前被误判为「claim 响应丢失竞态」，经深入日志验证实为 **worker 侧 asyncio 事件循环被同步生成器阻塞**。

---

## 〇、现象回顾

| 现象 | 详情 |
|---|---|
| worker 日志停更 | 跑 #7 coding 期间日志从 13:08:23 后不再增长（实际是长任务迭代无新 NDJSON） |
| 新任务卡 dispatched | #8 seq499（vod, issue_comment）：13:03:48 入队 → 13:07:53 才被 claim（延迟 4 分钟）→ 此后无执行，永久 dispatched |
| 多任务 complete 异常 | #7 vod/issue 的 complete 重试间隔 5 分钟（13:02→13:07），远超指数退避上限（64s） |
| worker 心跳消失 | 13:03:45-13:08:00 期间远端 worker 到服务器**零 heartbeat**（应每 15 秒一次） |

---

## 一、证据链（逐步排除错误假设）

### 1.1 排除「网络抖动 / claim 响应丢失」

- 服务器 13:03:45-13:08:00 收到远端 worker 请求**仅 2 个**（13:07:53 的 `messages` + `claim`），**无 heartbeat**；
- 若仅单次网络抖动，heartbeat 不会连续 4 分钟消失——事件循环整体停摆；
- 其它本地 worker（127.0.0.1，runtime 4/5）13:03:47 起每 3 秒正常 claim——服务器本身正常。

### 1.2 排除「服务器超时」

- 服务器进程 11:58:34 启动，13:07 无重启；
- 服务器 health 200，其它任务正常执行。

### 1.3 核心证据：worker 事件循环被同步迭代占住

- worker 13:03:51 启动 #7 coding（`opencode run`），13:03:51-13:07:53 连续跑 Step 1-12；
- **期间 `_run_loop` 的 heartbeat/claim 全部无法执行**（心跳 0 次、claim 仅 13:07:53 一次）；
- 13:07:53 恰好是 #7 coding **batch 攒满 50 条**触发 `await report_messages` 的让出点——事件循环短暂恢复，才补发了 claim/messages。

---

## 二、根因：`stream_runtime` 同步生成器阻塞 asyncio 事件循环

### 2.1 问题代码（`src/runtime/agent.py:105-129`）

```python
async def _execute_task(client, task):
    ...
    for evt_type, data in stream_runtime(     # ← 同步生成器
        prompt, cwd, session_id=..., event_type=...,
        backend_name="opencode", ...
    ):
        ...
        batch.append({"type": evt_type, "data": data})
        if len(batch) >= 50:                  # ← 仅每 50 条才 await
            await client.report_messages(tid, batch)
            batch = []
```

- `stream_runtime`（`src/runtime/executor.py:19`）是 `yield from stream_agent(...)` 的**同步生成器**；
- 在 `async def _execute_task` 中**同步 `for` 迭代**，每次 `next()` 可能阻塞（读取 opencode stdout NDJSON）；
- **batch 攒到 50 条之前没有任何 `await`** → 整个 asyncio 事件循环被同步 for 循环占住。

### 2.2 阻塞影响面

`_run_loop`（`src/runtime/agent.py:142-174`）与 `_execute_task` 跑在**同一事件循环**：

```
_run_loop:  while True:
                await client.claim(rid)      ← 被同步迭代阻塞，无法执行
                await client.heartbeat(...)  ← 同样被阻塞
_execute_task: for ... in stream_runtime(...)  ← 同步占住事件循环
```

- **claim 轮询停止** → 新任务无法被及时领取（seq499 延迟 4 分钟）；
- **heartbeat 停止** → 服务器侧看 worker 失联；
- 多任务并发时（#7 coding 长任务 + #8 vod 新任务）问题暴露。

### 2.3 为什么 seq499 卡 dispatched 且不执行

```
13:03:48  seq499 入队（queued）
13:03:51  worker 启动 #7 coding（同步迭代占住事件循环）
13:03:51-13:07:53  claim 轮询被阻塞，seq499 无法被领取
13:07:53  #7 coding batch 满 50 → await → 事件循环让出
          → _run_loop 补发 claim → 服务器置 seq499 dispatched
          → 但 worker 随即回到 #7 coding 同步迭代
          → seq499 的 _execute_task 无法启动（task_start await 排队）
          → seq499 永久 dispatched
```

### 2.4 为什么单任务时"正常"

- 单任务无并发：事件循环被阻塞但**没有新任务需要领**、**没有并发 heartbeat 观察点** → 无感知；
- 多任务并发（一个长 coding + 一个新 vod 入队）才暴露——**隐藏 bug 的典型特征**。

---

## 三、与既有根因的关系

| 根因 | 归属 | 状态 |
|---|---|---|
| 每 Issue 共用 opencode session → cwd 错位（`vod-agent-hang-rootcause-fix.md` 方案 A） | 已修复（commit 3c86294） | ✅ |
| **Worker 事件循环被同步生成器阻塞**（本文） | 方案 1（`await asyncio.sleep(0)`）已实施 + 回归测试 `tests/test_event_loop_block.py` | ✅ |

两者独立：session 复用导致单个任务上下文错位；事件循环阻塞导致**并发下新任务无法领取/执行**。seq499 卡 dispatched 属于后者，**不是** session 复用或 claim 响应丢失。

---

## 四、修复方案

### 方案 1（核心）：`_execute_task` 迭代让出事件循环

`src/runtime/agent.py:105-129` 的同步 for 循环中，**每次迭代 `await asyncio.sleep(0)`**：

```python
for evt_type, data in stream_runtime(...):
    ...
    batch.append(...)
    if len(batch) >= 50:
        await client.report_messages(tid, batch)
        batch = []
    await asyncio.sleep(0)      # ← 每次让出，避免阻塞事件循环
```

- 让 `_run_loop` 的 claim/heartbeat 在长任务执行期间持续运行；
- 长任务每次事件处理都让出，其他 async 任务（claim/heartbeat）可调度。

### 方案 2（兜底）：将 `stream_runtime` 同步迭代放到线程池

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

loop = asyncio.get_running_loop()
executor = ThreadPoolExecutor(max_workers=2)
# 在线程中迭代 stream_runtime，事件循环不被阻塞
for evt_type, data in await loop.run_in_executor(executor, lambda: list(stream_runtime(...))):
    ...
```

> 注意：`stream_runtime` 是惰性生成器，`list()` 会一次性跑完——不适合长任务。更优做法是用 `asyncio.to_thread` 包住生成器 + 队列，或直接用方案 1 的 `await sleep(0)`（更简单）。

### 方案 3（防御）：heartbeat 独立任务 + 超时告警

- heartbeat 移到**独立 asyncio task** 或使用 `asyncio.wait_for` 包裹，避免被 `_execute_task` 连带阻塞；
- 服务器对「dispatched 超时（如 >10min）且无心跳」的任务自动置回 `queued`（recover-orphans 定时化），兜底孤儿任务。

### 推荐

**方案 1（`await asyncio.sleep(0)` 每次迭代让出）+ 方案 3（服务器 dispatched 超时回收）**：
- 方案 1 根除事件循环阻塞（长任务期间 claim/heartbeat 正常）；
- 方案 3 兜底任何极端情况下的孤儿 dispatched 任务。

---

## 五、改动点清单

| # | 文件 | 改动 | 方案 |
|---|---|---|---|
| 1 | `src/runtime/agent.py:105-129` | 同步 for 循环每次迭代 `await asyncio.sleep(0)` | 1 |
| 2 | `src/runtime/agent.py`（或独立任务） | heartbeat 独立化或 `wait_for` 保护 | 3 |
| 3 | `src/api/routes/runtime.py` / `service.py` | 新增 dispatched 超时扫描：`started_at` 超时且无心跳 → 置回 queued | 3 |

---

## 六、验证方案

1. **复现**：两个并发任务（长 coding + 新 vod 入队）：
   - 修复前：vod 任务延迟数分钟才被 claim，heartbeat 中断；
   - 修复后：vod 任务 <10s 被 claim，heartbeat 每 15s 持续；
2. **回归**：`tests/test_runtime.py`、`test_runtime_protocol.py`、`test_a2a_queue.py`、`test_claim_response_lost.py` 全量通过；
3. **新增测试**：模拟 `_execute_task` 长迭代，断言事件循环中 claim/heartbeat 不被阻塞（方案 1 回归）。

---

## 七、结论

- **根因**：`_execute_task` 用同步生成器（`stream_runtime`）迭代且仅 batch≥50 才 await，**阻塞整个 asyncio 事件循环**；
- **表现**：多任务并发时新任务无法领取/执行（卡 dispatched）、心跳中断、日志停更；
- **修复优先级**：方案 1（让出事件循环）→ 方案 3（dispatched 超时回收兜底）。

*本文为根因分析与修复方案，待评审后实施。*
