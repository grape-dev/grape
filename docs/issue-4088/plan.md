# Issue #4088 方案设计

## 落点

沿用 `event_active_guard` 的既有模式（原子抢占 + TTL 兜底 + 终态语义），新增
评论派发守卫表与配套 helper，插入 `run_pipeline` 评论分支与 `redispatch`。

## 1. 数据层：`comment_dispatch_guard` 表

```sql
CREATE TABLE IF NOT EXISTS comment_dispatch_guard (
    repo_key    TEXT NOT NULL,
    agent_type  TEXT NOT NULL,
    comment_id  TEXT NOT NULL,
    event_seq   INTEGER NOT NULL DEFAULT 0,
    task_id     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (repo_key, agent_type, comment_id)
)
```

- 注册：`src/api/db_setup.py` DDL 列表 + `src/kernel/db.py` `_RUNTIME_TABLES`。
- key = (repo_key, agent_type, comment_id)：同一评论对不同 Agent（订阅派发）
  各自独立一次；`event_seq` 为首派事件 seq（幂等命中回显与同 seq 恢复），**不是
  主键的一部分**。原草案「event_seq+comment_id 复合键」与实现不符，以本口径为准：
  同一评论在窗口内被编辑（新 seq）按设计一并去重。
- #4085 覆盖范围：仅覆盖 **cid 抽取稳定**的幂等键/投递指纹变体；cid 本身变化时
  视为不同内容标识，不在守卫覆盖范围（需内容判重等其它防线）。

## 2. helper（src/domains/runtime/event_store.py，经 api 别名导出）

- `COMMENT_DISPATCH_IDEMPOTENT_REASON = "duplicate_comment_dispatch"`
- `comment_dispatch_guard_window_seconds()`：env
  `GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS`，默认 3600.0，<=0 关闭；
- `claim_comment_dispatch_guard(repo_key, agent_type, comment_id, event_seq=0)`：
  同一 db_lock 临界区内先删过期行再 `INSERT OR IGNORE`，rowcount=1 即抢占成功
  （跨线程/跨进程原子；`event_seq` 在抢占时一次写入，无需回填）；
- `find_comment_dispatch_guard_hit(repo_key, agent_type, comment_id)`：窗口内
  命中返回 `{event_seq, task_id, created_at}`，过期/缺失返回 `{}`；
- `release_comment_dispatch_guard_by_seq(seq)`：按首派事件 seq 释放守卫，仅用于
  「接管跳过」等未真正派发的路径（终态/成功派发后守卫必须保留以维持窗口去重）；
- `comment_dispatch_guard_allows_redispatch(repo_key, agent_type, comment_id, seq)`：
  重派判定——无命中/同 seq → 允许；跨 seq 命中 → 拒绝并返回命中 seq；
- `purge_stale_comment_dispatch_guards()`：窗口过期行清理，与
  `purge_stale_event_guards` 同口径**排除运行中 task**（`agent_tasks.status IN
  ('dispatched','running')` 且 `event_seq` 命中），挂到既有周期调用点
  （api/main.py、api/server_tasks.py）。

## 3. 派发层接入（event_pipeline.run_pipeline 评论分支）

- **在接管守门（`_skip_dispatch_for_takeover`）之后**、引擎选择之前：此时事件已
  落库得到 `seq>0`：
  - claim 失败（命中）→ `append_skipped_event(detail="comment_id=… idempotent_hit_seq=…")`，
    `result` 回显 `idempotent=True` / `idempotent_hit_seq`（读不到 seq 时回显
    `idempotent_hit_seq_unknown=True`）/ `skipped="duplicate_comment_dispatch"`，
    直接 return（不建任务段）；
  - claim 成功 → 继续既有分派流程；
- claim 后若接管变为激活（`_skip_dispatch_for_takeover` 在 `_create_direct_task`
  再次命中）→ 置 `skipped_takeover` 并同步 `release_comment_dispatch_guard_by_seq`，
  不留残留守卫；
- 引擎不可用/入队失败等失败路径**保留**守卫（事件行已建，唯一恢复通道是
  同 seq 重派，守卫同 seq 放行）。

## 4. 重派接入（routes/events.py `_redispatch_event`）

- 在 1.0a 自环守卫之后：评论族事件（复用
  `event_pipeline._COMMENT_EVENT_TYPES` 单一常量）抽取 cid，命中守卫且 seq 不同
  → 409 `duplicate_comment_dispatch` + `idempotent_hit_seq`；同 seq/无命中放行。

## 5. 测试 seam

公开行为级测试（不测内部细节）：
- helper 层：窗口（窗内命中/窗外放行/窗口关闭）、并发（多线程同键仅一次成功）、
  TTL 清理与运行中 task（运行中不清理、终态/无任务清理）；
- pipeline 层：守卫命中短路（不建事件段 + 出参回显）、守卫命中但读不到 seq 回显
  unknown、接管跳过不留残留守卫（重投不被吞）；
- redispatch 层：跨 seq 命中拒绝、同 seq 放行。

## 风险与回退

- 守卫只作用于 `_COMMENT_EVENT_TYPES` 且 cid 非空，其它事件零影响；
- 窗口可 env 关闭（<=0），线上异常时秒级回退；
- 不改既有 `comment:<cid>` 唯一索引与内容判重语义，纯增量防线。
