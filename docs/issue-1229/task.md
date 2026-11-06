# 任务拆解：Issue #1229

| # | 任务 | 产出 | 验证 |
|---|------|------|------|
| 1 | 红测试：端点不存在 → 404（T1）；建端点骨架 → 200 空结构（T2 空数据转绿第一步） | tests/test_issue_1229_efficiency_board.py | pytest 先红后绿 |
| 2 | 绿实现：聚合端点（issue 提出/完成 + token 归属 + bot 排除 + 仓库隔离 + 鉴权） | src/api/routes/dashboard.py | T2-T7 全绿 |
| 3 | 红测试：i18n 键缺失（zh/en + 消费侧） | tests/js/test_issue_1229_efficiency_board.js | node 先红 |
| 4 | 绿实现：前端「能效看板」子 Tab + 渲染 + 空态 + i18n（zh/en） | observability.js + locales zh/en | node 转绿 |
| 5 | 回归：既有 dashboard/observability 相关测试全量通过 | — | pytest 0 fail |
| 6 | 自审查（5 轴）+ 前置增量检查（黑名单拦截 + 敏感扫描） | — | 两条通过 |
| 7 | push + 建 PR（base=weekly-10）+ Issue 评论 + A2A QA | PR | 链路完整 |

## 测试切片（一竖切片一测试一提交，红/绿分提交）
- 切片 A（端点契约）：T1 404→路由存在；T2 空库返回空 contributors（空态数据形态）。
- 切片 B（聚合正确性）：T3 单协作者多 issue（提出/完成计数）；T4 多协作者排序与 limit。
- 切片 C（token 归属）：T5 usage→task→issue 创建人回链求和；口径不含 reasoning；孤儿 task 丢弃。
- 切片 D（边界与安全）：T6 bot 排除 + 子任务排除 + created_by 空回退 assignee；T7 未带 repo 参数空态；T8 匿名 401；T9 仓库隔离（种 A/B 仓库只统计 A）。
- 切片 E（前端）：T10 i18n 键 zh/en 对齐 + inspect.subs 注册；T11 渲染：KPI/表格/空态/转义。
