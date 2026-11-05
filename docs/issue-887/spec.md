# Issue #887 Spec — 技能随 agent 绑定下发（启动前一致性校验/来源标注/健康检查覆盖）

> 权威 Spec 见 Issue #887 评论（id=198134334，Issue Agent 发布，developability=ready）。
> 本文件为本地开发基线摘录，目标分支 `weekly-mys`。

## Objective
修复技能下发链路：技能随 agent 绑定（仓库级/场景级）下发；任务启动前校验「即将执行的
skill」与绑定一致，不一致拒绝执行并显式报错；任务日志显式标注技能来源；引擎本地缓存
仅兜底；健康检查纳入 skill 一致性。

## 根因
events seq=46108 实测：agent 绑定 skill=caserun-prep，任务落到全局池引擎后实际执行
skill=casegen-prep（工位 `.skills` 本地缓存残留的另一条流水线技能），静默跑错流程并
跳过 caserun-prep 规定的 Windows 环境硬校验。

## Scope
1. 派发 payload 携带按 agent 绑定解析的 skill（`service.enqueue_task` env `GRAPE_BOUND_SKILL`）。
2. 执行侧前置一致性校验（worker `_skill_consistency_gate` + direct 端 `_execute_agent`/scheduler）。
3. 日志标注技能来源（`task_log_stream.build_skill_source_message`，绑定值 vs 实际值）。
4. 引擎本地缓存仅绑定为空时兜底；绑定非空未物化且缓存存在其它技能 → fail-fast 拒绝。
5. 健康检查新增 skill 一致性校验项（`health.check_skill_consistency`）。

Out of Scope：#875（落点锁定）、#657（user 级技能解析为空兜底）、#653（engine_runtime_ids 生效）。

## Acceptance Criteria
- 绑定 skill=caserun-prep 时实际执行 skill 非 caserun-prep 则拒绝执行并报错（`[skill_mismatch]`）
- 任务日志显式标注技能来源（绑定值 vs 实际值）
- 引擎本地缓存不覆盖仓库级/场景级绑定（仅兜底）
- 健康检查包含 skill 一致性校验项
- 不再静默跑错流程/静默跳过环境硬校验
- 现有测试通过 + 新增测试（seq=46108 为回归用例）

## 关键设计决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 不一致时语义 | fail-fast 拒绝执行 | 静默跑错流程正是本 bug 根因 |
| 校验位置 | 服务端派发校验（既有 #630 门禁）+ 执行侧前置硬校验兜底 | 执行侧是最后防线 |
| #617 声明回退 | 保留（服务端明示 skill_fallback），mismatch=True 显式标注 | 非引擎本地缓存决定，有 #617 既有语义与测试 |
| #607 FALLBACK 恢复 | 保留（本地无其它可跑偏技能时放行，source=degraded_recovery） | 该路径实际执行仍为绑定值（权威取回） |
