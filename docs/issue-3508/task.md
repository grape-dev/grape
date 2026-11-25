# Tasks: Issue #3508

- [ ] T1 后端：`_scene_engine_status` 结构化扩展（engines/low_score_nodes/reason）
- [ ] T2 后端：engine-status 路由异常降级（2xx + degraded/error，不 500）
- [ ] T3 后端：update_scene QA 节点提取修复 + 校验解包防护 + 悬空 id 落库清洗
- [ ] T4 前端：IssuesView 创建弹窗双指标告警 + 错误兜底重试
- [ ] T5 前端：DashboardView 流程配置引擎健康双指标 + 重试 + 弹窗健康标签
- [ ] T6 测试：Python 回归（健康分不足/物理不可达/degraded/清洗）+ JS 静态断言
- [ ] T7 自审查（5 轴 + pre-pr 增量检查）→ commit → PR → 评论 → A2A QA
