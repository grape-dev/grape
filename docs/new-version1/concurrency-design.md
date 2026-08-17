# 高并发支持设计（Concurrency & Scalability）

> 版本：v1.0
> 日期：2026-08-08
> 状态：待评审
> 前置：`agent-runtime-event-tech-impl.md` / `implementation-plan.md`
> 说明：覆盖任务侧（已有 D9/执行槽/jitter）**之外**的存储与进程侧高并发缺口，聚焦「不引入外部 MQ/Redis」前提下的 SQLite + 多进程 + 有界线程方案。

---

## 一、现状核查

### 1.1 现有并发点（任务侧，设计已覆盖）

| 机制 | 位置 | 说明 |
|---|---|---|
| D9 同 session 串行槽 | `service.claim`（`NOT EXISTS` 活跃 session） | 同 `agent_session_id` 最多 1 活跃 task |
| runtime 执行槽 | `agent.py` `RUNTIME_MAX_CONCURRENT_TASKS=20` | 占满执行槽则不 claim |
| poll jitter 防惊群 | `agent.py` `sleep(poll + jitter)` | 多 worker 错峰轮询 |
| claim 前占槽 | `waitForTaskSlot`（对标 multica） | 防任务卡 `dispatched` |
| event.json 每 repo 锁 + 原子替换 | `event_archive.py` `_lock_for` + `os.replace` | 并发写档案安全 |
| 终态幂等 | `update_task_status` 已终态重放安全 | complete/fail 重放安全 |
| chat 信号量 | `chat.py:12` `_CHAT_AGENT_SEMAPHORE(20)` | 现有 chat 并发上限 |

### 1.2 存储/进程侧缺口（本设计解决）

| # | 缺口 | 位置 | 风险 |
|---|---|---|---|
| C1 | **SQLite 跨进程写冲突**：生产双进程（3000 + 8080）共享 `sessions.db`，`db_lock` 是进程内 RLock，未开 WAL/busy_timeout | `session_manager.py:37-39` | 高并发写 → `database is locked` |
| C2 | **无界执行线程**：`run_pipeline`/`webhook` 每事件 `threading.Thread` 无上限 | `event_pipeline.py:307` / `webhook.py:163` | 事件洪峰线程爆炸 |
| C3 | **claim 原子性未定义**：多 worker 抢同一任务 | `service.claim`（新增） | 重复执行同一 task |
| C4 | **回报写库同步阻塞**：messages/complete 同步写 DB，阻塞 event loop | `service.report_*`（新增） | 高并发回报积压 |
| C5 | **chat 状态轮询读放大**：前端 500ms/任务轮询 | `chat.js`（计划） | 读压力 |
| C6 | **HTTP 连接复用**：RuntimeAgent 每请求新建连接 | `client.py`（计划） | 连接/握手开销 |

> 前提约束：不引入外部 MQ/Redis（D6）。高并发靠 **SQLite WAL + 单写者 + 有界线程 + 异步回报** 达成，量级目标见 §五。

---

## 二、方案设计

### 2.1 C1：SQLite WAL + busy_timeout + 连接策略

```python
# src/session_manager.py 初始化区（迁移前执行）
db = sqlite3.connect(_db_path, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")       # 读写并发，跨进程安全
db.execute("PRAGMA busy_timeout=5000")      # 写冲突等待 5s 而非立即报错
db.execute("PRAGMA synchronous=NORMAL")     # WAL 下足够持久性，吞吐更高
db.execute("PRAGMA foreign_keys=ON")
```

- **WAL**：读写互不阻塞，跨进程安全（替代文件锁）。
- **busy_timeout**：多进程写短暂竞争时等待而非 `database is locked` 直接失败。
- **单写者原则**：所有**写**事务仍过进程内 `db_lock`（维持现状，逐语句粒度不变）；WAL 解决的是**跨进程**写冲突兜底。
- 读路径（GET status/events/detail）不加锁或仅快照读（WAL 允许读不阻塞写）。

> ⚠️ 迁移注意：`journal_mode=WAL` 在每次连接打开时执行；现有 3000/8080 双进程各自连接，均需设置。升级时对旧 `sessions.db` 首启自动迁移，无需人工。

### 2.2 C2：有界执行（本地 worker 线程池）

**替代无界 `threading.Thread`：**

```python
# src/runtime/service.py —— 本地 worker 执行池（进程内）
from concurrent.futures import ThreadPoolExecutor
_worker_pool = ThreadPoolExecutor(max_workers=os.environ.get("RUNTIME_MAX_CONCURRENT_TASKS", "20"))

# 调用方不再 threading.Thread(...)，改为 enqueue_task → 由 worker 线程池消费
```

- `run_pipeline` / `webhook` / `chat` / IM 全部改为 `service.enqueue_task()`（M2 已规划），执行统一由有界池消费。
- 边端 `agent.py` 同样用 `ThreadPoolExecutor(max_workers=max_concurrent)` 替代 `spawn_executor_thread` 无限 spawn。
- 线程池满时：任务保持 `queued`（不进池），由 claim 侧执行槽天然节流。

### 2.3 C3：claim 原子抢单

```sql
-- 原子：仅 queued → dispatched 者胜出，且满足串行槽（D9）
UPDATE agent_tasks
SET status='dispatched', started_at=datetime('now')
WHERE id = (
    SELECT id FROM agent_tasks
    WHERE runtime_id=? AND status='queued'
      AND NOT EXISTS (SELECT 1 FROM agent_tasks a2
                      WHERE a2.agent_session_id=agent_tasks.agent_session_id
                        AND a2.status IN ('dispatched','running'))
    ORDER BY created_at ASC LIMIT 1
) AND status='queued'
RETURNING id, ...;
```

- 单条 `UPDATE ... WHERE status='queued' RETURNING` 保证**多 worker 抢单只有一个成功**（SQLite 串行写 + 条件原子性）。
- 配合 `db_lock`（进程内）+ WAL（跨进程），多进程/多 worker 并发 claim 安全。
- 占执行槽逻辑（`waitForTaskSlot`）在调用端 claim 前判断（`active_tasks(rid) >= max_concurrent` 则跳过）。

### 2.4 C4：回报异步化（消息批量落库）

```python
# src/runtime/service.py —— ingestion 用 asyncio.Queue + 后台批量写
_report_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

async def report_messages(task_id, messages):
    # 立即入队返回（不阻塞 event loop），由批量写 task 落库
    await _report_queue.put(("messages", task_id, messages))

async def _ingestion_loop():
    while True:
        batch = []
        # 攒批：最多 50 条 或 200ms 超时
        batch.append(await _report_queue.get())
        while len(batch) < 50:
            try:
                batch.append(await asyncio.wait_for(_report_queue.get(), timeout=0.2))
            except asyncio.TimeoutError:
                break
        with db_lock:
            for kind, tid, payload in batch:
                _apply_report(kind, tid, payload)   # append_event_archive_log / update_task_status
            db.commit()
```

- 回报（messages/progress/usage）**入队即返回**，后台线程批量写库，避免阻塞 FastAPI event loop。
- `complete`/`fail`（终态、低频）仍同步处理，保证强一致。
- 队列满（>2000）时降级：直接同步写（兜底，不丢数据）。
- FastAPI 启动时 `lifespan` 创建 `_ingestion_loop` task，关闭时 `await _report_queue.join()` 排空。

### 2.5 C5：轮询节流

- 前端 `chat.js` 轮询间隔 500ms → **1s + 随机 jitter（±200ms）**，避免 N 任务同时段打满。
- `GET /api/runtime/tasks/{id}/status` 只读快照，WAL 下读不阻塞写。
- 后续（M5）升级 SSE 推送替代轮询。

### 2.6 C6：HTTP 连接复用

- `client.py` 用 **`aiohttp.ClientSession`**（已在依赖）单实例复用连接 + `TCPConnector(limit=10)`，替代 `requests.post` 每次新建。
- 心跳/claim/回报共用同一 session，降低 TLS/连接握手开销。

---

## 三、服务端线程/进程模型总览

```
┌─ 进程 1: main API (3000) ────────────────────────────┐
│  FastAPI async（不阻塞）                               │
│  ├─ /api/runtime/* claim/report（异步，report 入队）    │
│  ├─ _report_queue → _ingestion_loop（批量写库）        │
│  ├─ _worker_pool（本地 worker，ThreadPool 20）          │
│  └─ SQLite 连接（WAL + busy_timeout + db_lock 单写）    │
├─ 进程 2: webhook (8080) ─────────────────────────────┘
│  ├─ run_pipeline → enqueue_task（不再无界 spawn）      │
│  └─ 同 SQLite（WAL 跨进程写安全）                      │
└─ 远端 RuntimeAgent（worker 机）───────────────────────
   ├─ aiohttp.ClientSession 连接池 → 服务端 /api/runtime/*
   └─ ThreadPoolExecutor(max_concurrent) 执行 stream_runtime
```

**原则**：FastAPI event loop 永不阻塞在 DB 写 / 子进程执行上；本地 worker 走线程池；DB 跨进程靠 WAL + busy_timeout 兜底。

---

## 四、实施位置与里程碑归属

| 项 | 落地文件 | 里程碑 |
|---|---|---|
| WAL + busy_timeout + 连接参数 | `src/session_manager.py` | **M0**（独立、可回滚、纯增强） |
| 无界线程 → enqueue_task（线程池消费） | `event_pipeline.py` / `webhook.py` / `chat.py` / `im/*` | M2（随切 enqueue 一并） |
| claim 原子 `UPDATE...RETURNING` | `src/runtime/service.py` | M1 |
| 回报异步化 `_ingestion_loop` | `src/runtime/service.py` + `main.py` lifespan | M2 |
| 边端线程池 | `src/runtime/agent.py` | M3 |
| aiohttp 连接池 | `src/runtime/client.py` | M3 |
| 前端轮询节流 1s+jitter | `src/api/static/js/pages/chat.js` | M4 |

---

## 五、性能目标与压测验收

| 指标 | 目标 | 验收方式 |
|---|---|---|
| 入队吞吐 | ≥ 100 events/s（单机） | 压测脚本注入 webhook 批量事件，观察 `agent_tasks` 增长无锁错误 |
| 并发写库 | 无 `database is locked` 错误（WAL+busy_timeout） | 双进程 3000+8080 同时写，monitor 日志无 lock 异常 |
| 回报批量落库 | messages 批量 50 条/200ms 落库 | 观察 `_ingestion_loop` 批大小与积压深度 |
| claim 单点正确 | 并发 N worker claim → 无重复 dispatched | `test_runtime` 多线程 claim 用例 |
| 远端并发 | 每 worker `max_concurrent=20`，多 worker 水平扩展 | 压测远端 worker 集群 |
| chat 轮询 | 500 任务同时轮询 1s 间隔无超时 | UI 压测 |

---

## 六、风险与回滚

| 风险 | 应对 |
|---|---|
| WAL 切换影响存量库 | 首启自动迁移（`PRAGMA journal_mode=WAL` 幂等）；回滚则删 `-wal/-shm` 后缀文件 + 恢复连接参数 |
| ingestion 队列积压 | 队列满降级同步写（不丢数据）；监控 `_report_queue.qsize()` |
| claim 条件竞争 | 原子 `UPDATE...RETURNING` + 幂等终态兜底（重放安全） |
| 线程池拒绝 | 任务保持 queued 由执行槽节流，不丢弃 |
