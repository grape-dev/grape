# Plan: Issue #1753

## 方案
1. `src/api/routes/webhook.py`
   - `_cross_pipeline_handoff` 场景/节点校验后，直接调用
     `session_store.get_scene_work_branch(repo_key, scene_id=target_scene_id)` 读取承接基线，
     写入 `carried_context.target_branch` + `target_branch_source`；源会话旧值不同时写
     `target_branch_replaced` 留痕。
   - 承接场景无 `work_branch` 且历史来源为权威标记时，失效为共享常量
     `TARGET_BRANCH_SOURCE_UNCONFIRMED_NO_SCENE_WORK_BRANCH`，并同步清除上一跳
     `target_branch_replaced`，保证多跳链留痕口径一致。
   - A2A 基线段仅在来源为 `TARGET_BRANCH_SOURCE_SCENE_WORK_BRANCH` 时宣称权威，否则标
     UNCONFIRMED；声明前置到 `context` 前部，并以独立字段 `handoff_baseline_note` 承载，
     派发 evt 的 `_a2a_context` 携带完整 `_handoff_context`。
2. `src/api/session_store.py`
   - 新增 `resolve_scene_id` / `get_scene_advanced_options` / `get_scene_work_branch`
     公共 helper，收敛 webhook / repogit / prompt_builder / event_pipeline 的重复查询。
   - 共享常量 `TARGET_BRANCH_SOURCE_SCENE_WORK_BRANCH` 与
     `TARGET_BRANCH_SOURCE_UNCONFIRMED_NO_SCENE_WORK_BRANCH`，webhook / prompt_builder 统一引用。
3. `src/prompt_builder.py`
   - 新增 `_build_cross_pipeline_baseline`：读取 `carried_context`，为 issue Agent 生成
     基线一致性校验指引（比对 Spec 头部、不一致纠正并留痕）。
   - 仅当 `target_branch_source == TARGET_BRANCH_SOURCE_SCENE_WORK_BRANCH` 时声明为权威基线；
     缺失/未知来源标注 `UNCONFIRMED`，明确「不得作为权威」。
   - 派发时基线改为现读承接场景 `work_branch`（`session_store.get_scene_work_branch`，
     与任务 env 同源），`os.environ['GRAPE_WORK_BRANCH']` 仅作可选覆盖；与转交快照不一致时
     提示派发时值优先并留痕。
   - `build_devops_agent_prompt` 在 `agent_type == "issue"` 时注入该段。
4. `src/api/event_pipeline.py`
   - `_resolve_scene_id` 委托 `session_store.resolve_scene_id`；`_inject_scene_env` 改用
     `get_scene_advanced_options`，消除等价私有查询。

## 决策
| 决策 | 选择 | 理由 |
|------|------|------|
| 权威来源 | 承接场景 `work_branch` | 与运行时 `$GRAPE_WORK_BRANCH` 同源，coding 实际基线一致 |
| 来源校验 | `target_branch_source` 必须匹配 | 防止旧 carried_context 残留源会话分支被误标权威 |
| 覆盖策略 | 承接值覆盖源值并留痕 | 双侧防单点缺失，可追溯 |
| 快照漂移 | 派发时现读值优先并留痕 | 转交后场景 work_branch 变更时以 coding 实际基线为准 |
| 派发时基线取值 | `session_store.get_scene_work_branch`（env 可选覆盖） | API 进程不持有任务级 `GRAPE_WORK_BRANCH`，直接读 env 会恒空 |
| 多跳失效 | 清空 `target_branch` 权威标记 + 清除上一跳 `target_branch_replaced` | 避免 prompt / A2A 继续宣称旧分支「已刷新」 |
| 同基线 | 不改写、仅核验 | 避免无意义变更 |

## 风险
- 承接场景未配置 `work_branch`：不写入权威字段，prompt 与 A2A 均标注 UNCONFIRMED（不宣称为权威）。
- 历史 carried_context 无 target_branch：prompt 指引不注入，零影响。
- 长 A2A 上下文：基线声明前置 + 独立字段，截断不丢失。