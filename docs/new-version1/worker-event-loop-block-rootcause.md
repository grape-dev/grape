# Worker 事件循环阻塞根因分析与修复方案（任务卡 dispatched / 日志停更）

> 版本：v1.2
> 日期：2026-08-15
> 状态：待评审
> 变更：
> - v1.2 记录「引擎池 + 直连引擎优化」（main 03a4abc9 起）对本方案的影响：服务端派发层改动不影响 worker 执行模型，
>   #704 根因（事件循环阻塞）依然存在；文档 §九 补充影响分析。
> - v1.1 记录 #704 复现——方案 1（`await asyncio.sleep(0)`）在「长命令静默无输出」场景下失效，
>   根因是 `stream_runtime` 生成器阻塞在读 stdout 时无让出点；补充测试覆盖盲区分析与方案 2（线程化）为推荐修复。
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

---

## 八、v1.1 复现：方案 1 在「长命令静默无输出」下失效（2026-08-15 #704）

### 8.1 现象（hello-html2，worker 123.249.12.67）

| 时间 | 事件 | 证据 |
|---|---|---|
| 18:30:59 | #704 入队（agent=issue, cwd=`issues/2/issue`, runtime=1, 新 session） | `Task enqueued: t_e29cb41c12dd runtime=1` |
| 18:31:54 | 服务端 claim → #704，标记 dispatched + started_at | `claim: runtime_id=1 task=t_e29cb41c12dd` |
| 18:31:54→18:32:19 | worker 无任何 #704 处理日志 | 全程无 `[seq=704]` |
| 18:32:19 | worker 再次 claim → 拿到 #705（coding），非 #704 | `claim: runtime_id=1 task=t_17d99100cbde` |
| 18:32:29→18:33:30 | worker claim 间隔 60 秒（#705 长命令执行中） | access log 间隔 |
| 18:33:55 | `recover stale dispatched: 1 tasks` → #704 回收为 queued（120s 无 start） | 服务端日志 |
| 之后 | #704 queued，worker 持续执行 #705→#706→#707… 未再领取 #704 | worker 日志 |

**与 seq499 同源**：worker 事件循环被前序任务的同步生成器阻塞 → claim/heartbeat 停止 → 新任务滞留 dispatched → 服务端 120s 回收。区别于 seq499，本次已确认**与目录结构/session 复用/权限/网络抖动均无关**（#703 同结构 completed 验证了目录改造成功）。

### 8.2 关键证据：claim 间隔不均匀 = 事件循环被阻塞

- 正常轮询应每 3s 一次（poll_interval）；
- 实测 #703 执行中 claim 间隔 25s、#705 执行中 60s；
- 间隔拉长点恰好落在长任务执行窗口 → **claim 循环未按节奏调度 = 被同步迭代占住**。

### 8.3 方案 1（`await asyncio.sleep(0)`）为何失效

方案 1 的让出点位于 **`stream_runtime` 每次 `yield` 之后**：

```python
for evt_type, data in stream_runtime(...):
    ...
    await asyncio.sleep(0)      # 仅当生成器产出事件时才执行
```

- **当 opencode 执行长命令（git push / curl / 脚本）且数秒~数十秒无 NDJSON 输出时**，生成器停在
  `executor.py` 的 `proc.stdout.readline()` 阻塞读取（`_run_once` 的 `while True` 循环）；
- **没有 `yield` → 没有 `await` 让出点** → 整个 asyncio 事件循环被 `readline()` 阻塞；
- 因此方案 1 只能缓解「有输出事件时」的阻塞，**无法解决「长命令静默等待」期间的事件循环停摆**。

### 8.4 测试覆盖盲区

`tests/test_event_loop_block.py` 的 `_slow_stream_runtime` 模拟**每个事件间 0.02s 短暂阻塞且持续有输出**：

```python
for i in range(5):
    time.sleep(0.02)      # 短阻塞，但每次都 yield
    yield ("step_start", ...)
```

- 该 mock **未模拟「长命令静默无输出」**（生成器长时间停在 readline 上、无 yield）；
- 因此测试通过（heartbeat 持续增长），但真实场景（opencode 静默等待长命令输出）仍复现阻塞。

### 8.5 修复方向（待实施）

1. **方案 2 落地（推荐）**：`_execute_task` 中 `stream_runtime` 的同步迭代放入**后台线程**（`asyncio.to_thread` + 队列，或 `ThreadPoolExecutor`），生成器内 `readline()` 阻塞不占事件循环 → claim/heartbeat 持续调度。
2. **补测试**：新增 mock 模拟「长命令静默无输出」（生成器 `time.sleep(长)` 后才有输出，期间无 yield），断言事件循环中的 heartbeat/claim 计数器仍增长，覆盖方案 1 失效场景。
3. 保持方案 3（服务端 dispatched 超时回收）作为兜底。

---

## 九、v1.2 引擎池 + 直连引擎优化对本方案的影响（2026-08-15，main 03a4abc9 起）

### 9.1 优化内容（另一会话，未部署到生产，生产仍 62c1764b）

| 改动 | 说明 |
|---|---|
| 新增 `devops_agent_runtime_binding` + `src/runtime/engine_select.py` | `pick_engine`（专属→全局→本机直连）+ `resolve_default_engine` |
| 废除 `queue_local` | 归并为 `direct`（本机就地执行） |
| 弃用 claim-based 引擎池 | `agent_type_runtime_pool` / `pool_agent_type` 不再用于派发 |
| 执行通道收敛 | `direct \| queue_remote` |
| 移除硬编码 opencode 兜底 | a2a / pipeline 均改 `pick_engine`，无引擎直接 `engine_unavailable` |

### 9.2 对本方案的影响评估

| 影响面 | 结论 |
|---|---|
| a2a_event 的 engine_id | 由 `pick_engine` 确定，不再可能出现 `runtime_id=0` 入队（无引擎直接跳过）→ 消除了「0 任务卡 queued」这类上游诱因 |
| worker 端 `agent.py` / `executor.py` | **零改动**（62c1764b..HEAD 无提交）→ #704 根因（事件循环阻塞）**依然存在** |
| claim 的 dispatched 120s 回收 | 最新 `service.py` claim 仍保留 `recover stale dispatched` → 兜底仍在 |
| 方案 2（线程化 stream_runtime） | **依然必要且有效**——引擎池优化是服务端派发层，不触及 worker 执行模型 |

### 9.3 结论

- **引擎池优化与当前方案无冲突**，但**不解决 #704 根因**；
- #704 根因是 worker 侧 `agent.py` 执行模型（事件循环阻塞），引擎池优化未触碰；
- 文档 §八 的方案 2（线程化 `stream_runtime`）**是必要的独立修复**，不受引擎池优化影响；
- 生产部署引擎池优化后（`pick_engine` 生效），可消除「runtime_id=0 卡 queued」类问题，但「worker 阻塞导致 dispatched 超时回收」仍需方案 2 根治。

*本文为根因分析与修复方案，待评审后实施。*
