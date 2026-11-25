# Issue #4088 需求文档：派发层评论事件重试/重放幂等（event_seq + comment_id）

## 目标

issue_comment / note_hook_pr 事件的**派发层重试/重放**以 `comment_id`（同一评论
对同一 Agent）为幂等键，并记录首派 `event_seq`：先查后派，命中已有派发记录即
复用/短路，不再新建任务段；幂等命中在事件/任务出参中回显 `idempotent=true` 与
命中事件 seq（命中但读不到 seq 时回显 `idempotent_hit_seq_unknown=true`，不伪造 0）。

## 幂等键口径（与实现对齐）

- 守卫表主键为 `(repo_key, agent_type, comment_id)`；`event_seq` 是首派事件 seq
  **列**（用于命中回显与同 seq 自身恢复），**不参与主键**。原草案写的
  「event_seq+comment_id 复合键」与实现不一致，以本口径为准。
- 同一评论在同窗口内被编辑（新 seq 的合法事件）按设计一并去重——目标是「同一
  评论对同一 Agent 只送达一次」，而非按 seq 各送一次。
- 对 #4085「幂等键变体重放」的覆盖范围：**cid 抽取稳定**的变体可被拦截
  （幂等键/投递指纹不同但 comment_id 相同）；若 cid 本身变化（内容标识改变），
  守卫无法识别为同一条评论，不在本守卫覆盖范围内。

## 现状与缺口

- 事件级 `comment:<id>` 幂等键（events.idempotency_key 唯一索引）只能拦截
  「同 cid 再建新事件」；派发层（建任务段）无独立的先查后派守卫：
  - cid 抽取在不同投递间可能不同源（见 #4085）时，唯一索引拦不住重放；
  - 内容判重窗口默认仅 1s，重试/重放集中窗口（分钟级）远超该窗。
- 幂等命中无出参回显：被拦投递调用方拿不到 `idempotent` / 命中 seq，
  Agent 侧无法核对，只能重复回执。
- 重派（redispatch）对评论事件无同 cid 跨 seq 守卫。

## 期望行为

1. 派发前（且**在接管守门之后**）以 `(repo_key, agent_type, comment_id)` 原子抢占
   派发守卫 `comment_dispatch_guard`（先查后派；窗口内命中 → 短路，不建事件、
   不建任务段）；接管跳过的评论事件不抢占守卫，并在接管跳过时同步释放残留守卫；
2. 守卫行记录首派 `event_seq`（命中回显用）与 `created_at`（窗口判定用）；
3. 窗口（`GRAPE_COMMENT_DISPATCH_GUARD_WINDOW_SECONDS`，默认 3600s，<=0 关闭）
   内同一评论对同一 Agent 只送达一次；窗口过后放行重新派发；
4. 幂等命中路径出参回显：`result["idempotent"]=True`、
   `result["idempotent_hit_seq"]=<命中事件 seq>`、`result["skipped"]="duplicate_comment_dispatch"`，
   并写 `event_skipped` 留痕（detail 含 comment_id 与命中 seq）；
5. 重派侧：对评论事件，守卫命中且 `event_seq` 不同（跨 seq 重放）→ 拒绝重派
   （409 + idempotent 信息）；同 seq（本事件自身恢复）→ 放行。

## 验收

- 同一 comment_id 在派发失败自动重试/重放后仅生成 1 个任务段；
- 重试计数与实际派发次数解耦（errors 可见 N 次重试，派发守卫只放行一次）；
- 幂等命中路径有可观测字段（idempotent 标记 / 命中事件 seq）；
- 重试窗口（窗口内短路、窗口外放行、窗口关闭守卫旁路）与并发抢占
  （多线程同键仅一次成功）有测试覆盖。
