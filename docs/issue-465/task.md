# Task: Issue #465

- [x] T1: 红测试 tests/test_issue_465_runtime_device_name.py（name 优先/空回退/本机 host_name 三断言）
- [x] T2: dashboard.py runtimes 查询加 rc.name、行构造远端取 rc.name or device_name
- [x] T3: 核对 observability.js 渲染与 _filterRuntimes 字段一致性
- [x] T4: 运行相关回归测试（test_issue_417 / test_issue_436 js 等）
- [x] T5: 自审查 + 敏感增量扫描 + 提交推送 + 建 PR + 评论 + A2A QA

## 追加（用户澄清 + SRE 根因定位后的遗留对齐点）

- [x] T6: 红测试 tests/js/test_issue_465_worker_card_name.js（Worker 卡片名称 name 优先/空回退/本机不变/排序口径一致/渲染全链路/排序稳定，对未修复代码 3 红）
- [x] T7: observability.js `_workerCardHTML` 标题与 `_sortedWorkers` 排序键换 name 优先口径（新增 `_workerDisplayName` helper）
- [x] T8: 关联字段核查（runtimes 筛选下拉 / _devIpMap / _timelineDevIpMap）——均与后端原始字段绑定，无需改动，结论记录在 plan.md
- [x] T9: observability 全量 js 回归 + #465/#417 后端回归
- [x] T10: 更新 docs（plan.md 追加修复记录）+ 提交推送 + 评论 + A2A QA
