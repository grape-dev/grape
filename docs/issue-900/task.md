# Task: Issue #900（R2）

- [x] T1 list_milestones 新增 page/per_page/title Query 参数（可选、钳制、按需透传）
- [x] T2 新增测试：透传 / title 过滤 / 钳制 / 默认兼容 / OpenAPI 契约断言 / 非法值 422
- [x] T3 回归 pytest（76 passed，1 基线既有失败与本改动无关）
- [x] T4 QA 复审 3 项修复核验：(a) 钳制非 422 (b) 契约测试 (c) 无参不透传
