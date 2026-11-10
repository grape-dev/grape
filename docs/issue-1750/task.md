# Task: Issue #1750

- [x] T1 服务端接收可选 `active_task_ids`，命中白名单优先 `skipped_active`。
- [x] T2 返回体补充 `skipped_active_reason`，日志可观测。
- [x] T3 `RuntimeClient.recover_orphans` 支持白名单 body，缺省空 body 兼容旧服务端。
- [x] T4 `_reregister` 的 `recover_orphans` 默认 False（调用方显式声明）；启动路径在 `main()` 中直接空白名单回收。
- [x] T5 周期重注册不回收；rid 集合变化时携带 `active_tasks` 白名单回收。
- [x] T6 新增 `tests/test_issue_1750_recover_orphans_active_whitelist.py` 覆盖 A1–A6。
- [x] T7 回归 `test_recover_orphans_*`、`test_issue_417_*`、`test_issue_1495_*` 全通过。
- [x] T8 心跳失败重注册复用 `_recover_orphans_on_rid_change` 判定，不再默认空白名单无条件回收；新增 `test_a5_heartbeat_failure_reregister_keeps_whitelist`。
- [x] T9 新测试接入 CI linux-backend job；文档与测试文件补末尾换行。
