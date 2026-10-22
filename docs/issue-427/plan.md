# Plan: Issue #427 实现方案

## 改动清单（src/api/event_pipeline.py + src/api/routes/events.py）

### 1. `_inject_scene_env`（event_pipeline.py）

签名追加 `scene_id_override: str = ""`；场景解析改为：

```python
scene_id = scene_id_override or _resolve_scene_id(repo_key, issue_id)
```

- override 仅在非空时生效；空串/未传 → 回退现有解析，零破坏
- GRAPE_SCENE_ID 与 custom_fields 注入共用该 scene_id（后续代码不动）
- docstring 更新说明 PR 订阅场景（Issue #427）用途

### 2. `_enqueue_pipeline_task`（event_pipeline.py，env 组装处 L2578）

```python
_inject_scene_env(env_extra, repo_key, issue_id, agent_type,
                  scene_id_override=evt.get("_pr_sub_scene_id", ""))
```

覆盖主路由入队 + PR 订阅多场景补派（`_dispatch_pr_subscription_extra`）+ PR 订阅专用重派（`_dispatch_pr_subscription_redispatch`）三条经 `_enqueue_pipeline_task` 的路径（三者 evt 均带 `_pr_sub_scene_id`）。

### 3. `_execute_agent`（event_pipeline.py，direct 路径 env 组装处 L3409）

同 2 传 `scene_id_override=evt.get("_pr_sub_scene_id", "")`——PR 订阅 direct 派发点（`_dispatch_pr_subscription_extra` / `_dispatch_pr_subscription_redispatch` 的 direct 分支）evt 携带该键；普通事件 evt 无此键 → 空串，行为不变。

### 4. `build_task_env`（event_pipeline.py，重派共用）

签名追加 `scene_id_override: str = ""`，内部 `_inject_scene_env(env, repo_key, issue_id, agent_type, scene_id_override=scene_id_override)`。

调用方（src/api/routes/events.py）：
- redispatch 本机重试（L500）与换机重派（L867）：传 `scene_id_override=evt.get("scene_id") or ""`——`_load_event` 返回事件行，PR 订阅事件的 `scene_id` 列即订阅场景（`_scene_id_for_event` 写入）；普通事件为 Issue 场景，与现有解析同值，无行为变化
- 对话入队（L1449）：不传（保持默认空串，行为不变）

### 5. 测试（tests/test_scene_env.py 追加）

按 spec.md §5 四条路径追加用例（复用现有 seed helper，新增 `_seed_scene` 带 `BUILD0007_PASSWORD` 的订阅场景 + Issue 场景）：

- `test_pr_linked_issue_scene_override_injects_subscription_custom_fields`（路径一）
- `test_pr_no_issue_pseudo_id_override_matches_regex_resolution`（路径二）
- `test_plain_issue_task_no_override_unchanged`（路径三，含参数缺省不传）
- `test_build_task_env_scene_override_redispatch_path`（路径四，直接调 `build_task_env`）
- `test_redispatch_event_row_scene_id_passed_as_override`（路径四调用链：模拟 events.py redispatch 的取参口径，验证 `_load_event` 风格 dict 的 `scene_id` 能传入并生效）

## 提交计划

- commit 1：`feat(#427): _inject_scene_env 支持 scene_id_override，PR 订阅 env 按订阅场景注入`（event_pipeline.py + events.py）
- commit 2：`test(#427): 四条路径单元测试覆盖`（tests/test_scene_env.py）
- 文档：docs/issue-427/spec.md + plan.md（随 commit 1）

## 回归风险评估

- override 空串回退：普通 Issue 任务、direct 普通事件、对话入队零变化
- PR 无关联 Issue：override 值与伪 id 正则解析值相同（同源于 `evt._pr_sub_scene_id`），结果一致
- PR 有关联 Issue：GRAPE_SCENE_ID 从 Issue 场景修正为订阅场景（修复目标本身）；`GRAPE_WORK_BRANCH`/`GRAPE_LINKED_REPOS` 仍按 issue_id 解析——work_branch 场景一致性由 `_read_scene_work_branch` 的伪 id 正则（PR 无关联 Issue 时）与订阅场景 advanced_options（本修复后 env 侧）共同保证，PR 有关联 Issue 时 work_branch 仍按 Issue 场景（与 base 分支语义无冲突，PR 审查任务不依赖 worktree 基线）
