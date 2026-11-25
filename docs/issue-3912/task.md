# Task: Issue #3912

- [x] 实测 weekly-mys 超基线点（3 处，非 Spec 取证树的 5 处——源会话分支作废）
- [x] branch_guard.py 聚合告警 log 兜底 → stderr 兜底
- [x] terminal_disposition.py get_manual_override 吞异常 → log.warning
- [x] a2a_terminal_guard.py 载体 PR 号解析 continue → log.debug 留痕
- [x] 两门禁用例全绿；基线/豁免登记零改动
- [x] 受影响模块回归测试通过
- [x] rebase 到 weekly-mys@885ceface：新基线 11 处超基线点逐点显式化（engine_select/repogit/service/drain_state/insight-public/两个 migration/runtime_id_guard）
- [x] rebase 后两门禁 2 绿；受影响模块回归通过（新增失败均为基线自带）
