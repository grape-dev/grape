# Task: Issue #4185

- [x] 定位 PR 订阅触发门槛（1.5 预匹配 / 主路由 / 出站台账）
- [x] 新增 `_pr_target_branch_changed` 目标分支变化判定
- [x] 早期 1.5 预匹配纳入目标分支变化
- [x] 主路由 PR 订阅纳入目标分支变化
- [x] 出站台账放行判定纳入目标分支变化
- [x] 新增四类回归用例：目标分支切换无 commit、分支未变无 commit、merged、跨流水线移交
- [x] 回归测试与 lint / arch 检查
