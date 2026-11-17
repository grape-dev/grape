# Spec: Issue #2816 — 看门狗重复唤醒已终态载体的 folded 单

## 目标
在 A2A 派发入口增加「终态判定短路」：当 Issue 已带 `needs-human` 标签，或载体 PR
已带 `needs-human-terminal` 评论标记行时，不再向 coding 派发，直接把 Issue 置人工态。

## 范围
- 派发 coding 前读取 Issue 标签 + 载体 PR 评论标记行，命中即短路。
- folded + 载体 terminal 的单据 stage 置人工态，退出 coding 自动循环。
- `CONFLICT_SCOPE=intersect` 场景提供人工处置入口：指定新载体 / 改派 / 关闭。

## 非目标
- 不改变 `needs-human` / `needs-human-terminal` 标记的产生逻辑。
- 不重构 B1/B11-fallback 机制本身。
- 不处理 #2218/#5140 的 issue 节点重触发问题。

## 验收
- 构造 folded+terminal 单：超阈值后不再派发 coding，事件留 `event_skipped` 审计。
- folded+terminal 单的 issue 状态置 `awaiting_input`、stage 置人工态，巡检不再判为断链而复活派发。
- 载体优先级端到端生效：显式/meta 新载体存在时，旧文本载体与旧标签不再拦截；新载体再次终态照常短路。
- 人工处置（new_carrier/reassign）后持久恢复：下一次非 force 派发不再被旧 `needs-human` 标签置回人工态。
- `CONFLICT_SCOPE=intersect` 时人工处置入口可用（new_carrier / reassign / close）。
- 正常单据（无终态标签/标记）行为不变；`force=true`、非 coding 目标、开关关闭均不短路。
- 新增测试覆盖纯判定、短路、人工处置、载体优先级（resolve+evaluate 联合）、人工越过持久化。