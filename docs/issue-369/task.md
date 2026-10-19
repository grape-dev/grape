# Tasks: Issue #369

- [x] 盘点 service.py 全部置 failed 路径及事件回写现状（见 plan.md 表格）
- [x] 新增 `_sync_event_failed` 统一回写 helper（对齐 `_fail_event` 语义）
- [x] 三处仅写 status 的判死路径接入完整回写（unreachable / unhealthy / 回收超限）
- [x] 全仓盘点补齐同类缺口：executor 停机判死、recover-orphans 封顶判死（plan.md #9/#10）
- [x] 幂等验证：终态保护下双主重复回写安全（测试 UT-D/UT-E）
- [x] 新增测试 8 例全部通过；#253 引擎绑定回归 6 例通过
- [x] 自审查（5 轴：正确性/安全/性能/可维护性/测试）通过
- [x] 提交 feat/issue-369 → PR base=weekly-07-clean-mys
