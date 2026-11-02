# Spec: Issue #607 — 技能物化失败时写入 FALLBACK.md 降级指引 + 失败留痕

> 已由 issue agent 发布并确认（见 Issue #607「Spec Document」评论），本文为归档副本。
> Developability: ready | 排期: weekly-09 | 标签: agent-feedback/enhancement/P1/triaged/spec-ready

## Objective

技能物化失败时（zip 直连/代拉/文本物化均失败，或物化结果不含 SKILL.md），向
`DEVOPS_SKILLS_DIR/<skill>/` 写入 FALLBACK.md 降级指引文件（含技能名、失败原因、
`grape skills detail --skill <name>` 权威取回命令），并记录结构化失败日志
（`log.warning`，tag=skill、技能名、失败阶段、原因）。Agent 首轮即可读到降级指引，
无需多轮 find/试错；失败留痕支撑间歇性失败率统计。

## Scope

- worker 端：`src/runtime/agent.py` `_execute_task` 技能物化失败分支（zip 物化失败、
  skill_files 为空、外层异常）
- direct 端：`src/api/event_pipeline.py` `_materialize_direct_workdir` 失败分支
  （degraded、外层异常）
- 兜底函数单一实现两端复用（`src/runtime/skill_materialize.write_skill_fallback`，
  两侧零依赖共用模块，无循环导入风险）
- FALLBACK.md 口径与任务下发头 [Skill Materialization Notice] 一致（均为
  `grape skills detail` 取回）

## Out of Scope

- 物化失败根因修复（OBS 稳定性等）——仅加兜底与留痕
- project-info / knowledge 物化降级（已有三级降级）
- `grape skills detail` CLI 新增（已存在）
- #605 / #606 各自独立处理

## Acceptance Criteria

1. skill_obs_url 失败 + skill_files 为空：`.skills/<skill>/FALLBACK.md` 存在且含
   技能名与 `grape skills detail` 取回指引
2. zip 解压后无 SKILL.md（空 zip）：同样触发 FALLBACK.md
3. direct 端技能物化失败：ws/.skills/ 下出现 FALLBACK.md
4. 失败日志可检索：`log.warning` 含 skill 名、stage、失败原因（tag=skill）
5. FALLBACK.md 与 Materialization Notice 取回口径一致（均为 `grape skills detail`）
6. 物化成功路径不受影响
7. 新增测试覆盖以上失败分支
