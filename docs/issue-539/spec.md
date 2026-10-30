# Spec: Issue #539 — 流水线摘要区分待人工确认与阶段执行完成，关联 SRE 验收结果

> 来源：Issue #539 Spec Document（评论 id 197061424）｜目标分支：`weekly-09-ljt`｜评级：ready

## Objective
`issues pipeline`（及对应展示）在阶段执行状态之外，明确暴露需求级 `pending_review`
待人工确认状态，并在 SRE 阶段摘要中提供可授权查询的任务关联（task_ref）与真实验收
结论引用（result_ref）。避免仅看流水线误认为需求与生产发布已完成（真实样本 #428：
stage=pending_review 而顶层 status=completed）。

## Scope (In)
- pipeline 汇总新增需求级业务状态字段（`issue_stage` / `issue_task_status` / `review_status`），
  与阶段执行状态（顶层 `status` = 事件执行结束）区分；
- 各阶段（尤其 SRE 段）`output` 新增 `task_ref` + `result_ref`（引用+脱敏摘要，非全量内嵌）；
- 无结果/不可读/待处理用显式枚举（`available` / `pending` / `unavailable`）标注，
  不泄漏受限日志/凭据；
- 文档（help/api/11-issues.md）与前端（issues.js 详情卡）语义澄清；
- 回归测试：pending_review / 验收失败 / 取消 / 生产已完成 / 生产尚未开始 + 旧字段兼容性。

## Out of Scope
- 不改变现有阶段执行状态字段的取值与语义（兼容性硬约束）；
- 不自动续接生产发布；不做 runtime task 存储结构重构。

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 状态暴露方式 | 新增独立业务状态字段（非改 status 语义） | 旧字段语义不变是硬约束 |
| 结论呈现方式 | 引用+摘要（task_ref/result_ref），非全量内嵌 result_text | 摘要可控、避免泄漏，授权后经 query_path 查全量 |
| 不可读/无结果处理 | 显式枚举（unavailable/pending），非空值/省略 | 空值无法与「尚无数据」区分 |

## Acceptance Criteria
- [x] pending_review 场景 pipeline 可识别「待人工确认」且 SRE 段含验收结论或结果引用；
- [x] 验收失败/取消/生产已完成/生产尚未开始回归测试通过；
- [x] 旧字段语义与结构不变，新增字段可选，旧客户端无破坏；
- [x] 无结果/不可读/待处理显式标注，不泄漏受限日志或凭据（token 脱敏）；
- [x] 所有现有测试通过（基线已存在的 13 个失败与本次改动无关，已用 HEAD 对照验证）。
