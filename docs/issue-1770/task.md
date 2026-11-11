# Task: Issue #1770

- [x] T1 新增 `a2a_dispatch_guard`：场景/节点顺序、下游 running 判定、时间窗去重、活跃重复判定、原子落库、短窗告警（含跳过计数）、跳过留痕
- [x] T2 派发入口接入门禁（下游 running → 幂等窗口 → 活跃重复 → 短窗告警），保留 `force=true` 语义
- [x] T3 跳过写 `event_skipped`（reason 可读，含引用 seq）
- [x] T4 测试：下游 running 跳过/恢复、时间窗丢弃/过期、失败重试、活跃重复拦截、force 越过、告警、窗口键维度、并发原子落库
- [x] T5 回归验证：既有 A2A 去重/排队/幂等/跨流水线测试通过
- [x] T6 文档：`docs/issue-1770/{spec,plan,task}.md`