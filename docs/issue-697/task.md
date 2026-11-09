# 任务拆解（Issue #697）

- [x] T1 scheduler：AUTOPILOT_DEDUP_RULE 常量 + _build_autopilot_prompt 注入 + parse_findings dedup 字段透传
- [x] T2 routes/autopilot.py：polish prompt 注入规则；dispatch 疑似重复 owner 确认门禁（409）+ 确认标记落库
- [x] T3 prompt_builder：issue summary 注入处追加根因判重守护规则
- [x] T4 前端：issue_patrol 模板 instruction 重写 + findings 疑似重复徽标 + zh/en 文案
- [x] T5 测试：pytest（三历史案例 fixture 回归 + 门禁 + 透传）+ node 前端模板测试
- [x] T6 自审查（5 轴 + pre-pr 增量检查）→ 提交 → PR
