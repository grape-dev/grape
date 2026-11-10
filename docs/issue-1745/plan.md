# Plan: Issue #1745 实现方案

## 根因
- 普通事件重派把事件行 `scene_id` 无条件作为 `scene_id_override` 传入 `build_task_env`。跨场景转交后，
  事件行仍停留在源场景，而 Issue 归属场景已刷新为目标场景 → override 覆写真值，`GRAPE_SCENE_ID` 陈旧。
- worker 执行前拉取的 task env 只同步 AK/SK 与 session token，不校正 `GRAPE_SCENE_ID`，转交前入队/陈旧快照
  无兜底。

## 改动点

| # | 文件 | 改动 |
|---|------|------|
| 1 | `src/api/routes/events.py` | 新增 `_redispatch_scene_override(evt)`：仅 PR 订阅事件（伪 id `#pr_` / PR 事件类型 / `PR 订阅` 标题）保留事件行 scene 作 override；普通 Issue/A2A/评论事件返回空串，交由 `issue_id` 解析真值。两处重派调用点改用该 helper，并在丢弃陈旧 override 时记录双方取值日志。 |
| 2 | `src/api/event_pipeline.py` | 新增 `_issue_authoritative_scene`（`issues detail.scene_id` 同源，仅真实绑定不回退 dev_scene）、`scene_env_mismatch_diagnostic`（可操作诊断文案）、`_is_pr_subscription_task`、`reconcile_task_scene_env`（按 `agent_tasks → issue` 校正任务快照）。`_inject_scene_env` 对 override 与真实值不一致记录诊断日志（PR 订阅属预期）。 |
| 3 | `src/api/routes/runtime.py` | `GET /runtimes/{runtime_id}/tasks/{task_id}/env` 返回前调用 `reconcile_task_scene_env`，让 worker 以校正后的 `GRAPE_SCENE_ID` 覆盖任务快照。 |
| 4 | `help/usage/agent-environment-variables.md` | 固化口径：`GRAPE_SCENE_ID` 跨场景转交后以 `issues detail.scene_id` 为权威、执行前校正；不一致时按真实值重取节点后重试。 |
| 5 | `tests/test_issue_1745_scene_env_truth.py` | 新增：重派 override 选择、真值注入、PR 订阅兼容、执行前校正、诊断文案。 |
| 6 | `docs/issue-1745/{spec,plan,task}.md` | 交付文档。 |

## 关键语义
- **真值**：Issue 当前归属场景 = `get_issue_bound_scene`（issue_state / issue 单表），与 `issues detail.scene_id` 同源。
- **PR 订阅例外**（#427）：订阅场景可不同于关联 Issue 场景，必须保留 override；伪 id 本身编码订阅场景。
- **诊断不阻断**：不一致只输出诊断；仅真实场景确实无目标节点时由 runbook 进入待确认。
- **无额外 scenes 查询**：校正仅读 `agent_tasks`/`issue` 两表，不调用 `scenes detail`/`get_pipeline_config`。

## runbook（技能资产，跨仓协同交付）
`grape-dev/grape-skills` 的 issue runbook Step 5 建议口径：
1. 先取 `grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out`
   的 `scene_id` 作为权威值；
2. 权威值为空时再用 `$GRAPE_SCENE_ID` 兜底；
3. 两者不一致时输出诊断（注入值 / 真实值 / 按真实值 `scenes detail` 重取节点重试），不直接 BLOCK；
4. 仅真实场景确实不含目标节点时才进入待确认。

## 回归范围
- `tests/test_scene_env.py`（#427 PR 订阅 override）
- `tests/test_issue_568_scene_visibility_unification.py`（scenes detail / cross scene handoff）
- `tests/test_issue_990_session_token_snapshot.py`（task env 快照）