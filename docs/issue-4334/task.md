# Task: Issue #4334

- [x] T1 新增 `watchdog_redispatch_guard`：终态/external-block/重派/context 判定
- [x] T2 接入 `a2a_terminal_guard.guard_terminal_dispatch`
- [x] T3 接入 `patrol_compensate` 终检预检与重派标记/context 保护
- [x] T4 新增测试：终态预检、上限拦截、context 校验、留痕
- [ ] T5 回归：相关既有测试全通过
- [ ] T6 自审查（5 轴 + pre-pr 增量检查）
- [ ] T7 创建 PR 并通知 QA