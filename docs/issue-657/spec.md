# Spec: Issue #657 — 场景引用技能 scope 不匹配时静默解析为空导致 timeout_failed

> 来源：user feedback #284（提交人 huangshulin）| P1 bug | 基线分支 weekly-08-mys

## 问题
repo 级场景引用 user 级技能（scope=user）时：
- 场景 apply 阶段无校验，apply 成功；
- 派发阶段 `resolve_skill_id` 在 repo/official scope 找不到该技能 → skill_id=0 / skill 解析为空，仍继续派发；
- 任务进入 `pending_self_report`，1800s 后 `timeout_failed`，全程无指向「scope 不匹配」的报错。

## 目标（In Scope）
1. 场景 apply（含保存）校验：节点引用技能 scope 与场景 scope 匹配（repo 场景可引用 repo/official 级技能），不匹配直接报错，文案含技能名与 scope 值。
2. 派发兜底：派发前 skill 解析为空/不可解析 → 立即产生 `skill_resolve_failed` 错误事件（含场景 ID/节点/技能名/原因），任务不再派发（非 1800s 超时）。
3. 回归用例：早期 smoke 场景（skill scope=user → repo 场景）。

## Out of Scope
技能上传/管理流程、跨 org scope 语义重构、历史 events 数据清洗。

## 验收标准
1. repo 级场景引用 user 级技能，apply 直接失败，错误含「scope 不匹配」+ 技能名 + scope 值。
2. apply 漏过的场景派发时 skill 解析失败 → 立即错误事件、任务 failed（非 1800s 超时）。
3. events 表不再出现 skill="" / 不可解析技能仍被派发到 pending_self_report 的记录。
4. 早期 smoke 场景回归覆盖。
5. 错误事件文案含「scope 不匹配」及技能名/scope 值。
6. 现有测试全通过 + 新增测试覆盖。
