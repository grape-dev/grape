# Task: Issue #3758

- [x] T1 编写 spec/plan/task 文档
- [x] T2 新增失败测试（红）：
  - [x] `resolve_work_branch` 不存在分支 → 回退 + note
  - [x] 交办链路不存在分支 → `target_branch` 为回退值 + fallback 日志
  - [x] 不可核实 → 保持原配置
  - [x] webhook/repogit 符号导入冒烟
- [x] T3 实现 repogit `resolve_work_branch` + 分支列表查询/缓存
- [x] T4 webhook 模块级导入 + 收窄异常捕获
- [x] T5 运行相关测试与全量回归
- [x] T6 自审查 + 敏感增量扫描
- [x] T7 创建 PR、Issue 评论、A2A 通知 QA