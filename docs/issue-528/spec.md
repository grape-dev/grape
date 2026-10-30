# Spec: Issue #528 — PR 订阅场景 ISSUE_NUMBER 注入非数字值

> 来源：Issue Agent Spec（评论区发布）| Developability: ready | 开发基线：weekly-08-mys

## Objective
修复 PR 订阅场景（PR 无关联 Issue）触发任务时服务端把 `ISSUE_NUMBER` 注入非数字占位符 `pr_{PR号}_{scene_id}` 的问题；为技能 runbook 补充 `ISSUE_NUMBER` 非数字/为空时的降级路径。

## Scope
- In：`src/api/event_pipeline.py` 两处 env 注入点（`_enqueue_pipeline_task` / `_execute_agent_inner`）纯数字校验 + `PR_NUMBER` 独立注入（`_pr_sub_pr_number` 兜底）；`build_task_env`（重派路径）同口径防御；qa 技能 runbook 降级路径；新增测试。
- Out：伪 `issue_id` 机制重构；PR 订阅引擎选择/多场景分发；A2A 接口放宽数字校验。

## Acceptance Criteria
- [x] 无关联 Issue 的 PR 触发任务时，Agent env 不再出现非数字 `ISSUE_NUMBER`（不注入）
- [x] 该场景下 `PR_NUMBER` 为纯数字 PR 编号
- [x] runbook A2A 步骤的降级路径（PR 评论回帖替代 A2A）已文档化
- [x] 关联 Issue 的 PR 事件 `ISSUE_NUMBER` 仍注入真实数字编号（不回归）
- [x] 新增测试覆盖上述场景；相关既有测试通过

## 设计决策（摘 Spec）
| 决策 | 选择 | 理由 |
|------|------|------|
| 无关联 Issue 时 ISSUE_NUMBER | 不注入该变量 | 与「A2A 缺 issue_number 不调用」约定一致；避免空串被 `if _issue_num` 误判 |
| PR 号承载变量 | 独立 PR_NUMBER | 语义清晰 |
| 校验位置 | env 注入时校验 | 伪 id 参与去重/会话键，不动事件流转；注入点收敛为统一 helper |
