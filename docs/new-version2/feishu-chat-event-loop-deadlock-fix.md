# Feishu Chat 事件循环死锁修复方案（asyncio.Lock）

> 状态：方案（v0.1，待评审）｜ 分支：new-version2
> 现象：2026-08-16 用户在飞书切换到其它仓库（hello-calculator3）后连续提问，服务彻底无响应。

## 1. 事故现象

- 19:49 用户切仓库并连续发两条同仓库消息（绑定意图 + "当前有哪些issue"）。
- 19:49-19:53 agent 在**后台线程**运行（opencode 耗时 ~4 分钟），19:53:04 完成（有日志）。
- 此后**整个服务永久卡死**：`/health` 超时、日志静默 ~1.5h、DB `sessions.db-wal` 冻结在 19:49。

## 2. 根因（py-spy 确证）

`src/im/handler_async.py:425-426`：在 **asyncio 事件循环线程里做同步 `threading.Lock.acquire()`**。

```python
lock = _acquire_repo_lock(repo_key)     # threading.Lock，每仓库一个（src/api/event_store.py:12）
lock.acquire()                          # ★ 同步阻塞（事件循环线程内！）
try:
    await _process_chat_inner_async(...)   # agent 可能跑数分钟（consume_stream_async 在线程池）
finally:
    lock.release()
```

**死锁机制**：
1. 消息1 拿到 repo 锁 → `await` agent（agent 在线程池跑，完成后经 `loop.call_soon_threadsafe` 投递事件）。
2. 消息2（同仓库）到达 → `lock.acquire()` **同步占死事件循环线程**。
3. 消息1 的 `await` 需要事件循环继续才能收到 agent 完成事件 → 永不完成 → 永不 release → 消息2 永远阻塞。**循环永久卡死**。

**py-spy 证据**：主线程堆栈停在 `process_chat_async (src/im/handler_async.py:426)` = `lock.acquire()`。

**为何以前不炸**：同仓库两条消息极少重叠；agent 一旦耗时数分钟且用户连发两条同仓库消息即触发。属潜伏的 event-loop 死锁。

## 3. 修复方案（彻底：asyncio.Lock）

把同步锁改为 **asyncio.Lock**，`async with` 获取，事件循环不被阻塞。

```python
# src/api/event_store.py
import asyncio
_repo_locks = {}                          # repo_key -> asyncio.Lock
_repo_locks_lock = threading.Lock()       # 仍用于并发建锁（普通锁即可，瞬时）

def acquire_repo_lock(repo_key: str) -> asyncio.Lock:
    with _repo_locks_lock:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = asyncio.Lock()
        return _repo_locks[repo_key]
```

```python
# src/im/handler_async.py
lock = _acquire_repo_lock(repo_key)
async with lock:                          # await，不阻塞事件循环
    await _process_chat_inner_async(...)
```

### 语义一致性
- 同一仓库 chat 仍串行执行（持锁至 agent 完成），并发语义与现状完全一致。
- `asyncio.Lock` 不可重入——当前就是"获取一次/释放一次"，无差异。
- `async with` 在协程取消时自动释放，比 `try/finally` 更稳。

### 使用面
- `acquire_repo_lock` 当前**仅 Feishu chat 使用**（grep 确认：`src/im/handler_async.py:18/425`），无其它同步调用方，改造安全。
- uvicorn 每进程单事件循环，`_repo_locks` 每进程独立，无跨循环问题。

## 4. 性能影响（结论：无负面影响，优于所有替代）

| 场景 | 开销 |
|---|---|
| 无竞争（绝大多数） | `async with` = 检查布尔标志，O(1)、无系统调用、无线程切换，纳秒级 |
| 有竞争（同仓库并发） | 协程挂起等锁，**零线程占用**，循环继续服务其它请求/健康检查/WS |
| 内存 | 每仓库一个 asyncio.Lock 对象，与现状 threading.Lock 同等量级 |

### 与替代方案对比
| 方案 | 竞争行为 | 性能 |
|---|---|---|
| 现状（同步 acquire） | **死锁、整机卡死** | 灾难性 |
| `asyncio.to_thread(lock.acquire)` | 每条等待消息各占线程池 worker（默认 ~32），并发多时可能**耗尽线程池**，拖累 stream_agent 等 run_in_executor | 每次还多一次线程跳跃 |
| **asyncio.Lock（本方案）** | 协程挂起，零线程占用 | 无竞争 O(1)，**最优** |

## 5. 实施步骤

1. **恢复服务**：`systemctl restart grape`（当前已卡死）。
2. **改代码**：
   - `src/api/event_store.py`：`_repo_locks` 改存 `asyncio.Lock`。
   - `src/im/handler_async.py:425-426`：`lock.acquire()`/`finally release()` → `async with lock:`。
3. **清理**：可选在 release 后清理 `_repo_locks` 空条目（次要，内存极小）。
4. **测试**（用例设计见 §5.1）。
5. **验证**：压测同仓库连发消息 + 期间 `curl /health` 必须持续 200；agent 完成后两条消息均收到回复。

### 5.1 测试用例设计

新增 `tests/test_feishu_chat_unified.py`（复用既有隔离 DB 约定）：

| # | 用例 | 前置 | 断言 |
|---|---|---|---|
| D01 | `acquire_repo_lock` 返回 asyncio.Lock 且同仓库复用同一实例 | 调用两次 `acquire_repo_lock("r1")` | 两次返回 `is` 同一对象；`isinstance(lock, asyncio.Lock)` |
| D02 | 不同仓库返回不同锁 | `acquire_repo_lock("r1")` / `acquire_repo_lock("r2")` | 两个实例 `is not` |
| D03 | 同仓库串行：并发两任务持锁依次执行 | `async with` 包两个递增计数器任务，仓库相同 | 计数最终 =2，且执行区间不重叠（记录时间戳，两段不相交） |
| D04 | 同仓库并发期间事件循环不被阻塞 | 任务1 `async with lock` 内 `await asyncio.sleep(0.1)`，任务2 同时等锁；期间启动"心跳"协程每 5ms 计数 | 任务2 最终完成；心跳协程在整个等待期间持续递增（循环未被占死） |
| D05 | 不同仓库并行不互斥 | 两个不同仓库任务各 `async with` + `sleep(0.1)` 并发 | 总耗时 ≈ 0.1s（并行），而非 0.2s（串行） |
| D06 | 协程取消安全释放 | 任务 `async with lock` 内被 `cancel()` | 取消后锁可被后续任务正常获取（无泄漏） |
| D07 | `process_chat_async` 同仓库连发消息不卡死（集成） | mock `_process_chat_inner_async` 慢速（sleep 0.2s）；连续投递两条同仓库消息 | 两条均被处理（各自执行一次），期间事件循环可响应其它任务 |
| D08 | 锁实例清理（若实施 §5 步骤 3） | 处理完成后再次 `acquire_repo_lock` | 无残留锁条目（字典大小不变或已清空） |

> 对照旧行为：D04 在 `threading.Lock` + 事件循环下即触发死锁（等锁同步阻塞循环），D07 为该事故的最小复现。

### 5.2 回归测试面

- `tests/test_feishu_handler.py`、`tests/test_chat_handler.py`、`tests/test_feishu_chat_unified.py`、`tests/test_lark_md.py`（asyncio.Lock 改动不涉及这些用例，确保不回归）。

## 6. 附加项

- 诊断用 `py-spy`（已在服务器 `pip install --break-system-packages` 安装）可在完成后卸载。
- `_repo_locks` 无清理逻辑（现状即有，次要）。
