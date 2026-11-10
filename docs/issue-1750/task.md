# Task: Issue #1750

- [x] T1 服务端接收可选 `active_task_ids`，命中白名单优先 `skipped_active`。
- [x] T2 返回体补充 `skipped_active_reason`，日志可观测。
- [x] T3 `RuntimeClient.recover_orphans` 支持白名单 body，缺省空 body 兼容旧服务端。
- [x] T4 `_reregister` 增加 `recover_orphans` 开关；启动路径保持空白名单回收。
- [x] T5 周期重注册不回收；rid 集合变化时携带 `active_tasks` 白名单回收。
- [x] T6 新增 `tests/test_issue_1750_recover_orphans_active_whitelist.py` 覆盖 A1–A6。
- [x] T7 回归 `test_recover_orphans_*`、`test_issue_417_*`、`test_issue_1495_*` 全通过。