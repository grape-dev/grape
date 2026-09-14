# Issue #103：知识写入加分布式锁

> Issue: https://gitcode.com/grape-dev/grape/issues/103
> 分支: feat/issue-103-knowledge-distributed-lock
> 基线: weekly-02 (594ecc77)
> 最新提交: eb199362

## 一、问题

集群多实例部署时，知识写入存在并发冲突：

### 场景 1：write_knowledge 并发写同一个 scope

`src/api/knowledge.py` 的 write_knowledge 三步操作都只靠 `db_lock`（threading.Lock）保护，多进程不互斥：

1. **文件追加** `open(file_path, "a")` — 多进程同时追加同一文件 → 内容交错
2. **OBS 上传** `storage.save_bytes()` — 两个实例同时读全文上传 → 互相覆盖，content_hash 对不上
3. **DB 先查再 INSERT/UPDATE** — 两个实例同时查 existing=False，都走 INSERT → 两条记录

调用入口：`src/api/routes/events.py` — agent 事件回调 `POST /{seq}/knowledge`

### 场景 2：_write_to_obs 并发生成 baseline

`src/project_info/builder.py` 的 `maybe_refresh_project_info` 用 `_lock_guard`（threading.Lock）+ `_building`（set）做同进程去重，多实例不互斥。

两个实例同时触发 `build_project_info` → `_write_to_obs` 同时写 OBS + DB baseline 记录 + ref 记录。

触发路径：`src/api/event_pipeline.py` — issue_opened/issue_updated 事件 → threading.Thread 启动 maybe_refresh_project_info

### 场景 3：_sync_baseline_files 并发写 baseline

`src/api/knowledge.py` 的 `_sync_baseline_files` 在 `knowledge_sync_loop` 中每小时执行，虽有 meta 表标记保护，但不如分布式锁可靠。两个实例同时扫描 baseline 文件 → 都走 INSERT → 两条记录。

## 二、方案：生产者-消费者模式

### 核心思路

write_knowledge 永远只写任务表（生产者），不直接写文件。后台 worker 消费任务表（消费者）。同维度内按 created_at 顺序串行处理，不同维度之间并行。

### 两层消费

**第一层：入队方立即消费（同步，零延迟）**

```
write_knowledge():
  1. INSERT 任务到 knowledge_write_queue 表
  2. acquire_lock(同维度 key, ttl=60)
  3. 拿到锁 → 处理（内容去重 + 写文件 + OBS + DB）→ DELETE 任务 → 释放锁 → 返回 task_id
     失败 → _fail_queue_task（更新 last_error + next_retry_at）→ 释放锁 → 返回 task_id
  4. 拿不到锁 → 返回 task_id（不阻塞调用方，任务留 pending）
```

无并发时知识零延迟可读。有并发时抢不到锁就返回，不阻塞。

**第二层：后台 worker 兜底（异步，线程池并行）**

```
每 3 秒轮询：
  1. SELECT pending 任务 LIMIT 50
  2. 按 (repo_key, scope, agent_type, issue_num) 分组
  3. 每组取最老的一条，提交到线程池（4 并发）
  4. 线程池内每个任务：acquire_lock → 再查任务是否存在 → _do_write_knowledge → 成功 DELETE / 失败 pending+retry
  5. 拿不到锁的跳过，留 pending 下一轮
```

覆盖场景：
- 入队方抢不到锁的任务
- 入队方处理崩溃的任务（还是 pending）
- 内容重复（_do_write_knowledge 内 content 去重兜底）

### 异步语义行为变化

write_knowledge 从同步写入改为异步入队 + 第一层尝试消费。行为变化：

- **写入失败不再报 HTTP 500**：原来文件写入失败 → RuntimeError → 500；现在 catch 异常 → _fail_queue_task → 返回 task_id → HTTP 200。任务留 pending 由 worker 重试。
- **线程留痕文案**：从"已沉淀"改为"已提交知识沉淀任务"，诚实反映异步语义。task_id=0（content 空）时不发线程消息。
- **知识可见性延迟**：无并发时第一层同步消费零延迟；有并发时抢不到锁的任务由 worker 3 秒后处理。

这是生产者-消费者模式的固有 trade-off — 异步化后调用方不再知道最终结果。大多数消息队列系统都这样设计。

### 锁设计

| 项 | 值 |
|----|-----|
| 锁 key (write_knowledge) | `knowledge_write:{md5(repo_key:scope:agent_type:issue_num)[:16]}` |
| 锁 key (builder) | `project_info_build:{md5(repo_key)[:16]}` |
| 锁 key (baseline sync) | `baseline_sync:{md5('sync')[:16]}` |
| TTL | write=60s, builder=300s, baseline_sync=300s |
| 实现 | src/lock.py（acquire_lock/release_lock/renew_lock，基于 distributed_locks 表） |
| 锁逻辑 | 应用层统一（MySQL/SQLite 不分叉）：先 INSERT，冲突后查过期，过期才条件 UPDATE |
| 续期 | renew_lock — builder 在 codegraph 探索步骤间每两步续期一次 |

串行靠分布式锁（跨进程互斥），不靠任务状态。任务状态只有 pending/failed：
- pending：待处理（正常状态）
- failed：retry_count >= 100 后标记，不再重试
- 成功的直接 DELETE，不在表里占位

崩溃恢复：任务还是 pending，下一轮 worker 重新捞取。内容去重保证不重复写。

表里只有 pending 和少量 failed，不会膨胀。failed 记录由 knowledge_sync_loop 7 天后清理。

### 内容去重

消费时写文件前先读全文，按行检查是否已有相同 content 的条目：
- 拆分文件按行，检查是否有行 endswith(content) → 已存在 → 不追加文件
- 子串匹配（content in existing_content）会误匹配：content="fix bug" 会命中 "fix bug in auth module" 那行
- endswith 精确到行尾，"fix bug" 不会匹配 "- [seq 1 / coding] fix bug in auth module"

**部分写入恢复**：如果上次文件追加成功但 DB 写失败（抛异常），重试时：
- 文件有 content 但 DB 无记录 → 不追加文件，补写 OBS + DB
- 文件有 content 且 DB 有记录 → 完全跳过，直接 DELETE 任务
- 文件无 content → 正常全流程（追加 + OBS + DB）

判断 DB 有无记录：查 agent_shared_knowledge 表同 repo_key+scope+agent_type+issue_num 的记录是否存在。

注意：marker 格式是 `- [seq N / agent] {content}`，content 在行尾。endswith(content) 精确匹配整条知识文本。

## 三、改动点

### 3.1 新建 knowledge_write_queue 表

文件: `src/api/db_setup.py`

```sql
CREATE TABLE IF NOT EXISTS knowledge_write_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key      TEXT NOT NULL,
    scope         TEXT NOT NULL,              -- project / issue / issue_agent
    agent_type    TEXT NOT NULL DEFAULT '',
    issue_num     INTEGER DEFAULT 0,
    content       TEXT NOT NULL,
    status        TEXT DEFAULT 'accepted',    -- accepted / rejected 业务内容状态
    source_seq    INTEGER DEFAULT 0,
    created_by    TEXT DEFAULT '',
    source        TEXT DEFAULT '',            -- system / agent / manual
    description   TEXT DEFAULT '',
    name          TEXT DEFAULT '',
    queue_status  TEXT NOT NULL DEFAULT 'pending',  -- pending / failed
    retry_count   INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    next_retry_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
)
```

索引（加到 `_create_indexes`）：
```sql
CREATE INDEX IF NOT EXISTS idx_kwq_status_retry ON knowledge_write_queue(queue_status, next_retry_at)
CREATE INDEX IF NOT EXISTS idx_kwq_dim_created ON knowledge_write_queue(repo_key, scope, agent_type, issue_num, created_at)
CREATE INDEX IF NOT EXISTS idx_kwq_source_seq ON knowledge_write_queue(repo_key, source_seq)
```

DDL 用 SQLite 语法，MySQL 后端靠 `mysql_compat.py` 的 `translate_sql` 自动翻译。不用 `ON UPDATE CURRENT_TIMESTAMP`（SQLite 不支持），`updated_at` 由应用层手动 SET。不用触发器（项目其他表一致）。

### 3.2 write_knowledge 拆分

文件: `src/api/knowledge.py`

**write_knowledge（生产者 + 第一层消费者）**:
- 签名不变（调用方无感知），返回值从 str(file_path) 改为 int(task_id)
- 入队前校验：content 空 → 返回 0；scope 非法 → raise ValueError。非法任务不入队
- scope 校验后生成 name/description，存入队列表
- INSERT 任务到 knowledge_write_queue（含已生成的 name/description）
- acquire_lock → 拿到锁调 _do_write_knowledge → DELETE 任务 → 释放锁
  - 失败 → _fail_queue_task → 释放锁 → 返回 task_id
- 拿不到锁 → 返回 task_id（任务留 pending）

**_do_write_knowledge（实际写入逻辑，从原 write_knowledge 提取）**:
- name/description 从队列任务取（入队时已生成），不再重新计算
- status=='rejected' 分支：只写 DB 索引不写文件，在锁内执行避免并发产生两条 DB 记录
- 内容去重（仅 status!='rejected'）：读文件按行 endswith(content) 判断 + 查 DB 记录是否存在
  - 文件有 + DB 有 → 完全跳过
  - 文件有 + DB 无 → 不追加文件，补写 OBS + DB
  - 文件无 → 正常全流程（追加 + OBS + DB）
- 文件追加
- OBS 写入 + content_hash
- DB 先查再 INSERT/UPDATE
- 自动生成 ref

**_enqueue_knowledge_write**:
- INSERT 到 knowledge_write_queue 表，返回自增 id

**_delete_queue_task**:
- 处理成功后直接 DELETE

**_fail_queue_task**:
- retry_count < 100 → queue_status='pending' + last_error + next_retry_at（指数退避 3s→60s 封顶）
- retry_count >= 100 → queue_status='failed'，不再重试
- retry_count 达到 10/50/100 时 log warning

**knowledge_write_worker（第二层消费者）**:
- async 循环，每 3 秒一轮
- CancelledError 单独 catch 并 raise（服务关闭时正常退出）
- SELECT pending + next_retry_at <= ? (Python 计算时间戳传参，不用 datetime('now')，兼容 MySQL)
- 按维度分组，每组取最老一条
- 线程池并行处理（模块级全局 ThreadPoolExecutor, max_workers=4），通过 asyncio.get_running_loop().run_in_executor 集成
- 每个任务：acquire_lock → 再查任务是否存在（可能已被第一层 DELETE）→ 不存在跳过 → _do_write_knowledge → 成功 DELETE / 失败 _fail_queue_task
- 拿不到锁的跳过

**done 记录清理**:
- 成功后直接 DELETE，不需要清理
- failed 记录由 knowledge_sync_loop 7 天后清理（Python 计算时间传参）

**_sync_baseline_files 分布式锁**:
- 加分布式锁 `baseline_sync:{md5('sync')[:16]}`（ttl=300s），防止多实例同时写 baseline
- 在 knowledge_sync_loop 内调用，与原有 meta 表标记双重保护

### 3.3 maybe_refresh_project_info 分布式锁 + 锁续期

文件: `src/project_info/builder.py`

替换 `_lock_guard` + `_building` 为分布式锁：

```python
def maybe_refresh_project_info(repo_key, token=""):
    if not repo_key:
        return
    from src.lock import acquire_lock, release_lock, renew_lock
    lock_key = f"project_info_build:{hashlib.md5(repo_key.encode()).hexdigest()[:16]}"
    if not acquire_lock(lock_key, ttl=300):
        log.info(f"project-info: lock contention for {repo_key}, skipped")
        return
    try:
        if should_refresh(repo_key):
            build_project_info(repo_key, token, lock_key=lock_key,
                             renew_fn=lambda: renew_lock(lock_key, ttl=300))
    except Exception as e:
        log.warning(f"project-info refresh failed {repo_key}: {e}")
    finally:
        release_lock(lock_key)
```

**build_project_info 锁续期**：
- 新增 lock_key + renew_fn 参数
- codegraph 探索步骤间每两步调 renew_fn 续期锁，防止大仓库 build 超 300s 锁过期

删除 `_lock_guard`、`_building`、`threading` 导入。

### 3.4 lock.py 统一应用层锁逻辑

文件: `src/lock.py`

- 去掉 MySQL `INSERT ... ON DUPLICATE KEY UPDATE`（数据库原生语法）
- MySQL/SQLite 统一走应用层逻辑：先 INSERT，冲突后查过期，过期才条件 UPDATE
- 用 Python `time.time()` 代替 DB 函数（UNIX_TIMESTAMP/strftime）
- 新增 `renew_lock(lock_key, ttl)` — 续期锁（只续自己的），返回 False 表示锁已被别人抢走
- 修复 bug：原 MySQL 分支 ON DUPLICATE KEY UPDATE + VALUES() + holder 匹配，同进程第二次抢锁总返回 True

### 3.5 main.py 启动 worker + 优雅关闭

文件: `src/api/main.py`

启动：
```python
_knowledge_write_task = None
try:
    from src.api.knowledge import knowledge_write_worker
    _knowledge_write_task = asyncio.create_task(knowledge_write_worker())
    log.info("Knowledge write worker started")
except Exception as e:
    log.warning(f"Knowledge write worker start failed: {e}")
```

`_knowledge_write_task` 必须赋值持有强引用，防止 GC 回收导致任务静默消失。

关闭：
```python
if _knowledge_write_task:
    _knowledge_write_task.cancel()
    try:
        await asyncio.wait_for(_knowledge_write_task, timeout=5)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
```

与其他 task（_lease_sweep_task、_sweep_task 等）的关闭方式一致。

### 3.6 events.py 返回值适配 + 文案诚实化

文件: `src/api/routes/events.py`

```python
task_id = write_knowledge(...)
if task_id:
    _add_thread_message(seq, ..., f"📌 已提交 {body.scope} 级知识沉淀任务：{body.content}")
return {"ok": True, "scope": body.scope, "task_id": task_id}
```

- 返回值从 file_path(str) 改为 task_id(int)
- 文案从"已沉淀"改为"已提交知识沉淀任务"（诚实反映异步语义）
- task_id=0（content 空）时不发线程消息

## 四、不变的部分

- write_knowledge 函数签名不变（调用方无感知）
- _do_write_knowledge 内部的 DB 先查再 INSERT/UPDATE 逻辑不变（锁内执行，不并发了）
- scene_knowledge_ref 自动生成逻辑不变
- knowledge_sync_loop 主体不变（老数据迁移，不是并发写入）
- 读取路径（load_project_info / collect_knowledge_files / read_knowledge）不变
- _write_to_obs 内部写逻辑不变（锁加在调用方 maybe_refresh_project_info）

## 五、消费流程详图

```
                    生产者                     消费者
                    ------                    ------

  agent 事件回调                           后台 worker（3s 轮询）
        |                                       |
        v                                       v
  write_knowledge()                      SELECT pending LIMIT 50
        |                                       |
        v                                       v
  INSERT 任务到队列                       按维度分组
        |                               (repo_key+scope+agent_type+issue_num)
        v                                       |
  acquire_lock(同维度, ttl=60)                v
        |                               每组取最老一条 → 线程池
   +----+----+                                 |
   |         |                            acquire_lock(同维度, ttl=60)
  成功      失败                              |
   |         |                          +-----+-----+
   v         v                          |           |
  _do_write  返回                       成功        失败
  _knowledge  (pending)                  |           |
   |                                    v           v
   v                               再查任务存在?    跳过
  DELETE                           → 不存在跳过    (留 pending)
  释放锁                            → 存在:
  返回                                _do_write_knowledge
                                      成功 → DELETE
                                      失败 → _fail_queue_task
                                             (pending+retry / failed)
                                      释放锁
```

## 六、确认记录

| 项 | 决策 | 来源 |
|----|------|------|
| 锁 key 粒度 | knowledge_write:{md5(repo_key:scope:agent_type:issue_num)[:16]} | 确认（MD5 压缩防 VARCHAR(128) 溢出） |
| TTL | write=60s, builder=300s, baseline_sync=300s | 确认（30→60 兼容 OBS，120→300 兼容大仓库） |
| maybe_refresh 锁策略 | 去掉 _lock_guard，直接用分布式锁 | 确认（选项A） |
| 抢不到锁策略 | 入队方返回 + 后台 worker 兜底 | 确认 |
| 内容去重 | 按行 endswith(content) 精确匹配 | 确认 |
| 队列存储 | DB 队列（持久化，进程重启可恢复） | 确认 |
| 重试策略 | 99 次 + 指数退避（封顶 60s），第 100 次标记 failed | 确认 |
| 同维度串行 | 分布式锁保证，不引入 running 状态 | 确认 |
| worker 并行 | 线程池 4 并发，不同维度并行 | 确认 |
| 入队立即消费 | 是，第一层同步消费，零延迟 | 确认 |
| done 清理 | 成功后直接 DELETE，failed 7 天后清理 | 确认 |
| 锁逻辑 | 应用层统一（MySQL/SQLite 不分叉） | 确认（不用 DB 原生 ON DUPLICATE KEY） |
| 锁续期 | builder 在 codegraph 步骤间每两步 renew_lock | 确认 |
| 异步语义 | 写入失败不再报 500，线程文案改"已提交" | 确认 |

## 七、深度检查修订（22 项）

| # | 问题 | 修复 |
|---|------|------|
| 1 | rejected 分支没覆盖 | _do_write_knowledge 内部判断 status=='rejected' 走原 rejected 逻辑（只写 DB 索引不写文件），在锁内执行避免并发产生两条 DB 记录 |
| 2 | 第一层消费失败 last_error 丢失 | 第一层消费 catch 异常后调 _fail_queue_task（更新 last_error + next_retry_at），任务留 pending 等 worker 重试 |
| 3 | worker SELECT 后任务可能已被第一层 DELETE | worker 拿到锁后再查任务是否存在，不存在直接跳过，避免处理过期数据 |
| 4 | OBS 上传超时 vs 锁 TTL=30s | TTL 从 30s 提到 60s，兼容 OBS 慢上传场景 |
| 5 | async + ThreadPoolExecutor 集成方式 | worker 用 asyncio.get_running_loop().run_in_executor 提交任务到线程池，不阻塞 event loop |
| 6 | 内容去重子串误匹配 | content in existing_content 改为按行 endswith(content)，"fix bug" 不会误匹配 "fix bug in auth module" |
| 7 | 文件已写但 DB 写失败 → 数据不一致 | 去重同时检查文件和 DB：文件有+DB无 → 补写 DB 不追加文件；文件有+DB有 → 跳过；文件无 → 全流程 |
| 8 | 线程池每轮创建销毁 | 模块级全局 ThreadPoolExecutor(max_workers=4) 复用 |
| 9 | events.py file_path 字段兼容性 | 去掉 file_path 字段（当前调用链确认 agent 客户端不解析），返回 task_id |
| 10 | name/description 生成时机 | 入队时生成好存入队列表，_do_write_knowledge 直接取队列值不再重新计算 |
| 11 | worker CancelledError 被吞 | CancelledError 单独 catch 并 raise，服务关闭时正常退出 |
| 12 | worker 任务被 GC 回收 | _knowledge_write_task 赋值持有强引用，防止静默消失 |
| 13 | scope 校验时机 | 入队前校验 content 空 + scope 非法，非法任务不入队直接报错，worker 不需要再校验 |
| 14 | lock_key 超 VARCHAR(128) | 用 MD5 hash 压缩：knowledge_write:{md5(repo_key:scope:agent_type:issue_num)[:16]}，固定 33 字符 |
| 15 | datetime('now') 在 WHERE 子句 MySQL 不兼容 | Python 计算时间戳传参，SQL 用 ? 占位符，和 lock.py / knowledge_sync_loop 一致 |
| 16 | 无限重试缺少监控 | retry_count 达到 10/50/100 时 log warning |
| 17 | asyncio.get_event_loop() Python 3.10+ 废弃 | 改用 asyncio.get_running_loop().run_in_executor |
| 18 | failed 状态永远不会被设置（清理逻辑死代码） | retry_count >= 100 → queue_status='failed'，不再重试，清理 SQL 生效 |
| 19 | builder TTL=120s 对大仓库可能不够 | TTL 从 120s 提到 300s |
| 20 | _sync_baseline_files 缺分布式锁 | 加 baseline_sync 分布式锁保护（ttl=300s） |
| 21 | worker 关闭时未 cancel | shutdown 时 cancel + wait 5s，与其他 task 一致 |
| 22 | 写入失败不再报 500 + 文案误导 | 文案改"已提交知识沉淀任务"；task_id=0 不发线程消息；builder 加锁续期 renew_lock |

## 八、OBS 模式真实验证结果

在 MySQL + OBS（华为云 obs-grape-dev-file）环境下真实验证通过：

| 测试 | 结果 |
|------|------|
| 表创建 + 索引 | knowledge_write_queue 表 + 3 个索引创建成功 |
| Storage 类型 | ObsStorage |
| 无并发写入 | 文件 + DB + OBS 三处都正确写入，队列任务 DELETE |
| 内容去重 | 重复内容不追加，文件行数=1 |
| 不同内容追加 | 正常追加，文件行数=2 |
| OBS 对象 | key 正确、117 bytes、内容与文件一致 |
| 分布式锁串行 | 第一次抢锁 True、第二次 False、释放后 True |
| read_knowledge | 读到写入的内容 |
