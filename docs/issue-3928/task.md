# Issue #3928 Task Breakdown

- [ ] T1 后端能力探针（backend_capability）：映射 + which + TTL 缓存 + probe→状态映射。
- [ ] T2 存储：agent_runtimes 熔断/能力列（DDL + 迁移 202611230005）。
- [ ] T3 worker 注册/心跳上报能力；服务端注册/心跳落库、缺 CLI 立即摘除并告警。
- [ ] T4 认领闸门收紧：fail/failed/spawn_failed/degraded/unavailable/healthy=0 拒绝；
      正常/unknown 实例零回归；池可达性统一。
- [ ] T5 后端失败重派 + runtime 熔断 + 半开试探 + 成功恢复 + 上限 2 判死告警。
- [ ] T6 跨引擎 WARNING + task/runtime/engine + 计数 + 按 task 去重。
- [ ] T7 17641 根因结论与规避措施（注册/心跳 skip 未降级 + 旧闸门短路）。
- [ ] T8 四类场景测试 + 回归测试全绿。