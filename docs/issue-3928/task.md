# Issue #3928 Task Breakdown

- [x] T1 后端能力探针（backend_capability）：映射 + which + TTL 缓存 + probe→状态映射。
- [x] T2 存储：agent_runtimes 熔断/能力列（DDL + 迁移 202611240003）。
- [x] T3 worker 注册/心跳上报能力；服务端注册/心跳落库、缺 CLI 立即摘除并告警。
- [x] T4 认领闸门收紧：fail/failed/spawn_failed/degraded/unavailable/healthy=0 拒绝；
      正常/unknown 实例零回归；池可达性统一。
- [x] T5 后端失败重派 + runtime 熔断 + 半开试探 + 成功恢复 + 上限 2 判死告警。
- [x] T6 跨引擎 WARNING + task/runtime/engine + 计数 + 按 task 去重。
- [x] T7 17641 根因结论与规避措施（注册/心跳 skip 未降级 + 旧闸门短路）。
- [x] T8 四类场景测试 + 回归测试全绿。