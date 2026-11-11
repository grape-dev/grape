# Spec: Issue #1753 — 跨流水线转交后 Spec 头部目标分支未随承接流水线基线刷新

> 开发基准分支：`weekly-mys`（承接流水线运行时 `$GRAPE_WORK_BRANCH`）。源 Spec 的 weekly-10 已作废。

## 目标
跨流水线转交时，目标分支以承接流水线的权威基线（`work_branch` / 运行时 `$GRAPE_WORK_BRANCH`）为准，
避免承接侧按源会话固化分支开发而落错基线。

## 范围
- 转交链：`_cross_pipeline_handoff` 在 A2A `carried_context` 中补/覆盖 `target_branch`，并留痕原值。
- 承接侧：`issue` Agent prompt 注入承接基线校验指引（比对 Spec 头部分支，不一致纠正并留痕）。
- 转交上下文与审计事件显式携带目标分支。

## 非目标
- 场景标识绑定（#1745）问题；coding Agent 开发流程改造。
- 外部 `grape-skills` 仓库模板文本（单独仓库提交）。

## 验收
- [x] 转交后 `carried_context.target_branch` 等于承接场景 `work_branch`
- [x] 源会话旧分支被覆盖时保留 `target_branch_replaced` 留痕
- [x] A2A 上下文与 cross_scene_handoff 事件携带目标分支
- [x] 承接侧 issue Agent prompt 含基线一致性校验指引
- [x] prompt 仅当 `target_branch_source == "target_scene_work_branch"` 时声明权威基线；
      缺失/未知来源标注 `UNCONFIRMED`，不得按旧分支生成 Spec
- [x] 端到端断言：派发前现读承接场景 `work_branch`（改值为 X → prompt 出现 X），
      而非沿用转交快照（覆盖 `build_devops_agent_prompt` 注入路径）
- [x] 转交快照与派发时基线漂移时，prompt 提示以派发时值为准并留痕
- [x] 场景 work_branch 查询收敛为 `session_store` 公共 helper（webhook/repogit/prompt_builder/event_pipeline）
- [x] 同基线转交不产生额外改写；测试通过
- [x] 第三轮：A2A 基线声明以 `target_branch_source == TARGET_BRANCH_SOURCE_SCENE_WORK_BRANCH`
      为门；未确认时输出 UNCONFIRMED，不得把旧分支宣称为运行时权威
- [x] 第三轮：多跳链承接场景无 `work_branch` 时失效历史权威标记，并同步清除上一跳
      `target_branch_replaced`，prompt / A2A 留痕口径一致
- [x] 第三轮：`target_scene_work_branch` 与 `unconfirmed_no_target_scene_work_branch`
      统一提为 `session_store` 共享常量
- [x] 第四轮：运行时基线取值源改为派发时现读承接场景 `work_branch`
      （`session_store.get_scene_work_branch`），`os.environ['GRAPE_WORK_BRANCH']` 仅作可选覆盖，
      修复 API 进程无该变量导致漂移保护空转
- [x] 第四轮：A2A 基线声明前置，并新增独立字段 `handoff_baseline_note`；
      派发 evt 的 `_a2a_context` 携带完整 `_handoff_context`，避免长上下文截断丢失